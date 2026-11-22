"""Tell the person who scheduled a deletion how it went.

A delete request already notifies its requester when it runs. A deletion that
someone scheduled directly has no request, so without this nobody hears about
it, not even the person who scheduled it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import Movie, ScheduledDeletion, Series
from backend.enums import MediaType, NotificationType
from backend.services.notifications import (
    media_event_context,
    notify_user,
    request_scope_label,
)


async def notify_schedule_creator(
    schedule_id: int, *, succeeded: bool, error: str | None = None
) -> None:
    """Email the user who scheduled a deletion, when there is one and no request.

    Never raises: delivery must not change the recorded outcome of a deletion.
    """
    try:
        async with async_db() as db:
            schedule = await db.get(ScheduledDeletion, schedule_id)
            if (
                schedule is None
                or schedule.delete_request_id is not None
                or schedule.created_by_user_id is None
            ):
                return
            target: dict[str, Any] = schedule.target or {}
            media = None
            media_id = target.get("media_id")
            if isinstance(media_id, int):
                model = Movie if target.get("media_type") == MediaType.MOVIE else Series
                media = await db.get(model, media_id)
            context = {
                "request_type": "Deletion",
                "media_title": target.get("title") or "Unknown media",
                "media_year": target.get("year"),
                "media_type": target.get("media_type"),
                "scope": request_scope_label(
                    target.get("scope"),
                    target.get("season_number"),
                    target.get("episode_number"),
                ),
                "reason": "Scheduled deletion",
                "error": None if succeeded else (error or schedule.last_error),
                "requested_at": schedule.created_at,
                "executed_at": schedule.completed_at or datetime.now(UTC),
                # the title may already be gone from the library by now
                "media_size_bytes": target.get("estimated_space_bytes"),
                "tmdb_id": target.get("tmdb_id"),
                **{k: v for k, v in media_event_context(media).items() if v},
            }
            user_id = schedule.created_by_user_id

        title = context["media_title"]
        if succeeded:
            await notify_user(
                user_id,
                NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
                "Deletion Completed",
                f"Deletion completed for {title}",
                context=context,
            )
        else:
            await notify_user(
                user_id,
                NotificationType.DELETE_REQUEST_EXECUTION_FAILED,
                "Deletion Failed",
                f"Deletion failed for {title}",
                context=context,
            )
    except Exception as exc:
        LOG.error(
            f"Failed to notify the creator of scheduled deletion {schedule_id}: {exc}"
        )
