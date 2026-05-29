from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import delete, select

from backend.core.logger import LOG
from backend.core.task_tracking import track_task_execution
from backend.database import async_db
from backend.database.models import AdminNotice, BackgroundJob, TaskRun, UserSession
from backend.enums import Task

__all__ = ["weekly_house_keeping"]


async def _trim_task_runs(keep_recent: int) -> None:
    """Trim old task runs from the database, keeping only the most recent N runs total."""
    async with async_db() as session:
        try:
            rows_to_del = (
                select(TaskRun.id)
                .order_by(TaskRun.started_at.desc())
                .offset(keep_recent)
            )
            del_stmt = delete(TaskRun).where(TaskRun.id.in_(rows_to_del))
            result = await session.execute(del_stmt)
            count = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await session.commit()
            if count > 0:
                LOG.debug(f"Trimmed {count} old task runs")
        except Exception as e:
            LOG.error(f"Error trimming task runs: {e}")
            await session.rollback()


async def _trim_background_jobs(keep_recent: int) -> None:
    """Trim old background jobs from the database, keeping only the most recent N runs total."""
    async with async_db() as session:
        try:
            rows_to_del = (
                select(BackgroundJob.id)
                .order_by(BackgroundJob.created_at.desc())
                .offset(keep_recent)
            )
            del_stmt = delete(BackgroundJob).where(BackgroundJob.id.in_(rows_to_del))
            result = await session.execute(del_stmt)
            count = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await session.commit()
            if count > 0:
                LOG.debug(f"Trimmed {count} old background jobs")
        except Exception as e:
            LOG.error(f"Error trimming background jobs: {e}")
            await session.rollback()


async def _trim_admin_notices(retain_days: int) -> None:
    """Trim old resolved/read admin notices to keep table size bounded."""
    cutoff = datetime.now(UTC) - timedelta(days=retain_days)
    async with async_db() as session:
        try:
            del_stmt = delete(AdminNotice).where(
                AdminNotice.updated_at < cutoff,
                (AdminNotice.is_active == False) | (AdminNotice.is_read == True),
            )
            result = await session.execute(del_stmt)
            count = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await session.commit()
            if count > 0:
                LOG.debug(f"Trimmed {count} old admin notices")
        except Exception as e:
            LOG.error(f"Error trimming admin notices: {e}")
            await session.rollback()


async def _trim_user_sessions(retain_days: int) -> None:
    """Trim old expired/revoked user sessions to keep table size bounded."""
    cutoff = datetime.now(UTC) - timedelta(days=retain_days)
    async with async_db() as session:
        try:
            del_stmt = delete(UserSession).where(
                (
                    (UserSession.revoked_at.is_not(None))
                    & (UserSession.revoked_at < cutoff)
                )
                | (UserSession.expires_at < cutoff)
            )
            result = await session.execute(del_stmt)
            count = result.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
            await session.commit()
            if count > 0:
                LOG.debug(f"Trimmed {count} old user sessions")
        except Exception as e:
            LOG.error(f"Error trimming user sessions: {e}")
            await session.rollback()


class HouseKeepingTask(NamedTuple):
    """Represents a house keeping task (used only internally for this module)."""

    name: str
    func: Callable
    args: tuple | None = None
    kwargs: dict | None = None


# keep a tuple of house keeping tasks to run in the weekly house keeping task
_WEEKLY_HOUSE_KEEPING_TASKS = (
    HouseKeepingTask(
        name="Trim old task runs",
        func=_trim_task_runs,
        kwargs={"keep_recent": 200},
    ),
    HouseKeepingTask(
        name="Trim old background jobs",
        func=_trim_background_jobs,
        kwargs={"keep_recent": 200},
    ),
    HouseKeepingTask(
        name="Trim old admin notices",
        func=_trim_admin_notices,
        kwargs={"retain_days": 90},
    ),
    HouseKeepingTask(
        name="Trim old user sessions",
        func=_trim_user_sessions,
        kwargs={"retain_days": 30},
    ),
)


async def weekly_house_keeping() -> None:
    """Perform weekly house keeping tasks."""
    async with track_task_execution(Task.WEEKLY_HOUSE_KEEPING):
        LOG.info("Starting weekly house keeping tasks")

        for task in _WEEKLY_HOUSE_KEEPING_TASKS:
            LOG.info(f"Starting weekly house keeping sub-task: {task.name}")
            try:
                args = task.args or ()
                kwargs = task.kwargs or {}
                await task.func(*args, **kwargs)
                LOG.info(f"Completed weekly house keeping sub-task: {task.name}")
            except Exception as e:
                LOG.error(f"Error in weekly house keeping sub-task '{task.name}': {e}")

        LOG.info("Finished weekly house keeping tasks")
