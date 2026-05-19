from pydantic import BaseModel


class SidebarIndicatorsResponse(BaseModel):
    has_candidates: bool
    has_pending_requests: bool
    has_pending_protection_requests: bool
    has_pending_delete_requests: bool


class UiIndicatorsResponse(BaseModel):
    has_candidates: bool
    has_pending_requests: bool
    has_pending_protection_requests: bool
    has_pending_delete_requests: bool
    update_available: bool
    latest_version: str | None
    latest_release_url: str | None
    last_checked_at: str | None
