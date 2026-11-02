from __future__ import annotations

import asyncio
import logging

import pytest
from tenacity import RetryError, stop_after_attempt, wait_none

from backend.database.models import NotificationSetting
from backend.enums import NotificationChannel
from backend.services import notifications
from backend.services.smtp import SmtpConfig


@pytest.fixture(
    params=[False, RuntimeError("connection refused")], ids=["false", "error"]
)
def failed_delivery(monkeypatch, request):
    class FailedApprise:
        def __init__(self, **kwargs):
            pass

        def add(self, url):
            return True

        async def async_notify(self, **kwargs):
            if isinstance(request.param, Exception):
                raise request.param
            return request.param

    monkeypatch.setattr(notifications.apprise, "Apprise", FailedApprise)
    monkeypatch.setattr(
        notifications,
        "send_notification",
        notifications.send_notification.retry_with(
            stop=stop_after_attempt(1), wait=wait_none()
        ),
    )


@pytest.mark.parametrize("label", [None, ""])
def test_failed_notification_test_does_not_log_delivery_url(
    failed_delivery, caplog, label
):
    url = "mailto://sender:SMTP_PASSWORD@smtp.example.com/?to=user@example.com"
    with caplog.at_level(logging.WARNING, logger="reclaimerr"):
        result = asyncio.run(notifications.test_notification_url(url, log_label=label))

    assert result[0] is False
    assert "notification test" in caplog.text
    assert "SMTP_PASSWORD" not in caplog.text
    assert url not in caplog.text


def test_failed_system_email_keeps_the_safe_destination_label(failed_delivery, caplog):
    config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="sender",
        password="SMTP_PASSWORD",
        from_address="sender@example.com",
        from_name="Reclaimerr",
        reply_to=None,
    )
    with caplog.at_level(logging.WARNING, logger="reclaimerr"):
        result = asyncio.run(
            notifications.test_system_email(config, "user@example.com")
        )

    assert result[0] is False
    assert "system email to user@example.com" in caplog.text
    assert "SMTP_PASSWORD" not in caplog.text
    assert "mailto://" not in caplog.text


def test_blank_delivery_label_does_not_fall_back_to_url(failed_delivery, caplog):
    with caplog.at_level(logging.WARNING, logger="reclaimerr"):
        with pytest.raises(RetryError):
            asyncio.run(
                notifications.send_notification(
                    "discord://webhook/SECRET_TOKEN", "title", "body", log_label=""
                )
            )

    assert "notification destination" in caplog.text
    assert "SECRET_TOKEN" not in caplog.text


def test_unnamed_notification_destination_uses_its_id():
    setting = NotificationSetting(
        user_id=1,
        enabled=True,
        channel=NotificationChannel.APPRISE,
        url="discord://webhook/SECRET_TOKEN",
        name="",
    )
    setting.id = 42

    assert notifications._resolve_destination(setting, None, None) == (
        setting.url,
        "notification 42",
    )
