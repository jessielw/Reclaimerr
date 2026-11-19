"""Discover media users and provision explicitly selected accounts atomically."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree

import niquests
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.database.models import MediaUserIdentity, ServiceConfig, User
from backend.enums import PageAccess, Permission, Service, UserRole
from backend.models.media_user_import import (
    MediaImportAccount,
    MediaImportProvider,
    MediaUserImportRequest,
    MediaUserImportResponse,
    MediaUserImportRow,
)
from backend.services.admin_notices import resolve_singleton_notice
from backend.services.media_auth import (
    MEDIA_AUTH_DEVICE_ID,
    PLEX_TV_USER_URL,
    PLEX_TV_USERS_URL,
    DiscoveredMediaUser,
    MediaAuthProvider,
    _build_discovered_user,
    _decrypt_api_key,
    _display_name,
    _emby_family_email,
    _extract_machine_identifier,
    _media_browser_auth_header,
    _media_policy_bool,
    _safe_unique_username,
    default_allowed_pages_for_media_user,
    get_media_auth_provider,
    list_media_auth_providers,
    media_auth_conflict_notice_key,
    normalize_email,
    normalize_username,
    upsert_media_identity,
)
from backend.services.smtp import auto_enable_system_email

MAX_IMPORT_USERS = 1000


@dataclass(frozen=True)
class ImportSource:
    identity: DiscoveredMediaUser
    skip_reason: str | None = None


def account_summary(user: User) -> MediaImportAccount:
    return MediaImportAccount(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
    )


async def import_providers(db: AsyncSession) -> list[MediaImportProvider]:
    return [
        MediaImportProvider(
            service_config_id=provider.service_config_id,
            service_type=provider.service_type,
            name=provider.name,
        )
        for provider in await list_media_auth_providers(db)
    ]


def _source(
    provider: MediaAuthProvider,
    payload: dict,
    *,
    plex: bool = False,
) -> ImportSource:
    source_id = str(payload.get("id" if plex else "Id") or "").strip()
    username = str(
        (
            (payload.get("username") or payload.get("title"))
            if plex
            else payload.get("Name") or ""
        )
        or ""
    ).strip()
    if not source_id or not username:
        raise ValueError("Media server returned an incomplete user profile")
    # Never synthesize email addresses from usernames. Invalid optional email is absent.
    email = (
        _emby_family_email({"Email": payload.get("email")})
        if plex
        else _emby_family_email(payload)
    )
    if email and len(email) > 120:
        email = None
    skip_reason = None
    if plex and (not email or _media_policy_bool(payload, "managed")):
        skip_reason = (
            "Unsupported Plex profile: a sign-in-capable Plex account is required"
        )
    elif not plex and _media_policy_bool(payload.get("Policy"), "IsDisabled"):
        skip_reason = "Media server user is disabled"
    return ImportSource(
        _build_discovered_user(
            provider=provider,
            source_user_id=source_id,
            username=username,
            email=email,
            display_name=str(payload.get("title") or username) if plex else username,
            # Persist only the fields needed for identity attribution, never provider secrets.
            raw={"source": "media_user_import"},
        ),
        skip_reason,
    )


async def discover_import_users(
    provider: MediaAuthProvider,
    api_key: str,
) -> list[ImportSource]:
    """Strict discovery: transport failures must never look like an empty directory."""
    token = _decrypt_api_key(api_key)
    try:
        async with niquests.AsyncSession() as http:
            if provider.service_type is not Service.PLEX:
                response = await http.get(
                    f"{provider.base_url.rstrip('/')}/Users",
                    headers={
                        "accept": "application/json",
                        "X-Emby-Token": token,
                        "authorization": _media_browser_auth_header(token),
                    },
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list) or any(
                    not isinstance(u, dict) for u in payload
                ):
                    raise ValueError("Invalid media server user directory")
                sources = [_source(provider, user) for user in payload]
            else:
                headers = {"accept": "application/json", "X-Plex-Token": token}
                identity_response = await http.get(
                    f"{provider.base_url.rstrip('/')}/identity",
                    headers=headers,
                    timeout=30,
                )
                identity_response.raise_for_status()
                identity_payload = identity_response.json()
                machine_id = (
                    _extract_machine_identifier(identity_payload)
                    if isinstance(identity_payload, dict)
                    else None
                )
                if not machine_id:
                    raise ValueError("Plex returned no server identity")
                owner_response = await http.get(
                    PLEX_TV_USER_URL, headers=headers, timeout=30
                )
                owner_response.raise_for_status()
                owner = owner_response.json()
                if not isinstance(owner, dict):
                    raise ValueError("Invalid Plex owner profile")
                sources = [_source(provider, owner, plex=True)]
                response = await http.get(
                    PLEX_TV_USERS_URL,
                    headers={"accept": "application/xml"},
                    params={
                        "X-Plex-Token": token,
                        "X-Plex-Client-Identifier": MEDIA_AUTH_DEVICE_ID,
                    },
                    timeout=60,
                )
                response.raise_for_status()
                root = ElementTree.fromstring(response.text or "")
                if root.tag.split("}")[-1].lower() != "mediacontainer":
                    raise ValueError("Invalid Plex shared-user directory")
                for user in root:
                    if user.tag.split("}")[-1].lower() != "user":
                        continue
                    has_access = any(
                        server.tag.split("}")[-1].lower() == "server"
                        and str(server.get("machineIdentifier") or "").strip().lower()
                        == machine_id.lower()
                        for server in user
                    )
                    if (
                        has_access
                        and str(user.get("id")) != sources[0].identity.source_user_id
                    ):
                        sources.append(_source(provider, dict(user.attrib), plex=True))
        ids = [source.identity.source_user_id for source in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Media server returned duplicate user IDs")
        if len(ids) > MAX_IMPORT_USERS:
            raise ValueError(
                "Media server directory exceeds the 1,000-user import limit"
            )
        return sorted(sources, key=lambda source: source.identity.source_user_id)
    except Exception as exc:
        # URLs and exceptions from provider clients can contain credentials.
        LOG.warning(
            f"Media user discovery failed for config {provider.service_config_id} ({type(exc).__name__})"
        )
        raise HTTPException(
            status_code=502,
            detail="Could not read the media server user directory. Check the connection and credentials, then retry.",
        ) from None


def can_link_media_account(actor: User | None, target: User) -> bool:
    # None represents an explicitly scoped external API principal, never an anonymous caller.
    if actor is None:
        return target.role is not UserRole.ADMIN
    if actor.role is UserRole.ADMIN:
        return True
    target_pages = (
        set(target.allowed_pages)
        if target.allowed_pages is not None
        else {p.value for p in PageAccess}
    )
    actor_pages = (
        set(actor.allowed_pages)
        if actor.allowed_pages is not None
        else {p.value for p in PageAccess}
    )
    return (
        target.role is not UserRole.ADMIN
        and set(target.permissions or []) <= set(actor.permissions or [])
        and target_pages <= actor_pages
    )


def _check_grants(
    actor: User | None, request: MediaUserImportRequest, pages: list[str] | None
) -> None:
    if actor is not None and actor.role is UserRole.ADMIN:
        return
    if request.role is UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only administrators using the UI can import admin accounts",
        )
    if actor is None:
        return
    if not {p.value for p in request.permissions} <= set(actor.permissions or []):
        raise HTTPException(
            status_code=403, detail="You can only grant permissions you have yourself"
        )
    if not request.use_default_page_access:
        actor_pages = (
            set(actor.allowed_pages)
            if actor.allowed_pages is not None
            else {p.value for p in PageAccess}
        )
        requested_pages = (
            set(pages) if pages is not None else {p.value for p in PageAccess}
        )
        if not requested_pages <= actor_pages:
            raise HTTPException(
                status_code=403,
                detail="You can only grant page access you have yourself",
            )


def _rows(
    sources: list[ImportSource],
    users: list[User],
    identities: list[MediaUserIdentity],
    request: MediaUserImportRequest,
    actor: User | None,
    pages: list[str] | None,
) -> list[MediaUserImportRow]:
    by_id = {user.id: user for user in users}
    by_source = {identity.source_user_id: identity for identity in identities}
    links = {link.source_user_id: link.target_user_id for link in request.links}
    used_names = {normalize_username(user.username) for user in users}
    rows: list[MediaUserImportRow] = []
    email_counts = Counter(
        source.identity.email for source in sources if source.identity.email
    )
    # Reserve usernames for every candidate so selection changes do not change generated names.
    for source in sources:
        item = source.identity
        row = MediaUserImportRow(
            source_user_id=item.source_user_id,
            username=item.username,
            email=item.email,
            status="ready_to_create",
            message="Create account and link media identity",
        )
        existing = by_source.get(item.source_user_id)
        email_matches = [
            u for u in users if item.email and normalize_email(u.email) == item.email
        ]
        name_matches = [
            u
            for u in users
            if normalize_username(u.username) == item.username_normalized
        ]
        matches = {user.id: user for user in [*email_matches, *name_matches]}
        target = by_id.get(links.get(item.source_user_id, -1))
        if existing is not None and existing.user_id is not None:
            row.account = (
                account_summary(by_id[existing.user_id])
                if existing.user_id in by_id
                else None
            )
            if (
                item.source_user_id in links
                and existing.user_id != links[item.source_user_id]
            ):
                row.status, row.message = (
                    "conflict",
                    "Identity is already linked to a different account",
                )
            else:
                row.status, row.message = (
                    "skipped",
                    "Identity is already linked; no changes",
                )
        elif source.skip_reason:
            row.status, row.message = "skipped", source.skip_reason
        elif (
            len(matches) > 1
            or (item.email is not None and email_counts[item.email] > 1)
            or any(
                item.email and user.email and normalize_email(user.email) != item.email
                for user in name_matches
            )
        ):
            row.status, row.message = (
                "conflict",
                "Multiple accounts or conflicting email/username matches; resolve through Sign-In Identity Links",
            )
        elif item.source_user_id in links:
            if target is None:
                row.status, row.message = (
                    "conflict",
                    "Selected local account no longer exists",
                )
            elif not can_link_media_account(actor, target):
                row.status, row.message = (
                    "conflict",
                    "You cannot link an account with this level of access",
                )
            else:
                row.account = account_summary(target)
                row.status, row.message = (
                    "ready_to_link",
                    "Link selected account; preserve its settings",
                )
        elif matches:
            target = next(iter(matches.values()))
            row.account = account_summary(target)
            if can_link_media_account(actor, target):
                row.status, row.message = (
                    "needs_review",
                    "Select this suggested link explicitly to include it",
                )
            else:
                row.status, row.message = (
                    "skipped",
                    "Matching account exceeds your linking authority",
                )
        else:
            base = item.email.split("@", 1)[0] if item.email else item.username
            row.local_username = _safe_unique_username(base, used_names)
            used_names.add(normalize_username(row.local_username))
        if row.account:
            row.local_username = row.account.username
        revision = {
            "source": [
                item.source_user_id,
                item.username,
                item.email,
                source.skip_reason,
            ],
            "existing_link": existing.user_id if existing else None,
            "account": row.account.model_dump(mode="json") if row.account else None,
            "matches": sorted(matches),
            "username": row.local_username,
            "access": [
                request.role.value,
                sorted(p.value for p in request.permissions),
                pages,
            ],
            "target_access": [target.permissions, target.allowed_pages]
            if target
            else None,
        }
        row.revision = hashlib.sha256(
            json.dumps(revision, sort_keys=True).encode()
        ).hexdigest()
        rows.append(row)
    return rows


def _summary(rows: list[MediaUserImportRow]) -> MediaUserImportResponse:
    counts = Counter(row.status for row in rows)
    return MediaUserImportResponse(
        rows=rows,
        ready=counts["ready_to_create"] + counts["ready_to_link"],
        created=counts["created"],
        linked=counts["linked"],
        skipped=counts["skipped"] + counts["needs_review"],
        errors=counts["conflict"] + counts["error"],
    )


async def import_media_users(
    db: AsyncSession,
    request: MediaUserImportRequest,
    *,
    preview: bool,
    actor: User | None,
    actor_label: str,
) -> MediaUserImportResponse:
    if request.role is UserRole.ADMIN and (
        actor is None or actor.role is not UserRole.ADMIN
    ):
        raise HTTPException(
            status_code=403,
            detail="Only administrators using the UI can import admin accounts",
        )
    # Release the authentication/config read transaction before waiting for the provider.
    provider = await get_media_auth_provider(
        db, service_config_id=request.service_config_id
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="Enabled media server not found")
    config = await db.get(ServiceConfig, provider.service_config_id)
    assert config is not None
    api_key = config.api_key
    assert api_key is not None
    config_snapshot = (config.service_type, config.base_url, api_key)
    actor_id = actor.id if actor else None
    await db.commit()
    sources = await discover_import_users(provider, api_key)
    source_ids = {source.identity.source_user_id for source in sources}
    requested_ids = set(request.source_user_ids or []) | {
        link.source_user_id for link in request.links
    }
    if not requested_ids <= source_ids:
        raise HTTPException(
            status_code=409,
            detail="Selected media users no longer exist. Preview again; no changes were made.",
        )
    try:
        if not preview:
            # SQLite serializes this short write transaction with login and other imports.
            # No provider HTTP calls occur while the writer lock is held.
            await db.execute(text("BEGIN IMMEDIATE"))
        config = await db.get(
            ServiceConfig, request.service_config_id, populate_existing=True
        )
        if (
            config is None
            or not config.enabled
            or (config.service_type, config.base_url, config.api_key) != config_snapshot
        ):
            raise HTTPException(
                status_code=409,
                detail="Media server configuration changed. Preview again.",
            )
        users = list(
            (await db.execute(select(User).execution_options(populate_existing=True)))
            .scalars()
            .all()
        )
        if actor_id is not None:
            actor = next((user for user in users if user.id == actor_id), None)
            if (
                actor is None
                or not actor.is_active
                or (
                    actor.role is not UserRole.ADMIN
                    and Permission.MANAGE_USERS.value not in actor.permissions
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail="User management access is no longer available",
                )
        pages = (
            None
            if request.role is UserRole.ADMIN
            else (
                await default_allowed_pages_for_media_user(db)
                if request.use_default_page_access
                else [page.value for page in request.allowed_pages]
                if request.allowed_pages is not None
                else None
            )
        )
        _check_grants(actor, request, pages)
        identities = list(
            (
                await db.execute(
                    select(MediaUserIdentity).where(
                        MediaUserIdentity.source_service_config_id
                        == request.service_config_id,
                        MediaUserIdentity.source_service == provider.service_type,
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = _rows(sources, users, identities, request, actor, pages)
        if preview:
            return _summary(rows)
        selected = (
            set(request.source_user_ids)
            if request.source_user_ids is not None
            else {
                row.source_user_id
                for row in rows
                if row.status in {"ready_to_create", "ready_to_link"}
            }
        )
        for row in rows:
            if row.source_user_id not in selected:
                continue
            expected = request.expected_revisions.get(row.source_user_id)
            if (
                row.status != "skipped"
                or row.message != "Identity is already linked; no changes"
            ):
                if expected is not None and row.revision != expected:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{row.username}: account or provider details changed. Preview again; no changes were made.",
                    )
            if row.status in {"conflict", "error", "needs_review"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"{row.username}: {row.message}. No changes were made.",
                )
        items = {source.identity.source_user_id: source.identity for source in sources}
        for row in rows:
            if row.source_user_id not in selected:
                if row.status != "skipped":
                    row.status, row.message = "skipped", "Not selected; no changes"
                continue
            if row.status == "skipped":
                continue
            item = items[row.source_user_id]
            if row.status == "ready_to_create":
                assert row.local_username is not None
                user = User(
                    username=row.local_username,
                    password_hash="",
                    email=item.email,
                    display_name=_display_name(item.display_name, row.local_username),
                    role=request.role,
                    permissions=[p.value for p in request.permissions],
                    allowed_pages=list(pages) if pages is not None else None,
                    require_password_change=False,
                )
                db.add(user)
                await db.flush()
                await auto_enable_system_email(db, user)
                row.account = account_summary(user)
                row.status, row.message = (
                    "created",
                    "Account created and media identity linked",
                )
            else:
                assert row.account is not None
                user = next(user for user in users if user.id == row.account.id)
                row.status, row.message = (
                    "linked",
                    "Existing account linked; settings preserved",
                )
            await upsert_media_identity(
                db, item=item, user_id=user.id, now=datetime.now(UTC)
            )
            await resolve_singleton_notice(
                db, dedupe_key=media_auth_conflict_notice_key(item)
            )
        await db.commit()
    except (IntegrityError, OperationalError):
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Accounts changed or the database is busy. Preview again; no users were imported.",
        ) from None
    except Exception:
        await db.rollback()
        raise
    summary = _summary(rows)
    LOG.info(
        f"Media user import by {actor_label}: config {request.service_config_id}, {summary.created} created, {summary.linked} linked, {summary.skipped} skipped"
    )
    return summary


async def stage_media_identity(
    db: AsyncSession,
    *,
    service_config_id: int,
    source_user_id: str,
) -> int:
    """An explicit admin UI action, separate from read-only import preview.

    Newly discovered conflicts have no identity row to select in the existing
    manual linking screen. Stage just that identity without granting any access.
    """
    provider = await get_media_auth_provider(db, service_config_id=service_config_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Enabled media server not found")
    config = await db.get(ServiceConfig, service_config_id)
    assert config is not None and config.api_key is not None
    snapshot = (config.service_type, config.base_url, config.api_key)
    await db.commit()
    sources = await discover_import_users(provider, snapshot[2])
    source = next(
        (s for s in sources if s.identity.source_user_id == source_user_id), None
    )
    if source is None or source.skip_reason:
        raise HTTPException(
            status_code=409,
            detail="Media user is no longer eligible. Refresh the preview.",
        )
    try:
        await db.execute(text("BEGIN IMMEDIATE"))
        config = await db.get(ServiceConfig, service_config_id, populate_existing=True)
        if (
            config is None
            or not config.enabled
            or (config.service_type, config.base_url, config.api_key) != snapshot
        ):
            raise HTTPException(
                status_code=409,
                detail="Media server configuration changed. Refresh the preview.",
            )
        row = await upsert_media_identity(
            db, item=source.identity, preserve_user_id=True
        )
        await db.flush()
        identity_id = row.id
        await db.commit()
        return identity_id
    except (IntegrityError, OperationalError):
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Identity changed or the database is busy. Refresh the preview.",
        ) from None
    except Exception:
        await db.rollback()
        raise
