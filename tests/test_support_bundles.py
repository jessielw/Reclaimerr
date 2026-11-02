from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import zipfile

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from test_support_diagnostics import database
from backend.api.routes import diagnostics as route
from backend.core import rule_engine
from backend.core.auth import get_current_user
from backend.core.diagnostic_trace import (
    ScanRecorder,
    current_scan,
    current_job_id,
    job_id,
    JOB_ENV,
)
from backend.database.models import (
    Movie,
    MovieVersion,
    ReclaimRule,
    SupportScanRun,
    SupportDecisionTrace,
    SupportOperationEvent,
    User,
    ProtectedMedia,
)
from backend.enums import MediaType, UserRole
from backend.models.diagnostics import (
    BundleOptions,
    DiagnosticCheckRequest,
    Target,
    TimelineEntry,
)
from backend.services import support_bundle as bundle, diagnostic_checks as checks
from backend.services.log_reader import LogEntry, LogTail
from backend.services.support_history import persist_scan, trim_history


def options(database, **kwargs):
    return BundleOptions(
        target=Target(scope="movie_version", id=database[1]["movie_version"]), **kwargs
    )


@pytest.mark.parametrize("include_paths", [False, True])
def test_bundle_preview_archive_and_sqlite_share_only_allowlisted_data(
    database, include_paths
):
    before = hashlib.sha256(database[0].read_bytes()).hexdigest()
    preview = bundle.prepare_bundle(
        database[0],
        options(
            database,
            sections=list(bundle.GROUPS),
            include_sqlite=True,
            include_file_paths=include_paths,
        ),
        7,
    )
    cached = bundle.bundle_cache.get(7, preview.download_token)
    assert cached is not None
    data, _ = cached
    assert len(data) == preview.archive_bytes
    assert hashlib.sha256(data).hexdigest() == preview.sha256
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == set(preview.manifest.files)
        assert json.loads(archive.read("report.json")) == preview.report.model_dump(
            mode="json"
        )
        assert json.loads(archive.read("manifest.json")) == preview.manifest.model_dump(
            mode="json"
        )
        evidence = json.loads(archive.read("evidence.json"))
        assert evidence == {
            "traces": [],
            "timeline": [e.model_dump(mode="json") for e in preview.timeline],
            "logs": [],
        }
        all_bytes = b"".join(archive.read(name) for name in archive.namelist())
        for value in (
            b"APISECRET",
            b"HISTORYSECRET",
            b"URLSECRET",
            b"NESTEDSECRET",
            b"DELETESECRET",
            b"CRITERIASECRET",
            b"privateuser",
            b"PrivateServer",
            b"private-host",
        ):
            assert value not in all_bytes
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(archive.read("support-extract.sqlite"))
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert all(name.startswith("support_") for name in tables)
            for item in preview.manifest.inventory:
                if item.records:
                    assert (
                        db.execute(
                            f'SELECT count(*) FROM "support_{item.section}"'
                        ).fetchone()[0]
                        == item.records
                    )
            assert (
                db.execute("SELECT restorable FROM support_metadata").fetchone()[0] == 0
            )
    assert hashlib.sha256(database[0].read_bytes()).hexdigest() == before


def test_section_controls_remove_excluded_data_and_metadata(database):
    preview = bundle.prepare_bundle(
        database[0],
        options(database, sections=[], include_file_paths=True, include_sqlite=True),
        7,
    )
    assert all(
        value is None
        for key, value in preview.report.sections.model_dump().items()
        if key not in {"identity", "database"}
    )
    assert preview.report.sections.identity.path is None
    assert preview.report.sections.identity.last_viewed_at is None
    assert preview.report.collection.children_total is None
    assert not preview.timeline and not preview.traces and not preview.logs
    assert {item.section for item in preview.manifest.inventory if item.records} == {
        "identity"
    }
    assert {o.section for o in preview.report.collection.observations} <= {
        "identity",
        "database",
    }


def test_window_and_event_limit_are_enforced(database):
    with database[2].begin() as db:
        db.exec_driver_sql(
            "UPDATE playback_history_events SET played_at=? WHERE id<=20",
            (
                (datetime.now(UTC) - timedelta(days=3))
                .replace(tzinfo=None)
                .isoformat(" "),
            ),
        )
    preview = bundle.prepare_bundle(
        database[0],
        options(database, sections=["playback"], history_days=1, event_limit=10),
        7,
    )
    playback = preview.report.sections.playback
    assert len(playback.events) == 10
    assert all(e.played_at >= preview.manifest.window_start for e in playback.events)
    assert playback.stored_event_count == 55
    assert any(
        o.section == "playback_events" and o.total == 35
        for o in preview.report.collection.truncated
    )


def test_bundle_cache_owner_expiry_and_eviction(monkeypatch):
    cache = bundle.BundleCache(max_bytes=10, max_entries=2, ttl=10)
    clock = [10.0]
    monkeypatch.setattr(bundle.time, "monotonic", lambda: clock[0])
    first, _ = cache.put(1, b"12345", "first.zip")
    assert cache.get(2, first) is None
    assert cache.get(1, first) == (b"12345", "first.zip")
    second, _ = cache.put(1, b"123456", "second.zip")
    assert cache.get(1, first) is None
    clock[0] = 21
    assert cache.get(1, second) is None


def test_bundle_routes_are_admin_only_owner_bound_and_immutable(database, monkeypatch):
    app = FastAPI()
    app.include_router(route.router)
    user = User(username="user", password_hash="unused", role=UserRole.USER)
    user.id = 4
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(route, "settings", SimpleNamespace(db_path=database[0]))
    with TestClient(app) as client:
        assert (
            client.post("/api/diagnostics/bundles/preview", json={}).status_code == 403
        )
        assert client.get("/api/diagnostics/bundles/fake").status_code == 403
        assert (
            client.post(
                "/api/diagnostics/checks",
                json={"kind": "provider", "service_config_id": 1},
            ).status_code
            == 403
        )
        user.role = UserRole.ADMIN
        response = client.post("/api/diagnostics/bundles/preview", json={})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        value = response.json()
        url = "/api/diagnostics/bundles/" + value["download_token"]
        first = client.get(url)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert hashlib.sha256(first.content).hexdigest() == value["sha256"]
        with database[2].begin() as db:
            db.exec_driver_sql("UPDATE service_configs SET enabled=0")
        assert client.get(url).content == first.content
        user.id = 5
        assert client.get(url).status_code == 404
        user.role = UserRole.USER
        assert client.get(url).status_code == 403


def make_trace(database, monkeypatch):
    with Session(database[2], expire_on_commit=False) as db:
        rule = db.scalars(select(ReclaimRule)).first()
        rule.definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    {
                        "type": "condition",
                        "field": "media.size",
                        "operator": "greater_than",
                        "value": 100,
                    },
                    {
                        "type": "condition",
                        "field": "playback.usernames",
                        "operator": "contains_any",
                        "value": ["privateuser"],
                    },
                ],
            },
        }
        db.commit()
        movie = db.get(Movie, database[1]["movie"])
        version = db.get(MovieVersion, database[1]["movie_version"])
        monkeypatch.setattr(
            rule_engine,
            "_build_context",
            lambda *args: {"media.size": 20, "playback.usernames": ["privateuser"]},
        )
        recorder = ScanRecorder()
        token = current_scan.set(recorder)
        try:
            outcome = rule_engine.evaluate_advanced_rule(
                rule, target_scope="movie_version", movie=movie, version=version
            )
            assert outcome == (False, {}, [])
        finally:
            current_scan.reset(token)
    return recorder


def test_trace_captures_real_short_circuit_and_bounded_storage(database, monkeypatch):
    recorder = make_trace(database, monkeypatch)
    assert len(recorder.rows) == 1
    row = next(iter(recorder.rows.values()))
    first, skipped = row["payload"]["conditions"]
    assert first["actual"] == 20 and first["outcome"] == "not_matched"
    assert skipped["availability"] == skipped["outcome"] == "not_evaluated"
    assert skipped["actual"] is None
    assert len(row["rule_revision"]) == 64
    recorder.max_records = 0
    recorder.rows.clear()
    token = current_scan.set(recorder)
    try:
        rule_engine.capture_evaluation(
            SimpleNamespace(id=1),
            "movie",
            {"movie": SimpleNamespace(id=1)},
            {},
            False,
            object(),
        )
    finally:
        current_scan.reset(token)
    assert not recorder.rows and recorder.dropped == 1


def test_trace_persists_and_exports_historical_revision_and_parent_protection(
    database, monkeypatch
):
    recorder = make_trace(database, monkeypatch)
    with Session(database[2]) as db:
        db.add(
            ProtectedMedia(
                media_type=MediaType.MOVIE,
                movie_id=database[1]["movie"],
                permanent=True,
            )
        )
        db.commit()

    async def save():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        try:
            async with async_sessionmaker(engine)() as db:
                await persist_scan(db, recorder)
        finally:
            await engine.dispose()

    asyncio.run(save())
    with Session(database[2]) as db:
        assert len(db.scalars(select(SupportDecisionTrace)).all()) == 1
        rule = db.scalars(select(ReclaimRule)).first()
        rule.definition = {}
        db.commit()
    preview = bundle.prepare_bundle(
        database[0], options(database, sections=["decisions", "timeline"]), 1
    )
    assert len(preview.traces) == 1
    trace = preview.traces[0]
    assert trace.scan_id == recorder.scan_id and trace.candidate_id is not None
    assert trace.definition["root"]["conditions"]
    assert trace.protection_checks["protections"][0]["permanent"]
    assert trace.conditions[0].actual == 20
    assert "privateuser" not in preview.model_dump_json()
    assert any(event.correlation_id == trace.scan_id for event in preview.timeline)
    other = bundle.prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="episode", id=database[1]["episode"]),
            sections=["decisions", "timeline"],
        ),
        1,
    )
    assert not other.traces


def test_retention_removes_old_history_and_caps_scan_count(database):
    with Session(database[2]) as db:
        for index in range(53):
            when = datetime.now(UTC) - timedelta(minutes=index)
            db.add(SupportScanRun(id=str(uuid4()), started_at=when, completed_at=when))
        old = datetime.now(UTC) - timedelta(days=15)
        scan = str(uuid4())
        db.add(SupportScanRun(id=scan, started_at=old, completed_at=old))
        db.flush()
        db.add(
            SupportDecisionTrace(
                scan_id=scan,
                target_scope="movie",
                target_id=1,
                rule_id=1,
                rule_revision="a" * 64,
                observed_at=old,
                outcome="matched",
                payload={},
            )
        )
        db.add(
            SupportOperationEvent(
                correlation_id=str(uuid4()),
                event_type="delete_failed",
                occurred_at=old,
                target_scope="movie",
                target_id=1,
                candidate_id=1,
            )
        )
        db.commit()

    async def trim():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        try:
            async with async_sessionmaker(engine)() as db:
                await trim_history(db)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(trim())
    with Session(database[2]) as db:
        assert len(db.scalars(select(SupportScanRun)).all()) == 50
        assert not db.scalars(select(SupportDecisionTrace)).all()
        assert not db.scalars(select(SupportOperationEvent)).all()


def test_logs_are_opt_in_correlated_exactly_and_never_copy_message_text(
    database, monkeypatch
):
    collector = bundle.BundleCollector(
        database[0], options(database, include_logs=True)
    )
    stamp = (
        (collector.report.generated_at - timedelta(seconds=1))
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    )
    entries = [
        LogEntry(raw="", message=message, timestamp=stamp, level="ERROR")
        for message in (
            "[support job=10] private unrelated error",
            "[support job=1] connection failed https://host?token=TOPSECRET",
            "uncorrelated password=LEAK",
        )
    ]
    monkeypatch.setattr(
        bundle, "list_log_files", lambda: [SimpleNamespace(name="app.log")]
    )
    monkeypatch.setattr(bundle, "resolve_log_file", lambda name: name)
    monkeypatch.setattr(
        bundle,
        "tail_log",
        lambda *args, **kwargs: LogTail(file="app.log", entries=entries),
    )
    timeline = [
        TimelineEntry(
            occurred_at=datetime.now(UTC),
            event="test",
            correlation_id="job-1",
            job_id=1,
        )
    ]
    logs = collector.logs(timeline)
    assert len(logs) == 1
    assert logs[0].correlation_id == "job=1" and logs[0].category == "connection_failed"
    assert "TOPSECRET" not in logs[0].model_dump_json()
    monkeypatch.setattr(
        bundle,
        "list_log_files",
        lambda: pytest.fail("Logs must remain unread by default"),
    )
    assert not bundle.prepare_bundle(database[0], options(database), 1).logs


def test_explicit_checks_use_saved_paths_and_do_not_change_database(database, tmp_path):
    media = tmp_path / "private-file.mkv"
    media.write_bytes(b"content never read")
    with database[2].begin() as db:
        db.exec_driver_sql(
            "UPDATE movie_versions SET path=? WHERE id=?",
            (str(media), database[1]["movie_version"]),
        )
    before = database[0].read_bytes()
    result = asyncio.run(
        checks.run_check(
            database[0],
            DiagnosticCheckRequest(kind="path", target=options(database).target),
        )
    )
    assert result.observations and all(
        o.status == "passed" for o in result.observations
    )
    assert str(media) not in result.model_dump_json()
    assert "content never read" not in result.model_dump_json()
    relationships = asyncio.run(
        checks.run_check(
            database[0],
            DiagnosticCheckRequest(
                kind="relationships", target=options(database).target
            ),
        )
    )
    assert any(
        o.subject == "movie relationship" and o.status == "passed"
        for o in relationships.observations
    )
    assert database[0].read_bytes() == before


def test_provider_check_never_returns_credentials_or_raw_response(
    database, monkeypatch
):
    from backend.core.service_manager import service_manager

    monkeypatch.setattr(checks, "fer_decrypt", lambda value: "decrypted-secret")
    test = AsyncMock(return_value=(False, "401 token=TOPSECRET https://private-host"))
    monkeypatch.setattr(service_manager, "test_service", test)
    result = asyncio.run(
        checks.run_check(
            database[0], DiagnosticCheckRequest(kind="provider", service_config_id=1)
        )
    )
    assert test.await_count == 1
    assert test.call_args.args[1] == "https://private-host/?token=URLSECRET"
    assert test.call_args.args[2] == "decrypted-secret"
    assert result.observations[0].status == "failed"
    assert result.observations[0].detail.startswith("authentication failed")
    assert all(
        value not in result.model_dump_json()
        for value in ("TOPSECRET", "URLSECRET", "private-host", "decrypted-secret")
    )


def test_check_input_rejects_arbitrary_paths_urls_and_out_of_bounds_options():
    for data in (
        {"kind": "path", "path": "C:/secret"},
        {"kind": "provider", "url": "http://example.test"},
    ):
        with pytest.raises(ValidationError):
            DiagnosticCheckRequest(**data)
    for data in ({"history_days": 31}, {"event_limit": 101}, {"sections": ["users"]}):
        with pytest.raises(ValidationError):
            BundleOptions(**data)


def test_support_migration_matches_models_and_roundtrips(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "backend.alembic.versions.c4e9a1b6d328_support_diagnostic_history"
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(connection))
            )
            migration.upgrade()
            details_migration = importlib.import_module("backend.alembic.versions.d5f0b2c7e439_support_operation_details")
            monkeypatch.setattr(details_migration, "op", Operations(MigrationContext.configure(connection)))
            details_migration.upgrade()
            for model in (SupportScanRun, SupportDecisionTrace, SupportOperationEvent):
                assert {
                    c["name"]
                    for c in inspect(connection).get_columns(model.__tablename__)
                } == set(model.__table__.columns.keys())
                assert {
                    i["name"]
                    for i in inspect(connection).get_indexes(model.__tablename__)
                } == {i.name for i in model.__table__.indexes}
            details_migration.downgrade()
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    finally:
        engine.dispose()


def test_job_correlation_context_and_child_fallback(monkeypatch):
    monkeypatch.setenv(JOB_ENV, "15")
    assert job_id() == 15
    token = current_job_id.set(3)
    try:
        assert job_id() == 3
    finally:
        current_job_id.reset(token)
    monkeypatch.setenv(JOB_ENV, "invalid")
    assert job_id() is None


def test_timed_out_filesystem_worker_keeps_its_slot(database, monkeypatch):
    release = threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(checks, "check_slots", slots)

    def stalled(*args):
        release.wait(2)
        return []

    monkeypatch.setattr(checks, "inspect_stored", stalled)
    wait_for = asyncio.wait_for

    async def short_wait(awaitable, timeout):
        return await wait_for(awaitable, min(timeout, 0.02))

    monkeypatch.setattr(checks.asyncio, "wait_for", short_wait)

    async def exercise():
        request = DiagnosticCheckRequest(kind="path", target=options(database).target)
        try:
            result = await checks.run_check(database[0], request)
            assert result.observations[0].status == "unknown"
            with pytest.raises(checks.ChecksBusy):
                await checks.run_check(database[0], request)
        finally:
            release.set()
            await asyncio.sleep(0.05)
        assert slots.acquire(blocking=False)
        slots.release()

    asyncio.run(exercise())


def test_timeline_includes_only_correlated_jobs_and_operation_scope(database):
    from backend.database.models import BackgroundJob, ReclaimCandidate
    from backend.enums import BackgroundJobType, BackgroundJobStatus

    correlation = str(uuid4())
    with Session(database[2]) as db:
        candidate = db.scalars(
            select(ReclaimCandidate).where(
                ReclaimCandidate.movie_version_id == database[1]["movie_version"]
            )
        ).one()
        related = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            scheduled_at=datetime.now(UTC),
            payload={"candidate_ids": [candidate.id]},
            status=BackgroundJobStatus.FAILED,
        )
        unrelated = BackgroundJob(
            job_type=BackgroundJobType.TASK_RUN,
            scheduled_at=datetime.now(UTC),
            payload={"candidate_ids": [9999]},
            status=BackgroundJobStatus.FAILED,
        )
        related.error_message = "permission denied token=TIMELINESECRETS"
        unrelated.error_message = "password=PRIVATEJOB"
        related.created_at = datetime.now(UTC) - timedelta(days=3)
        related.completed_at = datetime.now(UTC)
        db.add_all([related, unrelated])
        db.flush()
        related_id, unrelated_id = related.id, unrelated.id
        db.add(
            SupportOperationEvent(
                correlation_id=correlation,
                event_type="delete_failed",
                occurred_at=datetime.now(UTC),
                target_scope="movie_version",
                target_id=database[1]["movie_version"],
                movie_id=database[1]["movie"],
                candidate_id=candidate.id,
                job_id=related.id,
                error_category="permission_denied",
            )
        )
        db.commit()
    preview = bundle.prepare_bundle(
        database[0], options(database, sections=["timeline"], history_days=1), 1
    )
    assert any(
        event.correlation_id == correlation
        and event.error_category == "permission_denied"
        for event in preview.timeline
    )
    assert any(
        event.job_id == related_id and event.event.endswith("failed")
        for event in preview.timeline
    )
    assert not any(event.job_id == unrelated_id for event in preview.timeline)
    assert not any(
        event.job_id == related_id and event.event.endswith("queued")
        for event in preview.timeline
    )
    assert preview.report.sections.decisions is None and not preview.traces
    assert "TIMELINESECRETS" not in preview.model_dump_json()
    assert "PRIVATEJOB" not in preview.model_dump_json()


def test_missing_history_tables_return_useful_partial_bundle(database):
    with database[2].begin() as db:
        db.exec_driver_sql("DROP TABLE support_decision_traces")
        db.exec_driver_sql("DROP TABLE support_operation_events")
    preview = bundle.prepare_bundle(
        database[0], options(database, sections=["decisions", "timeline"]), 1
    )
    assert preview.report.sections.identity is not None
    assert not preview.traces
    assert any(
        "Decision traces unavailable" in note for note in preview.manifest.limitations
    )
    assert any(
        "Operation history unavailable" in note for note in preview.manifest.limitations
    )


def test_preset_defaults_and_explicit_empty_override():
    assert "playback" in BundleOptions(preset="playback").sections
    assert "decisions" in BundleOptions(preset="candidate").sections
    assert BundleOptions(preset="candidate", sections=[]).sections == []


def test_unselected_sections_do_not_consume_selected_report_budget(
    database, monkeypatch
):
    from backend.models.diagnostics import StorageIdentity

    monkeypatch.setattr(
        bundle.BundleCollector,
        "storage",
        lambda self: [
            StorageIdentity(
                id=1, kind="movie_version", path="large excluded path" * 40000
            )
        ],
    )
    preview = bundle.prepare_bundle(
        database[0], options(database, sections=["decisions"]), 1
    )
    assert preview.report.sections.storage is None
    assert preview.report.sections.decisions.candidates
    assert (
        len(preview.report.model_dump_json(indent=2).encode())
        <= preview.report.collection.max_bytes
    )
