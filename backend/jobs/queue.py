from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy import update as sql_update

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import BackgroundJob
from backend.enums import BackgroundJobStatus, BackgroundJobType


async def enqueue_background_job(
    job_type: BackgroundJobType,
    payload: dict[str, Any],
    *,
    scheduled_at: datetime | None = None,
    dedupe_key: str | None = None,
    replace_pending: bool = False,
    skip_if_active: bool = False,
    max_attempts: int = 3,
) -> BackgroundJob | None:
    """Enqueue a background job with optional deduplication and scheduling."""
    run_at = scheduled_at or datetime.now(UTC)
    async with async_db() as session:
        if replace_pending and dedupe_key:
            await session.execute(
                sql_update(BackgroundJob)
                .where(
                    BackgroundJob.dedupe_key == dedupe_key,
                    BackgroundJob.status == BackgroundJobStatus.PENDING,
                )
                .values(
                    status=BackgroundJobStatus.CANCELED,
                    completed_at=run_at,
                    error_message="Replaced by newer queued job",
                )
            )

        if skip_if_active and dedupe_key:
            existing_result = await session.execute(
                select(BackgroundJob)
                .where(
                    BackgroundJob.dedupe_key == dedupe_key,
                    or_(
                        BackgroundJob.status == BackgroundJobStatus.PENDING,
                        BackgroundJob.status == BackgroundJobStatus.RUNNING,
                    ),
                )
                .order_by(BackgroundJob.scheduled_at.desc(), BackgroundJob.id.desc())
                .limit(1)
            )
            existing_job = existing_result.scalar_one_or_none()
            if existing_job is not None:
                LOG.info(
                    f"Skipping enqueue for active background job {existing_job.id} ({existing_job.job_type})"
                )
                return None

        job = BackgroundJob(
            job_type=job_type,
            status=BackgroundJobStatus.PENDING,
            payload=payload,
            dedupe_key=dedupe_key,
            scheduled_at=run_at,
            max_attempts=max_attempts,
        )
        session.add(job)
        await session.flush()
        await session.commit()
        await session.refresh(job)

    LOG.info(f"Enqueued background job {job.id} ({job.job_type})")
    return job


async def reset_stale_jobs() -> int:
    """
    Mark any RUNNING jobs as FAILED on startup.

    Because the worker runs in process, any job still in RUNNING state at
    startup is guaranteed to be from a previous process that was killed before
    it could complete or fail the job.  Leaving them as RUNNING would block the
    the new job from running again.
    """
    now = datetime.now(UTC)
    async with async_db() as session:
        result = await session.execute(
            sql_update(BackgroundJob)
            .where(BackgroundJob.status == BackgroundJobStatus.RUNNING)
            .values(
                status=BackgroundJobStatus.FAILED,
                completed_at=now,
                error_message="Server restarted while job was running",
            )
            .returning(BackgroundJob.id)
        )
        stale_ids = result.scalars().all()
        await session.commit()

    if stale_ids:
        LOG.warning(
            f"Reset {len(stale_ids)} stale RUNNING job(s) to FAILED on startup: {stale_ids}"
        )
    return len(stale_ids)


async def claim_next_background_job(worker_id: str) -> BackgroundJob | None:
    """Claim the next available background job for processing, marking it as RUNNING."""
    now = datetime.now(UTC)
    async with async_db() as session:
        result = await session.execute(
            select(BackgroundJob.id)
            .where(
                BackgroundJob.status == BackgroundJobStatus.PENDING,
                BackgroundJob.scheduled_at <= now,
            )
            .order_by(BackgroundJob.scheduled_at.asc(), BackgroundJob.id.asc())
            .limit(1)
        )
        job_id = result.scalar_one_or_none()
        if job_id is None:
            return None

        claim_result = await session.execute(
            sql_update(BackgroundJob)
            .where(
                BackgroundJob.id == job_id,
                BackgroundJob.status == BackgroundJobStatus.PENDING,
            )
            .values(
                status=BackgroundJobStatus.RUNNING,
                claimed_by=worker_id,
                claimed_at=now,
                started_at=now,
                attempts=BackgroundJob.attempts + 1,
            )
            .returning(BackgroundJob.id)
        )
        claimed_id = claim_result.scalar_one_or_none()
        if claimed_id is None:
            await session.rollback()
            return None

        await session.commit()
        claimed = await session.execute(
            select(BackgroundJob).where(BackgroundJob.id == claimed_id)
        )
        return claimed.scalar_one_or_none()


async def complete_background_job(
    job_id: int, result_payload: dict[str, Any] | None = None
) -> None:
    """Mark a background job as completed."""
    async with async_db() as session:
        payload_result = await session.execute(
            select(BackgroundJob.payload).where(BackgroundJob.id == job_id)
        )
        payload = payload_result.scalar_one_or_none() or {}
        if result_payload is not None:
            payload = {**payload, "result": result_payload}

        await session.execute(
            sql_update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(
                status=BackgroundJobStatus.COMPLETED,
                completed_at=datetime.now(UTC),
                error_message=None,
                payload=payload,
            )
        )
        await session.commit()


async def fail_background_job(job_id: int, error_message: str) -> None:
    """Mark a background job as failed with an error message."""
    async with async_db() as session:
        await session.execute(
            sql_update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(
                status=BackgroundJobStatus.FAILED,
                completed_at=datetime.now(UTC),
                error_message=error_message,
            )
        )
        await session.commit()
