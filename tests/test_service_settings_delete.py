from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings import services
from backend.api.routes.settings.services import (
    _utc_second,
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


def test_tracearr_match_timestamps_treat_naive_values_as_utc() -> None:
    naive_utc = datetime(2026, 8, 11, 12, 0, 0, 900_000)
    aware_offset = datetime(
        2026,
        8,
        11,
        8,
        0,
        0,
        100_000,
        tzinfo=timezone(-timedelta(hours=4)),
    )

    assert _utc_second(naive_utc) == _utc_second(aware_offset)
    assert _utc_second(naive_utc) == int(naive_utc.replace(tzinfo=UTC).timestamp())


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


def test_delete_arr_service_keeps_rule_enabled_when_other_targets_remain(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            configs = [
                ServiceConfig(
                    service_type=Service.RADARR,
                    base_url=f"http://radarr-{index}",
                    api_key="secret",
                    name=f"Radarr {index}",
                    enabled=True,
                    is_main=False,
                )
                for index in (1, 2)
            ]
            rule = ReclaimRule(
                name="Multi-target rule",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition={
                    "version": 1,
                    "root": {"type": "group", "op": "and", "children": []},
                },
                action={"arr_action": "delete"},
            )
            db_session.add_all([*configs, rule])
            await db_session.flush()
            rule.action = {
                "arr_action": "delete",
                "radarr_service_config_ids": [configs[0].id, configs[1].id],
            }
            await db_session.commit()

            clear_mock = AsyncMock()
            monkeypatch.setattr(
                service_runtime, "clear_deleted_service_runtime", clear_mock
            )

            response = await delete_service_settings(
                configs[0].id, _admin_user(), db_session
            )

            await db_session.refresh(rule)
            assert response["data"]["disabled_rule_count"] == 0
            assert rule.enabled is True
            assert rule.action["radarr_service_config_ids"] == [configs[1].id]
            assert rule.action["radarr_service_config_id"] == configs[1].id
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


def _seerr_rule(name: str, values: list[str]) -> ReclaimRule:
    """A rule whose requester condition sits inside a nested group."""
    return ReclaimRule(
        name=name,
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
                        "type": "group",
                        "op": "or",
                        "children": [
                            {
                                "type": "condition",
                                "field": "seerr.requested_by_user_ids",
                                "operator": "contains_any",
                                "value": values,
                            }
                        ],
                    }
                ],
            },
        },
        action={"arr_action": "delete"},
    )


def _requester_condition(rule: ReclaimRule) -> dict:
    return rule.definition["root"]["children"][0]["children"][0]


def test_deleting_one_seerr_leaves_the_other_working(monkeypatch):
    """Two Seerrs, one deleted: only its requesters leave the rule."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            overseerr = ServiceConfig(
                service_type=Service.SEERR,
                name="Overseerr",
                base_url="http://overseerr.local",
                api_key=fer_encrypt("a"),
                enabled=True,
            )
            jellyseerr = ServiceConfig(
                service_type=Service.SEERR,
                name="Jellyseerr",
                base_url="http://jellyseerr.local",
                api_key=fer_encrypt("b"),
                enabled=True,
            )
            db_session.add_all([overseerr, jellyseerr])
            await db_session.flush()

            rule = _seerr_rule(
                "Mixed rule",
                [f"{overseerr.id}:3", f"{jellyseerr.id}:3"],
            )
            settings = GeneralSettings(
                requester_watch_user_mappings=[
                    {
                        "seerr_service_config_id": overseerr.id,
                        "seerr_user_id": 3,
                        "media_user_key": "alice",
                    },
                    {
                        "seerr_service_config_id": jellyseerr.id,
                        "seerr_user_id": 3,
                        "media_user_key": "bob",
                    },
                ]
            )
            db_session.add_all([rule, settings])
            await db_session.commit()
            await db_session.refresh(rule)
            deleted_id = overseerr.id
            kept_id = jellyseerr.id

            monkeypatch.setattr(
                service_runtime,
                "clear_deleted_service_runtime",
                AsyncMock(return_value=None),
            )

            response = await delete_service_settings(
                deleted_id, _admin_user(), db_session
            )

            assert response["data"]["removed_requester_mappings"] == 1
            assert response["data"]["disabled_rule_count"] == 0
            assert [item["id"] for item in response["data"]["affected_rules"]] == [
                rule.id
            ]

            await db_session.refresh(rule)
            assert _requester_condition(rule)["value"] == [f"{kept_id}:3"]
            assert rule.enabled is True

            refreshed_settings = (
                (await db_session.execute(select(GeneralSettings))).scalars().first()
            )
            assert refreshed_settings is not None
            assert [
                mapping["seerr_service_config_id"]
                for mapping in refreshed_settings.requester_watch_user_mappings
            ] == [kept_id]

            remaining = (
                (
                    await db_session.execute(
                        select(ServiceConfig).where(
                            ServiceConfig.service_type == Service.SEERR
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [config.id for config in remaining] == [kept_id]
        await engine.dispose()

    asyncio.run(run())


def test_deleting_the_last_seerr_disables_rules_left_with_no_requesters(monkeypatch):
    """An emptied value list is not "matches nothing" -- `not_contains_any`
    against it matches everything, which would flip a protect into a delete."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            seerr = ServiceConfig(
                service_type=Service.SEERR,
                name="Overseerr",
                base_url="http://overseerr.local",
                api_key=fer_encrypt("a"),
                enabled=True,
            )
            db_session.add(seerr)
            await db_session.flush()

            rule = _seerr_rule("Only rule", [f"{seerr.id}:3"])
            db_session.add(rule)
            await db_session.commit()
            await db_session.refresh(rule)

            monkeypatch.setattr(
                service_runtime,
                "clear_deleted_service_runtime",
                AsyncMock(return_value=None),
            )

            response = await delete_service_settings(
                seerr.id, _admin_user(), db_session
            )

            assert response["data"]["disabled_rule_count"] == 1
            await db_session.refresh(rule)
            assert rule.enabled is False
            assert _requester_condition(rule)["value"] == []
        await engine.dispose()

    asyncio.run(run())
