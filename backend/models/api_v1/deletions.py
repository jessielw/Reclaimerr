from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from backend.enums import MediaType
from backend.models.requests import DeleteRequestResponse


class DeleteRequestListResponse(BaseModel):
    items: list[DeleteRequestResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class ApprovalOptionsResponse(BaseModel):
    delete_request_grace_period_days: int


class ScheduledDeletionTarget(BaseModel):
    media_type: MediaType
    media_id: int
    scope: Literal["movie", "version", "series", "season", "episode"]
    title: str
    year: int | None
    tmdb_id: int
    movie_version_id: int | None = None
    season_id: int | None = None
    episode_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    estimated_space_bytes: int | None = None
    operation: Literal["delete", "move"]


class ExternalScheduledDeletionResponse(BaseModel):
    id: int
    scheduled_at: datetime
    state: str
    delete_request_id: int | None
    target: ScheduledDeletionTarget
    created_by_username: str
    created_by_api_token_id: int | None
    last_error: str | None


class ScheduledDeletionListResponse(BaseModel):
    items: list[ExternalScheduledDeletionResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class CancelScheduleResponse(BaseModel):
    message: str
