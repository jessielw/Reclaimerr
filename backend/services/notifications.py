from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
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
from backend.enums import LogLevel, NotificationChannel, NotificationType, UserRole
from backend.models.settings import normalize_notification_preferences
from backend.services.admin_notices import create_event_notice
from backend.services.smtp import SmtpConfig, build_mailto_url, load_smtp_config

__all__ = [
    "build_cleanup_notification_context",
    "test_system_email",
    "notify_task_failure",
    "notify_user",
    "notify_users",
    "notify_all_users",
    "notify_admins",
    "request_scope_label",
]

_DEFAULT_BODY_FORMAT = apprise.NotifyFormat.MARKDOWN

# Separates facts on a single line, e.g. "The Matrix (1999) · Season 2 · 8.4 GB".
_SEPARATOR = " · "

# Bodies stay under the tightest transport limit (Discord embeds cap at 4096)
# so a long candidate list arrives as one message instead of several fragments.
_MAX_BODY_CHARS = 3500
_MAX_ERROR_CHARS = 1500
_MAX_TITLE_SUBJECT_CHARS = 80
_MAX_REASON_TOKENS = 3

# Apprise maps the notify type onto each transport's own severity styling,
# which is what colours a Discord embed or picks an ntfy/Gotify priority.
_NOTIFY_TYPES: dict[NotificationType, apprise.NotifyType] = {
    NotificationType.NEW_CLEANUP_CANDIDATES: apprise.NotifyType.INFO,
    NotificationType.REQUEST_APPROVED: apprise.NotifyType.SUCCESS,
    NotificationType.REQUEST_DECLINED: apprise.NotifyType.WARNING,
    NotificationType.ADMIN_MESSAGE: apprise.NotifyType.WARNING,
    NotificationType.TASK_FAILURE: apprise.NotifyType.FAILURE,
    NotificationType.ADMIN_NEW_DELETE_REQUEST: apprise.NotifyType.INFO,
    NotificationType.ADMIN_NEW_PROTECTION_REQUEST: apprise.NotifyType.INFO,
    NotificationType.ADMIN_REQUEST_CANCELLED: apprise.NotifyType.WARNING,
    NotificationType.ADMIN_DELETE_EXECUTION_FAILED: apprise.NotifyType.FAILURE,
    NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED: apprise.NotifyType.SUCCESS,
    NotificationType.DELETE_REQUEST_EXECUTION_FAILED: apprise.NotifyType.FAILURE,
}

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


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """Return the singular or plural noun for a count."""
    return singular if count == 1 else (plural or f"{singular}s")


def _humanize(value: Any) -> str:
    """Turn an enum-ish value such as "movie_version" into "Movie version"."""
    text = str(value or "").strip().replace("_", " ")
    return text[:1].upper() + text[1:] if text else ""


def _format_bytes(value: int | None) -> str:
    """Format a byte value into a readable string."""
    if not value or value <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx > 0 else f"{int(size)} {units[idx]}"


def _code(value: str) -> str:
    """Wrap a value in an inline code span.

    File names carry underscores and asterisks that markdown would otherwise
    swallow as emphasis, so anything verbatim goes inside a span.
    """
    text = value.replace("`", "'")
    return f"`{text}`"


def _code_block(value: str, *, max_chars: int = _MAX_ERROR_CHARS) -> list[str]:
    """Render an error blob as a fenced block so markdown leaves it alone."""
    text = value.strip().replace("```", "'''")
    if len(text) > max_chars:
        text = f"{text[:max_chars].rstrip()}\n[truncated]"
    return ["```", text, "```"]


def _field_lines(fields: Iterable[tuple[str, Any]]) -> list[str]:
    """Render label/value pairs as a markdown list, skipping empty values.

    Only the label is emphasized; values stay verbatim so titles and paths
    containing markdown punctuation cannot break the rest of the body.
    """
    lines: list[str] = []
    for label, value in fields:
        text = str(value).strip() if value is not None else ""
        if text:
            lines.append(f"- **{label}:** {text}")
    return lines


def _media_label(title: Any, year: Any = None) -> str:
    """Return "Title (Year)" when a year is known, otherwise just the title."""
    text = str(title or "").strip()
    if not text:
        return ""
    return f"{text} ({year})" if isinstance(year, int) else text


def _request_label(request_id: Any, request_type: Any) -> str:
    """Combine the request id and kind into a single readable field value."""
    parts: list[str] = []
    if request_id is not None and str(request_id).strip():
        parts.append(f"#{request_id}")
    kind = str(request_type or "").strip()
    if kind:
        parts.append(kind)
    return _SEPARATOR.join(parts)


def _body(lead: str, fields: list[str], error: Any = None) -> str:
    """Assemble a lead sentence, a field list, and an optional error block."""
    blocks: list[str] = []
    if lead:
        blocks.append(lead)
    if fields:
        blocks.append("\n".join(fields))
    error_text = str(error).strip() if error is not None else ""
    if error_text:
        blocks.append("\n".join(["**Error**", *_code_block(error_text)]))
    return "\n\n".join(blocks)


def _truncate_body(body: str, *, max_chars: int = _MAX_BODY_CHARS) -> str:
    """Trim an over-long body on a line boundary so transports do not split it."""
    if len(body) <= max_chars:
        return body
    kept = body[:max_chars]
    cut = kept.rfind("\n")
    if cut > max_chars // 2:
        kept = kept[:cut]
    return f"{kept.rstrip()}\n\n[message truncated]"


def _notify_type_for(notification_type: NotificationType) -> apprise.NotifyType:
    """Return the Apprise severity, which drives colour on rich transports."""
    return _NOTIFY_TYPES.get(notification_type, apprise.NotifyType.INFO)


def _compose_title(
    notification_type: NotificationType,
    fallback_title: str,
    context: dict[str, Any],
) -> str:
    """Append the notification's subject to the title.

    Push transports show the title alone on a lock screen, so naming the media
    or task there is what makes an alert readable without opening it.
    """
    title = fallback_title.strip() or "Reclaimerr"
    if notification_type is NotificationType.NEW_CLEANUP_CANDIDATES:
        return title
    if notification_type is NotificationType.TASK_FAILURE:
        subject = str(context.get("task_name") or "").strip()
    else:
        subject = str(context.get("media_title") or "").strip()
    if not subject or subject.lower() in title.lower():
        return title
    if len(subject) > _MAX_TITLE_SUBJECT_CHARS:
        subject = f"{subject[: _MAX_TITLE_SUBJECT_CHARS - 1].rstrip()}…"
    return f"{title}: {subject}"


def _format_cleanup_candidate_line(
    candidate: dict[str, Any], *, include_reason: bool = False
) -> str:
    """Format a single cleanup candidate as one scannable bullet."""
    media_type = str(candidate.get("media_type") or "").lower()
    season_number = candidate.get("season_number")
    episode_number = candidate.get("episode_number")
    version_file_name = str(candidate.get("version_file_name") or "").strip()

    parts = [
        _media_label(
            candidate.get("media_title") or "Unknown", candidate.get("media_year")
        )
    ]
    if media_type == "series" and isinstance(season_number, int):
        if isinstance(episode_number, int):
            parts.append(f"S{season_number:02d}E{episode_number:02d}")
        else:
            parts.append(f"Season {season_number}")
    elif version_file_name:
        parts.append(_code(version_file_name))
    parts.append(_format_bytes(int(candidate.get("estimated_space_bytes") or 0)))

    line = f"- {_SEPARATOR.join(parts)}"
    if include_reason:
        reasons = candidate.get("reason_tokens")
        if isinstance(reasons, list):
            tokens = [
                str(reason).strip()
                for reason in reasons[:_MAX_REASON_TOKENS]
                if str(reason).strip()
            ]
            if tokens:
                line += f" — {', '.join(tokens)}"
    return line


def _cleanup_candidate_lines(
    candidates: list[dict[str, Any]], *, include_reasons: bool
) -> list[str]:
    """Group the shown candidates by media type so the list reads in blocks."""
    grouped: dict[str, list[dict[str, Any]]] = {"Movies": [], "Series": [], "Other": []}
    for candidate in candidates:
        media_type = str(candidate.get("media_type") or "").lower()
        key = (
            "Movies"
            if media_type == "movie"
            else "Series"
            if media_type == "series"
            else "Other"
        )
        grouped[key].append(candidate)

    populated = [(label, items) for label, items in grouped.items() if items]
    # a lone group needs no heading; the bullets already read as one list
    show_headings = len(populated) > 1

    lines: list[str] = []
    for label, items in populated:
        if show_headings:
            if lines:
                lines.append("")
            lines.append(f"**{label}**")
        lines.extend(
            _format_cleanup_candidate_line(item, include_reason=include_reasons)
            for item in items
        )
    return lines


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
    message = _truncate_body(message)
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
    """Compose the title and markdown body for a notification.

    Every body follows the same shape: a lead sentence, an optional list of
    labelled fields, then any error output inside a fenced block.
    """
    preferences = normalize_notification_preferences(setting.preferences)
    context = context or {}
    pref = preferences.get(notification_type.value, {})
    detail = str(pref.get("detail") or "").lower()
    title = _compose_title(notification_type, fallback_title, context)
    lead = fallback_message.strip()

    if notification_type is NotificationType.NEW_CLEANUP_CANDIDATES:
        count = int(context.get("created_count") or 0)
        total_bytes = int(context.get("total_reclaimable_bytes") or 0)
        candidates = context.get("candidates")
        candidates = candidates if isinstance(candidates, list) else []
        noun = _plural(count, "cleanup candidate")
        if detail == "count_only":
            return title, f"**{count}** new {noun}.", _DEFAULT_BODY_FORMAT

        max_items = int(pref.get("max_items") or 5)
        max_items = min(max(max_items, 1), 20)
        shown = [item for item in candidates[:max_items] if isinstance(item, dict)]
        extra = max(0, len(candidates) - len(shown))
        lines = [
            f"**{count}** new {noun}{_SEPARATOR}"
            f"**{_format_bytes(total_bytes)}** reclaimable"
        ]
        if shown:
            lines.append("")
            lines.extend(
                _cleanup_candidate_lines(
                    shown, include_reasons=detail == "top_n_with_reasons"
                )
            )
            if extra > 0:
                lines.append("")
                lines.append(f"_and {extra} more_")
        return title, "\n".join(lines), _DEFAULT_BODY_FORMAT

    if detail == "compact":
        return title, lead, _DEFAULT_BODY_FORMAT

    if notification_type in {
        NotificationType.REQUEST_APPROVED,
        NotificationType.REQUEST_DECLINED,
        NotificationType.ADMIN_NEW_DELETE_REQUEST,
        NotificationType.ADMIN_NEW_PROTECTION_REQUEST,
        NotificationType.ADMIN_REQUEST_CANCELLED,
        NotificationType.ADMIN_DELETE_EXECUTION_FAILED,
        NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
        NotificationType.DELETE_REQUEST_EXECUTION_FAILED,
    }:
        fields = _field_lines(
            (
                (
                    "Media",
                    _media_label(context.get("media_title"), context.get("media_year")),
                ),
                ("Type", _humanize(context.get("media_type"))),
                ("Scope", context.get("scope")),
                ("Requested by", context.get("actor")),
                (
                    "Request",
                    _request_label(
                        context.get("request_id"), context.get("request_type")
                    ),
                ),
                ("Reason", context.get("reason")),
                ("Admin notes", context.get("admin_notes")),
            )
        )
        return title, _body(lead, fields, context.get("error")), _DEFAULT_BODY_FORMAT

    if notification_type is NotificationType.ADMIN_MESSAGE:
        fields = _field_lines(
            (
                ("By", context.get("actor")),
                (
                    "Media",
                    _media_label(context.get("media_title"), context.get("media_year")),
                ),
                ("Reason", context.get("reason")),
            )
        )
        return title, _body(lead, fields, context.get("error")), _DEFAULT_BODY_FORMAT

    if notification_type is NotificationType.TASK_FAILURE:
        # the title already names the task, so only the error adds anything
        error = str(context.get("error_message") or "").strip()
        if error:
            return title, _body(lead, [], error), _DEFAULT_BODY_FORMAT

    return title, lead, _DEFAULT_BODY_FORMAT


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
    notify_type: apprise.NotifyType = apprise.NotifyType.INFO,
    log_label: str | None = None,
) -> bool:
    """Send a single notification to a specific URL with automatic retry logic.

    `log_label` names the destination in the log instead of the URL. A system
    email URL carries the SMTP password in its userinfo, so it must never be
    logged verbatim; callers pass a label and everything else falls back to the
    URL, which is what the user typed in themselves.
    """
    ap = apprise.Apprise()
    ap.add(url)
    label = log_label or url

    try:
        result = await ap.async_notify(
            body=message,
            title=title,
            body_format=body_format,
            notify_type=notify_type,
        )
        if not result:
            LOG.warning(f"Apprise returned False for notification to {label}")
        return bool(result)
    except Exception as e:
        LOG.error(f"Failed to send notification to {label}: {e}")
        raise


def _resolve_destination(
    setting: NotificationSetting,
    user: User | None,
    smtp: SmtpConfig | None,
) -> tuple[str, str] | None:
    """Resolve a setting into the (url, log_label) pair used to deliver it.

    Returns None when the destination cannot be delivered to at all. That is a
    skip rather than a failure: an admin who has not configured SMTP, or a user
    with no email address on file, should not register as a failed send.
    """
    if setting.channel == NotificationChannel.SYSTEM_EMAIL:
        if smtp is None:
            LOG.debug(
                f"Skipping email destination {setting.id}: system SMTP is not "
                "configured or is incomplete"
            )
            return None
        recipient = (
            setting.target_email or (user.email if user else None) or ""
        ).strip()
        if not recipient:
            LOG.warning(
                f"Skipping email destination {setting.id}: no recipient address "
                "on the destination or the account"
            )
            return None
        return build_mailto_url(smtp, recipient), f"system email to {recipient}"

    url = (setting.url or "").strip()
    if not url:
        LOG.warning(f"Skipping notification destination {setting.id}: no URL set")
        return None
    return url, setting.name or url


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
            # the owning user is needed for the account email a system email
            # destination falls back to; the relationship is lazy="noload"
            select(NotificationSetting)
            .where(
                NotificationSetting.user_id == user_id,
                NotificationSetting.enabled == True,
            )
            .options(selectinload(NotificationSetting.user))
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
        smtp = await load_smtp_config(session)

        for setting in eligible_settings:
            destination = _resolve_destination(setting, setting.user, smtp)
            if destination is None:
                continue
            url, log_label = destination

            composed_title, composed_message, composed_format = _compose_notification(
                notification_type=notification_type,
                setting=setting,
                fallback_title=title,
                fallback_message=message,
                context=context,
                application_url=application_url,
            )
            success = await send_notification(
                url=url,
                title=composed_title,
                message=composed_message,
                body_format=composed_format or body_format,
                notify_type=_notify_type_for(notification_type),
                log_label=log_label,
            )

            if success:
                results["sent"] += 1
                LOG.info(
                    f"Sent {notification_type} notification to user {user_id} via {log_label}"
                )
            else:
                results["failed"] += 1
                LOG.warning(
                    f"Failed to send {notification_type} notification to user {user_id} via {log_label}"
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
        smtp = await load_smtp_config(session)

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
            destination = _resolve_destination(setting, user, smtp)
            if destination is None:
                continue
            url, log_label = destination

            composed_title, composed_message, composed_format = _compose_notification(
                notification_type=notification_type,
                setting=setting,
                fallback_title=title,
                fallback_message=message,
                context=context,
                application_url=application_url,
            )
            success = await send_notification(
                url=url,
                title=composed_title,
                message=composed_message,
                body_format=composed_format or body_format,
                notify_type=_notify_type_for(notification_type),
                log_label=log_label,
            )

            if success:
                results["sent"] += 1
                LOG.info(
                    f"Sent {notification_type} notification to user {user.id} via {log_label}"
                )
            else:
                results["failed"] += 1
                LOG.warning(
                    f"Failed to send {notification_type} notification to user {user.id} via {log_label}"
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
    log_label: str | None = None,
) -> tuple[bool, str | None]:
    """Test a notification by sending a test payload to the provided URL."""
    label = log_label or url
    try:
        return await send_notification(
            url=url,
            title="Reclaimerr test notification",
            message=_body(
                "Your notification settings are working correctly.",
                _field_lines(
                    (
                        ("Source", "Reclaimerr"),
                        ("Delivered", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")),
                    )
                ),
            ),
            body_format=_DEFAULT_BODY_FORMAT,
            notify_type=apprise.NotifyType.SUCCESS,
            log_label=log_label,
        ), None
    except RetryError:
        LOG.error(f"Failed to send notification after multiple attempts to {label}")
        return False, (
            "Failed to send test notification after multiple attempts, check your "
            "connection/credentials and try again"
        )
    except Exception as e:
        LOG.error(f"Unhandled Error testing notification URL {label}: {e}")
        return False, str(e)


async def test_system_email(
    smtp: SmtpConfig,
    recipient: str,
) -> tuple[bool, str | None]:
    """Send a test email through the instance SMTP server."""
    return await test_notification_url(
        build_mailto_url(smtp, recipient),
        log_label=f"system email to {recipient}",
    )
