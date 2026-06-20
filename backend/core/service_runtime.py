from __future__ import annotations

import asyncio

from sqlalchemy import select

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_runtime import enqueue_task_run
from backend.database import async_db
from backend.database.models import ServiceConfig
from backend.enums import Service, Task
from backend.models.settings import ServiceConfigUpdate
from backend.scheduler import refresh_main_server_tasks
from backend.user_types import MEDIA_SERVERS

_service_toggle_lock = asyncio.Lock()


async def handle_service_toggle(
    data: ServiceConfigUpdate, trigger_resync: bool = False
) -> None:
    async with _service_toggle_lock:
        if data.api_key is None:
            raise ValueError(
                "api_key must be resolved before calling handle_service_toggle"
            )

        if data.id is not None:
            async with async_db() as db:
                current = (
                    await db.execute(
                        select(ServiceConfig).where(ServiceConfig.id == data.id)
                    )
                ).scalar_one_or_none()
            if current is None:
                LOG.info(
                    "Skipping stale service toggle for deleted config "
                    f"{data.id} ({data.service_type.value})"
                )
                return
            if (
                current.service_type != data.service_type
                or current.enabled != data.enabled
                or current.base_url != data.base_url
                or current.is_main != data.is_main
                or fer_decrypt(current.api_key) != data.api_key
            ):
                LOG.info(
                    "Skipping stale service toggle for changed config "
                    f"{data.id} ({data.service_type.value})"
                )
                return

        await _apply_service_runtime_state(data)

        if data.service_type in MEDIA_SERVERS:
            await refresh_main_server_tasks()

        statuses = await service_manager.get_status()
        LOG.debug(f"Service statuses: {statuses}")

        if trigger_resync:
            queued = await enqueue_task_run(Task.RESYNC_MEDIA)
            if queued:
                LOG.info("Queued RESYNC_MEDIA task after main media server switch")
            else:
                LOG.info(
                    "Skipped queuing RESYNC_MEDIA task after main media server switch because it is already queued or running"
                )


async def clear_deleted_service_runtime(service_type: Service, config_id: int) -> None:
    """Clear a deleted service from the in-memory runtime without contacting it."""
    async with _service_toggle_lock:
        await _clear_service_runtime(service_type, config_id)
        if service_type in MEDIA_SERVERS:
            await refresh_main_server_tasks()


async def _apply_service_runtime_state(data: ServiceConfigUpdate) -> None:
    await _clear_service_runtime(data.service_type, data.id)
    if not data.enabled:
        return

    if data.service_type is Service.JELLYFIN:
        await service_manager.initialize_jellyfin(
            data.base_url, data.api_key or "", data.is_main
        )
    elif data.service_type is Service.EMBY:
        await service_manager.initialize_emby(
            data.base_url, data.api_key or "", data.is_main
        )
    elif data.service_type is Service.PLEX:
        await service_manager.initialize_plex(
            data.base_url, data.api_key or "", data.is_main
        )
    elif data.service_type is Service.RADARR:
        timeout = int((data.extra_settings or {}).get("timeout", 300))
        await service_manager.initialize_radarr(
            data.base_url, data.api_key or "", timeout, data.id
        )
    elif data.service_type is Service.SONARR:
        timeout = int((data.extra_settings or {}).get("timeout", 300))
        await service_manager.initialize_sonarr(
            data.base_url, data.api_key or "", timeout, data.id
        )
    elif data.service_type is Service.SEERR:
        await service_manager.initialize_seerr(data.base_url, data.api_key or "")
    elif data.service_type is Service.TAUTULLI:
        timeout = int((data.extra_settings or {}).get("timeout", 30))
        await service_manager.initialize_tautulli(
            data.base_url, data.api_key or "", timeout
        )


async def _clear_service_runtime(service_type: Service, config_id: int | None) -> None:
    if service_type is Service.JELLYFIN:
        await service_manager.clear_jellyfin()
    elif service_type is Service.EMBY:
        await service_manager.clear_emby()
    elif service_type is Service.PLEX:
        await service_manager.clear_plex()
    elif service_type is Service.RADARR:
        await service_manager.clear_radarr(config_id)
    elif service_type is Service.SONARR:
        await service_manager.clear_sonarr(config_id)
    elif service_type is Service.SEERR:
        await service_manager.clear_seerr()
    elif service_type is Service.TAUTULLI:
        await service_manager.clear_tautulli()
