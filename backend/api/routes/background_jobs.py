from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import require_admin
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import BackgroundJob, User
from backend.enums import BackgroundJobStatus, BackgroundJobType

router = APIRouter(prefix="/api/tasks", tags=["background-jobs"])


_REDACTED_PAYLOAD_KEYS = {"api_key", "password", "token", "secret"}


def _serialize_background_job(job: BackgroundJob) -> dict[str, Any]:
    """Serialize a BackgroundJob instance into a dictionary for API responses."""
    raw_payload = job.payload or {}
    payload = {
        k: "***" if k in _REDACTED_PAYLOAD_KEYS else v for k, v in raw_payload.items()
    }
    summary: str | None = None

    if job.job_type is BackgroundJobType.TASK_RUN:
        task_name = payload.get("task")
        summary = f"Task run: {task_name}" if task_name else "Task run"
    elif job.job_type is BackgroundJobType.SERVICE_TOGGLE:
        service_type = payload.get("service_type")
        enabled = payload.get("enabled")
        if service_type is not None and enabled is not None:
            action = "enable" if enabled else "disable"
            summary = f"Service toggle: {action} {service_type}"
        elif service_type is not None:
            summary = f"Service toggle: {service_type}"

    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "summary": summary,
        "dedupe_key": job.dedupe_key,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "claimed_by": job.claimed_by,
        "claimed_at": to_utc_isoformat(job.claimed_at),
        "scheduled_at": to_utc_isoformat(job.scheduled_at),
        "started_at": to_utc_isoformat(job.started_at),
        "completed_at": to_utc_isoformat(job.completed_at),
        "error_message": job.error_message,
        "created_at": to_utc_isoformat(job.created_at),
        "updated_at": to_utc_isoformat(job.updated_at),
        "payload": payload,
    }


@router.get("/background-jobs")
async def list_background_jobs(
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    status: BackgroundJobStatus | None = Query(default=None),
    job_type: BackgroundJobType | None = Query(default=None),
) -> dict[str, list[dict[str, Any]]]:
    """List background jobs with optional filtering by status and job type."""
    query = select(BackgroundJob)

    if status is not None:
        query = query.where(BackgroundJob.status == status)
    if job_type is not None:
        query = query.where(BackgroundJob.job_type == job_type)

    query = query.order_by(
        BackgroundJob.created_at.desc(), BackgroundJob.id.desc()
    ).limit(limit)
    result = await db.execute(query)
    jobs = result.scalars().all()

    return {"jobs": [_serialize_background_job(job) for job in jobs]}


@router.get("/background-jobs/{job_id}")
async def get_background_job(
    job_id: int,
    _admin: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get details of a specific background job by its ID."""
    result = await db.execute(select(BackgroundJob).where(BackgroundJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=404, detail=f"Background job '{job_id}' not found"
        )

    return _serialize_background_job(job)
