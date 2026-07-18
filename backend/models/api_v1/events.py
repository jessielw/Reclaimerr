from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventActorResponse(BaseModel):
    type: Literal["api_token", "user", "system"]
    id: int | None = None


class EventResponse(BaseModel):
    id: str
    type: str
    occurred_at: datetime
    candidate_id: int | None = None
    actor: EventActorResponse
    payload: dict[str, Any] = Field(default_factory=dict)


class EventFeedResponse(BaseModel):
    items: list[EventResponse]
    next_cursor: str | None = None
    has_more: bool = False
