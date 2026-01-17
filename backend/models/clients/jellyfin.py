from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.models.media import ExternalIDs


@dataclass(slots=True, frozen=True)
class JellyfinUser:
    """Jellyfin user information."""

    id: str
    name: str


@dataclass(slots=True, frozen=True)
class JellyfinUserData:
    """User-specific watch data for a media item."""

    id: str
    key: str
    play_count: int
    last_played_date: datetime | None
    played: bool


@dataclass(slots=True, frozen=True)
class JellyfinMovie:
    """Internal Jellyfin movie representation with user data."""

    id: str
    name: str
    year: int | None
    premiere_date: datetime | None
    date_created: datetime | None
    container: str
    library_id: str
    library_name: str
    path: str | None
    external_ids: ExternalIDs | None
    size: int
    user_data: JellyfinUserData | None


@dataclass(slots=True, frozen=True)
class JellyfinSeries:
    """Internal Jellyfin series representation with user data."""

    id: str
    name: str
    year: int | None
    premiere_date: datetime | None
    date_created: datetime | None
    library_id: str
    library_name: str
    path: str | None
    external_ids: ExternalIDs | None
    size: int
    user_data: JellyfinUserData | None
