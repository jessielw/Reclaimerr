from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings.services import delete_service_settings
from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import ServiceConfig, User
from backend.enums import Service, UserRole


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


def test_delete_service_config_success_radarr(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            config = ServiceConfig(
                service_type=Service.RADARR,
                name="Radarr A",
                base_url="http://radarr.local",
                api_key="encrypted",
                enabled=True,
                is_main=False,
                extra_settings=None,
            )
            db_session.add(config)
            await db_session.commit()
            await db_session.refresh(config)

            clear_mock = AsyncMock()
            monkeypatch.setattr(service_manager, "clear_radarr", clear_mock)

            response = await delete_service_settings(
                config.id, _admin_user(), db_session
            )
            assert response["data"]["deleted"] is True
            assert response["data"]["id"] == config.id

            deleted = await db_session.execute(
                select(ServiceConfig).where(ServiceConfig.id == config.id)
            )
            assert deleted.scalar_one_or_none() is None
            clear_mock.assert_awaited_once_with(config.id)
        await engine.dispose()

    asyncio.run(run())


def test_delete_service_config_not_found():
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            with pytest.raises(HTTPException) as exc:
                await delete_service_settings(999_999, _admin_user(), db_session)
            assert exc.value.status_code == 404
        await engine.dispose()

    asyncio.run(run())


def test_delete_service_config_blocks_main_media_server():
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            config = ServiceConfig(
                service_type=Service.PLEX,
                name="Main Plex",
                base_url="http://plex.local",
                api_key="encrypted",
                enabled=True,
                is_main=True,
                extra_settings=None,
            )
            db_session.add(config)
            await db_session.commit()
            await db_session.refresh(config)

            with pytest.raises(HTTPException) as exc:
                await delete_service_settings(config.id, _admin_user(), db_session)
            assert exc.value.status_code == 409
        await engine.dispose()

    asyncio.run(run())
