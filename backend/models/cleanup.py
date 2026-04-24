from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from backend.enums import MediaType


class CleanupRuleBase(BaseModel):
    """Base model with all cleanup rule fields."""

    name: str
    media_type: MediaType
    enabled: bool = True
    library_ids: list[str] | None = None

    # TMDB criteria
    min_popularity: float | None = None
    max_popularity: float | None = None
    min_vote_average: float | None = None
    max_vote_average: float | None = None
    min_vote_count: int | None = None
    max_vote_count: int | None = None

    # watch history criteria
    min_view_count: int | None = None
    max_view_count: int | None = None
    include_never_watched: bool = False

    # age criteria (days since added)
    min_days_since_added: int | None = None
    max_days_since_added: int | None = None

    # watch recency criteria (days since last watched)
    min_days_since_last_watched: int | None = None
    max_days_since_last_watched: int | None = None

    # size criteria (bytes)
    min_size: int | None = None
    max_size: int | None = None

    # path criteria - list of glob patterns rooted at known library paths
    paths: list[str] | None = None

    # series status criteria - only applies when media_type is Series
    # None or empty list = any status
    # List of TMDB status values to match (e.g., "Returning Series", "Ended", "Canceled", etc.)
    series_status: list[str] | None = None


class CleanupRuleCreate(CleanupRuleBase):
    """Model for creating a new cleanup rule."""

    pass


class CleanupRuleUpdate(BaseModel):
    """Model for updating an existing cleanup rule. All fields are optional."""

    name: str | None = None
    media_type: MediaType | None = None
    enabled: bool | None = None
    library_ids: list[str] | None = None

    # TMDB criteria
    min_popularity: float | None = None
    max_popularity: float | None = None
    min_vote_average: float | None = None
    max_vote_average: float | None = None
    min_vote_count: int | None = None
    max_vote_count: int | None = None

    # watch history criteria
    min_view_count: int | None = None
    max_view_count: int | None = None
    include_never_watched: bool | None = None

    # age criteria (days since added)
    min_days_since_added: int | None = None
    max_days_since_added: int | None = None

    # watch recency criteria (days since last watched)
    min_days_since_last_watched: int | None = None
    max_days_since_last_watched: int | None = None

    # size criteria (bytes)
    min_size: int | None = None
    max_size: int | None = None

    # path criteria - list of glob patterns rooted at known library paths
    paths: list[str] | None = None

    # series status criteria
    series_status: list[str] | None = None


class CleanupRuleResponse(CleanupRuleBase):
    """Model for cleanup rule API responses."""

    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
