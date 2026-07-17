from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from backend.enums import ScheduleType, TaskStatus


class TaskResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    enabled: bool
    status: TaskStatus
    error: str | None = None
    schedule_type: ScheduleType
    schedule_value: str
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    can_run: bool
    requires_main_server: bool


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    has_main_server: bool


class TaskRunResponse(BaseModel):
    id: int
    task_id: str
    status: TaskStatus
    items_processed: int | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class TaskRunListResponse(BaseModel):
    items: list[TaskRunResponse]
    total: int
    page: int
    per_page: int
    total_pages: int


class TaskRunTriggerResponse(BaseModel):
    task_id: str
    job_id: int | None = None
    queued: bool
    already_active: bool
