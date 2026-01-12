from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import niquests

from backend.enums import Service
from backend.models.clients.jellyfin import (
    JellyfinMovie,
    JellyfinSeries,
    JellyfinUser,
    JellyfinUserData,
)
from backend.models.media import AggregatedMovieData, AggregatedSeriesData, ExternalIDs


class JellyfinService:
    """Jellyfin media server backend."""

    def __init__(self, api_key: str, jellyfin_url: str) -> None:
        self.api_key = api_key
        self.jellyfin_url = jellyfin_url
        self.session = niquests.AsyncSession()
        self.session.headers.update(
            {
                "X-Emby-Token": self.api_key,
                "accept": "application/json",
            }
        )
        # cache of library ID to library name mapping
        self._library_map: dict[str, str] | None = None

    async def _make_request(
        self, endpoint: str, params: dict | None = None
    ) -> list | dict:
        max_retries = 3

        for attempt in range(max_retries):
            try:
                response = await self.session.get(
                    f"{self.jellyfin_url}/{endpoint}",
                    params=params,
                )

                # retry on rate limit or server error
                if response.status_code in (429, 503, 502, 504):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2**attempt)
                        continue

                response.raise_for_status()
                return response.json()

            except (ConnectionError, TimeoutError):
                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise

        # should never reach here, but satisfy type checker
        raise RuntimeError("Max retries exceeded")

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("System/Info")
            return True
        except Exception:
            return False

    async def _get_media_libraries(self, media_type: str) -> list[dict[str, str]]:
        """Get list of media libraries of a specific type with their IDs and names."""
        virtual_folders = await self._make_request("Library/VirtualFolders")  # pyright: ignore [reportAttributeAccessIssue]
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
            "Fields": "ProviderIds",
            "ParentId": library_id,
        }

        if filters:
            params.update(filters)
        get_data = await self._make_request("Items", params=params)
        if not get_data:
            return []
        items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

        data = []
        for item in items_data:
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
                tmdb=provider_ids.get("Tmdb"),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )
            movie = JellyfinMovie(
                id=item["Id"],
                name=item["Name"],
                year=item.get("ProductionYear"),
                premiere_date=datetime.fromisoformat(item["PremiereDate"])
                if item.get("PremiereDate")
                else None,
                date_created=datetime.fromisoformat(item["DateCreated"])
                if item.get("DateCreated")
                else None,
                container=item.get("Container"),
                library_id=library_id,
                library_name=library_name,
                external_ids=external_ids,
                user_data=user_data,
            )
            data.append(movie)
        return data

    async def get_series_for_user(
        self,
        user_id: str,
        library_id: str,
        library_name: str,
        filters: dict | None = None,
    ) -> list[JellyfinSeries]:
        """Get TV series for a specific user, optionally filtered by library.

        Args:
            user_id: The Jellyfin user ID
            library_id: Library ID to query
            library_name: The name of the library
            filters: Additional query filters
        """
        params = {
            "userId": user_id,
            "includeItemTypes": "Series",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            "Fields": "ProviderIds",
            "ParentId": library_id,
        }

        if filters:
            params.update(filters)
        get_data = await self._make_request("Items", params=params)
        if not get_data:
            return []
        items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

        data = []
        for item in items_data:
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
                tmdb=provider_ids.get("Tmdb"),
                tmdb_collection=provider_ids.get("TmdbCollection"),
                tvdb=provider_ids.get("Tvdb"),
            )
            series = JellyfinSeries(
                id=item["Id"],
                name=item["Name"],
                year=item.get("ProductionYear"),
                premiere_date=datetime.fromisoformat(item["PremiereDate"])
                if item.get("PremiereDate")
                else None,
                date_created=datetime.fromisoformat(item["DateCreated"])
                if item.get("DateCreated")
                else None,
                library_id=library_id,
                library_name=library_name,
                external_ids=external_ids,
                user_data=user_data,
            )
            data.append(series)
        return data

    async def get_all_watched_episodes_for_user(
        self, user_id: str
    ) -> dict[str, datetime]:
        """Get all watched episodes for a user and return a map of seriesId -> most recent watch date.

        This is much more efficient than querying each series individually.
        """
        series_watch_dates: dict[str, datetime] = {}

        try:
            params = {
                "userId": user_id,
                "includeItemTypes": "Episode",
                "recursive": "true",
                "Filters": "IsPlayed",
                "Fields": "SeriesId",
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
            }
            get_data = await self._make_request("Items", params=params)
            if not get_data:
                raise Exception("No data returned")
            items_data = get_data.get("Items", [])  # pyright: ignore [reportAttributeAccessIssue]

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

            for user in await self.get_users():
                user_movies = await self.get_movies_for_user(
                    user.id, library_id=library_id, library_name=library_name
                )

                for movie in user_movies:
                    if movie.id not in movie_data:
                        # first time seeing this movie
                        movie_data[movie.id] = {
                            "id": movie.id,
                            "name": movie.name,
                            "year": movie.year,
                            "library_name": movie.library_name,
                            "service": Service.JELLYFIN,
                            "added_at": movie.date_created,
                            "premiere_date": movie.premiere_date,
                            "container": movie.container,
                            "external_ids": movie.external_ids,
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
                **data, never_watched=(data["played_by_user_count"] == 0)
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

            for user in await self.get_users():
                # get all watched episodes for this user in one API call
                user_series_watch_dates = await self.get_all_watched_episodes_for_user(
                    user.id
                )

                # get series list for this user from this library
                user_series = await self.get_series_for_user(
                    user.id, library_id=library_id, library_name=library_name
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
                            "library_name": series.library_name,
                            "added_at": series.date_created,
                            "premiere_date": series.premiere_date,
                            "external_ids": series.external_ids,
                            "view_count": series.user_data.play_count
                            if series.user_data
                            else 0,
                            "last_viewed_at": episode_last_watched,
                            "played_by_user_count": 1 if episode_last_watched else 0,
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
                **data, never_watched=(data["played_by_user_count"] == 0)
            )
            for data in series_data.values()
        ]
