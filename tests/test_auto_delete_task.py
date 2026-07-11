from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings.general import (
    get_general_settings,
    update_general_settings,
)
from backend.api.routes.tasks import run_task_now
from backend.core.auto_delete import resolve_auto_delete_policy
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
    ReclaimRule,
    TaskSchedule,
    User,
)
from backend.enums import MediaType, ScheduleType, Task, UserRole
from backend.models.settings import GeneralSettingsResponse
from backend.scheduler import (
    DEFAULT_SCHEDULES,
    ensure_default_schedules,
    update_task_schedule,
)
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
            assert settings.auto_delete_movie_delay_days == 14
            assert settings.auto_delete_series_delay_days == 7

        default_schedule = next(
            item
            for item in DEFAULT_SCHEDULES
            if item["task"] is Task.DELETE_CLEANUP_CANDIDATES
        )
        assert Task.DELETE_CLEANUP_CANDIDATES in MAIN_SERVER_REQUIRED_TASKS
        assert Task.DELETE_CLEANUP_CANDIDATES in DISABLE_ABLE_TASKS
        assert default_schedule["schedule_type"] is ScheduleType.CRON
        assert default_schedule["schedule_value"] == "0 2 * * *"
        assert default_schedule["default_schedule_value"] == "0 2 * * *"
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

        mdblist_schedule = next(
            item
            for item in DEFAULT_SCHEDULES
            if item["task"] is Task.MDBLIST_RATINGS_REFRESH
        )
        omdb_schedule = next(
            item
            for item in DEFAULT_SCHEDULES
            if item["task"] is Task.OMDB_RATINGS_REFRESH
        )
        assert mdblist_schedule["schedule_value"] == "0 6 * * *"
        assert omdb_schedule["schedule_value"] == "0 7 * * *"
        assert Task.MDBLIST_RATINGS_REFRESH in DISABLE_ABLE_TASKS
        assert Task.OMDB_RATINGS_REFRESH in DISABLE_ABLE_TASKS
        assert Task.IMDB_RATINGS_REFRESH.friendly_name() == "Refresh IMDb Ratings"
        assert Task.ANILIST_RATINGS_REFRESH.friendly_name() == "Refresh AniList Ratings"
        assert Task.MDBLIST_RATINGS_REFRESH.friendly_name() == "Refresh MDBList Ratings"
        assert Task.OMDB_RATINGS_REFRESH.friendly_name() == "Refresh OMDb Ratings"

        await engine.dispose()

    asyncio.run(run())


def test_auto_delete_policy_uses_longest_applicable_delay():
    created_at = datetime(2026, 6, 1, 12, tzinfo=UTC)

    policy = resolve_auto_delete_policy(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[1, 2],
        created_at=created_at,
        rule_actions_by_id={
            1: {"auto_delete_enabled": True, "auto_delete_delay_days": 3},
            2: {"auto_delete_enabled": True, "auto_delete_delay_days": 30},
        },
        movie_delay_days=14,
        series_delay_days=7,
        now=created_at + timedelta(days=20),
    )

    assert policy.delay_days == 30
    assert policy.eligible_at == created_at + timedelta(days=30)
    assert policy.is_eligible is False
    assert policy.is_enabled is True


def test_auto_delete_policy_inherits_global_delay_and_supports_zero():
    created_at = datetime(2026, 6, 1, 12)

    inherited = resolve_auto_delete_policy(
        media_type=MediaType.SERIES,
        matched_rule_ids=[1],
        created_at=created_at,
        rule_actions_by_id={1: {"auto_delete_enabled": True}},
        movie_delay_days=14,
        series_delay_days=7,
        now=datetime(2026, 6, 8, 12, tzinfo=UTC),
    )
    immediate = resolve_auto_delete_policy(
        media_type=MediaType.SERIES,
        matched_rule_ids=[2],
        created_at=created_at,
        rule_actions_by_id={
            2: {"auto_delete_enabled": True, "auto_delete_delay_days": 0}
        },
        movie_delay_days=14,
        series_delay_days=7,
        now=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )

    assert inherited.delay_days == 7
    assert inherited.is_eligible is True
    assert inherited.is_enabled is True
    assert immediate.delay_days == 0
    assert immediate.is_eligible is True
    assert immediate.is_enabled is True


def test_auto_delete_policy_requires_rule_opt_in():
    created_at = datetime(2026, 6, 1, 12, tzinfo=UTC)

    disabled = resolve_auto_delete_policy(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[1, 2],
        created_at=created_at,
        rule_actions_by_id={
            1: {"auto_delete_delay_days": 0},
            2: {"auto_delete_enabled": False, "auto_delete_delay_days": 0},
        },
        movie_delay_days=14,
        series_delay_days=7,
        now=created_at + timedelta(days=30),
    )

    assert disabled.is_enabled is False
    assert disabled.is_eligible is False


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


def test_execute_task_sync_media_fails_when_main_server_unavailable(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(
            "backend.core.task_runtime.is_task_enabled",
            AsyncMock(return_value=True),
        )
        load_services = AsyncMock()
        monkeypatch.setattr(
            "backend.core.task_runtime.load_enabled_services",
            load_services,
        )
        monkeypatch.setattr(
            "backend.core.task_runtime.service_manager._main_media_server",
            None,
        )

        with pytest.raises(RuntimeError, match="requires an enabled main media server"):
            await execute_task(Task.SYNC_MEDIA)

        load_services.assert_awaited_once()

    asyncio.run(run())


def test_execute_task_routes_external_rating_providers(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(
            "backend.core.task_runtime.is_task_enabled",
            AsyncMock(return_value=True),
        )
        mdblist_refresh = AsyncMock()
        omdb_refresh = AsyncMock()
        monkeypatch.setattr(
            "backend.core.task_runtime.refresh_mdblist_ratings", mdblist_refresh
        )
        monkeypatch.setattr(
            "backend.core.task_runtime.refresh_omdb_ratings", omdb_refresh
        )

        await execute_task(Task.MDBLIST_RATINGS_REFRESH)
        await execute_task(Task.OMDB_RATINGS_REFRESH)

        mdblist_refresh.assert_awaited_once()
        omdb_refresh.assert_awaited_once()

    asyncio.run(run())


def test_update_general_settings_preserves_delete_task_schedule(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            db_session.add(GeneralSettings())
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

            response = await update_general_settings(
                GeneralSettingsResponse(),
                _admin_user(),
                db_session,
            )

            settings = (await db_session.execute(select(GeneralSettings))).scalar_one()
            assert response.auto_delete_movie_delay_days == 14
            assert settings.auto_delete_movie_delay_days == 14
            schedule = (
                await db_session.execute(
                    select(TaskSchedule).where(
                        TaskSchedule.task == Task.DELETE_CLEANUP_CANDIDATES
                    )
                )
            ).scalar_one()
            assert schedule.enabled is True

        await engine.dispose()

    asyncio.run(run())


def test_update_task_schedule_allows_delete_task_without_global_switch(
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
            db_session.add(GeneralSettings())
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
            assert schedule.enabled is True

        await engine.dispose()

    asyncio.run(run())


def test_run_task_now_allows_delete_task_without_global_switch(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(
            "backend.api.routes.tasks.is_task_enabled",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "backend.api.routes.tasks.request_task_run",
            AsyncMock(return_value=(SimpleNamespace(id=123), True)),
        )

        result = await run_task_now(Task.DELETE_CLEANUP_CANDIDATES.value, _admin_user())

        assert result["status"] == "success"
        assert result["job_id"] == 123

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
            db_session.add(
                GeneralSettings(
                    auto_delete_movie_delay_days=0, auto_delete_series_delay_days=0
                )
            )
            db_session.add(
                ReclaimRule(
                    name="Auto delete",
                    media_type=MediaType.SERIES,
                    enabled=True,
                    target_scope="episode",
                    definition=None,
                    action={"auto_delete_enabled": True},
                )
            )

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
                "waiting": 0,
                "skipped": 5,
                "deleted": 1,
                "failed": 0,
            }

        await engine.dispose()

    asyncio.run(run())


def test_default_schedule_update_preserves_existing_auto_delete_schedule():
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            existing = TaskSchedule(
                task=Task.DELETE_CLEANUP_CANDIDATES,
                description="old description",
                schedule_type=ScheduleType.CRON,
                schedule_value="0 4 * * 0",
                default_schedule_type=ScheduleType.CRON,
                default_schedule_value="0 2 * * 0",
                enabled=True,
            )
            db_session.add(existing)
            await db_session.commit()

            await ensure_default_schedules(db_session)
            await db_session.refresh(existing)

            assert existing.schedule_type is ScheduleType.CRON
            assert existing.schedule_value == "0 4 * * 0"
            assert existing.default_schedule_value == "0 2 * * *"
            assert existing.enabled is True

        await engine.dispose()

    asyncio.run(run())


def test_delete_cleanup_candidates_waits_for_review_period(monkeypatch):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        now = datetime.now(UTC)
        async with session_maker() as db_session:
            db_session.add(
                GeneralSettings(
                    auto_delete_movie_delay_days=14, auto_delete_series_delay_days=7
                )
            )
            db_session.add(
                ReclaimRule(
                    name="Auto delete",
                    media_type=MediaType.SERIES,
                    enabled=True,
                    target_scope="series",
                    definition=None,
                    action={"auto_delete_enabled": True},
                )
            )
            waiting_candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="waiting movie",
            )
            eligible_candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="eligible series",
            )
            db_session.add_all([waiting_candidate, eligible_candidate])
            await db_session.flush()
            waiting_candidate.created_at = now - timedelta(days=1)
            eligible_candidate.created_at = now - timedelta(days=8)
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
        delete_mock = AsyncMock(return_value=(1, 0))
        monkeypatch.setattr(
            "backend.tasks.cleanup.delete_specific_candidates",
            delete_mock,
        )

        summary = await delete_cleanup_candidates()

        delete_mock.assert_awaited_once_with(
            [eligible_candidate.id], approved_by="system:auto-delete"
        )
        assert summary == {
            "eligible": 1,
            "waiting": 1,
            "skipped": 0,
            "deleted": 1,
            "failed": 0,
        }

        await engine.dispose()

    asyncio.run(run())
