from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.__version__ import __version__, program_name, program_url
from backend.core.api_tokens import (
    API_TOKEN_SYSTEM_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.service_manager import service_manager
from backend.database import get_db
from backend.database.models import TaskRun
from backend.enums import Task, TaskStatus
from backend.models.api_v1 import SystemResponse

router = APIRouter(tags=["v1:system"])


async def _last_success(db: AsyncSession, task: Task) -> datetime | None:
    return (
        await db.execute(
            select(TaskRun.completed_at)
            .where(TaskRun.task == task, TaskRun.status == TaskStatus.COMPLETED)
            .order_by(TaskRun.completed_at.desc(), TaskRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/system", response_model=SystemResponse)
async def get_system(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_SYSTEM_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemResponse:
    media_sync = await _last_success(db, Task.SYNC_MEDIA)
    if media_sync is None:
        media_sync = await _last_success(db, Task.RESYNC_MEDIA)
    return SystemResponse(
        status="ok",
        program=program_name,
        version=str(__version__),
        project_url=program_url,
        api_version="v1",
        server_time=datetime.now(UTC),
        has_main_media_server=service_manager.main_media_server is not None,
        last_media_sync_at=media_sync,
        last_candidate_scan_at=await _last_success(db, Task.SCAN_CLEANUP_CANDIDATES),
        capabilities=[
            "candidate-lifecycle",
            "event-feed",
            "media-catalog",
            "protections",
            "task-observability",
            "task-triggering",
            "durable-webhooks",
        ],
    )
