from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote_plus

from authlib.integrations.base_client import OAuthError
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    build_session_expires_at,
    create_access_token,
    decode_token,
    generate_session_id,
    get_request_client_ip,
    get_request_user_agent,
    get_session_id_from_payload,
    verify_password,
)
from backend.core.encryption import fer_decrypt
from backend.core.oidc import (
    OIDCConfigError,
    OIDCError,
    OIDCExchangeError,
    OIDCValidationError,
    create_oidc_client,
    extract_claim_as_string,
    extract_userinfo,
    load_provider_metadata,
    normalize_token_endpoint_auth_method,
)
from backend.core.settings import settings
from backend.database import get_db
from backend.database.models import OIDCSettings, User, UserSession
from backend.models.auth import (
    AuthResponse,
    LoginRequest,
    OIDCAuthStatusResponse,
    UserInfo,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)


async def _issue_login_session(
    *,
    request: Request,
    response: Response,
    user: User,
    db: AsyncSession,
) -> None:
    """Create a tracked user session and issue auth cookie."""
    session_id = generate_session_id()
    now = datetime.now(UTC)
    user.last_login_at = now

    user_session = UserSession(
        user_id=user.id,
        session_id=session_id,
        expires_at=build_session_expires_at(),
        user_agent=get_request_user_agent(request),
        ip_address=get_request_client_ip(request),
        last_seen_at=now,
    )
    db.add(user_session)
    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(
        data={"sub": str(user.id)},
        token_version=user.token_version,
        session_id=session_id,
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )


def _oidc_enabled(settings_row: OIDCSettings | None) -> bool:
    if settings_row is None:
        return False
    return bool(
        settings_row.enabled
        and settings_row.issuer_url
        and settings_row.client_id
        and settings_row.client_secret
    )


def _oidc_callback_redirect_uri(
    request: Request,
    settings_row: OIDCSettings,
) -> str:
    if settings_row.redirect_uri_override:
        return settings_row.redirect_uri_override
    return str(request.url_for("oidc_callback"))


def _auth_error_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/?auth_error={quote_plus(message)}",
        status_code=status.HTTP_302_FOUND,
    )


def _resolve_oidc_client_secret(settings_row: OIDCSettings) -> str:
    try:
        return fer_decrypt(settings_row.client_secret)
    except Exception as exc:
        raise OIDCConfigError(
            "OIDC client secret is invalid; ask an admin to re-save OIDC settings"
        ) from exc


def _create_configured_oidc_client(settings_row: OIDCSettings):
    resolved_secret = _resolve_oidc_client_secret(settings_row)
    return create_oidc_client(
        issuer_url=settings_row.issuer_url,
        client_id=settings_row.client_id,
        client_secret=resolved_secret,
        scopes=settings_row.scopes or "openid profile email",
        token_endpoint_auth_method=normalize_token_endpoint_auth_method(
            settings_row.token_endpoint_auth_method
        ),
    )


@router.get("/oidc/status", response_model=OIDCAuthStatusResponse)
async def oidc_auth_status(
    db: AsyncSession = Depends(get_db),
) -> OIDCAuthStatusResponse:
    result = await db.execute(select(OIDCSettings))
    settings_row = result.scalars().first()
    return OIDCAuthStatusResponse(enabled=_oidc_enabled(settings_row))


@router.get("/oidc/start")
async def oidc_start(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(OIDCSettings))
    settings_row = result.scalars().first()
    if settings_row is None:
        raise HTTPException(status_code=404, detail="OIDC login is not configured")
    if not _oidc_enabled(settings_row):
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")

    callback_uri = _oidc_callback_redirect_uri(request, settings_row)
    try:
        client = _create_configured_oidc_client(settings_row)
        return await client.authorize_redirect(request, callback_uri)
    except (OIDCConfigError, OIDCExchangeError, OIDCValidationError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OIDCError as exc:
        raise HTTPException(
            status_code=503,
            detail="OIDC provider unavailable",
        ) from exc


@router.get("/oidc/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    if error:
        return _auth_error_redirect("OIDC authentication was denied or failed")

    result = await db.execute(select(OIDCSettings))
    settings_row = result.scalars().first()
    if settings_row is None:
        return _auth_error_redirect("OIDC login is not configured")
    if not _oidc_enabled(settings_row):
        return _auth_error_redirect("OIDC login is not enabled")

    if not code or not state:
        return _auth_error_redirect("OIDC callback is missing required parameters")

    email_claim_name = settings_row.email_claim or "email"
    user: User | None = None

    try:
        client = _create_configured_oidc_client(settings_row)
        token_payload = await client.authorize_access_token(request)
        if not isinstance(token_payload, dict):
            raise OIDCValidationError("OIDC token endpoint returned invalid payload")

        metadata = await load_provider_metadata(
            client,
            issuer_url=settings_row.issuer_url,
        )
        claims = await extract_userinfo(
            client,
            token_payload,
            required_claim=email_claim_name,
        )

        subject = extract_claim_as_string(claims, "sub")
        if not subject:
            raise OIDCValidationError("OIDC response missing subject claim")

        email = extract_claim_as_string(claims, email_claim_name)

        user_result = await db.execute(
            select(User).where(
                User.oidc_issuer == metadata.issuer,
                User.oidc_subject == subject,
            )
        )
        user = user_result.scalar_one_or_none()

        if user is None:
            if not email:
                raise OIDCValidationError(
                    f"OIDC claim '{email_claim_name}' is missing from provider response"
                )

            email_result = await db.execute(
                select(User).where(func.lower(User.email) == email.strip().lower())
            )
            user = email_result.scalar_one_or_none()
            if user is None:
                raise OIDCValidationError(
                    "No local user matches the OIDC identity. "
                    "Create the user first and ensure email matches exactly."
                )
            if (
                user.oidc_issuer
                and user.oidc_subject
                and (
                    user.oidc_issuer != metadata.issuer or user.oidc_subject != subject
                )
            ):
                raise OIDCValidationError(
                    "User is already linked to a different OIDC identity"
                )
            user.oidc_issuer = metadata.issuer
            user.oidc_subject = subject

        if not user.is_active:
            raise OIDCValidationError("User account is disabled")

    except (OIDCConfigError, OIDCExchangeError, OIDCValidationError) as exc:
        return _auth_error_redirect(str(exc))
    except OAuthError as exc:
        return _auth_error_redirect(str(exc))
    except OIDCError:
        return _auth_error_redirect("OIDC authentication failed")

    if user is None:
        return _auth_error_redirect("OIDC authentication failed")

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    await _issue_login_session(request=request, response=response, user=user, db=db)
    return response


@router.post("/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def login(
    body: LoginRequest,
    # 'request' cannot be prefixed with a _ or it will break the rate limiter which uses the request
    # object internally
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Login with username and password."""
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()

    if (
        not user
        or not user.password_hash
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled"
        )

    await _issue_login_session(request=request, response=response, user=user, db=db)
    return AuthResponse(user=UserInfo.from_user(user))


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Logout and clear authentication cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = decode_token(token)
            session_id = get_session_id_from_payload(payload)
            if session_id is not None:
                result = await db.execute(
                    select(UserSession).where(UserSession.session_id == session_id)
                )
                current_session = result.scalar_one_or_none()
                if current_session is not None and current_session.revoked_at is None:
                    current_session.revoked_at = datetime.now(UTC)
                    current_session.revoked_reason = "logout"
                    await db.commit()
        except HTTPException:
            # invalid/expired tokens are effectively logged out once cookie is removed
            pass

    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"message": "Logged out successfully"}
