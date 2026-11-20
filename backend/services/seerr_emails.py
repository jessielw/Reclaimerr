"""Look up user email addresses from connected Seerr instances.

Jellyfin does not expose an email address for its users, but Seerr usually
knows one. These helpers let the media user import, and an explicit "fill
missing emails" action, use it without ever overwriting an address that is
already set.
"""

from __future__ import annotations

from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.database.models import MediaUserIdentity, User
from backend.services.media_auth import normalize_email, normalize_username
from backend.services.seerr_cache import seerr_snapshot_cache


def _valid_email(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 120:
        return None
    try:
        return validate_email(candidate, check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def _id_key(value: str | None) -> str:
    # Jellyfin ids show up with and without dashes depending on the API.
    return (value or "").replace("-", "").strip().lower()


@dataclass(frozen=True, slots=True)
class SeerrEmailDirectory:
    """Seerr's email addresses, keyed by the ways a media user can be matched."""

    by_media_id: dict[str, str]
    by_username: dict[str, str]

    def lookup(self, *, media_user_id: str | None, username: str | None) -> str | None:
        if media_user_id and (hit := self.by_media_id.get(_id_key(media_user_id))):
            return hit
        key = normalize_username(username or "")
        return self.by_username.get(key) if key else None


async def load_seerr_email_directory() -> SeerrEmailDirectory:
    """Build the lookup, or an empty one when Seerr is missing or unreachable."""
    by_media_id: dict[str, str] = {}
    by_username: dict[str, str] = {}
    try:
        instance_users = await seerr_snapshot_cache.get_users()
    except Exception as exc:
        LOG.warning(f"Could not read Seerr users for email lookup: {exc}")
        return SeerrEmailDirectory(by_media_id, by_username)

    # Names and ids that more than one Seerr user claims are ambiguous: skip them
    # rather than guess which address is right.
    ambiguous: set[tuple[str, str]] = set()

    def add(kind: str, table: dict[str, str], key: str, email: str) -> None:
        if not key or (kind, key) in ambiguous:
            return
        if key in table and table[key] != email:
            del table[key]
            ambiguous.add((kind, key))
            return
        table[key] = email

    for item in instance_users:
        user = item.user
        email = _valid_email(user.email)
        if email is None:
            continue
        add("id", by_media_id, _id_key(user.jellyfin_user_id), email)
        for name in (user.jellyfin_username, user.username, user.display_name):
            add("name", by_username, normalize_username(name or ""), email)
    return SeerrEmailDirectory(by_media_id, by_username)


@dataclass(slots=True)
class FillEmailsResult:
    updated: list[str]
    skipped_no_match: list[str]
    skipped_conflict: list[str]


async def fill_missing_user_emails(db: AsyncSession) -> FillEmailsResult:
    """Set the email of accounts that have none from Seerr. Never overwrites."""
    directory = await load_seerr_email_directory()
    result = FillEmailsResult([], [], [])
    if not directory.by_media_id and not directory.by_username:
        return result

    users = list((await db.scalars(select(User))).all())
    taken = {normalize_email(u.email) for u in users if u.email}
    identities = (await db.scalars(select(MediaUserIdentity))).all()
    media_ids: dict[int, list[str]] = {}
    for identity in identities:
        if identity.user_id is not None:
            media_ids.setdefault(identity.user_id, []).append(identity.source_user_id)

    for user in users:
        if user.email:
            continue
        email = None
        for media_id in media_ids.get(user.id, []):
            email = directory.lookup(media_user_id=media_id, username=None)
            if email:
                break
        email = email or directory.lookup(media_user_id=None, username=user.username)
        if email is None:
            result.skipped_no_match.append(user.username)
        elif normalize_email(email) in taken:
            result.skipped_conflict.append(user.username)
        else:
            user.email = email
            taken.add(normalize_email(email))
            result.updated.append(user.username)
    if result.updated:
        await db.commit()
    return result
