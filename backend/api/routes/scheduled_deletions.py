from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routes.v1.dependencies import ensure_no_active_file_operation
from backend.core.auth import get_current_user, require_page_access
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database import get_db
from backend.database.models import (
    BackgroundJob,
    ReclaimCandidate,
    ScheduledDeletion,
    User,
)
from backend.enums import BackgroundJobStatus, BackgroundJobType, PageAccess
from backend.models.scheduled_deletions import (
    RescheduleDeletionRequest,
    ScheduleCandidatesRequest,
    ScheduledDeletionResponse,
)
from backend.services.candidate_lifecycle import candidate_deletion_blockers
from backend.services.scheduled_deletions import (
    ACTIVE_SCHEDULE_STATES,
    add_schedule_job,
    blocking_schedule,
    future_time,
    require_delete_permission,
    resolve_target,
    schedule_inventory_overlap,
    schedule_response,
    snapshot_target,
    target_inventory,
)
from backend.tasks.cleanup import request_leaving_soon_sync

router = APIRouter(prefix="/api/media", tags=["scheduled-deletions"])


@router.get("/scheduled-deletions", response_model=list[ScheduledDeletionResponse])
async def list_scheduled_deletions(
    _user: Annotated[User, Depends(require_page_access(PageAccess.CANDIDATES))],
    db: AsyncSession = Depends(get_db),
) -> list[ScheduledDeletionResponse]:
    schedules = (
        await db.scalars(
            select(ScheduledDeletion)
            .where(ScheduledDeletion.state.in_(ACTIVE_SCHEDULE_STATES))
            .order_by(ScheduledDeletion.scheduled_at, ScheduledDeletion.id)
        )
    ).all()
    return [await schedule_response(db, schedule) for schedule in schedules]


@router.post(
    "/candidates/schedule-deletion", response_model=list[ScheduledDeletionResponse]
)
async def schedule_candidate_deletions(
    request: ScheduleCandidatesRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[ScheduledDeletionResponse]:
    require_delete_permission(user)
    when = future_time(request.scheduled_at)
    async with candidate_workflow_lock:
        ids = list(dict.fromkeys(request.candidate_ids))
        await ensure_no_active_file_operation(db, ids)
        candidates = (
            await db.scalars(
                select(ReclaimCandidate).where(ReclaimCandidate.id.in_(ids))
            )
        ).all()
        if len(candidates) != len(ids):
            raise HTTPException(
                409,
                "Some selected candidates are no longer available; refresh the list",
            )
        targets = []
        for candidate in candidates:
            if candidate.delete_request_id is not None:
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
        schedules = []
        for target in targets:
            schedule = ScheduledDeletion(
                scheduled_at=when,
                target=target,
                created_by_user_id=user.id,
                created_by_username=user.username,
            )
            db.add(schedule)
            await db.flush()
            await add_schedule_job(db, schedule)
            schedules.append(schedule)
        # Intent and execution job are committed together.
        await db.commit()
        request_leaving_soon_sync()
        return [await schedule_response(db, schedule) for schedule in schedules]


async def _editable_schedule(db: AsyncSession, schedule_id: int) -> ScheduledDeletion:
    schedule = await db.get(ScheduledDeletion, schedule_id)
    if schedule is None:
        raise HTTPException(404, "Scheduled deletion not found")
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
                    error_message="Scheduled deletion changed by a user",
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


@router.post(
    "/scheduled-deletions/{schedule_id}/reschedule",
    response_model=ScheduledDeletionResponse,
)
async def reschedule_deletion(
    schedule_id: int,
    request: RescheduleDeletionRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> ScheduledDeletionResponse:
    require_delete_permission(user)
    when = future_time(request.scheduled_at)
    async with candidate_workflow_lock:
        schedule = await _editable_schedule(db, schedule_id)
        await resolve_target(db, schedule.target)
        schedule.scheduled_at = when
        schedule.created_by_user_id = user.id
        schedule.created_by_username = user.username
        schedule.state, schedule.last_error = "scheduled", None
        await add_schedule_job(db, schedule)
        await db.commit()
        request_leaving_soon_sync()
        return await schedule_response(db, schedule)


@router.post("/scheduled-deletions/{schedule_id}/cancel")
async def cancel_scheduled_deletion(
    schedule_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    require_delete_permission(user)
    async with candidate_workflow_lock:
        schedule = await _editable_schedule(db, schedule_id)
        schedule.state = "canceled"
        schedule.completed_at = datetime.now(UTC)
        await db.commit()
        request_leaving_soon_sync()
    return {"message": "Scheduled deletion canceled. Normal cleanup rules apply again."}
