from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.database.models import NotificationSetting, SMTPSettings, User
from backend.enums import NotificationChannel, UserRole

__all__ = [
    "SECURITY_DEFAULT_PORTS",
    "SECURITY_MODES",
    "SmtpConfig",
    "auto_enable_system_email",
    "build_mailto_url",
    "email_destination_user_ids",
    "load_smtp_config",
    "new_system_email_destination",
    "redact_mailto_url",
    "smtp_config_from_row",
    "system_email_enabled",
]

# Mirrors apprise.plugins.email.common.SECURE_MODES. Apprise picks the same
# defaults, but we store an explicit port so the admin always sees what is used.
SECURITY_DEFAULT_PORTS: dict[str, int] = {
    "starttls": 587,
    "ssl": 465,
    "insecure": 25,
}

SECURITY_MODES = tuple(SECURITY_DEFAULT_PORTS)

# What an auto-created destination opts into. These are the types a person
# actually wants in their inbox; the noisier admin-only ones are added for
# admins, who can still turn any of them off afterwards.
_DEFAULT_USER_TYPES: tuple[str, ...] = (
    "new_cleanup_candidates",
    "request_approved",
    "request_declined",
    "admin_message",
    "delete_request_execution_succeeded",
    "delete_request_execution_failed",
)
_DEFAULT_ADMIN_TYPES: tuple[str, ...] = (
    "task_failure",
    "admin_new_delete_request",
    "admin_new_protection_request",
    "admin_request_cancelled",
    "admin_delete_execution_failed",
    "update_available",
)


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """A usable SMTP configuration with the password already decrypted."""

    host: str
    port: int
    security: str
    username: str
    password: str
    from_address: str
    from_name: str
    reply_to: str | None = None


def smtp_config_from_row(row: SMTPSettings, *, password: str) -> SmtpConfig:
    """Build a config from a settings row and an already-decrypted password."""
    security = (row.security or "starttls").strip().lower()
    if security not in SECURITY_DEFAULT_PORTS:
        security = "starttls"
    return SmtpConfig(
        host=(row.host or "").strip(),
        port=row.port or SECURITY_DEFAULT_PORTS[security],
        security=security,
        username=(row.username or "").strip(),
        password=password,
        from_address=(row.from_address or "").strip(),
        from_name=(row.from_name or "").strip(),
        reply_to=(row.reply_to or "").strip() or None,
    )


async def load_smtp_config(session: AsyncSession) -> SmtpConfig | None:
    """Return the instance SMTP config, or None when email cannot be delivered.

    Returns None when SMTP is disabled, incompletely configured, or when the
    stored password cannot be decrypted. A rotated ENCRYPTION_KEY must disable
    email delivery only - it must not take every other notification down with
    it, so the failure is logged and swallowed here.
    """
    result = await session.execute(select(SMTPSettings))
    row = result.scalars().first()
    if row is None or not row.enabled:
        return None

    if not (row.host or "").strip() or not (row.from_address or "").strip():
        LOG.warning(
            "System SMTP is enabled but incomplete (host and from address are "
            "both required); email notifications will be skipped"
        )
        return None

    password = ""
    if row.password:
        try:
            password = fer_decrypt(row.password)
        except Exception as e:
            LOG.error(
                f"Stored SMTP password could not be decrypted, skipping email "
                f"notifications until it is re-entered: {e}"
            )
            return None

    return smtp_config_from_row(row, password=password)


async def system_email_enabled(session: AsyncSession) -> bool:
    """Whether the admin has switched system email on."""
    result = await session.execute(select(SMTPSettings.enabled))
    return bool(result.scalars().first())


async def email_destination_user_ids(session: AsyncSession) -> set[int]:
    """Ids of every user who already has a system email destination."""
    result = await session.execute(
        select(NotificationSetting.user_id).where(
            NotificationSetting.channel == NotificationChannel.SYSTEM_EMAIL
        )
    )
    return set(result.scalars().all())


def new_system_email_destination(user: User) -> NotificationSetting:
    """Build (but do not add) a default system email destination for a user."""
    destination = NotificationSetting(
        user_id=user.id,
        enabled=True,
        name="Email",
        channel=NotificationChannel.SYSTEM_EMAIL,
    )
    enabled_types = list(_DEFAULT_USER_TYPES)
    if user.role is UserRole.ADMIN:
        enabled_types += _DEFAULT_ADMIN_TYPES
    for field in enabled_types:
        setattr(destination, field, True)
    return destination


async def auto_enable_system_email(session: AsyncSession, user: User) -> bool:
    """Give a user a system email destination if SMTP is on and they lack one.

    Called when a user is created or first gains an email address, so admins do
    not have to re-run the bulk enable every time someone new signs in. The
    user must already have an id (flush first). Does not commit.
    """
    if not user.email or not user.is_active or user.id is None:
        return False
    if not await system_email_enabled(session):
        return False
    existing = await session.execute(
        select(NotificationSetting.id).where(
            NotificationSetting.user_id == user.id,
            NotificationSetting.channel == NotificationChannel.SYSTEM_EMAIL,
        )
    )
    if existing.first() is not None:
        return False
    session.add(new_system_email_destination(user))
    LOG.info(f"Enabled system email notifications for {user.username}")
    return True


def build_mailto_url(config: SmtpConfig, recipient: str) -> str:
    """Build the Apprise mailto:// URL that delivers to a single recipient.

    `smtp=` is always set, which makes Apprise skip the provider auto-detection
    that would otherwise rewrite the host, port, user and from address for a
    well-known domain such as gmail.com. Userinfo is percent-encoded so a
    password containing '@', ':', '/' or '?' survives the round trip.
    """
    auth = ""
    if config.username:
        auth = quote(config.username, safe="")
        if config.password:
            auth = f"{auth}:{quote(config.password, safe='')}"
        auth = f"{auth}@"

    params: dict[str, str] = {
        "smtp": config.host,
        "mode": config.security,
        "from": config.from_address,
        "to": recipient,
    }
    if config.from_name:
        params["name"] = config.from_name
    if config.reply_to:
        params["reply"] = config.reply_to

    host = quote(config.host, safe="")
    # quote_via=quote writes spaces as %20; Apprise does not decode the '+'
    # that urlencode emits by default, so a from name like "My Server" would
    # arrive as "My+Server".
    query = urlencode(params, quote_via=quote)
    return f"mailto://{auth}{host}:{config.port}/?{query}"


def redact_mailto_url(url: str) -> str:
    """Strip credentials from a URL so it is safe to write to the log."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<redacted url>"
    if not parts.hostname:
        return url
    netloc = parts.hostname
    if parts.username:
        netloc = f"***@{netloc}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
