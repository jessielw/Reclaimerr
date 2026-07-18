from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from backend.enums import MediaType


class ProtectionCreateRequest(BaseModel):
    media_type: MediaType
    media_id: int | None = Field(default=None, ge=1)
    tmdb_id: int | None = Field(default=None, ge=1)
    movie_version_id: int | None = Field(default=None, ge=1)
    season_id: int | None = Field(default=None, ge=1)
    episode_id: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=1000)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_locator(self) -> ProtectionCreateRequest:
        if (self.media_id is None) == (self.tmdb_id is None):
            raise ValueError("Provide exactly one of media_id or tmdb_id")
        if self.media_type is MediaType.MOVIE and (
            self.season_id is not None or self.episode_id is not None
        ):
            raise ValueError("Movie protections cannot target seasons or episodes")
        if self.media_type is MediaType.SERIES and self.movie_version_id is not None:
            raise ValueError("Series protections cannot target movie versions")
        return self


class ProtectionResponse(BaseModel):
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
    tmdb_id: int
    source: str
    reason: str | None = None
    permanent: bool
    expires_at: datetime | None = None
    active: bool
    created_at: datetime
    updated_at: datetime


class ProtectionListResponse(BaseModel):
    items: list[ProtectionResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class ProtectionMutationResponse(BaseModel):
    protection: ProtectionResponse
    created: bool = False
