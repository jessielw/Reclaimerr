from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.database.models import BackgroundJob
from backend.enums import BackgroundJobStatus, BackgroundJobType, Task
from backend.models.diagnostics import BundleOptions, Target
from backend.services.support_bundle import bundle_cache, prepare_bundle


def test_existing_job_results_survive_all_bundle_formats(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        job = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            status=BackgroundJobStatus.COMPLETED,
            scheduled_at=now,
            attempts=2,
            payload={
                "task": Task.DELETE_CLEANUP_CANDIDATES.value,
                "trigger": "scheduled",
                "result": {
                    "skipped": 1,
                    "revalidated_out": 1,
                    "playback_unavailable": 1,
                    "deleted": 0,
                    "failed": True,
                    "deferred": "COUNTER_SECRET",
                    "errors": ["ERROR_SECRET"],
                    "api_key": "RESULT_SECRET",
                },
                "progress": {
                    "completed_items": 3,
                    "percent": 100,
                    "current_item_label": "LABEL_SECRET",
                },
                "api_key": "PAYLOAD_SECRET",
            },
        )
        job.started_at = now
        job.completed_at = now
        db.add(job)
        db.commit()
        job_id = job.id
    before = hashlib.sha256(database[0].read_bytes()).hexdigest()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            support_code=f"job-{job_id}",
            sections=["tasks", "timeline"],
            include_sqlite=True,
        ),
        1,
    )
    exported = next(e.job for e in preview.timeline if e.job)
    assert exported.task == Task.DELETE_CLEANUP_CANDIDATES.value
    assert exported.trigger == "scheduled" and exported.attempts == 2
    assert exported.result == {
        "skipped": 1,
        "revalidated_out": 1,
        "playback_unavailable": 1,
        "deleted": 0,
    }
    assert exported.progress == {"completed_items": 3, "percent": 100}
    assert exported.scope == "app_wide"
    assert preview.report.sections.recent_jobs[0].result == exported.result
    data, _ = bundle_cache.get(1, preview.download_token)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert b"SECRET" not in b"".join(archive.read(n) for n in archive.namelist())
        assert (
            json.loads(archive.read("evidence.json"))["timeline"][-1]["job"]["result"]
            == exported.result
        )
    assert hashlib.sha256(database[0].read_bytes()).hexdigest() == before


def test_preflight_failure_survives_refresh_noise_as_app_wide_context(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        failed = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            status=BackgroundJobStatus.FAILED,
            scheduled_at=now,
            payload={
                "task": Task.DELETE_CLEANUP_CANDIDATES.value,
                "trigger": "scheduled",
            },
        )
        failed.completed_at = now
        failed.error_message = "connection failed password=FAILURE_SECRET"
        db.add(failed)
        db.flush()
        job_id = failed.id
        for _ in range(130):
            refresh = BackgroundJob(
                job_type=BackgroundJobType.TASK_RUN,
                status=BackgroundJobStatus.COMPLETED,
                scheduled_at=now,
                payload={"task": Task.REFRESH_PLAYBACK_HISTORY.value},
            )
            refresh.completed_at = now
            db.add(refresh)
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["tasks", "timeline"],
        ),
        1,
    )
    job = next(j for j in preview.report.sections.recent_jobs if j.id == job_id)
    assert job.error_category == "connection_failed"
    assert job.scope == "app_wide"
    assert not any(e.job_id == job_id for e in preview.timeline)
    assert any(t.section == "recent_jobs" for t in preview.report.collection.truncated)
    assert "FAILURE_SECRET" not in preview.model_dump_json()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "malformed",
        {"task": {}, "result": []},
        {
            "task": "PRIVATE_TASK",
            "trigger": "SECRET_TRIGGER",
            "result": {"skipped": -1, "deleted": 2**64},
        },
        {"result": {"skipped": 1}, "secret": "x" * 70000},
    ],
)
def test_untrusted_job_payloads_are_bounded(database, payload):
    with Session(database[2]) as db:
        job = BackgroundJob(
            job_type=BackgroundJobType.SERVICE_TOGGLE,
            scheduled_at=datetime.now(UTC),
            payload=payload,
        )
        db.add(job)
        db.commit()
    preview = prepare_bundle(database[0], BundleOptions(sections=["tasks"]), 1)
    assert preview.report.sections.recent_jobs
    job = preview.report.sections.recent_jobs[0]
    assert job.task is None and job.trigger is None
    assert job.result in (None, {})
    assert "PRIVATE_TASK" not in preview.model_dump_json()
    if isinstance(payload, dict) and "secret" in payload:
        assert any("payload" in value for value in preview.report.collection.omitted)
