from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.core.auth import require_admin
from backend.database.models import User

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/shutdown")
async def shutdown_app(
    request: Request,
    _admin: Annotated[User, Depends(require_admin)],
) -> dict[str, str]:
    """Gracefully shut down the desktop application process.

    Only available when running in desktop mode (i.e. launched via ``desktop/__main__.py``).
    In pure server mode this returns 503.
    """
    callback = getattr(request.app.state, "shutdown_callback", None)
    if callback is None:
        raise HTTPException(
            status_code=503,
            detail="Shutdown is not available in server mode",
        )

    # schedule the shutdown slightly after this response is sent so the
    # HTTP response has time to flush back to the client first.
    loop = asyncio.get_event_loop()
    loop.call_later(0.5, callback)

    return {"detail": "Shutting down"}
