from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.database.models import (
    GeneralSettings,
    ReclaimCandidate,
    MovieVersion,
    ProtectedMedia,
    SupportOperationEvent,
)
from backend.enums import MediaType
from backend.models.diagnostics import BundleOptions, Target
from backend.models.live_playback import CandidateOperationResult
from backend.services import support_history
from backend.services.support_bundle import prepare_bundle
from backend.tasks import cleanup
from backend.core.auto_delete_diagnostics import AutoDeleteRecorder


@pytest.mark.parametrize("scope", ["movie_version", "series", "season", "episode"])
@pytest.mark.parametrize("moving", [False, True])
def test_late_favorites_guard_exports_skip_without_changing_cleanup_counts(
    database, monkeypatch, scope, moving
):
    with Session(database[2]) as db:
        db.execute(delete(ReclaimCandidate))
        settings = db.query(GeneralSettings).one()
        settings.favorites_ignore_enabled = True
        settings.favorites_protect_all_users = True
        settings.favorites_usernames = ["FAVORITE_SECRET"]
        fields = (
            {
                "movie_id": database[1]["movie"],
                "movie_version_id": database[1]["movie_version"],
            }
            if scope == "movie_version"
            else {"series_id": database[1]["series"]}
        )
        if scope in ("season", "episode"):
            fields["season_id"] = database[1]["season"]
        if scope == "episode":
            fields["episode_id"] = database[1]["episode"]
        candidate = ReclaimCandidate(
            media_type=MediaType.MOVIE
            if scope == "movie_version"
            else MediaType.SERIES,
            matched_rule_ids=[],
            matched_criteria={},
            reason="private",
            **fields,
        )
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        monkeypatch.setattr(support_history, "async_db", sessions)
        monkeypatch.setattr(
            cleanup,
            "_load_favorite_tmdb_ids",
            AsyncMock(return_value={123 if scope == "movie_version" else 321}),
        )

        async def work(ids, **kwargs):
            if moving:
                succeeded, failed = await cleanup._move_specific_candidates_impl(
                    ids, approved_by="ACTOR_SECRET"
                )
                assert (succeeded, failed) == (0, 0)
            else:
                function = {
                    "movie_version": cleanup._delete_movie_candidates,
                    "series": cleanup._delete_series_candidates,
                    "season": cleanup._delete_season_candidates,
                    "episode": cleanup._delete_episode_candidates,
                }[scope]
                assert await function(restrict_to_ids=frozenset(ids)) == 0
                await cleanup._diagnose_unhandled_delete_failures(ids)
                succeeded, failed = 0, 1
            return CandidateOperationResult(succeeded=succeeded, failed=failed)

        monkeypatch.setattr(cleanup, "_run_playback_guarded_candidates_impl", work)
        try:
            result = await cleanup._run_playback_guarded_candidates(
                [candidate_id], approved_by="ACTOR_SECRET", moving=moving
            )
            assert result.succeeded == 0 and result.failed == (0 if moving else 1)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope=scope, id=database[1][scope]), sections=["timeline"]
        ),
        1,
    )
    final = next(e for e in preview.timeline if e.event == "item_outcome")
    assert final.operation.outcome == "skipped"
    assert final.operation.reason == "favorites_protection"
    assert final.operation.stage == "validation"
    assert final.operation.action == ("move" if moving else "delete")
    assert final.error_category is None
    assert "SECRET" not in preview.model_dump_json()


def test_sibling_guard_records_why_a_twin_file_was_held(database, monkeypatch):
    with Session(database[2]) as db:
        first = db.get(MovieVersion, database[1]["movie_version"])
        twin = db.get(MovieVersion, first.id + 1)
        twin.path = first.path
        db.add(
            ProtectedMedia(
                media_type=MediaType.MOVIE,
                protected_by_user_id=1,
                movie_id=first.movie_id,
                movie_version_id=twin.id,
            )
        )
        db.commit()

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        monkeypatch.setattr(support_history, "async_db", sessions)

        async def work(ids, **kwargs):
            async with sessions() as db:
                candidate = await db.get(ReclaimCandidate, 1)
                (
                    kept,
                    blocked,
                ) = await cleanup._filter_movie_candidates_by_sibling_protection(
                    db, [candidate], []
                )
                assert not kept and blocked == [1]
            await cleanup._diagnose_unhandled_delete_failures(ids)
            return CandidateOperationResult(failed=1)

        monkeypatch.setattr(cleanup, "_run_playback_guarded_candidates_impl", work)
        try:
            await cleanup._run_playback_guarded_candidates(
                [1], approved_by="test", moving=False
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    preview = prepare_bundle(
        database[0],
        BundleOptions(
            target=Target(scope="movie", id=database[1]["movie"]), sections=["timeline"]
        ),
        1,
    )
    final = next(e for e in preview.timeline if e.event == "item_outcome")
    assert (
        final.operation.outcome == "skipped"
        and final.operation.reason == "sibling_protection"
    )


def test_actionable_capture_displaces_passive_rows_and_discloses_every_drop():
    recorder = AutoDeleteRecorder(max_records=3)
    candidate = SimpleNamespace(
        id=1,
        movie_id=1,
        movie_version_id=1,
        series_id=None,
        season_id=None,
        episode_id=None,
        matched_rule_ids=[1],
    )

    def capture(identifier, stage, outcome, reason, **detail):
        candidate.id = candidate.movie_version_id = identifier
        recorder.capture(candidate, stage, outcome, reason, **detail)

    capture(1, "selection", "waiting", "review_period")
    capture(2, "selection", "skipped", "auto_delete_disabled")
    capture(3, "selection", "skipped", "canceled")
    capture(4, "selection", "skipped", "active_protection")
    capture(5, "selection", "eligible", "eligible", eligible_at="2026-11-17T00:00:00Z")
    capture(6, "revalidation", "skipped", "revalidation_not_matched")
    capture(7, "dispatch", "authorized", "authorized")
    capture(8, "selection", "waiting", "postponed")
    capture(5, "revalidation", "skipped", "revalidation_failed")
    capture(9, "selection", "eligible", "eligible")
    assert set(recorder.decisions) == {5, 6, 7}
    assert recorder.dropped == 6
    assert recorder.decisions[5]["detail"]["eligible_at"] == "2026-11-17T00:00:00Z"
    assert recorder.decisions[5]["detail"]["reason"] == "revalidation_failed"


@pytest.mark.parametrize("terminal_overflow", [False, True])
def test_retention_keeps_latest_terminal_evidence_and_still_enforces_caps(
    database, terminal_overflow
):
    now = datetime.now(UTC)
    code = str(uuid4())

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                common = {
                    "correlation_id": code,
                    "target_scope": "movie",
                    "candidate_id": None,
                }
                rows = [
                    {
                        **common,
                        "target_id": 1,
                        "event_type": "item_outcome",
                        "occurred_at": now - timedelta(hours=2),
                        "detail": {"outcome": "failed"},
                    },
                    {
                        **common,
                        "target_id": 1,
                        "event_type": "item_outcome",
                        "occurred_at": now - timedelta(hours=1),
                        "detail": {"outcome": "succeeded"},
                    },
                    {
                        **common,
                        "target_id": 1,
                        "event_type": "auto_delete_decision",
                        "occurred_at": now - timedelta(hours=1),
                        "detail": {"reason": "authorized"},
                    },
                    {
                        **common,
                        "target_id": 2,
                        "event_type": "item_outcome",
                        "occurred_at": now - timedelta(days=15),
                        "detail": {"outcome": "succeeded"},
                    },
                ]
                rows += [
                    {
                        **common,
                        "target_id": 100 + i if terminal_overflow else 9999,
                        "event_type": "item_outcome"
                        if terminal_overflow
                        else "operation_step",
                        "occurred_at": now
                        - timedelta(minutes=10)
                        + timedelta(milliseconds=i),
                        "detail": {"outcome": "unknown"},
                    }
                    for i in range(5001)
                ]
                await db.execute(insert(SupportOperationEvent), rows)
                await support_history.trim_history(db)
                await db.commit()
                assert (
                    await db.scalar(
                        select(func.count()).select_from(SupportOperationEvent)
                    )
                    == 5000
                )
                selected = (
                    await db.scalars(
                        select(SupportOperationEvent).where(
                            SupportOperationEvent.target_id == 1
                        )
                    )
                ).all()
                assert not await db.scalar(
                    select(SupportOperationEvent.id).where(
                        SupportOperationEvent.target_id == 2
                    )
                )
                if terminal_overflow:
                    assert not selected
                    assert not await db.scalar(
                        select(SupportOperationEvent.id).where(
                            SupportOperationEvent.target_id == 100
                        )
                    )
                else:
                    assert {row.event_type for row in selected} == {
                        "item_outcome",
                        "auto_delete_decision",
                    }
                    assert (
                        next(
                            row for row in selected if row.event_type == "item_outcome"
                        ).detail["outcome"]
                        == "succeeded"
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
