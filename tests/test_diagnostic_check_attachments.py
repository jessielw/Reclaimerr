from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_support_diagnostics import database

from backend.api.routes import diagnostics as route
from backend.core.auth import get_current_user
from backend.database.models import User
from backend.enums import UserRole
from backend.models import diagnostics as m
from backend.services import diagnostic_check_cache as cache_module, support_bundle


def result(kind="relationships", *, checked_at=None):
    return m.DiagnosticCheckResult(
        check_id=str(uuid4()),
        kind=kind,
        checked_at=checked_at or datetime.now(UTC),
        observations=[
            m.CheckObservation(
                subject="Selected target",
                status="passed",
                detail="Stored target exists.",
            )
        ],
    )


@pytest.fixture
def cache(monkeypatch):
    cache = cache_module.CheckCache()
    monkeypatch.setattr(route, "check_cache", cache)
    monkeypatch.setattr(support_bundle, "check_cache", cache)
    return cache


def test_check_attachment_is_explicit_immutable_and_identical_in_preview_zip_sqlite(
    database, monkeypatch, cache
):
    app = FastAPI()
    app.include_router(route.router)
    user = User(username="private", password_hash="unused", role=UserRole.ADMIN)
    user.id = 4
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(route, "settings", SimpleNamespace(db_path=database[0]))
    # Older completed observations remain timestamped snapshots, independent of the event window.
    observed = result(checked_at=datetime.now(UTC) - timedelta(days=10))
    run = AsyncMock(return_value=observed)
    monkeypatch.setattr(route, "run_check", run)
    target = {"scope": "movie_version", "id": database[1]["movie_version"]}
    before = hashlib.sha256(database[0].read_bytes()).hexdigest()
    with TestClient(app) as client:
        response = client.post(
            "/api/diagnostics/checks", json={"kind": "relationships", "target": target}
        )
        assert response.status_code == 200
        checked = response.json()
        assert checked["attachment_expires_at"] and checked["target"] == target
        observed.observations.clear()
        run.assert_awaited_once()
        monkeypatch.setattr(
            route,
            "run_check",
            AsyncMock(side_effect=AssertionError("must never rerun")),
        )
        plain = client.post(
            "/api/diagnostics/bundles/preview", json={"sections": []}
        ).json()
        assert (
            plain["checks"] == []
            and "completed_checks" in plain["manifest"]["excluded"]
        )
        payload = {
            "sections": [],
            "include_sqlite": True,
            "history_days": 1,
            "check_ids": [checked["check_id"]],
        }
        response = client.post("/api/diagnostics/bundles/preview", json=payload)
        assert response.status_code == 200
        preview = response.json()
        assert preview["checks"] == [checked]
        assert {
            row["section"]: row["records"] for row in preview["manifest"]["inventory"]
        }["checks"] == 1
        assert any(
            "did not rerun" in note for note in preview["manifest"]["limitations"]
        )
        archive_bytes = client.get(
            "/api/diagnostics/bundles/" + preview["download_token"]
        ).content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            assert (
                json.loads(archive.read("evidence.json"))["checks"] == preview["checks"]
            )
            with sqlite3.connect(":memory:") as db:
                db.deserialize(archive.read("support-extract.sqlite"))
                row = db.execute(
                    "SELECT check_id,target,observations FROM support_checks"
                ).fetchone()
                assert row[0] == checked["check_id"] and json.loads(row[1]) == target
                assert json.loads(row[2]) == checked["observations"]
        assert (
            client.get("/api/diagnostics/bundles/" + preview["download_token"]).content
            == archive_bytes
        )
        user.id = 5
        denied = client.post("/api/diagnostics/bundles/preview", json=payload)
        assert denied.status_code == 422 and "unavailable" in denied.json()["detail"]
        assert checked["check_id"] not in denied.text
        assert (
            client.get(
                "/api/diagnostics/bundles/" + preview["download_token"]
            ).status_code
            == 404
        )
    assert hashlib.sha256(database[0].read_bytes()).hexdigest() == before


def test_cached_checks_respect_target_path_policy_database_owner_and_expiry(
    database, monkeypatch, cache
):
    clock = [10.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock[0])
    target = m.Target(scope="movie_version", id=database[1]["movie_version"])
    saved = cache.put(
        1,
        database[0],
        m.DiagnosticCheckRequest(kind="path", target=target, include_file_paths=True),
        result("path"),
    )
    ids = [UUID(saved.check_id)]
    assert cache.selected(1, database[0], ids, target, True)[0].file_paths_included
    for owner, path, scope, paths, message in (
        (2, database[0], target, True, "unavailable"),
        (1, database[0].with_name("different.db"), target, True, "unavailable"),
        (1, database[0], target, False, "file paths"),
        (1, database[0], m.Target(scope="movie", id=1), True, "different media target"),
    ):
        with pytest.raises(cache_module.CheckAttachmentError, match=message):
            cache.selected(owner, path, ids, scope, paths)
    assert len(cache.selected(1, database[0], ids + ids, None, True)) == 1
    clock[0] = 611
    with pytest.raises(cache_module.CheckAttachmentError, match="expired"):
        cache.selected(1, database[0], ids, target, True)


def test_provider_checks_are_labeled_app_wide_and_cache_capacity_is_bounded(database):
    cache = cache_module.CheckCache(max_entries=1)
    request = m.DiagnosticCheckRequest(kind="provider", service_config_id=2)
    first = cache.put(1, database[0], request, result("provider"))
    second = cache.put(1, database[0], request, result("provider"))
    with pytest.raises(cache_module.CheckAttachmentError):
        cache.selected(1, database[0], [UUID(first.check_id)], None, False)
    attached = cache.selected(
        1, database[0], [UUID(second.check_id)], m.Target(scope="series", id=1), False
    )[0]
    assert attached.service_config_id == 2 and attached.target is None
    tiny = cache_module.CheckCache(max_bytes=1)
    oversized = tiny.put(1, database[0], request, result("provider"))
    assert oversized.attachment_expires_at is None and not tiny.entries


def test_attachment_inputs_accept_only_bounded_server_check_ids():
    for payload in (
        {"check_ids": ["not-a-check"]},
        {"check_ids": [str(uuid4()) for _ in range(6)]},
        {"checks": [{"observations": [{"detail": "FORGED_SECRET"}]}]},
    ):
        with pytest.raises(ValidationError):
            m.BundleOptions.model_validate(payload)


def test_attachment_budget_does_not_evict_or_silently_drop_selected_checks(database):
    cache = cache_module.CheckCache()
    request = m.DiagnosticCheckRequest(kind="provider", service_config_id=1)
    ids = []
    for _ in range(5):
        check = result("provider")
        check.observations[0].detail = "x" * 60000
        saved = cache.put(1, database[0], request, check)
        assert saved.attachment_expires_at
        ids.append(UUID(saved.check_id))
    assert len(cache.selected(1, database[0], ids[:4], None, False)) == 4
    with pytest.raises(cache_module.CheckAttachmentError, match="attachment budget"):
        cache.selected(1, database[0], ids, None, False)
    assert len(cache.entries) == 5
