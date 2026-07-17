from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import (
    API_TOKEN_TASKS_READ_SCOPE,
    API_TOKEN_TASKS_RUN_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.service_manager import service_manager
from backend.core.task_runtime import (
    MAIN_SERVER_REQUIRED_TASKS,
    is_task_enabled,
    request_task_run,
)
from backend.core.task_tracking import COMPLETION_TTL_MINUTES
from backend.core.utils.datetime_utils import ensure_utc
from backend.database import get_db
from backend.database.models import BackgroundJob, TaskRun, TaskSchedule
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    ScheduleType,
    Task,
    TaskStatus,
)
from backend.models.api_v1 import (
    TaskListResponse,
    TaskResponse,
    TaskRunListResponse,
    TaskRunResponse,
    TaskRunTriggerResponse,
)
from backend.models.api_v1.common import total_pages
from backend.scheduler import scheduler

router = APIRouter(tags=["v1:tasks"])


async def _latest_job(db: AsyncSession, task: Task) -> BackgroundJob | None:
    return (
        await db.execute(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == BackgroundJobType.TASK_RUN,
                BackgroundJob.dedupe_key == f"task-run-{task}",
            )
            .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _job_status(job: BackgroundJob | None) -> tuple[TaskStatus, str | None]:
    if job is None:
        return TaskStatus.SCHEDULED, None
    if job.status is BackgroundJobStatus.PENDING:
        return TaskStatus.QUEUED, None
    if job.status is BackgroundJobStatus.RUNNING:
        return TaskStatus.RUNNING, None
    if job.status is BackgroundJobStatus.FAILED:
        return TaskStatus.ERROR, job.error_message
    if job.status is BackgroundJobStatus.COMPLETED and job.completed_at is not None:
        age = datetime.now(UTC) - ensure_utc(job.completed_at)
        if age.total_seconds() < COMPLETION_TTL_MINUTES * 60:
            return TaskStatus.COMPLETED, None
    return TaskStatus.SCHEDULED, None


async def _task_response(db: AsyncSession, schedule: TaskSchedule) -> TaskResponse:
    live_job = scheduler.get_job(schedule.task.value)
    latest_run = (
        await db.execute(
            select(TaskRun)
            .where(TaskRun.task == schedule.task)
            .order_by(TaskRun.completed_at.desc(), TaskRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if schedule.enabled:
        task_status, error = _job_status(await _latest_job(db, schedule.task))
    else:
        task_status, error = TaskStatus.DISABLED, None
    requires_main = schedule.task in MAIN_SERVER_REQUIRED_TASKS
    return TaskResponse(
        id=schedule.task.value,
        name=schedule.task.friendly_name(),
        description=schedule.description,
        enabled=schedule.enabled,
        status=task_status,
        error=error,
        schedule_type=schedule.schedule_type,
        schedule_value=schedule.schedule_value,
        next_run_at=(
            ensure_utc(live_job.next_run_time)
            if live_job is not None and live_job.next_run_time is not None
            else None
        ),
        last_run_at=(
            ensure_utc(latest_run.completed_at)
            if latest_run is not None and latest_run.completed_at is not None
            else None
        ),
        can_run=(
            schedule.enabled
            and (not requires_main or service_manager.main_media_server is not None)
        ),
        requires_main_server=requires_main,
    )


async def _schedule_or_404(db: AsyncSession, task_id: str) -> TaskSchedule:
    try:
        task = Task(task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found")
    schedule = (
        await db.execute(select(TaskSchedule).where(TaskSchedule.task == task))
    ).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return schedule


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_TASKS_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskListResponse:
    schedules = (
        (await db.execute(select(TaskSchedule).order_by(TaskSchedule.id.asc())))
        .scalars()
        .all()
    )
    return TaskListResponse(
        items=[await _task_response(db, schedule) for schedule in schedules],
        has_main_server=service_manager.main_media_server is not None,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_TASKS_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskResponse:
    return await _task_response(db, await _schedule_or_404(db, task_id))


@router.get("/tasks/{task_id}/runs", response_model=TaskRunListResponse)
async def list_task_runs(
    task_id: str,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_TASKS_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
) -> TaskRunListResponse:
    schedule = await _schedule_or_404(db, task_id)
    filters = TaskRun.task == schedule.task
    total = int(
        (
            await db.execute(select(func.count()).select_from(TaskRun).where(filters))
        ).scalar_one()
    )
    runs = (
        (
            await db.execute(
                select(TaskRun)
                .where(filters)
                .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        )
        .scalars()
        .all()
    )
    return TaskRunListResponse(
        items=[
            TaskRunResponse(
                id=run.id,
                task_id=run.task.value,
                status=run.status,
                items_processed=run.items_processed,
                error=run.error_message,
                started_at=run.started_at,
                completed_at=run.completed_at,
                created_at=run.created_at,
            )
            for run in runs
        ],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@router.post("/tasks/{task_id}/run", response_model=TaskRunTriggerResponse)
async def run_task(
    task_id: str,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_TASKS_RUN_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskRunTriggerResponse:
    schedule = await _schedule_or_404(db, task_id)
    if not await is_task_enabled(schedule.task):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is disabled and cannot be run",
        )
    try:
        job, queued = await request_task_run(schedule.task)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return TaskRunTriggerResponse(
        task_id=task_id,
        job_id=job.id if job is not None else None,
        queued=queued,
        already_active=not queued,
    )
