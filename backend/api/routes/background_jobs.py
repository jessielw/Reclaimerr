from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import get_current_user, has_permission, require_admin
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import BackgroundJob, User
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    CandidateFileOpOperation,
    Permission,
    UserRole,
)
from backend.jobs.policy import background_job_resources

router = APIRouter(prefix="/api/tasks", tags=["background-jobs"])


_REDACTED_PAYLOAD_KEYS = {"api_key", "password", "token", "secret"}


def _candidate_file_op_label_preview(payload: dict[str, Any]) -> str | None:
    raw_details = payload.get("item_details")
    if isinstance(raw_details, list):
        labels = [
            detail["display_label"]
            for detail in raw_details
            if isinstance(detail, dict) and isinstance(detail.get("display_label"), str)
        ]
    else:
        labels = []
    if not labels:
        raw_labels = payload.get("item_labels")
        labels = (
            [label for label in raw_labels if isinstance(label, str)]
            if isinstance(raw_labels, list)
            else []
        )
    if not labels:
        return None

    total_count = payload.get("item_label_total")
    label_total = total_count if isinstance(total_count, int) else len(labels)
    trimmed_labels = labels[:3]
    preview = ", ".join(trimmed_labels)
    remaining = max(0, label_total - len(trimmed_labels))
    if remaining > 0:
        preview = f"{preview}, +{remaining} more"
    return preview


def _candidate_file_op_summary(payload: dict[str, Any]) -> str:
    operation = payload.get("operation")
    if operation == CandidateFileOpOperation.MOVE:
        label = "Move candidates"
    else:
        label = "Delete candidates"

    candidate_ids = payload.get("candidate_ids")
    item_count = len(candidate_ids) if isinstance(candidate_ids, list) else None
    result = payload.get("result")
    if isinstance(result, dict):
        succeeded = result.get("succeeded")
        failed = result.get("failed")
        if isinstance(succeeded, int) and isinstance(failed, int):
            details = [f"{succeeded} succeeded"]
            if failed > 0:
                details.append(f"{failed} failed")
            preview = _candidate_file_op_label_preview(payload)
            suffix = f" - {preview}" if preview else ""
            return f"{label}: {', '.join(details)}{suffix}"

    if isinstance(item_count, int):
        preview = _candidate_file_op_label_preview(payload)
        count_part = f"{item_count} item{'s' if item_count != 1 else ''}"
        return f"{label}: {count_part}" + (f" - {preview}" if preview else "")
    return label


def _candidate_job_requested_by_user_id(job: BackgroundJob) -> int | None:
    raw_user_id = (job.payload or {}).get("requested_by_user_id")
    return raw_user_id if isinstance(raw_user_id, int) else None


def _can_view_candidate_file_op_job(user: User, job: BackgroundJob) -> bool:
    if user.role is UserRole.ADMIN:
        return True
    return (
        job.job_type is BackgroundJobType.CANDIDATE_FILE_OP
        and _candidate_job_requested_by_user_id(job) == user.id
    )


def _can_view_history_details(user: User) -> bool:
    return (
        user.role is UserRole.ADMIN
        or has_permission(user, Permission.MANAGE_RECLAIM)
        or has_permission(user, Permission.MANAGE_REQUESTS)
    )


def _allowed_history_job_types(user: User) -> tuple[BackgroundJobType, ...]:
    if user.role is UserRole.ADMIN:
        return tuple(BackgroundJobType)
    if _can_view_history_details(user):
        return (BackgroundJobType.CANDIDATE_FILE_OP,)
    return ()


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
    elif job.job_type is BackgroundJobType.CANDIDATE_FILE_OP:
        summary = _candidate_file_op_summary(payload)

    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "concurrency_resources": sorted(background_job_resources(job)),
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


@router.get("/history")
async def list_history(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    job_status: BackgroundJobStatus | None = Query(default=None, alias="status"),
    job_type: BackgroundJobType | None = Query(default=None),
) -> dict[str, Any]:
    """List detailed history activity visible to the current user with paging and optional filters."""
    allowed_job_types = _allowed_history_job_types(current_user)
    if not allowed_job_types or (
        job_type is not None and job_type not in allowed_job_types
    ):
        return {
            "items": [],
            "total": 0,
            "page": page,
            "per_page": per_page,
            "total_pages": 0,
        }

    query = select(BackgroundJob).where(BackgroundJob.job_type.in_(allowed_job_types))
    count_query = (
        select(func.count())
        .select_from(BackgroundJob)
        .where(BackgroundJob.job_type.in_(allowed_job_types))
    )

    if job_status is not None:
        query = query.where(BackgroundJob.status == job_status)
        count_query = count_query.where(BackgroundJob.status == job_status)
    if job_type is not None:
        query = query.where(BackgroundJob.job_type == job_type)
        count_query = count_query.where(BackgroundJob.job_type == job_type)

    total = int((await db.execute(count_query)).scalar_one() or 0)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 0
    jobs = (
        (
            await db.execute(
                query.order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        )
        .scalars()
        .all()
    )

    return {
        "items": [_serialize_background_job(job) for job in jobs],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


@router.get("/candidate-file-op-jobs")
async def list_candidate_file_op_jobs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, list[dict[str, Any]]]:
    """List candidate file-op jobs visible to the current user."""
    fetch_limit = max(limit * 5, limit)
    result = await db.execute(
        select(BackgroundJob)
        .where(BackgroundJob.job_type == BackgroundJobType.CANDIDATE_FILE_OP)
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
        .limit(fetch_limit)
    )
    jobs = [
        job
        for job in result.scalars().all()
        if _can_view_candidate_file_op_job(current_user, job)
    ][:limit]
    return {"jobs": [_serialize_background_job(job) for job in jobs]}


@router.get("/candidate-file-op-jobs/{job_id}")
async def get_candidate_file_op_job(
    job_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get a candidate file-op job if visible to the current user."""
    result = await db.execute(select(BackgroundJob).where(BackgroundJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None or job.job_type is not BackgroundJobType.CANDIDATE_FILE_OP:
        raise HTTPException(
            status_code=404, detail=f"Background job '{job_id}' not found"
        )
    if not _can_view_candidate_file_op_job(current_user, job):
        raise HTTPException(status_code=403, detail="Not permitted to view this job")
    return _serialize_background_job(job)


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
