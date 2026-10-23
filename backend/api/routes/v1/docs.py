from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from backend.core.__version__ import __version__

router = APIRouter(tags=["v1:docs"])
_external_api_router: APIRouter | None = None

EXTERNAL_API_TITLE = "Reclaimerr External API"
EXTERNAL_API_DESCRIPTION = (
    "The supported, versioned API for Reclaimerr integrations and automation. "
    "Create a scoped bearer token under Settings > Integrations, then use the "
    "Authorize button to authenticate requests."
)

# Swagger UI and ReDoc are vendored and served from our own origin instead of
# FastAPI's default jsdelivr CDN, so a compromised CDN file can't run script on
# the app's origin. See docs_assets/README.md for versions and how to update.
DOCS_ASSETS_DIR = Path(__file__).resolve().parents[2] / "docs_assets"
DOCS_ASSETS_URL = "/docs-assets"


def swagger_ui_html(request: Request, openapi_url: str, title: str) -> HTMLResponse:
    """Render Swagger UI using the locally served assets."""
    root_path = request.scope.get("root_path", "").rstrip("/")
    assets = f"{root_path}{DOCS_ASSETS_URL}"
    return get_swagger_ui_html(
        openapi_url=f"{root_path}{openapi_url}",
        title=title,
        swagger_js_url=f"{assets}/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url=f"{assets}/swagger-ui/swagger-ui.css",
        swagger_favicon_url=f"{assets}/favicon.png",
    )


def redoc_html(request: Request, openapi_url: str, title: str) -> HTMLResponse:
    """Render ReDoc using the locally served assets."""
    root_path = request.scope.get("root_path", "").rstrip("/")
    assets = f"{root_path}{DOCS_ASSETS_URL}"
    return get_redoc_html(
        openapi_url=f"{root_path}{openapi_url}",
        title=title,
        redoc_js_url=f"{assets}/redoc/redoc.standalone.js",
        redoc_favicon_url=f"{assets}/favicon.png",
        with_google_fonts=False,
    )


def configure_external_api_docs(api_router: APIRouter) -> None:
    """Register the complete mounted v1 router used to generate documentation."""
    global _external_api_router
    _external_api_router = api_router


def build_external_openapi_schema(api_router: APIRouter) -> dict[str, Any]:
    """Build an OpenAPI document containing only supported v1 routes."""
    schema_app = FastAPI(
        title=EXTERNAL_API_TITLE,
        version=str(__version__),
        description=EXTERNAL_API_DESCRIPTION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    schema_app.include_router(api_router)
    schema = schema_app.openapi()
    schema["info"]["x-api-version"] = "v1"
    return schema


def _external_openapi_schema(request: Request) -> dict[str, Any]:
    cached = getattr(request.app.state, "external_api_openapi_schema", None)
    if cached is None:
        if _external_api_router is None:
            raise RuntimeError("External API documentation router is not configured")
        cached = build_external_openapi_schema(_external_api_router)
        request.app.state.external_api_openapi_schema = cached
    return cached


@router.get("/openapi.json", include_in_schema=False, name="external_api_openapi")
async def external_api_openapi(request: Request) -> JSONResponse:
    return JSONResponse(_external_openapi_schema(request))


@router.get("/docs", include_in_schema=False, name="external_api_swagger")
async def external_api_swagger(request: Request) -> HTMLResponse:
    return swagger_ui_html(
        request, "/api/v1/openapi.json", f"{EXTERNAL_API_TITLE} - Swagger UI"
    )


@router.get("/redoc", include_in_schema=False, name="external_api_redoc")
async def external_api_redoc(request: Request) -> HTMLResponse:
    return redoc_html(request, "/api/v1/openapi.json", f"{EXTERNAL_API_TITLE} - ReDoc")
