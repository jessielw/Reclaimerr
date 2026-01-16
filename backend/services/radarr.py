from __future__ import annotations

import asyncio

import niquests

from backend.models.clients.radarr import RadarrMovie
from backend.models.media import ArrTag


def build_radarr_movie_from_dict(data: dict) -> RadarrMovie:
    return RadarrMovie(
        id=data["id"],
        title=data.get("title", ""),
        tmdb_id=data.get("tmdbId"),
        imdb_id=data.get("imdbId"),
        year=data.get("year"),
        path=data.get("path", ""),
        has_file=data.get("hasFile", False),
        tags=data.get("tags", []),
        raw=data,
    )


class RadarrClient:
    def __init__(self, api_key: str, base_url: str) -> None:
        """Initialize Radarr client.

        Args:
            api_key: Radarr API key
            base_url: Radarr server URL
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = niquests.AsyncSession()
        self.session.headers.update(
            {
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
            }
        )

    async def _make_request(
        self, method: str, endpoint: str, **kwargs
    ) -> tuple[int, dict | list | None]:
        """Make HTTP request to Radarr API.

        Returns:
            Tuple of (status_code, response_data)
        """
        url = f"{self.base_url}/api/v3/{endpoint}"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                response = await self.session.request(method, url, **kwargs)

                # retry on rate limit or server error
                if response.status_code in (429, 503, 502, 504):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2**attempt)
                        continue

                response.raise_for_status()

                status_code = response.status_code
                if not status_code:
                    raise ValueError("Status code should not be None")

                if response.content:
                    return status_code, response.json()
                return status_code, None

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
            await self._make_request("GET", "health")
            return True
        except Exception:
            return False

    async def get_movie(self, movie_id: int) -> RadarrMovie:
        """Get movie by ID."""
        status_code, data = await self._make_request("GET", f"movie/{movie_id}")
        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid response for movie {movie_id} (status: {status_code})"
            )
        return build_radarr_movie_from_dict(data)

    async def get_all_movies(self) -> list[RadarrMovie]:
        """Get all movies from Radarr."""
        _, data = await self._make_request("GET", "movie")
        if not isinstance(data, list):
            return []
        return [build_radarr_movie_from_dict(movie) for movie in data]

    async def get_tags(self) -> list[ArrTag]:
        """Get all tags from Radarr."""
        _, data = await self._make_request("GET", "tag")
        if not isinstance(data, list):
            return []
        return [ArrTag(id=tag["id"], label=tag["label"]) for tag in data]

    async def create_tag(self, label: str) -> ArrTag:
        """Create a new tag.

        Args:
            label: Tag name

        Returns:
            Created tag
        """
        status_code, data = await self._make_request(
            "POST", "tag", json={"label": label}
        )
        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid response when creating tag {label} (status: {status_code})"
            )
        return ArrTag(id=data["id"], label=data["label"])

    async def get_or_create_tag(self, label: str) -> ArrTag:
        """Get existing tag by label or create if doesn't exist.

        Args:
            label: Tag name

        Returns:
            Tag object
        """
        tags = await self.get_tags()
        for tag in tags:
            if tag.label.lower() == label.lower():
                return tag
        return await self.create_tag(label)

    async def search_movie(self, tmdb_id: int) -> RadarrMovie:
        """Search for a movie by TMDB ID.

        Args:
            tmdb_id: TMDB ID of the movie

        Returns:
            Movie matching the TMDB ID
        """
        status_code, data = await self._make_request(
            "GET", "movie", params={"tmdbId": tmdb_id}
        )
        if not isinstance(data, list) or not data:
            raise ValueError(
                f"Movie with TMDB ID {tmdb_id} not found (status: {status_code})"
            )
        return build_radarr_movie_from_dict(data[0])

    async def add_tag_to_movies(
        self, movie_ids: list[int], tag_id: int
    ) -> list[RadarrMovie]:
        """Add a single tag to multiple movies (preserves existing tags).

        Args:
            movie_ids: List of Movie IDs
            tag_id: Tag ID to add

        Returns:
            List of updated movies
        """
        if not movie_ids:
            return []

        status_code, data = await self._make_request(
            "PUT",
            "movie/editor",
            json={
                "movieIds": movie_ids,
                "tags": [tag_id],
                "applyTags": "add",
            },
        )

        if not isinstance(data, list):
            raise ValueError(
                f"Invalid response updating movies {movie_ids} (status: {status_code})"
            )

        return [build_radarr_movie_from_dict(updated_movie) for updated_movie in data]

    async def remove_tag_from_movies(
        self, movie_ids: list[int], tag_id: int
    ) -> list[RadarrMovie]:
        """Remove a single tag from multiple movies (preserves other tags).

        Args:
            movie_ids: List of Movie IDs
            tag_id: Tag ID to remove

        Returns:
            List of updated movies
        """
        if not movie_ids:
            return []

        status_code, data = await self._make_request(
            "PUT",
            "movie/editor",
            json={
                "movieIds": movie_ids,
                "tags": [tag_id],
                "applyTags": "remove",
            },
        )

        if not isinstance(data, list):
            raise ValueError(
                f"Invalid response removing tag from movies {movie_ids} (status: {status_code})"
            )

        return [build_radarr_movie_from_dict(updated_movie) for updated_movie in data]

    async def delete_movies(
        self,
        movie_ids: list[int],
    ) -> None:
        """Delete multiple movies at once.

        Args:
            movie_ids: List of movie IDs to delete
        """
        status_code, _ = await self._make_request(
            "DELETE",
            "movie/editor",
            json={
                "movieIds": movie_ids,
                "deleteFiles": True,
            },
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete movies {movie_ids} (status: {status_code})"
            )
