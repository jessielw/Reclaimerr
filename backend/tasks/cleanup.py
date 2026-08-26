import asyncio
import shutil
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auto_delete import resolve_auto_delete_policy
from backend.core.logger import LOG
from backend.core.rule_actions import get_arr_service_config_ids
from backend.core.rule_engine import (
    ARR_ID_RULE_FIELDS,
    COLLECTION_RULE_FIELDS,
    FAVORITES_RULE_FIELDS,
    PLAYBACK_RULE_FIELDS,
    RANK_RULE_FIELDS,
    REGEX_OPERATORS,
    RULE_OUTCOME_CANDIDATE,
    RULE_OUTCOME_PROTECT,
    RULE_VALUE_UNAVAILABLE,
    SONARR_RULE_FIELDS,
    TAG_SUBSTRING_OPERATORS,
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    USER_SCOPED_PLAYBACK_FIELDS,
    ArrRuleDataResolver,
    CollectionSiblingRuleDataResolver,
    DiskStatsResolver,
    FavoritesRuleDataResolver,
    PlaybackHistoryResolver,
    PlaybackUserHistoryResolver,
    RankRuleDataResolver,
    SeerrRequestResolver,
    SonarrRuleDataResolver,
    SonarrRuleValue,
    collect_rule_conditions,
    evaluate_advanced_rule,
    evaluate_advanced_rule_state,
    normalize_rule_outcome,
    normalize_rule_target,
)
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.core.utils.datetime_utils import ensure_utc
from backend.core.utils.filesystem import (
    find_season_folder,
    mapped_path_variants,
    move_directory,
    move_media,
    move_season_files,
    remove_empty_directory,
    resolve_path,
    sibling_cleanup,
)
from backend.core.utils.request import summarize_error_message
from backend.core.utils.resolution import guesstimate_resolution
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database import async_db
from backend.database.models import (
    DeleteRequest,
    Episode,
    GeneralSettings,
    MediaFavorite,
    MediaWatchUser,
    MediaWatchUserEpisode,
    Movie,
    MovieArrRef,
    MovieVersion,
    PlaybackHistoryEvent,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimHistory,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import (
    MediaType,
    NotificationType,
    ProtectionRequestStatus,
    Service,
    Task,
)
from backend.models.cleanup import (
    MatchedCandidateRecord,
    RulePreviewMatchMetadata,
    RulePreviewMatchResult,
)
from backend.models.lifecycle_events import CandidateFileEvent
from backend.models.media import (
    RequesterWatchEvidence,
    RequesterWatchExplainResponse,
    RequesterWatchRequesterDetail,
)
from backend.services.admin_notices import (
    clear_playback_rule_data_notice,
    clear_seerr_rule_skip_notice,
    clear_sonarr_rule_data_notice,
    set_playback_rule_data_notice,
    set_seerr_rule_skip_notice,
    set_sonarr_rule_data_notice,
)
from backend.services.candidate_lifecycle import reconcile_candidate_schedule_events
from backend.services.lifecycle_webhooks import (
    dispatch_candidate_file_event,
    enqueue_event_deliveries,
)
from backend.services.media_favorites_cache import media_favorites_snapshot_cache
from backend.services.media_watch_snapshot_cache import media_watch_snapshot_cache
from backend.services.notifications import (
    build_cleanup_notification_context,
    notify_admins,
    notify_all_users,
)
from backend.services.playback_history import (
    IMPORTED_PLAYBACK_DURATION_FIELDS,
    NATIVE_PLAYBACK_FIELDS,
    PlaybackRuleSnapshot,
    completion_evidence_event_clause,
    load_active_tracearr_sources,
    load_playback_rule_snapshot,
    load_user_playback_totals,
    log_playback_rule_coverage,
    refresh_playback_history,
)
from backend.services.seerr_cache import SeerrRequestSnapshot, seerr_snapshot_cache
from backend.services.watch_identity import (
    AliasIndex,
    expand_watch_keys,
    load_watch_user_alias_index,
)
from backend.utils.helpers import normalize_leaving_soon_collection_title

__all__ = [
    "scan_cleanup_candidates",
    "tag_cleanup_candidates",
    "delete_cleanup_candidates",
    "delete_specific_candidates",
    "move_specific_candidates",
    "collect_rule_preview_matches",
    "collect_rule_preview_matches_with_metadata",
]

ArrDeleteFallback: TypeAlias = Literal["unmonitor", "unmonitor_only", "remove_if_empty"]
ArrDeleteAction: TypeAlias = Literal[
    "delete", "unmonitor", "unmonitor_only", "remove_if_empty"
]
# actions that unmonitor the arr entry rather than deleting it outright.
# "unmonitor_only" additionally skips touching the underlying files.
UNMONITOR_LIKE_ARR_ACTIONS: frozenset[str] = frozenset({"unmonitor", "unmonitor_only"})
SonarrProtectionPreserveKey: TypeAlias = tuple[int, int]

SONARR_UNAIRED_FIELD = "sonarr.latest_season_has_unaired_episodes"
SONARR_FINALE_FIELD = "sonarr.latest_season_has_finale"
SONARR_STATUS_FIELD = "sonarr.series_status"
SONARR_EPISODE_FETCH_CONCURRENCY = 8


@dataclass(slots=True)
class _SonarrRuleDataResult:
    unavailable_series_ids: set[int]
    preserve_protection_keys: set[SonarrProtectionPreserveKey]
    error: str | None = None


@dataclass(slots=True)
class _PlaybackRuleDataResult:
    snapshot: PlaybackRuleSnapshot | None
    unavailable_count: int = 0
    error: str | None = None


@dataclass(slots=True)
class _AutoDeleteRevalidationResult:
    candidate_ids: list[int]
    revalidated_out: int = 0
    playback_unavailable: int = 0


@dataclass(slots=True)
class _SonarrRefRuleState:
    config_id: int
    arr_series_id: int
    latest_season_number: int | None = None
    has_unaired_episodes: SonarrRuleValue = RULE_VALUE_UNAVAILABLE
    has_finale: SonarrRuleValue = RULE_VALUE_UNAVAILABLE
    series_status: SonarrRuleValue = RULE_VALUE_UNAVAILABLE


class _SonarrSeriesSnapshot:
    """Deduplicate bulk Sonarr series requests within one rule evaluation run."""

    __slots__ = ("clients", "_series_tasks")

    def __init__(self) -> None:
        clients = service_manager.sonarr_clients()
        if not clients and service_manager.sonarr:
            clients = {0: service_manager.sonarr}
        self.clients = clients
        self._series_tasks: dict[int, asyncio.Task[list[Any]]] = {}

    async def get_all_series(self, client: Any) -> list[Any]:
        client_key = id(client)
        task = self._series_tasks.get(client_key)
        if task is None:
            task = asyncio.create_task(client.get_all_series())
            self._series_tasks[client_key] = task
        return await task


def _build_reclaim_history_attributes(
    *,
    movie_version: MovieVersion | None = None,
    season: Season | None = None,
) -> dict[str, Any] | None:
    """Attributes relevant for reclaim history from the given media items."""
    resolution: str | None = None
    hdr: bool | None = None
    dolby_vision: bool | None = None

    if movie_version is not None:
        resolution = movie_version.video_resolution
        hdr = movie_version.video_hdr
        dolby_vision = movie_version.video_dolby_vision
    elif season is not None:
        resolution = guesstimate_resolution(
            season.max_video_width,
            season.max_video_height,
            None,
        )
        hdr = season.has_hdr
        dolby_vision = season.has_dolby_vision

    if resolution is None and hdr is None and dolby_vision is None:
        return None

    return {
        "resolution": resolution,
        "hdr": hdr,
        "dolby_vision": dolby_vision,
    }


def _is_series_scope(model: Any) -> Any:
    return and_(model.season_id.is_(None), model.episode_id.is_(None))


def _is_season_scope(model: Any) -> Any:
    return and_(model.season_id.isnot(None), model.episode_id.is_(None))


def _is_episode_scope(model: Any) -> Any:
    return model.episode_id.isnot(None)


def _has_media_path(path: str | None) -> bool:
    """Return whether a synchronized item points to physical media."""
    return bool(path and path.strip())


async def _select_auto_delete_eligible_candidate_ids() -> tuple[list[int], int, int]:
    async with async_db() as db:
        candidates = (await db.execute(select(ReclaimCandidate))).scalars().all()
        if not candidates:
            return [], 0, 0

        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        movie_delay_days = (
            settings_row.auto_delete_movie_delay_days if settings_row else 14
        )
        series_delay_days = (
            settings_row.auto_delete_series_delay_days if settings_row else 7
        )
        rule_ids = {
            rule_id
            for candidate in candidates
            for rule_id in (candidate.matched_rule_ids or [])
        }
        rule_actions_by_id = {
            rule.id: rule.action
            for rule in (
                (
                    await db.execute(
                        select(ReclaimRule).where(
                            ReclaimRule.id.in_(rule_ids),
                            ReclaimRule.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
                if rule_ids
                else []
            )
        }

        now = datetime.now(UTC)
        blocked_movie_ids: set[int] = set()
        blocked_movie_version_ids: set[int] = set()

        movie_overlap_queries = (
            select(ProtectedMedia.movie_id, ProtectedMedia.movie_version_id).where(
                ProtectedMedia.media_type == MediaType.MOVIE,
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            ),
            select(
                ProtectionRequest.movie_id, ProtectionRequest.movie_version_id
            ).where(
                ProtectionRequest.media_type == MediaType.MOVIE,
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            ),
            select(DeleteRequest.movie_id, DeleteRequest.movie_version_id).where(
                DeleteRequest.media_type == MediaType.MOVIE,
                DeleteRequest.status == ProtectionRequestStatus.PENDING,
            ),
        )
        for query in movie_overlap_queries:
            for movie_id, movie_version_id in (await db.execute(query)).all():
                if movie_id is not None:
                    blocked_movie_ids.add(movie_id)
                if movie_version_id is not None:
                    blocked_movie_version_ids.add(movie_version_id)

        if blocked_movie_version_ids:
            version_rows = (
                await db.execute(
                    select(MovieVersion.id, MovieVersion.movie_id).where(
                        MovieVersion.id.in_(blocked_movie_version_ids)
                    )
                )
            ).all()
            blocked_movie_ids.update(
                movie_id for _, movie_id in version_rows if movie_id is not None
            )

        blocked_series_ids: set[int] = set()
        blocked_season_ids: set[int] = set()
        blocked_episode_ids: set[int] = set()

        series_overlap_queries: tuple[Any, ...] = (
            select(
                ProtectedMedia.series_id,
                ProtectedMedia.season_id,
                ProtectedMedia.episode_id,
            ).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            ),
            select(
                ProtectionRequest.series_id,
                ProtectionRequest.season_id,
                ProtectionRequest.episode_id,
            ).where(
                ProtectionRequest.media_type == MediaType.SERIES,
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            ),
            select(
                DeleteRequest.series_id,
                DeleteRequest.season_id,
                DeleteRequest.episode_id,
            ).where(
                DeleteRequest.media_type == MediaType.SERIES,
                DeleteRequest.status == ProtectionRequestStatus.PENDING,
            ),
        )
        for query in series_overlap_queries:
            for series_id, season_id, episode_id in (await db.execute(query)).all():
                if series_id is not None:
                    blocked_series_ids.add(series_id)
                if season_id is not None:
                    blocked_season_ids.add(season_id)
                if episode_id is not None:
                    blocked_episode_ids.add(episode_id)

        if blocked_episode_ids:
            episode_rows = (
                await db.execute(
                    select(Episode.id, Episode.season_id).where(
                        Episode.id.in_(blocked_episode_ids)
                    )
                )
            ).all()
            blocked_season_ids.update(
                season_id for _, season_id in episode_rows if season_id is not None
            )

        if blocked_season_ids:
            season_rows = (
                await db.execute(
                    select(Season.id, Season.series_id).where(
                        Season.id.in_(blocked_season_ids)
                    )
                )
            ).all()
            blocked_series_ids.update(
                series_id for _, series_id in season_rows if series_id is not None
            )

        eligible_ids: list[int] = []
        blocked_count = 0
        waiting_count = 0
        for candidate in candidates:
            blocked = False
            if candidate.media_type is MediaType.MOVIE:
                blocked = (
                    candidate.movie_id is not None
                    and candidate.movie_id in blocked_movie_ids
                ) or (
                    candidate.movie_version_id is not None
                    and candidate.movie_version_id in blocked_movie_version_ids
                )
            else:
                blocked = (
                    (
                        candidate.series_id is not None
                        and candidate.series_id in blocked_series_ids
                    )
                    or (
                        candidate.season_id is not None
                        and candidate.season_id in blocked_season_ids
                    )
                    or (
                        candidate.episode_id is not None
                        and candidate.episode_id in blocked_episode_ids
                    )
                )

            if blocked:
                blocked_count += 1
                continue

            policy = resolve_auto_delete_policy(
                media_type=candidate.media_type,
                matched_rule_ids=candidate.matched_rule_ids,
                created_at=candidate.created_at,
                timer_started_at=candidate.auto_delete_timer_started_at,
                postponed_until=candidate.auto_delete_postponed_until,
                cancelled_at=candidate.auto_delete_cancelled_at,
                rule_actions_by_id=rule_actions_by_id,
                movie_delay_days=movie_delay_days,
                series_delay_days=series_delay_days,
                now=now,
            )
            if not policy.is_enabled:
                blocked_count += 1
                continue
            if policy.is_eligible:
                eligible_ids.append(candidate.id)
            else:
                waiting_count += 1

        return eligible_ids, blocked_count, waiting_count


def _candidate_target_key(candidate: ReclaimCandidate) -> tuple[str, int] | None:
    if candidate.media_type is MediaType.MOVIE:
        return (
            (TARGET_MOVIE_VERSION, candidate.movie_version_id)
            if candidate.movie_version_id is not None
            else None
        )
    if candidate.episode_id is not None:
        return TARGET_EPISODE, candidate.episode_id
    if candidate.season_id is not None:
        return TARGET_SEASON, candidate.season_id
    if candidate.series_id is not None:
        return TARGET_SERIES, candidate.series_id
    return None


def _record_target_key(record: MatchedCandidateRecord) -> tuple[str, int] | None:
    if record.media_type is MediaType.MOVIE:
        return (
            (TARGET_MOVIE_VERSION, record.movie_version_id)
            if record.movie_version_id is not None
            else None
        )
    if record.episode_id is not None:
        return TARGET_EPISODE, record.episode_id
    if record.season_id is not None:
        return TARGET_SEASON, record.season_id
    if record.series_id is not None:
        return TARGET_SERIES, record.series_id
    return None


async def _revalidate_auto_delete_candidate_ids(
    candidate_ids: Sequence[int],
) -> _AutoDeleteRevalidationResult:
    """Re-check watch-sensitive automatic rules against eligible targets.

    A candidate sits for its auto-delete delay before anything is removed. If
    somebody watches the media during that window the rule that created it no
    longer holds, so any rule whose verdict depends on watch state is evaluated
    again against current data before the deletion is authorized.
    """

    if not candidate_ids:
        return _AutoDeleteRevalidationResult(candidate_ids=[])

    async with async_db() as db:
        candidates = list(
            (
                await db.execute(
                    select(ReclaimCandidate).where(
                        ReclaimCandidate.id.in_(candidate_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        rule_ids = {
            rule_id
            for candidate in candidates
            for rule_id in (candidate.matched_rule_ids or [])
        }
        rules = (
            list(
                (
                    await db.execute(
                        select(ReclaimRule).where(
                            ReclaimRule.id.in_(rule_ids),
                            ReclaimRule.enabled.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if rule_ids
            else []
        )
        rules_by_id = {rule.id: rule for rule in rules}

        applicable_by_candidate: dict[int, set[int]] = {}
        affected: list[ReclaimCandidate] = []
        passthrough_ids: set[int] = set()
        for candidate in candidates:
            applicable = {
                rule_id
                for rule_id in (candidate.matched_rule_ids or [])
                if (
                    (rule := rules_by_id.get(rule_id)) is not None
                    and isinstance(rule.action, dict)
                    and rule.action.get("auto_delete_enabled") is True
                )
            }
            applicable_by_candidate[candidate.id] = applicable
            if any(
                _rule_uses_watch_sensitive_fields(rules_by_id[rule_id])
                for rule_id in applicable
            ):
                affected.append(candidate)
            else:
                passthrough_ids.add(candidate.id)

        if not affected:
            existing_ids = {candidate.id for candidate in candidates}
            return _AutoDeleteRevalidationResult(
                candidate_ids=[
                    candidate_id
                    for candidate_id in candidate_ids
                    if candidate_id in existing_ids
                ]
            )

        affected_rule_ids = {
            rule_id
            for candidate in affected
            for rule_id in applicable_by_candidate[candidate.id]
        }
        target_ids_by_scope: dict[str, set[int]] = defaultdict(set)
        for candidate in affected:
            target_key = _candidate_target_key(candidate)
            if target_key is not None:
                target_ids_by_scope[target_key[0]].add(target_key[1])

        try:
            preview = await collect_rule_preview_matches_with_metadata(
                db,
                [rules_by_id[rule_id] for rule_id in affected_rule_ids],
                target_ids_by_scope=target_ids_by_scope,
                require_fresh=True,
            )
        except Exception as exc:
            LOG.error(
                "Auto-delete playback revalidation failed; affected candidates "
                f"will be skipped: {exc}",
                exc_info=True,
            )
            return _AutoDeleteRevalidationResult(
                candidate_ids=[
                    candidate_id
                    for candidate_id in candidate_ids
                    if candidate_id in passthrough_ids
                ],
                revalidated_out=len(affected),
                playback_unavailable=len(affected),
            )

        if preview.metadata.playback_error:
            LOG.warning(
                "Auto-delete playback revalidation could not establish complete "
                f"provider coverage: {preview.metadata.playback_error}"
            )
            return _AutoDeleteRevalidationResult(
                candidate_ids=[
                    candidate_id
                    for candidate_id in candidate_ids
                    if candidate_id in passthrough_ids
                ],
                revalidated_out=len(affected),
                playback_unavailable=len(affected),
            )

        matched_rule_ids_by_target: dict[tuple[str, int], set[int]] = defaultdict(set)
        for record in preview.matches:
            target_key = _record_target_key(record)
            if target_key is not None:
                matched_rule_ids_by_target[target_key].update(record.matched_rule_ids)

        retained_ids = set(passthrough_ids)
        revalidated_out = 0
        for candidate in affected:
            target_key = _candidate_target_key(candidate)
            matched_rule_ids = (
                matched_rule_ids_by_target.get(target_key, set())
                if target_key is not None
                else set()
            )
            if matched_rule_ids & applicable_by_candidate[candidate.id]:
                retained_ids.add(candidate.id)
            else:
                revalidated_out += 1

        return _AutoDeleteRevalidationResult(
            candidate_ids=[
                candidate_id
                for candidate_id in candidate_ids
                if candidate_id in retained_ids
            ],
            revalidated_out=revalidated_out,
            playback_unavailable=min(
                preview.metadata.playback_unavailable_count,
                len(affected),
            ),
        )


def _normalize_favorites_username(value: str) -> str:
    """Normalizer for favorites usernames."""
    return value.strip().lower()


async def _load_favorites_policy(
    db: AsyncSession,
) -> tuple[bool, bool, set[str]]:
    """Load favorites protection settings."""
    row = (await db.execute(select(GeneralSettings))).scalars().first()
    if row is None:
        return False, False, set()
    usernames = {
        _normalize_favorites_username(str(raw))
        for raw in (row.favorites_usernames or [])
        if str(raw).strip()
    }
    return (
        bool(row.favorites_ignore_enabled),
        bool(row.favorites_protect_all_users),
        usernames,
    )


async def _ensure_favorites_snapshot_if_enabled(
    db: AsyncSession,
) -> tuple[bool, str | None]:
    """Ensure favorites snapshot exists/fresh when favorites protection is enabled."""
    enabled, _, _ = await _load_favorites_policy(db)
    if not enabled:
        return True, None
    ok, error = await media_favorites_snapshot_cache.ensure_fresh_snapshot()
    if not ok:
        return False, error
    return True, error


async def _load_favorite_tmdb_ids(
    db: AsyncSession,
    *,
    media_type: MediaType,
    protect_all_users: bool,
    usernames: set[str],
) -> set[int]:
    """Return protected TMDB IDs from favorites snapshot for the current policy."""
    if not protect_all_users and not usernames:
        return set()

    query = select(MediaFavorite.tmdb_id).where(MediaFavorite.media_type == media_type)
    if not protect_all_users:
        query = query.where(MediaFavorite.username_normalized.in_(usernames))
    rows = (await db.execute(query)).scalars().all()
    return {int(tmdb_id) for tmdb_id in rows if tmdb_id is not None}


async def _filter_movie_candidates_by_favorites(
    db: AsyncSession,
    candidates: Sequence[ReclaimCandidate],
    *,
    protect_all_users: bool,
    usernames: set[str],
) -> tuple[Sequence[ReclaimCandidate], int]:
    movie_ids = {candidate.movie_id for candidate in candidates if candidate.movie_id}
    if not movie_ids:
        return candidates, 0
    favorite_tmdb_ids = await _load_favorite_tmdb_ids(
        db,
        media_type=MediaType.MOVIE,
        protect_all_users=protect_all_users,
        usernames=usernames,
    )
    if not favorite_tmdb_ids:
        return candidates, 0

    rows = (
        await db.execute(select(Movie.id, Movie.tmdb_id).where(Movie.id.in_(movie_ids)))
    ).all()
    movie_tmdb_map = {movie_id: tmdb_id for movie_id, tmdb_id in rows}
    filtered = [
        candidate
        for candidate in candidates
        if candidate.movie_id is None
        or movie_tmdb_map.get(candidate.movie_id) not in favorite_tmdb_ids
    ]
    return filtered, len(candidates) - len(filtered)


async def _filter_series_candidates_by_favorites(
    db: AsyncSession,
    candidates: Sequence[ReclaimCandidate],
    *,
    protect_all_users: bool,
    usernames: set[str],
) -> tuple[Sequence[ReclaimCandidate], int]:
    series_ids = {
        candidate.series_id for candidate in candidates if candidate.series_id
    }
    if not series_ids:
        return candidates, 0
    favorite_tmdb_ids = await _load_favorite_tmdb_ids(
        db,
        media_type=MediaType.SERIES,
        protect_all_users=protect_all_users,
        usernames=usernames,
    )
    if not favorite_tmdb_ids:
        return candidates, 0

    rows = (
        await db.execute(
            select(Series.id, Series.tmdb_id).where(Series.id.in_(series_ids))
        )
    ).all()
    series_tmdb_map = {series_id: tmdb_id for series_id, tmdb_id in rows}
    filtered = [
        candidate
        for candidate in candidates
        if candidate.series_id is None
        or series_tmdb_map.get(candidate.series_id) not in favorite_tmdb_ids
    ]
    return filtered, len(candidates) - len(filtered)


async def _soft_remove_movie_if_empty(
    db: AsyncSession, movie_id: int | None
) -> Movie | None:
    if movie_id is None:
        return None
    movie = (
        await db.execute(select(Movie).where(Movie.id == movie_id))
    ).scalar_one_or_none()
    if movie is None:
        return None
    remaining_version_id = (
        await db.execute(
            select(MovieVersion.id).where(MovieVersion.movie_id == movie_id).limit(1)
        )
    ).scalar_one_or_none()
    if remaining_version_id is not None:
        return movie
    movie.removed_at = datetime.now(UTC)
    movie.added_at = None
    return movie


async def _soft_remove_series_if_empty(
    db: AsyncSession, series_id: int | None
) -> Series | None:
    if series_id is None:
        return None
    series = (
        await db.execute(select(Series).where(Series.id == series_id))
    ).scalar_one_or_none()
    if series is None:
        return None
    remaining_season_id = (
        await db.execute(
            select(Season.id).where(Season.series_id == series_id).limit(1)
        )
    ).scalar_one_or_none()
    if remaining_season_id is not None:
        return series
    series.removed_at = datetime.now(UTC)
    series.added_at = None
    return series


async def collect_rule_preview_matches(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> list[MatchedCandidateRecord]:
    """Evaluate rules without mutating persisted candidates."""
    result = await collect_rule_preview_matches_with_metadata(db, rules)
    return result.matches


async def _collect_rule_match_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    preview_metadata: RulePreviewMatchMetadata | None = None,
    exclude_favorites: bool = True,
    exclude_protected: bool = True,
    target_ids_by_scope: Mapping[str, set[int]] | None = None,
) -> list[MatchedCandidateRecord]:
    """Collect candidates that match the given rules."""
    movie_rules = [r for r in rules if normalize_rule_target(r) == TARGET_MOVIE_VERSION]
    series_rules = [r for r in rules if normalize_rule_target(r) == TARGET_SERIES]
    season_rules = [r for r in rules if normalize_rule_target(r) == TARGET_SEASON]
    episode_rules = [r for r in rules if normalize_rule_target(r) == TARGET_EPISODE]

    matches: list[MatchedCandidateRecord] = []
    if movie_rules:
        matches.extend(
            await _collect_movie_version_candidate_records(
                db,
                movie_rules,
                preview_metadata=preview_metadata,
                exclude_favorites=exclude_favorites,
                exclude_protected=exclude_protected,
                target_ids=(target_ids_by_scope or {}).get(TARGET_MOVIE_VERSION),
            )
        )
    if series_rules:
        matches.extend(
            await _collect_series_candidate_records(
                db,
                series_rules,
                preview_metadata=preview_metadata,
                exclude_favorites=exclude_favorites,
                exclude_protected=exclude_protected,
                target_ids=(target_ids_by_scope or {}).get(TARGET_SERIES),
            )
        )
    if season_rules:
        matches.extend(
            await _collect_season_candidate_records(
                db,
                season_rules,
                preview_metadata=preview_metadata,
                exclude_favorites=exclude_favorites,
                exclude_protected=exclude_protected,
                target_ids=(target_ids_by_scope or {}).get(TARGET_SEASON),
            )
        )
    if episode_rules:
        matches.extend(
            await _collect_episode_candidate_records(
                db,
                episode_rules,
                preview_metadata=preview_metadata,
                exclude_favorites=exclude_favorites,
                exclude_protected=exclude_protected,
                target_ids=(target_ids_by_scope or {}).get(TARGET_EPISODE),
            )
        )
    return matches


async def collect_rule_preview_matches_with_metadata(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    target_ids_by_scope: Mapping[str, set[int]] | None = None,
    require_fresh: bool = False,
) -> RulePreviewMatchResult:
    """Evaluate rules without mutating persisted candidates and return counters."""
    favorites_ready, favorites_error = await _ensure_favorites_snapshot_if_enabled(db)
    if not favorites_ready and favorites_error:
        LOG.warning(
            "Proceeding with existing favorites snapshot during rule preview: "
            f"{favorites_error}"
        )
    elif favorites_error:
        LOG.debug(
            "Using stale favorites snapshot during rule preview due to refresh error: "
            f"{favorites_error}"
        )
    DiskStatsResolver(
        arr_entries=await _load_arr_disk_space(),
        path_mappings=await _load_path_mappings(),
    ).activate()
    sonarr_series_snapshot = _SonarrSeriesSnapshot()
    await _refresh_arr_tags_for_rules(
        list(rules),
        sonarr_series_snapshot=sonarr_series_snapshot,
    )
    await _refresh_arr_monitoring_for_rules(
        list(rules),
        sonarr_series_snapshot=sonarr_series_snapshot,
    )
    await _activate_arr_id_rule_data_for_rules(db, list(rules))
    await _activate_favorites_rule_data_for_rules(
        db,
        list(rules),
        require_fresh=require_fresh,
    )
    await _activate_rank_rule_data_for_rules(db, list(rules))
    await _activate_collection_sibling_rule_data_for_rules(db, list(rules))
    metadata = RulePreviewMatchMetadata()
    seerr_ready, seerr_error = await _activate_seerr_request_resolver_for_rules(
        db,
        list(rules),
        require_fresh=require_fresh,
        allow_stale_on_failure=not require_fresh,
        metadata=metadata,
    )
    # Without this a Seerr outage silently evaluates every requester condition
    # as unknown and the preview looks like a normal empty result.
    metadata.seerr_unavailable = not seerr_ready
    metadata.seerr_error = seerr_error

    sonarr_result = await _activate_sonarr_rule_data_for_rules(
        db,
        list(rules),
        sonarr_series_snapshot=sonarr_series_snapshot,
    )
    metadata.sonarr_unavailable_count = len(sonarr_result.unavailable_series_ids)
    metadata.sonarr_error = sonarr_result.error
    playback_result = await _activate_playback_history_for_rules(
        db,
        list(rules),
        require_fresh=require_fresh,
    )
    if target_ids_by_scope and playback_result.snapshot is not None:
        metadata.playback_unavailable_count = _playback_unavailable_target_count(
            playback_result.snapshot, rules, target_ids_by_scope
        )
    else:
        metadata.playback_unavailable_count = playback_result.unavailable_count
    metadata.playback_error = playback_result.error
    is_protection_preview = bool(rules) and all(
        normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT for rule in rules
    )
    matches = await _collect_rule_match_records(
        db,
        rules,
        preview_metadata=metadata,
        exclude_favorites=not is_protection_preview,
        exclude_protected=not is_protection_preview,
        target_ids_by_scope=target_ids_by_scope,
    )
    metadata.matched_count = len(matches)
    return RulePreviewMatchResult(matches=matches, metadata=metadata)


ManagedProtectionKey: TypeAlias = tuple[
    int,
    MediaType,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
]


def _managed_protection_key(
    *,
    rule_id: int,
    media_type: MediaType,
    movie_id: int | None,
    movie_version_id: int | None,
    series_id: int | None,
    season_id: int | None,
    episode_id: int | None,
) -> ManagedProtectionKey:
    return (
        rule_id,
        media_type,
        movie_id,
        movie_version_id,
        series_id,
        season_id,
        episode_id,
    )


def _managed_protection_reason(
    record: MatchedCandidateRecord,
    rule_id: int,
) -> str:
    for reason in record.reason_data:
        if reason.get("rule_id") == rule_id:
            text = str(reason.get("text") or "").strip()
            if text:
                return text
    return record.reason or "Matched automated protection rule"


async def _reconcile_rule_managed_protections(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    preserve_rule_ids: set[int] | None = None,
    preserve_rule_series_keys: set[SonarrProtectionPreserveKey] | None = None,
) -> tuple[int, int, int]:
    preserve_rule_ids = preserve_rule_ids or set()
    preserve_rule_series_keys = preserve_rule_series_keys or set()
    records = await _collect_rule_match_records(
        db,
        rules,
        exclude_favorites=False,
        exclude_protected=False,
    )

    desired: dict[ManagedProtectionKey, tuple[MatchedCandidateRecord, int]] = {}
    active_rule_ids = {rule.id for rule in rules}
    for record in records:
        for rule_id in record.matched_rule_ids:
            if rule_id not in active_rule_ids:
                continue
            key = _managed_protection_key(
                rule_id=rule_id,
                media_type=record.media_type,
                movie_id=record.movie_id,
                movie_version_id=record.movie_version_id,
                series_id=record.series_id,
                season_id=record.season_id,
                episode_id=record.episode_id,
            )
            desired[key] = (record, rule_id)

    existing_result = await db.execute(
        select(ProtectedMedia).where(ProtectedMedia.source == "rule")
    )
    existing_rows = existing_result.scalars().all()
    existing: dict[ManagedProtectionKey, ProtectedMedia] = {}
    rows_to_delete: list[ProtectedMedia] = []
    for row in existing_rows:
        if row.source_rule_id is None:
            rows_to_delete.append(row)
            continue
        key = _managed_protection_key(
            rule_id=row.source_rule_id,
            media_type=row.media_type,
            movie_id=row.movie_id,
            movie_version_id=row.movie_version_id,
            series_id=row.series_id,
            season_id=row.season_id,
            episode_id=row.episode_id,
        )
        if key in existing:
            rows_to_delete.append(row)
        else:
            existing[key] = row

    created = 0
    updated = 0
    for key, (record, rule_id) in desired.items():
        reason = _managed_protection_reason(record, rule_id)
        existing_row = existing.pop(key) if key in existing else None
        if existing_row is None:
            db.add(
                ProtectedMedia(
                    media_type=record.media_type,
                    protected_by_user_id=None,
                    movie_id=record.movie_id,
                    movie_version_id=record.movie_version_id,
                    series_id=record.series_id,
                    season_id=record.season_id,
                    episode_id=record.episode_id,
                    source="rule",
                    source_rule_id=rule_id,
                    reason=reason,
                    permanent=True,
                    expires_at=None,
                )
            )
            created += 1
            continue

        if (
            existing_row.reason != reason
            or not existing_row.permanent
            or existing_row.expires_at is not None
            or existing_row.protected_by_user_id is not None
        ):
            existing_row.reason = reason
            existing_row.permanent = True
            existing_row.expires_at = None
            existing_row.protected_by_user_id = None
            updated += 1

    for row in existing.values():
        if row.source_rule_id in preserve_rule_ids:
            continue
        if (
            row.source_rule_id is not None
            and row.series_id is not None
            and (row.source_rule_id, row.series_id) in preserve_rule_series_keys
        ):
            continue
        rows_to_delete.append(row)

    for row in rows_to_delete:
        await db.delete(row)
    await db.flush()
    return created, updated, len(rows_to_delete)


async def scan_cleanup_candidates() -> None:
    """Scan media libraries and identify cleanup candidates based on configured rules."""
    LOG.info("Starting cleanup candidates scan")

    async with track_task_execution(Task.SCAN_CLEANUP_CANDIDATES):
        async with candidate_workflow_lock:
            try:
                scan_started_at = datetime.now(UTC)
                async with async_db() as session:
                    response = await _scan_with_db(session)
                    if response and response[0] > 0:
                        try:
                            context = await build_cleanup_notification_context(
                                created_count=response[0],
                                created_since=scan_started_at,
                            )
                            await notify_all_users(
                                notification_type=NotificationType.NEW_CLEANUP_CANDIDATES,
                                title="New Cleanup Candidates Found",
                                message=f"There are {response[0]} new cleanup candidates",
                                context=context,
                            )
                        except Exception as e:
                            LOG.error(f"Error sending cleanup scan notification: {e}")
            except Exception as e:
                LOG.error(f"Error scanning cleanup candidates: {e}", exc_info=True)
                raise


# arr.tags operators that match against patterns rather than whole tag labels.
# Their condition values are needles or regexes, never tag names, so they cannot
# be resolved to specific labels before the arr's tag catalog has been fetched.
NON_EXACT_ARR_TAG_OPERATORS = REGEX_OPERATORS | TAG_SUBSTRING_OPERATORS


def _collect_arr_tag_labels(rules: list[ReclaimRule]) -> set[str]:
    """Return the distinct lowercase tag labels named by exact-match arr.tags conditions.

    Values belonging to non-exact operators are patterns rather than labels, so they
    are skipped here; _has_non_exact_arr_tag_condition reports their presence instead.
    """
    labels: set[str] = set()
    for rule in rules:
        for condition in collect_rule_conditions(rule.definition, field="arr.tags"):
            if condition.get("operator") in NON_EXACT_ARR_TAG_OPERATORS:
                continue
            value = condition.get("value")
            values = value if isinstance(value, list) else [value]
            for v in values:
                if v is not None and str(v).strip():
                    labels.add(str(v).strip().lower())
    return labels


def _has_non_exact_arr_tag_condition(rules: list[ReclaimRule]) -> bool:
    """Return True when any arr.tags condition matches by pattern instead of by label.

    Such a condition cannot be narrowed to specific tag names ahead of time, so the
    refresh widens to the arr's whole tag catalog rather than a filtered subset.
    """
    for rule in rules:
        for condition in collect_rule_conditions(rule.definition, field="arr.tags"):
            if condition.get("operator") in NON_EXACT_ARR_TAG_OPERATORS:
                return True
    return False


def _all_catalog_labels(tags: Sequence[Any]) -> set[str]:
    """Return every non-blank lowercase label in an arr tag catalog.

    Normalisation matches _collect_arr_item_ids_by_label so that the labels used for
    lookup and the labels stripped from existing rows are the same strings.
    """
    labels: set[str] = set()
    for tag in tags:
        label_raw = getattr(tag, "label", None)
        if label_raw is None:
            continue
        label = str(label_raw).strip().lower()
        if label:
            labels.add(label)
    return labels


def _coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion for API payload IDs."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _collect_arr_item_ids_by_label(
    *,
    items: Sequence[Any],
    tags: Sequence[Any],
    wanted_labels: set[str],
) -> dict[str, set[int]]:
    """Build label -> arr item IDs from item payload tags + tag catalog labels."""
    if not wanted_labels:
        return {}

    tag_id_to_label: dict[int, str] = {}
    for tag in tags:
        tag_id = _coerce_int(getattr(tag, "id", None))
        label_raw = getattr(tag, "label", None)
        if tag_id is None or label_raw is None:
            continue
        label = str(label_raw).strip().lower()
        if not label:
            continue
        tag_id_to_label[tag_id] = label

    label_to_arr_ids: dict[str, set[int]] = {}
    for item in items:
        arr_item_id = _coerce_int(getattr(item, "id", None))
        if arr_item_id is None:
            continue
        tag_ids = getattr(item, "tags", None) or []
        for raw_tag_id in tag_ids:
            tag_id = _coerce_int(raw_tag_id)
            if tag_id is None:
                continue
            resolved_label = tag_id_to_label.get(tag_id)
            if resolved_label and resolved_label in wanted_labels:
                label_to_arr_ids.setdefault(resolved_label, set()).add(arr_item_id)

    return label_to_arr_ids


async def _refresh_arr_tags_for_rules(
    rules: list[ReclaimRule],
    *,
    sonarr_series_snapshot: _SonarrSeriesSnapshot | None = None,
) -> None:
    """Refresh arr_tags on Movie/Series rows for tag labels referenced by active rules.

    Steps:
    1. Collect the distinct tag labels used in arr.tags conditions across all rules.
       A pattern condition (regex/substring) cannot name its labels ahead of time, so
       its presence triggers a full refresh of the arr's whole tag catalog instead.
    2. Per arr client: fetch items + tag catalog and resolve tag IDs to labels.
    3. Map arr item IDs -> DB IDs via MovieArrRef / SeriesArrRef.
    4. Write the confirmed labels to each ref reachable from a successfully refreshed
       config, in one of two modes:
       - Full refresh (a pattern condition is present): replace arr_tags outright with
         the confirmed labels, since every label the arrs report for the item is known.
       - Exact-match only: patch arr_tags, stripping just the rule-relevant labels and
         re-adding only the ones confirmed present, leaving unrelated labels alone.
    Failed config fetches are non-destructive in both modes: a ref reachable from any
    failed config is excluded from the write, so its existing DB tags are preserved.
    """
    movie_rules = [
        r for r in rules if normalize_rule_target(r) in {TARGET_MOVIE_VERSION}
    ]
    series_rules = [
        r
        for r in rules
        if normalize_rule_target(r) in {TARGET_SERIES, TARGET_SEASON, TARGET_EPISODE}
    ]

    movie_tag_labels = _collect_arr_tag_labels(movie_rules)
    series_tag_labels = _collect_arr_tag_labels(series_rules)
    movie_needs_full_refresh = _has_non_exact_arr_tag_condition(movie_rules)
    series_needs_full_refresh = _has_non_exact_arr_tag_condition(series_rules)

    # A pattern condition cannot name the labels it will match, so the whole
    # catalog is refreshed for that media type instead of a filtered subset.
    movie_tags_wanted = bool(movie_tag_labels) or movie_needs_full_refresh
    series_tags_wanted = bool(series_tag_labels) or series_needs_full_refresh

    if not movie_tags_wanted and not series_tags_wanted:
        return

    #### radarr: refresh movie arr_tags for rule relevant labels ####
    if movie_tags_wanted:
        radarr_clients = service_manager.radarr_clients()
        if not radarr_clients and service_manager.radarr:
            radarr_clients = {0: service_manager.radarr}

        if radarr_clients:
            # movie_id (DB) -> set of labels to add
            movie_label_additions: dict[int, set[str]] = {}
            # Every label actually refreshed, across all configs that succeeded. Drives
            # the exact-match strip and the debug log only; full-refresh mode replaces
            # arr_tags with movie_label_additions instead.
            movie_refreshed_labels: set[str] = set()
            radarr_successful_config_ids: set[int] = set()
            radarr_failed_config_ids: set[int] = set()

            async with async_db() as db:
                rows = (
                    await db.execute(
                        select(
                            MovieArrRef.arr_movie_id,
                            MovieArrRef.movie_id,
                            MovieArrRef.service_config_id,
                        )
                    )
                ).all()
            # arr_movie_id -> db movie_id, grouped by config_id
            arr_to_db_by_config: dict[int, dict[int, int]] = {}
            for arr_movie_id, db_movie_id, config_id in rows:
                arr_to_db_by_config.setdefault(config_id, {})[arr_movie_id] = (
                    db_movie_id
                )

            for config_id, client in radarr_clients.items():
                try:
                    all_movies = await client.get_all_movies()
                    tag_list = await client.get_tags()
                except Exception as e:
                    radarr_failed_config_ids.add(config_id)
                    LOG.warning(
                        f"Failed to fetch Radarr movies/tags for config {config_id}: {e}"
                    )
                    continue

                radarr_successful_config_ids.add(config_id)
                effective_labels = (
                    _all_catalog_labels(tag_list)
                    if movie_needs_full_refresh
                    else movie_tag_labels
                )
                movie_refreshed_labels |= effective_labels
                label_to_arr_ids = _collect_arr_item_ids_by_label(
                    items=all_movies,
                    tags=tag_list,
                    wanted_labels=effective_labels,
                )
                arr_to_db = arr_to_db_by_config.get(config_id, {})
                for label, arr_ids in label_to_arr_ids.items():
                    for arr_id in arr_ids:
                        db_id = arr_to_db.get(arr_id)
                        if db_id is not None:
                            movie_label_additions.setdefault(db_id, set()).add(label)

            # apply only to refs we successfully refreshed, and never touch rows tied to a failed config.
            successful_db_movie_ids = {
                db_id
                for config_id, mapping in arr_to_db_by_config.items()
                if config_id in radarr_successful_config_ids
                for db_id in mapping.values()
            }
            failed_db_movie_ids = {
                db_id
                for config_id, mapping in arr_to_db_by_config.items()
                if config_id in radarr_failed_config_ids
                for db_id in mapping.values()
            }
            refreshable_db_movie_ids = successful_db_movie_ids - failed_db_movie_ids
            if refreshable_db_movie_ids:
                async with async_db() as db:
                    result = await db.execute(
                        select(Movie).where(Movie.id.in_(refreshable_db_movie_ids))
                    )
                    for movie in result.scalars().all():
                        confirmed = movie_label_additions.get(movie.id, set())
                        if movie_needs_full_refresh:
                            # Every label the arrs report for this item is known, so the
                            # row is replaced rather than patched. This also clears a
                            # label whose tag was deleted from the catalog, which a strip
                            # set derived from that catalog cannot name.
                            movie.arr_tags = sorted(confirmed)
                            continue
                        current = set(movie.arr_tags or [])
                        current -= movie_refreshed_labels  # strip refreshed labels
                        current |= confirmed  # re-add current ones
                        movie.arr_tags = sorted(current)
                    await db.commit()
            elif radarr_failed_config_ids:
                LOG.warning(
                    "Radarr tag refresh failed for all relevant refs; "
                    "keeping existing movie arr_tags rows unchanged"
                )
            label_summary = (
                f"{len(movie_refreshed_labels)} catalog labels"
                if movie_needs_full_refresh
                else f"labels: {movie_refreshed_labels}"
            )
            LOG.debug(
                f"Refreshed arr_tags for {len(refreshable_db_movie_ids)} movies ({label_summary})"
            )

    #### sonarr: refresh series arr_tags for rule relevant labels ####
    if series_tags_wanted:
        sonarr_series_snapshot = sonarr_series_snapshot or _SonarrSeriesSnapshot()
        sonarr_clients = sonarr_series_snapshot.clients

        if sonarr_clients:
            series_label_additions: dict[int, set[str]] = {}
            # Every label actually refreshed, across all configs that succeeded. Drives
            # the exact-match strip and the debug log only; full-refresh mode replaces
            # arr_tags with series_label_additions instead.
            series_refreshed_labels: set[str] = set()
            sonarr_successful_config_ids: set[int] = set()
            sonarr_failed_config_ids: set[int] = set()

            async with async_db() as db:
                rows = (
                    await db.execute(
                        select(
                            SeriesArrRef.arr_series_id,
                            SeriesArrRef.series_id,
                            SeriesArrRef.service_config_id,
                        )
                    )
                ).all()
            sonarr_arr_to_db_by_config: dict[int, dict[int, int]] = {}
            for arr_series_id, db_series_id, config_id in rows:
                sonarr_arr_to_db_by_config.setdefault(config_id, {})[arr_series_id] = (
                    db_series_id
                )

            for config_id, sonarr_client in sonarr_clients.items():
                try:
                    all_series = await sonarr_series_snapshot.get_all_series(
                        sonarr_client
                    )
                    tag_list = await sonarr_client.get_tags()
                except Exception as e:
                    sonarr_failed_config_ids.add(config_id)
                    LOG.warning(
                        f"Failed to fetch Sonarr series/tags for config {config_id}: {e}"
                    )
                    continue

                sonarr_successful_config_ids.add(config_id)
                effective_labels = (
                    _all_catalog_labels(tag_list)
                    if series_needs_full_refresh
                    else series_tag_labels
                )
                series_refreshed_labels |= effective_labels
                label_to_arr_ids = _collect_arr_item_ids_by_label(
                    items=all_series,
                    tags=tag_list,
                    wanted_labels=effective_labels,
                )
                arr_to_db = sonarr_arr_to_db_by_config.get(config_id, {})
                for label, arr_ids in label_to_arr_ids.items():
                    for arr_id in arr_ids:
                        db_id = arr_to_db.get(arr_id)
                        if db_id is not None:
                            series_label_additions.setdefault(db_id, set()).add(label)

            successful_db_series_ids = {
                db_id
                for config_id, mapping in sonarr_arr_to_db_by_config.items()
                if config_id in sonarr_successful_config_ids
                for db_id in mapping.values()
            }
            failed_db_series_ids = {
                db_id
                for config_id, mapping in sonarr_arr_to_db_by_config.items()
                if config_id in sonarr_failed_config_ids
                for db_id in mapping.values()
            }
            refreshable_db_series_ids = successful_db_series_ids - failed_db_series_ids
            if refreshable_db_series_ids:
                async with async_db() as db:
                    result = await db.execute(
                        select(Series).where(Series.id.in_(refreshable_db_series_ids))
                    )
                    for series in result.scalars().all():
                        confirmed = series_label_additions.get(series.id, set())
                        if series_needs_full_refresh:
                            # Every label the arrs report for this item is known, so the
                            # row is replaced rather than patched. This also clears a
                            # label whose tag was deleted from the catalog, which a strip
                            # set derived from that catalog cannot name.
                            series.arr_tags = sorted(confirmed)
                            continue
                        current = set(series.arr_tags or [])
                        current -= series_refreshed_labels
                        current |= confirmed
                        series.arr_tags = sorted(current)
                    await db.commit()
            elif sonarr_failed_config_ids:
                LOG.warning(
                    "Sonarr tag refresh failed for all relevant refs; "
                    "keeping existing series arr_tags rows unchanged"
                )
            label_summary = (
                f"{len(series_refreshed_labels)} catalog labels"
                if series_needs_full_refresh
                else f"labels: {series_refreshed_labels}"
            )
            LOG.debug(
                f"Refreshed arr_tags for {len(refreshable_db_series_ids)} series ({label_summary})"
            )


def _rules_use_monitoring_field(rules: list[ReclaimRule]) -> bool:
    """Return True if any rule condition references the arr.monitored field."""
    return _rules_use_field(rules, "arr.monitored")


def _rules_use_field(rules: list[ReclaimRule], field: str) -> bool:
    """Return True if any rule condition references the requested field."""
    return any(collect_rule_conditions(rule.definition, field=field) for rule in rules)


def _rules_use_any_field(rules: list[ReclaimRule], fields: Iterable[str]) -> bool:
    """Return True if any rule condition references one of the requested fields."""
    return any(_rules_use_field(rules, field) for field in fields)


def _rule_uses_sonarr_fields(rule: ReclaimRule) -> bool:
    return any(
        collect_rule_conditions(rule.definition, field=field)
        for field in SONARR_RULE_FIELDS
    )


async def _season_watch_inventory_rule_data(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> _SonarrRuleDataResult:
    """Report seasons whose canonical Sonarr episode inventory is unavailable."""
    watch_rules = [
        rule for rule in rules if _rule_uses_season_episode_watch_fields(rule)
    ]
    if not watch_rules:
        return _SonarrRuleDataResult(set(), set())

    rows = (
        await db.execute(
            select(Series.id, Season.sonarr_episode_numbers)
            .join(Season, Season.series_id == Series.id)
            .where(Series.removed_at.is_(None))
        )
    ).all()
    unavailable_series_ids = {
        series_id for series_id, inventory in rows if not inventory
    }
    preserve_protection_keys = {
        (rule.id, series_id)
        for rule in watch_rules
        if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT
        and isinstance(rule.id, int)
        for series_id in unavailable_series_ids
    }
    error = (
        "Run Sync Media to refresh Sonarr's season episode inventory"
        if unavailable_series_ids
        else None
    )
    return _SonarrRuleDataResult(
        unavailable_series_ids=unavailable_series_ids,
        preserve_protection_keys=preserve_protection_keys,
        error=error,
    )


def _rule_uses_playback_fields(rule: ReclaimRule) -> bool:
    return any(
        collect_rule_conditions(rule.definition, field=field)
        for field in PLAYBACK_RULE_FIELDS | USER_SCOPED_PLAYBACK_FIELDS
    )


# Fields whose answer can change between a candidate being created and its
# automatic deletion firing, simply because somebody watched the media in the
# meantime. They get the same pre-delete recheck the playback fields get.
WATCH_SENSITIVE_RULE_FIELDS = frozenset(
    {
        "seerr.requester_has_watched",
        "seerr.requester_watched_after_request",
        "season.fully_watched",
        "season.watched_percent",
        "watch.view_count",
        "watch.last_viewed_at",
        "watch.days_since_last_watched",
        "watch.never_watched",
    }
)


def _rule_uses_watch_sensitive_fields(rule: ReclaimRule) -> bool:
    """Return whether a rule's verdict depends on who has watched the media."""
    return _rule_uses_playback_fields(rule) or any(
        collect_rule_conditions(rule.definition, field=field)
        for field in WATCH_SENSITIVE_RULE_FIELDS
    )


def _targets_with_imported_playback(
    snapshot: PlaybackRuleSnapshot,
) -> set[tuple[str, int]]:
    """Return the targets backed by imported playback events.

    Per-user duration and percent are read from those events, so a target that
    only has native watch state behind it has unknown per-user totals rather
    than zero ones -- the same distinction the aggregate duration fields make.
    """
    return {
        key
        for key, available in snapshot.available_fields_by_target.items()
        if available & IMPORTED_PLAYBACK_DURATION_FIELDS
    }


def _playback_unavailable_target_count(
    snapshot: PlaybackRuleSnapshot,
    rules: Iterable[ReclaimRule],
    target_ids_by_scope: Mapping[str, set[int]],
) -> int:
    """Count preview targets whose playback values these rules cannot read."""
    requested_fields_by_scope: dict[str, set[str]] = defaultdict(set)
    user_scoped_scopes: set[str] = set()
    for rule in rules:
        scope = normalize_rule_target(rule)
        for field in PLAYBACK_RULE_FIELDS:
            if collect_rule_conditions(rule.definition, field=field):
                requested_fields_by_scope[scope].add(field)
        if any(
            collect_rule_conditions(rule.definition, field=field)
            for field in USER_SCOPED_PLAYBACK_FIELDS
        ):
            user_scoped_scopes.add(scope)
    imported_targets = (
        _targets_with_imported_playback(snapshot) if user_scoped_scopes else set()
    )

    def _unavailable(scope: str, target_id: int) -> bool:
        requested = requested_fields_by_scope.get(scope)
        if requested and not requested.issubset(
            snapshot.available_fields_by_target.get((scope, target_id), set())
        ):
            return True
        # per-user totals are not tracked per field in the snapshot; they come
        # from the same imported events the aggregate duration fields do
        return (
            scope in user_scoped_scopes and (scope, target_id) not in imported_targets
        )

    return sum(
        1
        for scope, target_ids in target_ids_by_scope.items()
        for target_id in target_ids
        if _unavailable(scope, target_id)
    )


async def _activate_playback_history_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    require_fresh: bool = False,
) -> _PlaybackRuleDataResult:
    playback_rules = [rule for rule in rules if _rule_uses_playback_fields(rule)]
    if not playback_rules:
        PlaybackHistoryResolver({}).activate()
        PlaybackUserHistoryResolver({}, available_targets=set()).activate()
        return _PlaybackRuleDataResult(snapshot=None)

    scopes = {normalize_rule_target(rule) for rule in playback_rules}
    # aggregate fields only: the snapshot tracks availability per field for
    # these, while the user-scoped fields are tracked per target instead (see
    # user_scoped_fields below), so mixing them here would compare a field name
    # against a set that can never contain it.
    fields = {
        field
        for rule in playback_rules
        for field in PLAYBACK_RULE_FIELDS
        if collect_rule_conditions(rule.definition, field=field)
    }
    user_scoped_fields = {
        field
        for rule in playback_rules
        for field in USER_SCOPED_PLAYBACK_FIELDS
        if collect_rule_conditions(rule.definition, field=field)
    }

    # Provider refreshes use their own sessions. Release this evaluator's read
    # transaction before they update persisted snapshots.
    await db.commit()
    native_error: str | None = None
    if fields & NATIVE_PLAYBACK_FIELDS:
        (
            native_ok,
            native_error,
        ) = await media_watch_snapshot_cache.ensure_fresh_snapshot(
            force=require_fresh,
            allow_stale_on_failure=not require_fresh,
        )
        if not native_ok and native_error:
            LOG.warning(
                "Native playback snapshot refresh failed; affected rule values "
                f"will remain unknown: {native_error}"
            )
        elif native_error:
            LOG.debug(
                "Using stale native playback snapshot during rule preview: "
                f"{native_error}"
            )

    refresh_result = await refresh_playback_history(force=require_fresh)
    snapshot = await load_playback_rule_snapshot(db, refresh_result)
    PlaybackHistoryResolver(snapshot.values_by_target).activate()
    if user_scoped_fields:
        PlaybackUserHistoryResolver(
            await load_user_playback_totals(db),
            available_targets=_targets_with_imported_playback(snapshot),
        ).activate()
    else:
        # no rule reads per-user totals, so skip the query entirely; an empty
        # observability set keeps the unread resolver from reporting targets as
        # watched by nobody
        PlaybackUserHistoryResolver({}, available_targets=set()).activate()
    log_playback_rule_coverage(snapshot, scopes, fields)
    unavailable_count = snapshot.unavailable_count(scopes, fields)
    if unavailable_count == 0 and not (
        require_fresh and (snapshot.errors or native_error)
    ):
        return _PlaybackRuleDataResult(snapshot=snapshot)

    errors = [*snapshot.errors]
    if native_error and native_error not in errors:
        errors.append(native_error)
    if errors:
        error = "; ".join(errors)
    elif not snapshot.has_configured_provider:
        error = "No playback source is configured"
    elif fields & IMPORTED_PLAYBACK_DURATION_FIELDS:
        error = (
            "Playback duration fields require imported Playback Reporting or "
            "Tautulli history for the media source"
        )
    else:
        error = (
            "Some media targets are not observable by the configured playback sources"
        )
    return _PlaybackRuleDataResult(
        snapshot=snapshot,
        unavailable_count=unavailable_count,
        error=error,
    )


async def _activate_arr_id_rule_data_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> None:
    if not _rules_use_any_field(rules, ARR_ID_RULE_FIELDS):
        ArrRuleDataResolver().activate()
        return

    movie_ids_by_movie_id: dict[int, set[int]] = {}
    series_ids_by_series_id: dict[int, set[int]] = {}
    if _rules_use_field(rules, "arr.movie_ids"):
        rows = (
            await db.execute(
                select(MovieArrRef.movie_id, MovieArrRef.arr_movie_id).where(
                    MovieArrRef.arr_movie_id.is_not(None)
                )
            )
        ).all()
        for movie_id, arr_movie_id in rows:
            movie_ids_by_movie_id.setdefault(movie_id, set()).add(arr_movie_id)
    if _rules_use_field(rules, "arr.series_ids"):
        rows = (
            await db.execute(
                select(SeriesArrRef.series_id, SeriesArrRef.arr_series_id).where(
                    SeriesArrRef.arr_series_id.is_not(None)
                )
            )
        ).all()
        for series_id, arr_series_id in rows:
            series_ids_by_series_id.setdefault(series_id, set()).add(arr_series_id)

    ArrRuleDataResolver(
        movie_ids_by_movie_id=movie_ids_by_movie_id,
        series_ids_by_series_id=series_ids_by_series_id,
    ).activate()


async def _activate_favorites_rule_data_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    require_fresh: bool = False,
) -> None:
    if not _rules_use_any_field(rules, FAVORITES_RULE_FIELDS):
        FavoritesRuleDataResolver({}).activate()
        return

    ok, error = await media_favorites_snapshot_cache.ensure_fresh_snapshot(
        force=require_fresh
    )
    if not ok and error:
        LOG.warning(
            "Favorites/watchlist rule data refresh failed; using existing snapshot: "
            f"{error}"
        )
    elif error:
        LOG.debug(
            f"Using stale favorites/watchlist rule data due to refresh error: {error}"
        )

    rows = (
        await db.execute(
            select(
                MediaFavorite.media_type,
                MediaFavorite.tmdb_id,
                MediaFavorite.username,
            )
        )
    ).all()
    usernames_by_key: dict[tuple[MediaType, int], set[str]] = {}
    for media_type, tmdb_id, username in rows:
        if not tmdb_id or not username:
            continue
        usernames_by_key.setdefault((media_type, int(tmdb_id)), set()).add(
            str(username)
        )
    FavoritesRuleDataResolver(usernames_by_key).activate()


async def _activate_rank_rule_data_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> None:
    if not _rules_use_any_field(rules, RANK_RULE_FIELDS):
        RankRuleDataResolver().activate()
        return

    season_rank_by_id: dict[int, int] = {}
    episode_rank_by_id: dict[int, int] = {}

    if _rules_use_field(rules, "season.position_by_air_date"):
        season_rows = (
            await db.execute(
                select(
                    Season.id,
                    Season.series_id,
                    Season.season_number,
                    Season.air_date,
                    Season.arr_added_at,
                ).where(Season.season_number > 0)
            )
        ).all()
        seasons_by_series: dict[int, list[tuple[int, datetime, int]]] = {}
        for (
            season_id,
            series_id,
            season_number,
            air_date,
            arr_added_at,
        ) in season_rows:
            rank_date = air_date or arr_added_at
            if rank_date is None:
                continue
            seasons_by_series.setdefault(series_id, []).append(
                (season_id, ensure_utc(rank_date), int(season_number))
            )
        for ranked_seasons in seasons_by_series.values():
            ranked_seasons.sort(key=lambda item: (item[1], item[2]), reverse=True)
            for index, (season_id, _rank_date, _season_number) in enumerate(
                ranked_seasons, start=1
            ):
                season_rank_by_id[season_id] = index

    if _rules_use_field(rules, "episode.position_by_air_date"):
        episode_rows = (
            await db.execute(
                select(
                    Episode.id,
                    Season.series_id,
                    Season.season_number,
                    Episode.episode_number,
                    Episode.air_date,
                    Episode.arr_added_at,
                )
                .join(Season, Episode.season_id == Season.id)
                .where(Season.season_number > 0)
            )
        ).all()
        episodes_by_series: dict[int, list[tuple[int, datetime, int, int]]] = {}
        for (
            episode_id,
            series_id,
            season_number,
            episode_number,
            air_date,
            arr_added_at,
        ) in episode_rows:
            rank_date = air_date or arr_added_at
            if rank_date is None:
                continue
            episodes_by_series.setdefault(series_id, []).append(
                (
                    episode_id,
                    ensure_utc(rank_date),
                    int(season_number),
                    int(episode_number),
                )
            )
        for ranked_episodes in episodes_by_series.values():
            ranked_episodes.sort(
                key=lambda item: (item[1], item[2], item[3]), reverse=True
            )
            for index, (
                episode_id,
                _rank_date,
                _season_number,
                _episode_number,
            ) in enumerate(ranked_episodes, start=1):
                episode_rank_by_id[episode_id] = index

    RankRuleDataResolver(
        season_rank_by_id=season_rank_by_id,
        episode_rank_by_id=episode_rank_by_id,
    ).activate()


def _normalized_rule_collection_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_leaving_soon_collection_title(value)
    return normalized.casefold() if normalized else None


async def _activate_collection_sibling_rule_data_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
) -> None:
    if not _rules_use_any_field(rules, COLLECTION_RULE_FIELDS):
        CollectionSiblingRuleDataResolver({}).activate()
        return

    rows = (
        await db.execute(
            select(
                Movie.id,
                Movie.last_viewed_at,
                MovieVersion.media_server_collection_names,
            )
            .join(MovieVersion, MovieVersion.movie_id == Movie.id)
            .where(Movie.removed_at.is_(None))
        )
    ).all()

    movie_collections: dict[int, set[str]] = {}
    collection_watch_dates: dict[str, list[tuple[int, datetime]]] = {}
    leaving_soon_name = _normalized_rule_collection_name("Leaving Soon")
    for movie_id, last_viewed_at, collection_names in rows:
        normalized_names = {
            normalized
            for name in (collection_names or [])
            if (normalized := _normalized_rule_collection_name(str(name)))
            and normalized != leaving_soon_name
        }
        if not normalized_names:
            continue
        movie_collections.setdefault(movie_id, set()).update(normalized_names)
        if last_viewed_at is None:
            continue
        watched_at = ensure_utc(last_viewed_at)
        for name in normalized_names:
            collection_watch_dates.setdefault(name, []).append((movie_id, watched_at))

    latest_by_movie_id: dict[int, datetime | None] = {}
    for movie_id, collection_names in movie_collections.items():
        candidates = [
            watched_at
            for collection_name in collection_names
            for sibling_id, watched_at in collection_watch_dates.get(
                collection_name, []
            )
            if sibling_id != movie_id
        ]
        latest_by_movie_id[movie_id] = max(candidates) if candidates else None

    CollectionSiblingRuleDataResolver(latest_by_movie_id).activate()


def _parse_sonarr_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return ensure_utc(parsed)


def _sonarr_episode_season_number(episode: Mapping[str, object]) -> int | None:
    value = episode.get("seasonNumber")
    if not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _aggregate_sonarr_ref_field(
    states: Sequence[_SonarrRefRuleState],
    field: str,
) -> SonarrRuleValue:
    values = [getattr(state, field) for state in states]
    if any(value is True for value in values):
        return True
    if values and all(value is False for value in values):
        return False
    return RULE_VALUE_UNAVAILABLE


def _aggregate_sonarr_status(
    states: Sequence[_SonarrRefRuleState],
) -> SonarrRuleValue:
    if not states:
        return RULE_VALUE_UNAVAILABLE
    statuses = [state.series_status for state in states]
    if any(status is RULE_VALUE_UNAVAILABLE for status in statuses):
        return RULE_VALUE_UNAVAILABLE
    normalized = {str(status).strip().lower() for status in statuses if status}
    if len(normalized) != 1:
        return RULE_VALUE_UNAVAILABLE
    return normalized.pop()


def _sonarr_values_by_series(
    series_ids: Iterable[int],
    states_by_series_id: Mapping[int, Sequence[_SonarrRefRuleState]],
) -> dict[int, dict[str, SonarrRuleValue]]:
    return {
        series_id: {
            SONARR_UNAIRED_FIELD: _aggregate_sonarr_ref_field(
                states_by_series_id.get(series_id, []),
                "has_unaired_episodes",
            ),
            SONARR_FINALE_FIELD: _aggregate_sonarr_ref_field(
                states_by_series_id.get(series_id, []),
                "has_finale",
            ),
            SONARR_STATUS_FIELD: _aggregate_sonarr_status(
                states_by_series_id.get(series_id, [])
            ),
        }
        for series_id in series_ids
    }


async def _activate_sonarr_rule_data_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    sonarr_series_snapshot: _SonarrSeriesSnapshot | None = None,
) -> _SonarrRuleDataResult:
    """Load Sonarr-derived values needed by active TV rules."""
    watch_inventory_result = await _season_watch_inventory_rule_data(db, rules)
    sonarr_rules = [
        rule
        for rule in rules
        if normalize_rule_target(rule) in {TARGET_SERIES, TARGET_SEASON, TARGET_EPISODE}
        and _rule_uses_sonarr_fields(rule)
    ]
    if not sonarr_rules:
        SonarrRuleDataResolver({}).activate()
        return _SonarrRuleDataResult(
            unavailable_series_ids=set(),
            preserve_protection_keys=(watch_inventory_result.preserve_protection_keys),
        )

    episode_state_rules = [
        rule
        for rule in sonarr_rules
        if normalize_rule_target(rule) == TARGET_SERIES
        and any(
            collect_rule_conditions(rule.definition, field=field)
            for field in (SONARR_UNAIRED_FIELD, SONARR_FINALE_FIELD)
        )
    ]
    status_rules = [
        rule
        for rule in sonarr_rules
        if collect_rule_conditions(rule.definition, field=SONARR_STATUS_FIELD)
    ]

    query_options = [selectinload(Series.service_refs)]
    if _rules_use_field(sonarr_rules, "series.library_season_count"):
        query_options.append(selectinload(Series.seasons))
    series_rows = (
        (
            await db.execute(
                select(Series)
                .where(Series.removed_at.is_(None))
                .options(*query_options)
            )
        )
        .scalars()
        .all()
    )
    series_by_id = {series.id: series for series in series_rows}
    series_ids = set(series_by_id)

    ref_rows = (
        await db.execute(
            select(
                SeriesArrRef.series_id,
                SeriesArrRef.service_config_id,
                SeriesArrRef.arr_series_id,
            ).where(SeriesArrRef.series_id.in_(series_ids))
        )
    ).all()
    refs_by_series_id: dict[int, list[tuple[int, int]]] = {}
    config_ids: set[int] = set()
    for series_id, config_id, arr_series_id in ref_rows:
        refs_by_series_id.setdefault(series_id, []).append((config_id, arr_series_id))
        config_ids.add(config_id)

    sonarr_series_snapshot = sonarr_series_snapshot or _SonarrSeriesSnapshot()
    clients = sonarr_series_snapshot.clients
    if set(clients) == {0}:
        clients = {config_id: clients[0] for config_id in config_ids}

    errors: list[str] = []
    sonarr_series_by_config: dict[int, dict[int, Any]] = {}

    async def load_series(config_id: int, client: Any) -> None:
        try:
            items = await sonarr_series_snapshot.get_all_series(client)
        except Exception as exc:
            errors.append(
                f"config {config_id} series list: {summarize_error_message(str(exc))}"
            )
            return
        sonarr_series_by_config[config_id] = {
            item.id: item for item in items if getattr(item, "id", None) is not None
        }

    await asyncio.gather(
        *(load_series(config_id, client) for config_id, client in clients.items())
    )

    now = datetime.now(UTC)
    states_by_series_id: dict[int, list[_SonarrRefRuleState]] = {}
    for series_id, refs in refs_by_series_id.items():
        states: list[_SonarrRefRuleState] = []
        for config_id, arr_series_id in refs:
            state = _SonarrRefRuleState(
                config_id=config_id,
                arr_series_id=arr_series_id,
            )
            states.append(state)
            sonarr_series = sonarr_series_by_config.get(config_id, {}).get(
                arr_series_id
            )
            if sonarr_series is None:
                continue
            raw_status = str(getattr(sonarr_series, "status", "") or "").strip()
            if raw_status:
                state.series_status = raw_status.lower()
            regular_seasons = [
                season
                for season in getattr(sonarr_series, "seasons", [])
                if getattr(season, "season_number", 0) > 0
            ]
            if not regular_seasons:
                continue
            latest_season = max(
                regular_seasons,
                key=lambda season: season.season_number,
            )
            state.latest_season_number = latest_season.season_number
            statistics = getattr(latest_season, "statistics", None)
            next_airing = (
                _parse_sonarr_datetime(statistics.get("nextAiring"))
                if isinstance(statistics, Mapping)
                else None
            )
            if next_airing is not None and next_airing > now:
                state.has_unaired_episodes = True
        states_by_series_id[series_id] = states

    partial_values = _sonarr_values_by_series(series_ids, states_by_series_id)
    SonarrRuleDataResolver(partial_values).activate()

    needed_series_ids: set[int] = set()
    for series_id, series in series_by_id.items():
        if any(
            evaluate_advanced_rule_state(
                rule,
                target_scope=TARGET_SERIES,
                series=series,
            )
            is None
            for rule in episode_state_rules
        ):
            needed_series_ids.add(series_id)

    semaphores: dict[int, asyncio.Semaphore] = {}

    async def load_latest_season(
        series_id: int,
        state: _SonarrRefRuleState,
    ) -> None:
        if state.latest_season_number is None:
            return
        client = clients.get(state.config_id)
        if client is None:
            return
        semaphore = semaphores.setdefault(
            id(client), asyncio.Semaphore(SONARR_EPISODE_FETCH_CONCURRENCY)
        )
        try:
            async with semaphore:
                episodes = await client.get_episodes(
                    state.arr_series_id,
                    season_number=state.latest_season_number,
                )
        except Exception as exc:
            errors.append(
                "config "
                f"{state.config_id} series {state.arr_series_id} season "
                f"{state.latest_season_number}: {summarize_error_message(str(exc))}"
            )
            return

        latest_episodes = [
            episode
            for episode in episodes
            if _sonarr_episode_season_number(episode) == state.latest_season_number
            and state.latest_season_number > 0
        ]
        if not latest_episodes:
            return
        state.has_unaired_episodes = any(
            (air_date := _parse_sonarr_datetime(episode.get("airDateUtc"))) is not None
            and air_date > now
            for episode in latest_episodes
        )
        state.has_finale = any(
            str(episode.get("finaleType") or "").strip().lower() in {"season", "series"}
            for episode in latest_episodes
        )

    await asyncio.gather(
        *(
            load_latest_season(series_id, state)
            for series_id in needed_series_ids
            for state in states_by_series_id.get(series_id, [])
        )
    )

    final_values = _sonarr_values_by_series(series_ids, states_by_series_id)
    SonarrRuleDataResolver(final_values).activate()

    unavailable_series_ids: set[int] = set()
    preserve_protection_keys: set[SonarrProtectionPreserveKey] = set()
    for series_id, series in series_by_id.items():
        for rule in episode_state_rules:
            if (
                evaluate_advanced_rule_state(
                    rule,
                    target_scope=TARGET_SERIES,
                    series=series,
                )
                is not None
            ):
                continue
            unavailable_series_ids.add(series_id)
            if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT and isinstance(
                rule.id, int
            ):
                preserve_protection_keys.add((rule.id, series_id))

        if (
            status_rules
            and final_values[series_id][SONARR_STATUS_FIELD] is RULE_VALUE_UNAVAILABLE
        ):
            unavailable_series_ids.add(series_id)
            for rule in status_rules:
                if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT and isinstance(
                    rule.id, int
                ):
                    preserve_protection_keys.add((rule.id, series_id))

    error: str | None = None
    if unavailable_series_ids:
        if errors:
            details = "; ".join(errors[:3])
            suffix = f"; and {len(errors) - 3} more" if len(errors) > 3 else ""
            error = f"{details}{suffix}"
        elif not clients:
            error = "Sonarr is not configured"
        else:
            error = (
                "Sonarr returned no usable or consistent rule data for "
                f"{len(unavailable_series_ids)} series"
            )
        LOG.warning(
            "Sonarr rules have unavailable data for "
            f"{len(unavailable_series_ids)} series: {error}"
        )
    else:
        LOG.debug(
            "Activated Sonarr rule data for "
            f"{len(series_ids)} series; fetched episodes for "
            f"{len(needed_series_ids)} series"
        )

    preserve_protection_keys.update(watch_inventory_result.preserve_protection_keys)
    return _SonarrRuleDataResult(
        unavailable_series_ids=unavailable_series_ids,
        preserve_protection_keys=preserve_protection_keys,
        error=error,
    )


async def _refresh_arr_monitoring_for_rules(
    rules: list[ReclaimRule],
    *,
    sonarr_series_snapshot: _SonarrSeriesSnapshot | None = None,
) -> None:
    """Sync arr monitoring status (series + season for Sonarr, movie for Radarr) into the DB.

    Only runs if at least one active rule uses arr.monitored.
    Multi arr semantics: OR across instances - monitored=True wins.
    """
    if not _rules_use_monitoring_field(rules):
        return

    movie_rules = [r for r in rules if normalize_rule_target(r) == TARGET_MOVIE_VERSION]
    series_rules = [
        r
        for r in rules
        if normalize_rule_target(r) in {TARGET_SERIES, TARGET_SEASON, TARGET_EPISODE}
    ]

    #### radarr: refresh movie monitoring ####
    if movie_rules:
        radarr_clients = service_manager.radarr_clients()
        if not radarr_clients and service_manager.radarr:
            radarr_clients = {0: service_manager.radarr}

        if radarr_clients:
            async with async_db() as db:
                rows = (
                    await db.execute(
                        select(
                            MovieArrRef.arr_movie_id,
                            MovieArrRef.movie_id,
                            MovieArrRef.service_config_id,
                        )
                    )
                ).all()

            arr_to_db_by_config: dict[int, dict[int, int]] = {}
            for arr_movie_id, db_movie_id, config_id in rows:
                arr_to_db_by_config.setdefault(config_id, {})[arr_movie_id] = (
                    db_movie_id
                )

            # OR (accumulate monitoring across all instances)
            movie_monitored: dict[int, bool] = {}
            for config_id, client in radarr_clients.items():
                try:
                    all_movies = await client.get_all_movies()
                except Exception as e:
                    LOG.warning(
                        f"Failed to fetch Radarr movies for monitoring (config {config_id}): {e}"
                    )
                    continue

                arr_to_db = arr_to_db_by_config.get(config_id, {})
                for radarr_movie in all_movies:
                    db_id = arr_to_db.get(radarr_movie.id)
                    if db_id is not None:
                        movie_monitored[db_id] = (
                            movie_monitored.get(db_id, False) or radarr_movie.monitored
                        )

            if movie_monitored:
                async with async_db() as db:
                    result = await db.execute(
                        select(Movie).where(Movie.id.in_(movie_monitored.keys()))
                    )
                    for movie in result.scalars().all():
                        movie.is_monitored = movie_monitored[movie.id]
                    await db.commit()
                LOG.debug(f"Refreshed arr monitoring for {len(movie_monitored)} movies")

    #### sonarr: refresh series + season monitoring ####
    if series_rules:
        sonarr_series_snapshot = sonarr_series_snapshot or _SonarrSeriesSnapshot()
        sonarr_clients = sonarr_series_snapshot.clients

        if sonarr_clients:
            async with async_db() as db:
                rows = (
                    await db.execute(
                        select(
                            SeriesArrRef.arr_series_id,
                            SeriesArrRef.series_id,
                            SeriesArrRef.service_config_id,
                        )
                    )
                ).all()

            sonarr_arr_to_db_by_config: dict[int, dict[int, int]] = {}
            for arr_series_id, db_series_id, config_id in rows:
                sonarr_arr_to_db_by_config.setdefault(config_id, {})[arr_series_id] = (
                    db_series_id
                )

            # OR (accumulate series monitoring and per-season monitoring across instances)
            series_monitored: dict[int, bool] = {}
            # (db_series_id, season_number) -> monitored
            season_monitored: dict[tuple[int, int], bool] = {}

            for config_id, sonarr_client in sonarr_clients.items():
                try:
                    all_sonarr_series = await sonarr_series_snapshot.get_all_series(
                        sonarr_client
                    )
                except Exception as e:
                    LOG.warning(
                        f"Failed to fetch Sonarr series for monitoring (config {config_id}): {e}"
                    )
                    continue

                arr_to_db = sonarr_arr_to_db_by_config.get(config_id, {})
                for sonarr_series in all_sonarr_series:
                    db_id = arr_to_db.get(sonarr_series.id)
                    if db_id is None:
                        continue
                    series_monitored[db_id] = (
                        series_monitored.get(db_id, False) or sonarr_series.monitored
                    )
                    for sonarr_season in sonarr_series.seasons:
                        key = (db_id, sonarr_season.season_number)
                        season_monitored[key] = (
                            season_monitored.get(key, False) or sonarr_season.monitored
                        )

            if series_monitored:
                async with async_db() as db:
                    # update series monitoring
                    series_result = await db.execute(
                        select(Series).where(Series.id.in_(series_monitored.keys()))
                    )
                    for series in series_result.scalars().all():
                        series.is_monitored = series_monitored[series.id]

                    # update season monitoring (load all seasons for tracked series)
                    if season_monitored:
                        season_result = await db.execute(
                            select(Season).where(
                                Season.series_id.in_(series_monitored.keys())
                            )
                        )
                        for season in season_result.scalars().all():
                            key = (season.series_id, season.season_number)
                            if key in season_monitored:
                                season.is_monitored = season_monitored[key]

                    await db.commit()
                LOG.debug(
                    f"Refreshed arr monitoring for {len(series_monitored)} series "
                    f"and {len(season_monitored)} season entries"
                )


def _rule_uses_seerr_fields(rule: ReclaimRule) -> bool:
    """Return True if the rule conditions reference any Seerr request state fields."""
    for _ in collect_rule_conditions(rule.definition, field="seerr.requested"):
        return True
    for _ in collect_rule_conditions(
        rule.definition, field="seerr.requested_by_user_ids"
    ):
        return True
    for field in (
        "seerr.requester_has_watched",
        "seerr.requester_watched_after_request",
    ):
        for _ in collect_rule_conditions(rule.definition, field=field):
            return True
    for field in ("seerr.last_requested_at", "seerr.days_since_last_requested"):
        for _ in collect_rule_conditions(rule.definition, field=field):
            return True
    return False


def _rule_uses_season_episode_watch_fields(rule: ReclaimRule) -> bool:
    """Return True if the rule references season episode-level watch progress fields."""
    for _ in collect_rule_conditions(rule.definition, field="season.fully_watched"):
        return True
    for _ in collect_rule_conditions(rule.definition, field="season.watched_percent"):
        return True
    return False


def _rules_use_season_episode_watch_fields(rules: list[ReclaimRule]) -> bool:
    """Return True if any rule references season episode-level watch progress fields."""
    return any(_rule_uses_season_episode_watch_fields(r) for r in rules)


def _rules_use_seerr_fields(rules: list[ReclaimRule]) -> bool:
    """Return True if any of the rules reference Seerr request state fields."""
    return any(_rule_uses_seerr_fields(r) for r in rules)


def _normalize_watch_key(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def _extract_requester_mapping_identity(
    requester_id: int,
    requester_identity_keys: set[str],
    mapping: dict[str, Any],
) -> bool:
    raw_id = mapping.get("seerr_user_id")
    if raw_id is not None:
        try:
            return int(raw_id) == requester_id
        except Exception:
            return False
    raw_name = _normalize_watch_key(mapping.get("seerr_username"))
    return bool(raw_name and raw_name in requester_identity_keys)


def _extract_mapping_service(mapping: dict[str, Any]) -> Service | None:
    raw_service = mapping.get("service_type")
    if raw_service is None:
        return None
    try:
        if isinstance(raw_service, Service):
            return raw_service
        return Service(str(raw_service))
    except Exception:
        return None


def _build_watch_keys_for_requester(
    *,
    requester_id: int,
    requester_identity_keys: set[str],
    target_service: Service,
    mappings: list[dict[str, Any]],
    alias_index: AliasIndex | None = None,
) -> set[str]:
    keys: set[str] = set()
    for mapping in mappings:
        if not _extract_requester_mapping_identity(
            requester_id, requester_identity_keys, mapping
        ):
            continue
        service_scope = _extract_mapping_service(mapping)
        if service_scope is not None and service_scope is not target_service:
            continue
        media_user_key = _normalize_watch_key(mapping.get("media_user_key"))
        if media_user_key:
            keys.add(media_user_key)

    # fallback by direct normalized identity keys
    keys.update(requester_identity_keys)
    if alias_index is None:
        return keys
    # One known name is enough: the registry supplies the rest of the names the
    # same provider account answers to, which is how a Seerr display name
    # reaches a Plex history key or a Tracearr username.
    return expand_watch_keys(keys, alias_index, target_service)


# (target_scope, tmdb_id, season_number, episode_number) -> watched
RequesterWatchTargets = dict[tuple[str, int, int | None, int | None], bool]


def _requested_seasons_for_requester(
    *,
    snapshot: SeerrRequestSnapshot,
    tmdb_id: int,
    requester_id: int,
    series_requested_at: datetime,
    season_numbers: Iterable[int],
) -> dict[int, datetime]:
    """Return the earliest request date this requester has per season.

    Older Seerr responses do not identify requested seasons at all; those fall
    back to the series request date so the season is still judged rather than
    silently skipped.
    """
    requested_seasons = {
        season_number: by_user[requester_id]
        for (series_tmdb_id, season_number), by_user in (
            snapshot.first_request_at_by_series_season_user.items()
        )
        if series_tmdb_id == tmdb_id and requester_id in by_user
    }
    if not requested_seasons:
        requested_seasons = {
            season_number: series_requested_at for season_number in season_numbers
        }
    return requested_seasons


def _requester_watched_at_by_coordinate(
    *,
    requester_id: int,
    requester_identity_keys: set[str],
    watch_by_service_and_user: Mapping[
        Service, Mapping[str, Mapping[tuple[int, int], datetime]]
    ],
    mappings: list[dict[str, Any]],
    alias_index: AliasIndex | None,
) -> dict[tuple[int, int], datetime]:
    """Latest completed play per episode for one requester, across every source.

    One person can be recorded under several names on the same server, so the
    keys are expanded first and the newest play per episode wins.
    """
    watched_at_by_coordinate: dict[tuple[int, int], datetime] = {}
    for watch_service, watch_by_user in watch_by_service_and_user.items():
        candidate_keys = _build_watch_keys_for_requester(
            requester_id=requester_id,
            requester_identity_keys=requester_identity_keys,
            target_service=watch_service,
            mappings=mappings,
            alias_index=alias_index,
        )
        for watch_key in candidate_keys:
            for coordinate, watched_at in watch_by_user.get(watch_key, {}).items():
                watched_at_utc = ensure_utc(watched_at)
                existing = watched_at_by_coordinate.get(coordinate)
                if existing is None or watched_at_utc > existing:
                    watched_at_by_coordinate[coordinate] = watched_at_utc
    return watched_at_by_coordinate


def _requester_movie_watched_at(
    *,
    requester_id: int,
    requester_identity_keys: set[str],
    watch_by_service_and_user: Mapping[Service, Mapping[str, datetime]],
    mappings: list[dict[str, Any]],
    alias_index: AliasIndex | None,
) -> datetime | None:
    """Latest completed movie play for one requester, across every source."""
    latest_watched_at: datetime | None = None
    for watch_service, watch_by_user in watch_by_service_and_user.items():
        candidate_keys = _build_watch_keys_for_requester(
            requester_id=requester_id,
            requester_identity_keys=requester_identity_keys,
            target_service=watch_service,
            mappings=mappings,
            alias_index=alias_index,
        )
        for watch_key in candidate_keys:
            watched_at = watch_by_user.get(watch_key)
            if watched_at is None:
                continue
            watched_at_utc = ensure_utc(watched_at)
            if latest_watched_at is None or watched_at_utc > latest_watched_at:
                latest_watched_at = watched_at_utc
    return latest_watched_at


def _compute_requester_has_watched_for_key(
    *,
    media_key: tuple[MediaType, int],
    snapshot: SeerrRequestSnapshot,
    watch_by_service_and_user: Mapping[
        tuple[MediaType, int], Mapping[Service, Mapping[str, datetime]]
    ],
    mappings: list[dict[str, Any]],
    alias_index: AliasIndex | None = None,
) -> tuple[bool, bool]:
    """Return (a requester watched it, a requester watched it after requesting).

    Both answers come from one pass because they share the expensive half --
    expanding each requester's identities into the keys providers actually
    recorded plays under.
    """
    watches_for_key = watch_by_service_and_user.get(media_key)
    if not watches_for_key:
        return False, False
    requester_times = snapshot.first_request_at_by_key_user.get(media_key, {})
    if not requester_times:
        return False, False

    watched_ever = False
    for requester_id, requested_at in requester_times.items():
        requested_at_utc = ensure_utc(requested_at)
        requester_identity_keys = snapshot.requester_identity_keys_by_user_id.get(
            requester_id, set()
        )
        if not requester_identity_keys:
            # retain user-id path if username/display_name wasn't present in request payload
            requester_identity_keys = {str(requester_id)}
        watched_at = _requester_movie_watched_at(
            requester_id=requester_id,
            requester_identity_keys=requester_identity_keys,
            watch_by_service_and_user=watches_for_key,
            mappings=mappings,
            alias_index=alias_index,
        )
        if watched_at is None:
            continue
        watched_ever = True
        if watched_at > requested_at_utc:
            # Nothing found later can improve either answer.
            return True, True
    return watched_ever, False


def _compute_requester_tv_watch_targets_for_key(
    *,
    media_key: tuple[MediaType, int],
    snapshot: SeerrRequestSnapshot,
    watch_by_service_and_user: Mapping[
        Service, Mapping[str, Mapping[tuple[int, int], datetime]]
    ],
    mappings: list[dict[str, Any]],
    expected_episodes: set[tuple[int, int]],
    alias_index: AliasIndex | None = None,
) -> tuple[RequesterWatchTargets, RequesterWatchTargets]:
    """Compute requester-specific episode and completion state for one series.

    Returns two target maps: watched-ever, which asks only whether one requester
    watched every required episode, and watched-after-request, which also
    requires each play to postdate that requester's earliest request for the
    season. They are computed together because the costly half -- expanding
    identities into provider watch keys -- is shared.
    """
    tmdb_id = media_key[1]
    expected_by_season: dict[int, set[tuple[int, int]]] = {}
    for coordinate in expected_episodes:
        expected_by_season.setdefault(coordinate[0], set()).add(coordinate)

    def _blank() -> RequesterWatchTargets:
        blank: RequesterWatchTargets = {
            (TARGET_EPISODE, tmdb_id, season_number, episode_number): False
            for season_number, episode_number in expected_episodes
        }
        for season_number in expected_by_season:
            blank[(TARGET_SEASON, tmdb_id, season_number, None)] = False
        blank[(TARGET_SERIES, tmdb_id, None, None)] = False
        return blank

    def _apply(
        result: RequesterWatchTargets,
        watched: set[tuple[int, int]],
        requested_seasons: Mapping[int, datetime] | None,
    ) -> None:
        """Roll one requester's watched episodes up to season and series targets.

        A ``requested_seasons`` of None means "do not require a request record
        for the season", which is what separates membership from the gated
        answer.
        """
        for season_number, episode_number in watched & expected_episodes:
            result[(TARGET_EPISODE, tmdb_id, season_number, episode_number)] = True
        for season_number, season_episodes in expected_by_season.items():
            if requested_seasons is not None and season_number not in requested_seasons:
                continue
            if season_episodes and season_episodes.issubset(watched):
                result[(TARGET_SEASON, tmdb_id, season_number, None)] = True
        regular_episodes = {
            coordinate
            for coordinate in expected_episodes
            if coordinate[0] > 0
            and (requested_seasons is None or coordinate[0] in requested_seasons)
        }
        if regular_episodes and regular_episodes.issubset(watched):
            result[(TARGET_SERIES, tmdb_id, None, None)] = True

    ever = _blank()
    after = _blank()

    requester_times = snapshot.first_request_at_by_key_user.get(media_key, {})
    for requester_id, series_requested_at in requester_times.items():
        requested_seasons = _requested_seasons_for_requester(
            snapshot=snapshot,
            tmdb_id=tmdb_id,
            requester_id=requester_id,
            series_requested_at=series_requested_at,
            season_numbers=expected_by_season,
        )
        requester_identity_keys = snapshot.requester_identity_keys_by_user_id.get(
            requester_id, set()
        ) or {str(requester_id)}
        watched_at_by_coordinate = _requester_watched_at_by_coordinate(
            requester_id=requester_id,
            requester_identity_keys=requester_identity_keys,
            watch_by_service_and_user=watch_by_service_and_user,
            mappings=mappings,
            alias_index=alias_index,
        )

        watched_after: set[tuple[int, int]] = set()
        for coordinate, watched_at in watched_at_by_coordinate.items():
            requested_at = requested_seasons.get(coordinate[0])
            if requested_at is not None and watched_at > ensure_utc(requested_at):
                watched_after.add(coordinate)

        _apply(ever, set(watched_at_by_coordinate), None)
        _apply(after, watched_after, requested_seasons)

    return ever, after


async def _unobservable_requester_watch_keys(
    db: AsyncSession,
    *,
    movie_tmdb_ids: set[int],
    series_tmdb_ids: set[int],
    durable_completion_services: set[Service],
) -> set[tuple[MediaType, int]]:
    """Return media whose requester watch state cannot honestly be answered.

    "Nobody watched this" and "we could not read the server that would know"
    used to be the same False, and an ``is false`` rule deletes on both. An item
    stops being answerable when a media server known to hold it cannot report
    completion, from either its own watch snapshot or retained durable history.
    """
    (
        configured,
        observable,
    ) = await media_watch_snapshot_cache.load_observable_watch_services(db)
    trusted = observable | durable_completion_services
    if not trusted:
        # Nothing anywhere can report completion, so no answer is honest.
        return {(MediaType.MOVIE, tmdb_id) for tmdb_id in movie_tmdb_ids} | {
            (MediaType.SERIES, tmdb_id) for tmdb_id in series_tmdb_ids
        }

    unobservable_services = configured - trusted
    if not unobservable_services:
        return set()

    services_by_key: dict[tuple[MediaType, int], set[Service]] = {}
    if movie_tmdb_ids:
        rows = (
            await db.execute(
                select(Movie.tmdb_id, MovieVersion.service)
                .join(MovieVersion, MovieVersion.movie_id == Movie.id)
                .where(Movie.tmdb_id.in_(movie_tmdb_ids))
            )
        ).all()
        for tmdb_id, service in rows:
            if tmdb_id is not None:
                services_by_key.setdefault((MediaType.MOVIE, int(tmdb_id)), set()).add(
                    service
                )
    if series_tmdb_ids:
        rows = (
            await db.execute(
                select(Series.tmdb_id, SeriesServiceRef.service)
                .join(SeriesServiceRef, SeriesServiceRef.series_id == Series.id)
                .where(Series.tmdb_id.in_(series_tmdb_ids))
            )
        ).all()
        for tmdb_id, service in rows:
            if tmdb_id is not None:
                services_by_key.setdefault((MediaType.SERIES, int(tmdb_id)), set()).add(
                    service
                )

    unobservable: set[tuple[MediaType, int]] = set()
    for media_type, tmdb_ids in (
        (MediaType.MOVIE, movie_tmdb_ids),
        (MediaType.SERIES, series_tmdb_ids),
    ):
        for tmdb_id in tmdb_ids:
            key = (media_type, int(tmdb_id))
            holding = services_by_key.get(key)
            # An item with no recorded server still has the trusted sources
            # above to answer for it; only a server we know holds it and cannot
            # read makes the answer untrustworthy.
            if holding and holding & unobservable_services:
                unobservable.add(key)
    if unobservable:
        LOG.debug(
            f"Requester watch state is unknown for {len(unobservable)} item(s); "
            f"unobservable services: "
            f"{sorted(service.value for service in unobservable_services) or 'none'}"
        )
    return unobservable


def _episode_label(season_number: int, episode_number: int) -> str:
    return f"S{season_number:02d}E{episode_number:02d}"


async def explain_requester_watch(
    db: AsyncSession,
    *,
    media_type: MediaType,
    tmdb_id: int,
    target_scope: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> RequesterWatchExplainResponse:
    """Explain how `Seerr requester has watched` resolves for one item.

    Every failure in this area renders as a bare true/false/unknown, which makes
    reports unanswerable without a screenshot exchange. This reports the whole
    chain -- who requested it, which names were tried, what completion evidence
    exists, and which server could not be read -- so a mismatch is visible.

    The verdict comes from the same helpers the scan uses, so it cannot disagree
    with what a rule would actually do.
    """
    is_series = media_type is MediaType.SERIES
    scope = target_scope or (TARGET_SERIES if is_series else TARGET_MOVIE_VERSION)
    media_key = (media_type, int(tmdb_id))

    title: str | None = None
    if is_series:
        title = await db.scalar(select(Series.title).where(Series.tmdb_id == tmdb_id))
    else:
        title = await db.scalar(select(Movie.title).where(Movie.tmdb_id == tmdb_id))

    if not service_manager.seerr:
        return RequesterWatchExplainResponse(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            target_scope=scope,
            season_number=season_number,
            episode_number=episode_number,
            result=None,
            reason="Seerr is not configured, so requester conditions never match.",
        )

    snapshot, snapshot_error = await seerr_snapshot_cache.get_request_snapshot(
        require_fresh=False,
        allow_stale_on_failure=True,
    )
    if snapshot is None:
        return RequesterWatchExplainResponse(
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            target_scope=scope,
            season_number=season_number,
            episode_number=episode_number,
            result=None,
            reason=(
                "Seerr request data could not be loaded: "
                f"{snapshot_error or 'unknown error'}"
            ),
        )

    settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
    raw_mappings = (
        settings_row.requester_watch_user_mappings if settings_row is not None else []
    )
    mappings = [m for m in raw_mappings if isinstance(m, dict)]
    ignore_request_date = bool(
        settings_row.requester_watch_ignore_request_date
        if settings_row is not None
        else False
    )
    alias_index = await load_watch_user_alias_index(db)
    active_tracearr_sources = await load_active_tracearr_sources(db)

    # Native per-user watch state.
    watch_by_service_and_user: dict[
        tuple[MediaType, int], dict[Service, dict[str, datetime]]
    ] = {}
    episode_watches: dict[Service, dict[str, dict[tuple[int, int], datetime]]] = {}
    if is_series:
        rows = (
            await db.execute(
                select(
                    MediaWatchUserEpisode.source_service,
                    MediaWatchUserEpisode.watch_user_key_normalized,
                    MediaWatchUserEpisode.season_number,
                    MediaWatchUserEpisode.episode_number,
                    MediaWatchUserEpisode.last_watched_at,
                ).where(MediaWatchUserEpisode.series_tmdb_id == tmdb_id)
            )
        ).all()
        for source_service, user_key, season_no, episode_no, watched_at in rows:
            key = _normalize_watch_key(user_key)
            if key is None or watched_at is None:
                continue
            episode_watches.setdefault(source_service, {}).setdefault(key, {})[
                (int(season_no), int(episode_no))
            ] = ensure_utc(watched_at)
    else:
        rows = (
            await db.execute(
                select(
                    MediaWatchUser.source_service,
                    MediaWatchUser.watch_user_key_normalized,
                    MediaWatchUser.last_watched_at,
                ).where(
                    MediaWatchUser.media_type == media_type,
                    MediaWatchUser.tmdb_id == tmdb_id,
                )
            )
        ).all()
        for source_service, user_key, watched_at in rows:
            key = _normalize_watch_key(user_key)
            if key is None or watched_at is None:
                continue
            by_user = watch_by_service_and_user.setdefault(media_key, {}).setdefault(
                source_service, {}
            )
            existing = by_user.get(key)
            watched_at_utc = ensure_utc(watched_at)
            if existing is None or watched_at_utc > existing:
                by_user[key] = watched_at_utc

    # Durable completion evidence.
    durable_completion_services: set[Service] = set()
    durable_rows = (
        await db.execute(
            select(
                PlaybackHistoryEvent.source_service,
                PlaybackHistoryEvent.observed_service,
                PlaybackHistoryEvent.provider_media_type,
                PlaybackHistoryEvent.season_number,
                PlaybackHistoryEvent.episode_number,
                PlaybackHistoryEvent.source_username,
                PlaybackHistoryEvent.source_user_id,
                PlaybackHistoryEvent.played_at,
            ).where(
                PlaybackHistoryEvent.completed.is_(True),
                PlaybackHistoryEvent.tmdb_id == tmdb_id,
                PlaybackHistoryEvent.provider_media_type.in_(("movie", "episode")),
                completion_evidence_event_clause(active_tracearr_sources),
            )
        )
    ).all()
    for (
        source_service,
        observed_service,
        provider_media_type,
        season_no,
        episode_no,
        source_username,
        source_user_id,
        played_at,
    ) in durable_rows:
        key = _normalize_watch_key(source_username or source_user_id)
        if key is None or played_at is None:
            continue
        watch_service = observed_service or (
            Service.PLEX if source_service is Service.TAUTULLI else source_service
        )
        durable_completion_services.add(watch_service)
        watched_at_utc = ensure_utc(played_at)
        if provider_media_type == "movie" and not is_series:
            by_user = watch_by_service_and_user.setdefault(media_key, {}).setdefault(
                watch_service, {}
            )
            existing = by_user.get(key)
            if existing is None or watched_at_utc > existing:
                by_user[key] = watched_at_utc
        elif is_series and season_no is not None and episode_no is not None:
            coordinates = episode_watches.setdefault(watch_service, {}).setdefault(
                key, {}
            )
            coordinate = (int(season_no), int(episode_no))
            existing = coordinates.get(coordinate)
            if existing is None or watched_at_utc > existing:
                coordinates[coordinate] = watched_at_utc

    expected_episodes: set[tuple[int, int]] = set()
    if is_series:
        rows = (
            await db.execute(
                select(Season.season_number, Episode.episode_number)
                .join(Season, Episode.season_id == Season.id)
                .join(Series, Season.series_id == Series.id)
                .where(Series.tmdb_id == tmdb_id)
            )
        ).all()
        expected_episodes = {
            (int(season_no), int(episode_no)) for season_no, episode_no in rows
        }

    # The verdict needs the whole series (a series target rolls every season
    # up), but "required" as reported to a person means the episodes this
    # particular target depends on -- a season target listing another season's
    # episodes reads as a bug.
    if not is_series:
        target_episodes: set[tuple[int, int]] = set()
    elif scope == TARGET_EPISODE and season_number is not None:
        target_episodes = {
            coordinate
            for coordinate in expected_episodes
            if coordinate == (season_number, episode_number)
        }
    elif scope == TARGET_SEASON and season_number is not None:
        target_episodes = {
            coordinate
            for coordinate in expected_episodes
            if coordinate[0] == season_number
        }
    else:
        # Season 0 never counts toward series completion.
        target_episodes = {
            coordinate for coordinate in expected_episodes if coordinate[0] > 0
        }

    unobservable_keys = await _unobservable_requester_watch_keys(
        db,
        movie_tmdb_ids=set() if is_series else {int(tmdb_id)},
        series_tmdb_ids={int(tmdb_id)} if is_series else set(),
        durable_completion_services=durable_completion_services,
    )

    holding_services: set[Service] = set()
    if is_series:
        holding_services = {
            service
            for (service,) in (
                await db.execute(
                    select(SeriesServiceRef.service)
                    .join(Series, SeriesServiceRef.series_id == Series.id)
                    .where(Series.tmdb_id == tmdb_id)
                )
            ).all()
        }
    else:
        holding_services = {
            service
            for (service,) in (
                await db.execute(
                    select(MovieVersion.service)
                    .join(Movie, MovieVersion.movie_id == Movie.id)
                    .where(Movie.tmdb_id == tmdb_id)
                )
            ).all()
        }
    (
        configured,
        observable,
    ) = await media_watch_snapshot_cache.load_observable_watch_services(db)
    unobservable_services = configured - (observable | durable_completion_services)

    # Per-requester detail, using the same key expansion the scan performs.
    requester_times = snapshot.first_request_at_by_key_user.get(media_key, {})
    evidence_services = set(episode_watches) | set(
        watch_by_service_and_user.get(media_key, {})
    )
    requesters: list[RequesterWatchRequesterDetail] = []
    keys_by_requester: dict[int, set[str]] = {}
    for requester_id, requested_at in sorted(requester_times.items()):
        identity_keys = snapshot.requester_identity_keys_by_user_id.get(
            requester_id, set()
        ) or {str(requester_id)}
        candidate_keys: dict[str, list[str]] = {}
        all_keys: set[str] = set()
        for service in sorted(evidence_services or {Service.PLEX}, key=str):
            keys = _build_watch_keys_for_requester(
                requester_id=requester_id,
                requester_identity_keys=set(identity_keys),
                target_service=service,
                mappings=mappings,
                alias_index=alias_index,
            )
            candidate_keys[str(service.value)] = sorted(keys)
            all_keys |= keys
        keys_by_requester[requester_id] = all_keys
        user = snapshot.requester_users_by_id.get(requester_id)

        missing: list[str] = []
        too_early: list[str] = []
        movie_watched_at: datetime | None = None
        movie_watched_before_request = False
        if is_series and target_episodes:
            requested_seasons = _requested_seasons_for_requester(
                snapshot=snapshot,
                tmdb_id=int(tmdb_id),
                requester_id=requester_id,
                series_requested_at=requested_at,
                season_numbers={coordinate[0] for coordinate in expected_episodes},
            )
            watched_at_by_coordinate = _requester_watched_at_by_coordinate(
                requester_id=requester_id,
                requester_identity_keys=set(identity_keys),
                watch_by_service_and_user=episode_watches,
                mappings=mappings,
                alias_index=alias_index,
            )
            for coordinate in sorted(target_episodes):
                watched_at = watched_at_by_coordinate.get(coordinate)
                if watched_at is None:
                    missing.append(_episode_label(*coordinate))
                    continue
                season_requested_at = requested_seasons.get(coordinate[0])
                if season_requested_at is None or watched_at <= ensure_utc(
                    season_requested_at
                ):
                    too_early.append(_episode_label(*coordinate))
        elif not is_series:
            movie_watched_at = _requester_movie_watched_at(
                requester_id=requester_id,
                requester_identity_keys=set(identity_keys),
                watch_by_service_and_user=watch_by_service_and_user.get(
                    media_key, {}
                ),
                mappings=mappings,
                alias_index=alias_index,
            )
            movie_watched_before_request = (
                movie_watched_at is not None
                and movie_watched_at <= ensure_utc(requested_at)
            )

        requesters.append(
            RequesterWatchRequesterDetail(
                seerr_user_id=requester_id,
                display_name=(user.display_name or user.username) if user else None,
                identity_keys=sorted(identity_keys),
                requested_at=ensure_utc(requested_at),
                requested_seasons={
                    season_no: ensure_utc(by_user[requester_id])
                    for (series_tmdb_id, season_no), by_user in (
                        snapshot.first_request_at_by_series_season_user.items()
                    )
                    if series_tmdb_id == tmdb_id and requester_id in by_user
                },
                candidate_watch_keys=candidate_keys,
                missing_episodes=missing,
                episodes_watched_before_request=too_early,
                movie_watched_at=movie_watched_at,
                movie_watched_before_request=movie_watched_before_request,
            )
        )

    def matched_requesters(watch_key: str) -> list[int]:
        return sorted(
            requester_id
            for requester_id, keys in keys_by_requester.items()
            if watch_key in keys
        )

    evidence: list[RequesterWatchEvidence] = []
    for service, by_user in sorted(episode_watches.items(), key=lambda item: str(item)):
        for watch_key, coordinates in sorted(by_user.items()):
            evidence.append(
                RequesterWatchEvidence(
                    source_service=service,
                    watch_user_key=watch_key,
                    matched_requester_ids=matched_requesters(watch_key),
                    watched_at=max(coordinates.values(), default=None),
                    episodes=sorted(
                        _episode_label(*coordinate) for coordinate in coordinates
                    ),
                )
            )
    for service, by_user in sorted(
        watch_by_service_and_user.get(media_key, {}).items(), key=lambda item: str(item)
    ):
        for watch_key, watched_at in sorted(by_user.items()):
            evidence.append(
                RequesterWatchEvidence(
                    source_service=service,
                    watch_user_key=watch_key,
                    matched_requester_ids=matched_requesters(watch_key),
                    watched_at=watched_at,
                )
            )

    # Both verdicts, from the same helpers a scan uses, so an explanation can
    # never disagree with what a rule would actually do.
    result: bool | None
    result_after_request: bool | None
    if media_key in unobservable_keys:
        result = None
        result_after_request = None
        reason = (
            "Unknown: no media server holding this item can report completion "
            f"({', '.join(sorted(s.value for s in unobservable_services)) or 'none'} "
            "unreadable)."
        )
    elif is_series:
        target_key = (scope, tmdb_id, season_number, episode_number)
        ever_targets, after_targets = _compute_requester_tv_watch_targets_for_key(
            media_key=media_key,
            snapshot=snapshot,
            watch_by_service_and_user=episode_watches,
            mappings=mappings,
            expected_episodes=expected_episodes,
            alias_index=alias_index,
        )
        result = ever_targets.get(target_key)
        result_after_request = after_targets.get(target_key)
        if not requester_times:
            reason = "No active Seerr request records a requester for this item."
        elif result is None:
            reason = (
                "Unknown: this target has no locally known episodes to judge "
                "completion against."
            )
        elif result and ignore_request_date:
            reason = (
                "A requester watched every required episode. Request dates are "
                "ignored in User Signals, so plays from before the request "
                "still count."
            )
        elif result and result_after_request:
            reason = "A requester watched every required episode after requesting it."
        elif result:
            reason = (
                "A requester watched every required episode, but not all of it "
                "after their earliest request for the season."
            )
        else:
            reason = (
                "No single requester has a completed watch for every required "
                "episode."
            )
    else:
        if not requester_times:
            result = False
            result_after_request = False
            reason = "No active Seerr request records a requester for this item."
        else:
            result, result_after_request = _compute_requester_has_watched_for_key(
                media_key=media_key,
                snapshot=snapshot,
                watch_by_service_and_user=watch_by_service_and_user,
                mappings=mappings,
                alias_index=alias_index,
            )
            if result and ignore_request_date:
                reason = (
                    "A requester has a completed watch. Request dates are "
                    "ignored in User Signals, so a play from before the request "
                    "still counts."
                )
            elif result and result_after_request:
                reason = "A requester has a completed watch after requesting it."
            elif result:
                reason = (
                    "A requester has a completed watch, but only from before "
                    "their earliest request."
                )
            else:
                reason = "No requester has a completed watch for this item."

    if ignore_request_date and result is not None:
        result_after_request = result

    return RequesterWatchExplainResponse(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        target_scope=scope,
        season_number=season_number,
        episode_number=episode_number,
        result=result,
        result_after_request=result_after_request,
        request_date_gate_ignored=ignore_request_date,
        reason=reason,
        holding_services=sorted(holding_services, key=str),
        unobservable_services=sorted(unobservable_services, key=str),
        requesters=requesters,
        expected_episodes=sorted(
            _episode_label(*coordinate) for coordinate in target_episodes
        ),
        evidence=evidence,
    )


async def _activate_seerr_request_resolver_for_rules(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    require_fresh: bool,
    allow_stale_on_failure: bool,
    metadata: RulePreviewMatchMetadata | None = None,
) -> tuple[bool, str | None]:
    """Activate scan local Seerr request state for rules that reference Seerr fields."""
    if not _rules_use_seerr_fields(rules):
        return True, None

    if not service_manager.seerr:
        LOG.warning(
            "Rule uses Seerr fields but Seerr service is not configured; "
            "Seerr rule conditions will not match"
        )
        SeerrRequestResolver({}, requester_has_watched_by_key={}).activate()
        return False, "Seerr service is not configured"

    if any(
        collect_rule_conditions(rule.definition, field=field)
        for rule in rules
        for field in (
            "seerr.requester_has_watched",
            "seerr.requester_watched_after_request",
        )
    ):
        # Requester watch state is read straight out of the persisted watch
        # tables, so without this it silently reflects whenever Sync Media last
        # ran. Match the playback path: refresh if stale for previews, require
        # current data for scans.
        await db.commit()
        watch_ok, watch_error = await media_watch_snapshot_cache.ensure_fresh_snapshot(
            force=require_fresh,
            allow_stale_on_failure=allow_stale_on_failure,
        )
        if not watch_ok and watch_error:
            LOG.warning(
                "Watch snapshot refresh failed; requester watch conditions may "
                f"be evaluated against stale data: {watch_error}"
            )
        elif watch_error:
            LOG.debug(f"Using stale watch snapshot for requester rules: {watch_error}")

    movie_targets = any(normalize_rule_target(r) == TARGET_MOVIE_VERSION for r in rules)
    series_targets = any(
        normalize_rule_target(r) in {TARGET_SERIES, TARGET_SEASON, TARGET_EPISODE}
        for r in rules
    )

    movie_tmdb_ids: set[int] = set()
    series_tmdb_ids: set[int] = set()

    if movie_targets:
        rows = (
            await db.execute(select(Movie.tmdb_id).where(Movie.removed_at.is_(None)))
        ).all()
        movie_tmdb_ids = {tmdb_id for (tmdb_id,) in rows if tmdb_id is not None}

    if series_targets:
        rows = (
            await db.execute(select(Series.tmdb_id).where(Series.removed_at.is_(None)))
        ).all()
        series_tmdb_ids = {tmdb_id for (tmdb_id,) in rows if tmdb_id is not None}

    snapshot, snapshot_error = await seerr_snapshot_cache.get_request_snapshot(
        require_fresh=require_fresh,
        allow_stale_on_failure=allow_stale_on_failure,
    )
    if snapshot is None:
        SeerrRequestResolver({}, requester_has_watched_by_key={}).activate()
        return False, snapshot_error or "Failed to load Seerr request snapshot"

    requester_ids_by_key: dict[tuple[MediaType, int], set[int]] = {
        (MediaType.MOVIE, tmdb_id): set() for tmdb_id in movie_tmdb_ids
    }
    requester_ids_by_key.update(
        {(MediaType.SERIES, tmdb_id): set() for tmdb_id in series_tmdb_ids}
    )
    for key, user_ids in snapshot.requester_ids_by_key.items():
        media_type, tmdb_id = key
        if media_type is MediaType.MOVIE and tmdb_id not in movie_tmdb_ids:
            continue
        if media_type is MediaType.SERIES and tmdb_id not in series_tmdb_ids:
            continue
        if key not in requester_ids_by_key:
            continue
        requester_ids_by_key[key].update(user_ids)

    latest_active_request_at_by_key = {
        key: requested_at
        for key, requested_at in snapshot.latest_active_request_at_by_key.items()
        if key in requester_ids_by_key
    }

    relevant_keys = set(requester_ids_by_key.keys())
    watch_rows = (
        await db.execute(
            select(
                MediaWatchUser.media_type,
                MediaWatchUser.tmdb_id,
                MediaWatchUser.source_service,
                MediaWatchUser.watch_user_key_normalized,
                MediaWatchUser.last_watched_at,
            ).where(
                or_(
                    and_(
                        MediaWatchUser.media_type == MediaType.MOVIE,
                        MediaWatchUser.tmdb_id.in_(movie_tmdb_ids),
                    ),
                    and_(
                        MediaWatchUser.media_type == MediaType.SERIES,
                        MediaWatchUser.tmdb_id.in_(series_tmdb_ids),
                    ),
                )
            )
        )
    ).all()
    watch_by_service_and_user: dict[
        tuple[MediaType, int], dict[Service, dict[str, datetime]]
    ] = {}
    for media_type, tmdb_id, source_service, user_key, last_watched_at in watch_rows:
        key = (media_type, int(tmdb_id))
        if key not in relevant_keys:
            continue
        normalized_key = _normalize_watch_key(user_key)
        if not normalized_key or last_watched_at is None:
            continue
        last_watched_at_utc = ensure_utc(last_watched_at)
        by_service = watch_by_service_and_user.setdefault(key, {})
        by_user = by_service.setdefault(source_service, {})
        existing = by_user.get(normalized_key)
        if existing is None or last_watched_at_utc > existing:
            by_user[normalized_key] = last_watched_at_utc

    settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
    raw_mappings = (
        settings_row.requester_watch_user_mappings if settings_row is not None else []
    )
    mappings = [m for m in raw_mappings if isinstance(m, dict)]
    ignore_request_date = bool(
        settings_row.requester_watch_ignore_request_date
        if settings_row is not None
        else False
    )
    alias_index = await load_watch_user_alias_index(db)

    active_tracearr_sources = await load_active_tracearr_sources(db)
    durable_rows = (
        await db.execute(
            select(
                PlaybackHistoryEvent.source_service,
                PlaybackHistoryEvent.observed_service,
                PlaybackHistoryEvent.provider_media_type,
                PlaybackHistoryEvent.tmdb_id,
                PlaybackHistoryEvent.season_number,
                PlaybackHistoryEvent.episode_number,
                PlaybackHistoryEvent.source_username,
                PlaybackHistoryEvent.source_user_id,
                PlaybackHistoryEvent.played_at,
            ).where(
                PlaybackHistoryEvent.completed.is_(True),
                PlaybackHistoryEvent.tmdb_id.is_not(None),
                PlaybackHistoryEvent.provider_media_type.in_(("movie", "episode")),
                completion_evidence_event_clause(active_tracearr_sources),
            )
        )
    ).all()
    durable_episode_watches: dict[
        int, dict[Service, dict[str, dict[tuple[int, int], datetime]]]
    ] = {}
    durable_event_count = 0
    # Media servers whose completion state reached us through durable history.
    durable_completion_services: set[Service] = set()
    for (
        source_service,
        observed_service,
        provider_media_type,
        tmdb_id,
        season_number,
        episode_number,
        source_username,
        source_user_id,
        played_at,
    ) in durable_rows:
        watch_key = _normalize_watch_key(source_username or source_user_id)
        if not watch_key or played_at is None or tmdb_id is None:
            continue
        watch_service = observed_service or (
            Service.PLEX if source_service is Service.TAUTULLI else source_service
        )
        watched_at = ensure_utc(played_at)
        durable_event_count += 1
        durable_completion_services.add(watch_service)
        if provider_media_type == "movie":
            media_key = (MediaType.MOVIE, int(tmdb_id))
            if media_key not in relevant_keys:
                continue
            by_user = watch_by_service_and_user.setdefault(media_key, {}).setdefault(
                watch_service, {}
            )
            existing = by_user.get(watch_key)
            if existing is None or watched_at > existing:
                by_user[watch_key] = watched_at
            continue
        if season_number is None or episode_number is None:
            continue
        coordinate = (int(season_number), int(episode_number))
        by_coordinate = (
            durable_episode_watches.setdefault(int(tmdb_id), {})
            .setdefault(watch_service, {})
            .setdefault(watch_key, {})
        )
        existing = by_coordinate.get(coordinate)
        if existing is None or watched_at > existing:
            by_coordinate[coordinate] = watched_at

    unobservable_keys = await _unobservable_requester_watch_keys(
        db,
        movie_tmdb_ids=movie_tmdb_ids,
        series_tmdb_ids=series_tmdb_ids,
        durable_completion_services=durable_completion_services,
    )
    if metadata is not None:
        metadata.requester_watch_unavailable_count = len(unobservable_keys)

    requester_has_watched_by_key: dict[tuple[MediaType, int], bool] = {}
    requester_watched_after_request_by_key: dict[tuple[MediaType, int], bool] = {}
    for key in relevant_keys:
        if key[0] is not MediaType.MOVIE:
            continue
        if key in unobservable_keys:
            # Leaving the key out makes the field unknown. Reporting False here
            # would let an "is false" cleanup rule delete media purely because
            # its media server could not be read.
            continue
        watched_ever, watched_after = _compute_requester_has_watched_for_key(
            media_key=key,
            snapshot=snapshot,
            watch_by_service_and_user=watch_by_service_and_user,
            mappings=mappings,
            alias_index=alias_index,
        )
        requester_has_watched_by_key[key] = watched_ever
        requester_watched_after_request_by_key[key] = watched_after

    # Deliberately the locally present episodes, not Sonarr's full inventory
    # (which `season.fully_watched` uses). A requester can only watch what
    # exists, and widening this to unaired or missing episodes would make
    # "requester has watched" false for seasons they did finish -- which an
    # `is false` cleanup rule turns into a deletion.
    expected_episode_rows = (
        await db.execute(
            select(
                Series.tmdb_id,
                Season.season_number,
                Episode.episode_number,
            )
            .join(Season, Episode.season_id == Season.id)
            .join(Series, Season.series_id == Series.id)
            .where(Series.tmdb_id.in_(series_tmdb_ids))
        )
    ).all()
    expected_by_series: dict[int, set[tuple[int, int]]] = {}
    for tmdb_id, season_number, episode_number in expected_episode_rows:
        if tmdb_id is not None:
            expected_by_series.setdefault(int(tmdb_id), set()).add(
                (int(season_number), int(episode_number))
            )

    requester_ids_by_target: dict[tuple[str, int, int | None], set[int]] = {}
    latest_active_request_at_by_target: dict[tuple[str, int, int | None], datetime] = {}
    for tmdb_id in series_tmdb_ids:
        season_numbers = {
            season_number
            for season_number, _episode_number in expected_by_series.get(tmdb_id, set())
        }
        has_season_request_data = any(
            series_tmdb_id == tmdb_id
            for series_tmdb_id, _season_number in (
                snapshot.requester_ids_by_series_season
            )
        )
        for season_number in season_numbers:
            season_key = (tmdb_id, season_number)
            user_ids = (
                snapshot.requester_ids_by_series_season.get(season_key, set())
                if has_season_request_data
                else requester_ids_by_key.get((MediaType.SERIES, tmdb_id), set())
            )
            for target_scope in (TARGET_SEASON, TARGET_EPISODE):
                target_key = (target_scope, tmdb_id, season_number)
                requester_ids_by_target[target_key] = set(user_ids)
                active_at = snapshot.latest_active_request_at_by_series_season.get(
                    season_key
                )
                if active_at is None and not has_season_request_data:
                    active_at = latest_active_request_at_by_key.get(
                        (MediaType.SERIES, tmdb_id)
                    )
                if active_at is not None:
                    latest_active_request_at_by_target[target_key] = active_at

    episode_watch_rows = (
        await db.execute(
            select(
                MediaWatchUserEpisode.series_tmdb_id,
                MediaWatchUserEpisode.source_service,
                MediaWatchUserEpisode.watch_user_key_normalized,
                MediaWatchUserEpisode.season_number,
                MediaWatchUserEpisode.episode_number,
                MediaWatchUserEpisode.last_watched_at,
            ).where(MediaWatchUserEpisode.series_tmdb_id.in_(series_tmdb_ids))
        )
    ).all()
    episode_watches: dict[
        int, dict[Service, dict[str, dict[tuple[int, int], datetime]]]
    ] = {}
    for (
        tmdb_id,
        source_service,
        user_key,
        season_number,
        episode_number,
        watched_at,
    ) in episode_watch_rows:
        normalized_user_key = _normalize_watch_key(user_key)
        if watched_at is None or normalized_user_key is None:
            continue
        coordinate = (int(season_number), int(episode_number))
        by_coordinate = (
            episode_watches.setdefault(int(tmdb_id), {})
            .setdefault(source_service, {})
            .setdefault(normalized_user_key, {})
        )
        watched_at_utc = ensure_utc(watched_at)
        existing = by_coordinate.get(coordinate)
        if existing is None or watched_at_utc > existing:
            by_coordinate[coordinate] = watched_at_utc

    for tmdb_id, durable_by_service in durable_episode_watches.items():
        for source_service, durable_by_user in durable_by_service.items():
            for user_key, by_coordinate in durable_by_user.items():
                target_coordinates = (
                    episode_watches.setdefault(tmdb_id, {})
                    .setdefault(source_service, {})
                    .setdefault(user_key, {})
                )
                for coordinate, watched_at in by_coordinate.items():
                    existing = target_coordinates.get(coordinate)
                    if existing is None or watched_at > existing:
                        target_coordinates[coordinate] = watched_at

    requester_has_watched_by_target: RequesterWatchTargets = {}
    requester_watched_after_request_by_target: RequesterWatchTargets = {}
    for tmdb_id in series_tmdb_ids:
        if (MediaType.SERIES, tmdb_id) in unobservable_keys:
            continue
        watched_ever_targets, watched_after_targets = (
            _compute_requester_tv_watch_targets_for_key(
                media_key=(MediaType.SERIES, tmdb_id),
                snapshot=snapshot,
                watch_by_service_and_user=episode_watches.get(tmdb_id, {}),
                mappings=mappings,
                expected_episodes=expected_by_series.get(tmdb_id, set()),
                alias_index=alias_index,
            )
        )
        requester_has_watched_by_target.update(watched_ever_targets)
        requester_watched_after_request_by_target.update(watched_after_targets)

    SeerrRequestResolver(
        requester_ids_by_key,
        requester_has_watched_by_key=requester_has_watched_by_key,
        requester_has_watched_by_target=requester_has_watched_by_target,
        requester_watched_after_request_by_key=requester_watched_after_request_by_key,
        requester_watched_after_request_by_target=(
            requester_watched_after_request_by_target
        ),
        latest_active_request_at_by_key=latest_active_request_at_by_key,
        requester_ids_by_target=requester_ids_by_target,
        latest_active_request_at_by_target=latest_active_request_at_by_target,
        ignore_request_date=ignore_request_date,
    ).activate()
    LOG.debug(
        f"Activated Seerr request resolver for {len(movie_tmdb_ids)} movie keys and "
        f"{len(series_tmdb_ids)} series keys"
    )
    LOG.debug(
        "Requester-watch resolver merged "
        f"{durable_event_count} completed durable playback event(s)"
    )
    if snapshot_error:
        LOG.debug(
            "Activated Seerr request resolver from stale snapshot due to refresh error: "
            f"{snapshot_error}"
        )
    return True, snapshot_error


_LEAVING_SOON_MEDIA_SERVICES = {
    Service.PLEX,
    Service.JELLYFIN,
    Service.EMBY,
}


async def _normalize_leaving_soon_last_success_titles(
    db: AsyncSession,
    raw_titles: object,
) -> dict[int, str]:
    """Normalize the persisted last-success-title map to `{service_config_id: title}`.

    Tolerates the pre-multi-instance shape (`{service_type: title}`) on read: a
    key that isn't a valid config id is resolved to whichever ServiceConfig
    currently has that service_type, so upgrading loses no in-flight state.
    Callers should always write the new shape back.
    """
    if not isinstance(raw_titles, Mapping):
        return {}
    normalized_titles: dict[int, str] = {}
    legacy_type_titles: dict[Service, str] = {}
    for raw_key, raw_title in raw_titles.items():
        title = normalize_leaving_soon_collection_title(str(raw_title))
        try:
            normalized_titles[int(raw_key)] = title
            continue
        except (TypeError, ValueError):
            pass
        try:
            service = Service(str(raw_key))
        except Exception:
            continue
        if service in _LEAVING_SOON_MEDIA_SERVICES:
            legacy_type_titles[service] = title

    if legacy_type_titles:
        rows = (
            await db.execute(
                select(ServiceConfig.id, ServiceConfig.service_type).where(
                    ServiceConfig.service_type.in_(legacy_type_titles.keys())
                )
            )
        ).all()
        config_id_by_type: dict[Service, int] = {}
        for config_id, service_type in rows:
            # first match wins - pre-multi-instance installs have at most one
            config_id_by_type.setdefault(service_type, config_id)
        for service, title in legacy_type_titles.items():
            config_id = config_id_by_type.get(service)
            if config_id is not None and config_id not in normalized_titles:
                normalized_titles[config_id] = title

    return normalized_titles


async def _load_leaving_soon_collection_settings(
    db: AsyncSession,
) -> tuple[GeneralSettings | None, bool, str, dict[int, str]]:
    settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
    if settings_row is None:
        return None, False, "Leaving Soon", {}
    collection_base_title = normalize_leaving_soon_collection_title(
        settings_row.leaving_soon_collection_title
    )
    last_success_titles = await _normalize_leaving_soon_last_success_titles(
        db, settings_row.leaving_soon_last_success_titles
    )
    return (
        settings_row,
        bool(settings_row.leaving_soon_enabled),
        collection_base_title,
        last_success_titles,
    )


async def _get_enabled_leaving_soon_configs(db: AsyncSession) -> list[ServiceConfig]:
    """Return enabled media-server ServiceConfig rows Leaving Soon can target."""
    rows = (
        await db.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type.in_(_LEAVING_SOON_MEDIA_SERVICES),
                ServiceConfig.enabled.is_(True),
            )
        )
    ).scalars()
    return list(rows)


def _append_config_item_id(
    expected_items_by_config: dict[int, set[str]],
    *,
    config_id: int | None,
    item_id: str | None,
) -> None:
    if config_id is None:
        return
    normalized_item_id = str(item_id or "").strip()
    if not normalized_item_id:
        return
    expected_items_by_config.setdefault(config_id, set()).add(normalized_item_id)


def _leaving_soon_config_id_resolver(
    configs: list[ServiceConfig],
) -> Callable[[Service], int | None]:
    """Build a `service_type -> config_id` resolver for tables with no
    config_id column of their own (MovieVersion, SeriesServiceRef).

    Those tables are written by the main config only, so a row whose service
    matches the CURRENT main server's type is unambiguously attributed to it.
    A row whose service matches some other (non-main) type is legacy/stale
    data - from before a main-server swap, or from before this table had a
    single-writer invariant - and is attributed to whichever enabled config of
    that type comes first; this can't be perfectly precise if two configs
    share that non-main type, but it never misattributes to the wrong TYPE,
    and it never affects the main config's own item set.
    """
    main_config = next((config for config in configs if config.is_main), None)
    config_id_by_type: dict[Service, int] = {}
    for config in configs:
        config_id_by_type.setdefault(config.service_type, config.id)

    def _resolve(service: Service) -> int | None:
        if main_config is not None and service == main_config.service_type:
            return main_config.id
        return config_id_by_type.get(service)

    return _resolve


async def _build_leaving_soon_expected_item_ids(
    db: AsyncSession,
    configs: list[ServiceConfig],
) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """Resolve expected Leaving Soon item IDs, keyed by service_config_id.

    MovieVersion/SeriesServiceRef have no config_id column of their own, so
    their rows are attributed via `_leaving_soon_config_id_resolver`.
    SupplementalMediaMatch rows (from linked servers) already carry their own
    source_service_config_id.
    """
    resolve_config_id = _leaving_soon_config_id_resolver(configs)
    movie_expected_by_config: dict[int, set[str]] = {}
    series_expected_by_config: dict[int, set[str]] = {}

    candidate_rows = (
        await db.execute(
            select(
                ReclaimCandidate.media_type,
                ReclaimCandidate.movie_id,
                ReclaimCandidate.movie_version_id,
                ReclaimCandidate.series_id,
            )
        )
    ).all()
    if not candidate_rows:
        return movie_expected_by_config, series_expected_by_config

    movie_candidate_version_ids: set[int] = set()
    movie_candidate_ids: set[int] = set()
    series_candidate_ids: set[int] = set()
    for media_type, movie_id, movie_version_id, series_id in candidate_rows:
        if media_type == MediaType.MOVIE:
            if movie_id is not None:
                movie_candidate_ids.add(int(movie_id))
            if movie_version_id is not None:
                movie_candidate_version_ids.add(int(movie_version_id))
            continue
        if media_type == MediaType.SERIES and series_id is not None:
            series_candidate_ids.add(int(series_id))

    if movie_candidate_version_ids:
        version_rows = (
            await db.execute(
                select(
                    MovieVersion.service,
                    MovieVersion.service_item_id,
                    MovieVersion.movie_id,
                ).where(MovieVersion.id.in_(movie_candidate_version_ids))
            )
        ).all()
        for service, service_item_id, movie_id in version_rows:
            _append_config_item_id(
                movie_expected_by_config,
                config_id=resolve_config_id(service),
                item_id=service_item_id,
            )
            movie_candidate_ids.add(int(movie_id))

    if movie_candidate_ids:
        movie_version_rows = (
            await db.execute(
                select(MovieVersion.service, MovieVersion.service_item_id).where(
                    MovieVersion.movie_id.in_(movie_candidate_ids)
                )
            )
        ).all()
        for service, service_item_id in movie_version_rows:
            _append_config_item_id(
                movie_expected_by_config,
                config_id=resolve_config_id(service),
                item_id=service_item_id,
            )

        supplemental_movie_rows = (
            await db.execute(
                select(
                    SupplementalMediaMatch.source_service_config_id,
                    SupplementalMediaMatch.source_item_id,
                ).where(
                    SupplementalMediaMatch.media_type == MediaType.MOVIE,
                    SupplementalMediaMatch.movie_id.in_(movie_candidate_ids),
                )
            )
        ).all()
        for source_service_config_id, source_item_id in supplemental_movie_rows:
            _append_config_item_id(
                movie_expected_by_config,
                config_id=source_service_config_id,
                item_id=source_item_id,
            )

    if series_candidate_ids:
        series_ref_rows = (
            await db.execute(
                select(SeriesServiceRef.service, SeriesServiceRef.service_id).where(
                    SeriesServiceRef.series_id.in_(series_candidate_ids)
                )
            )
        ).all()
        for service, service_id in series_ref_rows:
            _append_config_item_id(
                series_expected_by_config,
                config_id=resolve_config_id(service),
                item_id=service_id,
            )

        supplemental_series_rows = (
            await db.execute(
                select(
                    SupplementalMediaMatch.source_service_config_id,
                    SupplementalMediaMatch.source_item_id,
                ).where(
                    SupplementalMediaMatch.media_type == MediaType.SERIES,
                    SupplementalMediaMatch.series_id.in_(series_candidate_ids),
                )
            )
        ).all()
        for source_service_config_id, source_item_id in supplemental_series_rows:
            _append_config_item_id(
                series_expected_by_config,
                config_id=source_service_config_id,
                item_id=source_item_id,
            )

    return movie_expected_by_config, series_expected_by_config


async def _cleanup_disabled_leaving_soon_collections(
    db: AsyncSession,
    *,
    settings_row: GeneralSettings | None,
    last_success_titles_by_config: Mapping[int, str],
) -> None:
    if settings_row is None or not last_success_titles_by_config:
        return

    updated_last_success_titles = dict(last_success_titles_by_config)
    last_success_changed = False
    configs_by_id = {
        config.id: config for config in await _get_enabled_leaving_soon_configs(db)
    }

    for config_id, previous_success_title in list(updated_last_success_titles.items()):
        config = configs_by_id.get(config_id)
        service_client = (
            service_manager.get_media_server(config.service_type, config.id)
            if config is not None
            else None
        )
        if service_client is None:
            # keep title for retry while the config is unavailable/removed
            continue

        delete_method = getattr(service_client, "delete_leaving_soon_collections", None)
        if not callable(delete_method):
            LOG.warning(
                "Leaving Soon cleanup method missing for "
                f"{config.service_type.value} (config {config_id}); cannot remove "
                f"title {previous_success_title!r} while Leaving Soon is disabled"
            )
            continue
        delete_func = cast(Callable[..., Awaitable[Any]], delete_method)
        try:
            await delete_func(base_title=previous_success_title)
        except Exception as e:
            LOG.warning(
                "Failed cleaning Leaving Soon collections for "
                f"{config.service_type.value} (config {config_id}) while disabled "
                f"(title {previous_success_title!r}): {e}"
            )
            continue

        del updated_last_success_titles[config_id]
        last_success_changed = True

    if not last_success_changed:
        return

    settings_row.leaving_soon_last_success_titles = {
        str(config_id): title for config_id, title in updated_last_success_titles.items()
    }
    db.add(settings_row)
    await db.commit()


async def _sync_leaving_soon_collections(db: AsyncSession) -> None:
    (
        settings_row,
        enabled,
        collection_base_title,
        last_success_titles_by_config,
    ) = await _load_leaving_soon_collection_settings(db)
    if not enabled:
        await _cleanup_disabled_leaving_soon_collections(
            db,
            settings_row=settings_row,
            last_success_titles_by_config=last_success_titles_by_config,
        )
        return

    configs = await _get_enabled_leaving_soon_configs(db)
    (
        movie_expected_by_config,
        series_expected_by_config,
    ) = await _build_leaving_soon_expected_item_ids(db, configs)
    updated_last_success_titles = dict(last_success_titles_by_config)
    last_success_changed = False

    for config in configs:
        service_client = service_manager.get_media_server(
            config.service_type, config.id
        )
        if service_client is None:
            continue
        service_label = f"{config.service_type.value} (config {config.id})"
        previous_success_title = updated_last_success_titles.get(config.id)
        service_success = True

        # if this config last synced under a different title, clean it first.
        if (
            previous_success_title is not None
            and previous_success_title != collection_base_title
        ):
            delete_method = getattr(
                service_client, "delete_leaving_soon_collections", None
            )
            if not callable(delete_method):
                service_success = False
                LOG.warning(
                    f"Leaving Soon cleanup method missing for {service_label}; "
                    f"cannot remove previous title {previous_success_title!r}"
                )
            else:
                delete_func = cast(Callable[..., Awaitable[Any]], delete_method)
                try:
                    await delete_func(base_title=previous_success_title)
                except Exception as e:
                    service_success = False
                    LOG.warning(
                        "Failed cleaning previous Leaving Soon collections for "
                        f"{service_label} (title {previous_success_title!r}): {e}"
                    )

        sync_method = getattr(service_client, "sync_leaving_soon_collections", None)
        if not callable(sync_method):
            LOG.warning(
                f"Leaving Soon sync method missing for {service_label}; "
                "skipping config sync"
            )
            continue
        sync_func = cast(Callable[..., Awaitable[Any]], sync_method)
        movie_item_ids = movie_expected_by_config.get(config.id, set())
        series_item_ids = series_expected_by_config.get(config.id, set())
        try:
            await sync_func(
                base_title=collection_base_title,
                movie_item_ids=movie_item_ids,
                series_item_ids=series_item_ids,
            )
        except Exception as e:
            service_success = False
            LOG.warning(f"Failed syncing Leaving Soon collections for {service_label}: {e}")

        if not service_success:
            continue
        if previous_success_title == collection_base_title:
            continue

        updated_last_success_titles[config.id] = collection_base_title
        last_success_changed = True

    if not last_success_changed:
        return
    if settings_row is None:
        return

    settings_row.leaving_soon_last_success_titles = {
        str(config_id): title for config_id, title in updated_last_success_titles.items()
    }
    db.add(settings_row)
    await db.commit()


async def _build_leaving_soon_prune_item_ids(
    db: AsyncSession,
    candidate_ids: Iterable[int],
    configs: list[ServiceConfig],
) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """Resolve media-server collection item IDs affected by candidate actions.

    Keyed by service_config_id - see _build_leaving_soon_expected_item_ids for
    how MovieVersion/SeriesServiceRef rows (which have no config_id column of
    their own) are attributed.
    """
    resolve_config_id = _leaving_soon_config_id_resolver(configs)
    normalized_candidate_ids = {int(candidate_id) for candidate_id in candidate_ids}
    movie_item_ids: dict[int, set[str]] = {}
    series_item_ids: dict[int, set[str]] = {}
    if not normalized_candidate_ids:
        return movie_item_ids, series_item_ids

    candidates = (
        (
            await db.execute(
                select(ReclaimCandidate).where(
                    ReclaimCandidate.id.in_(normalized_candidate_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    movie_ids = {
        int(candidate.movie_id)
        for candidate in candidates
        if candidate.media_type == MediaType.MOVIE and candidate.movie_id is not None
    }
    movie_version_ids = {
        int(candidate.movie_version_id)
        for candidate in candidates
        if candidate.media_type == MediaType.MOVIE
        and candidate.movie_version_id is not None
    }
    series_ids = {
        int(candidate.series_id)
        for candidate in candidates
        if candidate.media_type == MediaType.SERIES and candidate.series_id is not None
    }

    if movie_version_ids:
        version_rows = (
            await db.execute(
                select(MovieVersion.service, MovieVersion.service_item_id).where(
                    MovieVersion.id.in_(movie_version_ids)
                )
            )
        ).all()
        for service, service_item_id in version_rows:
            _append_config_item_id(
                movie_item_ids,
                config_id=resolve_config_id(service),
                item_id=service_item_id,
            )

    if movie_ids:
        version_rows = (
            await db.execute(
                select(MovieVersion.service, MovieVersion.service_item_id).where(
                    MovieVersion.movie_id.in_(movie_ids)
                )
            )
        ).all()
        for service, service_item_id in version_rows:
            _append_config_item_id(
                movie_item_ids,
                config_id=resolve_config_id(service),
                item_id=service_item_id,
            )

    if movie_ids:
        supplemental_rows = (
            await db.execute(
                select(
                    SupplementalMediaMatch.source_service_config_id,
                    SupplementalMediaMatch.source_item_id,
                ).where(
                    SupplementalMediaMatch.media_type == MediaType.MOVIE,
                    SupplementalMediaMatch.movie_id.in_(movie_ids),
                )
            )
        ).all()
        for source_service_config_id, service_item_id in supplemental_rows:
            _append_config_item_id(
                movie_item_ids,
                config_id=source_service_config_id,
                item_id=service_item_id,
            )

    if series_ids:
        series_ref_rows = (
            await db.execute(
                select(SeriesServiceRef.service, SeriesServiceRef.service_id).where(
                    SeriesServiceRef.series_id.in_(series_ids)
                )
            )
        ).all()
        for service, service_item_id in series_ref_rows:
            _append_config_item_id(
                series_item_ids,
                config_id=resolve_config_id(service),
                item_id=service_item_id,
            )

        supplemental_rows = (
            await db.execute(
                select(
                    SupplementalMediaMatch.source_service_config_id,
                    SupplementalMediaMatch.source_item_id,
                ).where(
                    SupplementalMediaMatch.media_type == MediaType.SERIES,
                    SupplementalMediaMatch.series_id.in_(series_ids),
                )
            )
        ).all()
        for source_service_config_id, service_item_id in supplemental_rows:
            _append_config_item_id(
                series_item_ids,
                config_id=source_service_config_id,
                item_id=service_item_id,
            )

    return movie_item_ids, series_item_ids


async def _prune_leaving_soon_before_candidate_actions(
    candidate_ids: Iterable[int],
) -> None:
    """Remove affected items from managed collections before destructive actions."""
    normalized_candidate_ids = {int(candidate_id) for candidate_id in candidate_ids}
    if not normalized_candidate_ids:
        return

    async with async_db() as db:
        (
            _settings_row,
            enabled,
            collection_base_title,
            last_success_titles,
        ) = await _load_leaving_soon_collection_settings(db)
        if not enabled:
            return
        configs = await _get_enabled_leaving_soon_configs(db)
        movie_item_ids, series_item_ids = await _build_leaving_soon_prune_item_ids(
            db,
            normalized_candidate_ids,
            configs,
        )

    for config in configs:
        service_movie_ids = movie_item_ids.get(config.id, set())
        service_series_ids = series_item_ids.get(config.id, set())
        if not service_movie_ids and not service_series_ids:
            continue

        service_label = f"{config.service_type.value} (config {config.id})"
        titles = {
            collection_base_title,
            *(
                [last_success_titles[config.id]]
                if config.id in last_success_titles
                else []
            ),
        }
        service_client = service_manager.get_media_server(
            config.service_type, config.id
        )
        if service_client is None:
            if config.id in last_success_titles:
                raise RuntimeError(
                    f"{service_label} is unavailable; cannot safely prune "
                    "its Leaving Soon collection"
                )
            continue

        prune_method = getattr(service_client, "prune_leaving_soon_items", None)
        if not callable(prune_method):
            raise RuntimeError(
                f"{service_label} Leaving Soon prune method is unavailable"
            )
        prune_func = cast(Callable[..., Awaitable[Any]], prune_method)
        for title in titles:
            try:
                await prune_func(
                    base_title=title,
                    movie_item_ids=service_movie_ids,
                    series_item_ids=service_series_ids,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Failed pruning {service_label} Leaving Soon collection "
                    f"{title!r}: {e}"
                ) from e


async def _reconcile_leaving_soon_after_candidate_actions() -> None:
    """Best-effort reconciliation after candidate deletion or move attempts."""
    try:
        async with async_db() as db:
            await _sync_leaving_soon_collections(db)
    except Exception as e:
        LOG.warning(f"Leaving Soon post-action reconciliation failed: {e}")


async def _scan_with_db(db: AsyncSession) -> tuple[int, int, int] | None:
    """Internal method to perform scan with database session.

    Returns (created_count, updated_count, removed_count) or None if no rules found."""
    try:
        # load all enabled cleanup rules
        result = await db.execute(
            select(ReclaimRule).where(ReclaimRule.enabled == True)
        )
        rules = result.scalars().all()

        if not rules:
            LOG.info(
                "No enabled cleanup rules found, clearing candidates and "
                "rule-managed protections"
            )
            await _reconcile_rule_managed_protections(db, [])
            await db.execute(delete(ReclaimCandidate))
            await clear_playback_rule_data_notice(db)
            await clear_sonarr_rule_data_notice(db)
            await db.commit()
            try:
                await _sync_leaving_soon_collections(db)
            except Exception as e:
                LOG.warning(f"Leaving Soon collection sync failed: {e}")
            return None

        LOG.info(f"Found {len(rules)} enabled cleanup rules")

        favorites_ready, favorites_error = await _ensure_favorites_snapshot_if_enabled(
            db
        )
        if not favorites_ready and favorites_error:
            LOG.warning(
                "Favorites snapshot refresh failed for this cleanup scan: "
                f"{favorites_error}"
            )
        elif favorites_error:
            LOG.debug(
                "Using stale favorites snapshot for this cleanup scan due to refresh error: "
                f"{favorites_error}"
            )

        # refresh arr data from Radarr/Sonarr for any labels/fields referenced in active rules
        # (fetches full item lists + tag catalog per client, then resolves item tag IDs -> labels)
        DiskStatsResolver(
            arr_entries=await _load_arr_disk_space(),
            path_mappings=await _load_path_mappings(),
        ).activate()
        sonarr_series_snapshot = _SonarrSeriesSnapshot()
        await _refresh_arr_tags_for_rules(
            list(rules),
            sonarr_series_snapshot=sonarr_series_snapshot,
        )
        await _refresh_arr_monitoring_for_rules(
            list(rules),
            sonarr_series_snapshot=sonarr_series_snapshot,
        )
        await _activate_arr_id_rule_data_for_rules(db, list(rules))
        await _activate_favorites_rule_data_for_rules(
            db,
            list(rules),
            require_fresh=True,
        )
        await _activate_rank_rule_data_for_rules(db, list(rules))
        await _activate_collection_sibling_rule_data_for_rules(db, list(rules))

        preserved_protection_rule_ids: set[int] = set()
        seerr_skipped_rules = 0
        seerr_skip_reason: str | None = None
        seerr_ready, seerr_error = await _activate_seerr_request_resolver_for_rules(
            db,
            list(rules),
            require_fresh=True,
            allow_stale_on_failure=False,
        )
        if not seerr_ready:
            seerr_dependent_rules = [r for r in rules if _rule_uses_seerr_fields(r)]
            if seerr_dependent_rules:
                preserved_protection_rule_ids = {
                    rule.id
                    for rule in seerr_dependent_rules
                    if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT
                }
                rules = [r for r in rules if not _rule_uses_seerr_fields(r)]
                seerr_skipped_rules = len(seerr_dependent_rules)
                seerr_skip_reason = (
                    seerr_error or "Failed to refresh Seerr request snapshot"
                )
                LOG.warning(
                    f"Skipping {seerr_skipped_rules} Seerr dependent cleanup rule(s) "
                    f"this run: {seerr_skip_reason}"
                )

        sonarr_rule_result = await _activate_sonarr_rule_data_for_rules(
            db,
            list(rules),
            sonarr_series_snapshot=sonarr_series_snapshot,
        )

        # Provider refreshes use their own sessions. End this session's read
        # transaction before they write, and defer all notice/candidate writes
        # until those refreshes are complete.
        await db.commit()

        playback_rule_result = await _activate_playback_history_for_rules(
            db, list(rules), require_fresh=True
        )

        playback_dependent_rules = [
            rule for rule in rules if _rule_uses_playback_fields(rule)
        ]
        if playback_rule_result.error:
            preserved_protection_rule_ids.update(
                rule.id
                for rule in playback_dependent_rules
                if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT
            )
            rules = [rule for rule in rules if not _rule_uses_playback_fields(rule)]
            LOG.warning(
                f"Skipping {len(playback_dependent_rules)} playback-dependent "
                "cleanup rule(s) because current provider data could not be "
                f"verified: {playback_rule_result.error}"
            )

        if seerr_skipped_rules:
            await set_seerr_rule_skip_notice(
                db,
                skipped_rules=seerr_skipped_rules,
                reason=seerr_skip_reason or "Failed to refresh Seerr request snapshot",
            )
        else:
            await clear_seerr_rule_skip_notice(db)

        if sonarr_rule_result.unavailable_series_ids:
            await set_sonarr_rule_data_notice(
                db,
                unavailable_series=len(sonarr_rule_result.unavailable_series_ids),
                reason=sonarr_rule_result.error or "unknown Sonarr data error",
            )
        else:
            await clear_sonarr_rule_data_notice(db)

        if playback_rule_result.unavailable_count or playback_rule_result.error:
            playback_dependent_protection_rules = {
                rule.id
                for rule in playback_dependent_rules
                if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT
            }
            preserved_protection_rule_ids.update(playback_dependent_protection_rules)
            await set_playback_rule_data_notice(
                db,
                unavailable_targets=max(1, playback_rule_result.unavailable_count),
                reason=playback_rule_result.error
                or "unknown playback history provider error",
            )
        else:
            await clear_playback_rule_data_notice(db)

        protection_rules = [
            rule
            for rule in rules
            if normalize_rule_outcome(rule) == RULE_OUTCOME_PROTECT
        ]
        candidate_rules = [
            rule
            for rule in rules
            if normalize_rule_outcome(rule) == RULE_OUTCOME_CANDIDATE
        ]
        (
            protected_created,
            protected_updated,
            protected_removed,
        ) = await _reconcile_rule_managed_protections(
            db,
            protection_rules,
            preserve_rule_ids=preserved_protection_rule_ids,
            preserve_rule_series_keys=(sonarr_rule_result.preserve_protection_keys),
        )
        LOG.info(
            "Automated protection reconciliation completed: "
            f"{protected_created} created, {protected_updated} updated, "
            f"{protected_removed} removed"
        )

        # Separate candidate rules by explicit advanced target. Rules without a
        # valid advanced definition are skipped by the evaluator and will not match.
        movie_rules = [
            r
            for r in candidate_rules
            if normalize_rule_target(r) == TARGET_MOVIE_VERSION
        ]
        series_rules = [
            r for r in candidate_rules if normalize_rule_target(r) == TARGET_SERIES
        ]
        season_rules = [
            r for r in candidate_rules if normalize_rule_target(r) == TARGET_SEASON
        ]
        episode_rules = [
            r for r in candidate_rules if normalize_rule_target(r) == TARGET_EPISODE
        ]

        candidates_created = 0
        candidates_updated = 0
        candidates_removed = 0

        # process movies
        if movie_rules:
            created, updated, removed = await _process_media(
                db, movie_rules, MediaType.MOVIE, commit=False
            )
            candidates_created += created
            candidates_updated += updated
            candidates_removed += removed
        else:
            # no movie rules active - remove stale movie candidates
            del_result = cast(
                CursorResult[Any],
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.media_type == MediaType.MOVIE
                    )
                ),
            )
            candidates_removed += del_result.rowcount or 0

        # process series
        if series_rules:
            created, updated, removed = await _process_media(
                db, series_rules, MediaType.SERIES, commit=False
            )
            candidates_created += created
            candidates_updated += updated
            candidates_removed += removed
        else:
            del_result = cast(
                CursorResult[Any],
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.media_type == MediaType.SERIES,
                        _is_series_scope(ReclaimCandidate),
                    )
                ),
            )
            candidates_removed += del_result.rowcount or 0

        if season_rules:
            s_cr, s_up, s_rm = await _process_series_seasons(
                db, season_rules, commit=False
            )
            candidates_created += s_cr
            candidates_updated += s_up
            candidates_removed += s_rm
        else:
            del_result = cast(
                CursorResult[Any],
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.media_type == MediaType.SERIES,
                        _is_season_scope(ReclaimCandidate),
                    )
                ),
            )
            candidates_removed += del_result.rowcount or 0

        if episode_rules:
            e_cr, e_up, e_rm = await _process_series_episodes(
                db, episode_rules, commit=False
            )
            candidates_created += e_cr
            candidates_updated += e_up
            candidates_removed += e_rm
        else:
            del_result = cast(
                CursorResult[Any],
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.media_type == MediaType.SERIES,
                        _is_episode_scope(ReclaimCandidate),
                    )
                ),
            )
            candidates_removed += del_result.rowcount or 0

        lifecycle_event_ids = await reconcile_candidate_schedule_events(db)
        await db.commit()
        for lifecycle_event_id in lifecycle_event_ids:
            await enqueue_event_deliveries(lifecycle_event_id)

        if seerr_skipped_rules:
            try:
                await notify_admins(
                    notification_type=NotificationType.ADMIN_MESSAGE,
                    title="Seerr rules skipped during cleanup scan",
                    message=(
                        f"Skipped {seerr_skipped_rules} Seerr dependent rule(s) for "
                        "this cleanup run because Seerr request data could not be "
                        "refreshed."
                    ),
                    context={"reason": seerr_skip_reason},
                )
            except Exception as notify_error:
                LOG.error(
                    f"Failed to notify admins about skipped Seerr rules: {notify_error}"
                )

        try:
            await _sync_leaving_soon_collections(db)
        except Exception as e:
            LOG.warning(f"Leaving Soon collection sync failed: {e}")

        LOG.info(
            f"Cleanup scan completed: {candidates_created} new candidates, "
            f"{candidates_updated} updated, {candidates_removed} removed"
        )

        return candidates_created, candidates_updated, candidates_removed
    except Exception:
        await db.rollback()
        raise


async def _process_media(
    db: AsyncSession,
    rules: list[ReclaimRule],
    media_type: MediaType,
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """
    Process movies or series against cleanup rules.

    Returns (created_count, updated_count, removed_count)
    """
    if media_type is MediaType.MOVIE:
        return await _process_movie_versions(db, rules, commit=commit)

    records = await _collect_series_candidate_records(db, rules)
    return await _sync_series_candidates(db, records, commit=commit)


async def _collect_series_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    preview_metadata: RulePreviewMatchMetadata | None = None,
    exclude_favorites: bool = True,
    exclude_protected: bool = True,
    target_ids: set[int] | None = None,
) -> list[MatchedCandidateRecord]:
    """Evaluate whole series rules without mutating persisted candidates."""
    (
        favorites_enabled,
        favorites_all_users,
        favorites_usernames,
    ) = await _load_favorites_policy(db)
    favorite_tmdb_ids = (
        await _load_favorite_tmdb_ids(
            db,
            media_type=MediaType.SERIES,
            protect_all_users=favorites_all_users,
            usernames=favorites_usernames,
        )
        if favorites_enabled and exclude_favorites
        else set()
    )

    # get all media items
    query_options = [selectinload(Series.service_refs)]
    if _rules_use_field(rules, "series.library_season_count"):
        query_options.append(selectinload(Series.seasons))
    query = (
        select(Series)
        .where(
            Series.removed_at.is_(None),
            Series.seasons.any(
                and_(Season.path.is_not(None), func.trim(Season.path) != "")
            ),
        )
        .options(*query_options)
    )
    if target_ids is not None:
        if not target_ids:
            return []
        query = query.where(Series.id.in_(target_ids))
    result = await db.execute(query)
    media_items = result.scalars().all()
    if preview_metadata is not None:
        preview_metadata.source_media_count += len(media_items)

    LOG.info(f"Processing {len(media_items)} {MediaType.SERIES.value} items")

    # fetch all protected items for this media type to skip them
    protected_ids: set[int | None] = set()
    if exclude_protected:
        now = datetime.now(UTC)
        protected_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                ProtectedMedia.series_id.isnot(None),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_ids = {b.series_id for b in protected_result.scalars().all()}

    LOG.info(
        f"Found {len(protected_ids)} protected {MediaType.SERIES.value} items to skip"
    )

    records: list[MatchedCandidateRecord] = []

    for item in media_items:
        if favorite_tmdb_ids and item.tmdb_id in favorite_tmdb_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_favorites_count += 1
            LOG.info(
                f"Skipping favorite series: {item.title} (TMDB ID: {item.tmdb_id})"
            )
            continue
        # skip protected items
        if item.id in protected_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_protected_count += 1
            continue

        # evaluate all rules against this item
        matched_rules: list[int] = []
        matched_criteria: dict[str, Any] = {}
        reasons: list[dict[str, Any]] = []

        for rule in rules:
            if _evaluate_movie_rule(item, rule, matched_criteria, reasons):
                matched_rules.append(rule.id)

        if not matched_rules:
            continue

        records.append(
            MatchedCandidateRecord(
                media_type=MediaType.SERIES,
                series_id=item.id,
                matched_rule_ids=matched_rules,
                matched_criteria=matched_criteria,
                reason=_rule_reason_text(reasons),
                reason_data=reasons,
                estimated_space_bytes=item.size if item.size else None,
            )
        )

    return records


async def _sync_series_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Synchronize series candidates with the database."""
    result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.media_type == MediaType.SERIES,
            _is_series_scope(ReclaimCandidate),
        )
    )
    existing_candidates = result.scalars().all()
    candidate_lookup = {
        candidate.series_id: candidate
        for candidate in existing_candidates
        if candidate.series_id is not None
    }
    matched_item_ids = {
        record.series_id for record in records if record.series_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.series_id is None:
            continue
        existing = candidate_lookup.get(record.series_id)
        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_bytes = record.estimated_space_bytes
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=record.series_id,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_bytes=record.estimated_space_bytes,
                )
            )
            candidates_created += 1

    stale_candidates = [
        candidate
        for series_id, candidate in candidate_lookup.items()
        if series_id not in matched_item_ids
    ]
    candidates_removed = len(stale_candidates)
    for candidate in stale_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        if commit:
            await db.commit()
        else:
            await db.flush()

    return candidates_created, candidates_updated, candidates_removed


async def _process_movie_versions(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Evaluate movie rules at movie-version granularity."""
    records = await _collect_movie_version_candidate_records(db, rules)
    return await _sync_movie_version_candidates(db, records, commit=commit)


async def _collect_movie_version_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    preview_metadata: RulePreviewMatchMetadata | None = None,
    exclude_favorites: bool = True,
    exclude_protected: bool = True,
    target_ids: set[int] | None = None,
) -> list[MatchedCandidateRecord]:
    """Evaluate movie-version rules without mutating persisted candidates."""
    (
        favorites_enabled,
        favorites_all_users,
        favorites_usernames,
    ) = await _load_favorites_policy(db)
    favorite_tmdb_ids = (
        await _load_favorite_tmdb_ids(
            db,
            media_type=MediaType.MOVIE,
            protect_all_users=favorites_all_users,
            usernames=favorites_usernames,
        )
        if favorites_enabled and exclude_favorites
        else set()
    )
    query = (
        select(Movie)
        .where(Movie.removed_at.is_(None))
        .options(selectinload(Movie.versions))
    )
    if target_ids is not None:
        if not target_ids:
            return []
        query = query.where(
            Movie.id.in_(
                select(MovieVersion.movie_id).where(MovieVersion.id.in_(target_ids))
            )
        )
    result = await db.execute(query)
    movies = result.scalars().all()
    if preview_metadata is not None:
        preview_metadata.source_media_count += len(movies)
    LOG.info(f"Processing {len(movies)} movie items at version granularity")

    protected_rows: list[ProtectedMedia] = []
    if exclude_protected:
        now = datetime.now(UTC)
        protected_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.MOVIE,
                ProtectedMedia.movie_id.isnot(None),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_rows = list(protected_result.scalars().all())
    protected_movie_ids = {
        p.movie_id
        for p in protected_rows
        if p.movie_id is not None and p.movie_version_id is None
    }
    protected_version_ids = {
        p.movie_version_id for p in protected_rows if p.movie_version_id is not None
    }
    LOG.info(
        f"Found {len(protected_movie_ids)} protected movies and "
        f"{len(protected_version_ids)} protected movie versions to skip"
    )

    records: list[MatchedCandidateRecord] = []

    for movie in movies:
        if favorite_tmdb_ids and movie.tmdb_id in favorite_tmdb_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_favorites_count += 1
            LOG.info(
                f"Skipping favorite movie: {movie.title} (TMDB ID: {movie.tmdb_id})"
            )
            continue
        if movie.id in protected_movie_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_protected_count += 1
            continue
        if not movie.versions:
            continue

        for version in movie.versions:
            if target_ids is not None and version.id not in target_ids:
                continue
            if version.id in protected_version_ids:
                continue

            matched_rules: list[int] = []
            matched_criteria: dict[str, Any] = {}
            reasons: list[dict[str, Any]] = []

            for rule in rules:
                if _evaluate_movie_version_rule(
                    movie, version, rule, matched_criteria, reasons
                ):
                    matched_rules.append(rule.id)

            if not matched_rules:
                continue

            candidate_size = (
                version.size if version.size and version.size > 0 else movie.size
            )
            records.append(
                MatchedCandidateRecord(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=version.id,
                    matched_rule_ids=matched_rules,
                    matched_criteria=matched_criteria,
                    reason=_rule_reason_text(reasons),
                    reason_data=reasons,
                    estimated_space_bytes=candidate_size if candidate_size else None,
                )
            )

    return records


async def _sync_movie_version_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Synchronize movie version candidates with the database."""
    existing_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.media_type == MediaType.MOVIE)
    )
    existing_candidates = existing_result.scalars().all()
    version_candidate_lookup: dict[int, ReclaimCandidate] = {
        candidate.movie_version_id: candidate
        for candidate in existing_candidates
        if candidate.movie_version_id is not None
    }
    legacy_movie_candidates = [
        candidate
        for candidate in existing_candidates
        if candidate.movie_version_id is None
    ]

    matched_version_ids = {
        record.movie_version_id
        for record in records
        if record.movie_version_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.movie_version_id is None or record.movie_id is None:
            continue
        existing = version_candidate_lookup.get(record.movie_version_id)
        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_bytes = record.estimated_space_bytes
            existing.movie_id = record.movie_id
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=record.movie_id,
                    movie_version_id=record.movie_version_id,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_bytes=record.estimated_space_bytes,
                )
            )
            candidates_created += 1

    stale_version_candidates = [
        candidate
        for version_id, candidate in version_candidate_lookup.items()
        if version_id not in matched_version_ids
    ]
    candidates_removed = len(stale_version_candidates) + len(legacy_movie_candidates)
    for candidate in stale_version_candidates:
        await db.delete(candidate)
    for candidate in legacy_movie_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        if commit:
            await db.commit()
        else:
            await db.flush()

    return candidates_created, candidates_updated, candidates_removed


async def _process_series_seasons(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Evaluate each series' seasons against rules and create/update season-level candidates.

    Returns (created_count, updated_count, removed_count).
    """
    records = await _collect_season_candidate_records(db, rules)
    return await _sync_season_candidates(db, records, commit=commit)


async def _process_series_episodes(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Evaluate each series' episodes against rules and create/update episode-level candidates.

    Returns (created_count, updated_count, removed_count).
    """
    records = await _collect_episode_candidate_records(db, rules)
    return await _sync_episode_candidates(db, records, commit=commit)


async def _collect_season_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    preview_metadata: RulePreviewMatchMetadata | None = None,
    exclude_favorites: bool = True,
    exclude_protected: bool = True,
    target_ids: set[int] | None = None,
) -> list[MatchedCandidateRecord]:
    """Evaluate season rules without mutating persisted candidates."""
    include_episodes = _rules_use_season_episode_watch_fields(rules)
    (
        favorites_enabled,
        favorites_all_users,
        favorites_usernames,
    ) = await _load_favorites_policy(db)
    favorite_tmdb_ids = (
        await _load_favorite_tmdb_ids(
            db,
            media_type=MediaType.SERIES,
            protect_all_users=favorites_all_users,
            usernames=favorites_usernames,
        )
        if favorites_enabled and exclude_favorites
        else set()
    )

    # load all non-deleted series with their seasons and service refs
    season_loader = selectinload(Series.seasons)
    if include_episodes:
        season_loader = season_loader.selectinload(Season.episodes)
    query = (
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(selectinload(Series.service_refs), season_loader)
    )
    if target_ids is not None:
        if not target_ids:
            return []
        query = query.where(
            Series.id.in_(select(Season.series_id).where(Season.id.in_(target_ids)))
        )
    result = await db.execute(query)
    all_series = result.scalars().all()
    if preview_metadata is not None:
        preview_metadata.source_media_count += len(all_series)

    if not all_series:
        return []

    # whole-series protection also covers every season of that series
    protected_series_ids: set[int | None] = set()
    protected_season_ids: set[int | None] = set()
    if exclude_protected:
        now = datetime.now(UTC)
        protected_series_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                ProtectedMedia.series_id.isnot(None),
                _is_series_scope(ProtectedMedia),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_series_ids = {
            b.series_id for b in protected_series_result.scalars().all()
        }

        protected_season_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                _is_season_scope(ProtectedMedia),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_season_ids = {
            b.season_id for b in protected_season_result.scalars().all()
        }

    records: list[MatchedCandidateRecord] = []

    for series in all_series:
        if favorite_tmdb_ids and series.tmdb_id in favorite_tmdb_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_favorites_count += 1
            LOG.info(
                f"Skipping favorite series: {series.title} (TMDB ID: {series.tmdb_id})"
            )
            continue
        if not series.seasons:
            continue
        if series.id in protected_series_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_protected_count += 1
            continue

        for season in series.seasons:
            if target_ids is not None and season.id not in target_ids:
                continue
            if not _has_media_path(season.path):
                continue
            if season.id in protected_season_ids:
                continue

            matched_rules: list[int] = []
            matched_criteria: dict[str, Any] = {}
            reasons: list[dict[str, Any]] = []

            for rule in rules:
                if (
                    preview_metadata is not None
                    and season.size
                    and _rule_uses_season_episode_watch_fields(rule)
                    and evaluate_advanced_rule_state(
                        rule,
                        target_scope=TARGET_SEASON,
                        series=series,
                        season=season,
                    )
                    is None
                ):
                    _record_unavailable_season_inventory(
                        preview_metadata, series, season
                    )
                if _evaluate_rule_for_season(
                    series, season, rule, matched_criteria, reasons
                ):
                    matched_rules.append(rule.id)

            if not matched_rules:
                continue

            records.append(
                MatchedCandidateRecord(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    matched_rule_ids=matched_rules,
                    matched_criteria=matched_criteria,
                    reason=_rule_reason_text(reasons),
                    reason_data=reasons,
                    estimated_space_bytes=season.size if season.size else None,
                )
            )

    return records


async def _sync_season_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Synchronize season candidates with the database."""
    existing_result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.media_type == MediaType.SERIES,
            _is_season_scope(ReclaimCandidate),
        )
    )
    season_candidate_lookup: dict[int, ReclaimCandidate] = {
        candidate.season_id: candidate
        for candidate in existing_result.scalars().all()
        if candidate.season_id is not None
    }

    matched_season_ids = {
        record.season_id for record in records if record.season_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.season_id is None or record.series_id is None:
            continue
        existing = season_candidate_lookup.get(record.season_id)

        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_bytes = record.estimated_space_bytes
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_bytes=record.estimated_space_bytes,
                    series_id=record.series_id,
                    season_id=record.season_id,
                )
            )
            candidates_created += 1

    stale_candidates = [
        candidate
        for season_id, candidate in season_candidate_lookup.items()
        if season_id not in matched_season_ids
    ]
    candidates_removed = len(stale_candidates)
    for candidate in stale_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        if commit:
            await db.commit()
        else:
            await db.flush()

    return candidates_created, candidates_updated, candidates_removed


async def _collect_episode_candidate_records(
    db: AsyncSession,
    rules: list[ReclaimRule],
    *,
    preview_metadata: RulePreviewMatchMetadata | None = None,
    exclude_favorites: bool = True,
    exclude_protected: bool = True,
    target_ids: set[int] | None = None,
) -> list[MatchedCandidateRecord]:
    """Evaluate episode rules without mutating persisted candidates."""
    (
        favorites_enabled,
        favorites_all_users,
        favorites_usernames,
    ) = await _load_favorites_policy(db)
    favorite_tmdb_ids = (
        await _load_favorite_tmdb_ids(
            db,
            media_type=MediaType.SERIES,
            protect_all_users=favorites_all_users,
            usernames=favorites_usernames,
        )
        if favorites_enabled and exclude_favorites
        else set()
    )

    query = (
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(
            selectinload(Series.service_refs),
            selectinload(Series.seasons).selectinload(Season.episodes),
        )
    )
    if target_ids is not None:
        if not target_ids:
            return []
        query = query.where(
            Series.id.in_(
                select(Season.series_id)
                .join(Episode, Episode.season_id == Season.id)
                .where(Episode.id.in_(target_ids))
            )
        )
    result = await db.execute(query)
    all_series = result.scalars().all()
    if preview_metadata is not None:
        preview_metadata.source_media_count += len(all_series)

    if not all_series:
        return []

    protected_series_ids: set[int | None] = set()
    protected_season_ids: set[int | None] = set()
    protected_episode_ids: set[int | None] = set()
    if exclude_protected:
        now = datetime.now(UTC)
        protected_series_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                ProtectedMedia.series_id.isnot(None),
                _is_series_scope(ProtectedMedia),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_series_ids = {
            b.series_id for b in protected_series_result.scalars().all()
        }

        protected_season_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                _is_season_scope(ProtectedMedia),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_season_ids = {
            b.season_id for b in protected_season_result.scalars().all()
        }

        protected_episode_result = await db.execute(
            select(ProtectedMedia).where(
                ProtectedMedia.media_type == MediaType.SERIES,
                _is_episode_scope(ProtectedMedia),
                or_(
                    ProtectedMedia.permanent.is_(True),
                    ProtectedMedia.expires_at.is_(None),
                    ProtectedMedia.expires_at > now,
                ),
            )
        )
        protected_episode_ids = {
            b.episode_id for b in protected_episode_result.scalars().all()
        }

    records: list[MatchedCandidateRecord] = []

    for series in all_series:
        if favorite_tmdb_ids and series.tmdb_id in favorite_tmdb_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_favorites_count += 1
            LOG.info(
                f"Skipping favorite series: {series.title} (TMDB ID: {series.tmdb_id})"
            )
            continue
        if not series.seasons:
            continue
        if series.id in protected_series_ids:
            if preview_metadata is not None:
                preview_metadata.skipped_protected_count += 1
            continue

        for season in series.seasons:
            if season.id in protected_season_ids:
                continue
            if not season.episodes:
                continue

            for episode in season.episodes:
                if target_ids is not None and episode.id not in target_ids:
                    continue
                if not _has_media_path(episode.path):
                    continue
                if episode.id in protected_episode_ids:
                    continue

                matched_rules: list[int] = []
                matched_criteria: dict[str, Any] = {}
                reasons: list[dict[str, Any]] = []

                for rule in rules:
                    if (
                        preview_metadata is not None
                        and _rule_uses_season_episode_watch_fields(rule)
                        and evaluate_advanced_rule_state(
                            rule,
                            target_scope=TARGET_EPISODE,
                            series=series,
                            season=season,
                            episode=episode,
                        )
                        is None
                    ):
                        _record_unavailable_season_inventory(
                            preview_metadata, series, season
                        )
                    if _evaluate_rule_for_episode(
                        series, season, episode, rule, matched_criteria, reasons
                    ):
                        matched_rules.append(rule.id)

                if not matched_rules:
                    continue

                records.append(
                    MatchedCandidateRecord(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        season_id=season.id,
                        episode_id=episode.id,
                        matched_rule_ids=matched_rules,
                        matched_criteria=matched_criteria,
                        reason=_rule_reason_text(reasons),
                        reason_data=reasons,
                        estimated_space_bytes=episode.size if episode.size else None,
                    )
                )

    return records


async def _sync_episode_candidates(
    db: AsyncSession,
    records: list[MatchedCandidateRecord],
    *,
    commit: bool = True,
) -> tuple[int, int, int]:
    """Synchronize episode candidates with the database."""
    existing_result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.media_type == MediaType.SERIES,
            _is_episode_scope(ReclaimCandidate),
        )
    )
    episode_candidate_lookup: dict[int, ReclaimCandidate] = {
        candidate.episode_id: candidate
        for candidate in existing_result.scalars().all()
        if candidate.episode_id is not None
    }

    matched_episode_ids = {
        record.episode_id for record in records if record.episode_id is not None
    }

    candidates_created = 0
    candidates_updated = 0
    for record in records:
        if record.episode_id is None or record.series_id is None:
            continue
        existing = episode_candidate_lookup.get(record.episode_id)

        if existing:
            existing.matched_rule_ids = record.matched_rule_ids
            existing.matched_criteria = record.matched_criteria
            existing.reason = record.reason
            existing.reason_data = record.reason_data
            existing.estimated_space_bytes = record.estimated_space_bytes
            existing.updated_at = datetime.now(UTC)
            candidates_updated += 1
        else:
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    matched_rule_ids=record.matched_rule_ids,
                    matched_criteria=record.matched_criteria,
                    reason=record.reason,
                    reason_data=record.reason_data,
                    estimated_space_bytes=record.estimated_space_bytes,
                    series_id=record.series_id,
                    season_id=record.season_id,
                    episode_id=record.episode_id,
                )
            )
            candidates_created += 1

    stale_candidates = [
        candidate
        for episode_id, candidate in episode_candidate_lookup.items()
        if episode_id not in matched_episode_ids
    ]
    candidates_removed = len(stale_candidates)
    for candidate in stale_candidates:
        await db.delete(candidate)

    if candidates_created > 0 or candidates_updated > 0 or candidates_removed > 0:
        if commit:
            await db.commit()
        else:
            await db.flush()

    return candidates_created, candidates_updated, candidates_removed


def _append_rule_reason(
    reasons: list[dict[str, Any]],
    *,
    rule: ReclaimRule,
    target_scope: str,
    conditions: list[dict[str, Any]],
    season_label: str | None = None,
) -> None:
    """Append normalized reason data for a matched rule."""
    if not conditions:
        return

    label = rule.name if not season_label else f"{rule.name} ({season_label})"
    summary = ", ".join(
        str(condition.get("display", "")).strip()
        for condition in conditions
        if isinstance(condition, dict) and str(condition.get("display", "")).strip()
    )
    text = f"{label}: {summary}" if summary else label
    reasons.append(
        {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "target_scope": target_scope,
            "season_label": season_label,
            "conditions": conditions,
            "text": text,
        }
    )


def _rule_reason_text(reasons: list[dict[str, Any]]) -> str:
    """Generate a human-readable text summary of rule reasons."""
    tokens = [
        str(part.get("text", "")).strip() for part in reasons if isinstance(part, dict)
    ]
    tokens = [token for token in tokens if token]
    return " | ".join(tokens) if tokens else "Matched cleanup rule"


def _evaluate_movie_rule(
    item: Movie | Series,
    rule: ReclaimRule,
    matched_criteria: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a whole-series rule, or any movie version for direct tests."""
    if isinstance(item, Movie):
        if normalize_rule_target(rule) != TARGET_MOVIE_VERSION:
            return False
        for version in item.versions:
            if _evaluate_movie_version_rule(
                item, version, rule, matched_criteria, reasons
            ):
                return True
        return False

    if normalize_rule_target(rule) != TARGET_SERIES:
        return False

    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule, target_scope=TARGET_SERIES, series=item
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_SERIES,
        conditions=rule_reasons,
    )
    return True


def _evaluate_movie_version_rule(
    movie: Movie,
    version: MovieVersion,
    rule: ReclaimRule,
    matched_criteria: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a movie rule against a single movie version candidate."""
    if normalize_rule_target(rule) != TARGET_MOVIE_VERSION:
        return False

    effective_size = version.size if version.size and version.size > 0 else movie.size
    if not effective_size or effective_size == 0:
        return False

    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    matched_criteria["media_version_id"] = version.id
    matched_criteria["movie_id"] = movie.id
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_MOVIE_VERSION,
        conditions=rule_reasons,
    )
    return True


def _evaluate_rule_for_season(
    series: Series,
    season: Season,
    rule: ReclaimRule,
    matched_criteria: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a rule against a specific season of a series."""
    if normalize_rule_target(rule) != TARGET_SEASON:
        return False

    if not season.size or season.size == 0:
        return False

    label = f"S{season.season_number:02d}"
    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule, target_scope=TARGET_SEASON, series=series, season=season
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_SEASON,
        conditions=rule_reasons,
        season_label=label,
    )
    return True


def _record_unavailable_season_inventory(
    metadata: RulePreviewMatchMetadata,
    series: Series,
    season: Season,
) -> None:
    """Record one preview-scoped season with unavailable Sonarr inventory."""
    key = (series.id, season.id)
    if key in metadata.season_inventory_unavailable_keys:
        return
    metadata.season_inventory_unavailable_keys.add(key)
    metadata.season_inventory_unavailable_count = len(
        metadata.season_inventory_unavailable_keys
    )
    if len(metadata.season_inventory_unavailable_examples) < 5:
        metadata.season_inventory_unavailable_examples.append(
            f"{series.title} S{season.season_number:02d}"
        )


def _evaluate_rule_for_episode(
    series: Series,
    season: Season,
    episode: Episode,
    rule: ReclaimRule,
    matched_criteria: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    """Evaluate a rule against a specific episode of a series."""
    if normalize_rule_target(rule) != TARGET_EPISODE:
        return False

    label = f"S{season.season_number:02d}E{episode.episode_number:02d}"
    matched, criteria, rule_reasons = evaluate_advanced_rule(
        rule,
        target_scope=TARGET_EPISODE,
        series=series,
        season=season,
        episode=episode,
    )
    if not matched:
        return False
    matched_criteria.update(criteria)
    _append_rule_reason(
        reasons,
        rule=rule,
        target_scope=TARGET_EPISODE,
        conditions=rule_reasons,
        season_label=label,
    )
    return True


async def tag_cleanup_candidates() -> None:
    """Serialize tag reconciliation with other candidate workflows."""
    async with candidate_workflow_lock:
        await _tag_cleanup_candidates_unlocked()


async def _tag_cleanup_candidates_unlocked() -> None:
    """Sync rule scoped rec-* tags for cleanup candidates in Radarr/Sonarr."""
    # check if services are configured before doing any work
    if not service_manager.radarr and not service_manager.sonarr:
        LOG.debug("Neither Radarr nor Sonarr configured, skipping tag sync")
        return

    LOG.info("Starting rule scoped cleanup candidate tagging")

    async with track_task_execution(Task.TAG_CLEANUP_CANDIDATES):
        try:
            movies_tagged, movies_untagged = await _sync_rule_radarr_tags()
            series_tagged, series_untagged = await _sync_rule_sonarr_tags()

            LOG.info(
                f"Tag sync completed: Movies ({movies_tagged} tagged, {movies_untagged} untagged), "
                f"Series ({series_tagged} tagged, {series_untagged} untagged)"
            )

        except Exception as e:
            LOG.error(f"Error syncing cleanup tags: {e}", exc_info=True)
            raise


def _rule_action(rule: ReclaimRule) -> dict[str, Any]:
    """Get the action dictionary for a rule, or an empty dictionary if none."""
    return rule.action or {}


def _rule_arr_config_ids(
    rule: ReclaimRule, service: Literal["radarr", "sonarr"]
) -> set[int]:
    """Return the explicitly selected ARR configs for one rule."""
    return set(get_arr_service_config_ids(_rule_action(rule), service))


def _candidate_arr_config_ids(
    candidate: "ReclaimCandidate",
    rules: Mapping[int, ReclaimRule],
    service: Literal["radarr", "sonarr"],
) -> set[int] | None:
    """Return configs eligible for a candidate, or None for automatic routing.

    Empty/unset targets preserve the historical behavior of routing to any
    matching active instance. When all matched rules explicitly select targets,
    their selections are combined.
    """
    matched_rules = [
        rules[rule_id]
        for rule_id in candidate.matched_rule_ids or []
        if rule_id in rules
    ]
    if not matched_rules:
        return None

    selected: set[int] = set()
    for rule in matched_rules:
        rule_targets = _rule_arr_config_ids(rule, service)
        if not rule_targets:
            return None
        selected.update(rule_targets)
    return selected


def _coerce_arr_delete_fallback(value: str | None) -> ArrDeleteFallback:
    if value == "remove_if_empty":
        return "remove_if_empty"
    if value == "unmonitor_only":
        return "unmonitor_only"
    return "unmonitor"


def _get_arr_action(
    candidate: "ReclaimCandidate",
    rules: dict[int, ReclaimRule],
    default_behavior: ArrDeleteFallback = "unmonitor",
) -> ArrDeleteAction:
    """Resolve ARR behavior for a candidate.

    Matched rules remain authoritative: if any matched rule requests
    ``unmonitor_only`` we honor that first (it is the most conservative -
    files are never touched), then ``unmonitor``, otherwise any matched rule
    implies ``delete``. Synthetic/no-rule candidates fall back to the global
    delete behavior.
    """
    matched_rules = [
        (rule_id, rules[rule_id])
        for rule_id in candidate.matched_rule_ids or []
        if rule_id in rules
    ]
    matched_rule_ids = [rule_id for rule_id, _rule in matched_rules]
    matched_arr_actions = {
        _rule_action(rule).get("arr_action") for _rule_id, rule in matched_rules
    }
    if "unmonitor_only" in matched_arr_actions:
        resolved_action: ArrDeleteAction = "unmonitor_only"
    elif "unmonitor" in matched_arr_actions:
        resolved_action = "unmonitor"
    else:
        resolved_action = "delete" if matched_rule_ids else default_behavior

    source = "matched_rule" if matched_rule_ids else "global_fallback"
    LOG.debug(
        f"Resolved ARR action for candidate {candidate.id}: {resolved_action} "
        f"(source={source}, matched_rule_ids={matched_rule_ids}, "
        f"configured_fallback={default_behavior})"
    )
    return resolved_action


def _matched_rule_ids_should_move_instead_of_delete(
    matched_rule_ids: Sequence[int],
    rules: dict[int, ReclaimRule],
) -> bool:
    """Return True when any matched rule ID chooses move over delete."""
    for rule_id in matched_rule_ids:
        rule = rules.get(rule_id)
        if (
            rule is not None
            and _rule_action(rule).get("move_instead_of_delete") is True
        ):
            return True
    return False


def _managed_tag_for_rule(rule: ReclaimRule) -> str | None:
    """Determine the rec-* tag to manage for a given rule, or None if no tagging."""
    action = _rule_action(rule)
    if action.get("tag_enabled", True) is False:
        return None
    tag = str(action.get("arr_tag") or "").strip().lower()
    if not tag:
        return None
    return tag if tag.startswith("rec-") else f"rec-{tag}"


def _merge_arr_action(
    current: ArrDeleteAction | None,
    candidate_action: ArrDeleteAction,
) -> ArrDeleteAction:
    """Merge multiple candidate actions for the same parent media.

    Precedence is strict, most conservative (files kept) to least:
    1. ``unmonitor_only`` wins - files are never touched
    2. ``unmonitor`` wins over delete
    3. explicit rule ``delete`` beats fallback ``remove_if_empty``
    4. ``remove_if_empty`` applies only if nothing stronger exists
    """
    if current == "unmonitor_only" or candidate_action == "unmonitor_only":
        return "unmonitor_only"
    if current == "unmonitor" or candidate_action == "unmonitor":
        return "unmonitor"
    if current == "delete" or candidate_action == "delete":
        return "delete"
    return "remove_if_empty"


async def _sync_rule_radarr_tags() -> tuple[int, int]:
    """Synchronize Radarr tags for cleanup candidates."""
    if not service_manager.radarr:
        return 0, 0

    clients = service_manager.radarr_clients()
    if not clients and service_manager.radarr:
        clients = {0: service_manager.radarr}

    async with async_db() as db:
        rules = {
            rule.id: rule
            for rule in (
                await db.execute(select(ReclaimRule).where(ReclaimRule.enabled == True))
            )
            .scalars()
            .all()
        }
        candidate_rows = (
            await db.execute(
                select(
                    ReclaimCandidate,
                    Movie.id,
                    MovieArrRef.service_config_id,
                    MovieArrRef.arr_movie_id,
                    Movie.tmdb_id,
                )
                .join(Movie, Movie.id == ReclaimCandidate.movie_id)
                .outerjoin(MovieArrRef, MovieArrRef.movie_id == Movie.id)
                .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            )
        ).all()

    by_config: dict[int, dict[str, set[int]]] = {}
    fallback_tmdb: dict[int, set[tuple[str, int]]] = {}
    for candidate, _movie_id, ref_config_id, arr_movie_id, tmdb_id in candidate_rows:
        for rule_id in candidate.matched_rule_ids or []:
            rule = rules.get(rule_id)
            if not rule:
                continue
            tag = _managed_tag_for_rule(rule)
            config_ids = _rule_arr_config_ids(rule, "radarr")
            if not tag or not config_ids:
                continue
            for config_id in config_ids:
                if ref_config_id == config_id and arr_movie_id is not None:
                    by_config.setdefault(config_id, {}).setdefault(tag, set()).add(
                        arr_movie_id
                    )
                elif tmdb_id:
                    fallback_tmdb.setdefault(config_id, set()).add((tag, tmdb_id))

    total_tagged = 0
    total_untagged = 0
    for config_id, client in clients.items():
        expected = by_config.get(config_id, {})
        all_movies = await client.get_all_movies()
        tmdb_to_id = {movie.tmdb_id: movie.id for movie in all_movies if movie.tmdb_id}
        for tag, tmdb_id in fallback_tmdb.get(config_id, set()):
            resolved = tmdb_to_id.get(tmdb_id)
            if resolved is not None:
                expected.setdefault(tag, set()).add(resolved)
        tagged, untagged = await _sync_expected_arr_tags(
            client=client,
            items_by_id={movie.id: movie for movie in all_movies},
            expected_by_tag=expected,
            add_tag=client.add_tag_to_movies,
            remove_tag=client.remove_tag_from_movies,
            label="Radarr",
        )
        total_tagged += tagged
        total_untagged += untagged
    return total_tagged, total_untagged


async def _sync_rule_sonarr_tags() -> tuple[int, int]:
    """Synchronize Sonarr tags for cleanup candidates."""
    if not service_manager.sonarr:
        return 0, 0

    clients = service_manager.sonarr_clients()
    if not clients and service_manager.sonarr:
        clients = {0: service_manager.sonarr}

    async with async_db() as db:
        rules = {
            rule.id: rule
            for rule in (
                await db.execute(select(ReclaimRule).where(ReclaimRule.enabled == True))
            )
            .scalars()
            .all()
        }
        candidate_rows = (
            await db.execute(
                select(
                    ReclaimCandidate,
                    Series.id,
                    SeriesArrRef.service_config_id,
                    SeriesArrRef.arr_series_id,
                    Series.tmdb_id,
                )
                .join(Series, Series.id == ReclaimCandidate.series_id)
                .outerjoin(SeriesArrRef, SeriesArrRef.series_id == Series.id)
                .where(ReclaimCandidate.media_type == MediaType.SERIES)
            )
        ).all()

    by_config: dict[int, dict[str, set[int]]] = {}
    fallback_tmdb: dict[int, set[tuple[str, int]]] = {}
    for candidate, _series_id, ref_config_id, arr_series_id, tmdb_id in candidate_rows:
        for rule_id in candidate.matched_rule_ids or []:
            rule = rules.get(rule_id)
            if not rule:
                continue
            tag = _managed_tag_for_rule(rule)
            config_ids = _rule_arr_config_ids(rule, "sonarr")
            if not tag or not config_ids:
                continue
            for config_id in config_ids:
                if ref_config_id == config_id and arr_series_id is not None:
                    by_config.setdefault(config_id, {}).setdefault(tag, set()).add(
                        arr_series_id
                    )
                elif tmdb_id:
                    fallback_tmdb.setdefault(config_id, set()).add((tag, int(tmdb_id)))

    total_tagged = 0
    total_untagged = 0
    for config_id, client in clients.items():
        expected = by_config.get(config_id, {})
        all_series = await client.get_all_series()
        tmdb_to_id = {
            series.tmdb_id: series.id for series in all_series if series.tmdb_id
        }
        for tag, tmdb_id in fallback_tmdb.get(config_id, set()):
            resolved = tmdb_to_id.get(tmdb_id)
            if resolved is not None:
                expected.setdefault(tag, set()).add(resolved)
        tagged, untagged = await _sync_expected_arr_tags(
            client=client,
            items_by_id={series.id: series for series in all_series},
            expected_by_tag=expected,
            add_tag=client.add_tag_to_series,
            remove_tag=client.remove_tag_from_series,
            label="Sonarr",
        )
        total_tagged += tagged
        total_untagged += untagged
    return total_tagged, total_untagged


async def _sync_expected_arr_tags(
    *,
    client: Any,
    items_by_id: dict[int, object],
    expected_by_tag: dict[str, set[int]],
    add_tag: Callable[[list[int], int], Awaitable[Any]],
    remove_tag: Callable[[list[int], int], Awaitable[Any]],
    label: str,
) -> tuple[int, int]:
    """Sync expected tags for a set of items, adding/removing as needed. Returns
    (tagged_count, untagged_count)."""
    tags = await client.get_tags()
    active_labels = set(expected_by_tag)
    tag_by_label = {tag.label.lower(): tag for tag in tags}
    managed_stale_tags = [
        tag
        for tag in tags
        if (
            tag.label.lower().startswith("rec-")
            or tag.label.lower().startswith("reclaimerr")
        )
        and tag.label.lower() not in active_labels
    ]

    tagged_count = 0
    untagged_count = 0

    for tag_label, expected_ids in expected_by_tag.items():
        tag = tag_by_label.get(tag_label) or await client.get_or_create_tag(tag_label)
        tag_by_label[tag_label] = tag
        to_add = [
            item_id
            for item_id in expected_ids
            if item_id in items_by_id
            and tag.id not in getattr(items_by_id[item_id], "tags", [])
        ]
        if to_add:
            await add_tag(to_add, tag.id)
            tagged_count += len(to_add)
            LOG.info(f"Tagged {len(to_add)} {label} item(s) with {tag_label}")

    for stale_tag in managed_stale_tags:
        to_remove = [
            item_id
            for item_id, item in items_by_id.items()
            if stale_tag.id in getattr(item, "tags", [])
        ]
        if to_remove:
            await remove_tag(to_remove, stale_tag.id)
            untagged_count += len(to_remove)
            LOG.info(
                f"Removed stale managed tag '{stale_tag.label}' from {len(to_remove)} {label} item(s)"
            )

    for tag_label, tag in tag_by_label.items():
        if not tag_label.startswith("rec-") or tag_label not in active_labels:
            continue
        expected_ids = expected_by_tag.get(tag_label, set())
        to_remove = [
            item_id
            for item_id, item in items_by_id.items()
            if item_id not in expected_ids and tag.id in getattr(item, "tags", [])
        ]
        if to_remove:
            await remove_tag(to_remove, tag.id)
            untagged_count += len(to_remove)
            LOG.info(
                f"Removed tag '{tag.label}' from {len(to_remove)} non-candidate {label} item(s)"
            )

    return tagged_count, untagged_count


# NOT USING THIS YET BUT RE-WRITE IT WHEN WE AUTOMATE FULL DELETIONS IF NEEDED
# async def delete_cleanup_candidates() -> None:
#     """Deletes all cleanup candidates from their respective services.

#     Deletion is based purely on ReclaimCandidate records in the database.
#     Tags are optional visual indicators only and do not affect deletion.

#     Deletion priority:
#     1. Use Radarr (movies) or Sonarr (series) if item has radarr_id/sonarr_id
#     2. Fall back to Media Server deletion (Jellyfin/Emby/Plex) if no radarr_id/sonarr_id but media_refs
#     exist for that service

#     After deletion, resets the request in Seerr and marks item as removed in database.
#     """
#     LOG.info("Starting cleanup candidate deletion")

#     async with track_task_execution(Task.DELETE_CLEANUP_CANDIDATES):
#         try:
#             movies_deleted = 0
#             series_deleted = 0
#             season_deleted = 0

#             # process movies
#             if (
#                 service_manager.radarr
#                 or service_manager.jellyfin
#                 or service_manager.emby
#                 or service_manager.plex
#             ):
#                 movies_deleted = await _delete_movie_candidates()

#             # process series
#             if (
#                 service_manager.sonarr
#                 or service_manager.jellyfin
#                 or service_manager.emby
#                 or service_manager.plex
#             ):
#                 series_deleted = await _delete_series_candidates()
#                 season_deleted = await _delete_season_candidates()

#             LOG.info(
#                 f"Deletion completed: {movies_deleted} movies, "
#                 f"{series_deleted} whole series, {season_deleted} season(s) removed"
#             )

#         except Exception as e:
#             LOG.error(f"Error deleting cleanup candidates: {e}", exc_info=True)
#             raise


async def delete_cleanup_candidates() -> dict[str, int]:
    """Serialize automatic deletion with other candidate workflows."""
    async with candidate_workflow_lock:
        return await _delete_cleanup_candidates_unlocked()


async def _delete_cleanup_candidates_unlocked() -> dict[str, int]:
    """Automatically delete eligible cleanup candidates when globally enabled."""
    LOG.info("Starting automatic cleanup candidate deletion")

    async with track_task_execution(Task.DELETE_CLEANUP_CANDIDATES):
        (
            eligible_ids,
            skipped_count,
            waiting_count,
        ) = await _select_auto_delete_eligible_candidate_ids()
        if not eligible_ids:
            LOG.info(
                "Automatic cleanup deletion found no eligible candidates "
                f"(waiting={waiting_count}, skipped={skipped_count})"
            )
            return {
                "eligible": 0,
                "waiting": waiting_count,
                "skipped": skipped_count,
                "revalidated_out": 0,
                "playback_unavailable": 0,
                "deleted": 0,
                "failed": 0,
            }

        revalidation = await _revalidate_auto_delete_candidate_ids(eligible_ids)
        eligible_ids = revalidation.candidate_ids
        skipped_count += revalidation.revalidated_out
        if revalidation.revalidated_out:
            LOG.info(
                "Auto-delete revalidation skipped "
                f"{revalidation.revalidated_out} candidate(s) that no longer "
                "matched an active automatic-deletion rule"
            )
        if revalidation.playback_unavailable:
            LOG.warning(
                "Auto-delete revalidation found unavailable playback data for "
                f"{revalidation.playback_unavailable} target(s); affected "
                "playback rules did not authorize deletion"
            )
        if not eligible_ids:
            return {
                "eligible": 0,
                "waiting": waiting_count,
                "skipped": skipped_count,
                "revalidated_out": revalidation.revalidated_out,
                "playback_unavailable": revalidation.playback_unavailable,
                "deleted": 0,
                "failed": 0,
            }

        deleted_count, failed_count = await delete_specific_candidates(
            eligible_ids,
            approved_by="system:auto-delete",
        )
        summary = {
            "eligible": len(eligible_ids),
            "waiting": waiting_count,
            "skipped": skipped_count,
            "revalidated_out": revalidation.revalidated_out,
            "playback_unavailable": revalidation.playback_unavailable,
            "deleted": deleted_count,
            "failed": failed_count,
        }
        LOG.info(
            "Automatic cleanup deletion completed: "
            f"eligible={summary['eligible']}, "
            f"waiting={summary['waiting']}, "
            f"skipped={summary['skipped']}, "
            f"revalidated_out={summary['revalidated_out']}, "
            f"playback_unavailable={summary['playback_unavailable']}, "
            f"deleted={summary['deleted']}, "
            f"failed={summary['failed']}"
        )
        return summary


async def _mark_candidate_delete_failure(candidate_id: int, error: str) -> None:
    """Persist delete failure details for a candidate."""
    async with async_db() as db:
        result = await db.execute(
            select(ReclaimCandidate).where(ReclaimCandidate.id == candidate_id)
        )
        candidate = result.scalar_one_or_none()
        if candidate is None:
            return
        candidate.delete_attempts = (candidate.delete_attempts or 0) + 1
        candidate.last_delete_attempt_at = datetime.now(UTC)
        candidate.last_delete_error = (
            summarize_error_message(error, max_chars=500) or "Deletion failed"
        )
        await db.commit()


async def _mark_candidate_delete_failures(
    candidate_ids: Iterable[int | None], error: str
) -> None:
    """Persist the same delete failure on multiple remaining candidates."""
    unique_ids = {candidate_id for candidate_id in candidate_ids if candidate_id}
    for candidate_id in unique_ids:
        await _mark_candidate_delete_failure(candidate_id, error)


async def _mark_unexplained_delete_failures(
    candidate_ids: Iterable[int], error: str
) -> None:
    """Mark remaining candidates that did not receive a more specific error."""
    unique_ids = {candidate_id for candidate_id in candidate_ids if candidate_id}
    if not unique_ids:
        return

    async with async_db() as db:
        result = await db.execute(
            select(ReclaimCandidate.id)
            .where(ReclaimCandidate.id.in_(unique_ids))
            .where(ReclaimCandidate.last_delete_error.is_(None))
        )
        remaining_ids = [row[0] for row in result.all()]

    await _mark_candidate_delete_failures(remaining_ids, error)


async def _load_path_mappings() -> list[dict[str, Any]]:
    """Load path mappings from GeneralSettings (returns empty list if unset)."""
    async with async_db() as db:
        result = await db.execute(select(GeneralSettings.path_mappings))
        path_mappings = result.scalars().first()
        if path_mappings:
            p_mappings: list[dict[str, Any]] = path_mappings
            return p_mappings
    return []


def _main_media_server_type() -> Service | None:
    """Return the configured main media server type based on the active client."""
    return service_manager.main_media_server_type


def _season_media_server_id(season: Season, service_type: Service | None) -> str | None:
    if service_type is Service.JELLYFIN:
        return season.jellyfin_season_id
    if service_type is Service.EMBY:
        return season.emby_season_id
    if service_type is Service.PLEX:
        return season.plex_season_rating_key
    return None


def _episode_media_server_id(
    episode: Episode, service_type: Service | None
) -> str | None:
    if service_type is Service.JELLYFIN:
        return episode.jellyfin_episode_id
    if service_type is Service.EMBY:
        return episode.emby_episode_id
    if service_type is Service.PLEX:
        return episode.plex_rating_key
    return None


def _path_is_inside_folder(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def _path_matches_arr_folder(
    path: str | None,
    arr_folder: str | None,
    path_mappings: Sequence[Mapping[str, Any]] | None,
    *,
    path_service_type: str | None = None,
    arr_service_type: str | None = None,
    arr_service_config_id: int | None = None,
) -> bool:
    path_variants = mapped_path_variants(
        path,
        path_mappings,
        service_type=path_service_type,
    )
    arr_variants = mapped_path_variants(
        arr_folder,
        path_mappings,
        service_type=arr_service_type,
        service_config_id=arr_service_config_id,
    )
    return any(
        _path_is_inside_folder(path_variant, arr_variant)
        for path_variant in path_variants
        for arr_variant in arr_variants
    )


def _order_series_arr_refs(
    refs: Sequence[SeriesArrRef],
    candidate_paths: Sequence[str | None],
    path_mappings: Sequence[Mapping[str, Any]] | None,
    *,
    media_service_type: Service | None,
) -> list[SeriesArrRef]:
    def ref_score(ref: SeriesArrRef) -> tuple[int, int]:
        if any(
            _path_matches_arr_folder(
                path,
                ref.arr_series_path,
                path_mappings,
                path_service_type=media_service_type.value
                if media_service_type
                else None,
                arr_service_type=Service.SONARR.value,
                arr_service_config_id=ref.service_config_id,
            )
            for path in candidate_paths
            if path
        ):
            return (0, ref.id or 0)
        if ref.arr_series_path:
            return (1, ref.id or 0)
        return (2, ref.id or 0)

    return sorted(refs, key=ref_score)


async def _load_arr_disk_space() -> list[dict[str, Any]]:
    """Fetch disk space from all configured Radarr/Sonarr instances.

    Each entry retains its service type and config ID so scoped path mappings
    and provider-specific rule targets can be resolved without conflating two
    Arr instances that happen to expose the same container path.
    """
    radarr_clients = service_manager.radarr_clients()
    if not radarr_clients and service_manager.radarr:
        radarr_clients = {0: service_manager.radarr}
    sonarr_clients = service_manager.sonarr_clients()
    if not sonarr_clients and service_manager.sonarr:
        sonarr_clients = {0: service_manager.sonarr}

    result: list[dict[str, Any]] = []

    client_groups = (
        (Service.RADARR, radarr_clients),
        (Service.SONARR, sonarr_clients),
    )
    for service_type, clients in client_groups:
        for config_id, client in clients.items():
            try:
                for entry in await client.get_disk_space():
                    path = str(entry.get("path", "") or "")
                    if not path:
                        continue
                    result.append(
                        {
                            **entry,
                            "path": path,
                            "service_type": service_type.value,
                            "service_config_id": config_id,
                        }
                    )
            except Exception as exc:
                LOG.debug(
                    f"Could not load {service_type.value} disk-space data "
                    f"for config {config_id}: {exc}"
                )

    return sorted(result, key=lambda e: -len(str(e.get("path") or "")))


async def _best_effort_radarr_rescan(
    movie_ids_by_config: dict[int, set[int]],
    *,
    context: str,
) -> None:
    """Best effort Radarr refresh for affected movies, grouped by config."""
    if not movie_ids_by_config:
        return
    radarr_clients = service_manager.radarr_clients()
    if not radarr_clients and service_manager.radarr:
        radarr_clients = {0: service_manager.radarr}
    for config_id, movie_ids in movie_ids_by_config.items():
        client = radarr_clients.get(config_id)
        if not client or not movie_ids:
            continue
        try:
            ids = sorted(movie_ids)
            await client.rescan_movies(ids)
            LOG.debug(
                f"{context}: triggered Radarr refresh for {len(ids)} movie(s) "
                f"(config {config_id})"
            )
        except Exception as e:
            LOG.warning(f"{context}: Radarr refresh failed for config {config_id}: {e}")


async def _best_effort_sonarr_refresh(
    series_ids_by_config: dict[int, set[int]],
    *,
    context: str,
) -> None:
    """Best effort Sonarr refresh for affected series, grouped by config."""
    if not series_ids_by_config:
        return
    sonarr_clients = service_manager.sonarr_clients()
    if not sonarr_clients and service_manager.sonarr:
        sonarr_clients = {0: service_manager.sonarr}
    for config_id, series_ids in series_ids_by_config.items():
        client = sonarr_clients.get(config_id)
        if not client or not series_ids:
            continue
        try:
            ids = sorted(series_ids)
            await client.refresh_series(ids)
            LOG.debug(
                f"{context}: triggered Sonarr refresh for {len(ids)} series "
                f"(config {config_id})"
            )
        except Exception as e:
            LOG.warning(f"{context}: Sonarr refresh failed for config {config_id}: {e}")


def _cleanup_moved_source_directories(
    paths: Iterable[Path],
    *,
    context: str,
) -> None:
    """Remove exact moved source directories when empty, deepest paths first."""
    unique_paths = set(paths)
    for path in sorted(unique_paths, key=lambda item: len(item.parts), reverse=True):
        if not path.exists():
            LOG.debug(f"{context}: verified source directory was removed: {path}")
            continue
        remove_empty_directory(
            path,
            log_context=context,
            log_remaining=True,
        )


def _service_value(service: Service | str | None) -> str | None:
    if service is None:
        return None
    return service.value if isinstance(service, Service) else str(service)


def _path_text(path: Path | None) -> str | None:
    return str(path) if path is not None else None


async def _resolve_event_local_path(
    path: str | None,
    service_type: Service | str | None = None,
    service_config_id: int | None = None,
) -> str | None:
    if not path:
        return None
    mappings = await _load_path_mappings()
    return _path_text(
        resolve_path(
            path,
            mappings,
            service_type=_service_value(service_type),
            service_config_id=service_config_id,
        )
    )


async def _dispatch_reclaim_event(
    *,
    action: str,
    media_type: MediaType,
    title: str | None = None,
    tmdb_id: int | None = None,
    candidate_id: int | None = None,
    path: str | None = None,
    destination_path: str | None = None,
    service_type: Service | str | None = None,
    service_config_id: int | None = None,
    movie_version_id: int | None = None,
    season_id: int | None = None,
    season_number: int | None = None,
    episode_id: int | None = None,
    episode_number: int | None = None,
    local_path: str | None = None,
) -> None:
    try:
        await dispatch_candidate_file_event(
            CandidateFileEvent(
                action=action,
                media_type=media_type,
                title=title,
                tmdb_id=tmdb_id,
                candidate_id=candidate_id,
                path=path,
                local_path=local_path
                if local_path is not None
                else await _resolve_event_local_path(
                    path, service_type=service_type, service_config_id=service_config_id
                ),
                destination_path=destination_path,
                service_type=service_type,
                service_config_id=service_config_id,
                movie_version_id=movie_version_id,
                season_id=season_id,
                season_number=season_number,
                episode_id=episode_id,
                episode_number=episode_number,
            )
        )
    except Exception as e:
        LOG.warning(f"Post-action webhook dispatch failed: {e}")


async def _delete_movie_version_candidates(
    version_candidates: list[ReclaimCandidate],
    approved_by: str = "system",
    media_server_fallback_enabled: bool = True,
    add_arr_import_exclusions_on_delete: bool = True,
    rules: dict[int, ReclaimRule] | None = None,
    default_arr_delete_behavior: ArrDeleteFallback = "unmonitor",
) -> int:
    """Deletes movie version candidates using service aware targeted deletion.

    These are version scoped deletions (e.g. delete 1080p but keep 4K) or
    candidates that could not be safely promoted to a full Radarr movie delete.
    Radarr cannot delete individual quality versions, so the media server is the
    only authority here.  When *media_server_fallback_enabled* is False the
    deletion is skipped and a warning is logged instead.
    """
    if not version_candidates:
        return 0

    if not media_server_fallback_enabled:
        for candidate in version_candidates:
            async with async_db() as db:
                result = await db.execute(
                    select(MovieVersion)
                    .where(MovieVersion.id == candidate.movie_version_id)
                    .options(selectinload(MovieVersion.movie))
                )
                version = result.scalars().first()
                title = version.movie.title if version and version.movie else "unknown"
            await _mark_candidate_delete_failure(
                candidate.id,
                "Partial movie-version delete requires media server fallback, "
                "or a Radarr delete that covers the full Radarr movie entry",
            )
            LOG.warning(
                f"Media server fallback disabled - skipping movie-version deletion "
                f"for '{title}'. Enable 'Allow Media Server Fallback Deletion' in "
                f"General Settings to delete individual quality versions or "
                f"non-promotable version candidates."
            )
        return 0

    main_service = service_manager.main_media_server
    if not main_service:
        for candidate in version_candidates:
            await _mark_candidate_delete_failure(
                candidate.id, "No main media server configured"
            )
        return 0

    main_service_type = service_manager.main_media_server_type or Service.PLEX

    async with async_db() as db:
        version_ids = [
            c.movie_version_id for c in version_candidates if c.movie_version_id
        ]
        result = await db.execute(
            select(MovieVersion)
            .where(MovieVersion.id.in_(version_ids))
            .options(selectinload(MovieVersion.movie).selectinload(Movie.versions))
        )
        versions = {v.id: v for v in result.scalars().all()}

    deleted_count = 0
    for candidate in version_candidates:
        if candidate.movie_version_id is None:
            continue
        cand_arr_action = _get_arr_action(
            candidate, rules or {}, default_arr_delete_behavior
        )
        if cand_arr_action == "unmonitor_only":
            # Radarr can't isolate a single quality version to unmonitor, and
            # this fallback path only runs when no Radarr instance could be
            # routed for the whole movie either - there's nothing to unmonitor,
            # so the file is left untouched by design rather than deleted.
            await _mark_candidate_delete_failure(
                candidate.id,
                "Unmonitor Only: no Radarr instance covers this quality version, "
                "so nothing could be unmonitored; the file was left untouched by "
                "design",
            )
            continue
        version = versions.get(candidate.movie_version_id)
        if version is None:
            await _mark_candidate_delete_failure(
                candidate.id, "Movie version missing from database"
            )
            continue
        movie = version.movie
        if movie is None:
            await _mark_candidate_delete_failure(
                candidate.id, "Movie missing for candidate version"
            )
            continue
        if version.service != main_service_type:
            await _mark_candidate_delete_failure(
                candidate.id,
                f"Version belongs to {version.service.value}, main media server is {main_service_type.value}",
            )
            continue

        same_item_versions = [
            v
            for v in movie.versions
            if v.service == main_service_type
            and v.service_item_id == version.service_item_id
        ]
        if main_service_type in {Service.JELLYFIN, Service.EMBY}:
            if len(same_item_versions) != 1:
                await _mark_candidate_delete_failure(
                    candidate.id,
                    "Exact version delete unsupported: multiple versions share one Emby/Jellyfin item id",
                )
                continue

        try:
            await main_service.delete_movie_version(
                version.service_item_id, version.service_media_id
            )

            # attempt filesystem sibling cleanup (subtitle/nfo files + empty dirs)
            path_mappings = await _load_path_mappings()
            local_path = resolve_path(
                version.path, path_mappings, service_type=main_service_type.value
            )
            if local_path:
                try:
                    sibling_cleanup(local_path)
                except Exception as fs_err:
                    LOG.warning(
                        f"sibling_cleanup failed for '{version.path}': {fs_err}"
                    )

            async with async_db() as db:
                remove_arr_refs: list[tuple[int, int]] = []
                cand_result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = cand_result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                ver_result = await db.execute(
                    select(MovieVersion).where(MovieVersion.id == version.id)
                )
                ver_db = ver_result.scalar_one_or_none()
                if ver_db:
                    deleted_size = ver_db.size or 0
                    movie_result = await db.execute(
                        select(Movie).where(Movie.id == ver_db.movie_id)
                    )
                    movie_db = movie_result.scalar_one_or_none()
                    if movie_db and movie_db.size:
                        movie_db.size = max(0, movie_db.size - deleted_size)
                    await db.delete(ver_db)
                    await db.flush()
                    movie_db = await _soft_remove_movie_if_empty(db, ver_db.movie_id)
                    if (
                        cand_arr_action == "remove_if_empty"
                        and movie_db is not None
                        and movie_db.removed_at is not None
                    ):
                        arr_ref_rows = (
                            await db.execute(
                                select(
                                    MovieArrRef.service_config_id,
                                    MovieArrRef.arr_movie_id,
                                ).where(MovieArrRef.movie_id == ver_db.movie_id)
                            )
                        ).all()
                        eligible_config_ids = _candidate_arr_config_ids(
                            candidate, rules or {}, "radarr"
                        )
                        remove_arr_refs = [
                            (config_id, arr_movie_id)
                            for config_id, arr_movie_id in arr_ref_rows
                            if eligible_config_ids is None
                            or config_id in eligible_config_ids
                        ]

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.MOVIE,
                        tmdb_id=movie.tmdb_id,
                        name=movie.title,
                        path=version.path,
                        size=version.size,
                        attributes=_build_reclaim_history_attributes(
                            movie_version=version
                        ),
                    )
                )

                await db.commit()

            if remove_arr_refs:
                radarr_clients = service_manager.radarr_clients()
                if not radarr_clients and service_manager.radarr:
                    radarr_clients = {0: service_manager.radarr}
                for config_id, arr_movie_id in remove_arr_refs:
                    client = radarr_clients.get(config_id)
                    if not client:
                        continue
                    try:
                        await client.delete_movies(
                            [arr_movie_id],
                            delete_files=False,
                            add_import_exclusion=add_arr_import_exclusions_on_delete,
                        )
                        LOG.info(
                            f"Removed '{movie.title}' from Radarr entirely "
                            f"(no files remaining, config_id={config_id}, "
                            f"arr_id={arr_movie_id}, resolved_action={cand_arr_action}, "
                            f"matched_rule_ids={candidate.matched_rule_ids or []}, "
                            f"configured_fallback={default_arr_delete_behavior})"
                        )
                    except Exception as arr_err:
                        LOG.warning(
                            f"Could not remove empty movie '{movie.title}' from Radarr: {arr_err}"
                        )

            deleted_count += 1
            LOG.info(
                f"Deleted movie version {version.id} for '{movie.title}' via {main_service_type.value}"
            )
            await _dispatch_reclaim_event(
                action="deleted",
                media_type=MediaType.MOVIE,
                title=movie.title,
                tmdb_id=movie.tmdb_id,
                candidate_id=candidate.id,
                path=version.path,
                local_path=_path_text(local_path),
                service_type=main_service_type,
                movie_version_id=version.id,
            )
        except Exception as e:
            await _mark_candidate_delete_failure(
                candidate.id,
                f"Version delete failed via {main_service_type.value}: {e}",
            )

    return deleted_count


async def _delete_movie_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete movie candidates. Returns count of deleted candidates."""
    deleted_count = 0

    # load fallback toggle + move settings from settings
    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )
        default_arr_delete_behavior = _coerce_arr_delete_fallback(
            settings_row.default_arr_delete_behavior if settings_row else None
        )
        add_arr_import_exclusions_on_delete = (
            settings_row.add_arr_import_exclusions_on_delete if settings_row else True
        )
        favorites_ignore_enabled = (
            bool(settings_row.favorites_ignore_enabled) if settings_row else False
        )
        favorites_protect_all_users = (
            bool(settings_row.favorites_protect_all_users) if settings_row else False
        )
        favorites_usernames = {
            _normalize_favorites_username(str(raw))
            for raw in (
                (settings_row.favorites_usernames or []) if settings_row else []
            )
            if str(raw).strip()
        }
        move_path_mappings: list[dict[str, Any]] = (
            settings_row.path_mappings or [] if settings_row else []
        )

    # get all movie candidates from database
    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Movie, ReclaimCandidate.movie_id == Movie.id)
            .where(ReclaimCandidate.media_type == MediaType.MOVIE)
            .where(Movie.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()

        if not candidates:
            LOG.debug("No movie candidates to delete")
            return 0

        if favorites_ignore_enabled:
            (
                candidates,
                skipped_for_favorites,
            ) = await _filter_movie_candidates_by_favorites(
                db,
                candidates,
                protect_all_users=favorites_protect_all_users,
                usernames=favorites_usernames,
            )
            if skipped_for_favorites:
                LOG.info(
                    f"Skipped {skipped_for_favorites} movie candidate(s) due to favorites protection"
                )
            if not candidates:
                LOG.info("All movie candidates skipped due to favorites protection")
                return 0

        LOG.info(f"Found {len(candidates)} movie candidates to evaluate for deletion")

    # load rules for arr_action resolution
    all_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        rules_by_id: dict[int, ReclaimRule] = {}
        if all_rule_ids:
            rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(all_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    # determine per-movie action ("delete" or "unmonitor")
    movie_arr_action: dict[int, ArrDeleteAction] = {}
    for cand in candidates:
        if cand.movie_id:
            action = _get_arr_action(cand, rules_by_id, default_arr_delete_behavior)
            movie_arr_action[cand.movie_id] = _merge_arr_action(
                movie_arr_action.get(cand.movie_id),
                action,
            )

    # track which specific version IDs should have files cleaned up per movie.
    # None in the set means a whole-movie candidate exists (clean up all versions).
    candidate_version_ids_by_movie: dict[int, set[int | None]] = {}
    for cand in candidates:
        if cand.movie_id is not None:
            candidate_version_ids_by_movie.setdefault(cand.movie_id, set()).add(
                cand.movie_version_id
            )

    whole_movie_candidates = [c for c in candidates if c.movie_version_id is None]
    version_candidates = [c for c in candidates if c.movie_version_id is not None]

    radarr_clients = service_manager.radarr_clients()
    if not radarr_clients and service_manager.radarr:
        radarr_clients = {0: service_manager.radarr}

    # Load movie data + all arr refs (with stored arr_movie_path for path-first routing).
    # Path-first routing: match the media-server file path (MovieVersion.path) against
    # the Radarr folder path (MovieArrRef.arr_movie_path) to find the correct Radarr
    # instance for each version.  Falls back to service_config_id membership when
    # arr_movie_path is not yet populated (e.g. before first sync after upgrade).
    all_movie_ids = {c.movie_id for c in candidates if c.movie_id}
    async with async_db() as db:
        rows = (
            await db.execute(
                select(
                    Movie.id,
                    Movie.title,
                    Movie.tmdb_id,
                    MovieArrRef.service_config_id,
                    MovieArrRef.arr_movie_id,
                    MovieArrRef.arr_movie_path,
                )
                .outerjoin(MovieArrRef, MovieArrRef.movie_id == Movie.id)
                .where(Movie.id.in_(all_movie_ids))
                .where(Movie.removed_at.is_(None))
            )
        ).all()
        movie_data: dict[int, dict[str, Any]] = {}
        for movie_id, title, tmdb_id, config_id, arr_movie_id, arr_movie_path in rows:
            info = movie_data.setdefault(
                movie_id,
                {"title": title, "tmdb_id": tmdb_id, "refs": []},
            )
            if config_id is not None and arr_movie_id is not None:
                info["refs"].append((config_id, arr_movie_id, arr_movie_path))

        # load version paths needed for path-based routing and safety checks
        version_ids = [
            c.movie_version_id for c in version_candidates if c.movie_version_id
        ]
        version_path_by_id: dict[int, str | None] = {}
        version_service_by_id: dict[int, Service | None] = {}
        if version_ids:
            ver_rows = (
                await db.execute(
                    select(
                        MovieVersion.id,
                        MovieVersion.path,
                        MovieVersion.service,
                    ).where(MovieVersion.id.in_(version_ids))
                )
            ).all()
            version_path_by_id = {
                ver_id: ver_path for ver_id, ver_path, _service in ver_rows
            }
            version_service_by_id = {
                ver_id: service for ver_id, _ver_path, service in ver_rows
            }

        all_version_rows = (
            await db.execute(
                select(
                    MovieVersion.movie_id,
                    MovieVersion.id,
                    MovieVersion.path,
                    MovieVersion.service,
                ).where(MovieVersion.movie_id.in_(all_movie_ids))
            )
        ).all()
        version_info_by_movie: dict[
            int, dict[int, tuple[str | None, Service | None]]
        ] = {}
        for movie_id, version_id, version_path, version_service in all_version_rows:
            if movie_id is not None:
                version_info_by_movie.setdefault(movie_id, {})[version_id] = (
                    version_path,
                    version_service,
                )

    def _match_version_to_arr(
        version_path: str | None,
        version_service: Service | None,
        refs: list[tuple[Any, ...]],
        allowed_config_ids: set[int] | None,
    ) -> set[tuple[int, int]]:
        """Return (config_id, arr_movie_id) pairs whose folder path contains this version.

        Path prefix matching is tried first.  If there is only one active Radarr
        ref and no path proof is available, route to that one ref.  Multi Radarr
        cases without a path proof fail closed and use media-server fallback.
        """
        active_refs = [
            (config_id, arr_movie_id, arr_movie_path)
            for config_id, arr_movie_id, arr_movie_path in refs
            if config_id in radarr_clients
            and (allowed_config_ids is None or config_id in allowed_config_ids)
        ]
        if not active_refs:
            return set()
        matched: set[tuple[int, int]] = set()
        for config_id, arr_movie_id, arr_movie_path in active_refs:
            if _path_matches_arr_folder(
                version_path,
                arr_movie_path,
                move_path_mappings,
                path_service_type=version_service.value if version_service else None,
                arr_service_type=Service.RADARR.value,
                arr_service_config_id=config_id,
            ):
                matched.add((config_id, arr_movie_id))
        if not matched:
            if len(active_refs) == 1:
                config_id, arr_movie_id, arr_movie_path = active_refs[0]
                # Preserve the legacy automatic single-instance fallback. For
                # explicitly targeted rules, a known non-matching folder is
                # negative proof and must not be overridden by selection.
                if allowed_config_ids is None or not version_path or not arr_movie_path:
                    matched.add((config_id, arr_movie_id))
        return matched

    def _candidate_covers_full_radarr_entry(
        movie_id: int,
        selected_version_ids: set[int],
        matched_refs: set[tuple[int, int]],
        refs: list[tuple[int, int, str | None]],
    ) -> bool:
        """Return True only when Radarr delete cannot remove unselected versions."""
        if not selected_version_ids or not matched_refs:
            return False

        version_info = version_info_by_movie.get(movie_id, {})
        if not version_info:
            return False

        refs_by_pair = {
            (config_id, arr_movie_id): arr_movie_path
            for config_id, arr_movie_id, arr_movie_path in refs
        }
        all_version_ids = set(version_info)

        for pair in matched_refs:
            arr_movie_path = refs_by_pair.get(pair)
            if arr_movie_path:
                ref_version_ids = {
                    version_id
                    for version_id, (
                        version_path,
                        version_service,
                    ) in version_info.items()
                    if _path_matches_arr_folder(
                        version_path,
                        arr_movie_path,
                        move_path_mappings,
                        path_service_type=version_service.value
                        if version_service
                        else None,
                        arr_service_type=Service.RADARR.value,
                        arr_service_config_id=pair[0],
                    )
                }
                if not ref_version_ids:
                    if len(refs) == 1 and selected_version_ids == all_version_ids:
                        continue
                    return False
                if not ref_version_ids.issubset(selected_version_ids):
                    return False
                continue

            # without an arr path, only a single ref movie with all known versions
            # selected is provably safe to delete through Radarr.
            if len(refs) != 1 or selected_version_ids != all_version_ids:
                return False

        return True

    # Route each candidate to the correct arr instance(s).
    # movie_id -> set of (config_id, arr_movie_id)
    movie_arr_routing: dict[int, set[tuple[int, int]]] = {}
    # movie_id -> all candidate IDs that belong to it (for DB cleanup after deletion)
    all_cand_ids_by_movie: dict[int, list[int]] = {}
    # version candidates that could not be routed to any arr instance
    unmatched_version_candidates: list[ReclaimCandidate] = []

    if radarr_clients:
        for cand in version_candidates:
            if not cand.movie_id or not cand.movie_version_id:
                unmatched_version_candidates.append(cand)
                continue
            movie_info = movie_data.get(cand.movie_id)
            if not movie_info or not movie_info["refs"]:
                unmatched_version_candidates.append(cand)
                continue
            ver_path = version_path_by_id.get(cand.movie_version_id)
            ver_service = version_service_by_id.get(cand.movie_version_id)
            allowed_config_ids = _candidate_arr_config_ids(cand, rules_by_id, "radarr")
            eligible_refs = [
                ref
                for ref in movie_info["refs"]
                if allowed_config_ids is None or ref[0] in allowed_config_ids
            ]
            matched = _match_version_to_arr(
                ver_path,
                ver_service,
                eligible_refs,
                allowed_config_ids,
            )
            cand_action = movie_arr_action.get(cand.movie_id or 0, "delete")
            if matched and cand_action in UNMONITOR_LIKE_ARR_ACTIONS:
                # version-level unmonitor: Radarr un-monitors the whole movie;
                # file deletion (if any - "unmonitor_only" skips it) is scoped
                # to just this version in the unmonitor block
                movie_arr_routing.setdefault(cand.movie_id, set()).update(matched)
                all_cand_ids_by_movie.setdefault(cand.movie_id, []).append(cand.id)
            elif matched and cand_action == "delete":
                selected_version_ids = {
                    version_id
                    for version_id in candidate_version_ids_by_movie.get(
                        cand.movie_id, set()
                    )
                    if version_id is not None
                }
                if _candidate_covers_full_radarr_entry(
                    cand.movie_id,
                    selected_version_ids,
                    matched,
                    eligible_refs,
                ):
                    movie_arr_routing.setdefault(cand.movie_id, set()).update(matched)
                    all_cand_ids_by_movie.setdefault(cand.movie_id, []).append(cand.id)
                else:
                    # Radarr would remove unselected versions; use media-server
                    # version delete if fallback is enabled.
                    unmatched_version_candidates.append(cand)
            else:
                LOG.warning(
                    f"No Radarr instance found for '{movie_info['title']}' "
                    f"(path: {ver_path!r}) - routing to media server fallback"
                )
                unmatched_version_candidates.append(cand)

        for cand in whole_movie_candidates:
            if not cand.movie_id:
                continue
            movie_info = movie_data.get(cand.movie_id)
            if not movie_info:
                continue
            # whole-movie candidates go to ALL arr instances that know this movie
            allowed_config_ids = _candidate_arr_config_ids(cand, rules_by_id, "radarr")
            for config_id, arr_movie_id, _ in movie_info["refs"]:
                if config_id in radarr_clients and (
                    allowed_config_ids is None or config_id in allowed_config_ids
                ):
                    movie_arr_routing.setdefault(cand.movie_id, set()).add(
                        (config_id, arr_movie_id)
                    )
            all_cand_ids_by_movie.setdefault(cand.movie_id, []).append(cand.id)
    else:
        unmatched_version_candidates = list(version_candidates)

    # Handle version candidates not routable to any arr instance via media server
    if unmatched_version_candidates:
        deleted_count += await _delete_movie_version_candidates(
            unmatched_version_candidates,
            approved_by,
            media_server_fallback_enabled,
            add_arr_import_exclusions_on_delete,
            rules_by_id,
            default_arr_delete_behavior,
        )

    if not movie_arr_routing:
        # No arr deletions - route remaining whole-movie candidates to media server
        if whole_movie_candidates:
            if media_server_fallback_enabled:
                deleted_count += await _delete_movies_via_media_server(
                    whole_movie_candidates, [], approved_by
                )
            else:
                for cand in whole_movie_candidates:
                    title = movie_data.get(cand.movie_id or 0, {}).get(
                        "title", "unknown"
                    )
                    await _mark_unexplained_delete_failures(
                        [cand.id],
                        (
                            "Media server fallback disabled and movie was not found "
                            "in any active Radarr instance"
                        ),
                    )
                    LOG.warning(
                        f"Media server fallback disabled - skipping deletion for "
                        f"'{title}' (not found in any Radarr instance)"
                    )
        return deleted_count

    # build per-config deletion / unmonitor batches
    movies_to_delete_by_config: dict[int, list[dict[str, Any]]] = {}
    movies_to_unmonitor_by_config: dict[int, list[dict[str, Any]]] = {}
    for movie_id, config_arr_set in movie_arr_routing.items():
        movie_info = movie_data.get(movie_id, {})
        all_cand_ids = all_cand_ids_by_movie.get(movie_id, [])
        target = (
            movies_to_unmonitor_by_config
            if movie_arr_action.get(movie_id) in UNMONITOR_LIKE_ARR_ACTIONS
            else movies_to_delete_by_config
        )
        for config_id, arr_movie_id in config_arr_set:
            target.setdefault(config_id, []).append(
                {
                    "candidate_id": all_cand_ids[0] if all_cand_ids else None,
                    "all_candidate_ids": all_cand_ids,
                    "movie_id": movie_id,
                    "radarr_id": arr_movie_id,
                    "config_id": config_id,
                    "title": movie_info.get("title", ""),
                    "method": "radarr",
                    "tmdb_id": movie_info.get("tmdb_id"),
                    "target_version_ids": candidate_version_ids_by_movie.get(
                        movie_id, set()
                    ),
                }
            )

    movies_to_delete: list[dict[str, Any]] = []
    radarr_refresh_after_delete: dict[int, set[int]] = {}
    for config_id, batch in movies_to_delete_by_config.items():
        client = radarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Deleting {len(batch)} movies via Radarr config {config_id}")
        radarr_ids = [m["radarr_id"] for m in batch]
        try:
            await client.delete_movies(
                radarr_ids,
                add_import_exclusion=add_arr_import_exclusions_on_delete,
            )
            movies_to_delete.extend(batch)
            radarr_refresh_after_delete.setdefault(config_id, set()).update(radarr_ids)
        except Exception as e:
            failed_candidate_ids = [
                cand_id
                for movie_info in batch
                for cand_id in movie_info.get("all_candidate_ids", [])
            ]
            await _mark_candidate_delete_failures(
                failed_candidate_ids,
                f"Radarr delete failed for config {config_id}: {e}",
            )
            LOG.error(
                f"Error deleting movies via Radarr config {config_id}: {e}",
                exc_info=True,
            )

    if movies_to_delete:
        movie_events: list[dict[str, Any]] = []
        try:
            async with async_db() as db:
                # use seen_movie_ids to avoid double writes when a movie appears in
                # multiple arr configs (both Radarr_1080p and Radarr4K)
                seen_movie_ids: set[int] = set()
                for movie_info in movies_to_delete:
                    movie_id = movie_info["movie_id"]
                    already_processed = movie_id in seen_movie_ids
                    seen_movie_ids.add(movie_id)

                    result = await db.execute(
                        select(Movie)
                        .where(Movie.id == movie_id)
                        .options(selectinload(Movie.versions))
                    )
                    movie = result.scalar_one_or_none()

                    if not already_processed:
                        history_size = None
                        history_attributes = None
                        if movie:
                            delete_target_version_ids: set[int | None] = (
                                movie_info.get("target_version_ids") or set()
                            )
                            is_whole_movie = (
                                not delete_target_version_ids
                                or None in delete_target_version_ids
                            )
                            concrete_delete_target_version_ids = {
                                version_id
                                for version_id in delete_target_version_ids
                                if version_id is not None
                            }
                            if is_whole_movie:
                                event_versions: list[MovieVersion | None] = [
                                    v for v in movie.versions if v.path
                                ] or [None]
                                movie.removed_at = datetime.now(UTC)
                                movie.added_at = None
                                history_size = movie.size
                                history_attributes = None
                            else:
                                target_versions = [
                                    v
                                    for v in movie.versions
                                    if v.id in concrete_delete_target_version_ids
                                ]
                                event_versions = [
                                    v for v in target_versions if v.path
                                ] or [None]
                                history_size = sum(v.size or 0 for v in target_versions)
                                history_attributes = (
                                    _build_reclaim_history_attributes(
                                        movie_version=target_versions[0]
                                    )
                                    if len(target_versions) == 1
                                    else None
                                )
                                if history_size and movie.size is not None:
                                    movie.size = max(0, movie.size - history_size)
                                if concrete_delete_target_version_ids:
                                    await db.execute(
                                        delete(MovieVersion).where(
                                            MovieVersion.id.in_(
                                                concrete_delete_target_version_ids
                                            )
                                        )
                                    )
                                    await db.flush()
                                    await _soft_remove_movie_if_empty(db, movie.id)

                            for version in event_versions:
                                movie_events.append(
                                    {
                                        "title": movie.title,
                                        "tmdb_id": movie.tmdb_id,
                                        "candidate_id": movie_info["candidate_id"],
                                        "path": version.path if version else None,
                                        "service_type": version.service
                                        if version
                                        else Service.RADARR,
                                        "movie_version_id": version.id
                                        if version
                                        else None,
                                    }
                                )

                        for cand_id in movie_info["all_candidate_ids"]:
                            result = await db.execute(
                                select(ReclaimCandidate).where(
                                    ReclaimCandidate.id == cand_id
                                )
                            )
                            reclaim_candidate = result.scalar_one_or_none()
                            if reclaim_candidate:
                                await db.delete(reclaim_candidate)

                        movie_tmdb_id = movie_info.get("tmdb_id")
                        if service_manager.seerr and movie and movie_tmdb_id:
                            try:
                                await _reset_seerr_request(
                                    movie_tmdb_id, MediaType.MOVIE
                                )
                            except Exception as e:
                                LOG.warning(
                                    f"Failed to reset Seerr request for {movie_info['title']}: {e}"
                                )

                        db.add(
                            ReclaimHistory(
                                approved_by=approved_by,
                                media_type=MediaType.MOVIE,
                                tmdb_id=movie_info.get("tmdb_id"),
                                name=movie_info.get("title"),
                                size=history_size if movie else None,
                                attributes=history_attributes if movie else None,
                            )
                        )

                await db.commit()

            unique_deleted = len({m["movie_id"] for m in movies_to_delete})
            deleted_count += unique_deleted
            LOG.info(f"Successfully deleted {unique_deleted} movies via Radarr")
            for event in movie_events:
                await _dispatch_reclaim_event(
                    action="deleted",
                    media_type=MediaType.MOVIE,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing movie deletion state: {e}", exc_info=True)
        await _best_effort_radarr_rescan(
            radarr_refresh_after_delete,
            context="movie delete cleanup",
        )

    # process unmonitor batches: mark unmonitored in Radarr, then delete files + media server
    movies_to_unmonitor: list[dict[str, Any]] = []
    radarr_refresh_after_unmonitor: dict[int, set[int]] = {}
    for config_id, batch in movies_to_unmonitor_by_config.items():
        client = radarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Unmonitoring {len(batch)} movies via Radarr config {config_id}")
        radarr_ids = [m["radarr_id"] for m in batch]
        try:
            await client.unmonitor_movies(radarr_ids)
            movies_to_unmonitor.extend(batch)
            radarr_refresh_after_unmonitor.setdefault(config_id, set()).update(
                radarr_ids
            )
        except Exception as e:
            failed_candidate_ids = [
                cand_id
                for movie_info in batch
                for cand_id in movie_info.get("all_candidate_ids", [])
            ]
            await _mark_candidate_delete_failures(
                failed_candidate_ids,
                f"Radarr unmonitor failed for config {config_id}: {e}",
            )
            LOG.error(
                f"Error unmonitoring movies via Radarr config {config_id}: {e}",
                exc_info=True,
            )

    if movies_to_unmonitor:
        unmonitor_events: list[dict[str, Any]] = []
        path_mappings = await _load_path_mappings()
        main_service = service_manager.main_media_server
        unmonitor_service_type: Service | None = service_manager.main_media_server_type
        try:
            async with async_db() as db:
                seen_unmonitor_ids: set[int] = set()
                for movie_info in movies_to_unmonitor:
                    movie_id = movie_info["movie_id"]
                    already_processed = movie_id in seen_unmonitor_ids
                    seen_unmonitor_ids.add(movie_id)
                    keep_files = movie_arr_action.get(movie_id) == "unmonitor_only"

                    result = await db.execute(
                        select(Movie)
                        .where(Movie.id == movie_id)
                        .options(selectinload(Movie.versions))
                    )
                    movie = result.scalar_one_or_none()

                    if not already_processed:
                        if movie:
                            # determine whether this is a whole-movie or version specific operation
                            unmonitor_target_version_ids: set[int | None] = (
                                movie_info.get("target_version_ids") or set()
                            )
                            is_whole_movie = (
                                not unmonitor_target_version_ids
                                or None in unmonitor_target_version_ids
                            )
                            concrete_unmonitor_target_version_ids: set[int] = {
                                ver_id
                                for ver_id in unmonitor_target_version_ids
                                if ver_id is not None
                            }

                            if not keep_files:
                                # delete files from disk (only for candidate version(s)).
                                # sibling_cleanup matches by file stem so it naturally scopes
                                # to subtitles/nfo files belonging to that specific version.
                                for ver in movie.versions:
                                    if not ver.path:
                                        continue
                                    if (
                                        not is_whole_movie
                                        and ver.id not in unmonitor_target_version_ids
                                    ):
                                        continue  # preserve non candidate version files
                                    local_path = resolve_path(
                                        ver.path,
                                        path_mappings,
                                        service_type=unmonitor_service_type.value
                                        if unmonitor_service_type
                                        else None,
                                    )
                                    if local_path:
                                        try:
                                            sibling_cleanup(local_path)
                                        except Exception as fs_err:
                                            LOG.warning(
                                                f"sibling_cleanup failed for '{ver.path}': {fs_err}"
                                            )

                                # remove from media server
                                if (
                                    media_server_fallback_enabled
                                    and main_service is not None
                                    and unmonitor_service_type is not None
                                ):
                                    if is_whole_movie:
                                        service_versions: list[MovieVersion] = [
                                            v
                                            for v in movie.versions
                                            if v.service == unmonitor_service_type
                                        ]
                                    else:
                                        # version only (only include candidate versions)
                                        service_versions = [
                                            v
                                            for v in movie.versions
                                            if v.service == unmonitor_service_type
                                            and v.id in unmonitor_target_version_ids
                                        ]
                                    deleted_item_ids: set[str] = set()
                                    for ver in service_versions:
                                        if ver.service_item_id in deleted_item_ids:
                                            continue
                                        if not is_whole_movie:
                                            # skip if a non candidate version still uses this item id
                                            # (e.g. multi version Plex entry sharing one ratingKey)
                                            if any(
                                                v.service_item_id == ver.service_item_id
                                                for v in movie.versions
                                                if v.service == unmonitor_service_type
                                                and v.id
                                                not in unmonitor_target_version_ids
                                            ):
                                                continue
                                        try:
                                            await main_service.delete_item(
                                                ver.service_item_id
                                            )
                                            deleted_item_ids.add(ver.service_item_id)
                                        except Exception as ms_err:
                                            LOG.warning(
                                                f"Media server delete_item failed for "
                                                f"'{movie.title}': {ms_err}"
                                            )

                                # only soft delete the movie record when all versions are removed
                                if is_whole_movie:
                                    movie.removed_at = datetime.now(UTC)
                                    movie.added_at = None
                                elif concrete_unmonitor_target_version_ids:
                                    target_versions = [
                                        v
                                        for v in movie.versions
                                        if v.id in concrete_unmonitor_target_version_ids
                                    ]
                                    deleted_size = sum(
                                        v.size or 0 for v in target_versions
                                    )
                                    if deleted_size and movie.size is not None:
                                        movie.size = max(0, movie.size - deleted_size)
                                    await db.execute(
                                        delete(MovieVersion).where(
                                            MovieVersion.id.in_(
                                                concrete_unmonitor_target_version_ids
                                            )
                                        )
                                    )
                                    await db.flush()
                                    await _soft_remove_movie_if_empty(db, movie.id)
                            # "unmonitor_only" leaves the movie/version records and
                            # files exactly as-is - only the Radarr entry (unmonitored
                            # above) changes.
                            event_versions = [
                                v
                                for v in movie.versions
                                if v.path
                                and (
                                    is_whole_movie
                                    or v.id in concrete_unmonitor_target_version_ids
                                )
                            ] or [None]
                            for version in event_versions:
                                unmonitor_events.append(
                                    {
                                        "action": "unmonitored_only"
                                        if keep_files
                                        else "unmonitored",
                                        "title": movie.title,
                                        "tmdb_id": movie.tmdb_id,
                                        "candidate_id": movie_info["candidate_id"],
                                        "path": version.path if version else None,
                                        "service_type": version.service
                                        if version
                                        else Service.RADARR,
                                        "movie_version_id": version.id
                                        if version
                                        else None,
                                    }
                                )

                        for cand_id in movie_info["all_candidate_ids"]:
                            result = await db.execute(
                                select(ReclaimCandidate).where(
                                    ReclaimCandidate.id == cand_id
                                )
                            )
                            reclaim_candidate = result.scalar_one_or_none()
                            if reclaim_candidate:
                                await db.delete(reclaim_candidate)

                        movie_tmdb_id = movie_info.get("tmdb_id")
                        if service_manager.seerr and movie and movie_tmdb_id:
                            try:
                                await _reset_seerr_request(
                                    movie_tmdb_id, MediaType.MOVIE
                                )
                            except Exception as e:
                                LOG.warning(
                                    f"Failed to reset Seerr request for {movie_info['title']}: {e}"
                                )

                        db.add(
                            ReclaimHistory(
                                approved_by=approved_by,
                                media_type=MediaType.MOVIE,
                                tmdb_id=movie_info.get("tmdb_id"),
                                name=movie_info.get("title"),
                                size=movie.size if movie else None,
                                action="unmonitored_only"
                                if keep_files
                                else "unmonitored",
                            )
                        )

                await db.commit()

            unique_unmonitored = len(seen_unmonitor_ids)
            deleted_count += unique_unmonitored
            LOG.info(f"Successfully unmonitored {unique_unmonitored} movies via Radarr")
            for event in unmonitor_events:
                await _dispatch_reclaim_event(
                    media_type=MediaType.MOVIE,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing movie unmonitor state: {e}", exc_info=True)
        await _best_effort_radarr_rescan(
            radarr_refresh_after_unmonitor,
            context="movie unmonitor cleanup",
        )

    # fallback to media server for whole-movie candidates not handled by any arr instance
    if whole_movie_candidates:
        movies_handled_ids = {
            m["movie_id"] for m in movies_to_delete + movies_to_unmonitor
        }
        unhandled = [
            c for c in whole_movie_candidates if c.movie_id not in movies_handled_ids
        ]
        # "unmonitor_only" never deletes files, including via the media server
        # fallback - if no Radarr instance could be found to unmonitor, there
        # is nothing safe to do, so leave the file untouched.
        unmonitor_only_unhandled = [
            c
            for c in unhandled
            if movie_arr_action.get(c.movie_id or 0) == "unmonitor_only"
        ]
        for cand in unmonitor_only_unhandled:
            title = movie_data.get(cand.movie_id or 0, {}).get("title", "unknown")
            await _mark_unexplained_delete_failures(
                [cand.id],
                (
                    "Unmonitor Only: no active Radarr instance is tracking this "
                    "movie, so there is nothing to unmonitor; the file was left "
                    "untouched by design"
                ),
            )
            LOG.info(
                f"Unmonitor Only - skipping '{title}' (not tracked by any Radarr "
                f"instance, file left untouched)"
            )
        media_fallback_candidates = [
            c for c in unhandled if c not in unmonitor_only_unhandled
        ]
        if media_fallback_candidates:
            if media_server_fallback_enabled:
                deleted_count += await _delete_movies_via_media_server(
                    media_fallback_candidates, [], approved_by
                )
            else:
                for cand in media_fallback_candidates:
                    title = movie_data.get(cand.movie_id or 0, {}).get(
                        "title", "unknown"
                    )
                    await _mark_unexplained_delete_failures(
                        [cand.id],
                        (
                            "Media server fallback disabled and movie was not handled "
                            "by any active Radarr instance"
                        ),
                    )
                    LOG.warning(
                        f"Media server fallback disabled - skipping deletion for "
                        f"'{title}' (not handled by any Radarr instance)"
                    )

    return deleted_count


async def _delete_series_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete series candidates. Returns count of deleted series."""
    deleted_count = 0

    # load fallback toggle + move settings from settings
    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )
        default_arr_delete_behavior = _coerce_arr_delete_fallback(
            settings_row.default_arr_delete_behavior if settings_row else None
        )
        add_arr_import_exclusions_on_delete = (
            settings_row.add_arr_import_exclusions_on_delete if settings_row else True
        )
        favorites_ignore_enabled = (
            bool(settings_row.favorites_ignore_enabled) if settings_row else False
        )
        favorites_protect_all_users = (
            bool(settings_row.favorites_protect_all_users) if settings_row else False
        )
        favorites_usernames = {
            _normalize_favorites_username(str(raw))
            for raw in (
                (settings_row.favorites_usernames or []) if settings_row else []
            )
            if str(raw).strip()
        }
    # get all series candidates from database
    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Series, ReclaimCandidate.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(_is_series_scope(ReclaimCandidate))
            .where(Series.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()

        if not candidates:
            LOG.debug("No series candidates to delete")
            return 0

        if favorites_ignore_enabled:
            (
                candidates,
                skipped_for_favorites,
            ) = await _filter_series_candidates_by_favorites(
                db,
                candidates,
                protect_all_users=favorites_protect_all_users,
                usernames=favorites_usernames,
            )
            if skipped_for_favorites:
                LOG.info(
                    f"Skipped {skipped_for_favorites} series candidate(s) due to favorites protection"
                )
            if not candidates:
                LOG.info("All series candidates skipped due to favorites protection")
                return 0

        LOG.info(f"Found {len(candidates)} series candidates to evaluate for deletion")

    # load rules for arr_action resolution
    series_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        series_rules_by_id: dict[int, ReclaimRule] = {}
        if series_rule_ids:
            series_rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(series_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    # determine per-series action ("delete" or "unmonitor")
    series_arr_action: dict[int, ArrDeleteAction] = {}
    for cand in candidates:
        if cand.series_id:
            action = _get_arr_action(
                cand, series_rules_by_id, default_arr_delete_behavior
            )
            series_arr_action[cand.series_id] = _merge_arr_action(
                series_arr_action.get(cand.series_id),
                action,
            )

    async with async_db() as db:
        # load series data + all arr refs (with stored arr_series_path for path routing)
        result = await db.execute(
            select(
                Series.id,
                Series.title,
                Series.tmdb_id,
                SeriesArrRef.service_config_id,
                SeriesArrRef.arr_series_id,
                SeriesArrRef.arr_series_path,
            )
            .join(ReclaimCandidate, Series.id == ReclaimCandidate.series_id)
            .outerjoin(SeriesArrRef, SeriesArrRef.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(Series.removed_at.is_(None))
        )
        rows = result.all()
        series_data: dict[int, dict[str, Any]] = {}
        for (
            series_id,
            title,
            tmdb_id,
            config_id,
            arr_series_id,
            arr_series_path,
        ) in rows:
            info = series_data.setdefault(
                series_id,
                {"title": title, "tmdb_id": tmdb_id, "refs": []},
            )
            if config_id is not None and arr_series_id is not None:
                info["refs"].append((config_id, arr_series_id, arr_series_path))

    sonarr_clients = service_manager.sonarr_clients()
    if not sonarr_clients and service_manager.sonarr:
        sonarr_clients = {0: service_manager.sonarr}

    # Route each candidate to ALL matching Sonarr instances.
    # Series-level candidates don't have a per-version path, so we route to every
    # Sonarr instance that tracks this series (arr_series_path used when available
    # for disambiguation in multi-Sonarr setups; falls back to config_id membership).
    # series_id -> set of (config_id, arr_series_id)
    series_arr_routing: dict[int, set[tuple[int, int]]] = {}

    if sonarr_clients:
        for candidate in candidates:
            if candidate.series_id is None:
                continue
            series_info = series_data.get(candidate.series_id)
            if not series_info:
                continue
            allowed_config_ids = _candidate_arr_config_ids(
                candidate, series_rules_by_id, "sonarr"
            )
            for config_id, arr_series_id, _ in series_info["refs"]:
                if config_id in sonarr_clients and (
                    allowed_config_ids is None or config_id in allowed_config_ids
                ):
                    series_arr_routing.setdefault(candidate.series_id, set()).add(
                        (config_id, arr_series_id)
                    )

    series_to_delete_by_config: dict[int, list[dict[str, Any]]] = {}
    series_to_unmonitor_by_config: dict[int, list[dict[str, Any]]] = {}
    for series_id, config_arr_set in series_arr_routing.items():
        series_info = series_data.get(series_id, {})
        candidate_id = next(
            (c.id for c in candidates if c.series_id == series_id), None
        )
        target = (
            series_to_unmonitor_by_config
            if series_arr_action.get(series_id) in UNMONITOR_LIKE_ARR_ACTIONS
            else series_to_delete_by_config
        )
        for config_id, arr_series_id in config_arr_set:
            target.setdefault(config_id, []).append(
                {
                    "candidate_id": candidate_id,
                    "series_id": series_id,
                    "sonarr_id": arr_series_id,
                    "title": series_info.get("title", ""),
                    "method": "sonarr",
                    "tmdb_id": series_info.get("tmdb_id"),
                }
            )

    series_to_delete: list[dict[str, Any]] = []
    sonarr_refresh_after_delete: dict[int, set[int]] = {}
    for config_id, batch in series_to_delete_by_config.items():
        client = sonarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Deleting {len(batch)} series via Sonarr config {config_id}")
        try:
            for series_info in batch:
                await client.delete_series(
                    series_info["sonarr_id"],
                    delete_files=True,
                    add_import_exclusion=add_arr_import_exclusions_on_delete,
                )
            series_to_delete.extend(batch)
            sonarr_refresh_after_delete.setdefault(config_id, set()).update(
                {int(s["sonarr_id"]) for s in batch}
            )
        except Exception as e:
            await _mark_candidate_delete_failures(
                [series_info.get("candidate_id") for series_info in batch],
                f"Sonarr delete failed for config {config_id}: {e}",
            )
            LOG.error(
                f"Error deleting series via Sonarr config {config_id}: {e}",
                exc_info=True,
            )

    if series_to_delete:
        series_events: list[dict[str, Any]] = []
        try:
            async with async_db() as db:
                # deduplicate: a series may appear in multiple Sonarr instances
                seen_series_ids: set[int] = set()
                for series_info in series_to_delete:
                    series_id = series_info["series_id"]
                    already_processed = series_id in seen_series_ids
                    seen_series_ids.add(series_id)

                    if already_processed:
                        continue

                    result = await db.execute(
                        select(Series)
                        .where(Series.id == series_id)
                        .options(selectinload(Series.service_refs))
                    )
                    series = result.scalar_one_or_none()
                    if series:
                        series.removed_at = datetime.now(UTC)
                        series.added_at = None
                        event_refs = [r for r in series.service_refs if r.path] or [
                            None
                        ]
                        for ref in event_refs:
                            series_events.append(
                                {
                                    "title": series.title,
                                    "tmdb_id": series.tmdb_id,
                                    "candidate_id": series_info["candidate_id"],
                                    "path": ref.path if ref else None,
                                    "service_type": ref.service
                                    if ref
                                    else Service.SONARR,
                                }
                            )

                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == series_info["candidate_id"]
                        )
                    )
                    reclaim_candidate = result.scalar_one_or_none()
                    if reclaim_candidate:
                        await db.delete(reclaim_candidate)

                    series_tmdb_id = series_info.get("tmdb_id")
                    if service_manager.seerr and series and series_tmdb_id:
                        try:
                            await _reset_seerr_request(series_tmdb_id, MediaType.SERIES)
                        except Exception as e:
                            LOG.warning(
                                f"Failed to reset Seerr request for {series_info['title']}: {e}"
                            )

                    db.add(
                        ReclaimHistory(
                            approved_by=approved_by,
                            media_type=MediaType.SERIES,
                            tmdb_id=series_info.get("tmdb_id"),
                            name=series_info.get("title"),
                            size=series.size if series else None,
                        )
                    )

                await db.commit()

            unique_deleted = len({s["series_id"] for s in series_to_delete})
            deleted_count += unique_deleted
            LOG.info(f"Successfully deleted {unique_deleted} series via Sonarr")
            for event in series_events:
                await _dispatch_reclaim_event(
                    action="deleted",
                    media_type=MediaType.SERIES,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing series deletion state: {e}", exc_info=True)
        await _best_effort_sonarr_refresh(
            sonarr_refresh_after_delete,
            context="series delete cleanup",
        )

    # process unmonitor batches: mark unmonitored in Sonarr, then delete files + media server
    series_to_unmonitor: list[dict[str, Any]] = []
    sonarr_refresh_after_unmonitor: dict[int, set[int]] = {}
    for config_id, batch in series_to_unmonitor_by_config.items():
        client = sonarr_clients.get(config_id)
        if not client:
            continue
        LOG.info(f"Unmonitoring {len(batch)} series via Sonarr config {config_id}")
        sonarr_ids = [s["sonarr_id"] for s in batch]
        try:
            await client.unmonitor_series(sonarr_ids)
            series_to_unmonitor.extend(batch)
            sonarr_refresh_after_unmonitor.setdefault(config_id, set()).update(
                sonarr_ids
            )
        except Exception as e:
            await _mark_candidate_delete_failures(
                [series_info.get("candidate_id") for series_info in batch],
                f"Sonarr unmonitor failed for config {config_id}: {e}",
            )
            LOG.error(
                f"Error unmonitoring series via Sonarr config {config_id}: {e}",
                exc_info=True,
            )

    if series_to_unmonitor:
        unmonitor_series_events: list[dict[str, Any]] = []
        path_mappings_series = await _load_path_mappings()
        main_service_s = service_manager.main_media_server
        unmonitor_svc_type: Service | None = service_manager.main_media_server_type
        try:
            async with async_db() as db:
                seen_series_unmonitor: set[int] = set()
                for series_info in series_to_unmonitor:
                    series_id = series_info["series_id"]
                    if series_id in seen_series_unmonitor:
                        continue
                    seen_series_unmonitor.add(series_id)
                    keep_files = series_arr_action.get(series_id) == "unmonitor_only"

                    result = await db.execute(
                        select(Series)
                        .where(Series.id == series_id)
                        .options(selectinload(Series.service_refs))
                    )
                    series = result.scalar_one_or_none()

                    if series:
                        if not keep_files:
                            # delete series folder from disk using arr_series_path
                            s_refs = series_data.get(series_id, {}).get("refs", [])
                            for _, _, arr_series_path in s_refs:
                                if not arr_series_path:
                                    continue
                                initial_path = Path(arr_series_path)
                                local_path: Path | None = initial_path
                                if not initial_path.exists():
                                    local_path = resolve_path(
                                        arr_series_path,
                                        path_mappings_series,
                                        service_type="sonarr",
                                    )
                                if local_path and local_path.exists():
                                    try:
                                        shutil.rmtree(str(local_path))
                                        LOG.info(f"Removed series folder: {local_path}")
                                    except Exception as fs_err:
                                        LOG.warning(
                                            f"shutil.rmtree failed for series '{series.title}' "
                                            f"at '{local_path}': {fs_err}"
                                        )
                                    break  # only delete the matched folder once

                            # remove from media server
                            if (
                                media_server_fallback_enabled
                                and main_service_s is not None
                                and unmonitor_svc_type is not None
                            ):
                                ref = next(
                                    (
                                        r
                                        for r in series.service_refs
                                        if r.service == unmonitor_svc_type
                                    ),
                                    None,
                                )
                                if ref:
                                    try:
                                        await main_service_s.delete_item(ref.service_id)
                                    except Exception as ms_err:
                                        LOG.warning(
                                            f"Media server delete_item failed for series "
                                            f"'{series.title}': {ms_err}"
                                        )

                            series.removed_at = datetime.now(UTC)
                            series.added_at = None
                        # "unmonitor_only" leaves the series record and its files
                        # exactly as-is - only the Sonarr entry (unmonitored above)
                        # changes.
                        for ref in series.service_refs:
                            if ref.path:
                                unmonitor_series_events.append(
                                    {
                                        "action": "unmonitored_only"
                                        if keep_files
                                        else "unmonitored",
                                        "title": series.title,
                                        "tmdb_id": series.tmdb_id,
                                        "candidate_id": series_info["candidate_id"],
                                        "path": ref.path,
                                        "service_type": ref.service,
                                    }
                                )

                    result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == series_info["candidate_id"]
                        )
                    )
                    reclaim_candidate = result.scalar_one_or_none()
                    if reclaim_candidate:
                        await db.delete(reclaim_candidate)

                    series_tmdb_id = series_info.get("tmdb_id")
                    if service_manager.seerr and series and series_tmdb_id:
                        try:
                            await _reset_seerr_request(series_tmdb_id, MediaType.SERIES)
                        except Exception as e:
                            LOG.warning(
                                f"Failed to reset Seerr request for {series_info['title']}: {e}"
                            )

                    db.add(
                        ReclaimHistory(
                            approved_by=approved_by,
                            media_type=MediaType.SERIES,
                            tmdb_id=series_info.get("tmdb_id"),
                            name=series_info.get("title"),
                            size=series.size if series else None,
                            action="unmonitored_only" if keep_files else "unmonitored",
                        )
                    )

                await db.commit()

            unique_unmonitored_series = len(seen_series_unmonitor)
            deleted_count += unique_unmonitored_series
            LOG.info(
                f"Successfully unmonitored {unique_unmonitored_series} series via Sonarr"
            )
            for event in unmonitor_series_events:
                await _dispatch_reclaim_event(
                    media_type=MediaType.SERIES,
                    **event,
                )
        except Exception as e:
            LOG.error(f"Error finalizing series unmonitor state: {e}", exc_info=True)
        await _best_effort_sonarr_refresh(
            sonarr_refresh_after_unmonitor,
            context="series unmonitor cleanup",
        )

    # fallback to media server deletion for candidates not handled by any Sonarr instance
    series_handled_ids = {
        s["series_id"] for s in series_to_delete + series_to_unmonitor
    }
    unhandled = [c for c in candidates if c.series_id not in series_handled_ids]
    # "unmonitor_only" never deletes files, including via the media server
    # fallback - if no Sonarr instance could be found to unmonitor, there is
    # nothing safe to do, so leave the file untouched.
    unmonitor_only_unhandled = [
        c
        for c in unhandled
        if series_arr_action.get(c.series_id or 0) == "unmonitor_only"
    ]
    for cand in unmonitor_only_unhandled:
        title = series_data.get(cand.series_id or 0, {}).get("title", "unknown")
        await _mark_unexplained_delete_failures(
            [cand.id],
            (
                "Unmonitor Only: no active Sonarr instance is tracking this "
                "series, so there is nothing to unmonitor; the files were left "
                "untouched by design"
            ),
        )
        LOG.info(
            f"Unmonitor Only - skipping '{title}' (not tracked by any Sonarr "
            f"instance, files left untouched)"
        )
    media_fallback_candidates = [
        c for c in unhandled if c not in unmonitor_only_unhandled
    ]
    if media_fallback_candidates:
        if media_server_fallback_enabled:
            deleted_count += await _delete_series_via_media_server(
                media_fallback_candidates, [], approved_by
            )
        else:
            for cand in media_fallback_candidates:
                title = series_data.get(cand.series_id or 0, {}).get("title", "unknown")
                await _mark_unexplained_delete_failures(
                    [cand.id],
                    (
                        "Media server fallback disabled and series was not handled "
                        "by any active Sonarr instance"
                    ),
                )
                LOG.warning(
                    f"Media server fallback disabled - skipping deletion for "
                    f"'{title}' (not handled by any Sonarr instance)"
                )

    return deleted_count


async def _delete_season_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete season-level candidates.  Tries Sonarr first; falls back to media server.

    Returns count of seasons successfully deleted.
    """
    deleted_count = 0

    # load fallback toggle from settings
    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )
        default_arr_delete_behavior = _coerce_arr_delete_fallback(
            settings_row.default_arr_delete_behavior if settings_row else None
        )
        add_arr_import_exclusions_on_delete = (
            settings_row.add_arr_import_exclusions_on_delete if settings_row else True
        )
        favorites_ignore_enabled = (
            bool(settings_row.favorites_ignore_enabled) if settings_row else False
        )
        favorites_protect_all_users = (
            bool(settings_row.favorites_protect_all_users) if settings_row else False
        )
        favorites_usernames = {
            _normalize_favorites_username(str(raw))
            for raw in (
                (settings_row.favorites_usernames or []) if settings_row else []
            )
            if str(raw).strip()
        }
        path_mappings: list[dict[str, Any]] = (
            settings_row.path_mappings or [] if settings_row else []
        )

    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Season, ReclaimCandidate.season_id == Season.id)
            .join(Series, ReclaimCandidate.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(_is_season_scope(ReclaimCandidate))
            .where(Series.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()
        if favorites_ignore_enabled and candidates:
            (
                candidates,
                skipped_for_favorites,
            ) = await _filter_series_candidates_by_favorites(
                db,
                candidates,
                protect_all_users=favorites_protect_all_users,
                usernames=favorites_usernames,
            )
            if skipped_for_favorites:
                LOG.info(
                    f"Skipped {skipped_for_favorites} season candidate(s) due to favorites protection"
                )

    if not candidates:
        LOG.debug("No season candidates to delete")
        return 0

    LOG.info(f"Found {len(candidates)} season candidates to evaluate for deletion")
    sonarr_refresh_after_season_ops: dict[int, set[int]] = {}

    # load rules for arr_action resolution
    season_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        season_rules_by_id: dict[int, ReclaimRule] = {}
        if season_rule_ids:
            season_rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(season_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    # bulk-load seasons and series needed for deletion
    async with async_db() as db:
        season_ids = [c.season_id for c in candidates if c.season_id]
        series_ids = list({c.series_id for c in candidates if c.series_id})

        seasons_result = await db.execute(
            select(Season).where(Season.id.in_(season_ids))
        )
        seasons: dict[int, Season] = {s.id: s for s in seasons_result.scalars().all()}

        series_result = await db.execute(
            select(Series)
            .where(Series.id.in_(series_ids))
            .options(selectinload(Series.service_refs))
        )
        series_map: dict[int, Series] = {s.id: s for s in series_result.scalars().all()}

    for candidate in candidates:
        if not candidate.season_id or not candidate.series_id:
            continue
        season = seasons.get(candidate.season_id)
        series_obj = series_map.get(candidate.series_id)
        if not season or not series_obj:
            continue

        season_number = season.season_number
        deleted_via_sonarr = False

        # determine arr_action for this candidate
        cand_arr_action = _get_arr_action(
            candidate, season_rules_by_id, default_arr_delete_behavior
        )

        # try Sonarr first, preferring refs whose Sonarr path matches the media path
        sonarr_ref_id: int | None = None
        sonarr_ref_config_id: int | None = None
        arr_series_path: str | None = None
        sonarr_client = None
        async with async_db() as db:
            refs = (
                (
                    await db.execute(
                        select(SeriesArrRef)
                        .where(SeriesArrRef.series_id == series_obj.id)
                        .order_by(SeriesArrRef.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        ordered_refs = _order_series_arr_refs(
            refs,
            [season.path, *(season.episode_paths or [])],
            path_mappings,
            media_service_type=_main_media_server_type(),
        )
        allowed_config_ids = _candidate_arr_config_ids(
            candidate, season_rules_by_id, "sonarr"
        )
        if allowed_config_ids is not None:
            ordered_refs = [
                ref
                for ref in ordered_refs
                if ref.service_config_id in allowed_config_ids
            ]
        last_sonarr_error: str | None = None

        for ref in ordered_refs:
            ref_client = service_manager.get_sonarr(ref.service_config_id)
            if ref_client is None:
                ref_client = service_manager.sonarr
            if ref_client is None or ref.arr_series_id is None:
                continue

            try:
                sonarr_episodes = await ref_client.get_episodes(ref.arr_series_id)
            except Exception as e:
                last_sonarr_error = str(e)
                LOG.warning(
                    f"Failed to fetch episodes from Sonarr for "
                    f"'{series_obj.title}' S{season_number:02d} "
                    f"(config_id={ref.service_config_id}, sonarr_id={ref.arr_series_id}): {e}"
                )
                continue

            season_file_ids = [
                ep.get("episodeFileId")
                for ep in sonarr_episodes
                if ep.get("seasonNumber") == season_number
                and ep.get("episodeFileId") is not None
            ]
            if not season_file_ids:
                last_sonarr_error = (
                    f"No Sonarr episode files found for S{season_number:02d}"
                )
                LOG.warning(
                    f"No Sonarr episode files found for '{series_obj.title}' "
                    f"S{season_number:02d} "
                    f"(config_id={ref.service_config_id}, sonarr_id={ref.arr_series_id})"
                )
                continue

            try:
                await ref_client.update_season_monitoring(
                    ref.arr_series_id, season_number, monitored=False
                )
                LOG.debug(
                    f"Unmonitored '{series_obj.title}' S{season_number:02d} in Sonarr"
                )
            except Exception as e:
                last_sonarr_error = str(e)
                LOG.warning(
                    f"Failed to unmonitor '{series_obj.title}' S{season_number:02d} "
                    f"in Sonarr "
                    f"(config_id={ref.service_config_id}, sonarr_id={ref.arr_series_id}): {e}"
                )
                continue

            sonarr_ref_id = ref.arr_series_id
            sonarr_ref_config_id = ref.service_config_id
            arr_series_path = ref.arr_series_path
            sonarr_client = ref_client

            if cand_arr_action == "unmonitor_only":
                # Sonarr side is done - the season is unmonitored above. Files
                # are left untouched by design, so there's nothing else to do
                # for this ref.
                deleted_via_sonarr = True
                break

            if cand_arr_action == "unmonitor":
                season_folder: Path | None = None
                if arr_series_path:
                    initial_series_path = Path(arr_series_path)
                    local_series_path: Path | None = initial_series_path
                    if not initial_series_path.is_dir():
                        local_series_path = resolve_path(
                            arr_series_path,
                            path_mappings,
                            service_type=Service.SONARR.value,
                            service_config_id=sonarr_ref_config_id,
                        )
                    if local_series_path and local_series_path.is_dir():
                        season_folder = find_season_folder(
                            local_series_path, season_number
                        )
                if season_folder and season_folder.is_dir():
                    try:
                        shutil.rmtree(str(season_folder))
                        LOG.info(
                            f"Removed '{series_obj.title}' S{season_number:02d} "
                            f"folder: {season_folder}"
                        )
                        deleted_via_sonarr = True
                    except Exception as fs_err:
                        last_sonarr_error = str(fs_err)
                        LOG.warning(
                            f"shutil.rmtree failed for '{series_obj.title}' "
                            f"S{season_number:02d} at '{season_folder}': {fs_err} "
                            f"- will attempt media server fallback"
                        )
                else:
                    last_sonarr_error = "Could not locate season folder"
                    LOG.warning(
                        f"Could not locate season folder for '{series_obj.title}' "
                        f"S{season_number:02d} (arr_series_path={arr_series_path!r})"
                    )
            else:
                try:
                    await ref_client.delete_season_files(
                        ref.arr_series_id, season_number
                    )
                    deleted_via_sonarr = True
                    LOG.info(
                        f"Deleted '{series_obj.title}' S{season_number:02d} "
                        f"via Sonarr (sonarr_id={ref.arr_series_id})"
                    )
                except Exception as e:
                    last_sonarr_error = str(e)
                    LOG.warning(
                        f"Sonarr file deletion failed for '{series_obj.title}' "
                        f"S{season_number:02d}: {e} - will attempt media server fallback"
                    )

            if deleted_via_sonarr:
                break

        if (
            deleted_via_sonarr
            and media_server_fallback_enabled
            and cand_arr_action != "unmonitor_only"
        ):
            media_svc = service_manager.main_media_server
            media_svc_type = _main_media_server_type()
            season_service_id = _season_media_server_id(season, media_svc_type)
            if media_svc is not None and season_service_id:
                try:
                    await media_svc.delete_item(season_service_id)
                except Exception as ms_err:
                    LOG.warning(
                        f"Media server delete_item failed for "
                        f"'{series_obj.title}' S{season_number:02d}: {ms_err}"
                    )

        # if no files remain across all seasons (delete path only), remove series
        if (
            deleted_via_sonarr
            and cand_arr_action not in UNMONITOR_LIKE_ARR_ACTIONS
            and sonarr_client is not None
            and sonarr_ref_id is not None
        ):
            try:
                fresh_series = await sonarr_client.get_series(sonarr_ref_id)
                all_empty = all(
                    (s.statistics or {}).get("episodeFileCount", 0) == 0
                    for s in fresh_series.seasons
                )
                if all_empty:
                    await sonarr_client.delete_series(
                        sonarr_ref_id,
                        delete_files=False,
                        add_import_exclusion=add_arr_import_exclusions_on_delete,
                    )
                    LOG.info(
                        f"Removed '{series_obj.title}' from Sonarr entirely "
                        f"(no files remaining, sonarr_id={sonarr_ref_id}, "
                        f"resolved_action={cand_arr_action}, "
                        f"matched_rule_ids={candidate.matched_rule_ids or []}, "
                        f"configured_fallback={default_arr_delete_behavior})"
                    )
            except Exception as e:
                LOG.warning(
                    f"Could not check/remove empty series '{series_obj.title}' "
                    f"from Sonarr: {e}"
                )

        # fall back to media server if Sonarr failed or unavailable
        if not deleted_via_sonarr:
            if cand_arr_action == "unmonitor_only":
                # never delete files for "unmonitor_only" - no Sonarr instance
                # could unmonitor this season, so there's nothing safe to do.
                await _mark_candidate_delete_failure(
                    candidate.id,
                    last_sonarr_error
                    or (
                        "Unmonitor Only: no active Sonarr instance is tracking "
                        "this season, so there is nothing to unmonitor; the "
                        "files were left untouched by design"
                    ),
                )
                LOG.info(
                    f"Unmonitor Only - skipping '{series_obj.title}' "
                    f"S{season_number:02d} (not tracked by any Sonarr instance, "
                    f"files left untouched)"
                )
                continue
            if not media_server_fallback_enabled:
                await _mark_candidate_delete_failure(
                    candidate.id,
                    last_sonarr_error
                    or "Media server fallback disabled for season deletion",
                )
                LOG.warning(
                    f"Media server fallback disabled - skipping season deletion for "
                    f"'{series_obj.title}' S{season_number:02d}. Enable 'Allow Media "
                    f"Server Fallback Deletion' in General Settings."
                )
                continue
            media_service = service_manager.main_media_server
            season_service_id = _season_media_server_id(
                season, _main_media_server_type()
            )
            if media_service and season_service_id:
                try:
                    await media_service.delete_item(season_service_id)
                    LOG.info(
                        f"Deleted '{series_obj.title}' S{season_number:02d} via media server"
                    )
                except Exception as e:
                    await _mark_candidate_delete_failure(
                        candidate.id,
                        f"Media server season deletion failed: {e}",
                    )
                    LOG.error(
                        f"Media server deletion failed for '{series_obj.title}' "
                        f"S{season_number:02d}: {e} - skipping"
                    )
                    continue
            else:
                await _mark_candidate_delete_failure(
                    candidate.id,
                    "No deletion method available for season",
                )
                LOG.warning(
                    f"No deletion method available for '{series_obj.title}' "
                    f"S{season_number:02d} - skipping"
                )
                continue

        # remove candidate and update series size in DB
        async with async_db() as db:
            if cand_arr_action == "unmonitor_only":
                # "unmonitor_only" leaves the season/series records and files
                # exactly as-is - only the Sonarr entry (unmonitored above)
                # changes. Just clear the candidate(s) queued for this season.
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.season_id == candidate.season_id
                    )
                )
            else:
                result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                # reduce series stored size by the season's size
                if season.size:
                    series_result = await db.execute(
                        select(Series).where(Series.id == candidate.series_id)
                    )
                    series_db = series_result.scalar_one_or_none()
                    if series_db and series_db.size:
                        series_db.size = max(0, series_db.size - season.size)

                # delete the season row so it doesn't show stale data
                result = await db.execute(
                    select(Season).where(Season.id == candidate.season_id)
                )
                season_db = result.scalar_one_or_none()
                if season_db:
                    await db.execute(
                        delete(ReclaimCandidate).where(
                            ReclaimCandidate.season_id == season_db.id
                        )
                    )
                    await db.execute(
                        delete(ProtectionRequest).where(
                            ProtectionRequest.season_id == season_db.id,
                            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
                        )
                    )
                    await db.execute(
                        update(ProtectionRequest)
                        .where(
                            ProtectionRequest.season_id == season_db.id,
                            ProtectionRequest.status != ProtectionRequestStatus.PENDING,
                        )
                        .values(season_id=None, episode_id=None)
                    )
                    await db.execute(
                        delete(DeleteRequest).where(
                            DeleteRequest.season_id == season_db.id,
                            DeleteRequest.status == ProtectionRequestStatus.PENDING,
                        )
                    )
                    await db.execute(
                        update(DeleteRequest)
                        .where(
                            DeleteRequest.season_id == season_db.id,
                            DeleteRequest.status != ProtectionRequestStatus.PENDING,
                        )
                        .values(season_id=None, episode_id=None)
                    )
                    await db.execute(
                        delete(ProtectedMedia).where(
                            ProtectedMedia.season_id == season_db.id
                        )
                    )
                    await db.delete(season_db)
                    await db.flush()
                    await _soft_remove_series_if_empty(db, candidate.series_id)

            db.add(
                ReclaimHistory(
                    approved_by=approved_by,
                    media_type=MediaType.SERIES,
                    tmdb_id=series_obj.tmdb_id,
                    name=f"{series_obj.title} S{season_number:02d}",
                    size=season.size,
                    attributes=_build_reclaim_history_attributes(season=season),
                    action="unmonitored_only"
                    if cand_arr_action == "unmonitor_only"
                    else "unmonitored"
                    if cand_arr_action == "unmonitor"
                    else "deleted",
                )
            )
            await db.commit()

        deleted_count += 1
        event_service_type: Service | None = service_manager.main_media_server_type
        await _dispatch_reclaim_event(
            action="unmonitored_only"
            if cand_arr_action == "unmonitor_only"
            else "unmonitored"
            if cand_arr_action == "unmonitor"
            else "deleted",
            media_type=MediaType.SERIES,
            title=series_obj.title,
            tmdb_id=series_obj.tmdb_id,
            candidate_id=candidate.id,
            path=season.path,
            service_type=event_service_type,
            season_id=season.id,
            season_number=season_number,
        )
        if sonarr_ref_config_id is not None and sonarr_ref_id is not None:
            sonarr_refresh_after_season_ops.setdefault(sonarr_ref_config_id, set()).add(
                sonarr_ref_id
            )

    await _best_effort_sonarr_refresh(
        sonarr_refresh_after_season_ops,
        context="season cleanup",
    )
    return deleted_count


async def _delete_episode_candidates(
    restrict_to_ids: frozenset[int] | None = None,
    approved_by: str = "system",
) -> int:
    """Delete episode level candidates via Sonarr (delete_episode_file / unmonitor_episode).

    Returns count of episodes successfully processed.
    """
    deleted_count = 0
    default_arr_delete_behavior: ArrDeleteFallback = "unmonitor"

    async with async_db() as db:
        settings_row = (await db.execute(select(GeneralSettings))).scalars().first()
        media_server_fallback_enabled = (
            settings_row.media_server_fallback_enabled if settings_row else True
        )
        default_arr_delete_behavior = _coerce_arr_delete_fallback(
            settings_row.default_arr_delete_behavior if settings_row else None
        )
        add_arr_import_exclusions_on_delete = (
            settings_row.add_arr_import_exclusions_on_delete if settings_row else True
        )
        favorites_ignore_enabled = (
            bool(settings_row.favorites_ignore_enabled) if settings_row else False
        )
        favorites_protect_all_users = (
            bool(settings_row.favorites_protect_all_users) if settings_row else False
        )
        favorites_usernames = {
            _normalize_favorites_username(str(raw))
            for raw in (
                (settings_row.favorites_usernames or []) if settings_row else []
            )
            if str(raw).strip()
        }
        path_mappings: list[dict[str, Any]] = (
            settings_row.path_mappings or [] if settings_row else []
        )

    async with async_db() as db:
        query = (
            select(ReclaimCandidate)
            .join(Episode, ReclaimCandidate.episode_id == Episode.id)
            .join(Season, ReclaimCandidate.season_id == Season.id)
            .join(Series, ReclaimCandidate.series_id == Series.id)
            .where(ReclaimCandidate.media_type == MediaType.SERIES)
            .where(_is_episode_scope(ReclaimCandidate))
            .where(Series.removed_at.is_(None))
        )
        if restrict_to_ids:
            query = query.where(ReclaimCandidate.id.in_(restrict_to_ids))
        candidates = (await db.execute(query)).scalars().all()
        if favorites_ignore_enabled and candidates:
            (
                candidates,
                skipped_for_favorites,
            ) = await _filter_series_candidates_by_favorites(
                db,
                candidates,
                protect_all_users=favorites_protect_all_users,
                usernames=favorites_usernames,
            )
            if skipped_for_favorites:
                LOG.info(
                    f"Skipped {skipped_for_favorites} episode candidate(s) due to favorites protection"
                )

    if not candidates:
        LOG.debug("No episode candidates to delete")
        return 0

    LOG.info(f"Found {len(candidates)} episode candidates to evaluate for deletion")
    sonarr_refresh_after_episode_ops: dict[int, set[int]] = {}

    episode_rule_ids = {rid for c in candidates for rid in (c.matched_rule_ids or [])}
    async with async_db() as db:
        episode_rules_by_id: dict[int, ReclaimRule] = {}
        if episode_rule_ids:
            episode_rules_by_id = {
                r.id: r
                for r in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(episode_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    async with async_db() as db:
        episode_ids = [c.episode_id for c in candidates if c.episode_id]
        season_ids_needed = list({c.season_id for c in candidates if c.season_id})
        series_ids_needed = list({c.series_id for c in candidates if c.series_id})

        eps_result = await db.execute(
            select(Episode).where(Episode.id.in_(episode_ids))
        )
        episodes_map: dict[int, Episode] = {e.id: e for e in eps_result.scalars().all()}

        seasons_result = await db.execute(
            select(Season).where(Season.id.in_(season_ids_needed))
        )
        seasons_map: dict[int, Season] = {
            s.id: s for s in seasons_result.scalars().all()
        }

        series_result = await db.execute(
            select(Series)
            .where(Series.id.in_(series_ids_needed))
            .options(selectinload(Series.service_refs))
        )
        series_map: dict[int, Series] = {s.id: s for s in series_result.scalars().all()}

    for candidate in candidates:
        if (
            not candidate.episode_id
            or not candidate.season_id
            or not candidate.series_id
        ):
            continue
        episode = episodes_map.get(candidate.episode_id)
        season = seasons_map.get(candidate.season_id)
        series_obj = series_map.get(candidate.series_id)
        if not episode or not season or not series_obj:
            continue

        cand_arr_action = _get_arr_action(
            candidate, episode_rules_by_id, default_arr_delete_behavior
        )
        ep_label = f"S{season.season_number:02d}E{episode.episode_number:02d}"

        sonarr_ref_id: int | None = None
        sonarr_ref_config_id: int | None = None
        sonarr_client = None
        deleted_via_sonarr = False
        last_sonarr_error: str | None = None
        async with async_db() as db:
            refs = (
                (
                    await db.execute(
                        select(SeriesArrRef)
                        .where(SeriesArrRef.series_id == series_obj.id)
                        .order_by(SeriesArrRef.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        ordered_refs = _order_series_arr_refs(
            refs,
            [episode.path, season.path, *(season.episode_paths or [])],
            path_mappings,
            media_service_type=_main_media_server_type(),
        )
        allowed_config_ids = _candidate_arr_config_ids(
            candidate, episode_rules_by_id, "sonarr"
        )
        if allowed_config_ids is not None:
            ordered_refs = [
                ref
                for ref in ordered_refs
                if ref.service_config_id in allowed_config_ids
            ]

        for ref in ordered_refs:
            ref_client = service_manager.get_sonarr(ref.service_config_id)
            if ref_client is None:
                ref_client = service_manager.sonarr
            if ref_client is None or ref.arr_series_id is None:
                continue
            try:
                sonarr_episodes = await ref_client.get_episodes(ref.arr_series_id)
            except Exception as e:
                last_sonarr_error = str(e)
                LOG.warning(
                    f"Failed to fetch episodes from Sonarr for "
                    f"'{series_obj.title}' {ep_label} "
                    f"(config_id={ref.service_config_id}, sonarr_id={ref.arr_series_id}): {e}"
                )
                continue

            matching = [
                ep
                for ep in sonarr_episodes
                if ep.get("seasonNumber") == season.season_number
                and ep.get("episodeNumber") == episode.episode_number
            ]
            if not matching:
                last_sonarr_error = f"No Sonarr episode found for {ep_label}"
                LOG.warning(
                    f"No Sonarr episode found for '{series_obj.title}' {ep_label} "
                    f"(config_id={ref.service_config_id}, sonarr_id={ref.arr_series_id})"
                )
                continue

            sonarr_ep = matching[0]
            sonarr_ep_id = _coerce_int(sonarr_ep.get("id"))
            sonarr_ep_file_id = _coerce_int(sonarr_ep.get("episodeFileId"))

            if not sonarr_ep_file_id:
                last_sonarr_error = f"No episode file in Sonarr for {ep_label}"
                LOG.debug(
                    f"No episode file in Sonarr for '{series_obj.title}' {ep_label} "
                    f"(config_id={ref.service_config_id}, sonarr_id={ref.arr_series_id})"
                )
                continue

            try:
                if cand_arr_action in UNMONITOR_LIKE_ARR_ACTIONS and sonarr_ep_id:
                    await ref_client.unmonitor_episode(sonarr_ep_id)
                    LOG.debug(f"Unmonitored '{series_obj.title}' {ep_label} in Sonarr")
                if cand_arr_action != "unmonitor_only":
                    await ref_client.delete_episode_file(sonarr_ep_file_id)
                deleted_via_sonarr = True
                sonarr_ref_id = ref.arr_series_id
                sonarr_ref_config_id = ref.service_config_id
                sonarr_client = ref_client
                if cand_arr_action == "unmonitor_only":
                    LOG.info(
                        f"Unmonitored '{series_obj.title}' {ep_label} via Sonarr "
                        f"(file left untouched)"
                    )
                else:
                    LOG.info(
                        f"Deleted '{series_obj.title}' {ep_label} via Sonarr"
                        f" (file_id={sonarr_ep_file_id})"
                    )
                break
            except Exception as e:
                last_sonarr_error = str(e)
                LOG.warning(
                    f"Sonarr episode file deletion failed for "
                    f"'{series_obj.title}' {ep_label}: {e}"
                )

        if deleted_via_sonarr and cand_arr_action != "unmonitor_only":
            # remove from media server to keep library in sync
            media_svc = service_manager.main_media_server
            ep_svc_id = _episode_media_server_id(episode, _main_media_server_type())
            if media_svc is not None and ep_svc_id:
                try:
                    await media_svc.delete_item(ep_svc_id)
                except Exception as ms_err:
                    LOG.warning(
                        f"Media server delete_item failed for "
                        f"'{series_obj.title}' {ep_label}: {ms_err}"
                    )
            if (
                cand_arr_action == "remove_if_empty"
                and sonarr_client is not None
                and sonarr_ref_id is not None
            ):
                try:
                    fresh_series = await sonarr_client.get_series(sonarr_ref_id)
                    all_empty = all(
                        (s.statistics or {}).get("episodeFileCount", 0) == 0
                        for s in fresh_series.seasons
                    )
                    if all_empty:
                        await sonarr_client.delete_series(
                            sonarr_ref_id,
                            delete_files=False,
                            add_import_exclusion=add_arr_import_exclusions_on_delete,
                        )
                        LOG.info(
                            f"Removed '{series_obj.title}' from Sonarr entirely "
                            f"(no files remaining, sonarr_id={sonarr_ref_id}, "
                            f"resolved_action={cand_arr_action}, "
                            f"matched_rule_ids={candidate.matched_rule_ids or []}, "
                            f"configured_fallback={default_arr_delete_behavior})"
                        )
                except Exception as e:
                    LOG.warning(
                        f"Could not check/remove empty series '{series_obj.title}' "
                        f"from Sonarr after episode delete: {e}"
                    )

        if not deleted_via_sonarr:
            if cand_arr_action == "unmonitor_only":
                # never delete files for "unmonitor_only" - no Sonarr instance
                # could unmonitor this episode, so there's nothing safe to do.
                await _mark_candidate_delete_failure(
                    candidate.id,
                    last_sonarr_error
                    or (
                        "Unmonitor Only: no active Sonarr instance is tracking "
                        "this episode, so there is nothing to unmonitor; the "
                        "file was left untouched by design"
                    ),
                )
                LOG.info(
                    f"Unmonitor Only - skipping '{series_obj.title}' {ep_label} "
                    f"(not tracked by any Sonarr instance, file left untouched)"
                )
                continue
            if not media_server_fallback_enabled:
                await _mark_candidate_delete_failure(
                    candidate.id,
                    last_sonarr_error
                    or "Media server fallback disabled for episode deletion",
                )
                LOG.warning(
                    f"Media server fallback disabled - skipping episode deletion for "
                    f"'{series_obj.title}' {ep_label}. Enable 'Allow Media Server "
                    f"Fallback Deletion' in General Settings."
                )
                continue
            media_svc = service_manager.main_media_server
            ep_svc_id = _episode_media_server_id(episode, _main_media_server_type())
            if media_svc is not None and ep_svc_id:
                try:
                    await media_svc.delete_item(ep_svc_id)
                    LOG.info(
                        f"Deleted '{series_obj.title}' {ep_label} via media server"
                    )
                except Exception as e:
                    await _mark_candidate_delete_failure(
                        candidate.id,
                        f"Media server episode deletion failed: {e}",
                    )
                    LOG.error(
                        f"Media server deletion failed for '{series_obj.title}' "
                        f"{ep_label}: {e} - skipping"
                    )
                    continue
            else:
                await _mark_candidate_delete_failure(
                    candidate.id,
                    last_sonarr_error or "No deletion method available for episode",
                )
                LOG.warning(
                    f"No deletion method available for '{series_obj.title}' "
                    f"{ep_label} - skipping"
                )
                continue

        async with async_db() as db:
            cand_obj = (
                await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
            ).scalar_one_or_none()
            if cand_obj:
                await db.delete(cand_obj)

            if cand_arr_action == "unmonitor_only":
                # "unmonitor_only" leaves the episode/season/series records and
                # files exactly as-is - only the Sonarr entry (unmonitored
                # above) changes.
                deleted_episode_size = None
            else:
                episode_db = (
                    await db.execute(
                        select(Episode).where(Episode.id == candidate.episode_id)
                    )
                ).scalar_one_or_none()
                season_db = (
                    await db.execute(
                        select(Season).where(Season.id == candidate.season_id)
                    )
                ).scalar_one_or_none()
                series_db = (
                    await db.execute(
                        select(Series).where(Series.id == candidate.series_id)
                    )
                ).scalar_one_or_none()

                deleted_episode_size = (
                    episode_db.size
                    if episode_db and episode_db.size is not None
                    else episode.size
                )

                if episode_db:
                    await db.execute(
                        delete(ProtectedMedia).where(
                            ProtectedMedia.episode_id == episode_db.id
                        )
                    )
                    await db.execute(
                        delete(ProtectionRequest).where(
                            ProtectionRequest.episode_id == episode_db.id,
                            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
                        )
                    )
                    await db.execute(
                        update(ProtectionRequest)
                        .where(
                            ProtectionRequest.episode_id == episode_db.id,
                            ProtectionRequest.status != ProtectionRequestStatus.PENDING,
                        )
                        .values(episode_id=None)
                    )
                    await db.execute(
                        delete(DeleteRequest).where(
                            DeleteRequest.episode_id == episode_db.id,
                            DeleteRequest.status == ProtectionRequestStatus.PENDING,
                        )
                    )
                    await db.execute(
                        update(DeleteRequest)
                        .where(
                            DeleteRequest.episode_id == episode_db.id,
                            DeleteRequest.status != ProtectionRequestStatus.PENDING,
                        )
                        .values(episode_id=None)
                    )
                    await db.delete(episode_db)

                if (
                    deleted_episode_size is not None
                    and season_db is not None
                    and season_db.size is not None
                ):
                    season_db.size = max(0, season_db.size - deleted_episode_size)
                if season_db is not None and season_db.episode_count is not None:
                    season_db.episode_count = max(0, season_db.episode_count - 1)
                if (
                    deleted_episode_size is not None
                    and series_db is not None
                    and series_db.size is not None
                ):
                    series_db.size = max(0, series_db.size - deleted_episode_size)

                await db.flush()

                if season_db is not None:
                    remaining_episode = (
                        await db.execute(
                            select(Episode.id)
                            .where(Episode.season_id == season_db.id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if remaining_episode is None:
                        await db.execute(
                            delete(ReclaimCandidate).where(
                                ReclaimCandidate.season_id == season_db.id
                            )
                        )
                        await db.execute(
                            delete(ProtectionRequest).where(
                                ProtectionRequest.season_id == season_db.id,
                                ProtectionRequest.status
                                == ProtectionRequestStatus.PENDING,
                            )
                        )
                        await db.execute(
                            update(ProtectionRequest)
                            .where(
                                ProtectionRequest.season_id == season_db.id,
                                ProtectionRequest.status
                                != ProtectionRequestStatus.PENDING,
                            )
                            .values(season_id=None, episode_id=None)
                        )
                        await db.execute(
                            delete(DeleteRequest).where(
                                DeleteRequest.season_id == season_db.id,
                                DeleteRequest.status == ProtectionRequestStatus.PENDING,
                            )
                        )
                        await db.execute(
                            update(DeleteRequest)
                            .where(
                                DeleteRequest.season_id == season_db.id,
                                DeleteRequest.status != ProtectionRequestStatus.PENDING,
                            )
                            .values(season_id=None, episode_id=None)
                        )
                        await db.execute(
                            delete(ProtectedMedia).where(
                                ProtectedMedia.season_id == season_db.id
                            )
                        )
                        await db.delete(season_db)
                        await db.flush()
                        await _soft_remove_series_if_empty(db, candidate.series_id)

            db.add(
                ReclaimHistory(
                    approved_by=approved_by,
                    media_type=MediaType.SERIES,
                    tmdb_id=series_obj.tmdb_id,
                    name=f"{series_obj.title} {ep_label}",
                    size=deleted_episode_size,
                    attributes=_build_reclaim_history_attributes(season=season),
                    action="unmonitored_only"
                    if cand_arr_action == "unmonitor_only"
                    else "unmonitored"
                    if cand_arr_action == "unmonitor"
                    else "deleted",
                )
            )
            await db.commit()

        deleted_count += 1
        if sonarr_ref_config_id is not None and sonarr_ref_id is not None:
            sonarr_refresh_after_episode_ops.setdefault(
                sonarr_ref_config_id, set()
            ).add(sonarr_ref_id)

    await _best_effort_sonarr_refresh(
        sonarr_refresh_after_episode_ops,
        context="episode cleanup",
    )
    return deleted_count


async def _delete_movies_via_media_server(
    candidates: Sequence[ReclaimCandidate],
    already_deleted: list[dict[str, Any]],
    approved_by: str = "system",
) -> int:
    """Deletes movies via the main media server as fallback when not in Radarr.

    Uses whichever Plex/Jellyfin/Emby server is designated as main.  If no main is
    set, falls back to whichever of Jellyfin/Emby/Plex is initialized (Jellyfin
    first).

    Returns count of movies deleted.
    """
    already_deleted_ids = {m["movie_id"] for m in already_deleted}
    remaining_candidates = [
        c for c in candidates if c.movie_id and c.movie_id not in already_deleted_ids
    ]

    if not remaining_candidates:
        return 0

    LOG.info(
        f"Attempting to delete {len(remaining_candidates)} movies via media server"
    )

    main_service = service_manager.main_media_server
    if not main_service:
        LOG.warning("No main media server available for movie deletion fallback")
        return 0

    main_service_type = service_manager.main_media_server_type or Service.PLEX

    # load movies with their versions
    async with async_db() as db:
        movie_ids = [c.movie_id for c in remaining_candidates]
        result = await db.execute(
            select(Movie)
            .where(Movie.id.in_(movie_ids))
            .options(selectinload(Movie.versions))
        )
        movies = {m.id: m for m in result.scalars().all()}

    deleted_count = 0

    for candidate in remaining_candidates:
        if candidate.movie_id is None:
            continue
        movie = movies.get(candidate.movie_id)
        if not movie or not movie.tmdb_id:
            continue

        service_versions = [v for v in movie.versions if v.service == main_service_type]
        if not service_versions:
            LOG.debug(
                f"Movie '{movie.title}' has no versions for {main_service_type} - skipping"
            )
            continue

        try:
            # delete each unique item (same movie may appear in multiple libraries)
            deleted_item_ids: set[str] = set()
            for ver in service_versions:
                if ver.service_item_id not in deleted_item_ids:
                    await main_service.delete_item(ver.service_item_id)
                    deleted_item_ids.add(ver.service_item_id)

            # attempt filesystem sibling cleanup for each deleted version
            path_mappings = await _load_path_mappings()
            for ver in service_versions:
                local_path = resolve_path(
                    ver.path, path_mappings, service_type=main_service_type.value
                )
                if local_path:
                    try:
                        sibling_cleanup(local_path)
                    except Exception as fs_err:
                        LOG.warning(
                            f"sibling_cleanup failed for '{ver.path}': {fs_err}"
                        )

            # mark as removed and delete candidate
            async with async_db() as db:
                result = await db.execute(
                    select(Movie).where(Movie.id == candidate.movie_id)
                )
                movie_obj = result.scalar_one_or_none()
                if movie_obj:
                    movie_obj.removed_at = datetime.now(UTC)
                    movie_obj.added_at = None

                result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                if service_manager.seerr and movie.tmdb_id:
                    try:
                        await _reset_seerr_request(movie.tmdb_id, MediaType.MOVIE)
                    except Exception as e:
                        LOG.warning(
                            f"Failed to reset Seerr request for '{movie.title}': {e}"
                        )

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.MOVIE,
                        tmdb_id=movie.tmdb_id,
                        name=movie.title,
                        size=movie.size,
                    )
                )

                await db.commit()

            deleted_count += 1
            LOG.info(f"Deleted movie '{movie.title}' via {main_service_type}")
            for ver in service_versions:
                await _dispatch_reclaim_event(
                    action="deleted",
                    media_type=MediaType.MOVIE,
                    title=movie.title,
                    tmdb_id=movie.tmdb_id,
                    candidate_id=candidate.id,
                    path=ver.path,
                    service_type=main_service_type,
                    movie_version_id=ver.id,
                )

        except Exception as e:
            LOG.error(
                f"Failed to delete movie '{movie.title}' via {main_service_type}: {e}"
            )

    return deleted_count


async def _delete_series_via_media_server(
    candidates: Sequence[ReclaimCandidate],
    already_deleted: list[dict[str, Any]],
    approved_by: str = "system",
) -> int:
    """Deletes series via Jellyfin or Plex as fallback when not in Sonarr.

    Args:
        candidates: All series candidates
        already_deleted: List of series already deleted via Sonarr

    Returns:
        Count of series deleted via media servers
    """
    already_deleted_ids = {s["series_id"] for s in already_deleted}
    remaining_candidates = [
        c for c in candidates if c.series_id and c.series_id not in already_deleted_ids
    ]

    if not remaining_candidates:
        return 0

    LOG.info(
        f"Attempting to delete {len(remaining_candidates)} series via media server (not in Sonarr)"
    )
    deleted_count = 0

    main_service = service_manager.main_media_server
    if not main_service:
        LOG.warning("No main media server available for series deletion fallback")
        return 0

    main_service_type = service_manager.main_media_server_type or Service.PLEX

    # get series from database to access service refs
    async with async_db() as db:
        series_ids = [c.series_id for c in remaining_candidates]
        result = await db.execute(
            select(Series)
            .where(Series.id.in_(series_ids))
            .options(selectinload(Series.service_refs))
        )
        series = {s.id: s for s in result.scalars().all()}

    for candidate in remaining_candidates:
        if candidate.series_id is None:
            continue
        series_obj = series.get(candidate.series_id)
        if not series_obj or not series_obj.tmdb_id:
            continue

        ref = next(
            (r for r in series_obj.service_refs if r.service == main_service_type),
            None,
        )
        if not ref:
            LOG.debug(
                f"Series '{series_obj.title}' not found in {main_service_type} (no service ref)"
            )
            continue

        try:
            await main_service.delete_item(ref.service_id)

            async with async_db() as db:
                result = await db.execute(
                    select(Series).where(Series.id == candidate.series_id)
                )
                series_db = result.scalar_one_or_none()
                if series_db:
                    series_db.removed_at = datetime.now(UTC)
                    series_db.added_at = None

                result = await db.execute(
                    select(ReclaimCandidate).where(ReclaimCandidate.id == candidate.id)
                )
                cand = result.scalar_one_or_none()
                if cand:
                    await db.delete(cand)

                if service_manager.seerr and series_obj.tmdb_id:
                    try:
                        await _reset_seerr_request(series_obj.tmdb_id, MediaType.SERIES)
                    except Exception as e:
                        LOG.warning(
                            f"Failed to reset Seerr request for {series_obj.title}: {e}"
                        )

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.SERIES,
                        tmdb_id=series_obj.tmdb_id,
                        name=series_obj.title,
                        size=series_db.size if series_db else None,
                    )
                )

                await db.commit()

            deleted_count += 1
            LOG.info(f"Deleted series '{series_obj.title}' via {main_service_type}")
            await _dispatch_reclaim_event(
                action="deleted",
                media_type=MediaType.SERIES,
                title=series_obj.title,
                tmdb_id=series_obj.tmdb_id,
                candidate_id=candidate.id,
                path=ref.path,
                service_type=main_service_type,
            )

        except Exception as e:
            LOG.error(
                f"Failed to delete series '{series_obj.title}' via {main_service_type}: {e}"
            )

    return deleted_count


async def _reset_seerr_request(tmdb_id: int, media_type: MediaType) -> None:
    """Remove requests and media items in Seerr (Overseerr/Jellyseerr) after media deletion.

    Args:
        tmdb_id: TMDB ID of the media
        media_type: Movie or Series
    """
    if not service_manager.seerr:
        return

    try:
        if media_type is MediaType.MOVIE:
            # delete all requests first
            await service_manager.seerr.delete_movie_requests(tmdb_id)
            LOG.debug(f"Deleted Seerr movie requests for TMDB ID {tmdb_id}")
            # then delete the media item itself
            await service_manager.seerr.delete_movie_media(tmdb_id)
            LOG.debug(f"Deleted Seerr movie media for TMDB ID {tmdb_id}")
        else:
            # delete all requests first
            await service_manager.seerr.delete_tv_requests(tmdb_id)
            LOG.debug(f"Deleted Seerr TV requests for TMDB ID {tmdb_id}")
            # then delete the media item itself
            await service_manager.seerr.delete_tv_media(tmdb_id)
            LOG.debug(f"Deleted Seerr TV media for TMDB ID {tmdb_id}")
    except PermissionError as e:
        LOG.warning(f"Seerr permission error for TMDB {tmdb_id}: {e}")
    except Exception as e:
        LOG.warning(f"Failed to delete Seerr data for TMDB {tmdb_id}: {e}")


async def delete_specific_candidates(
    candidate_ids: list[int], approved_by: str = "system"
) -> tuple[int, int]:
    """Safely delete candidates after pruning managed Leaving Soon collections."""
    unique_candidate_ids = list(dict.fromkeys(candidate_ids))
    if not unique_candidate_ids:
        return 0, 0

    try:
        await _prune_leaving_soon_before_candidate_actions(unique_candidate_ids)
    except Exception as e:
        error = f"Deletion blocked by Leaving Soon collection cleanup: {e}"
        LOG.error(error)
        await _mark_candidate_delete_failures(unique_candidate_ids, error)
        await _reconcile_leaving_soon_after_candidate_actions()
        return 0, len(unique_candidate_ids)

    try:
        return await _delete_specific_candidates_impl(
            unique_candidate_ids,
            approved_by=approved_by,
        )
    finally:
        await _reconcile_leaving_soon_after_candidate_actions()


async def _delete_specific_candidates_impl(
    candidate_ids: list[int], approved_by: str = "system"
) -> tuple[int, int]:
    """Deletes specific reclaim candidates by their IDs.

    Uses the same deletion priority as delete_cleanup_candidates:
    Radarr/Sonarr first, then Jellyfin/Emby/Plex (main server first) fallback.

    Returns (deleted_count, failed_count).
    """
    if not candidate_ids:
        return 0, 0

    restrict = frozenset(candidate_ids)

    # look up which types/actions we're dealing with so we only invoke relevant paths
    async with async_db() as db:
        result = await db.execute(
            select(
                ReclaimCandidate.id,
                ReclaimCandidate.media_type,
                ReclaimCandidate.matched_rule_ids,
            ).where(ReclaimCandidate.id.in_(restrict))
        )
        rows = result.all()

    found_ids = {r[0] for r in rows}
    all_rule_ids = {
        rule_id for _id, _type, rule_ids in rows for rule_id in (rule_ids or [])
    }
    rules_by_id: dict[int, ReclaimRule] = {}
    if all_rule_ids:
        async with async_db() as db:
            rules_by_id = {
                rule.id: rule
                for rule in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(all_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    move_ids = {
        candidate_id
        for candidate_id, _media_type, rule_ids in rows
        if _matched_rule_ids_should_move_instead_of_delete(rule_ids or [], rules_by_id)
    }
    delete_ids = found_ids - move_ids
    delete_restrict = frozenset(delete_ids)
    types = {
        media_type
        for candidate_id, media_type, _rule_ids in rows
        if candidate_id in delete_ids
    }

    LOG.info(
        f"Manual deletion of {len(found_ids)} candidate(s) requested "
        f"({len(move_ids)} move-routed, {len(delete_ids)} delete-routed, "
        f"movies={MediaType.MOVIE in types}, series={MediaType.SERIES in types})"
    )

    moved = 0
    move_failed = 0
    if move_ids:
        moved, move_failed = await _move_specific_candidates_impl(
            list(move_ids), approved_by=approved_by
        )

    delete_deleted = 0
    if MediaType.MOVIE in types and (
        service_manager.radarr or service_manager.main_media_server
    ):
        delete_deleted += await _delete_movie_candidates(
            restrict_to_ids=delete_restrict, approved_by=approved_by
        )

    if MediaType.SERIES in types and (
        service_manager.sonarr or service_manager.main_media_server
    ):
        delete_deleted += await _delete_series_candidates(
            restrict_to_ids=delete_restrict, approved_by=approved_by
        )
        delete_deleted += await _delete_season_candidates(
            restrict_to_ids=delete_restrict, approved_by=approved_by
        )
        delete_deleted += await _delete_episode_candidates(
            restrict_to_ids=delete_restrict, approved_by=approved_by
        )

    delete_failed = max(0, len(delete_ids) - delete_deleted)
    failed = move_failed + delete_failed
    if delete_failed:
        await _mark_unexplained_delete_failures(
            delete_ids,
            (
                "Candidate delete failed before a scoped handler could complete. "
                "The candidate may be stale, have an invalid scope, lack an active "
                "Arr/media-server route, or have failed in a branch without a more "
                "specific error."
            ),
        )
    processed = moved + delete_deleted
    LOG.info(f"Manual deletion complete: {processed} processed, {failed} failed")
    return processed, failed


async def move_specific_candidates(
    candidate_ids: list[int], approved_by: str = "system"
) -> tuple[int, int]:
    """Safely move candidates after pruning managed Leaving Soon collections."""
    unique_candidate_ids = list(dict.fromkeys(candidate_ids))
    if not unique_candidate_ids:
        return 0, 0

    try:
        await _prune_leaving_soon_before_candidate_actions(unique_candidate_ids)
    except Exception as e:
        error = f"Move blocked by Leaving Soon collection cleanup: {e}"
        LOG.error(error)
        await _mark_candidate_delete_failures(unique_candidate_ids, error)
        await _reconcile_leaving_soon_after_candidate_actions()
        return 0, len(unique_candidate_ids)

    try:
        return await _move_specific_candidates_impl(
            unique_candidate_ids,
            approved_by=approved_by,
        )
    finally:
        await _reconcile_leaving_soon_after_candidate_actions()


async def _move_specific_candidates_impl(
    candidate_ids: list[int], approved_by: str = "system"
) -> tuple[int, int]:
    """Move specific reclaim candidates to the configured destination root.

    Resolves each candidate's file path using configured path mappings, moves the
    file (and same stem siblings) to ``move_destination_root``, then removes the
    item from the arr/media-server without deleting the original (since it was
    already moved).

    Returns (moved_count, failed_count).
    """
    if not candidate_ids:
        return 0, 0

    # load move destinations + path mappings from DB
    async with async_db() as db:
        result = await db.execute(select(GeneralSettings))
        gen_settings = result.scalars().first()

    destination_movies_str = (
        gen_settings.move_destination_movies if gen_settings else None
    ) or ""
    destination_series_str = (
        gen_settings.move_destination_series if gen_settings else None
    ) or ""
    path_mappings = (gen_settings.path_mappings if gen_settings else None) or []
    default_arr_delete_behavior = _coerce_arr_delete_fallback(
        gen_settings.default_arr_delete_behavior if gen_settings else None
    )
    add_arr_import_exclusions_on_delete = bool(
        gen_settings.add_arr_import_exclusions_on_delete if gen_settings else True
    )
    favorites_ignore_enabled = bool(
        gen_settings.favorites_ignore_enabled if gen_settings else False
    )
    favorites_protect_all_users = bool(
        gen_settings.favorites_protect_all_users if gen_settings else False
    )
    favorites_usernames = {
        _normalize_favorites_username(str(raw))
        for raw in ((gen_settings.favorites_usernames or []) if gen_settings else [])
        if str(raw).strip()
    }

    # load candidates with their version/movie data
    async with async_db() as db:
        query = select(ReclaimCandidate).where(ReclaimCandidate.id.in_(candidate_ids))
        candidate_result = await db.execute(query)
        candidates: Sequence[ReclaimCandidate] = candidate_result.scalars().all()
        if favorites_ignore_enabled and candidates:
            movie_candidates = [
                candidate
                for candidate in candidates
                if candidate.media_type == MediaType.MOVIE
            ]
            series_candidates = [
                candidate
                for candidate in candidates
                if candidate.media_type == MediaType.SERIES
            ]
            (
                filtered_movie_candidates,
                skipped_movies,
            ) = await _filter_movie_candidates_by_favorites(
                db,
                movie_candidates,
                protect_all_users=favorites_protect_all_users,
                usernames=favorites_usernames,
            )
            (
                filtered_series_candidates,
                skipped_series,
            ) = await _filter_series_candidates_by_favorites(
                db,
                series_candidates,
                protect_all_users=favorites_protect_all_users,
                usernames=favorites_usernames,
            )
            allowed_ids = {
                candidate.id
                for candidate in [
                    *filtered_movie_candidates,
                    *filtered_series_candidates,
                ]
            }
            candidates = [
                candidate for candidate in candidates if candidate.id in allowed_ids
            ]
            skipped_total = skipped_movies + skipped_series
            if skipped_total:
                LOG.info(
                    f"Skipped {skipped_total} move candidate(s) due to favorites protection"
                )

        candidate_version_ids = [
            c.movie_version_id for c in candidates if c.movie_version_id
        ]
        ver_result = await db.execute(
            select(MovieVersion)
            .where(MovieVersion.id.in_(candidate_version_ids))
            .options(selectinload(MovieVersion.movie))
        )
        versions_by_id: dict[int, MovieVersion] = {
            v.id: v for v in ver_result.scalars().all()
        }

        all_rule_ids = {
            rule_id
            for candidate in candidates
            for rule_id in (candidate.matched_rule_ids or [])
        }
        rules_by_id: dict[int, ReclaimRule] = {}
        if all_rule_ids:
            rules_by_id = {
                rule.id: rule
                for rule in (
                    await db.execute(
                        select(ReclaimRule).where(ReclaimRule.id.in_(all_rule_ids))
                    )
                )
                .scalars()
                .all()
            }

    movie_arr_actions: dict[int, ArrDeleteAction] = {}
    series_arr_actions: dict[int, ArrDeleteAction] = {}
    for candidate in candidates:
        action = _get_arr_action(
            candidate,
            rules_by_id,
            default_arr_delete_behavior,
        )
        if candidate.movie_id is not None:
            movie_arr_actions[candidate.movie_id] = _merge_arr_action(
                movie_arr_actions.get(candidate.movie_id),
                action,
            )
        if candidate.series_id is not None:
            series_arr_actions[candidate.series_id] = _merge_arr_action(
                series_arr_actions.get(candidate.series_id),
                action,
            )

    moved = 0
    failed = 0
    move_radarr_refresh: dict[int, set[int]] = {}
    move_sonarr_refresh: dict[int, set[int]] = {}
    finalized_radarr_refs: set[tuple[int, int]] = set()
    finalized_sonarr_refs: set[tuple[int, int]] = set()

    #### movie candidates ####
    movie_candidates = [c for c in candidates if c.media_type == MediaType.MOVIE]
    for candidate in movie_candidates:
        try:
            version = (
                versions_by_id.get(candidate.movie_version_id)
                if candidate.movie_version_id
                else None
            )
            if version is None:
                LOG.warning(
                    f"move_specific_candidates: candidate {candidate.id} has no movie version"
                )
                failed += 1
                continue

            movie = version.movie
            if movie is None:
                failed += 1
                continue

            # pick destination root based on media type
            dest_str = (
                destination_series_str
                if candidate.media_type == MediaType.SERIES
                else destination_movies_str
            )
            if not dest_str.strip():
                LOG.warning(
                    f"move_specific_candidates: destination not configured for "
                    f"{candidate.media_type} (candidate {candidate.id})"
                )
                failed += 1
                continue
            destination_root = Path(dest_str)

            local_path = resolve_path(
                version.path, path_mappings, service_type=version.service.value
            )
            if local_path is None:
                LOG.warning(
                    f"move_specific_candidates: cannot resolve path for '{version.path}' "
                    f"(candidate {candidate.id})"
                )
                failed += 1
                continue

            source_movie_folder = local_path.parent

            # move the file + same stem siblings to destination
            dest = move_media(
                local_path,
                destination_root,
                path_mappings,
                service_type=version.service.value,
            )
            _cleanup_moved_source_directories(
                {source_movie_folder},
                context="move local cleanup",
            )
            source_movie_folder_cleared = not source_movie_folder.exists()

            # Honor the configured Arr action after the files are safely moved.
            # A Radarr entry is only removable when its complete source folder
            # is gone; otherwise deleting the entry could affect another version.
            radarr_clients = service_manager.radarr_clients()
            if not radarr_clients and service_manager.radarr:
                radarr_clients = {0: service_manager.radarr}
            if radarr_clients and movie.id:
                try:
                    async with async_db() as db:
                        arr_refs = (
                            await db.execute(
                                select(
                                    MovieArrRef.service_config_id,
                                    MovieArrRef.arr_movie_id,
                                    MovieArrRef.arr_movie_path,
                                ).where(MovieArrRef.movie_id == movie.id)
                            )
                        ).all()
                    arr_action = movie_arr_actions.get(movie.id, "unmonitor")
                    allowed_config_ids = _candidate_arr_config_ids(
                        candidate, rules_by_id, "radarr"
                    )
                    for config_id, arr_movie_id, arr_movie_path in arr_refs:
                        if config_id not in radarr_clients or arr_movie_id is None:
                            continue
                        if (
                            allowed_config_ids is not None
                            and config_id not in allowed_config_ids
                        ):
                            continue
                        if (
                            version.path
                            and arr_movie_path
                            and not _path_matches_arr_folder(
                                version.path,
                                arr_movie_path,
                                path_mappings,
                                path_service_type=version.service.value,
                                arr_service_type=Service.RADARR.value,
                                arr_service_config_id=config_id,
                            )
                        ):
                            continue
                        pair = (config_id, arr_movie_id)
                        if pair in finalized_radarr_refs:
                            continue
                        finalized_radarr_refs.add(pair)
                        if (
                            arr_action in {"delete", "remove_if_empty"}
                            and source_movie_folder_cleared
                        ):
                            await radarr_clients[config_id].delete_movies(
                                [arr_movie_id],
                                delete_files=False,
                                add_import_exclusion=(
                                    add_arr_import_exclusions_on_delete
                                ),
                            )
                            LOG.info(
                                f"move: removed '{movie.title}' from Radarr without "
                                f"deleting moved files (config_id={config_id}, "
                                f"arr_id={arr_movie_id})"
                            )
                        else:
                            if arr_action not in UNMONITOR_LIKE_ARR_ACTIONS:
                                LOG.info(
                                    f"move: retained '{movie.title}' in Radarr because "
                                    "the source folder still contains other content; "
                                    "falling back to unmonitor"
                                )
                            await radarr_clients[config_id].unmonitor_movies(
                                [arr_movie_id]
                            )
                            LOG.info(
                                f"move: unmonitored '{movie.title}' in Radarr "
                                f"(config_id={config_id}, arr_id={arr_movie_id})"
                            )
                            move_radarr_refresh.setdefault(config_id, set()).add(
                                arr_movie_id
                            )
                except Exception as arr_err:
                    LOG.warning(
                        f"move_specific_candidates: Radarr finalization failed for "
                        f"'{movie.title}' after files were moved; no delete fallback "
                        f"will be attempted: {arr_err}"
                    )

            # Remove the item from the media server after the local files have
            # already been moved. This is intentionally best-effort and never
            # falls back to the normal destructive delete path.
            try:
                main_service = service_manager.main_media_server
                if main_service:
                    await main_service.delete_movie_version(
                        version.service_item_id, version.service_media_id
                    )
            except Exception as svc_err:
                LOG.warning(
                    f"move_specific_candidates: media-server metadata removal failed "
                    f"for '{movie.title}' after files were moved; no delete fallback "
                    f"will be attempted: {svc_err}"
                )

            # update DB
            async with async_db() as db:
                # bulk-delete all candidates and delete-requests referencing this
                # version before removing the version row (no ondelete on their FKs)
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.movie_version_id == version.id
                    )
                )
                await db.execute(
                    delete(DeleteRequest).where(
                        DeleteRequest.movie_version_id == version.id
                    )
                )

                ver_result = await db.execute(
                    select(MovieVersion).where(MovieVersion.id == version.id)
                )
                ver_db = ver_result.scalar_one_or_none()
                if ver_db:
                    deleted_size = ver_db.size or 0
                    movie_result = await db.execute(
                        select(Movie).where(Movie.id == ver_db.movie_id)
                    )
                    movie_db = movie_result.scalar_one_or_none()
                    if movie_db and movie_db.size:
                        movie_db.size = max(0, movie_db.size - deleted_size)
                    await db.delete(ver_db)
                    await db.flush()
                    await _soft_remove_movie_if_empty(db, ver_db.movie_id)

                db.add(
                    ReclaimHistory(
                        approved_by=approved_by,
                        media_type=MediaType.MOVIE,
                        tmdb_id=movie.tmdb_id,
                        name=movie.title,
                        path=version.path,
                        size=version.size,
                        attributes=_build_reclaim_history_attributes(
                            movie_version=version
                        ),
                        action="moved",
                        destination_path=str(dest),
                    )
                )
                await db.commit()

            moved += 1
            LOG.info(f"Moved '{movie.title}' version {version.id} to {dest}")
            await _dispatch_reclaim_event(
                action="moved",
                media_type=MediaType.MOVIE,
                title=movie.title,
                tmdb_id=movie.tmdb_id,
                candidate_id=candidate.id,
                path=version.path,
                local_path=str(local_path),
                destination_path=str(dest),
                service_type=version.service,
                movie_version_id=version.id,
            )

        except Exception as e:
            LOG.error(
                f"move_specific_candidates: failed for candidate {candidate.id}: {e}",
                exc_info=True,
            )
            failed += 1

    await _best_effort_radarr_rescan(
        move_radarr_refresh,
        context="move cleanup",
    )

    #### series / season candidates ####
    series_candidates = [c for c in candidates if c.media_type == MediaType.SERIES]
    if series_candidates:
        series_ids = list({c.series_id for c in series_candidates if c.series_id})
        season_ids = [c.season_id for c in series_candidates if c.season_id]
        episode_ids = [c.episode_id for c in series_candidates if c.episode_id]

        async with async_db() as db:
            series_result = await db.execute(
                select(Series)
                .where(Series.id.in_(series_ids))
                .options(selectinload(Series.service_refs))
            )
            series_map: dict[int, Series] = {
                s.id: s for s in series_result.scalars().all()
            }

            seasons_map: dict[int, Season] = {}
            if season_ids:
                seasons_result = await db.execute(
                    select(Season).where(Season.id.in_(season_ids))
                )
                seasons_map = {s.id: s for s in seasons_result.scalars().all()}

            episodes_map: dict[int, Episode] = {}
            if episode_ids:
                episodes_result = await db.execute(
                    select(Episode).where(Episode.id.in_(episode_ids))
                )
                episodes_map = {e.id: e for e in episodes_result.scalars().all()}

            # load arr refs for Sonarr unmonitor routing
            arr_refs_result = await db.execute(
                select(
                    SeriesArrRef.series_id,
                    SeriesArrRef.service_config_id,
                    SeriesArrRef.arr_series_id,
                    SeriesArrRef.arr_series_path,
                ).where(SeriesArrRef.series_id.in_(series_ids))
            )
            # series_id -> list of (config_id, arr_series_id, arr_series_path)
            series_arr_refs: dict[int, list[tuple[Any, ...]]] = {}
            for s_id, config_id, arr_s_id, arr_s_path in arr_refs_result.all():
                series_arr_refs.setdefault(s_id, []).append(
                    (config_id, arr_s_id, arr_s_path)
                )

        for candidate in series_candidates:
            try:
                series_obj = (
                    series_map.get(candidate.series_id) if candidate.series_id else None
                )
                if not series_obj:
                    LOG.warning(
                        f"move_specific_candidates: no series for candidate {candidate.id}"
                    )
                    failed += 1
                    continue

                if not destination_series_str.strip():
                    LOG.warning(
                        f"move_specific_candidates: series destination not configured "
                        f"(candidate {candidate.id})"
                    )
                    failed += 1
                    continue
                destination_root = Path(destination_series_str)

                # pick the primary service ref that has a path
                series_ref = next((r for r in series_obj.service_refs if r.path), None)
                if not series_ref or not series_ref.path:
                    LOG.warning(
                        f"move_specific_candidates: no path on service ref for "
                        f"'{series_obj.title}' (candidate {candidate.id})"
                    )
                    failed += 1
                    continue

                local_series_path = resolve_path(
                    series_ref.path,
                    path_mappings,
                    service_type=series_ref.service.value,
                )
                if local_series_path is None:
                    LOG.warning(
                        f"move_specific_candidates: cannot resolve path for "
                        f"'{series_obj.title}' (candidate {candidate.id})"
                    )
                    failed += 1
                    continue

                is_episode = candidate.episode_id is not None
                is_season = candidate.season_id is not None and not is_episode
                episode = (
                    episodes_map.get(candidate.episode_id)
                    if is_episode and candidate.episode_id
                    else None
                )
                season = (
                    seasons_map.get(candidate.season_id)
                    if (is_season or is_episode) and candidate.season_id
                    else None
                )
                season_folder: Path | None = None
                candidate_source_paths: set[Path] = set()

                local_episode_path = None
                if is_episode:
                    if episode is None or not episode.path:
                        LOG.warning(
                            f"move_specific_candidates: episode path missing "
                            f"(candidate {candidate.id})"
                        )
                        failed += 1
                        continue
                    local_episode_path = resolve_path(
                        episode.path,
                        path_mappings,
                        service_type=series_ref.service.value,
                    )
                    if local_episode_path is None:
                        LOG.warning(
                            f"move_specific_candidates: cannot resolve episode path "
                            f"for '{series_obj.title}' (candidate {candidate.id})"
                        )
                        failed += 1
                        continue
                    dest = move_media(
                        local_episode_path,
                        destination_root,
                        path_mappings,
                        service_type=series_ref.service.value,
                    )
                    candidate_source_paths.add(local_episode_path.parent)

                elif is_season:
                    if season is None:
                        LOG.warning(
                            f"move_specific_candidates: season record missing "
                            f"(candidate {candidate.id})"
                        )
                        failed += 1
                        continue
                    resolved_season_path = (
                        resolve_path(
                            season.path,
                            path_mappings,
                            service_type=series_ref.service.value,
                        )
                        if season.path
                        else None
                    )
                    season_folder = resolved_season_path or find_season_folder(
                        local_series_path, season.season_number
                    )
                    if season_folder is None:
                        LOG.warning(
                            f"move_specific_candidates: cannot find season folder "
                            f"S{season.season_number:02d} inside '{local_series_path}' "
                            f"(candidate {candidate.id})"
                        )
                        failed += 1
                        continue

                    # flat series: episodes live directly in the series root with
                    # no season sub folders (move only this season's files so
                    # other seasons are left intact)
                    if season_folder == local_series_path:
                        dest = move_season_files(
                            local_series_path,
                            destination_root,
                            episode_paths=season.episode_paths or [],
                            path_mappings=path_mappings,
                            service_type=series_ref.service.value,
                        )
                        candidate_source_paths.add(local_series_path)
                    else:
                        dest = move_directory(
                            season_folder,
                            destination_root,
                            path_mappings,
                            service_type=series_ref.service.value,
                            cleanup_empty_parent=True,
                        )
                        candidate_source_paths.update(
                            {season_folder, local_series_path}
                        )
                else:
                    dest = move_directory(
                        local_series_path,
                        destination_root,
                        path_mappings,
                        service_type=series_ref.service.value,
                    )
                    candidate_source_paths.add(local_series_path)

                _cleanup_moved_source_directories(
                    candidate_source_paths,
                    context="move local cleanup",
                )
                whole_series_source_cleared = not local_series_path.exists()

                # Whole-series moves honor Delete versus Unmonitor. Partial TV
                # scopes remain in Sonarr and only adjust their monitoring/file state.
                sonarr_clients = service_manager.sonarr_clients()
                if not sonarr_clients and service_manager.sonarr:
                    sonarr_clients = {0: service_manager.sonarr}
                if sonarr_clients and candidate.series_id:
                    try:
                        refs = series_arr_refs.get(candidate.series_id, [])
                        allowed_config_ids = _candidate_arr_config_ids(
                            candidate, rules_by_id, "sonarr"
                        )
                        for config_id, arr_s_id, _arr_s_path in refs:
                            if config_id not in sonarr_clients or arr_s_id is None:
                                continue
                            if (
                                allowed_config_ids is not None
                                and config_id not in allowed_config_ids
                            ):
                                continue
                            if is_episode:
                                move_sonarr_refresh.setdefault(config_id, set()).add(
                                    arr_s_id
                                )
                                continue
                            if is_season and season:
                                await sonarr_clients[
                                    config_id
                                ].update_season_monitoring(
                                    arr_s_id, season.season_number, False
                                )
                                LOG.info(
                                    f"move: unmonitored '{series_obj.title}' "
                                    f"S{season.season_number:02d} in Sonarr "
                                    f"(config_id={config_id}, arr_id={arr_s_id})"
                                )
                                move_sonarr_refresh.setdefault(config_id, set()).add(
                                    arr_s_id
                                )
                            else:
                                pair = (config_id, arr_s_id)
                                if pair in finalized_sonarr_refs:
                                    continue
                                finalized_sonarr_refs.add(pair)
                                arr_action = series_arr_actions.get(
                                    candidate.series_id, "unmonitor"
                                )
                                if (
                                    arr_action in {"delete", "remove_if_empty"}
                                    and whole_series_source_cleared
                                ):
                                    await sonarr_clients[config_id].delete_series(
                                        arr_s_id,
                                        delete_files=False,
                                        add_import_exclusion=(
                                            add_arr_import_exclusions_on_delete
                                        ),
                                    )
                                    LOG.info(
                                        f"move: removed '{series_obj.title}' from "
                                        "Sonarr without deleting moved files "
                                        f"(config_id={config_id}, arr_id={arr_s_id})"
                                    )
                                else:
                                    if arr_action not in UNMONITOR_LIKE_ARR_ACTIONS:
                                        LOG.info(
                                            f"move: retained '{series_obj.title}' in "
                                            "Sonarr because its source folder still "
                                            "contains other content; falling back to "
                                            "unmonitor"
                                        )
                                    await sonarr_clients[config_id].unmonitor_series(
                                        [arr_s_id]
                                    )
                                    LOG.info(
                                        f"move: unmonitored '{series_obj.title}' in "
                                        f"Sonarr (config_id={config_id}, "
                                        f"arr_id={arr_s_id})"
                                    )
                                    move_sonarr_refresh.setdefault(
                                        config_id, set()
                                    ).add(arr_s_id)
                    except Exception as arr_err:
                        LOG.warning(
                            f"move_specific_candidates: Sonarr finalization failed for "
                            f"'{series_obj.title}' after files were moved; no delete "
                            f"fallback will be attempted: {arr_err}"
                        )

                # Remove from the media server after the local files have
                # already been moved. This is intentionally best-effort and
                # never falls back to the normal destructive delete path.
                try:
                    main_service = service_manager.main_media_server
                    main_service_type = service_manager.main_media_server_type
                    if main_service and series_ref:
                        if is_episode and episode:
                            if main_service_type is Service.JELLYFIN:
                                episode_service_id = episode.jellyfin_episode_id
                            elif main_service_type is Service.EMBY:
                                episode_service_id = episode.emby_episode_id
                            else:
                                episode_service_id = episode.plex_rating_key
                            if episode_service_id:
                                await main_service.delete_item(episode_service_id)
                        elif is_season and season:
                            # delete the season item from the media server
                            if main_service_type is Service.JELLYFIN:
                                season_service_id = season.jellyfin_season_id
                            elif main_service_type is Service.EMBY:
                                season_service_id = season.emby_season_id
                            else:
                                season_service_id = season.plex_season_rating_key
                            if season_service_id:
                                await main_service.delete_item(season_service_id)
                        else:
                            await main_service.delete_item(series_ref.service_id)
                except Exception as svc_err:
                    LOG.warning(
                        f"move_specific_candidates: media-server metadata removal "
                        f"failed for '{series_obj.title}' after files were moved; "
                        f"no delete fallback will be attempted: {svc_err}"
                    )

                # update DB
                async with async_db() as db:
                    cand_result = await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.id == candidate.id
                        )
                    )
                    cand = cand_result.scalar_one_or_none()
                    if cand:
                        await db.delete(cand)

                    if is_episode and episode:
                        series_db = (
                            await db.execute(
                                select(Series).where(Series.id == candidate.series_id)
                            )
                        ).scalar_one_or_none()
                        season_db = (
                            await db.execute(
                                select(Season).where(Season.id == candidate.season_id)
                            )
                        ).scalar_one_or_none()
                        if series_db and series_db.size and episode.size:
                            series_db.size = max(0, series_db.size - episode.size)
                        if season_db and season_db.size and episode.size:
                            season_db.size = max(0, season_db.size - episode.size)

                        episode_db = (
                            await db.execute(
                                select(Episode).where(
                                    Episode.id == candidate.episode_id
                                )
                            )
                        ).scalar_one_or_none()
                        if episode_db:
                            await db.execute(
                                delete(ReclaimCandidate).where(
                                    ReclaimCandidate.episode_id == episode_db.id
                                )
                            )
                            await db.execute(
                                delete(DeleteRequest).where(
                                    DeleteRequest.episode_id == episode_db.id
                                )
                            )
                            await db.execute(
                                update(ProtectionRequest)
                                .where(ProtectionRequest.episode_id == episode_db.id)
                                .values(episode_id=None)
                            )
                            await db.delete(episode_db)
                            await db.flush()
                            await _soft_remove_series_if_empty(db, candidate.series_id)

                        ep_label = (
                            f"S{season.season_number:02d}E{episode.episode_number:02d}"
                            if season
                            else f"E{episode.episode_number:02d}"
                        )
                        history_name = f"{series_obj.title} {ep_label}"
                        history_size = episode.size
                    elif is_season and season:
                        # reduce series size and remove season row
                        series_db = (
                            await db.execute(
                                select(Series).where(Series.id == candidate.series_id)
                            )
                        ).scalar_one_or_none()
                        if series_db and series_db.size and season.size:
                            series_db.size = max(0, series_db.size - season.size)

                        season_db = (
                            await db.execute(
                                select(Season).where(Season.id == candidate.season_id)
                            )
                        ).scalar_one_or_none()
                        if season_db:
                            await db.execute(
                                delete(ReclaimCandidate).where(
                                    ReclaimCandidate.season_id == season_db.id
                                )
                            )
                            await db.execute(
                                delete(ProtectionRequest).where(
                                    ProtectionRequest.season_id == season_db.id,
                                    ProtectionRequest.status
                                    == ProtectionRequestStatus.PENDING,
                                )
                            )
                            await db.execute(
                                update(ProtectionRequest)
                                .where(
                                    ProtectionRequest.season_id == season_db.id,
                                    ProtectionRequest.status
                                    != ProtectionRequestStatus.PENDING,
                                )
                                .values(season_id=None, episode_id=None)
                            )
                            await db.execute(
                                delete(DeleteRequest).where(
                                    DeleteRequest.season_id == season_db.id,
                                    DeleteRequest.status
                                    == ProtectionRequestStatus.PENDING,
                                )
                            )
                            await db.execute(
                                update(DeleteRequest)
                                .where(
                                    DeleteRequest.season_id == season_db.id,
                                    DeleteRequest.status
                                    != ProtectionRequestStatus.PENDING,
                                )
                                .values(season_id=None, episode_id=None)
                            )
                            await db.execute(
                                delete(ProtectedMedia).where(
                                    ProtectedMedia.season_id == season_db.id
                                )
                            )
                            await db.delete(season_db)
                            await db.flush()
                            await _soft_remove_series_if_empty(db, candidate.series_id)

                        history_name = f"{series_obj.title} S{season.season_number:02d}"
                        history_size = season.size
                    else:
                        # mark entire series as removed
                        series_db = (
                            await db.execute(
                                select(Series).where(Series.id == candidate.series_id)
                            )
                        ).scalar_one_or_none()
                        if series_db:
                            series_db.removed_at = datetime.now(UTC)
                            series_db.added_at = None

                        history_name = series_obj.title
                        history_size = series_obj.size

                    db.add(
                        ReclaimHistory(
                            approved_by=approved_by,
                            media_type=MediaType.SERIES,
                            tmdb_id=series_obj.tmdb_id,
                            name=history_name,
                            path=episode.path
                            if is_episode and episode
                            else series_ref.path,
                            size=history_size,
                            attributes=_build_reclaim_history_attributes(
                                season=season if is_season else None
                            ),
                            action="moved",
                            destination_path=str(dest),
                        )
                    )
                    await db.commit()

                moved += 1
                LOG.info(f"Moved '{series_obj.title}' to {dest}")
                event_local_path = (
                    local_episode_path
                    if is_episode and episode
                    else season_folder
                    if is_season and season
                    else local_series_path
                )
                await _dispatch_reclaim_event(
                    action="moved",
                    media_type=MediaType.SERIES,
                    title=series_obj.title,
                    tmdb_id=series_obj.tmdb_id,
                    candidate_id=candidate.id,
                    path=episode.path
                    if is_episode and episode
                    else season.path
                    if is_season and season
                    else series_ref.path,
                    local_path=str(event_local_path),
                    destination_path=str(dest),
                    service_type=series_ref.service,
                    season_id=season.id
                    if (is_season or is_episode) and season
                    else None,
                    season_number=season.season_number
                    if (is_season or is_episode) and season
                    else None,
                    episode_id=episode.id if is_episode and episode else None,
                    episode_number=episode.episode_number
                    if is_episode and episode
                    else None,
                )

            except Exception as e:
                LOG.error(
                    f"move_specific_candidates: failed for series candidate "
                    f"{candidate.id}: {e}",
                    exc_info=True,
                )
                failed += 1

    await _best_effort_sonarr_refresh(
        move_sonarr_refresh,
        context="move cleanup",
    )
    LOG.info(f"Move complete: {moved} moved, {failed} failed")
    return moved, failed
