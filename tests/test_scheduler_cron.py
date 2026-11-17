from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.scheduler as task_scheduler
from backend.api.routes.tasks import update_schedule
from backend.core.utils.cron_utils import InvalidCronSchedule, parse_cron_schedule
from backend.database import Base
from backend.database.models import TaskSchedule
from backend.enums import ScheduleType, Task
from backend.models.tasks import TaskScheduleRequest


def _run_dates(trigger: CronTrigger) -> list[int]:
    # Check a full Sunday-to-Saturday week, rather than just the next run.
    now = datetime(2026, 11, 15, tzinfo=trigger.timezone)
    end = now + timedelta(days=7)
    previous = None
    dates = []
    for _ in range(8):
        upcoming = trigger.get_next_fire_time(previous, now)
        if upcoming is None or upcoming >= end:
            return dates
        assert upcoming.hour == 8
        assert upcoming.minute == 0
        dates.append(upcoming.day)
        previous = upcoming
        now = upcoming + timedelta(microseconds=1)
    pytest.fail("Cron trigger fired more than once per day")


@asynccontextmanager
async def _schedule_database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(task_scheduler, "async_db", sessions)
        yield sessions
    finally:
        await engine.dispose()


def _schedule(expression: str, *, enabled: bool = True) -> TaskSchedule:
    return TaskSchedule(
        task=Task.DELETE_CLEANUP_CANDIDATES,
        schedule_type=ScheduleType.CRON,
        schedule_value=expression,
        default_schedule_type=ScheduleType.CRON,
        default_schedule_value="0 2 * * *",
        enabled=enabled,
    )


@pytest.mark.parametrize(
    ("weekday", "expected"),
    [
        ("0", [15]),
        ("7", [15]),
        ("1", [16]),
        ("2", [17]),
        ("3", [18]),
        ("4", [19]),
        ("5", [20]),
        ("6", [21]),
        ("0,7", [15]),
        ("1,3,5", [16, 18, 20]),
        ("0-6", [15, 16, 17, 18, 19, 20, 21]),
        ("0-7", [15, 16, 17, 18, 19, 20, 21]),
        ("1-5", [16, 17, 18, 19, 20]),
        ("5-7", [15, 20, 21]),
        ("*", [15, 16, 17, 18, 19, 20, 21]),
        ("*/2", [15, 17, 19, 21]),
        ("*/7", [15]),
        ("0-6/2", [15, 17, 19, 21]),
        ("1-5/2", [16, 18, 20]),
        ("1/2", [15, 16, 18, 20]),
        ("sun", [15]),
        ("MON-FRI", [16, 17, 18, 19, 20]),
        ("mon-sun", [15, 16, 17, 18, 19, 20, 21]),
        ("sun,1-5/2", [15, 16, 18, 20]),
        ("0-2,5-7", [15, 16, 17, 20, 21]),
    ],
)
def test_cron_weekday_lists_ranges_and_steps(weekday, expected):
    trigger = parse_cron_schedule(f"0 8 * * {weekday}", timezone=UTC)
    assert _run_dates(trigger) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "0 8 * *",
        "0 8 * * 0 extra",
        "61 8 * * 0",
        "0 24 * * 0",
        "0 8 * 13 0",
        "0 8 * * -1",
        "0 8 * * 8",
        "0 8 * * 0-8",
        "0 8 * * 6-1",
        "0 8 * * */0",
        "0 8 * * 1-5/0",
        "0 8 * * 1-5/-2",
        "0 8 * * *-5",
        "0 8 * * 0,",
        "0 8 * * ,0",
        "0 8 * * 0,,1",
        "0 8 * * monday",
        "0 8 * * foo",
        "0 8 * * mon/2",
        "0 8 * * mon-fri/2",
        "0 8 * * mon-5",
    ],
)
def test_invalid_cron_expressions_are_rejected(expression):
    with pytest.raises(InvalidCronSchedule, match="Invalid cron expression"):
        parse_cron_schedule(expression, timezone=UTC)


def test_cron_preserves_other_fields_and_timezone():
    trigger = parse_cron_schedule(" 15\t6 20 nov fri ", timezone=UTC)
    assert trigger.get_next_fire_time(
        None, datetime(2026, 11, 15, tzinfo=UTC)
    ) == datetime(2026, 11, 20, 6, 15, tzinfo=UTC)


@pytest.mark.parametrize("operation", ["startup", "refresh", "update"])
@pytest.mark.parametrize(
    ("weekday", "expected"),
    [("0", [15]), ("7", [15]), ("1-5", [16, 17, 18, 19, 20])],
)
def test_scheduler_uses_crontab_weekdays(monkeypatch, operation, weekday, expected):
    async def run():
        async with _schedule_database(monkeypatch) as sessions:
            expression = f"0 8 * * {weekday}"
            async with sessions() as session:
                session.add(
                    _schedule("0 8 * * mon" if operation == "update" else expression)
                )
                await session.commit()

            scheduler = Mock()
            scheduler.get_job.return_value = None
            monkeypatch.setattr(task_scheduler, "scheduler", scheduler)
            if operation == "startup":
                await task_scheduler.setup_scheduler()
            elif operation == "refresh":
                await task_scheduler.refresh_main_server_tasks()
            else:
                await task_scheduler.update_task_schedule(
                    Task.DELETE_CLEANUP_CANDIDATES, ScheduleType.CRON, expression, True
                )

            trigger = next(
                call.args[1]
                for call in scheduler.add_job.call_args_list
                if call.kwargs["id"] == Task.DELETE_CLEANUP_CANDIDATES.value
            )
            assert _run_dates(trigger) == expected
            async with sessions() as session:
                saved = await session.scalar(
                    select(TaskSchedule).where(
                        TaskSchedule.task == Task.DELETE_CLEANUP_CANDIDATES
                    )
                )
                assert saved.schedule_value == expression

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "expression", ["0 8 * * 8", "0 8 * * 1-5/0", "invalid", "61 8 * * mon"]
)
def test_invalid_cron_returns_400_without_changing_schedule(
    monkeypatch, enabled, expression
):
    async def run():
        async with _schedule_database(monkeypatch) as sessions:
            async with sessions() as session:
                original = _schedule("900")
                original.schedule_type = ScheduleType.INTERVAL
                session.add(original)
                await session.commit()

            scheduler = Mock()
            cancel_pending = AsyncMock()
            monkeypatch.setattr(task_scheduler, "scheduler", scheduler)
            monkeypatch.setattr(
                task_scheduler,
                "cancel_pending_background_jobs_by_dedupe_key",
                cancel_pending,
            )
            request = TaskScheduleRequest(
                schedule_type=ScheduleType.CRON,
                schedule_value=expression,
                enabled=enabled,
            )
            with pytest.raises(HTTPException) as error:
                await update_schedule(
                    Task.DELETE_CLEANUP_CANDIDATES.value, request, Mock()
                )
            assert error.value.status_code == 400
            assert "Invalid cron expression" in error.value.detail
            scheduler.add_job.assert_not_called()
            scheduler.get_job.assert_not_called()
            cancel_pending.assert_not_awaited()

            async with sessions() as session:
                saved = await session.scalar(select(TaskSchedule))
                assert saved.schedule_type is ScheduleType.INTERVAL
                assert saved.schedule_value == "900"
                assert saved.enabled is True

    asyncio.run(run())


@pytest.mark.parametrize(
    ("task", "hour"), [(Task.WEEKLY_HOUSE_KEEPING, 8), (Task.BACKUP_DATABASE, 1)]
)
@pytest.mark.parametrize("customized", [True, False])
def test_weekly_defaults_update_without_overwriting_custom_schedules(
    monkeypatch, task, hour, customized
):
    async def run():
        async with _schedule_database(monkeypatch) as sessions:
            old_default = f"0 {hour} * * 0"
            expression = "0 4 * * 0" if customized else old_default
            async with sessions() as session:
                original = _schedule(expression, enabled=False)
                original.task = task
                original.default_schedule_value = old_default
                session.add(original)
                await session.commit()

                await task_scheduler.ensure_default_schedules(session)
                await session.refresh(original)
                new_default = f"0 {hour} * * sun"
                assert original.default_schedule_value == new_default
                assert original.schedule_value == (
                    expression if customized else new_default
                )
                assert original.enabled is False
                next_run = parse_cron_schedule(
                    original.schedule_value, timezone=UTC
                ).get_next_fire_time(None, datetime(2026, 11, 15, tzinfo=UTC))
                assert next_run == datetime(
                    2026, 11, 15, 4 if customized else hour, tzinfo=UTC
                )

    asyncio.run(run())
