from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from backend.scheduler import scheduler
from backend.tasks.cleanup import auto_tag_candidates, scan_cleanup_candidates
from backend.tasks.sync import sync_with_media_servers

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("/test")
async def test_task():
    try:
        # Queue the task to run immediately in the background
        job = scheduler.add_job(
            sync_with_media_servers,
            id=f"test_job",
            name="Test Job",
            replace_existing=False,
        )
        return {
            "status": "queued",
            "message": "Test job queued",
            "job_id": job.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
#         logger.error(f"Error queuing cleanup scan: {e}", exc_info=True)
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


# @router.get("/jobs")
# async def list_scheduled_jobs():
#     """
#     List all scheduled jobs and their next run times.
#     Includes both recurring scheduled jobs and one-time manual jobs.
#     """
#     jobs = []
#     for job in scheduler.get_jobs():
#         next_run = None
#         if job.next_run_time:
#             next_run = job.next_run_time.isoformat()

#         jobs.append(
#             {
#                 "id": job.id,
#                 "name": job.name,
#                 "next_run": next_run,
#                 "trigger": str(job.trigger),
#             }
#         )
#     return {"jobs": jobs}


# @router.get("/jobs/{job_id}")
# async def get_job_status(job_id: str):
#     """
#     Get the status of a specific job.
#     """
#     job = scheduler.get_job(job_id)
#     if not job:
#         raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

#     return {
#         "id": job.id,
#         "name": job.name,
#         "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
#         "trigger": str(job.trigger),
#         "pending": job.pending,
#     }


# @router.post("/jobs/{job_id}/run")
# async def run_job_now(job_id: str):
#     """
#     Trigger a scheduled job to run immediately (doesn't affect schedule).
#     """
#     job = scheduler.get_job(job_id)
#     if not job:
#         raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

#     try:
#         # Trigger the job to run now (in addition to its regular schedule)
#         job.modify(next_run_time=datetime.now())
#         return {
#             "status": "success",
#             "message": f"Job '{job_id}' scheduled to run immediately",
#         }
#     except Exception as e:
#         logger.error(f"Error running job {job_id}: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=str(e))


# @router.post("/jobs/{job_id}/pause")
# async def pause_job(job_id: str):
#     """
#     Pause a scheduled job.
#     """
#     job = scheduler.get_job(job_id)
#     if not job:
#         raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

#     job.pause()
#     return {"status": "success", "message": f"Job '{job_id}' paused"}


# @router.post("/jobs/{job_id}/resume")
# async def resume_job(job_id: str):
#     """
#     Resume a paused scheduled job.
#     """
#     job = scheduler.get_job(job_id)
#     if not job:
#         raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

#     job.resume()
#     return {"status": "success", "message": f"Job '{job_id}' resumed"}
