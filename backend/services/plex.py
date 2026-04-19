from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import niquests
from niquests.exceptions import ReadTimeout
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.logger import LOG
from backend.core.tmdb import AsyncTMDBClient
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

# history tuple (total_view_count, max_last_viewed_at, distinct_user_count)
_HistEntry = tuple[int, datetime | None, int]


class PlexService:
    """Plex media server backend."""

    def __init__(self, token: str, plex_url: str) -> None:
        self.token = token
        self.plex_url = plex_url.rstrip("/")
        self.session = niquests.AsyncSession(timeout=300)
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
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self, endpoint: str, params: dict | None = None, **kwargs
    ) -> tuple[dict | list, int | None]:
        """Make HTTP request to Plex API with automatic retry."""
        response = await self.session.get(
            f"{self.plex_url}/{endpoint}", params=params, **kwargs
        )
        response.raise_for_status()
        return response.json(), response.status_code

    @staticmethod
    def _fromtimestamp(ts: Any) -> datetime | None:
        """Safe wrapper around datetime.fromtimestamp (returns None for invalid/out of range values)."""
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            response, status_code = await self._make_request("identity")
            if status_code == 200 and self._check_plex_healthy(response):
                return True
        except Exception:
            pass
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
                section_details, _ = await self._make_request(
                    f"library/sections/{section_id}",
                    timeout=60,
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
        virtual_folders, _ = await self._make_request("library/sections/all")  # pyright: ignore [reportAttributeAccessIssue]
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
        data, _ = await self._make_request("library/sections")
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
        episodes_data, _ = await self._make_request(
            f"library/sections/{section_id}/all",
            params={"type": 4},
            timeout=300,
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
            if episode.get("lastViewedAt"):
                try:
                    lva = datetime.fromtimestamp(
                        int(episode["lastViewedAt"]), tz=timezone.utc
                    )
                    prev = season_last_viewed.get(sk)
                    if prev is None or lva > prev:
                        season_last_viewed[sk] = lva
                except (TypeError, ValueError, OSError):
                    if sk not in season_last_viewed:
                        season_last_viewed[sk] = None
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
                never_watched=(agg_view == 0 and lva is None),
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
            items_data, _ = await self._make_request(
                f"library/sections/{section_id}/all",
                params={"type": 1, "includeGuids": 1},
                timeout=300,
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
                added_at = self._fromtimestamp(item.get("addedAt"))
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
                    updated_at=self._fromtimestamp(item.get("updatedAt")),
                    last_viewed_at=self._fromtimestamp(item.get("lastViewedAt")),
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
            items_data, _ = await self._make_request(
                f"library/sections/{section_id}/all",
                params={"type": 2, "includeGuids": 1},
                timeout=300,
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
                    # fallback (legacy TVDB agent, we'll resolve to TMDB via API)
                    tvdb_id = self._extract_legacy_tvdb_id(item)
                    if tvdb_id:
                        tmdb_id = await self._resolve_tvdb_to_tmdb(tvdb_id)
                        if tmdb_id:
                            ext_ids = ExternalIDs(
                                tmdb=tmdb_id,
                                imdb=None,
                                tmdb_collection=None,
                                tvdb=tvdb_id,
                            )
                if not ext_ids:
                    continue

                series = PlexSeries(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_id=section_uuid,
                    library_name=section_name,
                    path=series_path,
                    added_at=self._fromtimestamp(item.get("addedAt")),
                    updated_at=self._fromtimestamp(item.get("updatedAt")),
                    last_viewed_at=self._fromtimestamp(item.get("lastViewedAt")),
                    view_count=item.get("viewCount", 0),
                    external_ids=ext_ids,
                    size=total_size,
                    season_data=list(
                        v for k, v in season_data_map.items() if k[0] == rating_key
                    ),
                )
                all_series.append(series)

        return all_series

    async def _get_all_history(
        self,
        rating_keys: set[str] | None = None,
        history_type: str = "movie",
        key_field: str = "ratingKey",
    ) -> dict[str, _HistEntry]:
        """Fetch watch history for ALL users from /status/sessions/history/all.

        Uses the admin token so records from every account on the server are returned.
        Paginates automatically until all records are parsed.

        Args:
            rating_keys: Optional set of ratingKey/parentRatingKey/grandparentRatingKey
                values to keep.  Records not matching are skipped early.
            history_type: Plex metadata type string passed as ``type`` param
                ("movie" = movies, "episode" = episodes).
            key_field: Which field on each history record to use as the dict key
                ("ratingKey", "parentRatingKey", or "grandparentRatingKey").

        Returns:
            Dict mapping the chosen key field value to
            (total_view_count, max_last_viewed_at, distinct_user_count).
        """
        PAGE_SIZE = 1000
        container_start = 0
        # key -> [view_count, max_lva, set(accountIDs)]
        aggregated: dict[str, list] = {}

        type_map = {"movie": 1, "episode": 4}
        plex_type = type_map.get(history_type)

        while True:
            params: dict = {
                "X-Plex-Container-Start": container_start,
                "X-Plex-Container-Size": PAGE_SIZE,
                "sort": "viewedAt:desc",
            }
            if plex_type:
                params["type"] = plex_type

            try:
                data, _ = await self._make_request(
                    "status/sessions/history/all", params=params, timeout=300
                )
            except Exception as e:
                LOG.warning(
                    f"Plex history fetch failed at offset {container_start}: {e}"
                )
                break

            if not isinstance(data, dict):
                break

            container = data.get("MediaContainer", {})
            records = container.get("Metadata", [])
            if not records:
                break

            for record in records:
                key = str(record.get(key_field, ""))
                if not key:
                    continue
                if rating_keys is not None and key not in rating_keys:
                    continue

                viewed_at = self._fromtimestamp(record.get("viewedAt"))
                account_id = str(record.get("accountID", ""))

                if key not in aggregated:
                    aggregated[key] = [0, None, set()]

                aggregated[key][0] += 1  # each history record = one play
                if viewed_at:
                    if aggregated[key][1] is None or viewed_at > aggregated[key][1]:
                        aggregated[key][1] = viewed_at
                if account_id:
                    aggregated[key][2].add(account_id)

            # check if there are more pages
            total_size = container.get("size", len(records))
            container_start += len(records)
            if container_start >= total_size:
                break

        return {k: (v[0], v[1], len(v[2])) for k, v in aggregated.items()}

    async def get_aggregated_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data with optional section inclusion."""
        movies = await self.get_movies(included_libraries=included_libraries)

        # build ratingKey set for movies so history fetch can be scoped
        movie_keys = {m.id for m in movies}
        history = await self._get_all_history(rating_keys=movie_keys)

        return [
            AggregatedMovieData(
                name=m.name,
                year=m.year,
                premiere_date=None,  # plex doesn't provide premiere date directly
                external_ids=m.external_ids,
                versions=m.versions,
                view_count=self._merge_view_count(m.view_count, history.get(m.id)),
                last_viewed_at=self._merge_last_viewed(
                    m.last_viewed_at, history.get(m.id)
                ),
                never_watched=self._merge_never_watched(
                    m.view_count, m.last_viewed_at, history.get(m.id)
                ),
                played_by_user_count=history[m.id][2] if m.id in history else None,
            )
            for m in movies
        ]

    async def get_aggregated_series(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data with optional section inclusion."""
        series = await self.get_series(included_libraries=included_libraries)

        # collect all series and season rating keys to scope the history fetch
        series_keys: set[str] = set()
        season_keys: set[str] = set()
        for s in series:
            series_keys.add(s.id)
            for sd in s.season_data:
                if sd.service_season_id:
                    season_keys.add(sd.service_season_id)

        # fetch full cross user history keyed by season ratingKey
        # (episodes roll up to parentRatingKey = season, grandparentRatingKey = series)
        season_history = await self._get_all_history(
            rating_keys=season_keys, history_type="episode"
        )
        series_history = await self._get_all_history(
            rating_keys=series_keys,
            history_type="episode",
            key_field="grandparentRatingKey",
        )

        result = []
        for s in series:
            merged_seasons = []
            for sd in s.season_data:
                hist = (
                    season_history.get(sd.service_season_id or "")
                    if sd.service_season_id
                    else None
                )
                merged_seasons.append(
                    AggregatedSeasonData(
                        service_series_id=sd.service_series_id,
                        season_number=sd.season_number,
                        size=sd.size,
                        episode_count=sd.episode_count,
                        view_count=self._merge_view_count(sd.view_count, hist),
                        last_viewed_at=self._merge_last_viewed(sd.last_viewed_at, hist),
                        never_watched=self._merge_never_watched(
                            sd.view_count, sd.last_viewed_at, hist
                        ),
                        air_date=sd.air_date,
                        service_season_id=sd.service_season_id,
                    )
                )

            s_hist = series_history.get(s.id)
            result.append(
                AggregatedSeriesData(
                    id=s.id,
                    name=s.name,
                    year=s.year,
                    service=Service.PLEX,
                    library_id=s.library_id,
                    library_name=s.library_name,
                    path=s.path,
                    added_at=s.added_at,
                    premiere_date=None,
                    external_ids=s.external_ids,
                    size=s.size,
                    view_count=self._merge_view_count(s.view_count, s_hist),
                    last_viewed_at=self._merge_last_viewed(s.last_viewed_at, s_hist),
                    never_watched=self._merge_never_watched(
                        s.view_count, s.last_viewed_at, s_hist
                    ),
                    played_by_user_count=s_hist[2] if s_hist else None,
                    season_data=merged_seasons,
                )
            )
        return result

    async def _resolve_tvdb_to_tmdb(self, tvdb_id: str) -> int | None:
        """Resolve a TVDB series ID to a TMDB series ID via the TMDB /find endpoint."""
        try:
            async with AsyncTMDBClient() as tmdb:
                data = await tmdb.find_by_external_id(tvdb_id, "tvdb_id")
            if not data or data is False:
                return None
            results = data.get("tv_results", [])  # type: ignore[reportAttributeAccessIssue]
            if results:
                return int(results[0]["id"])
        except Exception:
            LOG.warning(f"Failed to resolve TVDB ID {tvdb_id!r} to TMDB ID")
        return None

    @staticmethod
    def _parse_external_ids(media: dict) -> ExternalIDs | None:
        imdb_id = None
        tmdb_id = None
        tvdb_id = None

        guids = media.get("Guid", [])
        if guids:
            # newer plex agent format
            for guid in guids:
                guid_id = guid.get("id", "")
                if guid_id.startswith("imdb://"):
                    imdb_id = guid_id.replace("imdb://", "")
                elif guid_id.startswith("tmdb://"):
                    raw = guid_id.replace("tmdb://", "")
                    if not raw.isdigit():
                        LOG.warning(
                            f"Skipping media item: invalid TMDb ID '{raw}' in Plex GUID"
                        )
                        continue
                    tmdb_id = int(raw)
                elif guid_id.startswith("tvdb://"):
                    tvdb_id = guid_id.replace("tvdb://", "")
        else:
            # legacy plex agent format (single top level guid attribute)
            legacy_guid = media.get("guid", "")
            if "agents.themoviedb://" in legacy_guid:
                raw = legacy_guid.split("://", 1)[1].split("?")[0].strip("/")
                if raw.isdigit():
                    tmdb_id = int(raw)
                else:
                    LOG.warning(
                        f"Skipping media item: invalid legacy TMDb ID '{raw}' in Plex GUID"
                    )

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
    def _extract_legacy_tvdb_id(media: dict) -> str | None:
        """Extract a TVDB ID from a legacy Plex agent GUID attribute.

        Handles both show level and episode level GUIDs:
          com.plexapp.agents.thetvdb://301824?lang=en       -> "301824"
          com.plexapp.agents.thetvdb://301824/1/1?lang=en   -> "301824"
        """
        guid = media.get("guid", "")
        if "agents.thetvdb://" not in guid:
            return None
        raw = guid.split("://", 1)[1].split("?")[0].split("/")[0]
        return raw if raw.isdigit() else None

    @staticmethod
    def _merge_view_count(scan_count: int, hist: _HistEntry | None) -> int:
        """Return the higher of the library-scan view count and the history count."""
        if hist is None:
            return scan_count
        return max(scan_count, hist[0])

    @staticmethod
    def _merge_last_viewed(
        scan_lva: datetime | None, hist: _HistEntry | None
    ) -> datetime | None:
        """Return the most recent last-viewed timestamp between scan and history."""
        if hist is None:
            return scan_lva
        hist_lva = hist[1]
        if scan_lva is None:
            return hist_lva
        if hist_lva is None:
            return scan_lva
        return max(scan_lva, hist_lva)

    @staticmethod
    def _merge_never_watched(
        scan_count: int, scan_lva: datetime | None, hist: _HistEntry | None
    ) -> bool:
        """Item is never-watched only if both the scan AND the history show zero plays."""
        if hist is not None and (hist[0] > 0 or hist[1] is not None):
            return False
        return scan_count == 0 and scan_lva is None

    @staticmethod
    def _check_plex_healthy(response: Any) -> bool:
        """Check if the Plex /identity response indicates a healthy connection.

        We'll verify the response is a dict and has the expected structure:
        ```python
        {'MediaContainer': {'size': 0, 'apiVersion': '1.2.0', 'claimed': True,
        'machineIdentifier': 'someMachineID', 'version': '1.43.1.10611-1e34174b1'}}
        ```
        """
        return (
            isinstance(response, dict)
            and "MediaContainer" in response
            and isinstance(response["MediaContainer"], dict)
            and "machineIdentifier" in response["MediaContainer"]
        )

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Plex service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/identity",
                headers={"X-Plex-Token": api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            try:
                data = response.json()
                if PlexService._check_plex_healthy(data):
                    return True
            except Exception:
                pass  # we can just pass and raise the ValueError
            raise ValueError(
                f"Unexpected response (status code: {response.status_code})"
            )
