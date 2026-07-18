from __future__ import annotations

import asyncio
from collections.abc import Collection
from time import monotonic

from sqlalchemy import select

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import GeneralSettings
from backend.enums import BackgroundJobType
from backend.jobs.policy import background_job_resources
from backend.jobs.queue import (
    claim_next_background_job,
    complete_background_job,
    fail_background_job,
)
from backend.jobs.runner import run_background_job

# polling settings
DEFAULT_IDLE_POLL_MIN_SECONDS = 0.5
DEFAULT_IDLE_POLL_MAX_SECONDS = 5.0
IDLE_POLL_BACKOFF_FACTOR = 2.0
POLL_SETTINGS_REFRESH_SECONDS = 60.0


def start_worker_pool(
    worker_namespace: str, worker_count: int
) -> list[asyncio.Task[None]]:
    """Start the configured number of durable command executor loops."""
    return [
        asyncio.create_task(
            worker_loop(f"{worker_namespace}:command-{index}"),
            name=f"background-command-{index}",
        )
        for index in range(1, worker_count + 1)
    ]


async def _load_worker_poll_settings() -> tuple[float, float]:
    """Load worker polling settings from the database, applying defaults and constraints as needed."""
    poll_min_seconds = DEFAULT_IDLE_POLL_MIN_SECONDS
    poll_max_seconds = DEFAULT_IDLE_POLL_MAX_SECONDS

    async with async_db() as session:
        result = await session.execute(select(GeneralSettings))
        settings = result.scalars().first()

    if settings is not None:
        if settings.worker_poll_min_seconds is not None:
            poll_min_seconds = settings.worker_poll_min_seconds
        if settings.worker_poll_max_seconds is not None:
            poll_max_seconds = settings.worker_poll_max_seconds

    poll_max_seconds = max(poll_max_seconds, poll_min_seconds)
    return poll_min_seconds, poll_max_seconds


async def worker_loop(
    worker_id: str,
    *,
    allowed_job_types: Collection[BackgroundJobType] | None = None,
) -> None:
    """Job processing loop. Runs in-process as an asyncio task alongside the API server."""
    poll_min_seconds, poll_max_seconds = await _load_worker_poll_settings()
    idle_poll_delay = poll_min_seconds
    next_settings_refresh = monotonic() + POLL_SETTINGS_REFRESH_SECONDS

    while True:
        job = None
        try:
            if monotonic() >= next_settings_refresh:
                poll_min_seconds, poll_max_seconds = await _load_worker_poll_settings()
                idle_poll_delay = min(
                    max(idle_poll_delay, poll_min_seconds), poll_max_seconds
                )
                next_settings_refresh = monotonic() + POLL_SETTINGS_REFRESH_SECONDS

            job = await claim_next_background_job(
                worker_id,
                allowed_job_types=allowed_job_types,
            )
            if job is None:
                await asyncio.sleep(idle_poll_delay)
                idle_poll_delay = min(
                    idle_poll_delay * IDLE_POLL_BACKOFF_FACTOR,
                    poll_max_seconds,
                )
                continue

            idle_poll_delay = poll_min_seconds

            LOG.info(
                f"Worker {worker_id} running background job {job.id} "
                f"({job.job_type}, priority={job.priority}, "
                f"resources={sorted(background_job_resources(job))})"
            )
            result_payload = await run_background_job(job)
            await complete_background_job(job.id, result_payload=result_payload)
            LOG.info(f"Worker {worker_id} completed background job {job.id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if job is None:
                LOG.error(
                    f"Worker {worker_id} could not poll for background work: {exc}",
                    exc_info=True,
                )
                await asyncio.sleep(idle_poll_delay)
                idle_poll_delay = min(
                    idle_poll_delay * IDLE_POLL_BACKOFF_FACTOR,
                    poll_max_seconds,
                )
                continue
            LOG.error(
                f"Worker {worker_id} failed background job {job.id}: {exc}",
                exc_info=True,
            )
            try:
                await fail_background_job(job.id, str(exc))
            except Exception as persistence_exc:
                LOG.error(
                    f"Worker {worker_id} could not persist failure state for "
                    f"background job {job.id}: {persistence_exc}",
                    exc_info=True,
                )
            idle_poll_delay = poll_min_seconds
