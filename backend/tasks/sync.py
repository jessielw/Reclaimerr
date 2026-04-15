from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_tracking import track_task_execution
from backend.core.tmdb import AsyncTMDBClient
from backend.database import async_db
from backend.database.models import (
    Movie,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    ServiceMediaLibrary,
)
from backend.enums import MediaType, NotificationType, Service, Task
from backend.models.media import (
    AggregatedMovieData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    MovieVersionData,
)
from backend.models.services.radarr import RadarrMovie
from backend.models.services.sonarr import SonarrSeries
from backend.services.jellyfin import JellyfinService
from backend.services.notifications import notify_admins
from backend.services.plex import PlexService
from backend.types import MEDIA_SERVERS, MediaServerType

__all__ = [
    "sync_media",
    "resync_media",
    "sync_media_libraries",
    "sync_linked_data",
]

# number of records to process before committing to the database during sync tasks
COMMIT_BATCH_SIZE = 100


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
) -> JellyfinService | PlexService | None:
    """Return initialized media service instance for Plex/Jellyfin or None."""
    service_instance = await service_manager.return_service(service_type)
    if not service_instance:
        LOG.error(f"Service {service_type} not initialized")
        return None
    if not isinstance(service_instance, (JellyfinService, PlexService)):
        LOG.error(f"Service {service_type} is not a media server")
        return None
    return service_instance


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
    time_now = datetime.now(timezone.utc)

    # check for missing critical fields if not recently checked
    if (
        time_now - obj.last_metadata_refresh_at.replace(tzinfo=timezone.utc)
    ).days > 7 and (
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
        days_since_release = (time_now - release_date.replace(tzinfo=timezone.utc)).days
        days_since_refresh = (
            time_now - obj.last_metadata_refresh_at.replace(tzinfo=timezone.utc)
        ).days

        # if released within last 6 months and not refreshed in 30 days
        if days_since_release <= 180 and days_since_refresh > 30:
            return True

    return False


async def _sync_seasons(
    session: AsyncSession,
    series_id: int,
    season_data: list[AggregatedSeasonData],
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
            s.never_watched = sd.never_watched
            if sd.service_season_id:
                # detect whether this is plex (numeric ratingKey) or jellyfin (UUID)
                if len(sd.service_season_id) > 20:  # jellyfin UUIDs are longer
                    s.jellyfin_season_id = sd.service_season_id
                else:
                    s.plex_season_rating_key = sd.service_season_id
        else:
            jellyfin_id = None
            plex_key = None
            if sd.service_season_id:
                if len(sd.service_season_id) > 20:
                    jellyfin_id = sd.service_season_id
                else:
                    plex_key = sd.service_season_id
            session.add(
                Season(
                    series_id=series_id,
                    season_number=sd.season_number,
                    size=sd.size,
                    episode_count=sd.episode_count,
                    view_count=sd.view_count,
                    last_viewed_at=sd.last_viewed_at,
                    never_watched=sd.never_watched,
                    jellyfin_season_id=jellyfin_id,
                    plex_season_rating_key=plex_key,
                )
            )

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

    incoming_keys: set[tuple] = set()
    for ver in versions:
        key = (ver.service, ver.service_media_id)
        incoming_keys.add(key)
        if key in existing:
            ev = existing[key]
            ev.service_item_id = ver.service_item_id
            ev.library_id = ver.library_id
            ev.library_name = ver.library_name
            ev.path = ver.path
            ev.size = ver.size
            ev.container = ver.container
            if ver.added_at and not ev.added_at:
                ev.added_at = ver.added_at
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
                    container=ver.container,
                )
            )

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
            merged_versions = existing.versions + movie.versions
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
                premiere_date=existing.premiere_date or movie.premiere_date,
                versions=merged_versions,
                view_count=merged_view_count,
                last_viewed_at=merged_lva,
                never_watched=(merged_lva is None and merged_view_count == 0),
                played_by_user_count=max(pbu_candidates) if pbu_candidates else None,
            )

    if skipped_count > 0:
        LOG.warning(f"Skipped {skipped_count} movies without TMDB IDs")

    return unique_movies


async def gather_series(
    service: MediaServerType | None = None,
) -> dict[int, AggregatedSeriesData] | None:
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

    # deduplicate series, keeping the one with most recent watch date
    unique_series: dict[int, AggregatedSeriesData] = {}
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
                unique_series[tmdb_id] = series
            # if watch dates are equal/both None, prefer latest added_at
            elif series.last_viewed_at == existing.last_viewed_at:
                if series.added_at and (
                    not existing.added_at or series.added_at > existing.added_at
                ):
                    unique_series[tmdb_id] = series

    if skipped_count > 0:
        LOG.warning(f"Skipped {skipped_count} series without TMDB IDs")

    return unique_series


async def sync_movies(
    service: MediaServerType | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
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
    start_time = datetime.now(timezone.utc)

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

            parsed_tmdb_ids: set[int] = set()

            # if radarr is enabled collect it's ids
            radarr_movies: dict[int, RadarrMovie] = {}
            if service_manager.radarr:
                get_movies = await service_manager.radarr.get_all_movies()
                radarr_movies = {m.tmdb_id: m for m in get_movies if m.tmdb_id}

            # iterate through aggregated movies
            batch_count = 0
            for idx, movie in enumerate[AggregatedMovieData](
                aggregated_movies.values(), start=1
            ):
                tmdb_id = int(movie.external_ids.tmdb)
                parsed_tmdb_ids.add(tmdb_id)

                radarr_obj = (
                    radarr_movies.get(tmdb_id) if tmdb_id in radarr_movies else None
                )

                # earliest added_at across all versions
                earliest_added = min(
                    (v.added_at for v in movie.versions if v.added_at), default=None
                )

                # if movie already exists, update it
                if tmdb_id in existing_movies:
                    existing_movie = existing_movies[tmdb_id]

                    existing_movie.radarr_id = (
                        radarr_obj.id if radarr_obj else existing_movie.radarr_id
                    )
                    # update added_at if available and not already set
                    if earliest_added and not existing_movie.added_at:
                        existing_movie.added_at = earliest_added
                    existing_movie.last_viewed_at = movie.last_viewed_at
                    existing_movie.view_count = movie.view_count
                    existing_movie.never_watched = movie.never_watched

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
                    LOG.info(f"Adding new movie: {movie.name} ({tmdb_id})")
                    radarr_obj = (
                        radarr_movies.get(tmdb_id) if tmdb_id in radarr_movies else None
                    )
                    initial_size = sum(v.size for v in movie.versions)
                    new_movie = Movie(
                        title=movie.name,
                        year=movie.year,
                        tmdb_id=tmdb_id,
                        size=initial_size,
                        radarr_id=radarr_obj.id if radarr_obj else None,
                        imdb_id=movie.external_ids.imdb,
                        last_viewed_at=movie.last_viewed_at,
                        view_count=movie.view_count,
                        never_watched=movie.never_watched,
                    )

                    if earliest_added:
                        new_movie.added_at = earliest_added

                    # fetch TMDB metadata
                    await _update_movie_tmdb_metadata(new_movie, tmdb_id, tmdb_service)
                    session.add(new_movie)
                    # flush so new_movie.id is available for version FK
                    await session.flush()
                    for ver in movie.versions:
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
                                container=ver.container,
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
                        movie.removed_at = datetime.now(timezone.utc)
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

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            LOG.info(
                f"Movie sync ({effective_service.value}) completed successfully in {duration:.2f}s"
            )
            return parsed_tmdb_ids
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
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
        movie.last_metadata_refresh_at = datetime.now(timezone.utc)

    except Exception as e:
        LOG.error(f"Error updating TMDB metadata for movie {tmdb_id}: {e}")


async def sync_series(
    service: MediaServerType | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
    """Sync series from media servers to database."""
    start_time = datetime.now(timezone.utc)
    source_label = service.value if service else "all-media-services"
    LOG.info(f"Starting series sync ({source_label})...")

    aggregated_series = await gather_series(service)
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

            # track all tmdb_ids seen in this sync
            parsed_tmdb_ids = set[int]()

            # if sonarr is enabled collect its ids
            sonarr_series: dict[int, SonarrSeries] = {}
            if service_manager.sonarr:
                get_series = await service_manager.sonarr.get_all_series()
                sonarr_series = {s.tmdb_id: s for s in get_series if s.tmdb_id}

            # iterate through aggregated series
            batch_count = 0
            for idx, series in enumerate[AggregatedSeriesData](
                aggregated_series.values(), start=1
            ):
                tmdb_id = series.external_ids.tmdb
                parsed_tmdb_ids.add(tmdb_id)

                # match Sonarr by tmdb_id if available
                sonarr_obj = (
                    sonarr_series.get(tmdb_id) if tmdb_id in sonarr_series else None
                )

                # if series already exists, update it
                if tmdb_id in existing_series:
                    existing_series_obj = existing_series[tmdb_id]

                    # always update watch data, size, and file info from media server
                    existing_series_obj.size = series.size

                    # update service-specific fields based on source
                    await _upsert_series_service_ref(
                        session, existing_series_obj.id, series
                    )

                    existing_series_obj.sonarr_id = (
                        sonarr_obj.id if sonarr_obj else existing_series_obj.sonarr_id
                    )
                    # update added_at if available and not already set
                    if series.added_at and not existing_series_obj.added_at:
                        existing_series_obj.added_at = series.added_at
                    existing_series_obj.last_viewed_at = series.last_viewed_at
                    existing_series_obj.view_count = series.view_count
                    existing_series_obj.never_watched = series.never_watched

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
                        session, existing_series_obj.id, series.season_data
                    )

                # if series doesn't exist, create new entry
                else:
                    LOG.info(f"Adding new series: {series.name} ({tmdb_id})")
                    new_series = Series(
                        title=series.name,
                        year=series.year,
                        tmdb_id=tmdb_id,
                        size=series.size,
                        sonarr_id=sonarr_obj.id if sonarr_obj else None,
                        imdb_id=series.external_ids.imdb,
                        tvdb_id=series.external_ids.tvdb,
                        last_viewed_at=series.last_viewed_at,
                        view_count=series.view_count,
                        never_watched=series.never_watched,
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
                    await _upsert_series_service_ref(session, new_series.id, series)
                    # sync season data
                    await _sync_seasons(session, new_series.id, series.season_data)

                # commit in batches
                if idx % COMMIT_BATCH_SIZE == 0:
                    await session.commit()
                    batch_count += 1

            # commit any remaining series
            await session.commit()
            LOG.debug(
                f"Committed {len(aggregated_series)} series in {batch_count + 1} batches"
            )

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
                        s.removed_at = datetime.now(timezone.utc)
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

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            LOG.info(
                f"Series sync ({source_label}) completed successfully in {duration:.2f}s"
            )
            return parsed_tmdb_ids
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        LOG.critical(
            f"Error during series sync ({source_label}) after {duration:.2f}s: {e}",
            exc_info=True,
        )
        raise
    finally:
        await tmdb_service.session.close()


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
        for svr in all_servers:
            if svr.service_type != main_server and svr.service_type in MEDIA_SERVERS:
                LOG.debug(f"Linked watch sync from {svr.service_type}")
                await sync_linked_data(svr.service_type)  # type: ignore[reportArgumentType]

        return {"library_sync": library_sync_result}


async def sync_linked_data(
    service: MediaServerType,
) -> None:
    """
    Update watch data (view_count, last_viewed_at, never_watched) on existing Movie rows
    from a linked (non-main) media server.  No version rows are written.
    """
    async with track_task_execution(Task.SYNC_LINKED_DATA):
        LOG.info(f"Syncing linked data from {service}...")
        service_instance = await _get_media_service_instance(service)
        if not service_instance:
            return

        # fetch all libraries - linked servers don't have library selection
        aggregated = await service_instance.get_aggregated_movies(
            included_libraries=None
        )
        if not aggregated:
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

        if not watch_by_tmdb:
            return

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
                # normalize to naive UTC so comparison with DB values is safe
                if last_viewed_at is not None and last_viewed_at.tzinfo is not None:
                    last_viewed_at = last_viewed_at.replace(tzinfo=None)
                changed = False
                if view_count > movie.view_count:
                    movie.view_count = view_count
                    changed = True
                if last_viewed_at and (
                    not movie.last_viewed_at or last_viewed_at > movie.last_viewed_at
                ):
                    movie.last_viewed_at = last_viewed_at
                    movie.never_watched = False
                    changed = True
                if changed:
                    updated += 1
            await session.commit()

        LOG.info(f"Updated watch data from {service} for {updated} movies")


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
                await session.execute(sql_delete(MovieVersion))
                await session.execute(sql_delete(SeriesServiceRef))
                await session.execute(sql_update(Movie).values(size=0))
                await session.execute(sql_update(Series).values(size=0))
                await session.commit()
            LOG.info(
                "Cleared all MovieVersion and SeriesServiceRef rows for main server resync"
            )
            # sync libraries first so stale library IDs get scrubbed from rules
            # before the movie/series sync restores version data
            await sync_media_libraries()
            await sync_movies(allow_soft_delete=False)
            await sync_series(allow_soft_delete=False)
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
        series.last_metadata_refresh_at = datetime.now(timezone.utc)

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

            # scrub removed library IDs from any rules that reference them
            affected_rules: list[dict[str, Any]] = []
            if removed_ids:
                rules_result = await session.execute(
                    select(ReclaimRule).where(ReclaimRule.library_ids.is_not(None))
                )
                for rule in rules_result.scalars().all():
                    if rule.library_ids and any(
                        lid in removed_ids for lid in rule.library_ids
                    ):
                        removed_from_rule = [
                            lid for lid in rule.library_ids if lid in removed_ids
                        ]
                        cleaned = [
                            lid for lid in rule.library_ids if lid not in removed_ids
                        ]
                        rule.library_ids = cleaned if cleaned else None
                        affected_rules.append(
                            {
                                "id": rule.id,
                                "name": rule.name,
                                "removed_library_ids": removed_from_rule,
                                "remaining_library_ids": cleaned,
                            }
                        )
                        LOG.info(
                            f"Removed stale library IDs {removed_ids} from rule '{rule.name}'"
                        )

            await session.commit()

            if affected_rules:
                try:
                    await notify_admins(
                        notification_type=NotificationType.ADMIN_MESSAGE,
                        title="Rules Updated After Library Sync",
                        message=(
                            f"Library sync removed missing libraries from {len(affected_rules)} rule(s): "
                            + ", ".join(rule["name"] for rule in affected_rules)
                        ),
                    )
                except Exception as notify_error:
                    LOG.error(
                        f"Failed to notify admins about rule library cleanup: {notify_error}"
                    )

            LOG.info(
                f"Updated service libraries: {len(current_ids)} total libraries "
                f"from {main.service_type}"
            )
            return {
                "libraries": current_libraries,
                "affected_rules": affected_rules,
            }
