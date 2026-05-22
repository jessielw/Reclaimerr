from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio.engine import AsyncEngine

from backend.api.routes.requests import create_protection_request
from backend.database import Base
from backend.database.models import (
    Episode,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Season,
    Series,
    User,
)
from backend.enums import MediaType, ProtectionRequestStatus, UserRole
from backend.models.requests import CreateProtectionRequest


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


async def _make_session() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return engine, session_maker


async def _seed_episode_candidate(
    db: AsyncSession,
) -> tuple[User, Series, Season, Episode, ReclaimCandidate]:
    admin = _admin_user()
    series = Series(title="Scoped Show", tmdb_id=987, year=2026, size=500)
    db.add_all([admin, series])
    await db.flush()

    season = Season(series_id=series.id, season_number=1, size=300)
    db.add(season)
    await db.flush()

    episode = Episode(
        season_id=season.id,
        episode_number=1,
        name="Pilot",
        size=100,
    )
    db.add(episode)
    await db.flush()

    candidate = ReclaimCandidate(
        media_type=MediaType.SERIES,
        matched_rule_ids=[1],
        matched_criteria={},
        reason="episode candidate",
        reason_data=[],
        series_id=series.id,
        season_id=season.id,
        episode_id=episode.id,
        estimated_space_bytes=100,
    )
    db.add(candidate)
    await db.commit()
    return admin, series, season, episode, candidate


def test_episode_protection_request_accepts_episode_id_only() -> None:
    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, series, season, episode, candidate = (
                    await _seed_episode_candidate(db)
                )

                response = await create_protection_request(
                    CreateProtectionRequest(
                        media_type=MediaType.SERIES,
                        media_id=series.id,
                        episode_id=episode.id,
                        reason="Admin decision",
                    ),
                    admin,
                    db,
                )

                assert response.status is ProtectionRequestStatus.APPROVED
                assert response.season_id == season.id
                assert response.episode_id == episode.id
                assert response.target_scope == "episode"
                assert await db.get(ReclaimCandidate, candidate.id) is None

                protected = (await db.execute(select(ProtectedMedia))).scalar_one()
                assert protected.series_id == series.id
                assert protected.season_id == season.id
                assert protected.episode_id == episode.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_protection_request_accepts_matching_season_and_episode_ids() -> None:
    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, series, season, episode, _candidate = (
                    await _seed_episode_candidate(db)
                )

                response = await create_protection_request(
                    CreateProtectionRequest(
                        media_type=MediaType.SERIES,
                        media_id=series.id,
                        season_id=season.id,
                        episode_id=episode.id,
                        reason="Admin decision",
                    ),
                    admin,
                    db,
                )

                assert response.status is ProtectionRequestStatus.APPROVED
                assert response.season_id == season.id
                assert response.episode_id == episode.id
                assert response.target_scope == "episode"

                request = (await db.execute(select(ProtectionRequest))).scalar_one()
                assert request.season_id == season.id
                assert request.episode_id == episode.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_episode_protection_request_rejects_mismatched_season_and_episode_ids() -> None:
    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, series, _season, episode, _candidate = (
                    await _seed_episode_candidate(db)
                )
                other_season = Season(series_id=series.id, season_number=2, size=200)
                db.add(other_season)
                await db.commit()

                try:
                    await create_protection_request(
                        CreateProtectionRequest(
                            media_type=MediaType.SERIES,
                            media_id=series.id,
                            season_id=other_season.id,
                            episode_id=episode.id,
                            reason="Admin decision",
                        ),
                        admin,
                        db,
                    )
                except HTTPException as exc:
                    assert exc.status_code == 400
                    assert exc.detail == "season_id does not match episode_id"
                else:
                    raise AssertionError("Expected mismatched scope to be rejected")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_season_protection_request_allows_existing_episode_protection() -> None:
    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, series, season, episode, _candidate = (
                    await _seed_episode_candidate(db)
                )
                db.add(
                    ProtectedMedia(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        season_id=season.id,
                        episode_id=episode.id,
                        protected_by_user_id=admin.id,
                        reason="existing episode",
                    )
                )
                await db.commit()

                response = await create_protection_request(
                    CreateProtectionRequest(
                        media_type=MediaType.SERIES,
                        media_id=series.id,
                        season_id=season.id,
                        reason="Admin decision",
                    ),
                    admin,
                    db,
                )

                assert response.status is ProtectionRequestStatus.APPROVED
                assert response.season_id == season.id
                assert response.episode_id is None

                protected_entries = (
                    await db.execute(
                        select(ProtectedMedia).order_by(ProtectedMedia.id)
                    )
                ).scalars().all()
                assert len(protected_entries) == 2
                assert protected_entries[0].episode_id == episode.id
                assert protected_entries[1].season_id == season.id
                assert protected_entries[1].episode_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())
