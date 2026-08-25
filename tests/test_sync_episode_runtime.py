from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

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


@pytest.mark.anyio
async def test_episode_added_at_survives_a_second_sync(session: AsyncSession) -> None:
    """Media servers report aware timestamps; SQLite reads them back naive.

    Comparing the two directly raised "can't compare offset-naive and
    offset-aware datetimes" and failed the whole Sync Media task.
    """
    season_id = await _make_season(session)
    first_seen = datetime(2023, 8, 23, 12, 0, tzinfo=UTC)

    await _upsert_episodes(
        session,
        season_id,
        [AggregatedEpisodeData(episode_number=1, view_count=0, added_at=first_seen)],
        Service.PLEX,
    )
    await session.commit()
    session.expire_all()

    # Second sync: the stored value comes back naive, the provider is aware.
    await _upsert_episodes(
        session,
        season_id,
        [
            AggregatedEpisodeData(
                episode_number=1,
                view_count=0,
                added_at=first_seen + timedelta(days=30),
            )
        ],
        Service.PLEX,
    )
    await session.commit()
    session.expire_all()

    result = await session.execute(
        select(Episode).where(Episode.season_id == season_id)
    )
    episode = result.scalar_one()
    # Stored as naive UTC, and the earliest date wins so a re-scan cannot
    # retroactively invalidate a recorded watch.
    assert episode.added_at == first_seen.replace(tzinfo=None)


@pytest.mark.anyio
async def test_episode_added_at_takes_the_earliest_report(
    session: AsyncSession,
) -> None:
    season_id = await _make_season(session)
    later = datetime(2024, 1, 2, tzinfo=UTC)
    earlier = datetime(2023, 5, 6, tzinfo=UTC)

    await _upsert_episodes(
        session,
        season_id,
        [AggregatedEpisodeData(episode_number=1, view_count=0, added_at=later)],
        Service.PLEX,
    )
    await session.commit()
    session.expire_all()

    await _upsert_episodes(
        session,
        season_id,
        [AggregatedEpisodeData(episode_number=1, view_count=0, added_at=earlier)],
        Service.PLEX,
    )
    await session.commit()
    session.expire_all()

    result = await session.execute(
        select(Episode).where(Episode.season_id == season_id)
    )
    assert result.scalar_one().added_at == earlier.replace(tzinfo=None)


@pytest.mark.anyio
async def test_episode_last_viewed_merges_across_timezone_shapes(
    session: AsyncSession,
) -> None:
    season_id = await _make_season(session)
    watched = datetime(2025, 1, 6, 4, 7, tzinfo=UTC)

    await _upsert_episodes(
        session,
        season_id,
        [
            AggregatedEpisodeData(
                episode_number=1, view_count=1, last_viewed_at=watched
            )
        ],
        Service.PLEX,
    )
    await session.commit()
    session.expire_all()

    await _upsert_episodes(
        session,
        season_id,
        [
            AggregatedEpisodeData(
                episode_number=1,
                view_count=2,
                last_viewed_at=watched + timedelta(hours=5),
            )
        ],
        Service.PLEX,
    )
    await session.commit()
    session.expire_all()

    result = await session.execute(
        select(Episode).where(Episode.season_id == season_id)
    )
    episode = result.scalar_one()
    assert episode.last_viewed_at == (watched + timedelta(hours=5)).replace(tzinfo=None)
    assert episode.view_count == 2
