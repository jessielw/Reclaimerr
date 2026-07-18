from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy import update as sql_update

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import BackgroundJob
from backend.enums import (
    BackgroundJobPriority,
    BackgroundJobStatus,
    BackgroundJobType,
)
from backend.jobs.policy import background_job_resources

_job_claim_lock = asyncio.Lock()


async def enqueue_background_job(
    job_type: BackgroundJobType,
    payload: dict[str, Any],
    *,
    scheduled_at: datetime | None = None,
    dedupe_key: str | None = None,
    replace_pending: bool = False,
    skip_if_active: bool = False,
    max_attempts: int = 3,
    priority: BackgroundJobPriority = BackgroundJobPriority.NORMAL,
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
            priority=priority,
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


async def claim_next_background_job(
    worker_id: str,
    *,
    allowed_job_types: Collection[BackgroundJobType] | None = None,
) -> BackgroundJob | None:
    """Claim the highest priority due job compatible with running work."""
    async with _job_claim_lock:
        now = datetime.now(UTC)
        async with async_db() as session:
            running_jobs = (
                (
                    await session.execute(
                        select(BackgroundJob).where(
                            BackgroundJob.status == BackgroundJobStatus.RUNNING
                        )
                    )
                )
                .scalars()
                .all()
            )
            busy_resources = {
                resource
                for running_job in running_jobs
                for resource in background_job_resources(running_job)
            }

            priority_rank = case(
                (BackgroundJob.priority == BackgroundJobPriority.HIGH, 3),
                (BackgroundJob.priority == BackgroundJobPriority.NORMAL, 2),
                (BackgroundJob.priority == BackgroundJobPriority.LOW, 1),
                else_=0,
            )
            query = (
                select(BackgroundJob)
                .where(
                    BackgroundJob.status == BackgroundJobStatus.PENDING,
                    BackgroundJob.scheduled_at <= now,
                )
                .order_by(
                    priority_rank.desc(),
                    BackgroundJob.scheduled_at.asc(),
                    BackgroundJob.id.asc(),
                )
            )
            if allowed_job_types is not None:
                allowed_values = tuple(allowed_job_types)
                if not allowed_values:
                    return None
                query = query.where(BackgroundJob.job_type.in_(allowed_values))

            pending_jobs = (await session.execute(query)).scalars().all()
            job = next(
                (
                    pending_job
                    for pending_job in pending_jobs
                    if not (background_job_resources(pending_job) & busy_resources)
                ),
                None,
            )
            if job is None:
                if pending_jobs and busy_resources:
                    LOG.debug(
                        "Due background jobs are waiting for resources held by "
                        f"running work: {sorted(busy_resources)}"
                    )
                return None

            claim_result = await session.execute(
                sql_update(BackgroundJob)
                .where(
                    BackgroundJob.id == job.id,
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


async def update_background_job_payload(
    job_id: int,
    payload_updates: dict[str, Any],
) -> None:
    """Merge payload updates into an existing background job without changing its status."""
    async with async_db() as session:
        payload_result = await session.execute(
            select(BackgroundJob.payload).where(BackgroundJob.id == job_id)
        )
        payload = payload_result.scalar_one_or_none() or {}
        payload = {**payload, **payload_updates}
        await session.execute(
            sql_update(BackgroundJob)
            .where(BackgroundJob.id == job_id)
            .values(
                payload=payload,
                updated_at=datetime.now(UTC),
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


async def cancel_pending_background_jobs_by_dedupe_key(
    dedupe_key: str, *, reason: str = "Canceled by configuration change"
) -> int:
    """Cancel all pending background jobs matching a dedupe key."""
    now = datetime.now(UTC)
    async with async_db() as session:
        result = await session.execute(
            sql_update(BackgroundJob)
            .where(
                BackgroundJob.dedupe_key == dedupe_key,
                BackgroundJob.status == BackgroundJobStatus.PENDING,
            )
            .values(
                status=BackgroundJobStatus.CANCELED,
                completed_at=now,
                error_message=reason,
            )
            .returning(BackgroundJob.id)
        )
        canceled_ids = result.scalars().all()
        await session.commit()

    if canceled_ids:
        LOG.info(
            f"Canceled {len(canceled_ids)} pending background job(s) for dedupe key {dedupe_key}"
        )
    return len(canceled_ids)
