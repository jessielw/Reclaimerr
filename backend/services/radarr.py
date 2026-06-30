from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
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

from backend.core.utils.misc import as_int
from backend.core.utils.request import format_http_failure, should_retry_on_status
from backend.models.media import ArrTag
from backend.models.services.radarr import RadarrMovie


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _as_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    values: list[int] = []
    for item in value:
        parsed = as_int(item)
        if parsed is not None:
            values.append(parsed)
    return values


def _as_datetime(value: object) -> datetime | None:
    raw = _as_optional_str(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


# def _mapping_list(value: object) -> list[Mapping[str, object]]:
#     if not isinstance(value, list):
#         return []
#     items: list[Mapping[str, object]] = []
#     for item in value:
#         if isinstance(item, Mapping):
#             items.append(item)
#     return items


def _movie_from_mapping(data: Mapping[str, object]) -> RadarrMovie:
    raw_movie_file = data.get("movieFile")
    movie_file = raw_movie_file if isinstance(raw_movie_file, Mapping) else {}
    return RadarrMovie(
        id=as_int(data.get("id")) or 0,
        title=_as_optional_str(data.get("title")) or "",
        tmdb_id=as_int(data.get("tmdbId")),
        imdb_id=_as_optional_str(data.get("imdbId")),
        year=as_int(data.get("year")),
        path=_as_optional_str(data.get("path")) or "",
        has_file=_as_bool(data.get("hasFile")),
        monitored=_as_bool(data.get("monitored")),
        tags=_as_int_list(data.get("tags")),
        file_path=_as_optional_str(movie_file.get("path")),
        file_added_at=_as_datetime(movie_file.get("dateAdded")),
        raw=data,
    )


def build_radarr_movie_from_dict(data: Mapping[str, object]) -> RadarrMovie:
    return _movie_from_mapping(data)


class RadarrClient:
    """Client for interacting with Radarr API."""

    __slots__ = ("api_key", "base_url", "timeout", "session")

    def __init__(self, api_key: str, base_url: str, timeout: int = 300) -> None:
        """Initialize Radarr client.

        Args:
            api_key: Radarr API key
            base_url: Radarr server URL
            timeout: Request timeout in seconds (default: 300)
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.session = niquests.AsyncSession()
        self.session.headers.update(
            {
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
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
        self,
        method: str,
        endpoint: str,
        *,
        error_context: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, Mapping[str, object] | list[Mapping[str, object]] | None]:
        """Make HTTP request to Radarr API with automatic retry.

        Returns:
            Tuple of (status_code, response_data)
        """
        url = f"{self.base_url}/api/v3/{endpoint}"
        response = await self.session.request(method, url, **kwargs)
        try:
            response.raise_for_status()
        except niquests.HTTPError as exc:
            if error_context:
                raise ValueError(
                    format_http_failure(
                        action=error_context,
                        exception=exc,
                        response=response,
                        method=method,
                        endpoint=endpoint,
                    )
                ) from exc
            raise

        status_code = response.status_code
        if not status_code:
            raise ValueError("Status code should not be None")

        if response.content:
            return status_code, response.json()
        return status_code, None

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
        if not isinstance(data, Mapping):
            raise ValueError(
                f"Invalid response for movie {movie_id} (status: {status_code})"
            )
        return build_radarr_movie_from_dict(data)

    async def get_all_movies(self) -> list[RadarrMovie]:
        """Get all movies from Radarr."""
        _, data = await self._make_request("GET", "movie", timeout=self.timeout)
        if not isinstance(data, list):
            return []
        return [
            build_radarr_movie_from_dict(movie)
            for movie in data
            if isinstance(movie, Mapping)
        ]

    async def get_disk_space(self) -> list[dict[str, int | str]]:
        """Get disk space stats from Radarr (GET /diskspace).

        Returns a list of dicts with keys: path, free_space, total_space.
        These are reported by the Radarr server itself, so they work correctly
        regardless of where Reclaimerr is running (Docker, remote machine, etc.).
        """
        _, data = await self._make_request("GET", "diskspace")
        if not isinstance(data, list):
            return []
        disk_space: list[dict[str, int | str]] = []
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            path = _as_optional_str(entry.get("path"))
            if path is None:
                continue
            disk_space.append(
                {
                    "path": path,
                    "free_space": as_int(entry.get("freeSpace")) or 0,
                    "total_space": as_int(entry.get("totalSpace")) or 0,
                }
            )
        return disk_space

    async def get_tags(self) -> list[ArrTag]:
        """Get all tags from Radarr."""
        _, data = await self._make_request("GET", "tag")
        if not isinstance(data, list):
            return []
        tags: list[ArrTag] = []
        for tag in data:
            if not isinstance(tag, Mapping):
                continue
            tag_id = as_int(tag.get("id"))
            label = _as_optional_str(tag.get("label"))
            if tag_id is None or label is None:
                continue
            tags.append(ArrTag(id=tag_id, label=label))
        return tags

    async def get_all_tag_details(self) -> dict[str, list[int]]:
        """Get all tags with their associated movie IDs in a single call.

        Returns a dict mapping lowercase tag label → list of Radarr movie IDs.
        """
        _, data = await self._make_request("GET", "tag/detail")
        if not isinstance(data, list):
            return {}
        tag_details: dict[str, list[int]] = {}
        for tag in data:
            if not isinstance(tag, Mapping):
                continue
            label = _as_optional_str(tag.get("label"))
            if label is None:
                continue
            tag_details[label.lower()] = _as_int_list(tag.get("movieIds"))
        return tag_details

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
        if not isinstance(data, Mapping):
            raise ValueError(
                f"Invalid response when creating tag {label} (status: {status_code})"
            )
        tag_id = as_int(data.get("id"))
        tag_label = _as_optional_str(data.get("label"))
        if tag_id is None or tag_label is None:
            raise ValueError(
                f"Invalid response when creating tag {label} (status: {status_code})"
            )
        return ArrTag(id=tag_id, label=tag_label)

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
        first_movie = data[0]
        if not isinstance(first_movie, Mapping):
            raise ValueError(
                f"Movie with TMDB ID {tmdb_id} not found (status: {status_code})"
            )
        return build_radarr_movie_from_dict(first_movie)

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

        return [
            build_radarr_movie_from_dict(updated_movie)
            for updated_movie in data
            if isinstance(updated_movie, Mapping)
        ]

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

        return [
            build_radarr_movie_from_dict(updated_movie)
            for updated_movie in data
            if isinstance(updated_movie, Mapping)
        ]

    async def delete_movies(
        self,
        movie_ids: list[int],
        delete_files: bool = True,
        add_import_exclusion: bool = False,
    ) -> None:
        """Delete multiple movies at once."""
        status_code, _ = await self._make_request(
            "DELETE",
            "movie/editor",
            json={
                "movieIds": movie_ids,
                "deleteFiles": delete_files,
                "addImportExclusion": add_import_exclusion,
            },
            error_context=f"Failed to delete movies {movie_ids} via Radarr",
        )
        if status_code != 200:
            raise ValueError(
                f"Failed to delete movies {movie_ids} (status: {status_code})"
            )

    async def unmonitor_movies(self, movie_ids: list[int]) -> None:
        """Set multiple movies to unmonitored so Radarr won't queue re-downloads."""
        if not movie_ids:
            return
        await self._make_request(
            "PUT",
            "movie/editor",
            json={"movieIds": movie_ids, "monitored": False},
            error_context=f"Failed to unmonitor movies {movie_ids} via Radarr",
        )

    async def rescan_movies(self, movie_ids: list[int]) -> None:
        """Queue a RefreshMovie command so Radarr re-checks metadata and file status.

        Uses RefreshMovie (not RescanMovie) because RescanMovie only adds newly found files;
        RefreshMovie also removes stale file records when files have been deleted from disk.
        """
        if not movie_ids:
            return
        await self._make_request(
            "POST",
            "command",
            json={"name": "RefreshMovie", "movieIds": movie_ids},
        )

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Radarr service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/api/v3/health",
                headers={"X-Api-Key": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
