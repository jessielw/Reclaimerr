from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import BackgroundJob, ServiceConfig
from backend.enums import BackgroundJobStatus, BackgroundJobType, Task
from backend.jobs import enqueue_background_job
from backend.models.jobs import TaskRunJobPayload
from backend.tasks.cleanup import scan_cleanup_candidates, tag_cleanup_candidates
from backend.tasks.house_keeping import weekly_house_keeping
from backend.tasks.sync import (
    resync_media,
    sync_linked_data,
    sync_media,
    sync_media_libraries,
)
from backend.types import MEDIA_SERVERS

MAIN_SERVER_REQUIRED_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA,
        Task.RESYNC_MEDIA,
        Task.SYNC_MEDIA_LIBRARIES,
        Task.SYNC_LINKED_DATA,
        Task.SCAN_CLEANUP_CANDIDATES,
        Task.TAG_CLEANUP_CANDIDATES,
    }
)


async def _get_active_task_job(task: Task) -> BackgroundJob | None:
    async with async_db() as session:
        result = await session.execute(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == BackgroundJobType.TASK_RUN,
                BackgroundJob.dedupe_key == f"task-run-{task}",
                BackgroundJob.status.in_(
                    [BackgroundJobStatus.PENDING, BackgroundJobStatus.RUNNING]
                ),
            )
            .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def request_task_run(task: Task) -> tuple[BackgroundJob | None, bool]:
    queued_job = await enqueue_background_job(
        job_type=BackgroundJobType.TASK_RUN,
        payload=TaskRunJobPayload(task=task).model_dump(mode="json"),
        scheduled_at=datetime.now(UTC),
        dedupe_key=f"task-run-{task}",
        skip_if_active=True,
    )
    if queued_job is not None:
        return queued_job, True

    return await _get_active_task_job(task), False


async def enqueue_task_run(task: Task) -> bool:
    _, queued = await request_task_run(task)
    return queued


async def enqueue_scheduled_task(task: Task) -> None:
    queued = await enqueue_task_run(task)
    if queued:
        LOG.info(f"Queued scheduled task: {task.friendly_name()}")
    else:
        LOG.info(
            f"Skipped scheduled task enqueue for {task.friendly_name()} (already active)"
        )


async def _run_linked_data_sync() -> None:
    async with async_db() as session:
        result = await session.execute(
            select(ServiceConfig).where(
                ServiceConfig.service_type.in_(MEDIA_SERVERS),
                ServiceConfig.enabled.is_(True),
            )
        )
        media_servers = result.scalars().all()

    main_service_type = None
    for service_config in media_servers:
        if service_config.is_main:
            main_service_type = service_config.service_type
            break

    for service_config in media_servers:
        if service_config.service_type != main_service_type:
            await sync_linked_data(service_config.service_type)  # type: ignore[reportArgumentType]


async def execute_task(task: Task) -> dict[str, Any] | None:
    if task is Task.SYNC_MEDIA:
        return await sync_media()
    if task is Task.SYNC_MEDIA_LIBRARIES:
        return await sync_media_libraries()
    if task is Task.SYNC_LINKED_DATA:
        await _run_linked_data_sync()
        return
    if task is Task.RESYNC_MEDIA:
        await resync_media()
        return
    if task is Task.SCAN_CLEANUP_CANDIDATES:
        await scan_cleanup_candidates()
        return
    if task is Task.TAG_CLEANUP_CANDIDATES:
        await tag_cleanup_candidates()
        return
    if task is Task.WEEKLY_HOUSE_KEEPING:
        await weekly_house_keeping()
        return

    raise ValueError(f"Unsupported task for background execution: {task}")
