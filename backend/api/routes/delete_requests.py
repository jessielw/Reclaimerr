from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import get_current_user, has_permission, require_permission
from backend.core.logger import LOG
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.core.utils.resolution import guesstimate_resolution
from backend.database import get_db
from backend.database.models import (
    DeleteRequest,
    Episode,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    Season,
    Series,
    User,
)
from backend.enums import (
    CandidateFileOpOperation,
    MediaType,
    NotificationType,
    Permission,
    ProtectionRequestStatus,
)
from backend.jobs.candidate_file_ops import queue_candidate_file_op_job
from backend.models.jobs import CandidateFileOpJobItem
from backend.models.requests import (
    CreateDeleteRequest,
    DeleteRequestResponse,
    ReviewDeleteRequest,
)
from backend.services.notifications import notify_all_users, notify_user

router = APIRouter(prefix="/api", tags=["delete-requests"])


async def _resolve_series_scope(
    db: AsyncSession,
    *,
    media_id: int,
    season_id: int | None,
    episode_id: int | None,
) -> tuple[Season | None, Episode | None]:
    """Resolve season and episode based on provided IDs, ensuring they belong to the
    specified series and that both are not provided simultaneously.
    """
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
    model: type[DeleteRequest] | type[ProtectedMedia],
    *,
    season_id: int | None,
    episode_id: int | None,
) -> ColumnElement[bool]:
    """Build a SQLAlchemy clause to match delete requests or protections that overlap
    with the specified series scope."""
    if episode_id is not None:
        return or_(
            and_(model.season_id.is_(None), model.episode_id.is_(None)),
            and_(model.season_id == season_id, model.episode_id.is_(None)),
            model.episode_id == episode_id,
        )
    if season_id is not None:
        return or_(
            and_(model.season_id.is_(None), model.episode_id.is_(None)),
            and_(model.season_id == season_id, model.episode_id.is_(None)),
        )
    return and_(model.season_id.is_(None), model.episode_id.is_(None))


def _active_protection_filter() -> ColumnElement[bool]:
    """Check for active protection."""
    now = datetime.now(UTC)
    return or_(
        ProtectedMedia.permanent.is_(True),
        ProtectedMedia.expires_at.is_(None),
        ProtectedMedia.expires_at > now,
    )


async def _get_delete_request_media(
    db: AsyncSession, request: DeleteRequest
) -> tuple[Movie | Series, int]:
    """Get the media (movie or series) associated with a delete request."""
    if request.media_type is MediaType.MOVIE:
        result = await db.execute(select(Movie).where(Movie.id == request.movie_id))
        movie = result.scalar_one_or_none()
        if movie is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found"
            )
        return movie, request.movie_id or 0

    result = await db.execute(select(Series).where(Series.id == request.series_id))
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
        )
    return series, request.series_id or 0


async def _lookup_season_number(
    db: AsyncSession,
    season_id: int | None,
    season: Season | None = None,
    season_number_snapshot: int | None = None,
) -> int | None:
    """Lookup season number by season_id, with optional short-circuit if season already provided."""
    if season is not None:
        return season.season_number
    if season_number_snapshot is not None:
        return season_number_snapshot
    if not season_id:
        return None
    result = await db.execute(
        select(Season.season_number).where(Season.id == season_id)
    )
    return result.scalar_one_or_none()


async def _lookup_episode_fields(
    db: AsyncSession,
    episode_id: int | None,
    episode: Episode | None = None,
    episode_number_snapshot: int | None = None,
    episode_name_snapshot: str | None = None,
) -> tuple[int | None, str | None]:
    if episode is not None:
        return episode.episode_number, episode.name
    if episode_number_snapshot is not None or episode_name_snapshot is not None:
        return episode_number_snapshot, episode_name_snapshot
    if not episode_id:
        return None, None
    result = await db.execute(
        select(Episode.episode_number, Episode.name).where(Episode.id == episode_id)
    )
    row = result.one_or_none()
    if row is None:
        return None, None
    return row.episode_number, row.name


def _quality_suffix(
    resolution: str | None,
    hdr: bool | None,
    dolby_vision: bool | None,
) -> str:
    parts: list[str] = []
    if resolution:
        parts.append(resolution)
    if hdr:
        parts.append("HDR")
    if dolby_vision:
        parts.append("DV")
    return f" - {' '.join(parts)}" if parts else ""


async def _build_delete_request_job_item(
    db: AsyncSession,
    request: DeleteRequest,
    *,
    media_title: str,
    media_year: int | None,
    media_tmdb_id: int | None,
    candidate_id: int,
) -> CandidateFileOpJobItem:
    """Build a CandidateFileOpJobItem for a delete request, looking up any additional
    metadata needed for the job item."""
    titled_media = (
        f"{media_title} ({media_year})" if media_year is not None else media_title
    )

    if request.media_type is MediaType.MOVIE:
        if request.movie_version_id is None:
            return CandidateFileOpJobItem(
                candidate_id=candidate_id,
                media_type=MediaType.MOVIE,
                scope="movie",
                title=media_title,
                year=media_year,
                tmdb_id=media_tmdb_id,
                display_label=titled_media,
            )

        version_result = await db.execute(
            select(
                MovieVersion.video_resolution,
                MovieVersion.video_hdr,
                MovieVersion.video_dolby_vision,
            ).where(MovieVersion.id == request.movie_version_id)
        )
        version_row = version_result.one_or_none()
        resolution = version_row.video_resolution if version_row else None
        hdr = version_row.video_hdr if version_row else None
        dolby_vision = version_row.video_dolby_vision if version_row else None
        return CandidateFileOpJobItem(
            candidate_id=candidate_id,
            media_type=MediaType.MOVIE,
            scope="version",
            title=media_title,
            year=media_year,
            tmdb_id=media_tmdb_id,
            resolution=resolution,
            hdr=hdr,
            dolby_vision=dolby_vision,
            display_label=f"{titled_media}{_quality_suffix(resolution, hdr, dolby_vision)}",
        )

    if request.episode_id is not None or request.episode_number_snapshot is not None:
        season_number = await _lookup_season_number(
            db,
            request.season_id,
            season_number_snapshot=request.season_number_snapshot,
        )
        episode_number, episode_name = await _lookup_episode_fields(
            db,
            request.episode_id,
            episode_number_snapshot=request.episode_number_snapshot,
            episode_name_snapshot=request.episode_name_snapshot,
        )
        season_result = await db.execute(
            select(
                Season.max_video_width,
                Season.max_video_height,
                Season.has_hdr,
                Season.has_dolby_vision,
            ).where(Season.id == request.season_id)
        )
        season_row = season_result.one_or_none()
        resolution = (
            guesstimate_resolution(
                season_row.max_video_width,
                season_row.max_video_height,
                None,
            )
            if season_row
            else None
        )
        hdr = season_row.has_hdr if season_row else None
        dolby_vision = season_row.has_dolby_vision if season_row else None
        episode_tag = f"S{int(season_number or 0):02d}E{int(episode_number or 0):02d}"
        title = f"{titled_media} - {episode_tag}"
        if episode_name:
            title = f"{title} - {episode_name}"
        return CandidateFileOpJobItem(
            candidate_id=candidate_id,
            media_type=MediaType.SERIES,
            scope="episode",
            title=media_title,
            year=media_year,
            tmdb_id=media_tmdb_id,
            season_number=season_number,
            episode_number=episode_number,
            episode_name=episode_name,
            resolution=resolution,
            hdr=hdr,
            dolby_vision=dolby_vision,
            display_label=f"{title}{_quality_suffix(resolution, hdr, dolby_vision)}",
        )

    if request.season_id is not None or request.season_number_snapshot is not None:
        season_number = await _lookup_season_number(
            db,
            request.season_id,
            season_number_snapshot=request.season_number_snapshot,
        )
        season_result = await db.execute(
            select(
                Season.max_video_width,
                Season.max_video_height,
                Season.has_hdr,
                Season.has_dolby_vision,
            ).where(Season.id == request.season_id)
        )
        season_row = season_result.one_or_none()
        resolution = (
            guesstimate_resolution(
                season_row.max_video_width,
                season_row.max_video_height,
                None,
            )
            if season_row
            else None
        )
        hdr = season_row.has_hdr if season_row else None
        dolby_vision = season_row.has_dolby_vision if season_row else None
        title = (
            f"{titled_media} - Season {int(season_number)}"
            if season_number is not None
            else titled_media
        )
        return CandidateFileOpJobItem(
            candidate_id=candidate_id,
            media_type=MediaType.SERIES,
            scope="season",
            title=media_title,
            year=media_year,
            tmdb_id=media_tmdb_id,
            season_number=season_number,
            resolution=resolution,
            hdr=hdr,
            dolby_vision=dolby_vision,
            display_label=f"{title}{_quality_suffix(resolution, hdr, dolby_vision)}",
        )

    return CandidateFileOpJobItem(
        candidate_id=candidate_id,
        media_type=MediaType.SERIES,
        scope="series",
        title=media_title,
        year=media_year,
        tmdb_id=media_tmdb_id,
        display_label=titled_media,
    )


def _request_target_scope(
    *,
    media_type: MediaType,
    movie_version_id: int | None,
    season_id: int | None,
    episode_id: int | None,
    target_scope: str | None,
    season_number_snapshot: int | None = None,
    episode_number_snapshot: int | None = None,
) -> str:
    if target_scope:
        return target_scope
    if media_type is MediaType.MOVIE:
        return "movie_version" if movie_version_id is not None else "movie"
    if episode_id is not None or episode_number_snapshot is not None:
        return "episode"
    if season_id is not None or season_number_snapshot is not None:
        return "season"
    return "series"


async def _build_delete_request_response(
    db: AsyncSession,
    request: DeleteRequest,
    requested_by_username: str | None = None,
    reviewed_by_username: str | None = None,
    version: MovieVersion | None = None,
) -> DeleteRequestResponse:
    """Build a DeleteRequestResponse from a DeleteRequest, looking up related media and user info as needed."""
    media, media_id = await _get_delete_request_media(db, request)
    if requested_by_username is None:
        if request.requested_by is not None:
            requested_by_username = request.requested_by.username
        else:
            requester = await db.execute(
                select(User.username).where(User.id == request.requested_by_user_id)
            )
            requested_by_username = requester.scalar_one_or_none() or "unknown"

    if reviewed_by_username is None and request.reviewed_by_user_id is not None:
        if request.reviewed_by is not None:
            reviewed_by_username = request.reviewed_by.username
        else:
            reviewer = await db.execute(
                select(User.username).where(User.id == request.reviewed_by_user_id)
            )
            reviewed_by_username = reviewer.scalar_one_or_none()

    season = request.season  # may be None if not eager loaded
    episode = request.episode  # may be None if not eager loaded
    episode_number, episode_name = await _lookup_episode_fields(
        db,
        request.episode_id,
        episode,
        request.episode_number_snapshot,
        request.episode_name_snapshot,
    )
    target_scope = _request_target_scope(
        media_type=request.media_type,
        movie_version_id=request.movie_version_id,
        season_id=request.season_id,
        episode_id=request.episode_id,
        target_scope=request.target_scope,
        season_number_snapshot=request.season_number_snapshot,
        episode_number_snapshot=request.episode_number_snapshot,
    )
    return DeleteRequestResponse(
        id=request.id,
        media_type=request.media_type,
        media_id=media_id,
        media_title=media.title,
        media_year=media.year,
        target_scope=target_scope,
        movie_version_id=request.movie_version_id,
        season_id=request.season_id,
        season_number=await _lookup_season_number(
            db,
            request.season_id,
            season,
            request.season_number_snapshot,
        ),
        episode_id=request.episode_id,
        episode_number=episode_number,
        episode_name=episode_name,
        requested_by_user_id=request.requested_by_user_id,
        requested_by_username=requested_by_username,
        reason=request.reason,
        status=request.status,
        reviewed_by_user_id=request.reviewed_by_user_id,
        reviewed_by_username=reviewed_by_username,
        reviewed_at=to_utc_isoformat(request.reviewed_at),
        admin_notes=request.admin_notes,
        executed_at=to_utc_isoformat(request.executed_at),
        execution_error=request.execution_error,
        created_at=to_utc_isoformat(request.created_at) or "",
        updated_at=to_utc_isoformat(request.updated_at) or "",
        poster_url=media.poster_url,
        # version specific metadata
        version_resolution=version.video_resolution if version else None,
        version_file_name=version.file_name if version else None,
        version_size=version.size if version else None,
        version_video_codec=version.video_codec if version else None,
        version_hdr=version.video_hdr if version else None,
        version_dolby_vision=version.video_dolby_vision if version else None,
        # season aggregate metadata
        season_size=season.size if season else None,
        season_resolution=guesstimate_resolution(
            season.max_video_width, season.max_video_height, None
        )
        if season
        else None,
        season_video_codecs=season.video_codec_families if season else None,
        season_hdr=season.has_hdr if season else None,
        season_dolby_vision=season.has_dolby_vision if season else None,
    )


@router.post(
    "/delete-requests",
    response_model=DeleteRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_delete_request(
    request_data: CreateDeleteRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> DeleteRequestResponse:
    """Create a delete request for a movie or series."""
    if not has_permission(user, Permission.REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request permission required",
        )

    season: Season | None = None
    episode: Episode | None = None
    movie_version: MovieVersion | None = None

    if request_data.media_type is MediaType.MOVIE:
        media_result = await db.execute(
            select(Movie).where(
                Movie.id == request_data.media_id, Movie.removed_at.is_(None)
            )
        )
        media = media_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found"
            )

        if request_data.season_id is not None or request_data.episode_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="season_id and episode_id are only valid for series",
            )

        if request_data.movie_version_id is not None:
            version_result = await db.execute(
                select(MovieVersion).where(
                    MovieVersion.id == request_data.movie_version_id,
                    MovieVersion.movie_id == request_data.media_id,
                )
            )
            movie_version = version_result.scalar_one_or_none()
            if movie_version is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Movie version not found",
                )
    else:
        media_result = await db.execute(
            select(Series).where(
                Series.id == request_data.media_id, Series.removed_at.is_(None)
            )
        )
        media = media_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
            )

        if request_data.movie_version_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="movie_version_id is only valid for movies",
            )

        if request_data.season_id is not None or request_data.episode_id is not None:
            season, episode = await _resolve_series_scope(
                db,
                media_id=request_data.media_id,
                season_id=request_data.season_id,
                episode_id=request_data.episode_id,
            )

    protected_query = select(ProtectedMedia).where(_active_protection_filter())
    if request_data.media_type is MediaType.MOVIE:
        if request_data.movie_version_id is not None:
            protected_query = protected_query.where(
                ProtectedMedia.movie_id == request_data.media_id,
                or_(
                    ProtectedMedia.movie_version_id.is_(None),
                    ProtectedMedia.movie_version_id == request_data.movie_version_id,
                ),
            )
        else:
            protected_query = protected_query.where(
                ProtectedMedia.movie_id == request_data.media_id
            )
    else:
        if season is not None or episode is not None:
            protected_query = protected_query.where(
                ProtectedMedia.series_id == request_data.media_id,
                _series_scope_overlap_clause(
                    ProtectedMedia,
                    season_id=season.id if season else None,
                    episode_id=episode.id if episode else None,
                ),
            )
        else:
            protected_query = protected_query.where(
                ProtectedMedia.series_id == request_data.media_id,
                _series_scope_overlap_clause(
                    ProtectedMedia,
                    season_id=None,
                    episode_id=None,
                ),
            )

    protected_result = await db.execute(protected_query)
    if protected_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This media is currently protected from deletion",
        )

    existing_query = select(DeleteRequest).where(
        DeleteRequest.media_type == request_data.media_type,
        DeleteRequest.requested_by_user_id == user.id,
        DeleteRequest.status == ProtectionRequestStatus.PENDING,
    )
    if request_data.media_type is MediaType.MOVIE:
        existing_query = existing_query.where(
            DeleteRequest.movie_id == request_data.media_id,
            DeleteRequest.movie_version_id == request_data.movie_version_id,
        )
    else:
        existing_query = existing_query.where(
            DeleteRequest.series_id == request_data.media_id,
            _series_scope_overlap_clause(
                DeleteRequest,
                season_id=season.id if season else None,
                episode_id=episode.id if episode else None,
            ),
        )
    existing_result = await db.execute(existing_query)
    if existing_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have a pending delete request for this media",
        )

    target_scope = _request_target_scope(
        media_type=request_data.media_type,
        movie_version_id=request_data.movie_version_id,
        season_id=season.id if season else None,
        episode_id=episode.id if episode else None,
        target_scope=None,
        season_number_snapshot=season.season_number if season else None,
        episode_number_snapshot=episode.episode_number if episode else None,
    )

    delete_request = DeleteRequest(
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
        target_scope=target_scope,
        season_number_snapshot=season.season_number if season else None,
        episode_number_snapshot=episode.episode_number if episode else None,
        episode_name_snapshot=episode.name if episode else None,
        requested_by_user_id=user.id,
        reason=request_data.reason,
    )
    db.add(delete_request)
    await db.commit()
    await db.refresh(delete_request)

    LOG.info(
        f"User {user.username} created delete request for "
        f"{request_data.media_type.value} '{media.title}' (ID: {request_data.media_id})"
    )

    try:
        await notify_all_users(
            notification_type=NotificationType.ADMIN_MESSAGE,
            title="New Delete Request",
            message=f"{user.username} requested deletion for {media.title}",
            context={
                "actor": user.username,
                "media_title": media.title,
                "media_type": request_data.media_type.value,
                "reason": request_data.reason,
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    return DeleteRequestResponse(
        id=delete_request.id,
        media_type=delete_request.media_type,
        media_id=request_data.media_id,
        media_title=media.title,
        media_year=media.year,
        target_scope=target_scope,
        movie_version_id=delete_request.movie_version_id,
        season_id=delete_request.season_id,
        season_number=season.season_number if season else None,
        episode_id=delete_request.episode_id,
        episode_number=episode.episode_number if episode else None,
        episode_name=episode.name if episode else None,
        requested_by_user_id=user.id,
        requested_by_username=user.username,
        reason=delete_request.reason,
        status=delete_request.status,
        reviewed_by_user_id=None,
        reviewed_by_username=None,
        reviewed_at=None,
        admin_notes=None,
        executed_at=None,
        execution_error=None,
        created_at=to_utc_isoformat(delete_request.created_at) or "",
        updated_at=to_utc_isoformat(delete_request.updated_at) or "",
        poster_url=media.poster_url,
        version_resolution=movie_version.video_resolution if movie_version else None,
        version_file_name=movie_version.file_name if movie_version else None,
        version_size=movie_version.size if movie_version else None,
        version_video_codec=movie_version.video_codec if movie_version else None,
        version_hdr=movie_version.video_hdr if movie_version else None,
        version_dolby_vision=movie_version.video_dolby_vision
        if movie_version
        else None,
        season_size=season.size if season else None,
        season_resolution=guesstimate_resolution(
            season.max_video_width, season.max_video_height, None
        )
        if season
        else None,
        season_video_codecs=season.video_codec_families if season else None,
        season_hdr=season.has_hdr if season else None,
        season_dolby_vision=season.has_dolby_vision if season else None,
    )


@router.get("/delete-requests/my", response_model=list[DeleteRequestResponse])
async def get_my_delete_requests(
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    status_filter: ProtectionRequestStatus | None = Query(None),
) -> list[DeleteRequestResponse]:
    """Get the current user's delete requests, optionally filtered by status."""
    query = (
        select(DeleteRequest)
        .where(DeleteRequest.requested_by_user_id == user.id)
        .options(
            selectinload(DeleteRequest.movie),
            selectinload(DeleteRequest.series),
            selectinload(DeleteRequest.season),
            selectinload(DeleteRequest.episode),
        )
        .order_by(DeleteRequest.created_at.desc())
    )
    if status_filter:
        query = query.where(DeleteRequest.status == status_filter)

    result = await db.execute(query)
    requests = result.scalars().all()

    version_ids = [r.movie_version_id for r in requests if r.movie_version_id]
    versions_by_id: dict[int, MovieVersion] = {}
    if version_ids:
        vr = await db.execute(
            select(MovieVersion).where(MovieVersion.id.in_(version_ids))
        )
        for v in vr.scalars().all():
            versions_by_id[v.id] = v

    return [
        await _build_delete_request_response(
            db,
            req,
            requested_by_username=user.username,
            version=versions_by_id.get(req.movie_version_id)
            if req.movie_version_id
            else None,
        )
        for req in requests
    ]


@router.get("/delete-requests", response_model=list[DeleteRequestResponse])
async def get_all_delete_requests(
    _manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
    status_filter: ProtectionRequestStatus | None = Query(None),
) -> list[DeleteRequestResponse]:
    """Get all delete requests, optionally filtered by status. Manager permission required."""
    query = (
        select(DeleteRequest)
        .options(
            selectinload(DeleteRequest.movie),
            selectinload(DeleteRequest.series),
            selectinload(DeleteRequest.season),
            selectinload(DeleteRequest.episode),
            selectinload(DeleteRequest.requested_by),
            selectinload(DeleteRequest.reviewed_by),
        )
        .order_by(DeleteRequest.created_at.desc())
    )
    if status_filter:
        query = query.where(DeleteRequest.status == status_filter)

    result = await db.execute(query)
    requests = result.scalars().all()

    version_ids = [r.movie_version_id for r in requests if r.movie_version_id]
    versions_by_id: dict[int, MovieVersion] = {}
    if version_ids:
        vr = await db.execute(
            select(MovieVersion).where(MovieVersion.id.in_(version_ids))
        )
        for v in vr.scalars().all():
            versions_by_id[v.id] = v

    return [
        await _build_delete_request_response(
            db,
            req,
            version=versions_by_id.get(req.movie_version_id)
            if req.movie_version_id
            else None,
        )
        for req in requests
    ]


@router.post(
    "/delete-requests/{request_id}/approve", response_model=DeleteRequestResponse
)
async def approve_delete_request(
    request_id: int,
    review_data: ReviewDeleteRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
) -> DeleteRequestResponse:
    """Approve a delete request and queue it for background execution."""
    result = await db.execute(
        select(DeleteRequest).where(DeleteRequest.id == request_id)
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if request.status != ProtectionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request has already been {request.status.value}",
        )

    media, _ = await _get_delete_request_media(db, request)
    media_year = getattr(media, "year", None)
    media_tmdb_id = getattr(media, "tmdb_id", None)

    request.status = ProtectionRequestStatus.APPROVED
    request.reviewed_by_user_id = manager.id
    request.reviewed_at = datetime.now(UTC)
    request.admin_notes = review_data.admin_notes
    request.execution_error = None

    candidate = ReclaimCandidate(
        media_type=request.media_type,
        matched_rule_ids=[],
        matched_criteria={},
        reason=f"Approved delete request: {request.reason or 'No reason provided'}",
        reason_data=None,
        movie_id=request.movie_id,
        movie_version_id=request.movie_version_id,
        series_id=request.series_id,
        season_id=request.season_id,
        episode_id=request.episode_id,
        reviewed=True,
        approved_for_deletion=True,
        tagged_in_arr=False,
        estimated_space_bytes=None,
    )
    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)

    item_detail = await _build_delete_request_job_item(
        db,
        request,
        media_title=media.title,
        media_year=media_year,
        media_tmdb_id=media_tmdb_id,
        candidate_id=candidate.id,
    )
    item_label = item_detail.display_label

    await queue_candidate_file_op_job(
        operation=CandidateFileOpOperation.DELETE,
        candidate_ids=[candidate.id],
        requested_by_user_id=manager.id,
        requested_by_username=manager.username,
        delete_request_id=request.id,
        item_labels=[item_label],
        item_label_total=1,
        item_details=[item_detail],
    )

    tracked_request = await db.get(DeleteRequest, request_id)
    if tracked_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    await db.refresh(tracked_request)

    LOG.info(
        f"User {manager.username} approved delete request {request_id} "
        f"for {request.media_type.value} '{media.title}'"
    )

    try:
        await notify_user(
            user_id=tracked_request.requested_by_user_id,
            notification_type=NotificationType.REQUEST_APPROVED,
            title="Delete Request Approved",
            message=f"Your delete request for {media.title} was approved and queued for execution",
            context={
                "media_title": media.title,
                "media_type": tracked_request.media_type.value,
                "reason": tracked_request.reason,
                "admin_notes": tracked_request.admin_notes,
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    return await _build_delete_request_response(
        db,
        tracked_request,
        reviewed_by_username=manager.username,
        version=await db.get(MovieVersion, tracked_request.movie_version_id)
        if tracked_request.movie_version_id
        else None,
    )


@router.post("/delete-requests/{request_id}/deny", response_model=DeleteRequestResponse)
async def deny_delete_request(
    request_id: int,
    review_data: ReviewDeleteRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
) -> DeleteRequestResponse:
    """Deny a delete request. Manager permission required."""
    result = await db.execute(
        select(DeleteRequest).where(DeleteRequest.id == request_id)
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if request.status != ProtectionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request has already been {request.status.value}",
        )

    media, _ = await _get_delete_request_media(db, request)
    request.status = ProtectionRequestStatus.DENIED
    request.reviewed_by_user_id = manager.id
    request.reviewed_at = datetime.now(UTC)
    request.admin_notes = review_data.admin_notes

    await db.commit()
    await db.refresh(request)

    LOG.info(
        f"User {manager.username} denied delete request {request_id} "
        f"for {request.media_type.value} '{media.title}'"
    )

    try:
        await notify_user(
            user_id=request.requested_by_user_id,
            notification_type=NotificationType.REQUEST_DECLINED,
            title="Delete Request Denied",
            message=f"Your delete request for {media.title} has been denied",
            context={
                "media_title": media.title,
                "media_type": request.media_type.value,
                "reason": request.reason,
                "admin_notes": review_data.admin_notes,
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    return await _build_delete_request_response(
        db,
        request,
        reviewed_by_username=manager.username,
        version=await db.get(MovieVersion, request.movie_version_id)
        if request.movie_version_id
        else None,
    )


@router.delete("/delete-requests/{request_id}")
async def cancel_delete_request(
    request_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Cancel a pending delete request. The request must be owned by the current user, and still pending."""
    result = await db.execute(
        select(DeleteRequest).where(
            DeleteRequest.id == request_id,
            DeleteRequest.requested_by_user_id == user.id,
        )
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    if request.status != ProtectionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only cancel pending requests",
        )

    await db.delete(request)
    await db.commit()

    LOG.info(f"User {user.username} cancelled delete request {request_id}")
    return {"message": "Request cancelled"}
