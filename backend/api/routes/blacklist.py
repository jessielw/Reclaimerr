from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import get_current_user, has_permission
from backend.database import get_db
from backend.database.models import MediaBlacklist, Movie, Series, User
from backend.enums import MediaType, Permission, UserRole
from backend.models.blacklist import (
    BlacklistEntryResponse,
    CreateBlacklistEntryRequest,
    PaginatedBlacklistResponse,
    UpdateBlacklistDurationRequest,
)

router = APIRouter(prefix="/api/blacklist", tags=["blacklist"])


def can_manage_blacklist(user: User) -> bool:
    return user.role is UserRole.ADMIN or has_permission(
        user, Permission.MANAGE_BLACKLIST
    )


@router.get("", response_model=PaginatedBlacklistResponse)
async def get_blacklist_entries(
    _user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    search: str | None = Query(None, max_length=200),
    sort_by: str = Query("created_at", pattern="^(created_at|media_title|expires_at)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    media_type: MediaType | None = Query(None),
):
    """Retrieve a paginated list of blacklisted media entries."""
    base_query = (
        select(
            MediaBlacklist,
            Movie.title.label("movie_title"),
            Movie.year.label("movie_year"),
            Movie.poster_url.label("movie_poster_url"),
            Series.title.label("series_title"),
            Series.year.label("series_year"),
            Series.poster_url.label("series_poster_url"),
            User.username.label("actor_username"),
        )
        .outerjoin(Movie, Movie.id == MediaBlacklist.movie_id)
        .outerjoin(Series, Series.id == MediaBlacklist.series_id)
        .outerjoin(User, User.id == MediaBlacklist.blacklisted_by_user_id)
    )

    if media_type:
        base_query = base_query.where(MediaBlacklist.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        base_query = base_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                MediaBlacklist.reason.ilike(search_term),
                User.username.ilike(search_term),
            )
        )

    count_query = (
        select(func.count(MediaBlacklist.id))
        .outerjoin(Movie, Movie.id == MediaBlacklist.movie_id)
        .outerjoin(Series, Series.id == MediaBlacklist.series_id)
        .outerjoin(User, User.id == MediaBlacklist.blacklisted_by_user_id)
    )

    if media_type:
        count_query = count_query.where(MediaBlacklist.media_type == media_type)

    if search:
        search_term = f"%{search}%"
        count_query = count_query.where(
            or_(
                Movie.title.ilike(search_term),
                Series.title.ilike(search_term),
                MediaBlacklist.reason.ilike(search_term),
                User.username.ilike(search_term),
            )
        )

    total_result = await db.execute(count_query)
    total = total_result.scalar_one() or 0

    media_title_expr = func.coalesce(Movie.title, Series.title)
    if sort_by == "media_title":
        order_expr = media_title_expr
    elif sort_by == "expires_at":
        order_expr = MediaBlacklist.expires_at
    else:
        order_expr = MediaBlacklist.created_at

    if sort_order == "desc":
        order_expr = order_expr.desc()
    else:
        order_expr = order_expr.asc()

    offset = (page - 1) * per_page
    result = await db.execute(
        base_query.order_by(order_expr).offset(offset).limit(per_page)
    )
    rows = result.all()

    responses: list[BlacklistEntryResponse] = []
    for row in rows:
        entry: MediaBlacklist = row[0]
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
        media_id = (
            entry.movie_id if entry.media_type is MediaType.MOVIE else entry.series_id
        )

        if media_title is None or media_year is None or media_id is None:
            continue

        responses.append(
            BlacklistEntryResponse(
                id=entry.id,
                media_type=entry.media_type,
                media_id=media_id,
                media_title=media_title,
                media_year=media_year,
                poster_url=poster_url,
                reason=entry.reason,
                blacklisted_by_user_id=entry.blacklisted_by_user_id,
                blacklisted_by_username=row.actor_username or "Unknown",
                permanent=entry.permanent,
                expires_at=entry.expires_at.isoformat() if entry.expires_at else None,
                created_at=entry.created_at.isoformat(),
                updated_at=entry.updated_at.isoformat(),
            )
        )

    total_pages = (total + per_page - 1) // per_page if total else 0
    return PaginatedBlacklistResponse(
        items=responses,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )


@router.post(
    "", response_model=BlacklistEntryResponse, status_code=status.HTTP_201_CREATED
)
async def create_blacklist_entry(
    request_data: CreateBlacklistEntryRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Add a new media entry to the blacklist."""
    if not can_manage_blacklist(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage blacklist permission required",
        )

    if request_data.duration_days is not None and request_data.duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duration must be a positive number of days",
        )

    media = None
    if request_data.media_type is MediaType.MOVIE:
        media_result = await db.execute(
            select(Movie).where(
                Movie.id == request_data.media_id,
                Movie.removed_at.is_(None),
            )
        )
        media = media_result.scalar_one_or_none()
        existing_query = select(MediaBlacklist).where(
            MediaBlacklist.media_type == MediaType.MOVIE,
            MediaBlacklist.movie_id == request_data.media_id,
        )
    else:
        media_result = await db.execute(
            select(Series).where(
                Series.id == request_data.media_id,
                Series.removed_at.is_(None),
            )
        )
        media = media_result.scalar_one_or_none()
        existing_query = select(MediaBlacklist).where(
            MediaBlacklist.media_type == MediaType.SERIES,
            MediaBlacklist.series_id == request_data.media_id,
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
            detail="This media is already blacklisted",
        )

    permanent = request_data.duration_days is None
    expires_at = None
    if request_data.duration_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=request_data.duration_days
        )

    new_entry = MediaBlacklist(
        media_type=request_data.media_type,
        movie_id=request_data.media_id
        if request_data.media_type is MediaType.MOVIE
        else None,
        series_id=request_data.media_id
        if request_data.media_type is MediaType.SERIES
        else None,
        reason=request_data.reason,
        blacklisted_by_user_id=user.id,
        permanent=permanent,
        expires_at=expires_at,
    )

    db.add(new_entry)
    await db.commit()
    await db.refresh(new_entry)

    return BlacklistEntryResponse(
        id=new_entry.id,
        media_type=new_entry.media_type,
        media_id=request_data.media_id,
        media_title=media.title,
        media_year=media.year,
        poster_url=media.poster_url,
        reason=new_entry.reason,
        blacklisted_by_user_id=user.id,
        blacklisted_by_username=user.username,
        permanent=new_entry.permanent,
        expires_at=new_entry.expires_at.isoformat() if new_entry.expires_at else None,
        created_at=new_entry.created_at.isoformat(),
        updated_at=new_entry.updated_at.isoformat(),
    )


@router.put("/{entry_id}/duration", response_model=BlacklistEntryResponse)
async def update_blacklist_duration(
    entry_id: int,
    request_data: UpdateBlacklistDurationRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Update the duration of a blacklisted media entry."""
    if not can_manage_blacklist(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage blacklist permission required",
        )

    if request_data.duration_days is not None and request_data.duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duration must be a positive number of days",
        )

    result = await db.execute(
        select(MediaBlacklist).where(MediaBlacklist.id == entry_id)
    )
    entry = result.scalar_one_or_none()

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blacklist entry not found",
        )

    if request_data.duration_days is None:
        entry.permanent = True
        entry.expires_at = None
    else:
        entry.permanent = False
        entry.expires_at = datetime.now(timezone.utc) + timedelta(
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
        select(User).where(User.id == entry.blacklisted_by_user_id)
    )
    actor = actor_result.scalar_one_or_none()
    expires_at_value = entry.expires_at

    return BlacklistEntryResponse(
        id=entry.id,
        media_type=entry.media_type,
        media_id=media_id,
        media_title=media.title,
        media_year=media.year,
        poster_url=media.poster_url,
        reason=entry.reason,
        blacklisted_by_user_id=entry.blacklisted_by_user_id,
        blacklisted_by_username=actor.username if actor else "Unknown",
        permanent=entry.permanent,
        expires_at=expires_at_value.isoformat() if expires_at_value else None,
        created_at=entry.created_at.isoformat(),
        updated_at=entry.updated_at.isoformat(),
    )


@router.delete("/{entry_id}")
async def delete_blacklist_entry(
    entry_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Remove a media entry from the blacklist."""
    if not can_manage_blacklist(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Manage blacklist permission required",
        )

    result = await db.execute(
        select(MediaBlacklist).where(MediaBlacklist.id == entry_id)
    )
    entry = result.scalar_one_or_none()

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Blacklist entry not found",
        )

    await db.delete(entry)
    await db.commit()

    return {"message": "Blacklist entry removed successfully"}
