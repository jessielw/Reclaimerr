from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.enums import MediaType


class LeavingSoonItem(BaseModel):
    source: Literal["candidate", "manual", "delete_request"]
    candidate_id: int | None = None
    scheduled_deletion_id: int | None = None
    delete_request_id: int | None = None
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
    size_bytes: int | None = Field(
        default=None, description="Estimated bytes for this scope; null if unknown."
    )
    delete_operation: Literal["delete", "move"]
    delete_at: datetime = Field(
        description="UTC eligibility deadline, not a guaranteed execution time."
    )
    state: str = Field(
        description="scheduled, eligible, postponed, running, blocked, or failed."
    )


class LeavingSoonListResponse(BaseModel):
    items: list[LeavingSoonItem]
    total: int
    page: int
    per_page: int
    total_pages: int
