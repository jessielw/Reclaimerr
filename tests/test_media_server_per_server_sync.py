"""Per-media-server sync: each server syncs, and reports, on its own.

A single global `sync_media` run covered every server, so the dashboard showed
one shared timestamp against all of them and there was no way to refresh just
one. Main still owns the full sync (it owns library/version rows); a linked
server gets a linked-data run scoped to its own ServiceConfig.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings import services
from backend.api.routes.settings.services import sync_media_server
from backend.core import task_runtime
from backend.core.task_runtime import _run_linked_data_sync, _task_dedupe_key
from backend.database import Base
from backend.database.models import ServiceConfig, User
from backend.enums import Service, Task, UserRole
from backend.tasks.sync import _mark_service_config_synced


def _admin_user() -> User:
    return User(
        username="admin", password_hash="x", role=UserRole.ADMIN, permissions=[]
    )


async def _seeded_session() -> tuple[async_sessionmaker[AsyncSession], object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_maker() as db:
        db.add_all(
            [
                ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://plex-house",
                    api_key="k1",
                    name="Plex House",
                    enabled=True,
                    is_main=True,
                ),
                ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://plex-cabin",
                    api_key="k2",
                    name="Plex Cabin",
                    enabled=True,
                    is_main=False,
                ),
                ServiceConfig(
                    service_type=Service.JELLYFIN,
                    base_url="http://jellyfin",
                    api_key="k3",
                    name="Jellyfin Off",
                    enabled=False,
                    is_main=False,
                ),
            ]
        )
        await db.commit()
    return session_maker, engine


async def _config_id(session_maker, name: str) -> int:
    async with session_maker() as db:
        config = (
            await db.execute(select(ServiceConfig).where(ServiceConfig.name == name))
        ).scalar_one()
        return config.id


def test_dedupe_key_is_per_config() -> None:
    assert _task_dedupe_key(Task.SYNC_MEDIA, None) == "task-run-sync_media"
    assert _task_dedupe_key(Task.SYNC_LINKED_DATA, 2) != _task_dedupe_key(
        Task.SYNC_LINKED_DATA, 3
    )
    # an unscoped run and a scoped one must not collide either
    assert _task_dedupe_key(Task.SYNC_LINKED_DATA, None) != _task_dedupe_key(
        Task.SYNC_LINKED_DATA, 2
    )


def test_main_server_sync_runs_the_full_media_sync(monkeypatch) -> None:
    async def run() -> None:
        session_maker, engine = await _seeded_session()
        requested = AsyncMock(return_value=(type("Job", (), {"id": 7})(), True))
        monkeypatch.setattr(services, "request_task_run", requested)

        main_id = await _config_id(session_maker, "Plex House")
        async with session_maker() as db:
            response = await sync_media_server(main_id, _admin_user(), db)

        assert response["task"] == Task.SYNC_MEDIA.value
        assert response["scope"] == "main"
        assert response["job_id"] == 7
        requested.assert_awaited_once_with(Task.SYNC_MEDIA, service_config_id=None)
        await engine.dispose()

    asyncio.run(run())


def test_linked_server_sync_is_scoped_to_that_config(monkeypatch) -> None:
    async def run() -> None:
        session_maker, engine = await _seeded_session()
        requested = AsyncMock(return_value=(type("Job", (), {"id": 9})(), True))
        monkeypatch.setattr(services, "request_task_run", requested)

        linked_id = await _config_id(session_maker, "Plex Cabin")
        async with session_maker() as db:
            response = await sync_media_server(linked_id, _admin_user(), db)

        assert response["task"] == Task.SYNC_LINKED_DATA.value
        assert response["scope"] == "linked"
        requested.assert_awaited_once_with(
            Task.SYNC_LINKED_DATA, service_config_id=linked_id
        )
        await engine.dispose()

    asyncio.run(run())


def test_sync_rejects_unknown_and_disabled_servers(monkeypatch) -> None:
    async def run() -> None:
        session_maker, engine = await _seeded_session()
        monkeypatch.setattr(services, "request_task_run", AsyncMock())

        disabled_id = await _config_id(session_maker, "Jellyfin Off")
        async with session_maker() as db:
            with pytest.raises(HTTPException) as unknown:
                await sync_media_server(9999, _admin_user(), db)
            assert unknown.value.status_code == 404

            with pytest.raises(HTTPException) as off:
                await sync_media_server(disabled_id, _admin_user(), db)
            assert off.value.status_code == 409
        await engine.dispose()

    asyncio.run(run())


def test_scoped_linked_sync_touches_only_the_named_server(monkeypatch) -> None:
    async def run() -> None:
        session_maker, engine = await _seeded_session()
        monkeypatch.setattr(task_runtime, "async_db", session_maker)
        synced: list[int] = []
        monkeypatch.setattr(
            task_runtime,
            "sync_linked_data",
            AsyncMock(side_effect=lambda config: synced.append(config.id)),
        )

        linked_id = await _config_id(session_maker, "Plex Cabin")

        await _run_linked_data_sync(linked_id)
        assert synced == [linked_id]

        # unscoped keeps its old behavior: every enabled non-main server
        synced.clear()
        await _run_linked_data_sync()
        assert synced == [linked_id]
        await engine.dispose()

    asyncio.run(run())


def test_scoped_linked_sync_refuses_the_main_server(monkeypatch) -> None:
    async def run() -> None:
        session_maker, engine = await _seeded_session()
        monkeypatch.setattr(task_runtime, "async_db", session_maker)
        monkeypatch.setattr(task_runtime, "sync_linked_data", AsyncMock())

        main_id = await _config_id(session_maker, "Plex House")

        with pytest.raises(RuntimeError, match="main media server"):
            await _run_linked_data_sync(main_id)
        await engine.dispose()

    asyncio.run(run())


def test_sync_stamp_is_written_per_config(monkeypatch) -> None:
    async def run() -> None:
        session_maker, engine = await _seeded_session()
        monkeypatch.setattr("backend.tasks.sync.async_db", session_maker)

        linked_id = await _config_id(session_maker, "Plex Cabin")
        before = datetime.now(UTC).replace(tzinfo=None)
        await _mark_service_config_synced(linked_id)

        async with session_maker() as db:
            rows = {
                row.name: row.last_synced_at
                for row in (await db.execute(select(ServiceConfig))).scalars().all()
            }
        assert rows["Plex Cabin"] is not None
        assert rows["Plex Cabin"] >= before
        # the other servers keep their own (unset) time
        assert rows["Plex House"] is None
        assert rows["Jellyfin Off"] is None
        await engine.dispose()

    asyncio.run(run())
