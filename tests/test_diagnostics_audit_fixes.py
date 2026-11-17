from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.database.models import BackgroundJob
from backend.database.models import SupportOperationEvent
from backend.database.models import (
    CandidateResyncIdentity,
    DeleteRequest,
    DeleteRequestResyncIdentity,
    ProtectedMedia,
    ReclaimCandidate,
    ScheduledDeletion,
    Season,
)
from backend.enums import MediaType, ProtectionRequestStatus
from backend.enums import BackgroundJobStatus, BackgroundJobType, Task
from backend.models.diagnostics import BundleOptions, Target, TimelineEntry
from backend.services.support_bundle import (
    bundle_cache,
    prepare_bundle,
    prioritize_timeline,
)


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
        assert any(
            e["job"] and e["job"]["result"] == exported.result
            for e in json.loads(archive.read("evidence.json"))["timeline"]
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


@pytest.mark.parametrize("exact", [False, True])
def test_final_recovery_and_decision_survive_error_flood(database, exact):
    code = str(uuid4())
    now = datetime.now(UTC) - timedelta(minutes=2)
    with Session(database[2]) as db:
        for index in range(101):
            db.add(
                SupportOperationEvent(
                    correlation_id=code,
                    event_type="operation_step",
                    occurred_at=now + timedelta(seconds=index),
                    target_scope="movie_version",
                    target_id=database[1]["movie_version"],
                    candidate_id=1,
                    movie_id=database[1]["movie"],
                    error_category="permission_denied",
                    detail={
                        "action": "delete",
                        "stage": "provider_delete",
                        "outcome": "failed",
                        "subject": "provider_request",
                    },
                )
            )
        for event_type, detail in (
            (
                "item_outcome",
                {
                    "action": "delete",
                    "stage": "completed",
                    "outcome": "succeeded",
                    "subject": "candidate",
                },
            ),
            (
                "auto_delete_decision",
                {"stage": "dispatch", "outcome": "authorized", "reason": "authorized"},
            ),
        ):
            db.add(
                SupportOperationEvent(
                    correlation_id=code,
                    event_type=event_type,
                    occurred_at=now,
                    target_scope="movie_version",
                    target_id=database[1]["movie_version"],
                    candidate_id=1,
                    movie_id=database[1]["movie"],
                    detail=detail,
                )
            )
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            support_code=f"operation-{code}" if exact else None,
            sections=["timeline"],
        ),
        1,
    )
    assert any(
        e.event == "item_outcome" and e.operation.outcome == "succeeded"
        for e in preview.timeline
    )
    assert any(
        e.auto_delete and e.auto_delete.reason == "authorized" for e in preview.timeline
    )
    assert any(
        e.operation and e.operation.outcome == "failed" for e in preview.timeline
    )
    assert any(
        t.section == "operation_events" and t.total == 103
        for t in preview.report.collection.truncated
    )


def test_timeline_limit_keeps_terminal_evidence_before_newer_noise():
    now = datetime.now(UTC)
    code = str(uuid4())
    target = Target(scope="movie_version", id=1)
    final = TimelineEntry(
        occurred_at=now - timedelta(days=1),
        event="item_outcome",
        correlation_id=code,
        candidate_id=1,
        target=target,
    )
    newer = final.model_copy(update={"occurred_at": now - timedelta(hours=1)})
    noise = [
        TimelineEntry(
            occurred_at=now,
            event="operation_step",
            correlation_id=code,
            error_category="permission_denied",
        )
        for _ in range(250)
    ]
    selected = prioritize_timeline([final, newer, *noise])[:200]
    assert selected[0] is newer
    assert all(e is not final for e in selected)
    assert len(selected) == 200


@pytest.mark.parametrize("scope", ["movie_version", "season"])
def test_sibling_blockers_keep_their_actual_scope(database, scope):
    with Session(database[2]) as db:
        if scope == "season":
            sibling = Season(series_id=database[1]["series"], season_number=2)
            db.add(sibling)
            db.flush()
            ids = {"series_id": database[1]["series"], "season_id": sibling.id}
            media_type = MediaType.SERIES
        else:
            ids = {
                "movie_id": database[1]["movie"],
                "movie_version_id": database[1]["movie_version"] + 1,
            }
            media_type = MediaType.MOVIE
        protection = ProtectedMedia(
            media_type=media_type, protected_by_user_id=1, **ids
        )
        request = DeleteRequest(media_type=media_type, requested_by_user_id=1, **ids)
        db.add_all([protection, request])
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope=scope, id=database[1][scope]),
            sections=["decisions"],
        ),
        1,
    )
    decisions = preview.report.sections.decisions
    assert decisions is not None
    assert decisions.protections[0].relationship == "sibling"
    assert decisions.protections[0].active
    assert decisions.deletion_requests[0].relationship == "sibling"
    assert (
        decisions.protections[0].target.id
        == ids["season_id" if scope == "season" else "movie_version_id"]
    )


def test_stored_holds_schedules_and_request_deferrals_are_exported(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        candidate = db.get(ReclaimCandidate, 1)
        candidate.matched_criteria = {"_unavailable_rule_ids": [1, True, "HOLD_SECRET"]}
        db.add(
            CandidateResyncIdentity(
                candidate_id=1,
                movie_id=database[1]["movie"],
                path="/private/RESYNC_SECRET.mkv",
            )
        )
        request = DeleteRequest(
            media_type=MediaType.MOVIE,
            requested_by_user_id=1,
            movie_id=database[1]["movie"],
            movie_version_id=database[1]["movie_version"],
            status=ProtectionRequestStatus.APPROVED,
            playback_deferral={
                "code": "currently_playing",
                "checked_at": now.isoformat(),
                "server_config_ids": [1],
                "message": "DEFERRAL_SECRET",
            },
        )
        db.add(request)
        db.flush()
        db.add(
            DeleteRequestResyncIdentity(
                delete_request_id=request.id,
                movie_id=database[1]["movie"],
                path="/private/REQUEST_SECRET.mkv",
            )
        )
        db.add(
            ScheduledDeletion(
                scheduled_at=now + timedelta(days=2),
                target={
                    "media_type": "movie",
                    "media_id": database[1]["movie"],
                    "scope": "movie",
                    "title": "TARGET_SECRET",
                },
                created_by_username="ACTOR_SECRET",
                state="blocked",
                last_error="permission denied ERROR_SECRET",
            )
        )
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["decisions"],
            include_sqlite=True,
        ),
        1,
    )
    decisions = preview.report.sections.decisions
    assert decisions is not None
    candidate = decisions.candidates[0]
    assert candidate.unavailable_rule_ids == [1]
    assert candidate.resync_pending and candidate.resync_path.startswith("path-")
    request = decisions.deletion_requests[0]
    assert request.playback_deferral.code == "currently_playing"
    assert request.error is None and request.resync_pending
    schedule = decisions.scheduled_deletions[0]
    assert schedule.state == "blocked" and schedule.holds_selected_scope
    assert schedule.scheduled_at == now + timedelta(days=2)
    assert schedule.error_category == "permission_denied"
    assert schedule.actor.startswith("user-")
    assert any(
        i.section == "scheduled_deletions" and i.records == 1
        for i in preview.manifest.inventory
    )
    assert "SECRET" not in preview.model_dump_json()


def test_manual_schedule_path_hold_can_cross_media_ids(database):
    with Session(database[2]) as db:
        db.add(
            ScheduledDeletion(
                scheduled_at=datetime.now(UTC) + timedelta(days=1),
                target={
                    "media_type": "movie",
                    "media_id": 9999,
                    "scope": "movie",
                    "inventory": {
                        "files": {"/remote/movie-0.mkv": 1},
                        "directories": [],
                    },
                },
                created_by_username="PRIVATE_ACTOR",
                state="failed",
            )
        )
        db.commit()
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie_version", id=database[1]["movie_version"]),
            sections=["decisions"],
        ),
        1,
    )
    schedule = preview.report.sections.decisions.scheduled_deletions[0]
    assert schedule.stored_path_overlap and not schedule.holds_selected_scope
    assert schedule.media_target.id == 9999
    assert "PRIVATE_ACTOR" not in preview.model_dump_json()
