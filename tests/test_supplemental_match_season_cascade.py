"""Regression tests for deleting a season that a linked server has matched.

`supplemental_media_matches.season_id` / `.episode_id` were declared with no
ON DELETE action, so any install with a linked media server (the only thing
that writes supplemental matches) hit

    (sqlite3.IntegrityError) FOREIGN KEY constraint failed
    [SQL: DELETE FROM seasons WHERE seasons.id = ?]

whenever a season candidate was applied, moved, or pruned by sync. A match row
holding only `episode_id` blocked it just the same, because deleting a season
cascades into `episodes`.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    Season,
    Series,
    ServiceConfig,
    SupplementalMediaMatch,
)
from backend.enums import MediaType, Service

MIGRATION = importlib.import_module(
    "backend.alembic.versions.f2c8b4d7e0a3_cascade_supplemental_match_season_episode"
)


async def _seeded_session() -> tuple[
    async_sessionmaker[AsyncSession], object, int, int
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

    async with session_maker() as db:
        await db.execute(text("PRAGMA foreign_keys=ON"))
        config = ServiceConfig(
            service_type=Service.JELLYFIN,
            name="Linked Jellyfin",
            base_url="http://jellyfin-linked",
            api_key="x",
            enabled=True,
        )
        db.add(config)
        await db.flush()

        series = Series(tmdb_id=4242, title="Cascade Show")
        db.add(series)
        await db.flush()
        season = Season(series_id=series.id, season_number=1)
        db.add(season)
        await db.flush()
        episode = Episode(season_id=season.id, episode_number=1)
        db.add(episode)
        await db.flush()

        db.add(
            SupplementalMediaMatch(
                source_service=Service.JELLYFIN,
                source_service_config_id=config.id,
                source_item_id="linked-season-1",
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season.id,
            )
        )
        db.add(
            SupplementalMediaMatch(
                source_service=Service.JELLYFIN,
                source_service_config_id=config.id,
                source_item_id="linked-episode-1",
                media_type=MediaType.SERIES,
                series_id=series.id,
                episode_id=episode.id,
            )
        )
        await db.commit()
        return session_maker, engine, season.id, series.id


def test_deleting_a_season_clears_its_supplemental_matches():
    async def run() -> None:
        session_maker, engine, season_id, series_id = await _seeded_session()

        async with session_maker() as db:
            await db.execute(text("PRAGMA foreign_keys=ON"))
            season = (
                await db.execute(select(Season).where(Season.id == season_id))
            ).scalar_one()
            await db.delete(season)
            await db.commit()

        async with session_maker() as db:
            matches = (await db.execute(select(SupplementalMediaMatch))).scalars().all()
            episodes = (await db.execute(select(Episode))).scalars().all()
            # the series row survives - only the season is being reclaimed
            series = (await db.execute(select(Series))).scalars().all()

        assert matches == []
        assert episodes == []
        assert [s.id for s in series] == [series_id]

        await engine.dispose()

    asyncio.run(run())


# the pre-migration table, as c2e5b8a1f4d7 + c9a3e5b7d1f4 leave it
_PRIOR_MATCHES_TABLE = """
    CREATE TABLE supplemental_media_matches (
        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        source_service VARCHAR(8) NOT NULL,
        source_service_config_id INTEGER NOT NULL,
        source_item_id VARCHAR(100) NOT NULL,
        media_type VARCHAR(6) NOT NULL,
        movie_id INTEGER,
        series_id INTEGER,
        season_id INTEGER,
        source_media_id VARCHAR(100),
        path_tail VARCHAR(1024),
        confidence SMALLINT NOT NULL,
        signals JSON,
        updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
        episode_id INTEGER REFERENCES episodes (id),
        CONSTRAINT uq_supplemental_media_match_source_item
            UNIQUE (source_service_config_id, source_item_id, media_type),
        FOREIGN KEY(movie_id) REFERENCES movies (id),
        FOREIGN KEY(season_id) REFERENCES seasons (id),
        FOREIGN KEY(series_id) REFERENCES series (id),
        FOREIGN KEY(source_service_config_id)
            REFERENCES service_configs (id) ON DELETE CASCADE
    )
"""

_PRIOR_SCHEMA = (
    "CREATE TABLE seasons (id INTEGER PRIMARY KEY)",
    "CREATE TABLE episodes (id INTEGER PRIMARY KEY)",
    "CREATE TABLE movies (id INTEGER PRIMARY KEY)",
    "CREATE TABLE series (id INTEGER PRIMARY KEY)",
    "CREATE TABLE service_configs (id INTEGER PRIMARY KEY)",
    _PRIOR_MATCHES_TABLE,
)

_SEED_MATCHES = (
    "INSERT INTO supplemental_media_matches "
    "(id, source_service, source_service_config_id, source_item_id, "
    " media_type, series_id, season_id, episode_id, confidence) VALUES "
    # healthy season and episode matches
    "(1, 'JELLYFIN', 1, 'a', 'SERIES', 10, 20, NULL, 100), "
    "(2, 'JELLYFIN', 1, 'b', 'SERIES', 10, 20, 30, 100), "
    # left behind by a delete that ran with PRAGMA foreign_keys off
    "(3, 'JELLYFIN', 1, 'c', 'SERIES', 10, 99, NULL, 100), "
    "(4, 'JELLYFIN', 1, 'd', 'SERIES', 10, NULL, 98, 100), "
    # series-level match, unaffected either way
    "(5, 'JELLYFIN', 1, 'e', 'SERIES', 10, NULL, NULL, 90)"
)


def test_migration_cascades_and_drops_rows_already_orphaned(
    monkeypatch, tmp_path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'cascade.db'}")
    with engine.begin() as connection:
        for statement in _PRIOR_SCHEMA:
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO service_configs (id) VALUES (1)"))
        connection.execute(text("INSERT INTO series (id) VALUES (10)"))
        connection.execute(text("INSERT INTO seasons (id) VALUES (20)"))
        connection.execute(text("INSERT INTO episodes (id) VALUES (30)"))
        connection.execute(text(_SEED_MATCHES))

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(MIGRATION, "op", operations)
        MIGRATION.upgrade()

        surviving = [
            row[0]
            for row in connection.execute(
                text("SELECT id FROM supplemental_media_matches ORDER BY id")
            )
        ]
        assert surviving == [1, 2, 5]

        actions = {
            row[3]: row[6]
            for row in connection.execute(
                text("PRAGMA foreign_key_list(supplemental_media_matches)")
            )
        }
        assert actions["season_id"] == "CASCADE"
        assert actions["episode_id"] == "CASCADE"
        # untouched by this migration
        assert actions["series_id"] == "NO ACTION"
        assert actions["source_service_config_id"] == "CASCADE"

        indexes = {
            row[1]
            for row in connection.execute(
                text("PRAGMA index_list(supplemental_media_matches)")
            )
        }
        assert "ix_supplemental_media_matches_season_id" in indexes
        assert "ix_supplemental_media_matches_episode_id" in indexes

    engine.dispose()

    # PRAGMA foreign_keys is a no-op inside an open transaction, so the cascade
    # has to be exercised on a connection of its own.
    connection = sqlite3.connect(tmp_path / "cascade.db")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM seasons WHERE id = 20")
        connection.commit()
        surviving = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM supplemental_media_matches ORDER BY id"
            )
        ]
        # both matches for season 20 go with it; the series-level row stays
        assert surviving == [5]
    finally:
        connection.close()
