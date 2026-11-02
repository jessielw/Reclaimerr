from __future__ import annotations

import asyncio
import errno
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.api.routes import diagnostics as routes
from backend.core.auth import get_current_user
from backend.core.operation_diagnostics import (
    OperationRecorder,
    candidate_scope,
    current_action,
    current_candidates,
    current_group,
    current_operation_recorder,
    current_step,
    observe_step,
    operation_step,
    record_outcome,
    select_candidate,
)
from backend.core.support_codes import normalize_support_code
from backend.core.utils import filesystem
from backend.database.models import (
    BackgroundJob,
    SupportDecisionTrace,
    SupportOperationEvent,
    SupportScanRun,
    User,
)
from backend.enums import BackgroundJobType, UserRole
from backend.models.diagnostics import BundleOptions, Target
from backend.models.live_playback import CandidateOperationResult
from backend.services import support_history
from backend.services.support_bundle import prepare_bundle
from backend.services.support_lookup import lookup_support


@contextmanager
def observing(action="move", ids=(1, 2), limit=1000):
    recorder = OperationRecorder(action=action, max_steps=limit)
    token = current_operation_recorder.set(recorder)
    candidates = current_candidates.set(ids)
    group = current_group.set(ids)
    try:
        yield recorder
    finally:
        current_group.reset(group)
        current_candidates.reset(candidates)
        current_operation_recorder.reset(token)


def seed_history(database):
    scan_ids, operations, jobs = [], [], []
    with Session(database[2]) as db:
        for number in (1, 2):
            job = BackgroundJob(
                job_type=BackgroundJobType.TASK_RUN,
                scheduled_at=datetime.now(UTC),
                payload={"token": "LOOKUP_SECRET"},
            )
            db.add(job)
            db.flush()
            jobs.append(job.id)
            scan_id, operation = str(uuid4()), str(uuid4())
            scan_ids.append(scan_id)
            operations.append(operation)
            db.add(
                SupportScanRun(
                    id=scan_id,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                    job_id=job.id,
                    context={"status": "completed"},
                )
            )
            db.flush()
            db.add(
                SupportDecisionTrace(
                    scan_id=scan_id,
                    target_scope="movie_version",
                    target_id=number,
                    movie_id=database[1]["movie"],
                    rule_id=1,
                    candidate_id=number,
                    rule_revision="a" * 64,
                    observed_at=datetime.now(UTC),
                    outcome="not_matched",
                    payload={
                        "definition": {
                            "root": {
                                "type": "condition",
                                "field": "media.size",
                                "operator": "greater_than",
                                "value": 100,
                            }
                        },
                        "conditions": [
                            {
                                "field": "media.size",
                                "operator": "greater_than",
                                "expected": 100,
                                "actual": 20,
                                "availability": "available",
                                "outcome": "not_matched",
                            }
                        ],
                    },
                )
            )
            db.add(
                SupportOperationEvent(
                    correlation_id=operation,
                    event_type="operation_step",
                    occurred_at=datetime.now(UTC),
                    target_scope="movie_version",
                    target_id=number,
                    movie_id=database[1]["movie"],
                    candidate_id=number,
                    job_id=job.id,
                    error_category="permission_denied",
                    detail={
                        "action": "move",
                        "stage": "copy",
                        "outcome": "failed",
                        "subject": "file",
                        "path": "/home/privateperson/file.mkv",
                        "destination": "https://host?token=PATH_SECRET",
                        "provider": "plex",
                        "reference": "PRIVATE_PROVIDER_ITEM",
                        "secret": "DETAIL_SECRET",
                        "shared_request": False,
                    },
                )
            )
        db.commit()
    return scan_ids, operations, jobs


@pytest.mark.parametrize(
    "value",
    [
        "job-0",
        "job--1",
        "job-1 OR 1=1",
        "scan-' UNION SELECT",
        "https://server",
        "C:/private",
        "job-99999999999999999999",
        "[support job=3]",
    ],
)
def test_support_code_rejects_arbitrary_input(value):
    with pytest.raises(ValueError):
        normalize_support_code(value)


def test_lookup_exact_code_readonly_and_no_private_payload(database):
    scans, operations, jobs = seed_history(database)
    before = hashlib.sha256(database[0].read_bytes()).digest()
    for code, kind in (
        ("scan-" + scans[0], "scan"),
        (operations[0], "operation"),
        (" JOB=" + str(jobs[0]) + " ", "job"),
    ):
        result = lookup_support(database[0], normalize_support_code(code))
        assert result.state == "found"
        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.kind == kind
        assert match.targets == [Target(scope="movie_version", id=1)]
        assert match.suggested.support_code == match.code
        assert (
            not match.suggested.include_logs and not match.suggested.include_file_paths
        )
        assert "LOOKUP_SECRET" not in result.model_dump_json()
        assert "privateperson" not in result.model_dump_json()
    assert hashlib.sha256(database[0].read_bytes()).digest() == before
    assert lookup_support(database[0], "scan-" + str(uuid4())).state == "not_found"


@pytest.mark.parametrize("kind", ["scan", "operation", "job"])
def test_code_filters_bundle_evidence_without_replaying(database, monkeypatch, kind):
    scans, operations, jobs = seed_history(database)
    identifier = {"scan": scans[0], "operation": operations[0], "job": str(jobs[0])}[
        kind
    ]
    from backend.core import rule_engine

    monkeypatch.setattr(
        rule_engine,
        "evaluate_advanced_rule",
        lambda *args, **kwargs: pytest.fail("Export must not rerun rules"),
    )
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            support_code=f"{kind}-{identifier}",
            sections=["decisions", "timeline"],
            include_sqlite=True,
        ),
        1,
    )
    assert preview.manifest.support_code == f"{kind}-{identifier}"
    assert all(trace.scan_id == scans[0] for trace in preview.traces)
    assert not any(
        event.correlation_id in (scans[1], operations[1], f"job-{jobs[1]}")
        for event in preview.timeline
    )
    if kind == "scan":
        assert preview.traces and not any(event.operation for event in preview.timeline)
        assert preview.traces[0].conditions[0].field_label == "Size"
    else:
        detail = next(event.operation for event in preview.timeline if event.operation)
        assert detail.stage == "copy" and detail.outcome == "failed"
        assert detail.path.startswith("path-")
        assert detail.reference.startswith("provider-item-")
    exported = preview.model_dump_json()
    assert all(
        secret not in exported
        for secret in (
            "LOOKUP_SECRET",
            "privateperson",
            "PATH_SECRET",
            "PRIVATE_PROVIDER_ITEM",
            "DETAIL_SECRET",
        )
    )


def test_operation_paths_opt_in_still_masks_credentials(database):
    _, operations, _ = seed_history(database)
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            support_code="operation-" + operations[0],
            sections=["timeline"],
            include_file_paths=True,
        ),
        1,
    )
    detail = next(event.operation for event in preview.timeline if event.operation)
    assert detail.path.startswith("/home/user-")
    assert detail.destination.startswith("omitted-path-")
    assert "PATH_SECRET" not in preview.model_dump_json()


def test_lookup_authorization_validation_and_no_store(database, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    user = User(username="user", password_hash="unused", role=UserRole.USER)
    user.id = 7
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(routes, "settings", SimpleNamespace(db_path=database[0]))
    with TestClient(app) as client:
        assert (
            client.post("/api/diagnostics/lookup", json={"code": "job-1"}).status_code
            == 403
        )
        user.role = UserRole.ADMIN
        assert (
            client.post(
                "/api/diagnostics/lookup", json={"code": "job-1 OR 1=1"}
            ).status_code
            == 422
        )
        response = client.post("/api/diagnostics/lookup", json={"code": "job-99999"})
        assert (
            response.status_code == 200
            and response.headers["cache-control"] == "no-store"
        )
        assert response.json()["state"] == "not_found"


def test_lookup_missing_database_is_unavailable_and_never_created(tmp_path):
    path = tmp_path / "missing.db"
    assert lookup_support(path, "job-1").state == "unavailable"
    assert not path.exists()


def test_mixed_file_move_records_exact_failed_file_and_stage(tmp_path, monkeypatch):
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    destination = tmp_path / "destination"
    original_copy = filesystem.shutil.copy2
    failure = PermissionError(errno.EACCES, "permission denied", str(second))
    attempts = []

    def copy(src, dest):
        attempts.append(src)
        if src == str(second):
            raise failure
        return original_copy(src, dest)

    def cross_device(*args):
        raise OSError(errno.EXDEV, "cross device")

    monkeypatch.setattr(filesystem, "os_rename", cross_device)
    monkeypatch.setattr(filesystem.shutil, "copy2", copy)
    with observing() as recorder:
        select_candidate(1)
        filesystem._move_single_file_to_path(first, destination / first.name)
        record_outcome(1, "succeeded", stage="completed")
        select_candidate(2)
        with pytest.raises(PermissionError) as captured:
            filesystem._move_single_file_to_path(second, destination / second.name)
        assert captured.value is failure
        record_outcome(2, "failed", error=failure)
        assert recorder.outcomes[2]["stage"] == "copy"
        assert recorder.outcomes[2]["path"] == str(second)
        assert any(
            row["candidate_ids"] == (1,) and row["outcome"] == "succeeded"
            for row in recorder.steps
        )
        assert any(
            row["candidate_ids"] == (2,)
            and row["stage"] == "copy"
            and row["outcome"] == "failed"
            for row in recorder.steps
        )
    assert current_step.get() is None
    assert not first.exists() and second.exists()
    assert attempts == [str(first), str(second)]


def test_collision_records_sidecar_without_moving_any_file(tmp_path):
    src, dest = tmp_path / "private-sidecar.srt", tmp_path / "destination.srt"
    src.write_text("source")
    dest.write_text("different")
    with observing(ids=(1,)) as recorder:
        with pytest.raises(FileExistsError):
            filesystem._preflight_file_moves([(src, dest)])
        assert recorder.steps[0]["stage"] == "destination_collision"
        assert recorder.steps[0]["path"] == str(src)
    assert src.read_text() == "source" and dest.read_text() == "different"


def test_provider_failure_remains_visible_after_successful_fallback():
    @observe_step(
        "provider_delete",
        subject="provider_request",
        provider="radarr",
        reference="ids",
    )
    async def provider(ids):
        raise ConnectionError("private token=SECRET")

    async def exercise():
        with observing(ids=(1, 2)) as recorder:
            with pytest.raises(ConnectionError):
                await provider([10, 11])
            record_outcome(1, "succeeded", stage="completed")
            assert recorder.steps[0]["outcome"] == "failed"
            assert recorder.steps[0]["candidate_ids"] == (1, 2)
            assert recorder.outcomes[1]["outcome"] == "succeeded"

    asyncio.run(exercise())


def test_context_resets_after_cancellation_and_limits_keep_failures():
    @candidate_scope(action="move")
    async def cancelled():
        select_candidate(3)
        with operation_step("provider_delete", subject="provider_request"):
            assert current_action.get() == "move"
        raise asyncio.CancelledError()

    async def exercise():
        with observing(ids=(1, 2), limit=1) as recorder:
            with pytest.raises(asyncio.CancelledError):
                await cancelled()
            assert current_candidates.get() == (1, 2)
            assert current_action.get() is None
            assert recorder.steps[0]["candidate_ids"] == (3,)
            assert recorder.steps[0]["action"] == "move"
            recorder.steps.clear()
            recorder.append({"outcome": "succeeded"})
            recorder.append({"outcome": "failed"})
            assert recorder.steps == [{"outcome": "failed"}] and recorder.dropped == 1

    asyncio.run(exercise())


def test_copy_verification_preserves_source_and_useful_failure(tmp_path, monkeypatch):
    src, dest = tmp_path / "source.mkv", tmp_path / "target.mkv"
    src.write_bytes(b"complete")

    def cross_device(*args):
        raise OSError(errno.EXDEV, "cross device")

    monkeypatch.setattr(filesystem, "os_rename", cross_device)
    monkeypatch.setattr(
        filesystem.shutil, "copy2", lambda *args: dest.write_bytes(b"bad")
    )
    with observing(ids=(1,)) as recorder:
        with pytest.raises(OSError, match="expected 8, got 3"):
            filesystem._move_single_file_to_path(src, dest)
        assert recorder.steps[-1]["stage"] == "verify_copy"
        assert (
            support_history.error_category(recorder.steps[-1]["error"])
            == "verification_failed"
        )
    assert src.read_bytes() == b"complete" and not dest.exists()


def test_directory_failure_keeps_exact_child_and_original_exception(tmp_path):
    import shutil

    source, destination = tmp_path / "folder", tmp_path / "destination"
    failure = shutil.Error(
        [
            (
                str(source / "failed.mkv"),
                str(destination / "failed.mkv"),
                "permission denied",
            )
        ]
    )
    with observing(ids=(1,)) as recorder:
        with pytest.raises(shutil.Error) as captured:
            with operation_step(
                "copy", path=source, destination=destination, subject="directory"
            ):
                raise failure
        assert captured.value is failure
        assert recorder.steps[0]["subject"] == "file"
        assert recorder.steps[0]["path"] == str(source / "failed.mkv")
        assert recorder.steps[0]["stage"] == "copy"


def test_history_write_failure_does_not_replace_operation_result(monkeypatch):
    def unavailable():
        raise OSError("database unavailable")

    monkeypatch.setattr(support_history, "async_db", unavailable)
    asyncio.run(
        support_history.persist_operation_details(
            [], str(uuid4()), OperationRecorder("move")
        )
    )


def test_job_lookup_fallback_is_bounded_and_deduplicates_targets(database):
    with Session(database[2]) as db:
        job = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            scheduled_at=datetime.now(UTC),
            payload={"candidate_ids": [1] * 101, "token": "PAYLOAD_SECRET"},
        )
        db.add(job)
        db.commit()
        job_id = job.id
    result = lookup_support(database[0], f"job-{job_id}")
    assert result.state == "found"
    assert len(result.matches[0].targets) == 1
    assert result.matches[0].targets_truncated
    assert result.matches[0].suggested.target is None
    assert "PAYLOAD_SECRET" not in result.model_dump_json()


@pytest.mark.parametrize("aborted", [False, True])
def test_batch_persists_item_outcomes_without_inventing_success(
    database, monkeypatch, aborted
):
    from backend.tasks import cleanup

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        monkeypatch.setattr(support_history, "async_db", sessions)

        async def work(*args, **kwargs):
            record_outcome(
                1,
                "failed",
                error="permission denied PRIVATE_FAILURE",
                stage="copy",
                path="/home/privateperson/fail.mkv",
            )
            if aborted:
                raise RuntimeError("operation interrupted")
            return CandidateOperationResult(failed=1, succeeded=1)

        monkeypatch.setattr(cleanup, "_run_playback_guarded_candidates_impl", work)
        try:
            if aborted:
                with pytest.raises(RuntimeError, match="operation interrupted"):
                    await cleanup._run_playback_guarded_candidates(
                        [1, 2], approved_by="test", moving=True
                    )
            else:
                result = await cleanup._run_playback_guarded_candidates(
                    [1, 2], approved_by="test", moving=True
                )
                assert result.failed == 1 and result.succeeded == 1
            assert current_operation_recorder.get() is None
            async with sessions() as db:
                events = (
                    await db.scalars(
                        select(SupportOperationEvent).where(
                            SupportOperationEvent.event_type == "item_outcome"
                        )
                    )
                ).all()
                assert {e.candidate_id: e.detail["outcome"] for e in events} == {
                    1: "failed",
                    2: "unknown",
                }
                assert events[0].error_category == "permission_denied"
                assert "PRIVATE_FAILURE" not in json.dumps([e.detail for e in events])
        finally:
            await engine.dispose()

    asyncio.run(exercise())
