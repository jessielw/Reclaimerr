from __future__ import annotations

import os
from datetime import datetime

import niquests
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.logger import LOG
from backend.core.utils.request import should_retry_on_status
from backend.enums import Service
from backend.models.media import (
    AggregatedMovieData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    ExternalIDs,
    MovieVersionData,
)
from backend.models.services.plex import PlexMovie, PlexSeries


class PlexService:
    """Plex media server backend."""

    def __init__(self, token: str, plex_url: str) -> None:
        self.token = token
        self.plex_url = plex_url
        self.session = niquests.AsyncSession()
        self.session.headers.update(
            {
                "X-Plex-Token": self.token,
                "Accept": "application/json",
            }
        )

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self, endpoint: str, params: dict | None = None
    ) -> dict | list:
        """Make HTTP request to Plex API with automatic retry."""
        response = await self.session.get(f"{self.plex_url}/{endpoint}", params=params)
        response.raise_for_status()
        return response.json()

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("")
            return True
        except Exception:
            return False

    async def delete_item(self, rating_key: str) -> None:
        """Delete an item (movie or series) from Plex.

        Args:
            rating_key: Plex rating key (item ID)
        """
        try:
            response = await self.session.delete(
                f"{self.plex_url}/library/metadata/{rating_key}"
            )
            response.raise_for_status()
            LOG.debug(f"Deleted Plex item {rating_key}")
        except Exception as e:
            raise ValueError(f"Failed to delete Plex item {rating_key}: {e}")

    async def scan_item_path(self, item_path: str) -> bool:
        """Scan a specific item path in Plex library.

        This triggers Plex to scan the specific path, similar to how Radarr/Sonarr
        notify Plex to update specific movie/series paths after deletion.

        Args:
            item_path: Filesystem path to the item (e.g., '/movies/The Matrix (1999)')

        Returns:
            True if scan was triggered successfully, False otherwise
        """
        try:
            # plex's scan endpoint with path parameter scans the specific path
            # we need to find the section that contains this path first
            sections = await self.get_library_sections()

            # try to find which section this path belongs to by checking location paths
            for section in sections:
                section_id = section.get("key")
                if not section_id:
                    continue

                # get section details to check locations
                section_details = await self._make_request(
                    f"library/sections/{section_id}"
                )
                directories = section_details.get("MediaContainer", {}).get(  # pyright: ignore [reportAttributeAccessIssue]
                    "Directory", []
                )
                if not directories:
                    continue

                locations = (
                    directories[0].get("Location", [])
                    if isinstance(directories, list) and directories
                    else []
                )

                # check if item_path is within any of this section's locations
                for location in locations:
                    location_path = location.get("path", "")
                    if item_path.startswith(location_path):
                        # found the right section, trigger scan with specific path
                        response = await self.session.get(
                            f"{self.plex_url}/library/sections/{section_id}/refresh",
                            params={"path": item_path},
                        )
                        response.raise_for_status()
                        LOG.debug(
                            f"Triggered Plex scan for path: {item_path} in section {section_id}"
                        )
                        return True

            LOG.warning(f"Could not find Plex section for path: {item_path}")
            return False
        except Exception as e:
            LOG.error(f"Failed to scan Plex path {item_path}: {e}")
            return False

    async def _get_media_libraries(self, media_type: str) -> list[dict[str, str]]:
        """Get list of media libraries of a specific type with their IDs and names."""
        virtual_folders = await self._make_request("library/sections/all")  # pyright: ignore [reportAttributeAccessIssue]
        media_libs = []
        for section in virtual_folders.get("MediaContainer", {}).get("Directory", []):  # pyright: ignore [reportAttributeAccessIssue]
            if section.get("type") == media_type:
                item_id = section.get("uuid")
                name = section.get("title")
                if item_id and name:
                    media_libs.append({"id": item_id, "name": name})
        return media_libs

    async def get_movie_libraries(self) -> list[dict[str, str]]:
        """Get list of movie libraries with their IDs and names."""
        return await self._get_media_libraries("movie")

    async def get_series_libraries(self) -> list[dict[str, str]]:
        """Get list of TV series libraries with their IDs and names."""
        return await self._get_media_libraries("show")

    async def get_library_sections(self) -> list[dict]:
        """Get all library sections."""
        data = await self._make_request("library/sections")
        if not isinstance(data, dict):
            return []
        return data.get("MediaContainer", {}).get("Directory", [])  # pyright: ignore [reportAttributeAccessIssue]

    async def get_series_sizes_for_section(self, section_id: str) -> dict[str, int]:
        """Get total sizes for all series in a library section by fetching all episodes in one call.

        Args:
            section_id: The Plex library section ID

        Returns:
            Dictionary mapping series rating key to total size in bytes
        """
        sizes, _, _ = await self._get_episode_data_for_section(section_id)
        return sizes

    async def get_series_paths_for_section(self, section_id: str) -> dict[str, str]:
        """Get paths for all series in a library section by extracting from first episode.

        Args:
            section_id: The Plex library section ID

        Returns:
            Dictionary mapping series rating key to series directory path
        """
        _, paths, _ = await self._get_episode_data_for_section(section_id)
        return paths

    async def _get_episode_data_for_section(
        self, section_id: str
    ) -> tuple[
        dict[str, int], dict[str, str], dict[tuple[str, int], AggregatedSeasonData]
    ]:
        """Fetch all episodes for a section once and extract sizes, paths, and season data.

        Returns:
            (series_sizes, series_paths, season_data)
            - series_sizes: series ratingKey -> total bytes
            - series_paths: series ratingKey -> series dir path
            - season_data: (series ratingKey, season_number) -> AggregatedSeasonData
        """
        episodes_data = await self._make_request(
            f"library/sections/{section_id}/all",
            params={"type": 4},
        )
        if not episodes_data:
            return {}, {}, {}

        episodes = episodes_data.get("MediaContainer", {}).get("Metadata", [])  # pyright: ignore [reportAttributeAccessIssue]

        series_sizes: dict[str, int] = {}
        series_paths: dict[str, str] = {}
        # (series_key, season_number) -> accumulated size + episode count
        season_sizes: dict[tuple[str, int], int] = {}
        season_episode_counts: dict[tuple[str, int], int] = {}
        season_keys: dict[tuple[str, int], str] = {}  # season ratingKey
        season_last_viewed: dict[tuple[str, int], datetime | None] = {}
        season_view_counts: dict[tuple[str, int], int] = {}

        for episode in episodes:
            series_key = episode.get("grandparentRatingKey")
            season_key = episode.get("parentRatingKey")
            season_num_raw = episode.get("parentIndex")
            if not series_key or season_num_raw is None:
                continue
            try:
                season_num = int(season_num_raw)
            except (TypeError, ValueError):
                continue

            # compute episode file size
            episode_size = 0
            for media in episode.get("Media", []):
                for part in media.get("Part", []):
                    episode_size += part.get("size", 0)

            # accumulate series totals
            series_sizes[series_key] = series_sizes.get(series_key, 0) + episode_size

            # series path (first episode of each series)
            if series_key not in series_paths:
                media_list = episode.get("Media", [])
                if media_list and media_list[0].get("Part"):
                    ep_file = media_list[0]["Part"][0].get("file")
                    if ep_file:
                        series_paths[series_key] = os.path.dirname(
                            os.path.dirname(ep_file)
                        )

            # accumulate season totals
            sk = (series_key, season_num)
            season_sizes[sk] = season_sizes.get(sk, 0) + episode_size
            season_episode_counts[sk] = season_episode_counts.get(sk, 0) + 1
            if season_key and sk not in season_keys:
                season_keys[sk] = season_key

            # watch data per season
            ep_view_count = episode.get("viewCount", 0) or 0
            season_view_counts[sk] = season_view_counts.get(sk, 0) + ep_view_count
            if ep_view_count > 0 and episode.get("lastViewedAt"):
                try:
                    lva = datetime.fromtimestamp(
                        int(episode["lastViewedAt"])
                    ).astimezone()
                    prev = season_last_viewed.get(sk)
                    if prev is None or lva > prev:
                        season_last_viewed[sk] = lva
                except (TypeError, ValueError):
                    pass
            elif sk not in season_last_viewed:
                season_last_viewed[sk] = None

        # build AggregatedSeasonData objects
        season_data: dict[tuple[str, int], AggregatedSeasonData] = {}
        for sk, size in season_sizes.items():
            series_key, season_num = sk
            agg_view = season_view_counts.get(sk, 0)
            lva = season_last_viewed.get(sk)
            season_data[sk] = AggregatedSeasonData(
                service_series_id=series_key,
                season_number=season_num,
                size=size,
                episode_count=season_episode_counts.get(sk, 0),
                view_count=agg_view,
                last_viewed_at=lva,
                never_watched=(agg_view == 0),
                service_season_id=season_keys.get(sk),
            )

        return series_sizes, series_paths, season_data

    async def get_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[PlexMovie]:
        """Get all movies from all movie libraries.

        Args:
            included_libraries: List of library names to include (None for all)
        """
        sections = await self.get_library_sections()
        movie_sections = [s for s in sections if s.get("type") == "movie"]

        if included_libraries:
            movie_sections = [
                s for s in movie_sections if s.get("title") in included_libraries
            ]

        all_movies = []
        for section in movie_sections:
            section_id = section["key"]
            section_uuid = section.get("uuid")
            if not section_uuid:
                LOG.warning(f"Section {section_id} missing UUID, skipping")
                continue
            section_name = section.get("title", "Unknown")
            LOG.debug(f"Processing movie library: {section_name} (ID: {section_id})")

            # type=1 to only fetch movies, not collections
            # includeGuids=1 to get external IDs
            items_data = await self._make_request(
                f"library/sections/{section_id}/all",
                params={"type": 1, "includeGuids": 1},
            )
            if not items_data:
                continue
            items = items_data.get("MediaContainer", {}).get("Metadata", [])  # pyright: ignore [reportAttributeAccessIssue]

            for item in items:
                # only include actual movies, not collections or other types
                if item.get("type") != "movie":
                    continue

                ext_ids = self._parse_external_ids(item)
                if not ext_ids:
                    continue

                # build one MovieVersionData per Media entry (each = one physical file/version)
                added_at = (
                    datetime.fromtimestamp(item["addedAt"]).astimezone()
                    if item.get("addedAt")
                    else None
                )
                versions = []
                for media in item.get("Media", []):
                    media_id = str(media.get("id", ""))
                    if not media_id:
                        continue
                    part = media.get("Part", [{}])[0] if media.get("Part") else {}
                    version_size = sum(p.get("size", 0) for p in media.get("Part", []))
                    versions.append(
                        MovieVersionData(
                            service=Service.PLEX,
                            service_item_id=item["ratingKey"],
                            service_media_id=media_id,
                            library_id=section_uuid,
                            library_name=section_name,
                            path=part.get("file"),
                            size=version_size,
                            added_at=added_at,
                            container=media.get("container"),
                        )
                    )

                movie = PlexMovie(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_id=section_uuid,
                    library_name=section_name,
                    added_at=added_at,
                    updated_at=datetime.fromtimestamp(item["updatedAt"]).astimezone()
                    if item.get("updatedAt")
                    else None,
                    last_viewed_at=datetime.fromtimestamp(
                        item["lastViewedAt"]
                    ).astimezone()
                    if item.get("lastViewedAt")
                    else None,
                    view_count=item.get("viewCount", 0),
                    external_ids=ext_ids,
                    versions=versions,
                )
                all_movies.append(movie)

        return all_movies

    async def get_series(
        self, included_libraries: list[str] | None = None
    ) -> list[PlexSeries]:
        """Get all TV series from all show libraries.

        Args:
            included_libraries: List of library names to include (None for all)
        """
        sections = await self.get_library_sections()
        show_sections = [s for s in sections if s.get("type") == "show"]

        if included_libraries:
            show_sections = [
                s for s in show_sections if s.get("title") in included_libraries
            ]

        all_series = []
        for section in show_sections:
            section_id = section["key"]
            section_uuid = section.get("uuid")
            if not section_uuid:
                LOG.warning(f"Section {section_id} missing UUID, skipping")
                continue
            section_name = section.get("title", "Unknown")
            LOG.debug(f"Processing series library: {section_name} (ID: {section_id})")

            # fetch all episode sizes and paths for this section in one API call
            (
                series_sizes,
                series_paths,
                season_data_map,
            ) = await self._get_episode_data_for_section(section_id)

            # type=2 to only fetch shows, not collections
            # includeGuids=1 to get external IDs
            items_data = await self._make_request(
                f"library/sections/{section_id}/all",
                params={"type": 2, "includeGuids": 1},
            )
            if not items_data:
                continue
            items = items_data.get("MediaContainer", {}).get("Metadata", [])  # pyright: ignore [reportAttributeAccessIssue]

            for item in items:
                # only include actual shows, not collections or other types
                if item.get("type") != "show":
                    continue

                rating_key = str(item["ratingKey"])
                # get size and path from pre-calculated data
                total_size = series_sizes.get(rating_key, 0)
                series_path = series_paths.get(rating_key)

                ext_ids = self._parse_external_ids(item)
                if not ext_ids:
                    continue

                series = PlexSeries(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_id=section_uuid,
                    library_name=section_name,
                    path=series_path,
                    added_at=datetime.fromtimestamp(item["addedAt"]).astimezone()
                    if item.get("addedAt")
                    else None,
                    updated_at=datetime.fromtimestamp(item["updatedAt"]).astimezone()
                    if item.get("updatedAt")
                    else None,
                    last_viewed_at=datetime.fromtimestamp(
                        item["lastViewedAt"]
                    ).astimezone()
                    if item.get("lastViewedAt")
                    else None,
                    view_count=item.get("viewCount", 0),
                    external_ids=ext_ids,
                    size=total_size,
                    season_data=list(
                        v for k, v in season_data_map.items() if k[0] == rating_key
                    ),
                )
                all_series.append(series)

        return all_series

    async def get_aggregated_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data with optional section inclusion."""
        movies = await self.get_movies(included_libraries=included_libraries)

        return [
            AggregatedMovieData(
                name=m.name,
                year=m.year,
                premiere_date=None,  # plex doesn't provide premiere date directly
                external_ids=m.external_ids,
                versions=m.versions,
                view_count=m.view_count,
                last_viewed_at=m.last_viewed_at,
                never_watched=(m.view_count == 0),
                played_by_user_count=None,  # plex provides global counts, not per-user
            )
            for m in movies
        ]

    async def get_aggregated_series(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data with optional section inclusion."""
        series = await self.get_series(included_libraries=included_libraries)

        return [
            AggregatedSeriesData(
                id=s.id,
                name=s.name,
                year=s.year,
                service=Service.PLEX,
                library_id=s.library_id,
                library_name=s.library_name,
                path=s.path,
                added_at=s.added_at,
                premiere_date=None,  # plex doesn't provide premiere date directly
                external_ids=s.external_ids,
                size=s.size,
                view_count=s.view_count,
                last_viewed_at=s.last_viewed_at,
                never_watched=(s.view_count == 0),
                played_by_user_count=None,  # plex provides global counts, not per-user
                season_data=s.season_data,
            )
            for s in series
        ]

    @staticmethod
    def _parse_external_ids(media: dict) -> ExternalIDs | None:
        imdb_id = None
        tmdb_id = None
        tvdb_id = None

        guids = media.get("Guid", [])
        for guid in guids:
            guid_id = guid.get("id", "")
            if guid_id.startswith("imdb://"):
                imdb_id = guid_id.replace("imdb://", "")
            elif guid_id.startswith("tmdb://"):
                tmdb_id = int(guid_id.replace("tmdb://", ""))
            elif guid_id.startswith("tvdb://"):
                tvdb_id = guid_id.replace("tvdb://", "")

        if not tmdb_id:
            return None

        if tmdb_id or imdb_id or tvdb_id:
            return ExternalIDs(
                tmdb=tmdb_id,
                imdb=imdb_id,
                tmdb_collection=None,
                tvdb=tvdb_id,
            )
        return None

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Plex service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                url,
                headers={"X-Plex-Token": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
