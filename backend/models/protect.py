from pydantic import BaseModel

from backend.enums import MediaType


class ProtectedEntryResponse(BaseModel):
    id: int
    media_type: MediaType
    media_id: int
    movie_version_id: int | None = None
    season_id: int | None = None
    season_number: int | None = None
    episode_id: int | None = None
    episode_number: int | None = None
    episode_name: str | None = None
    media_title: str
    media_year: int | None
    poster_url: str | None
    tmdb_id: int | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    imdb_id: str | None = None
    imdb_rating: float | None = None
    imdb_vote_count: int | None = None
    reason: str | None
    protected_by_user_id: int
    protected_by_username: str
    permanent: bool
    expires_at: str | None
    created_at: str
    updated_at: str


class CreateProtectedEntryRequest(BaseModel):
    media_type: MediaType
    media_id: int
    movie_version_id: int | None = None
    season_id: int | None = None
    episode_id: int | None = None
    reason: str | None = None
    duration_days: int | None = None


class UpdateProtectionDurationRequest(BaseModel):
    duration_days: int | None = None


class PaginatedProtectedResponse(BaseModel):
    items: list[ProtectedEntryResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
