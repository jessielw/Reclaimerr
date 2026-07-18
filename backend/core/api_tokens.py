from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.utils.datetime_utils import ensure_utc
from backend.database import get_db
from backend.database.models import ApiToken

API_TOKEN_CANDIDATES_READ_SCOPE = "candidates:read"
API_TOKEN_CANDIDATES_MANAGE_SCOPE = "candidates:manage"
API_TOKEN_EVENTS_READ_SCOPE = "events:read"
API_TOKEN_MEDIA_READ_SCOPE = "media:read"
API_TOKEN_PROTECTIONS_READ_SCOPE = "protections:read"
API_TOKEN_PROTECTIONS_MANAGE_SCOPE = "protections:manage"
API_TOKEN_SYSTEM_READ_SCOPE = "system:read"
API_TOKEN_TASKS_READ_SCOPE = "tasks:read"
API_TOKEN_TASKS_RUN_SCOPE = "tasks:run"

API_TOKEN_SCOPES = {
    API_TOKEN_CANDIDATES_READ_SCOPE,
    API_TOKEN_CANDIDATES_MANAGE_SCOPE,
    API_TOKEN_EVENTS_READ_SCOPE,
    API_TOKEN_MEDIA_READ_SCOPE,
    API_TOKEN_PROTECTIONS_READ_SCOPE,
    API_TOKEN_PROTECTIONS_MANAGE_SCOPE,
    API_TOKEN_SYSTEM_READ_SCOPE,
    API_TOKEN_TASKS_READ_SCOPE,
    API_TOKEN_TASKS_RUN_SCOPE,
}
API_TOKEN_LAST_USED_TOUCH_INTERVAL = timedelta(hours=1)

_bearer = HTTPBearer(auto_error=False)


@dataclass(slots=True, frozen=True)
class ApiPrincipal:
    token_id: int
    name: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        if scope in self.scopes:
            return True
        implied_scopes = {
            API_TOKEN_CANDIDATES_READ_SCOPE: API_TOKEN_CANDIDATES_MANAGE_SCOPE,
            API_TOKEN_PROTECTIONS_READ_SCOPE: API_TOKEN_PROTECTIONS_MANAGE_SCOPE,
            API_TOKEN_TASKS_READ_SCOPE: API_TOKEN_TASKS_RUN_SCOPE,
        }
        return implied_scopes.get(scope) in self.scopes


def generate_api_token() -> tuple[str, str, str]:
    """Return the plaintext token, lookup prefix, and one-way token hash."""
    prefix = secrets.token_hex(6)
    plaintext = f"rcl_{prefix}_{secrets.token_urlsafe(32)}"
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, prefix, digest


def api_token_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def api_token_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != "rcl" or not parts[1] or not parts[2]:
        return None
    return parts[1]


async def get_api_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A bearer API token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    plaintext = credentials.credentials.strip()
    prefix = api_token_prefix(plaintext)
    if prefix is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    row = (
        await db.execute(select(ApiToken).where(ApiToken.token_prefix == prefix))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(
        row.token_hash, api_token_hash(plaintext)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = datetime.now(UTC)
    if row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if row.expires_at is not None and ensure_utc(row.expires_at) <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if (
        row.last_used_at is None
        or now - ensure_utc(row.last_used_at) >= API_TOKEN_LAST_USED_TOUCH_INTERVAL
    ):
        row.last_used_at = now
    request.state.api_token_id = row.id
    return ApiPrincipal(
        token_id=row.id,
        name=row.name,
        scopes=frozenset(str(scope) for scope in row.scopes),
    )


def require_api_scope(scope: str) -> Callable[..., Awaitable[ApiPrincipal]]:
    async def dependency(
        principal: Annotated[ApiPrincipal, Depends(get_api_principal)],
    ) -> ApiPrincipal:
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API token requires the {scope} scope",
            )
        return principal

    return dependency
