from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select

from backend.core.logger import LOG
from backend.core.service_bootstrap import load_enabled_services
from backend.core.service_manager import service_manager
from backend.database import async_db
from backend.database.models import (
    BackgroundJob,
    ServiceConfig,
    TaskSchedule,
)
from backend.enums import (
    BackgroundJobPriority,
    BackgroundJobStatus,
    BackgroundJobType,
    Task,
)
from backend.jobs.queue import enqueue_background_job
from backend.models.jobs import TaskRunJobPayload
from backend.tasks.anilist import refresh_anilist_ratings
from backend.tasks.cleanup import (
    delete_cleanup_candidates,
    scan_cleanup_candidates,
    tag_cleanup_candidates,
)
from backend.tasks.external_ratings import (
    refresh_mdblist_ratings,
    refresh_omdb_ratings,
)
from backend.tasks.house_keeping import weekly_house_keeping
from backend.tasks.imdb import refresh_imdb_ratings
from backend.tasks.sync import (
    refresh_playback_history_task,
    resync_media,
    sync_linked_data,
    sync_media,
    sync_media_libraries,
)
from backend.tasks.update_check import check_app_updates
from backend.user_types import MEDIA_SERVERS, MediaServerType

MAIN_SERVER_REQUIRED_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA,
        Task.RESYNC_MEDIA,
        Task.SYNC_MEDIA_LIBRARIES,
        Task.SYNC_LINKED_DATA,
        Task.REFRESH_PLAYBACK_HISTORY,
        Task.SCAN_CLEANUP_CANDIDATES,
        Task.TAG_CLEANUP_CANDIDATES,
        Task.DELETE_CLEANUP_CANDIDATES,
    }
)

TASK_RUNNER_MAIN_SERVER_PREFLIGHT_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA,
        Task.RESYNC_MEDIA,
        Task.SYNC_MEDIA_LIBRARIES,
    }
)

# any tasks that can be disabled by the user (e.g. via config or UI toggle) should be added to this set
DISABLE_ABLE_TASKS: frozenset[Task] = frozenset(
    {
        Task.CHECK_APP_UPDATES,
        Task.IMDB_RATINGS_REFRESH,
        Task.ANILIST_RATINGS_REFRESH,
        Task.MDBLIST_RATINGS_REFRESH,
        Task.OMDB_RATINGS_REFRESH,
        Task.DELETE_CLEANUP_CANDIDATES,
    }
)


def can_disable_task(task: Task) -> bool:
    """Whether the given task is allowed to be disabled by the user (e.g. via config or UI toggle)."""
    return task in DISABLE_ABLE_TASKS


async def is_task_enabled(task: Task) -> bool:
    """Whether the given task is currently enabled."""
    async with async_db() as session:
        result = await session.execute(
            select(TaskSchedule.enabled).where(TaskSchedule.task == task)
        )
        enabled = result.scalar_one_or_none()
    # default safe behavior for unknown schedule row: treat as enabled
    return True if enabled is None else bool(enabled)


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


async def request_task_run(
    task: Task,
    *,
    trigger: Literal["manual", "scheduled", "system"] = "manual",
) -> tuple[BackgroundJob | None, bool]:
    if not await is_task_enabled(task):
        raise ValueError(f"Task '{task.value}' is disabled")

    queued_job = await enqueue_background_job(
        job_type=BackgroundJobType.TASK_RUN,
        payload=TaskRunJobPayload(task=task, trigger=trigger).model_dump(mode="json"),
        scheduled_at=datetime.now(UTC),
        dedupe_key=f"task-run-{task}",
        skip_if_active=True,
        priority=(
            BackgroundJobPriority.LOW
            if trigger == "scheduled"
            else BackgroundJobPriority.NORMAL
        ),
    )
    if queued_job is not None:
        return queued_job, True

    return await _get_active_task_job(task), False


async def enqueue_task_run(task: Task) -> bool:
    _, queued = await request_task_run(task, trigger="system")
    return queued


async def enqueue_scheduled_task(task: Task) -> None:
    if not await is_task_enabled(task):
        LOG.info(
            f"Skipped scheduled task enqueue for {task.friendly_name()} (disabled)"
        )
        return

    _, queued = await request_task_run(task, trigger="scheduled")
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
            await sync_linked_data(service_config.service_type)  # type: ignore


async def _ensure_main_media_server_for_task(task: Task) -> None:
    """Ensure tasks that require a main media server do not silently no-op."""
    if task not in TASK_RUNNER_MAIN_SERVER_PREFLIGHT_TASKS:
        return

    if service_manager.main_media_server is None:
        LOG.warning(
            f"{task.friendly_name()} requested but no main media server is loaded; "
            "reloading enabled service configurations before execution"
        )
        await load_enabled_services()

    if service_manager.main_media_server is None:
        raise RuntimeError(
            f"{task.friendly_name()} requires an enabled main media server, but "
            "none is currently available. Check the media server configuration, "
            "main-server selection, API key, URL, and service health."
        )


async def execute_task(task: Task) -> dict[str, Any] | None:
    if not await is_task_enabled(task):
        LOG.info(f"Skipped task execution for {task.friendly_name()} (disabled)")
        return None

    await _ensure_main_media_server_for_task(task)

    if task is Task.SYNC_MEDIA:
        return await sync_media()
    if task is Task.SYNC_MEDIA_LIBRARIES:
        return await sync_media_libraries()
    if task is Task.SYNC_LINKED_DATA:
        await _run_linked_data_sync()
        return None
    if task is Task.REFRESH_PLAYBACK_HISTORY:
        return await refresh_playback_history_task()
    if task is Task.RESYNC_MEDIA:
        await resync_media()
        return None
    if task is Task.SCAN_CLEANUP_CANDIDATES:
        await scan_cleanup_candidates()
        return None
    if task is Task.TAG_CLEANUP_CANDIDATES:
        await tag_cleanup_candidates()
        return None
    if task is Task.DELETE_CLEANUP_CANDIDATES:
        return await delete_cleanup_candidates()
    if task is Task.WEEKLY_HOUSE_KEEPING:
        await weekly_house_keeping()
        return None
    if task is Task.CHECK_APP_UPDATES:
        await check_app_updates()
        return None
    if task is Task.IMDB_RATINGS_REFRESH:
        await refresh_imdb_ratings()
        return None
    if task is Task.ANILIST_RATINGS_REFRESH:
        await refresh_anilist_ratings()
        return None
    if task is Task.MDBLIST_RATINGS_REFRESH:
        await refresh_mdblist_ratings()
        return None
    if task is Task.OMDB_RATINGS_REFRESH:
        await refresh_omdb_ratings()
        return None
    raise ValueError(f"Unsupported task for background execution: {task}")
