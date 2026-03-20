from __future__ import annotations

import asyncio

from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.core.task_runtime import enqueue_task_run
from backend.enums import Service, Task
from backend.models.settings import ServiceConfigUpdate
from backend.scheduler import refresh_main_server_tasks
from backend.types import MEDIA_SERVERS

_service_toggle_lock = asyncio.Lock()


async def handle_service_toggle(
    data: ServiceConfigUpdate, trigger_resync: bool = False
) -> None:
    async with _service_toggle_lock:
        if data.api_key is None:
            raise ValueError(
                "api_key must be resolved before calling handle_service_toggle"
            )

        if data.service_type is Service.JELLYFIN:
            await service_manager.clear_jellyfin()
            if data.enabled:
                await service_manager.initialize_jellyfin(
                    data.base_url, data.api_key, data.is_main
                )
        elif data.service_type is Service.PLEX:
            await service_manager.clear_plex()
            if data.enabled:
                await service_manager.initialize_plex(
                    data.base_url, data.api_key, data.is_main
                )
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
