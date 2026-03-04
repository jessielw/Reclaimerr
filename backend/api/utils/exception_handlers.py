from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core.logger import LOG

__all__ = ("register_exception_handlers",)


async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler with logging."""
    LOG.exception(f"Unhandled exception on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "path": str(request.url.path)},
    )


async def http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Handle Starlette HTTP exceptions (routing, static files, etc.) cleanly."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a flat string detail instead of FastAPI's default array."""
    messages = [error.get("msg", "Invalid value") for error in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"detail": ", ".join(messages)},
    )


async def rate_limit_handler(
    _request: Request, _exc: RateLimitExceeded
) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."},
    )


def register_exception_handlers(app: FastAPI):
    """Register global exception handlers for the FastAPI app."""
    app.add_exception_handler(Exception, global_exception_handler)  # type: ignore
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)  # type: ignore
