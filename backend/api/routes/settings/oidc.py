from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.core.encryption import fer_decrypt, fer_encrypt
from backend.core.oidc import (
    OIDCConfigError,
    OIDCError,
    OIDCExchangeError,
    OIDCValidationError,
    fetch_provider_metadata,
    normalize_scopes,
    normalize_token_endpoint_auth_method,
)
from backend.database import get_db
from backend.database.models import OIDCSettings, User
from backend.models.settings import (
    OIDCSettingsResponse,
    OIDCSettingsUpdate,
    OIDCTestResponse,
)

router = APIRouter(tags=["settings", "oidc"])


async def _get_or_create_oidc_settings(db: AsyncSession) -> OIDCSettings:
    result = await db.execute(select(OIDCSettings))
    existing = result.scalars().first()
    if existing is not None:
        return existing

    created = OIDCSettings()
    db.add(created)
    await db.commit()
    await db.refresh(created)
    return created


def _to_response(settings_row: OIDCSettings) -> OIDCSettingsResponse:
    return OIDCSettingsResponse(
        enabled=settings_row.enabled,
        issuer_url=settings_row.issuer_url or "",
        client_id=settings_row.client_id or "",
        scopes=settings_row.scopes or "openid profile email",
        email_claim=settings_row.email_claim or "email",
        token_endpoint_auth_method=normalize_token_endpoint_auth_method(
            settings_row.token_endpoint_auth_method
        ),
        redirect_uri_override=settings_row.redirect_uri_override,
        client_secret_configured=bool(settings_row.client_secret),
        updated_at=settings_row.updated_at,
    )


def _resolve_client_secret(
    request: OIDCSettingsUpdate, settings_row: OIDCSettings
) -> str | None:
    if request.client_secret is not None:
        return request.client_secret
    if settings_row.client_secret:
        try:
            return fer_decrypt(settings_row.client_secret)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Stored OIDC client secret could not be decrypted. "
                    "Please re-enter and save the secret."
                ),
            ) from exc
    return None


def _validate_enabled_config(
    request: OIDCSettingsUpdate,
    *,
    client_secret: str | None,
) -> None:
    if not request.enabled:
        return
    if not request.issuer_url:
        raise HTTPException(status_code=400, detail="OIDC issuer URL is required")
    if not request.client_id:
        raise HTTPException(status_code=400, detail="OIDC client ID is required")
    if not client_secret:
        raise HTTPException(
            status_code=400,
            detail="OIDC client secret is required when OIDC is enabled",
        )


@router.get("/oidc", response_model=OIDCSettingsResponse)
async def get_oidc_settings(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OIDCSettingsResponse:
    """Get OIDC settings. There is always exactly one settings row, so this will create it
    if it doesn't exist yet."""
    settings_row = await _get_or_create_oidc_settings(db)
    return _to_response(settings_row)


@router.put("/oidc", response_model=OIDCSettingsResponse)
async def update_oidc_settings(
    request: OIDCSettingsUpdate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OIDCSettingsResponse:
    """Updates OIDC settings. There is always exactly one settings row, so this will create it
    if it doesn't exist yet."""
    settings_row = await _get_or_create_oidc_settings(db)
    resolved_secret = _resolve_client_secret(request, settings_row)
    _validate_enabled_config(request, client_secret=resolved_secret)

    settings_row.enabled = request.enabled
    settings_row.issuer_url = request.issuer_url
    settings_row.client_id = request.client_id
    settings_row.scopes = normalize_scopes(request.scopes)
    settings_row.email_claim = request.email_claim
    settings_row.token_endpoint_auth_method = request.token_endpoint_auth_method
    settings_row.redirect_uri_override = request.redirect_uri_override
    if request.client_secret is not None:
        settings_row.client_secret = fer_encrypt(request.client_secret)
    settings_row.updated_at = datetime.now(UTC)
    settings_row.updated_by_user_id = admin.id

    db.add(settings_row)
    await db.commit()
    await db.refresh(settings_row)
    return _to_response(settings_row)


@router.post("/oidc/test", response_model=OIDCTestResponse)
async def test_oidc_settings(
    request: OIDCSettingsUpdate,
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OIDCTestResponse:
    """Tests the provided OIDC settings by attempting to fetch the provider metadata. This
    does not save the settings. The client secret can be provided either in the request or
    it will be resolved from the existing settings if not provided in the request."""
    settings_row = await _get_or_create_oidc_settings(db)
    resolved_secret = _resolve_client_secret(request, settings_row)
    if not request.issuer_url:
        raise HTTPException(status_code=400, detail="OIDC issuer URL is required")
    if not request.client_id:
        raise HTTPException(status_code=400, detail="OIDC client ID is required")
    if not resolved_secret:
        raise HTTPException(status_code=400, detail="OIDC client secret is required")

    try:
        metadata = await fetch_provider_metadata(
            request.issuer_url,
            client_id=request.client_id,
            client_secret=resolved_secret,
            scopes=request.scopes,
            token_endpoint_auth_method=request.token_endpoint_auth_method,
            force_refresh=True,
        )
    except (OIDCConfigError, OIDCExchangeError, OIDCValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OIDCError as exc:
        raise HTTPException(
            status_code=400,
            detail="OIDC configuration test failed",
        ) from exc

    return OIDCTestResponse(
        success=True,
        issuer=metadata.issuer,
        authorization_endpoint=metadata.authorization_endpoint,
        token_endpoint=metadata.token_endpoint,
        jwks_uri=metadata.jwks_uri,
        userinfo_endpoint=metadata.userinfo_endpoint,
    )
