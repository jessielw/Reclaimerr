from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import Movie, Series
from backend.enums import MediaType
from backend.models.media_locator import MediaLocator


async def resolve_media_locator(
    db: AsyncSession, locator: MediaLocator
) -> Movie | Series:
    model = Movie if locator.media_type is MediaType.MOVIE else Series
    query = select(model).where(model.removed_at.is_(None))
    for field in ("media_id", "tmdb_id", "imdb_id", "tvdb_id", "anilist_id"):
        value = getattr(locator, field)
        if value is not None:
            query = query.where(
                getattr(model, "id" if field == "media_id" else field) == value
            )
            break
    matches = (await db.execute(query.limit(2))).scalars().all()
    if not matches:
        raise HTTPException(
            status_code=404, detail="Title not found in the synced catalog"
        )
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="This ID matches multiple titles; use a TMDB ID or Reclaimerr media ID",
        )
    return matches[0]
