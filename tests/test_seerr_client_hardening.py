"""SeerrClient edge cases around titles Seerr never tracked and odd API data.

A title with no `mediaInfo` is not an error, a real HTTP failure must not be
swallowed, and one request with an unknown status must not sink a whole page.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.enums import MediaType
from backend.services.seerr import SeerrClient


def _client(response: object = None, *, side_effect: Exception | None = None):
    # SeerrClient uses __slots__, so stub the request on a subclass
    make_request = AsyncMock(return_value=(200, response), side_effect=side_effect)

    class _StubClient(SeerrClient):
        async def _make_request(self, *args, **kwargs):  # type: ignore[override]
            return await make_request(*args, **kwargs)

    return _StubClient("key", "http://seerr")


def _request(request_id: int, status: int) -> dict[str, object]:
    return {
        "id": request_id,
        "status": status,
        "type": "movie",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "media": {"id": 10, "tmdbId": 603},
        "requestedBy": {"id": 1, "username": "alice"},
    }


# TMDB details only, as Seerr returns them for a title it has never tracked
UNTRACKED = {"id": 603, "title": "The Matrix"}


def test_untracked_title_has_no_requests():
    async def run() -> None:
        client = _client(UNTRACKED)
        assert await client.get_movie_requests(603) == []
        assert await client.get_tv_requests(603) == []
        await client.delete_movie_requests(603)
        await client.delete_tv_requests(603)

    asyncio.run(run())


def test_untracked_title_has_no_media_id():
    async def run() -> None:
        client = _client(UNTRACKED)
        assert await client.get_media_id(603, MediaType.MOVIE) is None
        assert await client.get_media_id(603, MediaType.SERIES) is None

    asyncio.run(run())


def test_get_media_id_propagates_http_errors():
    async def run() -> None:
        client = _client(side_effect=ConnectionError("seerr down"))
        with pytest.raises(ConnectionError):
            await client.get_media_id(603, MediaType.MOVIE)
        with pytest.raises(ConnectionError):
            await client.delete_movie_media(603)

    asyncio.run(run())


def test_unknown_request_status_skips_only_that_request():
    async def run() -> None:
        client = _client(
            {
                "pageInfo": {"page": 1, "pages": 1, "results": 2},
                "results": [_request(1, 99), _request(2, 2)],
            }
        )
        _, requests = await client.get_requests()
        assert [req.id for req in requests] == [2]

    asyncio.run(run())
