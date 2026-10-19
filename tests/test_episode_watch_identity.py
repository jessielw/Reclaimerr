from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    Season,
    Series,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service
from backend.models.media import MediaWatchSnapshot
from backend.services.episode_ordering import EpisodeOrderingSnapshot
from backend.services.media_watch_snapshot_cache import MediaWatchSnapshotCache
from backend.services.playback_history import _identity_maps


@pytest.mark.parametrize("service", [Service.EMBY, Service.JELLYFIN, Service.PLEX])
@pytest.mark.parametrize("different_main_type", [False, True])
@pytest.mark.parametrize("has_match", [False, True])
def test_episode_watch_ids_are_isolated_by_server(
    monkeypatch, service, different_main_type, has_match
):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                main_type = (
                    (Service.PLEX if service != Service.PLEX else Service.EMBY)
                    if different_main_type
                    else service
                )
                main = ServiceConfig(
                    service_type=main_type,
                    base_url="http://main.invalid",
                    api_key="synthetic",
                    name="Main",
                    is_main=True,
                )
                linked = ServiceConfig(
                    service_type=service,
                    base_url="http://linked.invalid",
                    api_key="synthetic",
                    name="Linked",
                )
                db.add_all([main, linked])
                if different_main_type:
                    db.add(
                        ServiceConfig(
                            service_type=service,
                            base_url="http://other.invalid",
                            api_key="synthetic",
                            name="Other linked",
                        )
                    )
                wrong = Series(title="Unrelated", tmdb_id=101)
                right = Series(title="Intended", tmdb_id=202)
                db.add_all([wrong, right])
                await db.flush()
                wrong_season = Season(series_id=wrong.id, season_number=1)
                right_season = Season(series_id=right.id, season_number=2)
                db.add_all([wrong_season, right_season])
                await db.flush()
                id_attr = {
                    Service.PLEX: "plex_rating_key",
                    Service.EMBY: "emby_episode_id",
                    Service.JELLYFIN: "jellyfin_episode_id",
                }[service]
                wrong_episode = Episode(season_id=wrong_season.id, episode_number=1)
                setattr(wrong_episode, id_attr, "collision")
                right_episodes = [
                    Episode(season_id=right_season.id, episode_number=n) for n in (3, 4)
                ]
                db.add_all([wrong_episode, *right_episodes])
                await db.flush()
                if has_match:
                    db.add(
                        SupplementalMediaMatch(
                            source_service=service,
                            source_service_config_id=linked.id,
                            source_item_id="collision",
                            media_type=MediaType.SERIES,
                            series_id=right.id,
                            season_id=right_season.id,
                            episode_id=right_episodes[0].id,
                            signals={"episode_numbers": [3, 4]},
                            confidence=100,
                        )
                    )
                await db.commit()
                for name in ("primary_order", "linked_order"):
                    monkeypatch.setattr(
                        EpisodeOrderingSnapshot, name, AsyncMock(return_value="aired")
                    )
                (
                    rows,
                    unmatched,
                ) = await MediaWatchSnapshotCache._build_episode_watch_rows(
                    session=db,
                    source_service=service,
                    source_service_config_id=linked.id,
                    snapshots=[
                        MediaWatchSnapshot(
                            media_type=MediaType.SERIES,
                            tmdb_id=202,
                            watch_user_key="Alice",
                            source_item_id="collision",
                            last_watched_at=datetime(2026, 10, 19, tzinfo=UTC),
                            play_count=1,
                        )
                    ],
                )
                assert {
                    (r.series_tmdb_id, r.season_number, r.episode_number) for r in rows
                } == ({(202, 2, 3), (202, 2, 4)} if has_match else set())
                assert unmatched == (0 if has_match else 1)
                _, history_ids = await _identity_maps(db, service, linked.id)
                assert {
                    identity[3] for identity in history_ids.get("collision", [])
                } == ({202} if has_match else set())
                if not different_main_type:
                    (
                        main_rows,
                        unmatched,
                    ) = await MediaWatchSnapshotCache._build_episode_watch_rows(
                        session=db,
                        source_service=service,
                        source_service_config_id=main.id,
                        snapshots=[
                            MediaWatchSnapshot(
                                media_type=MediaType.SERIES,
                                tmdb_id=101,
                                watch_user_key="Alice",
                                source_item_id="collision",
                                last_watched_at=datetime(2026, 10, 19, tzinfo=UTC),
                                play_count=1,
                            )
                        ],
                    )
                    assert unmatched == 0
                    assert {
                        (r.series_tmdb_id, r.season_number, r.episode_number)
                        for r in main_rows
                    } == {(101, 1, 1)}
        finally:
            await engine.dispose()

    asyncio.run(run())
