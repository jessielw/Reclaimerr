"""A Plex history page that fails must never read as "these items were not watched".

Issue #401: one failed page during Sync Media left hundreds of series with a
view count of 0, and a view-count rule turned them into cleanup candidates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from backend.database.models import Movie, Season, Series
from backend.services.plex import PlexHistoryIncompleteError, PlexService
from backend.tasks.sync import _apply_media_server_watch


def _history_page(start: int, total: int, size: int) -> dict[str, object]:
    return {
        "MediaContainer": {
            "totalSize": total,
            "Metadata": [
                {
                    "ratingKey": f"ep{n}",
                    "parentRatingKey": "season1",
                    "grandparentRatingKey": "show1",
                    "accountID": "1",
                    "viewedAt": 1_700_000_000 + n,
                }
                for n in range(start, min(start + size, total))
            ],
        }
    }


def _service_failing_at(
    monkeypatch, fail_offset: int | None
) -> tuple[PlexService, list[int]]:
    calls: list[int] = []

    async def fake_request(self, endpoint, params=None, **kwargs):
        start = int(params["X-Plex-Container-Start"])
        calls.append(start)
        if fail_offset is not None and start == fail_offset:
            raise ConnectionError("Remote end closed connection without response")
        return _history_page(start, 3, int(params["X-Plex-Container-Size"])), 200

    monkeypatch.setattr(PlexService, "_make_request", fake_request)
    return PlexService("token", "http://plex.local"), calls


def test_failed_page_raises_with_partial_records(monkeypatch) -> None:
    service, _ = _service_failing_at(monkeypatch, fail_offset=2)

    with pytest.raises(PlexHistoryIncompleteError) as exc_info:
        asyncio.run(
            service._fetch_all_history_records(library_section_ids=["4"], page_size=2)
        )

    assert [r["ratingKey"] for r in exc_info.value.records] == ["ep0", "ep1"]


def test_partial_history_is_not_cached(monkeypatch) -> None:
    service, calls = _service_failing_at(monkeypatch, fail_offset=2)

    async def run() -> None:
        for _ in range(2):
            with pytest.raises(PlexHistoryIncompleteError):
                await service._get_all_history_records(
                    library_section_ids=["4"], page_size=2
                )

    asyncio.run(run())

    # both calls went to Plex: the first partial result was not reused
    assert calls == [0, 2, 0, 2]


def test_get_all_history_reports_incomplete(monkeypatch) -> None:
    service, _ = _service_failing_at(monkeypatch, fail_offset=2)

    history, complete = asyncio.run(
        service._get_all_history(
            library_section_ids=["4"], key_field="grandparentRatingKey", page_size=2
        )
    )

    assert complete is False
    assert history["show1"][0] == 2


def test_get_all_history_reports_complete(monkeypatch) -> None:
    service, _ = _service_failing_at(monkeypatch, fail_offset=None)

    history, complete = asyncio.run(
        service._get_all_history(
            library_section_ids=["4"], key_field="grandparentRatingKey", page_size=2
        )
    )

    assert complete is True
    assert history["show1"][0] == 3


def test_watched_snapshots_raise_on_partial_history(monkeypatch) -> None:
    """The snapshot cache records the error and keeps its cursor instead."""
    service, _ = _service_failing_at(monkeypatch, fail_offset=0)

    async def no_items(self, **kwargs):
        return []

    async def sections(self):
        return [{"key": "4", "title": "TV", "type": "show"}]

    monkeypatch.setattr(PlexService, "_get_section_metadata_items", no_items)
    monkeypatch.setattr(PlexService, "get_library_sections", sections)

    with pytest.raises(PlexHistoryIncompleteError):
        asyncio.run(service.get_watched_user_snapshots_with_cursor(use_cache=False))


WATCHED = datetime(2026, 9, 1, 12, 0)

_REQUIRED = {
    Movie: {"title": "Pernicious", "tmdb_id": 326441},
    Series: {"title": "The Terror", "tmdb_id": 75191},
    Season: {"series_id": 1, "season_number": 1},
}


def _row(model, view_count: int, last_viewed_at: datetime | None):
    row = model(**_REQUIRED[model])
    row.view_count = view_count
    row.last_viewed_at = last_viewed_at
    return row


@pytest.mark.parametrize("model", [Movie, Series, Season])
def test_partial_history_never_lowers_stored_watch(model) -> None:
    row = _row(model, 14, WATCHED)

    kept = _apply_media_server_watch(row, 0, None, history_complete=False)

    assert kept is True
    assert row.view_count == 14
    assert row.last_viewed_at == WATCHED


def test_partial_history_still_raises_stored_watch() -> None:
    row = _row(Series, 2, WATCHED)
    newer = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)

    kept = _apply_media_server_watch(row, 5, newer, history_complete=False)

    assert kept is False
    assert row.view_count == 5
    assert row.last_viewed_at == newer


def test_complete_history_is_authoritative() -> None:
    row = _row(Series, 14, WATCHED)

    kept = _apply_media_server_watch(row, 0, None, history_complete=True)

    assert kept is False
    assert row.view_count == 0
    assert row.last_viewed_at is None


def test_season_partial_history_stores_naive_utc() -> None:
    row = _row(Season, 0, None)
    aware = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)

    _apply_media_server_watch(row, 1, aware, history_complete=False, naive=True)

    assert row.last_viewed_at == datetime(2026, 9, 20, 8, 0)


def test_episode_watch_data_merges_history_from_other_users(monkeypatch) -> None:
    """Issue #503: the scan only sees the connected account's plays per episode."""
    from backend.models.media import (
        AggregatedEpisodeData,
        AggregatedSeasonData,
        AggregatedSeriesData,
        ExternalIDs,
    )

    watched = datetime(2026, 6, 14, tzinfo=UTC)
    episodes = [
        AggregatedEpisodeData(episode_number=1, view_count=0, plex_rating_key="ep1"),
        AggregatedEpisodeData(episode_number=2, view_count=0, plex_rating_key="ep2"),
    ]
    season = AggregatedSeasonData(
        service_series_id="show1",
        season_number=1,
        size=0,
        episode_count=2,
        view_count=0,
        last_viewed_at=None,
        service_season_id="season1",
        episode_data=episodes,
    )
    series = AggregatedSeriesData(
        id="show1",
        name="Show",
        year=2026,
        service="plex",
        library_name="TV",
        library_id="4",
        path=None,
        added_at=None,
        external_ids=ExternalIDs(None, None, None, None),
        size=0,
        view_count=0,
        last_viewed_at=None,
        season_data=[season],
    )
    records = [
        {
            "ratingKey": "ep1",
            "parentRatingKey": "season1",
            "grandparentRatingKey": "show1",
            "accountID": "2",
            "viewedAt": int(watched.timestamp()),
        }
    ]

    async def fake_get_series(self, included_libraries=None):
        return [series]

    async def fake_sections(self):
        return [{"type": "show", "key": "4", "title": "TV"}]

    async def fake_records(self, **kwargs):
        return records

    monkeypatch.setattr(PlexService, "get_series", fake_get_series)
    monkeypatch.setattr(PlexService, "get_library_sections", fake_sections)
    monkeypatch.setattr(PlexService, "_get_all_history_records", fake_records)

    result = asyncio.run(
        PlexService("token", "http://plex.local").get_aggregated_series()
    )

    ep1, ep2 = result[0].season_data[0].episode_data
    assert (ep1.view_count, ep1.last_viewed_at) == (1, watched)
    assert (ep2.view_count, ep2.last_viewed_at) == (0, None)
