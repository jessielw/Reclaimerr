"""Regression tests for multi-instance media server support in the services API.

Covers two bugs found while implementing same-type multi-instance media
servers (e.g. two Plex configs):

1. `_find_existing_service_config` matched non-arr services (including media
   servers) by `service_type` alone when `id` was omitted, so creating a
   second same-type media server threw `MultipleResultsFound` after insert,
   and silently reused the first instance's API key when a new instance's key
   was omitted.
2. `main_switched` (which gates whether a full resync runs) was computed by
   comparing `service_type`, so promoting a different config of the SAME type
   to main (e.g. swapping which of two Plex servers is main) never triggered
   the resync that clears the old main's stale MovieVersion rows.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings import services
from backend.api.routes.settings.services import (
    get_service_settings,
    set_service_settings,
)
from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import ServiceConfig, User
from backend.enums import Service, UserRole
from backend.models.settings import ServiceConfigUpdate


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


async def _make_session() -> tuple[async_sessionmaker[AsyncSession], object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return session_maker, engine


def test_creating_second_same_type_media_server_by_name_succeeds(monkeypatch):
    async def run() -> None:
        session_maker, engine = await _make_session()
        monkeypatch.setattr(
            service_manager, "test_service", AsyncMock(return_value=(True, ""))
        )
        # enqueue_background_job's return value only needs a truthy `.id`
        monkeypatch.setattr(
            services,
            "enqueue_background_job",
            AsyncMock(return_value=type("Job", (), {"id": 1})()),
        )

        async with session_maker() as db:
            first = await set_service_settings(
                ServiceConfigUpdate(
                    id=None,
                    name="Plex House",
                    service_type=Service.PLEX,
                    base_url="http://plex-house",
                    api_key="key-house",
                    enabled=True,
                    is_main=True,
                ),
                _admin_user(),
                db,
            )
            # regression: before the fix, this raised MultipleResultsFound
            # once a second same-type config existed - here it's still the
            # very first Plex config, so this call alone wouldn't have
            # triggered the bug, but establishes the baseline.
            assert first["data"]["name"] == "Plex House"

            second = await set_service_settings(
                ServiceConfigUpdate(
                    id=None,
                    name="Plex Cabin",
                    service_type=Service.PLEX,
                    base_url="http://plex-cabin",
                    api_key="key-cabin",
                    enabled=True,
                    is_main=False,
                ),
                _admin_user(),
                db,
            )
            assert second["data"]["name"] == "Plex Cabin"
            assert second["data"]["id"] != first["data"]["id"]

            rows = (
                (await db.execute(select(ServiceConfig).where(
                    ServiceConfig.service_type == Service.PLEX
                )))
                .scalars()
                .all()
            )
            assert {row.name for row in rows} == {"Plex House", "Plex Cabin"}
            assert len({row.id for row in rows}) == 2

            # GET /services must return both under plex.instances, not just one
            response = await get_service_settings(_admin_user(), db)
            plex_bucket = response[Service.PLEX]
            assert len(plex_bucket["instances"]) == 2
            assert {inst["name"] for inst in plex_bucket["instances"]} == {
                "Plex House",
                "Plex Cabin",
            }
        await engine.dispose()

    asyncio.run(run())


def test_omitted_api_key_on_new_same_type_instance_does_not_reuse_first_key(
    monkeypatch,
):
    async def run() -> None:
        session_maker, engine = await _make_session()
        monkeypatch.setattr(
            service_manager, "test_service", AsyncMock(return_value=(True, ""))
        )
        monkeypatch.setattr(
            services,
            "enqueue_background_job",
            AsyncMock(return_value=type("Job", (), {"id": 1})()),
        )

        async with session_maker() as db:
            await set_service_settings(
                ServiceConfigUpdate(
                    id=None,
                    name="Plex House",
                    service_type=Service.PLEX,
                    base_url="http://plex-house",
                    api_key="key-house",
                    enabled=True,
                    is_main=True,
                ),
                _admin_user(),
                db,
            )

            # a brand new, distinctly-named second instance with NO api_key -
            # must be rejected, not silently resolved to "Plex House"'s key.
            try:
                await set_service_settings(
                    ServiceConfigUpdate(
                        id=None,
                        name="Plex Cabin",
                        service_type=Service.PLEX,
                        base_url="http://plex-cabin",
                        api_key=None,
                        enabled=True,
                        is_main=False,
                    ),
                    _admin_user(),
                    db,
                )
            except Exception as exc:  # HTTPException
                assert getattr(exc, "status_code", None) == 400
            else:
                raise AssertionError(
                    "expected HTTPException for missing api_key on new instance"
                )

            # and the second/query-level MultipleResultsFound must not occur
            # even after a successful first save.
            rows = (
                (await db.execute(select(ServiceConfig).where(
                    ServiceConfig.service_type == Service.PLEX
                )))
                .scalars()
                .all()
            )
            assert len(rows) == 1  # the rejected save must not have inserted anything
        await engine.dispose()

    asyncio.run(run())


def test_promoting_same_type_config_to_main_marks_main_switched(monkeypatch):
    """main_switched must be identity-based, not type-based.

    Swapping which of two Plex configs is main is exactly the scenario this
    feature exists for, and it must trigger the same full-resync path as
    swapping to a different-type main - otherwise the old main's stale
    MovieVersion rows are never cleared.
    """

    async def run() -> None:
        session_maker, engine = await _make_session()
        monkeypatch.setattr(
            service_manager, "test_service", AsyncMock(return_value=(True, ""))
        )
        monkeypatch.setattr(
            services,
            "enqueue_background_job",
            AsyncMock(return_value=type("Job", (), {"id": 1})()),
        )

        async with session_maker() as db:
            house = ServiceConfig(
                service_type=Service.PLEX,
                name="Plex House",
                base_url="http://plex-house",
                api_key="encrypted-house",
                enabled=True,
                is_main=True,
            )
            cabin = ServiceConfig(
                service_type=Service.PLEX,
                name="Plex Cabin",
                base_url="http://plex-cabin",
                api_key="encrypted-cabin",
                enabled=True,
                is_main=False,
            )
            db.add_all([house, cabin])
            await db.commit()
            await db.refresh(house)
            await db.refresh(cabin)

            response = await set_service_settings(
                ServiceConfigUpdate(
                    id=cabin.id,
                    name="Plex Cabin",
                    service_type=Service.PLEX,
                    base_url="http://plex-cabin",
                    api_key="key-cabin",
                    enabled=True,
                    is_main=True,
                ),
                _admin_user(),
                db,
            )

            # regression: a type-only comparison would see PLEX == PLEX and
            # report "sync" here, never triggering the clearing resync.
            assert response["sync_action"] == "resync"
        await engine.dispose()

    asyncio.run(run())


def test_multiple_results_found_would_have_been_raised_pre_fix() -> None:
    """Documents the exact failure mode the fix prevents, independent of the
    API layer: matching by service_type alone raises once >1 row exists."""

    async def run() -> None:
        session_maker, engine = await _make_session()
        async with session_maker() as db:
            db.add_all(
                [
                    ServiceConfig(
                        service_type=Service.PLEX,
                        name="Plex House",
                        base_url="http://plex-house",
                        api_key="x",
                        enabled=True,
                        is_main=True,
                    ),
                    ServiceConfig(
                        service_type=Service.PLEX,
                        name="Plex Cabin",
                        base_url="http://plex-cabin",
                        api_key="x",
                        enabled=True,
                        is_main=False,
                    ),
                ]
            )
            await db.commit()

            # the pre-fix lookup used in _find_existing_service_config's
            # buggy branch: service_type alone, no name filter.
            try:
                (
                    await db.execute(
                        select(ServiceConfig).where(
                            ServiceConfig.service_type == Service.PLEX
                        )
                    )
                ).scalar_one()
            except MultipleResultsFound:
                pass
            else:
                raise AssertionError("expected MultipleResultsFound from the old query shape")
        await engine.dispose()

    asyncio.run(run())
