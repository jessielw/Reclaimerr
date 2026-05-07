from __future__ import annotations

from pydantic import BaseModel

from backend.enums import MediaType, ProtectionRequestStatus


class CreateProtectionRequest(BaseModel):
    """Request to create an exception for a media item."""

    media_type: MediaType
    media_id: int
    reason: str | None = None
    duration_days: int | None = None
    movie_version_id: int | None = None
    # set when protecting a specific season instead of the whole series
    season_id: int | None = None


class ReviewProtectionRequest(BaseModel):
    """Admin review of an exception request."""

    admin_notes: str | None = None
    approved_duration_days: int | None = None
    approved_permanent: bool | None = None


class ProtectionRequestResponse(BaseModel):
    """Exception request with details."""

    id: int
    media_type: MediaType
    media_id: int
    media_title: str
    media_year: int | None
    candidate_id: int | None
    movie_version_id: int | None = None

    # season specific fields (None for series/movie level requests)
    season_id: int | None = None
    season_number: int | None = None

    requested_by_user_id: int
    requested_by_username: str
    reason: str | None
    requested_expires_at: str | None

    status: ProtectionRequestStatus
    reviewed_by_user_id: int | None
    reviewed_by_username: str | None
    reviewed_at: str | None
    admin_notes: str | None

    effective_permanent: bool | None = None
    effective_expires_at: str | None = None

    created_at: str
    updated_at: str

    poster_url: str | None = None

    # version specific fields (populated when movie_version_id is set)
    version_resolution: str | None = None
    version_file_name: str | None = None
    version_size: int | None = None
    version_video_codec: str | None = None
    version_hdr: bool | None = None
    version_dolby_vision: bool | None = None
    # season aggregate fields (populated when season_id is set)
    season_size: int | None = None
    season_resolution: str | None = None
    season_video_codecs: list[str] | None = None
    season_hdr: bool | None = None
    season_dolby_vision: bool | None = None


class CreateDeleteRequest(BaseModel):
    """Request to delete a media item."""

    media_type: MediaType
    media_id: int
    reason: str | None = None
    movie_version_id: int | None = None
    season_id: int | None = None


class ReviewDeleteRequest(BaseModel):
    """Admin review of a delete request."""

    admin_notes: str | None = None


class DeleteRequestResponse(BaseModel):
    """Delete request with details and execution status."""

    id: int
    media_type: MediaType
    media_id: int
    media_title: str
    media_year: int | None
    movie_version_id: int | None = None
    season_id: int | None = None
    season_number: int | None = None

    requested_by_user_id: int
    requested_by_username: str
    reason: str | None

    status: ProtectionRequestStatus
    reviewed_by_user_id: int | None
    reviewed_by_username: str | None
    reviewed_at: str | None
    admin_notes: str | None

    executed_at: str | None = None
    execution_error: str | None = None

    created_at: str
    updated_at: str

    poster_url: str | None = None

    # version specific fields (populated when movie_version_id is set)
    version_resolution: str | None = None
    version_file_name: str | None = None
    version_size: int | None = None
    version_video_codec: str | None = None
    version_hdr: bool | None = None
    version_dolby_vision: bool | None = None
    # season aggregate fields (populated when season_id is set)
    season_size: int | None = None
    season_resolution: str | None = None
    season_video_codecs: list[str] | None = None
    season_hdr: bool | None = None
    season_dolby_vision: bool | None = None
