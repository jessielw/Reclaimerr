from __future__ import annotations

import asyncio
from datetime import datetime

import niquests

from backend.enums import Service
from backend.models.clients.plex import PlexMovie, PlexSeries
from backend.models.media import AggregatedMovieData, AggregatedSeriesData, ExternalIDs


class PlexBackend:
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

    async def _make_request(
        self, endpoint: str, params: dict | None = None
    ) -> dict | list:
        max_retries = 3

        for attempt in range(max_retries):
            try:
                response = await self.session.get(
                    f"{self.plex_url}/{endpoint}", params=params
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
            await self._make_request("")
            return True
        except Exception:
            return False

    async def get_library_sections(self) -> list[dict]:
        """Get all library sections."""
        data = await self._make_request("library/sections")
        if not isinstance(data, dict):
            return []
        return data.get("MediaContainer", {}).get("Directory", [])  # pyright: ignore [reportAttributeAccessIssue]

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
            section_name = section.get("title", "Unknown")
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

                movie = PlexMovie(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_name=section_name,
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
                    external_ids=self._parse_external_ids(item),
                )
                all_movies.append(movie)

        return all_movies

    async def get_series(
        self, included_sections: list[str] | None = None
    ) -> list[PlexSeries]:
        """Get all TV series from all show libraries.

        Args:
            included_sections: List of section names to include (None for all)
        """
        sections = await self.get_library_sections()
        show_sections = [s for s in sections if s.get("type") == "show"]

        if included_sections:
            show_sections = [
                s for s in show_sections if s.get("title") in included_sections
            ]

        all_series = []
        for section in show_sections:
            section_id = section["key"]
            section_name = section.get("title", "Unknown")
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

                series = PlexSeries(
                    id=item["ratingKey"],
                    name=item.get("title", ""),
                    year=item.get("year"),
                    library_name=section_name,
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
                    external_ids=self._parse_external_ids(item),
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
                id=m.id,
                name=m.name,
                year=m.year,
                service=Service.PLEX,
                library_name=m.library_name,
                added_at=m.added_at,
                premiere_date=None,  # plex doesn't provide premiere date directly
                external_ids=m.external_ids,
                view_count=m.view_count,
                last_viewed_at=m.last_viewed_at,
                never_watched=(m.view_count == 0),
                played_by_user_count=None,  # plex provides global counts, not per-user
                container=None,  # not readily available in plex API
            )
            for m in movies
        ]

    async def get_aggregated_series(
        self, included_sections: list[str] | None = None
    ) -> list[AggregatedSeriesData]:
        """Get aggregated series data with optional section inclusion."""
        series = await self.get_series(included_sections=included_sections)

        return [
            AggregatedSeriesData(
                id=s.id,
                name=s.name,
                year=s.year,
                library_name=s.library_name,
                added_at=s.added_at,
                premiere_date=None,  # plex doesn't provide premiere date directly
                external_ids=s.external_ids,
                view_count=s.view_count,
                last_viewed_at=s.last_viewed_at,
                never_watched=(s.view_count == 0),
                played_by_user_count=None,  # plex provides global counts, not per-user
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
                tmdb_id = guid_id.replace("tmdb://", "")
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
