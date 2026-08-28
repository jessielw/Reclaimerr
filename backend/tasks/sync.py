import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, TypeGuard, TypeVar

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.core.tmdb import AsyncTMDBClient
from backend.core.utils.datetime_utils import ensure_utc
from backend.core.utils.filesystem import normalize_fpath, paths_equivalent
from backend.database import async_db
from backend.database.models import (
    DeleteRequest,
    Episode,
    GeneralSettings,
    Movie,
    MovieArrRef,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
    ServiceMediaLibrary,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, ProtectionRequestStatus, Service, Task
from backend.models.media import (
    AggregatedEpisodeData,
    AggregatedMovieData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    MovieVersionData,
)
from backend.services.admin_notices import reconcile_stale_library_notice
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.media_favorites_cache import media_favorites_snapshot_cache
from backend.services.media_watch_snapshot_cache import media_watch_snapshot_cache
from backend.services.playback_history import (
    PlaybackRefreshResult,
    refresh_playback_history,
)
from backend.services.plex import PlexService
from backend.services.watch_identity import refresh_watch_user_aliases
from backend.user_types import MEDIA_SERVERS, MediaServerType

__all__ = [
    "sync_media",
    "resync_media",
    "sync_media_libraries",
    "refresh_playback_history_task",
    "sync_linked_data",
]

# number of records to process before committing to the database during sync tasks
COMMIT_BATCH_SIZE = 100
SONARR_DATE_FETCH_CONCURRENCY = 5
# refuse to tombstone more than half the library in one pass - a partial
# media-server response should not look like a mass deletion
MAX_SOFT_DELETE_RATIO = 0.5
MIN_LIBRARY_FOR_RATIO_CHECK = 20

_RowT = TypeVar("_RowT", Movie, Series)


def _select_rows_to_soft_delete(
    rows: Sequence[_RowT],
    matched_row_ids: set[int],
) -> list[_RowT]:
    """Rows this sync did not touch, and that are not already tombstoned.

    Identity here is the row primary key, not the TMDB id. A row can be matched
    through the tvdb/imdb fallback while keeping a different tmdb_id, so testing
    the tmdb_id would tombstone a row that was just updated from the main server.
    """
    return [row for row in rows if row.id not in matched_row_ids and not row.removed_at]


def _soft_delete_guard_tripped(delete_count: int, live_count: int) -> bool:
    """True when a delete set is too large a share of the library to trust.

    A partial media-server response makes the incoming set look small, and
    everything missing from it look deleted. Small libraries are exempt: removing
    three of four items there is legitimate.
    """
    if live_count < MIN_LIBRARY_FOR_RATIO_CHECK:
        return False
    return delete_count > live_count * MAX_SOFT_DELETE_RATIO


# A delete set larger than the ratio allows is usually a partial media-server
# response, but it is also what a legitimate main-server switch looks like. A
# flapping server proposes a DIFFERENT set each time; a real reduction proposes
# the same one. So skip the first time and act if the same set comes back.
_previous_large_delete_sets: dict[MediaType, frozenset[int]] = {}


def _soft_delete_blocked(
    media_type: MediaType,
    rows_to_delete: Sequence[_RowT],
    live_count: int,
) -> bool:
    """True when this delete set should be skipped this run.

    Blocking on the ratio alone deadlocks: a main-server switch proposes the
    same large set every run, and the phantom rows it wants gone inflate the
    live count faster than the real library grows, so the pass never relents.
    Remembering the previous over-ratio set turns the guard into one grace run
    rather than a permanent block.

    The memory is process-local by design; a restart just costs one more grace
    run, which is not worth persisting for.
    """
    if not _soft_delete_guard_tripped(len(rows_to_delete), live_count):
        _previous_large_delete_sets.pop(media_type, None)
        return False
    signature = frozenset(row.id for row in rows_to_delete)
    if _previous_large_delete_sets.get(media_type) == signature:
        _previous_large_delete_sets.pop(media_type, None)
        return False
    _previous_large_delete_sets[media_type] = signature
    return True


def _tvdb_sorts_first(a: str, b: str) -> bool:
    """True when tvdb id `a` orders before `b`.

    Numeric when both are numeric, which they are in practice, so that "9001"
    sorts before "10001" rather than after it. The ordering only has to be
    stable, not meaningful.
    """
    if a.isdigit() and b.isdigit():
        return int(a) < int(b)
    return a < b


async def _apply_soft_deletes(
    session: AsyncSession,
    rows: Sequence[_RowT],
    media_type: MediaType,
) -> list[int]:
    """Tombstone rows and remove the derived rows hanging off them.

    The medium itself is kept so its metadata can be reused if it reappears;
    only `removed_at` marks it gone. Does not commit - the caller decides when.

    Returns the tombstoned row ids.
    """
    if not rows:
        return []

    now = datetime.now(UTC)
    deleted_ids: list[int] = []
    for row in rows:
        row.removed_at = now
        row.added_at = None
        row.arr_added_at = None
        deleted_ids.append(row.id)
        LOG.debug(f"Soft-deleted: {row.title} ({row.tmdb_id})")

    if media_type is MediaType.MOVIE:
        candidate_col = ReclaimCandidate.movie_id
        protected_col = ProtectedMedia.movie_id
    else:
        candidate_col = ReclaimCandidate.series_id
        protected_col = ProtectedMedia.series_id

    # Delete only what the system can rebuild. Candidates come from the reclaim
    # scan and rule-sourced protections from the rule task, so both regenerate.
    # Manual protections and protection requests are user intent that nothing can
    # reconstruct, and the medium itself is only soft-deleted, so they stay and are
    # still attached if the row is restored.
    await session.execute(
        sql_delete(ReclaimCandidate).where(candidate_col.in_(deleted_ids))
    )
    await session.execute(
        sql_delete(ProtectedMedia).where(
            protected_col.in_(deleted_ids),
            ProtectedMedia.source == "rule",
        )
    )
    LOG.debug(
        f"Cleaned up candidates and rule protections for {len(deleted_ids)} "
        f"soft-deleted {media_type.value} rows"
    )
    return deleted_ids


def _is_media_server_type(service: Service) -> TypeGuard[MediaServerType]:
    return service in MEDIA_SERVERS


def _path_tail(path: str | None, depth: int) -> str | None:
    """Extract the last `depth` segments of a file path (normalized for consistent matching)."""
    if not path:
        return None
    normalized = normalize_fpath(path, strip_ending_slash=True, lower=True)
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < depth:
        return None
    return "/".join(parts[-depth:])


def _episode_file_names(paths: list[str] | None) -> set[str]:
    names: set[str] = set()
    for path in paths or []:
        tail = _path_tail(path, 1)
        if tail:
            names.add(tail)
    return names


def _duration_close(left: float | None, right: float | None) -> bool:
    """Determine if two durations are close enough to be considered a match, allowing for some variance."""
    if left is None or right is None:
        return False
    return abs(left - right) <= 2000


def _as_naive_utc(value: datetime | None) -> datetime | None:
    """Normalize a provider timestamp to the naive UTC the columns store.

    Media servers hand back timezone-aware values while SQLite reads back naive
    ones, so comparing the two directly raises "can't compare offset-naive and
    offset-aware datetimes". Converting on the way in keeps every stored value
    on the same footing.
    """
    if value is None:
        return None
    return ensure_utc(value).replace(tzinfo=None)


def _merge_last_viewed(
    current: datetime | None, incoming: datetime | None
) -> datetime | None:
    """Merge last viewed timestamps by taking the max (most recent) value, ignoring timezone differences."""
    if incoming is not None and incoming.tzinfo is not None:
        incoming = incoming.replace(tzinfo=None)
    return (
        max(filter(None, [current, incoming]))
        if (current is not None or incoming is not None)
        else None
    )


def _play_entry(
    play_counts: dict[int, tuple[int, datetime | None]], source_item_id: str
) -> tuple[int, datetime | None] | None:
    """Retrieve play count and last played timestamp for a given source item ID."""
    try:
        return play_counts.get(int(source_item_id))
    except (TypeError, ValueError):
        return None


async def _get_configured_media_servers(
    session: AsyncSession,
    service: MediaServerType | None = None,
) -> list[ServiceConfig]:
    """Return enabled/valid configured media servers, optionally filtered by service."""
    query = select(ServiceConfig).where(
        ServiceConfig.service_type.in_(MEDIA_SERVERS),
        ServiceConfig.enabled.is_(True),
        ServiceConfig.base_url.isnot(None),
        ServiceConfig.api_key.isnot(None),
    )
    if service is not None:
        query = query.where(ServiceConfig.service_type == service)
    result = await session.execute(query)
    return list(result.scalars().all())


async def _get_main_media_server(session: AsyncSession) -> ServiceConfig | None:
    """Return the designated main media server config, or None if not set."""
    result = await session.execute(
        select(ServiceConfig).where(
            ServiceConfig.service_type.in_(MEDIA_SERVERS),
            ServiceConfig.is_main.is_(True),
            ServiceConfig.enabled.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _mark_service_config_synced(service_config_id: int) -> None:
    """Stamp one media server with the moment its own sync finished.

    Per config, not per type: a full sync stamps main, and each linked-data sync
    stamps only the server it pulled from, so two servers of the same type never
    report each other's time.
    """
    async with async_db() as session:
        await session.execute(
            sql_update(ServiceConfig)
            .where(ServiceConfig.id == service_config_id)
            .values(last_synced_at=datetime.now(UTC).replace(tzinfo=None))
        )
        await session.commit()


async def _get_media_service_instance(
    config: ServiceConfig,
) -> JellyfinService | EmbyService | PlexService | None:
    """Return the initialized media service client for a specific ServiceConfig row."""
    service_instance = service_manager.get_media_server(config.service_type, config.id)
    if not service_instance:
        LOG.error(f"Service {config.service_type} (config {config.id}) not initialized")
        return None
    if not isinstance(service_instance, (JellyfinService, EmbyService, PlexService)):
        LOG.error(f"Service {config.service_type} is not a media server")
        return None
    return service_instance


async def _replace_supplemental_matches(
    session: AsyncSession,
    source_service_config_id: int,
    media_type: MediaType,
    matches: list[SupplementalMediaMatch],
) -> None:
    """Replace all supplemental matches for a given source config and media type with a new set of matches."""
    await session.execute(
        sql_delete(SupplementalMediaMatch).where(
            SupplementalMediaMatch.source_service_config_id == source_service_config_id,
            SupplementalMediaMatch.media_type == media_type,
        )
    )
    session.add_all(matches)


async def _clear_supplemental_matches(
    source_service_config_id: int,
    media_type: MediaType | None = None,
) -> None:
    """Clear supplemental matches for a given source config, optionally filtered by media type."""
    async with async_db() as session:
        query = sql_delete(SupplementalMediaMatch).where(
            SupplementalMediaMatch.source_service_config_id == source_service_config_id
        )
        if media_type is not None:
            query = query.where(SupplementalMediaMatch.media_type == media_type)
        await session.execute(query)
        await session.commit()


async def _prune_supplemental_matches(
    active_linked_service_config_ids: set[int],
) -> None:
    """Prune supplemental matches for inactive linked media-server configs."""
    async with async_db() as session:
        if active_linked_service_config_ids:
            query = sql_delete(SupplementalMediaMatch).where(
                SupplementalMediaMatch.source_service_config_id.not_in(
                    active_linked_service_config_ids
                )
            )
        else:
            query = sql_delete(SupplementalMediaMatch)
        await session.execute(query)
        await session.commit()


async def _build_movie_supplemental_matches(
    session: AsyncSession,
    config: ServiceConfig,
    movies: list[AggregatedMovieData],
) -> list[SupplementalMediaMatch]:
    """Build supplemental matches for movies."""
    source_service = config.service_type
    rows = (
        await session.execute(
            select(MovieVersion, Movie)
            .join(Movie, MovieVersion.movie_id == Movie.id)
            .where(
                Movie.removed_at.is_(None),
                MovieVersion.path.is_not(None),
            )
        )
    ).all()

    main_by_key: dict[tuple[int, str], list[tuple[MovieVersion, Movie]]] = {}
    for version, movie in rows:
        tail = _path_tail(version.path, 2)
        if not tail:
            continue
        main_by_key.setdefault((movie.tmdb_id, tail), []).append((version, movie))

    matches_by_item: dict[str, SupplementalMediaMatch] = {}
    ambiguous = 0
    for movie in movies:
        tmdb_id = movie.external_ids.tmdb if movie.external_ids else None
        if not tmdb_id:
            continue
        for version in movie.versions:
            tail = _path_tail(version.path, 2)
            if not tail:
                continue
            candidates = main_by_key.get((tmdb_id, tail), [])
            candidate_movie_ids = {candidate.id for _, candidate in candidates}
            if len(candidate_movie_ids) != 1:
                if candidates:
                    ambiguous += 1
                continue
            main_version, main_movie = candidates[0]
            signals: dict[str, Any] = {
                "match": "movie_parent_file",
                "tmdb_id": tmdb_id,
            }
            if main_version.size and version.size and main_version.size == version.size:
                signals["size"] = "exact"
            if _duration_close(main_version.duration, version.duration):
                signals["duration"] = "close"
            matches_by_item[version.service_item_id] = SupplementalMediaMatch(
                source_service=source_service,
                source_service_config_id=config.id,
                source_item_id=version.service_item_id,
                media_type=MediaType.MOVIE,
                movie_id=main_movie.id,
                source_media_id=version.service_media_id,
                path_tail=tail,
                confidence=100,
                signals=signals,
            )

    if ambiguous:
        LOG.debug(
            f"Skipped {ambiguous} ambiguous supplemental movie path matches for "
            f"{source_service.value}"
        )
    return list(matches_by_item.values())


async def _build_series_supplemental_matches(
    session: AsyncSession,
    config: ServiceConfig,
    series_items: list[AggregatedSeriesData],
) -> list[SupplementalMediaMatch]:
    """Build supplemental matches for series."""
    source_service = config.service_type
    ref_rows = (
        await session.execute(
            select(SeriesServiceRef, Series)
            .join(Series, SeriesServiceRef.series_id == Series.id)
            .where(Series.removed_at.is_(None))
        )
    ).all()

    series_by_tmdb: dict[int, Series] = {}
    series_by_tail: dict[tuple[int, str], list[Series]] = {}
    for ref, series in ref_rows:
        series_by_tmdb[series.tmdb_id] = series
        tail = _path_tail(ref.path, 1)
        if tail:
            series_by_tail.setdefault((series.tmdb_id, tail), []).append(series)

    season_rows = (
        await session.execute(
            select(Season, Series)
            .join(Series, Season.series_id == Series.id)
            .where(Series.removed_at.is_(None))
        )
    ).all()
    seasons_by_key: dict[tuple[int, int], Season] = {}
    season_tails: dict[int, str] = {}
    season_episode_names: dict[int, set[str]] = {}
    for season, series in season_rows:
        seasons_by_key[(series.id, season.season_number)] = season
        tail = _path_tail(season.path, 2)
        if tail:
            season_tails[season.id] = tail
        names = _episode_file_names(season.episode_paths)
        if names:
            season_episode_names[season.id] = names

    matches_by_item: dict[str, SupplementalMediaMatch] = {}
    ambiguous = 0
    for source_series in series_items:
        tmdb_id = (
            source_series.external_ids.tmdb if source_series.external_ids else None
        )
        if not tmdb_id:
            continue

        local_series: Series | None = None
        source_series_tail = _path_tail(source_series.path, 1)
        if source_series_tail:
            candidates = series_by_tail.get((tmdb_id, source_series_tail), [])
            candidate_ids = {candidate.id for candidate in candidates}
            if len(candidate_ids) == 1:
                local_series = candidates[0]
            elif candidates:
                ambiguous += 1
                continue
        if local_series is None:
            local_series = series_by_tmdb.get(tmdb_id)
        if local_series is None:
            continue

        signals: dict[str, Any] = {"tmdb_id": tmdb_id}
        if source_series_tail:
            signals["match"] = "series_folder"
            signals["path_tail"] = source_series_tail
        else:
            signals["match"] = "tmdb_fallback"

        matches_by_item[source_series.id] = SupplementalMediaMatch(
            source_service=source_service,
            source_service_config_id=config.id,
            source_item_id=source_series.id,
            media_type=MediaType.SERIES,
            series_id=local_series.id,
            path_tail=source_series_tail,
            confidence=100 if source_series_tail else 90,
            signals=signals,
        )

        for source_season in source_series.season_data:
            if not source_season.service_season_id:
                continue
            local_season = seasons_by_key.get(
                (local_series.id, source_season.season_number)
            )
            if local_season is None:
                continue

            source_season_tail = _path_tail(source_season.path, 2)
            local_season_tail = season_tails.get(local_season.id)
            source_episode_names = _episode_file_names(source_season.episode_paths)
            local_episode_names = season_episode_names.get(local_season.id, set())
            episode_overlap = bool(source_episode_names & local_episode_names)

            if source_season_tail and local_season_tail:
                if source_season_tail != local_season_tail and not episode_overlap:
                    continue
                season_match = (
                    "season_folder"
                    if source_season_tail == local_season_tail
                    else "episode_filename_overlap"
                )
            elif not episode_overlap:
                continue
            else:
                season_match = "episode_filename_overlap"

            matches_by_item[source_season.service_season_id] = SupplementalMediaMatch(
                source_service=source_service,
                source_service_config_id=config.id,
                source_item_id=source_season.service_season_id,
                media_type=MediaType.SERIES,
                series_id=local_series.id,
                season_id=local_season.id,
                path_tail=source_season_tail,
                confidence=100,
                signals={
                    "match": season_match,
                    "tmdb_id": tmdb_id,
                    "season_number": source_season.season_number,
                },
            )

    if ambiguous:
        LOG.debug(
            f"Skipped {ambiguous} ambiguous supplemental series path matches for "
            f"{source_service.value}"
        )
    return list(matches_by_item.values())


def _needs_metadata_refresh(obj: Movie | Series, media_type: MediaType) -> bool:
    """Determine if TMDB metadata needs refreshing.

    Refresh if:
    - Never refreshed before
    - Missing critical display fields (rating, popularity, backdrop, poster)
    - Been >30 days AND release date is within last 6 months (recent releases get updates)
    """
    # never refreshed - always refresh
    if not obj.last_metadata_refresh_at:
        return True

    # one time collection backfill for movie records created before this field existed
    if media_type is MediaType.MOVIE and not getattr(
        obj, "tmdb_collection_checked", False
    ):
        return True

    # cache time now
    time_now = datetime.now(UTC)

    # check for missing critical fields if not recently checked
    if (time_now - obj.last_metadata_refresh_at.replace(tzinfo=UTC)).days > 7 and (
        not obj.vote_average
        or not obj.popularity
        or not obj.backdrop_url
        or not obj.poster_url
    ):
        return True

    # check if it's a recent release that might need updates
    if isinstance(obj, Movie):
        release_date = obj.tmdb_release_date
    else:
        release_date = obj.tmdb_first_air_date

    if release_date:
        days_since_release = (time_now - release_date.replace(tzinfo=UTC)).days
        days_since_refresh = (
            time_now - obj.last_metadata_refresh_at.replace(tzinfo=UTC)
        ).days

        # if released within last 6 months and not refreshed in 30 days
        if days_since_release <= 180 and days_since_refresh > 30:
            return True

    return False


def _rollup_series_media_from_seasons(
    season_data: list[AggregatedSeasonData],
) -> dict[str, Any]:
    """Roll up minimal media aggregate signals from seasons to series-level values."""
    if not season_data:
        return {
            "has_hdr": None,
            "has_dolby_vision": None,
            "max_video_width": None,
            "max_video_height": None,
            "video_codec_families": None,
            "audio_codec_families": None,
            "max_audio_channels": None,
            "subtitle_languages": None,
        }

    video_families: set[str] = set()
    audio_families: set[str] = set()
    subtitle_langs: set[str] = set()
    max_width: int | None = None
    max_height: int | None = None
    max_audio_channels: int | None = None
    has_hdr = False
    has_dolby_vision = False

    for sd in season_data:
        if sd.has_hdr:
            has_hdr = True
        if sd.has_dolby_vision:
            has_dolby_vision = True
        if sd.max_video_width is not None:
            max_width = (
                sd.max_video_width
                if max_width is None
                else max(max_width, sd.max_video_width)
            )
        if sd.max_video_height is not None:
            max_height = (
                sd.max_video_height
                if max_height is None
                else max(max_height, sd.max_video_height)
            )
        if sd.max_audio_channels is not None:
            max_audio_channels = (
                sd.max_audio_channels
                if max_audio_channels is None
                else max(max_audio_channels, sd.max_audio_channels)
            )
        if sd.video_codec_families:
            video_families.update(sd.video_codec_families)
        if sd.audio_codec_families:
            audio_families.update(sd.audio_codec_families)
        if sd.subtitle_languages:
            subtitle_langs.update(sd.subtitle_languages)

    return {
        "has_hdr": True if has_hdr else None,
        "has_dolby_vision": True if has_dolby_vision else None,
        "max_video_width": max_width,
        "max_video_height": max_height,
        "video_codec_families": sorted(video_families) or None,
        "audio_codec_families": sorted(audio_families) or None,
        "max_audio_channels": max_audio_channels,
        "subtitle_languages": sorted(subtitle_langs) or None,
    }


async def _sync_seasons(
    session: AsyncSession,
    series_id: int,
    season_data: list[AggregatedSeasonData],
    service_type: Service,
) -> None:
    """Upsert season rows for a series from freshly-fetched media server data."""
    result = await session.execute(select(Season).where(Season.series_id == series_id))
    existing: dict[int, Season] = {s.season_number: s for s in result.scalars().all()}

    incoming_season_numbers: set[int] = set()
    for sd in season_data:
        incoming_season_numbers.add(sd.season_number)
        if sd.season_number in existing:
            s = existing[sd.season_number]
            s.size = sd.size
            s.episode_count = sd.episode_count
            s.view_count = sd.view_count
            # Normalized on the way in so these never mix with the naive values
            # SQLite reads back, which is what broke episode syncs.
            s.last_viewed_at = _as_naive_utc(sd.last_viewed_at)
            s.air_date = sd.air_date
            s.added_at = _as_naive_utc(sd.added_at)
            s.has_hdr = sd.has_hdr
            s.has_dolby_vision = sd.has_dolby_vision
            s.max_video_width = sd.max_video_width
            s.max_video_height = sd.max_video_height
            s.video_codec_families = sd.video_codec_families
            s.audio_codec_families = sd.audio_codec_families
            s.audio_languages = sd.audio_languages
            s.max_audio_channels = sd.max_audio_channels
            s.subtitle_languages = sd.subtitle_languages
            s.path = sd.path
            s.episode_paths = sd.episode_paths
            s.media_server_user_rating = sd.media_server_user_rating
            if sd.service_season_id:
                if service_type is Service.JELLYFIN:
                    s.jellyfin_season_id = sd.service_season_id
                elif service_type is Service.EMBY:
                    s.emby_season_id = sd.service_season_id
                else:
                    s.plex_season_rating_key = sd.service_season_id
        else:
            jellyfin_id = None
            emby_id = None
            plex_key = None
            if sd.service_season_id:
                if service_type is Service.JELLYFIN:
                    jellyfin_id = sd.service_season_id
                elif service_type is Service.EMBY:
                    emby_id = sd.service_season_id
                else:
                    plex_key = sd.service_season_id
            new_season = Season(
                series_id=series_id,
                season_number=sd.season_number,
                size=sd.size,
                episode_count=sd.episode_count,
                view_count=sd.view_count,
                last_viewed_at=_as_naive_utc(sd.last_viewed_at),
                air_date=sd.air_date,
                has_hdr=sd.has_hdr,
                has_dolby_vision=sd.has_dolby_vision,
                max_video_width=sd.max_video_width,
                max_video_height=sd.max_video_height,
                video_codec_families=sd.video_codec_families,
                audio_codec_families=sd.audio_codec_families,
                audio_languages=sd.audio_languages,
                max_audio_channels=sd.max_audio_channels,
                subtitle_languages=sd.subtitle_languages,
                jellyfin_season_id=jellyfin_id,
                emby_season_id=emby_id,
                plex_season_rating_key=plex_key,
                media_server_user_rating=sd.media_server_user_rating,
            )
            new_season.added_at = _as_naive_utc(sd.added_at)
            new_season.path = sd.path
            new_season.episode_paths = sd.episode_paths
            session.add(new_season)

    # remove seasons no longer in the media server
    removed_season_ids = [
        season_obj.id
        for season_number, season_obj in existing.items()
        if season_number not in incoming_season_numbers
    ]
    if removed_season_ids:
        # clean up orphaned candidates and protection entries before deleting seasons
        await session.execute(
            sql_delete(ReclaimCandidate).where(
                ReclaimCandidate.season_id.in_(removed_season_ids)
            )
        )
        await session.execute(
            sql_delete(ProtectedMedia).where(
                ProtectedMedia.season_id.in_(removed_season_ids)
            )
        )
        await session.execute(
            sql_delete(ProtectionRequest).where(
                ProtectionRequest.season_id.in_(removed_season_ids),
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            )
        )
        await session.execute(
            sql_update(ProtectionRequest)
            .where(
                ProtectionRequest.season_id.in_(removed_season_ids),
                ProtectionRequest.status != ProtectionRequestStatus.PENDING,
            )
            .values(season_id=None, episode_id=None)
        )
        await session.execute(
            sql_delete(DeleteRequest).where(
                DeleteRequest.season_id.in_(removed_season_ids),
                DeleteRequest.status == ProtectionRequestStatus.PENDING,
            )
        )
        await session.execute(
            sql_update(DeleteRequest)
            .where(
                DeleteRequest.season_id.in_(removed_season_ids),
                DeleteRequest.status != ProtectionRequestStatus.PENDING,
            )
            .values(season_id=None, episode_id=None)
        )
        await session.execute(
            sql_delete(Episode).where(Episode.season_id.in_(removed_season_ids))
        )
        for season_number, season_obj in existing.items():
            if season_number not in incoming_season_numbers:
                await session.delete(season_obj)

    if not season_data:
        # With no physical seasons left, a whole-series candidate is no longer
        # actionable. Keep the Series catalog row and any series-level protection.
        await session.execute(
            sql_delete(ReclaimCandidate).where(
                ReclaimCandidate.series_id == series_id,
                ReclaimCandidate.season_id.is_(None),
                ReclaimCandidate.episode_id.is_(None),
            )
        )

    # upsert episode rows for seasons that have episode_data
    # flush first so new Season rows get their IDs
    await session.flush()
    for sd in season_data:
        if not sd.episode_data:
            continue
        # resolve the season id (may be newly created)
        season_id: int | None = None
        if sd.season_number in existing:
            season_id = existing[sd.season_number].id
        else:
            # newly added season (look it up)
            result2 = await session.execute(
                select(Season).where(
                    Season.series_id == series_id,
                    Season.season_number == sd.season_number,
                )
            )
            new_s = result2.scalar_one_or_none()
            if new_s:
                season_id = new_s.id
        if season_id is None:
            continue
        await _upsert_episodes(session, season_id, sd.episode_data, service_type)


async def _upsert_episodes(
    session: AsyncSession,
    season_id: int,
    episode_data: list[AggregatedEpisodeData],
    service_type: Service,
    *,
    remove_stale: bool = True,
    backfill_ids: bool = True,
) -> None:
    """Upsert Episode rows for a season from freshly-fetched media server episode data.

    Args:
        remove_stale: When True (default), delete episodes no longer reported by the
            service. Set to False for supplemental/linked-server calls where the service
            may only have partial season coverage and should not delete episodes written
            by the primary server.
        backfill_ids: When True (default), write the service-specific episode ID
            column (plex_rating_key/jellyfin_episode_id/emby_episode_id) matching
            service_type. There is exactly one such column per service *type*, not
            per config, so this must be False when the caller is a linked (non-main)
            server whose type matches the main server's type - otherwise its IDs
            would silently overwrite the main server's IDs, which media-server
            delete operations rely on.
    """
    # Flush any pending inserts (e.g. from a prior _upsert_episodes call for the same
    # season_id) so that the query below reflects the full current state. With
    # autoflush=False this is required to avoid any UNIQUE-constraint violations when
    # season_data contains duplicate season numbers.
    await session.flush()
    result = await session.execute(
        select(Episode).where(Episode.season_id == season_id)
    )
    existing_eps: dict[int, Episode] = {
        e.episode_number: e for e in result.scalars().all()
    }
    incoming_nums: set[int] = set()
    for ep in episode_data:
        incoming_nums.add(ep.episode_number)
        if ep.episode_number in existing_eps:
            e = existing_eps[ep.episode_number]
            # merge watch data: take max view_count and most recent last_viewed_at
            e.view_count = max(e.view_count or 0, ep.view_count)
            if ep.last_viewed_at is not None:
                incoming_viewed = _as_naive_utc(ep.last_viewed_at)
                existing_viewed = _as_naive_utc(e.last_viewed_at)
                if existing_viewed is None or (
                    incoming_viewed is not None and incoming_viewed > existing_viewed
                ):
                    e.last_viewed_at = incoming_viewed
            if ep.added_at is not None:
                # Earliest wins, mirroring the season rollup, so a re-scan that
                # reports a newer date cannot retroactively invalidate a watch.
                incoming_added = _as_naive_utc(ep.added_at)
                existing_added = _as_naive_utc(e.added_at)
                if incoming_added is not None and (
                    existing_added is None or incoming_added < existing_added
                ):
                    e.added_at = incoming_added
            if ep.air_date is not None and e.air_date is None:
                e.air_date = ep.air_date
            if ep.name is not None and e.name is None:
                e.name = ep.name
            if ep.size is not None:
                e.size = ep.size
            if ep.path is not None:
                e.path = ep.path
            if ep.runtime_seconds is not None:
                e.runtime = ep.runtime_seconds
            if ep.media_server_user_rating is not None:
                e.media_server_user_rating = ep.media_server_user_rating
            if backfill_ids:
                if service_type is Service.PLEX and ep.plex_rating_key:
                    e.plex_rating_key = ep.plex_rating_key
                elif service_type is Service.JELLYFIN and ep.jellyfin_episode_id:
                    e.jellyfin_episode_id = ep.jellyfin_episode_id
                elif service_type is Service.EMBY and ep.emby_episode_id:
                    e.emby_episode_id = ep.emby_episode_id
        else:
            new_ep = Episode(
                season_id=season_id,
                episode_number=ep.episode_number,
                name=ep.name,
                air_date=ep.air_date,
                size=ep.size,
                path=ep.path,
                view_count=ep.view_count,
                last_viewed_at=ep.last_viewed_at,
                plex_rating_key=ep.plex_rating_key if backfill_ids else None,
                jellyfin_episode_id=ep.jellyfin_episode_id if backfill_ids else None,
                emby_episode_id=ep.emby_episode_id if backfill_ids else None,
                media_server_user_rating=ep.media_server_user_rating,
                runtime=ep.runtime_seconds,
            )
            new_ep.added_at = _as_naive_utc(ep.added_at)
            session.add(new_ep)
            # We have to register in existing_eps so a duplicate ep_number later in the
            # same episode_data list hits the update branch rather than creating a second
            # pending INSERT (which would violate the UNIQUE constraint on flush)!
            existing_eps[ep.episode_number] = new_ep

    # remove episodes no longer present on the media server
    if remove_stale:
        for ep_num, ep_obj in existing_eps.items():
            if ep_num not in incoming_nums:
                await session.execute(
                    sql_delete(ReclaimCandidate).where(
                        ReclaimCandidate.episode_id == ep_obj.id
                    )
                )
                await session.execute(
                    sql_delete(ProtectedMedia).where(
                        ProtectedMedia.episode_id == ep_obj.id
                    )
                )
                await session.execute(
                    sql_delete(ProtectionRequest).where(
                        ProtectionRequest.episode_id == ep_obj.id,
                        ProtectionRequest.status == ProtectionRequestStatus.PENDING,
                    )
                )
                await session.execute(
                    sql_update(ProtectionRequest)
                    .where(
                        ProtectionRequest.episode_id == ep_obj.id,
                        ProtectionRequest.status != ProtectionRequestStatus.PENDING,
                    )
                    .values(episode_id=None)
                )
                await session.execute(
                    sql_delete(DeleteRequest).where(
                        DeleteRequest.episode_id == ep_obj.id,
                        DeleteRequest.status == ProtectionRequestStatus.PENDING,
                    )
                )
                await session.execute(
                    sql_update(DeleteRequest)
                    .where(
                        DeleteRequest.episode_id == ep_obj.id,
                        DeleteRequest.status != ProtectionRequestStatus.PENDING,
                    )
                    .values(episode_id=None)
                )
                await session.delete(ep_obj)


async def _upsert_series_service_ref(
    session: AsyncSession,
    series_id: int,
    data: AggregatedSeriesData,
) -> None:
    """Upsert the service reference row for a series (one row per service)."""
    result = await session.execute(
        select(SeriesServiceRef).where(
            SeriesServiceRef.series_id == series_id,
            SeriesServiceRef.service == data.service,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.service_id = data.id
        existing.library_id = data.library_id
        existing.library_name = data.library_name
        existing.path = data.path
        existing.media_server_collection_names = data.media_server_collection_names
        existing.media_server_genres = data.media_server_genres
    else:
        session.add(
            SeriesServiceRef(
                series_id=series_id,
                service=data.service,
                service_id=data.id,
                library_id=data.library_id,
                library_name=data.library_name,
                path=data.path,
                media_server_collection_names=data.media_server_collection_names,
                media_server_genres=data.media_server_genres,
            )
        )


def _make_fp(
    svc: object,
    width: int | None,
    height: int | None,
    codec: str | None,
    hdr: bool | None,
    dv: bool | None,
    size: int,
    container: str | None,
) -> tuple[Any, ...] | None:
    """Build a fingerprint map of existing versions for rename resilient fallback matching.
    Fingerprint covers fields that are stable across file renames but change on re-encode.
    Entries with duplicate fingerprints are marked None (ambiguous - we skip to avoid a mis-match)
    """
    if not (size and width and height):
        return None
    return (svc, width, height, codec, hdr, dv, size, container)


async def _upsert_movie_versions(
    session: AsyncSession,
    db_movie: Movie,
    versions: list[MovieVersionData],
) -> None:
    """Upsert per-file versions for a movie from the main server, pruning any stale entries."""
    result = await session.execute(
        select(MovieVersion).where(MovieVersion.movie_id == db_movie.id)
    )
    existing: dict[tuple[Any, ...], MovieVersion] = {
        (v.service, v.service_media_id): v for v in result.scalars().all()
    }

    fp_map: dict[tuple[Any, ...], MovieVersion | None] = {}
    for ev in existing.values():
        fp = _make_fp(
            ev.service,
            ev.video_width,
            ev.video_height,
            ev.video_codec,
            ev.video_hdr,
            ev.video_dolby_vision,
            ev.size,
            ev.container,
        )
        if fp is None:
            continue
        # None = ambiguous, skip
        fp_map[fp] = None if fp in fp_map else ev

    incoming_keys: set[tuple[Any, ...]] = set()
    for ver in versions:
        key = (ver.service, ver.service_media_id)
        incoming_keys.add(key)
        if key in existing:
            ev = existing[key]
        else:
            # Primary key miss (try fingerprint fallback before creating a new row).
            # This handles Jellyfin/Emby renames where service_media_id changes but the
            # physical file (and all its codec/resolution metadata) is identical.
            fp = _make_fp(
                ver.service,
                ver.video_width,
                ver.video_height,
                ver.video_codec,
                ver.video_hdr,
                ver.video_dolby_vision,
                ver.size,
                ver.container,
            )
            matched_ev = fp_map.get(fp) if fp else None
            if fp is not None and matched_ev is not None:
                # rename detected: update service IDs in place so all FK references
                # (protections, requests, candidates) pointing at this row are preserved.
                old_key = (matched_ev.service, matched_ev.service_media_id)
                matched_ev.service_media_id = ver.service_media_id
                matched_ev.service_item_id = ver.service_item_id
                # prevent the prune step from deleting this row
                incoming_keys.add(old_key)
                # consumed (prevent re-matching another version to it)
                fp_map[fp] = None
                ev = matched_ev
            else:
                session.add(
                    MovieVersion(
                        movie_id=db_movie.id,
                        service=ver.service,
                        service_item_id=ver.service_item_id,
                        service_media_id=ver.service_media_id,
                        library_id=ver.library_id,
                        library_name=ver.library_name,
                        path=ver.path,
                        size=ver.size,
                        added_at=ver.added_at,
                        file_name=ver.file_name,
                        container=ver.container,
                        duration=ver.duration,
                        video_track_count=ver.video_track_count,
                        video_codec=ver.video_codec,
                        video_codec_family=ver.video_codec_family,
                        video_hdr=ver.video_hdr,
                        video_dolby_vision=ver.video_dolby_vision,
                        video_dolby_vision_profile=ver.video_dolby_vision_profile,
                        video_bitrate=ver.video_bitrate,
                        video_bit_depth=ver.video_bit_depth,
                        video_width=ver.video_width,
                        video_height=ver.video_height,
                        video_resolution=ver.video_resolution,
                        video_color_primaries=ver.video_color_primaries,
                        video_color_space=ver.video_color_space,
                        video_color_transfer=ver.video_color_transfer,
                        video_fps=ver.video_fps,
                        audio_count=ver.audio_count,
                        audio_languages=ver.audio_languages,
                        audio_codec=ver.audio_codec,
                        audio_codec_family=ver.audio_codec_family,
                        audio_title=ver.audio_title,
                        audio_language=ver.audio_language,
                        audio_channels=ver.audio_channels,
                        audio_channel_layout=ver.audio_channel_layout,
                        audio_bitrate=ver.audio_bitrate,
                        audio_sample_rate=ver.audio_sample_rate,
                        subtitle_count=ver.subtitle_count,
                        subtitle_has_forced=ver.subtitle_has_forced,
                        subtitle_languages=ver.subtitle_languages,
                        has_chapters=ver.has_chapters,
                        media_server_collection_names=ver.media_server_collection_names,
                        media_server_genres=ver.media_server_genres,
                        media_server_user_rating=ver.media_server_user_rating,
                    )
                )
                continue

        # update all fields on ev (reached for both primary-key match and fingerprint match)
        ev.library_id = ver.library_id
        ev.library_name = ver.library_name
        ev.path = ver.path
        ev.size = ver.size
        ev.file_name = ver.file_name
        ev.container = ver.container
        ev.duration = ver.duration
        ev.video_track_count = ver.video_track_count
        ev.video_codec = ver.video_codec
        ev.video_codec_family = ver.video_codec_family
        ev.video_hdr = ver.video_hdr
        ev.video_dolby_vision = ver.video_dolby_vision
        ev.video_dolby_vision_profile = ver.video_dolby_vision_profile
        ev.video_bitrate = ver.video_bitrate
        ev.video_bit_depth = ver.video_bit_depth
        ev.video_width = ver.video_width
        ev.video_height = ver.video_height
        ev.video_resolution = ver.video_resolution
        ev.video_color_primaries = ver.video_color_primaries
        ev.video_color_space = ver.video_color_space
        ev.video_color_transfer = ver.video_color_transfer
        ev.video_fps = ver.video_fps
        ev.audio_count = ver.audio_count
        ev.audio_languages = ver.audio_languages
        ev.audio_codec = ver.audio_codec
        ev.audio_codec_family = ver.audio_codec_family
        ev.audio_title = ver.audio_title
        ev.audio_language = ver.audio_language
        ev.audio_channels = ver.audio_channels
        ev.audio_channel_layout = ver.audio_channel_layout
        ev.audio_bitrate = ver.audio_bitrate
        ev.audio_sample_rate = ver.audio_sample_rate
        ev.subtitle_count = ver.subtitle_count
        ev.subtitle_has_forced = ver.subtitle_has_forced
        ev.subtitle_languages = ver.subtitle_languages
        ev.has_chapters = ver.has_chapters
        ev.media_server_collection_names = ver.media_server_collection_names
        ev.media_server_genres = ver.media_server_genres
        ev.media_server_user_rating = ver.media_server_user_rating
        if ver.added_at:
            ev.added_at = ver.added_at

    # prune stale versions - all incoming versions come from the authoritative main server
    for key, ev in existing.items():
        if key not in incoming_keys:
            await session.delete(ev)

    # size = sum of incoming versions (all stale versions are being deleted)
    db_movie.size = sum(ver.size for ver in versions)


async def gather_movies(
    config: ServiceConfig | None = None,
) -> dict[int, AggregatedMovieData] | None:
    """
    Fetch movies from the main media server (or a specific config) and group by TMDB ID.
    Same movie in multiple libraries on the same server gets its versions merged.
    Watch data takes the max across libraries.

    Note: this always resolves exactly one server - either the given `config`
    or the designated main server. It never fans out across every configured
    server of a type, since only one config (main, or the explicitly given
    one) should ever contribute physical version data.
    """
    async with async_db() as session:
        target = config
        if target is None:
            # use designated main server (required)
            target = await _get_main_media_server(session)
            if not target:
                LOG.error(
                    "No main media server configured. Must have a main server designated."
                )
                return None

        service_instance = await _get_media_service_instance(target)
        if not service_instance:
            return None
        LOG.debug(f"Fetching movies from {target.service_type} at {target.base_url}")
        aggregated_movies: list[AggregatedMovieData] = (
            await service_instance.get_aggregated_movies(included_libraries=None) or []
        )
        LOG.debug(f"Fetched {len(aggregated_movies)} movies from {target.service_type}")

    # group by TMDB ID (merges same movie from multiple libraries on the same server)
    unique_movies: dict[int, AggregatedMovieData] = {}
    skipped_count = 0

    for movie in aggregated_movies:
        ext_ids = movie.external_ids
        if not ext_ids or not ext_ids.tmdb:
            skipped_count += 1
            continue

        tmdb_id = ext_ids.tmdb
        if tmdb_id not in unique_movies:
            unique_movies[tmdb_id] = movie
        else:
            existing = unique_movies[tmdb_id]
            # deduplicate by (service, service_media_id) as the same physical file can appear
            # in multiple Jellyfin/Plex/Emby libraries with identical MediaSource IDs
            seen_version_keys: set[tuple[Any, ...]] = {
                (v.service, v.service_media_id) for v in existing.versions
            }
            merged_versions = existing.versions + [
                v
                for v in movie.versions
                if (v.service, v.service_media_id) not in seen_version_keys
            ]
            lva_candidates = [
                dt for dt in [existing.last_viewed_at, movie.last_viewed_at] if dt
            ]
            merged_lva = max(lva_candidates) if lva_candidates else None
            merged_view_count = max(existing.view_count, movie.view_count)
            pbu_candidates = [
                c
                for c in [existing.played_by_user_count, movie.played_by_user_count]
                if c is not None
            ]
            unique_movies[tmdb_id] = AggregatedMovieData(
                name=existing.name,
                year=existing.year,
                external_ids=existing.external_ids,
                versions=merged_versions,
                view_count=merged_view_count,
                last_viewed_at=merged_lva,
                played_by_user_count=max(pbu_candidates) if pbu_candidates else None,
                media_server_user_rating=max(
                    rating
                    for rating in (
                        existing.media_server_user_rating,
                        movie.media_server_user_rating,
                    )
                    if rating is not None
                )
                if any(
                    rating is not None
                    for rating in (
                        existing.media_server_user_rating,
                        movie.media_server_user_rating,
                    )
                )
                else None,
            )

    if skipped_count > 0:
        LOG.warning(f"Skipped {skipped_count} movies without TMDB IDs")

    return unique_movies


# supplemental episode data from services that lost deduplication:
# tmdb_id -> list of (service, season_data_list)
_SupplementalEpisodeData = dict[int, list[tuple[Service, list[AggregatedSeasonData]]]]


def _dedupe_aggregated_series(
    aggregated_series: list[AggregatedSeriesData],
) -> tuple[dict[int, AggregatedSeriesData], _SupplementalEpisodeData]:
    """Reduce gathered series to one entry per TMDB id.

    Two cases share this code path and must not be confused. One series
    reported more than once from the same server, typically because it sits in
    two libraries, is a duplicate: keep one and stash the loser's season data as
    supplemental so its service-specific episode ids (plex_rating_key,
    jellyfin_episode_id, emby_episode_id) still reach the episodes table. Two
    different series that share a TMDB id, which happens where TMDB carries an
    umbrella entry, are not duplicates: merging them would write the loser's
    episode ids onto the winner's episode rows. Distinct tvdb ids on both sides
    are what tells them apart.

    The merge branch also covers one series reported by two services, but
    sync_series is the only caller of gather_series and always passes a single
    resolved service, so that no longer arises from a gather.
    """
    unique_series: dict[int, AggregatedSeriesData] = {}
    supplemental: _SupplementalEpisodeData = {}
    skipped_count = 0

    for series in aggregated_series:
        ext_ids = series.external_ids
        if not ext_ids or not ext_ids.tmdb:
            skipped_count += 1
            continue

        tmdb_id = ext_ids.tmdb
        if tmdb_id not in unique_series:
            unique_series[tmdb_id] = series
            continue

        existing = unique_series[tmdb_id]
        existing_tvdb = existing.external_ids.tvdb if existing.external_ids else None
        incoming_tvdb = ext_ids.tvdb

        # genuine collision: two distinct series under one TMDB id
        if existing_tvdb and incoming_tvdb and existing_tvdb != incoming_tvdb:
            if _tvdb_sorts_first(existing_tvdb, incoming_tvdb):
                winner, loser = existing, series
            else:
                winner, loser = series, existing
            LOG.warning(
                f"TMDB id {tmdb_id} is shared by two distinct series: "
                f"'{winner.name}' (tvdb {winner.external_ids.tvdb}) and "
                f"'{loser.name}' (tvdb {loser.external_ids.tvdb}). Only one can be "
                f"stored, so '{loser.name}' will not appear in Reclaimerr. Its "
                f"episode data is discarded rather than merged, which would "
                f"otherwise attach it to the wrong series."
            )
            if winner is series:
                # the displaced incumbent may have already stashed supplemental
                # under this tmdb id from an earlier cross-service merge - that
                # data belongs to the incumbent, not the winner, so drop it here
                # or it grafts onto the winner's episode rows instead
                supplemental.pop(tmdb_id, None)
            unique_series[tmdb_id] = winner
            continue

        # keep series with most recent watch date
        if series.last_viewed_at and (
            not existing.last_viewed_at
            or series.last_viewed_at > existing.last_viewed_at
        ):
            # existing loses - stash its season data as supplemental
            supplemental.setdefault(tmdb_id, []).append(
                (existing.service, existing.season_data)
            )
            unique_series[tmdb_id] = series
        else:
            # new series loses - stash its season data as supplemental
            # (covers both the equal-date case and the "existing wins" case)
            if series.last_viewed_at == existing.last_viewed_at:
                if series.added_at and (
                    not existing.added_at or series.added_at > existing.added_at
                ):
                    # new series wins on added_at - existing loses
                    supplemental.setdefault(tmdb_id, []).append(
                        (existing.service, existing.season_data)
                    )
                    unique_series[tmdb_id] = series
                    continue
            supplemental.setdefault(tmdb_id, []).append(
                (series.service, series.season_data)
            )

    if skipped_count > 0:
        LOG.warning(f"Skipped {skipped_count} series without TMDB IDs")

    return unique_series, supplemental


async def gather_series(
    config: ServiceConfig | None = None,
) -> tuple[dict[int, AggregatedSeriesData], _SupplementalEpisodeData] | None:
    """Fetch series from the main media server (or a specific config), deduplicating by TMDB ID.

    Note: mirrors `gather_movies` - resolves exactly one server (the given
    `config` or the designated main server), never every configured server of
    a type.
    """
    async with async_db() as session:
        target = config
        if target is None:
            target = await _get_main_media_server(session)
            if not target:
                LOG.error(
                    "No main media server configured. Must have a main server designated."
                )
                return None

        service_instance = await _get_media_service_instance(target)
        if not service_instance:
            return None
        LOG.debug(f"Fetching series from {target.service_type} at {target.base_url}")
        aggregated_series = (
            await service_instance.get_aggregated_series(included_libraries=None) or []
        )
        LOG.debug(f"Fetched {len(aggregated_series)} series from {target.service_type}")

    return _dedupe_aggregated_series(aggregated_series)


async def sync_movies(
    config_id: int | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
    """Sync movies from media server to database, optionally filtered by a
    specific media-server config ID.

    Returns set of synced TMDB IDs.
    """
    # resolve main server, and (if a specific config was requested) that config
    async with async_db() as _cfg:
        main_config = await _get_main_media_server(_cfg)
        target_config = (
            await _cfg.get(ServiceConfig, config_id) if config_id is not None else None
        )

    # if a specific non-main config was requested, only sync watch data from it -
    # compared by config identity, not type, so a same-type non-main config is
    # always treated as linked even when its type matches the main server's
    if config_id is not None and main_config is not None and config_id != main_config.id:
        if target_config is None:
            LOG.warning(
                f"sync_movies: config {config_id} not found or no longer configured - skipping"
            )
            return set()
        LOG.info(
            f"{target_config.service_type} (config {config_id}) is a linked server - "
            "syncing watch data only"
        )
        await sync_linked_data(target_config)
        return set()

    # resolve effective config for the full (version + watch) sync
    effective_config = target_config if config_id is not None else main_config
    if not effective_config:
        LOG.error(
            "No media server available for syncing movies. Please configure a main media server "
            "or specify a service."
        )
        return set()
    effective_service = effective_config.service_type
    LOG.info(f"Starting movie sync ({effective_service.value})...")
    start_time = datetime.now(UTC)

    aggregated_movies = await gather_movies(effective_config)
    if not aggregated_movies:
        LOG.info(f"No movies to sync from {effective_service.value}")
        return set()
    LOG.info(
        f"Gathered {len(aggregated_movies)} unique movies from {effective_service.value}"
    )

    # tmdb service instance
    tmdb_service = AsyncTMDBClient()

    try:
        async with async_db() as session:
            # get all existing movies from database
            result = await session.execute(select(Movie))
            existing_movies_list = result.scalars().all()

            # convert to dictionary keyed by tmdb_id for easier lookup
            existing_movies = {m.tmdb_id: m for m in existing_movies_list if m.tmdb_id}
            existing_by_imdb = {m.imdb_id: m for m in existing_movies_list if m.imdb_id}

            parsed_tmdb_ids: set[int] = set()
            # row primary keys touched this run. parsed_tmdb_ids cannot serve
            # this purpose: a row matched by the imdb fallback keeps its own
            # tmdb_id, which is not the id that was just parsed.
            matched_row_ids: set[int] = set()

            # iterate through aggregated movies
            batch_count = 0
            for idx, movie in enumerate[AggregatedMovieData](
                aggregated_movies.values(), start=1
            ):
                tmdb_id = int(movie.external_ids.tmdb)
                parsed_tmdb_ids.add(tmdb_id)

                # earliest added_at across all versions
                earliest_added = min(
                    (v.added_at for v in movie.versions if v.added_at), default=None
                )

                # if movie already exists, update it
                if tmdb_id in existing_movies:
                    existing_movie = existing_movies[tmdb_id]
                    matched_row_ids.add(existing_movie.id)

                    # update added_at if available
                    if earliest_added:
                        existing_movie.added_at = earliest_added
                    existing_movie.last_viewed_at = movie.last_viewed_at
                    existing_movie.view_count = movie.view_count
                    existing_movie.media_server_user_rating = (
                        movie.media_server_user_rating
                    )

                    # restore if soft-deleted
                    if existing_movie.removed_at:
                        existing_movie.removed_at = None
                        LOG.info(
                            f"Restored soft-deleted movie: {movie.name} ({tmdb_id})"
                        )

                    # refresh TMDB metadata if needed
                    if _needs_metadata_refresh(existing_movie, MediaType.MOVIE):
                        LOG.debug(
                            f"Refreshing TMDB metadata for {movie.name} ({tmdb_id})"
                        )
                        await _update_movie_tmdb_metadata(
                            existing_movie, tmdb_id, tmdb_service
                        )

                    # upsert per-file versions
                    await _upsert_movie_versions(
                        session, existing_movie, movie.versions
                    )

                # if movie doesn't exist, create new entry
                else:
                    # before inserting, check if a movie with this imdb_id already exists
                    # (can happen when TMDB returns 404 so tmdb_id lookup fails, but the
                    # movie is already stored under a different tmdb_id or same imdb_id)
                    imdb_id = movie.external_ids.imdb
                    if imdb_id and imdb_id in existing_by_imdb:
                        existing_movie = existing_by_imdb[imdb_id]
                        matched_row_ids.add(existing_movie.id)
                        LOG.info(
                            f"Movie '{movie.name}' not found by tmdb_id ({tmdb_id}) but matched "
                            f"existing record by imdb_id ({imdb_id}) - updating instead of inserting"
                        )
                        if earliest_added:
                            existing_movie.added_at = earliest_added
                        existing_movie.last_viewed_at = movie.last_viewed_at
                        existing_movie.view_count = movie.view_count
                        existing_movie.media_server_user_rating = (
                            movie.media_server_user_rating
                        )
                        if existing_movie.removed_at:
                            existing_movie.removed_at = None
                            LOG.info(
                                f"Restored soft-deleted movie: {movie.name} ({tmdb_id})"
                            )
                        if _needs_metadata_refresh(existing_movie, MediaType.MOVIE):
                            await _update_movie_tmdb_metadata(
                                existing_movie, tmdb_id, tmdb_service
                            )
                        await _upsert_movie_versions(
                            session, existing_movie, movie.versions
                        )
                    else:
                        LOG.info(f"Adding new movie: {movie.name} ({tmdb_id})")
                        initial_size = sum(v.size for v in movie.versions)
                        new_movie = Movie(
                            title=movie.name,
                            year=movie.year,
                            tmdb_id=tmdb_id,
                            size=initial_size,
                            imdb_id=imdb_id,
                            last_viewed_at=movie.last_viewed_at,
                            view_count=movie.view_count,
                            media_server_user_rating=movie.media_server_user_rating,
                        )

                        if earliest_added:
                            new_movie.added_at = earliest_added

                        await _update_movie_tmdb_metadata(
                            new_movie, tmdb_id, tmdb_service
                        )
                        session.add(new_movie)
                        await session.flush()
                        seen_new_ver_keys: set[tuple[Any, ...]] = set()
                        for ver in movie.versions:
                            key = (ver.service, ver.service_media_id)
                            if key in seen_new_ver_keys:
                                continue
                            seen_new_ver_keys.add(key)
                            session.add(
                                MovieVersion(
                                    movie_id=new_movie.id,
                                    service=ver.service,
                                    service_item_id=ver.service_item_id,
                                    service_media_id=ver.service_media_id,
                                    library_id=ver.library_id,
                                    library_name=ver.library_name,
                                    path=ver.path,
                                    size=ver.size,
                                    added_at=ver.added_at,
                                    file_name=ver.file_name,
                                    container=ver.container,
                                    duration=ver.duration,
                                    video_track_count=ver.video_track_count,
                                    video_codec=ver.video_codec,
                                    video_codec_family=ver.video_codec_family,
                                    video_hdr=ver.video_hdr,
                                    video_dolby_vision=ver.video_dolby_vision,
                                    video_dolby_vision_profile=ver.video_dolby_vision_profile,
                                    video_bitrate=ver.video_bitrate,
                                    video_bit_depth=ver.video_bit_depth,
                                    video_width=ver.video_width,
                                    video_height=ver.video_height,
                                    video_resolution=ver.video_resolution,
                                    video_color_primaries=ver.video_color_primaries,
                                    video_color_space=ver.video_color_space,
                                    video_color_transfer=ver.video_color_transfer,
                                    video_fps=ver.video_fps,
                                    audio_count=ver.audio_count,
                                    audio_languages=ver.audio_languages,
                                    audio_codec=ver.audio_codec,
                                    audio_codec_family=ver.audio_codec_family,
                                    audio_title=ver.audio_title,
                                    audio_language=ver.audio_language,
                                    audio_channels=ver.audio_channels,
                                    audio_channel_layout=ver.audio_channel_layout,
                                    audio_bitrate=ver.audio_bitrate,
                                    audio_sample_rate=ver.audio_sample_rate,
                                    subtitle_count=ver.subtitle_count,
                                    subtitle_has_forced=ver.subtitle_has_forced,
                                    subtitle_languages=ver.subtitle_languages,
                                    has_chapters=ver.has_chapters,
                                    media_server_collection_names=ver.media_server_collection_names,
                                    media_server_genres=ver.media_server_genres,
                                    media_server_user_rating=ver.media_server_user_rating,
                                )
                            )

                # commit in batches
                if idx % COMMIT_BATCH_SIZE == 0:
                    await session.commit()
                    batch_count += 1

            # commit any remaining movies
            await session.commit()
            LOG.debug(
                f"Committed {len(aggregated_movies)} movies in {batch_count + 1} batches"
            )

            # refresh per instance Radarr refs for active movies
            radarr_clients = service_manager.radarr_clients()
            if not radarr_clients and service_manager.radarr:
                radarr_clients = {0: service_manager.radarr}
            if radarr_clients:
                # purge legacy rows written before multi-arr support (service_config_id=0)
                await session.execute(
                    sql_delete(MovieArrRef).where(MovieArrRef.service_config_id == 0)
                )

                movie_rows = await session.execute(
                    select(Movie.id, Movie.tmdb_id).where(Movie.removed_at.is_(None))
                )
                movie_id_by_tmdb = {
                    tmdb_id: movie_id for movie_id, tmdb_id in movie_rows
                }
                path_mapping_result = await session.execute(
                    select(GeneralSettings.path_mappings)
                )
                path_mappings = path_mapping_result.scalars().first() or []
                version_rows = (
                    (
                        await session.execute(
                            select(MovieVersion).where(
                                MovieVersion.movie_id.in_(movie_id_by_tmdb.values())
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                versions_by_movie: dict[int, list[MovieVersion]] = {}
                for version in version_rows:
                    versions_by_movie.setdefault(version.movie_id, []).append(version)

                # accumulate resolved tag labels per movie across all Radarr instances
                movie_tags: dict[int, set[str]] = {}
                movie_arr_files: dict[int, list[tuple[int, str | None, datetime]]] = {}
                for config_id, client in radarr_clients.items():
                    await session.execute(
                        sql_delete(MovieArrRef).where(
                            MovieArrRef.service_config_id == config_id
                        )
                    )
                    all_movies = await client.get_all_movies()
                    tag_list = await client.get_tags()
                    id_to_label: dict[int, str] = {t.id: t.label for t in tag_list}
                    for arr_movie in all_movies:
                        if not arr_movie.tmdb_id:
                            continue
                        movie_id = movie_id_by_tmdb.get(arr_movie.tmdb_id)
                        if movie_id is None:
                            continue
                        arr_path = (
                            str(PurePosixPath(normalize_fpath(arr_movie.path)))
                            if arr_movie.path
                            else None
                        )
                        session.add(
                            MovieArrRef(
                                movie_id=movie_id,
                                service_config_id=config_id,
                                arr_movie_id=arr_movie.id,
                                arr_title_slug=arr_movie.title_slug,
                                arr_movie_path=arr_path,
                                tmdb_id=arr_movie.tmdb_id,
                            )
                        )
                        if arr_movie.has_file and arr_movie.file_added_at is not None:
                            movie_arr_files.setdefault(movie_id, []).append(
                                (
                                    config_id,
                                    arr_movie.file_path,
                                    arr_movie.file_added_at,
                                )
                            )
                        for tag_id in arr_movie.tags:
                            label = id_to_label.get(tag_id)
                            if label:
                                movie_tags.setdefault(movie_id, set()).add(label)

                # write resolved tags back to Movie rows
                for movie_id in movie_id_by_tmdb.values():
                    result_row = await session.get(Movie, movie_id)
                    if result_row is not None:
                        tags = movie_tags.get(movie_id)
                        result_row.arr_tags = sorted(tags) if tags else []
                        arr_files = movie_arr_files.get(movie_id, [])
                        result_row.arr_added_at = max(
                            (added_at for _, _, added_at in arr_files), default=None
                        )

                        versions = versions_by_movie.get(movie_id, [])
                        for version in versions:
                            matched_dates = [
                                added_at
                                for config_id, file_path, added_at in arr_files
                                if paths_equivalent(
                                    version.path,
                                    file_path,
                                    path_mappings,
                                    left_service_type=version.service.value,
                                    right_service_type=Service.RADARR.value,
                                    right_service_config_id=config_id,
                                )
                            ]
                            if matched_dates:
                                version.arr_added_at = max(matched_dates)
                            elif len(versions) == 1 and len(arr_files) == 1:
                                version.arr_added_at = arr_files[0][2]
                            else:
                                version.arr_added_at = None

                await session.commit()

            if allow_soft_delete:
                movies_to_delete = _select_rows_to_soft_delete(
                    existing_movies_list, matched_row_ids
                )
                live_movie_count = sum(
                    1 for movie in existing_movies_list if not movie.removed_at
                )
                if _soft_delete_blocked(
                    MediaType.MOVIE, movies_to_delete, live_movie_count
                ):
                    LOG.warning(
                        f"Skipping movie soft-delete this run: "
                        f"{len(movies_to_delete)} of {live_movie_count} live movies "
                        f"would be removed, which may be a partial response from "
                        f"{effective_service.value} rather than real deletions. If "
                        f"the next sync proposes the same movies they will be "
                        f"removed then."
                    )
                    movies_to_delete = []

                if movies_to_delete:
                    LOG.info(
                        f"Soft-deleting {len(movies_to_delete)} movies no longer in {effective_service.value}"
                    )
                    await _apply_soft_deletes(
                        session, movies_to_delete, MediaType.MOVIE
                    )
                    await session.commit()

            duration = (datetime.now(UTC) - start_time).total_seconds()
            LOG.info(
                f"Movie sync ({effective_service.value}) completed successfully in {duration:.2f}s"
            )
            return parsed_tmdb_ids
    except Exception as e:
        duration = (datetime.now(UTC) - start_time).total_seconds()
        LOG.critical(
            f"Error during movie sync ({effective_service.value}) after {duration:.2f}s: {e}",
            exc_info=True,
        )
        raise
    finally:
        await tmdb_service.session.close()


async def _update_movie_tmdb_metadata(
    movie: Movie, tmdb_id: int, tmdb_service: AsyncTMDBClient
) -> None:
    """Update movie with TMDB metadata."""
    try:
        movie_metadata = await tmdb_service.get_movie_details(tmdb_id)
        if not movie_metadata or not isinstance(movie_metadata, dict):
            LOG.warning(f"Failed to fetch TMDB metadata for movie {tmdb_id}")
            return

        ext_ids = movie_metadata.get("external_ids", {})
        movie.imdb_id = ext_ids.get("imdb_id") or None
        movie.tmdb_title = movie_metadata.get("title")
        movie.original_title = movie_metadata.get("original_title")

        collection = movie_metadata.get("belongs_to_collection")
        collection_id: int | None = None
        collection_name: str | None = None
        if isinstance(collection, dict):
            raw_collection_id = collection.get("id")
            if isinstance(raw_collection_id, int):
                collection_id = raw_collection_id
            else:
                raw_collection_text = str(raw_collection_id or "").strip()
                if raw_collection_text.isdigit():
                    collection_id = int(raw_collection_text)

            raw_collection_name = str(collection.get("name") or "").strip()
            if raw_collection_name:
                collection_name = raw_collection_name
        movie.tmdb_collection_id = collection_id
        movie.tmdb_collection_name = collection_name
        movie.tmdb_collection_checked = True

        release_date = movie_metadata.get("release_date")
        if release_date:
            parsed = datetime.strptime(release_date, "%Y-%m-%d")
            movie.tmdb_release_date = parsed
            # backfill year if media server didn't provide one
            if not movie.year:
                movie.year = parsed.year

        movie.original_language = movie_metadata.get("original_language")
        movie.homepage = movie_metadata.get("homepage")
        movie.origin_country = movie_metadata.get("origin_country")
        movie.poster_url = movie_metadata.get("poster_path")
        movie.backdrop_url = movie_metadata.get("backdrop_path")
        movie.overview = movie_metadata.get("overview")
        movie.genres = movie_metadata.get("genres")
        movie.popularity = movie_metadata.get("popularity")
        # TMDB reports vote_average as 0 when nothing has been voted on, so a
        # stored 0 would read as a genuine bad rating in reclaim rules
        votes = movie_metadata.get("vote_count")
        movie.vote_average = movie_metadata.get("vote_average") if votes else None
        movie.vote_count = votes
        movie.revenue = movie_metadata.get("revenue")
        movie.runtime = movie_metadata.get("runtime")
        movie.status = movie_metadata.get("status")
        movie.tagline = movie_metadata.get("tagline")
        movie.last_metadata_refresh_at = datetime.now(UTC)

    except Exception as e:
        LOG.error(f"Error updating TMDB metadata for movie {tmdb_id}: {e}")


async def sync_series(
    config_id: int | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
    """Sync series from the main media server (or a specific media-server config).

    Mirrors sync_movies: a linked (non-main) server contributes watch data
    rather than series rows, and no argument means the main server rather than
    every configured server. That watch data comes from sync_linked_data, which
    sync_media calls in its own loop over the linked servers; unlike sync_movies
    this function does not call it, it only declines to sync the linked server.
    Compared by config identity, not type, so a same-type non-main config is
    always treated as linked even when its type matches the main server's.
    """
    # resolve main server, and (if a specific config was requested) that config
    async with async_db() as _cfg:
        main_config = await _get_main_media_server(_cfg)
        target_config = (
            await _cfg.get(ServiceConfig, config_id) if config_id is not None else None
        )

    # a linked server never contributes series rows
    if config_id is not None and main_config is not None and config_id != main_config.id:
        LOG.info(f"config {config_id} is a linked server - skipping series sync")
        return set()

    effective_config = target_config if config_id is not None else main_config
    if effective_config is None:
        LOG.error("No main media server configured for series sync")
        return set()

    start_time = datetime.now(UTC)
    source_label = effective_config.service_type.value
    LOG.info(f"Starting series sync ({source_label})...")

    gather_result = await gather_series(effective_config)
    if not gather_result:
        LOG.info(f"No series to sync from {source_label}")
        return set()
    aggregated_series, supplemental_episode_data = gather_result
    if not aggregated_series:
        LOG.info(f"No series to sync from {source_label}")
        return set()
    LOG.info(f"Gathered {len(aggregated_series)} unique series from {source_label}")

    # tmdb service instance
    tmdb_service = AsyncTMDBClient()

    try:
        async with async_db() as session:
            # get all existing series from database
            result = await session.execute(select(Series))
            existing_series_list = result.scalars().all()

            # convert to dictionary keyed by tmdb_id
            existing_series = {s.tmdb_id: s for s in existing_series_list if s.tmdb_id}
            # fallback lookups by tvdb_id / imdb_id for cross-service de-dup
            existing_by_tvdb = {s.tvdb_id: s for s in existing_series_list if s.tvdb_id}
            existing_by_imdb = {s.imdb_id: s for s in existing_series_list if s.imdb_id}

            # track all tmdb_ids seen in this sync
            parsed_tmdb_ids = set[int]()
            # row primary keys touched this run. parsed_tmdb_ids cannot serve
            # this purpose: a row matched by the tvdb/imdb fallback keeps its
            # own tmdb_id, which is not the id that was just parsed.
            matched_row_ids: set[int] = set()

            # iterate through aggregated series
            batch_count = 0
            for idx, series in enumerate[AggregatedSeriesData](
                aggregated_series.values(), start=1
            ):
                tmdb_id = series.external_ids.tmdb
                parsed_tmdb_ids.add(tmdb_id)

                # locate existing series: primary key is tmdb_id, fall back to
                # tvdb_id / imdb_id to avoid UNIQUE constraint violations when two
                # services report the same show with different TMDB IDs
                existing_series_obj = existing_series.get(tmdb_id)
                if existing_series_obj is None and series.external_ids.tvdb:
                    existing_series_obj = existing_by_tvdb.get(series.external_ids.tvdb)
                if existing_series_obj is None and series.external_ids.imdb:
                    existing_series_obj = existing_by_imdb.get(series.external_ids.imdb)

                # if series already exists, update it
                if existing_series_obj is not None:
                    if existing_series_obj.id in matched_row_ids:
                        # two incoming series resolved to one row. only one of
                        # them can be represented while tmdb_id is unique, so
                        # the other is silently unreachable without this line.
                        LOG.warning(
                            f"Series '{series.name}' resolved to a database row "
                            f"already claimed by another series in this sync "
                            f"(row {existing_series_obj.id}, "
                            f"tmdb {existing_series_obj.tmdb_id}). Only one can "
                            f"be stored; this one will not appear in Reclaimerr."
                        )
                    matched_row_ids.add(existing_series_obj.id)
                    # always update watch data, size, and file info from media server
                    existing_series_obj.size = series.size
                    media_rollup = _rollup_series_media_from_seasons(series.season_data)
                    existing_series_obj.has_hdr = media_rollup["has_hdr"]
                    existing_series_obj.has_dolby_vision = media_rollup[
                        "has_dolby_vision"
                    ]
                    existing_series_obj.max_video_width = media_rollup[
                        "max_video_width"
                    ]
                    existing_series_obj.max_video_height = media_rollup[
                        "max_video_height"
                    ]
                    existing_series_obj.video_codec_families = media_rollup[
                        "video_codec_families"
                    ]
                    existing_series_obj.audio_codec_families = media_rollup[
                        "audio_codec_families"
                    ]
                    existing_series_obj.max_audio_channels = media_rollup[
                        "max_audio_channels"
                    ]
                    existing_series_obj.subtitle_languages = media_rollup[
                        "subtitle_languages"
                    ]

                    # update service-specific fields based on source
                    await _upsert_series_service_ref(
                        session, existing_series_obj.id, series
                    )

                    # update added_at if available
                    if series.added_at:
                        existing_series_obj.added_at = series.added_at
                    existing_series_obj.last_viewed_at = series.last_viewed_at
                    existing_series_obj.view_count = series.view_count
                    existing_series_obj.media_server_user_rating = (
                        series.media_server_user_rating
                    )

                    # restore if soft-deleted
                    if existing_series_obj.removed_at:
                        existing_series_obj.removed_at = None
                        LOG.info(
                            f"Restored soft-deleted series: {series.name} ({tmdb_id})"
                        )

                    # refresh TMDB metadata if needed
                    if _needs_metadata_refresh(existing_series_obj, MediaType.SERIES):
                        LOG.debug(
                            f"Refreshing TMDB metadata for {series.name} ({tmdb_id})"
                        )
                        await _update_series_tmdb_metadata(
                            existing_series_obj, tmdb_id, tmdb_service
                        )

                    # sync season data
                    await _sync_seasons(
                        session,
                        existing_series_obj.id,
                        series.season_data,
                        series.service,
                    )

                # if series doesn't exist, create new entry
                else:
                    LOG.info(f"Adding new series: {series.name} ({tmdb_id})")
                    media_rollup = _rollup_series_media_from_seasons(series.season_data)
                    new_series = Series(
                        title=series.name,
                        year=series.year,
                        tmdb_id=tmdb_id,
                        size=series.size,
                        imdb_id=series.external_ids.imdb,
                        tvdb_id=series.external_ids.tvdb,
                        last_viewed_at=series.last_viewed_at,
                        view_count=series.view_count,
                        media_server_user_rating=series.media_server_user_rating,
                        has_hdr=media_rollup["has_hdr"],
                        has_dolby_vision=media_rollup["has_dolby_vision"],
                        max_video_width=media_rollup["max_video_width"],
                        max_video_height=media_rollup["max_video_height"],
                        video_codec_families=media_rollup["video_codec_families"],
                        audio_codec_families=media_rollup["audio_codec_families"],
                        max_audio_channels=media_rollup["max_audio_channels"],
                        subtitle_languages=media_rollup["subtitle_languages"],
                    )

                    # set service-specific fields based on source
                    if series.added_at:
                        new_series.added_at = series.added_at

                    # fetch TMDB metadata
                    await _update_series_tmdb_metadata(
                        new_series, tmdb_id, tmdb_service
                    )
                    session.add(new_series)
                    # flush so new_series.id is available for the service ref FK
                    await session.flush()
                    # register in lookup dicts so later iterations (from another service)
                    # don't attempt a duplicate insert
                    existing_series[tmdb_id] = new_series
                    if new_series.tvdb_id:
                        existing_by_tvdb[new_series.tvdb_id] = new_series
                    if new_series.imdb_id:
                        existing_by_imdb[new_series.imdb_id] = new_series
                    await _upsert_series_service_ref(session, new_series.id, series)
                    # sync season data
                    await _sync_seasons(
                        session, new_series.id, series.season_data, series.service
                    )

                # commit in batches
                if idx % COMMIT_BATCH_SIZE == 0:
                    await session.commit()
                    batch_count += 1

            # commit any remaining series
            await session.commit()
            LOG.debug(
                f"Committed {len(aggregated_series)} series in {batch_count + 1} batches"
            )

            #### supplemental episode ID pass ####
            # For series reported more than once by the gather, the losing
            # entry's episode data was discarded during deduplication. We re-run
            # _upsert_episodes for those seasons here so that all service
            # specific IDs (plex_rating_key, jellyfin_episode_id,
            # emby_episode_id) are written to the episodes table
            if supplemental_episode_data:
                LOG.debug(
                    f"Running supplemental episode ID upsert for "
                    f"{len(supplemental_episode_data)} series"
                )
                for (
                    sup_tmdb_id,
                    service_season_list,
                ) in supplemental_episode_data.items():
                    sup_series = existing_series.get(sup_tmdb_id)
                    if sup_series is None:
                        continue
                    for sup_service, sup_seasons in service_season_list:
                        for sd in sup_seasons:
                            if not sd.episode_data:
                                continue
                            result_s = await session.execute(
                                select(Season).where(
                                    Season.series_id == sup_series.id,
                                    Season.season_number == sd.season_number,
                                )
                            )
                            db_season = result_s.scalar_one_or_none()
                            if db_season is None:
                                continue
                            await _upsert_episodes(
                                session,
                                db_season.id,
                                sd.episode_data,
                                sup_service,
                                remove_stale=False,
                            )
                await session.commit()
                LOG.debug("Supplemental episode ID upsert committed")

            # refresh per instance Sonarr refs for active series
            sonarr_clients = service_manager.sonarr_clients()
            if not sonarr_clients and service_manager.sonarr:
                sonarr_clients = {0: service_manager.sonarr}
            if sonarr_clients:
                # purge legacy rows written before multi-arr support (service_config_id=0)
                await session.execute(
                    sql_delete(SeriesArrRef).where(SeriesArrRef.service_config_id == 0)
                )

                series_rows = (
                    await session.execute(
                        select(Series.id, Series.tmdb_id, Series.tvdb_id).where(
                            Series.removed_at.is_(None)
                        )
                    )
                ).all()
                series_id_by_tmdb = {
                    tmdb_id: series_id for series_id, tmdb_id, _tvdb_id in series_rows
                }
                # Sonarr is TVDB-native and reports tmdbId 0 for shows it could
                # not map. Matching on tmdb alone left those series unmatched
                # forever: no Arr ref, no tags, and an episode inventory this
                # loop nulls on every run, so their seasons reported "inventory
                # unavailable" no matter how often Sync Media ran.
                series_id_by_tvdb = {
                    str(tvdb_id): series_id
                    for series_id, _tmdb_id, tvdb_id in series_rows
                    if tvdb_id
                }

                def _match_series_id(arr_series: Any) -> int | None:
                    """Resolve a Sonarr series to a local one, tmdb first."""
                    if arr_series.tmdb_id:
                        series_id = series_id_by_tmdb.get(arr_series.tmdb_id)
                        if series_id is not None:
                            return series_id
                    if arr_series.tvdb_id:
                        return series_id_by_tvdb.get(str(arr_series.tvdb_id))
                    return None

                # accumulate resolved tag labels per series across all Sonarr instances
                series_tags: dict[int, set[str]] = {}
                series_episode_dates: dict[int, dict[tuple[int, int], datetime]] = {}
                series_episode_inventory: dict[int, dict[int, set[int]]] = {}
                inventory_fetched_series_ids: set[int] = set()
                date_fetch_failed_series_ids: set[int] = set()
                for config_id, client in sonarr_clients.items():
                    await session.execute(
                        sql_delete(SeriesArrRef).where(
                            SeriesArrRef.service_config_id == config_id
                        )
                    )
                    all_series = await client.get_all_series()
                    tag_list = await client.get_tags()
                    id_to_label: dict[int, str] = {t.id: t.label for t in tag_list}
                    matched_series = [
                        (arr_series, series_id)
                        for arr_series in all_series
                        if (series_id := _match_series_id(arr_series)) is not None
                    ]
                    semaphore = asyncio.Semaphore(SONARR_DATE_FETCH_CONCURRENCY)

                    async def _fetch_episode_data(arr_series_id: int) -> Any:
                        async with semaphore:
                            return await client.get_episode_sync_data(arr_series_id)

                    date_results = await asyncio.gather(
                        *(
                            _fetch_episode_data(arr_series.id)
                            for arr_series, _series_id in matched_series
                        ),
                        return_exceptions=True,
                    )
                    for (arr_series, series_id), episode_result in zip(
                        matched_series, date_results, strict=True
                    ):
                        if isinstance(episode_result, BaseException):
                            date_fetch_failed_series_ids.add(series_id)
                            LOG.warning(
                                "Failed to fetch Sonarr episode data for "
                                f"series {arr_series.id} on config {config_id}: "
                                f"{episode_result}"
                            )
                            continue
                        inventory_fetched_series_ids.add(series_id)
                        bucket = series_episode_dates.setdefault(series_id, {})
                        for key, added_at in episode_result.file_dates.items():
                            current = bucket.get(key)
                            if current is None or added_at > current:
                                bucket[key] = added_at
                        inventory = series_episode_inventory.setdefault(series_id, {})
                        for (
                            season_number,
                            episode_numbers,
                        ) in episode_result.episode_numbers_by_season.items():
                            inventory.setdefault(season_number, set()).update(
                                episode_numbers
                            )

                    for arr_series in all_series:
                        series_id = _match_series_id(arr_series)
                        if series_id is None:
                            continue
                        arr_path = (
                            str(PurePosixPath(normalize_fpath(arr_series.path)))
                            if arr_series.path
                            else None
                        )
                        session.add(
                            SeriesArrRef(
                                series_id=series_id,
                                service_config_id=config_id,
                                arr_series_id=arr_series.id,
                                arr_title_slug=arr_series.title_slug,
                                arr_series_path=arr_path,
                                tmdb_id=arr_series.tmdb_id or None,
                            )
                        )
                        for tag_id in arr_series.tags:
                            label = id_to_label.get(tag_id)
                            if label:
                                series_tags.setdefault(series_id, set()).add(label)

                # write resolved tags back to Series rows
                for series_id in series_id_by_tmdb.values():
                    result_row = await session.get(Series, series_id)
                    if result_row is not None:
                        tags = series_tags.get(series_id)
                        result_row.arr_tags = sorted(tags) if tags else []

                eligible_series_ids = set(series_id_by_tmdb.values()).difference(
                    date_fetch_failed_series_ids
                )
                season_objects = {
                    season.id: season
                    for season in (
                        await session.execute(
                            select(Season).where(
                                Season.series_id.in_(series_id_by_tmdb.values())
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                for season in season_objects.values():
                    if (
                        season.series_id in date_fetch_failed_series_ids
                        or season.series_id not in inventory_fetched_series_ids
                    ):
                        season.sonarr_episode_numbers = None
                        continue
                    episode_numbers = series_episode_inventory.get(
                        season.series_id, {}
                    ).get(season.season_number)
                    season.sonarr_episode_numbers = (
                        sorted(episode_numbers) if episode_numbers else None
                    )
                episode_rows = (
                    await session.execute(
                        select(Episode, Season)
                        .join(Season, Episode.season_id == Season.id)
                        .where(Season.series_id.in_(eligible_series_ids))
                    )
                ).all()
                season_dates: dict[int, list[datetime]] = {}
                series_dates: dict[int, list[datetime]] = {}
                for episode, season in episode_rows:
                    episode_arr_added_at = series_episode_dates.get(
                        season.series_id, {}
                    ).get((season.season_number, episode.episode_number))
                    episode.arr_added_at = episode_arr_added_at
                    if episode_arr_added_at is not None:
                        season_dates.setdefault(season.id, []).append(
                            episode_arr_added_at
                        )
                        series_dates.setdefault(season.series_id, []).append(
                            episode_arr_added_at
                        )

                for season in season_objects.values():
                    season.arr_added_at = max(
                        season_dates.get(season.id, []), default=None
                    )
                for series_id in eligible_series_ids:
                    result_row = await session.get(Series, series_id)
                    if result_row is not None:
                        result_row.arr_added_at = max(
                            series_dates.get(series_id, []), default=None
                        )

                await session.commit()

            else:
                await session.execute(
                    sql_update(Season).values(sonarr_episode_numbers=None)
                )
                await session.commit()

            if allow_soft_delete:
                series_to_delete = _select_rows_to_soft_delete(
                    existing_series_list, matched_row_ids
                )
                live_series_count = sum(
                    1 for s in existing_series_list if not s.removed_at
                )
                if _soft_delete_blocked(
                    MediaType.SERIES, series_to_delete, live_series_count
                ):
                    LOG.warning(
                        f"Skipping series soft-delete this run: "
                        f"{len(series_to_delete)} of {live_series_count} live series "
                        f"would be removed, which may be a partial response from "
                        f"{source_label} rather than real deletions. If the next "
                        f"sync proposes the same series they will be removed then."
                    )
                    series_to_delete = []

                if series_to_delete:
                    LOG.info(
                        f"Soft-deleting {len(series_to_delete)} series no longer in {source_label}"
                    )
                    await _apply_soft_deletes(
                        session, series_to_delete, MediaType.SERIES
                    )
                    await session.commit()

            duration = (datetime.now(UTC) - start_time).total_seconds()
            LOG.info(
                f"Series sync ({source_label}) completed successfully in {duration:.2f}s"
            )
            return parsed_tmdb_ids
    except Exception as e:
        duration = (datetime.now(UTC) - start_time).total_seconds()
        LOG.critical(
            f"Error during series sync ({source_label}) after {duration:.2f}s: {e}",
            exc_info=True,
        )
        raise
    finally:
        await tmdb_service.session.close()


async def _run_supplemental_syncs() -> PlaybackRefreshResult:
    """Run all supplemental play-count sync steps (Emby plugin + Tautulli).
    Called at the end of both sync_media() and resync_media().
    """
    return await refresh_playback_history(force=True)


async def _run_playback_data_refresh(
    *,
    all_servers: list[ServiceConfig] | None = None,
) -> tuple[bool, str | None, PlaybackRefreshResult]:
    """Refresh native media-server state and imported playback history."""

    watch_ok, watch_error = await media_watch_snapshot_cache.refresh_snapshot(
        all_servers=all_servers
    )
    # Identity aliases are refreshed alongside watch data because requester
    # matching is only as good as the names it can bridge. It is auxiliary, so
    # a failure here degrades requester matching to plain name comparison
    # rather than taking the playback refresh down with it.
    try:
        alias_ok, alias_error = await refresh_watch_user_aliases()
    except Exception as exc:
        LOG.warning(f"Watch identity alias refresh failed: {exc}")
    else:
        if not alias_ok and alias_error:
            LOG.warning(f"Watch identity alias refresh incomplete: {alias_error}")
    history_result = await _run_supplemental_syncs()
    return watch_ok, watch_error, history_result


async def refresh_playback_history_task() -> dict[str, Any]:
    """Refresh native watch state and durable playback-history providers."""
    async with track_task_execution(Task.REFRESH_PLAYBACK_HISTORY):
        watch_ok, watch_error, result = await _run_playback_data_refresh()
        errors = [*result.errors]
        if watch_error:
            errors.append(watch_error)
        return {
            "native_snapshot_available": watch_ok,
            "providers": len(result.statuses),
            "available_services": sorted(
                service.value for service in result.available_services
            ),
            "imported_events": sum(
                status.imported_events for status in result.statuses
            ),
            "errors": errors,
        }


async def sync_media() -> dict[str, Any] | None:
    """
    Main sync tasks task.

    1. Sync libraries
    2. Sync movies
    3. Sync series
    4. Update watch data from any linked servers
    """
    if not service_manager.main_media_server:
        LOG.warning("No main media server configured - skipping sync")
        return None

    # determine main server
    async with track_task_execution(Task.SYNC_MEDIA):
        async with async_db() as session:
            get_main_server = await _get_main_media_server(session)
            if not get_main_server:
                LOG.error("No main media server configured for sync")
                return None
        main_server = get_main_server.service_type
        if not _is_media_server_type(main_server):
            LOG.error(f"Unsupported main media server {main_server} for sync")
            return None

        # update libraries
        library_sync_result = await sync_media_libraries()

        # sync movies
        await sync_movies(get_main_server.id)

        # sync series
        await sync_series(get_main_server.id)

        await _mark_service_config_synced(get_main_server.id)

        # sync linked watch data from every other configured server - compared
        # by config identity, not type, so a non-main config of the SAME type
        # as main is still correctly treated as linked rather than silently
        # excluded from both the main sync and the linked sync
        async with async_db() as linked_session:
            all_servers = await _get_configured_media_servers(linked_session)
        linked_servers = [
            svr
            for svr in all_servers
            if svr.id != get_main_server.id and _is_media_server_type(svr.service_type)
        ]
        active_linked_service_config_ids: set[int] = {
            svr.id for svr in linked_servers
        }
        await _prune_supplemental_matches(active_linked_service_config_ids)
        for svr in linked_servers:
            LOG.debug(f"Linked watch sync from {svr.service_type} (config {svr.id})")
            await sync_linked_data(svr)

        # refresh favorites snapshot from supported media servers
        ok, error = await media_favorites_snapshot_cache.refresh_snapshot(
            all_servers=all_servers
        )
        if not ok and error:
            LOG.warning(f"Favorites snapshot refresh failed during sync: {error}")

        watch_ok, watch_error, _playback_result = await _run_playback_data_refresh(
            all_servers=all_servers
        )
        if not watch_ok and watch_error:
            LOG.warning(f"Watch snapshot refresh failed during sync: {watch_error}")

        return {"library_sync": library_sync_result}


async def sync_linked_data(
    config: ServiceConfig,
) -> None:
    """
    Update watch data (view_count, last_viewed_at, never_watched) on existing Movie rows
    from a linked (non-main) media server. No version rows are written, but high
    confidence same-media supplemental identity mappings are refreshed.

    `config` must be a specific, non-main media server ServiceConfig row - never
    "all configs of this type", so two linked configs of the same type are always
    synced (and attributed) independently.
    """
    service = config.service_type
    async with track_task_execution(Task.SYNC_LINKED_DATA):
        LOG.info(f"Syncing linked data from {service} (config {config.id})...")
        service_instance = await _get_media_service_instance(config)
        if not service_instance:
            await _clear_supplemental_matches(config.id)
            return

        # determine whether this linked config's type matches the main
        # server's type, so episode ID backfill (a single column per type,
        # not per config) is only ever written by one config of that type
        async with async_db() as _cfg:
            main_config = await _get_main_media_server(_cfg)
        backfill_episode_ids = (
            main_config is None or main_config.service_type != config.service_type
        )

        # fetch all libraries - linked servers don't have library selection
        try:
            aggregated = await service_instance.get_aggregated_movies(
                included_libraries=None
            )
        except Exception as e:
            LOG.warning(
                f"Failed to fetch linked movie data from {service}; clearing "
                f"supplemental matches for that service: {e}"
            )
            await _clear_supplemental_matches(config.id)
            return

        # build watch data keyed by TMDB ID (merge same-TMDB across libraries)
        watch_by_tmdb: dict[int, tuple[int, datetime | None]] = {}
        for movie in aggregated:
            if not movie.external_ids or not movie.external_ids.tmdb:
                continue
            tmdb_id = movie.external_ids.tmdb
            if tmdb_id not in watch_by_tmdb:
                watch_by_tmdb[tmdb_id] = (movie.view_count, movie.last_viewed_at)
            else:
                prev_count, prev_lva = watch_by_tmdb[tmdb_id]
                merged_count = max(prev_count, movie.view_count)
                lva_candidates = [dt for dt in [prev_lva, movie.last_viewed_at] if dt]
                watch_by_tmdb[tmdb_id] = (
                    merged_count,
                    max(lva_candidates) if lva_candidates else None,
                )

        if watch_by_tmdb:
            async with async_db() as session:
                result = await session.execute(
                    select(Movie).where(
                        Movie.tmdb_id.in_(watch_by_tmdb.keys()),
                        Movie.removed_at.is_(None),
                    )
                )
                updated = 0
                for db_movie in result.scalars().all():
                    view_count, last_viewed_at = watch_by_tmdb[db_movie.tmdb_id]
                    last_viewed_at = _merge_last_viewed(None, last_viewed_at)
                    changed = False
                    if view_count > db_movie.view_count:
                        db_movie.view_count = view_count
                        changed = True
                    if last_viewed_at and (
                        not db_movie.last_viewed_at
                        or last_viewed_at > db_movie.last_viewed_at
                    ):
                        db_movie.last_viewed_at = last_viewed_at
                        changed = True
                    if changed:
                        updated += 1
                await session.commit()

            LOG.info(f"Updated watch data from {service} for {updated} movies")
        else:
            LOG.debug(f"No linked movie watch data returned from {service}")

        async with async_db() as session:
            movie_matches = await _build_movie_supplemental_matches(
                session, config, aggregated
            )
            await _replace_supplemental_matches(
                session, config.id, MediaType.MOVIE, movie_matches
            )
            await session.commit()
        LOG.info(
            f"Refreshed {len(movie_matches)} supplemental movie matches from {service}"
        )

        try:
            aggregated_series = await service_instance.get_aggregated_series(
                included_libraries=None
            )
        except Exception as e:
            LOG.warning(
                f"Failed to fetch linked series data from {service} for "
                f"supplemental matching; clearing stale series matches: {e}"
            )
            await _clear_supplemental_matches(config.id, MediaType.SERIES)
            return

        async with async_db() as session:
            series_matches = await _build_series_supplemental_matches(
                session, config, aggregated_series
            )
            await _replace_supplemental_matches(
                session, config.id, MediaType.SERIES, series_matches
            )
            await session.commit()
        LOG.info(
            f"Refreshed {len(series_matches)} supplemental series/season matches "
            f"from {service}"
        )

        #### Merge watch data + episode IDs from the linked server ####
        # The main sync only writes watch data and episode IDs from the main server.
        # This pass merges view_count / last_viewed_at at series, season, and episode
        # level from the linked server, and backfills service-specific episode IDs
        # (e.g. jellyfin_episode_id, plex_rating_key).
        # remove_stale=False because the linked server may only have partial seasons.
        ep_series_count = 0
        ep_updated_count = 0
        async with async_db() as session:
            # build tmdb_id -> Series row lookup for fast matching
            result_sids = await session.execute(
                select(Series).where(Series.removed_at.is_(None))
            )
            series_by_tmdb: dict[int, Series] = {
                s.tmdb_id: s for s in result_sids.scalars().all() if s.tmdb_id
            }
            for linked_series in aggregated_series:
                ext = linked_series.external_ids
                if not ext or not ext.tmdb:
                    continue
                db_series = series_by_tmdb.get(ext.tmdb)
                if db_series is None:
                    continue

                # merge series level watch data
                db_series.view_count = max(
                    db_series.view_count or 0, linked_series.view_count or 0
                )
                db_series.last_viewed_at = _merge_last_viewed(
                    db_series.last_viewed_at, linked_series.last_viewed_at
                )

                for sd in linked_series.season_data:
                    result_s = await session.execute(
                        select(Season).where(
                            Season.series_id == db_series.id,
                            Season.season_number == sd.season_number,
                        )
                    )
                    db_season = result_s.scalar_one_or_none()
                    if db_season is None:
                        continue

                    # merge season level watch data
                    db_season.view_count = max(
                        db_season.view_count or 0, sd.view_count or 0
                    )
                    db_season.last_viewed_at = _merge_last_viewed(
                        db_season.last_viewed_at, sd.last_viewed_at
                    )

                    # backfill episode IDs + merge episode watch data
                    if sd.episode_data:
                        await _upsert_episodes(
                            session,
                            db_season.id,
                            sd.episode_data,
                            service,
                            remove_stale=False,
                            backfill_ids=backfill_episode_ids,
                        )
                        ep_updated_count += len(sd.episode_data)

                ep_series_count += 1
            await session.commit()
        LOG.info(
            f"Merged watch data + episode IDs from {service} for {ep_series_count} series "
            f"({ep_updated_count} episode records processed)"
        )

        await _mark_service_config_synced(config.id)


async def resync_media() -> None:
    """
    Full re-sync triggered when the main media server is switched.
    Wipes all MovieVersion and SeriesServiceRef rows (old server IDs are invalid
    for the new server), resets Movie.size and Series.size, then runs a full sync
    from the new main server.
    """
    if not service_manager.main_media_server:
        LOG.warning("No main media server configured - skipping resync")
        return

    LOG.info("Starting resync...")
    async with track_task_execution(Task.RESYNC_MEDIA):
        try:
            async with async_db() as session:
                # movie_version scoped rows must be detached before deleting versions
                # when switching main media server, old version IDs are invalid anyway
                await session.execute(
                    sql_update(ReclaimCandidate)
                    .where(ReclaimCandidate.movie_version_id.is_not(None))
                    .values(movie_version_id=None)
                )
                await session.execute(
                    sql_update(ProtectedMedia)
                    .where(ProtectedMedia.movie_version_id.is_not(None))
                    .values(movie_version_id=None)
                )
                await session.execute(
                    sql_update(ProtectionRequest)
                    .where(ProtectionRequest.movie_version_id.is_not(None))
                    .values(movie_version_id=None)
                )
                await session.execute(sql_delete(MovieVersion))
                await session.execute(sql_delete(SeriesServiceRef))
                await session.execute(sql_delete(SupplementalMediaMatch))
                await session.execute(sql_update(Movie).values(size=0))
                await session.execute(sql_update(Series).values(size=0))
                await session.commit()
            LOG.info(
                "Cleared all MovieVersion, SeriesServiceRef, and supplemental match "
                "rows for main server resync"
            )
            # sync libraries first so stale library IDs get scrubbed from rules
            # before the movie/series sync restores version data
            await sync_media_libraries()
            await sync_movies(allow_soft_delete=False)
            await sync_series(allow_soft_delete=False)
            async with async_db() as session:
                resynced_main = await _get_main_media_server(session)
            if resynced_main is not None:
                await _mark_service_config_synced(resynced_main.id)
            await _run_supplemental_syncs()
        except Exception as e:
            LOG.error(f"Error during main server resync: {e}", exc_info=True)
            raise


async def _update_series_tmdb_metadata(
    series: Series, tmdb_id: int, tmdb_service: AsyncTMDBClient
) -> None:
    """Update series with TMDB metadata."""
    try:
        series_metadata = await tmdb_service.get_tv_details(tmdb_id)
        if not series_metadata or not isinstance(series_metadata, dict):
            LOG.warning(f"Failed to fetch TMDB metadata for series {tmdb_id}")
            return

        ext_ids = series_metadata.get("external_ids", {})
        series.imdb_id = ext_ids.get("imdb_id") or None
        tvdb_id = ext_ids.get("tvdb_id")
        series.tvdb_id = str(tvdb_id) if tvdb_id is not None else None
        series.tmdb_title = series_metadata.get("name")
        series.original_title = series_metadata.get("original_name")

        first_air_date = series_metadata.get("first_air_date")
        if first_air_date:
            parsed = datetime.strptime(first_air_date, "%Y-%m-%d")
            series.tmdb_first_air_date = parsed
            # backfill year if media server didn't provide one
            if not series.year:
                series.year = parsed.year

        last_air_date = series_metadata.get("last_air_date")
        if last_air_date:
            series.tmdb_last_air_date = datetime.strptime(last_air_date, "%Y-%m-%d")

        series.original_language = series_metadata.get("original_language")
        series.homepage = series_metadata.get("homepage")
        series.origin_country = series_metadata.get("origin_country")
        series.poster_url = series_metadata.get("poster_path")
        series.backdrop_url = series_metadata.get("backdrop_path")
        series.overview = series_metadata.get("overview")
        series.genres = series_metadata.get("genres")
        series.popularity = series_metadata.get("popularity")
        # TMDB reports vote_average as 0 when nothing has been voted on, so a
        # stored 0 would read as a genuine bad rating in reclaim rules
        votes = series_metadata.get("vote_count")
        series.vote_average = series_metadata.get("vote_average") if votes else None
        series.vote_count = votes
        series.status = series_metadata.get("status")
        series.tagline = series_metadata.get("tagline")
        series.season_count = series_metadata.get("number_of_seasons")
        series.last_metadata_refresh_at = datetime.now(UTC)

    except Exception as e:
        LOG.error(
            f"Error updating TMDB metadata for series {tmdb_id}: {e}", exc_info=True
        )


async def sync_media_libraries() -> dict[str, Any]:
    """Update service libraries in the database from the main media server."""
    if not service_manager.main_media_server:
        LOG.warning("No main media server configured - skipping library sync")
        return {"libraries": [], "affected_rules": []}

    async with track_task_execution(Task.SYNC_MEDIA_LIBRARIES):
        async with async_db() as session:
            main = await _get_main_media_server(session)
        if not main:
            LOG.error("No main media server configured - skipping library sync")
            return {"libraries": [], "affected_rules": []}

        service_instance = await _get_media_service_instance(main)
        if not service_instance:
            return {"libraries": [], "affected_rules": []}

        movie_libs = await service_instance.get_movie_libraries()
        series_libs = await service_instance.get_series_libraries()

        async with async_db() as session:
            result = await session.execute(select(ServiceMediaLibrary))
            all_rows = list(result.scalars().all())
            # Keyed by config as well as library id. Jellyfin and Emby derive a
            # library's id from its path, so two servers each holding a library
            # at the same path report the same id - keying on the id alone let a
            # main-server switch update the old server's row in place and
            # silently retarget every rule scoped to it.
            existing_map: dict[str, ServiceMediaLibrary] = {}
            foreign_rows: list[ServiceMediaLibrary] = []
            for row in all_rows:
                # A row with no config predates this column; adopt it onto main
                # if main still reports that library, drop it below if not.
                if row.service_config_id in (main.id, None):
                    existing_map[row.library_id] = row
                else:
                    foreign_rows.append(row)

            current_ids: set[str] = set()
            current_libraries: list[dict[str, Any]] = []

            for lib, media_type in [
                *[(lib, MediaType.MOVIE) for lib in movie_libs],
                *[(lib, MediaType.SERIES) for lib in series_libs],
            ]:
                lib_id = lib["id"]
                current_ids.add(lib_id)
                current_libraries.append(
                    {"id": lib_id, "name": lib["name"], "type": media_type}
                )
                if lib_id in existing_map:
                    existing = existing_map[lib_id]
                    if existing.library_name != lib["name"]:
                        existing.library_name = lib["name"]
                    existing.service_config_id = main.id
                else:
                    session.add(
                        ServiceMediaLibrary(
                            library_id=lib_id,
                            library_name=lib["name"],
                            media_type=media_type,
                            service_config_id=main.id,
                        )
                    )

            # delete libraries no longer present on the main server
            removed_ids: set[str] = set()
            for lib_id, existing_library in existing_map.items():
                if lib_id not in current_ids:
                    await session.delete(existing_library)
                    removed_ids.add(lib_id)

            # and every row left over from a server that is no longer main -
            # only main contributes libraries, so those can never be in scope.
            # Not counted as removed: main may report the same id itself, and
            # the row that survives is the one rules should now resolve against.
            for foreign in foreign_rows:
                await session.delete(foreign)

            # Advanced rules now keep library scope inside the rule definition.
            # We surface stale-library references through alerts instead of
            # mutating rule definitions during sync.
            affected_rules: list[dict[str, Any]] = []
            try:
                # Sessions run with autoflush off, so the notice would otherwise
                # read the pre-sync rows and judge staleness against them.
                await session.flush()
                await reconcile_stale_library_notice(session)
            except Exception as e:
                LOG.warning(f"Failed to reconcile stale-library notice state: {e}")

            await session.commit()

            LOG.info(
                f"Updated service libraries: {len(current_ids)} total libraries "
                f"from {main.service_type}"
            )
            return {
                "libraries": current_libraries,
                "affected_rules": affected_rules,
            }
