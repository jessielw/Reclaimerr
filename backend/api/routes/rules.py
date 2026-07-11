import re
from collections import defaultdict
from dataclasses import dataclass
from os import PathLike
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.candidate_views import build_rule_preview_items
from backend.core.auth import require_admin
from backend.core.logger import LOG
from backend.core.rule_engine import (
    RULE_OUTCOME_CANDIDATE,
    RULE_OUTCOME_PROTECT,
    TARGET_MOVIE_VERSION,
    collect_rule_path_conditions,
    derive_path_scope_library_ids,
    normalize_rule_definition,
    normalize_rule_target,
    validate_rule_definition,
)
from backend.core.utils.filesystem import normalize_fpath
from backend.core.utils.language import language_name, normalize_language
from backend.core.utils.misc import normalize_genre_names, normalize_name_list
from backend.database import get_db
from backend.database.models import (
    Movie,
    MovieVersion,
    PlaybackHistoryEvent,
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
from backend.models.media import PaginatedRulePreviewResponse, RulePreviewMetadata
from backend.models.rules import (
    GenreLookupResponse,
    MediaServerCollectionLookupResponse,
    MetadataValueLookupResponse,
    MovieCollectionLookupResponse,
    PaginatedGenresResponse,
    PaginatedMediaServerCollectionsResponse,
    PaginatedMetadataValuesResponse,
    PaginatedMovieCollectionsResponse,
    PlaybackUserLookupResponse,
    RulePreviewRequest,
    SeerrUserLookupResponse,
    ValidatePathCondition,
    ValidatePathsRequest,
    ValidatePathsResponse,
    ValidateRegexRequest,
    ValidateRegexResponse,
)
from backend.services.admin_notices import reconcile_stale_library_notice
from backend.services.seerr_cache import seerr_snapshot_cache
from backend.tasks.cleanup import collect_rule_preview_matches_with_metadata

router = APIRouter(prefix="/api", tags=["rules"])

PathValidationField = Literal["media.path", "media.file_name"]

PATH_VALIDATION_FIELDS: set[PathValidationField] = {"media.path", "media.file_name"}
PATH_INCLUDE_OPERATORS = {"equals", "in", "contains_any", "contains_all"}
PATH_REGEX_OPERATOR = "matches_any_regex"
PATH_VALIDATION_OPERATORS = PATH_INCLUDE_OPERATORS | {PATH_REGEX_OPERATOR}


@dataclass(frozen=True, slots=True)
class _PathCriterion:
    field: PathValidationField
    operator: str
    value: str


@dataclass(frozen=True, slots=True)
class _InvalidPathCriterion:
    criterion: _PathCriterion
    reason: str


def _media_type_for_target(target_scope: str | None, fallback: MediaType) -> MediaType:
    """Determine the media type based on the target scope."""
    if target_scope == TARGET_MOVIE_VERSION:
        return MediaType.MOVIE
    if target_scope:
        return MediaType.SERIES
    return fallback


def _action_or_default(action: dict[str, Any] | None) -> dict[str, Any]:
    """Return the action dictionary with default values applied."""
    return {
        "outcome": RULE_OUTCOME_CANDIDATE,
        "candidate": True,
        "tag_enabled": True,
        "arr_tag": None,
        "arr_action": "delete",
        "media_server_action": "delete",
        "auto_delete_enabled": False,
        "auto_delete_delay_days": None,
        "move_instead_of_delete": False,
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
    action: dict[str, Any] | None, rule_name: str, target_scope: str | None
) -> dict[str, Any]:
    """Normalize the rule action dictionary."""
    normalized = _action_or_default(action)
    outcome = (
        RULE_OUTCOME_PROTECT
        if normalized.get("outcome") == RULE_OUTCOME_PROTECT
        else RULE_OUTCOME_CANDIDATE
    )
    normalized["outcome"] = outcome
    normalized["candidate"] = outcome == RULE_OUTCOME_CANDIDATE
    normalized["tag_enabled"] = bool(normalized.get("tag_enabled", True))
    normalized["auto_delete_enabled"] = normalized.get("auto_delete_enabled") is True
    normalized["move_instead_of_delete"] = (
        normalized.get("move_instead_of_delete") is True
    )
    raw_auto_delete_delay = normalized.get("auto_delete_delay_days")
    normalized["auto_delete_delay_days"] = (
        raw_auto_delete_delay
        if isinstance(raw_auto_delete_delay, int)
        and not isinstance(raw_auto_delete_delay, bool)
        and 0 <= raw_auto_delete_delay <= 3650
        else None
    )
    normalized["arr_tag"] = _slugify_rule_tag(
        str(normalized.get("arr_tag") or rule_name)
    )
    if target_scope == TARGET_MOVIE_VERSION:
        normalized["sonarr_service_config_id"] = None
    else:
        normalized["radarr_service_config_id"] = None
    if outcome == RULE_OUTCOME_PROTECT:
        normalized["tag_enabled"] = False
        normalized["arr_tag"] = None
        normalized["arr_action"] = "delete"
        normalized["media_server_action"] = None
        normalized["auto_delete_enabled"] = False
        normalized["auto_delete_delay_days"] = None
        normalized["move_instead_of_delete"] = False
        normalized["radarr_service_config_id"] = None
        normalized["sonarr_service_config_id"] = None
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


def _normalize_path_criterion(
    field: str,
    operator: str,
    value: str,
) -> _PathCriterion | None:
    normalized_field = str(field or "").strip()
    if normalized_field == "media.path":
        field_value: PathValidationField = "media.path"
    elif normalized_field == "media.file_name":
        field_value = "media.file_name"
    else:
        return None
    normalized_operator = str(operator or "").strip().lower()
    if not normalized_operator:
        return None
    normalized_value = str(value or "").strip()
    if not normalized_value:
        return None
    return _PathCriterion(
        field=field_value,
        operator=normalized_operator,
        value=normalized_value,
    )


def _collect_unique_path_criteria(
    *,
    conditions: list[ValidatePathCondition] | None = None,
    legacy_paths: list[str] | None = None,
    criteria: list[_PathCriterion] | None = None,
) -> list[_PathCriterion]:
    unique: list[_PathCriterion] = []
    seen: set[tuple[str, str, str]] = set()

    for criterion in criteria or []:
        key = (criterion.field, criterion.operator, criterion.value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(criterion)

    for condition in conditions or []:
        normalized = _normalize_path_criterion(
            condition.field, condition.operator, condition.value
        )
        if normalized is None:
            continue
        key = (normalized.field, normalized.operator, normalized.value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)

    for raw_path in legacy_paths or []:
        normalized = _normalize_path_criterion(
            "media.path",
            PATH_REGEX_OPERATOR,
            raw_path,
        )
        if normalized is None:
            continue
        key = (normalized.field, normalized.operator, normalized.value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)

    return unique


def _normalize_criterion_value(field: str, value: str) -> str:
    if field == "media.path":
        return normalize_fpath(value, lower=True)
    return str(value).strip().lower()


def _matches_path_prefix(candidate: str, expected: str) -> bool:
    expected_prefix = normalize_fpath(expected, lower=True, strip_ending_slash=True)
    normalized_candidate = normalize_fpath(candidate, lower=True)
    if not expected_prefix or not normalized_candidate:
        return False
    return normalized_candidate == expected_prefix or normalized_candidate.startswith(
        f"{expected_prefix}/"
    )


def _index_media_path_values(media_paths: list[str]) -> dict[str, set[str]]:
    indexed_paths: set[str] = set()
    indexed_filenames: set[str] = set()
    for raw_path in media_paths:
        normalized = normalize_fpath(raw_path, lower=True)
        if not normalized:
            continue
        indexed_paths.add(normalized)
        file_name = normalized.rsplit("/", 1)[-1].strip()
        if file_name:
            indexed_filenames.add(file_name)
    return {
        "media.path": indexed_paths,
        "media.file_name": indexed_filenames,
    }


def _evaluate_path_criteria(
    criteria: list[_PathCriterion],
    indexed_values: dict[str, set[str]],
) -> tuple[list[_PathCriterion], list[_InvalidPathCriterion]]:
    valid: list[_PathCriterion] = []
    invalid: list[_InvalidPathCriterion] = []

    for criterion in criteria:
        if criterion.operator not in PATH_VALIDATION_OPERATORS:
            # non inclusion operators are intentionally skipped for existence checks.
            valid.append(criterion)
            continue

        candidates = indexed_values.get(criterion.field, set())
        if criterion.operator == PATH_REGEX_OPERATOR:
            try:
                regex = re.compile(criterion.value, re.IGNORECASE)
            except re.error:
                invalid.append(_InvalidPathCriterion(criterion, "invalid_regex"))
                continue
            if candidates and any(regex.search(candidate) for candidate in candidates):
                valid.append(criterion)
            else:
                invalid.append(_InvalidPathCriterion(criterion, "no_match"))
            continue

        expected = _normalize_criterion_value(criterion.field, criterion.value)
        if criterion.field == "media.path":
            has_match = candidates and any(
                _matches_path_prefix(candidate, expected) for candidate in candidates
            )
        else:
            has_match = candidates and expected in candidates
        if has_match:
            valid.append(criterion)
        else:
            invalid.append(_InvalidPathCriterion(criterion, "no_match"))

    return valid, invalid


async def _validate_rule_paths(
    db: AsyncSession,
    criteria: list[_PathCriterion] | None,
    media_type: MediaType,
    library_ids: list[str] | None,
) -> None:
    """Validate path criteria against indexed media paths in the requested scope."""
    if not criteria:
        return None

    criteria_to_validate = [
        criterion
        for criterion in criteria
        if criterion.operator in PATH_VALIDATION_OPERATORS
    ]
    if not criteria_to_validate:
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

    _, invalid = _evaluate_path_criteria(
        criteria_to_validate, _index_media_path_values(media_paths)
    )
    if not invalid:
        return None

    first = invalid[0]
    if first.reason == "invalid_regex":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid regex pattern '{first.criterion.value}'",
        )

    label = "Path" if first.criterion.field == "media.path" else "Filename"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"{label} '{first.criterion.value}' does not match any indexed media "
            "location."
        ),
    )


async def _validate_definition_paths(
    db: AsyncSession,
    definition: dict[str, Any] | None,
    media_type: MediaType,
) -> None:
    raw_criteria: list[_PathCriterion] = []
    for condition in collect_rule_path_conditions(definition):
        criterion = _normalize_path_criterion(
            condition["field"],
            condition["operator"],
            condition["value"],
        )
        if criterion is not None:
            raw_criteria.append(criterion)

    definition_criteria = _collect_unique_path_criteria(criteria=raw_criteria)
    await _validate_rule_paths(
        db,
        definition_criteria,
        media_type,
        derive_path_scope_library_ids(definition),
    )


def _validate_definition_path_syntax(definition: dict[str, Any] | None) -> None:
    """Validate path criteria in a rule definition without requiring indexed media."""
    for condition in collect_rule_path_conditions(definition):
        criterion = _normalize_path_criterion(
            condition["field"],
            condition["operator"],
            condition["value"],
        )
        if criterion is None or criterion.operator != PATH_REGEX_OPERATOR:
            continue
        try:
            re.compile(criterion.value, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid regex pattern '{criterion.value}'",
            ) from exc


@router.get("/rules/path-tree")
async def get_path_tree(
    _admin: Annotated[User, Depends(require_admin)],
    media_type: Annotated[MediaType, Query()],
    library_ids: Annotated[list[str] | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
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

    def build_node(p: str) -> dict[str, Any]:
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


@router.get("/rules/playback-users", response_model=list[PlaybackUserLookupResponse])
async def get_playback_users(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PlaybackUserLookupResponse]:
    """Return distinct usernames resolved from retained playback events."""

    rows = (
        await db.execute(
            select(
                PlaybackHistoryEvent.source_username,
                PlaybackHistoryEvent.source_service,
            ).where(PlaybackHistoryEvent.source_username.is_not(None))
        )
    ).all()
    by_username: dict[str, PlaybackUserLookupResponse] = {}
    for raw_username, source_service in rows:
        username = str(raw_username or "").strip()
        normalized = username.casefold()
        if not normalized:
            continue
        existing = by_username.get(normalized)
        source = source_service.value
        if existing is None:
            by_username[normalized] = PlaybackUserLookupResponse(
                username=username,
                source_services=[source],
            )
        elif source not in existing.source_services:
            existing.source_services.append(source)

    needle = q.strip().casefold()
    users = [
        user
        for normalized, user in by_username.items()
        if not needle or needle in normalized
    ]
    users.sort(key=lambda user: user.username.casefold())
    for user in users:
        user.source_services.sort()
    return users[:limit]


@router.get(
    "/rules/movie-collections",
    response_model=PaginatedMovieCollectionsResponse,
)
async def get_movie_collections(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaginatedMovieCollectionsResponse:
    """Return paginated movie collection names sourced from local active movies."""
    trimmed_name = func.trim(Movie.tmdb_collection_name)
    normalized_name = func.lower(trimmed_name)

    grouped = select(
        normalized_name.label("name_key"),
        func.min(trimmed_name).label("name"),
        func.count(Movie.id).label("movie_count"),
    ).where(
        Movie.removed_at.is_(None),
        Movie.tmdb_collection_id.is_not(None),
        Movie.tmdb_collection_name.is_not(None),
        func.length(trimmed_name) > 0,
    )

    needle = q.strip().lower()
    if needle:
        grouped = grouped.where(normalized_name.contains(needle))

    grouped = grouped.group_by(normalized_name)
    grouped_subquery = grouped.subquery()

    total = await db.scalar(select(func.count()).select_from(grouped_subquery)) or 0
    total_pages = (total + per_page - 1) // per_page if total else 0
    offset = (page - 1) * per_page

    rows = await db.execute(
        select(
            grouped_subquery.c.name,
            grouped_subquery.c.movie_count,
        )
        .order_by(grouped_subquery.c.name.asc())
        .offset(offset)
        .limit(per_page)
    )
    items = [
        MovieCollectionLookupResponse(
            name=str(row.name or "").strip(),
            movie_count=int(row.movie_count or 0),
        )
        for row in rows.all()
        if str(row.name or "").strip()
    ]

    return PaginatedMovieCollectionsResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get(
    "/rules/genres",
    response_model=PaginatedGenresResponse,
)
async def get_genres(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    media_type: MediaType = MediaType.MOVIE,
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaginatedGenresResponse:
    """Return paginated genre names sourced from local active media."""
    model = Movie if media_type is MediaType.MOVIE else Series
    rows = await db.execute(
        select(model.genres).where(
            model.removed_at.is_(None),
            model.genres.is_not(None),
        )
    )

    needle = q.strip().lower()
    counts: dict[str, tuple[str, int]] = {}
    for (raw_genres,) in rows.all():
        seen_for_media: set[str] = set()
        for name in normalize_genre_names(raw_genres) or []:
            normalized = name.lower()
            if needle and needle not in normalized:
                continue
            if normalized in seen_for_media:
                continue
            seen_for_media.add(normalized)
            display_name, count = counts.get(normalized, (name, 0))
            counts[normalized] = (display_name, count + 1)

    sorted_items = sorted(
        counts.values(),
        key=lambda item: item[0].lower(),
    )
    total = len(sorted_items)
    total_pages = (total + per_page - 1) // per_page if total else 0
    offset = (page - 1) * per_page
    page_items = sorted_items[offset : offset + per_page]

    return PaginatedGenresResponse(
        items=[
            GenreLookupResponse(name=name, media_count=count)
            for name, count in page_items
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get(
    "/rules/original-languages",
    response_model=PaginatedMetadataValuesResponse,
)
async def get_original_languages(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    media_type: MediaType = MediaType.MOVIE,
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaginatedMetadataValuesResponse:
    """Return canonical original languages sourced from active local media."""
    model = Movie if media_type is MediaType.MOVIE else Series
    rows = await db.execute(
        select(model.original_language).where(
            model.removed_at.is_(None),
            model.original_language.is_not(None),
        )
    )

    needle = q.strip().lower()
    counts: dict[str, tuple[str, int]] = {}
    for (raw_language,) in rows.all():
        value = normalize_language(raw_language)
        if value is None:
            continue
        name = language_name(value) or value
        if needle and needle not in value.lower() and needle not in name.lower():
            continue
        _, count = counts.get(value, (name, 0))
        counts[value] = (name, count + 1)

    sorted_items = sorted(counts.items(), key=lambda item: item[1][0].lower())
    total = len(sorted_items)
    total_pages = (total + per_page - 1) // per_page if total else 0
    offset = (page - 1) * per_page
    page_items = sorted_items[offset : offset + per_page]
    return PaginatedMetadataValuesResponse(
        items=[
            MetadataValueLookupResponse(
                value=value,
                name=name,
                media_count=count,
            )
            for value, (name, count) in page_items
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get(
    "/rules/origin-countries",
    response_model=PaginatedMetadataValuesResponse,
)
async def get_origin_countries(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    media_type: MediaType = MediaType.MOVIE,
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaginatedMetadataValuesResponse:
    """Return origin-country codes sourced from active local media."""
    model = Movie if media_type is MediaType.MOVIE else Series
    rows = await db.execute(
        select(model.origin_country).where(
            model.removed_at.is_(None),
            model.origin_country.is_not(None),
        )
    )

    needle = q.strip().lower()
    counts: dict[str, int] = {}
    for (raw_countries,) in rows.all():
        seen_for_media: set[str] = set()
        for raw_country in raw_countries or []:
            value = str(raw_country or "").strip().upper()
            if not value or value in seen_for_media:
                continue
            seen_for_media.add(value)
            if needle and needle not in value.lower():
                continue
            counts[value] = counts.get(value, 0) + 1

    sorted_items = sorted(counts.items())
    total = len(sorted_items)
    total_pages = (total + per_page - 1) // per_page if total else 0
    offset = (page - 1) * per_page
    page_items = sorted_items[offset : offset + per_page]
    return PaginatedMetadataValuesResponse(
        items=[
            MetadataValueLookupResponse(
                value=value,
                name=value,
                media_count=count,
            )
            for value, count in page_items
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get(
    "/rules/media-server-collections",
    response_model=PaginatedMediaServerCollectionsResponse,
)
async def get_media_server_collections(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    media_type: MediaType = MediaType.MOVIE,
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PaginatedMediaServerCollectionsResponse:
    """Return paginated media-server collection names from active local media."""
    if media_type is MediaType.MOVIE:
        rows = await db.execute(
            select(Movie.id, MovieVersion.media_server_collection_names)
            .join(MovieVersion, MovieVersion.movie_id == Movie.id)
            .where(
                Movie.removed_at.is_(None),
                MovieVersion.media_server_collection_names.is_not(None),
            )
        )
    else:
        rows = await db.execute(
            select(Series.id, SeriesServiceRef.media_server_collection_names)
            .join(SeriesServiceRef, SeriesServiceRef.series_id == Series.id)
            .where(
                Series.removed_at.is_(None),
                SeriesServiceRef.media_server_collection_names.is_not(None),
            )
        )

    needle = q.strip().lower()
    media_ids_by_name: dict[str, tuple[str, set[int]]] = {}
    for media_id, raw_names in rows.all():
        for name in normalize_name_list(raw_names) or []:
            normalized = name.lower()
            if needle and needle not in normalized:
                continue
            display_name, media_ids = media_ids_by_name.get(normalized, (name, set()))
            media_ids.add(int(media_id))
            media_ids_by_name[normalized] = (display_name, media_ids)

    sorted_items = sorted(
        media_ids_by_name.values(),
        key=lambda item: item[0].lower(),
    )
    total = len(sorted_items)
    total_pages = (total + per_page - 1) // per_page if total else 0
    offset = (page - 1) * per_page
    page_items = sorted_items[offset : offset + per_page]

    return PaginatedMediaServerCollectionsResponse(
        items=[
            MediaServerCollectionLookupResponse(
                name=name,
                media_count=len(media_ids),
            )
            for name, media_ids in page_items
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/rules", response_model=list[CleanupRuleResponse])
async def get_rules(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> list[CleanupRuleResponse]:
    """Get all cleanup rules. We sort it in ASC order by name."""
    result = await db.execute(select(ReclaimRule).order_by(ReclaimRule.name))
    rules = result.scalars().all()
    return [_rule_response(rule) for rule in rules]


@router.post(
    "/rules", response_model=CleanupRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_rule(
    rule_data: CleanupRuleCreate,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> CleanupRuleResponse:
    """Create a new cleanup rule."""
    if not rule_data.target_scope:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Rules require target_scope",
        )
    try:
        validate_rule_definition(
            rule_data.definition, target_scope=rule_data.target_scope
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from e

    effective_media_type = _media_type_for_target(
        rule_data.target_scope, rule_data.media_type
    )
    _validate_definition_path_syntax(rule_data.definition)
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
    """Validate path criteria against indexed media paths for the given scope.

    Returns split lists so callers can offer confirm/prune UX without mutating
    backend state.
    """
    criteria = _collect_unique_path_criteria(
        conditions=body.conditions,
        legacy_paths=body.paths,
    )
    if not criteria:
        return ValidatePathsResponse()

    media_paths = await _collect_media_paths(db, body.media_type, body.library_ids)
    valid, invalid = _evaluate_path_criteria(
        criteria, _index_media_path_values(media_paths)
    )

    valid_conditions = [
        ValidatePathCondition(
            field=criterion.field,
            operator=criterion.operator,
            value=criterion.value,
        )
        for criterion in valid
    ]
    invalid_conditions = [
        ValidatePathCondition(
            field=result.criterion.field,
            operator=result.criterion.operator,
            value=result.criterion.value,
        )
        for result in invalid
    ]

    return ValidatePathsResponse(
        valid_paths=[
            condition.value
            for condition in valid_conditions
            if condition.field == "media.path"
        ],
        invalid_paths=[
            condition.value
            for condition in invalid_conditions
            if condition.field == "media.path"
        ],
        valid_conditions=valid_conditions,
        invalid_conditions=invalid_conditions,
    )


@router.post("/rules/preview", response_model=PaginatedRulePreviewResponse)
async def preview_rule_matches(
    body: RulePreviewRequest,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> PaginatedRulePreviewResponse:
    """Dry-run an unsaved advanced rule without writing reclaim candidates."""
    if not body.target_scope:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Rules require target_scope",
        )
    try:
        validate_rule_definition(body.definition, target_scope=body.target_scope)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        ) from e

    effective_media_type = _media_type_for_target(body.target_scope, body.media_type)
    _validate_definition_path_syntax(body.definition)

    preview_rule = ReclaimRule(
        name=(body.name or "").strip() or "Preview Rule",
        media_type=effective_media_type,
        enabled=True,
        target_scope=body.target_scope,
        definition=body.definition,
        action=_normalize_rule_action(
            {"outcome": body.outcome},
            (body.name or "").strip() or "Preview Rule",
            body.target_scope,
        ),
    )

    preview_result = await collect_rule_preview_matches_with_metadata(
        db, [preview_rule]
    )
    items = await build_rule_preview_items(db, preview_result.matches)
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
        metadata=RulePreviewMetadata(
            source_media_count=preview_result.metadata.source_media_count,
            skipped_favorites_count=preview_result.metadata.skipped_favorites_count,
            skipped_protected_count=preview_result.metadata.skipped_protected_count,
            sonarr_unavailable_count=preview_result.metadata.sonarr_unavailable_count,
            sonarr_error=preview_result.metadata.sonarr_error,
            season_inventory_unavailable_count=(
                preview_result.metadata.season_inventory_unavailable_count
            ),
            season_inventory_unavailable_examples=(
                preview_result.metadata.season_inventory_unavailable_examples
            ),
            playback_unavailable_count=(
                preview_result.metadata.playback_unavailable_count
            ),
            playback_error=preview_result.metadata.playback_error,
            matched_count=preview_result.metadata.matched_count,
        ),
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
                validate_rule_definition(
                    rule_data.definition, target_scope=rule_data.target_scope
                )
            except ValueError as e:
                raise ValueError(str(e)) from e
            _validate_definition_path_syntax(rule_data.definition)

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
) -> CleanupRuleResponse:
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Rules require definition",
        )
    if "definition" in update_data and update_data["definition"] is not None:
        try:
            validate_rule_definition(update_data["definition"])
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
            ) from e
    if "target_scope" in update_data and not update_data["target_scope"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
        effective_definition = update_data.get("definition", rule.definition)
        try:
            validate_rule_definition(
                effective_definition, target_scope=effective_target_scope
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
            ) from e

        _validate_definition_path_syntax(effective_definition)

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
) -> dict[str, str]:
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
