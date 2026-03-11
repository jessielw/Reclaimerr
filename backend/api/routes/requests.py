from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import get_current_user, has_permission, require_permission
from backend.core.logger import LOG
from backend.database import get_db
from backend.database.models import (
    ExceptionRequest,
    MediaBlacklist,
    Movie,
    ReclaimCandidate,
    Series,
    User,
)
from backend.enums import (
    ExceptionRequestStatus,
    MediaType,
    NotificationType,
    Permission,
)
from backend.models.requests import (
    CreateExceptionRequest,
    ExceptionRequestResponse,
    ReviewExceptionRequest,
)
from backend.services.notifications import notify_all_users, notify_user

router = APIRouter(prefix="/api", tags=["requests"])


async def resolve_effective_protection(
    db: AsyncSession,
    request: ExceptionRequest,
) -> tuple[bool | None, datetime | None]:
    """Resolve actual approved protection from blacklist entry for approved requests."""
    if request.status != ExceptionRequestStatus.APPROVED:
        return None, None

    blacklist_query = select(MediaBlacklist).where(
        MediaBlacklist.media_type == request.media_type
    )
    if request.media_type == MediaType.MOVIE:
        blacklist_query = blacklist_query.where(
            MediaBlacklist.movie_id == request.movie_id
        )
    else:
        blacklist_query = blacklist_query.where(
            MediaBlacklist.series_id == request.series_id
        )

    result = await db.execute(blacklist_query)
    blacklist_entry = result.scalar_one_or_none()
    if blacklist_entry:
        return blacklist_entry.permanent, blacklist_entry.expires_at

    LOG.warning(
        f"Approved request {request.id} has no blacklist entry - data inconsistency"
    )
    return None, None


@router.post(
    "/requests",
    response_model=ExceptionRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_exception_request(
    request_data: CreateExceptionRequest,
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

    # check if already blacklisted
    blacklist_query = select(MediaBlacklist).where(
        MediaBlacklist.media_type == request_data.media_type
    )
    if request_data.media_type is MediaType.MOVIE:
        blacklist_query = blacklist_query.where(
            MediaBlacklist.movie_id == request_data.media_id
        )
    else:
        blacklist_query = blacklist_query.where(
            MediaBlacklist.series_id == request_data.media_id
        )

    result = await db.execute(blacklist_query)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This media is already protected from deletion",
        )

    # check if user already has a pending request for this media
    existing_query = select(ExceptionRequest).where(
        ExceptionRequest.media_type == request_data.media_type,
        ExceptionRequest.requested_by_user_id == user.id,
        ExceptionRequest.status == ExceptionRequestStatus.PENDING,
    )
    if request_data.media_type == MediaType.MOVIE:
        existing_query = existing_query.where(
            ExceptionRequest.movie_id == request_data.media_id
        )
    else:
        existing_query = existing_query.where(
            ExceptionRequest.series_id == request_data.media_id
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
            ReclaimCandidate.movie_id == request_data.media_id
        )
    else:
        candidate_query = candidate_query.where(
            ReclaimCandidate.series_id == request_data.media_id
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
        requested_expires_at = datetime.now(timezone.utc) + timedelta(
            days=request_data.duration_days
        )

    # users with auto approve permission are immediately approved and blacklisted
    auto_approve = has_permission(user, Permission.AUTO_APPROVE)
    now = datetime.now(timezone.utc)
    bl_permanent: bool | None = None
    bl_expires_at: datetime | None = None

    # create exception request
    exception_request = ExceptionRequest(
        media_type=request_data.media_type,
        movie_id=request_data.media_id
        if request_data.media_type is MediaType.MOVIE
        else None,
        series_id=request_data.media_id
        if request_data.media_type is MediaType.SERIES
        else None,
        candidate_id=candidate.id if candidate else None,
        requested_by_user_id=user.id,
        reason=request_data.reason,
        requested_expires_at=requested_expires_at,
        status=ExceptionRequestStatus.APPROVED
        if auto_approve
        else ExceptionRequestStatus.PENDING,
        reviewed_by_user_id=user.id if auto_approve else None,
        reviewed_at=now if auto_approve else None,
    )

    db.add(exception_request)

    if auto_approve:
        # determine protection duration (same logic as approve endpoint)
        if requested_expires_at is None:
            bl_permanent = True
            bl_expires_at = None
        else:
            bl_permanent = False
            bl_expires_at = requested_expires_at

        blacklist_entry = MediaBlacklist(
            media_type=request_data.media_type,
            movie_id=request_data.media_id
            if request_data.media_type == MediaType.MOVIE
            else None,
            series_id=request_data.media_id
            if request_data.media_type == MediaType.SERIES
            else None,
            reason=f"Admin exception: {request_data.reason}",
            blacklisted_by_user_id=user.id,
            permanent=bl_permanent,
            expires_at=bl_expires_at,
        )
        db.add(blacklist_entry)

        # remove from candidates if present
        if candidate:
            await db.delete(candidate)

    await db.commit()
    await db.refresh(exception_request)

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
            )
        except Exception as e:
            LOG.error(f"Failed to send notification: {e}")

    # build response
    return ExceptionRequestResponse(
        id=exception_request.id,
        media_type=exception_request.media_type,
        media_id=request_data.media_id,
        media_title=media.title,
        media_year=media.year,
        candidate_id=exception_request.candidate_id,
        requested_by_user_id=user.id,
        requested_by_username=user.username,
        reason=exception_request.reason,
        requested_expires_at=(
            exception_request.requested_expires_at.isoformat()
            if exception_request.requested_expires_at
            else None
        ),
        status=exception_request.status,
        reviewed_by_user_id=user.id if auto_approve else None,
        reviewed_by_username=user.username if auto_approve else None,
        reviewed_at=exception_request.reviewed_at.isoformat()
        if exception_request.reviewed_at
        else None,
        admin_notes=None,
        effective_permanent=bl_permanent if auto_approve else None,
        effective_expires_at=(bl_expires_at.isoformat() if bl_expires_at else None)
        if auto_approve
        else None,
        created_at=exception_request.created_at.isoformat(),
        updated_at=exception_request.updated_at.isoformat(),
    )


@router.get("/requests/my", response_model=list[ExceptionRequestResponse])
async def get_my_requests(
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    status_filter: ExceptionRequestStatus | None = Query(None),
):
    """Get current user's exception requests."""
    query = (
        select(ExceptionRequest)
        .where(ExceptionRequest.requested_by_user_id == user.id)
        .options(
            selectinload(ExceptionRequest.movie),
            selectinload(ExceptionRequest.series),
        )
    )

    if status_filter:
        query = query.where(ExceptionRequest.status == status_filter)

    query = query.order_by(ExceptionRequest.created_at.desc())

    result = await db.execute(query)
    requests = result.scalars().all()

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

        effective_permanent, effective_expires_at = await resolve_effective_protection(
            db, req
        )

        responses.append(
            ExceptionRequestResponse(
                id=req.id,
                media_type=req.media_type,
                media_id=(
                    req.movie_id if req.media_type == MediaType.MOVIE else req.series_id
                )
                or 0,
                media_title=media.title,
                media_year=media.year,
                poster_url=media.poster_url,
                candidate_id=req.candidate_id,
                requested_by_user_id=req.requested_by_user_id,
                requested_by_username=user.username,
                reason=req.reason,
                requested_expires_at=(
                    req.requested_expires_at.isoformat()
                    if req.requested_expires_at
                    else None
                ),
                status=req.status,
                reviewed_by_user_id=req.reviewed_by_user_id,
                reviewed_by_username=reviewed_by_username,
                reviewed_at=req.reviewed_at.isoformat() if req.reviewed_at else None,
                admin_notes=req.admin_notes,
                effective_permanent=effective_permanent,
                effective_expires_at=(
                    effective_expires_at.isoformat() if effective_expires_at else None
                ),
                created_at=req.created_at.isoformat(),
                updated_at=req.updated_at.isoformat(),
            )
        )

    return responses


@router.get("/requests", response_model=list[ExceptionRequestResponse])
async def get_all_requests(
    _manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
    status_filter: ExceptionRequestStatus | None = Query(None),
):
    """Get all exception requests (manage-requests permission)."""
    query = select(ExceptionRequest).options(
        selectinload(ExceptionRequest.movie),
        selectinload(ExceptionRequest.series),
        selectinload(ExceptionRequest.requested_by),
        selectinload(ExceptionRequest.reviewed_by),
    )

    if status_filter:
        query = query.where(ExceptionRequest.status == status_filter)

    query = query.order_by(ExceptionRequest.created_at.desc())

    result = await db.execute(query)
    requests = result.scalars().all()

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
        effective_permanent, effective_expires_at = await resolve_effective_protection(
            db, req
        )

        responses.append(
            ExceptionRequestResponse(
                id=req.id,
                media_type=req.media_type,
                media_id=(
                    req.movie_id if req.media_type is MediaType.MOVIE else req.series_id
                )
                or 0,
                media_title=media.title,
                media_year=media.year,
                poster_url=media.poster_url,
                candidate_id=req.candidate_id,
                requested_by_user_id=req.requested_by_user_id,
                requested_by_username=req.requested_by.username,
                reason=req.reason,
                requested_expires_at=(
                    req.requested_expires_at.isoformat()
                    if req.requested_expires_at
                    else None
                ),
                status=req.status,
                reviewed_by_user_id=req.reviewed_by_user_id,
                reviewed_by_username=reviewed_by_username,
                reviewed_at=req.reviewed_at.isoformat() if req.reviewed_at else None,
                admin_notes=req.admin_notes,
                effective_permanent=effective_permanent,
                effective_expires_at=(
                    effective_expires_at.isoformat() if effective_expires_at else None
                ),
                created_at=req.created_at.isoformat(),
                updated_at=req.updated_at.isoformat(),
            )
        )

    return responses


@router.post("/requests/{request_id}/approve", response_model=ExceptionRequestResponse)
async def approve_request(
    request_id: int,
    review_data: ReviewExceptionRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
):
    """
    Approve an exception request.

    This will:
    1. Mark the request as approved
    2. Add the media to the blacklist
    3. Remove it from candidates if present
    4. Notify the requester
    """
    # get request
    result = await db.execute(
        select(ExceptionRequest).where(ExceptionRequest.id == request_id)
    )
    request = result.scalar_one_or_none()

    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )

    if request.status != ExceptionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request has already been {request.status.value}",
        )

    # get media
    if request.media_type is MediaType.MOVIE:
        media_result = await db.execute(
            select(Movie).where(Movie.id == request.movie_id)
        )
        media = media_result.scalar_one()
        media_id = request.movie_id
    else:
        media_result = await db.execute(
            select(Series).where(Series.id == request.series_id)
        )
        media = media_result.scalar_one()
        media_id = request.series_id

    # update request status
    request.status = ExceptionRequestStatus.APPROVED
    request.reviewed_by_user_id = manager.id
    request.reviewed_at = datetime.now(timezone.utc)
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
        approved_expires_at = datetime.now(timezone.utc) + timedelta(
            days=review_data.approved_duration_days
        )
    elif request.requested_expires_at:
        approved_permanent = False
        approved_expires_at = request.requested_expires_at
    else:
        approved_permanent = True
        approved_expires_at = None

    # add to blacklist
    blacklist_entry = MediaBlacklist(
        media_type=request.media_type,
        movie_id=request.movie_id,
        series_id=request.series_id,
        reason=f"Exception request approved: {request.reason}",
        blacklisted_by_user_id=manager.id,
        permanent=approved_permanent,
        expires_at=approved_expires_at,
    )
    db.add(blacklist_entry)

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
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    # build response
    reviewed_at_value = request.reviewed_at
    reviewed_at_str = (
        reviewed_at_value.isoformat()
        if isinstance(reviewed_at_value, datetime)
        else None
    )
    return ExceptionRequestResponse(
        id=request.id,
        media_type=request.media_type,
        media_id=media_id or 0,
        media_title=media.title,
        media_year=media.year,
        candidate_id=request.candidate_id,
        requested_by_user_id=request.requested_by_user_id,
        requested_by_username="N/A",  # would need another query (this is unused so we can return any str)
        reason=request.reason,
        requested_expires_at=(
            request.requested_expires_at.isoformat()
            if request.requested_expires_at
            else None
        ),
        status=request.status,
        reviewed_by_user_id=manager.id,
        reviewed_by_username=manager.username,
        reviewed_at=reviewed_at_str,
        admin_notes=request.admin_notes,
        effective_permanent=approved_permanent,
        effective_expires_at=(
            approved_expires_at.isoformat() if approved_expires_at else None
        ),
        created_at=request.created_at.isoformat(),
        updated_at=request.updated_at.isoformat(),
    )


@router.post("/requests/{request_id}/deny", response_model=ExceptionRequestResponse)
async def deny_request(
    request_id: int,
    review_data: ReviewExceptionRequest,
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
        select(ExceptionRequest).where(ExceptionRequest.id == request_id)
    )
    request = result.scalar_one_or_none()

    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )

    if request.status != ExceptionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Request has already been {request.status.value}",
        )

    # get media
    if request.media_type is MediaType.MOVIE:
        media_result = await db.execute(
            select(Movie).where(Movie.id == request.movie_id)
        )
        media = media_result.scalar_one()
        media_id = request.movie_id
    else:
        media_result = await db.execute(
            select(Series).where(Series.id == request.series_id)
        )
        media = media_result.scalar_one()
        media_id = request.series_id

    # update request status
    request.status = ExceptionRequestStatus.DENIED
    request.reviewed_by_user_id = manager.id
    request.reviewed_at = datetime.now(timezone.utc)
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
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    # build response
    reviewed_at_value = request.reviewed_at
    reviewed_at_str = (
        reviewed_at_value.isoformat()
        if isinstance(reviewed_at_value, datetime)
        else None
    )
    return ExceptionRequestResponse(
        id=request.id,
        media_type=request.media_type,
        media_id=media_id or 0,
        media_title=media.title,
        media_year=media.year,
        candidate_id=request.candidate_id,
        requested_by_user_id=request.requested_by_user_id,
        requested_by_username="N/A",  # would need another query (this is unused so we can return any str)
        reason=request.reason,
        requested_expires_at=(
            request.requested_expires_at.isoformat()
            if request.requested_expires_at
            else None
        ),
        status=request.status,
        reviewed_by_user_id=manager.id,
        reviewed_by_username=manager.username,
        reviewed_at=reviewed_at_str,
        admin_notes=request.admin_notes,
        effective_permanent=None,
        effective_expires_at=None,
        created_at=request.created_at.isoformat(),
        updated_at=request.updated_at.isoformat(),
    )


@router.delete("/requests/{request_id}")
async def cancel_request(
    request_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Cancel own pending exception request."""
    # get request
    result = await db.execute(
        select(ExceptionRequest).where(
            ExceptionRequest.id == request_id,
            ExceptionRequest.requested_by_user_id == user.id,
        )
    )
    request = result.scalar_one_or_none()

    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )

    if request.status != ExceptionRequestStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only cancel pending requests",
        )

    await db.delete(request)
    await db.commit()

    LOG.info(f"User {user.username} cancelled exception request {request_id}")

    return {"message": "Request cancelled"}
