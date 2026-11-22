"""A user acting through an integration's API token, and the creator notice."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_scheduled_deletions import (
    database,
    due_job,
    media_request,
    schedule_media_deletion,
    seed,
)

from backend.api.routes.v1 import router
from backend.core.api_tokens import generate_api_token
from backend.core.auth import ORIGINAL_CLIENT_HOST_STATE_KEY
from backend.core.settings import settings
from backend.database import get_db
from backend.database.models import ApiToken, ScheduledDeletion, User
from backend.services import delete_request_workflow, schedule_workflow
from backend.services.deletion_actor import DeletionActor

SCHEDULES = "/api/v1/scheduled-deletions"
PEER = "172.18.0.4"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_acting_user_is_recorded_while_the_token_stays_on_record():
    principal = SimpleNamespace(token_id=3, name="Integration", scopes=frozenset())
    user = SimpleNamespace(id=9, username="alex")

    token_only = DeletionActor.from_token(principal)
    acting = DeletionActor.from_token_for_user(principal, user)

    assert token_only.label == "API token: Integration"
    assert token_only.user_id is None and token_only.token_name == "Integration"
    assert acting.label == "alex"
    assert (acting.user_id, acting.token_id, acting.token_name) == (
        9,
        3,
        "Integration",
    )
    assert DeletionActor.from_user(user).label == "alex"


@asynccontextmanager
async def api(monkeypatch, *, peer=PEER) -> AsyncIterator:
    monkeypatch.setattr(schedule_workflow, "request_leaving_soon_sync", lambda: None)
    monkeypatch.setattr(delete_request_workflow, "notify_user", AsyncMock())
    monkeypatch.setattr(settings, "forward_auth_enabled", True)
    monkeypatch.setattr(settings, "forward_auth_user_header", "Remote-User")
    monkeypatch.setattr(settings, "forward_auth_trusted_proxies", PEER)
    async with database(monkeypatch) as factory:
        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def record_peer(request: Request, call_next):
            # what the proxy-header wrapper does in the real app
            request.scope.setdefault("state", {})[ORIGINAL_CLIENT_HOST_STATE_KEY] = peer
            return await call_next(request)

        async def session():
            async with factory() as db:
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        app.dependency_overrides[get_db] = session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, factory


async def _token(db):
    secret, prefix, digest = generate_api_token()
    row = ApiToken(
        name="Integration",
        token_prefix=prefix,
        token_hash=digest,
        scopes=["media:read", "scheduled_deletions:manage"],
    )
    db.add(row)
    await db.flush()
    return row, {"Authorization": f"Bearer {secret}"}


async def _schedule(client, headers, movie, version):
    return await client.post(
        SCHEDULES,
        headers=headers,
        json={
            "media_type": "movie",
            "media_id": movie.id,
            "movie_version_id": version.id,
            "scheduled_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        },
    )


async def _only_schedule(factory) -> ScheduledDeletion:
    async with factory() as db:
        return (await db.scalars(select(ScheduledDeletion))).one()


@pytest.mark.anyio
async def test_a_trusted_caller_can_name_the_user_behind_a_token_call(monkeypatch):
    async with api(monkeypatch) as (client, factory):
        async with factory() as db:
            user, movie, version, _, _ = await seed(db)
            token, headers = await _token(db)
            await db.commit()
            user_id, username, token_id = user.id, user.username, token.id

        response = await _schedule(
            client, {**headers, "Remote-User": username}, movie, version
        )

        assert response.status_code == 201, response.text
        row = await _only_schedule(factory)
        assert row.created_by_user_id == user_id
        assert row.created_by_username == username
        # the token still decides whether the schedule may run
        assert row.created_by_api_token_id == token_id


@pytest.mark.anyio
async def test_without_a_user_the_call_stays_attributed_to_the_token(monkeypatch):
    async with api(monkeypatch) as (client, factory):
        async with factory() as db:
            _, movie, version, _, _ = await seed(db)
            _, headers = await _token(db)
            await db.commit()

        response = await _schedule(client, headers, movie, version)

        assert response.status_code == 201, response.text
        row = await _only_schedule(factory)
        assert row.created_by_user_id is None
        assert row.created_by_username == "API token: Integration"


@pytest.mark.anyio
@pytest.mark.parametrize("claimed", ["nobody-by-that-name", "disabled"])
async def test_an_unknown_or_disabled_user_never_fails_the_call(monkeypatch, claimed):
    async with api(monkeypatch) as (client, factory):
        async with factory() as db:
            _, movie, version, _, _ = await seed(db)
            _, headers = await _token(db)
            db.add(User(username="disabled", password_hash="unused", is_active=False))
            await db.commit()

        response = await _schedule(
            client, {**headers, "Remote-User": claimed}, movie, version
        )

        assert response.status_code == 201, response.text
        row = await _only_schedule(factory)
        assert row.created_by_user_id is None
        assert row.created_by_username == "API token: Integration"


@pytest.mark.anyio
async def test_the_user_is_ignored_from_a_peer_that_is_not_trusted(monkeypatch):
    async with api(monkeypatch, peer="203.0.113.9") as (client, factory):
        async with factory() as db:
            user, movie, version, _, _ = await seed(db)
            _, headers = await _token(db)
            await db.commit()
            username = user.username

        response = await _schedule(
            client, {**headers, "Remote-User": username}, movie, version
        )

        assert response.status_code == 201, response.text
        assert (await _only_schedule(factory)).created_by_user_id is None


@pytest.mark.anyio
async def test_the_user_is_ignored_when_forward_auth_is_off(monkeypatch):
    async with api(monkeypatch) as (client, factory):
        monkeypatch.setattr(settings, "forward_auth_enabled", False)
        async with factory() as db:
            user, movie, version, _, _ = await seed(db)
            _, headers = await _token(db)
            await db.commit()
            username = user.username

        response = await _schedule(
            client, {**headers, "Remote-User": username}, movie, version
        )

        assert response.status_code == 201, response.text
        assert (await _only_schedule(factory)).created_by_user_id is None
