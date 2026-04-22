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

from backend.core.utils.request import should_retry_on_status
from backend.enums import MediaType, SeerrRequestStatus
from backend.models.services.seerr import SeerrPageInfo, SeerrRequest


def build_seerr_request_from_dict(data: dict[str, Any]) -> SeerrRequest:
    media_type = MediaType.MOVIE if data["type"] == "movie" else MediaType.SERIES
    return SeerrRequest(
        id=data["id"],
        status=SeerrRequestStatus(data["status"]),
        media_id=data["media"]["id"],
        media_type=media_type,
        tmdb_id=data["media"]["tmdbId"],
        created_at=datetime.fromisoformat(data["createdAt"]),
        requested_by_id=data["requestedBy"]["id"],
        is_4k=data.get("is4k", False),
        raw=data,
    )


class SeerrClient:
    """Client for interacting with Seerr API."""

    __slots__ = ("api_key", "base_url", "session")

    def __init__(self, api_key: str, base_url: str) -> None:
        """Initialize Seerr client.

        Args:
            api_key: Seerr API key
            base_url: Seerr server URL
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

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(
        self, method: str, endpoint: str, **kwargs
    ) -> tuple[int, dict | list | None]:
        """Make HTTP request to Seerr API with automatic retry.

        Returns:
            Tuple of (status_code, response_data)
        """
        url = f"{self.base_url}/api/v1/{endpoint}"
        response = await self.session.request(method, url, **kwargs)

        # 403 means the API key lacks admin permissions (don't retry)
        if response.status_code == 403:
            raise PermissionError(
                f"Seerr returned 403 Forbidden for {method} {endpoint}. "
                "The configured API key does not have admin permissions. "
                "Please use an admin account's API key in Seerr's settings."
            )

        response.raise_for_status()

        status_code = response.status_code
        if not status_code:
            raise ValueError("Status code should not be None")

        if response.content:
            return status_code, response.json()
        return status_code, None

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            await self._make_request("GET", "auth/me")
            return True
        except Exception:
            return False

    async def get_user_requests(
        self, user_id: int, take: int = 20, skip: int = 0
    ) -> tuple[SeerrPageInfo, list[SeerrRequest]]:
        """Get all requests for a specific user.

        Args:
            user_id: User ID
            take: Number of results to return
            skip: Number of results to skip

        Returns:
            List of user's requests
        """
        _, data = await self._make_request(
            "GET",
            f"user/{user_id}/requests",
            params={"take": take, "skip": skip},
            timeout=60,
        )
        if not isinstance(data, dict):
            return SeerrPageInfo(0, 0, 0), []

        results = data.get("results", [])
        page_info = SeerrPageInfo(
            page=data.get("pageInfo", {}).get("page", 0),
            pages=data.get("pageInfo", {}).get("pages", 0),
            results=data.get("pageInfo", {}).get("results", 0),
        )
        return page_info, [build_seerr_request_from_dict(req) for req in results]

    async def delete_request(self, request_id: int) -> None:
        """Delete/un-request a media request.

        Args:
            request_id: Request ID to delete
        """
        status_code, _ = await self._make_request("DELETE", f"request/{request_id}")
        if status_code != 204:
            raise ValueError(
                f"Failed to delete request {request_id} (status: {status_code})"
            )

    async def get_requests(
        self,
        take: int = 20,
        skip: int = 0,
        filter: str = "all",
        requested_by: int | None = None,
    ) -> tuple[SeerrPageInfo, list[SeerrRequest]]:
        """Get all requests (requires appropriate permissions).

        Args:
            take: Number of results to return
            skip: Number of results to skip
            filter: Filter type (all, approved, available, pending, etc.)
            requested_by: Filter by user ID

        Returns:
            List of requests
        """
        params = {"take": take, "skip": skip, "filter": filter}
        if requested_by:
            params["requestedBy"] = requested_by

        _, data = await self._make_request("GET", "request", params=params, timeout=300)
        if not isinstance(data, dict):
            return SeerrPageInfo(0, 0, 0), []

        results = data.get("results", [])
        page_info = SeerrPageInfo(
            page=data.get("pageInfo", {}).get("page", 0),
            pages=data.get("pageInfo", {}).get("pages", 0),
            results=data.get("pageInfo", {}).get("results", 0),
        )
        return page_info, [build_seerr_request_from_dict(req) for req in results]

    async def get_movie_requests(self, tmdb_id: int) -> list[SeerrRequest]:
        """Get movie requests by TMDB ID.

        Args:
            tmdb_id: TMDB movie ID
        """
        _, data = await self._make_request("GET", f"movie/{tmdb_id}")
        if not isinstance(data, dict):
            raise ValueError(f"Movie {tmdb_id} not found")
        return [
            build_seerr_request_from_dict(req)
            for req in data.get("mediaInfo", {}).get("requests", [])
        ]

    async def delete_movie_requests(self, tmdb_id: int) -> None:
        """Collect and delete movie requests by TMDB ID.

        Args:
            tmdb_id: TMDB movie ID
        """
        requests = await self.get_movie_requests(tmdb_id)
        for req in requests:
            await self.delete_request(req.id)

    async def get_tv_requests(self, tmdb_id: int) -> list[SeerrRequest]:
        """Get TV series requests by TMDB ID.

        Args:
            tmdb_id: TMDB TV series ID
        """
        _, data = await self._make_request("GET", f"tv/{tmdb_id}")
        if not isinstance(data, dict):
            raise ValueError(f"TV series {tmdb_id} not found")
        return [
            build_seerr_request_from_dict(req)
            for req in data.get("mediaInfo", {}).get("requests", [])
        ]

    async def delete_tv_requests(self, tmdb_id: int) -> None:
        """Collect and delete TV series requests by TMDB ID.

        Args:
            tmdb_id: TMDB TV series ID
        """
        requests = await self.get_tv_requests(tmdb_id)
        for req in requests:
            await self.delete_request(req.id)

    async def get_media_id(self, tmdb_id: int, media_type: MediaType) -> int | None:
        """Get Seerr internal media ID from TMDB ID.

        Args:
            tmdb_id: TMDB ID
            media_type: Movie or Series

        Returns:
            Seerr media ID or None if not found
        """
        try:
            endpoint = "movie" if media_type is MediaType.MOVIE else "tv"
            _, data = await self._make_request("GET", f"{endpoint}/{tmdb_id}")
            if not isinstance(data, dict):
                return None
            return data.get("mediaInfo", {}).get("id")
        except Exception:
            return None

    async def delete_media(self, media_id: int) -> None:
        """Delete media item from Seerr database.

        Args:
            media_id: Seerr internal media ID
        """
        status_code, _ = await self._make_request("DELETE", f"media/{media_id}")
        if status_code != 204:
            raise ValueError(
                f"Failed to delete media {media_id} (status: {status_code})"
            )

    async def delete_movie_media(self, tmdb_id: int) -> None:
        """Delete movie media item from Seerr database.

        Args:
            tmdb_id: TMDB movie ID
        """
        media_id = await self.get_media_id(tmdb_id, MediaType.MOVIE)
        if media_id:
            await self.delete_media(media_id)

    async def delete_tv_media(self, tmdb_id: int) -> None:
        """Delete TV series media item from Seerr database.

        Args:
            tmdb_id: TMDB TV series ID
        """
        media_id = await self.get_media_id(tmdb_id, MediaType.SERIES)
        if media_id:
            await self.delete_media(media_id)

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Seerr service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/api/v1/auth/me",
                headers={"X-Api-Key": api_key},
            )
            response.raise_for_status()
            if response.status_code == 200:
                return True
            raise ValueError(f"Unexpected status code: {response.status_code}")
