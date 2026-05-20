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
    has_unread_notices: bool


class AdminNoticeResponse(BaseModel):
    id: int
    kind: str
    severity: str
    title: str
    message: str
    action_label: str | None
    action_href: str | None
    is_read: bool
    is_active: bool
    read_at: str | None
    last_occurred_at: str | None
    created_at: str
    updated_at: str


class AdminNoticesResponse(BaseModel):
    unread_count: int
    items: list[AdminNoticeResponse]
