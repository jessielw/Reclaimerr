from __future__ import annotations

from datetime import datetime
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
from backend.core.utils.request import should_retry_on_status
from backend.enums import Service
from backend.models.media import (
    AggregatedMovieData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    ExternalIDs,
    MovieVersionData,
)
from backend.models.services.jellyfin import (
    JellyfinMovie,
    JellyfinSeries,
    JellyfinUser,
    JellyfinUserData,
)


class JellyfinService:
    """Jellyfin media server backend."""

    def __init__(self, api_key: str, jellyfin_url: str) -> None:
        self.api_key = api_key
        self.jellyfin_url = jellyfin_url.rstrip("/")
        self.session = niquests.AsyncSession(timeout=300)
        self.session.headers.update(
            {
                "X-Emby-Token": self.api_key,
                "accept": "application/json",
            }
        )
        # cache of library ID to library name mapping
        self._library_map: dict[str, str] | None = None

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
    ) -> list | dict:
        """Make HTTP request to Jellyfin API with automatic retry."""
        response = await self.session.get(
            f"{self.jellyfin_url}/{endpoint}",
            params=params,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("System/Info")
            return True
        except Exception:
            return False

    async def delete_item(self, item_id: str) -> None:
        """Delete an item (movie or series) from Jellyfin.

        Args:
            item_id: Jellyfin item ID
        """
        try:
            response = await self.session.delete(f"{self.jellyfin_url}/Items/{item_id}")
            response.raise_for_status()
            LOG.debug(f"Deleted Jellyfin item {item_id}")
        except Exception as e:
            raise ValueError(f"Failed to delete Jellyfin item {item_id}: {e}")

    async def scan_item_path(self, item_path: str) -> bool:
        """Refresh a specific item by its filesystem path in Jellyfin.

        This triggers Jellyfin to scan the specific path, similar to how Radarr/Sonarr
        notify Jellyfin to update specific movie/series paths after deletion.

        Args:
            item_path: Filesystem path to the item (e.g., '/movies/The Matrix (1999)')

        Returns:
            True if refresh was triggered successfully, False otherwise
        """
        try:
            # jellyfin's Library/Media/Updated endpoint accepts path-specific updates
            response = await self.session.post(
                f"{self.jellyfin_url}/Library/Media/Updated",
                json={"Updates": [{"Path": item_path, "UpdateType": "Deleted"}]},
            )
            response.raise_for_status()
            LOG.debug(f"Triggered Jellyfin refresh for path: {item_path}")
            return True
        except Exception as e:
            LOG.error(f"Failed to refresh Jellyfin path {item_path}: {e}")
            return False

    async def _get_media_libraries(self, media_type: str) -> list[dict[str, str]]:
        """Get list of media libraries of a specific type with their IDs and names."""
        virtual_folders = await self._make_request(
            "Library/VirtualFolders", timeout=300
        )  # pyright: ignore [reportAttributeAccessIssue]
        media_libs = []
        for vf in virtual_folders:
            if vf.get("CollectionType") == media_type:
                item_id = vf.get("ItemId")
                name = vf.get("Name")
                if item_id and name:
                    media_libs.append({"id": item_id, "name": name})
        return media_libs

    async def get_movie_libraries(self) -> list[dict[str, str]]:
        """Get list of movie libraries with their IDs and names."""
        return await self._get_media_libraries("movies")

    async def get_series_libraries(self) -> list[dict[str, str]]:
        """Get list of TV series libraries with their IDs and names."""
        return await self._get_media_libraries("tvshows")

    async def get_users(self) -> list[JellyfinUser]:
        """Get all Jellyfin users."""
        users_data = await self._make_request("Users")
        if not users_data:
            return []
        return [JellyfinUser(name=user["Name"], id=user["Id"]) for user in users_data]

    async def get_movies_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        filters: dict | None = None,
    ) -> list[JellyfinMovie]:
        """Get movies for a specific user, optionally filtered by library.

        Args:
            user_id: The Jellyfin user ID
            library_id: Library ID to query
            library_name: The name of the library
            filters: Additional query filters
        """
        params = {
            "userId": user_id,
            "includeItemTypes": "Movie",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            "Fields": "ProviderIds,MediaSources,DateCreated,Path",
            "ParentId": library_id,
        }

        if filters:
            params.update(filters)
        get_data = await self._make_request("Items", params=params, timeout=300)
        if not get_data:
            return []
        items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

        data = []
        for item in items_data:
            provider_ids = item.get("ProviderIds", {})
            tmdb_id = provider_ids.get("Tmdb")
            if not tmdb_id:
                continue  # skip items without TMDB ID (will be logged after aggregation)
            if not str(tmdb_id).isdigit():
                LOG.warning(
                    f"Skipping movie '{item.get('Name', item.get('Id'))}': "
                    f"invalid TMDb ID '{tmdb_id}' in Jellyfin ProviderIds"
                )
                continue
            user_data = JellyfinUserData(
                id=item["UserData"]["ItemId"],
                key=item["UserData"]["Key"],
                play_count=item["UserData"]["PlayCount"],
                last_played_date=datetime.fromisoformat(
                    item["UserData"]["LastPlayedDate"]
                )
                if item["UserData"].get("LastPlayedDate")
                else None,
                played=item["UserData"]["Played"],
            )
            external_ids = ExternalIDs(
                imdb=provider_ids.get("Imdb"),
                tmdb=int(tmdb_id),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )
            # build one MovieVersionData per MediaSource (each = one physical file)
            added_at = (
                datetime.fromisoformat(item["DateCreated"])
                if item.get("DateCreated")
                else None
            )
            versions = [
                MovieVersionData(
                    service=Service.JELLYFIN,
                    service_item_id=item["Id"],
                    service_media_id=source["Id"],
                    library_id=library_id,
                    library_name=library_name,
                    path=source.get("Path"),
                    size=source.get("Size", 0),
                    added_at=added_at,
                    container=source.get("Container"),
                )
                for source in item.get("MediaSources", [])
                if source.get("Id")
            ]
            movie = JellyfinMovie(
                id=item["Id"],
                name=item["Name"],
                year=item.get("ProductionYear"),
                premiere_date=datetime.fromisoformat(item["PremiereDate"])
                if item.get("PremiereDate")
                else None,
                date_created=added_at,
                library_id=library_id,
                library_name=library_name,
                external_ids=external_ids,
                versions=versions,
                user_data=user_data,
            )
            data.append(movie)
        return data

    async def get_series_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        series_sizes: dict[str, int] | None = None,
        series_dates: dict[str, datetime] | None = None,
        filters: dict | None = None,
    ) -> list[JellyfinSeries]:
        """Get TV series for a specific user, optionally filtered by library.

        Args:
            user_id: The Jellyfin user ID
            library_id: Library ID to query
            library_name: The name of the library
            series_sizes: Pre-calculated series sizes (series_id -> total bytes)
            series_dates: Pre-calculated series dates (series_id -> oldest episode DateCreated)
            filters: Additional query filters
        """
        params = {
            "userId": user_id,
            "includeItemTypes": "Series",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            "Fields": "ProviderIds,Path",
            "ParentId": library_id,
        }

        if filters:
            params.update(filters)
        get_data = await self._make_request("Items", params=params, timeout=300)
        if not get_data:
            return []
        items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

        data = []
        for item in items_data:
            provider_ids = item.get("ProviderIds", {})
            tmdb_id = provider_ids.get("Tmdb")
            if not tmdb_id:
                continue  # skip items without TMDB ID
            if not str(tmdb_id).lstrip("-").isdigit():
                LOG.warning(
                    f"Skipping series '{item.get('Name', item.get('Id'))}': "
                    f"invalid TMDb ID '{tmdb_id}' in Jellyfin ProviderIds"
                )
                continue
            user_data = JellyfinUserData(
                id=item["UserData"]["ItemId"],
                key=item["UserData"]["Key"],
                play_count=item["UserData"]["PlayCount"],
                last_played_date=datetime.fromisoformat(
                    item["UserData"]["LastPlayedDate"]
                )
                if item["UserData"].get("LastPlayedDate")
                else None,
                played=item["UserData"]["Played"],
            )
            provider_ids = item.get("ProviderIds", {})
            external_ids = ExternalIDs(
                imdb=provider_ids.get("Imdb"),
                tmdb=int(tmdb_id),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )

            # get size from pre-calculated series sizes (if available)
            total_size = series_sizes.get(item["Id"], 0) if series_sizes else 0
            # get oldest episode date (if available)
            series_date = series_dates.get(item["Id"]) if series_dates else None

            series = JellyfinSeries(
                id=item["Id"],
                name=item["Name"],
                year=item.get("ProductionYear"),
                premiere_date=datetime.fromisoformat(item["PremiereDate"])
                if item.get("PremiereDate")
                else None,
                date_created=series_date,
                library_id=library_id,
                library_name=library_name,
                path=item.get("Path"),
                external_ids=external_ids,
                size=total_size,
                user_data=user_data,
            )
            data.append(series)
        return data

    async def get_series_sizes_for_library(
        self, library_id: str, user_id: str
    ) -> tuple[
        dict[str, int], dict[str, datetime], dict[tuple[str, int], AggregatedSeasonData]
    ]:
        """Get total sizes, oldest DateCreated, and season data for all series in a library.

        Args:
            library_id: The Jellyfin library ID
            user_id: The user ID to fetch episodes for

        Returns:
            Tuple of (series_sizes, series_dates, season_data) where:
            - series_sizes: Dictionary mapping series_id to total size in bytes
            - series_dates: Dictionary mapping series_id to oldest episode DateCreated
            - season_data: Dictionary mapping (series_id, season_number) to AggregatedSeasonData
        """
        series_sizes: dict[str, int] = {}
        series_dates: dict[str, datetime] = {}
        # season accumulation
        season_sizes: dict[tuple[str, int], int] = {}
        season_episode_counts: dict[tuple[str, int], int] = {}
        season_view_counts: dict[tuple[str, int], int] = {}
        season_last_viewed: dict[tuple[str, int], datetime | None] = {}
        season_ids: dict[tuple[str, int], str] = {}  # jellyfin SeasonId

        start_index = 0
        limit = 500

        while True:
            params = {
                "userId": user_id,
                "includeItemTypes": "Episode",
                "recursive": "true",
                "Fields": "MediaSources,SeriesId,DateCreated,ParentIndexNumber,SeasonId,UserData",
                "ParentId": library_id,
                "StartIndex": str(start_index),
                "Limit": str(limit),
            }

            get_data = await self._make_request("Items", params=params, timeout=60)
            if not get_data:
                break

            episodes = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]
            if not episodes:
                break

            LOG.debug(
                f"Processing episodes {start_index} to {start_index + len(episodes)}"
            )

            for episode in episodes:
                series_id = episode.get("SeriesId")
                if not series_id:
                    continue

                # sum sizes
                episode_size = 0
                for source in episode.get("MediaSources", []):
                    episode_size += source.get("Size", 0)

                series_sizes[series_id] = series_sizes.get(series_id, 0) + episode_size

                # track oldest DateCreated
                if episode.get("DateCreated"):
                    episode_date = datetime.fromisoformat(episode["DateCreated"])
                    if (
                        series_id not in series_dates
                        or episode_date < series_dates[series_id]
                    ):
                        series_dates[series_id] = episode_date

                # season accumulation
                season_num_raw = episode.get("ParentIndexNumber")
                if season_num_raw is None:
                    continue
                try:
                    season_num = int(season_num_raw)
                except (TypeError, ValueError):
                    continue

                sk = (series_id, season_num)
                season_sizes[sk] = season_sizes.get(sk, 0) + episode_size
                season_episode_counts[sk] = season_episode_counts.get(sk, 0) + 1

                # store jellyfin SeasonId for first episode of each season
                if sk not in season_ids and episode.get("SeasonId"):
                    season_ids[sk] = episode["SeasonId"]

                # watch data
                user_data = episode.get("UserData", {})
                play_count = user_data.get("PlayCount", 0) or 0
                season_view_counts[sk] = season_view_counts.get(sk, 0) + play_count
                if play_count > 0 and user_data.get("LastPlayedDate"):
                    try:
                        lva = datetime.fromisoformat(user_data["LastPlayedDate"])
                        prev = season_last_viewed.get(sk)
                        if prev is None or lva > prev:
                            season_last_viewed[sk] = lva
                    except (TypeError, ValueError):
                        pass
                elif sk not in season_last_viewed:
                    season_last_viewed[sk] = None

            total_record_count = get_data.get("TotalRecordCount", 0)  # pyright: ignore [reportAttributeAccessIssue]
            start_index += len(episodes)
            if start_index >= total_record_count:
                break

        # build AggregatedSeasonData objects
        season_data: dict[tuple[str, int], AggregatedSeasonData] = {}
        for sk, size in season_sizes.items():
            series_id, season_num = sk
            agg_view = season_view_counts.get(sk, 0)
            lva = season_last_viewed.get(sk)
            season_data[sk] = AggregatedSeasonData(
                service_series_id=series_id,
                season_number=season_num,
                size=size,
                episode_count=season_episode_counts.get(sk, 0),
                view_count=agg_view,
                last_viewed_at=lva,
                never_watched=(agg_view == 0),
                service_season_id=season_ids.get(sk),
            )

        return series_sizes, series_dates, season_data

    async def get_all_watched_episodes_for_user(
        self, user_id: str
    ) -> dict[str, datetime]:
        """Get all watched episodes for a user and return a map of seriesId -> most recent watch date.

        This is much more efficient than querying each series individually.
        """
        series_watch_dates: dict[str, datetime] = {}

        try:
            # fetch in paginated batches to avoid timeout on large libraries
            start_index = 0
            limit = 500

            while True:
                params = {
                    "userId": user_id,
                    "includeItemTypes": "Episode",
                    "recursive": "true",
                    "Filters": "IsPlayed",
                    "Fields": "SeriesId",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "StartIndex": str(start_index),
                    "Limit": str(limit),
                }
                get_data = await self._make_request("Items", params=params, timeout=60)
                if not get_data:
                    break

                items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]
                if not items_data:
                    break

                # group by series and keep the most recent watch date
                for item in items_data:
                    series_id = item.get("SeriesId")
                    last_played = item.get("UserData", {}).get("LastPlayedDate")

                    if series_id and last_played:
                        watch_date = datetime.fromisoformat(last_played)

                        # keep the most recent date (items are already sorted by DatePlayed descending)
                        if series_id not in series_watch_dates:
                            series_watch_dates[series_id] = watch_date
                        elif watch_date > series_watch_dates[series_id]:
                            series_watch_dates[series_id] = watch_date

                # check if we've fetched all episodes
                total_record_count = get_data.get("TotalRecordCount", 0)  # pyright: ignore [reportAttributeAccessIssue]
                start_index += len(items_data)
                if start_index >= total_record_count:
                    break

            return series_watch_dates
        except Exception:
            return {}

    async def get_aggregated_movies(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data across all users with optional library filters."""
        movie_data: dict[str, dict[str, Any]] = {}

        # get all movie libraries
        all_libraries = await self.get_movie_libraries()

        # filter to included libraries if specified
        if included_libraries:
            libraries_to_query = [
                lib for lib in all_libraries if lib["name"] in included_libraries
            ]
        else:
            libraries_to_query = all_libraries

        for library in libraries_to_query:
            library_id = library["id"]
            library_name = library["name"]
            LOG.debug(f"Processing movie library: {library_name} (ID: {library_id})")

            for user in await self.get_users():
                user_movies = await self.get_movies_for_user(
                    user.id, library_id=library_id, library_name=library_name
                )

                for movie in user_movies:
                    if movie.id not in movie_data:
                        # first time seeing this movie - capture versions once (same files for all users)
                        movie_data[movie.id] = {
                            "name": movie.name,
                            "year": movie.year,
                            "premiere_date": movie.premiere_date,
                            "external_ids": movie.external_ids,
                            "versions": list(movie.versions),
                            "view_count": movie.user_data.play_count
                            if movie.user_data
                            else 0,
                            "last_viewed_at": movie.user_data.last_played_date
                            if movie.user_data
                            else None,
                            "played_by_user_count": 1
                            if (movie.user_data and movie.user_data.played)
                            else 0,
                        }
                    else:
                        # aggregate data
                        existing = movie_data[movie.id]
                        if movie.user_data:
                            existing["view_count"] += movie.user_data.play_count
                            if movie.user_data.last_played_date:
                                if (
                                    not existing["last_viewed_at"]
                                    or movie.user_data.last_played_date
                                    > existing["last_viewed_at"]
                                ):
                                    existing["last_viewed_at"] = (
                                        movie.user_data.last_played_date
                                    )
                            if movie.user_data.played:
                                existing["played_by_user_count"] += 1

        # convert to final format
        return [
            AggregatedMovieData(
                name=data["name"],
                year=data["year"],
                premiere_date=data["premiere_date"],
                external_ids=data["external_ids"],
                versions=data["versions"],
                view_count=data["view_count"],
                last_viewed_at=data["last_viewed_at"],
                never_watched=(data["played_by_user_count"] == 0),
                played_by_user_count=data["played_by_user_count"],
            )
            for data in movie_data.values()
        ]

    async def get_aggregated_series(
        self, included_libraries: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data across all users with optional library inclusion.

        Note: Jellyfin doesn't populate LastPlayedDate at the series level, so we check
        episodes to get accurate watch dates. We optimize by getting all watched episodes
        per user in a single API call instead of querying each series individually.
        """
        series_data: dict[str, dict[str, Any]] = {}

        # get all TV libraries
        all_libraries = await self.get_series_libraries()

        # filter to included libraries if specified
        if included_libraries:
            libraries_to_query = [
                lib for lib in all_libraries if lib["name"] in included_libraries
            ]
        else:
            libraries_to_query = all_libraries

        for library in libraries_to_query:
            library_id = library["id"]
            library_name = library["name"]
            LOG.debug(f"Processing series library: {library_name} (ID: {library_id})")

            # get first user to fetch series sizes (sizes are same for all users)
            users = await self.get_users()
            if not users:
                continue

            # fetch series sizes once per library (not per user as this is expensive)
            (
                series_sizes,
                series_dates,
                season_data_map,
            ) = await self.get_series_sizes_for_library(library_id, users[0].id)

            for user in users:
                # get all watched episodes for this user in one API call
                user_series_watch_dates = await self.get_all_watched_episodes_for_user(
                    user.id
                )

                # get series list for this user from this library
                user_series = await self.get_series_for_user(
                    user.id,
                    library_id=library_id,
                    library_name=library_name,
                    series_sizes=series_sizes,
                    series_dates=series_dates,
                )

                for series in user_series:
                    # get watch date from our pre-fetched data
                    episode_last_watched = user_series_watch_dates.get(series.id)

                    if series.id not in series_data:
                        # first time seeing this series
                        series_data[series.id] = {
                            "id": series.id,
                            "name": series.name,
                            "year": series.year,
                            "service": Service.JELLYFIN,
                            "library_id": series.library_id,
                            "library_name": series.library_name,
                            "path": series.path,
                            "added_at": series.date_created,
                            "premiere_date": series.premiere_date,
                            "external_ids": series.external_ids,
                            "size": series.size,
                            "view_count": series.user_data.play_count
                            if series.user_data
                            else 0,
                            "last_viewed_at": episode_last_watched,
                            "played_by_user_count": 1 if episode_last_watched else 0,
                            "season_data": [
                                v
                                for k, v in season_data_map.items()
                                if k[0] == series.id
                            ],
                        }
                    else:
                        # aggregate data
                        existing = series_data[series.id]
                        if series.user_data:
                            existing["view_count"] += series.user_data.play_count

                        # update last_viewed_at if this user's episodes were watched more recently
                        if episode_last_watched:
                            if (
                                not existing["last_viewed_at"]
                                or episode_last_watched > existing["last_viewed_at"]
                            ):
                                existing["last_viewed_at"] = episode_last_watched
                            existing["played_by_user_count"] += 1

        # convert to final format
        return [
            AggregatedSeriesData(
                **{k: v for k, v in data.items() if k != "season_data"},
                never_watched=(data["played_by_user_count"] == 0),
                season_data=data.get("season_data", []),
            )
            for data in series_data.values()
        ]

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Jellyfin service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/System/Info",
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
