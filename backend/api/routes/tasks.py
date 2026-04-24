from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import require_admin
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_runtime import MAIN_SERVER_REQUIRED_TASKS, request_task_run
from backend.core.task_tracking import COMPLETION_TTL_MINUTES
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import BackgroundJob, TaskRun, TaskSchedule, User
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    ScheduleType,
    Task,
    TaskStatus,
)
from backend.models.tasks import TaskScheduleRequest
from backend.scheduler import scheduler, update_task_schedule

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


async def _get_last_task_run(
    db: AsyncSession, task_id: str, task_schedule_id: int | None
) -> TaskRun | None:
    """Get last task run."""
    task = Task(task_id)
    query = select(TaskRun).where(TaskRun.task == task)
    if task_schedule_id:
        query = query.where(TaskRun.task_schedule_id == task_schedule_id)
    query = query.order_by(TaskRun.completed_at.desc()).limit(1)
    result = await db.execute(query)
    return result.scalar_one_or_none()


def _map_job_status_to_task_status(
    job: BackgroundJob | None,
) -> tuple[TaskStatus, str | None]:
    if job is None:
        return (TaskStatus.SCHEDULED, None)
    if job.status is BackgroundJobStatus.PENDING:
        return (TaskStatus.QUEUED, None)
    if job.status is BackgroundJobStatus.RUNNING:
        return (TaskStatus.RUNNING, None)
    if job.status is BackgroundJobStatus.FAILED:
        return (TaskStatus.ERROR, job.error_message)
    if job.status is BackgroundJobStatus.COMPLETED and job.completed_at is not None:
        completed_at = job.completed_at
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        age_minutes = (datetime.now(UTC) - completed_at).total_seconds() / 60
        if age_minutes < COMPLETION_TTL_MINUTES:
            return (TaskStatus.COMPLETED, None)
    return (TaskStatus.SCHEDULED, None)


async def _get_latest_task_job(db: AsyncSession, task: Task) -> BackgroundJob | None:
    result = await db.execute(
        select(BackgroundJob)
        .where(
            BackgroundJob.job_type == BackgroundJobType.TASK_RUN,
            BackgroundJob.dedupe_key == f"task-run-{task}",
        )
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/tasks")
async def list_tasks(
    _admin: Annotated[User, Depends(require_admin)], db: AsyncSession = Depends(get_db)
) -> dict[str, list[dict] | bool]:
    """
    List all tasks from the database, enriched with live scheduler state.
    Manual tasks (default_schedule_type == MANUAL) are always present but not editable.
    """
    result = await db.execute(select(TaskSchedule))
    db_schedules = result.scalars().all()

    tasks = []
    for db_schedule in db_schedules:
        task_id = db_schedule.task.value

        # live APScheduler job only exists for non manual, enabled tasks
        job = scheduler.get_job(task_id)
        next_run = to_utc_isoformat(job.next_run_time) if job else None

        if not db_schedule.enabled:
            status = TaskStatus.DISABLED
            error = None
        else:
            latest_job = await _get_latest_task_job(db, db_schedule.task)
            status, error = _map_job_status_to_task_status(latest_job)

        last_task_run = await _get_last_task_run(db, task_id, db_schedule.id)
        last_run = (
            to_utc_isoformat(last_task_run.completed_at) if last_task_run else None
        )

        # manual tasks are not editable - their schedule cannot be configured
        editable = db_schedule.default_schedule_type != ScheduleType.MANUAL

        tasks.append(
            {
                "id": task_id,
                "name": db_schedule.task.friendly_name(),
                "description": db_schedule.description,
                "next_run": next_run,
                "last_run": last_run,
                "status": status,
                "error": error,
                "schedule_type": db_schedule.schedule_type,
                "schedule_value": db_schedule.schedule_value,
                "default_schedule_type": db_schedule.default_schedule_type,
                "default_schedule_value": db_schedule.default_schedule_value,
                "enabled": db_schedule.enabled,
                "editable": editable,
                "requires_main_server": db_schedule.task in MAIN_SERVER_REQUIRED_TASKS,
            }
        )

    return {
        "tasks": tasks,
        "has_main_server": service_manager.main_media_server is not None,
    }


@router.get("/tasks/{task_id}")
async def task_status(
    task_id: str,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """
    Get the status of a specific task.
    """
    task = scheduler.get_job(task_id)
    try:
        task_enum = Task(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    latest_job = await _get_latest_task_job(db, task_enum)
    status, error = _map_job_status_to_task_status(latest_job)

    # get last run timestamp from DB for display purposes only
    result = await db.execute(
        select(TaskRun)
        .where(TaskRun.task == task_enum)
        .order_by(TaskRun.completed_at.desc())
        .limit(1)
    )
    last_task_run = result.scalar_one_or_none()
    last_run = None
    if last_task_run and last_task_run.completed_at:
        last_run = to_utc_isoformat(last_task_run.completed_at)

    return {
        "id": task_id,
        "name": task.name if task else task_enum.friendly_name(),
        "next_run": to_utc_isoformat(task.next_run_time) if task else None,
        "last_run": last_run,
        "status": status,
        "error": error,
        "trigger": str(task.trigger) if task else None,
        "pending": task.pending if task else False,
    }


@router.post("/tasks/{task_id}/run")
async def run_task_now(task_id: str, _admin: Annotated[User, Depends(require_admin)]):
    """Trigger a task to run immediately via the standalone worker."""
    try:
        task_enum = Task(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    try:
        job, queued = await request_task_run(task_enum)
        return {
            "status": "success",
            "message": (
                f"Task '{task_id}' queued to run immediately"
                if queued
                else f"Task '{task_id}' is already queued or running"
            ),
            "job_id": job.id if job is not None else None,
            "already_active": not queued,
        }
    except Exception as e:
        LOG.error(f"Error queueing task {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/tasks/{task_id}/schedule")
async def update_schedule(
    task_id: str,
    request: TaskScheduleRequest,
    _admin: Annotated[User, Depends(require_admin)],
):
    """
    Update a task's schedule configuration.
    Requires admin role.
    """
    try:
        task_enum = Task(task_id)
        task_schedule = await update_task_schedule(
            task=task_enum,
            schedule_type=request.schedule_type,
            schedule_value=request.schedule_value,
            enabled=request.enabled,
        )

        if not task_schedule:
            raise HTTPException(
                status_code=500, detail="Failed to update task schedule"
            )

        return {
            "status": "success",
            "message": f"Task schedule updated for '{task_id}'",
            "task": {
                "id": task_schedule.task.value,
                "name": task_schedule.task.friendly_name(),
                "schedule_type": task_schedule.schedule_type.value,
                "schedule_value": task_schedule.schedule_value,
                "enabled": task_schedule.enabled,
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        LOG.error(f"Error updating task schedule {task_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# @router.post("/tasks/{task_id}/pause")
# async def pause_task(task_id: str, _admin: Annotated[User, Depends(require_admin)]):
#     """
#     Pause a scheduled task.
#     """
#     task = scheduler.get_job(task_id)
#     if not task:
#         raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

#     task.pause()
#     return {"status": "success", "message": f"Task '{task_id}' paused"}


# @router.post("/tasks/{task_id}/resume")
# async def resume_task(task_id: str, _admin: Annotated[User, Depends(require_admin)]):
#     """
#     Resume a paused scheduled task.
#     """
#     task = scheduler.get_job(task_id)
#     if not task:
#         raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

#     task.resume()
#     return {"status": "success", "message": f"Task '{task_id}' resumed"}
