from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

from backend.core.auth import (
    COOKIE_NAME,
    ORIGINAL_CLIENT_HOST_STATE_KEY,
    create_access_token,
    get_current_user,
)
from backend.core.settings import settings
from backend.database import Base
from backend.database.models import User, UserSession
from backend.enums import UserRole


def _request(
    *,
    username: str | None = None,
    header: str = "Remote-User",
    original_client: str = "172.18.0.4",
    cookie: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if username is not None:
        headers.append((header.lower().encode(), username.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/account/me",
            "headers": headers,
            "query_string": b"",
            "client": ("203.0.113.20", 4242),
            "scheme": "https",
            "server": ("reclaimerr.example", 443),
            "state": {ORIGINAL_CLIENT_HOST_STATE_KEY: original_client},
        }
    )


async def _session_maker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


def _configure_forward_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "forward_auth_enabled", True)
    monkeypatch.setattr(settings, "forward_auth_user_header", "Remote-User")
    monkeypatch.setattr(
        settings,
        "forward_auth_trusted_proxies",
        "172.18.0.4,10.20.0.0/16",
    )


def test_trusted_proxy_maps_existing_active_user(monkeypatch) -> None:
    _configure_forward_auth(monkeypatch)

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            user = User(
                username="alice",
                password_hash="hashed",
                role=UserRole.USER,
                permissions=[],
            )
            db.add(user)
            await db.commit()

            request = _request(username="alice")
            authenticated = await get_current_user(request, db)

            assert authenticated.username == "alice"
            assert authenticated.role is UserRole.USER
            assert request.state.auth_method == "forward_auth"
            assert getattr(request.state, "session_id", None) is None
        await engine.dispose()

    asyncio.run(run())


def test_configurable_header_and_proxy_cidr_are_supported(monkeypatch) -> None:
    _configure_forward_auth(monkeypatch)
    monkeypatch.setattr(settings, "forward_auth_user_header", "X-Auth-User")

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            db.add(
                User(
                    username="cidr-user",
                    password_hash="hashed",
                    role=UserRole.USER,
                    permissions=[],
                )
            )
            await db.commit()

            authenticated = await get_current_user(
                _request(
                    username="cidr-user",
                    header="X-Auth-User",
                    original_client="10.20.4.8",
                ),
                db,
            )

            assert authenticated.username == "cidr-user"
        await engine.dispose()

    asyncio.run(run())


def test_untrusted_proxy_header_cannot_authenticate(monkeypatch, caplog) -> None:
    _configure_forward_auth(monkeypatch)

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            db.add(
                User(
                    username="admin",
                    password_hash="hashed",
                    role=UserRole.ADMIN,
                    permissions=[],
                )
            )
            await db.commit()

            with pytest.raises(HTTPException) as exc:
                await get_current_user(
                    _request(username="admin", original_client="192.0.2.44"), db
                )

            assert exc.value.status_code == 401
            assert exc.value.detail == "Not authenticated"
            assert "untrusted peer" in caplog.text
        await engine.dispose()

    asyncio.run(run())


def test_unknown_trusted_identity_does_not_fall_back_to_cookie(monkeypatch) -> None:
    _configure_forward_auth(monkeypatch)

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            user = User(
                username="local-admin",
                password_hash="hashed",
                role=UserRole.ADMIN,
                permissions=[],
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            session_id = "existing-session"
            db.add(
                UserSession(
                    user_id=user.id,
                    session_id=session_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db.commit()
            cookie = create_access_token(
                data={"sub": str(user.id)},
                token_version=user.token_version,
                session_id=session_id,
            )

            with pytest.raises(HTTPException) as exc:
                await get_current_user(
                    _request(username="unknown-user", cookie=cookie), db
                )

            assert exc.value.status_code == 401
            assert exc.value.detail == "Trusted proxy user is not configured"
        await engine.dispose()

    asyncio.run(run())


def test_disabled_trusted_identity_is_rejected(monkeypatch) -> None:
    _configure_forward_auth(monkeypatch)

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            db.add(
                User(
                    username="disabled-user",
                    password_hash="hashed",
                    role=UserRole.USER,
                    permissions=[],
                    is_active=False,
                )
            )
            await db.commit()

            with pytest.raises(HTTPException) as exc:
                await get_current_user(_request(username="disabled-user"), db)

            assert exc.value.status_code == 403
            assert exc.value.detail == "User account is disabled"
        await engine.dispose()

    asyncio.run(run())


def test_multiple_trusted_identity_headers_are_rejected(monkeypatch) -> None:
    _configure_forward_auth(monkeypatch)

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            db.add(
                User(
                    username="alice",
                    password_hash="hashed",
                    role=UserRole.USER,
                    permissions=[],
                )
            )
            await db.commit()

            request = _request(username="alice")
            request.scope["headers"].append((b"remote-user", b"admin"))
            with pytest.raises(HTTPException) as exc:
                await get_current_user(request, db)

            assert exc.value.status_code == 401
            assert (
                exc.value.detail == "Trusted proxy supplied multiple username headers"
            )
        await engine.dispose()

    asyncio.run(run())


def test_missing_forward_auth_header_preserves_cookie_auth(monkeypatch) -> None:
    _configure_forward_auth(monkeypatch)

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            user = User(
                username="cookie-user",
                password_hash="hashed",
                role=UserRole.USER,
                permissions=[],
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

            session_id = "cookie-session"
            db.add(
                UserSession(
                    user_id=user.id,
                    session_id=session_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db.commit()
            cookie = create_access_token(
                data={"sub": str(user.id)},
                token_version=user.token_version,
                session_id=session_id,
            )

            request = _request(cookie=cookie)
            authenticated = await get_current_user(request, db)

            assert authenticated.username == "cookie-user"
            assert getattr(request.state, "auth_method", None) is None
        await engine.dispose()

    asyncio.run(run())
