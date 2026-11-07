from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete

from backend.core.utils.datetime_utils import ensure_utc
from backend.core.utils.request import describe_exception
from backend.database import async_db
from backend.database.models import ReclaimCandidate, ScheduledDeletion, User
from backend.models.jobs import CandidateFileOpJobItem, CandidateFileOpJobPayload
from backend.services.candidate_lifecycle import candidate_deletion_blockers
from backend.services.scheduled_deletions import (
    executing_schedule_id,
    require_delete_permission,
    resolve_target,
)


async def run_scheduled_deletion(
    job_id: int, payload: CandidateFileOpJobPayload
) -> dict[str, Any]:
    """Called while holding the candidate workflow lock, including finalization."""
    from backend.jobs.candidate_file_ops import _run_candidate_file_op_job_unlocked

    candidate_id = None
    owns_execution = False
    token = executing_schedule_id.set(payload.scheduled_deletion_id)
    result: dict[str, Any] = {
        "operation": "delete",
        "processed": 1,
        "succeeded": 0,
        "failed": 0,
    }
    state, error = "blocked", None
    try:
        async with async_db() as db:
            schedule = await db.get(ScheduledDeletion, payload.scheduled_deletion_id)
            # Stale/replaced jobs must never execute the current intent.
            if (
                schedule is None
                or schedule.job_id != job_id
                or schedule.state != "scheduled"
            ):
                return {**result, "processed": 0, "skipped": True}
            owns_execution = True
            if ensure_utc(schedule.scheduled_at) > datetime.now(UTC):
                raise RuntimeError("Scheduled deletion was invoked before its due time")
            user = (
                await db.get(User, schedule.created_by_user_id)
                if schedule.created_by_user_id
                else None
            )
            if user is None:
                raise HTTPException(
                    409, "The user who scheduled this deletion no longer exists"
                )
            require_delete_permission(user)
            candidate = await resolve_target(db, schedule.target)
            candidate.matched_criteria = {"scheduled_deletion_id": schedule.id}
            blockers = await candidate_deletion_blockers(db, candidate)
            if blockers:
                raise HTTPException(409, "Deletion blocked: " + ", ".join(blockers))
            db.add(candidate)
            await db.flush()
            candidate_id = candidate.id
            target = schedule.target
            item = CandidateFileOpJobItem(
                candidate_id=candidate.id,
                media_type=candidate.media_type,
                scope=target["scope"],
                title=target["title"],
                year=target["year"],
                tmdb_id=target["tmdb_id"],
                display_label=target["title"],
                season_number=target.get("season_number"),
                episode_number=target.get("episode_number"),
            )
            schedule.state = "running"
            await db.commit()
        result = await _run_candidate_file_op_job_unlocked(
            job_id,
            payload.model_copy(
                update={
                    "candidate_ids": [candidate_id],
                    "item_details": [item],
                }
            ),
        )
        if result.get("deferred"):
            state, error = (
                "blocked",
                "; ".join(
                    result.get("deferrals")
                    or ["Deletion deferred by playback protection"]
                ),
            )
        elif result.get("failed"):
            state, error = (
                "failed",
                "; ".join(
                    result.get("errors")
                    or ["Deletion failed; review History before retrying"]
                ),
            )
        else:
            state = "completed"
    except HTTPException as exc:
        error = str(exc.detail)
        result.update(failed=1, errors=[error])
    except Exception as exc:
        state, error = "failed", describe_exception(exc)
        result.update(failed=1, errors=[error])
    finally:
        executing_schedule_id.reset(token)
        async with async_db() as db:
            if candidate_id is not None:
                # The schedule owns the intent; a temporary execution candidate
                # must never become an independent automatic cleanup candidate.
                await db.execute(
                    delete(ReclaimCandidate).where(
                        ReclaimCandidate.id == candidate_id,
                        ReclaimCandidate.matched_criteria[
                            "scheduled_deletion_id"
                        ].as_integer()
                        == payload.scheduled_deletion_id,
                    )
                )
            schedule = await db.get(ScheduledDeletion, payload.scheduled_deletion_id)
            if (
                owns_execution
                and schedule is not None
                and schedule.job_id == job_id
                and schedule.state in ("scheduled", "running")
            ):
                schedule.state, schedule.last_error = state, error
                if state == "completed":
                    schedule.completed_at = datetime.now(UTC)
            await db.commit()
    return result
