from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import ApiPrincipal
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database.models import (
    BackgroundJob,
    DeleteRequest,
    ReclaimCandidate,
    ScheduledDeletion,
    User,
)
from backend.enums import BackgroundJobStatus, BackgroundJobType, MediaType
from backend.models.scheduled_deletions import (
    RescheduleDeletionRequest,
    ScheduledDeletionResponse,
    ScheduleMediaRequest,
)
from backend.services.candidate_lifecycle import candidate_deletion_blockers
from backend.services.deletion_actor import DeletionActor
from backend.services.media_server_items import MediaItemTarget, load_media_server_items
from backend.services.scheduled_deletions import (
    ACTIVE_SCHEDULE_STATES,
    active_schedules,
    add_schedule_job,
    blocking_schedule,
    future_time,
    require_schedule_permission,
    resolve_target,
    schedule_inventory_overlap,
    schedule_response,
    schedule_responses,
    snapshot_target,
    target_inventory,
)
from backend.tasks.cleanup import request_leaving_soon_sync


async def create_media_schedule(
    db: AsyncSession, actor: DeletionActor, request: ScheduleMediaRequest
) -> ScheduledDeletionResponse:
    when = future_time(request.scheduled_at)
    is_movie = request.media_type is MediaType.MOVIE
    if is_movie and (request.season_id or request.episode_id):
        raise HTTPException(422, "Movies have no seasons or episodes")
    if not is_movie and request.movie_version_id:
        raise HTTPException(422, "Series have no movie versions")
    if request.episode_id and not request.season_id:
        raise HTTPException(422, "An episode needs its season")
    # Never added to the session: it only describes the scope to schedule.
    stand_in = ReclaimCandidate(
        media_type=request.media_type,
        movie_id=request.media_id if is_movie else None,
        series_id=None if is_movie else request.media_id,
        movie_version_id=request.movie_version_id,
        season_id=request.season_id,
        episode_id=request.episode_id,
        matched_rule_ids=[],
        matched_criteria={},
        reason="Manually scheduled deletion",
    )
    async with candidate_workflow_lock:
        schedules = await create_schedules(db, actor, [stand_in], when)
        return schedules[0]


async def _attach_media_server_items(
    db: AsyncSession, targets: list[dict[str, Any]]
) -> None:
    item_targets = [MediaItemTarget.from_schedule(target) for target in targets]
    media_server_items = await load_media_server_items(db, item_targets)
    for target, item_target in zip(targets, item_targets, strict=True):
        if items := media_server_items[item_target]:
            target["media_server_items"] = [
                item.model_dump(mode="json") for item in items
            ]


async def _refreshed_request_target(
    db: AsyncSession, schedule: ScheduledDeletion
) -> dict[str, Any]:
    """Re-snapshot an approved request's unchanged scope (#529).

    The request's scope is the approved intent. Files added or replaced during
    the grace period, or changed routing, would otherwise block it forever:
    canceling is final for a request, and retry defers to its schedule.
    """
    candidate = await resolve_target(
        db, schedule.target, approved_request=True, verify_snapshot=False
    )
    others = [s for s in await active_schedules(db) if s.id != schedule.id]
    if await blocking_schedule(db, candidate, others):
        raise HTTPException(409, "The current files overlap another scheduled deletion")
    # Size the current files, not the files from the original snapshot.
    candidate.estimated_space_bytes = None
    target = await snapshot_target(db, candidate)
    await _attach_media_server_items(db, [target])
    return target


async def create_schedules(
    db: AsyncSession,
    actor: DeletionActor,
    candidates: list[ReclaimCandidate],
    when: datetime,
    *,
    delete_request: DeleteRequest | None = None,
) -> list[ScheduledDeletionResponse]:
    """Validate and save schedules. Call with candidate_workflow_lock held.

    Candidates may be unsaved stand-ins built from a media target; only their
    scope fields are read.
    """
    targets = []
    for candidate in candidates:
        if candidate.delete_request_id is not None and (
            delete_request is None or candidate.delete_request_id != delete_request.id
        ):
            raise HTTPException(
                409, "Approved deletion requests cannot be scheduled here"
            )
        if await candidate_deletion_blockers(db, candidate):
            raise HTTPException(
                409, "A selected item is protected or has a pending request"
            )
        if await blocking_schedule(db, candidate):
            raise HTTPException(
                409, "A selected item already overlaps a scheduled deletion"
            )
        target = await snapshot_target(db, candidate)
        if any(
            schedule_inventory_overlap(target["inventory"], previous)
            for previous in targets
        ):
            raise HTTPException(
                409,
                "Selected scopes overlap; choose either a parent item or its children",
            )
        targets.append(target)
    # Reject already queued parent/child operations, not just identical IDs.
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
    queued_ids = {
        cid for job in jobs for cid in (job.payload or {}).get("candidate_ids", [])
    }
    for candidate in (
        await db.scalars(
            select(ReclaimCandidate).where(ReclaimCandidate.id.in_(queued_ids))
        )
    ).all():
        # Inventory only: an unrelated queued item with missing media or
        # files must not prevent scheduling.
        queued_inventory = await target_inventory(db, candidate)
        if not queued_inventory["files"] and not queued_inventory["directories"]:
            continue
        if any(schedule_inventory_overlap(queued_inventory, t) for t in targets):
            raise HTTPException(
                409, "An overlapping delete or move operation is already queued"
            )
    await _attach_media_server_items(db, targets)
    schedules = []
    for target in targets:
        schedule = ScheduledDeletion(
            scheduled_at=when,
            target=target,
            created_by_user_id=actor.user_id,
            created_by_username=actor.label,
            created_by_api_token_id=actor.token_id,
            delete_request_id=delete_request.id if delete_request else None,
        )
        db.add(schedule)
        await db.flush()
        await add_schedule_job(db, schedule)
        schedules.append(schedule)
    # Intent and execution job are committed together.
    await db.commit()
    request_leaving_soon_sync()
    return await schedule_responses(db, schedules)


async def _editable_schedule(
    db: AsyncSession, schedule_id: int, actor: User | ApiPrincipal
) -> ScheduledDeletion:
    schedule = await db.get(ScheduledDeletion, schedule_id)
    if schedule is None:
        raise HTTPException(404, "Scheduled deletion not found")
    await require_schedule_permission(db, actor, schedule)
    if schedule.state not in ACTIVE_SCHEDULE_STATES:
        raise HTTPException(
            409, "This scheduled deletion has already finished or been canceled"
        )
    if schedule.job_id:
        job = await db.get(BackgroundJob, schedule.job_id)
        if job is not None and job.status is BackgroundJobStatus.RUNNING:
            raise HTTPException(
                409, "This deletion has started and can no longer be changed"
            )
        if job is not None and job.status is BackgroundJobStatus.PENDING:
            canceled = await db.scalar(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == job.id,
                    BackgroundJob.status == BackgroundJobStatus.PENDING,
                )
                .values(
                    status=BackgroundJobStatus.CANCELED,
                    completed_at=datetime.now(UTC),
                    error_message="Scheduled deletion changed",
                )
                .returning(BackgroundJob.id)
            )
            if canceled is None:
                raise HTTPException(
                    409, "This deletion has started and can no longer be changed"
                )
    # A process can stop between committing its temporary execution candidate
    # and removing it. Never release that candidate into ordinary cleanup.
    await db.execute(
        delete(ReclaimCandidate).where(
            ReclaimCandidate.matched_criteria["scheduled_deletion_id"].as_integer()
            == schedule.id
        )
    )
    return schedule


async def reschedule_schedule(
    db: AsyncSession,
    schedule_id: int,
    request: RescheduleDeletionRequest,
    authorized_actor: User | ApiPrincipal,
    *,
    actor: DeletionActor | None = None,
) -> ScheduledDeletionResponse:
    if actor is None:
        actor = (
            DeletionActor.from_user(authorized_actor)
            if isinstance(authorized_actor, User)
            else DeletionActor.from_token(authorized_actor)
        )
    when = future_time(request.scheduled_at)
    async with candidate_workflow_lock:
        schedule = await _editable_schedule(db, schedule_id, authorized_actor)
        if schedule.delete_request_id is None:
            await resolve_target(db, schedule.target)
        else:
            schedule.target = await _refreshed_request_target(db, schedule)
        schedule.scheduled_at = when
        schedule.created_by_user_id = actor.user_id
        schedule.created_by_username = actor.label
        schedule.created_by_api_token_id = actor.token_id
        schedule.state, schedule.last_error = "scheduled", None
        if schedule.delete_request_id is not None:
            deletion_request = await db.get(DeleteRequest, schedule.delete_request_id)
            if deletion_request is not None:
                deletion_request.execution_error = None
                deletion_request.playback_deferral = None
        await add_schedule_job(db, schedule)
        await db.commit()
        request_leaving_soon_sync()
        return await schedule_response(db, schedule)


async def cancel_schedule(
    db: AsyncSession, schedule_id: int, actor: User | ApiPrincipal
) -> dict[str, str]:
    async with candidate_workflow_lock:
        schedule = await _editable_schedule(db, schedule_id, actor)
        schedule.state = "canceled"
        if schedule.delete_request_id is not None:
            deletion_request = await db.get(DeleteRequest, schedule.delete_request_id)
            if deletion_request is not None:
                deletion_request.execution_error = None
                deletion_request.playback_deferral = None
        schedule.completed_at = datetime.now(UTC)
        await db.commit()
        request_leaving_soon_sync()
    return {"message": "Scheduled deletion canceled. Normal cleanup rules apply again."}
