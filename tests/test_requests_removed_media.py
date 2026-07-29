from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.api.routes.requests import (
    approve_request,
    get_all_requests,
    get_my_requests,
)
from backend.database import Base
from backend.database.models import Movie, ProtectedMedia, ProtectionRequest, User
from backend.enums import MediaType, ProtectionRequestStatus, UserRole
from backend.models.requests import ReviewProtectionRequest


async def _seeded_session() -> tuple[
    async_sessionmaker[AsyncSession], User, int, int, AsyncEngine
]:
    """One pending request for live media and one for tombstoned media.

    Both belong to the same user, so the "my requests" list and the admin list
    see the same pair. Returns the session maker, the requesting user, the live
    request's id, the tombstoned request's id, and the engine. The caller must
    dispose the engine, otherwise the aiosqlite worker thread outlives the event
    loop and pytest reports an unhandled thread exception.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_maker() as db:
        user = User(username="someone", password_hash="x", role=UserRole.ADMIN)
        live_movie = Movie(title="Movie A", tmdb_id=2001, size=1)
        tombstoned_movie = Movie(title="Movie B", tmdb_id=2002, size=1)
        tombstoned_movie.removed_at = datetime.now(UTC)
        db.add_all([user, live_movie, tombstoned_movie])
        await db.flush()

        live_request = ProtectionRequest(
            media_type=MediaType.MOVIE, requested_by_user_id=user.id
        )
        live_request.movie_id = live_movie.id
        live_request.reason = "still here"
        tombstoned_request = ProtectionRequest(
            media_type=MediaType.MOVIE, requested_by_user_id=user.id
        )
        tombstoned_request.movie_id = tombstoned_movie.id
        tombstoned_request.reason = "gone"
        db.add_all([live_request, tombstoned_request])
        await db.commit()
        return session_maker, user, live_request.id, tombstoned_request.id, engine


@pytest.mark.anyio
async def test_my_requests_excludes_requests_for_tombstoned_media() -> None:
    """A request outlives a soft-delete of its media, so the list must filter.

    The `if not media: continue` bail does not cover this: the medium still
    exists as a tombstone, so the relationship resolves and the request renders
    for a title that appears nowhere else in the app.
    """
    session_maker, user, live_id, _, engine = await _seeded_session()
    async with session_maker() as db:
        responses = await get_my_requests(user=user, db=db, status_filter=None)
    await engine.dispose()

    assert [r.id for r in responses] == [live_id]


@pytest.mark.anyio
async def test_all_requests_excludes_requests_for_tombstoned_media() -> None:
    """The admin queue would otherwise hold the request permanently."""
    session_maker, user, live_id, _, engine = await _seeded_session()
    async with session_maker() as db:
        responses = await get_all_requests(
            _manager=user, _page_user=user, db=db, status_filter=None
        )
    await engine.dispose()

    assert [r.id for r in responses] == [live_id]


@pytest.mark.anyio
async def test_approving_a_request_for_tombstoned_media_is_refused() -> None:
    """Approving would create a protection that the protected lists then hide.

    The requester is told it worked, the admin sees the queue clear, and the
    protection appears nowhere. Refuse instead, the same way creating a request
    for removed media is refused.
    """
    session_maker, user, _, tombstoned_id, engine = await _seeded_session()
    async with session_maker() as db:
        with pytest.raises(HTTPException) as exc_info:
            await approve_request(
                request_id=tombstoned_id,
                review_data=ReviewProtectionRequest(),
                manager=user,
                db=db,
            )
        await db.rollback()
        protections = (await db.execute(select(ProtectedMedia))).scalars().all()
        request = await db.get(ProtectionRequest, tombstoned_id)
        assert request is not None
        status_after = request.status
    await engine.dispose()

    assert exc_info.value.status_code == 404
    assert protections == []
    assert status_after is ProtectionRequestStatus.PENDING


@pytest.mark.anyio
async def test_approving_a_request_for_live_media_still_works() -> None:
    """The refusal must not disable approval for media that is still present."""
    session_maker, user, live_id, _, engine = await _seeded_session()
    async with session_maker() as db:
        response = await approve_request(
            request_id=live_id,
            review_data=ReviewProtectionRequest(),
            manager=user,
            db=db,
        )
        protections = (await db.execute(select(ProtectedMedia))).scalars().all()
    await engine.dispose()

    assert response.status is ProtectionRequestStatus.APPROVED
    assert len(protections) == 1
