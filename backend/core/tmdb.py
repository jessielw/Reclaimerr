from __future__ import annotations

import asyncio
import time
from base64 import b64decode
from collections import deque

from niquests import (
    AsyncSession,
    ConnectionError,
    ConnectTimeout,
)

from backend.core.logger import LOG


class AsyncTMDBClient:
    """Async client for The Movie Database (TMDB) API."""

    API_URL = "https://api.themoviedb.org/3"

    # class-level rate limiting: 50 requests per second across all instances
    _rate_limit_lock = asyncio.Lock()
    _request_timestamps: deque[float] = deque[float](maxlen=50)
    _max_requests_per_second = 50

    __slots__ = ("session",)

    def __init__(self) -> None:
        """Initialize TMDB client."""
        self.session = AsyncSession()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self._get_tvdb_k()}",
                "accept": "application/json",
            }
        )

    async def __aenter__(self):
        """Enter async context manager."""
        return self

    async def __aexit__(self, *args):
        """Ensure session is closed on exit."""
        await self.session.close()

    async def _wait_for_rate_limit(self) -> None:
        """Ensure we don't exceed rate limit across all instances."""
        async with self._rate_limit_lock:
            now = time.time()

            # remove timestamps older than 1 second
            while self._request_timestamps and now - self._request_timestamps[0] >= 1.0:
                self._request_timestamps.popleft()

            # if we've hit the limit, wait until the oldest request is 1 second old
            if len(self._request_timestamps) >= self._max_requests_per_second:
                sleep_time = 1.0 - (now - self._request_timestamps[0])
                if sleep_time > 0:
                    LOG.debug(f"Rate limit reached, waiting {sleep_time:.3f}s")
                    await asyncio.sleep(sleep_time)
                    # remove the old timestamp after waiting
                    self._request_timestamps.popleft()

            # record this request
            self._request_timestamps.append(time.time())

    async def _make_request(
        self, mode: str, endpoint: str, **kwargs
    ) -> dict | None | bool:
        """Make HTTP request to TMDB API.

        Args:
            mode: HTTP method (GET, POST, etc.)
            endpoint: API endpoint
            **kwargs: Additional request parameters

        Returns:
            Parsed JSON response, None on failure, or False on 404
        """
        MAX_RETRIES = 5
        RETRY_DELAY = 2

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # apply rate limiting before making the request
                await self._wait_for_rate_limit()

                resp = await self.session.request(
                    mode, f"{self.API_URL}/{endpoint}", **kwargs
                )
                if resp.status_code == 404:
                    LOG.debug(f"404 Not Found for {endpoint}, skipping retries.")
                    return False
                resp.raise_for_status()
                return resp.json()
            except (ConnectionError, ConnectTimeout) as e:
                if attempt == MAX_RETRIES:
                    LOG.warning(f"Failed to get data from TMDB API ({endpoint} - {e}) ")
                    return None
                LOG.warning(
                    f"Retry {attempt}/{MAX_RETRIES} for {endpoint} due to client or timeout error."
                )
            await asyncio.sleep(RETRY_DELAY)

        return None

    async def get_movie_details(self, tmdb_id: int | str) -> dict | None | bool:
        """Get movie details by TMDB ID.

        Args:
            tmdb_id: TMDB movie ID

        Returns:
            Parsed JSON response, None on failure, or False on 404
        """
        return await self._make_request(
            "GET", f"movie/{tmdb_id}?append_to_response=external_ids"
        )

    async def get_tv_details(self, tmdb_id: int | str) -> dict | None | bool:
        """Get TV show details by TMDB ID.

        Args:
            tmdb_id: TMDB TV show ID

        Returns:
            Parsed JSON response, None on failure, or False on 404
        """
        return await self._make_request(
            "GET", f"tv/{tmdb_id}?append_to_response=external_ids"
        )

    @staticmethod
    def _get_tvdb_k() -> str:
        k = (
            b"ZXlKaGJHY2lPaUpJVXpJMU5pSjkuZXlKaGRXUWlPaUpqTVRnMU9URTVNR1EyWlRNNVltSTVabVJsT0d"
            b"VMllXRXpaamt4TXprek5TSXNJbTVpWmlJNk1UYzJOelUwTXpjeE55NHdNeklzSW5OMVlpSTZJalk1Tld"
            b"FNU0yRTFaamxpWTJFd056ZGxNalJoWm1SaFpTSXNJbk5qYjNCbGN5STZXeUpoY0dsZmNtVmhaQ0pkTEN"
            b"KMlpYSnphVzl1SWpveGZRLkJTd29CSzBDU2V5LTFlUXdXUEMtVk0ySmVMTTV1WGtFVnpmSG8yR0FYbHM="
        )
        return b64decode(k).decode(encoding="utf-8")
