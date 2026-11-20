from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from tenacity import stop_after_attempt, wait_none

from backend.enums import NotificationType, UserRole
from backend.services import notifications


def _user(**overrides):
    values = {
        "username": "alex",
        "display_name": "Alex",
        "email": "alex@example.com",
        "role": UserRole.USER,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_event_payload_is_json_safe_and_names_the_recipient():
    payload = notifications.build_event_payload(
        notification_type=NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
        context={
            "request_id": 7,
            "scheduled_at": datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
            "media_type": NotificationType.TASK_FAILURE,  # any enum
            "nested": {"values": (1, 2)},
        },
        application_url="https://reclaim.example.com/",
        recipient=_user(),
    )

    # must survive a round trip through JSON untouched
    assert json.loads(json.dumps(payload)) == payload
    assert payload["version"] == 1
    assert payload["event"] == "delete_request_execution_succeeded"
    assert payload["url"] == "https://reclaim.example.com/#/requests"
    assert payload["recipient"] == {
        "username": "alex",
        "display_name": "Alex",
        "email": "alex@example.com",
    }
    assert payload["context"]["request_id"] == 7
    assert payload["context"]["scheduled_at"] == "2026-01-02T03:04:00+00:00"
    assert payload["context"]["nested"] == {"values": [1, 2]}


def test_event_payload_without_application_url_or_recipient():
    payload = notifications.build_event_payload(
        notification_type=NotificationType.REQUEST_APPROVED,
        context=None,
        application_url=None,
        recipient=None,
    )

    assert payload["url"] is None
    assert payload["recipient"] is None
    assert payload["context"] == {}


def test_json_destination_receives_the_structured_payload(monkeypatch):
    """The Apprise json:// plugin posts our payload next to its own fields."""
    import apprise.plugins.custom_json as custom_json

    posted: list[dict] = []

    class Response:
        status_code = 200
        content = b""

    def fake_request(method, url, data=None, **kwargs):
        posted.append({"url": url, "body": json.loads(data)})
        return Response()

    monkeypatch.setattr(custom_json.requests, "request", fake_request)
    send = notifications.send_notification.retry_with(
        stop=stop_after_attempt(1), wait=wait_none()
    )
    event = notifications.build_event_payload(
        notification_type=NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED,
        context={"media_title": "The Matrix", "media_size_bytes": 5},
        application_url=None,
        recipient=_user(),
    )

    ok = asyncio.run(
        send(
            url="json://hooks.example.com/reclaimerr",
            title="Deletion Completed",
            message="Deletion completed for The Matrix",
            log_label="test webhook",
            event=event,
        )
    )

    assert ok is True
    assert len(posted) == 1
    body = posted[0]["body"]
    # the standard Apprise fields are still there
    assert body["title"] == "Deletion Completed"
    assert body["message"] == "Deletion completed for The Matrix"
    assert body["reclaimerr"]["event"] == "delete_request_execution_succeeded"
    assert body["reclaimerr"]["context"]["media_title"] == "The Matrix"
    assert body["reclaimerr"]["recipient"]["email"] == "alex@example.com"


def test_json_destination_without_an_event_is_unchanged(monkeypatch):
    import apprise.plugins.custom_json as custom_json

    posted: list[dict] = []

    class Response:
        status_code = 200
        content = b""

    monkeypatch.setattr(
        custom_json.requests,
        "request",
        lambda method, url, data=None, **kwargs: (
            posted.append(json.loads(data)) or Response()
        ),
    )
    send = notifications.send_notification.retry_with(
        stop=stop_after_attempt(1), wait=wait_none()
    )

    asyncio.run(
        send(
            url="json://hooks.example.com/reclaimerr",
            title="t",
            message="m",
            log_label="test webhook",
        )
    )

    assert "reclaimerr" not in posted[0]
