from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from backend.enums import MediaType


class DashboardLeavingSoonTitle(BaseModel):
    media_id: int
    media_type: MediaType
    title: str
    year: int | None
    poster_url: str | None
    season_number: int | None
    episode_number: int | None
    movie_version_id: int | None
    size_bytes: int | None
    delete_operation: Literal["delete", "move"]
    delete_at: datetime
    source: Literal["candidate", "manual", "delete_request"]
    scheduled_deletion_id: int | None
    additional_count: int


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
    next_deletion_at: str | None = None


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
    # media servers only: whether this is the main (library-owning) server
    is_main: bool = False


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
