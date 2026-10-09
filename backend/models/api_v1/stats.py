from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class StatsResponse(BaseModel):
    """Flat numbers for dashboard widgets such as Homepage's ``customapi``."""

    # flagged titles, counted the way the Candidates page groups them
    candidates_total: int
    candidates_movies: int
    candidates_series: int
    reclaimable_bytes: int
    reclaimed_bytes_total: int
    reclaimed_items_total: int
    pending_protection_requests: int
    pending_delete_requests: int
    # earliest automatic deletion or move; in the past when an eligible item is
    # waiting for the next run of the deletion task
    next_auto_delete_at: datetime | None = None
    # whether that task is enabled at all; when it is not, nothing above is
    # deleted automatically regardless of its date
    auto_delete_task_enabled: bool
