from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Response
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from backend.api.routes.auth import logout
from backend.core import auth
from backend.database import Base, set_sqlite_pragma
from backend.database import engine as app_engine
from backend.database.models import (
    BackgroundJob,
    Movie,
    Series,
    ServiceConfig,
    User,
    UserSession,
)
from backend.enums import BackgroundJobType, Service
from backend.jobs import queue
from backend.models.media import (
    AggregatedMovieData,
    AggregatedSeriesData,
    ExternalIDs,
    MovieVersionData,
)
from backend.tasks import sync


async def _database(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", pool_size=5)
    event.listen(engine.sync_engine, "connect", set_sqlite_pragma)
    # Exercise the production checkout policy on a separate, disposable database.
    for listener in app_engine.sync_engine.pool.dispatch.checkout:
        event.listen(engine.sync_engine, "checkout", listener)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest.mark.parametrize("action", ["touch", "logout"])
@pytest.mark.parametrize("locked", [False, True])
def test_auth_short_timeout_does_not_escape_to_other_writers(
    tmp_path, monkeypatch, action, locked
):
    async def run():
        engine, sessions = await _database(tmp_path / "auth.db")
        monkeypatch.setattr(auth, "async_db", sessions)
        try:
            async with sessions() as db:
                user = User(username="timeout-test", password_hash="unused")
                db.add(user)
                await db.flush()
                login = UserSession(
                    user_id=user.id,
                    session_id="timeout-session",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                db.add(login)
                await db.commit()

            # Commit/rollback returns the first connection behind the second.
            # A restoration issued through the Session can then change the wrong one.
            first = await engine.connect()
            second = await engine.connect()
            third = await engine.connect()
            await first.close()
            await second.close()
            await third.close()

            writer = None
            if locked:
                writer = await engine.connect()
                await writer.execute(update(User).values(username="busy"))
            try:
                if action == "touch":
                    await auth._touch_user_session_last_seen(
                        login.id, datetime.now(UTC)
                    )
                else:
                    token = auth.create_access_token(
                        {"sub": str(user.id)},
                        token_version=0,
                        session_id=login.session_id,
                    )
                    request = Request(
                        {
                            "type": "http",
                            "headers": [
                                (b"cookie", f"{auth.COOKIE_NAME}={token}".encode())
                            ],
                        }
                    )
                    response = Response()
                    async with sessions() as db:
                        await logout(request, response, db)
                    assert "Max-Age=0" in response.headers["set-cookie"]
            finally:
                if writer is not None:
                    await writer.rollback()
                    await writer.close()

            # Hold all pool connections simultaneously; repeatedly checking out one
            # would miss a leaked setting on another physical connection.
            connections = [await engine.connect() for _ in range(5)]
            try:
                timeouts = [
                    (await conn.execute(text("PRAGMA busy_timeout"))).scalar_one()
                    for conn in connections
                ]
                assert timeouts == [30000] * 5
            finally:
                for connection in connections:
                    await connection.close()
            async with sessions() as db:
                persisted = await db.get(UserSession, login.id)
                assert persisted is not None
                if action == "touch":
                    assert (persisted.last_seen_at is not None) is not locked
                else:
                    assert (persisted.revoked_at is not None) is not locked
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_concurrent_enqueues_deduplicate_atomically(tmp_path, monkeypatch):
    async def run():
        engine, sessions = await _database(tmp_path / "queue.db")
        monkeypatch.setattr(queue, "async_db", sessions)
        try:
            # Hold the writer while requests arrive. A non-atomic SELECT/INSERT
            # lets every request observe no active job before any can insert it.
            async with engine.connect() as writer:
                await writer.execute(text("BEGIN IMMEDIATE"))
                pending = [
                    asyncio.create_task(
                        queue.enqueue_background_job(
                            BackgroundJobType.TASK_RUN,
                            {"task": "sync_media"},
                            dedupe_key="task-run-sync_media",
                            skip_if_active=True,
                        )
                    )
                    for _ in range(12)
                ]
                await asyncio.sleep(0.5)
                await writer.rollback()
            results = await asyncio.gather(*pending)
            assert sum(job is not None for job in results) == 1
            async with sessions() as db:
                assert len((await db.scalars(select(BackgroundJob))).all()) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_queue_waits_for_writer_and_keeps_distinct_tasks(tmp_path, monkeypatch):
    async def run():
        engine, sessions = await _database(tmp_path / "busy-queue.db")
        monkeypatch.setattr(queue, "async_db", sessions)
        started = asyncio.Event()

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def writing(_conn, _cursor, statement, _params, _context, _many):
            if statement.startswith(("INSERT INTO background_jobs", "BEGIN IMMEDIATE")):
                started.set()

        pending = []
        try:
            async with engine.connect() as writer:
                await writer.execute(text("BEGIN IMMEDIATE"))
                started.clear()
                pending = [
                    asyncio.create_task(
                        queue.enqueue_background_job(
                            BackgroundJobType.TASK_RUN,
                            {"task": task},
                            dedupe_key=f"task-run-{task}",
                            skip_if_active=True,
                        )
                    )
                    for task in (
                        "sync_media",
                        "sync_media_libraries",
                        "sync_linked_data",
                    )
                ]
                await asyncio.wait_for(started.wait(), timeout=5)
                # Deliberately exceed the leaked authentication timeout.
                await asyncio.sleep(0.4)
                assert not any(task.done() for task in pending)
                await writer.rollback()
            results = await asyncio.wait_for(asyncio.gather(*pending), timeout=5)
            assert all(job is not None for job in results)
            async with sessions() as db:
                assert len((await db.scalars(select(BackgroundJob))).all()) == 3
        finally:
            await asyncio.gather(*pending, return_exceptions=True)
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("media", ["movie", "series"])
@pytest.mark.parametrize("existing", [False, True])
def test_sync_releases_writer_before_metadata_http(
    tmp_path, monkeypatch, media, existing
):
    async def run():
        path = tmp_path / "sync.db"
        engine, sessions = await _database(path)
        monkeypatch.setattr(sync, "async_db", sessions)
        monkeypatch.setattr(
            sync,
            "service_manager",
            SimpleNamespace(
                radarr_clients=lambda: {},
                radarr=None,
                sonarr_clients=lambda: {},
                sonarr=None,
            ),
        )
        monkeypatch.setattr(
            sync,
            "AsyncTMDBClient",
            lambda: SimpleNamespace(session=SimpleNamespace(close=AsyncMock())),
        )
        probes = []

        async def metadata(_row, _tmdb_id, _client):
            with closing(sqlite3.connect(path, timeout=0.1)) as other:
                try:
                    other.execute("UPDATE service_configs SET name = name")
                    other.commit()
                    probes.append(None)
                except sqlite3.OperationalError as exc:
                    probes.append(str(exc))

        monkeypatch.setattr(sync, f"_update_{media}_tmdb_metadata", metadata)
        monkeypatch.setattr(sync, "_needs_metadata_refresh", lambda *_: True)
        model = Movie if media == "movie" else Series
        try:
            async with sessions() as db:
                db.add(
                    ServiceConfig(
                        service_type=Service.PLEX,
                        base_url="http://plex",
                        api_key="test",
                        enabled=True,
                        is_main=True,
                    )
                )
                if existing:
                    db.add_all([model(title=f"Item {i}", tmdb_id=i) for i in (1, 2)])
                await db.commit()
            incoming = {}
            for i in (1, 2):
                ids = ExternalIDs(tmdb=i, imdb=None, tvdb=None, tmdb_collection=None)
                if media == "movie":
                    incoming[i] = AggregatedMovieData(
                        name=f"Item {i}",
                        year=None,
                        external_ids=ids,
                        versions=[
                            MovieVersionData(
                                service=Service.PLEX,
                                service_item_id=str(i),
                                service_media_id=str(i),
                                library_id="1",
                                library_name="Movies",
                                path=f"/movies/{i}.mkv",
                                size=1,
                                added_at=None,
                            )
                        ],
                        view_count=0,
                        last_viewed_at=None,
                    )
                else:
                    incoming[i] = AggregatedSeriesData(
                        id=str(i),
                        name=f"Item {i}",
                        year=None,
                        service=Service.PLEX,
                        library_id="1",
                        library_name="TV",
                        path=f"/series/{i}",
                        added_at=None,
                        external_ids=ids,
                        size=1,
                        view_count=0,
                        last_viewed_at=None,
                    )
            if media == "movie":
                monkeypatch.setattr(
                    sync, "gather_movies", AsyncMock(return_value=incoming)
                )
                result = await sync.sync_movies(allow_soft_delete=False)
            else:
                monkeypatch.setattr(
                    sync, "gather_series", AsyncMock(return_value=(incoming, {}))
                )
                result = await sync.sync_series(allow_soft_delete=False)
            assert result == {1, 2}
            assert probes == [None, None]
            async with sessions() as db:
                assert len((await db.scalars(select(model))).all()) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
