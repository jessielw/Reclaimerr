from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.api_tokens import (
    API_TOKEN_EVENTS_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.utils.datetime_utils import ensure_utc
from backend.database import get_db
from backend.database.models import LifecycleEvent
from backend.models.api_v1 import EventFeedResponse, EventResponse
from backend.models.api_v1.events import EventActorResponse

router = APIRouter(tags=["v1:events"])


def _event_response(event: LifecycleEvent) -> EventResponse:
    if event.actor_api_token_id is not None:
        actor = EventActorResponse(type="api_token", id=event.actor_api_token_id)
    elif event.actor_user_id is not None:
        actor = EventActorResponse(type="user", id=event.actor_user_id)
    else:
        actor = EventActorResponse(type="system")
    return EventResponse(
        id=event.event_id,
        type=event.event_type,
        occurred_at=event.occurred_at,
        candidate_id=event.candidate_id,
        actor=actor,
        payload=dict(event.payload),
    )


@router.get("/events", response_model=EventFeedResponse)
async def list_events(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_EVENTS_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=100, ge=1, le=200),
    event_type: str | None = Query(default=None, max_length=64),
    candidate_id: int | None = Query(default=None, ge=1),
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> EventFeedResponse:
    query = select(LifecycleEvent)
    if cursor is not None:
        anchor = (
            await db.execute(
                select(LifecycleEvent.id).where(LifecycleEvent.event_id == cursor)
            )
        ).scalar_one_or_none()
        if anchor is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event cursor is invalid or no longer available",
            )
        query = query.where(LifecycleEvent.id > anchor)
    if event_type is not None:
        query = query.where(LifecycleEvent.event_type == event_type)
    if candidate_id is not None:
        query = query.where(LifecycleEvent.candidate_id == candidate_id)
    if occurred_after is not None:
        query = query.where(LifecycleEvent.occurred_at >= ensure_utc(occurred_after))
    if occurred_before is not None:
        query = query.where(LifecycleEvent.occurred_at <= ensure_utc(occurred_before))

    rows = (
        (await db.execute(query.order_by(LifecycleEvent.id.asc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return EventFeedResponse(
        items=[_event_response(event) for event in page],
        next_cursor=page[-1].event_id if page else cursor,
        has_more=has_more,
    )
