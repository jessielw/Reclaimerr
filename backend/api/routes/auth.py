from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

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
from sqlalchemy import func, or_, select
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
from backend.database.models import GeneralSettings, OIDCSettings, User, UserSession
from backend.enums import Service
from backend.models.auth import (
    AuthResponse,
    LoginRequest,
    MediaAuthProvidersResponse,
    MediaLoginRequest,
    OIDCAuthStatusResponse,
    UserInfo,
)
from backend.models.auth import (
    MediaAuthProvider as MediaAuthProviderResponse,
)
from backend.services.media_auth import (
    MediaAuthAccessDeniedError,
    MediaAuthConflictError,
    MediaAuthCredentialsError,
    MediaAuthProviderError,
    authenticate_emby_family_credentials,
    authenticate_plex_token,
    exchange_plex_pin_for_token,
    get_media_auth_provider,
    list_media_auth_providers,
    persist_plex_identity_token,
    pop_pending_plex_auth,
    resolve_or_create_user_for_identity,
    start_plex_pin_flow,
)
from backend.services.media_auth import (
    MediaAuthProvider as MediaAuthProviderConfig,
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
    *,
    application_url: str | None = None,
) -> str:
    if settings_row.redirect_uri_override:
        return settings_row.redirect_uri_override
    if application_url:
        return f"{application_url.rstrip('/')}/api/auth/oidc/callback"
    return str(request.url_for("oidc_callback"))


async def _get_application_url(db: AsyncSession) -> str | None:
    result = await db.execute(select(GeneralSettings.application_url))
    application_url = result.scalars().first()
    if not application_url:
        return None
    normalized = application_url.strip().rstrip("/")
    return normalized or None


def _origin_tuple(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return (parsed.scheme.lower(), parsed.netloc.lower())


def _is_loopback_host(host: str) -> bool:
    return host.lower() in {"localhost", "127.0.0.1", "::1"}


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return _is_loopback_host(str(parsed.hostname or ""))


def _default_frontend_redirect(*, application_url: str | None = None) -> str:
    if application_url:
        return f"{application_url.rstrip('/')}/"

    for origin in settings.cors_origins_list:
        if origin == "*":
            continue
        try:
            parsed = urlsplit(origin.strip())
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
    return "/"


def _is_allowed_return_to_url(
    value: str | None,
    *,
    application_url: str | None = None,
) -> bool:
    if not value:
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False

    if not parsed.scheme and not parsed.netloc:
        return str(value).strip().startswith("/")

    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False

    candidate = (parsed.scheme.lower(), parsed.netloc.lower())
    if application_url:
        application_origin = _origin_tuple(application_url)
        if application_origin and candidate == application_origin:
            return True
        if _is_loopback_url(value) and not _is_loopback_url(application_url):
            return False

    host = str(parsed.hostname or "").lower()
    if _is_loopback_host(host):
        return True

    for origin in settings.cors_origins_list:
        if origin == "*":
            continue
        allowed_origin = _origin_tuple(origin)
        if allowed_origin is None:
            continue
        if candidate == allowed_origin:
            return True
    return False


def _resolve_post_auth_redirect(
    value: str | None,
    *,
    application_url: str | None = None,
) -> str:
    if _is_allowed_return_to_url(value, application_url=application_url):
        return str(value).strip()
    return _default_frontend_redirect(application_url=application_url)


def _with_auth_error(url: str, message: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["auth_error"] = message
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            urlencode(query),
            parsed.fragment,
        )
    )


def _auth_error_redirect(
    message: str, *, redirect_to: str | None = None
) -> RedirectResponse:
    target = redirect_to or "/"
    if target == "/":
        return RedirectResponse(
            url=f"/?auth_error={quote_plus(message)}",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=_with_auth_error(target, message),
        status_code=status.HTTP_302_FOUND,
    )


def _serialize_media_provider(
    provider: MediaAuthProviderConfig,
) -> MediaAuthProviderResponse:
    return MediaAuthProviderResponse(
        service_config_id=provider.service_config_id,
        service_type=provider.service_type.value,
        name=provider.name,
        auth_mode=provider.auth_mode,
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

    application_url = await _get_application_url(db)
    callback_uri = _oidc_callback_redirect_uri(
        request,
        settings_row,
        application_url=application_url,
    )
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
    application_url = await _get_application_url(db)
    redirect_target = _resolve_post_auth_redirect(None, application_url=application_url)
    if error:
        return _auth_error_redirect(
            "OIDC authentication was denied or failed",
            redirect_to=redirect_target,
        )

    result = await db.execute(select(OIDCSettings))
    settings_row = result.scalars().first()
    if settings_row is None:
        return _auth_error_redirect(
            "OIDC login is not configured", redirect_to=redirect_target
        )
    if not _oidc_enabled(settings_row):
        return _auth_error_redirect(
            "OIDC login is not enabled", redirect_to=redirect_target
        )

    if not code or not state:
        return _auth_error_redirect(
            "OIDC callback is missing required parameters",
            redirect_to=redirect_target,
        )

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
        return _auth_error_redirect(str(exc), redirect_to=redirect_target)
    except OAuthError as exc:
        return _auth_error_redirect(str(exc), redirect_to=redirect_target)
    except OIDCError:
        return _auth_error_redirect(
            "OIDC authentication failed", redirect_to=redirect_target
        )

    response = RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)
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
    # email or username can be used as the identifier for login, so we need to check both fields for a match
    result = await db.execute(
        select(User).where(
            or_(User.username == body.username, User.email == body.username)
        )
    )
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


@router.get("/media/providers", response_model=MediaAuthProvidersResponse)
async def list_media_auth_login_providers(
    db: AsyncSession = Depends(get_db),
) -> MediaAuthProvidersResponse:
    providers = await list_media_auth_providers(db)
    payload = [_serialize_media_provider(provider) for provider in providers]
    default_provider = next(
        (provider for provider in payload if provider.auth_mode == "redirect"),
        payload[0] if payload else None,
    )
    default_service_config_id = (
        default_provider.service_config_id if default_provider else None
    )
    return MediaAuthProvidersResponse(
        providers=payload,
        default_service_config_id=default_service_config_id,
    )


@router.post("/media/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def media_login(
    body: MediaLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Login with media server credentials for providers that support it (e.g. Emby/Jellyfin family users)."""
    provider = await get_media_auth_provider(
        db,
        service_config_id=body.service_config_id,
    )
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media login provider was not found or is disabled",
        )
    if provider.auth_mode != "credentials":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected provider requires browser redirect sign-in",
        )
    if provider.service_type not in (Service.JELLYFIN, Service.EMBY):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected provider does not support credentials sign-in",
        )

    try:
        identity = await authenticate_emby_family_credentials(
            provider=provider,
            username=body.username,
            password=body.password,
        )
        user = await resolve_or_create_user_for_identity(db, identity=identity)
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is disabled",
            )
    except MediaAuthCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid media server credentials",
        ) from exc
    except MediaAuthAccessDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except MediaAuthConflictError as exc:
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your media account matches multiple local users and needs an "
                "admin to link it before sign-in."
            ),
        ) from exc
    except MediaAuthProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    await _issue_login_session(request=request, response=response, user=user, db=db)
    return AuthResponse(user=UserInfo.from_user(user))


@router.get("/media/plex/start")
async def media_plex_start(
    request: Request,
    service_config_id: int = Query(..., ge=1),
    return_to: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Start Plex login flow by requesting a PIN and redirecting user to Plex auth page."""
    provider = await get_media_auth_provider(
        db,
        service_config_id=service_config_id,
    )
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plex login provider was not found or is disabled",
        )
    if provider.service_type is not Service.PLEX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected provider is not a Plex server",
        )

    application_url = await _get_application_url(db)
    callback_url = (
        f"{application_url.rstrip('/')}/api/auth/media/plex/callback"
        if application_url
        else str(request.url_for("media_plex_callback"))
    )
    redirect_target = _resolve_post_auth_redirect(
        return_to,
        application_url=application_url,
    )
    try:
        redirect_url = await start_plex_pin_flow(
            provider=provider,
            callback_url=callback_url,
            return_to=redirect_target,
        )
    except MediaAuthProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/media/plex/callback", name="media_plex_callback")
async def media_plex_callback(
    request: Request,
    state: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Handle callback from Plex after user authorizes PIN login. Exchange PIN for token, authenticate,
    and issue session cookie."""
    application_url = await _get_application_url(db)
    default_redirect = _resolve_post_auth_redirect(
        None,
        application_url=application_url,
    )
    if not state:
        return _auth_error_redirect(
            "Plex callback is missing state", redirect_to=default_redirect
        )

    pending = pop_pending_plex_auth(state)
    if pending is None:
        return _auth_error_redirect(
            "Plex login session expired. Please try again.",
            redirect_to=default_redirect,
        )
    redirect_target = _resolve_post_auth_redirect(
        pending.return_to,
        application_url=application_url,
    )

    provider = await get_media_auth_provider(
        db,
        service_config_id=pending.service_config_id,
    )
    if provider is None:
        return _auth_error_redirect(
            "Plex login provider is unavailable",
            redirect_to=redirect_target,
        )

    try:
        plex_user_token = await exchange_plex_pin_for_token(pending)
        identity = await authenticate_plex_token(
            db,
            provider=provider,
            plex_user_token=plex_user_token,
        )
        user = await resolve_or_create_user_for_identity(db, identity=identity)
        await persist_plex_identity_token(
            db,
            identity=identity,
            plex_user_token=plex_user_token,
        )
        if not user.is_active:
            return _auth_error_redirect(
                "Account is disabled", redirect_to=redirect_target
            )
    except MediaAuthCredentialsError:
        return _auth_error_redirect(
            "Plex sign-in was not approved",
            redirect_to=redirect_target,
        )
    except MediaAuthAccessDeniedError:
        return _auth_error_redirect(
            "Your Plex account does not have access to this server",
            redirect_to=redirect_target,
        )
    except MediaAuthConflictError:
        await db.commit()
        return _auth_error_redirect(
            "Your media account needs an admin link before sign-in",
            redirect_to=redirect_target,
        )
    except MediaAuthProviderError:
        return _auth_error_redirect(
            "Plex sign-in failed. Please try again.",
            redirect_to=redirect_target,
        )

    response = RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)
    await _issue_login_session(request=request, response=response, user=user, db=db)
    return response


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
