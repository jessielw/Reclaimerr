from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.database import Base
from backend.database.models import ServiceConfig
from backend.enums import Service
from backend.tasks import sync as sync_module


class _RecordingGather:
    """Captures the ServiceConfig gather_series was asked for."""

    def __init__(self) -> None:
        self.called_with: list[Any] = []

    async def __call__(self, config: Any = None) -> None:
        self.called_with.append(config)
        return None


def _async_db_override(session_maker: async_sessionmaker[AsyncSession]):
    @asynccontextmanager
    async def _override():
        async with session_maker() as session:
            yield session

    return _override


async def _in_memory_session_maker() -> tuple[
    async_sessionmaker[AsyncSession], AsyncEngine
]:
    """Returns the session maker and the engine backing it.

    The caller must dispose the engine, otherwise the aiosqlite worker thread
    outlives the event loop and pytest reports an unhandled thread exception in
    the warnings summary.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return session_maker, engine


async def _seed_config(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    service_type: Service,
    name: str,
    is_main: bool,
) -> int:
    """Insert a ServiceConfig row and return its id."""
    async with session_maker() as session:
        config = ServiceConfig(
            service_type=service_type,
            name=name,
            base_url="http://example",
            api_key="key",
            enabled=True,
            is_main=is_main,
        )
        session.add(config)
        await session.commit()
        await session.refresh(config)
        return config.id


@pytest.mark.anyio
async def test_linked_server_does_not_sync_series(monkeypatch) -> None:
    """A non-main server must not contribute series rows.

    sync_movies already returns early for a linked server. sync_series did not,
    so a resync gathered series from every configured server and the table
    became the union of all of them.
    """
    session_maker, engine = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    await _seed_config(
        session_maker, service_type=Service.JELLYFIN, name="Jellyfin Main", is_main=True
    )
    linked_id = await _seed_config(
        session_maker, service_type=Service.PLEX, name="Plex Linked", is_main=False
    )

    result = await sync_module.sync_series(linked_id)
    await engine.dispose()

    assert result == set()
    assert gather.called_with == []


@pytest.mark.anyio
async def test_same_type_non_main_server_is_still_treated_as_linked(monkeypatch) -> None:
    """A non-main config of the SAME type as main must still be treated as linked.

    Regression test: comparing by service_type (rather than config identity)
    would let a second config of main's own type slip through the "is this
    linked?" check and be (incorrectly) synced as if it were main.
    """
    session_maker, engine = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    await _seed_config(
        session_maker, service_type=Service.PLEX, name="Plex Main", is_main=True
    )
    linked_id = await _seed_config(
        session_maker, service_type=Service.PLEX, name="Plex Linked", is_main=False
    )

    result = await sync_module.sync_series(linked_id)
    await engine.dispose()

    assert result == set()
    assert gather.called_with == []


@pytest.mark.anyio
async def test_the_main_server_passed_explicitly_syncs_normally(monkeypatch) -> None:
    """sync_media passes the main server's config id explicitly, so this is the normal path.

    Every other test here asserts that gather is not reached, which a guard of
    `if config_id is not None: return set()` would satisfy while disabling series
    syncing across the whole application. This is the test that catches it.
    """
    session_maker, engine = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    main_id = await _seed_config(
        session_maker, service_type=Service.JELLYFIN, name="Jellyfin Main", is_main=True
    )

    await sync_module.sync_series(main_id)
    await engine.dispose()

    assert len(gather.called_with) == 1
    assert gather.called_with[0].id == main_id
    assert gather.called_with[0].service_type is Service.JELLYFIN


@pytest.mark.anyio
async def test_no_service_resolves_to_the_main_server(monkeypatch) -> None:
    """resync_media calls sync_series() with no argument.

    That must mean the main server, matching sync_movies, not every server.
    """
    session_maker, engine = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    main_id = await _seed_config(
        session_maker, service_type=Service.JELLYFIN, name="Jellyfin Main", is_main=True
    )

    await sync_module.sync_series()
    await engine.dispose()

    assert len(gather.called_with) == 1
    assert gather.called_with[0].id == main_id


@pytest.mark.anyio
async def test_no_main_server_configured_returns_empty(monkeypatch) -> None:
    session_maker, engine = await _in_memory_session_maker()
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    gather = _RecordingGather()
    monkeypatch.setattr(sync_module, "gather_series", gather)

    result = await sync_module.sync_series()
    await engine.dispose()

    assert result == set()
    assert gather.called_with == []
