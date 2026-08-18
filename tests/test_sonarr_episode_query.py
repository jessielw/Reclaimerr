from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from backend.services.sonarr import SonarrClient, build_sonarr_series_from_dict


def test_sonarr_series_status_is_normalized() -> None:
    for raw, expected in (
        ("Continuing", "continuing"),
        ("ended", "ended"),
        ("UPCOMING", "upcoming"),
        ("deleted", "deleted"),
        (None, None),
    ):
        series = build_sonarr_series_from_dict(
            {"id": 1, "title": "Series", "status": raw}
        )
        assert series.status == expected


def test_sonarr_series_includes_title_slug() -> None:
    series = build_sonarr_series_from_dict(
        {"id": 1, "title": "Series", "titleSlug": "example-series"}
    )

    assert series.title_slug == "example-series"


def test_get_episodes_filters_by_season_when_requested() -> None:
    async def run() -> None:
        client = SonarrClient(api_key="key", base_url="http://sonarr")
        request = AsyncMock(return_value=(200, []))
        try:
            with patch.object(SonarrClient, "_make_request", request):
                assert await client.get_episodes(42, season_number=3) == []
        finally:
            await client.session.close()

        request.assert_awaited_once_with(
            "GET",
            "episode",
            params={"seriesId": 42, "seasonNumber": 3},
            timeout=60,
        )

    asyncio.run(run())
