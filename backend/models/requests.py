from __future__ import annotations

from pydantic import BaseModel

from backend.enums import MediaType, ProtectionRequestStatus


class CreateProtectionRequest(BaseModel):
    """Request to create an exception for a media item."""

    media_type: MediaType
    media_id: int
    reason: str
    duration_days: int | None = None


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
    media_year: int
    candidate_id: int | None

    requested_by_user_id: int
    requested_by_username: str
    reason: str
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
