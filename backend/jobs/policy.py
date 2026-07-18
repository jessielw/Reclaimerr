from __future__ import annotations

from backend.database.models import BackgroundJob
from backend.enums import BackgroundJobType, Task

TASK_RUN_RESOURCE = "task-run"
SERVICE_RUNTIME_RESOURCE = "service-runtime"
CANDIDATE_WORKFLOW_RESOURCE = "candidate-workflow"
LEGACY_WEBHOOK_RESOURCE = "webhook-delivery"

SERVICE_RUNTIME_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA,
        Task.RESYNC_MEDIA,
        Task.SYNC_MEDIA_LIBRARIES,
        Task.SYNC_LINKED_DATA,
        Task.REFRESH_PLAYBACK_HISTORY,
        Task.SCAN_CLEANUP_CANDIDATES,
        Task.TAG_CLEANUP_CANDIDATES,
        Task.DELETE_CLEANUP_CANDIDATES,
    }
)

CANDIDATE_WORKFLOW_TASKS: frozenset[Task] = frozenset(
    {
        Task.SCAN_CLEANUP_CANDIDATES,
        Task.TAG_CLEANUP_CANDIDATES,
        Task.DELETE_CLEANUP_CANDIDATES,
    }
)


def background_job_resources(job: BackgroundJob) -> frozenset[str]:
    """Return exclusive resource keys required while a job is running."""
    payload = job.payload or {}
    if job.job_type is BackgroundJobType.TASK_RUN:
        resources = {TASK_RUN_RESOURCE}
        try:
            task = Task(str(payload.get("task")))
        except ValueError:
            return frozenset(resources)
        if task in SERVICE_RUNTIME_TASKS:
            resources.add(SERVICE_RUNTIME_RESOURCE)
        if task in CANDIDATE_WORKFLOW_TASKS:
            resources.add(CANDIDATE_WORKFLOW_RESOURCE)
        return frozenset(resources)

    if job.job_type is BackgroundJobType.SERVICE_TOGGLE:
        return frozenset({SERVICE_RUNTIME_RESOURCE})

    if job.job_type is BackgroundJobType.CANDIDATE_FILE_OP:
        return frozenset({CANDIDATE_WORKFLOW_RESOURCE, SERVICE_RUNTIME_RESOURCE})

    if job.job_type is BackgroundJobType.WEBHOOK_DELIVERY:
        endpoint_id = payload.get("endpoint_id")
        if isinstance(endpoint_id, int) and endpoint_id > 0:
            return frozenset({f"webhook-endpoint:{endpoint_id}"})
        return frozenset({LEGACY_WEBHOOK_RESOURCE})

    return frozenset()
