from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from backend.enums import MediaType


class ScheduleCandidatesRequest(BaseModel):
    candidate_ids: list[int] = Field(min_length=1)
    scheduled_at: AwareDatetime


class ScheduleMediaRequest(BaseModel):
    media_type: MediaType
    media_id: int
    movie_version_id: int | None = None
    season_id: int | None = None
    episode_id: int | None = None
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
