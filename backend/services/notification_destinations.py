"""Which Apprise destinations an account may deliver to.

Apprise happily sends to whatever host a URL names (``json://127.0.0.1:8989/...``),
so letting every account with Settings access save an arbitrary URL is a
server-side request forgery primitive against loopback, LAN and cloud-metadata
addresses. Admins keep the full Apprise catalogue. Everyone else may:

* use a service that always talks to one fixed, public vendor API (``not_required``)
* use any other single destination once an admin has approved that exact URL
  (``approved``); until then it is ``pending`` and never delivered to

The check is on the plugin Apprise resolves the URL to, never on the scheme
text: ``https://discord.com/api/webhooks/...`` and ``discord://...`` both land
on the Discord plugin. A host allow/deny check is deliberately not used, since
Apprise follows HTTP redirects by default and a public host could bounce the
request to an internal one. Approved destinations of non-admins are sent with
redirects disabled for the same reason (see ``send_notification``).
"""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Literal

import apprise
from apprise.plugins.discord import NotifyDiscord
from apprise.plugins.ntfy import NotifyNtfy
from apprise.plugins.pushbullet import NotifyPushBullet
from apprise.plugins.pushover import NotifyPushover
from apprise.plugins.slack import NotifySlack
from apprise.plugins.telegram import NotifyTelegram

from backend.database.models import User
from backend.enums import UserRole

DestinationApproval = Literal["not_required", "approved", "pending", "invalid"]

# every one of these sends to a single fixed vendor host regardless of the URL
_FIXED_HOST_PLUGINS: tuple[type, ...] = (
    NotifyDiscord,
    NotifyTelegram,
    NotifySlack,
    NotifyPushover,
    NotifyPushBullet,
)

INVALID_DESTINATION_ERROR = (
    "This is not a valid single Apprise destination URL, so it cannot be used."
)
PENDING_DESTINATION_ERROR = (
    "This destination needs administrator approval before it can be used. Save "
    "it and an administrator will be asked to review it."
)


def url_fingerprint(url: str) -> str:
    """Stable hash of the exact URL an approval applies to."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _single_plugin(url: str) -> apprise.NotifyBase | None:
    """The one plugin `url` resolves to, or None if it is not exactly one.

    Apprise splits a string on commas/whitespace and adds every URL in it, so
    an allowed URL could otherwise smuggle a second, forbidden one along.
    """
    ap = apprise.Apprise()
    if not ap.add(url) or len(ap) != 1:
        return None
    return next(iter(ap))


def _is_fixed_host(plugin: object) -> bool:
    if isinstance(plugin, _FIXED_HOST_PLUGINS):
        return True
    # ntfy only has a fixed host in cloud mode; private mode names its own host
    return isinstance(plugin, NotifyNtfy) and plugin.mode == "cloud"


def destination_approval(
    url: str, role: UserRole | None, approved_hash: str | None
) -> DestinationApproval:
    """Where `url` stands for an owner with `role`.

    A missing role is treated as non-admin so an unresolved owner can never
    widen what is allowed.
    """
    if role is UserRole.ADMIN:
        return "not_required"
    plugin = _single_plugin(url)
    if plugin is None:
        return "invalid"
    if _is_fixed_host(plugin):
        return "not_required"
    if approved_hash and approved_hash == url_fingerprint(url):
        return "approved"
    return "pending"


def destination_approval_for_user(
    url: str, user: User | None, approved_hash: str | None
) -> DestinationApproval:
    """`destination_approval` for the owner of a destination."""
    return destination_approval(url, user.role if user else None, approved_hash)


def is_deliverable(state: DestinationApproval) -> bool:
    return state in ("not_required", "approved")


def approval_error(state: DestinationApproval) -> str | None:
    """User-facing reason a destination in `state` cannot be used, if any."""
    if state == "invalid":
        return INVALID_DESTINATION_ERROR
    if state == "pending":
        return PENDING_DESTINATION_ERROR
    return None


def describe_destination(url: str) -> dict[str, str | bool] | None:
    """What an admin needs to judge a destination, with secrets redacted."""
    plugin = _single_plugin(url)
    if plugin is None:
        return None
    host = (getattr(plugin, "host", None) or "").strip()
    return {
        "service": str(plugin.service_name or type(plugin).__name__),
        "host": host,
        "redacted_url": plugin.url(privacy=True),
        "internal_host": _looks_internal(host),
    }


def _looks_internal(host: str) -> bool:
    """Cheap hint (no DNS) that a host is loopback, private or a bare LAN name.

    Only a warning for the reviewing admin, never a security decision.
    """
    if not host:
        return False
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        pass
    lowered = host.lower()
    return (
        lowered == "localhost"
        or "." not in lowered
        or lowered.endswith((".local", ".lan", ".internal", ".home", ".localdomain"))
    )
