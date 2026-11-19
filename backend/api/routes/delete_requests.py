from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.auth import (
    get_current_user,
    has_permission,
    require_page_access,
    require_permission,
)
from backend.core.logger import LOG
from backend.core.protection_scope import (
    active_protection_clause,
    movie_scope_overlap_clause,
    series_scope_overlap_clause,
)
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.core.utils.resolution import guesstimate_resolution
from backend.database import get_db
from backend.database.models import (
    DeleteRequest,
    Episode,
    Movie,
    MovieVersion,
    ProtectedMedia,
    Season,
    Series,
    User,
)
from backend.enums import (
    MediaType,
    NotificationType,
    PageAccess,
    Permission,
    ProtectionRequestStatus,
)
from backend.models.requests import (
    ApproveDeleteRequest,
    CreateDeleteRequest,
    DeleteRequestResponse,
    ReviewDeleteRequest,
)
from backend.services.delete_request_workflow import (
    approval_options,
    approve_request,
    build_delete_request_response,
    deny_request,
    list_requests,
    request_target_scope,
    retry_request,
)
from backend.services.deletion_actor import DeletionActor
from backend.services.media_user_identity import linked_media_identities
from backend.services.notifications import (
    notify_admins,
    request_scope_label,
)

router = APIRouter(prefix="/api", tags=["delete-requests"])


@router.post(
    "/delete-requests/{request_id}/retry", response_model=DeleteRequestResponse
)
async def retry_delete_request(
    request_id: int,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
) -> DeleteRequestResponse:
    return await retry_request(db, request_id, DeletionActor.from_user(manager))


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

    protected_query = select(ProtectedMedia).where(
        active_protection_clause(datetime.now(UTC))
    )
    if request_data.media_type is MediaType.MOVIE:
        if request_data.movie_version_id is not None:
            protected_query = protected_query.where(
                ProtectedMedia.movie_id == request_data.media_id,
                movie_scope_overlap_clause(
                    ProtectedMedia, movie_version_id=request_data.movie_version_id
                ),
            )
        else:
            # deleting the whole movie is blocked by a protection on any one of its
            # versions, which is wider than the protection-side overlap rule
            protected_query = protected_query.where(
                ProtectedMedia.movie_id == request_data.media_id
            )
    else:
        if season is not None or episode is not None:
            protected_query = protected_query.where(
                ProtectedMedia.series_id == request_data.media_id,
                series_scope_overlap_clause(
                    ProtectedMedia,
                    season_id=season.id if season else None,
                    episode_id=episode.id if episode else None,
                ),
            )
        else:
            protected_query = protected_query.where(
                ProtectedMedia.series_id == request_data.media_id,
                series_scope_overlap_clause(
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
            series_scope_overlap_clause(
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

    target_scope = request_target_scope(
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
        await notify_admins(
            notification_type=NotificationType.ADMIN_NEW_DELETE_REQUEST,
            title="New Delete Request",
            message=f"{user.username} requested deletion for {media.title}",
            context={
                "actor": user.username,
                "media_title": media.title,
                "media_year": media.year,
                "media_type": request_data.media_type.value,
                "reason": request_data.reason,
                "request_id": delete_request.id,
                "request_type": "Deletion",
                "scope": request_scope_label(
                    delete_request.target_scope,
                    delete_request.season_number_snapshot,
                    delete_request.episode_number_snapshot,
                    delete_request.episode_name_snapshot,
                ),
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
        requested_by_media_identities=(await linked_media_identities(db, [user.id]))[
            user.id
        ],
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
    user: Annotated[User, Depends(require_page_access(PageAccess.REQUESTS))],
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

    identities = await linked_media_identities(
        db, (req.requested_by_user_id for req in requests)
    )

    version_ids = [r.movie_version_id for r in requests if r.movie_version_id]
    versions_by_id: dict[int, MovieVersion] = {}
    if version_ids:
        vr = await db.execute(
            select(MovieVersion).where(MovieVersion.id.in_(version_ids))
        )
        for v in vr.scalars().all():
            versions_by_id[v.id] = v

    return [
        await build_delete_request_response(
            db,
            req,
            requested_by_username=user.username,
            requested_by_media_identities=identities[req.requested_by_user_id],
            version=versions_by_id.get(req.movie_version_id)
            if req.movie_version_id
            else None,
        )
        for req in requests
    ]


@router.get("/delete-requests", response_model=list[DeleteRequestResponse])
async def get_all_delete_requests(
    _manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    _page_user: Annotated[User, Depends(require_page_access(PageAccess.REQUESTS))],
    db: AsyncSession = Depends(get_db),
    status_filter: ProtectionRequestStatus | None = Query(None),
) -> list[DeleteRequestResponse]:
    """Get all delete requests, optionally filtered by status. Manager permission required."""
    items, _ = await list_requests(db, status_filter)
    return items


@router.get("/delete-requests/approval-options")
async def delete_request_approval_options(
    _manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    return await approval_options(db)


@router.post(
    "/delete-requests/{request_id}/approve", response_model=DeleteRequestResponse
)
async def approve_delete_request(
    request_id: int,
    review_data: ApproveDeleteRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
) -> DeleteRequestResponse:
    """Approve a deletion for immediate or scheduled background execution."""
    return await approve_request(
        db, request_id, review_data, DeletionActor.from_user(manager)
    )


@router.post("/delete-requests/{request_id}/deny", response_model=DeleteRequestResponse)
async def deny_delete_request(
    request_id: int,
    review_data: ReviewDeleteRequest,
    manager: Annotated[User, Depends(require_permission(Permission.MANAGE_REQUESTS))],
    db: AsyncSession = Depends(get_db),
) -> DeleteRequestResponse:
    """Deny a delete request. Manager permission required."""
    return await deny_request(
        db, request_id, review_data, DeletionActor.from_user(manager)
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

    media = (
        await db.get(Movie, request.movie_id)
        if request.movie_id is not None
        else await db.get(Series, request.series_id)
        if request.series_id is not None
        else None
    )
    context = {
        "request_id": request.id,
        "request_type": "Deletion",
        "actor": user.username,
        "media_title": media.title if media else "Unknown media",
        "media_type": request.media_type.value,
        "scope": request_scope_label(
            request.target_scope,
            request.season_number_snapshot,
            request.episode_number_snapshot,
            request.episode_name_snapshot,
        ),
        "reason": request.reason,
    }
    await db.delete(request)
    await db.commit()

    LOG.info(f"User {user.username} cancelled delete request {request_id}")
    try:
        await notify_admins(
            NotificationType.ADMIN_REQUEST_CANCELLED,
            "Delete Request Cancelled",
            f"{user.username} cancelled a deletion request",
            context=context,
        )
    except Exception as e:
        LOG.error(f"Failed to send cancellation notification: {e}")
    return {"message": "Request cancelled"}
