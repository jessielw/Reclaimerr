from __future__ import annotations

from pydantic import BaseModel

from backend.enums import MediaType


class CleanupRuleReq(BaseModel):
    name: str
    media_type: MediaType
    min_popularity: float | None = None
    max_popularity: float | None = None
    min_vote_average: float | None = None
    max_vote_average: float | None = None
    min_vote_count: int | None = None
    max_vote_count: int | None = None
    min_view_count: int | None = None
    max_view_count: int | None = None
    include_never_watched: bool | None = None
    min_days_since_added: int | None = None
    max_days_since_added: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    library_names: list[str] | None = None
