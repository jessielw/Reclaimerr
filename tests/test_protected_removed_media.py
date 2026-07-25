from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.api.routes.protected import get_protected_entries
from backend.api.routes.v1.protections import list_protections
from backend.database import Base
from backend.database.models import Movie, ProtectedMedia, Series, User
from backend.enums import MediaType, UserRole


async def _seeded_session() -> tuple[
    async_sessionmaker[AsyncSession], int, int, AsyncEngine
]:
    """A live and a tombstoned row for each media type, each manually protected.

    Both movie and series are covered so a clause that only guards one media
    type's null foreign key -- for example a broken
    ``ProtectedMedia.series_id.is_(None)`` guard, under which a movie-keyed
    row's NULL series_id would hit ``NULL.in_(subquery)`` and silently
    evaluate to unknown -- gets exercised in both directions.

    Returns the session maker, the live movie's id, the live series' id, and
    the engine backing the session maker. The caller must dispose the engine
    once done with it, otherwise the aiosqlite worker thread outlives the
    event loop and pytest reports an unhandled thread exception in the
    warnings summary.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_maker() as db:
        live_movie = Movie(title="Movie A", tmdb_id=2001, size=1)
        tombstoned_movie = Movie(title="Movie B", tmdb_id=2002, size=1)
        tombstoned_movie.removed_at = datetime.now(UTC)
        live_series = Series(title="Series A", tmdb_id=1001, size=1)
        tombstoned_series = Series(title="Series B", tmdb_id=1002, size=1)
        tombstoned_series.removed_at = datetime.now(UTC)
        db.add_all([live_movie, tombstoned_movie, live_series, tombstoned_series])
        await db.flush()

        live_movie_protection = ProtectedMedia(media_type=MediaType.MOVIE)
        live_movie_protection.movie_id = live_movie.id
        live_movie_protection.source = "manual"
        tombstoned_movie_protection = ProtectedMedia(media_type=MediaType.MOVIE)
        tombstoned_movie_protection.movie_id = tombstoned_movie.id
        tombstoned_movie_protection.source = "manual"
        live_series_protection = ProtectedMedia(media_type=MediaType.SERIES)
        live_series_protection.series_id = live_series.id
        live_series_protection.source = "manual"
        tombstoned_series_protection = ProtectedMedia(media_type=MediaType.SERIES)
        tombstoned_series_protection.series_id = tombstoned_series.id
        tombstoned_series_protection.source = "manual"
        db.add_all(
            [
                live_movie_protection,
                tombstoned_movie_protection,
                live_series_protection,
                tombstoned_series_protection,
            ]
        )
        await db.commit()
        return session_maker, live_movie.id, live_series.id, engine


@pytest.mark.anyio
async def test_v1_list_excludes_protections_for_tombstoned_media() -> None:
    """Manual protections now outlive their media, so the list must filter them.

    Before the soft-delete fix these rows could not exist, so this filter is
    additive: it only governs rows that fix started preserving. Both live
    protections must still be present -- that's what would catch a clause
    that wrongly hides the other media type instead of just the tombstoned
    rows -- and both tombstoned protections must be absent.
    """
    session_maker, live_movie_id, live_series_id, engine = await _seeded_session()
    async with session_maker() as db:
        response = await list_protections(
            _principal=None,  # type: ignore[arg-type]
            db=db,
            page=1,
            per_page=50,
            media_type=None,
            media_id=None,
            active_only=True,
        )
    await engine.dispose()

    assert response.total == 2
    seen = {(item.media_type, item.media_id) for item in response.items}
    assert seen == {
        (MediaType.MOVIE, live_movie_id),
        (MediaType.SERIES, live_series_id),
    }


@pytest.mark.anyio
async def test_ui_list_count_excludes_protections_for_tombstoned_media() -> None:
    """The count query needs the same filter or it disagrees with the page."""
    session_maker, _, _, engine = await _seeded_session()
    async with session_maker() as db:
        user = User(username="admin", password_hash="x", role=UserRole.ADMIN)
        response = await get_protected_entries(
            _user=user,
            db=db,
            page=1,
            per_page=25,
            search=None,
            sort_by="created_at",
            sort_order="desc",
            media_type=None,
        )
    await engine.dispose()

    assert response.total == 2
