from typing import Literal

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
    anilist_id: int | None = None
    anilist_score: int | None = None
    anilist_popularity: int | None = None
    anilist_favourites: int | None = None
    rottentomatoes_tomato_meter: int | None = None
    rottentomatoes_tomato_vote_count: int | None = None
    rottentomatoes_popcorn_meter: int | None = None
    rottentomatoes_popcorn_vote_count: int | None = None
    metacritic_metascore: int | None = None
    metacritic_vote_count: int | None = None
    metacritic_user_score: int | None = None
    metacritic_user_vote_count: int | None = None
    trakt_rating: int | None = None
    trakt_vote_count: int | None = None
    letterboxd_score: int | None = None
    letterboxd_vote_count: int | None = None
    external_ratings_source: str | None = None
    external_ratings_refreshed_at: str | None = None
    reason: str | None
    protected_by_user_id: int | None
    protected_by_username: str
    source: Literal["manual", "rule"] = "manual"
    source_rule_id: int | None = None
    source_rule_name: str | None = None
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
