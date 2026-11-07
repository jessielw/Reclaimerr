import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.calendar import get_calendar
from backend.api.routes.scheduled_deletions import (
    cancel_scheduled_deletion,
    reschedule_deletion,
    schedule_candidate_deletions,
)
from backend.database import Base
from backend.database.models import (
    BackgroundJob,
    Episode,
    EpisodeVersion,
    GeneralSettings,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimRule,
    ScheduledDeletion,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    UpgradeLeftover,
    User,
)
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    MediaType,
    Permission,
    Service,
    UserRole,
)
from backend.jobs import candidate_file_ops, queue
from backend.jobs import scheduled_deletions as schedule_jobs
from backend.models.jobs import CandidateFileOpJobPayload
from backend.models.scheduled_deletions import (
    RescheduleDeletionRequest,
    ScheduleCandidatesRequest,
)
from backend.services.scheduled_deletions import (
    blocking_schedule,
    resolve_target,
    schedule_response,
)
from backend.tasks import cleanup


@asynccontextmanager
async def database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    for module in (candidate_file_ops, queue, schedule_jobs, cleanup):
        monkeypatch.setattr(module, "async_db", factory)
    monkeypatch.setattr(
        candidate_file_ops.PlaybackGuard, "load", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(candidate_file_ops, "reconcile_candidate_arr_tags", AsyncMock())
    try:
        yield factory
    finally:
        await engine.dispose()


async def seed(db, *, role=UserRole.ADMIN, permissions=None):
    user = User(
        username="scheduler",
        password_hash="unused",
        role=role,
        permissions=permissions or [],
    )
    movie = Movie(title="Scheduled movie", tmdb_id=987, year=2020)
    rule = ReclaimRule(
        name="Cleanup",
        media_type=MediaType.MOVIE,
        action={"auto_delete_enabled": True, "auto_delete_delay_days": 0},
    )
    db.add_all([user, movie, rule])
    await db.flush()
    version = MovieVersion(
        movie_id=movie.id,
        service=Service.PLEX,
        service_item_id="movie",
        service_media_id="file",
        library_id="movies",
        library_name="Movies",
        path="/movies/original.mkv",
        size=1000,
    )
    db.add(version)
    await db.flush()
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE,
        movie_id=movie.id,
        movie_version_id=version.id,
        matched_rule_ids=[rule.id],
        matched_criteria={},
        reason="Old",
        estimated_space_bytes=1000,
    )
    db.add(candidate)
    await db.commit()
    return user, movie, version, candidate, rule


async def schedule(db, user, candidate, when=None):
    return (
        await schedule_candidate_deletions(
            ScheduleCandidatesRequest(
                candidate_ids=[candidate.id],
                scheduled_at=when or datetime.now(UTC) + timedelta(days=2),
            ),
            user,
            db,
        )
    )[0]


async def due_job(db, schedule_id):
    saved = await db.get(ScheduledDeletion, schedule_id)
    saved.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    job = await db.get(BackgroundJob, saved.job_id)
    job.scheduled_at = saved.scheduled_at
    await db.commit()
    return job


@pytest.mark.parametrize("value", ["2020-01-01T12:00:00", "not-a-date"])
def test_schedule_requires_explicit_timezone(value):
    with pytest.raises(ValidationError):
        ScheduleCandidatesRequest(candidate_ids=[1], scheduled_at=value)


@pytest.mark.parametrize(
    "role,permissions,allowed",
    [
        (UserRole.USER, [], False),
        (UserRole.USER, [Permission.MANAGE_RECLAIM.value], True),
        (UserRole.USER, [Permission.MANAGE_REQUESTS.value], False),
        (UserRole.ADMIN, [], True),
    ],
)
def test_schedule_permission(monkeypatch, role, permissions, allowed):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(
                db, role=role, permissions=permissions
            )
            if allowed:
                assert (await schedule(db, user, candidate)).state == "scheduled"
            else:
                with pytest.raises(HTTPException) as error:
                    await schedule(db, user, candidate)
                assert error.value.status_code == 403
                assert await db.scalar(select(ScheduledDeletion.id)) is None

    asyncio.run(run())


def test_schedule_atomicity_timezone_and_future_claim(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
                when = datetime.fromisoformat("2099-06-01T12:30:00-04:00")
                saved = await schedule(db, user, candidate, when)
                assert saved.scheduled_at == datetime(2099, 6, 1, 16, 30, tzinfo=UTC)
                record = await db.get(ScheduledDeletion, saved.id)
                assert await db.get(BackgroundJob, record.job_id) is not None
            assert await queue.claim_next_background_job("test") is None
            assert await queue.reset_stale_jobs() == 0
            async with factory() as db:
                assert (await db.get(ScheduledDeletion, saved.id)).state == "scheduled"

    asyncio.run(run())


def test_failed_enqueue_rolls_back_intent(monkeypatch):
    async def run():
        from backend.api.routes import scheduled_deletions as routes

        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
                monkeypatch.setattr(
                    routes,
                    "add_schedule_job",
                    AsyncMock(side_effect=RuntimeError("queue failed")),
                )
                with pytest.raises(RuntimeError):
                    await schedule(db, user, candidate)
                await db.rollback()
            async with factory() as db:
                assert await db.scalar(select(ScheduledDeletion.id)) is None
                assert await db.scalar(select(BackgroundJob.id)) is None

    asyncio.run(run())


def test_past_date_and_missing_batch_target_leave_no_jobs(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            with pytest.raises(HTTPException):
                await schedule(
                    db, user, candidate, datetime.now(UTC) - timedelta(seconds=1)
                )
            with pytest.raises(HTTPException):
                await schedule_candidate_deletions(
                    ScheduleCandidatesRequest(
                        candidate_ids=[candidate.id, 999],
                        scheduled_at=datetime.now(UTC) + timedelta(days=1),
                    ),
                    user,
                    db,
                )
            assert await db.scalar(select(BackgroundJob.id)) is None

    asyncio.run(run())


def test_duplicate_schedule_rejected_and_cancel_restores_eligibility(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            saved = await schedule(db, user, candidate)
            assert await blocking_schedule(db, candidate) is not None
            (
                eligible,
                _,
                waiting,
            ) = await cleanup._select_auto_delete_eligible_candidate_ids()
            assert candidate.id not in eligible and waiting == 1
            with pytest.raises(HTTPException) as error:
                await schedule(db, user, candidate)
            assert error.value.status_code == 409
            await cancel_scheduled_deletion(saved.id, user, db)
            assert await blocking_schedule(db, candidate) is None
            eligible, _, _ = await cleanup._select_auto_delete_eligible_candidate_ids()
            assert candidate.id in eligible

    asyncio.run(run())


def test_unrelated_queued_item_without_files_does_not_block_scheduling(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, rule = await seed(db)
            other = Movie(title="Files already gone", tmdb_id=654, year=2019)
            db.add(other)
            await db.flush()
            stale = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=other.id,
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="Old",
            )
            db.add(stale)
            await db.flush()
            db.add(
                BackgroundJob(
                    job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                    status=BackgroundJobStatus.RUNNING,
                    scheduled_at=datetime.now(UTC),
                    payload={"candidate_ids": [stale.id]},
                )
            )
            await db.commit()
            saved = await schedule(db, user, candidate)
            assert saved.state == "scheduled"

    asyncio.run(run())


def test_reschedule_cancels_old_job_and_old_job_cannot_execute(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            saved = await schedule(db, user, candidate)
            record = await db.get(ScheduledDeletion, saved.id)
            old = await db.get(BackgroundJob, record.job_id)
            await reschedule_deletion(
                saved.id,
                RescheduleDeletionRequest(
                    scheduled_at=datetime.now(UTC) + timedelta(days=4)
                ),
                user,
                db,
            )
            await db.refresh(old)
            assert old.status is BackgroundJobStatus.CANCELED
            assert record.job_id != old.id
            result = await candidate_file_ops.run_candidate_file_op_job(
                old.id, CandidateFileOpJobPayload.model_validate(old.payload)
            )
            assert result["skipped"]
            await db.refresh(record)
            assert record.state == "scheduled"

    asyncio.run(run())


def test_claimed_job_cannot_be_canceled_or_rescheduled(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            saved = await schedule(db, user, candidate)
            job = await due_job(db, saved.id)
            job.status = BackgroundJobStatus.RUNNING
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await cancel_scheduled_deletion(saved.id, user, db)
            assert error.value.status_code == 409
            with pytest.raises(HTTPException):
                await reschedule_deletion(
                    saved.id,
                    RescheduleDeletionRequest(
                        scheduled_at=datetime.now(UTC) + timedelta(days=3)
                    ),
                    user,
                    db,
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "replacement",
        "size",
        "action",
        "routing",
        "protected",
        "pending",
        "revoked",
    ],
)
def test_execution_blocks_changed_or_protected_targets(monkeypatch, change):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, movie, version, candidate, rule = await seed(db)
                saved = await schedule(db, user, candidate)
                job = await due_job(db, saved.id)
                if change == "missing":
                    await db.delete(version)
                elif change == "replacement":
                    version.path = "/movies/replacement.mkv"
                elif change == "size":
                    version.size = 2000
                elif change == "action":
                    rule.action = {"move_instead_of_delete": True}
                elif change == "routing":
                    db.add(GeneralSettings(default_arr_delete_behavior="delete"))
                elif change == "protected":
                    db.add(
                        ProtectedMedia(
                            media_type=MediaType.MOVIE,
                            movie_id=movie.id,
                            protected_by_user_id=user.id,
                            permanent=True,
                        )
                    )
                elif change == "pending":
                    db.add(
                        ProtectionRequest(
                            media_type=MediaType.MOVIE,
                            movie_id=movie.id,
                            requested_by_user_id=user.id,
                            reason="Keep",
                        )
                    )
                elif change == "revoked":
                    user.role = UserRole.USER
                await db.commit()
            delete_files = AsyncMock()
            monkeypatch.setattr(
                candidate_file_ops, "delete_specific_candidates", delete_files
            )
            result = await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert result["failed"] == 1
            delete_files.assert_not_awaited()
            async with factory() as db:
                record = await db.get(ScheduledDeletion, saved.id)
                assert record.state == "blocked" and record.last_error

    asyncio.run(run())


def test_rule_rescan_removal_and_version_resync_preserve_manual_intent(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, movie, version, candidate, _ = await seed(db)
                saved = await schedule(db, user, candidate)
                old_id = version.id
                await db.delete(candidate)
                # Rebuild with a different ID, as a resync does.
                version.service_media_id = "old"
                replacement = MovieVersion(
                    movie_id=movie.id,
                    service=Service.PLEX,
                    service_item_id="new",
                    service_media_id="new",
                    library_id="new-library",
                    library_name="Movies",
                    path=version.path,
                    size=version.size,
                )
                db.add(replacement)
                await db.flush()
                await db.delete(version)
                await db.commit()
                record = await db.get(ScheduledDeletion, saved.id)
                resolved = await resolve_target(db, record.target)
                assert resolved.movie_version_id == replacement.id != old_id
                assert resolved.movie_id == movie.id
                job = await due_job(db, saved.id)

            async def delete_files(ids, **kwargs):
                async with factory() as db:
                    execution_candidate = await db.get(ReclaimCandidate, ids[0])
                    assert execution_candidate.movie_version_id == replacement.id
                    assert await blocking_schedule(db, execution_candidate) is None
                    await db.delete(execution_candidate)
                    await db.commit()
                return 1, 0

            monkeypatch.setattr(
                candidate_file_ops, "delete_specific_candidates", delete_files
            )
            result = await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert result["succeeded"] == 1
            async with factory() as db:
                assert (await db.get(ScheduledDeletion, saved.id)).state == "completed"
                assert await db.scalar(select(ReclaimCandidate.id)) is None

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["season", "episode"])
def test_missing_tv_scope_never_broadens_to_parent(monkeypatch, scope):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, _, _ = await seed(db)
            series = Series(title="Show", tmdb_id=1234)
            db.add(series)
            await db.flush()
            season = Season(series_id=series.id, season_number=1, path="/show/s1")
            db.add(season)
            await db.flush()
            episode = Episode(
                season_id=season.id, episode_number=1, path="/show/s1/e1.mkv", size=100
            )
            db.add(episode)
            await db.flush()
            candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season.id,
                episode_id=episode.id if scope == "episode" else None,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Manual",
            )
            db.add(candidate)
            await db.commit()
            saved = await schedule(db, user, candidate)
            await db.delete(candidate)
            await db.flush()
            await db.delete(episode)
            if scope == "season":
                await db.flush()
                await db.delete(season)
            await db.commit()
            record = await db.get(ScheduledDeletion, saved.id)
            with pytest.raises(HTTPException):
                await resolve_target(db, record.target)
            assert await db.get(Series, series.id) is not None

    asyncio.run(run())


def test_calendar_suppresses_earlier_auto_date_and_survives_candidate_removal(
    monkeypatch,
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            when = datetime(2099, 2, 3, 1, 0, tzinfo=UTC)
            saved = await schedule(db, user, candidate, when)
            calendar = await get_calendar(
                user, when.date() - timedelta(days=1), when.date(), db, -300, 50
            )
            assert calendar.total_items == 1
            assert calendar.days[0].date.isoformat() == "2099-02-02"
            assert calendar.days[0].items[0].scheduled_deletion_id == saved.id
            await db.delete(candidate)
            await db.commit()
            calendar = await get_calendar(
                user, when.date() - timedelta(days=1), when.date(), db, -300, 50
            )
            assert calendar.total_items == 1

    asyncio.run(run())


def test_restart_during_execution_is_visible_and_recoverable(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
                saved = await schedule(db, user, candidate)
                job = await due_job(db, saved.id)
                job.status = BackgroundJobStatus.RUNNING
                record = await db.get(ScheduledDeletion, saved.id)
                record.state = "running"
                await db.commit()
            assert await queue.reset_stale_jobs() == 1
            async with factory() as db:
                record = await db.get(ScheduledDeletion, saved.id)
                assert (await schedule_response(db, record)).state == "failed"
                await reschedule_deletion(
                    saved.id,
                    RescheduleDeletionRequest(
                        scheduled_at=datetime.now(UTC) + timedelta(days=1)
                    ),
                    user,
                    db,
                )
                assert record.state == "scheduled"

    asyncio.run(run())


@pytest.mark.parametrize("moving", [False, True])
def test_shared_delete_and_move_guard_holds_overlapping_parent(monkeypatch, moving):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, movie, _, candidate, _ = await seed(db)
            await schedule(db, user, candidate)
            parent = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Whole movie",
            )
            db.add(parent)
            await db.commit()
            delete_files, move_files, prune = AsyncMock(), AsyncMock(), AsyncMock()
            monkeypatch.setattr(cleanup, "_delete_candidates_prepared", delete_files)
            monkeypatch.setattr(cleanup, "_move_candidates_prepared", move_files)
            monkeypatch.setattr(
                cleanup, "_prune_leaving_soon_before_candidate_actions", prune
            )
            result = await cleanup._run_playback_guarded_candidates_impl(
                [parent.id], approved_by="test", moving=moving
            )
            assert result.deferred == 1
            assert result.deferrals[parent.id]["code"] == "manual_deletion_scheduled"
            delete_files.assert_not_awaited()
            move_files.assert_not_awaited()
            prune.assert_not_awaited()

    asyncio.run(run())


def test_playback_deferral_stays_visible_and_holds_cleanup_until_rescheduled(
    monkeypatch,
):
    from backend.models.live_playback import CandidateOperationResult

    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
                saved = await schedule(db, user, candidate)
                job = await due_job(db, saved.id)

            async def deferred(ids, **kwargs):
                return CandidateOperationResult(
                    deferrals={ids[0]: {"message": "Deferred: currently playing"}}
                )

            monkeypatch.setattr(
                candidate_file_ops, "delete_specific_candidates", deferred
            )
            result = await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert result["deferred"] == 1 and result["failed"] == 0
            async with factory() as db:
                record = await db.get(ScheduledDeletion, saved.id)
                assert record.state == "blocked"
                assert "currently playing" in record.last_error
                original = await db.get(ReclaimCandidate, candidate.id)
                assert await blocking_schedule(db, original) is not None
                await reschedule_deletion(
                    saved.id,
                    RescheduleDeletionRequest(
                        scheduled_at=datetime.now(UTC) + timedelta(minutes=10)
                    ),
                    user,
                    db,
                )
                assert record.state == "scheduled"

    asyncio.run(run())


def test_schedules_protect_same_physical_file_in_another_library(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, movie, version, candidate, _ = await seed(db)
            await schedule(db, user, candidate)
            alias_movie = Movie(title="Alias", tmdb_id=2345)
            db.add(alias_movie)
            await db.flush()
            alias = MovieVersion(
                movie_id=alias_movie.id,
                service=Service.PLEX,
                service_item_id="alias",
                service_media_id="alias",
                library_id="other",
                library_name="Other",
                path=version.path,
                size=version.size,
            )
            db.add(alias)
            await db.flush()
            alias_candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=alias_movie.id,
                movie_version_id=alias.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Alias",
            )
            db.add(alias_candidate)
            await db.commit()
            assert await blocking_schedule(db, alias_candidate) is not None

    asyncio.run(run())


def test_concurrent_scheduling_creates_only_one_intent(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)

            async def submit():
                async with factory() as db:
                    try:
                        return await schedule(db, user, candidate)
                    except HTTPException as error:
                        return error.status_code

            results = await asyncio.gather(submit(), submit())
            assert sum(result == 409 for result in results) == 1
            async with factory() as db:
                assert len((await db.scalars(select(ScheduledDeletion))).all()) == 1
                assert len((await db.scalars(select(BackgroundJob))).all()) == 1

    asyncio.run(run())


def test_migration_matches_schedule_model_and_preserves_existing_rows():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        Base.metadata.create_all(
            connection,
            tables=[
                table
                for table in Base.metadata.sorted_tables
                if table.name != "scheduled_deletions"
            ],
        )
        connection.execute(
            User.__table__.insert().values(
                username="existing",
                password_hash="x",
                role=UserRole.ADMIN,
                permissions=[],
            )
        )
        config = Config()
        config.set_main_option("script_location", "backend/alembic")
        config.attributes["connection"] = connection
        command.stamp(config, "e6a1c3d8f540")
        command.upgrade(config, "head")
        assert {
            column["name"]
            for column in inspect(connection).get_columns("scheduled_deletions")
        } == set(ScheduledDeletion.__table__.columns.keys())
        assert connection.scalar(text("SELECT username FROM users")) == "existing"
    engine.dispose()


def test_disabled_account_cannot_execute_saved_schedule(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
                saved = await schedule(db, user, candidate)
                job = await due_job(db, saved.id)
                user.is_active = False
                await db.commit()
            delete_files = AsyncMock()
            monkeypatch.setattr(
                candidate_file_ops, "delete_specific_candidates", delete_files
            )
            result = await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert result["failed"] == 1
            delete_files.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["cancel", "reschedule"])
def test_recovering_interrupted_schedule_removes_only_its_temporary_candidate(
    monkeypatch, action
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, movie, version, original, rule = await seed(db)
            saved = await schedule(db, user, original)
            record = await db.get(ScheduledDeletion, saved.id)
            job = await db.get(BackgroundJob, record.job_id)
            job.status = BackgroundJobStatus.FAILED
            record.state = "running"
            temporary = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=version.id,
                matched_rule_ids=[rule.id],
                matched_criteria={"scheduled_deletion_id": record.id},
                reason="Interrupted execution",
            )
            db.add(temporary)
            await db.commit()
            temporary_id = temporary.id
            if action == "cancel":
                await cancel_scheduled_deletion(saved.id, user, db)
            else:
                await reschedule_deletion(
                    saved.id,
                    RescheduleDeletionRequest(
                        scheduled_at=datetime.now(UTC) + timedelta(days=1)
                    ),
                    user,
                    db,
                )
            assert (
                await db.scalar(
                    select(ReclaimCandidate.id).where(
                        ReclaimCandidate.id == temporary_id
                    )
                )
                is None
            )
            assert await db.get(ReclaimCandidate, original.id) is not None

    asyncio.run(run())


def test_housekeeping_keeps_future_and_active_schedule_jobs(monkeypatch):
    from backend.enums import BackgroundJobType
    from backend.tasks import house_keeping

    async def run():
        async with database(monkeypatch) as factory:
            monkeypatch.setattr(house_keeping, "async_db", factory)
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
                saved = await schedule(db, user, candidate)
                record = await db.get(ScheduledDeletion, saved.id)
                future_job_id = record.job_id
                running = BackgroundJob(
                    job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                    scheduled_at=datetime.now(UTC),
                    status=BackgroundJobStatus.RUNNING,
                )
                finished = BackgroundJob(
                    job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                    scheduled_at=datetime.now(UTC),
                    status=BackgroundJobStatus.COMPLETED,
                )
                db.add_all([running, finished])
                await db.commit()
                running_id, finished_id = running.id, finished.id
            await house_keeping._trim_background_jobs(0)
            async with factory() as db:
                assert await db.get(BackgroundJob, future_job_id) is not None
                assert await db.get(BackgroundJob, running_id) is not None
                assert await db.get(BackgroundJob, finished_id) is None
                record = await db.get(ScheduledDeletion, saved.id)
                job = await db.get(BackgroundJob, record.job_id)
                job.status = BackgroundJobStatus.FAILED
                record.state = "running"
                await db.commit()
            await house_keeping._trim_background_jobs(0)
            async with factory() as db:
                assert await db.get(BackgroundJob, future_job_id) is not None

    asyncio.run(run())


def test_flat_tv_layout_does_not_block_an_unrelated_season(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, _, _ = await seed(db)
            series = Series(title="Flat show", tmdb_id=4567)
            db.add(series)
            await db.flush()
            db.add(
                SeriesServiceRef(
                    series_id=series.id,
                    service=Service.PLEX,
                    service_id="show",
                    library_id="tv",
                    library_name="TV",
                    path="/show",
                )
            )
            seasons = [
                Season(series_id=series.id, season_number=number, path="/show")
                for number in (1, 2)
            ]
            db.add_all(seasons)
            await db.flush()
            candidates = []
            for season in seasons:
                db.add(
                    Episode(
                        season_id=season.id,
                        episode_number=1,
                        path=f"/show/S{season.season_number}E1.mkv",
                        size=100,
                    )
                )
                candidate = ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="Flat layout",
                )
                db.add(candidate)
                candidates.append(candidate)
            await db.commit()
            await schedule(db, user, candidates[0])
            assert await blocking_schedule(db, candidates[1]) is None
            assert (await schedule(db, user, candidates[1])).state == "scheduled"

    asyncio.run(run())


def test_execution_cleanup_does_not_delete_a_reused_candidate_id(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, movie, _, candidate, _ = await seed(db)
                saved = await schedule(db, user, candidate)
                job = await due_job(db, saved.id)
            reused_id = None

            async def finish_and_create_another_candidate(ids, **kwargs):
                nonlocal reused_id
                async with factory() as db:
                    await db.execute(
                        delete(ReclaimCandidate).where(ReclaimCandidate.id == ids[0])
                    )
                    replacement = ReclaimCandidate(
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                        matched_rule_ids=[],
                        matched_criteria={},
                        reason="Another user's operation",
                    )
                    db.add(replacement)
                    await db.commit()
                    assert replacement.id == ids[0]
                    reused_id = replacement.id
                return 1, 0

            monkeypatch.setattr(
                candidate_file_ops,
                "delete_specific_candidates",
                finish_and_create_another_candidate,
            )
            await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            async with factory() as db:
                assert await db.get(ReclaimCandidate, reused_id) is not None

    asyncio.run(run())


def test_skipping_an_already_running_schedule_does_not_change_its_state(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            saved = await schedule(db, user, candidate)
            job = await due_job(db, saved.id)
            record = await db.get(ScheduledDeletion, saved.id)
            record.state = "running"
            await db.commit()
            result = await candidate_file_ops.run_candidate_file_op_job(
                job.id, CandidateFileOpJobPayload.model_validate(job.payload)
            )
            assert result["skipped"]
            await db.refresh(record)
            assert record.state == "running"

    asyncio.run(run())


@pytest.mark.parametrize("missing_version", [False, True])
def test_tv_schedule_rejects_an_unidentified_file_in_a_larger_scope(
    monkeypatch, missing_version
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, _, _ = await seed(db)
            series = Series(title="Incomplete inventory", tmdb_id=6789)
            db.add(series)
            await db.flush()
            season = Season(series_id=series.id, season_number=1, path="/show/s1")
            db.add(season)
            await db.flush()
            known = Episode(
                season_id=season.id, episode_number=1, path="/show/s1/e1.mkv", size=100
            )
            unknown = Episode(season_id=season.id, episode_number=2, size=100)
            db.add_all([known, unknown])
            await db.flush()
            if missing_version:
                db.add(
                    EpisodeVersion(
                        episode_id=unknown.id,
                        service=Service.PLEX,
                        service_item_id="unknown",
                        service_media_id="unknown",
                        library_id="tv",
                        library_name="TV",
                        size=100,
                        path=None,
                    )
                )
            candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Whole season",
            )
            db.add(candidate)
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await schedule(db, user, candidate)
            assert error.value.status_code == 409

    asyncio.run(run())


@pytest.mark.parametrize("selected_scheduled_file", [True, False])
def test_duplicates_page_cannot_bypass_a_manual_schedule(
    monkeypatch, selected_scheduled_file
):
    from backend.jobs import duplicate_file_ops
    from backend.models.jobs import DuplicateDeleteJobItem
    from backend.services.duplicates import DuplicateActionError

    async def run():
        async with database(monkeypatch) as factory:
            monkeypatch.setattr(duplicate_file_ops, "async_db", factory)
            async with factory() as db:
                user, movie, original, candidate, _ = await seed(db)
                kept = MovieVersion(
                    movie_id=movie.id,
                    service=Service.PLEX,
                    service_item_id="kept",
                    service_media_id="kept",
                    library_id="movies",
                    library_name="Movies",
                    path="/movies/kept.mkv",
                    size=2000,
                )
                db.add(kept)
                await db.commit()
                await schedule(db, user, candidate)
                item = DuplicateDeleteJobItem(
                    media_type=MediaType.MOVIE,
                    item_id=movie.id,
                    version_ids=[original.id if selected_scheduled_file else kept.id],
                    display_label="Movie",
                )
            implementation = AsyncMock(return_value=1000)
            monkeypatch.setattr(
                duplicate_file_ops, "_delete_duplicate_files_impl", implementation
            )
            guard = SimpleNamespace(check=AsyncMock())
            if selected_scheduled_file:
                with pytest.raises(DuplicateActionError, match="scheduled deletion"):
                    await duplicate_file_ops.delete_duplicate_files(
                        item, approved_by="test", playback_guard=guard
                    )
                implementation.assert_not_awaited()
            else:
                assert (
                    await duplicate_file_ops.delete_duplicate_files(
                        item, approved_by="test", playback_guard=guard
                    )
                    == 1000
                )
                implementation.assert_awaited_once()

    asyncio.run(run())


def test_leftover_cannot_remove_a_file_named_by_a_manual_schedule(monkeypatch):
    from backend.jobs import duplicate_file_ops
    from backend.models.jobs import LeftoverDeleteJobItem
    from backend.services.duplicates import DuplicateActionError

    async def run():
        async with database(monkeypatch) as factory:
            monkeypatch.setattr(duplicate_file_ops, "async_db", factory)
            async with factory() as db:
                user, _, original, candidate, _ = await seed(db)
                await schedule(db, user, candidate)
                config = ServiceConfig(
                    service_type=Service.RADARR,
                    base_url="http://unused",
                    api_key="unused",
                )
                db.add(config)
                await db.flush()
                leftover = UpgradeLeftover(
                    service_config_id=config.id,
                    arr_movie_id=1,
                    title="Misidentified leftover",
                    dropped_path=original.path,
                    local_path=original.path,
                    size=original.size,
                    link_count=1,
                )
                db.add(leftover)
                await db.commit()
                item = LeftoverDeleteJobItem(id=leftover.id, display_label="Old copy")
            implementation = AsyncMock(return_value=1000)
            monkeypatch.setattr(
                duplicate_file_ops, "_delete_leftover_impl", implementation
            )
            with pytest.raises(DuplicateActionError, match="scheduled deletion"):
                await duplicate_file_ops.delete_leftover(
                    item,
                    approved_by="test",
                    playback_guard=SimpleNamespace(check=AsyncMock()),
                )
            implementation.assert_not_awaited()

    asyncio.run(run())


def test_tv_hold_uses_season_numbers_after_inventory_ids_are_rebuilt(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, _, _ = await seed(db)
            series = Series(title="Rebuilt TV", tmdb_id=7890)
            db.add(series)
            await db.flush()
            original = Season(series_id=series.id, season_number=1, path="/show/s1")
            db.add(original)
            await db.flush()
            episode = Episode(
                season_id=original.id,
                episode_number=1,
                path="/show/s1/e1.mkv",
                size=100,
            )
            db.add(episode)
            candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=original.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Original",
            )
            db.add(candidate)
            await db.commit()
            original_id = original.id
            await schedule(db, user, candidate)
            await db.delete(candidate)
            await db.delete(episode)
            await db.flush()
            await db.delete(original)
            await db.commit()
            other = Season(series_id=series.id, season_number=2, path="/show/s2")
            db.add(other)
            await db.flush()
            assert other.id == original_id
            rebuilt = Season(series_id=series.id, season_number=1, path="/show/s1")
            db.add(rebuilt)
            await db.flush()
            db.add_all(
                [
                    Episode(
                        season_id=other.id,
                        episode_number=1,
                        path="/show/s2/e1.mkv",
                        size=100,
                    ),
                    Episode(
                        season_id=rebuilt.id, episode_number=1, path=None, size=100
                    ),
                ]
            )
            unrelated = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=other.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Unrelated season",
            )
            held = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=rebuilt.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Rebuilt selected season",
            )
            db.add_all([unrelated, held])
            await db.commit()
            assert await blocking_schedule(db, unrelated) is None
            assert await blocking_schedule(db, held) is not None

    asyncio.run(run())


def test_mapped_physical_aliases_are_held_by_the_schedule(monkeypatch):
    from backend.services.scheduled_deletions import blocking_file_paths

    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, original, candidate, _ = await seed(db)
            db.add(
                GeneralSettings(
                    path_mappings=[
                        {"source_prefix": "/movies", "local_prefix": "/mnt/media"}
                    ]
                )
            )
            await db.commit()
            await schedule(db, user, candidate)
            assert (
                await blocking_file_paths(db, ["/mnt/media/original.mkv"]) is not None
            )
            assert await blocking_file_paths(db, ["/mnt/media/unrelated.mkv"]) is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "saved_path,other_path,blocked",
    [
        ("C:/Movies/Original.mkv", "c:/movies/original.mkv", True),
        ("/movies/Original.mkv", "/movies/original.mkv", False),
    ],
)
def test_hold_path_case_follows_windows_or_posix_identity(
    monkeypatch, saved_path, other_path, blocked
):
    from backend.services.scheduled_deletions import blocking_file_paths

    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, version, candidate, _ = await seed(db)
            version.path = saved_path
            await db.commit()
            await schedule(db, user, candidate)
            assert (await blocking_file_paths(db, [other_path]) is not None) is blocked

    asyncio.run(run())


def test_schedule_api_create_list_reschedule_cancel_and_validation(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from backend.api.routes.scheduled_deletions import router
    from backend.core.auth import get_current_user
    from backend.database import get_db

    async def run():
        async with database(monkeypatch) as factory:
            async with factory() as db:
                user, _, _, candidate, _ = await seed(db)
            app = FastAPI()
            app.include_router(router)

            async def current_user():
                return user

            async def session():
                async with factory() as db:
                    try:
                        yield db
                    except Exception:
                        await db.rollback()
                        raise

            app.dependency_overrides[get_current_user] = current_user
            app.dependency_overrides[get_db] = session
            when = (datetime.now(UTC) + timedelta(days=2)).isoformat()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                naive = await client.post(
                    "/api/media/candidates/schedule-deletion",
                    json={
                        "candidate_ids": [candidate.id],
                        "scheduled_at": "2099-01-01T12:00:00",
                    },
                )
                assert naive.status_code == 422
                created = await client.post(
                    "/api/media/candidates/schedule-deletion",
                    json={"candidate_ids": [candidate.id], "scheduled_at": when},
                )
                assert created.status_code == 200, created.text
                schedule_id = created.json()[0]["id"]
                listed = await client.get("/api/media/scheduled-deletions")
                assert (
                    listed.status_code == 200 and listed.json()[0]["id"] == schedule_id
                )
                updated = await client.post(
                    f"/api/media/scheduled-deletions/{schedule_id}/reschedule",
                    json={
                        "scheduled_at": (
                            datetime.now(UTC) + timedelta(days=3)
                        ).isoformat()
                    },
                )
                assert updated.status_code == 200, updated.text
                canceled = await client.post(
                    f"/api/media/scheduled-deletions/{schedule_id}/cancel", json={}
                )
                assert canceled.status_code == 200
                assert (await client.get("/api/media/scheduled-deletions")).json() == []
                user.role = UserRole.USER
                forbidden = await client.post(
                    "/api/media/candidates/schedule-deletion",
                    json={"candidate_ids": [candidate.id], "scheduled_at": when},
                )
                assert forbidden.status_code == 403

    asyncio.run(run())


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
def test_whitespace_paths_are_not_accepted_as_identifiable_files(
    monkeypatch, media_type
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, version, candidate, _ = await seed(db)
            if media_type is MediaType.MOVIE:
                version.path = "   "
            else:
                series = Series(title="Blank path", tmdb_id=9012)
                db.add(series)
                await db.flush()
                season = Season(series_id=series.id, season_number=1)
                db.add(season)
                await db.flush()
                db.add(
                    Episode(season_id=season.id, episode_number=1, path="   ", size=100)
                )
                candidate = ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="Blank",
                )
                db.add(candidate)
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await schedule(db, user, candidate)
            assert error.value.status_code == 409

    asyncio.run(run())


def test_bulk_schedule_accepts_more_than_200_version_rows(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, movie, _, original, _ = await seed(db)
            versions = [
                MovieVersion(
                    movie_id=movie.id,
                    service=Service.PLEX,
                    service_item_id=f"item-{index}",
                    service_media_id=f"file-{index}",
                    library_id="movies",
                    library_name="Movies",
                    path=f"/movies/copy-{index}.mkv",
                    size=1000,
                )
                for index in range(200)
            ]
            db.add_all(versions)
            await db.flush()
            candidates = [
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=version.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="Bulk selected",
                )
                for version in versions
            ]
            db.add_all(candidates)
            await db.commit()
            selected_ids = [original.id, *(candidate.id for candidate in candidates)]
            response = await schedule_candidate_deletions(
                ScheduleCandidatesRequest(
                    candidate_ids=selected_ids,
                    scheduled_at=datetime.now(UTC) + timedelta(days=1),
                ),
                user,
                db,
            )
            assert len(response) == 201
            assert len((await db.scalars(select(BackgroundJob.id))).all()) == 201

    asyncio.run(run())


def test_batch_rejects_mapped_aliases_before_creating_conflicting_schedules(
    monkeypatch,
):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            user, _, _, candidate, _ = await seed(db)
            db.add(
                GeneralSettings(
                    path_mappings=[
                        {"source_prefix": "/movies", "local_prefix": "/mnt/media"}
                    ]
                )
            )
            alias_movie = Movie(title="Mapped alias", tmdb_id=8901)
            db.add(alias_movie)
            await db.flush()
            alias_version = MovieVersion(
                movie_id=alias_movie.id,
                service=Service.PLEX,
                service_item_id="alias",
                service_media_id="alias",
                library_id="alias",
                library_name="Alias",
                path="/mnt/media/original.mkv",
                size=1000,
            )
            db.add(alias_version)
            await db.flush()
            alias = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=alias_movie.id,
                movie_version_id=alias_version.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Alias",
            )
            db.add(alias)
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await schedule_candidate_deletions(
                    ScheduleCandidatesRequest(
                        candidate_ids=[candidate.id, alias.id],
                        scheduled_at=datetime.now(UTC) + timedelta(days=1),
                    ),
                    user,
                    db,
                )
            assert error.value.status_code == 409
            assert await db.scalar(select(ScheduledDeletion.id)) is None
            assert await db.scalar(select(BackgroundJob.id)) is None

    asyncio.run(run())
