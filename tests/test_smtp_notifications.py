from __future__ import annotations

import apprise
import pytest

from backend.database.models import NotificationSetting, User
from backend.enums import NotificationChannel
from backend.services.notifications import _resolve_destination
from backend.services.smtp import (
    SECURITY_DEFAULT_PORTS,
    SmtpConfig,
    build_mailto_url,
    redact_mailto_url,
)


def _config(**overrides: object) -> SmtpConfig:
    base: dict[str, object] = {
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "notify@example.com",
        "password": "hunter2",
        "from_address": "notify@example.com",
        "from_name": "Reclaimerr",
        "reply_to": None,
    }
    base.update(overrides)
    return SmtpConfig(**base)  # pyright: ignore[reportArgumentType]


def _parse(url: str) -> apprise.plugins.NotifyBase:  # type: ignore[reportAttributeAccessIssue]
    """Hand the URL to Apprise and return the plugin it built.

    This is the contract that matters: a URL that formats nicely but that
    Apprise parses into a different server, port or recipient would still
    deliver to the wrong place, and only round-tripping catches that.
    """
    ap = apprise.Apprise()
    assert ap.add(url) is True, f"Apprise rejected {redact_mailto_url(url)}"
    return ap[0]


def test_url_round_trips_through_apprise() -> None:
    plugin = _parse(build_mailto_url(_config(), "user@inbox.net"))

    assert plugin.smtp_host == "smtp.example.com"
    assert plugin.port == 587
    assert plugin.secure_mode == "starttls"
    assert plugin.user == "notify@example.com"
    assert plugin.password == "hunter2"
    assert plugin.from_addr == ("Reclaimerr", "notify@example.com")
    assert plugin.targets == [(False, "user@inbox.net")]


@pytest.mark.parametrize("security", sorted(SECURITY_DEFAULT_PORTS))
def test_every_security_mode_round_trips(security: str) -> None:
    port = SECURITY_DEFAULT_PORTS[security]
    plugin = _parse(
        build_mailto_url(_config(security=security, port=port), "user@inbox.net")
    )

    assert plugin.secure_mode == security
    assert plugin.port == port


def test_password_with_url_metacharacters_survives() -> None:
    # An unencoded '@', ':' or '/' would split the userinfo in the wrong place
    # and silently authenticate with a truncated password.
    password = "p@ss:w/rd?x&y#z"
    plugin = _parse(build_mailto_url(_config(password=password), "user@inbox.net"))

    assert plugin.password == password
    assert plugin.user == "notify@example.com"
    assert plugin.smtp_host == "smtp.example.com"


def test_unauthenticated_relay_omits_credentials() -> None:
    url = build_mailto_url(
        _config(username="", password="", security="insecure", port=25),
        "user@inbox.net",
    )

    assert "@smtp.example.com" not in url
    plugin = _parse(url)
    assert plugin.user is None
    assert plugin.secure_mode == "insecure"
    assert plugin.port == 25


def test_well_known_domain_is_not_rewritten() -> None:
    # Without an explicit smtp= Apprise applies its gmail template, which would
    # override the host, port and login the admin actually configured.
    plugin = _parse(
        build_mailto_url(
            _config(
                host="smtp.gmail.com",
                username="me@gmail.com",
                from_address="me@gmail.com",
            ),
            "user@inbox.net",
        )
    )

    assert plugin.smtp_host == "smtp.gmail.com"
    assert plugin.port == 587
    assert plugin.user == "me@gmail.com"


def test_reply_to_is_carried() -> None:
    plugin = _parse(
        build_mailto_url(_config(reply_to="noreply@example.com"), "user@inbox.net")
    )

    assert plugin.reply_to == {"noreply@example.com"}


def test_from_name_is_optional() -> None:
    plugin = _parse(build_mailto_url(_config(from_name=""), "user@inbox.net"))

    assert plugin.from_addr == (False, "notify@example.com")


def test_redaction_hides_the_password() -> None:
    url = build_mailto_url(_config(password="hunter2"), "user@inbox.net")
    redacted = redact_mailto_url(url)

    assert "hunter2" not in redacted
    assert "notify%40example.com:" not in redacted
    # the parts an admin needs in order to debug a failure are kept
    assert "smtp.example.com" in redacted
    assert "587" in redacted


def _setting(**overrides: object) -> NotificationSetting:
    base: dict[str, object] = {
        "user_id": 1,
        "enabled": True,
        "url": "discord://token",
        "channel": NotificationChannel.APPRISE,
    }
    base.update(overrides)
    return NotificationSetting(**base)  # pyright: ignore[reportArgumentType]


def _user(email: str | None) -> User:
    return User(username="jess", password_hash="hashed", email=email)


def test_resolve_apprise_destination_passes_the_url_through() -> None:
    setting = _setting(name="Discord")
    resolved = _resolve_destination(setting, _user("jess@inbox.net"), None)

    assert resolved == ("discord://token", "Discord")


def test_resolve_apprise_destination_without_a_url_is_skipped() -> None:
    assert _resolve_destination(_setting(url=""), _user(None), None) is None


def test_resolve_email_falls_back_to_the_account_address() -> None:
    setting = _setting(channel=NotificationChannel.SYSTEM_EMAIL, url=None)
    resolved = _resolve_destination(setting, _user("jess@inbox.net"), _config())

    assert resolved is not None
    url, label = resolved
    assert label == "system email to jess@inbox.net"
    assert _parse(url).targets == [(False, "jess@inbox.net")]


def test_resolve_email_prefers_the_destination_override() -> None:
    setting = _setting(
        channel=NotificationChannel.SYSTEM_EMAIL,
        url=None,
        target_email="other@inbox.net",
    )
    resolved = _resolve_destination(setting, _user("jess@inbox.net"), _config())

    assert resolved is not None
    url, label = resolved
    assert label == "system email to other@inbox.net"
    assert _parse(url).targets == [(False, "other@inbox.net")]


def test_resolve_email_is_skipped_without_smtp() -> None:
    setting = _setting(channel=NotificationChannel.SYSTEM_EMAIL, url=None)

    assert _resolve_destination(setting, _user("jess@inbox.net"), None) is None


def test_resolve_email_is_skipped_without_a_recipient() -> None:
    setting = _setting(channel=NotificationChannel.SYSTEM_EMAIL, url=None)

    assert _resolve_destination(setting, _user(None), _config()) is None


def test_resolve_email_never_labels_with_the_password() -> None:
    setting = _setting(channel=NotificationChannel.SYSTEM_EMAIL, url=None)
    resolved = _resolve_destination(setting, _user("jess@inbox.net"), _config())

    assert resolved is not None
    assert "hunter2" not in resolved[1]
