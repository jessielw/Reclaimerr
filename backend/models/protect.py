from pydantic import BaseModel

from backend.enums import MediaType


class ProtectedEntryResponse(BaseModel):
    id: int
    media_type: MediaType
    media_id: int
    media_title: str
    media_year: int
    poster_url: str | None
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
