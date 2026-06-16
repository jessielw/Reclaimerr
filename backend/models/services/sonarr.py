from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class SonarrSeason:
    """Sonarr season representation."""

    season_number: int
    monitored: bool
    statistics: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class SonarrSeries:
    """Sonarr series representation."""

    id: int
    title: str
    tvdb_id: int | None
    tmdb_id: int | None
    imdb_id: str | None
    year: int | None
    path: str
    monitored: bool
    season_count: int
    seasons: list[SonarrSeason]
    tags: list[int]
    raw: dict[str, Any] | None = None

    def __repr__(self) -> str:
        return (
            f"SonarrSeries(id={self.id}, title='{self.title}', tvdb_id={self.tvdb_id}, tmdb_id={self.tmdb_id}, "
            f"year={self.year}, seasons={self.season_count}, monitored={self.monitored}, tags={self.tags})"
        )
