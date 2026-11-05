from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes import delete_requests as routes
from backend.database import Base
from backend.database.models import (
    DeleteRequest,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    User,
)
from backend.enums import MediaType, ProtectionRequestStatus, Service, UserRole
from backend.models.requests import ReviewDeleteRequest


@asynccontextmanager
async def seeded_requests():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            user = User(username="requester", password_hash="unused")
            manager = User(
                username="manager", password_hash="unused", role=UserRole.ADMIN
            )
            movie = Movie(title="Movie with replaced version", tmdb_id=505, size=100)
            db.add_all([user, manager, movie])
            await db.flush()
            original = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="movie",
                service_media_id="original",
                library_id="movies",
                library_name="Movies",
                path="/movies/original.mkv",
                size=100,
            )
            replacement = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="movie",
                service_media_id="replacement",
                library_id="movies",
                library_name="Movies",
                path="/movies/replacement.mkv",
                size=100,
            )
            db.add_all([original, replacement])
            await db.flush()
            stale = DeleteRequest(
                media_type=MediaType.MOVIE,
                requested_by_user_id=user.id,
                movie_id=movie.id,
                movie_version_id=original.id,
                target_scope="movie_version",
            )
            live = DeleteRequest(
                media_type=MediaType.MOVIE,
                requested_by_user_id=user.id,
                movie_id=movie.id,
                movie_version_id=replacement.id,
                target_scope="movie_version",
            )
            whole = DeleteRequest(
                media_type=MediaType.MOVIE,
                requested_by_user_id=user.id,
                movie_id=movie.id,
                target_scope="movie",
            )
            db.add_all([stale, live, whole])
            await db.commit()
            # Match sync's deletion of a file row and the FK's SET NULL behavior.
            await db.execute(delete(MovieVersion).where(MovieVersion.id == original.id))
            await db.commit()
            await db.refresh(stale)
            assert stale.movie_version_id is None
            yield db, user, manager, stale, live, whole
    finally:
        await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("admin_list", [False, True])
@pytest.mark.parametrize("historical", [False, True])
async def test_missing_version_does_not_break_request_lists(admin_list, historical):
    async with seeded_requests() as (db, user, manager, stale, live, whole):
        if historical:
            stale.status = ProtectionRequestStatus.APPROVED
            stale.executed_at = datetime.now(UTC)
            await db.commit()
        if admin_list:
            responses = await routes.get_all_delete_requests(manager, manager, db, None)
        else:
            responses = await routes.get_my_delete_requests(user, db, None)

        by_id = {response.id: response for response in responses}
        assert set(by_id) == {stale.id, live.id, whole.id}
        assert by_id[stale.id].media_title == "Movie with replaced version"
        assert by_id[stale.id].target_scope == "movie_version"
        assert by_id[stale.id].movie_version_id is None
        assert by_id[stale.id].target_available is False
        assert by_id[live.id].target_available is True
        assert by_id[whole.id].target_available is True


@pytest.mark.anyio
async def test_manager_can_deny_missing_version_request(monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(routes, "notify_user", notify)
    async with seeded_requests() as (db, _, manager, stale, _, _):
        response = await routes.deny_delete_request(
            stale.id, ReviewDeleteRequest(admin_notes="Version removed"), manager, db
        )
        assert response.status is ProtectionRequestStatus.DENIED
        assert response.target_available is False
        assert response.reviewed_by_user_id == manager.id
        assert response.admin_notes == "Version removed"
        notify.assert_awaited_once()


@pytest.mark.anyio
async def test_owner_can_cancel_missing_version_request(monkeypatch):
    monkeypatch.setattr(routes, "notify_admins", AsyncMock())
    async with seeded_requests() as (db, user, _, stale, live, _):
        stale_id = stale.id
        response = await routes.cancel_delete_request(stale_id, user, db)
        assert response == {"message": "Request cancelled"}
        assert await db.get(DeleteRequest, stale_id) is None
        assert await db.get(DeleteRequest, live.id) is not None


@pytest.mark.anyio
async def test_other_user_cannot_cancel_missing_version_request():
    async with seeded_requests() as (db, _, manager, stale, _, _):
        with pytest.raises(HTTPException) as caught:
            await routes.cancel_delete_request(stale.id, manager, db)
        assert caught.value.status_code == 404
        assert await db.get(DeleteRequest, stale.id) is not None


@pytest.mark.anyio
@pytest.mark.parametrize("retry", [False, True])
async def test_missing_version_cannot_queue_deletion(monkeypatch, retry):
    queue = AsyncMock()
    monkeypatch.setattr(routes, "queue_candidate_file_op_job", queue)
    async with seeded_requests() as (db, _, manager, stale, _, _):
        if retry:
            stale.status = ProtectionRequestStatus.APPROVED
            stale.playback_deferral = {"code": "resync_pending"}
            db.add(
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=stale.movie_id,
                    delete_request_id=stale.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="Approved request",
                )
            )
            await db.commit()
        with pytest.raises(HTTPException) as caught:
            if retry:
                await routes.retry_delete_request(stale.id, manager, db)
            else:
                await routes.approve_delete_request(
                    stale.id, ReviewDeleteRequest(), manager, db
                )
        assert caught.value.status_code == 409
        queue.assert_not_awaited()
        if not retry:
            assert stale.status is ProtectionRequestStatus.PENDING
            assert not (await db.scalars(select(ReclaimCandidate))).all()


@pytest.mark.anyio
async def test_version_from_another_movie_is_unavailable():
    async with seeded_requests() as (db, user, _, stale, _, _):
        other = Movie(title="Other movie", tmdb_id=506, size=100)
        db.add(other)
        await db.flush()
        wrong_version = MovieVersion(
            movie_id=other.id,
            service=Service.PLEX,
            service_item_id="other",
            service_media_id="other",
            library_id="movies",
            library_name="Movies",
            path="/movies/other.mkv",
            size=100,
        )
        db.add(wrong_version)
        await db.flush()
        stale.movie_version_id = wrong_version.id
        await db.commit()
        responses = await routes.get_my_delete_requests(user, db, None)
        response = next(r for r in responses if r.id == stale.id)
        assert response.target_available is False
        assert response.version_file_name is None
