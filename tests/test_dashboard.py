from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.dashboard import get_dashboard
from backend.database import Base
from backend.database.models import Movie, Series, TaskRun, User
from backend.enums import Task, TaskStatus, UserRole


def _admin_user() -> User:
    return User(username="admin", password_hash="x", role=UserRole.ADMIN)


@pytest.mark.anyio
async def test_dashboard_totals_exclude_soft_deleted_media() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        user = _admin_user()
        removed_movie = Movie(title="Removed Movie", tmdb_id=2, size=200)
        removed_movie.removed_at = datetime.now(UTC)
        removed_series = Series(title="Removed Series", tmdb_id=4, size=400)
        removed_series.removed_at = datetime.now(UTC)
        db.add_all(
            [
                user,
                Movie(title="Active Movie", tmdb_id=1, size=100),
                removed_movie,
                Series(title="Active Series", tmdb_id=3, size=300),
                removed_series,
            ]
        )
        await db.flush()

        response = await get_dashboard(current_user=user, db=db)

    await engine.dispose()

    assert response.kpis.total_movies == 1
    assert response.kpis.total_series == 1
    assert response.kpis.total_movies_size_bytes == 100
    assert response.kpis.total_series_size_bytes == 300


@pytest.mark.anyio
async def test_dashboard_materializes_provider_rating_task_history() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        user = _admin_user()
        db.add_all(
            [
                user,
                TaskRun(
                    task=Task.MDBLIST_RATINGS_REFRESH,
                    status=TaskStatus.COMPLETED,
                ),
            ]
        )
        await db.flush()

        response = await get_dashboard(current_user=user, db=db)

    await engine.dispose()

    assert any(
        item.title == "Refresh MDBList Ratings completed"
        for item in response.activity
    )
