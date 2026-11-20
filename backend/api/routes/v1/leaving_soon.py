from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import (
    API_TOKEN_CANDIDATES_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.utils.datetime_utils import ensure_utc
from backend.database import get_db
from backend.enums import MediaType
from backend.models.api_v1.common import total_pages
from backend.models.api_v1.leaving_soon import LeavingSoonListResponse
from backend.services.leaving_soon import load_leaving_soon

router = APIRouter(tags=["v1:leaving-soon"])


@router.get("/leaving-soon", response_model=LeavingSoonListResponse)
async def list_leaving_soon(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    media_type: MediaType | None = None,
    delete_after: Annotated[
        AwareDatetime | None, Query(description="Inclusive earliest deletion deadline")
    ] = None,
    delete_before: Annotated[
        AwareDatetime | None, Query(description="Inclusive latest deletion deadline")
    ] = None,
) -> LeavingSoonListResponse:
    """List cleanup timers, manual schedules, and delayed deletion approvals.

    Deadlines may be overdue. Blocked and failed schedules remain visible until
    canceled or rescheduled. Dates express eligibility, not guaranteed execution.
    """
    after: datetime | None = ensure_utc(delete_after) if delete_after else None
    before: datetime | None = ensure_utc(delete_before) if delete_before else None
    if after is not None and before is not None and before < after:
        raise HTTPException(422, "delete_before must not precede delete_after")
    items, total = await load_leaving_soon(
        db,
        media_type=media_type,
        after=after,
        before=before,
        offset=(page - 1) * per_page,
        limit=per_page,
    )
    return LeavingSoonListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )
