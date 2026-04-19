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
from backend.models.services.emby import (
    EmbyMovie,
    EmbySeries,
    EmbyUser,
    EmbyUserData,
)


class EmbyService:
    """Emby media server backend."""

    def __init__(self, api_key: str, emby_url: str) -> None:
        self.api_key = api_key
        self.emby_url = emby_url.rstrip("/")
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
        """Make HTTP request to Emby API with automatic retry."""
        response = await self.session.get(
            f"{self.emby_url}/{endpoint}",
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
        """Delete an item (movie or series) from Emby.

        Args:
            item_id: Emby item ID
        """
        try:
            response = await self.session.delete(f"{self.emby_url}/Items/{item_id}")
            response.raise_for_status()
            LOG.debug(f"Deleted Emby item {item_id}")
        except Exception as e:
            raise ValueError(f"Failed to delete Emby item {item_id}: {e}")

    async def scan_item_path(self, item_path: str) -> bool:
        """Refresh a specific item by its filesystem path in Emby.

        Args:
            item_path: Filesystem path to the item (e.g., '/movies/The Matrix (1999)')

        Returns:
            True if refresh was triggered successfully, False otherwise
        """
        try:
            response = await self.session.post(
                f"{self.emby_url}/Library/Media/Updated",
                json={"Updates": [{"Path": item_path, "UpdateType": "Deleted"}]},
            )
            response.raise_for_status()
            LOG.debug(f"Triggered Emby refresh for path: {item_path}")
            return True
        except Exception as e:
            LOG.error(f"Failed to refresh Emby path {item_path}: {e}")
            return False

    async def _get_media_libraries(self, media_type: str) -> list[dict[str, str]]:
        """Get list of media libraries of a specific type with their IDs and names."""
        virtual_folders = await self._make_request(
            "Library/VirtualFolders", timeout=300
        )
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

    async def get_users(self) -> list[EmbyUser]:
        """Get all Emby users."""
        users_data = await self._make_request("Users")
        if not users_data:
            return []
        return [EmbyUser(name=user["Name"], id=user["Id"]) for user in users_data]

    async def get_movies_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        filters: dict | None = None,
    ) -> list[EmbyMovie]:
        """Get movies for a specific user, optionally filtered by library.

        Args:
            user_id: The Emby user ID
            library_id: Library ID to query
            library_name: The name of the library
            filters: Additional query filters
        """
        params = {
            "userId": user_id,
            "includeItemTypes": "Movie",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            "Fields": "ProviderIds,MediaSources,DateCreated,Path,UserData,UserDataLastPlayedDate,UserDataPlayCount,ProductionYear,PremiereDate",
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
            if not str(tmdb_id).isdigit():
                LOG.warning(
                    f"Skipping movie '{item.get('Name', item.get('Id'))}': "
                    f"invalid TMDb ID '{tmdb_id}' in Emby ProviderIds"
                )
                continue
            user_data_raw = item.get("UserData", {})
            user_data = EmbyUserData(
                id=item["Id"],
                key=user_data_raw.get("Key", ""),
                play_count=user_data_raw.get("PlayCount", 0),
                last_played_date=datetime.fromisoformat(user_data_raw["LastPlayedDate"])
                if user_data_raw.get("LastPlayedDate")
                else None,
                played=user_data_raw.get("Played", False),
            )
            external_ids = ExternalIDs(
                imdb=provider_ids.get("Imdb"),
                tmdb=int(tmdb_id),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )
            added_at = (
                datetime.fromisoformat(item["DateCreated"])
                if item.get("DateCreated")
                else None
            )
            versions = [
                MovieVersionData(
                    service=Service.EMBY,
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
            movie = EmbyMovie(
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
    ) -> list[EmbySeries]:
        """Get TV series for a specific user, optionally filtered by library.

        Args:
            user_id: The Emby user ID
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
            "Fields": "ProviderIds,Path,UserData,UserDataLastPlayedDate,UserDataPlayCount,ProductionYear,PremiereDate",
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
                    f"invalid TMDb ID '{tmdb_id}' in Emby ProviderIds"
                )
                continue
            user_data_raw = item.get("UserData", {})
            user_data = EmbyUserData(
                id=item["Id"],
                key=user_data_raw.get("Key", ""),
                play_count=user_data_raw.get("PlayCount", 0),
                last_played_date=datetime.fromisoformat(user_data_raw["LastPlayedDate"])
                if user_data_raw.get("LastPlayedDate")
                else None,
                played=user_data_raw.get("Played", False),
            )
            external_ids = ExternalIDs(
                imdb=provider_ids.get("Imdb"),
                tmdb=int(tmdb_id),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )

            total_size = series_sizes.get(item["Id"], 0) if series_sizes else 0
            series_date = series_dates.get(item["Id"]) if series_dates else None

            series = EmbySeries(
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
            library_id: The Emby library ID
            user_id: The user ID to fetch episodes for

        Returns:
            Tuple of (series_sizes, series_dates, season_data) where:
            - series_sizes: Dictionary mapping series_id to total size in bytes
            - series_dates: Dictionary mapping series_id to oldest episode DateCreated
            - season_data: Dictionary mapping (series_id, season_number) to AggregatedSeasonData
        """
        series_sizes: dict[str, int] = {}
        series_dates: dict[str, datetime] = {}
        season_sizes: dict[tuple[str, int], int] = {}
        season_episode_counts: dict[tuple[str, int], int] = {}
        season_view_counts: dict[tuple[str, int], int] = {}
        season_last_viewed: dict[tuple[str, int], datetime | None] = {}
        season_ids: dict[tuple[str, int], str] = {}  # emby SeasonId

        start_index = 0
        limit = 500

        while True:
            params = {
                "userId": user_id,
                "includeItemTypes": "Episode",
                "recursive": "true",
                "Fields": "MediaSources,SeriesId,DateCreated,ParentIndexNumber,SeasonId,UserData,UserDataLastPlayedDate,UserDataPlayCount",
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

                episode_size = 0
                for source in episode.get("MediaSources", []):
                    episode_size += source.get("Size", 0)

                series_sizes[series_id] = series_sizes.get(series_id, 0) + episode_size

                if episode.get("DateCreated"):
                    episode_date = datetime.fromisoformat(episode["DateCreated"])
                    if (
                        series_id not in series_dates
                        or episode_date < series_dates[series_id]
                    ):
                        series_dates[series_id] = episode_date

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

                if sk not in season_ids and episode.get("SeasonId"):
                    season_ids[sk] = episode["SeasonId"]

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
        """Get all watched episodes for a user and return a map of seriesId -> most recent watch date."""
        series_watch_dates: dict[str, datetime] = {}

        try:
            start_index = 0
            limit = 500

            while True:
                params = {
                    "userId": user_id,
                    "includeItemTypes": "Episode",
                    "recursive": "true",
                    "Filters": "IsPlayed",
                    "Fields": "SeriesId,UserData,UserDataLastPlayedDate,UserDataPlayCount",
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

                for item in items_data:
                    series_id = item.get("SeriesId")
                    last_played = item.get("UserData", {}).get("LastPlayedDate")

                    if series_id and last_played:
                        watch_date = datetime.fromisoformat(last_played)
                        if series_id not in series_watch_dates:
                            series_watch_dates[series_id] = watch_date
                        elif watch_date > series_watch_dates[series_id]:
                            series_watch_dates[series_id] = watch_date

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

        all_libraries = await self.get_movie_libraries()

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
        """Get aggregated series data across all users with optional library inclusion."""
        series_data: dict[str, dict[str, Any]] = {}

        all_libraries = await self.get_series_libraries()

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

            users = await self.get_users()
            if not users:
                continue

            (
                series_sizes,
                series_dates,
                season_data_map,
            ) = await self.get_series_sizes_for_library(library_id, users[0].id)

            for user in users:
                user_series_watch_dates = await self.get_all_watched_episodes_for_user(
                    user.id
                )

                user_series = await self.get_series_for_user(
                    user.id,
                    library_id=library_id,
                    library_name=library_name,
                    series_sizes=series_sizes,
                    series_dates=series_dates,
                )

                for series in user_series:
                    episode_last_watched = user_series_watch_dates.get(series.id)

                    if series.id not in series_data:
                        series_data[series.id] = {
                            "id": series.id,
                            "name": series.name,
                            "year": series.year,
                            "service": Service.EMBY,
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
                        existing = series_data[series.id]
                        if series.user_data:
                            existing["view_count"] += series.user_data.play_count

                        if episode_last_watched:
                            if (
                                not existing["last_viewed_at"]
                                or episode_last_watched > existing["last_viewed_at"]
                            ):
                                existing["last_viewed_at"] = episode_last_watched
                            existing["played_by_user_count"] += 1

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
        """Test Emby service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/System/Info",
                headers={"X-Emby-Token": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
