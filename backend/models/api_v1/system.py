from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ApiDiscoveryResponse(BaseModel):
    api_version: str
    resources: dict[str, str]
    granted_scopes: list[str] = Field(default_factory=list)


class SystemResponse(BaseModel):
    status: str
    program: str
    version: str
    project_url: str
    api_version: str
    server_time: datetime
    has_main_media_server: bool
    last_media_sync_at: datetime | None = None
    last_candidate_scan_at: datetime | None = None
    capabilities: list[str] = Field(default_factory=list)
