from datetime import datetime, timezone

from sqlalchemy import select

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.tmdb import AsyncTMDBClient
from backend.database.database import async_db
from backend.database.models import Movie, Series, ServiceConfig
from backend.enums import Service
from backend.models.clients.radarr import RadarrMovie
from backend.models.clients.sonarr import SonarrSeries
from backend.models.media import AggregatedMovieData, AggregatedSeriesData
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService

COMMIT_BATCH_SIZE = 100


async def gather_movies() -> dict[int, AggregatedMovieData] | None:
    """
    Fetch and combine movies from all configured media servers, deduplicating by TMDB ID. Only
    grabs libraries that are included in the service configuration.
    """
    # fetch service configs from database to generate combined aggregated movie list
    aggregated_movies = []
    async with async_db() as session:
        # get all enabled media servers with valid configs
        query = select(ServiceConfig).where(
            ServiceConfig.service_type.in_((Service.PLEX, Service.JELLYFIN)),
            ServiceConfig.enabled.is_(True),
            ServiceConfig.base_url.isnot(None),
            ServiceConfig.api_key.isnot(None),
        )
        result = await session.execute(query)
        media_servers = result.scalars().all()

        if not media_servers:
            return

        # fetch movies from each media server
        for server in media_servers:
            # get service instance (ensuring initialized and a supported media server)
            service = await service_manager.return_service(server.service_type)
            if not service:
                LOG.error(f"Service {server.service_type} not initialized")
                continue
            if not isinstance(service, (JellyfinService, PlexService)):
                LOG.error(f"Service {server.service_type} is not a media server")
                continue
            LOG.debug(
                f"Fetching movies from {server.service_type} at {server.base_url}"
            )

            # fetch aggregated movies
            extra_settings = server.extra_settings
            get_movies = await service.get_aggregated_movies(
                included_libraries=extra_settings.get("movies", {}).get(
                    "included_libraries"
                )
                if extra_settings
                else None
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


async def gather_series() -> dict[int, AggregatedSeriesData] | None:
    """Fetch and combine series from all configured media servers, deduplicating by TMDB ID."""
    aggregated_series = []
    async with async_db() as session:
        # get all enabled media servers with valid configs
        query = select(ServiceConfig).where(
            ServiceConfig.service_type.in_((Service.PLEX, Service.JELLYFIN)),
            ServiceConfig.enabled.is_(True),
            ServiceConfig.base_url.isnot(None),
            ServiceConfig.api_key.isnot(None),
        )
        result = await session.execute(query)
        media_servers = result.scalars().all()

        if not media_servers:
            return

        # fetch series from each media server
        for server in media_servers:
            service = await service_manager.return_service(server.service_type)
            if not service:
                LOG.error(f"Service {server.service_type} not initialized")
                continue
            if not isinstance(service, (JellyfinService, PlexService)):
                LOG.error(f"Service {server.service_type} is not a media server")
                continue
            LOG.debug(
                f"Fetching series from {server.service_type} at {server.base_url}"
            )

            # fetch aggregated series
            extra_settings = server.extra_settings
            get_series = await service.get_aggregated_series(
                included_libraries=extra_settings.get("series", {}).get(
                    "included_libraries"
                )
                if extra_settings
                else None
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


async def sync_movies():
    start_time = datetime.now(timezone.utc)
    LOG.info("Starting movie sync...")

    aggregated_movies = await gather_movies()
    if not aggregated_movies:
        LOG.info("No movies to sync from media servers")
        return
    LOG.info(f"Gathered {len(aggregated_movies)} unique movies from media servers")

    # tmdb service instance
    tmdb_service = AsyncTMDBClient()

    try:
        async with async_db() as session:
            # get all existing movies from database
            result = await session.execute(select(Movie))
            existing_movies_list = result.scalars().all()

            # convert to dictionary keyed by tmdb_id for easier lookup
            existing_movies = {m.tmdb_id: m for m in existing_movies_list if m.tmdb_id}

            # keep track of all tmdb_ids we've seen in this sync
            parsed_tmdb_ids = set[int]()

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
                    existing_movie.library_name = movie.library_name
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

                    # fetch TMDB metadata if missing or stale (30+ days old)
                    if not existing_movie.last_metadata_refresh_at:
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
                        library_name=movie.library_name,
                        radarr_id=radarr_obj.id if radarr_obj else None,
                        imdb_id=movie.external_ids.imdb,
                        last_viewed_at=movie.last_viewed_at,
                        view_count=movie.view_count,
                        never_watched=movie.never_watched,
                    )
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

            # soft-delete movies that no longer exist in media servers
            movies_to_delete = [
                movie
                for movie in existing_movies.values()
                if movie.tmdb_id not in parsed_tmdb_ids and not movie.removed_at
            ]

            if movies_to_delete:
                LOG.info(
                    f"Soft-deleting {len(movies_to_delete)} movies no longer in media servers"
                )
                for movie in movies_to_delete:
                    movie.removed_at = datetime.now(timezone.utc)
                    LOG.debug(f"Soft-deleted: {movie.title} ({movie.tmdb_id})")
                await session.commit()

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            LOG.info(f"Movie sync completed successfully in {duration:.2f}s")
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        LOG.critical(
            f"Error during movie sync after {duration:.2f}s: {e}", exc_info=True
        )
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
        movie.imdb_id = ext_ids.get("imdb_id")
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


async def sync_series():
    """Sync series from media servers to database."""
    start_time = datetime.now(timezone.utc)
    LOG.info("Starting series sync...")

    aggregated_series = await gather_series()
    if not aggregated_series:
        LOG.info("No series to sync from media servers")
        return
    LOG.info(f"Gathered {len(aggregated_series)} unique series from media servers")

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

                    # always update watch data and size from media server
                    existing_series_obj.size = series.size
                    existing_series_obj.library_name = series.library_name
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

                    # fetch TMDB metadata if missing
                    if not existing_series_obj.last_metadata_refresh_at:
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
                        library_name=series.library_name,
                        sonarr_id=sonarr_obj.id if sonarr_obj else None,
                        imdb_id=series.external_ids.imdb,
                        tvdb_id=series.external_ids.tvdb,
                        last_viewed_at=series.last_viewed_at,
                        view_count=series.view_count,
                        never_watched=series.never_watched,
                    )
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

            # soft-delete series that no longer exist in media servers
            series_to_delete = [
                s
                for s in existing_series.values()
                if s.tmdb_id not in parsed_tmdb_ids and not s.removed_at
            ]

            if series_to_delete:
                LOG.info(
                    f"Soft-deleting {len(series_to_delete)} series no longer in media servers"
                )
                for s in series_to_delete:
                    s.removed_at = datetime.now(timezone.utc)
                    LOG.debug(f"Soft-deleted: {s.title} ({s.tmdb_id})")
                await session.commit()

            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            LOG.info(f"Series sync completed successfully in {duration:.2f}s")
    except Exception as e:
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        LOG.critical(
            f"Error during series sync after {duration:.2f}s: {e}", exc_info=True
        )
    finally:
        await tmdb_service.session.close()


async def sync_all_media():
    """Main sync task - syncs both movies and series from media servers."""
    start_time = datetime.now(timezone.utc)
    LOG.info("Starting media sync task")
    failures = False
    try:
        # sync movies first
        await sync_movies()
    except Exception as e:
        failures = True
        LOG.critical(f"Media sync task failed: {e}", exc_info=True)

    try:
        # then sync series
        await sync_series()
    except Exception as e:
        failures = True
        LOG.critical(f"Media sync task failed: {e}", exc_info=True)

    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    if not failures:
        LOG.info(f"Media sync task completed successfully in {duration:.2f}s")
    else:
        LOG.error(f"Media sync task completed with failures after {duration:.2f}s")


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
        series.imdb_id = ext_ids.get("imdb_id")
        series.tvdb_id = str(ext_ids.get("tvdb_id")) if ext_ids.get("tvdb_id") else None
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


# async def reset_seerr_requests(deleted_items: list[dict]):
#     """
#     Reset requests in Seerr for deleted media items.

#     Args:
#         deleted_items: List of deleted media items with metadata
#     """
#     logger.info(f"Resetting Seerr requests for {len(deleted_items)} deleted items")

#     try:
#         # TODO: Implement Seerr request reset logic
#         # 1. For each deleted item, find corresponding request in Seerr
#         # 2. Reset/decline the request with appropriate message
#         # 3. Log results

#         logger.info("Seerr request reset completed successfully")
#     except Exception as e:
#         logger.error(f"Error resetting Seerr requests: {e}", exc_info=True)
