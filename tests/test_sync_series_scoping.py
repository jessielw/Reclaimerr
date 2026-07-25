from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.enums import Service
from backend.tasks import sync as sync_module


@dataclass
class _FakeServerConfig:
    service_type: Service


class _RecordingGather:
    """Captures the service gather_series was asked for."""

    def __init__(self) -> None:
        self.called_with: list[Any] = []

    async def __call__(self, service: Any = None) -> None:
        self.called_with.append(service)
        return None


def _async_db_override(session_maker: async_sessionmaker[AsyncSession]):
    @asynccontextmanager
    async def _override():
        async with session_maker() as session:
            yield session

    return _override


async def _in_memory_session_maker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.mark.anyio
async def test_linked_server_does_not_sync_series(monkeypatch) -> None:
    """A non-main server must not contribute series rows.

    sync_movies already returns early for a linked server. sync_series did not,
    so a resync gathered series from every configured server and the table
    became the union of all of them.
    """
    session_maker = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    async def _fake_main(_session: Any) -> _FakeServerConfig:
        return _FakeServerConfig(service_type=Service.JELLYFIN)

    monkeypatch.setattr(sync_module, "_get_main_media_server", _fake_main)

    result = await sync_module.sync_series(Service.PLEX)

    assert result == set()
    assert gather.called_with == []


@pytest.mark.anyio
async def test_no_service_resolves_to_the_main_server(monkeypatch) -> None:
    """resync_media calls sync_series() with no argument.

    That must mean the main server, matching sync_movies, not every server.
    """
    session_maker = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    async def _fake_main(_session: Any) -> _FakeServerConfig:
        return _FakeServerConfig(service_type=Service.JELLYFIN)

    monkeypatch.setattr(sync_module, "_get_main_media_server", _fake_main)

    await sync_module.sync_series()

    assert gather.called_with == [Service.JELLYFIN]


@pytest.mark.anyio
async def test_no_main_server_configured_returns_empty(monkeypatch) -> None:
    session_maker = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    async def _fake_main(_session: Any) -> None:
        return None

    monkeypatch.setattr(sync_module, "_get_main_media_server", _fake_main)

    result = await sync_module.sync_series()

    assert result == set()
    assert gather.called_with == []
