from __future__ import annotations

from backend.database.models import NotificationSetting
from backend.enums import NotificationType
from backend.services.notifications import (
    _compose_notification,
    _notification_link,
)

APP_URL = "https://reclaimerr.example.com"


def _setting() -> NotificationSetting:
    return NotificationSetting(
        user_id=1, enabled=True, url="json://localhost", preferences={}
    )


def test_link_targets_per_notification_type() -> None:
    assert _notification_link(NotificationType.NEW_CLEANUP_CANDIDATES, APP_URL) == (
        f"[View cleanup candidates]({APP_URL}/#/candidates)"
    )
    assert _notification_link(NotificationType.REQUEST_APPROVED, APP_URL) == (
        f"[View your requests]({APP_URL}/#/requests)"
    )
    assert _notification_link(NotificationType.ADMIN_NEW_DELETE_REQUEST, APP_URL) == (
        f"[View requests]({APP_URL}/#/requests)"
    )
    assert _notification_link(NotificationType.TASK_FAILURE, APP_URL) == (
        f"[Open Reclaimerr]({APP_URL}/#/)"
    )


def test_link_omitted_when_application_url_unset() -> None:
    for value in (None, "", "   "):
        assert (
            _notification_link(NotificationType.NEW_CLEANUP_CANDIDATES, value) is None
        )


def test_compose_appends_link_to_body() -> None:
    title, message, _ = _compose_notification(
        notification_type=NotificationType.REQUEST_APPROVED,
        setting=_setting(),
        fallback_title="Request approved",
        fallback_message="Your request was approved",
        context={"media_title": "Example Movie"},
        application_url=APP_URL,
    )

    assert title == "Request approved"
    assert message.startswith("Your request was approved")
    assert "Media: Example Movie" in message
    assert message.endswith(f"\n\n[View your requests]({APP_URL}/#/requests)")


def test_compose_body_unchanged_without_application_url() -> None:
    kwargs = {
        "notification_type": NotificationType.NEW_CLEANUP_CANDIDATES,
        "setting": _setting(),
        "fallback_title": "New cleanup candidates",
        "fallback_message": "3 new cleanup candidate(s).",
        "context": {
            "created_count": 3,
            "total_reclaimable_bytes": 1024,
            "candidates": [
                {"media_title": "Example", "media_year": 2020, "media_type": "movie"}
            ],
        },
    }

    _, without_link, _ = _compose_notification(**kwargs, application_url=None)
    _, with_link, _ = _compose_notification(**kwargs, application_url=APP_URL)

    assert "http" not in without_link
    assert with_link == (
        f"{without_link}\n\n[View cleanup candidates]({APP_URL}/#/candidates)"
    )
