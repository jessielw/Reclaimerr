from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import niquests
from niquests.exceptions import HTTPError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.database.models import MediaUserIdentity, ServiceConfig, User
from backend.enums import Permission, Service, UserRole
from backend.services.admin_notices import (
    resolve_singleton_notice,
    upsert_singleton_notice,
)
from backend.types import MEDIA_SERVERS

MediaAuthMode = Literal["credentials", "redirect"]

MEDIA_AUTH_DEVICE_ID = "reclaimerr-media-auth"
MEDIA_AUTH_CLIENT = "Reclaimerr"
MEDIA_AUTH_DEVICE = "Reclaimerr"
MEDIA_AUTH_VERSION = "1.0.0"
PLEX_CLIENT_ID_PREFIX = "reclaimerr"
PLEX_AUTH_BASE_URL = "https://app.plex.tv/auth#"
PLEX_TV_PINS_URL = "https://plex.tv/api/v2/pins"
PLEX_TV_USER_URL = "https://plex.tv/api/v2/user"
PLEX_PENDING_AUTH_TTL = timedelta(minutes=10)

MEDIA_AUTH_CONFLICT_NOTICE_KIND = "media_auth_conflict"


def normalize_username(value: str | None) -> str:
    return str(value or "").strip().lower()


def normalize_email(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


@dataclass(frozen=True, slots=True)
class DiscoveredMediaUser:
    source_service: Service
    source_service_config_id: int
    source_service_name: str
    source_user_id: str
    username: str
    username_normalized: str
    email: str | None
    display_name: str | None
    raw: dict[str, Any]


class MediaAuthError(Exception):
    """Base error for media authentication and provisioning."""


class MediaAuthProviderError(MediaAuthError):
    """Provider configuration or auth-flow error."""


class MediaAuthCredentialsError(MediaAuthError):
    """Invalid media credentials."""


class MediaAuthConflictError(MediaAuthError):
    """Identity matches multiple local users and requires admin action."""

    def __init__(
        self,
        message: str,
        *,
        identity: DiscoveredMediaUser,
        conflicting_user_ids: list[int],
    ) -> None:
        super().__init__(message)
        self.identity = identity
        self.conflicting_user_ids = conflicting_user_ids


class MediaAuthAccessDeniedError(MediaAuthError):
    """Media account authenticated but does not have access to selected server."""


@dataclass(frozen=True, slots=True)
class MediaAuthProvider:
    service_config_id: int
    service_type: Service
    name: str
    base_url: str
    auth_mode: MediaAuthMode


@dataclass(frozen=True, slots=True)
class PlexPendingAuth:
    state: str
    service_config_id: int
    client_identifier: str
    pin_id: int
    pin_code: str
    return_to: str | None
    created_at: datetime


_PLEX_PENDING_AUTHS: dict[str, PlexPendingAuth] = {}


def _cleanup_pending_plex_auths(now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    expired_states = [
        state
        for state, pending in _PLEX_PENDING_AUTHS.items()
        if now - pending.created_at > PLEX_PENDING_AUTH_TTL
    ]
    for state in expired_states:
        _PLEX_PENDING_AUTHS.pop(state, None)


def provider_auth_mode(service_type: Service) -> MediaAuthMode:
    if service_type is Service.PLEX:
        return "redirect"
    return "credentials"


def media_auth_conflict_notice_key_for_source(
    source_service: Service,
    source_service_config_id: int,
    source_user_id: str,
) -> str:
    return (
        f"media-auth-conflict:"
        f"{source_service.value}:"
        f"{source_service_config_id}:"
        f"{source_user_id}"
    )


def media_auth_conflict_notice_key(identity: DiscoveredMediaUser) -> str:
    return media_auth_conflict_notice_key_for_source(
        identity.source_service,
        identity.source_service_config_id,
        identity.source_user_id,
    )


def _decrypt_api_key(value: str) -> str:
    try:
        return fer_decrypt(value)
    except Exception:
        return value


def _display_name(value: str | None, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    if len(raw) < 3:
        raw = fallback
    return raw[:32]


def _sanitize_username(value: str, *, fallback: str = "media_user") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)
    cleaned = cleaned.strip("_-") or fallback
    if len(cleaned) < 5:
        cleaned = f"{cleaned}_user"
    return cleaned[:32]


def _safe_unique_username(base: str, used_usernames: set[str]) -> str:
    candidate_base = _sanitize_username(base)
    candidate = candidate_base
    suffix = 1
    while normalize_username(candidate) in used_usernames:
        suffix_text = f"_{suffix}"
        candidate = f"{candidate_base[: 32 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _media_browser_auth_header(token: str | None = None) -> str:
    parts = [
        f'Client="{MEDIA_AUTH_CLIENT}"',
        f'Device="{MEDIA_AUTH_DEVICE}"',
        f'DeviceId="{MEDIA_AUTH_DEVICE_ID}"',
        f'Version="{MEDIA_AUTH_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


async def list_media_auth_providers(db: AsyncSession) -> list[MediaAuthProvider]:
    """List all enabled media auth providers with valid configuration."""
    result = await db.execute(
        select(ServiceConfig)
        .where(
            ServiceConfig.enabled.is_(True),
            ServiceConfig.service_type.in_(MEDIA_SERVERS),
            ServiceConfig.base_url.isnot(None),
            ServiceConfig.api_key.isnot(None),
        )
        .order_by(ServiceConfig.service_type.asc(), ServiceConfig.name.asc())
    )
    providers: list[MediaAuthProvider] = []
    for config in result.scalars().all():
        providers.append(
            MediaAuthProvider(
                service_config_id=config.id,
                service_type=config.service_type,
                name=config.name or config.service_type.value,
                base_url=config.base_url,
                auth_mode=provider_auth_mode(config.service_type),
            )
        )
    return providers


async def get_media_auth_provider(
    db: AsyncSession,
    *,
    service_config_id: int,
) -> MediaAuthProvider | None:
    """Get the media auth provider for the given service config ID, or None if not
    found or not properly configured."""
    result = await db.execute(
        select(ServiceConfig).where(
            ServiceConfig.id == service_config_id,
            ServiceConfig.enabled.is_(True),
            ServiceConfig.service_type.in_(MEDIA_SERVERS),
            ServiceConfig.base_url.isnot(None),
            ServiceConfig.api_key.isnot(None),
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        return None
    return MediaAuthProvider(
        service_config_id=config.id,
        service_type=config.service_type,
        name=config.name or config.service_type.value,
        base_url=config.base_url,
        auth_mode=provider_auth_mode(config.service_type),
    )


async def _get_service_config_for_provider(
    db: AsyncSession,
    *,
    provider: MediaAuthProvider,
) -> ServiceConfig:
    result = await db.execute(
        select(ServiceConfig).where(ServiceConfig.id == provider.service_config_id)
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise MediaAuthProviderError("Media auth provider is not configured")
    return config


def _build_discovered_user(
    *,
    provider: MediaAuthProvider,
    source_user_id: str,
    username: str,
    email: str | None,
    display_name: str | None,
    raw: dict[str, Any],
) -> DiscoveredMediaUser:
    normalized_username = normalize_username(username or display_name or email or "")
    if not source_user_id or not normalized_username:
        raise MediaAuthProviderError("Media provider returned incomplete user profile")
    return DiscoveredMediaUser(
        source_service=provider.service_type,
        source_service_config_id=provider.service_config_id,
        source_service_name=provider.name,
        source_user_id=source_user_id,
        username=username or display_name or source_user_id,
        username_normalized=normalized_username,
        email=normalize_email(email),
        display_name=display_name or username or source_user_id,
        raw=raw,
    )


async def authenticate_emby_family_credentials(
    *,
    provider: MediaAuthProvider,
    username: str,
    password: str,
) -> DiscoveredMediaUser:
    """Authenticate the given Emby username and password and return the associated user profile."""
    payload = {"Username": username, "Pw": password}
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-emby-authorization": _media_browser_auth_header(),
        "authorization": _media_browser_auth_header(),
    }
    base_url = provider.base_url.rstrip("/")
    async with niquests.AsyncSession() as session:
        try:
            response = await session.post(
                f"{base_url}/Users/AuthenticateByName",
                headers=headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
        except HTTPError as exc:
            raise MediaAuthCredentialsError("Invalid media server credentials") from exc
        except Exception as exc:
            raise MediaAuthProviderError("Media server authentication failed") from exc

        data = response.json() if response.content else {}
        if not isinstance(data, dict):
            raise MediaAuthProviderError("Media server returned invalid auth response")

        user_payload = data.get("User")
        if not isinstance(user_payload, dict):
            raise MediaAuthProviderError("Media server auth response is missing user")

        is_disabled = bool(
            (user_payload.get("Policy") or {}).get("IsDisabled")
            if isinstance(user_payload.get("Policy"), dict)
            else False
        )
        if is_disabled:
            raise MediaAuthAccessDeniedError("Media server account is disabled")

        source_user_id = str(user_payload.get("Id") or "").strip()
        display_name = str(user_payload.get("Name") or "").strip() or None
        server_username = (
            str(user_payload.get("Username") or "").strip() or display_name or username
        )
        email = (
            str(user_payload.get("PrimaryEmail") or "").strip()
            or str(user_payload.get("Email") or "").strip()
            or None
        )

        return _build_discovered_user(
            provider=provider,
            source_user_id=source_user_id,
            username=server_username,
            email=email,
            display_name=display_name or server_username,
            raw={
                "auth_user": user_payload,
                "provider": provider.service_type.value,
            },
        )


async def start_plex_pin_flow(
    *,
    provider: MediaAuthProvider,
    callback_url: str,
    return_to: str | None = None,
) -> str:
    """Start a Plex PIN authentication flow and return the URL to direct the user to."""
    if provider.service_type is not Service.PLEX:
        raise MediaAuthProviderError("Plex PIN flow is only available for Plex")

    _cleanup_pending_plex_auths()
    state = uuid4().hex
    client_identifier = f"{PLEX_CLIENT_ID_PREFIX}-{state}"
    pin_headers = {
        "accept": "application/json",
        "X-Plex-Product": MEDIA_AUTH_CLIENT,
        "X-Plex-Client-Identifier": client_identifier,
    }

    async with niquests.AsyncSession() as session:
        try:
            response = await session.post(
                PLEX_TV_PINS_URL,
                params={"strong": "true"},
                headers=pin_headers,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            raise MediaAuthProviderError("Failed to initialize Plex login") from exc

        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise MediaAuthProviderError("Invalid Plex PIN response")
        pin_id = int(payload.get("id") or 0)
        pin_code = str(payload.get("code") or "").strip()
        if pin_id <= 0 or not pin_code:
            raise MediaAuthProviderError("Plex PIN response was incomplete")

    _PLEX_PENDING_AUTHS[state] = PlexPendingAuth(
        state=state,
        service_config_id=provider.service_config_id,
        client_identifier=client_identifier,
        pin_id=pin_id,
        pin_code=pin_code,
        return_to=return_to,
        created_at=datetime.now(UTC),
    )

    parsed_callback = urlsplit(callback_url)
    callback_query = dict(parse_qsl(parsed_callback.query, keep_blank_values=True))
    callback_query["state"] = state
    forward_url = urlunsplit(
        (
            parsed_callback.scheme,
            parsed_callback.netloc,
            parsed_callback.path,
            urlencode(callback_query),
            parsed_callback.fragment,
        )
    )

    params = urlencode(
        {
            "clientID": client_identifier,
            "code": pin_code,
            "context[device][product]": MEDIA_AUTH_CLIENT,
            "context[device][version]": MEDIA_AUTH_VERSION,
            "forwardUrl": forward_url,
        }
    )
    return f"{PLEX_AUTH_BASE_URL}?{params}"


def pop_pending_plex_auth(state: str) -> PlexPendingAuth | None:
    _cleanup_pending_plex_auths()
    return _PLEX_PENDING_AUTHS.pop(state, None)


async def exchange_plex_pin_for_token(pending: PlexPendingAuth) -> str:
    """Exchange the approved Plex PIN for a user token."""
    async with niquests.AsyncSession() as session:
        try:
            response = await session.get(
                f"{PLEX_TV_PINS_URL}/{pending.pin_id}",
                params={"code": pending.pin_code},
                headers={
                    "accept": "application/json",
                    "X-Plex-Client-Identifier": pending.client_identifier,
                },
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            raise MediaAuthProviderError("Failed to complete Plex login") from exc

        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise MediaAuthProviderError("Invalid Plex login response")
        token = str(payload.get("authToken") or "").strip()
        if not token:
            raise MediaAuthCredentialsError("Plex sign-in was not approved")
        return token


async def authenticate_plex_token(
    db: AsyncSession,
    *,
    provider: MediaAuthProvider,
    plex_user_token: str,
) -> DiscoveredMediaUser:
    """Authenticate the given Plex user token and return the associated user profile, verifying
    access to the configured Plex server."""
    config = await _get_service_config_for_provider(db, provider=provider)
    server_api_key = _decrypt_api_key(config.api_key)
    base_url = provider.base_url.rstrip("/")

    async with niquests.AsyncSession() as session:
        try:
            user_response = await session.get(
                PLEX_TV_USER_URL,
                headers={
                    "accept": "application/json",
                    "X-Plex-Token": plex_user_token,
                },
                timeout=30,
            )
            user_response.raise_for_status()
        except HTTPError as exc:
            raise MediaAuthCredentialsError("Plex sign-in failed") from exc
        except Exception as exc:
            raise MediaAuthProviderError("Plex user lookup failed") from exc

        user_payload = user_response.json() if user_response.content else {}
        if not isinstance(user_payload, dict):
            raise MediaAuthProviderError("Invalid Plex user response")

        source_user_id = str(user_payload.get("id") or "").strip()
        plex_username = str(user_payload.get("username") or "").strip()
        plex_title = str(user_payload.get("title") or "").strip()
        plex_email = normalize_email(str(user_payload.get("email") or "").strip())
        display_name = plex_title or plex_username or plex_email or source_user_id
        username = plex_username or display_name

        try:
            server_response = await session.get(
                f"{base_url}/library/sections",
                headers={
                    "accept": "application/json",
                    "X-Plex-Token": plex_user_token,
                },
                timeout=30,
            )
            server_response.raise_for_status()
        except HTTPError as exc:
            raise MediaAuthAccessDeniedError(
                "Plex account does not have access to this server"
            ) from exc
        except Exception as exc:
            raise MediaAuthProviderError(
                "Plex server access verification failed"
            ) from exc

        # cross check membership against the configured server owner's visible user list
        has_membership_hint = False
        try:
            users_response = await session.get(
                "https://plex.tv/api/users",
                params={
                    "X-Plex-Client-Identifier": MEDIA_AUTH_DEVICE_ID,
                    "X-Plex-Token": server_api_key,
                },
                headers={"Accept": "application/xml"},
                timeout=60,
            )
            users_response.raise_for_status()
            membership_xml = users_response.text or ""
            has_membership_hint = (
                source_user_id and f'id="{source_user_id}"' in membership_xml
            ) or (plex_email and plex_email in membership_xml)
        except Exception:
            LOG.debug(
                f"Plex membership hint check failed for provider {provider.service_config_id} and "
                f"user id {source_user_id}"
            )
        if not has_membership_hint:
            LOG.debug(
                f"Plex membership hint mismatch for provider {provider.service_config_id} and "
                f"user id {source_user_id}"
            )

    return _build_discovered_user(
        provider=provider,
        source_user_id=source_user_id,
        username=username,
        email=plex_email,
        display_name=display_name,
        raw={
            "plex_user": user_payload,
            "provider": provider.service_type.value,
            "membership_hint": bool(has_membership_hint),
        },
    )


async def _create_conflict_notice(
    db: AsyncSession,
    *,
    identity: DiscoveredMediaUser,
    conflicting_user_ids: list[int],
) -> None:
    """Create or update an admin notice about a media auth conflict that requires review."""
    await upsert_singleton_notice(
        db,
        dedupe_key=media_auth_conflict_notice_key(identity),
        kind=MEDIA_AUTH_CONFLICT_NOTICE_KIND,
        severity="warning",
        title="Media Login Needs Admin Link",
        message=(
            f"User '{identity.username}' from {identity.source_service_name} "
            "matched multiple local users and could not be linked automatically."
        ),
        action_label="Review User Links",
        action_href="/settings?tab=users",
        context_json={
            "source_service": identity.source_service.value,
            "source_service_config_id": identity.source_service_config_id,
            "source_user_id": identity.source_user_id,
            "username": identity.username,
            "email": identity.email,
            "conflicting_user_ids": conflicting_user_ids,
        },
    )


async def upsert_media_identity(
    db: AsyncSession,
    *,
    item: DiscoveredMediaUser,
    user_id: int | None = None,
    preserve_user_id: bool = False,
    now: datetime | None = None,
) -> MediaUserIdentity:
    """Create or update a media identity record for the given discovered user and link
    to the specified local user ID."""
    now = now or datetime.now(UTC)
    row = (
        await db.execute(
            select(MediaUserIdentity).where(
                MediaUserIdentity.source_service == item.source_service,
                MediaUserIdentity.source_service_config_id
                == item.source_service_config_id,
                MediaUserIdentity.source_user_id == item.source_user_id,
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = MediaUserIdentity(
            source_service=item.source_service,
            source_service_config_id=item.source_service_config_id,
            source_user_id=item.source_user_id,
            username=item.username,
            username_normalized=item.username_normalized,
            user_id=user_id,
            email=item.email,
            display_name=item.display_name,
            raw=item.raw,
            last_seen_at=now,
            linked_at=now if user_id is not None else None,
        )
        db.add(row)
        return row

    if not preserve_user_id:
        row.user_id = user_id
    row.username = item.username
    row.username_normalized = item.username_normalized
    row.email = item.email
    row.display_name = item.display_name
    row.raw = item.raw
    row.last_seen_at = now
    if user_id is not None and row.linked_at is None:
        row.linked_at = now
    return row


async def resolve_or_create_user_for_identity(
    db: AsyncSession,
    *,
    identity: DiscoveredMediaUser,
) -> User:
    """Find or create a local user account for the given media identity, and link them."""
    now = datetime.now(UTC)

    existing_identity = (
        await db.execute(
            select(MediaUserIdentity).where(
                MediaUserIdentity.source_service == identity.source_service,
                MediaUserIdentity.source_service_config_id
                == identity.source_service_config_id,
                MediaUserIdentity.source_user_id == identity.source_user_id,
            )
        )
    ).scalar_one_or_none()

    if existing_identity is not None and existing_identity.user_id is not None:
        user = await db.get(User, existing_identity.user_id)
        if user is not None:
            await upsert_media_identity(db, item=identity, user_id=user.id, now=now)
            await resolve_singleton_notice(
                db,
                dedupe_key=media_auth_conflict_notice_key(identity),
            )
            return user

    # match by email first
    email_candidates: Sequence[User] = []
    if identity.email:
        email_candidates = (
            (
                await db.execute(
                    select(User).where(func.lower(User.email) == identity.email.lower())
                )
            )
            .scalars()
            .all()
        )
        if len(email_candidates) == 1:
            user = email_candidates[0]
            await upsert_media_identity(db, item=identity, user_id=user.id, now=now)
            await resolve_singleton_notice(
                db,
                dedupe_key=media_auth_conflict_notice_key(identity),
            )
            return user
        if len(email_candidates) > 1:
            conflicting_user_ids = sorted(user.id for user in email_candidates)
            await upsert_media_identity(
                db,
                item=identity,
                user_id=None,
                preserve_user_id=True,
                now=now,
            )
            await _create_conflict_notice(
                db,
                identity=identity,
                conflicting_user_ids=conflicting_user_ids,
            )
            raise MediaAuthConflictError(
                "Media account matches multiple local users by email",
                identity=identity,
                conflicting_user_ids=conflicting_user_ids,
            )

    username_candidates = (
        (
            await db.execute(
                select(User).where(
                    func.lower(User.username) == identity.username_normalized
                )
            )
        )
        .scalars()
        .all()
    )
    if len(username_candidates) == 1:
        user = username_candidates[0]
        await upsert_media_identity(db, item=identity, user_id=user.id, now=now)
        await resolve_singleton_notice(
            db,
            dedupe_key=media_auth_conflict_notice_key(identity),
        )
        return user
    if len(username_candidates) > 1:
        conflicting_user_ids = sorted(user.id for user in username_candidates)
        await upsert_media_identity(
            db,
            item=identity,
            user_id=None,
            preserve_user_id=True,
            now=now,
        )
        await _create_conflict_notice(
            db,
            identity=identity,
            conflicting_user_ids=conflicting_user_ids,
        )
        raise MediaAuthConflictError(
            "Media account matches multiple local users by username",
            identity=identity,
            conflicting_user_ids=conflicting_user_ids,
        )

    used_usernames = {
        normalize_username(username)
        for username in (await db.execute(select(User.username))).scalars().all()
    }
    desired_username = (
        identity.email.split("@", 1)[0] if identity.email else identity.username
    )
    local_username = _safe_unique_username(desired_username, used_usernames)
    new_user = User(
        username=local_username,
        password_hash="",
        email=identity.email,
        display_name=_display_name(identity.display_name, local_username),
        role=UserRole.USER,
        permissions=[Permission.REQUEST.value],
        is_active=True,
        require_password_change=False,
    )
    db.add(new_user)
    await db.flush()
    await upsert_media_identity(db, item=identity, user_id=new_user.id, now=now)
    await resolve_singleton_notice(
        db,
        dedupe_key=media_auth_conflict_notice_key(identity),
    )
    return new_user
