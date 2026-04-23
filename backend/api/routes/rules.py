import re
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import require_admin
from backend.core.logger import LOG
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
)


class ValidateRegexRequest(BaseModel):
    base_path: str = ""
    suffix: str = ""


class ValidateRegexResponse(BaseModel):
    valid: bool
    error: str | None = None
    pattern: str | None = None


router = APIRouter(prefix="/api", tags=["rules"])


def _split_ancestors(path: str) -> list[str]:
    """Return all ancestor directory paths for ``path`` (including the path itself).

    The returned list is ordered from the shallowest ancestor to the full path.
    Handles both POSIX ("/media/movies/...") and Windows-style
    ("C:/media/movies/...") absolute paths. Backslashes are normalized to
    forward slashes so the tree can be built consistently.
    """
    if not path:
        return []
    norm = path.replace("\\", "/").rstrip("/")
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
    if media_type == MediaType.MOVIE:
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

    normalized_media = [mp.replace("\\", "/").lower() for mp in media_paths]

    cleaned: list[str] = []
    for pattern in paths:
        pattern = (pattern or "").strip()
        if not pattern:
            continue

        # Validate regex syntax (patterns should be pre-validated, but double-check)
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex pattern '{pattern}': {e}",
            )

        # Check if pattern matches any media path
        if not any(regex.search(mp) for mp in normalized_media):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Path '{pattern}' does not match any indexed media location."),
            )
        cleaned.append(pattern)
    return cleaned or None


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


@router.get("/rules", response_model=list[CleanupRuleResponse])
async def get_rules(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Get all cleanup rules."""
    result = await db.execute(select(ReclaimRule))
    rules = result.scalars().all()
    return rules


@router.post(
    "/rules", response_model=CleanupRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_rule(
    rule_data: CleanupRuleCreate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new cleanup rule."""
    cleaned_paths = await _validate_rule_paths(
        db, rule_data.paths, rule_data.media_type, rule_data.library_ids
    )
    new_rule = ReclaimRule(
        name=rule_data.name,
        media_type=rule_data.media_type,
        enabled=rule_data.enabled,
        library_ids=rule_data.library_ids,
        min_popularity=rule_data.min_popularity,
        max_popularity=rule_data.max_popularity,
        min_vote_average=rule_data.min_vote_average,
        max_vote_average=rule_data.max_vote_average,
        min_vote_count=rule_data.min_vote_count,
        max_vote_count=rule_data.max_vote_count,
        min_view_count=rule_data.min_view_count,
        max_view_count=rule_data.max_view_count,
        include_never_watched=rule_data.include_never_watched,
        min_days_since_added=rule_data.min_days_since_added,
        max_days_since_added=rule_data.max_days_since_added,
        min_days_since_last_watched=rule_data.min_days_since_last_watched,
        max_days_since_last_watched=rule_data.max_days_since_last_watched,
        min_size=rule_data.min_size,
        max_size=rule_data.max_size,
        paths=cleaned_paths,
        series_status=rule_data.series_status,
    )

    db.add(new_rule)
    await db.commit()
    await db.refresh(new_rule)

    LOG.info(f"Created cleanup rule: {new_rule.name} (ID: {new_rule.id})")
    return new_rule


@router.post("/rules/validate-regex", response_model=ValidateRegexResponse)
async def validate_regex_pattern(
    body: ValidateRegexRequest,
    _admin: Annotated[User, Depends(require_admin)],
) -> ValidateRegexResponse:
    """Validate and construct a regex pattern from base_path and suffix.
    The base_path is treated as a literal string (escaped), while the suffix
    can contain regex patterns. Returns the valid/complete pattern on success.
    """
    # Normalize both paths
    normalized_base = (body.base_path or "").replace("\\", "/").lower().rstrip("/")
    normalized_suffix = (body.suffix or "").replace("\\", "/").lower().rstrip("/")

    # Escape the base path to treat it as a literal string
    escaped_base = re.escape(normalized_base)

    # Combine escaped base with suffix, anchored to start of string
    combined = f"""^{escaped_base if not normalized_suffix else f"{escaped_base}/{normalized_suffix}"}"""

    # Validate the combined pattern
    try:
        re.compile(combined, re.IGNORECASE)
        return ValidateRegexResponse(valid=True, pattern=combined)
    except re.error as e:
        return ValidateRegexResponse(valid=False, error=str(e))


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

    # validate paths against the effective (post-update) media_type/library_ids
    if "paths" in update_data:
        effective_media_type = update_data.get("media_type", rule.media_type)
        effective_library_ids = (
            update_data["library_ids"]
            if "library_ids" in update_data
            else rule.library_ids
        )
        update_data["paths"] = await _validate_rule_paths(
            db,
            update_data.get("paths"),
            effective_media_type,
            effective_library_ids,
        )

    for field, value in update_data.items():
        setattr(rule, field, value)

    await db.commit()
    await db.refresh(rule)

    LOG.info(f"Updated cleanup rule: {rule.name} (ID: {rule.id})")
    return rule


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


# @router.post("/rules/check-library-impact")
# async def check_library_impact(
#     library_ids: list[str],
#     _admin: Annotated[User, Depends(require_admin)],
#     db: AsyncSession = Depends(get_db),
# ):
#     """Check which rules would be affected by deselecting the given library IDs."""
#     result = await db.execute(select(ReclaimRule))
#     all_rules = result.scalars().all()

#     affected_rules = []
#     for rule in all_rules:
#         if rule.library_ids:
#             # Check if any of the rule's library_ids match the ones being deselected
#             if any(lib_id in library_ids for lib_id in rule.library_ids):
#                 affected_rules.append(
#                     {
#                         "id": rule.id,
#                         "name": rule.name,
#                         "media_type": rule.media_type,
#                         "affected_library_ids": [
#                             lib_id for lib_id in rule.library_ids if lib_id in library_ids
#                         ],
#                     }
#                 )

#     return {
#         "affected_count": len(affected_rules),
#         "rules": affected_rules,
#     }
