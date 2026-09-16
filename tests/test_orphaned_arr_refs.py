from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Movie,
    MovieArrRef,
    Series,
    SeriesArrRef,
    ServiceConfig,
)
from backend.enums import Service
from backend.tasks.sync import _purge_orphaned_arr_refs


async def _memory_session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    return session_maker, engine


async def _config(
    db: AsyncSession, service_type: Service, *, enabled: bool, name: str = "primary"
) -> int:
    config = ServiceConfig(
        service_type=service_type,
        base_url=f"http://{service_type.value}-{name}",
        api_key="secret",
        name=f"{service_type.value} {name}",
        enabled=enabled,
    )
    db.add(config)
    await db.flush()
    return config.id


@pytest.mark.anyio
async def test_refs_for_enabled_configs_survive() -> None:
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        config_id = await _config(db, Service.RADARR, enabled=True)
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        db.add(movie)
        await db.flush()
        db.add(
            MovieArrRef(
                movie_id=movie.id,
                service_config_id=config_id,
                arr_movie_id=10,
                arr_movie_path="/movies/Movie",
                tmdb_id=1,
            )
        )
        await db.flush()

        await _purge_orphaned_arr_refs(db, MovieArrRef, Service.RADARR)
        await db.commit()

        refs = (await db.execute(select(MovieArrRef))).scalars().all()
        assert [ref.service_config_id for ref in refs] == [config_id]
    await engine.dispose()


@pytest.mark.anyio
async def test_refs_for_disabled_configs_are_purged() -> None:
    """A disabled instance's refs are invisible in the UI but still route deletes."""
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        live_id = await _config(db, Service.RADARR, enabled=True, name="live")
        retired_id = await _config(db, Service.RADARR, enabled=False, name="retired")
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        db.add(movie)
        await db.flush()
        db.add_all(
            [
                MovieArrRef(
                    movie_id=movie.id,
                    service_config_id=live_id,
                    arr_movie_id=10,
                    arr_movie_path="/movies/Movie",
                    tmdb_id=1,
                ),
                MovieArrRef(
                    movie_id=movie.id,
                    service_config_id=retired_id,
                    arr_movie_id=11,
                    arr_movie_path="/old/Movie",
                    tmdb_id=1,
                ),
            ]
        )
        await db.flush()

        await _purge_orphaned_arr_refs(db, MovieArrRef, Service.RADARR)
        await db.commit()

        refs = (await db.execute(select(MovieArrRef))).scalars().all()
        assert [ref.service_config_id for ref in refs] == [live_id]
    await engine.dispose()


@pytest.mark.anyio
async def test_refs_for_deleted_configs_are_purged() -> None:
    """Legacy rows (config_id 0) and rows for removed configs both go."""
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        live_id = await _config(db, Service.SONARR, enabled=True)
        series = Series(title="Show", tmdb_id=2, size=1)
        db.add(series)
        await db.flush()
        db.add_all(
            [
                SeriesArrRef(
                    series_id=series.id,
                    service_config_id=live_id,
                    arr_series_id=20,
                    arr_series_path="/tv/Show",
                    tmdb_id=2,
                ),
                SeriesArrRef(
                    series_id=series.id,
                    service_config_id=0,
                    arr_series_id=21,
                    arr_series_path="/tv-legacy/Show",
                    tmdb_id=2,
                ),
            ]
        )
        await db.flush()

        await _purge_orphaned_arr_refs(db, SeriesArrRef, Service.SONARR)
        await db.commit()

        refs = (await db.execute(select(SeriesArrRef))).scalars().all()
        assert [ref.service_config_id for ref in refs] == [live_id]
    await engine.dispose()


@pytest.mark.anyio
async def test_no_enabled_config_purges_every_ref() -> None:
    """With no enabled Radarr left, no ref can describe a real route."""
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        retired_id = await _config(db, Service.RADARR, enabled=False)
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        db.add(movie)
        await db.flush()
        db.add(
            MovieArrRef(
                movie_id=movie.id,
                service_config_id=retired_id,
                arr_movie_id=10,
                arr_movie_path="/movies/Movie",
                tmdb_id=1,
            )
        )
        await db.flush()

        await _purge_orphaned_arr_refs(db, MovieArrRef, Service.RADARR)
        await db.commit()

        assert (await db.execute(select(MovieArrRef))).scalars().all() == []
    await engine.dispose()


@pytest.mark.anyio
async def test_sonarr_purge_leaves_radarr_refs_alone() -> None:
    session_maker, engine = await _memory_session_maker()
    async with session_maker() as db:
        radarr_id = await _config(db, Service.RADARR, enabled=True)
        movie = Movie(title="Movie", tmdb_id=1, size=1)
        db.add(movie)
        await db.flush()
        db.add(
            MovieArrRef(
                movie_id=movie.id,
                service_config_id=radarr_id,
                arr_movie_id=10,
                arr_movie_path="/movies/Movie",
                tmdb_id=1,
            )
        )
        await db.flush()

        # no enabled Sonarr at all - the Radarr side must not be touched
        await _purge_orphaned_arr_refs(db, SeriesArrRef, Service.SONARR)
        await db.commit()

        refs = (await db.execute(select(MovieArrRef))).scalars().all()
        assert [ref.service_config_id for ref in refs] == [radarr_id]
    await engine.dispose()
