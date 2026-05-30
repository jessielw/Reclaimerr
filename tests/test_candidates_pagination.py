from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.media import get_candidates, get_candidates_presence
from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    Season,
    Series,
    User,
)
from backend.enums import MediaType, Service, UserRole


class SeededCandidateIds(TypedDict):
    alpha_candidate_id: int
    bravo_candidate_ids: list[int]
    charlie_candidate_ids: list[int]
    delta_candidate_id: int


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


def _candidate(
    *,
    media_type: MediaType,
    reason: str,
    created_at: datetime,
    estimated_space_bytes: int,
    movie_id: int | None = None,
    movie_version_id: int | None = None,
    series_id: int | None = None,
    season_id: int | None = None,
    episode_id: int | None = None,
) -> ReclaimCandidate:
    entry = ReclaimCandidate(
        media_type=media_type,
        matched_rule_ids=[1],
        matched_criteria={},
        reason=reason,
        reason_data=[],
        movie_id=movie_id,
        movie_version_id=movie_version_id,
        series_id=series_id,
        season_id=season_id,
        episode_id=episode_id,
        estimated_space_bytes=estimated_space_bytes,
    )
    entry.created_at = created_at
    return entry


async def _seed_candidates(db: AsyncSession) -> SeededCandidateIds:
    alpha = Movie(title="Alpha Movie", tmdb_id=101, year=2001, size=700)
    bravo = Movie(title="Bravo Movie", tmdb_id=102, year=2002, size=1000)
    charlie = Series(title="Charlie Show", tmdb_id=201, year=2010, size=500)
    delta = Series(title="Delta Show", tmdb_id=202, year=2011, size=900)
    alpha.imdb_id = "tt0000101"
    alpha.imdb_rating = 7.4
    alpha.imdb_vote_count = 12500
    alpha.anilist_id = 16498
    alpha.anilist_score = 85
    alpha.anilist_popularity = 998866
    alpha.anilist_favourites = 79392
    alpha.tmdb_collection_id = 10
    alpha.tmdb_collection_name = "Alpha Collection"
    alpha.tmdb_collection_checked = True
    delta.imdb_id = "tt0000202"
    delta.imdb_rating = 8.1
    delta.imdb_vote_count = 9200
    delta.anilist_id = 21459
    delta.anilist_score = 90
    delta.anilist_popularity = 543210
    delta.anilist_favourites = 40000
    db.add_all([alpha, bravo, charlie, delta])
    await db.flush()

    bravo_v1 = MovieVersion(
        movie_id=bravo.id,
        service=Service.PLEX,
        service_item_id="bravo-item-1",
        service_media_id="bravo-media-1",
        library_id="movies",
        library_name="Movies",
        size=400,
        file_name="bravo-1.mkv",
    )
    bravo_v2 = MovieVersion(
        movie_id=bravo.id,
        service=Service.PLEX,
        service_item_id="bravo-item-2",
        service_media_id="bravo-media-2",
        library_id="movies",
        library_name="Movies",
        size=600,
        file_name="bravo-2.mkv",
    )
    db.add_all([bravo_v1, bravo_v2])
    await db.flush()

    charlie_s1 = Season(series_id=charlie.id, season_number=1, size=300)
    db.add(charlie_s1)
    await db.flush()

    charlie_ep1 = Episode(
        season_id=charlie_s1.id,
        episode_number=1,
        name="Pilot",
        size=100,
    )
    db.add(charlie_ep1)
    await db.flush()

    created = lambda day: datetime(2026, 1, day, 12, 0, tzinfo=UTC)
    alpha_candidate = _candidate(
        media_type=MediaType.MOVIE,
        movie_id=alpha.id,
        reason="alpha",
        created_at=created(1),
        estimated_space_bytes=700,
    )
    bravo_candidates = [
        _candidate(
            media_type=MediaType.MOVIE,
            movie_id=bravo.id,
            movie_version_id=bravo_v1.id,
            reason="bravo v1",
            created_at=created(4),
            estimated_space_bytes=400,
        ),
        _candidate(
            media_type=MediaType.MOVIE,
            movie_id=bravo.id,
            movie_version_id=bravo_v2.id,
            reason="bravo v2",
            created_at=created(5),
            estimated_space_bytes=600,
        ),
    ]
    charlie_candidates = [
        _candidate(
            media_type=MediaType.SERIES,
            series_id=charlie.id,
            reason="charlie whole",
            created_at=created(2),
            estimated_space_bytes=500,
        ),
        _candidate(
            media_type=MediaType.SERIES,
            series_id=charlie.id,
            season_id=charlie_s1.id,
            reason="charlie season",
            created_at=created(6),
            estimated_space_bytes=300,
        ),
        _candidate(
            media_type=MediaType.SERIES,
            series_id=charlie.id,
            season_id=charlie_s1.id,
            episode_id=charlie_ep1.id,
            reason="charlie episode",
            created_at=created(7),
            estimated_space_bytes=100,
        ),
    ]
    delta_candidate = _candidate(
        media_type=MediaType.SERIES,
        series_id=delta.id,
        reason="delta",
        created_at=created(3),
        estimated_space_bytes=900,
    )
    db.add_all(
        [alpha_candidate, *bravo_candidates, *charlie_candidates, delta_candidate]
    )
    await db.commit()

    return {
        "alpha_candidate_id": alpha_candidate.id,
        "bravo_candidate_ids": [entry.id for entry in bravo_candidates],
        "charlie_candidate_ids": [entry.id for entry in charlie_candidates],
        "delta_candidate_id": delta_candidate.id,
    }


def test_get_candidates_all_paginates_by_display_groups() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=2,
                sort_by="created_at",
                sort_order="desc",
                search=None,
                media_type=None,
            )

            assert response.total == 4
            assert response.total_pages == 2
            returned_ids = {item.id for item in response.items}
            assert set(ids["charlie_candidate_ids"]).issubset(returned_ids)
            assert set(ids["bravo_candidate_ids"]).issubset(returned_ids)
            assert ids["alpha_candidate_id"] not in returned_ids
            assert ids["delta_candidate_id"] not in returned_ids
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_movies_filter_keeps_version_group_together() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=1,
                sort_by="estimated_space_bytes",
                sort_order="desc",
                search=None,
                media_type=MediaType.MOVIE,
            )

            assert response.total == 2
            assert response.total_pages == 2
            assert {item.id for item in response.items} == set(
                ids["bravo_candidate_ids"]
            )
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_search_keeps_series_group_intact() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search="Charlie",
                media_type=None,
            )

            assert response.total == 1
            assert response.total_pages == 1
            assert {item.id for item in response.items} == set(
                ids["charlie_candidate_ids"]
            )
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_presence_reports_existing_candidates() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            empty_response = await get_candidates_presence(_admin_user(), db_session)
            assert empty_response.has_candidates is False

            await _seed_candidates(db_session)
            populated_response = await get_candidates_presence(
                _admin_user(), db_session
            )
            assert populated_response.has_candidates is True
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_includes_imdb_fields() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)

            movie_response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search="Alpha",
                media_type=MediaType.MOVIE,
            )
            assert movie_response.total == 1
            movie_entry = movie_response.items[0]
            assert movie_entry.id == ids["alpha_candidate_id"]
            assert movie_entry.imdb_id == "tt0000101"
            assert movie_entry.imdb_rating == 7.4
            assert movie_entry.imdb_vote_count == 12500
            assert movie_entry.anilist_id == 16498
            assert movie_entry.anilist_score == 85
            assert movie_entry.anilist_popularity == 998866
            assert movie_entry.anilist_favourites == 79392
            assert movie_entry.tmdb_collection_id == 10
            assert movie_entry.tmdb_collection_name == "Alpha Collection"
            assert movie_entry.tmdb_in_collection is True

            series_response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search="Delta",
                media_type=MediaType.SERIES,
            )
            assert series_response.total == 1
            series_entry = series_response.items[0]
            assert series_entry.id == ids["delta_candidate_id"]
            assert series_entry.imdb_id == "tt0000202"
            assert series_entry.imdb_rating == 8.1
            assert series_entry.imdb_vote_count == 9200
            assert series_entry.anilist_id == 21459
            assert series_entry.anilist_score == 90
            assert series_entry.anilist_popularity == 543210
            assert series_entry.anilist_favourites == 40000
            assert series_entry.tmdb_collection_id is None
            assert series_entry.tmdb_collection_name is None
            assert series_entry.tmdb_in_collection is None

        await engine.dispose()

    asyncio.run(run())
