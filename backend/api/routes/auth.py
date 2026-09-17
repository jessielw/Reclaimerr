from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import uuid4

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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auth import (
    COOKIE_NAME,
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    SESSION_TOUCH_BUSY_TIMEOUT_MS,
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
from backend.core.logger import LOG
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
    PLEX_PENDING_AUTH_TTL,
    MediaAuthAccessDeniedError,
    MediaAuthConflictError,
    MediaAuthCredentialsError,
    MediaAuthProviderError,
    PlexAuthFlowMode,
    authenticate_emby_family_credentials,
    authenticate_plex_token,
    exchange_plex_pin_for_token,
    get_media_auth_provider,
    list_media_auth_providers,
    peek_pending_plex_auth,
    persist_plex_identity_token,
    poll_plex_pin_for_token,
    pop_pending_plex_auth,
    resolve_or_create_user_for_identity,
    start_plex_pin_flow,
)
from backend.services.media_auth import (
    MediaAuthProvider as MediaAuthProviderConfig,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)

# Binds a Plex PIN flow to the browser that started it, so the opener window can
# poll for the result without the pollable identifier ever travelling through
# Plex (unlike ``state``, which rides along in forwardUrl).
PLEX_FLOW_COOKIE = "plex_auth_flow"
PLEX_FLOW_COOKIE_PATH = "/api/auth/media/plex"
PLEX_FLOW_COOKIE_MAX_AGE = int(PLEX_PENDING_AUTH_TTL.total_seconds())


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


def _request_origin(request: Request | None) -> str | None:
    """The origin the browser itself says it is currently on.

    Reclaimerr sends ``Referrer-Policy: strict-origin-when-cross-origin``, so a
    same-origin navigation from the login page to one of the ``/start`` routes
    carries a full ``Referer``. That gives us the public origin a user actually
    reached us on even when Application URL is unset and the proxy headers are
    not trusted, which is the common self-hosted default.
    """
    if request is None:
        return None
    for header_name in ("origin", "referer"):
        raw = request.headers.get(header_name)
        if not raw or raw.strip().lower() == "null":
            continue
        origin = _origin_tuple(raw)
        if origin is not None:
            return f"{origin[0]}://{origin[1]}"
    return None


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
    request_origin: str | None = None,
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

    # The origin the browser is already on is not an open redirect - the user can
    # reach it without our help. Accepting it keeps reverse-proxy installs working
    # before anyone has configured Application URL or PROXY_TRUSTED_HOSTS.
    if request_origin:
        origin = _origin_tuple(request_origin)
        if origin is not None and candidate == origin:
            return True

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
    request_origin: str | None = None,
) -> str:
    if _is_allowed_return_to_url(
        value,
        application_url=application_url,
        request_origin=request_origin,
    ):
        return str(value).strip()
    return _default_frontend_redirect(application_url=application_url)


def _absolute_base_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


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


_AUTH_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reclaimerr</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; min-height: 100vh; display: flex;
    align-items: center; justify-content: center;
    background: #0a0a0a; color: #fafafa;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  .box {{ max-width: 32rem; padding: 24px; text-align: center; }}
  .spinner {{
    width: 32px; height: 32px; margin: 0 auto 16px; border-radius: 50%;
    border: 3px solid #27272a; border-top-color: #e5a00d;
    animation: spin 0.8s linear infinite;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  p {{ margin: 0; font-size: 0.875rem; color: #a1a1aa; }}
  p.error {{ color: #f87171; }}
</style>
</head>
<body>
  <div class="box">
    <div class="spinner" id="spinner"{spinner_style}></div>
    <p id="msg"{msg_class} data-error="{error_attr}">{message}</p>
  </div>
{script}
</body>
</html>
"""


# Nothing is ever interpolated into these scripts. Whatever the page has to say is
# escaped into the markup and read back out of the DOM, so text we do not control
# (an identity provider's error message, say) never reaches a script block.
_TERMINAL_SCRIPT = """  <script>
  (function () {
    var node = document.getElementById("msg");
    var error = node.getAttribute("data-error") || null;
    var payload = { type: "reclaimerr-auth-complete", error: error };
    // Nudge the opener so it polls immediately instead of waiting out its
    // interval. Both channels are best-effort: COOP on the identity provider can
    // sever window.opener, and BroadcastChannel may be unavailable. The opener's
    // poll is what actually completes the sign in.
    try {
      var channel = new BroadcastChannel("reclaimerr-auth");
      channel.postMessage(payload);
      channel.close();
    } catch (err) {}
    try {
      if (window.opener && !window.opener.closed) {
        window.opener.postMessage(payload, window.location.origin);
      }
    } catch (err) {}

    if (error) return;

    setTimeout(function () {
      try { window.close(); } catch (err) {}
    }, 150);
    setTimeout(function () {
      if (window.closed) return;
      var spinner = document.getElementById("spinner");
      if (spinner) spinner.style.display = "none";
      node.textContent = "Sign in complete. You can close this window.";
    }, 1000);
  })();
  </script>
"""


_LOADING_SCRIPT = """  <script>
  (function () {
    var handedOff = false;
    function tellOpener() {
      if (handedOff) return;
      handedOff = true;
      try {
        if (window.opener && !window.opener.closed) {
          window.opener.postMessage(
            { type: "reclaimerr-auth-window-ready" },
            window.location.origin
          );
          return;
        }
      } catch (err) {}
      giveUp();
    }
    function giveUp() {
      var spinner = document.getElementById("spinner");
      if (spinner) spinner.style.display = "none";
      document.getElementById("msg").textContent =
        "Could not reach the page that opened this window. Close it and try again.";
    }
    // Two frames guarantees the spinner has painted before we hand off. The opener
    // is still same-origin here and no COOP boundary has been crossed, so
    // window.opener is dependable - which it stops being once we reach the
    // provider.
    requestAnimationFrame(function () {
      requestAnimationFrame(tellOpener);
    });
    // Dies with the document as soon as the opener navigates us onward.
    setTimeout(giveUp, 8000);
  })();
  </script>
"""


def _auth_page(message: str, *, script: str, error: str | None = None) -> HTMLResponse:
    body = _AUTH_PAGE_TEMPLATE.format(
        message=html.escape(message),
        msg_class=' class="error"' if error else "",
        spinner_style=' style="display:none"' if error else "",
        error_attr=html.escape(error or "", quote=True),
        script=script,
    )
    return HTMLResponse(content=body, headers={"Cache-Control": "no-store"})


def _terminal_auth_page(error: str | None = None) -> HTMLResponse:
    """Render the dead-end page a sign-in popup lands on.

    This page never navigates anywhere. Sending the popup back into the SPA is what
    produced #353 - the popup ended up showing a second, separate copy of the app
    that the user had to close by hand.
    """
    return _auth_page(
        error or "Completing sign in...",
        script=_TERMINAL_SCRIPT,
        error=error,
    )


def _auth_flow_response(
    mode: PlexAuthFlowMode,
    message: str | None = None,
    *,
    redirect_to: str,
) -> Response:
    """Finish a browser auth hop the way the flow that started it expects.

    ``popup`` flows terminate in place and let the opener window pick the result
    up by polling; ``redirect`` flows own the whole tab and bounce back to the app.
    """
    if mode == "popup":
        return _terminal_auth_page(message)
    if message:
        return _auth_error_redirect(message, redirect_to=redirect_to)
    return RedirectResponse(url=redirect_to, status_code=status.HTTP_302_FOUND)


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


def _create_configured_oidc_client(settings_row: OIDCSettings) -> Any:
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


def _request_session(request: Request) -> dict[str, Any] | None:
    session = request.scope.get("session")
    return session if isinstance(session, dict) else None


@router.get("/signin-window")
async def auth_signin_window() -> HTMLResponse:
    """Serve the first page a sign-in popup shows.

    ``window.open`` has to run synchronously inside the click handler or the browser
    drops the user gesture and blocks the popup, which leaves no room to do any work
    first. So the popup opens here and paints a spinner, then tells the opener it is
    ready; the opener sends it on to the provider. The browser keeps showing this
    page until that next document commits, so nobody watches an empty window while
    the server talks to Plex.

    The destination deliberately is not a parameter on this route - handing a URL to
    a page that renders HTML is how reflected XSS starts, and the opener already
    knows where the window should go.
    """
    return _auth_page("Connecting...", script=_LOADING_SCRIPT)


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
    return_to: str | None = Query(default=None),
    mode: PlexAuthFlowMode = Query(default="redirect"),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    result = await db.execute(select(OIDCSettings))
    settings_row = result.scalars().first()
    if settings_row is None:
        raise HTTPException(status_code=404, detail="OIDC login is not configured")
    if not _oidc_enabled(settings_row):
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")

    application_url = await _get_application_url(db)
    request_origin = _request_origin(request)
    redirect_target = _resolve_post_auth_redirect(
        return_to,
        application_url=application_url,
        request_origin=request_origin,
    )
    session = _request_session(request)
    if session is not None:
        session["oidc_return_to"] = redirect_target
        session["oidc_mode"] = mode
    callback_uri = _oidc_callback_redirect_uri(
        request,
        settings_row,
        application_url=application_url
        or _absolute_base_url(redirect_target)
        or request_origin,
    )
    try:
        client = _create_configured_oidc_client(settings_row)
        auth_redirect: RedirectResponse = await client.authorize_redirect(
            request, callback_uri
        )
        return auth_redirect
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
) -> Response:
    application_url = await _get_application_url(db)
    session = _request_session(request)
    stored_return_to = (
        session.pop("oidc_return_to", None) if session is not None else None
    )
    stored_mode = session.pop("oidc_mode", None) if session is not None else None
    mode: PlexAuthFlowMode = "popup" if stored_mode == "popup" else "redirect"
    redirect_target = _resolve_post_auth_redirect(
        stored_return_to,
        application_url=application_url,
        # Already vetted on the /start hop; keep accepting that origin so a proxy
        # origin allowed there is not silently downgraded to "/" now.
        request_origin=_absolute_base_url(stored_return_to),
    )
    if error:
        return _auth_flow_response(
            mode,
            "OIDC authentication was denied or failed",
            redirect_to=redirect_target,
        )

    result = await db.execute(select(OIDCSettings))
    settings_row = result.scalars().first()
    if settings_row is None:
        return _auth_flow_response(
            mode, "OIDC login is not configured", redirect_to=redirect_target
        )
    if not _oidc_enabled(settings_row):
        return _auth_flow_response(
            mode, "OIDC login is not enabled", redirect_to=redirect_target
        )

    if not code or not state:
        return _auth_flow_response(
            mode,
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
        return _auth_flow_response(mode, str(exc), redirect_to=redirect_target)
    except OAuthError as exc:
        return _auth_flow_response(mode, str(exc), redirect_to=redirect_target)
    except OIDCError:
        return _auth_flow_response(
            mode, "OIDC authentication failed", redirect_to=redirect_target
        )

    response = _auth_flow_response(mode, redirect_to=redirect_target)
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
) -> AuthResponse:
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
    default_service_config_id = payload[0].service_config_id if payload else None
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
) -> AuthResponse:
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
    mode: PlexAuthFlowMode = Query(default="redirect"),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
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
    request_origin = _request_origin(request)
    redirect_target = _resolve_post_auth_redirect(
        return_to,
        application_url=application_url,
        request_origin=request_origin,
    )
    callback_base_url = (
        application_url or _absolute_base_url(redirect_target) or request_origin
    )
    callback_url = (
        f"{callback_base_url.rstrip('/')}/api/auth/media/plex/callback"
        if callback_base_url
        else str(request.url_for("media_plex_callback"))
    )
    if mode == "popup":
        # Carry the mode through Plex so the callback never has to infer it from
        # browser state - window.name is wiped by cross-origin navigation.
        callback_url = f"{callback_url}?mode=popup"

    state = uuid4().hex
    try:
        redirect_url = await start_plex_pin_flow(
            provider=provider,
            callback_url=callback_url,
            return_to=redirect_target,
            mode=mode,
            state=state,
        )
    except MediaAuthProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
    if mode == "popup":
        # First-party cookie, so the opener window's poll carries it automatically.
        # Plex only ever sees ``state`` (via forwardUrl), never this.
        response.set_cookie(
            key=PLEX_FLOW_COOKIE,
            value=state,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=PLEX_FLOW_COOKIE_MAX_AGE,
            path=PLEX_FLOW_COOKIE_PATH,
        )
    return response


@router.get("/media/plex/callback", name="media_plex_callback")
async def media_plex_callback(
    request: Request,
    state: str | None = Query(default=None),
    mode: PlexAuthFlowMode = Query(default="redirect"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle callback from Plex after user authorizes PIN login.

    In ``redirect`` mode this hop exchanges the PIN, issues the session cookie and
    sends the tab back to the app. In ``popup`` mode it does none of that: the
    opener window is polling ``/media/plex/poll`` and receives the session on its
    own request, so all this page has to do is close itself. Sending the popup back
    into the SPA is what left users with a second copy of the app to dismiss (#353).
    """
    if mode == "popup":
        return _terminal_auth_page()

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
        # Already vetted on the /start hop; keep accepting that origin so a proxy
        # origin allowed there is not silently downgraded to "/" now.
        request_origin=_absolute_base_url(pending.return_to),
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


def _plex_poll_response(state: str, message: str | None = None) -> JSONResponse:
    payload: dict[str, str] = {"status": state}
    if message:
        payload["message"] = message
    response = JSONResponse(content=payload)
    if state != "pending":
        response.delete_cookie(PLEX_FLOW_COOKIE, path=PLEX_FLOW_COOKIE_PATH)
    return response


@router.get("/media/plex/poll")
@limiter.limit("240/minute")
async def media_plex_poll(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Report whether a popup Plex sign-in has completed, completing it if so.

    The window that opened the popup drives this, never the popup itself. Once the
    popup has been through app.plex.tv, `popup.closed`, `window.opener` and
    `window.name` are all unreliable (Cross-Origin-Opener-Policy swaps the browsing
    context group), so the PIN is the only trustworthy signal. Issuing the session
    here also puts the cookie on the opener's own response instead of the popup's.
    """
    state = request.cookies.get(PLEX_FLOW_COOKIE)
    pending = peek_pending_plex_auth(state) if state else None
    if state is None or pending is None:
        return _plex_poll_response("expired")

    provider = await get_media_auth_provider(
        db,
        service_config_id=pending.service_config_id,
    )
    if provider is None:
        pop_pending_plex_auth(state)
        return _plex_poll_response("error", "Plex login provider is unavailable")

    try:
        plex_user_token = await poll_plex_pin_for_token(pending)
        if not plex_user_token:
            return _plex_poll_response("pending")

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
            pop_pending_plex_auth(state)
            return _plex_poll_response("error", "Account is disabled")
    except MediaAuthCredentialsError:
        pop_pending_plex_auth(state)
        return _plex_poll_response("error", "Plex sign-in was not approved")
    except MediaAuthAccessDeniedError:
        pop_pending_plex_auth(state)
        return _plex_poll_response(
            "error", "Your Plex account does not have access to this server"
        )
    except MediaAuthConflictError:
        await db.commit()
        pop_pending_plex_auth(state)
        return _plex_poll_response(
            "error", "Your media account needs an admin link before sign-in"
        )
    except MediaAuthProviderError:
        pop_pending_plex_auth(state)
        return _plex_poll_response("error", "Plex sign-in failed. Please try again.")

    pop_pending_plex_auth(state)
    response = _plex_poll_response("authenticated")
    await _issue_login_session(request=request, response=response, user=user, db=db)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Logout and clear authentication cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            payload = decode_token(token)
            session_id = get_session_id_from_payload(payload)
            if session_id is not None:
                await db.execute(
                    text(f"PRAGMA busy_timeout={SESSION_TOUCH_BUSY_TIMEOUT_MS}")
                )
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
        except OperationalError:
            await db.rollback()
            LOG.debug("Skipped session revocation during logout because SQLite is busy")
        except Exception:
            await db.rollback()
            LOG.debug("Failed to revoke session during logout", exc_info=True)
        finally:
            try:
                await db.execute(
                    text(f"PRAGMA busy_timeout={DEFAULT_SQLITE_BUSY_TIMEOUT_MS}")
                )
            except Exception:
                LOG.debug("Failed to restore SQLite busy timeout", exc_info=True)

    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"message": "Logged out successfully"}
