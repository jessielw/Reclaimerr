from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.models.media import ExternalIDs, MovieVersionData


@dataclass(slots=True, frozen=True)
class PlexMovie:
    """Internal Plex movie representation."""

    id: str
    name: str
    year: int
    library_id: str
    library_name: str
    added_at: datetime | None
    updated_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int
    external_ids: ExternalIDs
    versions: list[MovieVersionData]


@dataclass(slots=True, frozen=True)
class PlexSeries:
    """Internal Plex series representation."""

    id: str
    name: str
    year: int
    library_id: str
    library_name: str
    path: str | None
    added_at: datetime | None
    updated_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int
    external_ids: ExternalIDs
    size: int
