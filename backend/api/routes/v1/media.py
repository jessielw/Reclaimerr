from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.core.api_tokens import (
    API_TOKEN_MEDIA_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.utils.misc import normalize_genre_names
from backend.database import get_db
from backend.database.models import (
    Movie,
    ProtectedMedia,
    ReclaimCandidate,
    Series,
)
from backend.enums import MediaType
from backend.models.api_v1 import MediaListResponse, MediaResponse
from backend.models.api_v1.common import total_pages

router = APIRouter(tags=["v1:media"])


async def _related_ids(
    db: AsyncSession, media_type: MediaType, media_ids: list[int]
) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    candidates: dict[int, list[int]] = defaultdict(list)
    protections: dict[int, list[int]] = defaultdict(list)
    if not media_ids:
        return candidates, protections
    media_column = (
        ReclaimCandidate.movie_id
        if media_type is MediaType.MOVIE
        else ReclaimCandidate.series_id
    )
    for candidate_id, media_id in (
        await db.execute(
            select(ReclaimCandidate.id, media_column).where(media_column.in_(media_ids))
        )
    ).all():
        if media_id is not None:
            candidates[media_id].append(candidate_id)

    protection_column = (
        ProtectedMedia.movie_id
        if media_type is MediaType.MOVIE
        else ProtectedMedia.series_id
    )
    now = datetime.now(UTC)
    active = or_(ProtectedMedia.permanent.is_(True), ProtectedMedia.expires_at > now)
    for protection_id, media_id in (
        await db.execute(
            select(ProtectedMedia.id, protection_column).where(
                protection_column.in_(media_ids), active
            )
        )
    ).all():
        if media_id is not None:
            protections[media_id].append(protection_id)
    return candidates, protections


def _movie_response(
    movie: Movie,
    candidate_ids: dict[int, list[int]],
    protection_ids: dict[int, list[int]],
) -> MediaResponse:
    return MediaResponse(
        id=movie.id,
        media_type=MediaType.MOVIE,
        title=movie.title,
        year=movie.year,
        tmdb_id=movie.tmdb_id,
        imdb_id=movie.imdb_id,
        status=movie.status,
        size_bytes=movie.size,
        poster_url=movie.poster_url,
        genres=normalize_genre_names(movie.genres) or [],
        is_monitored=movie.is_monitored,
        added_at=movie.added_at,
        arr_added_at=movie.arr_added_at,
        last_viewed_at=movie.last_viewed_at,
        view_count=movie.view_count,
        removed_at=movie.removed_at,
        candidate_ids=candidate_ids.get(movie.id, []),
        protection_ids=protection_ids.get(movie.id, []),
    )


def _series_response(
    series: Series,
    candidate_ids: dict[int, list[int]],
    protection_ids: dict[int, list[int]],
) -> MediaResponse:
    return MediaResponse(
        id=series.id,
        media_type=MediaType.SERIES,
        title=series.title,
        year=series.year,
        tmdb_id=series.tmdb_id,
        imdb_id=series.imdb_id,
        tvdb_id=series.tvdb_id,
        status=series.status,
        size_bytes=series.size,
        poster_url=series.poster_url,
        genres=normalize_genre_names(series.genres) or [],
        is_monitored=series.is_monitored,
        added_at=series.added_at,
        arr_added_at=series.arr_added_at,
        last_viewed_at=series.last_viewed_at,
        view_count=series.view_count,
        removed_at=series.removed_at,
        candidate_ids=candidate_ids.get(series.id, []),
        protection_ids=protection_ids.get(series.id, []),
    )


@router.get("/movies", response_model=MediaListResponse)
async def list_movies(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_MEDIA_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    search: str | None = Query(default=None, max_length=200),
    tmdb_id: int | None = Query(default=None, ge=1),
    imdb_id: str | None = Query(default=None, max_length=20),
    status_filter: str | None = Query(default=None, alias="status", max_length=50),
    include_removed: bool = False,
    sort_by: Literal["title", "year", "added_at", "size"] = "title",
    sort_order: Literal["asc", "desc"] = "asc",
) -> MediaListResponse:
    filters: list[ColumnElement[bool]] = []
    if not include_removed:
        filters.append(Movie.removed_at.is_(None))
    if search:
        filters.append(Movie.title.ilike(f"%{search.strip()}%"))
    if tmdb_id is not None:
        filters.append(Movie.tmdb_id == tmdb_id)
    if imdb_id is not None:
        filters.append(Movie.imdb_id == imdb_id)
    if status_filter is not None:
        filters.append(Movie.status == status_filter)
    total = int(
        (
            await db.execute(select(func.count()).select_from(Movie).where(*filters))
        ).scalar_one()
    )
    columns = {
        "title": Movie.title,
        "year": Movie.year,
        "added_at": Movie.added_at,
        "size": Movie.size,
    }
    query = select(Movie).where(*filters)
    order_column = columns[sort_by]
    query = query.order_by(
        order_column.desc() if sort_order == "desc" else order_column.asc(),
        Movie.id.asc(),
    )
    rows = (
        (await db.execute(query.offset((page - 1) * per_page).limit(per_page)))
        .scalars()
        .all()
    )
    candidates, protections = await _related_ids(
        db, MediaType.MOVIE, [row.id for row in rows]
    )
    return MediaListResponse(
        items=[_movie_response(row, candidates, protections) for row in rows],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@router.get("/movies/{media_id}", response_model=MediaResponse)
async def get_movie(
    media_id: int,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_MEDIA_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaResponse:
    movie = await db.get(Movie, media_id)
    if movie is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found"
        )
    candidates, protections = await _related_ids(db, MediaType.MOVIE, [movie.id])
    return _movie_response(movie, candidates, protections)


@router.get("/series", response_model=MediaListResponse)
async def list_series(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_MEDIA_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    search: str | None = Query(default=None, max_length=200),
    tmdb_id: int | None = Query(default=None, ge=1),
    tvdb_id: str | None = Query(default=None, max_length=20),
    imdb_id: str | None = Query(default=None, max_length=20),
    status_filter: str | None = Query(default=None, alias="status", max_length=50),
    include_removed: bool = False,
    sort_by: Literal["title", "year", "added_at", "size"] = "title",
    sort_order: Literal["asc", "desc"] = "asc",
) -> MediaListResponse:
    filters: list[ColumnElement[bool]] = []
    if not include_removed:
        filters.append(Series.removed_at.is_(None))
    if search:
        filters.append(Series.title.ilike(f"%{search.strip()}%"))
    if tmdb_id is not None:
        filters.append(Series.tmdb_id == tmdb_id)
    if tvdb_id is not None:
        filters.append(Series.tvdb_id == tvdb_id)
    if imdb_id is not None:
        filters.append(Series.imdb_id == imdb_id)
    if status_filter is not None:
        filters.append(Series.status == status_filter)
    total = int(
        (
            await db.execute(select(func.count()).select_from(Series).where(*filters))
        ).scalar_one()
    )
    columns = {
        "title": Series.title,
        "year": Series.year,
        "added_at": Series.added_at,
        "size": Series.size,
    }
    query = select(Series).where(*filters)
    order_column = columns[sort_by]
    query = query.order_by(
        order_column.desc() if sort_order == "desc" else order_column.asc(),
        Series.id.asc(),
    )
    rows = (
        (await db.execute(query.offset((page - 1) * per_page).limit(per_page)))
        .scalars()
        .all()
    )
    candidates, protections = await _related_ids(
        db, MediaType.SERIES, [row.id for row in rows]
    )
    return MediaListResponse(
        items=[_series_response(row, candidates, protections) for row in rows],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@router.get("/series/{media_id}", response_model=MediaResponse)
async def get_series(
    media_id: int,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_MEDIA_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MediaResponse:
    series = await db.get(Series, media_id)
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
        )
    candidates, protections = await _related_ids(db, MediaType.SERIES, [series.id])
    return _series_response(series, candidates, protections)
