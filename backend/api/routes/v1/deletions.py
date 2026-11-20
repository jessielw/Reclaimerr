from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import (
    API_TOKEN_DELETE_REQUESTS_MANAGE_SCOPE,
    API_TOKEN_DELETE_REQUESTS_READ_SCOPE,
    API_TOKEN_SCHEDULED_DELETIONS_MANAGE_SCOPE,
    API_TOKEN_SCHEDULED_DELETIONS_READ_SCOPE,
    ApiPrincipal,
    get_api_principal,
    require_api_scope,
)
from backend.database import get_db
from backend.database.models import ScheduledDeletion
from backend.enums import ProtectionRequestStatus
from backend.models.api_v1.common import total_pages
from backend.models.api_v1.deletions import (
    ApprovalOptionsResponse,
    CancelScheduleResponse,
    DeleteRequestListResponse,
    ExternalScheduledDeletionResponse,
    ScheduledDeletionListResponse,
    ScheduledDeletionTarget,
)
from backend.models.requests import (
    ApproveDeleteRequest,
    DeleteRequestResponse,
    ReviewDeleteRequest,
)
from backend.models.scheduled_deletions import (
    RescheduleDeletionRequest,
    ScheduledDeletionResponse,
    ScheduleMediaRequest,
)
from backend.services import delete_request_workflow, schedule_workflow
from backend.services.deletion_actor import DeletionActor
from backend.services.scheduled_deletions import (
    ACTIVE_SCHEDULE_STATES,
    schedule_response,
    schedule_responses,
)

router = APIRouter()
request_router = APIRouter(prefix="/delete-requests", tags=["v1:delete-requests"])
schedule_router = APIRouter(
    prefix="/scheduled-deletions", tags=["v1:scheduled-deletions"]
)

Db = Annotated[AsyncSession, Depends(get_db)]
ReadRequests = Annotated[
    ApiPrincipal, Depends(require_api_scope(API_TOKEN_DELETE_REQUESTS_READ_SCOPE))
]
ManageRequests = Annotated[
    ApiPrincipal, Depends(require_api_scope(API_TOKEN_DELETE_REQUESTS_MANAGE_SCOPE))
]
ReadSchedules = Annotated[
    ApiPrincipal, Depends(require_api_scope(API_TOKEN_SCHEDULED_DELETIONS_READ_SCOPE))
]
ManageManualSchedules = Annotated[
    ApiPrincipal, Depends(require_api_scope(API_TOKEN_SCHEDULED_DELETIONS_MANAGE_SCOPE))
]
Page = Annotated[int, Query(ge=1)]
PerPage = Annotated[int, Query(ge=1, le=200)]


@request_router.get("")
async def list_delete_requests(
    _principal: ReadRequests,
    db: Db,
    page: Page = 1,
    per_page: PerPage = 50,
    status_filter: Annotated[
        ProtectionRequestStatus | None, Query(alias="status")
    ] = None,
) -> DeleteRequestListResponse:
    items, total = await delete_request_workflow.list_requests(
        db, status_filter, offset=(page - 1) * per_page, limit=per_page
    )
    return DeleteRequestListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@request_router.get("/approval-options")
async def get_approval_options(
    _principal: ReadRequests, db: Db
) -> ApprovalOptionsResponse:
    return ApprovalOptionsResponse(**await delete_request_workflow.approval_options(db))


@request_router.get("/{request_id}")
async def get_delete_request(
    request_id: int, _principal: ReadRequests, db: Db
) -> DeleteRequestResponse:
    return await delete_request_workflow.get_request(db, request_id)


@request_router.post("/{request_id}/approve")
async def approve_delete_request(
    request_id: int, review: ApproveDeleteRequest, principal: ManageRequests, db: Db
) -> DeleteRequestResponse:
    return await delete_request_workflow.approve_request(
        db, request_id, review, DeletionActor.from_token(principal)
    )


@request_router.post("/{request_id}/deny")
async def deny_delete_request(
    request_id: int, review: ReviewDeleteRequest, principal: ManageRequests, db: Db
) -> DeleteRequestResponse:
    return await delete_request_workflow.deny_request(
        db, request_id, review, DeletionActor.from_token(principal)
    )


@request_router.post("/{request_id}/retry")
async def retry_delete_request(
    request_id: int, principal: ManageRequests, db: Db
) -> DeleteRequestResponse:
    return await delete_request_workflow.retry_request(
        db, request_id, DeletionActor.from_token(principal)
    )


async def _require_schedule_manager(
    principal: Annotated[ApiPrincipal, Depends(get_api_principal)],
) -> ApiPrincipal:
    if not (
        principal.has_scope(API_TOKEN_SCHEDULED_DELETIONS_MANAGE_SCOPE)
        or principal.has_scope(API_TOKEN_DELETE_REQUESTS_MANAGE_SCOPE)
    ):
        raise HTTPException(403, "API token requires a deletion management scope")
    return principal


ManageSchedules = Annotated[ApiPrincipal, Depends(_require_schedule_manager)]


def _schedule_summary(
    schedule: ScheduledDeletionResponse,
) -> ExternalScheduledDeletionResponse:
    return ExternalScheduledDeletionResponse(
        id=schedule.id,
        scheduled_at=schedule.scheduled_at,
        state=schedule.state,
        delete_request_id=schedule.delete_request_id,
        target=ScheduledDeletionTarget.model_validate(schedule.target),
        created_by_username=schedule.created_by_username,
        created_by_api_token_id=schedule.created_by_api_token_id,
        last_error=schedule.last_error,
    )


@schedule_router.get("")
async def list_scheduled_deletions(
    _principal: ReadSchedules,
    db: Db,
    page: Page = 1,
    per_page: PerPage = 50,
    active_only: bool = True,
) -> ScheduledDeletionListResponse:
    query = select(ScheduledDeletion)
    if active_only:
        query = query.where(ScheduledDeletion.state.in_(ACTIVE_SCHEDULE_STATES))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await db.scalars(
        query.order_by(ScheduledDeletion.scheduled_at, ScheduledDeletion.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    items = [
        _schedule_summary(response)
        for response in await schedule_responses(db, rows.all())
    ]
    return ScheduledDeletionListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@schedule_router.get("/{schedule_id}")
async def get_scheduled_deletion(
    schedule_id: int, _principal: ReadSchedules, db: Db
) -> ExternalScheduledDeletionResponse:
    row = await db.get(ScheduledDeletion, schedule_id)
    if row is None:
        raise HTTPException(404, "Scheduled deletion not found")
    return _schedule_summary(await schedule_response(db, row))


@schedule_router.post("", status_code=201)
async def create_scheduled_deletion(
    request: ScheduleMediaRequest, principal: ManageManualSchedules, db: Db
) -> ExternalScheduledDeletionResponse:
    schedule = await schedule_workflow.create_media_schedule(
        db, DeletionActor.from_token(principal), request
    )
    return _schedule_summary(schedule)


@schedule_router.post("/{schedule_id}/reschedule")
async def reschedule_deletion(
    schedule_id: int,
    request: RescheduleDeletionRequest,
    principal: ManageSchedules,
    db: Db,
) -> ExternalScheduledDeletionResponse:
    schedule = await schedule_workflow.reschedule_schedule(
        db, schedule_id, request, principal
    )
    return _schedule_summary(schedule)


@schedule_router.post("/{schedule_id}/cancel")
async def cancel_deletion(
    schedule_id: int, principal: ManageSchedules, db: Db
) -> CancelScheduleResponse:
    return CancelScheduleResponse(
        **await schedule_workflow.cancel_schedule(db, schedule_id, principal)
    )


router.include_router(request_router)
router.include_router(schedule_router)
