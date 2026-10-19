"""Isolated audit reproducers; assertions describe the required safe behavior."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    DeleteRequest,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service
from backend.models.media import MovieVersionData
from backend.tasks import sync


@asynccontextmanager
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


def version(movie_id, key):
    return MovieVersion(
        movie_id=movie_id,
        service=Service.PLEX,
        service_item_id="item-" + key,
        service_media_id=key,
        library_id="library",
        library_name="Movies",
        path=f"/movies/{key}/film.mkv",
        size=1000,
        video_width=1920,
        video_height=1080,
        video_codec="h264",
    )


def incoming(key):
    return MovieVersionData(
        service=Service.PLEX,
        service_item_id="item-" + key,
        service_media_id=key,
        library_id="library",
        library_name="Movies",
        path=f"/movies/{key}/film.mkv",
        size=1000,
        added_at=None,
        video_width=1920,
        video_height=1080,
        video_codec="h264",
    )


@pytest.mark.parametrize("keys", [("original", "copy"), ("copy", "original")])
def test_identical_new_copy_must_not_steal_existing_candidate(keys):
    async def run():
        async with database() as factory:
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.flush()
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="original selected",
                    reason_data=[],
                )
                db.add(candidate)
                await db.commit()
                await sync._upsert_movie_versions(
                    db, movie, [incoming(key) for key in keys]
                )
                await db.commit()
                rows = (await db.scalars(select(MovieVersion))).all()
                assert len(rows) == 2, [
                    (v.id, v.service_media_id, v.path) for v in rows
                ]
                assert (
                    await db.get(MovieVersion, candidate.movie_version_id)
                ).path == "/movies/original/film.mkv"

    asyncio.run(run())


@pytest.mark.parametrize("keys", [("renamed",), ("renamed", "ambiguous-copy")])
def test_rename_preserves_scope_only_when_unambiguous(keys):
    async def run():
        async with database() as factory:
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.flush()
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="original selected",
                    reason_data=[],
                )
                db.add(candidate)
                await db.commit()
                original_id, candidate_id = original.id, candidate.id
                await sync._upsert_movie_versions(
                    db, movie, [incoming(key) for key in keys]
                )
                await db.commit()
            async with factory() as db:
                rows = (await db.scalars(select(MovieVersion))).all()
                assert {v.service_media_id for v in rows} == set(keys)
                candidate = await db.get(ReclaimCandidate, candidate_id)
                if len(keys) == 1:
                    assert rows[0].id == original_id
                    assert candidate.movie_version_id == original_id
                else:
                    assert candidate is None

    asyncio.run(run())


def test_direct_match_refreshes_item_id():
    async def run():
        from dataclasses import replace

        async with database() as factory:
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.commit()
                await sync._upsert_movie_versions(
                    db,
                    movie,
                    [replace(incoming("original"), service_item_id="new-item")],
                )
                await db.commit()
                assert original.service_item_id == "new-item"

    asyncio.run(run())
