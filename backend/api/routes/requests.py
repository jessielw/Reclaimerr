from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import get_current_user, has_permission, require_permission
from backend.core.logger import LOG
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.core.utils.resolution import guesstimate_resolution
from backend.database import get_db
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Season,
    Series,
    User,
)
from backend.enums import (
    MediaType,
    NotificationType,
    Permission,
    ProtectionRequestStatus,
)
from backend.models.requests import (
    CreateProtectionRequest,
    ProtectionRequestResponse,
    ReviewProtectionRequest,
)
from backend.services.notifications import notify_all_users, notify_user

router = APIRouter(prefix="/api", tags=["protection-requests"])


async def _resolve_series_scope(
    db: AsyncSession,
    *,
    media_id: int,
    season_id: int | None,
    episode_id: int | None,
) -> tuple[Season | None, Episode | None]:
    """Validate and resolve season/episode scope for a series level request."""
    if season_id is not None and episode_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Specify either season_id or episode_id, not both",
        )

    if episode_id is not None:
        episode_result = await db.execute(
            select(Episode, Season)
            .join(Season, Episode.season_id == Season.id)
            .where(
                Episode.id == episode_id,
                Season.series_id == media_id,
            )
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


def _series_scope_exact_clause(
    model: type[ProtectedMedia] | type[ProtectionRequest] | type[ReclaimCandidate],
    *,
    season_id: int | None,
    episode_id: int | None,
):
    """Build a SQLAlchemy clause to match entries that exactly match the specified season/episode scope."""
    if episode_id is not None:
        return model.episode_id == episode_id
    if season_id is not None:
        return and_(model.season_id == season_id, model.episode_id.is_(None))
    return and_(model.season_id.is_(None), model.episode_id.is_(None))


def _series_scope_overlap_clause(
    model: type[ProtectedMedia] | type[ProtectionRequest],
    *,
    season_id: int | None,
    episode_id: int | None,
):
    """Build a SQLAlchemy clause to match entries that overlap with the specified season/episode scope."""
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


async def resolve_effective_protection(
    db: AsyncSession,
    request: ProtectionRequest,
) -> tuple[bool | None, datetime | None]:
    """Resolve actual approved protection from protected entry for approved requests."""
    if request.status != ProtectionRequestStatus.APPROVED:
        return None, None

    protected_query = select(ProtectedMedia).where(
        ProtectedMedia.media_type == request.media_type
    )
    if request.media_type == MediaType.MOVIE:
        protected_query = protected_query.where(
            ProtectedMedia.movie_id == request.movie_id,
            ProtectedMedia.movie_version_id == request.movie_version_id,
        )
    else:
        protected_query = protected_query.where(
            ProtectedMedia.series_id == request.series_id,
            _series_scope_overlap_clause(
                ProtectedMedia,
                season_id=request.season_id,
                episode_id=request.episode_id,
            ),
        )

    result = await db.execute(protected_query)
    protection_entries = result.scalars().all()
    if protection_entries:
        protection_entry = sorted(
            protection_entries,
            key=lambda entry: (
                0
                if request.episode_id is not None
                and entry.episode_id == request.episode_id
                else 1
                if request.season_id is not None
                and entry.season_id == request.season_id
                and entry.episode_id is None
                else 2,
                entry.id,
            ),
        )[0]
        return protection_entry.permanent, protection_entry.expires_at

    LOG.warning(
        f"Approved request {request.id} has no protected entry - data inconsistency"
    )
    return None, None


def _request_target_scope(request: ProtectionRequest) -> str:
    """Determine the target scope string for a request based on its fields."""
    if request.target_scope:
        return request.target_scope
    if request.media_type is MediaType.MOVIE:
        return "movie_version" if request.movie_version_id is not None else "movie"
    if request.episode_id is not None or request.episode_number_snapshot is not None:
        return "episode"
    if request.season_id is not None or request.season_number_snapshot is not None:
        return "season"
    return "series"


async def _build_protection_request_response(
    db: AsyncSession,
    request: ProtectionRequest,
    media: Movie | Series,
    *,
    requested_by_username: str,
    reviewed_by_username: str | None,
    version: MovieVersion | None = None,
    effective_permanent: bool | None = None,
    effective_expires_at: datetime | None = None,
) -> ProtectionRequestResponse:
    """Build a ProtectionRequestResponse from a ProtectionRequest and related data."""
    season = request.season
    episode = request.episode
    season_number = season.season_number if season else request.season_number_snapshot
    episode_number = (
        episode.episode_number if episode else request.episode_number_snapshot
    )
    episode_name = episode.name if episode else request.episode_name_snapshot
    if (
        effective_permanent is None
        and effective_expires_at is None
        and request.status == ProtectionRequestStatus.APPROVED
    ):
        effective_permanent, effective_expires_at = await resolve_effective_protection(
            db, request
        )

    return ProtectionRequestResponse(
        id=request.id,
        media_type=request.media_type,
        media_id=(
            request.movie_id
            if request.media_type == MediaType.MOVIE
            else request.series_id
        )
        or 0,
        media_title=media.title,
        media_year=media.year,
        target_scope=_request_target_scope(request),
        poster_url=media.poster_url,
        candidate_id=request.candidate_id,
        movie_version_id=request.movie_version_id,
        season_id=request.season_id,
        season_number=season_number,
        episode_id=request.episode_id,
        episode_number=episode_number,
        episode_name=episode_name,
        requested_by_user_id=request.requested_by_user_id,
        requested_by_username=requested_by_username,
        reason=request.reason,
        requested_expires_at=to_utc_isoformat(request.requested_expires_at),
        status=request.status,
        reviewed_by_user_id=request.reviewed_by_user_id,
        reviewed_by_username=reviewed_by_username,
        reviewed_at=to_utc_isoformat(request.reviewed_at),
        admin_notes=request.admin_notes,
        effective_permanent=effective_permanent,
        effective_expires_at=to_utc_isoformat(effective_expires_at),
        created_at=to_utc_isoformat(request.created_at) or "",
        updated_at=to_utc_isoformat(request.updated_at) or "",
        version_resolution=version.video_resolution if version else None,
        version_file_name=version.file_name if version else None,
        version_size=version.size if version else None,
        version_video_codec=version.video_codec if version else None,
        version_hdr=version.video_hdr if version else None,
        version_dolby_vision=version.video_dolby_vision if version else None,
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
    "/protection-requests",
    response_model=ProtectionRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_protection_request(
    request_data: CreateProtectionRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Create a new exception request for a media item."""
    if not has_permission(user, Permission.REQUEST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request permission required",
        )

    # verify media exists
    if request_data.media_type is MediaType.MOVIE:
        result = await db.execute(
            select(Movie).where(
                Movie.id == request_data.media_id, Movie.removed_at.is_(None)
            )
        )
        media = result.scalar_one_or_none()
    else:
        result = await db.execute(
            select(Series).where(
                Series.id == request_data.media_id, Series.removed_at.is_(None)
            )
        )
        media = result.scalar_one_or_none()

    if not media:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{request_data.media_type.value.title()} not found",
        )

    # validate scoped series target if provided
    season: Season | None = None
    episode: Episode | None = None
    movie_version: MovieVersion | None = None
    if request_data.season_id is not None or request_data.episode_id is not None:
        if request_data.media_type is not MediaType.SERIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="season_id and episode_id are only valid for series",
            )
        season, episode = await _resolve_series_scope(
            db,
            media_id=request_data.media_id,
            season_id=request_data.season_id,
            episode_id=request_data.episode_id,
        )

    if request_data.movie_version_id is not None:
        if request_data.media_type is not MediaType.MOVIE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="movie_version_id is only valid for movies",
            )
        version_result = await db.execute(
            select(MovieVersion).where(
                MovieVersion.id == request_data.movie_version_id,
                MovieVersion.movie_id == request_data.media_id,
            )
        )
        movie_version = version_result.scalar_one_or_none()
        if not movie_version:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Movie version not found",
            )

    # check if already protected
    protected_query = select(ProtectedMedia).where(
        ProtectedMedia.media_type == request_data.media_type
    )
    if request_data.media_type is MediaType.MOVIE:
        protected_query = protected_query.where(
            ProtectedMedia.movie_id == request_data.media_id,
            ProtectedMedia.movie_version_id == request_data.movie_version_id,
        )
    else:
        protected_query = protected_query.where(
            ProtectedMedia.series_id == request_data.media_id,
            _series_scope_overlap_clause(
                ProtectedMedia,
                season_id=season.id if season else None,
                episode_id=episode.id if episode else None,
            ),
        )

    result = await db.execute(protected_query)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This media is already protected from deletion",
        )

    # check if user already has a pending request for this media
    existing_query = select(ProtectionRequest).where(
        ProtectionRequest.media_type == request_data.media_type,
        ProtectionRequest.requested_by_user_id == user.id,
        ProtectionRequest.status == ProtectionRequestStatus.PENDING,
    )
    if request_data.media_type == MediaType.MOVIE:
        existing_query = existing_query.where(
            ProtectionRequest.movie_id == request_data.media_id,
            ProtectionRequest.movie_version_id == request_data.movie_version_id,
        )
    else:
        existing_query = existing_query.where(
            ProtectionRequest.series_id == request_data.media_id,
            _series_scope_overlap_clause(
                ProtectionRequest,
                season_id=season.id if season else None,
                episode_id=episode.id if episode else None,
            ),
        )

    result = await db.execute(existing_query)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have a pending request for this media",
        )

    # check if there's a candidate for this media
    candidate_query = select(ReclaimCandidate).where(
        ReclaimCandidate.media_type == request_data.media_type
    )
    if request_data.media_type is MediaType.MOVIE:
        candidate_query = candidate_query.where(
            ReclaimCandidate.movie_id == request_data.media_id,
            ReclaimCandidate.movie_version_id == request_data.movie_version_id,
        )
    else:
        candidate_query = candidate_query.where(
            ReclaimCandidate.series_id == request_data.media_id,
            _series_scope_exact_clause(
                ReclaimCandidate,
                season_id=season.id if season else None,
                episode_id=episode.id if episode else None,
            ),
        )

    result = await db.execute(candidate_query)
    candidate = result.scalar_one_or_none()

    if request_data.duration_days is not None and request_data.duration_days <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duration must be a positive number of days",
        )

    requested_expires_at = None
    if request_data.duration_days is not None:
        requested_expires_at = datetime.now(UTC) + timedelta(
            days=request_data.duration_days
        )

    # users with auto approve permission are immediately approved and protected
    auto_approve = has_permission(user, Permission.AUTO_APPROVE)
    now = datetime.now(UTC)
    bl_permanent: bool | None = None
    bl_expires_at: datetime | None = None

    # create exception request
    target_scope = (
        "movie_version"
        if request_data.media_type is MediaType.MOVIE
        and request_data.movie_version_id is not None
        else request_data.media_type.value
        if request_data.media_type is MediaType.MOVIE
        else "episode"
        if episode is not None
        else "season"
        if season is not None
        else "series"
    )
    protection_request = ProtectionRequest(
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
        candidate_id=candidate.id if candidate else None,
        requested_by_user_id=user.id,
        reason=request_data.reason,
        requested_expires_at=requested_expires_at,
        status=ProtectionRequestStatus.APPROVED
        if auto_approve
        else ProtectionRequestStatus.PENDING,
        reviewed_by_user_id=user.id if auto_approve else None,
        reviewed_at=now if auto_approve else None,
    )

    db.add(protection_request)

    if auto_approve:
        # determine protection duration (same logic as approve endpoint)
        if requested_expires_at is None:
            bl_permanent = True
            bl_expires_at = None
        else:
            bl_permanent = False
            bl_expires_at = requested_expires_at

        protection_entry = ProtectedMedia(
            media_type=request_data.media_type,
            movie_id=request_data.media_id
            if request_data.media_type == MediaType.MOVIE
            else None,
            movie_version_id=request_data.movie_version_id,
            series_id=request_data.media_id
            if request_data.media_type == MediaType.SERIES
            else None,
            season_id=season.id if season else None,
            episode_id=episode.id if episode else None,
            reason=request_data.reason,
            protected_by_user_id=user.id,
            permanent=bl_permanent,
            expires_at=bl_expires_at,
        )
        db.add(protection_entry)

        # remove from candidates if present
        if candidate:
            await db.delete(candidate)

    await db.commit()
    await db.refresh(protection_request)

    if auto_approve:
        LOG.info(
            f"User {user.username} auto-approved exception for "
            f"{request_data.media_type.value} '{media.title}' (ID: {media.id})"
        )
    else:
        LOG.info(
            f"User {user.username} created exception request for "
            f"{request_data.media_type.value} '{media.title}' (ID: {media.id})"
        )

        # notify admins of new pending request
        try:
            await notify_all_users(
                notification_type=NotificationType.ADMIN_MESSAGE,
                title="New Exception Request",
                message=f"{user.username} requested an exception for {media.title}",
                context={
                    "actor": user.username,
                    "media_title": media.title,
                    "media_type": request_data.media_type.value,
                    "reason": request_data.reason,
                },
            )
        except Exception as e:
            LOG.error(f"Failed to send notification: {e}")

    # build response
    return await _build_protection_request_response(
        db,
        protection_request,
        media,
        requested_by_username=user.username,
        reviewed_by_username=user.username if auto_approve else None,
        version=movie_version,
        effective_permanent=bl_permanent if auto_approve else None,
        effective_expires_at=bl_expires_at if auto_approve else None,
    )


@router.get("/protection-requests/my", response_model=list[ProtectionRequestResponse])
async def get_my_requests(
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    status_filter: ProtectionRequestStatus | None = Query(None),
):
    """Get current user's exception requests."""
    query = (
        select(ProtectionRequest)
        .where(ProtectionRequest.requested_by_user_id == user.id)
        .options(
            selectinload(ProtectionRequest.movie),
            selectinload(ProtectionRequest.series),
            selectinload(ProtectionRequest.season),
            selectinload(ProtectionRequest.episode),
        )
    )

    if status_filter:
        query = query.where(ProtectionRequest.status == status_filter)

    query = query.order_by(ProtectionRequest.created_at.desc())

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

    # build responses
    responses = []
    for req in requests:
        # get media via eager loaded relationship
        media = req.movie if req.media_type is MediaType.MOVIE else req.series
        if not media:
            LOG.warning(
                f"Media not found for request {req.id} (media_type={req.media_type}, media_id={req.movie_id or req.series_id})"
            )
            continue
        reviewed_by_username = req.reviewed_by.username if req.reviewed_by else None

        # get reviewer username if reviewed
        reviewed_by_username = None
        if req.reviewed_by_user_id:
            reviewer_result = await db.execute(
                select(User).where(User.id == req.reviewed_by_user_id)
            )
            reviewer = reviewer_result.scalar_one_or_none()
            reviewed_by_username = reviewer.username if reviewer else None

        version = (
            versions_by_id.get(req.movie_version_id) if req.movie_version_id else None
        )
        responses.append(
            await _build_protection_request_response(
                db,
                req,
                media,
                requested_by_username=user.username,
                reviewed_by_username=reviewed_by_username,
                version=version,
            )
        )

    return responses


@router.get("/protection-requests", response_model=list[ProtectionRequestResponse])
async def get_all_requests(
    _manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
    status_filter: ProtectionRequestStatus | None = Query(None),
):
    """Get all exception requests (manage-requests permission)."""
    query = select(ProtectionRequest).options(
        selectinload(ProtectionRequest.movie),
        selectinload(ProtectionRequest.series),
        selectinload(ProtectionRequest.season),
        selectinload(ProtectionRequest.episode),
        selectinload(ProtectionRequest.requested_by),
        selectinload(ProtectionRequest.reviewed_by),
    )

    if status_filter:
        query = query.where(ProtectionRequest.status == status_filter)

    query = query.order_by(ProtectionRequest.created_at.desc())

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

    # build responses
    responses = []
    for req in requests:
        # get media/requester/reviewer via eager-loaded relationships
        media = req.movie if req.media_type is MediaType.MOVIE else req.series
        if not media:
            LOG.warning(
                f"Media not found for request {req.id} (media_type={req.media_type}, "
                f"media_id={req.movie_id or req.series_id})"
            )
            continue
        reviewed_by_username = req.reviewed_by.username if req.reviewed_by else None
        version = (
            versions_by_id.get(req.movie_version_id) if req.movie_version_id else None
        )
        responses.append(
            await _build_protection_request_response(
                db,
                req,
                media,
                requested_by_username=req.requested_by.username,
                reviewed_by_username=reviewed_by_username,
                version=version,
            )
        )

    return responses


@router.post(
    "/protection-requests/{request_id}/approve",
    response_model=ProtectionRequestResponse,
)
async def approve_request(
    request_id: int,
    review_data: ReviewProtectionRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
):
    """
    Approve an exception request.

    This will:
    1. Mark the request as approved
    2. Add the media to the protected list with the specified duration
    3. Remove it from candidates if present
    4. Notify the requester
    """
    # get request
    result = await db.execute(
        select(ProtectionRequest).where(ProtectionRequest.id == request_id)
    )
    request = result.scalar_one_or_none()

    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )

    if request.status != ProtectionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request has already been {request.status.value}",
        )

    # get media
    if request.media_type is MediaType.MOVIE:
        media_result = await db.execute(
            select(Movie).where(Movie.id == request.movie_id)
        )
        media = media_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found"
            )
    else:
        media_result = await db.execute(
            select(Series).where(Series.id == request.series_id)
        )
        media = media_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
            )

    # update request status
    request.status = ProtectionRequestStatus.APPROVED
    request.reviewed_by_user_id = manager.id
    request.reviewed_at = datetime.now(UTC)
    request.admin_notes = review_data.admin_notes

    if (
        review_data.approved_duration_days is not None
        and review_data.approved_duration_days <= 0
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Approved duration must be a positive number of days",
        )

    if review_data.approved_permanent is True:
        approved_permanent = True
        approved_expires_at = None
    elif review_data.approved_duration_days is not None:
        approved_permanent = False
        approved_expires_at = datetime.now(UTC) + timedelta(
            days=review_data.approved_duration_days
        )
    elif request.requested_expires_at:
        approved_permanent = False
        approved_expires_at = request.requested_expires_at
    else:
        approved_permanent = True
        approved_expires_at = None

    # add to protected list
    protection_entry = ProtectedMedia(
        media_type=request.media_type,
        movie_id=request.movie_id,
        movie_version_id=request.movie_version_id,
        series_id=request.series_id,
        season_id=request.season_id,
        episode_id=request.episode_id,
        reason=f"Exception request approved: {request.reason}",
        protected_by_user_id=manager.id,
        permanent=approved_permanent,
        expires_at=approved_expires_at,
    )
    db.add(protection_entry)

    # remove from candidates if present
    if request.candidate_id:
        candidate_result = await db.execute(
            select(ReclaimCandidate).where(ReclaimCandidate.id == request.candidate_id)
        )
        candidate = candidate_result.scalar_one_or_none()
        if candidate:
            await db.delete(candidate)

    await db.commit()
    await db.refresh(request)

    LOG.info(
        f"User {manager.username} approved exception request {request_id} "
        f"for {request.media_type.value} '{media.title}'"
    )

    # notify requester
    try:
        await notify_user(
            user_id=request.requested_by_user_id,
            notification_type=NotificationType.REQUEST_APPROVED,
            title="Exception Request Approved",
            message=f"Your request for {media.title} has been approved",
            context={
                "media_title": media.title,
                "media_type": request.media_type.value,
                "reason": request.reason,
                "admin_notes": review_data.admin_notes,
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    approve_version: MovieVersion | None = None
    if request.movie_version_id:
        approve_version = await db.get(MovieVersion, request.movie_version_id)
    requester_username = (
        await db.execute(
            select(User.username).where(User.id == request.requested_by_user_id)
        )
    ).scalar_one_or_none() or "unknown"
    return await _build_protection_request_response(
        db,
        request,
        media,
        requested_by_username=requester_username,
        reviewed_by_username=manager.username,
        version=approve_version,
        effective_permanent=approved_permanent,
        effective_expires_at=approved_expires_at,
    )


@router.post(
    "/protection-requests/{request_id}/deny", response_model=ProtectionRequestResponse
)
async def deny_request(
    request_id: int,
    review_data: ReviewProtectionRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
):
    """
    Deny an exception request.

    This will:
    1. Mark the request as denied
    2. Notify the requester
    """
    # get request
    result = await db.execute(
        select(ProtectionRequest).where(ProtectionRequest.id == request_id)
    )
    request = result.scalar_one_or_none()

    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )

    if request.status != ProtectionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request has already been {request.status.value}",
        )

    # get media
    if request.media_type is MediaType.MOVIE:
        media_result = await db.execute(
            select(Movie).where(Movie.id == request.movie_id)
        )
        media = media_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found"
            )
    else:
        media_result = await db.execute(
            select(Series).where(Series.id == request.series_id)
        )
        media = media_result.scalar_one_or_none()
        if media is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
            )

    # update request status
    request.status = ProtectionRequestStatus.DENIED
    request.reviewed_by_user_id = manager.id
    request.reviewed_at = datetime.now(UTC)
    request.admin_notes = review_data.admin_notes

    await db.commit()
    await db.refresh(request)

    LOG.info(
        f"User {manager.username} denied exception request {request_id} "
        f"for {request.media_type.value} '{media.title}'"
    )

    # notify requester
    try:
        await notify_user(
            user_id=request.requested_by_user_id,
            notification_type=NotificationType.REQUEST_DECLINED,
            title="Exception Request Denied",
            message=f"Your request for {media.title} has been denied",
            context={
                "media_title": media.title,
                "media_type": request.media_type.value,
                "reason": request.reason,
                "admin_notes": review_data.admin_notes,
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    deny_version: MovieVersion | None = None
    if request.movie_version_id:
        deny_version = await db.get(MovieVersion, request.movie_version_id)
    requester_username = (
        await db.execute(
            select(User.username).where(User.id == request.requested_by_user_id)
        )
    ).scalar_one_or_none() or "unknown"
    return await _build_protection_request_response(
        db,
        request,
        media,
        requested_by_username=requester_username,
        reviewed_by_username=manager.username,
        version=deny_version,
    )


@router.delete("/protection-requests/{request_id}")
async def cancel_request(
    request_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Cancel own pending exception request."""
    # get request
    result = await db.execute(
        select(ProtectionRequest).where(
            ProtectionRequest.id == request_id,
            ProtectionRequest.requested_by_user_id == user.id,
        )
    )
    request = result.scalar_one_or_none()

    if not request:
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

    LOG.info(f"User {user.username} cancelled exception request {request_id}")

    return {"message": "Request cancelled"}
