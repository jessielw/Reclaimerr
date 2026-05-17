from __future__ import annotations

from pydantic import BaseModel


class DashboardKpis(BaseModel):
    total_movies: int
    total_series: int
    total_movies_size_bytes: int
    total_series_size_bytes: int
    reclaimable_movies_bytes: int
    reclaimable_series_bytes: int
    reclaimable_total_bytes: int
    reclaimed_movies: int
    reclaimed_series: int
    reclaimed_total_bytes: int


class DashboardRequestsSummary(BaseModel):
    pending_count: int
    approved_7d: int
    denied_7d: int
    mine_pending: int
    mine_active: int


class DashboardServiceSummary(BaseModel):
    service_type: str
    name: str
    url: str
    enabled: bool
    last_sync_at: str | None


class DashboardActivityItem(BaseModel):
    id: str
    type: str
    title: str
    subtitle: str | None = None
    created_at: str
    actor_display: str | None = None
    media_type: str | None = None
    media_title: str | None = None


class DashboardViewer(BaseModel):
    role: str
    can_view_admin_panels: bool


class DashboardResponse(BaseModel):
    kpis: DashboardKpis
    requests: DashboardRequestsSummary
    services: list[DashboardServiceSummary]
    activity: list[DashboardActivityItem]
    viewer: DashboardViewer
    media_server_configured: bool
