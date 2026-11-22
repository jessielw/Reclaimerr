from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.core.logger import LOG
from backend.core.utils.datetime_utils import to_utc_isoformat
from backend.core.utils.resolution import guesstimate_resolution
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database.models import (
    BackgroundJob,
    DeleteRequest,
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ScheduledDeletion,
    Season,
    Series,
    User,
)
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    CandidateFileOpOperation,
    MediaType,
    NotificationType,
    ProtectionRequestStatus,
)
from backend.jobs.candidate_file_ops import queue_candidate_file_op_job
from backend.models.jobs import CandidateFileOpJobItem
from backend.models.media_identity import LinkedMediaIdentity, MediaServerItem
from backend.models.requests import (
    ApproveDeleteRequest,
    DeleteRequestResponse,
    ReviewDeleteRequest,
)
from backend.services.candidate_lifecycle import (
    candidate_deletion_blockers,
    candidate_scope,
)
from backend.services.deletion_actor import DeletionActor
from backend.services.media_server_items import MediaItemTarget, load_media_server_items
from backend.services.media_user_identity import linked_media_identities
from backend.services.notifications import (
    media_event_context,
    notify_user,
    request_scope_label,
)
from backend.services.schedule_workflow import create_schedules
from backend.services.scheduled_deletions import (
    blocking_schedule,
    future_time,
    schedule_execution_state,
    scheduled_media_server_items,
)


async def retry_request(
    db: AsyncSession, request_id: int, actor: DeletionActor
) -> DeleteRequestResponse:
    """Retry an approved request that did not finish executing."""
    if candidate_workflow_lock.locked():
        raise HTTPException(status_code=409, detail="A cleanup operation is running")
    async with candidate_workflow_lock:
        request = await db.get(DeleteRequest, request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="Request not found")
        if (
            request.status != ProtectionRequestStatus.APPROVED
            or request.executed_at
            or not (request.playback_deferral or request.execution_error)
        ):
            raise HTTPException(
                status_code=409,
                detail="Only failed or deferred approved requests can be retried",
            )
        if await db.scalar(
            select(ScheduledDeletion.id).where(
                ScheduledDeletion.delete_request_id == request_id
            )
        ):
            raise HTTPException(
                409, "Reschedule this deletion instead of retrying it immediately"
            )
        jobs = (
            await db.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.job_type == BackgroundJobType.CANDIDATE_FILE_OP,
                    BackgroundJob.status.in_(
                        [BackgroundJobStatus.PENDING, BackgroundJobStatus.RUNNING]
                    ),
                )
            )
        ).all()
        if any(
            (job.payload or {}).get("delete_request_id") == request_id for job in jobs
        ):
            raise HTTPException(
                status_code=409,
                detail="This request already has a queued or running job",
            )
        candidate = (
            await db.scalars(
                select(ReclaimCandidate).where(
                    ReclaimCandidate.delete_request_id == request_id
                )
            )
        ).first()
        media, _ = await _get_delete_request_media(db, request)

        if candidate is None:
            if not request.execution_error:
                raise HTTPException(409, "The deferred target is no longer available")
            candidate = _approved_request_candidate(request)
            db.add(candidate)
        # Legacy jobs can retain a narrower candidate than their request. Keep
        # that candidate, but never allow a different target or a wider scope.
        elif any(
            getattr(request, name) is not None
            and getattr(candidate, name) != getattr(request, name)
            for name in (
                "media_type",
                "movie_id",
                "movie_version_id",
                "series_id",
                "season_id",
                "episode_id",
            )
        ):
            raise HTTPException(409, "The requested target is no longer available")
        if await candidate_deletion_blockers(db, candidate):
            raise HTTPException(
                status_code=409,
                detail="The target is protected or has a pending request",
            )
        if await blocking_schedule(db, candidate):
            raise HTTPException(409, "This item already overlaps a scheduled deletion")
        candidate.last_delete_error = None
        await db.flush()
        item = await _build_delete_request_job_item(
            db,
            request,
            media_title=media.title,
            media_year=media.year,
            media_tmdb_id=media.tmdb_id,
            candidate_id=candidate.id,
        )
        await queue_candidate_file_op_job(
            operation=CandidateFileOpOperation.DELETE,
            candidate_ids=[candidate.id],
            requested_by_user_id=actor.user_id,
            requested_by_username=actor.label,
            requested_by_api_token_id=actor.token_id,
            delete_request_id=request.id,
            item_labels=[item.display_label],
            item_label_total=1,
            item_details=[
                item.model_copy(update={"scope": candidate_scope(candidate)})
            ],
            db=db,
        )
        request.playback_deferral = None
        request.execution_error = None
        await db.commit()
        await db.refresh(request)
        return await build_delete_request_response(db, request)


async def _get_delete_request_media(
    db: AsyncSession,
    request: DeleteRequest,
    *,
    require_available_target: bool = True,
) -> tuple[Movie | Series, int]:
    """Get the media (movie or series) associated with a delete request."""
    if request.media_type is MediaType.MOVIE:
        if require_available_target and (
            request.target_scope == "movie_version"
            or request.movie_version_id is not None
        ):
            version = (
                await db.get(MovieVersion, request.movie_version_id)
                if request.movie_version_id is not None
                else None
            )
            if version is None or version.movie_id != request.movie_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The requested movie version is no longer available; create a new request",
                )
        # get() reuses rows already eager-loaded by list queries
        movie = (
            await db.get(Movie, request.movie_id)
            if request.movie_id is not None
            else None
        )
        if movie is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Movie not found"
            )
        return movie, request.movie_id or 0

    series = (
        await db.get(Series, request.series_id)
        if request.series_id is not None
        else None
    )
    if series is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Series not found"
        )
    if require_available_target:
        scope = request_target_scope(
            media_type=request.media_type,
            movie_version_id=None,
            season_id=request.season_id,
            episode_id=request.episode_id,
            target_scope=request.target_scope,
            season_number_snapshot=request.season_number_snapshot,
            episode_number_snapshot=request.episode_number_snapshot,
        )
        if scope in ("season", "episode"):
            season = (
                await db.get(Season, request.season_id) if request.season_id else None
            )
            episode = (
                await db.get(Episode, request.episode_id)
                if request.episode_id
                else None
            )
            if (
                season is None
                or season.series_id != series.id
                or (
                    scope == "episode"
                    and (episode is None or episode.season_id != season.id)
                )
            ):
                raise HTTPException(
                    409, "The requested TV scope is no longer available"
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


def request_target_scope(
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


def _request_media_item_target(request: DeleteRequest) -> MediaItemTarget | None:
    media_id = (
        request.movie_id if request.media_type is MediaType.MOVIE else request.series_id
    )
    if media_id is None:
        return None
    return MediaItemTarget(
        request.media_type,
        media_id,
        request_target_scope(
            media_type=request.media_type,
            movie_version_id=request.movie_version_id,
            season_id=request.season_id,
            episode_id=request.episode_id,
            target_scope=request.target_scope,
            season_number_snapshot=request.season_number_snapshot,
            episode_number_snapshot=request.episode_number_snapshot,
        ),
        movie_version_id=request.movie_version_id,
        season_id=request.season_id,
        episode_id=request.episode_id,
    )


async def load_delete_request_item_refs(
    db: AsyncSession, requests: Iterable[DeleteRequest]
) -> dict[int, list[MediaServerItem]]:
    targets = {request.id: _request_media_item_target(request) for request in requests}
    refs = await load_media_server_items(
        db, (target for target in targets.values() if target is not None)
    )
    return {
        request_id: refs[target] if target is not None else []
        for request_id, target in targets.items()
    }


async def load_request_schedules(
    db: AsyncSession, requests: Sequence[DeleteRequest]
) -> dict[int, ScheduledDeletion]:
    """Batch-load request schedules and their jobs for list responses."""
    if not requests:
        return {}
    schedules = (
        await db.scalars(
            select(ScheduledDeletion).where(
                ScheduledDeletion.delete_request_id.in_([r.id for r in requests])
            )
        )
    ).all()
    job_ids = [s.job_id for s in schedules if s.job_id]
    if job_ids:
        # Loads the jobs into the session so schedule_execution_state's get() is free.
        (
            await db.scalars(select(BackgroundJob).where(BackgroundJob.id.in_(job_ids)))
        ).all()
    return {
        s.delete_request_id: s for s in schedules if s.delete_request_id is not None
    }


async def build_delete_request_response(
    db: AsyncSession,
    request: DeleteRequest,
    requested_by_username: str | None = None,
    reviewed_by_username: str | None = None,
    version: MovieVersion | None = None,
    requested_by_media_identities: list[LinkedMediaIdentity] | None = None,
    media_server_items: list[MediaServerItem] | None = None,
    schedules_by_request_id: Mapping[int, ScheduledDeletion] | None = None,
) -> DeleteRequestResponse:
    """Build a DeleteRequestResponse from a DeleteRequest, looking up related media and user info as needed."""
    # Reading/reviewing a stale request must remain possible even after its
    # version disappears. Approval and retry still require the original target.
    media, media_id = await _get_delete_request_media(
        db, request, require_available_target=False
    )
    if version is None and request.movie_version_id is not None:
        version = await db.get(MovieVersion, request.movie_version_id)
    if version is not None and version.movie_id != request.movie_id:
        version = None
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
    target_scope = request_target_scope(
        media_type=request.media_type,
        movie_version_id=request.movie_version_id,
        season_id=request.season_id,
        episode_id=request.episode_id,
        target_scope=request.target_scope,
        season_number_snapshot=request.season_number_snapshot,
        episode_number_snapshot=request.episode_number_snapshot,
    )
    schedule = (
        schedules_by_request_id.get(request.id)
        if schedules_by_request_id is not None
        else await db.scalar(
            select(ScheduledDeletion).where(
                ScheduledDeletion.delete_request_id == request.id
            )
        )
    )
    schedule_state, schedule_error = (
        await schedule_execution_state(db, schedule)
        if schedule is not None
        else (None, None)
    )
    if schedule is not None and "media_server_items" in schedule.target:
        media_server_items = (await scheduled_media_server_items(db, [schedule]))[
            schedule.id
        ]
    if media_server_items is None:
        media_server_items = (await load_delete_request_item_refs(db, [request]))[
            request.id
        ]
    if requested_by_media_identities is None:
        identities = await linked_media_identities(db, [request.requested_by_user_id])
        requested_by_media_identities = identities[request.requested_by_user_id]
    return DeleteRequestResponse(
        id=request.id,
        media_type=request.media_type,
        media_id=media_id,
        media_server_items=media_server_items,
        media_title=media.title,
        media_year=media.year,
        target_scope=target_scope,
        target_available=target_scope != "movie_version" or version is not None,
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
        requested_by_media_identities=requested_by_media_identities,
        reason=request.reason,
        status=request.status,
        reviewed_by_user_id=request.reviewed_by_user_id,
        reviewed_by_username=reviewed_by_username,
        reviewed_by_api_token_id=request.reviewed_by_api_token_id,
        reviewed_by_api_token_name=request.reviewed_by_api_token_name,
        reviewed_at=to_utc_isoformat(request.reviewed_at),
        admin_notes=request.admin_notes,
        executed_at=to_utc_isoformat(request.executed_at),
        scheduled_deletion_id=schedule.id if schedule else None,
        scheduled_at=to_utc_isoformat(schedule.scheduled_at) if schedule else None,
        schedule_state=schedule_state,
        execution_error=request.execution_error
        or (schedule_error if not request.playback_deferral else None),
        playback_deferral=request.playback_deferral,
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


async def list_requests(
    db: AsyncSession,
    status_filter: ProtectionRequestStatus | None,
    *,
    offset: int = 0,
    limit: int | None = None,
) -> tuple[list[DeleteRequestResponse], int]:
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
        .order_by(DeleteRequest.created_at.desc(), DeleteRequest.id.desc())
    )
    if status_filter:
        query = query.where(DeleteRequest.status == status_filter)

    total = None
    if limit is not None:
        count = select(func.count()).select_from(DeleteRequest)
        if status_filter:
            count = count.where(DeleteRequest.status == status_filter)
        total = await db.scalar(count)
        query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    requests = result.scalars().all()

    identities = await linked_media_identities(
        db, (req.requested_by_user_id for req in requests)
    )
    item_refs = await load_delete_request_item_refs(db, requests)
    schedules = await load_request_schedules(db, requests)

    version_ids = [r.movie_version_id for r in requests if r.movie_version_id]
    versions_by_id: dict[int, MovieVersion] = {}
    if version_ids:
        vr = await db.execute(
            select(MovieVersion).where(MovieVersion.id.in_(version_ids))
        )
        for v in vr.scalars().all():
            versions_by_id[v.id] = v

    items = [
        await build_delete_request_response(
            db,
            req,
            requested_by_media_identities=identities[req.requested_by_user_id],
            media_server_items=item_refs[req.id],
            schedules_by_request_id=schedules,
            version=versions_by_id.get(req.movie_version_id)
            if req.movie_version_id
            else None,
        )
        for req in requests
    ]
    return items, total if total is not None else len(items)


async def get_request(db: AsyncSession, request_id: int) -> DeleteRequestResponse:
    request = await db.get(DeleteRequest, request_id)
    if request is None:
        raise HTTPException(404, "Request not found")
    version = (
        await db.get(MovieVersion, request.movie_version_id)
        if request.movie_version_id
        else None
    )
    return await build_delete_request_response(db, request, version=version)


async def approval_options(db: AsyncSession) -> dict[str, int]:
    settings = await db.scalar(select(GeneralSettings).limit(1))
    return {
        "delete_request_grace_period_days": settings.delete_request_grace_period_days
        if settings
        else 0
    }


def _record_review_actor(request: DeleteRequest, actor: DeletionActor) -> None:
    request.reviewed_by_user_id = actor.user_id
    request.reviewed_by_api_token_id = actor.token_id
    request.reviewed_by_api_token_name = (
        actor.token_name if actor.token_id is not None else None
    )


def _approved_request_candidate(request: DeleteRequest) -> ReclaimCandidate:
    return ReclaimCandidate(
        media_type=request.media_type,
        matched_rule_ids=[],
        matched_criteria={},
        delete_request_id=request.id,
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


async def approve_request(
    db: AsyncSession,
    request_id: int,
    review_data: ApproveDeleteRequest,
    actor: DeletionActor,
) -> DeleteRequestResponse:
    async with candidate_workflow_lock:
        try:
            return await _approve_delete_request_unlocked(
                request_id, review_data, actor, db
            )
        except Exception:
            await db.rollback()
            raise


async def _approve_delete_request_unlocked(
    request_id: int,
    review_data: ApproveDeleteRequest,
    actor: DeletionActor,
    db: AsyncSession,
) -> DeleteRequestResponse:
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

    when = None
    if "scheduled_at" in review_data.model_fields_set:
        if review_data.scheduled_at is not None:
            when = future_time(review_data.scheduled_at)
    else:
        settings = await db.scalar(select(GeneralSettings).limit(1))
        days = settings.delete_request_grace_period_days if settings else 0
        if days:
            when = datetime.now(UTC) + timedelta(days=days)

    request.status = ProtectionRequestStatus.APPROVED
    _record_review_actor(request, actor)
    request.reviewed_at = datetime.now(UTC)
    request.admin_notes = review_data.admin_notes
    request.execution_error = None

    candidate = _approved_request_candidate(request)
    if when is not None:
        # Autoflush is disabled: persist the approval within this transaction so
        # validation no longer sees this request as pending. No candidate is saved.
        await db.flush()
        await create_schedules(db, actor, [candidate], when, delete_request=request)
    else:
        if await blocking_schedule(db, candidate):
            raise HTTPException(409, "This item already overlaps a scheduled deletion")
        db.add(candidate)
        await db.flush()
        item_detail = await _build_delete_request_job_item(
            db,
            request,
            media_title=media.title,
            media_year=media_year,
            media_tmdb_id=media_tmdb_id,
            candidate_id=candidate.id,
        )
        await queue_candidate_file_op_job(
            operation=CandidateFileOpOperation.DELETE,
            candidate_ids=[candidate.id],
            requested_by_user_id=actor.user_id,
            requested_by_username=actor.label,
            requested_by_api_token_id=actor.token_id,
            delete_request_id=request.id,
            item_labels=[item_detail.display_label],
            item_label_total=1,
            item_details=[item_detail],
            db=db,
        )
        await db.commit()

    tracked_request = await db.get(DeleteRequest, request_id)
    if tracked_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Request not found"
        )
    await db.refresh(tracked_request)

    LOG.info(
        f"{actor.label} approved delete request {request_id} "
        f"for {request.media_type.value} '{media.title}'"
    )

    try:
        await notify_user(
            user_id=tracked_request.requested_by_user_id,
            notification_type=NotificationType.REQUEST_APPROVED,
            title="Delete Request Approved",
            message=(
                f"Your delete request for {media.title} was approved and scheduled for {to_utc_isoformat(when)}"
                if when
                else f"Your delete request for {media.title} was approved and queued for execution"
            ),
            context={
                "media_title": media.title,
                "media_year": media.year,
                "media_type": tracked_request.media_type.value,
                "scope": request_scope_label(
                    tracked_request.target_scope,
                    tracked_request.season_number_snapshot,
                    tracked_request.episode_number_snapshot,
                    tracked_request.episode_name_snapshot,
                ),
                "request_id": tracked_request.id,
                "request_type": "Deletion",
                "scheduled_at": to_utc_isoformat(when),
                "reason": tracked_request.reason,
                "admin_notes": tracked_request.admin_notes,
                "requested_at": tracked_request.created_at,
                **media_event_context(media),
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    return await build_delete_request_response(
        db,
        tracked_request,
        reviewed_by_username=actor.name if actor.user_id is not None else None,
        version=await db.get(MovieVersion, tracked_request.movie_version_id)
        if tracked_request.movie_version_id
        else None,
    )


async def deny_request(
    db: AsyncSession,
    request_id: int,
    review_data: ReviewDeleteRequest,
    actor: DeletionActor,
) -> DeleteRequestResponse:
    async with candidate_workflow_lock:
        return await _deny_request_unlocked(db, request_id, review_data, actor)


async def _deny_request_unlocked(
    db: AsyncSession,
    request_id: int,
    review_data: ReviewDeleteRequest,
    actor: DeletionActor,
) -> DeleteRequestResponse:
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

    media, _ = await _get_delete_request_media(
        db, request, require_available_target=False
    )
    request.status = ProtectionRequestStatus.DENIED
    _record_review_actor(request, actor)
    request.reviewed_at = datetime.now(UTC)
    request.admin_notes = review_data.admin_notes

    await db.commit()
    await db.refresh(request)

    LOG.info(
        f"{actor.label} denied delete request {request_id} "
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
                "media_year": media.year,
                "media_type": request.media_type.value,
                "scope": request_scope_label(
                    request.target_scope,
                    request.season_number_snapshot,
                    request.episode_number_snapshot,
                    request.episode_name_snapshot,
                ),
                "request_id": request.id,
                "request_type": "Deletion",
                "reason": request.reason,
                "admin_notes": review_data.admin_notes,
                "requested_at": request.created_at,
                **media_event_context(media),
            },
        )
    except Exception as e:
        LOG.error(f"Failed to send notification: {e}")

    return await build_delete_request_response(
        db,
        request,
        reviewed_by_username=actor.name if actor.user_id is not None else None,
        version=await db.get(MovieVersion, request.movie_version_id)
        if request.movie_version_id
        else None,
    )
