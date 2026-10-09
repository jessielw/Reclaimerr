from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import (
    API_TOKEN_SYSTEM_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.database import get_db
from backend.database.models import (
    DeleteRequest,
    ProtectionRequest,
    ReclaimCandidate,
    TaskSchedule,
)
from backend.enums import MediaType, ProtectionRequestStatus, Task
from backend.models.api_v1 import StatsResponse
from backend.services.reclaim_stats import load_reclaim_totals, next_auto_delete_at

router = APIRouter(tags=["v1:system"])


async def _count(db: AsyncSession, statement: Any) -> int:
    return int(await db.scalar(statement) or 0)


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_SYSTEM_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StatsResponse:
    """Totals for a dashboard tile. The reclaim numbers match the Dashboard page."""
    candidates_movies = await _count(
        db,
        select(func.count(func.distinct(ReclaimCandidate.movie_id))).where(
            ReclaimCandidate.media_type == MediaType.MOVIE
        ),
    )
    candidates_series = await _count(
        db,
        select(func.count(func.distinct(ReclaimCandidate.series_id))).where(
            ReclaimCandidate.media_type == MediaType.SERIES
        ),
    )
    totals = await load_reclaim_totals(db)
    auto_delete_enabled = await db.scalar(
        select(TaskSchedule.enabled).where(
            TaskSchedule.task == Task.DELETE_CLEANUP_CANDIDATES
        )
    )
    return StatsResponse(
        candidates_total=candidates_movies + candidates_series,
        candidates_movies=candidates_movies,
        candidates_series=candidates_series,
        reclaimable_bytes=(
            totals.reclaimable_movies_bytes + totals.reclaimable_series_bytes
        ),
        reclaimed_bytes_total=totals.reclaimed_bytes,
        reclaimed_items_total=totals.reclaimed_movies + totals.reclaimed_series,
        pending_protection_requests=await _count(
            db,
            select(func.count()).where(
                ProtectionRequest.status == ProtectionRequestStatus.PENDING
            ),
        ),
        pending_delete_requests=await _count(
            db,
            select(func.count()).where(
                DeleteRequest.status == ProtectionRequestStatus.PENDING
            ),
        ),
        next_auto_delete_at=await next_auto_delete_at(db),
        auto_delete_task_enabled=bool(auto_delete_enabled),
    )
