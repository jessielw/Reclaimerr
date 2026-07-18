from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core import task_runtime
from backend.core import worker as worker_module
from backend.database import Base
from backend.database.models import BackgroundJob
from backend.enums import (
    BackgroundJobPriority,
    BackgroundJobStatus,
    BackgroundJobType,
    CandidateFileOpOperation,
    Task,
)
from backend.jobs import queue as job_queue
from backend.jobs.policy import (
    CANDIDATE_WORKFLOW_RESOURCE,
    SERVICE_RUNTIME_RESOURCE,
    TASK_RUN_RESOURCE,
    background_job_resources,
)


async def _session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


def _job(
    job_type: BackgroundJobType,
    payload: dict[str, object],
    *,
    status: BackgroundJobStatus = BackgroundJobStatus.PENDING,
    priority: BackgroundJobPriority = BackgroundJobPriority.NORMAL,
    scheduled_offset: timedelta = timedelta(),
) -> BackgroundJob:
    return BackgroundJob(
        job_type=job_type,
        payload=payload,
        status=status,
        priority=priority,
        scheduled_at=datetime.now(UTC) + scheduled_offset,
    )


def _patch_queue(monkeypatch, session_maker) -> None:
    monkeypatch.setattr(job_queue, "async_db", session_maker)
    monkeypatch.setattr(job_queue, "_job_claim_lock", asyncio.Lock())


def test_job_resources_encode_domain_conflicts() -> None:
    sync_job = _job(
        BackgroundJobType.TASK_RUN,
        {"task": Task.SYNC_MEDIA.value},
    )
    scan_job = _job(
        BackgroundJobType.TASK_RUN,
        {"task": Task.SCAN_CLEANUP_CANDIDATES.value},
    )
    housekeeping_job = _job(
        BackgroundJobType.TASK_RUN,
        {"task": Task.WEEKLY_HOUSE_KEEPING.value},
    )
    file_job = _job(
        BackgroundJobType.CANDIDATE_FILE_OP,
        {"operation": CandidateFileOpOperation.DELETE.value},
    )
    webhook_job = _job(
        BackgroundJobType.WEBHOOK_DELIVERY,
        {"delivery_id": 10, "endpoint_id": 4},
    )

    assert background_job_resources(sync_job) == frozenset(
        {TASK_RUN_RESOURCE, SERVICE_RUNTIME_RESOURCE}
    )
    assert background_job_resources(scan_job) == frozenset(
        {
            TASK_RUN_RESOURCE,
            SERVICE_RUNTIME_RESOURCE,
            CANDIDATE_WORKFLOW_RESOURCE,
        }
    )
    assert background_job_resources(housekeeping_job) == frozenset({TASK_RUN_RESOURCE})
    assert background_job_resources(file_job) == frozenset(
        {CANDIDATE_WORKFLOW_RESOURCE, SERVICE_RUNTIME_RESOURCE}
    )
    assert background_job_resources(webhook_job) == frozenset({"webhook-endpoint:4"})


def test_claim_prefers_priority_then_due_order(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        _patch_queue(monkeypatch, session_maker)
        async with session_maker() as db:
            low = _job(
                BackgroundJobType.WEBHOOK_DELIVERY,
                {"delivery_id": 1, "endpoint_id": 1},
                priority=BackgroundJobPriority.LOW,
                scheduled_offset=timedelta(minutes=-2),
            )
            high = _job(
                BackgroundJobType.WEBHOOK_DELIVERY,
                {"delivery_id": 2, "endpoint_id": 2},
                priority=BackgroundJobPriority.HIGH,
                scheduled_offset=timedelta(minutes=-1),
            )
            db.add_all([low, high])
            await db.commit()

        claimed = await job_queue.claim_next_background_job("worker-1")
        assert claimed is not None
        assert claimed.id == high.id
        assert claimed.priority is BackgroundJobPriority.HIGH
        await engine.dispose()

    asyncio.run(run())


def test_claim_skips_blocked_work_for_compatible_job(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        _patch_queue(monkeypatch, session_maker)
        async with session_maker() as db:
            running_file_op = _job(
                BackgroundJobType.CANDIDATE_FILE_OP,
                {"operation": CandidateFileOpOperation.DELETE.value},
                status=BackgroundJobStatus.RUNNING,
            )
            blocked_scan = _job(
                BackgroundJobType.TASK_RUN,
                {"task": Task.SCAN_CLEANUP_CANDIDATES.value},
                priority=BackgroundJobPriority.HIGH,
            )
            compatible_task = _job(
                BackgroundJobType.TASK_RUN,
                {"task": Task.CHECK_APP_UPDATES.value},
            )
            db.add_all([running_file_op, blocked_scan, compatible_task])
            await db.commit()

        claimed = await job_queue.claim_next_background_job("worker-1")
        assert claimed is not None
        assert claimed.id == compatible_task.id
        await engine.dispose()

    asyncio.run(run())


def test_service_sync_blocks_service_toggle_but_not_webhook(monkeypatch) -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        _patch_queue(monkeypatch, session_maker)
        async with session_maker() as db:
            running_sync = _job(
                BackgroundJobType.TASK_RUN,
                {"task": Task.SYNC_MEDIA.value},
                status=BackgroundJobStatus.RUNNING,
            )
            blocked_toggle = _job(
                BackgroundJobType.SERVICE_TOGGLE,
                {"service_config_id": 1},
                priority=BackgroundJobPriority.HIGH,
            )
            webhook = _job(
                BackgroundJobType.WEBHOOK_DELIVERY,
                {"delivery_id": 1, "endpoint_id": 7},
            )
            db.add_all([running_sync, blocked_toggle, webhook])
            await db.commit()

        claimed = await job_queue.claim_next_background_job("worker-1")
        assert claimed is not None
        assert claimed.id == webhook.id
        await engine.dispose()

    asyncio.run(run())


def test_webhooks_are_serial_per_endpoint_and_parallel_across_endpoints(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        _patch_queue(monkeypatch, session_maker)
        async with session_maker() as db:
            first = _job(
                BackgroundJobType.WEBHOOK_DELIVERY,
                {"delivery_id": 1, "endpoint_id": 3},
            )
            same_endpoint = _job(
                BackgroundJobType.WEBHOOK_DELIVERY,
                {"delivery_id": 2, "endpoint_id": 3},
            )
            other_endpoint = _job(
                BackgroundJobType.WEBHOOK_DELIVERY,
                {"delivery_id": 3, "endpoint_id": 4},
            )
            db.add_all([first, same_endpoint, other_endpoint])
            await db.commit()

        claimed = await asyncio.gather(
            job_queue.claim_next_background_job("worker-1"),
            job_queue.claim_next_background_job("worker-2"),
        )
        claimed_ids = {job.id for job in claimed if job is not None}
        assert claimed_ids == {first.id, other_endpoint.id}
        assert same_endpoint.id not in claimed_ids
        await engine.dispose()

    asyncio.run(run())


def test_scheduled_tasks_are_queued_at_low_priority(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, object] = {}

        async def fake_enabled(_task: Task) -> bool:
            return True

        async def fake_enqueue(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(task_runtime, "is_task_enabled", fake_enabled)
        monkeypatch.setattr(task_runtime, "enqueue_background_job", fake_enqueue)

        _, queued = await task_runtime.request_task_run(
            Task.SYNC_MEDIA,
            trigger="scheduled",
        )

        assert queued is True
        assert captured["priority"] is BackgroundJobPriority.LOW
        assert captured["payload"] == {
            "task": Task.SYNC_MEDIA.value,
            "trigger": "scheduled",
        }

    asyncio.run(run())


def test_worker_pool_uses_configured_executor_count(monkeypatch) -> None:
    async def run() -> None:
        release = asyncio.Event()
        worker_ids: list[str] = []

        async def fake_worker(worker_id: str, **_kwargs) -> None:
            worker_ids.append(worker_id)
            await release.wait()

        monkeypatch.setattr(worker_module, "worker_loop", fake_worker)
        tasks = worker_module.start_worker_pool("host:123", 3)
        await asyncio.sleep(0)

        assert worker_ids == [
            "host:123:command-1",
            "host:123:command-2",
            "host:123:command-3",
        ]
        assert [task.get_name() for task in tasks] == [
            "background-command-1",
            "background-command-2",
            "background-command-3",
        ]

        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())
