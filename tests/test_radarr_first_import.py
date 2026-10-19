from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import Movie, MovieArrRef, ServiceConfig
from backend.enums import Service
from backend.models.media import AggregatedMovieData, ExternalIDs
from backend.services.radarr import RadarrClient
from backend.tasks import sync


def _event(movie_id, date, event_type="downloadFolderImported"):
    return {"movieId": movie_id, "date": date, "eventType": event_type}


def test_first_import_dates_page_through_server_cap_and_keep_earliest():
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        pages = [
            (
                200,
                {
                    "totalRecords": 5,
                    "pageSize": 2,
                    "records": [
                        _event(12, "2026-06-20T12:00:00Z"),
                        _event(12, "2026-06-10T08:00:00-04:00"),
                    ],
                },
            ),
            (
                200,
                {
                    "totalRecords": 5,
                    "pageSize": 2,
                    "records": [
                        _event(12, "2026-06-01T12:00:00Z", "grabbed"),
                        _event(13, "2026-05-02T08:00:00-04:00"),
                    ],
                },
            ),
            (
                200,
                {
                    "totalRecords": 5,
                    "pageSize": 2,
                    "records": [
                        _event(12, "2026-05-01T12:00:00Z", 3),
                    ],
                },
            ),
        ]
        request = AsyncMock(side_effect=pages)
        try:
            with patch.object(RadarrClient, "_make_request", request):
                dates = await client.get_first_import_dates()
            assert dates == {12: datetime(2026, 5, 1, 12), 13: datetime(2026, 5, 2, 12)}
            assert [
                call.kwargs["params"]["page"] for call in request.await_args_list
            ] == [1, 2, 3]
        finally:
            await client.session.close()

    asyncio.run(run())


def test_import_history_without_total_pages_until_empty():
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        request = AsyncMock(
            side_effect=[
                (200, {"records": [_event(12, "2026-06-20T12:00:00Z")]}),
                (200, {"records": [_event(12, "2026-05-01T12:00:00Z")]}),
                (200, {"records": []}),
            ]
        )
        try:
            with patch.object(RadarrClient, "_make_request", request):
                assert await client.get_first_import_dates() == {
                    12: datetime(2026, 5, 1, 12)
                }
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        {"totalRecords": 1},
        {"totalRecords": 1, "records": "invalid"},
        {"totalRecords": 1, "records": []},
    ],
)
def test_import_history_rejects_incomplete_pages(response):
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        try:
            with patch.object(
                RadarrClient, "_make_request", AsyncMock(return_value=(200, response))
            ):
                with pytest.raises(ValueError):
                    await client.get_import_history()
        finally:
            await client.session.close()

    asyncio.run(run())


def test_import_history_rejects_repeated_page():
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        request = AsyncMock(
            return_value=(
                200,
                {
                    "totalRecords": 3,
                    "records": [_event(12, "2026-06-20T12:00:00Z")],
                },
            )
        )
        try:
            with patch.object(RadarrClient, "_make_request", request):
                with pytest.raises(ValueError):
                    await client.get_import_history(page_size=1)
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("page_size", [0, -1])
def test_import_history_rejects_invalid_page_size(page_size):
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        try:
            with patch.object(
                RadarrClient,
                "_make_request",
                AsyncMock(return_value=(200, {"records": []})),
            ):
                with pytest.raises(ValueError):
                    await client.get_import_history(page_size=page_size)
        finally:
            await client.session.close()

    asyncio.run(run())


def test_first_import_dates_ignore_unknown_dates_and_other_event_types():
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        records = [
            _event(12, "invalid"),
            _event(13, None),
            _event(14, "2026-01-01T00:00:00Z", "downloadFailed"),
            _event(15, "2026-01-01T00:00:00Z", "movieFolderImported"),
            _event(0, "2026-01-01T00:00:00Z"),
            _event(-1, "2026-01-01T00:00:00Z"),
            _event("invalid", "2026-01-01T00:00:00Z"),
        ]
        try:
            with patch.object(
                RadarrClient,
                "_make_request",
                AsyncMock(
                    return_value=(
                        200,
                        {"totalRecords": len(records), "records": records},
                    )
                ),
            ):
                assert await client.get_first_import_dates() == {}
        finally:
            await client.session.close()

    asyncio.run(run())


def test_first_import_dates_discard_partial_fetch_on_failure():
    async def run():
        client = RadarrClient(api_key="key", base_url="http://radarr")
        request = AsyncMock(
            side_effect=[
                (
                    200,
                    {
                        "totalRecords": 2,
                        "records": [_event(12, "2026-06-20T12:00:00Z")],
                    },
                ),
                ConnectionError("history unavailable"),
            ]
        )
        try:
            with patch.object(RadarrClient, "_make_request", request):
                with pytest.raises(ConnectionError):
                    await client.get_first_import_dates()
        finally:
            await client.session.close()

    asyncio.run(run())


def test_sync_movies_preserves_first_import_without_history_write_lock(
    monkeypatch, tmp_path
):
    async def run():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'movies.db'}",
            connect_args={"timeout": 0.1},
        )
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        clients = {
            2: RadarrClient("key", "http://radarr-one"),
            3: RadarrClient("key", "http://radarr-two"),
        }
        async with sessions() as db:
            db.add_all(
                [
                    ServiceConfig(
                        service_type=Service.PLEX,
                        base_url="http://plex",
                        api_key="key",
                        is_main=True,
                        enabled=True,
                    ),
                    ServiceConfig(
                        service_type=Service.RADARR,
                        base_url="http://radarr-one",
                        api_key="key",
                        name="One",
                        enabled=True,
                    ),
                    ServiceConfig(
                        service_type=Service.RADARR,
                        base_url="http://radarr-two",
                        api_key="key",
                        name="Two",
                        enabled=True,
                    ),
                    Movie(title="Known", tmdb_id=101),
                    Movie(title="Unknown", tmdb_id=102),
                ]
            )
            await db.commit()
        movies = {
            tmdb_id: AggregatedMovieData(
                name=title,
                year=2020,
                external_ids=ExternalIDs(
                    tmdb=tmdb_id, imdb=None, tmdb_collection=None, tvdb=None
                ),
                versions=[],
                view_count=0,
                last_viewed_at=None,
            )
            for tmdb_id, title in [(101, "Known"), (102, "Unknown")]
        }
        monkeypatch.setattr(sync, "async_db", sessions)
        monkeypatch.setattr(sync, "gather_movies", AsyncMock(return_value=movies))
        monkeypatch.setattr(sync, "_needs_metadata_refresh", lambda *args: False)
        monkeypatch.setattr(sync.service_manager, "radarr_clients", lambda: clients)
        monkeypatch.setattr(
            sync.AsyncTMDBClient,
            "__init__",
            lambda self: setattr(self, "session", AsyncMock()),
        )
        phase = 0
        history_calls = []
        history_write_errors = []

        async def request(client, method, endpoint, **kwargs):
            if endpoint == "movie":
                return 200, [
                    {
                        "id": 12,
                        "tmdbId": 101,
                        "hasFile": True,
                        "movieFile": {"dateAdded": "2026-07-01T00:00:00Z"},
                    },
                    {
                        "id": 13,
                        "tmdbId": 102,
                        "hasFile": True,
                        "movieFile": {"dateAdded": "2026-07-01T00:00:00Z"},
                    },
                ]
            if endpoint == "tag":
                return 200, []
            assert endpoint == "history"
            # History pagination must leave SQLite available to other writers.
            # Record lock failures so sync's history-error fallback cannot hide them.
            try:
                async with engine.begin() as conn:
                    await conn.execute(text("UPDATE movies SET title=title WHERE id=1"))
            except OperationalError as exc:
                history_write_errors.append(str(exc))
            history_calls.append(client.base_url)
            if phase == 2 or (phase == 3 and client.base_url.endswith("two")):
                raise ConnectionError("history unavailable")
            date = {0: "2026-05-01", 1: "2026-06-01", 3: "2026-04-01", 4: "2026-04-01"}[
                phase
            ]
            if client.base_url.endswith("one"):
                date = "2026-06-15"
            return 200, {
                "totalRecords": 1,
                "records": [_event(12, f"{date}T00:00:00Z")],
            }

        try:
            with patch.object(RadarrClient, "_make_request", request):
                for phase, expected in [
                    (2, None),
                    (0, datetime(2026, 5, 1)),
                    (1, datetime(2026, 5, 1)),
                    (2, datetime(2026, 5, 1)),
                    (3, datetime(2026, 5, 1)),
                    (4, datetime(2026, 4, 1)),
                ]:
                    assert await sync.sync_movies(allow_soft_delete=False) == {101, 102}
                    async with sessions() as db:
                        rows = {
                            m.tmdb_id: m
                            for m in (await db.execute(select(Movie))).scalars()
                        }
                        assert rows[101].arr_first_import_at == expected
                        assert rows[102].arr_first_import_at is None
                        assert rows[102].arr_added_at == datetime(2026, 7, 1)
                        assert (
                            len((await db.execute(select(MovieArrRef))).scalars().all())
                            == 4
                        )
                assert len(history_calls) == 12
                assert not history_write_errors
        finally:
            for client in clients.values():
                await client.session.close()
            await engine.dispose()

    asyncio.run(run())


def test_first_import_migration_keeps_existing_movies_unknown():
    path = Path(
        "backend/alembic/versions/d8c4e2a1b903_add_movie_arr_first_import_at.py"
    )
    spec = importlib.util.spec_from_file_location("first_import_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE movies (id INTEGER PRIMARY KEY, title TEXT)"))
        conn.execute(text("INSERT INTO movies VALUES (1, 'Existing')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert (
                conn.execute(
                    text("SELECT arr_first_import_at FROM movies")
                ).scalar_one()
                is None
            )
            column = next(
                c
                for c in inspect(conn).get_columns("movies")
                if c["name"] == "arr_first_import_at"
            )
            assert column["nullable"] is True
            migration.downgrade()
        assert conn.execute(text("SELECT title FROM movies")).scalar_one() == "Existing"
        assert "arr_first_import_at" not in {
            c["name"] for c in inspect(conn).get_columns("movies")
        }
    engine.dispose()
