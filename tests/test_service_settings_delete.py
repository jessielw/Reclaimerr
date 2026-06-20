from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings import services
from backend.api.routes.settings.services import (
    delete_service_settings,
    set_service_settings,
)
from backend.core import service_runtime
from backend.core.encryption import fer_encrypt
from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieArrRef,
    ReclaimRule,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.settings import ServiceConfigUpdate


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


def test_disable_unreachable_service_skips_connection_test(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            config = ServiceConfig(
                service_type=Service.SEERR,
                name="Seerr",
                base_url="http://offline.local",
                api_key=fer_encrypt("secret"),
                enabled=True,
                is_main=False,
                extra_settings=None,
            )
            db_session.add(config)
            await db_session.commit()
            await db_session.refresh(config)

            test_mock = AsyncMock(return_value=(False, "offline"))
            enqueue_mock = AsyncMock(return_value=SimpleNamespace(id=123))
            monkeypatch.setattr(service_manager, "test_service", test_mock)
            monkeypatch.setattr(services, "enqueue_background_job", enqueue_mock)

            response = await set_service_settings(
                ServiceConfigUpdate(
                    id=config.id,
                    name="Seerr",
                    service_type=Service.SEERR,
                    base_url="http://offline.local",
                    enabled=False,
                ),
                _admin_user(),
                db_session,
            )

            assert response["data"]["enabled"] is False
            test_mock.assert_not_awaited()
            enqueue_mock.assert_awaited_once()
        await engine.dispose()

    asyncio.run(run())


def test_enable_unreachable_service_still_fails(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            config = ServiceConfig(
                service_type=Service.SEERR,
                name="Seerr",
                base_url="http://offline.local",
                api_key=fer_encrypt("secret"),
                enabled=False,
                is_main=False,
                extra_settings=None,
            )
            db_session.add(config)
            await db_session.commit()
            await db_session.refresh(config)

            test_mock = AsyncMock(return_value=(False, "offline"))
            monkeypatch.setattr(service_manager, "test_service", test_mock)

            with pytest.raises(HTTPException) as exc:
                await set_service_settings(
                    ServiceConfigUpdate(
                        id=config.id,
                        name="Seerr",
                        service_type=Service.SEERR,
                        base_url="http://offline.local",
                        enabled=True,
                    ),
                    _admin_user(),
                    db_session,
                )

            assert exc.value.status_code == 400
            assert exc.value.detail == "offline"
            test_mock.assert_awaited_once()
        await engine.dispose()

    asyncio.run(run())


def test_delete_arr_config_cleans_dependencies_with_foreign_keys(
    monkeypatch,
):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            await db_session.execute(text("PRAGMA foreign_keys=ON"))
            config = ServiceConfig(
                service_type=Service.RADARR,
                name="Offline Radarr",
                base_url="http://offline.local",
                api_key=fer_encrypt("secret"),
                enabled=True,
                is_main=False,
                extra_settings=None,
            )
            movie = Movie(title="Movie", tmdb_id=123)
            rule = ReclaimRule(
                name="Pinned rule",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition={
                    "version": 1,
                    "root": {
                        "type": "group",
                        "op": "and",
                        "children": [
                            {
                                "type": "condition",
                                "field": "media.size",
                                "operator": "greater_than",
                                "value": 1,
                            }
                        ],
                    },
                },
                action={"radarr_service_config_id": 0, "arr_action": "delete"},
            )
            settings = GeneralSettings(
                path_mappings=[
                    {
                        "source_prefix": "/media",
                        "local_prefix": "/mnt/media",
                        "service_type": Service.RADARR.value,
                        "service_config_id": 0,
                    },
                    {
                        "source_prefix": "/global",
                        "local_prefix": "/mnt/global",
                        "service_type": None,
                        "service_config_id": None,
                    },
                ]
            )
            db_session.add_all([config, movie, rule, settings])
            await db_session.flush()
            rule.action = {
                "radarr_service_config_id": config.id,
                "arr_action": "delete",
            }
            settings.path_mappings[0]["service_config_id"] = config.id
            db_session.add(
                MovieArrRef(
                    movie_id=movie.id,
                    service_config_id=config.id,
                    arr_movie_id=42,
                    arr_movie_path="/media/Movie",
                    tmdb_id=movie.tmdb_id,
                )
            )
            await db_session.commit()

            clear_mock = AsyncMock()
            monkeypatch.setattr(
                service_runtime, "clear_deleted_service_runtime", clear_mock
            )

            response = await delete_service_settings(
                config.id, _admin_user(), db_session
            )

            assert response["data"]["removed_path_mappings"] == 1
            assert response["data"]["affected_rules"] == [
                {"id": rule.id, "name": "Pinned rule"}
            ]
            assert (
                await db_session.execute(
                    select(ServiceConfig).where(ServiceConfig.id == config.id)
                )
            ).scalar_one_or_none() is None
            assert (
                await db_session.execute(
                    select(MovieArrRef).where(
                        MovieArrRef.service_config_id == config.id
                    )
                )
            ).scalar_one_or_none() is None
            await db_session.refresh(rule)
            await db_session.refresh(settings)
            assert rule.enabled is False
            assert rule.action["radarr_service_config_id"] is None
            assert len(settings.path_mappings) == 1
            clear_mock.assert_awaited_once_with(Service.RADARR, config.id)
        await engine.dispose()

    asyncio.run(run())


def test_stale_service_toggle_cannot_restore_deleted_config(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        clear_mock = AsyncMock()
        init_mock = AsyncMock()
        monkeypatch.setattr(service_runtime, "async_db", session_maker)
        monkeypatch.setattr(service_manager, "clear_radarr", clear_mock)
        monkeypatch.setattr(service_manager, "initialize_radarr", init_mock)

        await service_runtime.handle_service_toggle(
            ServiceConfigUpdate(
                id=999,
                name="Deleted Radarr",
                service_type=Service.RADARR,
                base_url="http://deleted.local",
                api_key="secret",
                enabled=True,
            )
        )

        clear_mock.assert_not_awaited()
        init_mock.assert_not_awaited()
        await engine.dispose()

    asyncio.run(run())
