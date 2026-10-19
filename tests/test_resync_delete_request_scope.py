"""Isolated audit reproducers; assertions describe the required safe behavior."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    DeleteRequest,
    Movie,
    MovieVersion,
    ReclaimCandidate,
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
