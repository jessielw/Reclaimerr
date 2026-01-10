from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import niquests

from backend.enums import Service
from backend.models.media import AggregatedMovieData, AggregatedSeriesData, ExternalIDs


@dataclass(slots=True, frozen=True)
class JellyfinUser:
    """Jellyfin user information."""

    id: str
    name: str


@dataclass(slots=True, frozen=True)
class JellyfinUserData:
    """User-specific watch data for a media item."""

    id: str
    key: str
    play_count: int
    last_played_date: datetime | None
    played: bool


@dataclass(slots=True, frozen=True)
class JellyfinMovie:
    """Internal Jellyfin movie representation with user data."""

    id: str
    name: str
    year: int | None
    premiere_date: datetime | None
    date_created: datetime | None
    container: str
    library_id: str
    library_name: str
    external_ids: ExternalIDs | None
    user_data: JellyfinUserData | None


@dataclass(slots=True, frozen=True)
class JellyfinSeries:
    """Internal Jellyfin series representation with user data."""

    id: str
    name: str
    year: int | None
    premiere_date: datetime | None
    date_created: datetime | None
    library_id: str
    library_name: str
    external_ids: ExternalIDs | None
    user_data: JellyfinUserData | None


class JellyfinBackend:
    """Jellyfin media server backend."""

    def __init__(self, api_key: str, jellyfin_url: str):
        self.api_key = api_key
        self.jellyfin_url = jellyfin_url
        self.session = niquests.Session()
        self.session.headers.update(
            {"X-Emby-Token": self.api_key, "accept": "application/json"}
        )
        # cache library information - map folder IDs to library names
        self._library_cache: dict[str, str] = {}
        # cache of virtual folders for path-based lookup
        self._virtual_folders: list[dict] | None = None

    def _make_request(self, endpoint: str, params: dict | None = None):
        response = self.session.get(f"{self.jellyfin_url}/{endpoint}", params=params)
        response.raise_for_status()
        return response.json()

    def _get_virtual_folders(self) -> list[dict]:
        """Get and cache virtual folders (libraries)."""
        if self._virtual_folders is None:
            self._virtual_folders = self._make_request("Library/VirtualFolders")
        return self._virtual_folders  # type: ignore[return-value]

    def _get_library_name_from_path(self, item_path: str) -> str:
        """Get library name by matching item path to virtual folder paths."""
        if not item_path:
            return "Unknown"

        virtual_folders = self._get_virtual_folders()
        for vf in virtual_folders:
            lib_paths = vf.get("Locations", [])
            for lib_path in lib_paths:
                if item_path.startswith(lib_path):
                    return vf["Name"]

        return "Unknown"

    def _get_library_name(self, parent_id: str, item_path: str | None = None) -> str:
        """Get library name from parent ID and optionally item path, with caching."""
        # try path-based lookup first if available
        if item_path:
            cache_key = f"path:{item_path}"
            if cache_key in self._library_cache:
                return self._library_cache[cache_key]

            lib_name = self._get_library_name_from_path(item_path)
            self._library_cache[cache_key] = lib_name
            return lib_name

        # fallback to parent ID lookup
        if parent_id in self._library_cache:
            return self._library_cache[parent_id]

        return "Unknown"

    def health(self) -> bool:
        """Check server health and API key."""
        try:
            self._make_request("System/Info")
            return True
        except Exception:
            return False

    def get_users(self) -> list[JellyfinUser]:
        """Get all Jellyfin users."""
        users_data = self._make_request("Users")
        return [JellyfinUser(name=user["Name"], id=user["Id"]) for user in users_data]

    def get_movies_for_user(
        self, user_id: str, filters: dict | None = None
    ) -> list[JellyfinMovie]:
        """Get all movies for a specific user."""
        params = {
            "userId": user_id,
            "includeItemTypes": "Movie",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            # include Path for accurate library detection and ProviderIds for external IDs
            "Fields": "Path,ParentId,ProviderIds",
        }
        if filters:
            params.update(filters)
        items_data = self._make_request("Items", params=params).get("Items", [])
        data = []
        for item in items_data:
            # get library information using path (most reliable)
            parent_id = item.get("ParentId")
            item_path = item.get("Path")
            library_name = self._get_library_name(parent_id or "Unknown", item_path)

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
                imdb=item["ProviderIds"].get("Imdb")
                if item.get("ProviderIds")
                else None,
                tmdb=item["ProviderIds"].get("Tmdb"),
                tmdb_collection=item["ProviderIds"].get("TmdbCollection")
                if item.get("ProviderIds")
                else None,
                tvdb=item["ProviderIds"].get("Tvdb")
                if item.get("ProviderIds")
                else None,
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
                library_id=parent_id or "Unknown",
                library_name=library_name,
                external_ids=external_ids,
                user_data=user_data,
            )
            data.append(movie)
        return data

    def get_series_for_user(
        self, user_id: str, filters: dict | None = None
    ) -> list[JellyfinSeries]:
        """Get all TV series for a specific user."""
        params = {
            "userId": user_id,
            "includeItemTypes": "Series",
            "recursive": "true",
            "enableTotalRecordCount": "true",
            # include Path for accurate library detection and ProviderIds for external IDs
            "Fields": "Path,ParentId,ProviderIds",
        }
        if filters:
            params.update(filters)
        items_data = self._make_request("Items", params=params).get("Items", [])
        data = []
        for item in items_data:
            # get library information using path (most reliable)
            parent_id = item.get("ParentId")
            item_path = item.get("Path")
            library_name = self._get_library_name(parent_id or "Unknown", item_path)

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
                imdb=item["ProviderIds"].get("Imdb")
                if item.get("ProviderIds")
                else None,
                tmdb=item["ProviderIds"].get("Tmdb"),
                tmdb_collection=item["ProviderIds"].get("TmdbCollection")
                if item.get("ProviderIds")
                else None,
                tvdb=item["ProviderIds"].get("Tvdb")
                if item.get("ProviderIds")
                else None,
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
                library_id=parent_id or "Unknown",
                library_name=library_name,
                external_ids=external_ids,
                user_data=user_data,
            )
            data.append(series)
        return data

    def get_all_watched_episodes_for_user(self, user_id: str) -> dict[str, datetime]:
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
            response = self._make_request("Items", params=params)
            items = response.get("Items", [])

            # group by series and keep the most recent watch date
            for item in items:
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

    def get_aggregated_movies(
        self, exclude_libraries: list[str] | None = None
    ) -> list[AggregatedMovieData]:
        """Get aggregated movie data across all users with optional library exclusion."""
        movie_data: dict[str, dict[str, Any]] = {}

        for user in self.get_users():
            user_movies = self.get_movies_for_user(user.id)

            for movie in user_movies:
                # skip if library is in exclusion list
                if exclude_libraries and movie.library_name in exclude_libraries:
                    continue

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

    def get_aggregated_series(
        self, exclude_libraries: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data across all users with optional library exclusion.

        Note: Jellyfin doesn't populate LastPlayedDate at the series level, so we check
        episodes to get accurate watch dates. We optimize by getting all watched episodes
        per user in a single API call instead of querying each series individually.
        """
        series_data: dict[str, dict[str, Any]] = {}

        for user in self.get_users():
            # get all watched episodes for this user in one API call
            user_series_watch_dates = self.get_all_watched_episodes_for_user(user.id)

            # get series list for this user
            user_series = self.get_series_for_user(user.id)

            for series in user_series:
                # skip if library is in exclusion list
                if exclude_libraries and series.library_name in exclude_libraries:
                    continue

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
