from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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
from backend.core.utils.request import should_retry_on_status


def _mapping_from(data: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    value = data.get(key)
    return value if isinstance(value, Mapping) else None


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    items: list[Mapping[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            items.append(item)
    return items


def _history_payload(data: Mapping[str, object]) -> Mapping[str, object] | None:
    response = _mapping_from(data, "response")
    if response is None:
        return None
    return _mapping_from(response, "data")


def _optional_datetime(value: object) -> datetime | None:
    key = as_int(value)
    if key is None:
        return None
    try:
        return datetime.fromtimestamp(key, tz=UTC).replace(tzinfo=None)
    except (ValueError, OSError):
        return None


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
    async def _make_request(self, cmd: str, **params: Any) -> dict[str, object]:
        url = f"{self.base_url}/api/v2"
        response = await self.session.get(
            url,
            params={"apikey": self.api_key, "cmd": cmd, **params},
            timeout=self.timeout,
        )
        response.raise_for_status()
        resp: dict[str, object] = response.json()
        return resp

    async def health(self) -> bool:
        """Check server health and API key."""
        try:
            data = await self._make_request("status")
            response = _mapping_from(data, "response")
            return bool(response and response.get("result") == "success")
        except Exception:
            return False

    async def get_history(
        self,
        media_type: str | None = None,
        section_id: int | None = None,
        length: int = 10000,
        start: int = 0,
    ) -> dict[str, object]:
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
        payload = _history_payload(result)
        return dict(payload) if payload is not None else {}

    async def get_play_counts(
        self,
        media_type: str,
        since: datetime | None = None,
        page_size: int = 10000,
        episode_key: str = "grandparent_rating_key",
    ) -> dict[int, tuple[int, datetime | None]]:
        """Aggregate play counts and last-played timestamps from Tautulli history.

        Args:
            media_type: ``"movie"`` or ``"episode"``.
            since: If provided, only fetch records on or after this datetime minus
                a 1 day overlap buffer (date granularity safety margin).
            page_size: Number of records to request per API page.
            episode_key: Tautulli record key used for episode roll-ups. Use
                ``grandparent_rating_key`` for series or ``parent_rating_key`` for
                seasons.

        Returns:
            For ``media_type="movie"``:
                ``{rating_key: (play_count, last_played_at)}``
            For ``media_type="episode"``:
                ``{grandparent_rating_key: (play_count, last_played_at)}``
                (i.e. series-level rollup keyed by the show's rating key)
        """
        params: dict[str, str | int] = {"media_type": media_type, "length": page_size}

        if since is not None:
            # subtract 1 day buffer; Tautulli's `after` is date granularity.
            cutoff = since - timedelta(days=1)
            params["after"] = cutoff.strftime("%Y-%m-%d")

        aggregated: dict[int, tuple[int, datetime | None]] = {}
        offset = 0

        while True:
            params["start"] = offset
            data = await self._make_request("get_history", **params)
            payload = _history_payload(data)
            if payload is None:
                records: list[Mapping[str, object]] = []
                records_total = 0
            else:
                records = _mapping_list(payload.get("data"))
                records_total = as_int(payload.get("recordsFiltered")) or 0

            for record in records:
                if media_type == "episode":
                    key = record.get(episode_key)
                else:
                    key = record.get("rating_key")

                parsed_key = as_int(key)
                if parsed_key is None:
                    continue

                # parse stopped timestamp as UTC datetime
                last_played = _optional_datetime(record.get("stopped"))

                existing = aggregated.get(parsed_key)
                if existing is None:
                    aggregated[parsed_key] = (1, last_played)
                else:
                    prev_count, prev_last = existing
                    merged_last = (
                        max(filter(None, [prev_last, last_played]))
                        if (prev_last or last_played)
                        else None
                    )
                    aggregated[parsed_key] = (prev_count + 1, merged_last)

            offset += page_size
            if offset >= records_total:
                break

        return aggregated

    async def get_history_records(
        self,
        *,
        since: datetime | None = None,
        page_size: int = 5000,
    ) -> list[Mapping[str, object]]:
        """Fetch ungrouped movie and episode history in one paginated pass."""
        params: dict[str, str | int] = {
            "grouping": 0,
            "length": max(1, page_size),
        }
        if since is not None:
            cutoff = since - timedelta(days=1)
            params["after"] = cutoff.strftime("%Y-%m-%d")

        records: list[Mapping[str, object]] = []
        offset = 0
        while True:
            params["start"] = offset
            data = await self._make_request("get_history", **params)
            payload = _history_payload(data)
            if payload is None:
                break
            page = _mapping_list(payload.get("data"))
            records.extend(
                record
                for record in page
                if str(record.get("media_type") or "").lower() in {"movie", "episode"}
            )
            total = as_int(payload.get("recordsFiltered")) or 0
            offset += len(page)
            if not page or offset >= total:
                break
        return records

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
            data: dict[str, object] = response.json()

            response_data = _mapping_from(data, "response")
            if response_data and response_data.get("result") == "success":
                return True
            raise ValueError("Tautulli returned an unsuccessful status response")
