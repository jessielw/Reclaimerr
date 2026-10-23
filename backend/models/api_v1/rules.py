from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from backend.enums import MediaType


class RuleResponse(BaseModel):
    id: int
    name: str
    media_type: MediaType
    target_scope: str
    outcome: str
    enabled: bool


class RuleListResponse(BaseModel):
    items: list[RuleResponse]


class RuleMatchRuleRef(BaseModel):
    id: int
    name: str
    media_type: MediaType
    target_scope: str


class RuleMatchArrRef(BaseModel):
    service: Literal["radarr", "sonarr"]
    service_config_id: int
    service_name: str | None = None
    arr_id: int


class RuleMatchItem(BaseModel):
    target_scope: str
    media_type: MediaType
    movie_id: int | None = None
    movie_version_id: int | None = None
    series_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None
    arr: list[RuleMatchArrRef] = Field(default_factory=list)
    reason: str


class RuleMatchesResponse(BaseModel):
    rule: RuleMatchRuleRef
    # false when provider data was missing, so the matches may be partial
    complete: bool
    warnings: list[str] = Field(default_factory=list)
    total: int
    items: list[RuleMatchItem]
