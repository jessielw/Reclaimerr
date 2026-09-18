from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import Movie, MovieVersion, ReclaimCandidate
from backend.enums import MediaType, Service
from backend.tasks.sync import _restore_version_scoped_candidates


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(db, *, rebuilt_paths: list[str]) -> tuple[int, int]:
    """Seed a movie whose version rows have already been rebuilt by a resync."""
    movie = Movie(title="Movie1", tmdb_id=101, year=2020, size=0)
    db.add(movie)
    await db.flush()

    for index, path in enumerate(rebuilt_paths, start=1):
        db.add(
            MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id=f"item-{index}",
                service_media_id=f"media-{index}",
                library_id="lib-1",
                library_name="Movies",
                path=path,
                size=100,
            )
        )

    # a candidate the resync detached from its (now deleted) version row
    candidate = ReclaimCandidate(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[1],
        matched_criteria={},
        reason="cleanup",
        reason_data=[],
        movie_id=movie.id,
        movie_version_id=None,
        estimated_space_bytes=100,
    )
    db.add(candidate)
    await db.flush()
    await db.commit()
    return movie.id, candidate.id


@pytest.mark.anyio
async def test_candidate_reattaches_to_rebuilt_version_and_keeps_its_clock(
    monkeypatch,
) -> None:
    """The review period survives a resync.

    The candidate keeps its original created_at, so a title that was a day away
    from automatic deletion does not go back to the full waiting period.
    """
    engine, session_maker = await _make_session()
    try:
        monkeypatch.setattr("backend.tasks.sync.async_db", session_maker)
        async with session_maker() as db:
            movie_id, candidate_id = await _seed(
                db, rebuilt_paths=["/data/movies/Movie1/Movie1-4k.mkv"]
            )
            flagged_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=6)
            candidate = await db.get(ReclaimCandidate, candidate_id)
            assert candidate is not None
            candidate.created_at = flagged_at
            await db.commit()

        await _restore_version_scoped_candidates(
            {candidate_id: (movie_id, "/data/movies/movie1/movie1-4k.mkv")}
        )

        async with session_maker() as db:
            candidate = await db.get(ReclaimCandidate, candidate_id)
            assert candidate is not None
            version = (await db.execute(select(MovieVersion))).scalars().one()
            assert candidate.movie_version_id == version.id
            assert candidate.created_at == flagged_at
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_candidate_is_dropped_when_its_file_did_not_come_back(
    monkeypatch,
) -> None:
    """A detached candidate is never left movie-scoped.

    A movie-scoped candidate deletes every version of the movie, which is far
    more than the version rule selected, so it is removed instead.
    """
    engine, session_maker = await _make_session()
    try:
        monkeypatch.setattr("backend.tasks.sync.async_db", session_maker)
        async with session_maker() as db:
            movie_id, candidate_id = await _seed(
                db, rebuilt_paths=["/data/movies/Movie1/Movie1-1080p.mkv"]
            )

        await _restore_version_scoped_candidates(
            {candidate_id: (movie_id, "/data/movies/movie1/movie1-4k.mkv")}
        )

        async with session_maker() as db:
            assert await db.get(ReclaimCandidate, candidate_id) is None
    finally:
        await engine.dispose()
