from __future__ import annotations

from pydantic import BaseModel

from backend.enums import Service, Task


class ServiceToggleJobPayload(BaseModel):
    service_config_id: int | None = None
    name: str | None = None
    service_type: Service
    base_url: str
    api_key: str
    enabled: bool
    is_main: bool = False
    extra_settings: dict | None = None
    trigger_resync: bool = False


class TaskRunJobPayload(BaseModel):
    task: Task
