import re
from collections import defaultdict
from os import PathLike
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.candidate_views import build_rule_preview_items
from backend.core.auth import require_admin
from backend.core.logger import LOG
from backend.core.rule_engine import (
    TARGET_MOVIE_VERSION,
    collect_rule_path_patterns,
    derive_path_scope_library_ids,
    normalize_rule_definition,
    normalize_rule_target,
    validate_rule_definition,
)
from backend.core.utils.filesystem import normalize_fpath
from backend.database import get_db
from backend.database.models import (
    Movie,
    MovieVersion,
    ReclaimRule,
    Series,
    SeriesServiceRef,
    ServiceMediaLibrary,
    User,
)
from backend.enums import MediaType
from backend.models.cleanup import (
    CleanupRuleCreate,
    CleanupRuleResponse,
    CleanupRuleUpdate,
    RuleImportPayload,
    RuleImportResponse,
)
from backend.models.media import PaginatedRulePreviewResponse
from backend.models.rules import (
    RulePreviewRequest,
    SeerrUserLookupResponse,
    ValidatePathsRequest,
    ValidatePathsResponse,
    ValidateRegexRequest,
    ValidateRegexResponse,
)
from backend.services.admin_notices import reconcile_stale_library_notice
from backend.services.seerr_cache import seerr_snapshot_cache
from backend.tasks.cleanup import collect_rule_preview_matches

router = APIRouter(prefix="/api", tags=["rules"])


def _media_type_for_target(target_scope: str | None, fallback: MediaType) -> MediaType:
    """Determine the media type based on the target scope."""
    if target_scope == TARGET_MOVIE_VERSION:
        return MediaType.MOVIE
    if target_scope:
        return MediaType.SERIES
    return fallback


def _action_or_default(action: dict | None) -> dict:
    """Return the action dictionary with default values applied."""
    return {
        "candidate": True,
        "tag_enabled": True,
        "arr_tag": None,
        "arr_action": "delete",
        "media_server_action": "delete",
        "radarr_service_config_id": None,
        "sonarr_service_config_id": None,
        **(action or {}),
    }


def _slugify_rule_tag(value: str) -> str:
    """We want *arr tags to be a slug (alphanumeric + dashes, max 50 chars, prefixed with 'rec-')
    The character constraints are from the *arrs, the max length I couldn't find
    documentation or in the code - so I chose 50 as a good max."""
    slug = re.sub(r"[^a-zA-Z0-9-]", "", value.strip())
    ensure_50 = slug[:50]
    if ensure_50.startswith("rec-"):
        return ensure_50
    return f"rec-{slug or 'rule'}"


def _normalize_rule_action(
    action: dict | None, rule_name: str, target_scope: str | None
) -> dict:
    """Normalize the rule action dictionary."""
    normalized = _action_or_default(action)
    normalized["tag_enabled"] = bool(normalized.get("tag_enabled", True))
    normalized["arr_tag"] = _slugify_rule_tag(
        str(normalized.get("arr_tag") or rule_name)
    )
    if target_scope == TARGET_MOVIE_VERSION:
        normalized["sonarr_service_config_id"] = None
    else:
        normalized["radarr_service_config_id"] = None
    return normalized


def _rule_response(rule: ReclaimRule) -> CleanupRuleResponse:
    """Generate a CleanupRuleResponse from a ReclaimRule."""
    definition = normalize_rule_definition(rule)
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rule {rule.id} is missing a valid advanced definition",
        )
    target_scope = normalize_rule_target(rule)
    return CleanupRuleResponse.model_validate(
        {
            "id": rule.id,
            "name": rule.name,
            "media_type": rule.media_type,
            "enabled": rule.enabled,
            "target_scope": target_scope,
            "definition": definition,
            "action": _normalize_rule_action(rule.action, rule.name, target_scope),
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
        }
    )


async def _sync_stale_library_notice(db: AsyncSession) -> None:
    try:
        await reconcile_stale_library_notice(db)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        LOG.warning(f"Failed to sync stale-library notice state: {exc}")


def _split_ancestors(path: PathLike[str] | str) -> list[str]:
    """Return all ancestor directory paths for ``path`` (including the path itself).

    The returned list is ordered from the shallowest ancestor to the full path.
    Handles both POSIX ("/media/movies/...") and Windows-style
    ("C:/media/movies/...") absolute paths. Backslashes are normalized to
    forward slashes so the tree can be built consistently.
    """
    if not path:
        return []
    norm = normalize_fpath(path, strip_ending_slash=True) if path else None
    if not norm:
        return []
    if norm.startswith("/"):
        segments = [s for s in norm[1:].split("/") if s]
        parts: list[str] = []
        current = ""
        for seg in segments:
            current = f"{current}/{seg}"
            parts.append(current)
        return parts
    # non-absolute / windows-drive style
    segments = [s for s in norm.split("/") if s]
    if not segments:
        return []
    parts = [segments[0]]
    for seg in segments[1:]:
        parts.append(f"{parts[-1]}/{seg}")
    return parts


async def _collect_media_paths(
    db: AsyncSession,
    media_type: MediaType,
    library_ids: list[str] | None,
) -> list[str]:
    """Return all non-null media paths for the given media type and libraries."""
    if media_type is MediaType.MOVIE:
        stmt = select(MovieVersion.path).where(MovieVersion.path.is_not(None))
        if library_ids:
            stmt = stmt.where(MovieVersion.library_id.in_(library_ids))
    else:
        stmt = select(SeriesServiceRef.path).where(SeriesServiceRef.path.is_not(None))
        if library_ids:
            stmt = stmt.where(SeriesServiceRef.library_id.in_(library_ids))
    result = await db.execute(stmt)
    return [row[0] for row in result.all() if row[0]]


async def _validate_rule_paths(
    db: AsyncSession,
    paths: list[str] | None,
    media_type: MediaType,
    library_ids: list[str] | None,
) -> list[str] | None:
    """Normalize and validate that each path matches at least one indexed media file.

    Returns the cleaned list (or None if empty). Raises 400 if any path does
    not match any stored media path for the rule's media type / libraries, or if
    a regex pattern is invalid.
    """
    if not paths:
        return None

    media_paths = await _collect_media_paths(db, media_type, library_ids)
    if not media_paths:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No indexed media paths are available yet. Run a media sync "
                "before adding path criteria."
            ),
        )

    normalized_media = [normalize_fpath(mp, lower=True) for mp in media_paths if mp]

    cleaned: list[str] = []
    for pattern in paths:
        pattern = (pattern or "").strip()
        if not pattern:
            continue

        # validate regex syntax (patterns should be pre-validated, but double-check)
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex pattern '{pattern}': {e}",
            )

        # check if pattern matches any media path
        if not any(regex.search(mp) for mp in normalized_media):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Path '{pattern}' does not match any indexed media location."),
            )
        cleaned.append(pattern)
    return cleaned or None


async def _validate_definition_paths(
    db: AsyncSession,
    definition: dict | None,
    media_type: MediaType,
) -> None:
    await _validate_rule_paths(
        db,
        collect_rule_path_patterns(definition),
        media_type,
        derive_path_scope_library_ids(definition),
    )


@router.get("/rules/path-tree")
async def get_path_tree(
    _admin: Annotated[User, Depends(require_admin)],
    media_type: Annotated[MediaType, Query()],
    library_ids: Annotated[list[str] | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Return a navigable tree of directories derived from indexed media paths.

    Each node has ``path``, ``name``, and ``children``. Roots are the
    shallowest directories observed across all media paths.
    """
    media_paths = await _collect_media_paths(db, media_type, library_ids)

    all_paths: set[str] = set()
    children: dict[str, set[str]] = defaultdict(set)

    for raw in media_paths:
        ancestors = _split_ancestors(raw)
        if not ancestors:
            continue
        # exclude the file itself (last ancestor) - only include directories
        dir_ancestors = ancestors[:-1]
        if not dir_ancestors:
            continue
        # add all directory ancestors to all_paths
        all_paths.update(dir_ancestors)
        # link all parent -> child relationships for directories
        for parent, child in zip(dir_ancestors, dir_ancestors[1:]):
            children[parent].add(child)

    child_set = {c for kids in children.values() for c in kids}
    roots = sorted(all_paths - child_set)

    def build_node(p: str) -> dict:
        kids = sorted(children.get(p, set()))
        name = p.rsplit("/", 1)[-1] or p
        return {
            "path": p,
            "name": name,
            "children": [build_node(k) for k in kids],
        }

    return [build_node(r) for r in roots]


@router.get("/rules/seerr-users", response_model=list[SeerrUserLookupResponse])
async def get_seerr_users(
    _admin: Annotated[User, Depends(require_admin)],
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
) -> list[SeerrUserLookupResponse]:
    """Return cached Seerr users for requester rule picker."""
    users = await seerr_snapshot_cache.get_users()
    if not users:
        # bypass cache retry to recover from transient empty states if needed
        users = await seerr_snapshot_cache.get_users(force_refresh=True)

    response_users = [
        SeerrUserLookupResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
        )
        for user in users
    ]
    needle = q.strip().lower()
    if needle:
        response_users = [
            user
            for user in response_users
            if needle in str(user.id)
            or needle in (user.username or "").lower()
            or needle in (user.display_name or "").lower()
        ]
    return response_users[:limit]


@router.get("/rules", response_model=list[CleanupRuleResponse])
async def get_rules(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Get all cleanup rules."""
    result = await db.execute(select(ReclaimRule))
    rules = result.scalars().all()
    return [_rule_response(rule) for rule in rules]


@router.post(
    "/rules", response_model=CleanupRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_rule(
    rule_data: CleanupRuleCreate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new cleanup rule."""
    if not rule_data.target_scope:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Rules require target_scope",
        )
    try:
        validate_rule_definition(rule_data.definition)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e

    effective_media_type = _media_type_for_target(
        rule_data.target_scope, rule_data.media_type
    )
    await _validate_definition_paths(db, rule_data.definition, effective_media_type)
    new_rule = ReclaimRule(
        name=rule_data.name,
        media_type=effective_media_type,
        enabled=rule_data.enabled,
        target_scope=rule_data.target_scope,
        definition=rule_data.definition,
        action=_normalize_rule_action(
            rule_data.action, rule_data.name, rule_data.target_scope
        ),
    )

    db.add(new_rule)
    await db.commit()
    await db.refresh(new_rule)
    await _sync_stale_library_notice(db)

    LOG.info(f"Created cleanup rule: {new_rule.name} (ID: {new_rule.id})")
    return _rule_response(new_rule)


@router.post("/rules/validate-regex", response_model=ValidateRegexResponse)
async def validate_regex_pattern(
    body: ValidateRegexRequest,
    _admin: Annotated[User, Depends(require_admin)],
) -> ValidateRegexResponse:
    """Validate and construct a regex pattern from base_path and suffix.
    The base_path is treated as a literal string (escaped), while the suffix
    can contain regex patterns. Returns the valid/complete pattern on success.
    """
    # normalize both paths
    normalized_base = normalize_fpath(
        body.base_path, lower=True, strip_ending_slash=True
    )
    normalized_suffix = normalize_fpath(
        body.suffix, lower=True, strip_ending_slash=True
    )

    # escape the base path to treat it as a literal string
    escaped_base = re.escape(normalized_base)

    # combine escaped base with suffix, anchored to start of string
    combined = f"""^{escaped_base if not normalized_suffix else f"{escaped_base}/{normalized_suffix}"}"""

    # validate the combined pattern
    try:
        re.compile(combined, re.IGNORECASE)
        return ValidateRegexResponse(valid=True, pattern=combined)
    except re.error as e:
        return ValidateRegexResponse(valid=False, error=str(e))


@router.post("/rules/validate-paths", response_model=ValidatePathsResponse)
async def validate_paths_against_scope(
    body: ValidatePathsRequest,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> ValidatePathsResponse:
    """Validate path patterns against indexed media paths for the given scope.

    Returns split lists so callers can offer confirm/prune UX without mutating
    backend state.
    """
    unique_paths: list[str] = []
    seen: set[str] = set()
    for raw in body.paths:
        path = (raw or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        unique_paths.append(path)

    if not unique_paths:
        return ValidatePathsResponse(valid_paths=[], invalid_paths=[])

    media_paths = await _collect_media_paths(db, body.media_type, body.library_ids)
    normalized_media = [normalize_fpath(mp, lower=True) for mp in media_paths if mp]

    valid_paths: list[str] = []
    invalid_paths: list[str] = []
    for pattern in unique_paths:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            invalid_paths.append(pattern)
            continue
        if normalized_media and any(regex.search(mp) for mp in normalized_media):
            valid_paths.append(pattern)
        else:
            invalid_paths.append(pattern)

    return ValidatePathsResponse(valid_paths=valid_paths, invalid_paths=invalid_paths)


@router.post("/rules/preview", response_model=PaginatedRulePreviewResponse)
async def preview_rule_matches(
    body: RulePreviewRequest,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> PaginatedRulePreviewResponse:
    """Dry-run an unsaved advanced rule without writing reclaim candidates."""
    if not body.target_scope:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Rules require target_scope",
        )
    try:
        validate_rule_definition(body.definition)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e

    effective_media_type = _media_type_for_target(body.target_scope, body.media_type)
    await _validate_definition_paths(db, body.definition, effective_media_type)

    preview_rule = ReclaimRule(
        name=(body.name or "").strip() or "Preview Rule",
        media_type=effective_media_type,
        enabled=True,
        target_scope=body.target_scope,
        definition=body.definition,
        action=_action_or_default(None),
    )

    matches = await collect_rule_preview_matches(db, [preview_rule])
    items = await build_rule_preview_items(db, matches)
    total = len(items)
    total_pages = (total + body.per_page - 1) // body.per_page if total else 0
    offset = (body.page - 1) * body.per_page
    page_items = items[offset : offset + body.per_page]

    return PaginatedRulePreviewResponse(
        items=page_items,
        total=total,
        page=body.page,
        per_page=body.per_page,
        total_pages=total_pages,
    )


@router.post("/rules/import", status_code=status.HTTP_200_OK)
async def import_rules(
    payload: RuleImportPayload,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> RuleImportResponse:
    """Bulk import cleanup rules, we will auto rename duplicates."""
    existing_names_result = await db.execute(select(ReclaimRule.name))
    used_names: set[str] = set(existing_names_result.scalars().all())
    imported = 0
    errors: list[str] = []

    for rule_data in payload.rules:
        try:
            if not rule_data.target_scope:
                raise ValueError("Rules require target_scope")
            try:
                validate_rule_definition(rule_data.definition)
            except ValueError as e:
                raise ValueError(str(e)) from e

            name = rule_data.name
            if name in used_names:
                candidate = f"{name} (imported)"
                n = 2
                while candidate in used_names:
                    candidate = f"{name} (imported {n})"
                    n += 1
                name = candidate

            effective_media_type = _media_type_for_target(
                rule_data.target_scope, rule_data.media_type
            )
            new_rule = ReclaimRule(
                name=name,
                media_type=effective_media_type,
                enabled=rule_data.enabled,
                target_scope=rule_data.target_scope,
                definition=rule_data.definition,
                action=_normalize_rule_action(
                    rule_data.action, name, rule_data.target_scope
                ),
            )
            db.add(new_rule)
            used_names.add(name)
            imported += 1
        except Exception as e:
            errors.append(f"{rule_data.name}: {e}")

    if imported:
        await db.commit()
        await _sync_stale_library_notice(db)
        LOG.info(f"Imported {imported} cleanup rule(s)")

    return RuleImportResponse(imported=imported, errors=errors)


@router.post("/rules/{rule_id}", response_model=CleanupRuleResponse)
async def update_rule(
    rule_id: int,
    rule_data: CleanupRuleUpdate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Updates an existing cleanup rule."""
    result = await db.execute(select(ReclaimRule).where(ReclaimRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with ID {rule_id} not found",
        )

    # update only the fields that were provided
    update_data = rule_data.model_dump(exclude_unset=True)
    if "definition" in update_data and update_data["definition"] is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Rules require definition",
        )
    if "definition" in update_data and update_data["definition"] is not None:
        try:
            validate_rule_definition(update_data["definition"])
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
            ) from e
    if "target_scope" in update_data and not update_data["target_scope"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Rules require target_scope",
        )

    if "target_scope" in update_data:
        update_data["media_type"] = _media_type_for_target(
            update_data["target_scope"], update_data.get("media_type", rule.media_type)
        )
    if (
        "action" in update_data
        or "name" in update_data
        or "target_scope" in update_data
    ):
        update_data["action"] = _normalize_rule_action(
            update_data.get("action", rule.action),
            update_data.get("name", rule.name),
            update_data.get("target_scope", rule.target_scope),
        )

    if (
        "definition" in update_data
        or "target_scope" in update_data
        or "media_type" in update_data
    ):
        effective_target_scope = update_data.get("target_scope", rule.target_scope)
        effective_media_type = _media_type_for_target(
            effective_target_scope,
            update_data.get("media_type", rule.media_type),
        )
        await _validate_definition_paths(
            db,
            update_data.get("definition", rule.definition),
            effective_media_type,
        )

    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)
    await _sync_stale_library_notice(db)

    LOG.info(f"Updated cleanup rule: {rule.name} (ID: {rule.id})")
    return _rule_response(rule)


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Remove a cleanup rule."""
    result = await db.execute(select(ReclaimRule).where(ReclaimRule.id == rule_id))
    rule = result.scalar_one_or_none()

    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule with ID {rule_id} not found",
        )

    rule_name = rule.name
    await db.delete(rule)
    await db.commit()
    await _sync_stale_library_notice(db)

    msg = f"Deleted cleanup rule: {rule_name} (ID: {rule_id})"
    LOG.info(msg)
    return {"message": msg}


@router.get("/rules/check-synced")
async def check_synced_rules(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Return the count of synced libraries, movies, and series."""
    lib_count = await db.scalar(select(func.count(ServiceMediaLibrary.id)))
    movie_count = await db.scalar(select(func.count(Movie.id)))
    series_count = await db.scalar(select(func.count(Series.id)))
    return {
        "libraries": lib_count or 0,
        "movies": movie_count or 0,
        "series": series_count or 0,
    }
