from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routes.v1.dependencies import ensure_no_active_file_operation
from backend.core.auth import get_current_user, require_page_access
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database import get_db
from backend.database.models import (
    ReclaimCandidate,
    ScheduledDeletion,
    User,
)
from backend.enums import PageAccess
from backend.models.scheduled_deletions import (
    RescheduleDeletionRequest,
    ScheduleCandidatesRequest,
    ScheduledDeletionResponse,
    ScheduleMediaRequest,
)
from backend.services.deletion_actor import DeletionActor
from backend.services.schedule_workflow import (
    cancel_schedule,
    create_media_schedule,
    create_schedules,
    reschedule_schedule,
)
from backend.services.scheduled_deletions import (
    ACTIVE_SCHEDULE_STATES,
    future_time,
    require_delete_permission,
    schedule_response,
)

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
        return await create_schedules(
            db, DeletionActor.from_user(user), list(candidates), when
        )


@router.post("/schedule-deletion", response_model=list[ScheduledDeletionResponse])
async def schedule_media_deletion(
    request: ScheduleMediaRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> list[ScheduledDeletionResponse]:
    """Schedule a deletion straight from the Movies/Series screens."""
    require_delete_permission(user)
    return [await create_media_schedule(db, DeletionActor.from_user(user), request)]


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
    return await reschedule_schedule(db, schedule_id, request, user)


@router.post("/scheduled-deletions/{schedule_id}/cancel")
async def cancel_scheduled_deletion(
    schedule_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    return await cancel_schedule(db, schedule_id, user)
