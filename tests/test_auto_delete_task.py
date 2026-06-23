from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings.general import (
    get_general_settings,
    update_general_settings,
)
from backend.api.routes.tasks import run_task_now
from backend.core.task_runtime import (
    DISABLE_ABLE_TASKS,
    MAIN_SERVER_REQUIRED_TASKS,
    execute_task,
)
from backend.database import Base
from backend.database.models import (
    DeleteRequest,
    GeneralSettings,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    TaskSchedule,
    User,
)
from backend.enums import MediaType, ScheduleType, Task, UserRole
from backend.models.settings import GeneralSettingsResponse
from backend.scheduler import DEFAULT_SCHEDULES, update_task_schedule
from backend.tasks.cleanup import delete_cleanup_candidates


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


def _async_db_override(session_maker: async_sessionmaker[AsyncSession]):
    @asynccontextmanager
    async def _override():
        async with session_maker() as session:
            yield session

    return _override


def test_auto_delete_defaults_and_metadata():
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            settings = await get_general_settings(_admin_user(), db_session)
            assert settings.auto_delete_enabled is False

        default_schedule = next(
            item
            for item in DEFAULT_SCHEDULES
            if item["task"] is Task.DELETE_CLEANUP_CANDIDATES
        )
        assert Task.DELETE_CLEANUP_CANDIDATES in MAIN_SERVER_REQUIRED_TASKS
        assert Task.DELETE_CLEANUP_CANDIDATES in DISABLE_ABLE_TASKS
        assert default_schedule["schedule_type"] is ScheduleType.CRON
        assert default_schedule["schedule_value"] == "0 2 * * 0"
        assert default_schedule["enabled"] is False

        playback_schedule = next(
            item
            for item in DEFAULT_SCHEDULES
            if item["task"] is Task.REFRESH_PLAYBACK_HISTORY
        )
        assert Task.REFRESH_PLAYBACK_HISTORY in MAIN_SERVER_REQUIRED_TASKS
        assert Task.REFRESH_PLAYBACK_HISTORY not in DISABLE_ABLE_TASKS
        assert playback_schedule["schedule_type"] is ScheduleType.MANUAL
        assert playback_schedule["schedule_value"] == ""
        assert playback_schedule["enabled"] is True

        await engine.dispose()

    asyncio.run(run())


def test_execute_task_refreshes_playback_history(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(
            "backend.core.task_runtime.is_task_enabled",
            AsyncMock(return_value=True),
        )
        refresh_mock = AsyncMock(return_value={"providers": 1})
        monkeypatch.setattr(
            "backend.core.task_runtime.refresh_playback_history_task",
            refresh_mock,
        )

        result = await execute_task(Task.REFRESH_PLAYBACK_HISTORY)

        refresh_mock.assert_awaited_once()
        assert result == {"providers": 1}

    asyncio.run(run())


def test_update_general_settings_disables_auto_delete_task(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            db_session.add(GeneralSettings(auto_delete_enabled=True))
            db_session.add(
                TaskSchedule(
                    task=Task.DELETE_CLEANUP_CANDIDATES,
                    description="auto delete",
                    schedule_type=ScheduleType.CRON,
                    schedule_value="0 2 * * 0",
                    default_schedule_type=ScheduleType.CRON,
                    default_schedule_value="0 2 * * 0",
                    enabled=True,
                )
            )
            await db_session.commit()

            disable_mock = AsyncMock()
            monkeypatch.setattr(
                "backend.scheduler.update_task_schedule",
                disable_mock,
            )

            response = await update_general_settings(
                GeneralSettingsResponse(auto_delete_enabled=False),
                _admin_user(),
                db_session,
            )

            assert response.auto_delete_enabled is False
            disable_mock.assert_awaited_once()
            kwargs = disable_mock.await_args.kwargs if disable_mock.await_args else {}
            assert kwargs["task"] is Task.DELETE_CLEANUP_CANDIDATES
            assert kwargs["enabled"] is False

            settings = (await db_session.execute(select(GeneralSettings))).scalar_one()
            assert settings.auto_delete_enabled is False

        await engine.dispose()

    asyncio.run(run())


def test_update_task_schedule_blocks_auto_delete_enable_without_opt_in(
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
            db_session.add(GeneralSettings(auto_delete_enabled=False))
            db_session.add(
                TaskSchedule(
                    task=Task.DELETE_CLEANUP_CANDIDATES,
                    description="auto delete",
                    schedule_type=ScheduleType.CRON,
                    schedule_value="0 2 * * 0",
                    default_schedule_type=ScheduleType.CRON,
                    default_schedule_value="0 2 * * 0",
                    enabled=False,
                )
            )
            await db_session.commit()

        async_db_override = _async_db_override(session_maker)
        monkeypatch.setattr("backend.scheduler.async_db", async_db_override)
        monkeypatch.setattr("backend.core.task_runtime.async_db", async_db_override)

        with pytest.raises(ValueError, match="General Settings"):
            await update_task_schedule(
                task=Task.DELETE_CLEANUP_CANDIDATES,
                schedule_type=ScheduleType.CRON,
                schedule_value="0 2 * * 0",
                enabled=True,
            )

        async with session_maker() as db_session:
            schedule = (
                await db_session.execute(
                    select(TaskSchedule).where(
                        TaskSchedule.task == Task.DELETE_CLEANUP_CANDIDATES
                    )
                )
            ).scalar_one()
            assert schedule.enabled is False

        await engine.dispose()

    asyncio.run(run())


def test_run_task_now_rejects_auto_delete_without_opt_in(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(
            "backend.api.routes.tasks.is_auto_delete_enabled",
            AsyncMock(return_value=False),
        )

        with pytest.raises(HTTPException) as exc:
            await run_task_now(Task.DELETE_CLEANUP_CANDIDATES.value, _admin_user())

        assert exc.value.status_code == 409
        assert "General Settings" in str(exc.value.detail)

    asyncio.run(run())


def test_delete_cleanup_candidates_skips_overlapping_entries(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            admin = _admin_user()
            db_session.add(admin)
            db_session.add(GeneralSettings(auto_delete_enabled=True))

            movie_candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="movie",
                movie_id=101,
            )
            movie_version_candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="movie version",
                movie_id=102,
                movie_version_id=1002,
            )
            series_candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="series",
                series_id=201,
            )
            season_candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="season",
                series_id=202,
                season_id=2002,
            )
            blocked_episode_candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="episode blocked",
                series_id=203,
                season_id=2003,
                episode_id=3003,
            )
            safe_episode_candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="episode safe",
                series_id=204,
                season_id=2004,
                episode_id=3004,
            )
            db_session.add_all(
                [
                    movie_candidate,
                    movie_version_candidate,
                    series_candidate,
                    season_candidate,
                    blocked_episode_candidate,
                    safe_episode_candidate,
                ]
            )

            db_session.add_all(
                [
                    ProtectedMedia(
                        media_type=MediaType.MOVIE,
                        protected_by_user_id=1,
                        movie_id=101,
                    ),
                    ProtectionRequest(
                        media_type=MediaType.MOVIE,
                        requested_by_user_id=1,
                        movie_id=102,
                        movie_version_id=1002,
                    ),
                    DeleteRequest(
                        media_type=MediaType.SERIES,
                        requested_by_user_id=1,
                        series_id=201,
                    ),
                    ProtectedMedia(
                        media_type=MediaType.SERIES,
                        protected_by_user_id=1,
                        series_id=202,
                        season_id=2002,
                    ),
                    ProtectionRequest(
                        media_type=MediaType.SERIES,
                        requested_by_user_id=1,
                        series_id=203,
                        season_id=2003,
                        episode_id=3003,
                    ),
                ]
            )
            await db_session.commit()

            async_db_override = _async_db_override(session_maker)
            monkeypatch.setattr("backend.tasks.cleanup.async_db", async_db_override)

            @asynccontextmanager
            async def fake_track_task_execution(_task: Task):
                yield None

            monkeypatch.setattr(
                "backend.tasks.cleanup.track_task_execution",
                fake_track_task_execution,
            )

            captured: dict[str, object] = {}

            async def fake_delete_specific_candidates(
                candidate_ids: list[int], approved_by: str = "system"
            ) -> tuple[int, int]:
                captured["candidate_ids"] = candidate_ids
                captured["approved_by"] = approved_by
                return 1, 0

            monkeypatch.setattr(
                "backend.tasks.cleanup.delete_specific_candidates",
                fake_delete_specific_candidates,
            )

            summary = await delete_cleanup_candidates()

            assert captured["approved_by"] == "system:auto-delete"
            assert captured["candidate_ids"] == [safe_episode_candidate.id]
            assert summary == {
                "eligible": 1,
                "skipped": 5,
                "deleted": 1,
                "failed": 0,
            }

        await engine.dispose()

    asyncio.run(run())
