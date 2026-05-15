from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.core.tmdb import AsyncTMDBClient
from backend.core.utils.filesystem import normalize_fpath
from backend.database import async_db
from backend.database.models import (
    Episode,
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
from backend.enums import MediaType, Service, Task
from backend.models.media import (
    AggregatedEpisodeData,
    AggregatedMovieData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    MovieVersionData,
)
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.types import MEDIA_SERVERS, MediaServerType

__all__ = [
    "sync_media",
    "resync_media",
    "sync_media_libraries",
    "sync_linked_data",
    "sync_emby_playback_reporting_data",
    "sync_tautulli_playback_data",
]

# number of records to process before committing to the database during sync tasks
COMMIT_BATCH_SIZE = 100


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
    session,
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
    return result.scalars().all()


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


async def _get_media_service_instance(
    service_type: Service,
) -> JellyfinService | EmbyService | PlexService | None:
    """Return initialized media service instance for Plex/Jellyfin/Emby or None."""
    service_instance = await service_manager.return_service(service_type)
    if not service_instance:
        LOG.error(f"Service {service_type} not initialized")
        return None
    if not isinstance(service_instance, (JellyfinService, EmbyService, PlexService)):
        LOG.error(f"Service {service_type} is not a media server")
        return None
    return service_instance


async def _replace_supplemental_matches(
    session: AsyncSession,
    source_service: Service,
    media_type: MediaType,
    matches: list[SupplementalMediaMatch],
) -> None:
    """Replace all supplemental matches for a given source service and media type with a new set of matches."""
    await session.execute(
        sql_delete(SupplementalMediaMatch).where(
            SupplementalMediaMatch.source_service == source_service,
            SupplementalMediaMatch.media_type == media_type,
        )
    )
    session.add_all(matches)


async def _clear_supplemental_matches(
    source_service: Service,
    media_type: MediaType | None = None,
) -> None:
    """Clear supplemental matches for a given source service, optionally filtered by media type."""
    async with async_db() as session:
        query = sql_delete(SupplementalMediaMatch).where(
            SupplementalMediaMatch.source_service == source_service
        )
        if media_type is not None:
            query = query.where(SupplementalMediaMatch.media_type == media_type)
        await session.execute(query)
        await session.commit()


async def _prune_supplemental_matches(active_linked_services: set[Service]) -> None:
    """Prune supplemental matches for inactive linked services."""
    async with async_db() as session:
        if active_linked_services:
            query = sql_delete(SupplementalMediaMatch).where(
                SupplementalMediaMatch.source_service.not_in(active_linked_services)
            )
        else:
            query = sql_delete(SupplementalMediaMatch)
        await session.execute(query)
        await session.commit()


async def _build_movie_supplemental_matches(
    session: AsyncSession,
    source_service: Service,
    movies: list[AggregatedMovieData],
) -> list[SupplementalMediaMatch]:
    """Build supplemental matches for movies."""
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
    source_service: Service,
    series_items: list[AggregatedSeriesData],
) -> list[SupplementalMediaMatch]:
    """Build supplemental matches for series."""
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
    if media_type is MediaType.MOVIE:
        release_date = obj.tmdb_release_date  # pyright: ignore[reportAttributeAccessIssue]
    else:
        release_date = obj.tmdb_first_air_date  # pyright: ignore[reportAttributeAccessIssue]

    if release_date:
        days_since_release = (time_now - release_date.replace(tzinfo=UTC)).days
        days_since_refresh = (
            time_now - obj.last_metadata_refresh_at.replace(tzinfo=UTC)
        ).days

        # if released within last 6 months and not refreshed in 30 days
        if days_since_release <= 180 and days_since_refresh > 30:
            return True

    return False


def _rollup_series_media_from_seasons(season_data: list[AggregatedSeasonData]) -> dict:
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
    if not season_data:
        return

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
            s.last_viewed_at = sd.last_viewed_at
            s.air_date = sd.air_date
            s.added_at = sd.added_at
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
                last_viewed_at=sd.last_viewed_at,
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
            )
            new_season.added_at = sd.added_at
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
                ProtectionRequest.season_id.in_(removed_season_ids)
            )
        )
        for season_number, season_obj in existing.items():
            if season_number not in incoming_season_numbers:
                await session.delete(season_obj)

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
) -> None:
    """Upsert Episode rows for a season from freshly-fetched media server episode data.

    Args:
        remove_stale: When True (default), delete episodes no longer reported by the
            service. Set to False for supplemental/linked-server calls where the service
            may only have partial season coverage and should not delete episodes written
            by the primary server.
    """
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
                if e.last_viewed_at is None:
                    e.last_viewed_at = ep.last_viewed_at
                else:
                    # normalize both sides to UTC aware before comparing to avoid
                    # "offset-naive vs offset-aware" errors (SQLite returns naive datetimes)
                    ep_lva = (
                        ep.last_viewed_at
                        if ep.last_viewed_at.tzinfo
                        else ep.last_viewed_at.replace(tzinfo=UTC)
                    )
                    e_lva = (
                        e.last_viewed_at
                        if e.last_viewed_at.tzinfo
                        else e.last_viewed_at.replace(tzinfo=UTC)
                    )
                    if ep_lva > e_lva:
                        e.last_viewed_at = ep.last_viewed_at
            if ep.air_date is not None and e.air_date is None:
                e.air_date = ep.air_date
            if ep.name is not None and e.name is None:
                e.name = ep.name
            if ep.size is not None:
                e.size = ep.size
            if ep.path is not None:
                e.path = ep.path
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
                plex_rating_key=ep.plex_rating_key,
                jellyfin_episode_id=ep.jellyfin_episode_id,
                emby_episode_id=ep.emby_episode_id,
            )
            session.add(new_ep)

    # remove episodes no longer present on the media server
    if remove_stale:
        for ep_num, ep_obj in existing_eps.items():
            if ep_num not in incoming_nums:
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
    else:
        session.add(
            SeriesServiceRef(
                series_id=series_id,
                service=data.service,
                service_id=data.id,
                library_id=data.library_id,
                library_name=data.library_name,
                path=data.path,
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
) -> tuple | None:
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
    existing: dict[tuple, MovieVersion] = {
        (v.service, v.service_media_id): v for v in result.scalars().all()
    }

    fp_map: dict[tuple, MovieVersion | None] = {}
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

    incoming_keys: set[tuple] = set()
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
        if ver.added_at:
            ev.added_at = ver.added_at

    # prune stale versions - all incoming versions come from the authoritative main server
    for key, ev in existing.items():
        if key not in incoming_keys:
            await session.delete(ev)

    # size = sum of incoming versions (all stale versions are being deleted)
    db_movie.size = sum(ver.size for ver in versions)


async def gather_movies(
    service: MediaServerType | None = None,
) -> dict[int, AggregatedMovieData] | None:
    """
    Fetch movies from the main media server (or a specific service) and group by TMDB ID.
    Same movie in multiple libraries on the same server gets its versions merged.
    Watch data takes the max across libraries.
    """
    async with async_db() as session:
        if service is not None:
            # explicit service requested (use it directly)
            servers = await _get_configured_media_servers(session, service)
        else:
            # use designated main server (required)
            main = await _get_main_media_server(session)
            if not main:
                LOG.error(
                    "No main media server configured. Must have a main server designated."
                )
                return None
            servers = [main]

        if not servers:
            return None

        aggregated_movies: list[AggregatedMovieData] = []
        for server in servers:
            service_instance = await _get_media_service_instance(server.service_type)
            if not service_instance:
                continue
            LOG.debug(
                f"Fetching movies from {server.service_type} at {server.base_url}"
            )
            get_movies = await service_instance.get_aggregated_movies(
                included_libraries=None
            )
            if get_movies:
                aggregated_movies.extend(get_movies)
            LOG.debug(
                f"Fetched {len(get_movies or [])} movies from {server.service_type}"
            )

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
            seen_version_keys: set[tuple] = {
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
            )

    if skipped_count > 0:
        LOG.warning(f"Skipped {skipped_count} movies without TMDB IDs")

    return unique_movies


# supplemental episode data from services that lost deduplication:
# tmdb_id -> list of (service, season_data_list)
_SupplementalEpisodeData = dict[int, list[tuple[Service, list[AggregatedSeasonData]]]]


async def gather_series(
    service: MediaServerType | None = None,
) -> tuple[dict[int, AggregatedSeriesData], _SupplementalEpisodeData] | None:
    """Fetch and combine series from all configured media servers, deduplicating by TMDB ID."""
    aggregated_series = []
    async with async_db() as session:
        media_servers = await _get_configured_media_servers(session, service)

        if not media_servers:
            return

        # fetch series from each media server
        for server in media_servers:
            service_instance = await _get_media_service_instance(server.service_type)
            if not service_instance:
                continue
            LOG.debug(
                f"Fetching series from {server.service_type} at {server.base_url}"
            )

            # fetch aggregated series
            get_series = await service_instance.get_aggregated_series(
                included_libraries=None
            )
            if get_series:
                aggregated_series.extend(get_series)
            LOG.debug(f"Fetched {len(get_series)} series from {server.service_type}")

    # deduplicate series, keeping the one with most recent watch date.
    # When a series appears in multiple services (e.g. Plex + Jellyfin), the losing
    # service's season data is stored as supplemental so its episode IDs (plex_rating_key,
    # jellyfin_episode_id, emby_episode_id) can still be written to the episodes table.
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
        else:
            existing = unique_series[tmdb_id]
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


async def sync_movies(
    service: MediaServerType | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
    """Sync movies from media server to database, optionally filtered by service.

    Returns set of synced TMDB IDs.
    """
    # resolve main server
    async with async_db() as _cfg:
        main_server = await _get_main_media_server(_cfg)
    main_service_type: MediaServerType | None = (
        main_server.service_type if main_server else None  # type: ignore[assignment]
    )

    # if a specific non-main service was requested, only sync watch data from it
    if (
        service is not None
        and main_service_type is not None
        and service != main_service_type
    ):
        LOG.info(f"{service} is a linked server - syncing watch data only")
        await sync_linked_data(service)
        return set()

    # resolve effective service for the full (version + watch) sync
    effective_service = service if service is not None else main_service_type
    if not effective_service or not effective_service.value:
        LOG.error(
            "No media server available for syncing movies. Please configure a main media server "
            "or specify a service."
        )
        return set()
    LOG.info(f"Starting movie sync ({effective_service.value})...")
    start_time = datetime.now(UTC)

    aggregated_movies = await gather_movies(effective_service)
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

                    # update added_at if available
                    if earliest_added:
                        existing_movie.added_at = earliest_added
                    existing_movie.last_viewed_at = movie.last_viewed_at
                    existing_movie.view_count = movie.view_count

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
                        LOG.info(
                            f"Movie '{movie.name}' not found by tmdb_id ({tmdb_id}) but matched "
                            f"existing record by imdb_id ({imdb_id}) - updating instead of inserting"
                        )
                        if earliest_added:
                            existing_movie.added_at = earliest_added
                        existing_movie.last_viewed_at = movie.last_viewed_at
                        existing_movie.view_count = movie.view_count
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
                        )

                        if earliest_added:
                            new_movie.added_at = earliest_added

                        await _update_movie_tmdb_metadata(
                            new_movie, tmdb_id, tmdb_service
                        )
                        session.add(new_movie)
                        await session.flush()
                        seen_new_ver_keys: set[tuple] = set()
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

                # accumulate resolved tag labels per movie across all Radarr instances
                movie_tags: dict[int, set[str]] = {}
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
                                arr_movie_path=arr_path,
                                tmdb_id=arr_movie.tmdb_id,
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

                await session.commit()

            if allow_soft_delete:
                movies_to_delete = [
                    movie
                    for movie in existing_movies.values()
                    if movie.tmdb_id not in parsed_tmdb_ids and not movie.removed_at
                ]

                if movies_to_delete:
                    LOG.info(
                        f"Soft-deleting {len(movies_to_delete)} movies no longer in {effective_service.value}"
                    )
                    deleted_movie_ids = []
                    for movie in movies_to_delete:
                        movie.removed_at = datetime.now(UTC)
                        movie.added_at = None
                        deleted_movie_ids.append(movie.id)
                        LOG.debug(f"Soft-deleted: {movie.title} ({movie.tmdb_id})")

                    # clean up orphaned candidates and protection entries
                    if deleted_movie_ids:
                        await session.execute(
                            sql_delete(ReclaimCandidate).where(
                                ReclaimCandidate.movie_id.in_(deleted_movie_ids)
                            )
                        )
                        await session.execute(
                            sql_delete(ProtectedMedia).where(
                                ProtectedMedia.movie_id.in_(deleted_movie_ids)
                            )
                        )
                        await session.execute(
                            sql_delete(ProtectionRequest).where(
                                ProtectionRequest.movie_id.in_(deleted_movie_ids)
                            )
                        )
                        LOG.debug(
                            f"Cleaned up candidates/protection entries for {len(deleted_movie_ids)} soft-deleted movies"
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
):
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
        movie.vote_average = movie_metadata.get("vote_average")
        movie.vote_count = movie_metadata.get("vote_count")
        movie.revenue = movie_metadata.get("revenue")
        movie.runtime = movie_metadata.get("runtime")
        movie.status = movie_metadata.get("status")
        movie.tagline = movie_metadata.get("tagline")
        movie.last_metadata_refresh_at = datetime.now(UTC)

    except Exception as e:
        LOG.error(f"Error updating TMDB metadata for movie {tmdb_id}: {e}")


async def sync_series(
    service: MediaServerType | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
    """Sync series from media servers to database."""
    start_time = datetime.now(UTC)
    source_label = service.value if service else "all-media-services"
    LOG.info(f"Starting series sync ({source_label})...")

    gather_result = await gather_series(service)
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
            # For series that appeared in multiple services (e.g. Plex + Jellyfin/Emby),
            # the losing service's episode data was discarded during deduplication.
            # We re-run _upsert_episodes for those seasons here so that all
            # service specific IDs (plex_rating_key, jellyfin_episode_id, etc.)
            # are written to the episodes table
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

                series_rows = await session.execute(
                    select(Series.id, Series.tmdb_id).where(Series.removed_at.is_(None))
                )
                series_id_by_tmdb = {
                    tmdb_id: series_id for series_id, tmdb_id in series_rows
                }

                # accumulate resolved tag labels per series across all Sonarr instances
                series_tags: dict[int, set[str]] = {}
                for config_id, client in sonarr_clients.items():
                    await session.execute(
                        sql_delete(SeriesArrRef).where(
                            SeriesArrRef.service_config_id == config_id
                        )
                    )
                    all_series = await client.get_all_series()
                    tag_list = await client.get_tags()
                    id_to_label: dict[int, str] = {t.id: t.label for t in tag_list}
                    for arr_series in all_series:
                        if not arr_series.tmdb_id:
                            continue
                        series_id = series_id_by_tmdb.get(arr_series.tmdb_id)
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
                                arr_series_path=arr_path,
                                tmdb_id=arr_series.tmdb_id,
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

                await session.commit()

            if allow_soft_delete:
                series_to_delete = [
                    s
                    for s in existing_series.values()
                    if s.tmdb_id not in parsed_tmdb_ids and not s.removed_at
                ]

                if series_to_delete:
                    LOG.info(
                        f"Soft-deleting {len(series_to_delete)} series no longer in {source_label}"
                    )
                    deleted_series_ids = []
                    for s in series_to_delete:
                        s.removed_at = datetime.now(UTC)
                        s.added_at = None
                        deleted_series_ids.append(s.id)
                        LOG.debug(f"Soft-deleted: {s.title} ({s.tmdb_id})")

                    # clean up orphaned candidates and protection entries
                    if deleted_series_ids:
                        await session.execute(
                            sql_delete(ReclaimCandidate).where(
                                ReclaimCandidate.series_id.in_(deleted_series_ids)
                            )
                        )
                        await session.execute(
                            sql_delete(ProtectedMedia).where(
                                ProtectedMedia.series_id.in_(deleted_series_ids)
                            )
                        )
                        await session.execute(
                            sql_delete(ProtectionRequest).where(
                                ProtectionRequest.series_id.in_(deleted_series_ids)
                            )
                        )
                        LOG.debug(
                            f"Cleaned up candidates/protection entries for {len(deleted_series_ids)} soft-deleted series"
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


async def _run_supplemental_syncs() -> None:
    """Run all supplemental play-count sync steps (Emby plugin + Tautulli).
    Called at the end of both sync_media() and resync_media().
    """
    # emby/jellyfin playback reporting plugin sync (if applicable)
    await sync_emby_playback_reporting_data()
    # tautulli play history sync (if applicable) for plex
    await sync_tautulli_playback_data()


async def sync_emby_playback_reporting_data() -> None:
    """Supplement movie, series, and season view counts from Playback Reporting.

    Supports both the original Emby plugin (faush01/playback_reporting,
    ConfigurationFileName ``playback_reporting.xml``) and the Jellyfin fork
    (jellyfin/jellyfin-plugin-playbackreporting,
    ConfigurationFileName ``Jellyfin.Plugin.PlaybackReporting.xml``). Both expose
    the same ``POST /user_usage_stats/submit_custom_query`` endpoint.

    After the normal sync the plugin (if installed) provides play history filtered by
    actual play duration, which eliminates brief scrubs that the native media server
    play count API would otherwise count. The supplemental counts are applied as
    ``max(existing, plugin_count)`` so existing data is never decreased.

    Direct main-server IDs are used when Jellyfin/Emby is the main server.
    Otherwise, same-media supplemental mappings from linked sync are used.
    """
    async with async_db() as session:
        main = await _get_main_media_server(session)
        servers = await _get_configured_media_servers(session)
    if not main:
        LOG.debug(
            "Skipping Playback Reporting supplemental sync: no main media server configured"
        )
        return

    reporting_services = sorted(
        {
            server.service_type
            for server in servers
            if server.service_type in {Service.JELLYFIN, Service.EMBY}
        },
        key=lambda service: service.value,
    )
    if not reporting_services:
        LOG.debug(
            "Skipping Playback Reporting supplemental sync: no enabled jellyfin/emby services"
        )
        return

    for server_service in reporting_services:
        await _sync_playback_reporting_for_service(
            server_service, use_mapped_matches=server_service != main.service_type
        )


async def _sync_playback_reporting_for_service(
    server_service: Service, use_mapped_matches: bool
) -> None:
    service_instance = await _get_media_service_instance(server_service)
    if not isinstance(service_instance, (JellyfinService, EmbyService)):
        return

    LOG.info(f"Checking for Playback Reporting plugin on {server_service}...")
    if not await service_instance.has_playback_reporting_plugin():
        LOG.info(
            f"Playback Reporting plugin not found on {server_service} "
            "(skipping supplemental sync)"
        )
        return

    LOG.info(
        f"Playback Reporting plugin found on {server_service} "
        "(syncing supplemental play data)"
    )

    #### movies ####
    movie_stats = await service_instance.get_playback_reporting_stats(15, "Movie")
    if movie_stats:
        LOG.info(
            f"Playback Reporting plugin returned play counts for {len(movie_stats)} "
            f"movies from {server_service}"
        )
        async with async_db() as session:
            rows = (
                await session.execute(
                    select(MovieVersion.service_item_id, Movie.id, Movie.view_count)
                    .join(Movie, MovieVersion.movie_id == Movie.id)
                    .where(
                        MovieVersion.service == server_service,
                        Movie.removed_at.is_(None),
                    )
                )
            ).all()

            updated = 0
            for item_id, movie_id, current_count in rows:
                plugin_count = movie_stats.get(item_id)
                if plugin_count and plugin_count > current_count:
                    await session.execute(
                        sql_update(Movie)
                        .where(Movie.id == movie_id)
                        .values(view_count=plugin_count)
                    )
                    updated += 1

            if use_mapped_matches:
                mapped_rows = (
                    await session.execute(
                        select(
                            SupplementalMediaMatch.source_item_id,
                            Movie.id,
                            Movie.view_count,
                        )
                        .join(Movie, SupplementalMediaMatch.movie_id == Movie.id)
                        .where(
                            SupplementalMediaMatch.source_service == server_service,
                            SupplementalMediaMatch.media_type == MediaType.MOVIE,
                            SupplementalMediaMatch.movie_id.is_not(None),
                            Movie.removed_at.is_(None),
                        )
                    )
                ).all()
                for item_id, movie_id, current_count in mapped_rows:
                    plugin_count = movie_stats.get(item_id)
                    if plugin_count and plugin_count > (current_count or 0):
                        await session.execute(
                            sql_update(Movie)
                            .where(Movie.id == movie_id)
                            .values(view_count=plugin_count)
                        )
                        updated += 1

            await session.commit()
        LOG.info(
            f"Updated view_count for {updated} movies from {server_service} "
            "Playback Reporting plugin"
        )
    else:
        LOG.debug(f"No movie play data returned from {server_service}")

    #### series (aggregated from episode level data) ####
    episode_stats = await service_instance.get_playback_reporting_stats(7, "Episode")
    if not episode_stats:
        LOG.debug(f"No episode play data returned from {server_service}")
        return

    LOG.info(
        f"Playback Reporting plugin returned play counts for {len(episode_stats)} "
        f"episodes from {server_service}"
    )

    episode_to_parent = await service_instance.get_parent_ids_for_episode_ids(
        list(episode_stats.keys())
    )
    if not episode_to_parent:
        LOG.warning(
            "Could not resolve any episode IDs to parent IDs "
            f"for {server_service} (skipping series/season update)"
        )
        return

    series_play_counts: dict[str, int] = {}
    season_play_counts: dict[str, int] = {}
    for ep_id, ep_count in episode_stats.items():
        parent_series_id, parent_season_id = episode_to_parent.get(ep_id, (None, None))
        if parent_series_id:
            series_play_counts[parent_series_id] = (
                series_play_counts.get(parent_series_id, 0) + ep_count
            )
        if parent_season_id:
            season_play_counts[parent_season_id] = (
                season_play_counts.get(parent_season_id, 0) + ep_count
            )

    if not series_play_counts and not season_play_counts:
        return

    async with async_db() as session:
        updated_series = 0
        if series_play_counts:
            rows = (
                await session.execute(
                    select(
                        SeriesServiceRef.service_id,
                        Series.id,
                        Series.view_count,
                    )
                    .join(Series, SeriesServiceRef.series_id == Series.id)
                    .where(
                        SeriesServiceRef.service == server_service,
                        Series.removed_at.is_(None),
                    )
                )
            ).all()

            for service_id, series_id, current_count in rows:
                plugin_count = series_play_counts.get(service_id)
                if plugin_count and plugin_count > (current_count or 0):
                    await session.execute(
                        sql_update(Series)
                        .where(Series.id == series_id)
                        .values(view_count=plugin_count)
                    )
                    updated_series += 1

            if use_mapped_matches:
                mapped_rows = (
                    await session.execute(
                        select(
                            SupplementalMediaMatch.source_item_id,
                            Series.id,
                            Series.view_count,
                        )
                        .join(Series, SupplementalMediaMatch.series_id == Series.id)
                        .where(
                            SupplementalMediaMatch.source_service == server_service,
                            SupplementalMediaMatch.media_type == MediaType.SERIES,
                            SupplementalMediaMatch.series_id.is_not(None),
                            SupplementalMediaMatch.season_id.is_(None),
                            Series.removed_at.is_(None),
                        )
                    )
                ).all()
                for service_id, series_id, current_count in mapped_rows:
                    plugin_count = series_play_counts.get(service_id)
                    if plugin_count and plugin_count > (current_count or 0):
                        await session.execute(
                            sql_update(Series)
                            .where(Series.id == series_id)
                            .values(view_count=plugin_count)
                        )
                        updated_series += 1

        updated_seasons = 0
        if season_play_counts:
            season_service_col = (
                Season.jellyfin_season_id
                if server_service == Service.JELLYFIN
                else Season.emby_season_id
            )
            rows = (
                await session.execute(
                    select(
                        season_service_col,
                        Season.id,
                        Season.view_count,
                    ).where(season_service_col.is_not(None))
                )
            ).all()
            for service_id, season_id, current_count in rows:
                plugin_count = season_play_counts.get(service_id)
                if plugin_count and plugin_count > (current_count or 0):
                    await session.execute(
                        sql_update(Season)
                        .where(Season.id == season_id)
                        .values(view_count=plugin_count)
                    )
                    updated_seasons += 1

            if use_mapped_matches:
                mapped_rows = (
                    await session.execute(
                        select(
                            SupplementalMediaMatch.source_item_id,
                            Season.id,
                            Season.view_count,
                        )
                        .join(Season, SupplementalMediaMatch.season_id == Season.id)
                        .where(
                            SupplementalMediaMatch.source_service == server_service,
                            SupplementalMediaMatch.media_type == MediaType.SERIES,
                            SupplementalMediaMatch.season_id.is_not(None),
                        )
                    )
                ).all()
                for service_id, season_id, current_count in mapped_rows:
                    plugin_count = season_play_counts.get(service_id)
                    if plugin_count and plugin_count > (current_count or 0):
                        await session.execute(
                            sql_update(Season)
                            .where(Season.id == season_id)
                            .values(view_count=plugin_count)
                        )
                        updated_seasons += 1

        await session.commit()

    LOG.info(
        f"Updated view_count for {updated_series} series and {updated_seasons} "
        f"seasons from {server_service} Playback Reporting plugin"
    )

    #### episodes (individual, keyed by episode item ID) ####
    episode_service_col = (
        Episode.jellyfin_episode_id
        if server_service == Service.JELLYFIN
        else Episode.emby_episode_id
    )
    async with async_db() as session:
        ep_rows = (
            await session.execute(
                select(
                    episode_service_col,
                    Episode.id,
                    Episode.view_count,
                ).where(episode_service_col.is_not(None))
            )
        ).all()

        updated_episodes = 0
        for service_ep_id, ep_id, current_count in ep_rows:
            plugin_count = episode_stats.get(service_ep_id)
            if plugin_count and plugin_count > (current_count or 0):
                await session.execute(
                    sql_update(Episode)
                    .where(Episode.id == ep_id)
                    .values(view_count=plugin_count)
                )
                updated_episodes += 1

        await session.commit()

    LOG.info(
        f"Updated view_count for {updated_episodes} episodes from "
        f"{server_service} Playback Reporting plugin"
    )


async def sync_tautulli_playback_data() -> None:
    """Supplement movie and series view counts / last-viewed timestamps using
    Tautulli play history.

    Tautulli stores complete per-user play history that Plex's own API may under-
    report (e.g. plays from managed accounts, partially-counted plays).  The
    supplemental counts are applied as ``max(existing, tautulli_count)`` so
    existing data is never decreased.

    Movies are linked via ``MovieVersion.service_item_id`` (where service == PLEX).
    Series are linked via ``SeriesServiceRef.service_id`` (where service == PLEX)
    using series-level aggregation of episode history records.

    On first run a full history pull is performed.  On subsequent runs only
    records on/after ``last_synced_at`` (minus a 1-day overlap buffer) are
    fetched.  The timestamp is persisted in ``ServiceConfig.extra_settings``
    on the Tautulli config row.

    Does nothing if Tautulli is not configured/enabled.
    """
    if service_manager.tautulli is None:
        LOG.debug(
            "Skipping Tautulli supplemental sync: Tautulli client not initialized"
        )
        return

    async with async_db() as session:
        main = await _get_main_media_server(session)
    if not main:
        LOG.debug(
            "Skipping Tautulli supplemental sync: no main media server configured"
        )
        return

    # load Tautulli ServiceConfig to read/write last_synced_at
    async with async_db() as session:
        result = await session.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type == Service.TAUTULLI,
                ServiceConfig.enabled.is_(True),
            )
        )
        tautulli_config = result.scalar_one_or_none()
        plex_result = await session.execute(
            select(ServiceConfig.id).where(
                ServiceConfig.service_type == Service.PLEX,
                ServiceConfig.enabled.is_(True),
            )
        )
        has_enabled_plex = plex_result.first() is not None

    if tautulli_config is None:
        LOG.debug(
            "Skipping Tautulli supplemental sync: no enabled Tautulli service config"
        )
        return
    if not has_enabled_plex:
        LOG.debug("Skipping Tautulli supplemental sync: no enabled Plex service config")
        return
    use_mapped_matches = main.service_type != Service.PLEX

    LOG.info("Syncing Tautulli playback data (full pull)")

    try:
        movie_counts = await service_manager.tautulli.get_play_counts("movie")
        episode_counts = await service_manager.tautulli.get_play_counts("episode")
        season_counts = await service_manager.tautulli.get_play_counts(
            "episode", episode_key="parent_rating_key"
        )
        individual_episode_counts = await service_manager.tautulli.get_play_counts(
            "episode", episode_key="rating_key"
        )
    except Exception as e:
        LOG.error(f"Failed to fetch Tautulli history: {e}")
        return

    #### movies ####
    if movie_counts:
        LOG.info(
            f"Tautulli returned play data for {len(movie_counts)} unique movie rating keys"
        )
        async with async_db() as session:
            rows = (
                await session.execute(
                    select(
                        MovieVersion.service_item_id,
                        Movie.id,
                        Movie.view_count,
                        Movie.last_viewed_at,
                    )
                    .join(Movie, MovieVersion.movie_id == Movie.id)
                    .where(
                        MovieVersion.service == Service.PLEX,
                        Movie.removed_at.is_(None),
                    )
                )
            ).all()

            updated = 0
            for item_id, movie_id, current_count, current_lva in rows:
                entry = _play_entry(movie_counts, item_id)
                if not entry:
                    continue
                taut_count, taut_lva = entry
                new_count = max(current_count or 0, taut_count)
                new_lva = _merge_last_viewed(current_lva, taut_lva)
                if new_count != current_count or new_lva != current_lva:
                    await session.execute(
                        sql_update(Movie)
                        .where(Movie.id == movie_id)
                        .values(view_count=new_count, last_viewed_at=new_lva)
                    )
                    updated += 1

            if use_mapped_matches:
                mapped_rows = (
                    await session.execute(
                        select(
                            SupplementalMediaMatch.source_item_id,
                            Movie.id,
                            Movie.view_count,
                            Movie.last_viewed_at,
                        )
                        .join(Movie, SupplementalMediaMatch.movie_id == Movie.id)
                        .where(
                            SupplementalMediaMatch.source_service == Service.PLEX,
                            SupplementalMediaMatch.media_type == MediaType.MOVIE,
                            SupplementalMediaMatch.movie_id.is_not(None),
                            Movie.removed_at.is_(None),
                        )
                    )
                ).all()
                for item_id, movie_id, current_count, current_lva in mapped_rows:
                    entry = _play_entry(movie_counts, item_id)
                    if not entry:
                        continue
                    taut_count, taut_lva = entry
                    new_count = max(current_count or 0, taut_count)
                    new_lva = _merge_last_viewed(current_lva, taut_lva)
                    if new_count != current_count or new_lva != current_lva:
                        await session.execute(
                            sql_update(Movie)
                            .where(Movie.id == movie_id)
                            .values(view_count=new_count, last_viewed_at=new_lva)
                        )
                        updated += 1

            await session.commit()
        LOG.info(f"Updated {updated} movies from Tautulli playback data")
    else:
        LOG.debug("No movie play data returned from Tautulli")

    #### series (aggregated from episode-level data) ####
    if episode_counts:
        LOG.info(
            f"Tautulli returned episode play data for {len(episode_counts)} unique series rating keys"
        )
        async with async_db() as session:
            rows = (
                await session.execute(
                    select(
                        SeriesServiceRef.service_id,
                        Series.id,
                        Series.view_count,
                        Series.last_viewed_at,
                    )
                    .join(Series, SeriesServiceRef.series_id == Series.id)
                    .where(
                        SeriesServiceRef.service == Service.PLEX,
                        Series.removed_at.is_(None),
                    )
                )
            ).all()

            updated = 0
            for service_id, series_id, current_count, current_lva in rows:
                entry = _play_entry(episode_counts, service_id)
                if not entry:
                    continue
                taut_count, taut_lva = entry
                new_count = max(current_count or 0, taut_count)
                new_lva = _merge_last_viewed(current_lva, taut_lva)
                if new_count != current_count or new_lva != current_lva:
                    await session.execute(
                        sql_update(Series)
                        .where(Series.id == series_id)
                        .values(view_count=new_count, last_viewed_at=new_lva)
                    )
                    updated += 1

            if use_mapped_matches:
                mapped_rows = (
                    await session.execute(
                        select(
                            SupplementalMediaMatch.source_item_id,
                            Series.id,
                            Series.view_count,
                            Series.last_viewed_at,
                        )
                        .join(Series, SupplementalMediaMatch.series_id == Series.id)
                        .where(
                            SupplementalMediaMatch.source_service == Service.PLEX,
                            SupplementalMediaMatch.media_type == MediaType.SERIES,
                            SupplementalMediaMatch.series_id.is_not(None),
                            SupplementalMediaMatch.season_id.is_(None),
                            Series.removed_at.is_(None),
                        )
                    )
                ).all()
                for service_id, series_id, current_count, current_lva in mapped_rows:
                    entry = _play_entry(episode_counts, service_id)
                    if not entry:
                        continue
                    taut_count, taut_lva = entry
                    new_count = max(current_count or 0, taut_count)
                    new_lva = _merge_last_viewed(current_lva, taut_lva)
                    if new_count != current_count or new_lva != current_lva:
                        await session.execute(
                            sql_update(Series)
                            .where(Series.id == series_id)
                            .values(view_count=new_count, last_viewed_at=new_lva)
                        )
                        updated += 1

            await session.commit()
        LOG.info(f"Updated {updated} series from Tautulli playback data")
    else:
        LOG.debug("No episode play data returned from Tautulli")

    #### seasons (aggregated from episode-level data by parent season key) ####
    if season_counts:
        LOG.info(
            f"Tautulli returned episode play data for {len(season_counts)} unique season rating keys"
        )
        async with async_db() as session:
            rows = (
                await session.execute(
                    select(
                        Season.plex_season_rating_key,
                        Season.id,
                        Season.view_count,
                        Season.last_viewed_at,
                    ).where(Season.plex_season_rating_key.is_not(None))
                )
            ).all()

            updated = 0
            for service_id, season_id, current_count, current_lva in rows:
                entry = _play_entry(season_counts, service_id)
                if not entry:
                    continue
                taut_count, taut_lva = entry
                new_count = max(current_count or 0, taut_count)
                new_lva = _merge_last_viewed(current_lva, taut_lva)
                if new_count != current_count or new_lva != current_lva:
                    await session.execute(
                        sql_update(Season)
                        .where(Season.id == season_id)
                        .values(view_count=new_count, last_viewed_at=new_lva)
                    )
                    updated += 1

            if use_mapped_matches:
                mapped_rows = (
                    await session.execute(
                        select(
                            SupplementalMediaMatch.source_item_id,
                            Season.id,
                            Season.view_count,
                            Season.last_viewed_at,
                        )
                        .join(Season, SupplementalMediaMatch.season_id == Season.id)
                        .where(
                            SupplementalMediaMatch.source_service == Service.PLEX,
                            SupplementalMediaMatch.media_type == MediaType.SERIES,
                            SupplementalMediaMatch.season_id.is_not(None),
                        )
                    )
                ).all()
                for service_id, season_id, current_count, current_lva in mapped_rows:
                    entry = _play_entry(season_counts, service_id)
                    if not entry:
                        continue
                    taut_count, taut_lva = entry
                    new_count = max(current_count or 0, taut_count)
                    new_lva = _merge_last_viewed(current_lva, taut_lva)
                    if new_count != current_count or new_lva != current_lva:
                        await session.execute(
                            sql_update(Season)
                            .where(Season.id == season_id)
                            .values(view_count=new_count, last_viewed_at=new_lva)
                        )
                        updated += 1

            await session.commit()
        LOG.info(f"Updated {updated} seasons from Tautulli playback data")
    else:
        LOG.debug("No season play data returned from Tautulli")

    #### episodes (individual, keyed by episode rating_key) ####
    if individual_episode_counts:
        LOG.info(
            f"Tautulli returned individual play data for "
            f"{len(individual_episode_counts)} episode rating keys"
        )
        async with async_db() as session:
            rows = (
                await session.execute(
                    select(
                        Episode.plex_rating_key,
                        Episode.id,
                        Episode.view_count,
                        Episode.last_viewed_at,
                    ).where(Episode.plex_rating_key.is_not(None))
                )
            ).all()

            updated = 0
            for plex_key, ep_id, current_count, current_lva in rows:
                entry = _play_entry(individual_episode_counts, plex_key)
                if not entry:
                    continue
                taut_count, taut_lva = entry
                new_count = max(current_count or 0, taut_count)
                new_lva = _merge_last_viewed(current_lva, taut_lva)
                if new_count != current_count or new_lva != current_lva:
                    await session.execute(
                        sql_update(Episode)
                        .where(Episode.id == ep_id)
                        .values(view_count=new_count, last_viewed_at=new_lva)
                    )
                    updated += 1

            await session.commit()
        LOG.info(f"Updated {updated} episodes from Tautulli playback data")
    else:
        LOG.debug("No individual episode play data returned from Tautulli")

    LOG.info("Tautulli playback sync complete")


async def sync_media() -> dict[str, Any] | None:
    """
    Main sync tasks task.

    1. Sync libraries
    2. Sync movies
    3. Sync series
    4. Update watch data from any linked servers
    """
    if not service_manager.main_media_server:
        LOG.debug("No main media server configured - skipping sync")
        return

    # determine main server
    async with track_task_execution(Task.SYNC_MEDIA):
        async with async_db() as session:
            get_main_server = await _get_main_media_server(session)
            if not get_main_server:
                LOG.error("No main media server configured for sync")
                return
        main_server = get_main_server.service_type
        if main_server not in MEDIA_SERVERS:
            LOG.error(f"Unsupported main media server {main_server} for sync")
            return

        # update libraries
        library_sync_result = await sync_media_libraries()

        # sync movies
        await sync_movies(main_server)  # type: ignore[reportArgumentType]

        # sync series
        await sync_series(main_server)  # type: ignore[reportArgumentType]

        # sync linked watch data from any non-main servers
        async with async_db() as linked_session:
            all_servers = await _get_configured_media_servers(linked_session)
        active_linked_services = {
            svr.service_type
            for svr in all_servers
            if svr.service_type != main_server and svr.service_type in MEDIA_SERVERS
        }
        await _prune_supplemental_matches(active_linked_services)
        for svr in all_servers:
            if svr.service_type != main_server and svr.service_type in MEDIA_SERVERS:
                LOG.debug(f"Linked watch sync from {svr.service_type}")
                await sync_linked_data(svr.service_type)  # type: ignore[reportArgumentType]

        # gather supplemental sync data
        await _run_supplemental_syncs()

        return {"library_sync": library_sync_result}


async def sync_linked_data(
    service: MediaServerType,
) -> None:
    """
    Update watch data (view_count, last_viewed_at, never_watched) on existing Movie rows
    from a linked (non-main) media server. No version rows are written, but high
    confidence same-media supplemental identity mappings are refreshed.
    """
    async with track_task_execution(Task.SYNC_LINKED_DATA):
        LOG.info(f"Syncing linked data from {service}...")
        service_instance = await _get_media_service_instance(service)
        if not service_instance:
            await _clear_supplemental_matches(service)
            return

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
            await _clear_supplemental_matches(service)
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
                for movie in result.scalars().all():
                    view_count, last_viewed_at = watch_by_tmdb[movie.tmdb_id]
                    last_viewed_at = _merge_last_viewed(None, last_viewed_at)
                    changed = False
                    if view_count > movie.view_count:
                        movie.view_count = view_count
                        changed = True
                    if last_viewed_at and (
                        not movie.last_viewed_at
                        or last_viewed_at > movie.last_viewed_at
                    ):
                        movie.last_viewed_at = last_viewed_at
                        changed = True
                    if changed:
                        updated += 1
                await session.commit()

            LOG.info(f"Updated watch data from {service} for {updated} movies")
        else:
            LOG.debug(f"No linked movie watch data returned from {service}")

        async with async_db() as session:
            movie_matches = await _build_movie_supplemental_matches(
                session, service, aggregated
            )
            await _replace_supplemental_matches(
                session, service, MediaType.MOVIE, movie_matches
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
            await _clear_supplemental_matches(service, MediaType.SERIES)
            return

        async with async_db() as session:
            series_matches = await _build_series_supplemental_matches(
                session, service, aggregated_series
            )
            await _replace_supplemental_matches(
                session, service, MediaType.SERIES, series_matches
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
                        )
                        ep_updated_count += len(sd.episode_data)

                ep_series_count += 1
            await session.commit()
        LOG.info(
            f"Merged watch data + episode IDs from {service} for {ep_series_count} series "
            f"({ep_updated_count} episode records processed)"
        )


async def resync_media() -> None:
    """
    Full re-sync triggered when the main media server is switched.
    Wipes all MovieVersion and SeriesServiceRef rows (old server IDs are invalid
    for the new server), resets Movie.size and Series.size, then runs a full sync
    from the new main server.
    """
    if not service_manager.main_media_server:
        LOG.debug("No main media server configured - skipping resync")
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
            await _run_supplemental_syncs()
        except Exception as e:
            LOG.error(f"Error during main server resync: {e}", exc_info=True)
            raise


async def _update_series_tmdb_metadata(
    series: Series, tmdb_id: int, tmdb_service: AsyncTMDBClient
):
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
        series.vote_average = series_metadata.get("vote_average")
        series.vote_count = series_metadata.get("vote_count")
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
        LOG.debug("No main media server configured - skipping library sync")
        return {"libraries": [], "affected_rules": []}

    async with track_task_execution(Task.SYNC_MEDIA_LIBRARIES):
        async with async_db() as session:
            main = await _get_main_media_server(session)
        if not main:
            LOG.error("No main media server configured - skipping library sync")
            return {"libraries": [], "affected_rules": []}

        service_instance = await _get_media_service_instance(main.service_type)
        if not service_instance:
            return {"libraries": [], "affected_rules": []}

        movie_libs = await service_instance.get_movie_libraries()
        series_libs = await service_instance.get_series_libraries()

        async with async_db() as session:
            result = await session.execute(select(ServiceMediaLibrary))
            existing_map: dict[str, ServiceMediaLibrary] = {
                lib.library_id: lib for lib in result.scalars().all()
            }

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
                    if existing_map[lib_id].library_name != lib["name"]:
                        existing_map[lib_id].library_name = lib["name"]
                else:
                    session.add(
                        ServiceMediaLibrary(
                            library_id=lib_id,
                            library_name=lib["name"],
                            media_type=media_type,
                        )
                    )

            # delete libraries no longer present on the main server
            removed_ids: set[str] = set()
            for lib_id, lib in existing_map.items():
                if lib_id not in current_ids:
                    await session.delete(lib)
                    removed_ids.add(lib_id)

            # Advanced rules now keep library scope inside the rule definition.
            # We surface stale-library references through alerts instead of
            # mutating rule definitions during sync.
            affected_rules: list[dict[str, Any]] = []

            await session.commit()

            LOG.info(
                f"Updated service libraries: {len(current_ids)} total libraries "
                f"from {main.service_type}"
            )
            return {
                "libraries": current_libraries,
                "affected_rules": affected_rules,
            }
