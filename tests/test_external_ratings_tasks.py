from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import AniListRatingsIngestState, Movie, ServiceConfig
from backend.enums import Service
from backend.services.external_ratings import (
    ExternalRatingValues,
    MDBListClient,
    OMDbClient,
)
from backend.tasks import anilist as anilist_tasks
from backend.tasks import external_ratings as external_ratings_tasks


@pytest.mark.anyio
async def test_provider_tasks_keep_independent_caches_and_mdblist_precedence() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add_all(
            [
                ServiceConfig(
                    service_type=Service.MDBLIST,
                    name="MDBList",
                    base_url="https://api.mdblist.com",
                    api_key="key",
                    enabled=True,
                    extra_settings={"request_delay_seconds": 0},
                ),
                ServiceConfig(
                    service_type=Service.OMDB,
                    name="OMDb",
                    base_url="https://www.omdbapi.com",
                    api_key="key",
                    enabled=True,
                    extra_settings={"request_delay_seconds": 0},
                ),
                Movie(title="Ratings Movie", tmdb_id=100, imdb_id="tt100"),
            ]
        )
        await db.commit()

    with (
        patch.object(external_ratings_tasks, "async_db", session_maker),
        patch.object(
            MDBListClient,
            "get_ratings",
            new=AsyncMock(
                return_value=ExternalRatingValues(
                    rottentomatoes_tomato_meter=91,
                    trakt_rating=88,
                )
            ),
        ),
    ):
        await external_ratings_tasks._refresh_provider(Service.MDBLIST)

    async with session_maker() as db:
        movie = await db.get(Movie, 1)
        assert movie is not None
        mdblist_refreshed_at = movie.mdblist_ratings_refreshed_at
        assert mdblist_refreshed_at is not None
        assert movie.omdb_ratings_refreshed_at is None
        assert movie.rottentomatoes_tomato_meter == 91
        assert movie.metacritic_metascore is None
        assert movie.external_ratings_source == "mdblist"

    with (
        patch.object(external_ratings_tasks, "async_db", session_maker),
        patch.object(
            OMDbClient,
            "get_ratings",
            new=AsyncMock(
                return_value=ExternalRatingValues(
                    rottentomatoes_tomato_meter=72,
                    metacritic_metascore=64,
                )
            ),
        ),
    ):
        await external_ratings_tasks._refresh_provider(Service.OMDB)

    async with session_maker() as db:
        movie = await db.get(Movie, 1)
        assert movie is not None
        omdb_refreshed_at = movie.omdb_ratings_refreshed_at
        assert omdb_refreshed_at is not None
        assert movie.mdblist_ratings_refreshed_at == mdblist_refreshed_at
        assert movie.rottentomatoes_tomato_meter == 91
        assert movie.metacritic_metascore == 64
        assert movie.external_ratings_source == "mdblist+omdb"

        movie.mdblist_ratings_refreshed_at = datetime.now(UTC) - timedelta(days=15)
        await db.commit()

    with (
        patch.object(external_ratings_tasks, "async_db", session_maker),
        patch.object(
            MDBListClient,
            "get_ratings",
            new=AsyncMock(
                return_value=ExternalRatingValues(
                    rottentomatoes_tomato_meter=93,
                    metacritic_metascore=81,
                    trakt_rating=90,
                )
            ),
        ),
    ):
        await external_ratings_tasks._refresh_provider(Service.MDBLIST)

    async with session_maker() as db:
        movie = await db.get(Movie, 1)
        assert movie is not None
        assert movie.omdb_ratings_refreshed_at == omdb_refreshed_at
        assert movie.rottentomatoes_tomato_meter == 93
        assert movie.metacritic_metascore == 81
        assert movie.external_ratings_source == "mdblist"

    await engine.dispose()


@pytest.mark.anyio
async def test_anilist_denormalization_skips_unchanged_media_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    refreshed_at = datetime.now(UTC)
    async with session_maker() as db:
        movie = Movie(
            title="AniList Movie",
            tmdb_id=100,
            imdb_id="tt100",
            anilist_id=123,
            anilist_score=85,
            anilist_popularity=1000,
            anilist_favourites=50,
            anilist_refreshed_at=refreshed_at,
        )
        state = AniListRatingsIngestState(dataset_url="old")
        db.add_all([movie, state])
        await db.commit()
        state_id = state.id

    execute_count = 0
    original_execute = AsyncSession.execute

    async def counting_execute(self: AsyncSession, *args, **kwargs):
        nonlocal execute_count
        statement = args[0] if args else None
        if str(statement).startswith("UPDATE movies"):
            execute_count += 1
        return await original_execute(self, *args, **kwargs)

    with (
        patch.object(anilist_tasks, "async_db", session_maker),
        patch.object(AsyncSession, "execute", new=counting_execute),
    ):
        await anilist_tasks._persist_denormalized(
            state_id=state_id,
            now=datetime.now(UTC),
            dataset_url="new",
            etag=None,
            last_modified=None,
            content_length=123,
            dataset_sha256="sha",
            row_count=1,
            movie_assignments={1: 123},
            series_assignments={},
            anilist_meta_by_id={
                123: {"score": 85, "popularity": 1000, "favourites": 50}
            },
        )

    assert execute_count == 0

    await engine.dispose()


@pytest.mark.anyio
async def test_provider_failure_does_not_erase_cached_values() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    stale = datetime.now(UTC) - timedelta(days=15)
    cached = ExternalRatingValues(rottentomatoes_tomato_meter=85).to_dict()
    async with session_maker() as db:
        db.add_all(
            [
                ServiceConfig(
                    service_type=Service.MDBLIST,
                    name="MDBList",
                    base_url="https://api.mdblist.com",
                    api_key="key",
                    enabled=True,
                    extra_settings={"request_delay_seconds": 0},
                ),
                Movie(
                    title="Cached Movie",
                    tmdb_id=200,
                    rottentomatoes_tomato_meter=85,
                    external_ratings_source="mdblist",
                    mdblist_ratings_cache=cached,
                    mdblist_ratings_refreshed_at=stale,
                ),
            ]
        )
        await db.commit()

    with (
        patch.object(external_ratings_tasks, "async_db", session_maker),
        patch.object(
            MDBListClient,
            "get_ratings",
            new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
        ),
    ):
        await external_ratings_tasks._refresh_provider(Service.MDBLIST)

    async with session_maker() as db:
        movie = await db.get(Movie, 1)
        assert movie is not None
        assert movie.rottentomatoes_tomato_meter == 85
        assert movie.mdblist_ratings_cache == cached
        assert movie.mdblist_ratings_refreshed_at is not None
        assert movie.mdblist_ratings_refreshed_at.replace(tzinfo=UTC) == stale

    await engine.dispose()
