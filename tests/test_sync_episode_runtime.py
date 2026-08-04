from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.database import Base
from backend.database.models import Episode, Season, Series
from backend.enums import Service
from backend.models.media import AggregatedEpisodeData
from backend.tasks.sync import _upsert_episodes


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


async def _make_season(session: AsyncSession) -> int:
    series = Series(title="Test Series", tmdb_id=1, size=0)
    session.add(series)
    await session.flush()
    season = Season(series_id=series.id, season_number=1)
    session.add(season)
    await session.flush()
    return season.id


@pytest.mark.anyio
async def test_new_episode_stores_runtime(session: AsyncSession) -> None:
    season_id = await _make_season(session)
    await _upsert_episodes(
        session,
        season_id,
        [
            AggregatedEpisodeData(
                episode_number=1, view_count=0, runtime_seconds=1320
            )
        ],
        Service.PLEX,
    )
    await session.flush()

    result = await session.execute(
        select(Episode).where(Episode.season_id == season_id)
    )
    episode = result.scalar_one()
    assert episode.runtime == 1320


@pytest.mark.anyio
async def test_existing_episode_runtime_is_updated_when_provided(
    session: AsyncSession,
) -> None:
    season_id = await _make_season(session)
    await _upsert_episodes(
        session,
        season_id,
        [AggregatedEpisodeData(episode_number=1, view_count=0, runtime_seconds=None)],
        Service.PLEX,
    )
    await session.flush()

    # a later sync reports the runtime; it should now be filled in
    await _upsert_episodes(
        session,
        season_id,
        [
            AggregatedEpisodeData(
                episode_number=1, view_count=0, runtime_seconds=1500
            )
        ],
        Service.PLEX,
    )
    await session.flush()

    result = await session.execute(
        select(Episode).where(Episode.season_id == season_id)
    )
    episode = result.scalar_one()
    assert episode.runtime == 1500
