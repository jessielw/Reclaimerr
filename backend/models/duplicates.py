from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.enums import MediaType


class DuplicateFileResponse(BaseModel):
    version_ids: list[int]
    service: str
    library_names: list[str]
    path: str | None
    size: int
    added_at: datetime | None
    video_resolution: str | None
    video_width: int | None
    video_height: int | None
    video_codec_family: str | None
    video_hdr: bool | None
    video_dolby_vision: bool | None
    video_bitrate: int | None
    audio_codec_family: str | None
    audio_channels: int | None
    protected: bool


class DuplicateGroupResponse(BaseModel):
    key: str
    media_type: MediaType
    item_id: int
    title: str
    year: int | None
    poster_url: str | None
    series_id: int | None
    season_number: int | None
    episode_number: int | None
    episode_name: str | None
    # best first; files[0] is the suggested keeper
    files: list[DuplicateFileResponse]
    manual_reason: str | None
    cross_library: bool
    ignored: bool
    reclaimable_size: int


class DuplicateSummary(BaseModel):
    # counts over the whole filtered result, not just this page
    groups: int
    actionable: int
    reclaimable_size: int


class PaginatedDuplicatesResponse(BaseModel):
    items: list[DuplicateGroupResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
    summary: DuplicateSummary


class DuplicateDeleteItem(BaseModel):
    media_type: MediaType
    item_id: int
    version_ids: list[int] = Field(min_length=1)


class DuplicateDeleteRequest(BaseModel):
    items: list[DuplicateDeleteItem] = Field(min_length=1, max_length=500)


class DuplicateIgnoreRequest(BaseModel):
    media_type: MediaType
    item_id: int


class KeeperPriorityEntry(BaseModel):
    key: str
    enabled: bool


class DuplicateSettings(BaseModel):
    keeper_priority: list[KeeperPriorityEntry]
