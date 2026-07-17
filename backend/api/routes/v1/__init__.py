from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.routes.v1 import (
    candidates,
    docs,
    events,
    media,
    protections,
    system,
    tasks,
)
from backend.core.api_tokens import ApiPrincipal, get_api_principal
from backend.models.api_v1 import ApiDiscoveryResponse

router = APIRouter(prefix="/api/v1")


@router.get("", response_model=ApiDiscoveryResponse, tags=["v1:system"])
async def discover_api(
    principal: Annotated[ApiPrincipal, Depends(get_api_principal)],
) -> ApiDiscoveryResponse:
    return ApiDiscoveryResponse(
        api_version="v1",
        resources={
            "candidates": "/api/v1/candidates",
            "docs": "/api/v1/docs",
            "events": "/api/v1/events",
            "movies": "/api/v1/movies",
            "openapi": "/api/v1/openapi.json",
            "series": "/api/v1/series",
            "protections": "/api/v1/protections",
            "system": "/api/v1/system",
            "tasks": "/api/v1/tasks",
        },
        granted_scopes=sorted(principal.scopes),
    )


router.include_router(candidates.router)
router.include_router(events.router)
router.include_router(media.router)
router.include_router(protections.router)
router.include_router(system.router)
router.include_router(tasks.router)
router.include_router(docs.router)
docs.configure_external_api_docs(router)
