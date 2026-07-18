from __future__ import annotations

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
    root_path = request.scope.get("root_path", "").rstrip("/")
    return get_swagger_ui_html(
        openapi_url=f"{root_path}/api/v1/openapi.json",
        title=f"{EXTERNAL_API_TITLE} - Swagger UI",
    )


@router.get("/redoc", include_in_schema=False, name="external_api_redoc")
async def external_api_redoc(request: Request) -> HTMLResponse:
    root_path = request.scope.get("root_path", "").rstrip("/")
    return get_redoc_html(
        openapi_url=f"{root_path}/api/v1/openapi.json",
        title=f"{EXTERNAL_API_TITLE} - ReDoc",
    )
