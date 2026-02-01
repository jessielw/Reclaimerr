from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio.session import AsyncSession

from backend.core.auth import get_current_user
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import get_db
from backend.database.models import ServiceConfig, ServiceMediaLibrary, User
from backend.enums import Service
from backend.models.settings import ServiceConfigRequest, ServiceConfigUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/services", tags=["settings"])
async def get_service_settings(
    _current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get current service settings."""
    # service configs
    get_service_configs = await db.execute(select(ServiceConfig))
    service_configs = get_service_configs.scalars()

    # gather libraries from Jellyfin and/or Plex
    MEDIA_SERVICES = (Service.JELLYFIN, Service.PLEX)
    get_service_libraries = await db.execute(
        select(
            ServiceMediaLibrary.service_type,
            ServiceMediaLibrary.media_type,
            ServiceMediaLibrary.library_name,
        ).order_by(ServiceMediaLibrary.media_type)
    )
    service_libraries = get_service_libraries.all()

    return {
        config.service_type: {
            "enabled": config.enabled,
            "base_url": config.base_url,
            "api_key": config.api_key,
            # sort libraries for Plex and Jellyfin only
            "libraries": [
                [service_type, media_type, library_name]
                for (service_type, media_type, library_name) in service_libraries
                if service_type == config.service_type
            ]
            if config.service_type in MEDIA_SERVICES  # only include for these services
            else None,
        }
        for config in service_configs
    }


@router.post("/save/service", tags=["settings"])
async def set_service_settings(
    data: ServiceConfigRequest,
    _current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set service settings for a given service."""
    await _upsert_service_config(
        db,
        ServiceConfigUpdate(
            service_type=data.service_type,
            base_url=data.base_url,
            api_key=data.api_key,
            enabled=data.enabled,
        ),
    )

    # clear and enable/disable clients (trigger this to start in background)
    asyncio.create_task(
        _toggle_service(
            ServiceConfigUpdate(
                service_type=data.service_type,
                base_url=data.base_url,
                api_key=data.api_key,
                enabled=data.enabled,
            ),
        )
    )

    return {
        "message": (
            f"{data.service_type.title()} settings updated "
            f"{'and client initialized' if data.enabled else 'and client disabled'}"
        ),
        "data": data,
    }


async def _upsert_service_config(
    db: AsyncSession, data: ServiceConfigUpdate
) -> ServiceConfigUpdate:
    """Upsert service configuration into the database."""
    LOG.info(f"Updating config for {data.service_type}")

    # upsert into database
    insert_statement = sqlite_insert(ServiceConfig).values(
        service_type=data.service_type,
        base_url=data.base_url,
        api_key=data.api_key,
        enabled=data.enabled,
    )
    upsert_statement = insert_statement.on_conflict_do_update(
        index_elements=["service_type"],
        set_={
            "base_url": data.base_url,
            "api_key": data.api_key,
            "enabled": data.enabled,
        },
    )
    await db.execute(upsert_statement)
    await db.commit()
    return data


async def _toggle_service(data: ServiceConfigUpdate) -> None:
    if data.service_type is Service.JELLYFIN:
        await service_manager.clear_jellyfin()
        if data.enabled:
            await service_manager.initialize_jellyfin(data.base_url, data.api_key)
    elif data.service_type is Service.PLEX:
        await service_manager.clear_plex()
        if data.enabled:
            await service_manager.initialize_plex(data.base_url, data.api_key)
    elif data.service_type is Service.RADARR:
        await service_manager.clear_radarr()
        if data.enabled:
            await service_manager.initialize_radarr(data.base_url, data.api_key)
    elif data.service_type is Service.SONARR:
        await service_manager.clear_sonarr()
        if data.enabled:
            await service_manager.initialize_sonarr(data.base_url, data.api_key)
    elif data.service_type is Service.SEERR:
        await service_manager.clear_seerr()
        if data.enabled:
            await service_manager.initialize_seerr(data.base_url, data.api_key)

    # log the status after toggling
    statuses = await service_manager.get_status()
    LOG.debug(f"Service statuses: {statuses}")


@router.post("/test/service", tags=["settings"])
async def test_service_settings(
    data: ServiceConfigRequest,
    _current_user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Test service settings for a given service."""
    success, error_msg = await service_manager.test_service(
        data.service_type, data.base_url, data.api_key
    )
    if not success:
        raise HTTPException(status_code=400, detail=error_msg)
    return {
        "message": f"{data.service_type} settings tested successfully",
        "data": data,
    }
