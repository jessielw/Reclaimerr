from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.enums import MediaType, SeerrRequestStatus


@dataclass(slots=True, frozen=True)
class SeerrPageInfo:
    """Seerr pagination info."""

    page: int
    pages: int
    results: int


@dataclass(slots=True, frozen=True)
class SeerrRequest:
    """Seerr media request."""

    id: int
    status: SeerrRequestStatus
    media_id: int
    media_type: MediaType
    tmdb_id: int
    created_at: datetime
    requested_by_id: int
    is_4k: bool
    raw: dict | None = None

    def __repr__(self) -> str:
        return (
            f"SeerrRequest(id={self.id}, status={self.status}, media_id={self.media_id}, "
            f"media_type={self.media_type}, tmdb_id={self.tmdb_id}, created_at={self.created_at}, "
            f"requested_by_id={self.requested_by_id}, is_4k={self.is_4k})"
        )


@dataclass(slots=True, frozen=True)
class SeerrUser:
    """Seerr user identity for requester matching/pickers."""

    id: int
    username: str | None
    display_name: str | None
    email: str | None = None
    raw: dict | None = None
