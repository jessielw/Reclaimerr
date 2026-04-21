from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.models.media import ExternalIDs, MovieVersionData


@dataclass(slots=True, frozen=True)
class EmbyUserBase:
    """Emby user information."""

    id: str
    name: str


@dataclass(slots=True, frozen=True)
class EmbyUserDataBase:
    """User specific watch data for a media item."""

    id: str
    key: str
    play_count: int
    last_played_date: datetime | None
    played: bool


@dataclass(slots=True, frozen=True)
class EmbyMovieBase:
    """Internal Emby movie representation with user data."""

    id: str
    name: str
    year: int | None
    date_created: datetime | None
    library_id: str
    library_name: str
    external_ids: ExternalIDs | None
    versions: list[MovieVersionData]
    user_data: EmbyUserDataBase | None


@dataclass(slots=True, frozen=True)
class EmbySeriesBase:
    """Internal Emby series representation with user data."""

    id: str
    name: str
    year: int | None
    date_created: datetime | None
    library_id: str
    library_name: str
    path: str | None
    external_ids: ExternalIDs | None
    size: int
    user_data: EmbyUserDataBase | None
