from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.tmdb import AsyncTMDBClient
from backend.database import async_db
from backend.database.models import (
    Movie,
    Series,
    ServiceConfig,
    ServiceMediaLibrary,
)
from backend.enums import MediaType, Service, Task
from backend.models.media import AggregatedMovieData, AggregatedSeriesData
from backend.models.services.radarr import RadarrMovie
from backend.models.services.sonarr import SonarrSeries
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.tasks.task_tracker import track_task_execution

__all__ = (
    "sync_plex_media",
    "sync_jellyfin_media",
    "sync_service_libraries",
)

COMMIT_BATCH_SIZE = 100

MEDIA_SERVICES: tuple[Literal[Service.PLEX, Service.JELLYFIN], ...] = (
    Service.PLEX,
    Service.JELLYFIN,
)


async def _get_configured_media_servers(
    session,
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
) -> list[ServiceConfig]:
    """Return enabled/valid configured media servers, optionally filtered by service."""
    query = select(ServiceConfig).where(
        ServiceConfig.service_type.in_(MEDIA_SERVICES),
        ServiceConfig.enabled.is_(True),
        ServiceConfig.base_url.isnot(None),
        ServiceConfig.api_key.isnot(None),
    )
    if service is not None:
        query = query.where(ServiceConfig.service_type == service)
    result = await session.execute(query)
    return result.scalars().all()


async def _get_selected_library_names(
    session,
    service_type: Service,
    media_type: MediaType,
) -> list[str] | None:
    """Return selected library names for the given service/media type."""
    lib_query = select(ServiceMediaLibrary).where(
        ServiceMediaLibrary.service_type == service_type,
        ServiceMediaLibrary.media_type == media_type,
        ServiceMediaLibrary.selected.is_(True),
    )
    lib_result = await session.execute(lib_query)
    selected_libraries = lib_result.scalars().all()
    if not selected_libraries:
        return None
    return [lib.library_name for lib in selected_libraries]


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


async def _sync_service_media(
    service: Literal[Service.PLEX, Service.JELLYFIN], task: Task
) -> None:
    """Run movie+series sync for a single service with task tracking."""
    async with track_task_execution(task):
        await sync_movies(service=service, allow_soft_delete=False)
        await sync_series(service=service, allow_soft_delete=False)


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


def _set_service_fields(
    db_obj: Movie | Series,
    aggregated_data: AggregatedMovieData | AggregatedSeriesData,
) -> None:
    """Set service-specific ID, library ID, library name, and path based on source service."""
    if aggregated_data.service is Service.PLEX:
        db_obj.plex_id = aggregated_data.id
        db_obj.plex_library_id = aggregated_data.library_id
        db_obj.plex_library_name = aggregated_data.library_name
        db_obj.plex_path = aggregated_data.path
    elif aggregated_data.service is Service.JELLYFIN:
        db_obj.jellyfin_id = aggregated_data.id
        db_obj.jellyfin_library_id = aggregated_data.library_id
        db_obj.jellyfin_library_name = aggregated_data.library_name
        db_obj.jellyfin_path = aggregated_data.path


async def gather_movies(
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
) -> dict[int, AggregatedMovieData] | None:
    """
    Fetch and combine movies from all configured media servers, deduplicating by TMDB ID. Only
    grabs libraries that are included in the service configuration.
    """
    # fetch service configs from database to generate combined aggregated movie list
    aggregated_movies = []
    async with async_db() as session:
        media_servers = await _get_configured_media_servers(session, service)

        if not media_servers:
            return

        # fetch movies from each media server
        for server in media_servers:
            service_instance = await _get_media_service_instance(server.service_type)
            if not service_instance:
                continue
            LOG.debug(
                f"Fetching movies from {server.service_type} at {server.base_url}"
            )

            included_library_names = await _get_selected_library_names(
                session,
                server.service_type,
                MediaType.MOVIE,
            )

            # fetch aggregated movies
            get_movies = await service_instance.get_aggregated_movies(
                included_libraries=included_library_names
            )
            if get_movies:
                aggregated_movies.extend(get_movies)
            LOG.debug(f"Fetched {len(get_movies)} movies from {server.service_type}")

    # deduplicate movies, keeping the one with most recent watch date
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
            # Keep movie with most recent watch date
            if movie.last_viewed_at and (
                not existing.last_viewed_at
                or movie.last_viewed_at > existing.last_viewed_at
            ):
                unique_movies[tmdb_id] = movie
            # If watch dates are equal/both None, prefer latest added_at
            elif movie.last_viewed_at == existing.last_viewed_at:
                if movie.added_at and (
                    not existing.added_at or movie.added_at > existing.added_at
                ):
                    unique_movies[tmdb_id] = movie

    if skipped_count > 0:
        LOG.warning(f"Skipped {skipped_count} movies without TMDB IDs")

    return unique_movies


async def gather_series(
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
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

            included_library_names = await _get_selected_library_names(
                session,
                server.service_type,
                MediaType.SERIES,
            )

            # fetch aggregated series
            get_series = await service_instance.get_aggregated_series(
                included_libraries=included_library_names
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
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
    allow_soft_delete: bool = True,
) -> set[int]:
    start_time = datetime.now(timezone.utc)
    source_label = service.value if service else "all-media-services"
    LOG.info(f"Starting movie sync ({source_label})...")

    aggregated_movies = await gather_movies(service)
    if not aggregated_movies:
        LOG.info(f"No movies to sync from {source_label}")
        return set()
    LOG.info(f"Gathered {len(aggregated_movies)} unique movies from {source_label}")

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

                # if movie already exists, update it
                if tmdb_id in existing_movies:
                    existing_movie = existing_movies[tmdb_id]

                    # always update watch data and size from media server
                    existing_movie.size = movie.size

                    # update service-specific fields based on source
                    _set_service_fields(existing_movie, movie)

                    existing_movie.radarr_id = (
                        radarr_obj.id if radarr_obj else existing_movie.radarr_id
                    )
                    # update added_at if available and not already set
                    if movie.added_at and not existing_movie.added_at:
                        existing_movie.added_at = movie.added_at
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

                # if movie doesn't exist, create new entry
                else:
                    LOG.info(f"Adding new movie: {movie.name} ({tmdb_id})")
                    radarr_obj = (
                        radarr_movies.get(tmdb_id) if tmdb_id in radarr_movies else None
                    )
                    new_movie = Movie(
                        title=movie.name,
                        year=movie.year,
                        tmdb_id=tmdb_id,
                        size=movie.size,
                        radarr_id=radarr_obj.id if radarr_obj else None,
                        imdb_id=movie.external_ids.imdb,
                        last_viewed_at=movie.last_viewed_at,
                        view_count=movie.view_count,
                        never_watched=movie.never_watched,
                    )

                    # set service-specific fields based on source
                    _set_service_fields(new_movie, movie)

                    # set added_at from media server if available
                    if movie.added_at:
                        new_movie.added_at = movie.added_at

                    # fetch TMDB metadata
                    await _update_movie_tmdb_metadata(new_movie, tmdb_id, tmdb_service)
                    session.add(new_movie)

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
                        f"Soft-deleting {len(movies_to_delete)} movies no longer in {source_label}"
                    )
                    for movie in movies_to_delete:
                        movie.removed_at = datetime.now(timezone.utc)
                        LOG.debug(f"Soft-deleted: {movie.title} ({movie.tmdb_id})")
                    await session.commit()

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            LOG.info(
                f"Movie sync ({source_label}) completed successfully in {duration:.2f}s"
            )
            return parsed_tmdb_ids
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        LOG.critical(
            f"Error during movie sync ({source_label}) after {duration:.2f}s: {e}",
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
            movie.tmdb_release_date = datetime.strptime(release_date, "%Y-%m-%d")

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
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
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
                tmdb_id = int(series.external_ids.tmdb)
                parsed_tmdb_ids.add(tmdb_id)

                # match Sonarr by tmdb_id if available
                tmdb_id = series.external_ids.tmdb
                sonarr_obj = (
                    sonarr_series.get(tmdb_id) if tmdb_id in sonarr_series else None
                )

                # if series already exists, update it
                if tmdb_id in existing_series:
                    existing_series_obj = existing_series[tmdb_id]

                    # always update watch data, size, and file info from media server
                    existing_series_obj.size = series.size

                    # update service-specific fields based on source
                    _set_service_fields(existing_series_obj, series)

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
                    _set_service_fields(new_series, series)

                    # set added_at from media server if available
                    if series.added_at:
                        new_series.added_at = series.added_at

                    # fetch TMDB metadata
                    await _update_series_tmdb_metadata(
                        new_series, tmdb_id, tmdb_service
                    )
                    session.add(new_series)

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
                    for s in series_to_delete:
                        s.removed_at = datetime.now(timezone.utc)
                        LOG.debug(f"Soft-deleted: {s.title} ({s.tmdb_id})")
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


async def sync_plex_media() -> None:
    """Sync Plex media only and track as dedicated task run."""
    await _sync_service_media(Service.PLEX, Task.SYNC_PLEX_MEDIA)


async def sync_jellyfin_media() -> None:
    """Sync Jellyfin media only and track as dedicated task run."""
    await _sync_service_media(Service.JELLYFIN, Task.SYNC_JELLYFIN_MEDIA)


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
            series.tmdb_first_air_date = datetime.strptime(first_air_date, "%Y-%m-%d")

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


async def sync_service_libraries(
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
) -> dict[Literal[Service.PLEX, Service.JELLYFIN], list[dict[str, Any]]]:
    """
    Update service libraries in the database from Plex and Jellyfin.

    Pass None for service to update both.
    """
    # only use task tracking when called as a scheduled task (service=None)
    if service is None:
        async with track_task_execution(Task.SYNC_SERVICE_LIBRARIES):
            return await _sync_service_libraries_impl(service)
    else:
        return await _sync_service_libraries_impl(service)


async def _sync_service_libraries_impl(
    service: Literal[Service.PLEX, Service.JELLYFIN] | None = None,
) -> dict[Literal[Service.PLEX, Service.JELLYFIN], list[dict[str, Any]]]:
    """Internal implementation for syncing service libraries."""
    if not service_manager.plex and not service_manager.jellyfin:
        return {Service.PLEX: [], Service.JELLYFIN: []}

    async with async_db() as session:
        # get existing libraries from database
        result = await session.execute(select(ServiceMediaLibrary))
        existing_libraries = result.scalars().all()

        # create a map of (service_type, library_id) -> ServiceMediaLibrary for quick lookup
        existing_map: dict[tuple[Service, str], ServiceMediaLibrary] = {
            (lib.service_type, lib.library_id): lib for lib in existing_libraries
        }

        # track all current library keys to identify deletions
        current_library_keys = set[tuple[Service, str]]()

        # for returning current libraries
        current_libraries: dict[
            Literal[Service.PLEX, Service.JELLYFIN], list[dict[str, Any]]
        ] = {Service.PLEX: [], Service.JELLYFIN: []}

        # only update the specified service if provided, else both
        update_jellyfin = (service is None and service_manager.jellyfin) or (
            service == Service.JELLYFIN and service_manager.jellyfin
        )
        update_plex = (service is None and service_manager.plex) or (
            service == Service.PLEX and service_manager.plex
        )

        # handle Jellyfin libraries
        if update_jellyfin and service_manager.jellyfin:
            jellyfin_movie_libs = await service_manager.jellyfin.get_movie_libraries()
            jellyfin_series_libs = await service_manager.jellyfin.get_series_libraries()

            # process movie libraries
            for lib in jellyfin_movie_libs:
                key = (Service.JELLYFIN, lib["id"])
                current_library_keys.add(key)
                current_libraries[Service.JELLYFIN].append(
                    {"id": lib["id"], "name": lib["name"], "type": MediaType.MOVIE}
                )

                if key in existing_map:
                    # update if name changed
                    if existing_map[key].library_name != lib["name"]:
                        existing_map[key].library_name = lib["name"]
                else:
                    # insert new library
                    new_lib = ServiceMediaLibrary(
                        service_type=Service.JELLYFIN,
                        library_id=lib["id"],
                        library_name=lib["name"],
                        media_type=MediaType.MOVIE,
                    )
                    session.add(new_lib)

            # process series libraries
            for lib in jellyfin_series_libs:
                key = (Service.JELLYFIN, lib["id"])
                current_library_keys.add(key)
                current_libraries[Service.JELLYFIN].append(
                    {"id": lib["id"], "name": lib["name"], "type": MediaType.SERIES}
                )

                if key in existing_map:
                    # update if name changed
                    if existing_map[key].library_name != lib["name"]:
                        existing_map[key].library_name = lib["name"]
                else:
                    # insert new library
                    new_lib = ServiceMediaLibrary(
                        service_type=Service.JELLYFIN,
                        library_id=lib["id"],
                        library_name=lib["name"],
                        media_type=MediaType.SERIES,
                    )
                    session.add(new_lib)

        # handle Plex libraries
        if update_plex and service_manager.plex:
            plex_movie_libs = await service_manager.plex.get_movie_libraries()
            plex_series_libs = await service_manager.plex.get_series_libraries()

            # process movie libraries
            for lib in plex_movie_libs:
                key = (Service.PLEX, lib["id"])
                current_library_keys.add(key)
                current_libraries[Service.PLEX].append(
                    {"id": lib["id"], "name": lib["name"], "type": MediaType.MOVIE}
                )

                if key in existing_map:
                    # update if name changed
                    if existing_map[key].library_name != lib["name"]:
                        existing_map[key].library_name = lib["name"]
                else:
                    # insert new library
                    new_lib = ServiceMediaLibrary(
                        service_type=Service.PLEX,
                        library_id=lib["id"],
                        library_name=lib["name"],
                        media_type=MediaType.MOVIE,
                    )
                    session.add(new_lib)

            # process series libraries
            for lib in plex_series_libs:
                key = (Service.PLEX, lib["id"])
                current_library_keys.add(key)
                current_libraries[Service.PLEX].append(
                    {"id": lib["id"], "name": lib["name"], "type": MediaType.SERIES}
                )

                if key in existing_map:
                    # update if name changed
                    if existing_map[key].library_name != lib["name"]:
                        existing_map[key].library_name = lib["name"]
                else:
                    # insert new library
                    new_lib = ServiceMediaLibrary(
                        service_type=Service.PLEX,
                        library_id=lib["id"],
                        library_name=lib["name"],
                        media_type=MediaType.SERIES,
                    )
                    session.add(new_lib)

        # delete libraries that no longer exist in any service
        for key, lib in existing_map.items():
            # only delete libraries for the updated service(s)
            if key not in current_library_keys:
                # if service is specified, only delete for that service
                if service is None or key[0] == service:
                    await session.delete(lib)

        await session.commit()
        LOG.info(
            f"Updated service libraries: {len(current_library_keys)} total libraries"
        )

        return current_libraries
