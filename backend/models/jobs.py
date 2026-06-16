from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from backend.enums import CandidateFileOpOperation, MediaType, Service, Task


class ServiceToggleJobPayload(BaseModel):
    service_config_id: int | None = None
    name: str | None = None
    service_type: Service
    base_url: str
    api_key: str
    enabled: bool
    is_main: bool = False
    extra_settings: dict[str, Any] | None = None
    trigger_resync: bool = False


class TaskRunJobPayload(BaseModel):
    task: Task


class CandidateFileOpJobPayload(BaseModel):
    operation: CandidateFileOpOperation
    candidate_ids: list[int]
    requested_by_user_id: int
    requested_by_username: str
    delete_request_id: int | None = None
    item_labels: list[str] = []
    item_label_total: int | None = None
    item_details: list[CandidateFileOpJobItem] = []
    progress: CandidateFileOpJobProgress | None = None


class CandidateFileOpJobItem(BaseModel):
    candidate_id: int
    media_type: MediaType
    scope: Literal["movie", "version", "series", "season", "episode"]
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_name: str | None = None
    resolution: str | None = None
    hdr: bool | None = None
    dolby_vision: bool | None = None
    display_label: str


class CandidateFileOpJobProgress(BaseModel):
    total_items: int
    completed_items: int = 0
    failed_items: int = 0
    current_item_label: str | None = None
    percent: int = 0


class CandidateFileOpJobResult(BaseModel):
    operation: CandidateFileOpOperation
    processed: int
    succeeded: int
    failed: int
