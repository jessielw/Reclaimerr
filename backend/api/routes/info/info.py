from random import sample as random_sample

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core import __version__
from backend.database import get_db
from backend.database.models import Movie, Series

from .default_backdrops import TOP_RATED_BACKDROPS

router = APIRouter(tags=["info"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/version")
async def get_version() -> dict[str, str]:
    """Get application version."""
    return {
        "version": str(__version__),
        "program": __version__.program_name,
        "url": __version__.program_url,
    }


@router.get("/random-backdrop")
async def get_backdrops(
    num_of_bd: int = Query(
        10,
        ge=1,
        le=20,
        alias="num-of-bd",
        description="Number of random backdrops to return",
    ),
    fetch_limit: int = Query(
        40,
        ge=20,
        le=100,
        alias="fetch-limit",
        description="Number of backdrops to fetch from the database",
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[str]]:
    """
    Get backdrop image URLs.
    If no backdrops are found in the database, return a random selection of default top-rated backdrops.

    `fetch_limit` should be set higher than `num_of_bd` to ensure enough backdrops are available for
    random sampling. If there are fewer backdrops in the database than `num_of_bd`, all available
    backdrops will be returned.
    """
    # ensure fetch limit is at least as large as the number of backdrops requested
    if fetch_limit < num_of_bd:
        raise HTTPException(
            status_code=400,
            detail="Fetch limit must be greater than or equal to the number of backdrops requested",
        )

    KEY = "backdrops"

    # count movies with a backdrop
    movie_count_stmt = (
        select(func.count()).select_from(Movie).where(Movie.backdrop_url.isnot(None))
    )
    series_count_stmt = (
        select(func.count()).select_from(Series).where(Series.backdrop_url.isnot(None))
    )

    movie_count = (await db.execute(movie_count_stmt)).scalar_one()
    series_count = (await db.execute(series_count_stmt)).scalar_one()
    total_count = movie_count + series_count

    if total_count == 0:
        return {KEY: random_sample(TOP_RATED_BACKDROPS, num_of_bd)}

    # fetch up to 'fetch_limit' most popular backdrops from each
    movie_stmt = (
        select(Movie.backdrop_url)
        .where(Movie.backdrop_url.isnot(None))
        .order_by(desc(Movie.popularity))
        .limit(fetch_limit)
    )
    series_stmt = (
        select(Series.backdrop_url)
        .where(Series.backdrop_url.isnot(None))
        .order_by(desc(Series.popularity))
        .limit(fetch_limit)
    )

    movie_backdrops = [row[0] for row in (await db.execute(movie_stmt)).all()]
    series_backdrops = [row[0] for row in (await db.execute(series_stmt)).all()]
    all_backdrops = [url for url in movie_backdrops + series_backdrops if url]

    if not all_backdrops or len(all_backdrops) < num_of_bd:
        return {KEY: random_sample(TOP_RATED_BACKDROPS, num_of_bd)}

    return {KEY: random_sample(all_backdrops, num_of_bd)}
