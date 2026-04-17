from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import get_current_user, has_permission
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import (
    Movie,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Season,
    Series,
    User,
)
from backend.enums import MediaType, Permission, ProtectionRequestStatus, UserRole
from backend.models.media import (
    CandidateEntry,
    DeleteCandidatesRequest,
    DeleteCandidatesResponse,
    MediaStatusInfo,
    MovieVersionResponse,
    MovieWithStatus,
    PaginatedCandidatesResponse,
    PaginatedMediaResponse,
    SeasonWithStatus,
    SeriesServiceRefResponse,
    SeriesWithStatus,
)
from backend.tasks.cleanup import delete_specific_candidates

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
    - Protected
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
    # Note: use distinct() to guard against row multiplication if a movie ever ends up
    # with more than one ReclaimCandidate row (this should really not ever happen)
    if candidates_only:
        query = query.join(
            ReclaimCandidate, ReclaimCandidate.movie_id == Movie.id
        ).distinct()
        count_query = count_query.join(
            ReclaimCandidate, ReclaimCandidate.movie_id == Movie.id
        ).distinct()

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

    # get protected entries
    now = datetime.now(timezone.utc)
    protected_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.movie_id.in_(movie_ids),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected = {b.movie_id: b for b in protected_result.scalars().all()}

    # get exception requests
    requests_result = await db.execute(
        select(ProtectionRequest).where(
            ProtectionRequest.movie_id.in_(movie_ids),
            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    requests = {r.movie_id: r for r in requests_result.scalars().all()}

    # build response with status
    items = []
    for movie in movies:
        candidate = candidates.get(movie.id)
        protection_entry = protected.get(movie.id)
        request = requests.get(movie.id)

        status = MediaStatusInfo(
            is_candidate=candidate is not None,
            candidate_id=candidate.id if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            candidate_space_gb=candidate.estimated_space_gb if candidate else None,
            is_protected=protection_entry is not None,
            protected_reason=protection_entry.reason if protection_entry else None,
            protected_permanent=protection_entry.permanent
            if protection_entry
            else True,
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
    - Protected
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
    # Note: we're using distinct() to avoid row multiplication when a series has multiple
    # season level candidates (each shares the same series_id)
    if candidates_only:
        query = query.join(
            ReclaimCandidate, ReclaimCandidate.series_id == Series.id
        ).distinct()
        count_query = count_query.join(
            ReclaimCandidate, ReclaimCandidate.series_id == Series.id
        ).distinct()

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

    # get series level candidates (no season)
    candidates_result = await db.execute(
        select(ReclaimCandidate).where(
            ReclaimCandidate.series_id.in_(series_ids),
            ReclaimCandidate.season_id.is_(None),
        )
    )
    candidates = {c.series_id: c for c in candidates_result.scalars().all()}

    # collect series_ids that have at least one season level candidate
    season_cands_result = await db.execute(
        select(ReclaimCandidate.series_id).where(
            ReclaimCandidate.series_id.in_(series_ids),
            ReclaimCandidate.season_id.isnot(None),
        )
    )
    series_with_season_cands: set[int] = {
        row[0] for row in season_cands_result.all() if row[0] is not None
    }

    # get protected entries
    now = datetime.now(timezone.utc)
    protected_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.series_id.in_(series_ids),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    protected = {b.series_id: b for b in protected_result.scalars().all()}

    # get exception requests
    requests_result = await db.execute(
        select(ProtectionRequest).where(
            ProtectionRequest.series_id.in_(series_ids),
            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
        )
    )
    requests = {r.series_id: r for r in requests_result.scalars().all()}

    # build response with status
    items = []
    for series in series_list:
        candidate = candidates.get(series.id)
        protection_entry = protected.get(series.id)
        request = requests.get(series.id)

        status = MediaStatusInfo(
            is_candidate=candidate is not None,
            candidate_id=candidate.id if candidate else None,
            candidate_reason=candidate.reason if candidate else None,
            candidate_space_gb=candidate.estimated_space_gb if candidate else None,
            is_protected=protection_entry is not None,
            protected_reason=protection_entry.reason if protection_entry else None,
            protected_permanent=protection_entry.permanent
            if protection_entry
            else True,
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
            "has_season_candidates": series.id in series_with_season_cands
            and candidate is None,
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


@router.get("/series/{series_id}/seasons", response_model=list[SeasonWithStatus])
async def get_series_seasons(
    series_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Get per-season status for a series."""
    series_result = await db.execute(
        select(Series).where(Series.id == series_id, Series.removed_at.is_(None))
    )
    if series_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
        )

    seasons_result = await db.execute(
        select(Season)
        .where(Season.series_id == series_id)
        .order_by(Season.season_number)
    )
    seasons = seasons_result.scalars().all()

    season_ids = [s.id for s in seasons]
    if not season_ids:
        return []

    # season level reclaim candidates
    cand_result = await db.execute(
        select(ReclaimCandidate).where(ReclaimCandidate.season_id.in_(season_ids))
    )
    season_candidates = {c.season_id: c for c in cand_result.scalars().all()}

    # season level protection entries
    now = datetime.now(timezone.utc)
    prot_result = await db.execute(
        select(ProtectedMedia).where(
            ProtectedMedia.season_id.in_(season_ids),
            or_(
                ProtectedMedia.permanent.is_(True),
                ProtectedMedia.expires_at.is_(None),
                ProtectedMedia.expires_at > now,
            ),
        )
    )
    season_protected = {p.season_id: p for p in prot_result.scalars().all()}

    items: list[SeasonWithStatus] = []
    for season in seasons:
        cand = season_candidates.get(season.id)
        prot = season_protected.get(season.id)
        season_status = MediaStatusInfo(
            is_candidate=cand is not None,
            candidate_id=cand.id if cand else None,
            candidate_reason=cand.reason if cand else None,
            candidate_space_gb=cand.estimated_space_gb if cand else None,
            is_protected=prot is not None,
            protected_reason=prot.reason if prot else None,
            protected_permanent=prot.permanent if prot else True,
        )
        items.append(
            SeasonWithStatus(
                id=season.id,
                season_number=season.season_number,
                episode_count=season.episode_count,
                size=season.size,
                view_count=season.view_count or 0,
                last_viewed_at=to_utc_isoformat(season.last_viewed_at),
                never_watched=season.never_watched
                if season.never_watched is not None
                else True,
                air_date=to_utc_isoformat(season.air_date),
                status=season_status,
            )
        )

    return items


@router.get("/candidates", response_model=PaginatedCandidatesResponse)
async def get_candidates(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    sort_by: str = Query(
        "created_at",
        pattern="^(created_at|media_title|estimated_space_gb)$",
    ),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    search: str | None = Query(None, max_length=200),
    media_type: MediaType | None = Query(None),
):
    """Get all reclaim candidates with media info and pending request status."""
    base_query = (
        select(
            ReclaimCandidate,
            Movie.title.label("movie_title"),
            Movie.year.label("movie_year"),
            Movie.poster_url.label("movie_poster_url"),
            Series.title.label("series_title"),
            Series.year.label("series_year"),
            Series.poster_url.label("series_poster_url"),
            Season.season_number.label("season_number"),
        )
        .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
        .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
        .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
    )

    if media_type:
        base_query = base_query.where(ReclaimCandidate.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        base_query = base_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                ReclaimCandidate.reason.ilike(search_term),
            )
        )

    count_query = (
        select(func.count(ReclaimCandidate.id))
        .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
        .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
        .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
    )

    if media_type:
        count_query = count_query.where(ReclaimCandidate.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        count_query = count_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                ReclaimCandidate.reason.ilike(search_term),
            )
        )

    total = (await db.execute(count_query)).scalar_one() or 0

    media_title_expr = func.coalesce(Movie.title, Series.title)
    if sort_by == "media_title":
        order_expr = media_title_expr
    elif sort_by == "estimated_space_gb":
        order_expr = ReclaimCandidate.estimated_space_gb
    else:
        order_expr = ReclaimCandidate.created_at

    if sort_order == "desc":
        order_expr = order_expr.desc()
        id_tiebreak = ReclaimCandidate.id.desc()
    else:
        order_expr = order_expr.asc()
        id_tiebreak = ReclaimCandidate.id.asc()

    offset = (page - 1) * per_page
    result = await db.execute(
        base_query.order_by(order_expr, id_tiebreak).offset(offset).limit(per_page)
    )
    rows = result.all()

    # collect IDs to check for pending exception requests in one query each
    movie_ids = [
        r.ReclaimCandidate.movie_id for r in rows if r.ReclaimCandidate.movie_id
    ]
    series_ids = [
        r.ReclaimCandidate.series_id for r in rows if r.ReclaimCandidate.series_id
    ]

    pending_movies: set[int] = set()
    pending_series: set[int] = set()

    if movie_ids:
        req_result = await db.execute(
            select(ProtectionRequest.movie_id).where(
                ProtectionRequest.movie_id.in_(movie_ids),
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            )
        )
        pending_movies = {r[0] for r in req_result.all()}

    if series_ids:
        req_result = await db.execute(
            select(ProtectionRequest.series_id).where(
                ProtectionRequest.series_id.in_(series_ids),
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
            )
        )
        pending_series = {r[0] for r in req_result.all()}

    items_out: list[CandidateEntry] = []
    for row in rows:
        c = row.ReclaimCandidate
        is_movie = c.media_type is MediaType.MOVIE
        media_id = c.movie_id if is_movie else c.series_id
        media_title = row.movie_title if is_movie else row.series_title
        media_year = row.movie_year if is_movie else row.series_year
        poster_url = row.movie_poster_url if is_movie else row.series_poster_url
        has_pending = (
            c.movie_id in pending_movies if is_movie else c.series_id in pending_series
        )

        if media_id is None or media_title is None:
            continue

        items_out.append(
            CandidateEntry(
                id=c.id,
                media_type=c.media_type.value,
                media_id=media_id,
                media_title=media_title,
                media_year=media_year,
                poster_url=poster_url,
                reason=c.reason,
                estimated_space_gb=c.estimated_space_gb,
                has_pending_request=has_pending,
                created_at=to_utc_isoformat(c.created_at) or "",
                season_id=c.season_id,
                season_number=row.season_number,
                series_title=row.series_title if c.season_id is not None else None,
            )
        )

    total_pages = (total + per_page - 1) // per_page if total else 0
    return PaginatedCandidatesResponse(
        items=items_out,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.post("/candidates/delete", response_model=DeleteCandidatesResponse)
async def delete_candidates(
    request: DeleteCandidatesRequest,
    user: Annotated[User, Depends(get_current_user)],
    _db: AsyncSession = Depends(get_db),
):
    """Delete specific reclaim candidates, removing them from Radarr/Sonarr/Plex/Jellyfin.

    Requires admin or manage_reclaim permission. Uses same deletion priority as
    the automated task: Radarr/Sonarr first, then Jellyfin/Plex fallback.
    """
    if not (
        user.role is UserRole.ADMIN or has_permission(user, Permission.MANAGE_RECLAIM)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage reclaim permission required",
        )

    if not request.candidate_ids:
        return DeleteCandidatesResponse(deleted=0, failed=0)

    deleted, failed = await delete_specific_candidates(request.candidate_ids)
    return DeleteCandidatesResponse(deleted=deleted, failed=failed)
