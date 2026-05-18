from __future__ import annotations

from typing import Any

from backend.core.service_runtime import handle_service_toggle
from backend.core.task_runtime import execute_task
from backend.database.models import BackgroundJob
from backend.enums import BackgroundJobType
from backend.jobs.candidate_file_ops import run_candidate_file_op_job
from backend.models.jobs import (
    CandidateFileOpJobPayload,
    ServiceToggleJobPayload,
    TaskRunJobPayload,
)
from backend.models.settings import ServiceConfigUpdate


async def run_background_job(job: BackgroundJob) -> dict[str, Any] | None:
    if job.job_type is BackgroundJobType.SERVICE_TOGGLE:
        payload = ServiceToggleJobPayload.model_validate(job.payload)
        service_update = ServiceConfigUpdate(
            id=payload.service_config_id,
            name=payload.name,
            service_type=payload.service_type,
            base_url=payload.base_url,
            api_key=payload.api_key,
            enabled=payload.enabled,
            is_main=payload.is_main,
            extra_settings=payload.extra_settings,
        )
        await handle_service_toggle(
            service_update, trigger_resync=payload.trigger_resync
        )
        return

    if job.job_type is BackgroundJobType.TASK_RUN:
        payload = TaskRunJobPayload.model_validate(job.payload)
        return await execute_task(payload.task)

    if job.job_type is BackgroundJobType.CANDIDATE_FILE_OP:
        payload = CandidateFileOpJobPayload.model_validate(job.payload)
        return await run_candidate_file_op_job(job.id, payload)

    raise ValueError(f"Unsupported background job type: {job.job_type}")
