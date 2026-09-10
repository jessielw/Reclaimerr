from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from backend.core.rule_engine import TARGET_SERIES, _build_context
from backend.database.models import Series
from backend.enums import MediaType
from backend.tasks.sync import (
    ACTIVE_SERIES_METADATA_REFRESH_DAYS,
    FINISHED_SERIES_METADATA_REFRESH_DAYS,
    _needs_metadata_refresh,
    _update_series_tmdb_metadata,
)


class _FakeTMDBService:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def get_tv_details(self, tmdb_id: int | str) -> dict:
        return self._payload


def _established_series(status: str = "Returning Series") -> Series:
    """A long-running show with complete display metadata already cached."""
    series = Series(title="Silo", tmdb_id=125988)
    series.tmdb_first_air_date = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=1200
    )
    series.tmdb_last_air_date = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=602
    )
    series.status = status
    series.vote_average = 8.0
    series.popularity = 40.0
    series.backdrop_url = "/backdrop.jpg"
    series.poster_url = "/poster.jpg"
    return series


def test_established_series_still_refreshes_after_interval() -> None:
    series = _established_series()
    series.last_metadata_refresh_at = datetime.now(UTC) - timedelta(
        days=ACTIVE_SERIES_METADATA_REFRESH_DAYS + 1
    )

    assert _needs_metadata_refresh(series, MediaType.SERIES) is True


def test_series_not_refreshed_inside_interval() -> None:
    series = _established_series()
    series.last_metadata_refresh_at = datetime.now(UTC) - timedelta(days=1)

    assert _needs_metadata_refresh(series, MediaType.SERIES) is False


def test_finished_series_uses_longer_interval() -> None:
    series = _established_series(status="Ended")
    series.last_metadata_refresh_at = datetime.now(UTC) - timedelta(
        days=ACTIVE_SERIES_METADATA_REFRESH_DAYS + 1
    )

    assert _needs_metadata_refresh(series, MediaType.SERIES) is False

    series.last_metadata_refresh_at = datetime.now(UTC) - timedelta(
        days=FINISHED_SERIES_METADATA_REFRESH_DAYS + 1
    )

    assert _needs_metadata_refresh(series, MediaType.SERIES) is True


def test_unknown_status_series_uses_active_interval() -> None:
    series = _established_series(status="")
    series.status = None
    series.last_metadata_refresh_at = datetime.now(UTC) - timedelta(
        days=ACTIVE_SERIES_METADATA_REFRESH_DAYS
    )

    assert _needs_metadata_refresh(series, MediaType.SERIES) is True


def test_stale_last_air_date_is_updated_and_drives_rule_context() -> None:
    """An ongoing series must not stay pinned to an old season's finale."""
    series = _established_series()
    series.last_metadata_refresh_at = datetime.now(UTC) - timedelta(days=90)
    stale_last_air_date = series.tmdb_last_air_date

    assert _needs_metadata_refresh(series, MediaType.SERIES) is True

    new_last_air = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3)
    service = _FakeTMDBService(
        {
            "external_ids": {"imdb_id": "tt14688458", "tvdb_id": 400291},
            "name": "Silo",
            "original_name": "Silo",
            "first_air_date": series.tmdb_first_air_date.strftime("%Y-%m-%d"),  # type: ignore[reportOptionalMemberAccess]
            "last_air_date": new_last_air.strftime("%Y-%m-%d"),
            "status": "Returning Series",
            "number_of_seasons": 3,
            "vote_average": 8.1,
            "vote_count": 1200,
        }
    )

    asyncio.run(_update_series_tmdb_metadata(series, 125988, service))  # type: ignore[arg-type]

    assert series.tmdb_last_air_date != stale_last_air_date
    assert series.tmdb_last_air_date.date() == new_last_air.date()  # type: ignore[reportOptionalMemberAccess]
    assert series.season_count == 3

    context = _build_context(
        TARGET_SERIES, None, None, series, None, compute_disk=False
    )

    assert context["tmdb.days_since_last_air_date"] == 3
