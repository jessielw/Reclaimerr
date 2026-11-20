import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from test_scheduled_deletions import database, due_job, seed

from backend.api.routes import delete_requests as requests
from backend.api.routes import scheduled_deletions as schedules
from backend.core.utils.datetime_utils import ensure_utc
from backend.database.models import (
    BackgroundJob,
    DeleteRequest,
    Episode,
    GeneralSettings,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    ScheduledDeletion,
    Season,
    Series,
)
from backend.enums import (
    MediaType,
    Permission,
    ProtectionRequestStatus,
    Service,
    UserRole,
)
from backend.jobs import candidate_file_ops
from backend.jobs import scheduled_deletions as execution
from backend.models.jobs import CandidateFileOpJobPayload
from backend.models.requests import ApproveDeleteRequest
from backend.models.scheduled_deletions import RescheduleDeletionRequest
from backend.models.settings import GeneralSettingsResponse
from backend.services import delete_request_workflow, schedule_workflow


@pytest.fixture
def notifications(monkeypatch):
    approval = AsyncMock()
    monkeypatch.setattr(delete_request_workflow, "notify_user", approval)
    monkeypatch.setattr(candidate_file_ops, "notify_user", AsyncMock())
    monkeypatch.setattr(candidate_file_ops, "notify_admins", AsyncMock())
    monkeypatch.setattr(schedule_workflow, "request_leaving_soon_sync", lambda: None)
    return approval


async def seed_request(db, *, days=0, scope="version", permissions=None):
    user, movie, version, candidate, _ = await seed(
        db,
        role=UserRole.USER if permissions is not None else UserRole.ADMIN,
        permissions=permissions,
    )
    settings = GeneralSettings(delete_request_grace_period_days=days)
    request = DeleteRequest(
        media_type=MediaType.MOVIE,
        requested_by_user_id=user.id,
        movie_id=movie.id,
        movie_version_id=version.id if scope == "version" else None,
        target_scope="movie_version" if scope == "version" else "movie",
    )
    db.add_all([settings, request])
    await db.commit()
    return user, request, candidate, settings


@pytest.mark.parametrize(
    "days,body,delayed",
    [
        (0, {}, False),
        (3, {}, True),
        (3, {"scheduled_at": None}, False),
        (0, {"scheduled_at": "2099-06-01T12:30:00-04:00"}, True),
    ],
)
def test_approval_timing_defaults_and_overrides(
    monkeypatch, notifications, days, body, delayed
):
    async def run():
        queue = AsyncMock()
        monkeypatch.setattr(
            delete_request_workflow, "queue_candidate_file_op_job", queue
        )
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(db, days=days)
            before = datetime.now(UTC)
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(**body), user, db
            )
            assert response.status is ProtectionRequestStatus.APPROVED
            assert bool(response.scheduled_at) is delayed
            assert await db.scalar(select(func.count(ScheduledDeletion.id))) == int(
                delayed
            )
            assert await db.scalar(select(func.count(ReclaimCandidate.id))) == (
                1 if delayed else 2
            )
            if delayed:
                queue.assert_not_awaited()
                schedule = await db.get(
                    ScheduledDeletion, response.scheduled_deletion_id
                )
                job = await db.get(BackgroundJob, schedule.job_id)
                assert job.payload["delete_request_id"] == request.id
                assert job.payload["candidate_ids"] == []
                if days:
                    assert (
                        before + timedelta(days=days)
                        <= ensure_utc(schedule.scheduled_at)
                        <= datetime.now(UTC) + timedelta(days=days)
                    )
                else:
                    assert ensure_utc(schedule.scheduled_at) == datetime(
                        2099, 6, 1, 16, 30, tzinfo=UTC
                    )
                assert (
                    response.scheduled_at in notifications.call_args.kwargs["message"]
                )
            else:
                queue.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("value", ["2099-01-01T12:00:00", "garbage"])
def test_approval_requires_an_aware_datetime(value):
    with pytest.raises(ValidationError):
        ApproveDeleteRequest(scheduled_at=value)


@pytest.mark.parametrize("value", [-1, 0.5, 3651])
def test_grace_period_validation(value):
    with pytest.raises(ValidationError):
        GeneralSettingsResponse(delete_request_grace_period_days=value)


@pytest.mark.parametrize("failure", ["enqueue", "protected", "past"])
def test_failed_scheduling_preserves_pending_request(
    monkeypatch, notifications, failure
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, candidate, _ = await seed_request(db, days=2)
            request_id = request.id
            if failure == "enqueue":
                monkeypatch.setattr(
                    schedule_workflow,
                    "add_schedule_job",
                    AsyncMock(side_effect=RuntimeError("queue unavailable")),
                )
            if failure == "protected":
                db.add(
                    ProtectedMedia(
                        media_type=MediaType.MOVIE,
                        movie_id=request.movie_id,
                        permanent=True,
                    )
                )
                await db.commit()
            body = (
                ApproveDeleteRequest(
                    scheduled_at=datetime.now(UTC) - timedelta(seconds=1)
                )
                if failure == "past"
                else ApproveDeleteRequest()
            )
            with pytest.raises((HTTPException, RuntimeError)):
                await requests.approve_delete_request(request_id, body, user, db)
            async with factory() as check:
                saved = await check.get(DeleteRequest, request_id)
                assert saved.status is ProtectionRequestStatus.PENDING
                assert saved.reviewed_at is None
                assert await check.scalar(select(func.count(ScheduledDeletion.id))) == 0
                assert await check.scalar(select(func.count(BackgroundJob.id))) == 0
                assert await check.scalar(select(func.count(ReclaimCandidate.id))) == 1
            notifications.assert_not_awaited()

    asyncio.run(run())


def test_request_manager_can_reschedule_and_cancel_without_reclaim_permission(
    monkeypatch, notifications
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, settings = await seed_request(
                db, days=2, permissions=[Permission.MANAGE_REQUESTS.value]
            )
            assert (await requests.delete_request_approval_options(user, db))[
                "delete_request_grace_period_days"
            ] == 2
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(), user, db
            )
            schedule = await db.get(ScheduledDeletion, response.scheduled_deletion_id)
            old_job = schedule.job_id
            settings.delete_request_grace_period_days = 7
            await db.commit()
            assert (
                ensure_utc(schedule.scheduled_at).isoformat() == response.scheduled_at
            )
            when = datetime.now(UTC) + timedelta(days=1)
            await schedules.reschedule_deletion(
                schedule.id, RescheduleDeletionRequest(scheduled_at=when), user, db
            )
            assert schedule.job_id != old_job
            assert (
                await execution.run_scheduled_deletion(
                    old_job,
                    CandidateFileOpJobPayload.model_validate(
                        (await db.get(BackgroundJob, old_job)).payload
                    ),
                )
            )["skipped"]
            await schedules.cancel_scheduled_deletion(schedule.id, user, db)
            response = await delete_request_workflow.build_delete_request_response(
                db, request
            )
            assert response.status is ProtectionRequestStatus.APPROVED
            assert response.schedule_state == "canceled"
            request.playback_deferral = {"message": "Playing"}
            await db.commit()
            with pytest.raises(HTTPException, match="Reschedule"):
                await requests.retry_delete_request(request.id, user, db)

    asyncio.run(run())


@pytest.mark.parametrize(
    "outcome", ["success", "deferred", "failure", "revoked", "early", "changed"]
)
def test_scheduled_request_execution_and_outcomes(monkeypatch, notifications, outcome):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(
                db, days=2, permissions=[Permission.MANAGE_REQUESTS.value]
            )
            request_id = request.id
            response = await requests.approve_delete_request(
                request_id, ApproveDeleteRequest(), user, db
            )
            schedule = await db.get(ScheduledDeletion, response.scheduled_deletion_id)
            if outcome == "early":
                job = await db.get(BackgroundJob, schedule.job_id)
            else:
                job = await due_job(db, schedule.id)
            if outcome == "revoked":
                user.permissions = []
                await db.commit()
            if outcome == "changed":
                schedule.target = {
                    **schedule.target,
                    "inventory": {
                        "files": {"/different.mkv": 1000},
                        "directories": [],
                        "unidentified_files": 0,
                    },
                }
                await db.commit()

            async def execute(_job_id, payload):
                async with factory() as check:
                    candidate = await check.get(
                        ReclaimCandidate, payload.candidate_ids[0]
                    )
                    assert candidate.delete_request_id == request_id
                    assert payload.delete_request_id == request_id
                    if outcome == "deferred":
                        candidate.playback_deferral = {"message": "Currently playing"}
                        await check.commit()
                return {
                    "succeeded": int(outcome == "success"),
                    "failed": int(outcome == "failure"),
                    "deferred": int(outcome == "deferred"),
                }

            worker = AsyncMock(side_effect=execute)
            monkeypatch.setattr(
                candidate_file_ops, "_run_candidate_file_op_job_unlocked", worker
            )
            await execution.run_scheduled_deletion(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            await db.refresh(request)
            await db.refresh(schedule)
            assert schedule.state == {
                "success": "completed",
                "failure": "failed",
                "early": "failed",
            }.get(outcome, "blocked")
            assert bool(request.executed_at) is (outcome == "success")
            if outcome == "deferred":
                assert request.playback_deferral["message"] == "Currently playing"
                assert request.execution_error is None
                response = await delete_request_workflow.build_delete_request_response(
                    db, request
                )
                assert response.execution_error is None
                assert response.schedule_state == "blocked"
            elif outcome != "success":
                assert request.execution_error
            if outcome in ("revoked", "early", "changed"):
                worker.assert_not_awaited()
            assert await db.scalar(select(func.count(ReclaimCandidate.id))) == 1
            assert candidate_file_ops.notify_user.await_count == int(
                outcome != "deferred"
            )

    asyncio.run(run())


def test_rescheduling_approved_request_adopts_changed_files(monkeypatch, notifications):
    """#529: new files during the grace period must not strand an approval."""

    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(
                db,
                days=2,
                scope="movie",
                permissions=[Permission.MANAGE_REQUESTS.value],
            )
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(), user, db
            )
            schedule = await db.get(ScheduledDeletion, response.scheduled_deletion_id)
            job = await due_job(db, schedule.id)
            db.add(
                MovieVersion(
                    movie_id=request.movie_id,
                    service=Service.PLEX,
                    service_item_id="movie",
                    service_media_id="upgrade",
                    library_id="movies",
                    library_name="Movies",
                    path="/movies/upgrade.mkv",
                    size=500,
                )
            )
            await db.commit()

            worker = AsyncMock(return_value={"succeeded": 1})
            monkeypatch.setattr(
                candidate_file_ops, "_run_candidate_file_op_job_unlocked", worker
            )
            await execution.run_scheduled_deletion(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            await db.refresh(schedule)
            assert schedule.state == "blocked"
            assert "reschedule to confirm" in schedule.last_error
            worker.assert_not_awaited()

            when = datetime.now(UTC) + timedelta(days=1)
            await schedules.reschedule_deletion(
                schedule.id, RescheduleDeletionRequest(scheduled_at=when), user, db
            )
            await db.refresh(schedule)
            assert schedule.state == "scheduled"
            assert set(schedule.target["inventory"]["files"]) == {
                "/movies/original.mkv",
                "/movies/upgrade.mkv",
            }
            assert schedule.target["estimated_space_bytes"] == 1500

            job = await due_job(db, schedule.id)
            await execution.run_scheduled_deletion(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            await db.refresh(schedule)
            await db.refresh(request)
            assert schedule.state == "completed"
            assert request.executed_at is not None
            worker.assert_awaited_once()

    asyncio.run(run())


def test_rescheduling_approved_request_keeps_its_scope(monkeypatch, notifications):
    """Re-snapshotting adopts new files, never a replaced movie version."""

    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(
                db, days=2, permissions=[Permission.MANAGE_REQUESTS.value]
            )
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(), user, db
            )
            schedule = await db.get(ScheduledDeletion, response.scheduled_deletion_id)
            version = await db.get(MovieVersion, request.movie_version_id)
            version.path = "/movies/replaced.mkv"
            await db.commit()
            with pytest.raises(HTTPException, match="version is no longer available"):
                await schedules.reschedule_deletion(
                    schedule.id,
                    RescheduleDeletionRequest(
                        scheduled_at=datetime.now(UTC) + timedelta(days=1)
                    ),
                    user,
                    db,
                )

    asyncio.run(run())


def test_concurrent_approvals_create_only_one_schedule(monkeypatch, notifications):
    monkeypatch.setattr(
        delete_request_workflow, "candidate_workflow_lock", asyncio.Lock()
    )

    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(db, days=2)
            request_id = request.id

            async def approve():
                async with factory() as session:
                    return await requests.approve_delete_request(
                        request_id, ApproveDeleteRequest(), user, session
                    )

            results = await asyncio.gather(approve(), approve(), return_exceptions=True)
            assert sum(isinstance(result, HTTPException) for result in results) == 1
            assert await db.scalar(select(func.count(ScheduledDeletion.id))) == 1
            assert await db.scalar(select(func.count(BackgroundJob.id))) == 1

    asyncio.run(run())


def test_actual_worker_finalizes_request_once(monkeypatch, notifications):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(db, days=1)
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(), user, db
            )
            job = await due_job(db, response.scheduled_deletion_id)
            monkeypatch.setattr(
                candidate_file_ops,
                "delete_specific_candidates",
                AsyncMock(return_value=(1, 0)),
            )
            result = await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert result["succeeded"] == 1
            await db.refresh(request)
            assert request.executed_at is not None
            candidate_file_ops.notify_user.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["reschedule", "cancel"])
def test_reclaim_permission_alone_cannot_edit_approved_requests(
    monkeypatch, notifications, action
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(db, days=1)
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(), user, db
            )
            schedule = await db.get(ScheduledDeletion, response.scheduled_deletion_id)
            user.role = UserRole.USER
            user.permissions = [Permission.MANAGE_RECLAIM.value]
            await db.commit()
            with pytest.raises(HTTPException) as caught:
                if action == "reschedule":
                    await schedules.reschedule_deletion(
                        schedule.id,
                        RescheduleDeletionRequest(
                            scheduled_at=datetime.now(UTC) + timedelta(days=3)
                        ),
                        user,
                        db,
                    )
                else:
                    await schedules.cancel_scheduled_deletion(schedule.id, user, db)
            assert caught.value.status_code == 403
            assert schedule.state == "scheduled"

    asyncio.run(run())


def test_grace_period_settings_round_trip(monkeypatch, notifications):
    from backend.api.routes.settings.general import (
        get_general_settings,
        update_general_settings,
    )

    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, _ = await seed_request(db)
            current = await get_general_settings(user, db)
            updated = await update_general_settings(
                current.model_copy(update={"delete_request_grace_period_days": 4}),
                user,
                db,
            )
            assert updated.delete_request_grace_period_days == 4
            async with factory() as check:
                assert (await requests.delete_request_approval_options(user, check))[
                    "delete_request_grace_period_days"
                ] == 4

    asyncio.run(run())


def test_migration_preserves_requests_and_standalone_schedules():
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.orm import Session

    from backend.database import Base
    from backend.database.models import User

    migration = importlib.import_module(
        "backend.alembic.versions.c5e0a3b7d942_add_delete_request_scheduling"
    )
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        with Session(connection) as session:
            user = User(username="existing", password_hash="unused")
            session.add(user)
            session.flush()
            session.add_all(
                [
                    GeneralSettings(delete_request_grace_period_days=3),
                    DeleteRequest(
                        media_type=MediaType.MOVIE, requested_by_user_id=user.id
                    ),
                    ScheduledDeletion(
                        scheduled_at=datetime(2099, 1, 1),
                        target={"title": "Existing schedule"},
                        created_by_username="existing",
                    ),
                ]
            )
            session.commit()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        assert (
            connection.scalar(
                sa.text("SELECT delete_request_grace_period_days FROM general_settings")
            )
            == 0
        )
        assert (
            connection.scalar(sa.text("SELECT status FROM delete_requests"))
            == "PENDING"
        )
        assert connection.execute(
            sa.text(
                "SELECT state, delete_request_id, requester_warned_at FROM scheduled_deletions"
            )
        ).one() == ("scheduled", None, None)
        assert any(
            c["column_names"] == ["delete_request_id"]
            for c in sa.inspect(connection).get_unique_constraints(
                "scheduled_deletions"
            )
        )
        connection.execute(
            sa.text("UPDATE scheduled_deletions SET delete_request_id=1")
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                ScheduledDeletion.__table__.insert().values(
                    scheduled_at=datetime(2099, 1, 2),
                    target={},
                    created_by_username="existing",
                    delete_request_id=1,
                )
            )
    engine.dispose()


@pytest.mark.parametrize("scope", ["movie", "version", "series", "season", "episode"])
def test_delayed_approval_preserves_exact_scope(monkeypatch, notifications, scope):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, request, _, _ = await seed_request(db, days=1, scope=scope)
            if scope in ("series", "season", "episode"):
                series = Series(title="Show", tmdb_id=123)
                db.add(series)
                await db.flush()
                season = Season(series_id=series.id, season_number=2)
                db.add(season)
                await db.flush()
                episode = Episode(
                    season_id=season.id,
                    episode_number=3,
                    path="/show/s2/e3.mkv",
                    size=100,
                )
                db.add(episode)
                await db.flush()
                request.media_type = MediaType.SERIES
                request.movie_id = request.movie_version_id = None
                request.series_id = series.id
                request.season_id = season.id if scope != "series" else None
                request.episode_id = episode.id if scope == "episode" else None
                request.target_scope = scope
                await db.commit()
            response = await requests.approve_delete_request(
                request.id, ApproveDeleteRequest(), user, db
            )
            schedule = await db.get(ScheduledDeletion, response.scheduled_deletion_id)
            assert schedule.target["scope"] == scope
            assert schedule.target["movie_version_id"] == request.movie_version_id
            assert schedule.target["season_id"] == request.season_id
            assert schedule.target["episode_id"] == request.episode_id

    asyncio.run(run())
