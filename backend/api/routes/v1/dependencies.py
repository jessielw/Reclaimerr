from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import BackgroundJob, ReclaimCandidate
from backend.enums import BackgroundJobStatus, BackgroundJobType


async def candidate_or_404(db: AsyncSession, candidate_id: int) -> ReclaimCandidate:
    candidate = await db.get(ReclaimCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found"
        )
    return candidate


async def ensure_no_active_file_operation(
    db: AsyncSession, candidate_ids: int | list[int]
) -> None:
    requested_ids = (
        {candidate_ids} if isinstance(candidate_ids, int) else set(candidate_ids)
    )
    if not requested_ids:
        return
    jobs = (
        (
            await db.execute(
                select(BackgroundJob).where(
                    BackgroundJob.job_type == BackgroundJobType.CANDIDATE_FILE_OP,
                    BackgroundJob.status.in_(
                        [BackgroundJobStatus.PENDING, BackgroundJobStatus.RUNNING]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(
        requested_ids.intersection(job.payload.get("candidate_ids") or [])
        for job in jobs
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A delete or move operation is already pending for this media",
        )
