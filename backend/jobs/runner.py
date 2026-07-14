from __future__ import annotations

from typing import Any

from backend.core.service_runtime import handle_service_toggle
from backend.core.task_process import run_task_job
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
        service_payload = ServiceToggleJobPayload.model_validate(job.payload)
        service_update = ServiceConfigUpdate(
            id=service_payload.service_config_id,
            name=service_payload.name,
            service_type=service_payload.service_type,
            base_url=service_payload.base_url,
            api_key=service_payload.api_key,
            enabled=service_payload.enabled,
            is_main=service_payload.is_main,
            extra_settings=service_payload.extra_settings,
        )
        await handle_service_toggle(
            service_update, trigger_resync=service_payload.trigger_resync
        )
        return None

    if job.job_type is BackgroundJobType.TASK_RUN:
        task_payload = TaskRunJobPayload.model_validate(job.payload)
        return await run_task_job(task_payload.task)

    if job.job_type is BackgroundJobType.CANDIDATE_FILE_OP:
        file_op_payload = CandidateFileOpJobPayload.model_validate(job.payload)
        return await run_candidate_file_op_job(job.id, file_op_payload)

    raise ValueError(f"Unsupported background job type: {job.job_type}")
