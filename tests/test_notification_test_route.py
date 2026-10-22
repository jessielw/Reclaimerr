from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import backend.api.routes.settings.notifications as notification_routes
from backend.api.utils.exception_handlers import register_exception_handlers
from backend.core.auth import get_current_user
from backend.database import get_db
from backend.database.models import User
from backend.enums import UserRole

INTERNAL_ERROR = "Connection refused: http://10.0.0.5:8080/admin"


def _user(role: UserRole) -> User:
    return User(
        username="member",
        password_hash="hashed",
        role=role,
        permissions=[],
        allowed_pages=None,
    )


def _app(user: User) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.state.limiter = notification_routes.limiter
    notification_routes.limiter.reset()
    app.include_router(notification_routes.router, prefix="/api/settings")
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def _post_tests(monkeypatch, role: UserRole, count: int) -> list:
    async def fake_test_notification_url(_url: str) -> tuple[bool, str | None]:
        return False, INTERNAL_ERROR

    monkeypatch.setattr(
        notification_routes, "test_notification_url", fake_test_notification_url
    )

    async def run() -> list:
        async with AsyncClient(
            transport=ASGITransport(app=_app(_user(role))), base_url="http://test"
        ) as client:
            return [
                await client.post(
                    "/api/settings/notifications/test",
                    json={"channel": "apprise", "url": "json://10.0.0.5:8080/admin"},
                )
                for _ in range(count)
            ]

    return asyncio.run(run())


def test_non_admin_does_not_see_the_raw_delivery_error(monkeypatch) -> None:
    # The raw error can describe internal hosts the user probed through Apprise.
    (response,) = _post_tests(monkeypatch, UserRole.USER, 1)
    assert response.status_code == 400
    assert response.json()["detail"] == notification_routes.GENERIC_TEST_FAILURE


def test_admin_sees_the_raw_delivery_error(monkeypatch) -> None:
    (response,) = _post_tests(monkeypatch, UserRole.ADMIN, 1)
    assert response.status_code == 400
    assert response.json()["detail"] == INTERNAL_ERROR


def test_test_notifications_are_rate_limited(monkeypatch) -> None:
    responses = _post_tests(monkeypatch, UserRole.USER, 6)
    assert [r.status_code for r in responses] == [400] * 5 + [429]
