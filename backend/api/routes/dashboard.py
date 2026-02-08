from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.utils.file_utils import bytes_to_gb
from backend.database import get_db
from backend.database.models import CleanupCandidate, Movie, Series
from backend.enums import MediaType

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def test(db: AsyncSession = Depends(get_db)):
    """Test endpoint."""

    movie_count = (
        await db.execute(select(func.count()).select_from(Movie))
    ).scalar_one() or 0
    series_count = (
        await db.execute(select(func.count()).select_from(Series))
    ).scalar_one() or 0
    deletion_candidates = (
        await db.execute(select(func.count()).select_from(CleanupCandidate))
    ).scalar_one() or 0
    movie_size_total = (
        await db.scalar(
            select(func.coalesce(func.sum(Movie.size), 0))
            .select_from(CleanupCandidate)
            .join(Movie, CleanupCandidate.movie_id == Movie.id)
            .where(CleanupCandidate.media_type == MediaType.MOVIE)
        )
        or 0
    )
    series_size_total = (
        await db.scalar(
            select(func.coalesce(func.sum(Series.size), 0))
            .select_from(CleanupCandidate)
            .join(Series, CleanupCandidate.series_id == Series.id)
            .where(CleanupCandidate.media_type == MediaType.SERIES)
        )
        or 0
    )

    data = {
        "stats": {
            "total_movies": movie_count,
            "total_series": series_count,
            "deletion_candidates": deletion_candidates,
            "movies_reclaimable_size_gb": f"{bytes_to_gb(movie_size_total):.2f}",
            "series_reclaimable_size_gb": f"{bytes_to_gb(series_size_total):.2f}",
        }
    }
    return {"message": "test successful", "data": data}
