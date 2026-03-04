from __future__ import annotations

import asyncio
import time
from base64 import b64decode
from collections import deque

from niquests import AsyncSession, ConnectionError, ConnectTimeout, HTTPError
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.utils.request import should_retry_on_status


class AsyncTMDBClient:
    """Async client for The Movie Database (TMDB) API."""

    API_URL = "https://api.themoviedb.org/3"

    # class-level rate limiting: 50 requests per second across all instances
    _rate_limit_lock = asyncio.Lock()
    _request_timestamps: deque[float] = deque[float](maxlen=50)
    _max_requests_per_second = 50

    __slots__ = ("session",)

    def __init__(self, api_key: str | None = None) -> None:
        """Initialize TMDB client.

        Args:
            api_key: TMDB API bearer token. If not provided, reads from settings,
                     then falls back to the bundled default token.
        """
        token = api_key or settings.tmdb_api_key or self._resolve_token()

        self.session = AsyncSession()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
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

    @retry(
        stop=stop_after_attempt(5),  # 1 initial + 4 retries
        wait=wait_fixed(2),  # 2 second delay between retries
        retry=(
            retry_if_exception_type((ConnectionError, ConnectTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
        reraise=False,  # return None on failure instead of raising
    )
    async def _make_request(
        self, mode: str, endpoint: str, **kwargs
    ) -> dict | None | bool:
        """Make HTTP request to TMDB API with automatic retry.

        Args:
            mode: HTTP method (GET, POST, etc.)
            endpoint: API endpoint
            **kwargs: Additional request parameters

        Returns:
            Parsed JSON response, None on failure, or False on 404
        """
        # apply rate limiting before making the request
        await self._wait_for_rate_limit()

        try:
            resp = await self.session.request(
                mode, f"{self.API_URL}/{endpoint}", **kwargs
            )

            # special handling for 404 - don't retry
            if resp.status_code == 404:
                LOG.debug(f"404 Not Found for {endpoint}, skipping retries.")
                return False

            resp.raise_for_status()
            return resp.json()
        except HTTPError as e:
            # for non-404 HTTP errors, re-raise to let tenacity retry
            LOG.warning(f"HTTP error for {endpoint}: {e}")
            raise
        except (ConnectionError, ConnectTimeout) as e:
            LOG.warning(f"Connection error for {endpoint}: {e}")
            raise

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
    def _resolve_token() -> str:
        """Resolve the bundled default TMDB API bearer token."""
        k = (
            b"ZXlKaGJHY2lPaUpJVXpJMU5pSjkuZXlKaGRXUWlPaUpqTVRnMU9URTVNR1EyWlRNNVltSTVabVJsT0d"
            b"VMllXRXpaamt4TXprek5TSXNJbTVpWmlJNk1UYzJOelUwTXpjeE55NHdNeklzSW5OMVlpSTZJalk1Tld"
            b"FNU0yRTFaamxpWTJFd056ZGxNalJoWm1SaFpTSXNJbk5qYjNCbGN5STZXeUpoY0dsZmNtVmhaQ0pkTEN"
            b"KMlpYSnphVzl1SWpveGZRLkJTd29CSzBDU2V5LTFlUXdXUEMtVk0ySmVMTTV1WGtFVnpmSG8yR0FYbHM="
        )
        return b64decode(k).decode(encoding="utf-8")
