from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.enums import MediaType


class MediaResponse(BaseModel):
    id: int
    media_type: MediaType
    title: str
    year: int | None = None
    tmdb_id: int
    imdb_id: str | None = None
    tvdb_id: str | None = None
    status: str | None = None
    size_bytes: int | None = None
    poster_url: str | None = None
    genres: list[str] = Field(default_factory=list)
    is_monitored: bool | None = None
    added_at: datetime | None = None
    arr_added_at: datetime | None = None
    last_viewed_at: datetime | None = None
    view_count: int = 0
    removed_at: datetime | None = None
    candidate_ids: list[int] = Field(default_factory=list)
    protection_ids: list[int] = Field(default_factory=list)


class MediaListResponse(BaseModel):
    items: list[MediaResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
