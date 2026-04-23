from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import require_admin
from backend.database import get_db
from backend.database.models import GeneralSettings, User
from backend.models.settings import GeneralSettingsResponse

router = APIRouter(tags=["settings", "general"])


@router.get("/general")
async def get_general_settings(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GeneralSettingsResponse:
    """
    Get general settings.

    `updated_by` will be null if settings have never been updated since creation.
    """
    result = await db.execute(select(GeneralSettings))
    settings = result.scalars().first()
    # create default settings if not exist
    if not settings:
        settings = GeneralSettings()
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return GeneralSettingsResponse.model_validate(settings)


@router.put("/general")
async def update_general_settings(
    request: GeneralSettingsResponse,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GeneralSettingsResponse:
    """Update general settings."""
    result = await db.execute(select(GeneralSettings))
    settings = result.scalars().first()

    # should always exist since we create default on get, but just in case
    if not settings:
        raise HTTPException(status_code=404, detail="General settings not found")

    # update fields
    settings.auto_tag_enabled = request.auto_tag_enabled
    settings.cleanup_tag_suffix = request.cleanup_tag_suffix
    settings.worker_poll_min_seconds = request.worker_poll_min_seconds
    settings.worker_poll_max_seconds = request.worker_poll_max_seconds

    # update metadata
    settings.updated_at = datetime.now(UTC)
    settings.updated_by_user_id = admin.id

    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    return GeneralSettingsResponse.model_validate(settings)
