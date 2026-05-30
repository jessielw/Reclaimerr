from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from backend.core.utils.request import summarize_error_message
from backend.database import async_db
from backend.database.models import DeleteRequest, ReclaimCandidate
from backend.enums import BackgroundJobType, CandidateFileOpOperation
from backend.jobs.queue import enqueue_background_job, update_background_job_payload
from backend.models.jobs import (
    CandidateFileOpJobItem,
    CandidateFileOpJobPayload,
    CandidateFileOpJobProgress,
    CandidateFileOpJobResult,
)
from backend.tasks.cleanup import delete_specific_candidates, move_specific_candidates


async def queue_candidate_file_op_job(
    *,
    operation: CandidateFileOpOperation,
    candidate_ids: list[int],
    requested_by_user_id: int,
    requested_by_username: str,
    delete_request_id: int | None = None,
    item_labels: list[str] | None = None,
    item_label_total: int | None = None,
    item_details: list[CandidateFileOpJobItem] | None = None,
):
    """Queues a background job to perform file operations (delete/move) on candidates."""
    payload = CandidateFileOpJobPayload(
        operation=operation,
        candidate_ids=candidate_ids,
        requested_by_user_id=requested_by_user_id,
        requested_by_username=requested_by_username,
        delete_request_id=delete_request_id,
        item_labels=item_labels or [],
        item_label_total=item_label_total,
        item_details=item_details or [],
        progress=CandidateFileOpJobProgress(
            total_items=len(candidate_ids),
        ),
    ).model_dump(mode="json")
    job = await enqueue_background_job(
        job_type=BackgroundJobType.CANDIDATE_FILE_OP,
        payload=payload,
    )
    if job is None:
        raise RuntimeError("Failed to queue candidate file operation")
    return job


async def _finalize_delete_request_job(
    *,
    delete_request_id: int,
    candidate_ids: list[int],
    succeeded: int,
    failed: int,
    fallback_error: str | None = None,
) -> None:
    """Finalizes the delete request based on the results of the candidate file operation job."""
    candidate_id = candidate_ids[0] if candidate_ids else None

    async with async_db() as db:
        delete_request = await db.get(DeleteRequest, delete_request_id)
        if delete_request is None:
            return

        candidate_after = None
        if candidate_id is not None:
            candidate_result = await db.execute(
                select(ReclaimCandidate).where(ReclaimCandidate.id == candidate_id)
            )
            candidate_after = candidate_result.scalar_one_or_none()

        if succeeded > 0 and failed == 0:
            delete_request.executed_at = datetime.now(UTC)
            delete_request.execution_error = None
        else:
            delete_request.execution_error = summarize_error_message(
                (candidate_after.last_delete_error if candidate_after else None)
                or fallback_error
                or "Deletion failed",
                max_chars=500,
            )
            if not delete_request.execution_error:
                delete_request.execution_error = "Deletion failed"
            if candidate_after is not None:
                await db.delete(candidate_after)

        await db.commit()


async def run_candidate_file_op_job(
    job_id: int,
    payload: CandidateFileOpJobPayload,
) -> dict[str, Any]:
    """Runs the candidate file operation job, performing the specified operation on each candidate
    and updating progress."""
    item_labels_by_candidate_id = {
        item.candidate_id: item.display_label for item in payload.item_details
    }
    succeeded = 0
    failed = 0
    completed_items = 0
    total_items = len(payload.candidate_ids)

    async def _persist_progress(
        *,
        current_item_label: str | None,
        completed: int,
        failed_count: int,
    ) -> None:
        percent = 100 if total_items == 0 else int((completed / total_items) * 100)
        progress = CandidateFileOpJobProgress(
            total_items=total_items,
            completed_items=completed,
            failed_items=failed_count,
            current_item_label=current_item_label,
            percent=min(100, max(0, percent)),
        )
        await update_background_job_payload(
            job_id,
            {"progress": progress.model_dump(mode="json")},
        )

    try:
        for index, candidate_id in enumerate(payload.candidate_ids):
            current_item_label = item_labels_by_candidate_id.get(
                candidate_id,
                f"Item {index + 1} of {total_items}",
            )
            await _persist_progress(
                current_item_label=current_item_label,
                completed=completed_items,
                failed_count=failed,
            )

            if payload.operation is CandidateFileOpOperation.DELETE:
                item_succeeded, item_failed = await delete_specific_candidates(
                    [candidate_id],
                    approved_by=payload.requested_by_username,
                )
            elif payload.operation is CandidateFileOpOperation.MOVE:
                item_succeeded, item_failed = await move_specific_candidates(
                    [candidate_id],
                    approved_by=payload.requested_by_username,
                )
            else:
                raise ValueError(
                    f"Unsupported candidate file operation: {payload.operation}"
                )

            if item_succeeded == 0 and item_failed == 0:
                # a prior cascading delete/move may already have removed this candidate.
                item_succeeded = 1

            succeeded += item_succeeded
            failed += item_failed
            completed_items += 1
            await _persist_progress(
                current_item_label=current_item_label,
                completed=completed_items,
                failed_count=failed,
            )
    except Exception as exc:
        await _persist_progress(
            current_item_label=None,
            completed=completed_items,
            failed_count=failed,
        )
        if payload.delete_request_id is not None:
            await _finalize_delete_request_job(
                delete_request_id=payload.delete_request_id,
                candidate_ids=payload.candidate_ids,
                succeeded=succeeded,
                failed=max(failed, len(payload.candidate_ids) - completed_items),
                fallback_error=str(exc),
            )
        raise

    await _persist_progress(
        current_item_label=None,
        completed=completed_items,
        failed_count=failed,
    )
    result = CandidateFileOpJobResult(
        operation=payload.operation,
        processed=total_items,
        succeeded=succeeded,
        failed=failed,
    )

    if payload.delete_request_id is not None:
        await _finalize_delete_request_job(
            delete_request_id=payload.delete_request_id,
            candidate_ids=payload.candidate_ids,
            succeeded=succeeded,
            failed=failed,
        )

    return result.model_dump(mode="json")
