"""Which Apprise destinations a user may deliver to.

Apprise happily sends to whatever host a URL names (``json://127.0.0.1:8989/...``),
so letting every account with Settings access save an arbitrary URL is a
server-side request forgery primitive against loopback, LAN and cloud-metadata
addresses. Non-admins are therefore limited to services that always talk to
one fixed, public vendor API. Admins keep the full Apprise catalogue.

The check is on the plugin Apprise resolves the URL to, never on the scheme
text: ``https://discord.com/api/webhooks/...`` and ``discord://...`` both land
on the Discord plugin, and anything that does not parse is rejected. A host
allow/deny check is deliberately not used - Apprise follows HTTP redirects by
default, so a public host could bounce the request to an internal one.
"""

from __future__ import annotations

import apprise
from apprise.plugins.discord import NotifyDiscord
from apprise.plugins.ntfy import NotifyNtfy
from apprise.plugins.pushbullet import NotifyPushBullet
from apprise.plugins.pushover import NotifyPushover
from apprise.plugins.slack import NotifySlack
from apprise.plugins.telegram import NotifyTelegram

from backend.database.models import User
from backend.enums import UserRole

# every one of these sends to a single fixed vendor host regardless of the URL
_FIXED_HOST_PLUGINS: tuple[type, ...] = (
    NotifyDiscord,
    NotifyTelegram,
    NotifySlack,
    NotifyPushover,
    NotifyPushBullet,
)

NON_ADMIN_DESTINATION_ERROR = (
    "Only Discord, Telegram, Slack, Pushover, Pushbullet and ntfy.sh "
    "destinations are available to your account. Ask an administrator to add "
    "any other destination."
)


def destination_error_for_role(url: str, role: UserRole | None) -> str | None:
    """Return why `url` is not allowed for `role`, or None when it is.

    A missing role is treated as non-admin so an unresolved owner can never
    widen what is allowed.
    """
    if role is UserRole.ADMIN:
        return None

    ap = apprise.Apprise()
    # Apprise splits a string on commas/whitespace and adds every URL in it, so
    # an allowed URL could otherwise smuggle a second, forbidden one along.
    if not ap.add(url) or len(ap) != 1:
        return NON_ADMIN_DESTINATION_ERROR

    plugin = next(iter(ap))
    if isinstance(plugin, _FIXED_HOST_PLUGINS):
        return None
    # ntfy only has a fixed host in cloud mode; private mode names its own host
    if isinstance(plugin, NotifyNtfy) and plugin.mode == "cloud":
        return None
    return NON_ADMIN_DESTINATION_ERROR


def destination_error_for_user(url: str, user: User | None) -> str | None:
    """`destination_error_for_role` for the owner of a destination."""
    return destination_error_for_role(url, user.role if user else None)
