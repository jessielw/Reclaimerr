from __future__ import annotations

import pytest

from backend.database.models import NotificationSetting, User
from backend.enums import NotificationChannel, UserRole
from backend.services.notification_destinations import (
    approval_error,
    describe_destination,
    destination_approval,
    destination_approval_for_user,
    is_deliverable,
    url_fingerprint,
)
from backend.services.notifications import _follows_redirects, _resolve_destination

FIXED_HOST = [
    "discord://123/abc",
    "https://discord.com/api/webhooks/123/abc",
    "tgram://123:abc/456",
    "slack://a/b/c/d",
    "pover://user@token",
    "pbul://token",
    "ntfy://topic",
]

NEEDS_APPROVAL = [
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
]

INVALID = [
    # one allowed URL must not smuggle a forbidden one alongside it
    "discord://123/abc json://127.0.0.1/x",
    "discord://123/abc,json://127.0.0.1/x",
    "bogus://x",
    "discord://token",
    "",
]


@pytest.mark.parametrize("url", FIXED_HOST)
def test_fixed_host_services_need_no_approval(url: str) -> None:
    assert destination_approval(url, UserRole.USER, None) == "not_required"


@pytest.mark.parametrize("url", NEEDS_APPROVAL)
def test_other_services_are_pending_for_users(url: str) -> None:
    assert destination_approval(url, UserRole.USER, None) == "pending"


@pytest.mark.parametrize("url", INVALID)
def test_unparseable_or_multi_url_is_invalid_for_users(url: str) -> None:
    assert destination_approval(url, UserRole.USER, None) == "invalid"
    # and an approval hash can never rescue it
    assert destination_approval(url, UserRole.USER, url_fingerprint(url)) == "invalid"


@pytest.mark.parametrize("url", FIXED_HOST + NEEDS_APPROVAL + INVALID)
def test_admins_are_unrestricted(url: str) -> None:
    assert destination_approval(url, UserRole.ADMIN, None) == "not_required"


@pytest.mark.parametrize("url", NEEDS_APPROVAL)
def test_matching_hash_approves_exactly_that_url(url: str) -> None:
    approved = url_fingerprint(url)
    assert destination_approval(url, UserRole.USER, approved) == "approved"
    # a different URL (an edit) drops back to pending
    other = "json://example.org/other"
    assert destination_approval(other, UserRole.USER, approved) == "pending"
    assert destination_approval(url, UserRole.USER, url_fingerprint("other")) == (
        "pending"
    )


def test_only_usable_states_are_deliverable() -> None:
    assert is_deliverable("not_required")
    assert is_deliverable("approved")
    assert not is_deliverable("pending")
    assert not is_deliverable("invalid")
    assert approval_error("pending") and approval_error("invalid")
    assert approval_error("approved") is None


def test_unknown_owner_is_treated_as_non_admin() -> None:
    assert destination_approval("json://127.0.0.1/x", None, None) == "pending"
    assert destination_approval_for_user("json://127.0.0.1/x", None, None) == "pending"


def test_only_admins_follow_redirects() -> None:
    admin = User(username="a", password_hash="h", role=UserRole.ADMIN)
    member = User(username="m", password_hash="h", role=UserRole.USER)
    assert _follows_redirects(admin)
    assert not _follows_redirects(member)
    assert not _follows_redirects(None)


def test_describe_destination_redacts_and_flags_internal_hosts() -> None:
    info = describe_destination("json://user:secretpw@192.168.1.5:8080/hook")
    assert info is not None
    assert info["host"] == "192.168.1.5"
    assert info["internal_host"] is True
    assert "secretpw" not in str(info["redacted_url"])
    public = describe_destination("json://example.com/hook")
    assert public is not None and public["internal_host"] is False
    assert describe_destination("bogus://x") is None


def _setting(url: str, approved: str | None = None) -> NotificationSetting:
    return NotificationSetting(
        user_id=1,
        enabled=True,
        channel=NotificationChannel.APPRISE,
        url=url,
        approved_url_hash=approved,
    )


def test_delivery_skips_a_destination_that_is_not_approved() -> None:
    url = "json://example.com/hook"
    member = User(username="m", password_hash="h", role=UserRole.USER)
    admin = User(username="a", password_hash="h", role=UserRole.ADMIN)

    assert _resolve_destination(_setting(url), member, None) is None
    assert _resolve_destination(_setting(url), None, None) is None
    assert _resolve_destination(_setting(url), admin, None) is not None


def test_delivery_allows_an_approved_destination_until_it_is_edited() -> None:
    url = "json://example.com/hook"
    member = User(username="m", password_hash="h", role=UserRole.USER)
    approved = url_fingerprint(url)

    assert _resolve_destination(_setting(url, approved), member, None) is not None
    edited = _setting("json://127.0.0.1:8989/x", approved)
    assert _resolve_destination(edited, member, None) is None


def test_send_notification_disables_redirects_when_asked(monkeypatch) -> None:
    import asyncio

    import apprise

    from backend.services import notifications

    seen: list[bool] = []

    async def fake_notify(self, *args, **kwargs):  # noqa: ANN001
        seen.extend(plugin.redirects for plugin in self)
        return True

    monkeypatch.setattr(apprise.Apprise, "async_notify", fake_notify)
    send = notifications.send_notification
    for follow in (True, False):
        asyncio.run(
            send(
                "json://example.com/hook",
                "t",
                "b",
                log_label="x",
                follow_redirects=follow,
            )
        )
    assert seen == [True, False]


def test_email_recipient_pins_non_admins_to_their_own_address() -> None:
    from backend.services.notification_destinations import (
        email_override_allowed,
        email_recipient_for,
    )

    user = User(username="u", password_hash="x", email="me@x.io", role=UserRole.USER)
    admin = User(username="a", password_hash="x", email="a@x.io", role=UserRole.ADMIN)

    assert email_recipient_for(user, "victim@y.io") == "me@x.io"
    assert email_recipient_for(admin, "other@y.io") == "other@y.io"
    assert email_recipient_for(admin, None) == "a@x.io"
    assert not email_override_allowed(user, "victim@y.io")
    assert email_override_allowed(user, "ME@x.io")
    assert email_override_allowed(user, None)
    assert email_override_allowed(admin, "other@y.io")
