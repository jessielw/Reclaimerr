from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import get_current_user
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import (
    ExceptionRequest,
    MediaBlacklist,
    Movie,
    ReclaimCandidate,
    Series,
    User,
)
from backend.enums import ExceptionRequestStatus
from backend.models.media import (
    MediaStatusInfo,
    MovieVersionResponse,
    MovieWithStatus,
    PaginatedMediaResponse,
    SeriesServiceRefResponse,
    SeriesWithStatus,
)

router = APIRouter(prefix="/api/media", tags=["media"])


def extract_genre_names(genres: list[dict] | None) -> list[str] | None:
    """
    Extract genre names from TMDB genre objects.

    Comes in format [{'id': 16, 'name': 'Animation'}, ...] but we only want the names.
    """
    if not genres:
        return None
    return [g["name"] for g in genres]


@router.get("/movies", response_model=PaginatedMediaResponse)
async def get_movies(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort_by: str = Query("title", pattern="^(title|added_at|size|vote_average|year)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    search: str | None = Query(None, max_length=200),
    candidates_only: bool = Query(False),
):
    """
    Get all movies with status information.

    Includes whether each movie is:
    - A deletion candidate
    - Blacklisted/protected
    - Has pending exception request
    """
    # build base query
    query = (
        select(Movie)
        .where(Movie.removed_at.is_(None))
        .options(selectinload(Movie.versions))
    )
    count_query = (
        select(func.count()).select_from(Movie).where(Movie.removed_at.is_(None))
    )

    # apply search filter
    if search:
        search_term = f"%{search}%"
        query = query.where(Movie.title.ilike(search_term))
        count_query = count_query.where(Movie.title.ilike(search_term))

    # apply candidates filter
    if candidates_only:
        query = query.join(ReclaimCandidate, ReclaimCandidate.movie_id == Movie.id)
        count_query = count_query.join(
            ReclaimCandidate, ReclaimCandidate.movie_id == Movie.id
        )

    # apply sorting
    order_column = getattr(Movie, sort_by)
    if sort_order == "desc":
        query = query.order_by(order_column.desc())
    else:
        query = query.order_by(order_column.asc())

    # get total count
    total = (await db.execute(count_query)).scalar_one()

    # apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    # execute query
    result = await db.execute(query)
    movies = result.scalars().all()

    # fetch status information for all movies
    movie_ids = [m.id for m in movies]

    # get candidates
    candidates_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.movie_id.in_(movie_ids))
    )
    candidates = {c.movie_id: c for c in candidates_result.scalars().all()}

    # get blacklist entries
    now = datetime.now(timezone.utc)
    blacklist_result = await db.execute(
        select(MediaBlacklist).where(
            MediaBlacklist.movie_id.in_(movie_ids),
            or_(
                MediaBlacklist.permanent.is_(True),
                MediaBlacklist.expires_at.is_(None),
                MediaBlacklist.expires_at > now,
            ),
        )
    )
    blacklist = {b.movie_id: b for b in blacklist_result.scalars().all()}

    # get exception requests
    requests_result = await db.execute(
        select(ExceptionRequest).where(
            ExceptionRequest.movie_id.in_(movie_ids),
            ExceptionRequest.status == ExceptionRequestStatus.PENDING,
        )
    )
    requests = {r.movie_id: r for r in requests_result.scalars().all()}

    # build response with status
    items = []
    for movie in movies:
        candidate = candidates.get(movie.id)
        blacklist_entry = blacklist.get(movie.id)
        request = requests.get(movie.id)

        status = MediaStatusInfo(
            is_candidate=candidate is not None,
            candidate_id=candidate.id if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            candidate_space_gb=candidate.estimated_space_gb if candidate else None,
            is_blacklisted=blacklist_entry is not None,
            blacklist_reason=blacklist_entry.reason if blacklist_entry else None,
            blacklist_permanent=blacklist_entry.permanent if blacklist_entry else True,
            has_pending_request=request is not None,
            request_id=request.id if request else None,
            request_status=request.status if request else None,
            request_reason=request.reason if request else None,
        )

        movie_dict = {
            "id": movie.id,
            "title": movie.title,
            "year": movie.year,
            "tmdb_id": movie.tmdb_id,
            "size": movie.size,
            "versions": [
                MovieVersionResponse(
                    id=v.id,
                    service=v.service.value,
                    service_item_id=v.service_item_id,
                    service_media_id=v.service_media_id,
                    library_id=v.library_id,
                    library_name=v.library_name,
                    path=v.path,
                    size=v.size,
                    added_at=to_utc_isoformat(v.added_at),
                    container=v.container,
                )
                for v in movie.versions
            ],
            "radarr_id": movie.radarr_id,
            "imdb_id": movie.imdb_id,
            "tmdb_title": movie.tmdb_title,
            "original_title": movie.original_title,
            "tmdb_release_date": to_utc_isoformat(movie.tmdb_release_date),
            "original_language": movie.original_language,
            "poster_url": movie.poster_url,
            "backdrop_url": movie.backdrop_url,
            "overview": movie.overview,
            "genres": extract_genre_names(movie.genres),  # type: ignore
            "popularity": movie.popularity,
            "vote_average": movie.vote_average,
            "vote_count": movie.vote_count,
            "runtime": movie.runtime,
            "tagline": movie.tagline,
            "last_viewed_at": to_utc_isoformat(movie.last_viewed_at),
            "view_count": movie.view_count,
            "never_watched": movie.never_watched,
            "status": status,
            "added_at": to_utc_isoformat(movie.added_at),
        }
        items.append(MovieWithStatus(**movie_dict))

    total_pages = (total + per_page - 1) // per_page

    return PaginatedMediaResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.get("/series", response_model=PaginatedMediaResponse)
async def get_series(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort_by: str = Query("title", pattern="^(title|added_at|size|vote_average|year)$"),
    sort_order: str = Query("asc", pattern="^(asc|desc)$"),
    search: str | None = Query(None, max_length=200),
    candidates_only: bool = Query(False),
):
    """
    Get all series with status information.

    Includes whether each series is:
    - A deletion candidate
    - Blacklisted/protected
    - Has pending exception request
    """
    # build base query
    query = (
        select(Series)
        .where(Series.removed_at.is_(None))
        .options(selectinload(Series.service_refs))
    )
    count_query = (
        select(func.count()).select_from(Series).where(Series.removed_at.is_(None))
    )

    # apply search filter
    if search:
        search_term = f"%{search}%"
        query = query.where(Series.title.ilike(search_term))
        count_query = count_query.where(Series.title.ilike(search_term))

    # apply candidates filter
    if candidates_only:
        query = query.join(ReclaimCandidate, ReclaimCandidate.series_id == Series.id)
        count_query = count_query.join(
            ReclaimCandidate, ReclaimCandidate.series_id == Series.id
        )

    # apply sorting
    order_column = getattr(Series, sort_by)
    if sort_order == "desc":
        query = query.order_by(order_column.desc())
    else:
        query = query.order_by(order_column.asc())

    # get total count
    total = (await db.execute(count_query)).scalar_one()

    # apply pagination
    offset = (page - 1) * per_page
    query = query.offset(offset).limit(per_page)

    # execute query
    result = await db.execute(query)
    series_list = result.scalars().all()

    # fetch status information for all series
    series_ids = [s.id for s in series_list]

    # get candidates
    candidates_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.series_id.in_(series_ids))
    )
    candidates = {c.series_id: c for c in candidates_result.scalars().all()}

    # get blacklist entries
    now = datetime.now(timezone.utc)
    blacklist_result = await db.execute(
        select(MediaBlacklist).where(
            MediaBlacklist.series_id.in_(series_ids),
            or_(
                MediaBlacklist.permanent.is_(True),
                MediaBlacklist.expires_at.is_(None),
                MediaBlacklist.expires_at > now,
            ),
        )
    )
    blacklist = {b.series_id: b for b in blacklist_result.scalars().all()}

    # get exception requests
    requests_result = await db.execute(
        select(ExceptionRequest).where(
            ExceptionRequest.series_id.in_(series_ids),
            ExceptionRequest.status == ExceptionRequestStatus.PENDING,
        )
    )
    requests = {r.series_id: r for r in requests_result.scalars().all()}

    # build response with status
    items = []
    for series in series_list:
        candidate = candidates.get(series.id)
        blacklist_entry = blacklist.get(series.id)
        request = requests.get(series.id)

        status = MediaStatusInfo(
            is_candidate=candidate is not None,
            candidate_id=candidate.id if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            candidate_space_gb=candidate.estimated_space_gb if candidate else None,
            is_blacklisted=blacklist_entry is not None,
            blacklist_reason=blacklist_entry.reason if blacklist_entry else None,
            blacklist_permanent=blacklist_entry.permanent if blacklist_entry else True,
            has_pending_request=request is not None,
            request_id=request.id if request else None,
            request_status=request.status if request else None,
            request_reason=request.reason if request else None,
        )

        series_dict = {
            "id": series.id,
            "title": series.title,
            "year": series.year,
            "tmdb_id": series.tmdb_id,
            "size": series.size,
            "service_refs": [
                SeriesServiceRefResponse(
                    service=ref.service.value,
                    service_id=ref.service_id,
                    library_id=ref.library_id,
                    library_name=ref.library_name,
                    path=ref.path,
                )
                for ref in series.service_refs
            ],
            "sonarr_id": series.sonarr_id,
            "imdb_id": series.imdb_id,
            "tvdb_id": series.tvdb_id,
            "tmdb_title": series.tmdb_title,
            "original_title": series.original_title,
            "tmdb_first_air_date": to_utc_isoformat(series.tmdb_first_air_date),
            "tmdb_last_air_date": to_utc_isoformat(series.tmdb_last_air_date),
            "original_language": series.original_language,
            "poster_url": series.poster_url,
            "backdrop_url": series.backdrop_url,
            "overview": series.overview,
            "genres": extract_genre_names(series.genres),  # type: ignore
            "popularity": series.popularity,
            "vote_average": series.vote_average,
            "vote_count": series.vote_count,
            "season_count": series.season_count,
            "tagline": series.tagline,
            "last_viewed_at": to_utc_isoformat(series.last_viewed_at),
            "view_count": series.view_count,
            "never_watched": series.never_watched,
            "status": status,
            "added_at": to_utc_isoformat(series.added_at),
        }
        items.append(SeriesWithStatus(**series_dict))

    total_pages = (total + per_page - 1) // per_page

    return PaginatedMediaResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
