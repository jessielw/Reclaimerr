from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import TaskRun, TaskSchedule
from backend.enums import Task, TaskStatus
from backend.services.notifications import notify_task_failure

# in memory set to track currently running tasks
_running_tasks: set[Task] = set()

# in memory dict to track recent completions (status, timestamp, error)
# keeps completed/failed status visible for TTL minutes before returning to scheduled
_recent_completions: dict[Task, tuple[TaskStatus, datetime, str | None]] = {}

# how long to keep completed/failed status in memory (in minutes)
COMPLETION_TTL_MINUTES = 3


def is_task_running(task: Task) -> bool:
    """Check if a task is currently running (in-memory check)."""
    return task in _running_tasks


def get_running_tasks() -> set[Task]:
    """Get all currently running tasks."""
    return _running_tasks.copy()


def get_task_status(task: Task) -> tuple[str, str | None]:
    """
    Get the current status of a task from in-memory tracking.

    Returns:
        tuple[str, str | None]: (status_value, error_message)
        - If task is running: (RUNNING, None)
        - If recently completed/failed (within TTL): (COMPLETED/FAILED, error)
        - Otherwise: (PENDING, None) - task is scheduled but not active
    """
    # check if currently running
    if task in _running_tasks:
        return (TaskStatus.RUNNING, None)

    # check if recently completed/failed (with TTL)
    if task in _recent_completions:
        status, completed_time, error = _recent_completions[task]

        # check if still within TTL
        elapsed = (datetime.now(timezone.utc) - completed_time).total_seconds() / 60
        if elapsed < COMPLETION_TTL_MINUTES:
            return (status, error)
        else:
            # TTL expired, remove from recent completions
            del _recent_completions[task]

    # default: task is scheduled but not active
    return (TaskStatus.SCHEDULED, None)


@asynccontextmanager
async def track_task_execution(task: Task) -> AsyncGenerator[None, None]:
    """
    Context manager to track task execution status.

    Usage:
        async with track_task_execution(Task.SYNC_PLEX_MEDIA):
            await sync_movies(service=Service.PLEX)
            await sync_series(service=Service.PLEX)

    This will:
    - Add task to in-memory running set (checked by API for real-time status)
    - Write COMPLETED/FAILED to DB only when task finishes (historical record)
    - Remove task from running set when complete
    """
    start_time = datetime.now(timezone.utc)

    # add to in-memory running set
    _running_tasks.add(task)
    LOG.info(f"Task {task.friendly_name()} started")

    # get task schedule for DB relationship
    task_schedule_id = None
    async with async_db() as session:
        result = await session.execute(
            select(TaskSchedule).where(TaskSchedule.task == task)
        )
        task_schedule = result.scalar_one_or_none()
        if task_schedule:
            task_schedule_id = task_schedule.id

    try:
        yield

        # task completed successfully - store in memory and write to DB
        completion_time = datetime.now(timezone.utc)
        _recent_completions[task] = (TaskStatus.COMPLETED, completion_time, None)

        async with async_db() as session:
            task_run = TaskRun(
                task_schedule_id=task_schedule_id,
                task=task,
                status=TaskStatus.COMPLETED,
            )
            task_run.started_at = start_time
            task_run.completed_at = completion_time

            session.add(task_run)
            await session.commit()
            LOG.info(f"Task {task.friendly_name()} completed successfully")

    except Exception as e:
        # task failed - store in memory and write to DB
        completion_time = datetime.now(timezone.utc)
        _recent_completions[task] = (TaskStatus.ERROR, completion_time, str(e))

        async with async_db() as session:
            task_run = TaskRun(
                task_schedule_id=task_schedule_id,
                task=task,
                status=TaskStatus.ERROR,
            )
            task_run.started_at = start_time
            task_run.completed_at = completion_time
            task_run.error_message = str(e)

            session.add(task_run)
            await session.commit()
            LOG.error(f"Task {task.friendly_name()} failed: {e}")

            # send notification to admins about task failure
            try:
                await notify_task_failure(
                    task_name=task.friendly_name(),
                    error_message=str(e),
                )
            except Exception as notif_error:
                # don't let notification failures affect task tracking
                LOG.error(f"Failed to send task failure notification: {notif_error}")

        raise  # raise the exception

    finally:
        # always remove from running set
        _running_tasks.discard(task)
