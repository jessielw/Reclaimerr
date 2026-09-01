from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import apprise
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from tenacity import (
    RetryError,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.logger import LOG
from backend.database import async_db
from backend.database.models import (
    GeneralSettings,
    Movie,
    MovieVersion,
    NotificationSetting,
    ReclaimCandidate,
    Season,
    Series,
    User,
)
from backend.enums import LogLevel, NotificationType, UserRole
from backend.models.settings import normalize_notification_preferences
from backend.services.admin_notices import create_event_notice

__all__ = [
    "build_cleanup_notification_context",
    "notify_task_failure",
    "notify_user",
    "notify_users",
    "notify_all_users",
    "notify_admins",
    "request_scope_label",
]

_DEFAULT_BODY_FORMAT = apprise.NotifyFormat.MARKDOWN

# Frontend uses hash routing, so deep links are "<application_url>/#<route>".
# Admin notices render in the sidebar rather than on their own page, so the
# notice-backed types point at the app root instead.
_NOTIFICATION_LINKS: dict[NotificationType, tuple[str, str]] = {
    NotificationType.NEW_CLEANUP_CANDIDATES: ("/candidates", "View cleanup candidates"),
    NotificationType.REQUEST_APPROVED: ("/requests", "View your requests"),
    NotificationType.REQUEST_DECLINED: ("/requests", "View your requests"),
    NotificationType.ADMIN_NEW_DELETE_REQUEST: ("/requests", "View requests"),
    NotificationType.ADMIN_NEW_PROTECTION_REQUEST: ("/requests", "View requests"),
    NotificationType.ADMIN_REQUEST_CANCELLED: ("/requests", "View requests"),
    NotificationType.ADMIN_DELETE_EXECUTION_FAILED: ("/requests", "View requests"),
    NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED: ("/requests", "View requests"),
    NotificationType.DELETE_REQUEST_EXECUTION_FAILED: ("/requests", "View requests"),
    NotificationType.ADMIN_MESSAGE: ("/", "Open Reclaimerr"),
    NotificationType.TASK_FAILURE: ("/", "Open Reclaimerr"),
}


async def _get_application_url(session: AsyncSession) -> str | None:
    """Return the configured public application URL, or None when unset."""
    result = await session.execute(select(GeneralSettings.application_url))
    application_url = result.scalars().first()
    if not application_url:
        return None
    return application_url.strip().rstrip("/") or None


def _notification_link(
    notification_type: NotificationType,
    application_url: str | None,
) -> str | None:
    """Build a markdown deep link for a notification type.

    Returns None when no public application URL is configured; every transport
    Apprise supports renders markdown links natively, so no HTML is needed.
    """
    base = (application_url or "").strip().rstrip("/")
    if not base:
        return None
    target = _NOTIFICATION_LINKS.get(notification_type)
    if not target:
        return None
    route, label = target
    return f"[{label}]({base}/#{route})"


def request_scope_label(
    target_scope: str | None,
    season_number: int | None = None,
    episode_number: int | None = None,
    episode_name: str | None = None,
) -> str:
    """Return a readable media scope for request notifications."""
    if episode_number is not None:
        label = f"S{season_number or 0:02d}E{episode_number:02d}"
        return f"{label} - {episode_name}" if episode_name else label
    if season_number is not None:
        return f"Season {season_number}"
    return (target_scope or "media").replace("_", " ").title()


def _format_bytes(value: int | None) -> str:
    """ "Format a byte value into a readable string."""
    if not value or value <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx > 0 else f"{int(size)} {units[idx]}"


def _format_cleanup_candidate_line(
    candidate: dict[str, Any], *, include_reason: bool = False
) -> str:
    """ "Format a single cleanup candidate into a readable line for notifications."""
    title = str(candidate.get("media_title") or "Unknown")
    year = candidate.get("media_year")
    media_type = str(candidate.get("media_type") or "").lower()
    season_number = candidate.get("season_number")
    episode_number = candidate.get("episode_number")
    version_file_name = str(candidate.get("version_file_name") or "").strip()
    size_label = _format_bytes(int(candidate.get("estimated_space_bytes") or 0))

    scope_parts: list[str] = []
    if media_type == "series" and isinstance(season_number, int):
        if isinstance(episode_number, int):
            scope_parts.append(f"S{season_number:02d}E{episode_number:02d}")
        else:
            scope_parts.append(f"Season {season_number}")
    elif version_file_name:
        scope_parts.append(version_file_name)

    year_part = f" ({year})" if isinstance(year, int) else ""
    scope_suffix = f" - {', '.join(scope_parts)}" if scope_parts else ""
    line = f"- {title}{year_part}{scope_suffix} - {size_label}"
    if include_reason:
        reasons = candidate.get("reason_tokens")
        if isinstance(reasons, list) and reasons:
            line += f" [{str(reasons[0])}]"
    return line


def _compose_notification(
    *,
    notification_type: NotificationType,
    setting: NotificationSetting,
    fallback_title: str,
    fallback_message: str,
    context: dict[str, Any] | None = None,
    application_url: str | None = None,
) -> tuple[str, str, apprise.NotifyFormat]:
    """Compose a notification and append a deep link back into the app."""
    title, message, body_format = _compose_body(
        notification_type=notification_type,
        setting=setting,
        fallback_title=fallback_title,
        fallback_message=fallback_message,
        context=context,
    )
    link = _notification_link(notification_type, application_url)
    if link:
        message = f"{message}\n\n{link}"
    return title, message, body_format


def _compose_body(
    *,
    notification_type: NotificationType,
    setting: NotificationSetting,
    fallback_title: str,
    fallback_message: str,
    context: dict[str, Any] | None = None,
) -> tuple[str, str, apprise.NotifyFormat]:
    """ "Compose a notification title and message based on the notification type, user preferences, and context."""
    preferences = normalize_notification_preferences(setting.preferences)
    context = context or {}
    pref = preferences.get(notification_type.value, {})
    detail = str(pref.get("detail") or "").lower()

    if notification_type is NotificationType.NEW_CLEANUP_CANDIDATES:
        count = int(context.get("created_count") or 0)
        total_bytes = int(context.get("total_reclaimable_bytes") or 0)
        candidates = context.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        if detail == "count_only":
            return (
                fallback_title,
                f"{count} new cleanup candidate(s).",
                _DEFAULT_BODY_FORMAT,
            )

        max_items = int(pref.get("max_items") or 5)
        max_items = min(max(max_items, 1), 20)
        include_reasons = detail == "top_n_with_reasons"
        top = candidates[:max_items]
        extra = max(0, len(candidates) - len(top))
        lines = [
            f"{count} new cleanup candidate(s) identified.",
            f"Estimated reclaimable size: {_format_bytes(total_bytes)}",
        ]
        if top:
            lines.append("")
            lines.append("Top candidates:")
            lines.extend(
                _format_cleanup_candidate_line(item, include_reason=include_reasons)
                for item in top
                if isinstance(item, dict)
            )
            if extra > 0:
                lines.append(f"- +{extra} more")
        return fallback_title, "\n".join(lines), _DEFAULT_BODY_FORMAT

    if notification_type in {
        NotificationType.REQUEST_APPROVED,
        NotificationType.REQUEST_DECLINED,
    }:
        if detail == "compact":
            return fallback_title, fallback_message, _DEFAULT_BODY_FORMAT
        media_title = str(context.get("media_title") or "").strip()
        media_type = str(context.get("media_type") or "").strip()
        reason = str(context.get("reason") or "").strip()
        admin_notes = str(context.get("admin_notes") or "").strip()
        lines = [fallback_message]
        if media_title:
            lines.append(f"Media: {media_title}")
        if media_type:
            lines.append(f"Type: {media_type}")
        if reason:
            lines.append(f"Reason: {reason}")
        if admin_notes:
            lines.append(f"Admin notes: {admin_notes}")
        return fallback_title, "\n".join(lines), _DEFAULT_BODY_FORMAT

    if notification_type is NotificationType.ADMIN_MESSAGE:
        if detail == "compact":
            return fallback_title, fallback_message, _DEFAULT_BODY_FORMAT
        lines = [fallback_message]
        actor = str(context.get("actor") or "").strip()
        media_title = str(context.get("media_title") or "").strip()
        reason = str(context.get("reason") or "").strip()
        if actor:
            lines.append(f"By: {actor}")
        if media_title:
            lines.append(f"Media: {media_title}")
        if reason:
            lines.append(f"Reason: {reason}")
        return fallback_title, "\n".join(lines), _DEFAULT_BODY_FORMAT

    if notification_type in {
        NotificationType.ADMIN_NEW_DELETE_REQUEST,
        NotificationType.ADMIN_NEW_PROTECTION_REQUEST,
        NotificationType.ADMIN_REQUEST_CANCELLED,
        NotificationType.ADMIN_DELETE_EXECUTION_FAILED,
        NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
        NotificationType.DELETE_REQUEST_EXECUTION_FAILED,
    }:
        if detail == "compact":
            return fallback_title, fallback_message, _DEFAULT_BODY_FORMAT
        fields = (
            ("Request ID", context.get("request_id")),
            ("Request type", context.get("request_type")),
            ("By", context.get("actor")),
            ("Media", context.get("media_title")),
            ("Type", context.get("media_type")),
            ("Scope", context.get("scope")),
            ("Reason", context.get("reason")),
            ("Admin notes", context.get("admin_notes")),
            ("Error", context.get("error")),
        )
        lines = [fallback_message]
        lines.extend(f"{label}: {value}" for label, value in fields if value)
        return fallback_title, "\n".join(lines), _DEFAULT_BODY_FORMAT

    if notification_type is NotificationType.TASK_FAILURE:
        if detail == "compact":
            return fallback_title, fallback_message, _DEFAULT_BODY_FORMAT
        task_name = str(context.get("task_name") or "").strip()
        error = str(context.get("error_message") or "").strip()
        if task_name and error:
            msg = f"Task: {task_name}\nError:\n{error[:1500]}"
            return fallback_title, msg, _DEFAULT_BODY_FORMAT

    return fallback_title, fallback_message, _DEFAULT_BODY_FORMAT


async def build_cleanup_notification_context(
    *,
    created_count: int,
    created_since: datetime,
) -> dict[str, Any]:
    """Build context payload for cleanup-candidate notifications."""
    if created_count <= 0:
        return {"created_count": 0, "total_reclaimable_bytes": 0, "candidates": []}

    # SQLite CURRENT_TIMESTAMP is second precision while Python timestamps carry
    # microseconds. We'll use a small grace window to avoid missing rows created
    # in the same second as scan start.
    window_start = (created_since - timedelta(seconds=2)).replace(microsecond=0)

    async with async_db() as session:
        rows = (
            await session.execute(
                select(
                    ReclaimCandidate.id,
                    ReclaimCandidate.media_type,
                    ReclaimCandidate.movie_id,
                    ReclaimCandidate.movie_version_id,
                    ReclaimCandidate.series_id,
                    ReclaimCandidate.season_id,
                    ReclaimCandidate.episode_id,
                    ReclaimCandidate.estimated_space_bytes,
                    ReclaimCandidate.reason_data,
                )
                .where(ReclaimCandidate.created_at >= window_start)
                .order_by(desc(ReclaimCandidate.estimated_space_bytes))
            )
        ).all()
        if not rows:
            # fallback: if time window misses due to precision drift, at least
            # summarize the latest created candidates instead of reporting 0 B.
            rows = (
                await session.execute(
                    select(
                        ReclaimCandidate.id,
                        ReclaimCandidate.media_type,
                        ReclaimCandidate.movie_id,
                        ReclaimCandidate.movie_version_id,
                        ReclaimCandidate.series_id,
                        ReclaimCandidate.season_id,
                        ReclaimCandidate.episode_id,
                        ReclaimCandidate.estimated_space_bytes,
                        ReclaimCandidate.reason_data,
                    )
                    .order_by(
                        desc(ReclaimCandidate.created_at), desc(ReclaimCandidate.id)
                    )
                    .limit(max(created_count, 1))
                )
            ).all()
            if not rows:
                return {
                    "created_count": created_count,
                    "total_reclaimable_bytes": 0,
                    "candidates": [],
                }

        movie_ids = {r.movie_id for r in rows if r.movie_id is not None}
        series_ids = {r.series_id for r in rows if r.series_id is not None}
        movie_version_ids = {
            r.movie_version_id for r in rows if r.movie_version_id is not None
        }
        season_ids = {r.season_id for r in rows if r.season_id is not None}

        movies = {
            m.id: m
            for m in (
                (await session.execute(select(Movie).where(Movie.id.in_(movie_ids))))
                .scalars()
                .all()
                if movie_ids
                else []
            )
        }
        series = {
            s.id: s
            for s in (
                (await session.execute(select(Series).where(Series.id.in_(series_ids))))
                .scalars()
                .all()
                if series_ids
                else []
            )
        }
        versions = {
            v.id: v
            for v in (
                (
                    await session.execute(
                        select(MovieVersion).where(
                            MovieVersion.id.in_(movie_version_ids)
                        )
                    )
                )
                .scalars()
                .all()
                if movie_version_ids
                else []
            )
        }
        seasons = {
            s.id: s
            for s in (
                (await session.execute(select(Season).where(Season.id.in_(season_ids))))
                .scalars()
                .all()
                if season_ids
                else []
            )
        }

        candidates: list[dict[str, Any]] = []
        total_reclaimable_bytes = 0
        for row in rows:
            season = None
            if row.media_type.value == "movie":
                m = movies.get(row.movie_id)
                v = versions.get(row.movie_version_id)
                media_title = m.title if m else "Unknown"
                media_year = m.year if m else None
                version_file_name = v.file_name if v else None
            else:
                s = series.get(row.series_id)
                season = seasons.get(row.season_id)
                media_title = s.title if s else "Unknown"
                media_year = s.year if s else None
                version_file_name = None

            size = int(row.estimated_space_bytes or 0)
            total_reclaimable_bytes += size
            reason_tokens: list[str] = []
            reason_data = row.reason_data if isinstance(row.reason_data, list) else []
            for part in reason_data:
                if isinstance(part, dict):
                    text = str(part.get("text") or "").strip()
                    if text:
                        reason_tokens.append(text)

            candidates.append(
                {
                    "media_type": row.media_type.value,
                    "media_title": media_title,
                    "media_year": media_year,
                    "season_number": season.season_number
                    if row.media_type.value == "series" and season
                    else None,
                    "episode_number": None,
                    "version_file_name": version_file_name,
                    "estimated_space_bytes": size,
                    "reason_tokens": reason_tokens,
                }
            )

        return {
            "created_count": created_count,
            "total_reclaimable_bytes": total_reclaimable_bytes,
            "candidates": candidates,
        }


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=(retry_if_exception_type(Exception) | retry_if_result(lambda x: x is False)),
    before_sleep=before_sleep_log(LOG.logger, LogLevel.WARNING.value),
    reraise=False,  # don't raise after retries exhausted, return False
)
async def send_notification(
    url: str,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = _DEFAULT_BODY_FORMAT,
) -> bool:
    """Send a single notification to a specific URL with automatic retry logic."""
    ap = apprise.Apprise()
    ap.add(url)

    try:
        result = await ap.async_notify(
            body=message,
            title=title,
            body_format=body_format,
        )
        if not result:
            LOG.warning(f"Apprise returned False for notification to {url}")
        return bool(result)
    except Exception as e:
        LOG.error(f"Failed to send notification to {url}: {e}")
        raise


async def notify_user(
    user_id: int,
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = _DEFAULT_BODY_FORMAT,
    context: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Send a notification to a specific user based on their notification preferences."""
    results = {"sent": 0, "failed": 0}

    async with async_db() as session:
        result = await session.execute(
            select(NotificationSetting).where(
                NotificationSetting.user_id == user_id,
                NotificationSetting.enabled == True,
            )
        )
        settings = result.scalars().all()

        type_field = _notification_type_to_field(notification_type)
        eligible_settings = [s for s in settings if getattr(s, type_field, False)]

        if not eligible_settings:
            LOG.debug(
                f"No notification settings enabled for user {user_id} and type {notification_type}"
            )
            return results

        application_url = await _get_application_url(session)

        for setting in eligible_settings:
            composed_title, composed_message, composed_format = _compose_notification(
                notification_type=notification_type,
                setting=setting,
                fallback_title=title,
                fallback_message=message,
                context=context,
                application_url=application_url,
            )
            success = await send_notification(
                url=setting.url,
                title=composed_title,
                message=composed_message,
                body_format=composed_format or body_format,
            )

            if success:
                results["sent"] += 1
                LOG.info(
                    f"Sent {notification_type} notification to user {user_id} via {setting.name or setting.url}"
                )
            else:
                results["failed"] += 1
                LOG.warning(
                    f"Failed to send {notification_type} notification to user {user_id} via {setting.name or setting.url}"
                )

    return results


async def notify_admins(
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = _DEFAULT_BODY_FORMAT,
    context: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Send a notification to all admin users who have this notification type enabled."""
    results = {"sent": 0, "failed": 0}

    if notification_type in {
        NotificationType.ADMIN_MESSAGE,
        NotificationType.TASK_FAILURE,
    }:
        try:
            async with async_db() as session:
                await create_event_notice(
                    session,
                    kind=f"event_{notification_type.value}",
                    severity=(
                        "error"
                        if notification_type is NotificationType.TASK_FAILURE
                        else "warning"
                    ),
                    title=title,
                    message=message,
                    context_json=context,
                )
                await session.commit()
        except Exception as e:
            LOG.error(f"Failed to persist admin in-app notice: {e}")

    async with async_db() as session:
        result = await session.execute(
            select(User).where(
                User.role == UserRole.ADMIN,
                User.is_active == True,
            )
        )
        admin_users = result.scalars().all()

    if not admin_users:
        LOG.warning("No active admin users found to send notification to")
        return results

    for user in admin_users:
        user_results = await notify_user(
            user_id=user.id,
            notification_type=notification_type,
            title=title,
            message=message,
            body_format=body_format,
            context=context,
        )
        results["sent"] += user_results["sent"]
        results["failed"] += user_results["failed"]

    return results


async def notify_users(
    user_ids: list[int],
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = _DEFAULT_BODY_FORMAT,
    context: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Send a notification to multiple specific users."""
    results = {"sent": 0, "failed": 0}

    for user_id in user_ids:
        user_results = await notify_user(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            message=message,
            body_format=body_format,
            context=context,
        )
        results["sent"] += user_results["sent"]
        results["failed"] += user_results["failed"]

    return results


async def notify_all_users(
    notification_type: NotificationType,
    title: str,
    message: str,
    body_format: apprise.NotifyFormat = _DEFAULT_BODY_FORMAT,
    context: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Send a notification to all active users who have this notification type enabled."""
    results = {"sent": 0, "failed": 0}

    async with async_db() as session:
        stmt = (
            select(User)
            .where(User.is_active == True)
            .options(selectinload(User.notification_settings))
        )
        result = await session.execute(stmt)
        users = result.scalars().all()
        application_url = await _get_application_url(session)

    if not users:
        LOG.debug("No active users found to send notification to")
        return results

    type_field = _notification_type_to_field(notification_type)

    for user in users:
        eligible_settings = [
            s
            for s in user.notification_settings
            if s.enabled and getattr(s, type_field, False)
        ]

        if not eligible_settings:
            LOG.debug(
                f"No notification settings enabled for user {user.id} and type {notification_type}"
            )
            continue

        for setting in eligible_settings:
            composed_title, composed_message, composed_format = _compose_notification(
                notification_type=notification_type,
                setting=setting,
                fallback_title=title,
                fallback_message=message,
                context=context,
                application_url=application_url,
            )
            success = await send_notification(
                url=setting.url,
                title=composed_title,
                message=composed_message,
                body_format=composed_format or body_format,
            )

            if success:
                results["sent"] += 1
                LOG.info(
                    f"Sent {notification_type} notification to user {user.id} via {setting.name or setting.url}"
                )
            else:
                results["failed"] += 1
                LOG.warning(
                    f"Failed to send {notification_type} notification to user {user.id} via {setting.name or setting.url}"
                )

    return results


def _notification_type_to_field(notification_type: NotificationType) -> str:
    mapping = {
        NotificationType.NEW_CLEANUP_CANDIDATES: "new_cleanup_candidates",
        NotificationType.REQUEST_APPROVED: "request_approved",
        NotificationType.REQUEST_DECLINED: "request_declined",
        NotificationType.ADMIN_MESSAGE: "admin_message",
        NotificationType.TASK_FAILURE: "task_failure",
        NotificationType.ADMIN_NEW_DELETE_REQUEST: "admin_new_delete_request",
        NotificationType.ADMIN_NEW_PROTECTION_REQUEST: "admin_new_protection_request",
        NotificationType.ADMIN_REQUEST_CANCELLED: "admin_request_cancelled",
        NotificationType.ADMIN_DELETE_EXECUTION_FAILED: "admin_delete_execution_failed",
        NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED: "delete_request_execution_succeeded",
        NotificationType.DELETE_REQUEST_EXECUTION_FAILED: "delete_request_execution_failed",
    }

    return mapping.get(notification_type, "")


async def notify_task_failure(
    task_name: str,
    error_message: str,
) -> dict[str, int]:
    """Send a task failure notification to all admins."""
    return await notify_admins(
        notification_type=NotificationType.TASK_FAILURE,
        title="Task Failed",
        message=f"Task {task_name} failed",
        body_format=_DEFAULT_BODY_FORMAT,
        context={"task_name": task_name, "error_message": error_message},
    )


async def test_notification_url(
    url: str,
) -> tuple[bool, str | None]:
    """Test a notification by sending a test payload to the provided URL."""
    try:
        return await send_notification(
            url=url,
            title="This is a test notification from Reclaimerr",
            message="If you received this, your notification settings are working correctly!",
            body_format=_DEFAULT_BODY_FORMAT,
        ), None
    except RetryError:
        LOG.error(f"Failed to send notification after multiple attempts to {url}")
        return False, (
            "Failed to send test notification after multiple attempts, check your "
            "connection/credentials and try again"
        )
    except Exception as e:
        LOG.error(f"Unhandled Error testing notification URL {url}: {e}")
        return False, str(e)
