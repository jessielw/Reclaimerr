from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import Episode, Season, Series, SeriesArrRef, ServiceConfig
from backend.enums import Service
from backend.models.media import (
    AggregatedEpisodeData,
    AggregatedSeasonData,
    AggregatedSeriesData,
    ExternalIDs,
)
from backend.services.sonarr import SonarrEpisodeSyncData, build_sonarr_series_from_dict
from backend.tasks import sync


@pytest.mark.parametrize("sonarr_order", ["dvd", "aired", None])
def test_sync_clears_stale_numbered_data_when_order_is_incompatible(
    monkeypatch, sonarr_order, tmp_path
):
    async def run():
        db_path = tmp_path / "ordering.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(sync, "async_db", factory)
        old_date = datetime(2020, 1, 1)
        new_date = datetime(2026, 10, 13)
        async with factory() as db:
            main = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex.invalid",
                api_key="synthetic",
                name="Main",
                enabled=True,
                is_main=True,
            )
            arr = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr.invalid",
                api_key="synthetic",
                name="Arr",
                enabled=True,
            )
            series = Series(title="Example", tmdb_id=123, tvdb_id="456")
            series.arr_added_at = old_date
            db.add_all([main, arr, series])
            await db.flush()
            season = Season(
                series_id=series.id, season_number=1, sonarr_episode_numbers=[99]
            )
            season.arr_added_at = old_date
            db.add(season)
            await db.flush()
            episode = Episode(season_id=season.id, episode_number=1)
            episode.arr_added_at = old_date
            db.add(episode)
            await db.commit()
        aggregated = AggregatedSeriesData(
            id="primary-show",
            name="Example",
            year=None,
            service=Service.PLEX,
            library_name="TV",
            library_id="1",
            path="/synthetic/Example",
            added_at=None,
            external_ids=ExternalIDs(
                tmdb=123, tvdb="456", imdb=None, tmdb_collection=None
            ),
            size=1,
            view_count=0,
            last_viewed_at=None,
            season_data=[
                AggregatedSeasonData(
                    service_series_id="primary-show",
                    season_number=1,
                    size=1,
                    episode_count=1,
                    view_count=0,
                    last_viewed_at=None,
                    episode_data=[
                        AggregatedEpisodeData(episode_number=1, view_count=0)
                    ],
                )
            ],
        )
        probes = []

        async def sonarr_episode_order(*_args, **_kwargs):
            with closing(sqlite3.connect(db_path, timeout=0.1)) as other:
                try:
                    other.execute("UPDATE service_configs SET name = name")
                    other.commit()
                    probes.append(None)
                except sqlite3.OperationalError as exc:
                    probes.append(str(exc))
            return sonarr_order

        sonarr = SimpleNamespace(
            get_all_series=AsyncMock(
                return_value=[
                    build_sonarr_series_from_dict(
                        {"id": 42, "tmdbId": 123, "tvdbId": 456, "tags": [7]}
                    )
                ]
            ),
            get_tags=AsyncMock(return_value=[SimpleNamespace(id=7, label="keep")]),
            get_series_episode_order=AsyncMock(side_effect=sonarr_episode_order),
            get_episode_sync_data=AsyncMock(
                return_value=SonarrEpisodeSyncData(
                    file_dates={(1, 1): new_date}, episode_numbers_by_season={1: {1}}
                )
            ),
        )
        media = SimpleNamespace(get_series_episode_order=AsyncMock(return_value="dvd"))
        monkeypatch.setattr(sync.service_manager, "_sonarr_clients", {arr.id: sonarr})
        monkeypatch.setattr(sync.service_manager, "_sonarr", None)
        monkeypatch.setattr(
            type(sync.service_manager), "get_media_server", lambda *args: media
        )
        monkeypatch.setattr(
            sync, "gather_series", AsyncMock(return_value=({123: aggregated}, {}))
        )
        monkeypatch.setattr(
            sync,
            "AsyncTMDBClient",
            lambda: SimpleNamespace(session=SimpleNamespace(close=AsyncMock())),
        )
        monkeypatch.setattr(sync, "_update_series_tmdb_metadata", AsyncMock())
        try:
            assert await sync.sync_series(allow_soft_delete=False) == {123}
            assert probes == [None], (
                "Sonarr HTTP ran while the database writer was held"
            )
            async with factory() as db:
                stored_series = await db.get(Series, series.id)
                stored_season = await db.scalar(select(Season))
                stored_episode = await db.scalar(select(Episode))
                assert stored_series.arr_tags == ["keep"]
                assert await db.scalar(select(SeriesArrRef)) is not None
                if sonarr_order == "dvd":
                    assert stored_season.sonarr_episode_numbers == [1]
                    assert (
                        stored_series.arr_added_at
                        == stored_season.arr_added_at
                        == stored_episode.arr_added_at
                        == new_date
                    )
                    sonarr.get_episode_sync_data.assert_awaited_once_with(42)
                else:
                    assert stored_season.sonarr_episode_numbers is None
                    assert stored_series.arr_added_at is None
                    assert (
                        stored_season.arr_added_at is None
                        and stored_episode.arr_added_at is None
                    )
                    sonarr.get_episode_sync_data.assert_not_awaited()
        finally:
            await engine.dispose()

    asyncio.run(run())
