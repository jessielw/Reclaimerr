from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Movie,
    MovieArrRef,
    ReclaimCandidate,
    ReclaimRule,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.tasks import cleanup


class TaggingRadarr:
    def __init__(self, movie_id: int, tmdb_id: int) -> None:
        self.movie = SimpleNamespace(id=movie_id, tmdb_id=tmdb_id, tags=[])
        self.added: list[tuple[list[int], int]] = []
        self.removed: list[tuple[list[int], int]] = []

    async def get_all_movies(self):
        return [self.movie]

    async def get_tags(self):
        return [SimpleNamespace(id=5, label="rec-clean")]

    async def get_or_create_tag(self, _label: str):
        return SimpleNamespace(id=5, label="rec-clean")

    async def add_tag_to_movies(self, movie_ids: list[int], tag_id: int) -> None:
        self.added.append((movie_ids, tag_id))

    async def remove_tag_from_movies(self, movie_ids: list[int], tag_id: int) -> None:
        self.removed.append((movie_ids, tag_id))


def test_radarr_tag_sync_applies_one_rule_to_multiple_selected_instances(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        monkeypatch.setattr(cleanup, "async_db", session_maker)

        async with session_maker() as db:
            configs = [
                ServiceConfig(
                    service_type=Service.RADARR,
                    base_url=f"http://radarr-{index}",
                    api_key="secret",
                    name=f"Radarr {index}",
                    enabled=True,
                )
                for index in (1, 2)
            ]
            movie = Movie(title="Movie", tmdb_id=101, size=100)
            rule = ReclaimRule(
                name="Clean",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition={
                    "version": 1,
                    "root": {"type": "group", "op": "and", "children": []},
                },
                action={"tag_enabled": True, "arr_tag": "rec-clean"},
            )
            db.add_all([*configs, movie, rule])
            await db.flush()
            arr_ids = [51, 52]
            for config, arr_id in zip(configs, arr_ids, strict=True):
                db.add(
                    MovieArrRef(
                        movie_id=movie.id,
                        service_config_id=config.id,
                        arr_movie_id=arr_id,
                        tmdb_id=movie.tmdb_id,
                    )
                )
            rule.action = {
                **(rule.action or {}),
                "radarr_service_config_ids": [config.id for config in configs],
            }
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    matched_rule_ids=[rule.id],
                    matched_criteria={},
                    reason="cleanup",
                    reason_data=[],
                    movie_id=movie.id,
                )
            )
            await db.commit()

        clients = {
            config.id: TaggingRadarr(arr_id, movie.tmdb_id)
            for config, arr_id in zip(configs, arr_ids, strict=True)
        }
        monkeypatch.setattr(
            cleanup.service_manager, "_radarr", next(iter(clients.values()))
        )
        monkeypatch.setattr(cleanup.service_manager, "_radarr_clients", clients)

        tagged, untagged = await cleanup._sync_rule_radarr_tags()

        assert (tagged, untagged) == (2, 0)
        assert [client.added for client in clients.values()] == [
            [([51], 5)],
            [([52], 5)],
        ]
        await engine.dispose()

    asyncio.run(run())
