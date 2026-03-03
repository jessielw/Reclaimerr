from __future__ import annotations

from pydantic import BaseModel


class DashboardKpis(BaseModel):
    total_movies: int
    total_series: int
    reclaimable_movies_gb: float
    reclaimable_series_gb: float
    reclaimable_total_gb: float


class DashboardRequestsSummary(BaseModel):
    pending_count: int
    approved_7d: int
    denied_7d: int
    mine_pending: int
    mine_active: int


class DashboardServiceSummary(BaseModel):
    name: str
    status: str
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
