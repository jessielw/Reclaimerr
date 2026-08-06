from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.api.routes.account import router as account_router
from backend.core.auth import get_current_user
from backend.core.settings import settings
from backend.enums import UserRole
from backend.models.auth import CurrentUserInfo


class _FakeUser:
    id = 1
    username = "alice"
    display_name = "Alice"
    email = None
    avatar_path = None
    role = UserRole.ADMIN
    permissions: list[str] = []
    allowed_pages = None
    created_at = datetime.now(UTC)
    password_hash = "hashed"
    require_password_change = False


def test_forward_auth_session_carries_source_and_logout_url() -> None:
    info = CurrentUserInfo.from_current_user(
        _FakeUser(),
        auth_source="forward_auth",
        logout_url="https://auth.example.com/logout",
    )

    assert info.username == "alice"
    assert info.auth_source == "forward_auth"
    assert info.logout_url == "https://auth.example.com/logout"


def test_local_session_reports_local_source_and_no_logout_url() -> None:
    info = CurrentUserInfo.from_current_user(
        _FakeUser(), auth_source="local", logout_url=None
    )

    assert info.auth_source == "local"
    assert info.logout_url is None


def _client_for(auth_method: str | None) -> TestClient:
    app = FastAPI()
    app.include_router(account_router)

    async def _current_user(request: Request) -> _FakeUser:
        if auth_method is not None:
            request.state.auth_method = auth_method
        return _FakeUser()

    app.dependency_overrides[get_current_user] = _current_user
    return TestClient(app)


def test_me_reports_forward_auth_with_configured_logout_url(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "forward_auth_logout_url", "https://auth.example.com/logout"
    )

    body = _client_for("forward_auth").get("/api/account/me").json()

    assert body["auth_source"] == "forward_auth"
    assert body["logout_url"] == "https://auth.example.com/logout"


def test_me_reports_forward_auth_without_logout_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "forward_auth_logout_url", "")

    body = _client_for("forward_auth").get("/api/account/me").json()

    assert body["auth_source"] == "forward_auth"
    assert body["logout_url"] is None


def test_me_reports_local_session_and_never_leaks_the_logout_url(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "forward_auth_logout_url", "https://auth.example.com/logout"
    )

    body = _client_for(None).get("/api/account/me").json()

    assert body["auth_source"] == "local"
    assert body["logout_url"] is None
