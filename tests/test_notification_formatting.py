from __future__ import annotations

from typing import Any

import apprise

from backend.database.models import NotificationSetting
from backend.enums import NotificationType
from backend.services.notifications import (
    _compose_body,
    _format_bytes,
    _notify_type_for,
    _truncate_body,
)


def _setting(preferences: dict[str, Any] | None = None) -> NotificationSetting:
    return NotificationSetting(
        user_id=1, enabled=True, url="json://localhost", preferences=preferences or {}
    )


def _compose(
    notification_type: NotificationType,
    *,
    title: str = "Reclaimerr",
    message: str = "Something happened",
    context: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
) -> tuple[str, str]:
    composed_title, body, _ = _compose_body(
        notification_type=notification_type,
        setting=_setting(preferences),
        fallback_title=title,
        fallback_message=message,
        context=context,
    )
    return composed_title, body


def _cleanup_context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "created_count": 3,
        "total_reclaimable_bytes": 9_019_431_936,
        "candidates": [
            {
                "media_type": "movie",
                "media_title": "The Matrix",
                "media_year": 1999,
                "estimated_space_bytes": 5_476_083_302,
                "version_file_name": "The.Matrix.1999_x265-GRP.mkv",
                "reason_tokens": ["Watched 90+ days ago", "Not requested"],
            },
            {
                "media_type": "series",
                "media_title": "Breaking Bad",
                "media_year": 2008,
                "season_number": 2,
                "estimated_space_bytes": 3_543_348_634,
            },
        ],
    }
    context.update(overrides)
    return context


def test_severity_drives_apprise_notify_type() -> None:
    assert (
        _notify_type_for(NotificationType.REQUEST_APPROVED)
        is apprise.NotifyType.SUCCESS
    )
    assert (
        _notify_type_for(NotificationType.REQUEST_DECLINED)
        is apprise.NotifyType.WARNING
    )
    assert _notify_type_for(NotificationType.TASK_FAILURE) is apprise.NotifyType.FAILURE
    assert (
        _notify_type_for(NotificationType.DELETE_REQUEST_EXECUTION_FAILED)
        is apprise.NotifyType.FAILURE
    )
    assert (
        _notify_type_for(NotificationType.NEW_CLEANUP_CANDIDATES)
        is apprise.NotifyType.INFO
    )


def test_title_carries_the_subject() -> None:
    title, _ = _compose(
        NotificationType.ADMIN_NEW_DELETE_REQUEST,
        title="New Delete Request",
        context={"media_title": "Breaking Bad"},
    )
    assert title == "New Delete Request: Breaking Bad"


def test_title_does_not_repeat_a_subject_already_present() -> None:
    title, _ = _compose(
        NotificationType.REQUEST_APPROVED,
        title="Approved: Breaking Bad",
        context={"media_title": "Breaking Bad"},
    )
    assert title == "Approved: Breaking Bad"


def test_title_falls_back_when_no_subject_is_known() -> None:
    title, _ = _compose(NotificationType.ADMIN_MESSAGE, title="Heads up", context={})
    assert title == "Heads up"


def test_cleanup_title_stays_a_summary() -> None:
    title, _ = _compose(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        title="New Cleanup Candidates Found",
        context=_cleanup_context(),
    )
    assert title == "New Cleanup Candidates Found"


def test_request_body_renders_labelled_fields() -> None:
    _, body = _compose(
        NotificationType.ADMIN_NEW_DELETE_REQUEST,
        title="New Delete Request",
        message="jessie requested deletion for Breaking Bad",
        context={
            "actor": "jessie",
            "media_title": "Breaking Bad",
            "media_year": 2008,
            "media_type": "series",
            "scope": "Season 2",
            "request_id": 42,
            "request_type": "Deletion",
            "reason": "Freeing space",
        },
    )
    assert body.startswith("jessie requested deletion for Breaking Bad\n\n")
    assert "- **Media:** Breaking Bad (2008)" in body
    assert "- **Type:** Series" in body
    assert "- **Scope:** Season 2" in body
    assert "- **Requested by:** jessie" in body
    assert "- **Request:** #42 · Deletion" in body
    assert "- **Reason:** Freeing space" in body
    # empty context values must not leave dangling labels behind
    assert "Admin notes" not in body


def test_compact_detail_keeps_only_the_lead() -> None:
    _, body = _compose(
        NotificationType.REQUEST_APPROVED,
        message="Your request was approved",
        context={"media_title": "The Matrix", "reason": "Freeing space"},
        preferences={NotificationType.REQUEST_APPROVED.value: {"detail": "compact"}},
    )
    assert body == "Your request was approved"


def test_errors_render_inside_a_fenced_block() -> None:
    _, body = _compose(
        NotificationType.DELETE_REQUEST_EXECUTION_FAILED,
        title="Deletion Failed",
        message="Deletion failed for The Matrix",
        context={"media_title": "The Matrix", "error": "Radarr returned 404 for id_55"},
    )
    assert body.endswith("**Error**\n```\nRadarr returned 404 for id_55\n```")


def test_task_failure_body_is_the_error_alone() -> None:
    title, body = _compose(
        NotificationType.TASK_FAILURE,
        title="Task Failed",
        message="Task Sync Media failed",
        context={"task_name": "Sync Media", "error_message": "boom"},
    )
    assert title == "Task Failed: Sync Media"
    assert body == "Task Sync Media failed\n\n**Error**\n```\nboom\n```"


def test_cleanup_body_groups_by_media_type() -> None:
    _, body = _compose(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        title="New Cleanup Candidates Found",
        context=_cleanup_context(),
    )
    assert body.startswith("**3** new cleanup candidates · **8.4 GB** reclaimable")
    assert "**Movies**" in body
    assert "**Series**" in body
    assert "- The Matrix (1999) · `The.Matrix.1999_x265-GRP.mkv` · 5.1 GB" in body
    assert "- Breaking Bad (2008) · Season 2 · 3.3 GB" in body
    # reasons stay out of the default summary view
    assert "Watched 90+ days ago" not in body


def test_cleanup_body_omits_headings_for_a_single_media_type() -> None:
    context = _cleanup_context()
    context["candidates"] = [context["candidates"][0]]
    context["created_count"] = 1
    _, body = _compose(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        title="New Cleanup Candidates Found",
        context=context,
    )
    assert body.startswith("**1** new cleanup candidate ·")
    assert "**Movies**" not in body


def test_cleanup_body_appends_reasons_when_requested() -> None:
    _, body = _compose(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        title="New Cleanup Candidates Found",
        context=_cleanup_context(),
        preferences={
            NotificationType.NEW_CLEANUP_CANDIDATES.value: {
                "detail": "top_n_with_reasons",
                "max_items": 5,
            }
        },
    )
    assert "— Watched 90+ days ago, Not requested" in body


def test_cleanup_body_reports_the_remainder() -> None:
    context = _cleanup_context(created_count=9)
    context["candidates"] = context["candidates"] * 5
    _, body = _compose(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        title="New Cleanup Candidates Found",
        context=context,
        preferences={
            NotificationType.NEW_CLEANUP_CANDIDATES.value: {
                "detail": "top_n_summary",
                "max_items": 4,
            }
        },
    )
    assert body.endswith("_and 6 more_")


def test_cleanup_count_only_detail() -> None:
    _, body = _compose(
        NotificationType.NEW_CLEANUP_CANDIDATES,
        title="New Cleanup Candidates Found",
        context=_cleanup_context(),
        preferences={
            NotificationType.NEW_CLEANUP_CANDIDATES.value: {"detail": "count_only"}
        },
    )
    assert body == "**3** new cleanup candidates."


def test_truncate_body_cuts_on_a_line_boundary() -> None:
    body = "\n".join(f"- line {i}" for i in range(100))
    truncated = _truncate_body(body, max_chars=120)
    assert truncated.endswith("[message truncated]")
    assert "- line 0" in truncated
    assert len(truncated) < len(body)


def test_format_bytes() -> None:
    assert _format_bytes(None) == "0 B"
    assert _format_bytes(0) == "0 B"
    assert _format_bytes(512) == "512 B"
    assert _format_bytes(1024) == "1.0 KB"
    assert _format_bytes(5_476_083_302) == "5.1 GB"
