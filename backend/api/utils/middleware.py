from __future__ import annotations

import time

import jwt
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from backend.core.auth import COOKIE_NAME, SESSION_TTL_SECONDS, create_access_token
from backend.core.settings import settings
from backend.core.setup_state import setup_state

__all__ = [
    "cors_middleware",
    "security_headers_middleware",
    "setup_guard_middleware",
    "sliding_session_middleware",
]


def setup_guard_middleware(app: FastAPI) -> None:
    """Redirect all traffic to the setup wizard until setup is complete."""
    app.add_middleware(SetupGuardMiddleware)


class SetupGuardMiddleware(BaseHTTPMiddleware):
    """
    Block API requests until the first run setup wizard has been completed.

    Non API requests (SPA index, static assets) are always passed through, since
    the frontend handles the setup wizard via the /api/setup/status check in
    app.svelte. Redirecting browser page loads here would cause an infinite
    redirect loop because hash fragments (#/setup) are never sent to the server.
    """

    _ALLOWED_PREFIXES = ("/api/setup", "/api/info")

    async def dispatch(self, request, call_next):
        if not setup_state.needs_setup:
            return await call_next(request)

        path = request.url.path

        # always allow setup + info endpoints and all non API traffic
        if not path.startswith("/api/") or any(
            path.startswith(p) for p in self._ALLOWED_PREFIXES
        ):
            return await call_next(request)

        return JSONResponse(
            status_code=503,
            content={"detail": "setup_required"},
        )


def cors_middleware(app: FastAPI) -> None:
    """Add CORS middleware to the app."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials="*" not in settings.cors_origins_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def security_headers_middleware(app: FastAPI) -> None:
    """Add security headers middleware to the app."""
    app.add_middleware(SecurityHeadersMiddleware)


def sliding_session_middleware(app: FastAPI) -> None:
    """Add sliding session middleware to the app."""
    app.add_middleware(SlidingSessionMiddleware)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["X-XSS-Protection"] = "0"
        # prevent browsers/proxies from caching API responses that may contain
        # sensitive data
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


class SlidingSessionMiddleware(BaseHTTPMiddleware):
    """Silently refresh the JWT cookie when it is past the halfway mark.

    This means that as long as the user is actively making requests, their
    session will keep extending.  The cookie (and JWT) are only refreshed
    when less than half the original TTL remains, keeping overhead minimal.
    """

    REFRESH_THRESHOLD = SESSION_TTL_SECONDS / 2  # 12 hours for a 24-hour TTL

    async def dispatch(self, request, call_next):
        response = await call_next(request)

        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return response

        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            remaining = payload.get("exp", 0) - time.time()

            if 0 < remaining < self.REFRESH_THRESHOLD:
                new_token = create_access_token(
                    data={"sub": payload["sub"]},
                    token_version=payload.get("tv", 0),
                )
                response.set_cookie(
                    key=COOKIE_NAME,
                    value=new_token,
                    httponly=True,
                    secure=settings.cookie_secure,
                    samesite="lax",
                    max_age=SESSION_TTL_SECONDS,
                    path="/",
                )
        except Exception:
            pass  # don't interfere with the response if refresh fails

        return response
