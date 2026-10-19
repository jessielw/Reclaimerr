"""Isolated audit reproducers; assertions describe the required safe behavior."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    CandidateResyncIdentity,
    DeleteRequest,
    DeleteRequestResyncIdentity,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service
from backend.tasks import sync


@asynccontextmanager
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


def version(movie_id, key):
    return MovieVersion(
        movie_id=movie_id,
        service=Service.PLEX,
        service_item_id="item-" + key,
        service_media_id=key,
        library_id="library",
        library_name="Movies",
        path=f"/movies/{key}/film.mkv",
        size=1000,
        video_width=1920,
        video_height=1080,
        video_codec="h264",
    )


@asynccontextmanager
async def no_tracking(*args, **kwargs):
    yield


@pytest.mark.parametrize("rebuilt_paths", [("original", "keep"), ("keep",), ()])
def test_resync_keeps_delete_request_bound_to_original_file(monkeypatch, rebuilt_paths):
    async def run():
        async with database() as factory:
            monkeypatch.setattr(sync, "async_db", factory)
            monkeypatch.setattr(sync, "track_task_execution", no_tracking)
            monkeypatch.setattr(
                sync, "service_manager", SimpleNamespace(main_media_server=object())
            )
            monkeypatch.setattr(sync, "sync_media_libraries", AsyncMock())
            monkeypatch.setattr(sync, "sync_series", AsyncMock())
            monkeypatch.setattr(sync, "_run_supplemental_syncs", AsyncMock())
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=2000)
                user = User(username="audit", password_hash="unused")
                db.add_all([movie, user])
                await db.flush()
                original = version(movie.id, "original")
                db.add_all([original, version(movie.id, "keep")])
                await db.flush()
                request = DeleteRequest(
                    media_type=MediaType.MOVIE,
                    requested_by_user_id=user.id,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    # Legacy requests need their scope captured too.
                    target_scope=None,
                )
                db.add(request)
                await db.commit()
                request_id, movie_id = request.id, movie.id

            async def rebuild(**kwargs):
                async with factory() as db:
                    db.add_all([version(movie_id, key) for key in rebuilt_paths])
                    await db.flush()
                    await sync._restore_pending_movie_scopes(db, {movie_id})
                    await db.commit()

            monkeypatch.setattr(sync, "sync_movies", rebuild)
            await sync.resync_media()
            async with factory() as db:
                request = await db.get(DeleteRequest, request_id)
                assert request.target_scope == "movie_version"
                if "original" in rebuilt_paths:
                    restored = await db.get(MovieVersion, request.movie_version_id)
                    assert restored.path == "/movies/original/film.mkv"
                else:
                    from fastapi import HTTPException

                    from backend.api.routes.delete_requests import (
                        approve_delete_request,
                    )
                    from backend.models.requests import ReviewDeleteRequest

                    with pytest.raises(HTTPException) as exc:
                        await approve_delete_request(
                            request_id, ReviewDeleteRequest(), user, db
                        )
                    assert exc.value.status_code == 409
                    assert not (await db.scalars(select(ReclaimCandidate))).all()

    asyncio.run(run())


@pytest.mark.parametrize("request_candidate", [False, True])
def test_interrupted_resync_recovers_on_later_ordinary_sync(
    monkeypatch, request_candidate
):
    from backend.enums import ProtectionRequestStatus
    from backend.jobs import candidate_file_ops
    from backend.models.cleanup import MatchedCandidateRecord
    from backend.models.media import AggregatedMovieData, ExternalIDs, MovieVersionData
    from backend.tasks import cleanup

    async def run():
        async with database() as factory:
            for module in (sync, cleanup, candidate_file_ops):
                monkeypatch.setattr(module, "async_db", factory)
            monkeypatch.setattr(sync, "track_task_execution", no_tracking)
            manager = SimpleNamespace(
                main_media_server=object(), radarr=None, radarr_clients=lambda: {}
            )
            monkeypatch.setattr(sync, "service_manager", manager)
            monkeypatch.setattr(
                sync,
                "sync_media_libraries",
                AsyncMock(side_effect=RuntimeError("offline")),
            )
            monkeypatch.setattr(sync, "_update_movie_tmdb_metadata", AsyncMock())
            monkeypatch.setattr(
                sync,
                "AsyncTMDBClient",
                lambda: SimpleNamespace(session=SimpleNamespace(close=AsyncMock())),
            )
            monkeypatch.setattr(cleanup, "_warn_on_reflagged_media", AsyncMock())
            original_time = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=6)
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                user = User(username="audit", password_hash="unused")
                db.add_all(
                    [
                        movie,
                        user,
                        ServiceConfig(
                            service_type=Service.PLEX,
                            name="main",
                            base_url="http://unused",
                            api_key="unused",
                            enabled=True,
                            is_main=True,
                        ),
                    ]
                )
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.flush()
                request = DeleteRequest(
                    media_type=MediaType.MOVIE,
                    requested_by_user_id=user.id,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    status=ProtectionRequestStatus.APPROVED
                    if request_candidate
                    else ProtectionRequestStatus.PENDING,
                )
                db.add(request)
                await db.flush()
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="one file",
                    reason_data=[],
                    delete_request_id=request.id if request_candidate else None,
                )
                candidate.created_at = original_time
                candidate.auto_delete_timer_started_at = original_time
                db.add(candidate)
                await db.commit()
                movie_id, candidate_id, request_id = movie.id, candidate.id, request.id

            # A second failed reset must preserve identities saved by the first.
            for _ in range(2):
                with pytest.raises(RuntimeError, match="offline"):
                    await sync.resync_media()
            async with factory() as db:
                assert (
                    await db.get(CandidateResyncIdentity, candidate_id)
                ).path == "/movies/original/film.mkv"
                assert (
                    await db.get(DeleteRequestResyncIdentity, request_id)
                ).path == "/movies/original/film.mkv"
                assert await cleanup._sync_movie_version_candidates(db, []) == (0, 0, 0)

            # Unavailable inventory cannot consume pending identities.
            gather = AsyncMock(return_value=None)
            monkeypatch.setattr(sync, "gather_movies", gather)
            await sync.sync_movies()
            gather.return_value = {
                123: AggregatedMovieData(
                    name="Audit",
                    year=None,
                    external_ids=ExternalIDs(123, None, None, None),
                    versions=[
                        MovieVersionData(
                            Service.PLEX,
                            "new-item",
                            "new-media",
                            "library",
                            "Movies",
                            "/movies/original/film.mkv",
                            1000,
                            None,
                        )
                    ],
                    view_count=0,
                    last_viewed_at=None,
                )
            }
            # Even after version batches commit, a failed sync is not recovery.
            manager.radarr = SimpleNamespace(
                get_all_movies=AsyncMock(side_effect=RuntimeError("arr offline"))
            )
            with pytest.raises(RuntimeError, match="arr offline"):
                await sync.sync_movies()
            async with factory() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate.movie_id is None and candidate.movie_version_id is None
                rebuilt = (await db.scalars(select(MovieVersion))).one()
                record = MatchedCandidateRecord(
                    MediaType.MOVIE,
                    [],
                    {},
                    "one file",
                    [],
                    1000,
                    movie_id=movie_id,
                    movie_version_id=rebuilt.id,
                )
                assert await cleanup._sync_movie_version_candidates(db, [record]) == (
                    0,
                    0,
                    0,
                )
                assert len((await db.scalars(select(ReclaimCandidate))).all()) == 1

            guard = SimpleNamespace(
                check=AsyncMock(), expand_local_target=lambda *args, **kwargs: None
            )
            execute = AsyncMock(return_value=(1, 0))
            monkeypatch.setattr(cleanup, "_delete_candidates_prepared", execute)
            for moving in (False, True):
                result = await cleanup._run_playback_guarded_candidates(
                    [candidate_id],
                    approved_by="test",
                    moving=moving,
                    playback_guard=guard,
                )
                assert result.deferred == 1 and result.succeeded == result.failed == 0
                assert result.deferrals[candidate_id]["code"] == "resync_pending"
            execute.assert_not_awaited()
            guard.check.assert_not_awaited()
            assert (await cleanup._select_auto_delete_eligible_candidate_ids())[0] == []
            if request_candidate:
                await candidate_file_ops._finalize_delete_request_job(
                    delete_request_id=request_id,
                    candidate_ids=[candidate_id],
                    succeeded=0,
                    failed=0,
                    deferred=1,
                )
                async with factory() as db:
                    request = await db.get(DeleteRequest, request_id)
                    assert request.playback_deferral["code"] == "resync_pending"
                    assert request.executed_at is None
                    assert await db.get(ReclaimCandidate, candidate_id) is not None

            manager.radarr = None
            await sync.sync_movies()
            async with factory() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                request = await db.get(DeleteRequest, request_id)
                restored = await db.get(MovieVersion, candidate.movie_version_id)
                assert restored.path == "/movies/original/film.mkv"
                assert candidate.movie_id == movie_id
                assert request.movie_version_id == restored.id
                assert request.target_scope == "movie_version"
                assert (
                    candidate.created_at
                    == candidate.auto_delete_timer_started_at
                    == original_time
                )
                assert await db.get(CandidateResyncIdentity, candidate_id) is None
                assert await db.get(DeleteRequestResyncIdentity, request_id) is None

    asyncio.run(run())


@pytest.mark.parametrize("failure_stage", ["sync_media_libraries", "sync_movies"])
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_failed_resync_does_not_leave_whole_movie_candidate(
    monkeypatch, failure_stage, error_type
):
    async def run():
        async with database() as factory:
            monkeypatch.setattr(sync, "async_db", factory)
            monkeypatch.setattr(sync, "track_task_execution", no_tracking)
            monkeypatch.setattr(
                sync, "service_manager", SimpleNamespace(main_media_server=object())
            )
            monkeypatch.setattr(sync, "sync_media_libraries", AsyncMock())
            monkeypatch.setattr(
                sync, failure_stage, AsyncMock(side_effect=error_type("server offline"))
            )
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.flush()
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="one file",
                    reason_data=[],
                )
                db.add(candidate)
                await db.commit()
                candidate_id = candidate.id
            with pytest.raises(error_type, match="server offline"):
                await sync.resync_media()
            async with factory() as db:
                candidate = await db.get(ReclaimCandidate, candidate_id)
                assert candidate is not None
                assert candidate.movie_id is None
                assert candidate.movie_version_id is None
                from backend.tasks import cleanup

                monkeypatch.setattr(cleanup, "async_db", factory)
                assert (
                    await cleanup._delete_movie_candidates(frozenset({candidate_id}))
                    == 0
                )

    asyncio.run(run())


def test_confirmed_empty_inventory_retires_suspended_candidates(monkeypatch):
    async def run():
        async with database() as factory:
            monkeypatch.setattr(sync, "async_db", factory)
            monkeypatch.setattr(sync, "gather_movies", AsyncMock(return_value={}))
            async with factory() as db:
                config = ServiceConfig(
                    service_type=Service.PLEX,
                    name="main",
                    base_url="http://unused",
                    api_key="unused",
                    enabled=True,
                    is_main=True,
                )
                user = User(username="audit", password_hash="unused")
                db.add_all([config, user])
                await db.flush()
                for index in range(20):
                    movie = Movie(title=f"Movie {index}", tmdb_id=index + 1, size=1000)
                    db.add(movie)
                    await db.flush()
                    # A previous partial sync may have committed stale versions.
                    db.add(version(movie.id, str(index)))
                    candidate = ReclaimCandidate(
                        media_type=MediaType.MOVIE,
                        matched_rule_ids=[],
                        matched_criteria={},
                        reason="one file",
                    )
                    request = DeleteRequest(
                        media_type=MediaType.MOVIE,
                        requested_by_user_id=user.id,
                        movie_id=movie.id,
                        target_scope="movie_version",
                    )
                    db.add_all([candidate, request])
                    await db.flush()
                    path = f"/movies/{index}/film.mkv"
                    db.add_all(
                        [
                            CandidateResyncIdentity(candidate.id, movie.id, path),
                            DeleteRequestResyncIdentity(request.id, movie.id, path),
                        ]
                    )
                await db.commit()
            await sync.sync_movies()
            async with factory() as db:
                assert (
                    len((await db.scalars(select(CandidateResyncIdentity))).all()) == 20
                )
                assert all(
                    c.movie_id is None
                    for c in (await db.scalars(select(ReclaimCandidate))).all()
                )
            await sync.sync_movies()
            async with factory() as db:
                assert not (await db.scalars(select(ReclaimCandidate))).all()
                assert not (await db.scalars(select(CandidateResyncIdentity))).all()
                assert (
                    len((await db.scalars(select(DeleteRequestResyncIdentity))).all())
                    == 20
                )
                assert all(
                    r.movie_version_id is None
                    for r in (await db.scalars(select(DeleteRequest))).all()
                )
                assert all(
                    m.removed_at for m in (await db.scalars(select(Movie))).all()
                )

    asyncio.run(run())


def test_resync_identity_migration_roundtrip_and_cascades(monkeypatch):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    migration = importlib.import_module(
        "backend.alembic.versions.b3d8f0a5c217_persist_resync_file_identities"
    )
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            for table in ("movies", "reclaim_candidates", "delete_requests"):
                connection.execute(
                    text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                )
                connection.execute(text(f"INSERT INTO {table} VALUES (1), (2)"))
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(connection))
            )
            migration.upgrade()
            for table in (
                "candidate_resync_identities",
                "delete_request_resync_identities",
            ):
                connection.execute(
                    text(
                        f"INSERT INTO {table} VALUES (1, 1, '/one.mkv'), (2, 2, '/two.mkv')"
                    )
                )
            connection.execute(text("DELETE FROM movies WHERE id=1"))
            for table in (
                "candidate_resync_identities",
                "delete_request_resync_identities",
            ):
                assert connection.execute(
                    text(f"SELECT movie_id FROM {table}")
                ).scalars().all() == [2]
            connection.execute(text("DELETE FROM reclaim_candidates WHERE id=2"))
            connection.execute(text("DELETE FROM delete_requests WHERE id=2"))
            for table in (
                "candidate_resync_identities",
                "delete_request_resync_identities",
            ):
                assert (
                    connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                    == 0
                )
            migration.downgrade()
            assert (
                "candidate_resync_identities"
                not in inspect(connection).get_table_names()
            )
            assert (
                "delete_request_resync_identities"
                not in inspect(connection).get_table_names()
            )
            assert connection.execute(text("SELECT id FROM movies")).scalar() == 2
            migration.upgrade()
    finally:
        engine.dispose()
