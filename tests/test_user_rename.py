from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

from backend.api.routes.account import update_user
from backend.core.auth import COOKIE_NAME, create_access_token, get_current_user
from backend.database import Base
from backend.database.models import ReclaimHistory, User, UserSession
from backend.enums import MediaType, Permission, UserRole
from backend.models.auth import UpdateUserRequest


async def _session_maker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


def _admin(username: str = "admin") -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=UserRole.ADMIN,
        permissions=[],
    )


def _member(username: str, permissions: list[str] | None = None) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=UserRole.USER,
        permissions=permissions or [],
    )


def _update(username: str | None, role: UserRole = UserRole.USER) -> UpdateUserRequest:
    return UpdateUserRequest(username=username, role=role)


def _request_with_cookie(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/account/me",
            "headers": [(b"cookie", f"{COOKIE_NAME}={token}".encode())],
            "query_string": b"",
            "client": ("127.0.0.1", 4242),
            "scheme": "http",
            "server": ("testserver", 80),
            "state": {},
        }
    )


def test_admin_can_rename_another_user() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _admin()
            target = _member("oldname")
            db.add_all([actor, target])
            await db.commit()

            await update_user(target.id, _update("newname"), actor, db)

            renamed = await db.get(User, target.id)
            assert renamed is not None
            assert renamed.username == "newname"
        await engine.dispose()

    asyncio.run(run())


def test_admin_renaming_themselves_keeps_their_session() -> None:
    """The JWT subject is the user id, so a rename must not sign the actor out."""

    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _admin("admin")
            db.add(actor)
            await db.commit()

            session_id = "session-rename"
            db.add(
                UserSession(
                    user_id=actor.id,
                    session_id=session_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    user_agent="pytest-agent",
                    ip_address="127.0.0.1",
                )
            )
            await db.commit()

            token = create_access_token(
                data={"sub": str(actor.id)},
                token_version=actor.token_version,
                session_id=session_id,
            )

            await update_user(actor.id, _update("sso_name", UserRole.ADMIN), actor, db)

            authenticated = await get_current_user(_request_with_cookie(token), db)
            assert authenticated.id == actor.id
            assert authenticated.username == "sso_name"
        await engine.dispose()

    asyncio.run(run())


def test_rename_to_an_existing_username_is_rejected() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _admin()
            taken = _member("takenname")
            target = _member("oldname")
            db.add_all([actor, taken, target])
            await db.commit()
            target_id = target.id

            with pytest.raises(HTTPException) as exc:
                await update_user(target_id, _update("takenname"), actor, db)
            assert exc.value.status_code == 400
            assert exc.value.detail == "Username already exists"

            await db.rollback()
            unchanged = await db.get(User, target_id)
            assert unchanged is not None
            assert unchanged.username == "oldname"
        await engine.dispose()

    asyncio.run(run())


def test_rename_carries_reclaim_history_forward() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _admin()
            target = _member("oldname")
            db.add_all([actor, target])
            await db.commit()

            db.add_all(
                [
                    ReclaimHistory(
                        approved_by="oldname",
                        media_type=MediaType.MOVIE,
                        tmdb_id=101,
                        name="Approved By The Renamed User",
                    ),
                    ReclaimHistory(
                        approved_by="system:auto-delete",
                        media_type=MediaType.MOVIE,
                        tmdb_id=102,
                        name="Approved By The System",
                    ),
                ]
            )
            await db.commit()

            await update_user(target.id, _update("newname"), actor, db)

            result = await db.execute(
                select(ReclaimHistory.tmdb_id, ReclaimHistory.approved_by).order_by(
                    ReclaimHistory.tmdb_id
                )
            )
            assert result.all() == [(101, "newname"), (102, "system:auto-delete")]
        await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("username", ["abc", "has space", "bad!chars", "x" * 33])
def test_invalid_usernames_are_rejected_by_validation(username: str) -> None:
    with pytest.raises(ValidationError):
        UpdateUserRequest(username=username, role=UserRole.USER)


def test_omitting_username_leaves_it_unchanged() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _admin()
            target = _member("keepname")
            db.add_all([actor, target])
            await db.commit()

            # a client that predates the rename field sends no username at all
            await update_user(
                target.id, UpdateUserRequest(role=UserRole.USER), actor, db
            )

            unchanged = await db.get(User, target.id)
            assert unchanged is not None
            assert unchanged.username == "keepname"
        await engine.dispose()

    asyncio.run(run())


def test_blank_username_is_treated_as_no_change() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _admin()
            target = _member("keepname")
            db.add_all([actor, target])
            await db.commit()

            await update_user(target.id, _update("   "), actor, db)

            unchanged = await db.get(User, target.id)
            assert unchanged is not None
            assert unchanged.username == "keepname"
        await engine.dispose()

    asyncio.run(run())


def test_user_manager_cannot_rename_an_administrator() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            actor = _member("manager", [Permission.MANAGE_USERS.value])
            target = _admin("admin")
            db.add_all([actor, target])
            await db.commit()
            target_id = target.id

            with pytest.raises(HTTPException) as exc:
                await update_user(
                    target_id, _update("stolen_name", UserRole.ADMIN), actor, db
                )
            assert exc.value.status_code == 403

            await db.rollback()
            unchanged = await db.get(User, target_id)
            assert unchanged is not None
            assert unchanged.username == "admin"
        await engine.dispose()

    asyncio.run(run())
