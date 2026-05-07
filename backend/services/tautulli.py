from __future__ import annotations

from datetime import datetime, timedelta
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


class TautulliClient:
    """Client for Tautulli API operations."""

    __slots__ = ("api_key", "base_url", "timeout", "session")

    def __init__(self, api_key: str, base_url: str, timeout: int = 60) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.session = niquests.AsyncSession()

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=(
            retry_if_exception_type((ConnectionError, TimeoutError, ReadTimeout))
            | retry_if_exception(should_retry_on_status)
        ),
    )
    async def _make_request(self, cmd: str, **params: Any) -> dict:
        url = f"{self.base_url}/api/v2"
        response = await self.session.get(
            url,
            params={"apikey": self.api_key, "cmd": cmd, **params},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            data = await self._make_request("status")
            return data.get("response", {}).get("result") == "success"
        except Exception:
            return False

    async def get_history(
        self,
        media_type: str | None = None,
        section_id: int | None = None,
        length: int = 10000,
        start: int = 0,
    ) -> dict:
        """Fetch play history from Tautulli.

        Args:
            media_type: "movie", "episode", or None for all.
            section_id: Plex library section ID to filter by.
            length: Number of records to return per page.
            start: Row offset for pagination.

        Returns:
            The raw ``response.data`` dict from Tautulli (keys:
            ``recordsTotal``, ``recordsFiltered``, ``data``).
        """
        params: dict[str, str | int] = {"length": length, "start": start}
        if media_type is not None:
            params["media_type"] = media_type
        if section_id is not None:
            params["section_id"] = section_id

        result = await self._make_request("get_history", **params)
        return result.get("response", {}).get("data", {})

    async def get_play_counts(
        self,
        media_type: str,
        since: datetime | None = None,
        page_size: int = 10000,
    ) -> dict[int, tuple[int, datetime | None]]:
        """Aggregate play counts and last-played timestamps from Tautulli history.

        Args:
            media_type: ``"movie"`` or ``"episode"``.
            since: If provided, only fetch records on or after this datetime minus
                a 1 day overlap buffer (date granularity safety margin).
            page_size: Number of records to request per API page.

        Returns:
            For ``media_type="movie"``:
                ``{rating_key: (play_count, last_played_at)}``
            For ``media_type="episode"``:
                ``{grandparent_rating_key: (play_count, last_played_at)}``
                (i.e. series-level rollup keyed by the show's rating key)
        """
        params: dict[str, Any] = {"media_type": media_type, "length": page_size}

        if since is not None:
            # subtract 1 day buffer (tautulli start_date is date granularity only)
            cutoff = since - timedelta(days=1)
            params["start_date"] = cutoff.strftime("%Y-%m-%d")

        aggregated: dict[int, tuple[int, datetime | None]] = {}
        offset = 0

        while True:
            params["start"] = offset
            data = await self._make_request("get_history", **params)
            payload = data.get("response", {}).get("data", {})
            records: list[dict] = payload.get("data", [])
            records_total: int = payload.get("recordsFiltered", 0)

            for record in records:
                if media_type == "episode":
                    key = record.get("grandparent_rating_key")
                else:
                    key = record.get("rating_key")

                if not key:
                    continue

                key = int(key)

                # parse stopped timestamp as UTC datetime
                stopped_ts = record.get("stopped")
                last_played: datetime | None = None
                if stopped_ts:
                    try:
                        last_played = datetime.fromtimestamp(
                            int(stopped_ts), tz=None
                        ).replace(tzinfo=None)
                    except (ValueError, OSError):
                        last_played = None

                existing = aggregated.get(key)
                if existing is None:
                    aggregated[key] = (1, last_played)
                else:
                    prev_count, prev_last = existing
                    merged_last = (
                        max(filter(None, [prev_last, last_played]))
                        if (prev_last or last_played)
                        else None
                    )
                    aggregated[key] = (prev_count + 1, merged_last)

            offset += page_size
            if offset >= records_total:
                break

        return aggregated

    @staticmethod
    async def test_service(url: str, api_key: str) -> bool:
        """Test Tautulli service connection without full initialization."""
        async with niquests.AsyncSession() as session:
            response = await session.get(
                f"{url.rstrip('/')}/api/v2",
                params={"apikey": api_key, "cmd": "status"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("response", {}).get("result") == "success":
                return True
            raise ValueError("Tautulli returned an unsuccessful status response")
