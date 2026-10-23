from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.api.routes.account import create_user, update_user
from backend.database import Base
from backend.database.models import User
from backend.enums import PageAccess, Permission, UserRole
from backend.models.auth import CreateUserRequest, UpdateUserRequest

MANAGER_PAGES = [PageAccess.DASHBOARD.value, PageAccess.MOVIES.value]


async def _session_maker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


def _manager() -> User:
    return User(
        username="manager",
        password_hash="hashed",
        role=UserRole.USER,
        permissions=[Permission.MANAGE_USERS.value, Permission.REQUEST.value],
        allowed_pages=list(MANAGER_PAGES),
    )


def _member(
    username: str = "member",
    permissions: list[str] | None = None,
    allowed_pages: list[str] | None = None,
) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=UserRole.USER,
        permissions=permissions or [],
        allowed_pages=allowed_pages,
    )


def _run_update(
    target_factory, request: UpdateUserRequest, *, edit_self: bool = False
) -> User:
    async def run() -> User:
        engine, session_maker = await _session_maker()
        try:
            async with session_maker() as db:
                actor = _manager()
                target = actor if edit_self else target_factory()
                db.add_all([actor, target] if not edit_self else [actor])
                await db.commit()
                await update_user(target.id, request, actor, db)
                await db.refresh(target)
                return target
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_manager_cannot_grant_themselves_a_permission_they_lack() -> None:
    request = UpdateUserRequest(
        role=UserRole.USER,
        permissions=[
            Permission.MANAGE_USERS,
            Permission.REQUEST,
            Permission.MANAGE_RECLAIM,
        ],
        allowed_pages=[PageAccess(page) for page in MANAGER_PAGES],
    )
    with pytest.raises(HTTPException) as exc:
        _run_update(None, request, edit_self=True)
    assert exc.value.status_code == 403


def test_manager_cannot_widen_their_own_page_access() -> None:
    request = UpdateUserRequest(
        role=UserRole.USER,
        permissions=[Permission.MANAGE_USERS, Permission.REQUEST],
        allowed_pages=None,
    )
    with pytest.raises(HTTPException) as exc:
        _run_update(None, request, edit_self=True)
    assert exc.value.status_code == 403


def test_manager_can_grant_permissions_and_pages_they_hold() -> None:
    target = _run_update(
        lambda: _member(allowed_pages=[PageAccess.DASHBOARD.value]),
        UpdateUserRequest(
            role=UserRole.USER,
            permissions=[Permission.REQUEST],
            allowed_pages=[PageAccess.DASHBOARD, PageAccess.MOVIES],
        ),
    )
    assert target.permissions == [Permission.REQUEST.value]
    assert set(target.allowed_pages or []) == set(MANAGER_PAGES)


def test_manager_can_edit_a_user_who_holds_more_than_they_do() -> None:
    # Untouched permissions and pages the manager lacks are not a grant.
    target = _run_update(
        lambda: _member(
            permissions=[Permission.MANAGE_RECLAIM.value], allowed_pages=None
        ),
        UpdateUserRequest(
            role=UserRole.USER,
            display_name="Renamed",
            permissions=[Permission.MANAGE_RECLAIM],
            allowed_pages=None,
        ),
    )
    assert target.display_name == "Renamed"


def test_manager_cannot_remove_a_permission_they_lack() -> None:
    with pytest.raises(HTTPException) as exc:
        _run_update(
            lambda: _member(permissions=[Permission.MANAGE_RECLAIM.value]),
            UpdateUserRequest(role=UserRole.USER, permissions=[]),
        )
    assert exc.value.status_code == 403


def _run_create(request: CreateUserRequest) -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        try:
            async with session_maker() as db:
                actor = _manager()
                db.add(actor)
                await db.commit()
                await create_user(request, actor, db)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_manager_cannot_create_a_user_with_permissions_they_lack() -> None:
    with pytest.raises(HTTPException) as exc:
        _run_create(
            CreateUserRequest(
                username="sidekick",
                password="a-Strong-passw0rd!",
                role=UserRole.USER,
                permissions=[Permission.AUTO_APPROVE],
            )
        )
    assert exc.value.status_code == 403


def test_manager_can_create_a_user_with_default_page_access() -> None:
    # The default page set is chosen by an admin, so it is always allowed.
    _run_create(
        CreateUserRequest(
            username="newbie",
            password="a-Strong-passw0rd!",
            role=UserRole.USER,
            permissions=[Permission.REQUEST],
        )
    )
