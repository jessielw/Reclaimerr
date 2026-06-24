from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.account import create_user
from backend.core.auth import has_page_access, require_page_access
from backend.database import Base
from backend.database.models import GeneralSettings, User
from backend.enums import PageAccess, Permission, UserRole
from backend.models.auth import CreateUserRequest


def _user(*, allowed_pages: list[str] | None = None) -> User:
    return User(
        username="viewer",
        password_hash="x",
        role=UserRole.USER,
        permissions=[],
        allowed_pages=allowed_pages,
    )


def _admin() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


def test_page_access_allows_admin_and_legacy_users() -> None:
    assert has_page_access(_admin(), PageAccess.DASHBOARD) is True
    assert has_page_access(_user(allowed_pages=None), PageAccess.DASHBOARD) is True


@pytest.mark.anyio
async def test_require_page_access_rejects_denied_page() -> None:
    dependency = require_page_access(PageAccess.DASHBOARD)

    with pytest.raises(HTTPException) as exc:
        await dependency(_user(allowed_pages=[PageAccess.CANDIDATES.value]))

    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_create_user_applies_default_page_access() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add(GeneralSettings(default_allowed_pages=[PageAccess.HISTORY.value]))
        await db.flush()

        response = await create_user(
            CreateUserRequest(
                username="newviewer",
                password="password",
                role=UserRole.USER,
                permissions=[Permission.REQUEST],
                use_default_page_access=True,
            ),
            _admin(),
            db,
        )

    await engine.dispose()

    assert response.allowed_pages == [PageAccess.HISTORY]


@pytest.mark.anyio
async def test_create_user_falls_back_to_candidate_and_settings_pages() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        response = await create_user(
            CreateUserRequest(
                username="defaultviewer",
                password="password",
                role=UserRole.USER,
                permissions=[],
                use_default_page_access=True,
            ),
            _admin(),
            db,
        )

    await engine.dispose()

    assert response.allowed_pages == [PageAccess.CANDIDATES, PageAccess.SETTINGS]


@pytest.mark.anyio
async def test_create_user_can_store_unrestricted_page_access() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        response = await create_user(
            CreateUserRequest(
                username="openviewer",
                password="password",
                role=UserRole.USER,
                permissions=[],
                use_default_page_access=False,
                allowed_pages=None,
            ),
            _admin(),
            db,
        )

    await engine.dispose()

    assert response.allowed_pages is None
