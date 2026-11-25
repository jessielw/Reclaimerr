"""Tests for media server library selection in Settings and Sync Media."""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.api.routes.settings import services as settings_services
from backend.api.routes.settings.services import (
    get_service_settings,
    update_library_selections,
    update_service_libraries,
)
from backend.core.encryption import fer_encrypt
from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import (
    Episode,
    EpisodeVersion,
    Season,
    Series,
    ServiceConfig,
    ServiceMediaLibrary,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.media import (
    AggregatedEpisodeData,
    AggregatedSeasonData,
    EpisodeVersionData,
)
from backend.models.settings import LibrarySelectionUpdate, UpdateMediaLibrariesRequest
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.tasks import sync as sync_module

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "alembic"
    / "versions"
    / "e7a2b5c8d104_default_media_libraries_selected.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("_default_selected_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MigrationOp:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, statement: Any) -> Any:
        return self._conn.execute(str(statement))


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


def _async_db_override(session_maker: async_sessionmaker[AsyncSession]):
    @asynccontextmanager
    async def _override():
        async with session_maker() as session:
            yield session

    return _override


async def _make_db(*, foreign_keys: bool = False) -> tuple[
    async_sessionmaker[AsyncSession], AsyncEngine
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    if foreign_keys:
        @event.listens_for(engine.sync_engine, "connect")
        def _set_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine,
        autoflush=False,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    return session_maker, engine


def test_migration_selects_legacy_unselected_libraries_and_preserves_custom_selection():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE service_media_libraries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id VARCHAR(50) NOT NULL,
            library_name VARCHAR(255) NOT NULL,
            media_type VARCHAR(6) NOT NULL,
            selected BOOLEAN NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO service_media_libraries (library_id, library_name, media_type, selected) "
        "VALUES (?, ?, ?, ?)",
        [
            ("lib-movies", "Movies", "MOVIE", 0),
            ("lib-discover", "Discover Movies", "MOVIE", 0),
        ],
    )

    module = _load_migration()
    module.op = _MigrationOp(conn)
    module.upgrade()
    assert list(
        conn.execute(
            "SELECT library_id, selected FROM service_media_libraries ORDER BY library_id"
        )
    ) == [("lib-discover", 1), ("lib-movies", 1)]

    # If a user already selected a subset, running the migration again must keep it.
    conn.execute(
        "UPDATE service_media_libraries SET selected = 0 WHERE library_id = 'lib-discover'"
    )
    module.upgrade()
    assert list(
        conn.execute(
            "SELECT library_id, selected FROM service_media_libraries ORDER BY library_id"
        )
    ) == [("lib-discover", 0), ("lib-movies", 1)]


def test_sync_media_libraries_defaults_new_rows_to_selected_and_preserves_unselected(
    monkeypatch,
):
    async def run() -> None:
        session_maker, engine = await _make_db()
        try:
            async with session_maker() as db:
                main = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    name="Jellyfin",
                    base_url="http://jellyfin",
                    api_key="key",
                    enabled=True,
                    is_main=True,
                )
                db.add(main)
                await db.commit()
                await db.refresh(main)

            class _FakeServer:
                def __init__(self) -> None:
                    self.movies = [
                        {"id": "movies-1", "name": "Movies"},
                        {"id": "discover-1", "name": "Discover Movies"},
                    ]
                    self.series = [{"id": "shows-1", "name": "TV Shows"}]

                async def get_movie_libraries(self) -> list[dict[str, str]]:
                    return self.movies

                async def get_series_libraries(self) -> list[dict[str, str]]:
                    return self.series

            server = _FakeServer()
            monkeypatch.setattr(
                sync_module, "async_db", _async_db_override(session_maker)
            )
            monkeypatch.setattr(
                sync_module, "_get_media_service_instance", AsyncMock(return_value=server)
            )
            monkeypatch.setattr(
                sync_module,
                "service_manager",
                type("Mgr", (), {"main_media_server": server})(),
            )

            @asynccontextmanager
            async def _no_track(_task: Any):
                yield None

            monkeypatch.setattr(sync_module, "track_task_execution", _no_track)

            await sync_module.sync_media_libraries()

            async with session_maker() as db:
                rows = (
                    await db.scalars(
                        select(ServiceMediaLibrary).order_by(
                            ServiceMediaLibrary.library_id
                        )
                    )
                ).all()
                assert [(r.library_id, r.selected) for r in rows] == [
                    ("discover-1", True),
                    ("movies-1", True),
                    ("shows-1", True),
                ]
                for r in rows:
                    if r.library_id == "discover-1":
                        r.selected = False
                await db.commit()

            # Add another library and re-sync; discover-1 must stay unselected.
            server.series.append({"id": "anime-1", "name": "Anime"})
            await sync_module.sync_media_libraries()

            async with session_maker() as db:
                rows = (
                    await db.scalars(
                        select(ServiceMediaLibrary).order_by(
                            ServiceMediaLibrary.library_id
                        )
                    )
                ).all()
                assert [(r.library_id, r.selected) for r in rows] == [
                    ("anime-1", True),
                    ("discover-1", False),
                    ("movies-1", True),
                    ("shows-1", True),
                ]
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
def test_gather_passes_only_selected_library_ids_and_skips_when_none_selected(
    monkeypatch, media_type: MediaType
):
    async def run() -> None:
        session_maker, engine = await _make_db()
        try:
            async with session_maker() as db:
                main = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    name="Jellyfin",
                    base_url="http://jellyfin",
                    api_key="key",
                    enabled=True,
                    is_main=True,
                )
                db.add(main)
                await db.flush()
                real_lib = ServiceMediaLibrary(
                    library_id="lib-real",
                    library_name="Real Library",
                    media_type=media_type,
                    service_config_id=main.id,
                    selected=True,
                )
                discover_lib = ServiceMediaLibrary(
                    library_id="lib-discover",
                    library_name="Discover",
                    media_type=media_type,
                    service_config_id=main.id,
                    selected=False,
                )
                db.add_all([real_lib, discover_lib])
                await db.commit()
                await db.refresh(main)

            client = AsyncMock()
            client.get_aggregated_movies.return_value = []
            client.get_aggregated_series.return_value = []
            client.is_inventory_empty.return_value = True

            monkeypatch.setattr(
                sync_module, "async_db", _async_db_override(session_maker)
            )
            monkeypatch.setattr(
                sync_module,
                "_get_media_service_instance",
                AsyncMock(return_value=client),
            )

            gather = (
                sync_module.gather_movies
                if media_type is MediaType.MOVIE
                else sync_module.gather_series
            )
            result = await gather(main)
            assert result == ({} if media_type is MediaType.MOVIE else ({}, {}))

            fetch_mock = (
                client.get_aggregated_movies
                if media_type is MediaType.MOVIE
                else client.get_aggregated_series
            )
            fetch_mock.assert_awaited_once_with(included_libraries=["lib-real"])
            client.is_inventory_empty.assert_awaited_once_with(
                media_type, included_libraries=["lib-real"]
            )

            # Deselecting every library of this media type skips the media server completely.
            async with session_maker() as db:
                for row in (await db.scalars(select(ServiceMediaLibrary))).all():
                    row.selected = False
                await db.commit()

            fetch_mock.reset_mock()
            client.is_inventory_empty.reset_mock()

            empty_result = await gather(main)
            assert empty_result == ({} if media_type is MediaType.MOVIE else ({}, {}))
            fetch_mock.assert_not_awaited()
            client.is_inventory_empty.assert_not_awaited()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("client_class", [EmbyService, JellyfinService])
def test_emby_family_filters_by_library_id_and_short_circuits_empty(
    monkeypatch, client_class
):
    async def run() -> None:
        client = client_class("key", "http://jellyfin")
        try:
            monkeypatch.setattr(
                client,
                "_get_collection_names_by_item_id",
                AsyncMock(return_value={}),
            )
            monkeypatch.setattr(
                client,
                "get_movie_libraries",
                AsyncMock(
                    return_value=[
                        {"id": "movies-id", "name": "Movies"},
                        {"id": "discover-id", "name": "Discover Movies"},
                    ]
                ),
            )
            monkeypatch.setattr(
                client,
                "get_series_libraries",
                AsyncMock(
                    return_value=[
                        {"id": "shows-id", "name": "TV Shows"},
                        {"id": "discover-shows-id", "name": "Discover Shows"},
                    ]
                ),
            )
            monkeypatch.setattr(client, "get_users", AsyncMock(return_value=[]))
            series_sizes_mock = AsyncMock(return_value=({}, {}))
            monkeypatch.setattr(
                client, "get_series_sizes_for_library", series_sizes_mock
            )
            episode_user_data_mock = AsyncMock(return_value={})
            monkeypatch.setattr(
                client, "_get_aggregated_episode_user_data", episode_user_data_mock
            )
            monkeypatch.setattr(
                client, "get_series_for_user", AsyncMock(return_value=[])
            )

            # Empty list short-circuits without touching server endpoints.
            assert await client.get_aggregated_movies(included_libraries=[]) == []
            assert await client.get_aggregated_series(included_libraries=[]) == []
            client.get_movie_libraries.assert_not_awaited()
            client.get_series_libraries.assert_not_awaited()

            # Non-empty list queries only matching library IDs.
            await client.get_aggregated_series(included_libraries=["shows-id"])
            series_sizes_mock.assert_awaited_once()
            assert series_sizes_mock.call_args.args[0] == "shows-id"

            # Scoped is_inventory_empty queries ParentId for selected libraries only.
            request_mock = AsyncMock(
                return_value={"Items": [], "TotalRecordCount": 0}
            )
            monkeypatch.setattr(client, "_make_request", request_mock)
            assert (
                await client.is_inventory_empty(
                    MediaType.MOVIE, included_libraries=[]
                )
                is True
            )
            request_mock.assert_not_awaited()

            assert (
                await client.is_inventory_empty(
                    MediaType.MOVIE, included_libraries=["movies-id"]
                )
                is True
            )
            assert request_mock.call_args.kwargs["params"]["ParentId"] == "movies-id"
        finally:
            await client.session.close()

    asyncio.run(run())


def test_plex_filters_by_section_uuid_and_short_circuits_empty(monkeypatch):
    async def run() -> None:
        client = PlexService("token", "http://plex")
        try:
            sections = [
                {"type": "movie", "key": "1", "uuid": "uuid-movies", "title": "Movies"},
                {
                    "type": "movie",
                    "key": "2",
                    "uuid": "uuid-discover",
                    "title": "Discover Movies",
                },
            ]
            sections_mock = AsyncMock(return_value=sections)
            monkeypatch.setattr(PlexService, "get_library_sections", sections_mock)

            # Empty list returns immediately without querying sections or history.
            assert await client.get_aggregated_movies(included_libraries=[]) == []
            assert await client.get_aggregated_series(included_libraries=[]) == []
            sections_mock.assert_not_awaited()

            # get_movies with uuid-movies skips uuid-discover completely.
            collections_mock = AsyncMock(return_value={})
            items_mock = AsyncMock(return_value=[])
            monkeypatch.setattr(
                PlexService, "_get_collection_names_by_item_id", collections_mock
            )
            monkeypatch.setattr(
                PlexService, "_get_section_metadata_items", items_mock
            )
            assert await client.get_movies(included_libraries=["uuid-movies"]) == []
            collections_mock.assert_awaited_once_with("1")
            items_mock.assert_awaited_once()
            assert items_mock.call_args.kwargs["section_id"] == "1"

            # is_inventory_empty scoped to uuid-movies ignores uuid-discover even if non-empty.
            request_mock = AsyncMock(
                side_effect=[
                    ({"MediaContainer": {"size": 2, "Directory": sections}}, {}),
                    ({"MediaContainer": {"size": 0, "totalSize": 0}}, {}),
                ]
            )
            monkeypatch.setattr(PlexService, "_make_request", request_mock)
            assert (
                await client.is_inventory_empty(
                    MediaType.MOVIE, included_libraries=["uuid-movies"]
                )
                is True
            )
            assert request_mock.await_count == 2
            assert request_mock.call_args_list[1].args[0] == "library/sections/1/all"
        finally:
            await client.session.close()

    asyncio.run(run())


def test_settings_api_reads_updates_and_refreshes_libraries(monkeypatch):
    async def run() -> None:
        session_maker, engine = await _make_db()
        try:
            async with session_maker() as db:
                main = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    name="Main Jellyfin",
                    base_url="http://jellyfin",
                    api_key=fer_encrypt("secret-key"),
                    enabled=True,
                    is_main=True,
                )
                linked = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    name="Linked Jellyfin",
                    base_url="http://jellyfin-2",
                    api_key=fer_encrypt("secret-key-2"),
                    enabled=True,
                    is_main=False,
                )
                db.add_all([main, linked])
                await db.flush()
                lib_movies = ServiceMediaLibrary(
                    library_id="lib-1",
                    library_name="Movies",
                    media_type=MediaType.MOVIE,
                    service_config_id=main.id,
                    selected=True,
                )
                lib_discover = ServiceMediaLibrary(
                    library_id="lib-2",
                    library_name="Discover Movies",
                    media_type=MediaType.MOVIE,
                    service_config_id=main.id,
                    selected=True,
                )
                db.add_all([lib_movies, lib_discover])
                await db.commit()
                await db.refresh(lib_discover)

                # Update selection via PUT /api/settings/libraries
                await update_library_selections(
                    [LibrarySelectionUpdate(id=lib_discover.id, selected=False)],
                    _admin_user(),
                    db,
                )

                response = await get_service_settings(_admin_user(), db)
                instances = response[Service.JELLYFIN]["instances"]
                main_inst = next(i for i in instances if i["is_main"])
                linked_inst = next(i for i in instances if not i["is_main"])
                assert linked_inst["libraries"] is None
                assert [
                    (lib["library_name"], lib["selected"])
                    for lib in main_inst["libraries"]
                ] == [("Discover Movies", False), ("Movies", True)]

                # POST /api/settings/sync/libraries works even before service_manager
                # initializes the client, using a temporary client closed afterward.
                monkeypatch.setattr(
                    service_manager, "get_media_server", lambda *_a, **_kw: None
                )
                closed = {"called": False}

                class _TempClient:
                    def __init__(self) -> None:
                        self.session = self

                    async def close(self) -> None:
                        closed["called"] = True

                temp = _TempClient()
                monkeypatch.setattr(
                    settings_services, "_build_temp_media_client", lambda _cfg: temp
                )
                sync_mock = AsyncMock(
                    return_value={"libraries": [], "affected_rules": []}
                )
                monkeypatch.setattr(
                    settings_services, "sync_media_libraries", sync_mock
                )

                await update_service_libraries(
                    UpdateMediaLibrariesRequest(service_type=Service.JELLYFIN),
                    _admin_user(),
                    db,
                )
                sync_mock.assert_awaited_once_with(service_instance=temp)
                assert closed["called"] is True

                # Reject when no enabled main server exists.
                main.is_main = False
                await db.commit()
                with pytest.raises(HTTPException) as exc_info:
                    await update_service_libraries(
                        UpdateMediaLibrariesRequest(service_type=Service.JELLYFIN),
                        _admin_user(),
                        db,
                    )
                assert exc_info.value.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sync_seasons_flushes_episode_versions_without_sqlite_fk_error():
    """Regression test for FOREIGN KEY constraint failed on episode_versions.

    When two incoming series entries target the same Series row in one uncommitted
    session (e.g. a duplicate metadata match across libraries), flushing
    EpisodeVersion rows before raw Core deletes prevents SQLite FK failures.
    """

    async def run() -> None:
        session_maker, engine = await _make_db(foreign_keys=True)
        try:
            async with session_maker() as session:
                series = Series(title="Serial Swindlers", tmdb_id=292670, size=207995)
                session.add(series)
                await session.commit()
                await session.refresh(series)

                first_seasons = [
                    AggregatedSeasonData(
                        service_series_id="series-discover",
                        season_number=1,
                        size=207995,
                        episode_count=1,
                        view_count=0,
                        last_viewed_at=None,
                        episode_data=[
                            AggregatedEpisodeData(
                                episode_number=9999,
                                name="Dummy",
                                view_count=0,
                                last_viewed_at=None,
                                size=207995,
                                path="/jellybridge/Shows/Serial Swindlers/Season 01/S01E9999.mp4",
                                jellyfin_episode_id="ep-dummy",
                                versions=[
                                    EpisodeVersionData(
                                        service=Service.JELLYFIN,
                                        service_item_id="ep-dummy",
                                        service_media_id="ep-dummy",
                                        library_id="discover-shows",
                                        library_name="Discover Shows",
                                        path="/jellybridge/Shows/Serial Swindlers/Season 01/S01E9999.mp4",
                                        size=207995,
                                    )
                                ],
                            )
                        ],
                    )
                ]
                second_seasons = [
                    AggregatedSeasonData(
                        service_series_id="series-real",
                        season_number=2,
                        size=500000,
                        episode_count=1,
                        view_count=0,
                        last_viewed_at=None,
                        episode_data=[
                            AggregatedEpisodeData(
                                episode_number=1,
                                name="Episode 1",
                                view_count=0,
                                last_viewed_at=None,
                                size=500000,
                                path="/tv/Serial Swindlers/Season 02/S02E01.mkv",
                                jellyfin_episode_id="ep-real",
                                versions=[
                                    EpisodeVersionData(
                                        service=Service.JELLYFIN,
                                        service_item_id="ep-real",
                                        service_media_id="ep-real",
                                        library_id="tv-shows",
                                        library_name="TV Shows",
                                        path="/tv/Serial Swindlers/Season 02/S02E01.mkv",
                                        size=500000,
                                    )
                                ],
                            )
                        ],
                    )
                ]

                await sync_module._sync_seasons(
                    session, series.id, first_seasons, Service.JELLYFIN
                )
                # Second call on the same series within the same uncommitted session
                # removes Season 1 and adds Season 2 — must not raise IntegrityError.
                await sync_module._sync_seasons(
                    session, series.id, second_seasons, Service.JELLYFIN
                )
                await session.commit()

                seasons = (
                    await session.scalars(
                        select(Season).where(Season.series_id == series.id)
                    )
                ).all()
                assert [s.season_number for s in seasons] == [2]
                episodes = (await session.scalars(select(Episode))).all()
                assert [e.episode_number for e in episodes] == [1]
                versions = (await session.scalars(select(EpisodeVersion))).all()
                assert [v.library_name for v in versions] == ["TV Shows"]
        finally:
            await engine.dispose()

    asyncio.run(run())
