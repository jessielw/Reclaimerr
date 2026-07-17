from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.v1 import discover_api
from backend.api.routes.v1 import router as external_api_router
from backend.api.routes.v1.docs import build_external_openapi_schema
from backend.api.routes.v1.events import list_events
from backend.api.routes.v1.media import get_movie, list_movies, list_series
from backend.api.routes.v1.protections import (
    create_protection,
    delete_protection,
    list_protections,
)
from backend.api.routes.v1.system import get_system
from backend.api.routes.v1.tasks import list_task_runs, list_tasks, run_task
from backend.core.api_tokens import ApiPrincipal
from backend.database import Base
from backend.database.models import (
    LifecycleEvent,
    Movie,
    Series,
    TaskRun,
    TaskSchedule,
)
from backend.enums import MediaType, ScheduleType, Task, TaskStatus
from backend.models.api_v1 import ProtectionCreateRequest


def test_external_api_domain_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        principal = ApiPrincipal(
            token_id=1,
            name="Automation",
            scopes=frozenset(
                {
                    "system:read",
                    "media:read",
                    "events:read",
                    "protections:manage",
                    "tasks:run",
                }
            ),
        )
        async with session_maker() as db:
            enqueue_mock = AsyncMock()
            monkeypatch.setattr(
                "backend.api.routes.v1.protections.enqueue_event_deliveries",
                enqueue_mock,
            )
            movie = Movie(
                title="API Movie",
                tmdb_id=550,
                size=1234,
            )
            series = Series(
                title="API Series",
                tmdb_id=1399,
                tvdb_id="121361",
                size=5678,
            )
            cast(Any, movie).genres = [
                {"id": 18, "name": "Drama"},
                "Thriller",
            ]
            cast(Any, series).genres = [{"id": 18, "name": "Drama"}]
            schedule = TaskSchedule(
                task=Task.CHECK_APP_UPDATES,
                schedule_type=ScheduleType.INTERVAL,
                schedule_value="3600",
                default_schedule_type=ScheduleType.INTERVAL,
                default_schedule_value="3600",
                description="Check for updates",
                enabled=True,
            )
            event = LifecycleEvent(
                event_id="00000000-0000-0000-0000-000000000001",
                event_type="candidate.scheduled",
                payload={"schema_version": 1, "candidate": {"id": 1}},
            )
            db.add_all([movie, series, schedule, event])
            await db.flush()
            task_run = TaskRun(
                task=Task.CHECK_APP_UPDATES, task_schedule_id=schedule.id
            )
            task_run.status = TaskStatus.COMPLETED
            task_run.started_at = datetime.now(UTC) - timedelta(seconds=2)
            task_run.completed_at = datetime.now(UTC)
            db.add(task_run)
            await db.commit()

            discovery = await discover_api(principal)
            assert discovery.api_version == "v1"
            assert discovery.resources["events"] == "/api/v1/events"
            assert discovery.resources["docs"] == "/api/v1/docs"
            assert discovery.resources["openapi"] == "/api/v1/openapi.json"

            movies = await list_movies(
                principal,
                db,
                page=1,
                per_page=50,
                search=None,
                tmdb_id=550,
                imdb_id=None,
                status_filter=None,
                include_removed=False,
                sort_by="title",
                sort_order="asc",
            )
            assert movies.total == 1
            assert movies.items[0].size_bytes == 1234
            assert movies.items[0].genres == ["Drama", "Thriller"]
            assert (await get_movie(movie.id, principal, db)).tmdb_id == 550

            shows = await list_series(
                principal,
                db,
                page=1,
                per_page=50,
                search=None,
                tmdb_id=None,
                tvdb_id="121361",
                imdb_id=None,
                status_filter=None,
                include_removed=False,
                sort_by="title",
                sort_order="asc",
            )
            assert shows.total == 1
            assert shows.items[0].tvdb_id == "121361"
            assert shows.items[0].genres == ["Drama"]

            created = await create_protection(
                ProtectionCreateRequest(
                    media_type=MediaType.MOVIE,
                    tmdb_id=550,
                    reason="Keep for automation test",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                ),
                principal,
                db,
            )
            assert created.created is True
            assert created.protection.scope == "movie"
            assert created.protection.permanent is False
            promoted = await create_protection(
                ProtectionCreateRequest(
                    media_type=MediaType.MOVIE,
                    tmdb_id=550,
                    reason="Keep permanently",
                ),
                principal,
                db,
            )
            assert promoted.created is False
            assert promoted.protection.permanent is True
            protections = await list_protections(
                principal,
                db,
                page=1,
                per_page=50,
                media_type=MediaType.MOVIE,
                media_id=movie.id,
                active_only=True,
            )
            assert protections.total == 1
            removed = await delete_protection(created.protection.id, principal, db)
            assert removed.id == created.protection.id

            feed = await list_events(
                principal,
                db,
                cursor=None,
                limit=100,
                event_type=None,
                candidate_id=None,
                occurred_after=None,
                occurred_before=None,
            )
            assert feed.items[0].id == event.event_id
            assert [item.type for item in feed.items] == [
                "candidate.scheduled",
                "protection.created",
                "protection.removed",
            ]
            assert feed.next_cursor == feed.items[-1].id
            assert enqueue_mock.await_count == 2

            task_list = await list_tasks(principal, db)
            assert task_list.items[0].id == Task.CHECK_APP_UPDATES.value
            history = await list_task_runs(
                Task.CHECK_APP_UPDATES.value,
                principal,
                db,
                page=1,
                per_page=50,
            )
            assert history.total == 1
            assert history.items[0].status is TaskStatus.COMPLETED

            monkeypatch.setattr(
                "backend.api.routes.v1.tasks.is_task_enabled",
                AsyncMock(return_value=True),
            )
            monkeypatch.setattr(
                "backend.api.routes.v1.tasks.request_task_run",
                AsyncMock(return_value=(SimpleNamespace(id=77), True)),
            )
            triggered = await run_task(Task.CHECK_APP_UPDATES.value, principal, db)
            assert triggered.job_id == 77
            assert triggered.queued is True

            system = await get_system(principal, db)
            assert system.status == "ok"
            assert system.api_version == "v1"

        await engine.dispose()

    asyncio.run(run())


def test_external_openapi_schema_only_contains_supported_v1_routes() -> None:
    schema = build_external_openapi_schema(external_api_router)

    assert schema["info"]["title"] == "Reclaimerr External API"
    assert schema["info"]["x-api-version"] == "v1"
    assert schema["paths"]
    assert (
        sum(
            method in {"get", "post", "put", "patch", "delete"}
            for operations in schema["paths"].values()
            for method in operations
        )
        == 21
    )
    assert all(path.startswith("/api/v1") for path in schema["paths"])
    assert "/api/settings/general" not in schema["paths"]
    assert "/api/v1/docs" not in schema["paths"]
    assert "/api/v1/openapi.json" not in schema["paths"]
    assert schema["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }


def test_external_api_documentation_routes() -> None:
    app = FastAPI()
    app.include_router(external_api_router)
    client = TestClient(app)

    openapi_response = client.get("/api/v1/openapi.json")
    assert openapi_response.status_code == 200
    schema = openapi_response.json()
    assert all(path.startswith("/api/v1") for path in schema["paths"])
    assert (
        sum(
            method in {"get", "post", "put", "patch", "delete"}
            for operations in schema["paths"].values()
            for method in operations
        )
        == 21
    )

    swagger_response = client.get("/api/v1/docs")
    assert swagger_response.status_code == 200
    assert "/api/v1/openapi.json" in swagger_response.text

    redoc_response = client.get("/api/v1/redoc")
    assert redoc_response.status_code == 200
    assert "/api/v1/openapi.json" in redoc_response.text


def test_api_scope_implications_are_domain_specific() -> None:
    principal = ApiPrincipal(
        token_id=1,
        name="Scoped",
        scopes=frozenset({"candidates:manage", "protections:manage", "tasks:run"}),
    )
    assert principal.has_scope("candidates:read")
    assert principal.has_scope("protections:read")
    assert principal.has_scope("tasks:read")
    assert not principal.has_scope("media:read")
    assert not principal.has_scope("events:read")
