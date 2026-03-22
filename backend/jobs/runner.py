from __future__ import annotations

from typing import Any

from backend.core.service_runtime import handle_service_toggle
from backend.core.task_runtime import execute_task
from backend.database.models import BackgroundJob
from backend.enums import BackgroundJobType
from backend.models.jobs import ServiceToggleJobPayload, TaskRunJobPayload
from backend.models.settings import ServiceConfigUpdate


async def run_background_job(job: BackgroundJob) -> dict[str, Any] | None:
    if job.job_type is BackgroundJobType.SERVICE_TOGGLE:
        payload = ServiceToggleJobPayload.model_validate(job.payload)
        service_update = ServiceConfigUpdate(
            service_type=payload.service_type,
            base_url=payload.base_url,
            api_key=payload.api_key,
            enabled=payload.enabled,
            is_main=payload.is_main,
        )
        await handle_service_toggle(service_update, trigger_resync=payload.trigger_resync)
        return

    if job.job_type is BackgroundJobType.TASK_RUN:
        payload = TaskRunJobPayload.model_validate(job.payload)
        return await execute_task(payload.task)

    raise ValueError(f"Unsupported background job type: {job.job_type}")
