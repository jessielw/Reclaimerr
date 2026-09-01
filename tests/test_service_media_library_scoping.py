"""Library rows carry the media server that reported them.

Only the main server contributes libraries, but nothing recorded which one that
was, so a bare `library_id` was the whole identity. Jellyfin and Emby derive a
library's id from its path, so two servers each holding a library at the same
path report the same id: promoting one over the other updated the surviving row
in place and every rule scoped to it silently began matching a different
server's library, with the stale-library notice staying quiet because the id
still existed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.database import Base
from backend.database.models import (
    AdminNotice,
    ReclaimRule,
    ServiceConfig,
    ServiceMediaLibrary,
)
from backend.enums import MediaType, Service
from backend.services.admin_notices import (
    NOTICE_KEY_STALE_LIBRARY_IDS,
    reconcile_stale_library_notice,
)
from backend.services.media_origins import load_library_origins
from backend.tasks import sync as sync_module


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _async_db_override(session_maker: async_sessionmaker[AsyncSession]):
    @asynccontextmanager
    async def _override():
        async with session_maker() as session:
            yield session

    return _override


async def _in_memory_session_maker() -> tuple[
    async_sessionmaker[AsyncSession], AsyncEngine
]:
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
    name: str,
    is_main: bool,
    service_type: Service = Service.JELLYFIN,
) -> int:
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


class _FakeServer:
    """A media server reporting a fixed set of libraries."""

    def __init__(self, movies: list[dict[str, str]]) -> None:
        self._movies = movies

    async def get_movie_libraries(self) -> list[dict[str, str]]:
        return self._movies

    async def get_series_libraries(self) -> list[dict[str, str]]:
        return []


def _install_sync_stubs(
    monkeypatch,
    session_maker: async_sessionmaker[AsyncSession],
    server: _FakeServer,
) -> None:
    monkeypatch.setattr(
        "backend.tasks.sync.async_db", _async_db_override(session_maker)
    )

    class _ManagerStub:
        main_media_server = server

    monkeypatch.setattr(sync_module, "service_manager", _ManagerStub())

    async def _instance(config: Any) -> Any:
        return server

    monkeypatch.setattr(sync_module, "_get_media_service_instance", _instance)

    @asynccontextmanager
    async def _no_tracking(_task: Any):
        yield None

    monkeypatch.setattr(sync_module, "track_task_execution", _no_tracking)


async def _libraries(
    session_maker: async_sessionmaker[AsyncSession],
) -> list[tuple[Any, ...]]:
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(
                    ServiceMediaLibrary.library_id,
                    ServiceMediaLibrary.library_name,
                    ServiceMediaLibrary.service_config_id,
                ).order_by(ServiceMediaLibrary.library_id)
            )
        ).all()
    return [tuple(row) for row in rows]


@pytest.mark.anyio
async def test_sync_stamps_libraries_with_the_main_server(monkeypatch) -> None:
    session_maker, engine = await _in_memory_session_maker()
    try:
        main_id = await _seed_config(session_maker, name="Jellyfin Main", is_main=True)
        _install_sync_stubs(
            monkeypatch,
            session_maker,
            _FakeServer([{"id": "lib-1", "name": "Movies"}]),
        )

        await sync_module.sync_media_libraries()

        assert await _libraries(session_maker) == [("lib-1", "Movies", main_id)]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_a_demoted_servers_library_is_replaced_not_retargeted(
    monkeypatch,
) -> None:
    """The regression this scoping exists for.

    Both servers call their library `lib-1` - what Jellyfin does when the two
    libraries sit at the same path. Keyed on the id alone, the old row was
    renamed in place and kept its primary key, so a rule scoped to `lib-1`
    quietly followed the main server across to a different library.
    """
    session_maker, engine = await _in_memory_session_maker()
    try:
        old_main_id = await _seed_config(
            session_maker, name="Jellyfin Basement", is_main=False
        )
        new_main_id = await _seed_config(
            session_maker, name="Jellyfin Living Room", is_main=True
        )
        async with session_maker() as session:
            session.add(
                ServiceMediaLibrary(
                    library_id="lib-1",
                    library_name="Basement Movies",
                    media_type=MediaType.MOVIE,
                    service_config_id=old_main_id,
                )
            )
            await session.commit()

        _install_sync_stubs(
            monkeypatch,
            session_maker,
            _FakeServer([{"id": "lib-1", "name": "Living Room Movies"}]),
        )

        await sync_module.sync_media_libraries()

        assert await _libraries(session_maker) == [
            ("lib-1", "Living Room Movies", new_main_id)
        ]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_rows_predating_the_column_are_adopted_by_main(monkeypatch) -> None:
    session_maker, engine = await _in_memory_session_maker()
    try:
        main_id = await _seed_config(session_maker, name="Jellyfin Main", is_main=True)
        async with session_maker() as session:
            session.add(
                ServiceMediaLibrary(
                    library_id="lib-1",
                    library_name="Movies",
                    media_type=MediaType.MOVIE,
                    selected=True,
                )
            )
            await session.commit()

        _install_sync_stubs(
            monkeypatch,
            session_maker,
            _FakeServer([{"id": "lib-1", "name": "Movies"}]),
        )

        await sync_module.sync_media_libraries()

        assert await _libraries(session_maker) == [("lib-1", "Movies", main_id)]
        async with session_maker() as session:
            selected = await session.scalar(select(ServiceMediaLibrary.selected))
        assert selected is True, "adopting a row must not discard its selection"
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_stale_notice_fires_for_a_demoted_servers_library() -> None:
    """A rule scoped to the old main server's library is stale, not valid.

    Scoped only by id, the leftover row kept the rule looking healthy while it
    matched nothing.
    """
    session_maker, engine = await _in_memory_session_maker()
    try:
        old_main_id = await _seed_config(
            session_maker, name="Jellyfin Basement", is_main=False
        )
        await _seed_config(session_maker, name="Jellyfin Living Room", is_main=True)
        async with session_maker() as session:
            session.add(
                ServiceMediaLibrary(
                    library_id="lib-basement",
                    library_name="Movies",
                    media_type=MediaType.MOVIE,
                    service_config_id=old_main_id,
                )
            )
            session.add(
                ReclaimRule(
                    name="Old scope",
                    media_type=MediaType.MOVIE,
                    definition={
                        "version": 1,
                        "root": {
                            "type": "condition",
                            "field": "library.id",
                            "operator": "contains_any",
                            "value": ["lib-basement"],
                        },
                    },
                    action={"outcome": "candidate"},
                    enabled=True,
                )
            )
            await session.commit()

        async with session_maker() as session:
            stale = await reconcile_stale_library_notice(session)
            await session.commit()

        assert stale == ["Old scope"]

        async with session_maker() as session:
            notice = await session.scalar(
                select(AdminNotice).where(
                    AdminNotice.dedupe_key == NOTICE_KEY_STALE_LIBRARY_IDS
                )
            )
        assert notice is not None
        assert "Jellyfin Living Room" in notice.message
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_library_origins_name_the_server_only_when_ambiguous() -> None:
    """One media server needs no disambiguation; a second one does.

    `service_name` is the "show this" signal for a dozen display sites, so the
    decision is made once here rather than re-derived at each of them.
    """
    session_maker, engine = await _in_memory_session_maker()
    try:
        main_id = await _seed_config(
            session_maker, name="Plex Living Room", is_main=True
        )
        async with session_maker() as session:
            session.add(
                ServiceMediaLibrary(
                    library_id="lib-1",
                    library_name="Movies",
                    media_type=MediaType.MOVIE,
                    service_config_id=main_id,
                )
            )
            await session.commit()

        async with session_maker() as session:
            single = await load_library_origins(session)
        assert single.qualify is False
        assert single.name_for("lib-1") is None
        assert single.config_id_for("lib-1") == main_id
        assert single.label("lib-1", "fallback") == "Movies"

        await _seed_config(session_maker, name="Plex Basement", is_main=False)
        async with session_maker() as session:
            several = await load_library_origins(session)
        assert several.qualify is True
        assert several.name_for("lib-1") == "Plex Living Room"
        assert several.label("lib-1", "fallback") == "Movies (Plex Living Room)"
    finally:
        await engine.dispose()
