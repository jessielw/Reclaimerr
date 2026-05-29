from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from backend.api.routes.account import (
    list_sessions,
    revoke_other_sessions,
    revoke_session,
)
from backend.core.auth import COOKIE_NAME, create_access_token, get_current_user
from backend.database import Base
from backend.database.models import User, UserSession
from backend.enums import UserRole


def _make_request_with_cookie(token: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if token:
        headers.append((b"cookie", f"{COOKIE_NAME}={token}".encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 4242),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def _new_user(username: str = "tester") -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=UserRole.USER,
        permissions=[],
    )


def test_get_current_user_requires_sid_claim() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = _new_user("nosid_user")
            db_session.add(user)
            await db_session.commit()
            await db_session.refresh(user)

            token = create_access_token(
                data={"sub": str(user.id)},
                token_version=user.token_version,
            )
            request = _make_request_with_cookie(token)

            with pytest.raises(HTTPException) as exc:
                await get_current_user(request, db_session)
            assert exc.value.status_code == 401
            assert "Session is invalid" in str(exc.value.detail)

        await engine.dispose()

    asyncio.run(run())


def test_get_current_user_accepts_active_session() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = _new_user("sid_user")
            db_session.add(user)
            await db_session.commit()
            await db_session.refresh(user)

            session_id = "session-123"
            old_last_seen = datetime.now(UTC) - timedelta(minutes=30)
            user_session = UserSession(
                user_id=user.id,
                session_id=session_id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                user_agent="pytest-agent",
                ip_address="127.0.0.1",
                last_seen_at=old_last_seen,
            )
            db_session.add(user_session)
            await db_session.commit()

            token = create_access_token(
                data={"sub": str(user.id)},
                token_version=user.token_version,
                session_id=session_id,
            )
            request = _make_request_with_cookie(token)

            authed_user = await get_current_user(request, db_session)
            assert authed_user.id == user.id
            assert request.state.session_id == session_id
            await db_session.commit()

            refreshed = await db_session.execute(
                select(UserSession).where(UserSession.session_id == session_id)
            )
            updated_session = refreshed.scalar_one()
            assert updated_session.last_seen_at is not None
            assert updated_session.last_seen_at > old_last_seen

        await engine.dispose()

    asyncio.run(run())


def test_list_and_revoke_other_sessions() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = _new_user("session_list_user")
            db_session.add(user)
            await db_session.commit()
            await db_session.refresh(user)

            now = datetime.now(UTC)
            session_ids = ["cur-session", "other-a", "other-b"]
            for sid in session_ids:
                db_session.add(
                    UserSession(
                        user_id=user.id,
                        session_id=sid,
                        expires_at=now + timedelta(hours=6),
                        user_agent=f"ua-{sid}",
                        ip_address="127.0.0.1",
                        last_seen_at=now,
                    )
                )
            await db_session.commit()

            request = _make_request_with_cookie()
            request.state.session_id = "cur-session"

            listed = await list_sessions(request, user, db_session)
            assert len(listed) == 3
            current_entries = [entry for entry in listed if entry.is_current]
            assert len(current_entries) == 1
            assert current_entries[0].session_id == "cur-session"

            revoke_result = await revoke_other_sessions(request, user, db_session)
            assert revoke_result.revoked_count == 2

            rows = await db_session.execute(
                select(UserSession).where(UserSession.user_id == user.id)
            )
            sessions_by_id = {row.session_id: row for row in rows.scalars().all()}
            assert sessions_by_id["cur-session"].revoked_at is None
            assert sessions_by_id["other-a"].revoked_at is not None
            assert sessions_by_id["other-b"].revoked_at is not None

        await engine.dispose()

    asyncio.run(run())


def test_revoke_current_session_clears_cookie() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = _new_user("revoke_self_user")
            db_session.add(user)
            await db_session.commit()
            await db_session.refresh(user)

            session_id = "self-session"
            db_session.add(
                UserSession(
                    user_id=user.id,
                    session_id=session_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=2),
                    user_agent="pytest-agent",
                    ip_address="127.0.0.1",
                    last_seen_at=datetime.now(UTC),
                )
            )
            await db_session.commit()

            request = _make_request_with_cookie()
            request.state.session_id = session_id
            response = Response()

            revoke_response = await revoke_session(
                session_id=session_id,
                request=request,
                response=response,
                current_user=user,
                db=db_session,
            )
            assert revoke_response.revoked_current is True

            updated = await db_session.execute(
                select(UserSession).where(UserSession.session_id == session_id)
            )
            updated_session = updated.scalar_one()
            assert updated_session.revoked_at is not None

            set_cookie = response.headers.get("set-cookie", "")
            assert COOKIE_NAME in set_cookie
            assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie.lower()

        await engine.dispose()

    asyncio.run(run())
