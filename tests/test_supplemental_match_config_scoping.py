"""Regression tests for SupplementalMediaMatch being scoped by
source_service_config_id rather than just source_service (type).

Two linked media servers of the SAME type (e.g. two linked Jellyfin configs)
must contribute independent supplemental matches - before the Phase 0
migration + Phase 4 rework, everything was keyed by `source_service` alone,
so a second same-type config's matches would collide with (overwrite/prune)
the first's.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import Movie, ServiceConfig, SupplementalMediaMatch
from backend.enums import MediaType, Service
from backend.tasks.sync import (
    _clear_supplemental_matches,
    _prune_supplemental_matches,
    _replace_supplemental_matches,
)


async def _make_session() -> tuple[async_sessionmaker[AsyncSession], object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return session_maker, engine


def test_replace_supplemental_matches_is_scoped_per_config():
    async def run() -> None:
        session_maker, engine = await _make_session()
        async with session_maker() as db:
            config_a = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin A",
                base_url="http://jellyfin-a",
                api_key="x",
                enabled=True,
            )
            config_b = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin B",
                base_url="http://jellyfin-b",
                api_key="x",
                enabled=True,
            )
            movie = Movie(title="Movie", tmdb_id=1)
            db.add_all([config_a, config_b, movie])
            await db.flush()

            # seed config A with a match
            await _replace_supplemental_matches(
                db,
                config_a.id,
                MediaType.MOVIE,
                [
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_a.id,
                        source_item_id="a-item",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    )
                ],
            )
            await db.commit()

            # now seed config B independently - must not disturb config A's row
            await _replace_supplemental_matches(
                db,
                config_b.id,
                MediaType.MOVIE,
                [
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_b.id,
                        source_item_id="b-item",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    )
                ],
            )
            await db.commit()

            rows = (await db.execute(select(SupplementalMediaMatch))).scalars().all()
            by_config = {row.source_service_config_id: row.source_item_id for row in rows}
            assert by_config == {config_a.id: "a-item", config_b.id: "b-item"}

            # replacing config A's matches again must not touch config B's row
            await _replace_supplemental_matches(
                db,
                config_a.id,
                MediaType.MOVIE,
                [
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_a.id,
                        source_item_id="a-item-updated",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    )
                ],
            )
            await db.commit()

            rows = (await db.execute(select(SupplementalMediaMatch))).scalars().all()
            by_config = {row.source_service_config_id: row.source_item_id for row in rows}
            assert by_config == {config_a.id: "a-item-updated", config_b.id: "b-item"}
        await engine.dispose()

    asyncio.run(run())


def test_clear_supplemental_matches_only_clears_its_own_config(monkeypatch):
    async def run() -> None:
        session_maker, engine = await _make_session()
        # _clear_supplemental_matches opens its own session via the
        # module-level async_db - point it at our in-memory engine.
        monkeypatch.setattr("backend.tasks.sync.async_db", session_maker)
        async with session_maker() as db:
            config_a = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin A",
                base_url="http://jellyfin-a",
                api_key="x",
                enabled=True,
            )
            config_b = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin B",
                base_url="http://jellyfin-b",
                api_key="x",
                enabled=True,
            )
            movie = Movie(title="Movie", tmdb_id=2)
            db.add_all([config_a, config_b, movie])
            await db.flush()
            db.add_all(
                [
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_a.id,
                        source_item_id="a-item",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    ),
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_b.id,
                        source_item_id="b-item",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    ),
                ]
            )
            await db.commit()
            config_a_id, config_b_id = config_a.id, config_b.id

        await _clear_supplemental_matches(config_a_id)

        async with session_maker() as db:
            rows = (await db.execute(select(SupplementalMediaMatch))).scalars().all()
            assert [row.source_service_config_id for row in rows] == [config_b_id]
        await engine.dispose()

    asyncio.run(run())


def test_prune_supplemental_matches_only_removes_inactive_configs(monkeypatch):
    async def run() -> None:
        session_maker, engine = await _make_session()
        # _prune_supplemental_matches opens its own session via the
        # module-level async_db - point it at our in-memory engine.
        monkeypatch.setattr("backend.tasks.sync.async_db", session_maker)
        async with session_maker() as db:
            config_a = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin A",
                base_url="http://jellyfin-a",
                api_key="x",
                enabled=True,
            )
            config_b = ServiceConfig(
                service_type=Service.JELLYFIN,
                name="Jellyfin B (now disabled)",
                base_url="http://jellyfin-b",
                api_key="x",
                enabled=False,
            )
            movie = Movie(title="Movie", tmdb_id=3)
            db.add_all([config_a, config_b, movie])
            await db.flush()
            db.add_all(
                [
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_a.id,
                        source_item_id="a-item",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    ),
                    SupplementalMediaMatch(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config_b.id,
                        source_item_id="b-item",
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                    ),
                ]
            )
            await db.commit()
            config_a_id = config_a.id

        # only config A is still active/linked
        await _prune_supplemental_matches({config_a_id})

        async with session_maker() as db:
            rows = (await db.execute(select(SupplementalMediaMatch))).scalars().all()
            assert [row.source_service_config_id for row in rows] == [config_a_id]
        await engine.dispose()

    asyncio.run(run())
