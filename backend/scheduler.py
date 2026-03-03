from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import TaskSchedule
from backend.enums import ScheduleType, Task
from backend.tasks.cleanup import (
    delete_cleanup_candidates,
    scan_cleanup_candidates,
    tag_cleanup_candidates,
)
from backend.tasks.house_keeping import weekly_house_keeping
from backend.tasks.sync import (
    sync_jellyfin_media,
    sync_plex_media,
    sync_service_libraries,
)

scheduler = AsyncIOScheduler()


# map Task enum to their Python functions
TASK_FUNCTION_MAP = {
    Task.SYNC_PLEX_MEDIA: sync_plex_media,
    Task.SYNC_JELLYFIN_MEDIA: sync_jellyfin_media,
    Task.SYNC_SERVICE_LIBRARIES: sync_service_libraries,
    Task.SCAN_CLEANUP_CANDIDATES: scan_cleanup_candidates,
    Task.TAG_CLEANUP_CANDIDATES: tag_cleanup_candidates,
    # Task.DELETE_CLEANUP_CANDIDATES: delete_cleanup_candidates, # TODO: enable once delete task is ready and tested
    Task.WEEKLY_HOUSE_KEEPING: weekly_house_keeping,
}


async def ensure_default_schedules(db: AsyncSession) -> None:
    """Ensure default task schedules exist in database."""
    default_schedules = (
        {
            "task": Task.SYNC_PLEX_MEDIA,
            "description": "Synchronizes movies and series from Plex",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": "0 3 * * *",  # daily at 3 AM
            "default_schedule_type": ScheduleType.CRON,
            "default_schedule_value": "0 3 * * *",
            "enabled": True,
        },
        {
            "task": Task.SYNC_JELLYFIN_MEDIA,
            "description": "Synchronizes movies and series from Jellyfin",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": "0 4 * * *",  # daily at 4 AM
            "default_schedule_type": ScheduleType.CRON,
            "default_schedule_value": "0 4 * * *",
            "enabled": True,
        },
        {
            "task": Task.SYNC_SERVICE_LIBRARIES,
            "description": "Updates the library list from connected services",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": "0 6 * * *",  # daily at 6 AM
            "default_schedule_type": ScheduleType.CRON,
            "default_schedule_value": "0 6 * * *",
            "enabled": True,
        },
        {
            "task": Task.SCAN_CLEANUP_CANDIDATES,
            "description": "Identifies media that can be removed based on cleanup rules",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": "0 10 * * *",  # Daily at 10 AM
            "default_schedule_type": ScheduleType.CRON,
            "default_schedule_value": "0 10 * * *",
            "enabled": True,
        },
        {
            "task": Task.TAG_CLEANUP_CANDIDATES,
            "description": "Tags media identified as cleanup candidates for easier management",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": "0 12 * * *",  # Daily at 12 PM
            "default_schedule_type": ScheduleType.CRON,
            "default_schedule_value": "0 12 * * *",
            "enabled": True,
        },
        {
            "task": Task.WEEKLY_HOUSE_KEEPING,
            "description": "Performs weekly maintenance tasks to ensure system stability",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": "0 8 * * 0",  # Weekly on Sunday at 8 AM
            "default_schedule_type": ScheduleType.CRON,
            "default_schedule_value": "0 8 * * 0",
            "enabled": True,
        },
    )

    for schedule_data in default_schedules:
        # check if task schedule already exists
        result = await db.execute(
            select(TaskSchedule).where(TaskSchedule.task == schedule_data["task"])
        )
        existing = result.scalar_one_or_none()

        if not existing:
            task_schedule = TaskSchedule(**schedule_data)
            db.add(task_schedule)
            LOG.info(
                f"Created default schedule for {schedule_data['task'].friendly_name()}"
            )

    await db.commit()


async def setup_scheduler() -> None:
    """Configure and add scheduled tasks from database."""
    async with async_db() as db:
        # ensure default schedules exist
        await ensure_default_schedules(db)

        # load all enabled task schedules
        result = await db.execute(
            select(TaskSchedule).where(TaskSchedule.enabled.is_(True))
        )
        task_schedules = result.scalars().all()

        for task_schedule in task_schedules:
            task_func = TASK_FUNCTION_MAP.get(task_schedule.task)
            if not task_func:
                LOG.warning(f"No function found for task: {task_schedule.task}")
                continue

            # create trigger based on schedule type
            # interval
            if task_schedule.schedule_type is ScheduleType.INTERVAL:
                interval_seconds = int(task_schedule.schedule_value)
                trigger = IntervalTrigger(seconds=interval_seconds)
            else:  # CRON
                trigger = CronTrigger.from_crontab(task_schedule.schedule_value)

            # add job to scheduler (APScheduler still uses job terminology)
            scheduler.add_job(
                task_func,
                trigger,
                id=task_schedule.task.value,  # Use enum value as string ID
                name=task_schedule.task.friendly_name(),
                replace_existing=True,
            )
            LOG.info(f"Scheduled task: {task_schedule.task.friendly_name()}")

    LOG.info("Scheduler configured with tasks")


async def update_task_schedule(
    task: Task, schedule_type: ScheduleType, schedule_value: str, enabled: bool
) -> TaskSchedule:
    """Modify a task's schedule in the database and scheduler."""
    task_schedule = None
    async with async_db() as db:
        # update database
        result = await db.execute(select(TaskSchedule).where(TaskSchedule.task == task))
        task_schedule = result.scalar_one_or_none()

        if not task_schedule:
            raise ValueError(f"Task schedule not found: {task}")

        task_schedule.schedule_type = schedule_type
        task_schedule.schedule_value = schedule_value
        task_schedule.enabled = enabled
        await db.commit()

        # update scheduler
        if enabled:
            task_func = TASK_FUNCTION_MAP.get(task)
            if not task_func:
                raise ValueError(f"No function found for task: {task}")

            # create trigger
            # interval
            if schedule_type is ScheduleType.INTERVAL:
                interval_seconds = int(schedule_value)
                trigger = IntervalTrigger(seconds=interval_seconds)
            else:  # CRON
                trigger = CronTrigger.from_crontab(schedule_value)

            # update or add job
            scheduler.add_job(
                task_func,
                trigger,
                id=task,
                name=task.friendly_name(),
                replace_existing=True,
            )
            LOG.info(f"Updated task schedule: {task.friendly_name()}")
        else:
            # remove job from scheduler if disabled
            existing_job = scheduler.get_job(task)
            if existing_job:
                existing_job.remove()
                LOG.info(f"Removed disabled task: {task.friendly_name()}")

    return task_schedule


async def start_scheduler():
    """Start the scheduler."""
    await setup_scheduler()
    scheduler.start()
    LOG.info("Scheduler started")


async def shutdown_scheduler():
    """Gracefully shutdown the scheduler."""
    if scheduler.running:
        LOG.info("Shutting down scheduler...")
        # wait=True ensures running jobs complete (with timeout)
        # wait=False immediately stops all jobs
        scheduler.shutdown(wait=False)
        LOG.info("Scheduler stopped")
    else:
        LOG.info("Scheduler already stopped")
