from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from backend.api.routes.v1.dependencies import ensure_no_active_file_operation
from backend.core.api_tokens import (
    API_TOKEN_PROTECTIONS_MANAGE_SCOPE,
    API_TOKEN_PROTECTIONS_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.utils.datetime_utils import ensure_utc
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database import get_db
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    Season,
    Series,
)
from backend.enums import MediaType
from backend.models.api_v1 import (
    ProtectionCreateRequest,
    ProtectionListResponse,
    ProtectionMutationResponse,
    ProtectionResponse,
)
from backend.models.api_v1.common import total_pages
from backend.services.lifecycle_webhooks import (
    add_lifecycle_event,
    enqueue_event_deliveries,
)

router = APIRouter(tags=["v1:protections"])


def _active_filter(now: datetime) -> ColumnElement[bool]:
    return or_(ProtectedMedia.permanent.is_(True), ProtectedMedia.expires_at > now)


def _nullable_match(
    column: InstrumentedAttribute[int | None], value: int | None
) -> ColumnElement[bool]:
    return column.is_(None) if value is None else column == value


async def _protection_response(
    db: AsyncSession, protection: ProtectedMedia
) -> ProtectionResponse:
    season = (
        await db.get(Season, protection.season_id) if protection.season_id else None
    )
    episode = (
        await db.get(Episode, protection.episode_id) if protection.episode_id else None
    )
    if protection.media_type is MediaType.MOVIE:
        movie = await db.get(Movie, protection.movie_id)
        if movie is None:
            raise HTTPException(
                status_code=404, detail="Protected movie no longer exists"
            )
        scope: Literal["movie", "version", "series", "season", "episode"] = (
            "version" if protection.movie_version_id is not None else "movie"
        )
        media_id = movie.id
        series_id = None
        title = movie.title
        year = movie.year
        tmdb_id = movie.tmdb_id
    else:
        series = await db.get(Series, protection.series_id)
        if series is None:
            raise HTTPException(
                status_code=404, detail="Protected series no longer exists"
            )
        scope = (
            "episode"
            if protection.episode_id is not None
            else "season"
            if protection.season_id is not None
            else "series"
        )
        media_id = series.id
        series_id = series.id
        title = series.title
        year = series.year
        tmdb_id = series.tmdb_id
    now = datetime.now(UTC)
    active = protection.permanent or (
        protection.expires_at is not None and ensure_utc(protection.expires_at) > now
    )
    return ProtectionResponse(
        id=protection.id,
        media_type=protection.media_type,
        scope=scope,
        media_id=media_id,
        movie_version_id=protection.movie_version_id,
        series_id=series_id,
        season_id=protection.season_id,
        season_number=season.season_number if season else None,
        episode_id=protection.episode_id,
        episode_number=episode.episode_number if episode else None,
        title=title,
        year=year,
        tmdb_id=tmdb_id,
        source=protection.source,
        reason=protection.reason,
        permanent=protection.permanent,
        expires_at=protection.expires_at,
        active=active,
        created_at=protection.created_at,
        updated_at=protection.updated_at,
    )


async def _resolve_target(
    db: AsyncSession, request: ProtectionCreateRequest
) -> tuple[Movie | Series, MovieVersion | None, Season | None, Episode | None]:
    if request.media_type is MediaType.MOVIE:
        movie = (
            await db.get(Movie, request.media_id)
            if request.media_id is not None
            else (
                await db.execute(select(Movie).where(Movie.tmdb_id == request.tmdb_id))
            ).scalar_one_or_none()
        )
        if movie is None or movie.removed_at is not None:
            raise HTTPException(status_code=404, detail="Movie not found")
        version = (
            await db.get(MovieVersion, request.movie_version_id)
            if request.movie_version_id is not None
            else None
        )
        if version is not None and version.movie_id != movie.id:
            raise HTTPException(
                status_code=422, detail="Movie version does not belong to movie"
            )
        if request.movie_version_id is not None and version is None:
            raise HTTPException(status_code=404, detail="Movie version not found")
        return movie, version, None, None

    series = (
        await db.get(Series, request.media_id)
        if request.media_id is not None
        else (
            await db.execute(select(Series).where(Series.tmdb_id == request.tmdb_id))
        ).scalar_one_or_none()
    )
    if series is None or series.removed_at is not None:
        raise HTTPException(status_code=404, detail="Series not found")
    season = await db.get(Season, request.season_id) if request.season_id else None
    if request.season_id is not None and season is None:
        raise HTTPException(status_code=404, detail="Season not found")
    if season is not None and season.series_id != series.id:
        raise HTTPException(status_code=422, detail="Season does not belong to series")
    episode = await db.get(Episode, request.episode_id) if request.episode_id else None
    if request.episode_id is not None and episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    if episode is not None:
        episode_season = await db.get(Season, episode.season_id)
        if episode_season is None or episode_season.series_id != series.id:
            raise HTTPException(
                status_code=422, detail="Episode does not belong to series"
            )
        if season is not None and episode.season_id != season.id:
            raise HTTPException(
                status_code=422, detail="Episode does not belong to season"
            )
        season = episode_season
    return series, None, season, episode


async def _matching_candidate_ids(
    db: AsyncSession,
    media: Movie | Series,
    version: MovieVersion | None,
    season: Season | None,
    episode: Episode | None,
) -> list[int]:
    if isinstance(media, Movie):
        query = select(ReclaimCandidate.id).where(ReclaimCandidate.movie_id == media.id)
        if version is not None:
            query = query.where(ReclaimCandidate.movie_version_id == version.id)
    else:
        query = select(ReclaimCandidate.id).where(
            ReclaimCandidate.series_id == media.id
        )
        if episode is not None:
            query = query.where(ReclaimCandidate.episode_id == episode.id)
        elif season is not None:
            query = query.where(
                or_(
                    ReclaimCandidate.season_id == season.id,
                    ReclaimCandidate.episode_id.in_(
                        select(Episode.id).where(Episode.season_id == season.id)
                    ),
                )
            )
    return list((await db.execute(query)).scalars().all())


@router.get("/protections", response_model=ProtectionListResponse)
async def list_protections(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_PROTECTIONS_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    media_type: MediaType | None = None,
    media_id: int | None = Query(default=None, ge=1),
    active_only: bool = True,
) -> ProtectionListResponse:
    filters: list[ColumnElement[bool]] = []
    if media_type is not None:
        filters.append(ProtectedMedia.media_type == media_type)
        if media_id is not None:
            column = (
                ProtectedMedia.movie_id
                if media_type is MediaType.MOVIE
                else ProtectedMedia.series_id
            )
            filters.append(column == media_id)
    elif media_id is not None:
        filters.append(
            or_(
                ProtectedMedia.movie_id == media_id,
                ProtectedMedia.series_id == media_id,
            )
        )
    if active_only:
        filters.append(_active_filter(datetime.now(UTC)))
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(ProtectedMedia).where(*filters)
            )
        ).scalar_one()
    )
    rows = (
        (
            await db.execute(
                select(ProtectedMedia)
                .where(*filters)
                .order_by(ProtectedMedia.created_at.desc(), ProtectedMedia.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        )
        .scalars()
        .all()
    )
    return ProtectionListResponse(
        items=[await _protection_response(db, row) for row in rows],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@router.get("/protections/{protection_id}", response_model=ProtectionResponse)
async def get_protection(
    protection_id: int,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_PROTECTIONS_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProtectionResponse:
    protection = await db.get(ProtectedMedia, protection_id)
    if protection is None:
        raise HTTPException(status_code=404, detail="Protection not found")
    return await _protection_response(db, protection)


@router.post("/protections", response_model=ProtectionMutationResponse)
async def create_protection(
    request: ProtectionCreateRequest,
    principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_PROTECTIONS_MANAGE_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProtectionMutationResponse:
    expires_at = ensure_utc(request.expires_at) if request.expires_at else None
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=422, detail="Protection expiration must be in the future"
        )
    async with candidate_workflow_lock:
        media, version, season, episode = await _resolve_target(db, request)
        candidate_ids = await _matching_candidate_ids(
            db, media, version, season, episode
        )
        await ensure_no_active_file_operation(db, candidate_ids)
        movie_id = media.id if isinstance(media, Movie) else None
        series_id = media.id if isinstance(media, Series) else None
        exact = and_(
            ProtectedMedia.media_type == request.media_type,
            _nullable_match(ProtectedMedia.movie_id, movie_id),
            _nullable_match(
                ProtectedMedia.movie_version_id, version.id if version else None
            ),
            _nullable_match(ProtectedMedia.series_id, series_id),
            _nullable_match(ProtectedMedia.season_id, season.id if season else None),
            _nullable_match(ProtectedMedia.episode_id, episode.id if episode else None),
            _active_filter(datetime.now(UTC)),
        )
        protection = (
            await db.execute(select(ProtectedMedia).where(exact).limit(1))
        ).scalar_one_or_none()
        created = protection is None
        event_id: int | None = None
        if protection is None:
            protection = ProtectedMedia(
                media_type=request.media_type,
                protected_by_user_id=None,
                protected_by_api_token_id=principal.token_id,
                movie_id=movie_id,
                movie_version_id=version.id if version else None,
                series_id=series_id,
                season_id=season.id if season else None,
                episode_id=episode.id if episode else None,
                source="api",
                reason=request.reason
                or f"Protected through API token {principal.name}",
                permanent=expires_at is None,
                expires_at=expires_at,
            )
            db.add(protection)
            await db.flush()
            protection_response = await _protection_response(db, protection)
            event = await add_lifecycle_event(
                db,
                event_type="protection.created",
                resource_name="protection",
                resource_payload=jsonable_encoder(protection_response),
                media_type=request.media_type.value,
                actor_api_token_id=principal.token_id,
                links={"self": f"/api/v1/protections/{protection.id}"},
            )
            event_id = event.id
            await db.commit()
            await db.refresh(protection)
        else:
            changed = False
            if expires_at is None and not protection.permanent:
                protection.permanent = True
                protection.expires_at = None
                changed = True
            elif (
                expires_at is not None
                and not protection.permanent
                and (
                    protection.expires_at is None
                    or ensure_utc(protection.expires_at) < expires_at
                )
            ):
                protection.expires_at = expires_at
                changed = True
            if request.reason and protection.reason != request.reason:
                protection.reason = request.reason
                changed = True
            if changed:
                protection.protected_by_api_token_id = principal.token_id
                await db.commit()
                await db.refresh(protection)
        response = ProtectionMutationResponse(
            protection=await _protection_response(db, protection), created=created
        )
        if event_id is not None:
            await enqueue_event_deliveries(event_id)
        return response


@router.delete("/protections/{protection_id}", response_model=ProtectionResponse)
async def delete_protection(
    protection_id: int,
    principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_PROTECTIONS_MANAGE_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProtectionResponse:
    async with candidate_workflow_lock:
        protection = await db.get(ProtectedMedia, protection_id)
        if protection is None:
            raise HTTPException(status_code=404, detail="Protection not found")
        if protection.source == "rule":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Rule-managed protections must be changed through their rule",
            )
        response = await _protection_response(db, protection)
        event = await add_lifecycle_event(
            db,
            event_type="protection.removed",
            resource_name="protection",
            resource_payload=jsonable_encoder(response),
            media_type=response.media_type.value,
            actor_api_token_id=principal.token_id,
            links={"collection": "/api/v1/protections"},
        )
        await db.delete(protection)
        await db.commit()
        await enqueue_event_deliveries(event.id)
        return response
