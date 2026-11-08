from __future__ import annotations

import pytest

from backend.database.models import User
from backend.enums import UserRole
from backend.services.notification_destinations import (
    NON_ADMIN_DESTINATION_ERROR,
    destination_error_for_role,
    destination_error_for_user,
)

ALLOWED_FOR_USERS = [
    "discord://123/abc",
    "https://discord.com/api/webhooks/123/abc",
    "tgram://123:abc/456",
    "slack://a/b/c/d",
    "pover://user@token",
    "pbul://token",
    "ntfy://topic",
]

BLOCKED_FOR_USERS = [
    # generic HTTP senders: the SSRF primitive from the report
    "json://127.0.0.1:8989/api/v3/system/status",
    "jsons://169.254.169.254/latest/meta-data",
    "xml://10.0.0.5/x",
    "form://192.168.1.10:8080/admin?method=DELETE",
    # services that take a caller-chosen host
    "ntfys://internal.lan/topic",
    "ntfy://127.0.0.1/topic",
    "gotify://10.0.0.5/token",
    "mailto://user:pass@internal.lan",
    # one allowed URL must not smuggle a forbidden one alongside it
    "discord://123/abc json://127.0.0.1/x",
    "discord://123/abc,json://127.0.0.1/x",
    # unparseable or empty
    "bogus://x",
    "discord://token",
    "",
]


@pytest.mark.parametrize("url", ALLOWED_FOR_USERS)
def test_fixed_host_services_are_allowed_for_users(url: str) -> None:
    assert destination_error_for_role(url, UserRole.USER) is None


@pytest.mark.parametrize("url", BLOCKED_FOR_USERS)
def test_everything_else_is_blocked_for_users(url: str) -> None:
    assert destination_error_for_role(url, UserRole.USER) == NON_ADMIN_DESTINATION_ERROR


@pytest.mark.parametrize("url", ALLOWED_FOR_USERS + BLOCKED_FOR_USERS)
def test_admins_are_unrestricted(url: str) -> None:
    assert destination_error_for_role(url, UserRole.ADMIN) is None


def test_unknown_owner_is_treated_as_non_admin() -> None:
    assert destination_error_for_role("json://127.0.0.1/x", None) is not None
    assert destination_error_for_user("json://127.0.0.1/x", None) is not None


def test_user_object_role_is_used() -> None:
    admin = User(username="a", password_hash="h", role=UserRole.ADMIN)
    member = User(username="m", password_hash="h", role=UserRole.USER)
    assert destination_error_for_user("json://127.0.0.1/x", admin) is None
    assert destination_error_for_user("json://127.0.0.1/x", member) is not None


def test_delivery_skips_a_stored_destination_the_owner_may_not_use() -> None:
    from backend.database.models import NotificationSetting
    from backend.enums import NotificationChannel
    from backend.services.notifications import _resolve_destination

    setting = NotificationSetting(
        user_id=1,
        enabled=True,
        channel=NotificationChannel.APPRISE,
        url="json://127.0.0.1:8989/x",
    )
    member = User(username="m", password_hash="h", role=UserRole.USER)
    admin = User(username="a", password_hash="h", role=UserRole.ADMIN)

    assert _resolve_destination(setting, member, None) is None
    assert _resolve_destination(setting, None, None) is None
    assert _resolve_destination(setting, admin, None) is not None
