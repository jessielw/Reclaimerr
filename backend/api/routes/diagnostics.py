"""Admin-only, local support exports; POST does not persist the generated report."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.core.auth import require_admin
from backend.core.settings import settings
from backend.database.models import User
from backend.models.diagnostics import (
    BundleOptions,
    BundlePreview,
    DiagnosticCheckRequest,
    DiagnosticCheckResult,
    DiagnosticsOptions,
    DiagnosticsReport,
    MediaDiagnosticsOptions,
    SupportLookupOptions,
    SupportLookupResult,
)
from backend.services.diagnostic_checks import ChecksBusy, run_check
from backend.services.diagnostics import collect_report, generation_slots
from backend.services.support_bundle import bundle_cache, prepare_bundle
from backend.services.support_lookup import lookup_support

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


@router.post("/lookup", response_model=SupportLookupResult)
def support_lookup(
    options: SupportLookupOptions,
    response: Response,
    _admin: Annotated[User, Depends(require_admin)],
) -> SupportLookupResult:
    response.headers["Cache-Control"] = "no-store"
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            "Support diagnostics are busy. Try again shortly.",
            headers={"Retry-After": "5"},
        )
    try:
        return lookup_support(Path(settings.db_path), options.code)
    finally:
        generation_slots.release()


def generate(options: DiagnosticsOptions, response: Response) -> DiagnosticsReport:
    response.headers["Cache-Control"] = "no-store"
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            "Support diagnostics are busy. Try again in a few seconds.",
            headers={"Retry-After": "5"},
        )
    try:
        return collect_report(
            Path(settings.db_path),
            options,
            options.target if isinstance(options, MediaDiagnosticsOptions) else None,
        )
    finally:
        generation_slots.release()


@router.post("/system", response_model=DiagnosticsReport)
def system_report(
    options: DiagnosticsOptions,
    response: Response,
    _admin: Annotated[User, Depends(require_admin)],
) -> DiagnosticsReport:
    return generate(options, response)


@router.post("/media", response_model=DiagnosticsReport)
def media_report(
    options: MediaDiagnosticsOptions,
    response: Response,
    _admin: Annotated[User, Depends(require_admin)],
) -> DiagnosticsReport:
    return generate(options, response)


@router.post("/bundles/preview", response_model=BundlePreview)
def preview_bundle(
    options: BundleOptions,
    response: Response,
    admin: Annotated[User, Depends(require_admin)],
) -> BundlePreview:
    response.headers["Cache-Control"] = "no-store"
    if not generation_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            "Support diagnostics are busy. Try again shortly.",
            headers={"Retry-After": "5"},
        )
    try:
        return prepare_bundle(Path(settings.db_path), options, admin.id)
    except ValueError:
        raise HTTPException(
            422, "Choose a media target for an item preset, or reduce the bundle scope."
        ) from None
    finally:
        generation_slots.release()


@router.get("/bundles/{token}")
def download_bundle(
    token: str, admin: Annotated[User, Depends(require_admin)]
) -> Response:
    cached = bundle_cache.get(admin.id, token)
    if cached is None:
        raise HTTPException(
            404, "Bundle expired or unavailable. Prepare a new preview."
        )
    data, filename = cached
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/checks", response_model=DiagnosticCheckResult)
async def diagnostic_check(
    options: DiagnosticCheckRequest,
    response: Response,
    _admin: Annotated[User, Depends(require_admin)],
) -> DiagnosticCheckResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await run_check(Path(settings.db_path), options)
    except ChecksBusy:
        raise HTTPException(
            429, "Diagnostic checks are busy. Try again shortly."
        ) from None
    except ValueError:
        raise HTTPException(
            422, "Choose a configured service or a media target for this check."
        ) from None
