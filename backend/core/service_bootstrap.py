from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import async_db
from backend.database.models import ServiceConfig
from backend.enums import Service

BOOTSTRAP_SERVICE_INIT_ATTEMPTS = 4
BOOTSTRAP_SERVICE_INIT_BACKOFF_SECONDS = (1.0, 3.0, 5.0)


async def _initialize_with_retry(
    service_name: str, initializer: Callable[[], Awaitable[Any]]
) -> Any:
    """Initialize a service with a short retry window for transient startup issues."""
    for attempt in range(1, BOOTSTRAP_SERVICE_INIT_ATTEMPTS + 1):
        result = await initializer()
        if result is not None:
            if attempt > 1:
                LOG.info(f"{service_name} service initialized after {attempt} attempts")
            return result

        if attempt < BOOTSTRAP_SERVICE_INIT_ATTEMPTS:
            delay = BOOTSTRAP_SERVICE_INIT_BACKOFF_SECONDS[
                min(attempt - 1, len(BOOTSTRAP_SERVICE_INIT_BACKOFF_SECONDS) - 1)
            ]
            LOG.warning(
                f"{service_name} service initialization failed on attempt "
                f"{attempt}/{BOOTSTRAP_SERVICE_INIT_ATTEMPTS}; retrying in {delay:g}s"
            )
            await asyncio.sleep(delay)

    LOG.error(
        f"{service_name} service initialization failed after "
        f"{BOOTSTRAP_SERVICE_INIT_ATTEMPTS} attempts"
    )
    return None


async def load_enabled_services() -> None:
    """Load enabled service configs from the database into the shared service manager."""
    await service_manager.clear_all()

    async with async_db() as db:
        result = await db.execute(
            select(ServiceConfig).where(ServiceConfig.enabled == True)
        )
        service_configs = result.scalars().all()

    if sum(config.is_main for config in service_configs) > 1:
        LOG.critical(
            "Multiple main media servers configured. Only one main media server is allowed."
        )
        raise ValueError(
            "Multiple main media servers configured. Only one main media server is allowed."
        )

    for config in service_configs:
        try:
            api_key = fer_decrypt(config.api_key)
        except InvalidToken:
            LOG.critical("Failed to decrypt API key - The ENCRYPTION_KEY has changed")
            raise RuntimeError(
                f"ENCRYPTION_KEY mismatch: cannot decrypt stored API key for {config.service_type}."
            ) from None

        if config.service_type is Service.JELLYFIN:
            await _initialize_with_retry(
                "Jellyfin",
                lambda: service_manager.initialize_jellyfin(
                    config.base_url, api_key, config.is_main
                ),
            )
        elif config.service_type is Service.EMBY:
            await _initialize_with_retry(
                "Emby",
                lambda: service_manager.initialize_emby(
                    config.base_url, api_key, config.is_main
                ),
            )
        elif config.service_type is Service.PLEX:
            await _initialize_with_retry(
                "Plex",
                lambda: service_manager.initialize_plex(
                    config.base_url, api_key, config.is_main
                ),
            )
        elif config.service_type is Service.RADARR:
            timeout = int((config.extra_settings or {}).get("timeout", 300))
            await _initialize_with_retry(
                "Radarr",
                lambda: service_manager.initialize_radarr(
                    config.base_url, api_key, timeout, config.id
                ),
            )
        elif config.service_type is Service.SONARR:
            timeout = int((config.extra_settings or {}).get("timeout", 300))
            await _initialize_with_retry(
                "Sonarr",
                lambda: service_manager.initialize_sonarr(
                    config.base_url, api_key, timeout, config.id
                ),
            )
        elif config.service_type is Service.SEERR:
            await _initialize_with_retry(
                "Seerr",
                lambda: service_manager.initialize_seerr(config.base_url, api_key),
            )
        elif config.service_type is Service.TAUTULLI:
            await _initialize_with_retry(
                "Tautulli",
                lambda: service_manager.initialize_tautulli(config.base_url, api_key),
            )
