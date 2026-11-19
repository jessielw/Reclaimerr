"""Linked account identities survive HTTP serialization without leaking credentials."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes import delete_requests
from backend.api.routes.v1 import router as api_router
from backend.core.api_tokens import ApiPrincipal, get_api_principal
from backend.core.auth import get_current_user
from backend.database import Base, get_db
from backend.database.models import (
    DeleteRequest,
    MediaUserIdentity,
    Movie,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Permission, Service, UserRole


@pytest.fixture
def identity_api(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            admin = User(
                username="admin", password_hash="password-secret", role=UserRole.ADMIN
            )
            pat = User(
                username="Pat_user",
                password_hash="password-secret",
                permissions=[Permission.REQUEST.value],
            )
            other = User(username="other", password_hash="password-secret")
            unlinked = User(
                username="unlinked",
                password_hash="password-secret",
                permissions=[Permission.REQUEST.value],
            )
            movie = Movie(title="Requested Movie", tmdb_id=532)
            new_movie = Movie(title="New Movie", tmdb_id=533)
            db.add_all([admin, pat, other, unlinked, movie, new_movie])
            await db.flush()
            expected = []
            for index, service in enumerate(
                [Service.JELLYFIN, Service.PLEX, Service.EMBY, Service.JELLYFIN]
            ):
                server = ServiceConfig(
                    service_type=service,
                    name=f"Server {index}",
                    base_url="https://private-server.example",
                    api_key="server-secret",
                    enabled=index != 2,
                )
                db.add(server)
                await db.flush()
                identity = MediaUserIdentity(
                    source_service=service,
                    source_service_config_id=server.id,
                    source_user_id="42",
                    username=f"Pat_media_{index}",
                    username_normalized=f"pat_media_{index}",
                    display_name="Pat 媒体" if index == 0 else None,
                    last_seen_at=datetime.now(UTC),
                    user_id=pat.id,
                    raw={"token": "raw-secret"},
                    plex_auth_token="plex-secret",
                )
                db.add(identity)
                expected.append(
                    {
                        "source_service": service.value,
                        "source_service_config_id": server.id,
                        "source_service_name": server.name,
                        "source_user_id": "42",
                        "username": identity.username,
                        "display_name": identity.display_name,
                    }
                )
                if index == 0:
                    for user_id, source_id in [
                        (other.id, "other-id"),
                        (None, "unclaimed-id"),
                    ]:
                        db.add(
                            MediaUserIdentity(
                                source_service=service,
                                source_service_config_id=server.id,
                                source_user_id=source_id,
                                username=source_id,
                                username_normalized=source_id,
                                last_seen_at=datetime.now(UTC),
                                user_id=user_id,
                            )
                        )
            for owner in [pat, pat, other, unlinked]:
                db.add(
                    DeleteRequest(
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                        requested_by_user_id=owner.id,
                    )
                )
            await db.commit()
            return SimpleNamespace(
                admin=admin,
                pat=pat,
                other=other,
                unlinked=unlinked,
                new_movie_id=new_movie.id,
                expected=expected,
                current_user=pat,
            )

    state = asyncio.run(seed())
    state.principal = ApiPrincipal(1, "reader", frozenset({"users:read"}))
    state.identity_queries = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record_queries(_conn, _cursor, statement, _parameters, _context, _many):
        if "FROM media_user_identities" in statement:
            state.identity_queries.append(statement)

    async def session():
        async with sessions() as db:
            yield db

    async def principal():
        return state.principal

    async def current_user():
        return state.current_user

    monkeypatch.setattr(delete_requests, "notify_admins", AsyncMock())
    monkeypatch.setattr(delete_requests, "notify_user", AsyncMock())
    app = FastAPI()
    app.include_router(api_router)
    app.include_router(delete_requests.router)
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_api_principal] = principal
    app.dependency_overrides[get_current_user] = current_user
    with TestClient(app) as client:
        state.client = client
        yield state
    asyncio.run(engine.dispose())


def test_users_include_only_their_linked_identities(identity_api):
    state = identity_api
    response = state.client.get("/api/v1/users?q=Pat_user")
    assert response.status_code == 200
    (account,) = response.json()
    assert account["id"] == state.pat.id
    assert account["username"] == "Pat_user"
    assert account["media_identities"] == state.expected
    assert len(state.identity_queries) == 1
    for secret in [
        "password-secret",
        "server-secret",
        "raw-secret",
        "plex-secret",
        "private-server",
    ]:
        assert secret not in response.text

    state.identity_queries.clear()
    accounts = state.client.get("/api/v1/users").json()
    assert len(state.identity_queries) == 1  # One query for all accounts' links.
    by_id = {account["id"]: account for account in accounts}
    assert by_id[state.unlinked.id]["media_identities"] == []
    assert by_id[state.admin.id]["media_identities"] == []
    assert by_id[state.other.id]["media_identities"][0]["source_user_id"] == "other-id"

    page = state.client.get("/api/v1/users?offset=1&limit=1").json()
    assert [account["id"] for account in page] == [state.pat.id]
    assert page[0]["media_identities"] == state.expected
    state.identity_queries.clear()
    assert state.client.get("/api/v1/users?q=missing").json() == []
    assert state.identity_queries == []

    schema = state.client.get("/api/v1/openapi.json").json()
    assert (
        "media_identities"
        in schema["components"]["schemas"]["UserAccountResponse"]["properties"]
    )
    state.principal = ApiPrincipal(1, "no-access", frozenset())
    assert state.client.get("/api/v1/users").status_code == 403


def test_delete_request_lists_include_requester_identities(identity_api):
    state = identity_api
    response = state.client.get("/api/delete-requests/my")
    assert response.status_code == 200
    assert len(response.json()) == 2
    assert len(state.identity_queries) == 1
    for request in response.json():
        assert request["requested_by_username"] == "Pat_user"
        assert request["requested_by_media_identities"] == state.expected
    assert state.client.get("/api/delete-requests").status_code == 403

    state.current_user = state.admin
    state.identity_queries.clear()
    response = state.client.get("/api/delete-requests?status_filter=pending")
    assert response.status_code == 200
    assert len(response.json()) == 4
    assert len(state.identity_queries) == 1
    for request in response.json():
        identities = request["requested_by_media_identities"]
        if request["requested_by_user_id"] == state.pat.id:
            assert identities == state.expected
        elif request["requested_by_user_id"] == state.unlinked.id:
            assert identities == []
        else:
            assert len(identities) == 1
            assert identities[0]["source_user_id"] == "other-id"
    for secret in [
        "password-secret",
        "server-secret",
        "raw-secret",
        "plex-secret",
        "private-server",
    ]:
        assert secret not in response.text


@pytest.mark.parametrize("linked", [True, False])
def test_delete_request_create_and_review_include_identities(identity_api, linked):
    state = identity_api
    state.current_user = state.pat if linked else state.unlinked
    expected = state.expected if linked else []
    response = state.client.post(
        "/api/delete-requests",
        json={"media_type": "movie", "media_id": state.new_movie_id},
    )
    assert response.status_code == 201
    created = response.json()
    assert created["requested_by_media_identities"] == expected
    state.current_user = state.admin
    response = state.client.post(f"/api/delete-requests/{created['id']}/deny", json={})
    assert response.status_code == 200
    assert response.json()["requested_by_media_identities"] == expected
    assert response.json()["requested_by_user_id"] == created["requested_by_user_id"]
    assert response.json()["reviewed_by_username"] == "admin"
