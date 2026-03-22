from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_runtime import MAIN_SERVER_REQUIRED_TASKS, enqueue_scheduled_task
from backend.database import async_db
from backend.database.models import TaskSchedule
from backend.enums import ScheduleType, Task

scheduler = AsyncIOScheduler()


DEFAULT_SCHEDULES = (
    {
        "task": Task.SYNC_MEDIA,
        "description": (
            "Synchronizes libraries, movies and series from the main media server, "
            "plus linked watch data from non main servers"
        ),
        "schedule_type": ScheduleType.CRON,
        "schedule_value": "0 3 * * *",  # daily at 3 AM
        "default_schedule_type": ScheduleType.CRON,
        "default_schedule_value": "0 3 * * *",
        "enabled": True,
    },
    {
        "task": Task.SYNC_MEDIA_LIBRARIES,
        "description": "Updates the library list from connected services",
        "schedule_type": ScheduleType.MANUAL,
        "schedule_value": "",
        "default_schedule_type": ScheduleType.MANUAL,
        "default_schedule_value": "",
        "enabled": True,
    },
    {
        "task": Task.SYNC_LINKED_DATA,
        "description": "Updates linked data from connected services",
        "schedule_type": ScheduleType.MANUAL,
        "schedule_value": "",
        "default_schedule_type": ScheduleType.MANUAL,
        "default_schedule_value": "",
        "enabled": True,
    },
    {
        "task": Task.RESYNC_MEDIA,
        "description": (
            "Resynchronizes media from connected media servers (deletes and "
            "re-adds all media, used for fixing sync issues)"
        ),
        "schedule_type": ScheduleType.MANUAL,
        "schedule_value": "",
        "default_schedule_type": ScheduleType.MANUAL,
        "default_schedule_value": "",
        "enabled": False,
    },
    {
        "task": Task.SCAN_CLEANUP_CANDIDATES,
        "description": "Identifies media that can be removed based on cleanup rules",
        "schedule_type": ScheduleType.CRON,
        "schedule_value": "0 10 * * *",  # daily at 10 AM
        "default_schedule_type": ScheduleType.CRON,
        "default_schedule_value": "0 10 * * *",
        "enabled": True,
    },
    {
        "task": Task.TAG_CLEANUP_CANDIDATES,
        "description": "Tags media identified as cleanup candidates for easier management",
        "schedule_type": ScheduleType.CRON,
        "schedule_value": "0 12 * * *",  # daily at 12 PM
        "default_schedule_type": ScheduleType.CRON,
        "default_schedule_value": "0 12 * * *",
        "enabled": True,
    },
    {
        "task": Task.WEEKLY_HOUSE_KEEPING,
        "description": "Performs weekly maintenance tasks to ensure system stability",
        "schedule_type": ScheduleType.CRON,
        "schedule_value": "0 8 * * 0",  # weekly on Sunday at 8 AM
        "default_schedule_type": ScheduleType.CRON,
        "default_schedule_value": "0 8 * * 0",
        "enabled": True,
    },
)


async def ensure_default_schedules(db: AsyncSession) -> None:
    """Ensure default task schedules exist in database updating existing ones if necessary."""
    # fetch all existing schedules in one query
    all_schedules = (await db.execute(select(TaskSchedule))).scalars().all()
    schedule_map = {s.task: s for s in all_schedules}

    for default in DEFAULT_SCHEDULES:
        task = default["task"]
        existing = schedule_map.get(task)
        if existing:
            updated = False
            if existing.description != default["description"]:
                existing.description = default["description"]
                updated = True
            if existing.schedule_type != default["default_schedule_type"]:
                existing.schedule_type = default["default_schedule_type"]
                updated = True
            if existing.schedule_value != default["default_schedule_value"]:
                existing.schedule_value = default["default_schedule_value"]
                updated = True
            # do not override enabled
            if updated:
                db.add(existing)
                LOG.info(
                    f"Updated existing schedule for {task.friendly_name()} with default values"
                )
        else:
            task_schedule = TaskSchedule(**default)
            db.add(task_schedule)
            LOG.info(f"Created default schedule for {task.friendly_name()}")

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
            # create trigger based on schedule type
            if task_schedule.schedule_type is ScheduleType.MANUAL:
                continue  # manual tasks are not scheduled

            # skip tasks that require a main media server if none is configured
            if (
                task_schedule.task in MAIN_SERVER_REQUIRED_TASKS
                and not service_manager.main_media_server
            ):
                LOG.info(
                    f"Skipping {task_schedule.task.friendly_name()} - no main media server configured"
                )
                continue

            elif task_schedule.schedule_type is ScheduleType.INTERVAL:  # interval
                interval_seconds = int(task_schedule.schedule_value)
                trigger = IntervalTrigger(seconds=interval_seconds)
            else:  # CRON
                trigger = CronTrigger.from_crontab(task_schedule.schedule_value)

            # add job to scheduler (APScheduler still uses job terminology)
            scheduler.add_job(
                enqueue_scheduled_task,
                trigger,
                args=[task_schedule.task],
                id=str(task_schedule.task),
                name=task_schedule.task.friendly_name(),
                replace_existing=True,
            )
            LOG.info(f"Scheduled task: {task_schedule.task.friendly_name()}")

    LOG.info("Scheduler configured with tasks")


async def refresh_main_server_tasks() -> None:
    """Add or remove main-server-dependent tasks from the scheduler based on current state."""
    has_main = service_manager.main_media_server is not None
    async with async_db() as db:
        for task in MAIN_SERVER_REQUIRED_TASKS:
            result = await db.execute(
                select(TaskSchedule).where(TaskSchedule.task == task)
            )
            db_schedule = result.scalar_one_or_none()
            if (
                not db_schedule
                or not db_schedule.enabled
                or db_schedule.schedule_type is ScheduleType.MANUAL
            ):
                continue

            job = scheduler.get_job(task.value)
            if has_main and not job:
                if db_schedule.schedule_type is ScheduleType.INTERVAL:
                    trigger = IntervalTrigger(seconds=int(db_schedule.schedule_value))
                else:
                    trigger = CronTrigger.from_crontab(db_schedule.schedule_value)
                scheduler.add_job(
                    enqueue_scheduled_task,
                    trigger,
                    args=[task],
                    id=task.value,
                    name=task.friendly_name(),
                    replace_existing=True,
                )
                LOG.info(
                    f"Scheduled {task.friendly_name()} (main server now configured)"
                )
            elif not has_main and job:
                job.remove()
                LOG.info(
                    f"Unscheduled {task.friendly_name()} (no main server configured)"
                )


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
            # create trigger
            if schedule_type is ScheduleType.MANUAL:
                return task_schedule
            elif schedule_type is ScheduleType.INTERVAL:  # interval
                interval_seconds = int(schedule_value)
                trigger = IntervalTrigger(seconds=interval_seconds)
            else:  # CRON
                trigger = CronTrigger.from_crontab(schedule_value)

            # update or add job
            scheduler.add_job(
                enqueue_scheduled_task,
                trigger,
                args=[task],
                id=str(task),
                name=task.friendly_name(),
                replace_existing=True,
            )
            LOG.info(f"Updated task schedule: {task.friendly_name()}")
        else:
            # remove job from scheduler if disabled
            existing_job = scheduler.get_job(task.value)
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
