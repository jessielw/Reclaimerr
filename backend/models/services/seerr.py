from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.enums import MediaType, SeerrRequestStatus


@dataclass(slots=True, frozen=True)
class SeerrPageInfo:
    """Seerr pagination info."""

    page: int
    pages: int
    results: int


@dataclass(slots=True, frozen=True)
class SeerrRequestedSeason:
    """One season included in a Seerr TV request."""

    season_number: int
    created_at: datetime


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
    requested_seasons: tuple[SeerrRequestedSeason, ...] = ()
    raw: Mapping[str, Any] | None = None

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
    # Playback providers report the media-server account name rather than the
    # Seerr one, so these are what requester watch matching actually joins on.
    plex_username: str | None = None
    plex_id: int | None = None
    jellyfin_username: str | None = None
    jellyfin_user_id: str | None = None
    raw: Mapping[str, Any] | None = None

    def identity_values(self) -> tuple[str | int | None, ...]:
        """Return every value that can identify this user to a media server."""
        return (
            self.username,
            self.display_name,
            self.email,
            self.plex_username,
            self.plex_id,
            self.jellyfin_username,
            self.jellyfin_user_id,
        )
