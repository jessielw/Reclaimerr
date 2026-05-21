from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.rule_engine import collect_rule_library_ids
from backend.database.models import AdminNotice, ReclaimRule, ServiceMediaLibrary

NOTICE_KEY_UPDATE_AVAILABLE = "update_available"
NOTICE_KEY_SEERR_RULE_SKIP = "seerr_rules_skipped"
NOTICE_KEY_STALE_LIBRARY_IDS = "stale_library_ids"


def _normalize_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    return context if isinstance(context, dict) else None


def _render_stale_library_notice_message(stale_rule_names: list[str]) -> str:
    names = ", ".join(f'"{n}"' for n in stale_rule_names[:3])
    suffix = (
        f" and {len(stale_rule_names) - 3} more" if len(stale_rule_names) > 3 else ""
    )
    return (
        f"The following rules have library filters referencing libraries that no "
        f"longer exist (likely from a previous media server): {names}{suffix}. "
        f"Cleanup scans will produce no candidates until the library filters are "
        f"updated or cleared."
    )


async def upsert_singleton_notice(
    db: AsyncSession,
    *,
    dedupe_key: str,
    kind: str,
    severity: str,
    title: str,
    message: str,
    action_label: str | None = None,
    action_href: str | None = None,
    context_json: dict[str, Any] | None = None,
) -> AdminNotice:
    """Creates or updates a singleton notice with the given dedupe key."""
    now = datetime.now(UTC)
    context_json = _normalize_context(context_json)
    existing = (
        await db.execute(
            select(AdminNotice).where(AdminNotice.dedupe_key == dedupe_key).limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        notice = AdminNotice(
            kind=kind,
            severity=severity,
            title=title,
            message=message,
            action_label=action_label,
            action_href=action_href,
            context_json=context_json,
            is_active=True,
            is_read=False,
            read_at=None,
            read_by_user_id=None,
            dedupe_key=dedupe_key,
        )
        notice.last_occurred_at = now
        db.add(notice)
        await db.flush()
        return notice

    content_changed = any(
        [
            existing.kind != kind,
            existing.severity != severity,
            existing.title != title,
            existing.message != message,
            existing.action_label != action_label,
            existing.action_href != action_href,
            existing.context_json != context_json,
        ]
    )
    existing.kind = kind
    existing.severity = severity
    existing.title = title
    existing.message = message
    existing.action_label = action_label
    existing.action_href = action_href
    existing.context_json = context_json
    existing.is_active = True
    existing.last_occurred_at = now
    if content_changed:
        existing.is_read = False
        existing.read_at = None
        existing.read_by_user_id = None
    await db.flush()
    return existing


async def resolve_singleton_notice(db: AsyncSession, *, dedupe_key: str) -> bool:
    """Resolves (deactivates) the singleton notice with the given dedupe key. Returns True
    if a notice was found and resolved, False otherwise."""
    notice = (
        await db.execute(
            select(AdminNotice).where(AdminNotice.dedupe_key == dedupe_key).limit(1)
        )
    ).scalar_one_or_none()
    if notice is None:
        return False
    if notice.is_active:
        notice.is_active = False
        await db.flush()
    return True


async def create_event_notice(
    db: AsyncSession,
    *,
    kind: str,
    severity: str,
    title: str,
    message: str,
    action_label: str | None = None,
    action_href: str | None = None,
    context_json: dict[str, Any] | None = None,
) -> AdminNotice:
    """Creates a one-time event notice. These are not de-duplicated and are always treated as new notices."""
    now = datetime.now(UTC)
    notice = AdminNotice(
        kind=kind,
        severity=severity,
        title=title,
        message=message,
        action_label=action_label,
        action_href=action_href,
        context_json=_normalize_context(context_json),
        is_active=True,
        is_read=False,
        read_at=None,
        read_by_user_id=None,
        dedupe_key=None,
    )
    notice.last_occurred_at = now
    db.add(notice)
    await db.flush()
    return notice


async def list_active_admin_notices(
    db: AsyncSession,
    *,
    limit: int = 100,
    include_read: bool = True,
) -> list[AdminNotice]:
    """Lists active admin notices, ordered by unread first then most recent. By default,
    both read and unread notices are included, but can be filtered to only include unread notices."""
    filters = [AdminNotice.is_active == True]
    if not include_read:
        filters.append(AdminNotice.is_read == False)
    result = await db.execute(
        select(AdminNotice)
        .where(*filters)
        .order_by(
            AdminNotice.is_read.asc(),
            AdminNotice.last_occurred_at.desc(),
            AdminNotice.id.desc(),
        )
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_unread_active_notices(db: AsyncSession) -> int:
    """Counts the number of unread active admin notices."""
    value = await db.scalar(
        select(func.count(AdminNotice.id)).where(
            AdminNotice.is_active == True,
            AdminNotice.is_read == False,
        )
    )
    return int(value or 0)


async def has_unread_active_notices(db: AsyncSession) -> bool:
    """Checks if there are any unread active admin notices."""
    value = (
        await db.execute(
            select(AdminNotice.id)
            .where(AdminNotice.is_active == True, AdminNotice.is_read == False)
            .limit(1)
        )
    ).scalar_one_or_none()
    return value is not None


async def mark_admin_notice_read(
    db: AsyncSession,
    *,
    notice_id: int,
    admin_user_id: int,
) -> AdminNotice | None:
    """Marks the specified notice as read by the specified admin user. Returns the updated
    notice, or None if not found."""
    notice = (
        await db.execute(select(AdminNotice).where(AdminNotice.id == notice_id))
    ).scalar_one_or_none()
    if notice is None:
        return None
    notice.is_read = True
    notice.read_at = datetime.now(UTC)
    notice.read_by_user_id = admin_user_id
    await db.flush()
    return notice


async def mark_admin_notice_unread(
    db: AsyncSession,
    *,
    notice_id: int,
) -> AdminNotice | None:
    """Marks the specified notice as unread. Returns the updated notice, or None if not found."""
    notice = (
        await db.execute(select(AdminNotice).where(AdminNotice.id == notice_id))
    ).scalar_one_or_none()
    if notice is None:
        return None
    notice.is_read = False
    notice.read_at = None
    notice.read_by_user_id = None
    await db.flush()
    return notice


async def sync_update_available_notice(
    db: AsyncSession,
    *,
    update_available: bool,
    latest_version: str | None,
    latest_release_url: str | None,
) -> None:
    """Creates or resolves the 'update available' notice based on the given update availability status."""
    if not update_available:
        await resolve_singleton_notice(db, dedupe_key=NOTICE_KEY_UPDATE_AVAILABLE)
        return

    version_label = f" ({latest_version})" if latest_version else ""
    await upsert_singleton_notice(
        db,
        dedupe_key=NOTICE_KEY_UPDATE_AVAILABLE,
        kind="update_available",
        severity="warning",
        title="Application update available",
        message=f"A new Reclaimerr release is available{version_label}.",
        action_label="View release details",
        action_href=latest_release_url,
        context_json={
            "latest_version": latest_version,
            "latest_release_url": latest_release_url,
        },
    )


async def set_seerr_rule_skip_notice(
    db: AsyncSession,
    *,
    skipped_rules: int,
    reason: str,
) -> None:
    """Creates or updates a notice about Seerr dependent rules being skipped during cleanup scans."""
    await upsert_singleton_notice(
        db,
        dedupe_key=NOTICE_KEY_SEERR_RULE_SKIP,
        kind="seerr_rule_skip",
        severity="warning",
        title="Seerr dependent rules were skipped",
        message=(
            f"The latest cleanup scan skipped {skipped_rules} Seerr dependent rule(s) "
            f"because Seerr request data could not be refreshed: {reason or 'unknown error'}."
        ),
        action_label="Go to Settings",
        action_href="#/settings",
        context_json={"skipped_rules": skipped_rules, "reason": reason},
    )


async def clear_seerr_rule_skip_notice(db: AsyncSession) -> None:
    """Resolves the notice about Seerr dependent rules being skipped."""
    await resolve_singleton_notice(db, dedupe_key=NOTICE_KEY_SEERR_RULE_SKIP)


async def reconcile_stale_library_notice(db: AsyncSession) -> list[str]:
    """Checks for any reclaim rules that reference library IDs that no longer exist,
    and creates or resolves a notice accordingly. Returns a list of rule names that have
    stale library references."""
    known_lib_result = await db.execute(select(ServiceMediaLibrary.library_id))
    known_library_ids: set[str] = {row[0] for row in known_lib_result.all()}

    rules_result = await db.execute(
        select(ReclaimRule).where(ReclaimRule.enabled == True)
    )
    stale_rule_names: list[str] = []
    for rule in rules_result.scalars().all():
        rule_library_ids = collect_rule_library_ids(rule.definition)
        if rule_library_ids and any(
            lid not in known_library_ids for lid in rule_library_ids
        ):
            stale_rule_names.append(rule.name)

    if stale_rule_names:
        await upsert_singleton_notice(
            db,
            dedupe_key=NOTICE_KEY_STALE_LIBRARY_IDS,
            kind="stale_library_ids",
            severity="warning",
            title="Stale library filters in cleanup rules",
            message=_render_stale_library_notice_message(stale_rule_names),
            action_label="Go to Rules",
            action_href="#/settings",
            context_json={"rule_names": stale_rule_names},
        )
    else:
        await resolve_singleton_notice(db, dedupe_key=NOTICE_KEY_STALE_LIBRARY_IDS)

    return stale_rule_names
