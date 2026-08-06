from datetime import UTC, datetime, timedelta
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Annotated, Any
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logger import LOG
from backend.core.settings import settings
from backend.core.utils.datetime_utils import ensure_utc
from backend.database import get_db
from backend.database.models import User, UserSession
from backend.enums import PageAccess, Permission, UserRole

# password hashing with Argon2
argon_ph = PasswordHasher()

# cookie name for JWT token
COOKIE_NAME = "access_token"

# Session lifetime (24 hours)
# The sliding session middleware will refresh the cookie when
# less than half the TTL remains, so active users stay logged in.
SESSION_TTL = timedelta(hours=24)
SESSION_TTL_SECONDS = int(SESSION_TTL.total_seconds())  # 86400
SESSION_LAST_SEEN_TOUCH_INTERVAL = timedelta(minutes=5)
SESSION_TOUCH_BUSY_TIMEOUT_MS = 250
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30000
ORIGINAL_CLIENT_HOST_STATE_KEY = "reclaimerr_original_client_host"

FORWARD_AUTH_WARNED_PEERS_MAX = 256
_forward_auth_warned_peers: set[str] = set()
_forward_auth_warned_missing_wrapper = False


@lru_cache(maxsize=8)
def _parse_trusted_networks(raw: str) -> tuple[IPv4Network | IPv6Network, ...]:
    """Parse the forward-auth allowlist once per distinct configured value.

    Keyed on the raw string rather than cached on Settings, so tests that
    monkeypatch settings.forward_auth_trusted_proxies get a fresh entry instead
    of a stale one.
    """
    # Settings validation already rejects 0.0.0.0/0 and ::/0, but Settings does
    # not set validate_assignment, so a direct attribute assignment bypasses
    # that validator. Re-check here as defence in depth so an all-address
    # range can never confer trust, however the value arrived.
    return tuple(
        network
        for network in (
            ip_network(entry.strip(), strict=False)
            for entry in raw.split(",")
            if entry.strip()
        )
        if network.prefixlen != 0
    )


def _log_untrusted_forward_auth_peer(peer: str) -> None:
    """Warn once per peer, then drop to debug, so probing cannot flood the log."""
    message = (
        f"Ignored {settings.forward_auth_user_header} authentication header "
        f"from untrusted peer {peer}"
    )
    if peer in _forward_auth_warned_peers:
        LOG.debug(message)
        return
    if len(_forward_auth_warned_peers) >= FORWARD_AUTH_WARNED_PEERS_MAX:
        _forward_auth_warned_peers.clear()
    _forward_auth_warned_peers.add(peer)
    LOG.warning(message)


def _log_missing_proxy_wrapper() -> None:
    """Warn once per process that the proxy header wrapper did not run."""
    global _forward_auth_warned_missing_wrapper
    message = (
        "Forward auth is enabled but the proxy header wrapper did not record "
        "the socket peer, so this request cannot be trusted and forward auth "
        "is inactive. Serve backend.api.main:app rather than the bare FastAPI "
        "app, and disable any other proxy-header middleware in front of it."
    )
    if _forward_auth_warned_missing_wrapper:
        LOG.debug(message)
        return
    _forward_auth_warned_missing_wrapper = True
    LOG.warning(message)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against an Argon2 hash."""
    try:
        return argon_ph.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False


def get_password_hash(password: str) -> str:
    """Hash a password using Argon2."""
    return argon_ph.hash(password)


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
    token_version: int = 0,
    session_id: str | None = None,
) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + SESSION_TTL

    token_payload: dict[str, Any] = {"exp": expire, "tv": token_version}
    if session_id:
        token_payload["sid"] = session_id
    to_encode.update(token_payload)

    if settings.jwt_secret is None:
        raise RuntimeError("JWT secret is not set")
    encoded_jwt = jwt.encode(
        to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    return encoded_jwt


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT token."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )


def generate_session_id() -> str:
    """Generate an opaque session identifier."""
    return uuid4().hex


def build_session_expires_at() -> datetime:
    """Return the expiration timestamp for a new session."""
    return datetime.now(UTC) + SESSION_TTL


def get_session_id_from_payload(payload: dict[str, Any]) -> str | None:
    """Extract a validated session id from JWT payload."""
    sid = payload.get("sid")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def get_request_session_id(request: Request) -> str | None:
    """Get current request session id, if authentication dependency populated it."""
    session_id = getattr(request.state, "session_id", None)
    if isinstance(session_id, str) and session_id:
        return session_id
    return None


def get_request_client_ip(request: Request) -> str | None:
    """Best effort client IP extraction."""
    xff = request.headers.get("x-forwarded-for")
    if isinstance(xff, str) and xff.strip():
        ip = xff.split(",")[0].strip()
        return ip[:64] if ip else None

    x_real_ip = request.headers.get("x-real-ip")
    if isinstance(x_real_ip, str) and x_real_ip.strip():
        return x_real_ip.strip()[:64]

    if request.client and request.client.host:
        return request.client.host[:64]

    return None


def get_request_user_agent(request: Request) -> str | None:
    """Get a bounded User-Agent string from request headers."""
    value = request.headers.get("user-agent")
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized[:512]
    return None


def _get_original_client_host(request: Request) -> str | None:
    """Return the socket peer captured before proxy headers rewrite the scope."""
    state = request.scope.get("state")
    if isinstance(state, dict):
        original_host = state.get(ORIGINAL_CLIENT_HOST_STATE_KEY)
        if isinstance(original_host, str) and original_host:
            return original_host

    # No snapshot means the outer proxy-header wrapper did not run, so
    # request.client may already have been rewritten from X-Forwarded-For by
    # some other layer. Treat the peer as unknown rather than trusting it.
    return None


def _is_forward_auth_proxy_trusted(request: Request) -> bool:
    """Check the direct socket peer against the dedicated forward-auth allowlist."""
    client_host = _get_original_client_host(request)
    if client_host is None:
        return False
    try:
        client_ip = ip_address(client_host)
    except ValueError:
        return False

    return any(
        client_ip in network
        for network in _parse_trusted_networks(settings.forward_auth_trusted_proxies)
    )


async def _get_forward_auth_user(
    request: Request,
    db: AsyncSession,
) -> User | None:
    """Resolve an asserted proxy identity without creating or elevating accounts."""
    if not settings.forward_auth_enabled:
        return None

    asserted_usernames = request.headers.getlist(settings.forward_auth_user_header)
    if not asserted_usernames:
        return None

    client_host = _get_original_client_host(request)
    if client_host is None:
        _log_missing_proxy_wrapper()
        return None

    if not _is_forward_auth_proxy_trusted(request):
        _log_untrusted_forward_auth_peer(client_host)
        return None

    if len(asserted_usernames) != 1:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Trusted proxy supplied multiple username headers",
        )

    username = asserted_usernames[0].strip()
    if not username or len(username) > 32:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Trusted proxy supplied an invalid username",
        )

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        LOG.warning(f"Trusted proxy user {username!r} is not configured in Reclaimerr")
        if settings.forward_auth_allow_local_fallback:
            # Recovery mode: let the request try local cookie auth so an admin can
            # sign in and create the missing user. Still needs a valid JWT and a
            # live session row, so this is not a bypass.
            return None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Trusted proxy user is not configured",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    request.state.auth_method = "forward_auth"
    return user


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get the current user from a trusted proxy identity or HttpOnly JWT cookie."""
    forward_auth_user = await _get_forward_auth_user(request, db)
    if forward_auth_user is not None:
        return forward_auth_user

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_token(token)

    user_id_str = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )

    result = await db.execute(select(User).where(User.id == int(user_id_str)))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )

    # verify token version matches (allows invalidation on password change/logout)
    token_version = payload.get("tv", 0)
    if token_version != user.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked"
        )

    session_id = get_session_id_from_payload(payload)
    if session_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is invalid. Please login again.",
        )

    session_result = await db.execute(
        select(UserSession).where(
            UserSession.user_id == user.id,
            UserSession.session_id == session_id,
        )
    )
    user_session = session_result.scalar_one_or_none()
    if user_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session not found"
        )

    if user_session.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has been revoked"
        )

    now = datetime.now(UTC)
    if ensure_utc(user_session.expires_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has expired"
        )

    request.state.session_id = session_id

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is disabled"
        )

    if (
        user_session.last_seen_at is None
        or (now - ensure_utc(user_session.last_seen_at))
        >= SESSION_LAST_SEEN_TOUCH_INTERVAL
    ):
        await _touch_user_session_last_seen(db, user_session.id, now)

    return user


async def _touch_user_session_last_seen(
    db: AsyncSession, session_row_id: int, now: datetime
) -> None:
    """Best-effort session activity write.

    This value is useful account/session metadata, but it should not make normal API
    requests fail when a heavy SQLite writer is active.
    """
    try:
        await db.execute(text(f"PRAGMA busy_timeout={SESSION_TOUCH_BUSY_TIMEOUT_MS}"))
        await db.execute(
            update(UserSession)
            .where(UserSession.id == session_row_id)
            .values(last_seen_at=now)
        )
        await db.commit()
    except OperationalError:
        await db.rollback()
        LOG.debug("Skipped session last_seen_at update because SQLite is busy")
    except Exception:
        await db.rollback()
        LOG.debug("Failed to update session last_seen_at", exc_info=True)
    finally:
        try:
            await db.execute(
                text(f"PRAGMA busy_timeout={DEFAULT_SQLITE_BUSY_TIMEOUT_MS}")
            )
        except Exception:
            LOG.debug("Failed to restore SQLite busy timeout", exc_info=True)


async def require_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require admin role."""
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    return current_user


def has_permission(user: User, permission: Permission) -> bool:
    """Check whether user has a specific permission, with admin bypass."""
    if user.role is UserRole.ADMIN:
        return True
    user_permissions = user.permissions or []
    return permission.value in user_permissions


def require_permission(permission: Permission) -> Any:
    """Dependency factory requiring a specific permission (admins always pass)."""

    async def _require_permission(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if not has_permission(current_user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{permission.value.replace('_', ' ').title()} permission required",
            )
        return current_user

    return _require_permission


def has_page_access(user: User, page: PageAccess) -> bool:
    """Check whether user may view a page, with admin and legacy bypasses."""
    if user.role is UserRole.ADMIN:
        return True

    allowed_pages = user.allowed_pages
    if allowed_pages is None:
        return True

    return page.value in allowed_pages


def require_page_access(page: PageAccess) -> Any:
    """Dependency factory requiring access to an app page."""

    async def _require_page_access(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if not has_page_access(current_user, page):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{page.value.replace('_', ' ').title()} page access required",
            )
        return current_user

    return _require_page_access
