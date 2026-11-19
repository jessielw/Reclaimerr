from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from backend.enums import MediaType


class ScheduleCandidatesRequest(BaseModel):
    candidate_ids: list[int] = Field(min_length=1)
    scheduled_at: AwareDatetime


class ScheduleMediaRequest(BaseModel):
    media_type: MediaType
    media_id: int = Field(gt=0)
    movie_version_id: int | None = Field(default=None, gt=0)
    season_id: int | None = Field(default=None, gt=0)
    episode_id: int | None = Field(default=None, gt=0)
    scheduled_at: AwareDatetime


class RescheduleDeletionRequest(BaseModel):
    scheduled_at: AwareDatetime


class ScheduledDeletionResponse(BaseModel):
    id: int
    scheduled_at: datetime
    state: str
    delete_request_id: int | None = None
    target: dict[str, Any]
    created_by_username: str
    created_by_api_token_id: int | None = None
    last_error: str | None
