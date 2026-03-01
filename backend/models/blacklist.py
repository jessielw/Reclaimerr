from pydantic import BaseModel

from backend.enums import MediaType


class BlacklistEntryResponse(BaseModel):
    id: int
    media_type: MediaType
    media_id: int
    media_title: str
    media_year: int
    poster_url: str | None
    reason: str | None
    blacklisted_by_user_id: int
    blacklisted_by_username: str
    permanent: bool
    expires_at: str | None
    created_at: str
    updated_at: str


class CreateBlacklistEntryRequest(BaseModel):
    media_type: MediaType
    media_id: int
    reason: str | None = None
    duration_days: int | None = None


class UpdateBlacklistDurationRequest(BaseModel):
    duration_days: int | None = None


class PaginatedBlacklistResponse(BaseModel):
    items: list[BlacklistEntryResponse]
    total: int
    page: int
    per_page: int
    total_pages: int
