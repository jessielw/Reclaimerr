from __future__ import annotations

from cryptography.fernet import InvalidToken
from sqlalchemy import select

from backend.core.encryption import fer_decrypt
from backend.core.logger import LOG
from backend.core.service_manager import service_manager
from backend.database import async_db
from backend.database.models import ServiceConfig
from backend.enums import Service


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
            await service_manager.initialize_jellyfin(
                config.base_url, api_key, config.is_main
            )
        elif config.service_type is Service.EMBY:
            await service_manager.initialize_emby(
                config.base_url, api_key, config.is_main
            )
        elif config.service_type is Service.PLEX:
            await service_manager.initialize_plex(
                config.base_url, api_key, config.is_main
            )
        elif config.service_type is Service.RADARR:
            timeout = int((config.extra_settings or {}).get("timeout", 300))
            await service_manager.initialize_radarr(config.base_url, api_key, timeout)
        elif config.service_type is Service.SONARR:
            timeout = int((config.extra_settings or {}).get("timeout", 300))
            await service_manager.initialize_sonarr(config.base_url, api_key, timeout)
        elif config.service_type is Service.SEERR:
            await service_manager.initialize_seerr(config.base_url, api_key)
