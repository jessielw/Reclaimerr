from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.dashboard import get_dashboard
from backend.api.routes.v1 import router as external_api_router
from backend.api.routes.v1.stats import get_stats
from backend.core.api_tokens import ApiPrincipal
from backend.database import Base, get_db
from backend.database.models import (
    DeleteRequest,
    Movie,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimHistory,
    ReclaimRule,
    Series,
    TaskSchedule,
    User,
)
from backend.enums import MediaType, ScheduleType, Task, UserRole

FLAGGED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _candidate(media_type: MediaType, size: int, **ids: int) -> ReclaimCandidate:
    candidate = ReclaimCandidate(
        media_type=media_type,
        matched_rule_ids=[1],
        matched_criteria={},
        reason="test",
        reason_data=[],
        estimated_space_bytes=size,
        **ids,
    )
    candidate.created_at = FLAGGED_AT
    return candidate


async def _seed(db: AsyncSession) -> None:
    db.add(
        ReclaimRule(
            name="Auto-delete after 3 days",
            media_type=MediaType.MOVIE,
            enabled=True,
            target_scope="movie_version",
            definition=None,
            action={"auto_delete_enabled": True, "auto_delete_delay_days": 3},
        )
    )
    movie = Movie(title="Dune", tmdb_id=1, size=300)
    series = Series(title="The Wire", tmdb_id=2, size=900)
    db.add_all([movie, series])
    await db.flush()
    db.add_all(
        [
            # two versions of one movie and two seasons of one series: two titles
            *(
                _candidate(
                    MediaType.MOVIE, 100, movie_id=movie.id, movie_version_id=version
                )
                for version in (1001, 1002)
            ),
            *(
                _candidate(MediaType.SERIES, 400, series_id=series.id, season_id=season)
                for season in (2001, 2002)
            ),
            ReclaimHistory(
                approved_by="admin", media_type=MediaType.MOVIE, tmdb_id=9, size=50
            ),
            ReclaimHistory(
                approved_by="admin", media_type=MediaType.SERIES, tmdb_id=8, size=70
            ),
            ProtectionRequest(
                media_type=MediaType.MOVIE, requested_by_user_id=1, movie_id=movie.id
            ),
            DeleteRequest(
                media_type=MediaType.SERIES,
                requested_by_user_id=1,
                series_id=series.id,
            ),
            TaskSchedule(
                task=Task.DELETE_CLEANUP_CANDIDATES,
                schedule_type=ScheduleType.CRON,
                schedule_value="0 2 * * *",
                default_schedule_type=ScheduleType.CRON,
                default_schedule_value="0 2 * * *",
                description="Delete",
                enabled=False,
            ),
        ]
    )
    await db.commit()


def test_stats_match_the_dashboard_for_the_same_data() -> None:
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        principal = ApiPrincipal(
            token_id=1, name="Homepage", scopes=frozenset({"system:read"})
        )
        admin = User(
            username="admin", password_hash="x", role=UserRole.ADMIN, permissions=[]
        )
        async with session_maker() as db:
            await _seed(db)
            stats = await get_stats(principal, db)
            dashboard = await get_dashboard(admin, db)
        await engine.dispose()
        return stats, dashboard

    stats, dashboard = asyncio.run(run())

    assert (stats.candidates_total, stats.candidates_movies) == (2, 1)
    assert stats.candidates_series == 1
    assert stats.reclaimable_bytes == dashboard.kpis.reclaimable_total_bytes == 1000
    assert stats.reclaimed_bytes_total == dashboard.kpis.reclaimed_total_bytes == 120
    assert stats.reclaimed_items_total == (
        dashboard.kpis.reclaimed_movies + dashboard.kpis.reclaimed_series
    )
    assert stats.pending_protection_requests == dashboard.requests.pending_count == 1
    assert stats.pending_delete_requests == 1
    # only the movie rule auto-deletes; the series candidates match no such rule
    assert stats.next_auto_delete_at == FLAGGED_AT + timedelta(days=3)
    assert stats.auto_delete_task_enabled is False


@pytest.mark.parametrize("header", [None, "Bearer not-a-reclaimerr-token"])
def test_stats_need_a_valid_api_token(header: str | None) -> None:
    app = FastAPI()
    app.include_router(external_api_router)
    app.dependency_overrides[get_db] = lambda: None
    headers = {"Authorization": header} if header else {}

    response = TestClient(app).get("/api/v1/stats", headers=headers)

    assert response.status_code == 401
