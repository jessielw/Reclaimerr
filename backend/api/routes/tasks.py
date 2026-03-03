from __future__ import annotations

from datetime import datetime
from typing import Annotated

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import get_current_user, require_admin
from backend.core.logger import LOG
from backend.database import get_db
from backend.database.models import ReclaimRule, TaskRun, TaskSchedule, User
from backend.enums import MediaType, ScheduleType, Task, TaskStatus
from backend.models.tasks import TaskScheduleRequest

# from backend.models.cleanup import CleanupRuleReq
from backend.scheduler import scheduler, update_task_schedule
from backend.tasks.cleanup import (
    _reset_seerr_request,
    delete_cleanup_candidates,
    scan_cleanup_candidates,
    tag_cleanup_candidates,
)
from backend.tasks.sync import sync_movies, sync_series
from backend.tasks.task_tracker import get_task_status

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# @router.post("/test-auth")
# async def test_auth_task(current_user: Annotated[User, Depends(get_current_user)]):
#     return {"message": f"Hello {current_user.username}"}


# @router.post("/test")
# async def test_task():
#     # await _reset_seerr_request(10378, MediaType.MOVIE)
#     try:
#         # Queue the task to run immediately in the background
#         job = scheduler.add_job(
#             id=f"test_job",
#             name="Test Job",
#             replace_existing=False,
#         )
#         return {
#             "status": "queued",
#             "message": "Test job queued",
#             "job_id": job.id,
#         }
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))


# @router.post("/test-cleanup-rule")
# async def create_test_cleanup_rule(
#     request: CleanupRuleReq,
#     db: AsyncSession = Depends(get_db),
# ):
#     """Create a test cleanup rule and optionally trigger a scan."""
#     try:
#         # create the cleanup rule
#         cleanup_rule = ReclaimRule(
#             name=request.name,
#             media_type=request.media_type,
#             enabled=True,
#             library_names=request.library_names,
#             min_popularity=request.min_popularity,
#             max_popularity=request.max_popularity,
#             min_vote_average=request.min_vote_average,
#             max_vote_average=request.max_vote_average,
#             min_vote_count=request.min_vote_count,
#             max_vote_count=request.max_vote_count,
#             min_view_count=request.min_view_count,
#             max_view_count=request.max_view_count,
#             include_never_watched=request.include_never_watched
#             if request.include_never_watched is not None
#             else False,
#             min_days_since_added=request.min_days_since_added,
#             max_days_since_added=request.max_days_since_added,
#             min_size=request.min_size,
#             max_size=request.max_size,
#         )
#         db.add(cleanup_rule)
#         await db.commit()
#         await db.refresh(cleanup_rule)

#         logger.info(
#             f"Created test cleanup rule: {cleanup_rule.name} (ID: {cleanup_rule.id})"
#         )

#         # queue cleanup scan
#         job = scheduler.add_job(
#             scan_cleanup_candidates,
#             id=f"test_cleanup_scan_{datetime.now().timestamp()}",
#             name="Test cleanup scan",
#             replace_existing=False,
#         )

#         return {
#             "status": "success",
#             "rule_id": cleanup_rule.id,
#             "rule_name": cleanup_rule.name,
#             "scan_job_id": job.id,
#             "message": "Test cleanup rule created and scan queued",
#         }
#     except Exception as e:
#         logger.error(f"Error creating test cleanup rule: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))


# @router.post("/scan-cleanup")
# async def trigger_cleanup_scan():
#     """
#     Manually trigger a cleanup candidates scan.
#     Queues the task to run in the background without blocking the API response.
#     """
#     try:
#         # Queue the task to run immediately in the background
#         job = scheduler.add_job(
#             scan_cleanup_candidates,
#             id=f"manual_cleanup_scan_{datetime.now().timestamp()}",
#             name="Manual cleanup scan",
#             replace_existing=False,
#         )
#         return {
#             "status": "queued",
#             "message": "Cleanup scan queued",
#             "job_id": job.id,
#         }
#     except Exception as e:
#         LOG.error(f"Error queuing cleanup scan: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))


# @router.post("/sync-watch-history")
# async def trigger_watch_sync():
#     """
#     Manually trigger watch history synchronization.
#     Queues the task to run in the background without blocking the API response.
#     """
#     try:
#         job = scheduler.add_job(
#             sync_watch_history,
#             id=f"manual_watch_sync_{datetime.now().timestamp()}",
#             name="Manual watch history sync",
#             replace_existing=False,
#         )
#         return {
#             "status": "queued",
#             "message": "Watch history sync queued",
#             "job_id": job.id,
#         }
#     except Exception as e:
#         logger.error(f"Error queuing watch history sync: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))


# @router.post("/sync-libraries")
# async def trigger_library_sync():
#     """
#     Manually trigger media library synchronization.
#     Queues the task to run in the background without blocking the API response.
#     """
#     try:
#         job = scheduler.add_job(
#             sync_media_libraries,
#             id=f"manual_library_sync_{datetime.now().timestamp()}",
#             name="Manual library sync",
#             replace_existing=False,
#         )
#         return {
#             "status": "queued",
#             "message": "Library sync queued",
#             "job_id": job.id,
#         }
#     except Exception as e:
#         logger.error(f"Error queuing library sync: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))


# @router.post("/auto-tag")
# async def trigger_auto_tag():
#     """
#     Manually trigger auto-tagging of cleanup candidates.
#     Queues the task to run in the background without blocking the API response.
#     """
#     try:
#         job = scheduler.add_job(
#             auto_tag_candidates,
#             id=f"manual_auto_tag_{datetime.now().timestamp()}",
#             name="Manual auto-tag",
#             replace_existing=False,
#         )
#         return {
#             "status": "queued",
#             "message": "Auto-tagging queued",
#             "job_id": job.id,
#         }
#     except Exception as e:
#         logger.error(f"Error queuing auto-tag: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))


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


@router.get("/tasks")
async def list_tasks(
    _admin: Annotated[User, Depends(require_admin)], db: AsyncSession = Depends(get_db)
) -> dict[str, list[dict]]:
    """
    List all scheduled jobs and their next run times.
    Includes both recurring scheduled jobs and one-time manual jobs.
    """
    # get all task schedules from database
    result = await db.execute(select(TaskSchedule))
    db_schedules = {schedule.task: schedule for schedule in result.scalars().all()}

    tasks = []
    for task in scheduler.get_jobs():
        next_run = None
        if task.next_run_time:
            next_run = task.next_run_time.isoformat()

        # get database configuration
        db_schedule = db_schedules.get(task.id)
        schedule_type = db_schedule.schedule_type if db_schedule else None
        schedule_value = db_schedule.schedule_value if db_schedule else None
        default_schedule_type = (
            db_schedule.default_schedule_type if db_schedule else None
        )
        default_schedule_value = (
            db_schedule.default_schedule_value if db_schedule else None
        )
        description = db_schedule.description if db_schedule else None
        # default to True for non-DB tasks
        enabled = db_schedule.enabled if db_schedule else True
        task_schedule_id = db_schedule.id if db_schedule else None

        # get status from in-memory tracker (with TTL for completed/failed)
        status_str, error = (
            get_task_status(db_schedule.task)
            if db_schedule
            else (TaskStatus.SCHEDULED.value, None)
        )
        status = TaskStatus(status_str) if status_str else TaskStatus.SCHEDULED

        # get last run timestamp from DB for display purposes only
        last_task_run = await _get_last_task_run(db, task.id, task_schedule_id)
        last_run = None
        if last_task_run and last_task_run.completed_at:
            last_run = last_task_run.completed_at.isoformat()

        # determine trigger type and details
        trigger_type = None
        if isinstance(task.trigger, IntervalTrigger):
            trigger_type = ScheduleType.INTERVAL
        elif isinstance(task.trigger, CronTrigger):
            trigger_type = ScheduleType.CRON

        if trigger_type is None:
            raise HTTPException(
                status_code=500, detail=f"Unsupported trigger type for task '{task.id}'"
            )

        tasks.append(
            {
                "id": task.id,
                "name": task.name,
                "description": description,
                "next_run": next_run,
                "last_run": last_run,
                "status": status,
                "error": error,
                "trigger_type": trigger_type,
                "schedule_type": schedule_type,
                "schedule_value": schedule_value,
                "default_schedule_type": default_schedule_type,
                "default_schedule_value": default_schedule_value,
                "enabled": enabled,
                "editable": db_schedule
                is not None,  # Only DB-configured jobs are editable
            }
        )

    # also add disabled tasks from database that aren't in scheduler
    for task_id_str, db_schedule in db_schedules.items():
        if not db_schedule.enabled and not any(t["id"] == task_id_str for t in tasks):
            # get last execution from TaskRun table
            last_task_run = await _get_last_task_run(db, task_id_str, db_schedule.id)
            last_run = None
            if last_task_run and last_task_run.completed_at:
                last_run = last_task_run.completed_at.isoformat()

            tasks.append(
                {
                    "id": task_id_str,
                    "name": db_schedule.task.friendly_name(),
                    "description": db_schedule.description,
                    "next_run": None,
                    "last_run": last_run,
                    "status": TaskStatus.DISABLED,
                    "error": None,
                    "trigger_type": db_schedule.schedule_type.value,
                    "schedule_type": db_schedule.schedule_type.value,
                    "schedule_value": db_schedule.schedule_value,
                    "default_schedule_type": db_schedule.default_schedule_type.value,
                    "default_schedule_value": db_schedule.default_schedule_value,
                    "enabled": False,
                    "editable": True,
                }
            )

    return {"tasks": tasks}


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
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    # get status from in-memory tracker (with TTL for completed/failed)
    task_enum = Task(task_id)
    status_str, error = get_task_status(task_enum)
    status = TaskStatus(status_str) if status_str else TaskStatus.SCHEDULED

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
        last_run = last_task_run.completed_at.isoformat()

    return {
        "id": task.id,
        "name": task.name,
        "next_run": task.next_run_time.isoformat() if task.next_run_time else None,
        "last_run": last_run,
        "status": status,
        "error": error,
        "trigger": str(task.trigger),
        "pending": task.pending,
    }


@router.post("/tasks/{task_id}/run")
async def run_task_now(task_id: str, _admin: Annotated[User, Depends(require_admin)]):
    """
    Trigger a scheduled job to run immediately (doesn't affect schedule).
    """
    task = scheduler.get_job(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    try:
        # trigger the task to run now (in addition to its regular schedule)
        task.modify(next_run_time=datetime.now())
        return {
            "status": "success",
            "message": f"Task '{task_id}' scheduled to run immediately",
        }
    except Exception as e:
        LOG.error(f"Error running task {task_id}: {e}", exc_info=True)
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
