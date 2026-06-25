from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.core.auth import get_current_user, has_permission, require_page_access
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.database import get_db
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimRule,
    Season,
    Series,
    User,
)
from backend.enums import MediaType, PageAccess, Permission, UserRole
from backend.models.protect import (
    CreateProtectedEntryRequest,
    PaginatedProtectedResponse,
    ProtectedEntryResponse,
    UpdateProtectionDurationRequest,
)

router = APIRouter(prefix="/api/protected", tags=["protected"])


async def _resolve_series_scope(
    db: AsyncSession,
    *,
    media_id: int,
    season_id: int | None,
    episode_id: int | None,
) -> tuple[Season | None, Episode | None]:
    """Helper function to resolve season and episode based on provided IDs and media ID."""
    if season_id is not None and episode_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Specify either season_id or episode_id, not both",
        )

    if episode_id is not None:
        episode_result = await db.execute(
            select(Episode, Season)
            .join(Season, Episode.season_id == Season.id)
            .where(Episode.id == episode_id, Season.series_id == media_id)
        )
        row = episode_result.one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Episode not found",
            )
        return row.Season, row.Episode

    if season_id is not None:
        season_result = await db.execute(
            select(Season).where(
                Season.id == season_id,
                Season.series_id == media_id,
            )
        )
        season = season_result.scalar_one_or_none()
        if season is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Season not found"
            )
        return season, None

    return None, None


def _series_scope_overlap_clause(
    *,
    season_id: int | None,
    episode_id: int | None,
) -> ColumnElement[bool]:
    """Construct a SQLAlchemy clause to check for overlapping protection scopes based on season and episode IDs."""
    if episode_id is not None:
        return or_(
            and_(
                ProtectedMedia.season_id.is_(None), ProtectedMedia.episode_id.is_(None)
            ),
            and_(
                ProtectedMedia.season_id == season_id,
                ProtectedMedia.episode_id.is_(None),
            ),
            ProtectedMedia.episode_id == episode_id,
        )
    if season_id is not None:
        return or_(
            and_(
                ProtectedMedia.season_id.is_(None), ProtectedMedia.episode_id.is_(None)
            ),
            and_(
                ProtectedMedia.season_id == season_id,
                ProtectedMedia.episode_id.is_(None),
            ),
        )
    return and_(ProtectedMedia.season_id.is_(None), ProtectedMedia.episode_id.is_(None))


def can_manage_protection(user: User) -> bool:
    """Check if the user has permission to manage protected media entries."""
    return user.role is UserRole.ADMIN or has_permission(
        user, Permission.MANAGE_PROTECTION
    )


@router.get("", response_model=PaginatedProtectedResponse)
async def get_protected_entries(
    _user: Annotated[User, Depends(require_page_access(PageAccess.PROTECTED))],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    search: str | None = Query(None, max_length=200),
    sort_by: str = Query("created_at", pattern="^(created_at|media_title|expires_at)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    media_type: MediaType | None = Query(None),
) -> PaginatedProtectedResponse:
    """Retrieve a paginated list of protected media entries."""
    base_query = (
        select(
            ProtectedMedia,
            Movie.title.label("movie_title"),
            Movie.year.label("movie_year"),
            Movie.poster_url.label("movie_poster_url"),
            Movie.tmdb_id.label("movie_tmdb_id"),
            Movie.vote_average.label("movie_vote_average"),
            Movie.vote_count.label("movie_vote_count"),
            Movie.imdb_id.label("movie_imdb_id"),
            Movie.imdb_rating.label("movie_imdb_rating"),
            Movie.imdb_vote_count.label("movie_imdb_vote_count"),
            Movie.anilist_id.label("movie_anilist_id"),
            Movie.anilist_score.label("movie_anilist_score"),
            Movie.anilist_popularity.label("movie_anilist_popularity"),
            Movie.anilist_favourites.label("movie_anilist_favourites"),
            Movie.rottentomatoes_tomato_meter.label(
                "movie_rottentomatoes_tomato_meter"
            ),
            Movie.rottentomatoes_tomato_vote_count.label(
                "movie_rottentomatoes_tomato_vote_count"
            ),
            Movie.rottentomatoes_popcorn_meter.label(
                "movie_rottentomatoes_popcorn_meter"
            ),
            Movie.rottentomatoes_popcorn_vote_count.label(
                "movie_rottentomatoes_popcorn_vote_count"
            ),
            Movie.metacritic_metascore.label("movie_metacritic_metascore"),
            Movie.metacritic_vote_count.label("movie_metacritic_vote_count"),
            Movie.metacritic_user_score.label("movie_metacritic_user_score"),
            Movie.metacritic_user_vote_count.label("movie_metacritic_user_vote_count"),
            Movie.trakt_rating.label("movie_trakt_rating"),
            Movie.trakt_vote_count.label("movie_trakt_vote_count"),
            Movie.letterboxd_score.label("movie_letterboxd_score"),
            Movie.letterboxd_vote_count.label("movie_letterboxd_vote_count"),
            Movie.external_ratings_source.label("movie_external_ratings_source"),
            Movie.external_ratings_refreshed_at.label(
                "movie_external_ratings_refreshed_at"
            ),
            Series.title.label("series_title"),
            Series.year.label("series_year"),
            Series.poster_url.label("series_poster_url"),
            Series.tmdb_id.label("series_tmdb_id"),
            Series.vote_average.label("series_vote_average"),
            Series.vote_count.label("series_vote_count"),
            Series.imdb_id.label("series_imdb_id"),
            Series.imdb_rating.label("series_imdb_rating"),
            Series.imdb_vote_count.label("series_imdb_vote_count"),
            Series.anilist_id.label("series_anilist_id"),
            Series.anilist_score.label("series_anilist_score"),
            Series.anilist_popularity.label("series_anilist_popularity"),
            Series.anilist_favourites.label("series_anilist_favourites"),
            Series.rottentomatoes_tomato_meter.label(
                "series_rottentomatoes_tomato_meter"
            ),
            Series.rottentomatoes_tomato_vote_count.label(
                "series_rottentomatoes_tomato_vote_count"
            ),
            Series.rottentomatoes_popcorn_meter.label(
                "series_rottentomatoes_popcorn_meter"
            ),
            Series.rottentomatoes_popcorn_vote_count.label(
                "series_rottentomatoes_popcorn_vote_count"
            ),
            Series.metacritic_metascore.label("series_metacritic_metascore"),
            Series.metacritic_vote_count.label("series_metacritic_vote_count"),
            Series.metacritic_user_score.label("series_metacritic_user_score"),
            Series.metacritic_user_vote_count.label(
                "series_metacritic_user_vote_count"
            ),
            Series.trakt_rating.label("series_trakt_rating"),
            Series.trakt_vote_count.label("series_trakt_vote_count"),
            Series.letterboxd_score.label("series_letterboxd_score"),
            Series.letterboxd_vote_count.label("series_letterboxd_vote_count"),
            Series.external_ratings_source.label("series_external_ratings_source"),
            Series.external_ratings_refreshed_at.label(
                "series_external_ratings_refreshed_at"
            ),
            Season.season_number.label("season_number"),
            Episode.episode_number.label("episode_number"),
            Episode.name.label("episode_name"),
            User.username.label("actor_username"),
            ReclaimRule.name.label("source_rule_name"),
        )
        .outerjoin(Movie, Movie.id == ProtectedMedia.movie_id)
        .outerjoin(Series, Series.id == ProtectedMedia.series_id)
        .outerjoin(Season, Season.id == ProtectedMedia.season_id)
        .outerjoin(Episode, Episode.id == ProtectedMedia.episode_id)
        .outerjoin(User, User.id == ProtectedMedia.protected_by_user_id)
        .outerjoin(ReclaimRule, ReclaimRule.id == ProtectedMedia.source_rule_id)
    )

    if media_type:
        base_query = base_query.where(ProtectedMedia.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        base_query = base_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                ProtectedMedia.reason.ilike(search_term),
                User.username.ilike(search_term),
                ReclaimRule.name.ilike(search_term),
            )
        )

    count_query = (
        select(func.count(ProtectedMedia.id))
        .outerjoin(Movie, Movie.id == ProtectedMedia.movie_id)
        .outerjoin(Series, Series.id == ProtectedMedia.series_id)
        .outerjoin(User, User.id == ProtectedMedia.protected_by_user_id)
        .outerjoin(ReclaimRule, ReclaimRule.id == ProtectedMedia.source_rule_id)
    )

    if media_type:
        count_query = count_query.where(ProtectedMedia.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        count_query = count_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                ProtectedMedia.reason.ilike(search_term),
                User.username.ilike(search_term),
                ReclaimRule.name.ilike(search_term),
            )
        )

    total_result = await db.execute(count_query)
    total = total_result.scalar_one() or 0

    media_title_expr = func.coalesce(Movie.title, Series.title)
    order_expr: Any
    if sort_by == "media_title":
        order_expr = media_title_expr
    elif sort_by == "expires_at":
        order_expr = ProtectedMedia.expires_at
    else:
        order_expr = ProtectedMedia.created_at

    ordered_expr = order_expr.desc() if sort_order == "desc" else order_expr.asc()

    offset = (page - 1) * per_page
    result = await db.execute(
        base_query.order_by(ordered_expr).offset(offset).limit(per_page)
    )
    rows = result.all()

    responses: list[ProtectedEntryResponse] = []
    for row in rows:
        entry: ProtectedMedia = row[0]
        media_title = (
            row.movie_title if entry.media_type is MediaType.MOVIE else row.series_title
        )
        media_year = (
            row.movie_year if entry.media_type is MediaType.MOVIE else row.series_year
        )
        poster_url = (
            row.movie_poster_url
            if entry.media_type is MediaType.MOVIE
            else row.series_poster_url
        )
        tmdb_id = (
            row.movie_tmdb_id
            if entry.media_type is MediaType.MOVIE
            else row.series_tmdb_id
        )
        vote_average = (
            row.movie_vote_average
            if entry.media_type is MediaType.MOVIE
            else row.series_vote_average
        )
        vote_count = (
            row.movie_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_vote_count
        )
        imdb_id = (
            row.movie_imdb_id
            if entry.media_type is MediaType.MOVIE
            else row.series_imdb_id
        )
        imdb_rating = (
            row.movie_imdb_rating
            if entry.media_type is MediaType.MOVIE
            else row.series_imdb_rating
        )
        imdb_vote_count = (
            row.movie_imdb_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_imdb_vote_count
        )
        anilist_id = (
            row.movie_anilist_id
            if entry.media_type is MediaType.MOVIE
            else row.series_anilist_id
        )
        anilist_score = (
            row.movie_anilist_score
            if entry.media_type is MediaType.MOVIE
            else row.series_anilist_score
        )
        anilist_popularity = (
            row.movie_anilist_popularity
            if entry.media_type is MediaType.MOVIE
            else row.series_anilist_popularity
        )
        anilist_favourites = (
            row.movie_anilist_favourites
            if entry.media_type is MediaType.MOVIE
            else row.series_anilist_favourites
        )
        rottentomatoes_tomato_meter = (
            row.movie_rottentomatoes_tomato_meter
            if entry.media_type is MediaType.MOVIE
            else row.series_rottentomatoes_tomato_meter
        )
        rottentomatoes_tomato_vote_count = (
            row.movie_rottentomatoes_tomato_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_rottentomatoes_tomato_vote_count
        )
        rottentomatoes_popcorn_meter = (
            row.movie_rottentomatoes_popcorn_meter
            if entry.media_type is MediaType.MOVIE
            else row.series_rottentomatoes_popcorn_meter
        )
        rottentomatoes_popcorn_vote_count = (
            row.movie_rottentomatoes_popcorn_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_rottentomatoes_popcorn_vote_count
        )
        metacritic_metascore = (
            row.movie_metacritic_metascore
            if entry.media_type is MediaType.MOVIE
            else row.series_metacritic_metascore
        )
        metacritic_vote_count = (
            row.movie_metacritic_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_metacritic_vote_count
        )
        metacritic_user_score = (
            row.movie_metacritic_user_score
            if entry.media_type is MediaType.MOVIE
            else row.series_metacritic_user_score
        )
        metacritic_user_vote_count = (
            row.movie_metacritic_user_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_metacritic_user_vote_count
        )
        trakt_rating = (
            row.movie_trakt_rating
            if entry.media_type is MediaType.MOVIE
            else row.series_trakt_rating
        )
        trakt_vote_count = (
            row.movie_trakt_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_trakt_vote_count
        )
        letterboxd_score = (
            row.movie_letterboxd_score
            if entry.media_type is MediaType.MOVIE
            else row.series_letterboxd_score
        )
        letterboxd_vote_count = (
            row.movie_letterboxd_vote_count
            if entry.media_type is MediaType.MOVIE
            else row.series_letterboxd_vote_count
        )
        external_ratings_source = (
            row.movie_external_ratings_source
            if entry.media_type is MediaType.MOVIE
            else row.series_external_ratings_source
        )
        external_ratings_refreshed_at = (
            row.movie_external_ratings_refreshed_at
            if entry.media_type is MediaType.MOVIE
            else row.series_external_ratings_refreshed_at
        )
        media_id = (
            entry.movie_id if entry.media_type is MediaType.MOVIE else entry.series_id
        )

        if media_title is None or media_year is None or media_id is None:
            continue

        responses.append(
            ProtectedEntryResponse(
                id=entry.id,
                media_type=entry.media_type,
                media_id=media_id,
                movie_version_id=entry.movie_version_id,
                season_id=entry.season_id,
                season_number=row.season_number,
                episode_id=entry.episode_id,
                episode_number=row.episode_number,
                episode_name=row.episode_name,
                media_title=media_title,
                media_year=media_year,
                poster_url=poster_url,
                tmdb_id=tmdb_id,
                vote_average=vote_average,
                vote_count=vote_count,
                imdb_id=imdb_id,
                imdb_rating=imdb_rating,
                imdb_vote_count=imdb_vote_count,
                anilist_id=anilist_id,
                anilist_score=anilist_score,
                anilist_popularity=anilist_popularity,
                anilist_favourites=anilist_favourites,
                rottentomatoes_tomato_meter=rottentomatoes_tomato_meter,
                rottentomatoes_tomato_vote_count=(rottentomatoes_tomato_vote_count),
                rottentomatoes_popcorn_meter=rottentomatoes_popcorn_meter,
                rottentomatoes_popcorn_vote_count=(rottentomatoes_popcorn_vote_count),
                metacritic_metascore=metacritic_metascore,
                metacritic_vote_count=metacritic_vote_count,
                metacritic_user_score=metacritic_user_score,
                metacritic_user_vote_count=metacritic_user_vote_count,
                trakt_rating=trakt_rating,
                trakt_vote_count=trakt_vote_count,
                letterboxd_score=letterboxd_score,
                letterboxd_vote_count=letterboxd_vote_count,
                external_ratings_source=external_ratings_source,
                external_ratings_refreshed_at=to_utc_isoformat(
                    external_ratings_refreshed_at
                ),
                reason=entry.reason,
                protected_by_user_id=entry.protected_by_user_id,
                protected_by_username=row.actor_username
                or ("Automated rule" if entry.source == "rule" else "Unknown"),
                source="rule" if entry.source == "rule" else "manual",
                source_rule_id=entry.source_rule_id,
                source_rule_name=row.source_rule_name,
                permanent=entry.permanent,
                expires_at=to_utc_isoformat(entry.expires_at),
                created_at=to_utc_isoformat(entry.created_at) or "",
                updated_at=to_utc_isoformat(entry.updated_at) or "",
            )
        )

    total_pages = (total + per_page - 1) // per_page if total else 0
    return PaginatedProtectedResponse(
        items=responses,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.post(
    "", response_model=ProtectedEntryResponse, status_code=status.HTTP_201_CREATED
)
async def create_protection_entry(
    request_data: CreateProtectedEntryRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> ProtectedEntryResponse:
    """Add a new media entry to the protected list."""
    if not can_manage_protection(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage protection permission required",
        )

    if request_data.duration_days is not None and request_data.duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duration must be a positive number of days",
        )

    media = None
    season: Season | None = None
    episode: Episode | None = None
    if request_data.media_type is MediaType.MOVIE:
        if request_data.movie_version_id is not None:
            version_result = await db.execute(
                select(MovieVersion).where(
                    MovieVersion.id == request_data.movie_version_id,
                    MovieVersion.movie_id == request_data.media_id,
                )
            )
            if version_result.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Movie version not found",
                )
        if request_data.season_id is not None or request_data.episode_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="season_id and episode_id are only valid for series",
            )
        media_result = await db.execute(
            select(Movie).where(
                Movie.id == request_data.media_id,
                Movie.removed_at.is_(None),
            )
        )
        media = media_result.scalar_one_or_none()
        existing_query = select(ProtectedMedia).where(
            ProtectedMedia.source == "manual",
            ProtectedMedia.media_type == MediaType.MOVIE,
            ProtectedMedia.movie_id == request_data.media_id,
            ProtectedMedia.movie_version_id == request_data.movie_version_id,
        )
    else:
        if request_data.movie_version_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="movie_version_id is only valid for movies",
            )
        season, episode = await _resolve_series_scope(
            db,
            media_id=request_data.media_id,
            season_id=request_data.season_id,
            episode_id=request_data.episode_id,
        )
        media_result = await db.execute(
            select(Series).where(
                Series.id == request_data.media_id,
                Series.removed_at.is_(None),
            )
        )
        media = media_result.scalar_one_or_none()
        existing_query = select(ProtectedMedia).where(
            ProtectedMedia.source == "manual",
            ProtectedMedia.media_type == MediaType.SERIES,
            ProtectedMedia.series_id == request_data.media_id,
            _series_scope_overlap_clause(
                season_id=season.id if season else None,
                episode_id=episode.id if episode else None,
            ),
        )

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{request_data.media_type.value.title()} not found",
        )

    existing_result = await db.execute(existing_query)
    if existing_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This media is already protected",
        )

    permanent = request_data.duration_days is None
    expires_at = None
    if request_data.duration_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=request_data.duration_days)

    new_entry = ProtectedMedia(
        media_type=request_data.media_type,
        movie_id=request_data.media_id
        if request_data.media_type is MediaType.MOVIE
        else None,
        movie_version_id=request_data.movie_version_id,
        series_id=request_data.media_id
        if request_data.media_type is MediaType.SERIES
        else None,
        season_id=season.id if season else None,
        episode_id=episode.id if episode else None,
        reason=request_data.reason,
        protected_by_user_id=user.id,
        permanent=permanent,
        expires_at=expires_at,
    )

    db.add(new_entry)
    await db.commit()
    await db.refresh(new_entry)

    return ProtectedEntryResponse(
        id=new_entry.id,
        media_type=new_entry.media_type,
        media_id=request_data.media_id,
        movie_version_id=new_entry.movie_version_id,
        season_id=new_entry.season_id,
        season_number=season.season_number if season else None,
        episode_id=new_entry.episode_id,
        episode_number=episode.episode_number if episode else None,
        episode_name=episode.name if episode else None,
        media_title=media.title,
        media_year=media.year,
        poster_url=media.poster_url,
        tmdb_id=media.tmdb_id,
        vote_average=media.vote_average,
        vote_count=media.vote_count,
        imdb_id=media.imdb_id,
        imdb_rating=media.imdb_rating,
        imdb_vote_count=media.imdb_vote_count,
        anilist_id=media.anilist_id,
        anilist_score=media.anilist_score,
        anilist_popularity=media.anilist_popularity,
        anilist_favourites=media.anilist_favourites,
        rottentomatoes_tomato_meter=media.rottentomatoes_tomato_meter,
        rottentomatoes_tomato_vote_count=media.rottentomatoes_tomato_vote_count,
        rottentomatoes_popcorn_meter=media.rottentomatoes_popcorn_meter,
        rottentomatoes_popcorn_vote_count=media.rottentomatoes_popcorn_vote_count,
        metacritic_metascore=media.metacritic_metascore,
        metacritic_vote_count=media.metacritic_vote_count,
        metacritic_user_score=media.metacritic_user_score,
        metacritic_user_vote_count=media.metacritic_user_vote_count,
        trakt_rating=media.trakt_rating,
        trakt_vote_count=media.trakt_vote_count,
        letterboxd_score=media.letterboxd_score,
        letterboxd_vote_count=media.letterboxd_vote_count,
        external_ratings_source=media.external_ratings_source,
        external_ratings_refreshed_at=to_utc_isoformat(
            media.external_ratings_refreshed_at
        ),
        reason=new_entry.reason,
        protected_by_user_id=user.id,
        protected_by_username=user.username,
        source="manual",
        source_rule_id=None,
        source_rule_name=None,
        permanent=new_entry.permanent,
        expires_at=to_utc_isoformat(new_entry.expires_at),
        created_at=to_utc_isoformat(new_entry.created_at) or "",
        updated_at=to_utc_isoformat(new_entry.updated_at) or "",
    )


@router.put("/{entry_id}/duration", response_model=ProtectedEntryResponse)
async def update_protection_duration(
    entry_id: int,
    request_data: UpdateProtectionDurationRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> ProtectedEntryResponse:
    """Update the duration of a protected media entry."""
    if not can_manage_protection(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage protection permission required",
        )

    if request_data.duration_days is not None and request_data.duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duration must be a positive number of days",
        )

    result = await db.execute(
        select(ProtectedMedia).where(ProtectedMedia.id == entry_id)
    )
    entry = result.scalar_one_or_none()

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Protected entry not found",
        )
    if entry.source == "rule":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rule-managed protections can only be changed by editing the rule",
        )

    if request_data.duration_days is None:
        entry.permanent = True
        entry.expires_at = None
    else:
        entry.permanent = False
        entry.expires_at = datetime.now(UTC) + timedelta(
            days=request_data.duration_days
        )

    await db.commit()
    await db.refresh(entry)

    media = None
    media_id = (
        entry.movie_id if entry.media_type is MediaType.MOVIE else entry.series_id
    )

    if entry.media_type is MediaType.MOVIE and entry.movie_id is not None:
        media_result = await db.execute(select(Movie).where(Movie.id == entry.movie_id))
        media = media_result.scalar_one_or_none()
    elif entry.media_type is MediaType.SERIES and entry.series_id is not None:
        media_result = await db.execute(
            select(Series).where(Series.id == entry.series_id)
        )
        media = media_result.scalar_one_or_none()

    if not media or media_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Associated media not found",
        )

    actor_result = await db.execute(
        select(User).where(User.id == entry.protected_by_user_id)
    )
    actor = actor_result.scalar_one_or_none()
    season = await db.get(Season, entry.season_id) if entry.season_id else None
    episode = await db.get(Episode, entry.episode_id) if entry.episode_id else None
    expires_at_value = entry.expires_at

    return ProtectedEntryResponse(
        id=entry.id,
        media_type=entry.media_type,
        media_id=media_id,
        movie_version_id=entry.movie_version_id,
        season_id=entry.season_id,
        season_number=season.season_number if season else None,
        episode_id=entry.episode_id,
        episode_number=episode.episode_number if episode else None,
        episode_name=episode.name if episode else None,
        media_title=media.title,
        media_year=media.year,
        poster_url=media.poster_url,
        tmdb_id=media.tmdb_id,
        vote_average=media.vote_average,
        vote_count=media.vote_count,
        imdb_id=media.imdb_id,
        imdb_rating=media.imdb_rating,
        imdb_vote_count=media.imdb_vote_count,
        anilist_id=media.anilist_id,
        anilist_score=media.anilist_score,
        anilist_popularity=media.anilist_popularity,
        anilist_favourites=media.anilist_favourites,
        rottentomatoes_tomato_meter=media.rottentomatoes_tomato_meter,
        rottentomatoes_tomato_vote_count=media.rottentomatoes_tomato_vote_count,
        rottentomatoes_popcorn_meter=media.rottentomatoes_popcorn_meter,
        rottentomatoes_popcorn_vote_count=media.rottentomatoes_popcorn_vote_count,
        metacritic_metascore=media.metacritic_metascore,
        metacritic_vote_count=media.metacritic_vote_count,
        metacritic_user_score=media.metacritic_user_score,
        metacritic_user_vote_count=media.metacritic_user_vote_count,
        trakt_rating=media.trakt_rating,
        trakt_vote_count=media.trakt_vote_count,
        letterboxd_score=media.letterboxd_score,
        letterboxd_vote_count=media.letterboxd_vote_count,
        external_ratings_source=media.external_ratings_source,
        external_ratings_refreshed_at=to_utc_isoformat(
            media.external_ratings_refreshed_at
        ),
        reason=entry.reason,
        protected_by_user_id=entry.protected_by_user_id,
        protected_by_username=actor.username if actor else "Unknown",
        source="manual",
        source_rule_id=None,
        source_rule_name=None,
        permanent=entry.permanent,
        expires_at=to_utc_isoformat(expires_at_value),
        created_at=to_utc_isoformat(entry.created_at) or "",
        updated_at=to_utc_isoformat(entry.updated_at) or "",
    )


@router.delete("/{entry_id}")
async def delete_protection_entry(
    entry_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Remove a media entry from the protected list."""
    if not can_manage_protection(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage protection permission required",
        )

    result = await db.execute(
        select(ProtectedMedia).where(ProtectedMedia.id == entry_id)
    )
    entry = result.scalar_one_or_none()

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Protected entry not found",
        )
    if entry.source == "rule":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rule-managed protections can only be removed by editing the rule",
        )

    await db.delete(entry)
    await db.commit()

    return {"message": "Protection removed successfully"}
