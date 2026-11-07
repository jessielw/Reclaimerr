from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field


class ScheduleCandidatesRequest(BaseModel):
    candidate_ids: list[int] = Field(min_length=1)
    scheduled_at: AwareDatetime


class RescheduleDeletionRequest(BaseModel):
    scheduled_at: AwareDatetime


class ScheduledDeletionResponse(BaseModel):
    id: int
    scheduled_at: datetime
    state: str
    target: dict[str, Any]
    created_by_username: str
    last_error: str | None
