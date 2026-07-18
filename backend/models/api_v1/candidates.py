from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.enums import MediaType

AutoDeleteState = Literal[
    "disabled", "scheduled", "eligible", "postponed", "canceled"
]


class CandidateStatusResponse(BaseModel):
    id: int
    media_type: MediaType
    scope: Literal["movie", "version", "series", "season", "episode"]
    media_id: int
    movie_version_id: int | None = None
    series_id: int | None = None
    season_id: int | None = None
    season_number: int | None = None
    episode_id: int | None = None
    episode_number: int | None = None
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    matched_rule_ids: list[int]
    reason: str
    delete_operation: Literal["delete", "move"]
    created_at: datetime
    auto_delete_state: AutoDeleteState
    auto_delete_delay_days: int
    auto_delete_eligible_at: datetime
    auto_delete_is_active: bool
    auto_delete_is_eligible: bool
    auto_delete_cancelled_at: datetime | None = None
    auto_delete_postponed_until: datetime | None = None
    auto_delete_timer_started_at: datetime | None = None
    lifecycle_reason: str | None = None
    lifecycle_updated_at: datetime | None = None
    blockers: list[str] = Field(default_factory=list)


class CandidateListResponse(BaseModel):
    items: list[CandidateStatusResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class CandidateActionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class CandidatePostponeRequest(CandidateActionRequest):
    until: datetime


class CandidateActionResponse(BaseModel):
    candidate: CandidateStatusResponse
    event_id: str
    protection_id: int | None = None
    replayed: bool = False
