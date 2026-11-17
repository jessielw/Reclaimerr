from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from test_support_diagnostics import database

from backend.core.diagnostic_trace import current_job_id
from backend.core.operation_diagnostics import (
    current_candidates,
    current_operation_recorder,
    operation_step,
    record_outcome,
)
from backend.database.models import (
    BackgroundJob,
    MovieVersion,
    SupportOperationEvent,
    UpgradeLeftover,
)
from backend.enums import BackgroundJobType, MediaType
from backend.jobs import duplicate_file_ops as ops
from backend.models.diagnostics import BundleOptions, Target
from backend.models.jobs import DuplicateDeleteJobItem, LeftoverDeleteJobItem
from backend.models.live_playback import PlaybackDeferral, PlaybackDeferred
from backend.services.support_bundle import prepare_bundle
from backend.services.support_lookup import lookup_support


def test_duplicate_recorder_preserves_recovery_and_partial_outcomes(
    database, monkeypatch
):
    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(ops, "async_db", sessions)
        first = database[1]["movie_version"]
        second = first + 1

        @ops._observe_duplicate
        async def work(item, **kwargs):
            current_candidates.set((first,))
            try:
                with operation_step(
                    "provider_delete", provider="radarr", subject="provider_request"
                ):
                    raise ConnectionError("connection failed RECOVERY_SECRET")
            except ConnectionError:
                pass
            with operation_step(
                "provider_delete",
                provider="plex",
                path="/private/FILE_SECRET",
                subject="file",
            ):
                async with sessions() as db:
                    await db.execute(
                        delete(MovieVersion).where(MovieVersion.id == first)
                    )
                    await db.commit()
            record_outcome(first, "succeeded", stage="completed")
            current_candidates.set((second,))
            with operation_step(
                "remove_source", path="/private/FAILED_SECRET", subject="file"
            ):
                raise PermissionError("permission denied RAW_SECRET")

        try:
            with pytest.raises(PermissionError):
                await work(
                    DuplicateDeleteJobItem(
                        media_type=MediaType.MOVIE,
                        item_id=database[1]["movie"],
                        version_ids=[first, second],
                        display_label="LABEL_SECRET",
                    ),
                    approved_by="ACTOR_SECRET",
                )
            assert current_operation_recorder.get() is None
            async with sessions() as db:
                rows = (await db.scalars(select(SupportOperationEvent))).all()
                assert all(r.candidate_id is None for r in rows)
                outcomes = {
                    r.target_id: r.detail["outcome"]
                    for r in rows
                    if r.event_type == "item_outcome"
                }
                assert outcomes == {first: "succeeded", second: "failed"}
                assert any(r.error_category == "connection_failed" for r in rows)
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
    assert {e.operation.kind for e in preview.timeline if e.operation} == {"duplicate"}
    assert "SECRET" not in preview.model_dump_json()


def test_job_lookup_merges_recorded_and_not_yet_processed_duplicate_targets(database):
    from uuid import uuid4

    with Session(database[2]) as db:
        first = database[1]["movie_version"]
        job = BackgroundJob(
            job_type=BackgroundJobType.DUPLICATE_DELETE,
            scheduled_at=datetime.now(UTC),
            payload={
                "items": [
                    {
                        "media_type": "movie",
                        "item_id": database[1]["movie"],
                        "version_ids": [first, first + 1],
                    }
                ]
            },
        )
        db.add(job)
        db.flush()
        db.add(
            SupportOperationEvent(
                correlation_id=str(uuid4()),
                event_type="item_outcome",
                occurred_at=datetime.now(UTC),
                target_scope="movie_version",
                target_id=first,
                candidate_id=None,
                job_id=job.id,
                detail={"kind": "duplicate", "action": "delete", "outcome": "failed"},
            )
        )
        db.commit()
        job_id = job.id
    result = lookup_support(database[0], f"job-{job_id}")
    assert result.matches[0].targets == [
        Target(scope="movie_version", id=first),
        Target(scope="movie_version", id=first + 1),
    ]


@pytest.mark.parametrize(
    "outcome", ["succeeded", "deferred", "interrupted", "recording_failed"]
)
def test_leftover_evidence_survives_row_removal_and_recording_is_best_effort(
    database, monkeypatch, outcome
):
    with Session(database[2]) as db:
        row = UpgradeLeftover(
            service_config_id=1,
            arr_movie_id=20,
            movie_id=database[1]["movie"],
            title="private",
            dropped_path="/file",
            local_path="/file",
            size=1,
            link_count=1,
        )
        job = BackgroundJob(
            job_type=BackgroundJobType.DUPLICATE_DELETE, scheduled_at=datetime.now(UTC)
        )
        db.add_all([row, job])
        db.commit()
        row_id, job_id = row.id, job.id

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{database[0]}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(ops, "async_db", sessions)
        if outcome == "recording_failed":

            async def fail(*args, **kwargs):
                raise RuntimeError("recording failed")

            monkeypatch.setattr(ops, "persist_operation_details", fail)

        @ops._observe_duplicate
        async def work(item, **kwargs):
            async with sessions() as db:
                await db.execute(
                    delete(UpgradeLeftover).where(UpgradeLeftover.id == item.id)
                )
                await db.commit()
            if outcome == "deferred":
                raise PlaybackDeferred(
                    PlaybackDeferral(
                        "currently_playing", datetime.now(UTC).isoformat(), (1,)
                    )
                )
            if outcome == "interrupted":
                raise asyncio.CancelledError()
            return 12

        token = current_job_id.set(job_id)
        try:
            if outcome in ("deferred", "interrupted"):
                with pytest.raises(
                    PlaybackDeferred
                    if outcome == "deferred"
                    else asyncio.CancelledError
                ):
                    await work(
                        LeftoverDeleteJobItem(id=row_id, display_label="private"),
                        approved_by="actor",
                    )
            else:
                assert (
                    await work(
                        LeftoverDeleteJobItem(id=row_id, display_label="private"),
                        approved_by="actor",
                    )
                    == 12
                )
            assert current_operation_recorder.get() is None
        finally:
            current_job_id.reset(token)
            await engine.dispose()

    asyncio.run(exercise())
    if outcome == "recording_failed":
        return
    match = lookup_support(database[0], f"job-{job_id}").matches[0]
    assert match.targets == [Target(scope="movie", id=database[1]["movie"])]
    preview = prepare_bundle(
        database[0],
        BundleOptions(support_code=f"job-{job_id}", sections=["timeline"]),
        1,
    )
    final = next(e for e in preview.timeline if e.event == "item_outcome")
    assert final.operation.kind == "upgrade_leftover"
    assert final.candidate_id is None
    assert final.operation.outcome == (
        "unknown" if outcome == "interrupted" else outcome
    )
    if outcome == "deferred":
        assert final.operation.deferral_code == "currently_playing"


def test_non_candidate_history_migration_preserves_candidate_rows(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            migrations = [
                importlib.import_module("backend.alembic.versions." + name)
                for name in (
                    "c4e9a1b6d328_support_diagnostic_history",
                    "d5f0b2c7e439_support_operation_details",
                    "b4d9f2a6c831_support_non_candidate_operations",
                )
            ]
            for migration in migrations:
                monkeypatch.setattr(
                    migration, "op", Operations(MigrationContext.configure(connection))
                )
                migration.upgrade()
            columns = {
                c["name"]: c
                for c in inspect(connection).get_columns("support_operation_events")
            }
            assert columns["candidate_id"]["nullable"]
            for candidate in (1, None):
                connection.exec_driver_sql(
                    "INSERT INTO support_operation_events(correlation_id,event_type,occurred_at,target_scope,target_id,candidate_id) VALUES ('code','item_outcome','2026-11-17','movie',1,?)",
                    (candidate,),
                )
            migrations[-1].downgrade()
            assert connection.exec_driver_sql(
                "SELECT candidate_id FROM support_operation_events"
            ).fetchall() == [(1,)]
            assert not next(
                c
                for c in inspect(connection).get_columns("support_operation_events")
                if c["name"] == "candidate_id"
            )["nullable"]
    finally:
        engine.dispose()
