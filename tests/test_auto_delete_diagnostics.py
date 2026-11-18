from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.core import rule_engine, task_tracking
from backend.core.auto_delete_diagnostics import AutoDeleteRecorder, current_auto_delete
from backend.core.diagnostic_trace import current_scan
from backend.database.models import (
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SupportOperationEvent,
    SupportScanRun,
    TaskRun,
    User,
)
from backend.enums import MediaType, Task, TaskStatus, UserRole
from backend.models.cleanup import (
    MatchedCandidateRecord,
    RulePreviewMatchMetadata,
    RulePreviewMatchResult,
)
from backend.models.diagnostics import BundleOptions, Target
from backend.models.live_playback import CandidateOperationResult
from backend.services import support_bundle, support_history
from backend.services.support_lookup import lookup_support
from backend.tasks import cleanup


@pytest.mark.parametrize("preset", ["candidate", "deletion"])
def test_media_bundle_defaults_include_all_diagnostic_sections(preset):
    assert set(BundleOptions(preset=preset).sections) == set(support_bundle.GROUPS)


def test_daily_delete_run_survives_frequent_refresh_runs(database):
    now = datetime.now(UTC)
    with Session(database[2]) as db:
        old = TaskRun(task=Task.CHECK_APP_UPDATES, status=TaskStatus.COMPLETED)
        old.started_at = now - timedelta(days=10)
        db.add(old)
        deletion = TaskRun(
            task=Task.DELETE_CLEANUP_CANDIDATES, status=TaskStatus.COMPLETED
        )
        deletion.started_at = now - timedelta(hours=12)
        deletion.completed_at = deletion.started_at + timedelta(seconds=5)
        db.add(deletion)
        for i in range(60):
            refresh = TaskRun(
                task=Task.REFRESH_PLAYBACK_HISTORY, status=TaskStatus.COMPLETED
            )
            refresh.started_at = now - timedelta(minutes=60 - i)
            db.add(refresh)
        db.commit()
        deletion_id = deletion.id
    before = hashlib.sha256(database[0].read_bytes()).hexdigest()
    preview = support_bundle.prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="season", id=database[1]["season"]),
            preset="deletion",
            history_days=1,
        ),
        1,
    )
    runs = preview.report.sections.recent_runs
    assert len(runs) == 21
    assert any(run.id == deletion_id and run.status == "completed" for run in runs)
    assert not any(run.task == Task.CHECK_APP_UPDATES.value for run in runs)
    assert any(
        limit.section == "recent_runs" and limit.total == 61
        for limit in preview.report.collection.truncated
    )
    assert hashlib.sha256(database[0].read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "reason",
    [
        "revalidation_not_matched",
        "revalidation_data_unavailable",
        "playback_data_unavailable",
        "revalidation_failed",
        "active_protection",
        "review_period",
        "authorized",
    ],
)
def test_completed_delete_task_exports_why_the_season_remained(
    database, monkeypatch, reason
):
    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        monkeypatch.setattr(task_tracking, "async_db", sessions)
        async with sessions() as db:
            await db.execute(delete(ReclaimCandidate))
            rule = ReclaimRule(
                name="Delete stale season",
                media_type=MediaType.SERIES,
                enabled=True,
                target_scope="season",
                action={"auto_delete_enabled": True, "auto_delete_delay_days": 7},
                definition={
                    "version": 1,
                    "root": {
                        "type": "group",
                        "op": "and",
                        "children": [
                            {
                                "type": "condition",
                                "field": "playback.days_since_last_activity",
                                "operator": "greater_than",
                                "value": 30,
                            },
                        ],
                    },
                },
            )
            db.add(rule)
            await db.flush()
            candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=database[1]["series"],
                season_id=database[1]["season"],
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="Recorded match",
            )
            db.add(candidate)
            await db.flush()
            candidate.created_at = datetime.now(UTC) - timedelta(
                days=0 if reason == "review_period" else 15
            )
            if reason == "active_protection":
                user = User(
                    username="support-test",
                    password_hash="x",
                    role=UserRole.ADMIN,
                    permissions=[],
                )
                db.add(user)
                await db.flush()
                db.add(
                    ProtectedMedia(
                        media_type=MediaType.SERIES,
                        protected_by_user_id=user.id,
                        series_id=candidate.series_id,
                    )
                )
            await db.commit()
            candidate_id = candidate.id

        monkeypatch.setattr(
            rule_engine,
            "_build_context",
            lambda *args: {
                "playback.days_since_last_activity": rule_engine.RULE_VALUE_UNAVAILABLE
                if reason == "revalidation_data_unavailable"
                else 50
                if reason == "authorized"
                else 10
            },
        )

        async def preview(db, rules, **kwargs):
            assert current_scan.get() is not None
            if reason == "revalidation_failed":
                raise RuntimeError("timeout PRIVATE_ERROR_SECRET")
            series = await db.get(Series, database[1]["series"])
            season = await db.get(Season, database[1]["season"])
            rule_engine.evaluate_advanced_rule_state(
                rules[0], target_scope="season", series=series, season=season
            )
            metadata = RulePreviewMatchMetadata()
            matches = []
            if reason == "playback_data_unavailable":
                metadata.playback_error = "connection failed PRIVATE_ERROR_SECRET"
            if reason == "revalidation_data_unavailable":
                metadata.unavailable_rule_ids_by_target[("season", season.id)] = {
                    rules[0].id
                }
            if reason == "authorized":
                matches = [
                    MatchedCandidateRecord(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        season_id=season.id,
                        matched_rule_ids=[rules[0].id],
                        matched_criteria={},
                        reason="match",
                        reason_data=[],
                        estimated_space_bytes=1,
                    )
                ]
            return RulePreviewMatchResult(matches=matches, metadata=metadata)

        monkeypatch.setattr(
            cleanup, "collect_rule_preview_matches_with_metadata", preview
        )
        deletion = AsyncMock(
            return_value=CandidateOperationResult(
                deferrals={candidate_id: {"reason": "playback_active"}}
            )
        )
        monkeypatch.setattr(cleanup, "delete_specific_candidates", deletion)
        try:
            summary = await cleanup.delete_cleanup_candidates()
            assert summary["deleted"] == 0
            assert current_auto_delete.get() is None and current_scan.get() is None
            if reason == "authorized":
                deletion.assert_awaited_once()
                assert summary["deferred"] == 1
            else:
                deletion.assert_not_awaited()
            async with sessions() as db:
                stored = await db.get(ReclaimCandidate, candidate_id)
                assert stored is not None and stored.delete_attempts == 0
                run = await db.scalar(
                    select(TaskRun).where(
                        TaskRun.task == Task.DELETE_CLEANUP_CANDIDATES
                    )
                )
                assert run.status == TaskStatus.COMPLETED
        finally:
            await engine.dispose()
        return summary, candidate_id

    summary, candidate_id = asyncio.run(exercise())
    preview = support_bundle.prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="season", id=database[1]["season"]),
            preset="deletion",
            include_sqlite=True,
        ),
        1,
    )
    assert preview.report.sections.auto_delete_runs[0].summary == summary
    decision = next(event for event in preview.timeline if event.auto_delete)
    assert decision.candidate_id == candidate_id
    assert decision.auto_delete.reason == reason
    assert decision.auto_delete.outcome == (
        "authorized"
        if reason == "authorized"
        else "waiting"
        if reason == "review_period"
        else "skipped"
    )
    if reason in (
        "authorized",
        "playback_data_unavailable",
        "revalidation_not_matched",
    ):
        assert preview.traces[0].conditions[0].actual == (
            50 if reason == "authorized" else 10
        )
        assert preview.traces[0].scan_context["purpose"] == "auto_delete_revalidation"
        assert preview.traces[0].scan_id == decision.correlation_id
    if reason == "revalidation_data_unavailable":
        assert preview.traces[0].conditions[0].availability == "unknown"
        assert preview.traces[0].conditions[0].actual is None
        assert decision.auto_delete.unavailable_rule_ids
    if reason == "revalidation_failed":
        assert decision.error_category == "timeout"
    assert "PRIVATE_ERROR_SECRET" not in preview.model_dump_json()
    assert not any(
        "No automatic-deletion decision" in note
        for note in preview.manifest.limitations
    )

    for kind in ("scan", "operation"):
        code = f"{kind}-{decision.correlation_id}"
        lookup = lookup_support(database[0], code)
        assert lookup.state == "found"
        assert (
            Target(scope="season", id=database[1]["season"])
            in lookup.matches[0].targets
        )
        matching = support_bundle.prepare_bundle(
            database[0],
            BundleOptions(
                target=Target(scope="season", id=database[1]["season"]),
                preset="deletion",
                support_code=code,
            ),
            1,
        )
        assert (
            matching.report.sections.auto_delete_runs[0].correlation_id
            == decision.correlation_id
        )
        assert (
            next(
                event.auto_delete.reason
                for event in matching.timeline
                if event.auto_delete
            )
            == reason
        )


def test_missing_history_is_explicit_and_auto_delete_exports_are_allowlisted(database):
    target = Target(scope="movie_version", id=database[1]["movie_version"])
    empty = support_bundle.prepare_bundle(
        database[0], BundleOptions(target=target, preset="deletion"), 1
    )
    assert any(
        "No automatic-deletion decision" in note for note in empty.manifest.limitations
    )
    now = datetime.now(UTC)
    correlation = str(uuid4())
    with Session(database[2]) as db:
        db.add(
            SupportScanRun(
                id=correlation,
                started_at=now,
                completed_at=now,
                context={
                    "purpose": "auto_delete",
                    "status": "completed",
                    "summary": {
                        "deleted": 0,
                        "skipped": 1,
                        "failed": True,
                        "SECRET": "PRIVATE_SECRET",
                    },
                    "error_category": {"PRIVATE_SECRET": 1},
                },
            )
        )
        for detail in (
            {
                "stage": {"PRIVATE_SECRET": 1},
                "outcome": "skipped",
                "reason": "revalidation_failed",
            },
            {
                "stage": "revalidation",
                "outcome": "skipped",
                "reason": "revalidation_not_matched",
                "rule_ids": [1, True, "PRIVATE_SECRET", {}],
                "coverage": {
                    "playback_unavailable_count": 2,
                    "playback_blocking": False,
                    "seerr_unavailable": True,
                    "SECRET": "PRIVATE_SECRET",
                },
                "blocking_reasons": ["active_protection", "PRIVATE_SECRET"],
            },
        ):
            db.add(
                SupportOperationEvent(
                    correlation_id=correlation,
                    event_type="auto_delete_decision",
                    occurred_at=now,
                    target_scope=target.scope,
                    target_id=target.id,
                    candidate_id=1,
                    movie_id=database[1]["movie"],
                    detail=detail,
                )
            )
        db.commit()
    preview = support_bundle.prepare_bundle(
        database[0], BundleOptions(target=target, preset="deletion"), 1
    )
    assert preview.report.sections.auto_delete_runs[0].summary == {
        "deleted": 0,
        "skipped": 1,
    }
    decision = next(
        event.auto_delete for event in preview.timeline if event.auto_delete
    )
    assert decision.rule_ids == [1]
    assert decision.coverage == {
        "playback_unavailable_count": 2,
        "playback_blocking": False,
        "seerr_unavailable": True,
    }
    assert decision.blocking_reasons == ["active_protection"]
    assert "PRIVATE_SECRET" not in preview.model_dump_json()


def test_history_failure_does_not_change_task_result_or_leave_context(monkeypatch):
    def broken_database():
        raise RuntimeError("diagnostic database unavailable")

    result = {"eligible": 0, "deleted": 0}
    monkeypatch.setattr(
        cleanup, "_delete_cleanup_candidates_unlocked", AsyncMock(return_value=result)
    )
    monkeypatch.setattr(cleanup, "async_db", broken_database)
    assert asyncio.run(cleanup.delete_cleanup_candidates()) == result
    assert current_auto_delete.get() is None


def test_decision_recording_is_bounded_and_preserves_latest_stage():
    recorder = AutoDeleteRecorder(max_records=1)
    candidate = SimpleNamespace(
        id=1,
        movie_id=None,
        movie_version_id=None,
        series_id=2,
        season_id=3,
        episode_id=None,
        matched_rule_ids=[4],
    )
    recorder.capture(
        candidate,
        "selection",
        "eligible",
        "eligible",
        eligible_at="2026-11-09T09:00:40Z",
    )
    recorder.capture(candidate, "revalidation", "skipped", "playback_data_unavailable")
    candidate.id = 2
    recorder.capture(candidate, "selection", "eligible", "eligible")
    assert len(recorder.decisions) == 1 and recorder.dropped == 1
    assert recorder.decisions[1]["detail"]["reason"] == "playback_data_unavailable"
    assert recorder.decisions[1]["detail"]["eligible_at"] == "2026-11-09T09:00:40Z"


@pytest.mark.parametrize(
    "code",
    [
        "currently_playing",
        "playback_unavailable",
        "resync_pending",
        "manual_deletion_scheduled",
    ],
)
def test_existing_deferrals_and_future_operation_reasons_are_exported_without_private_text(
    database, monkeypatch, code
):
    detail = {
        "code": code,
        "checked_at": datetime.now(UTC).isoformat(),
        "server_config_ids": [1, True, "DEFERRAL_SECRET"],
        "message": "DEFERRAL_SECRET",
        "completed_steps": ["Moved /home/DEFERRAL_SECRET/file.mkv"],
    }
    with Session(database[2]) as db:
        candidate = db.get(ReclaimCandidate, 1)
        candidate.playback_deferral = detail
        db.commit()
    target = Target(scope="movie_version", id=database[1]["movie_version"])
    stored = support_bundle.prepare_bundle(
        database[0], BundleOptions(target=target, preset="deletion"), 1
    )
    deferral = stored.report.sections.decisions.candidates[0].playback_deferral
    assert deferral.code == code
    assert deferral.server_config_ids == [1] and deferral.completed_step_count == 1
    assert "DEFERRAL_SECRET" not in stored.model_dump_json()

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        monkeypatch.setattr(support_history, "async_db", sessions)
        monkeypatch.setattr(
            cleanup,
            "_run_playback_guarded_candidates_impl",
            AsyncMock(return_value=CandidateOperationResult(deferrals={1: detail})),
        )
        try:
            result = await cleanup._run_playback_guarded_candidates(
                [1], approved_by="test", moving=False
            )
            assert result.deferred == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    exported = support_bundle.prepare_bundle(
        database[0],
        BundleOptions(target=target, preset="deletion", include_sqlite=True),
        1,
    )
    outcome = next(
        event.operation for event in exported.timeline if event.event == "item_outcome"
    )
    assert outcome.outcome == "deferred" and outcome.deferral_code == code
    assert outcome.stage == (
        "validation"
        if code in ("resync_pending", "manual_deletion_scheduled")
        else "playback_guard"
    )
    assert "DEFERRAL_SECRET" not in exported.model_dump_json()
