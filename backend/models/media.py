from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from backend.types import MediaServerType


@dataclass(slots=True, frozen=True)
class ExternalIDs:
    """External provider IDs for media items."""

    tmdb: int
    imdb: str | None
    tmdb_collection: str | None
    tvdb: str | None


@dataclass(slots=True, frozen=True)
class MovieVersionData:
    """Single physical file version of a movie."""

    service: MediaServerType
    # plex ratingKey or jellyfin item ID (used for item-level ops like delete)
    service_item_id: str
    # plex Media.id or jellyfin MediaSource.Id (unique per physical file)
    service_media_id: str
    library_id: str
    library_name: str
    path: str | None
    size: int
    added_at: datetime | None
    container: str | None


@dataclass(slots=True, frozen=True)
class AggregatedMovieData:
    """Movie with aggregated watch data across all users, plus all physical file versions."""

    name: str
    year: int
    external_ids: ExternalIDs
    premiere_date: datetime | None
    versions: list[MovieVersionData]
    # watch data
    view_count: int
    last_viewed_at: datetime | None
    never_watched: bool
    # jellyfin-specific (None for Plex)
    played_by_user_count: int | None = None


@dataclass(slots=True, frozen=True)
class AggregatedSeriesData:
    """Series with aggregated watch data across all users."""

    id: str
    name: str
    year: int
    service: MediaServerType
    library_name: str
    library_id: str
    path: str | None
    added_at: datetime | None
    premiere_date: datetime | None
    external_ids: ExternalIDs
    size: int
    # watch data
    view_count: int
    last_viewed_at: datetime | None
    never_watched: bool
    # jellyfin-specific (None for Plex)
    played_by_user_count: int | None = None


@dataclass(slots=True, frozen=True)
class ArrTag:
    id: int
    label: str


class MovieVersionResponse(BaseModel):
    """API representation of a single physical file version."""

    id: int
    service: str
    service_item_id: str
    service_media_id: str
    library_id: str
    library_name: str
    path: str | None
    size: int
    added_at: str | None
    container: str | None


class MediaStatusInfo(BaseModel):
    """Status information for a media item."""

    is_candidate: bool = False
    candidate_id: int | None = None
    candidate_reason: str | None = None
    candidate_space_gb: float | None = None

    is_blacklisted: bool = False
    blacklist_reason: str | None = None
    blacklist_permanent: bool = True

    has_pending_request: bool = False
    request_id: int | None = None
    request_status: str | None = None
    request_reason: str | None = None


class MovieWithStatus(BaseModel):
    """Movie with all metadata and status information."""

    # basic info
    id: int
    title: str
    year: int
    tmdb_id: int

    # file info
    size: int | None
    versions: list[MovieVersionResponse]

    # external IDs
    radarr_id: int | None
    imdb_id: str | None

    # TMDB metadata
    tmdb_title: str | None
    original_title: str | None
    tmdb_release_date: str | None
    original_language: str | None
    poster_url: str | None
    backdrop_url: str | None
    overview: str | None
    genres: list[str] | None
    popularity: float | None
    vote_average: float | None
    vote_count: int | None
    runtime: int | None
    tagline: str | None

    # watch tracking
    last_viewed_at: str | None
    view_count: int
    never_watched: bool

    # status
    status: MediaStatusInfo

    # timestamps
    added_at: str | None


class SeriesServiceRefResponse(BaseModel):
    """API representation of a service-specific reference for a series."""

    service: str
    service_id: str
    library_id: str
    library_name: str
    path: str | None


class SeriesWithStatus(BaseModel):
    """Series with all metadata and status information."""

    # basic info
    id: int
    title: str
    year: int
    tmdb_id: int

    # file info
    size: int | None
    service_refs: list[SeriesServiceRefResponse]

    # external IDs
    sonarr_id: int | None
    imdb_id: str | None
    tvdb_id: str | None

    # TMDB metadata
    tmdb_title: str | None
    original_title: str | None
    tmdb_first_air_date: str | None
    tmdb_last_air_date: str | None
    original_language: str | None
    poster_url: str | None
    backdrop_url: str | None
    overview: str | None
    genres: list[str] | None
    popularity: float | None
    vote_average: float | None
    vote_count: int | None
    season_count: int | None
    tagline: str | None

    # watch tracking
    last_viewed_at: str | None
    view_count: int
    never_watched: bool

    # status
    status: MediaStatusInfo

    # timestamps
    added_at: str | None


class PaginatedMediaResponse(BaseModel):
    """Paginated response wrapper."""

    items: list[MovieWithStatus | SeriesWithStatus]
    total: int
    page: int
    per_page: int
    total_pages: int
