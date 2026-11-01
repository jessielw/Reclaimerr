from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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
    User,
    UserSession,
)
from backend.enums import BackgroundJobType
from backend.jobs import queue


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
