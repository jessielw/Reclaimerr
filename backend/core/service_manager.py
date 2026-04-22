from __future__ import annotations

import niquests.exceptions as niq_exceptions
import urllib3.exceptions as url3_exceptions

from backend.core.logger import LOG
from backend.enums import Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.services.radarr import RadarrClient
from backend.services.seerr import SeerrClient
from backend.services.sonarr import SonarrClient


class ServiceManager:
    """Manages all service instances.

    Service configurations should be loaded from the database (ServiceConfig table)
    and passed to the initialize_* methods.
    """

    def __init__(self) -> None:
        """Initialize service manager with no active clients."""
        self._main_media_server: JellyfinService | EmbyService | PlexService | None = (
            None
        )
        self._jellyfin: JellyfinService | None = None
        self._emby: EmbyService | None = None
        self._plex: PlexService | None = None
        self._radarr: RadarrClient | None = None
        self._sonarr: SonarrClient | None = None
        self._seerr: SeerrClient | None = None

        LOG.info("ServiceManager initialized")

    @property
    def main_media_server(self) -> JellyfinService | EmbyService | PlexService | None:
        """Get the main media server client (must be initialized first)."""
        return self._main_media_server

    @main_media_server.setter
    def main_media_server(
        self, service: JellyfinService | EmbyService | PlexService
    ) -> None:
        """Set the main media server client (must be initialized first)."""
        self._main_media_server = service

    @property
    def jellyfin(self) -> JellyfinService | None:
        """Get Jellyfin service (must be initialized first)."""
        return self._jellyfin

    @property
    def emby(self) -> EmbyService | None:
        """Get Emby service (must be initialized first)."""
        return self._emby

    @property
    def plex(self) -> PlexService | None:
        """Get Plex service (must be initialized first)."""
        return self._plex

    @property
    def radarr(self) -> RadarrClient | None:
        """Get Radarr service (must be initialized first)."""
        return self._radarr

    @property
    def sonarr(self) -> SonarrClient | None:
        """Get Sonarr service (must be initialized first)."""
        return self._sonarr

    @property
    def seerr(self) -> SeerrClient | None:
        """Get Seerr service (must be initialized first)."""
        return self._seerr

    async def get_status(self) -> dict[str, bool]:
        """Get connection status of all clients."""
        return {
            "jellyfin": self._jellyfin is not None,
            "emby": self._emby is not None,
            "plex": self._plex is not None,
            "radarr": self._radarr is not None,
            "sonarr": self._sonarr is not None,
            "seerr": self._seerr is not None,
        }

    async def test_service(
        self, service_type: Service, url: str, api_key: str
    ) -> tuple[bool, str]:
        """Test if the specified service is initialized."""
        try:
            if service_type is Service.JELLYFIN:
                return await JellyfinService.test_service(url, api_key), ""
            elif service_type is Service.EMBY:
                return await EmbyService.test_service(url, api_key), ""
            elif service_type is Service.PLEX:
                return await PlexService.test_service(url, api_key), ""
            elif service_type is Service.RADARR:
                return await RadarrClient.test_service(url, api_key), ""
            elif service_type is Service.SONARR:
                return await SonarrClient.test_service(url, api_key), ""
            elif service_type is Service.SEERR:
                return await SeerrClient.test_service(url, api_key), ""
        except niq_exceptions.ConnectionError:
            return (
                False,
                "Could not connect to the server. Please check the URL and network.",
            )
        except url3_exceptions.NameResolutionError:
            return False, "Could not resolve the server address. Please check the URL."
        except niq_exceptions.HTTPError:
            return False, "Invalid API key or server error."
        except niq_exceptions.Timeout:
            return False, "Connection timed out. The server may be down or unreachable."
        except niq_exceptions.TooManyRedirects:
            return False, "Too many redirects. Please check the server URL."
        except niq_exceptions.InvalidURL:
            return False, "Invalid URL. Please check the address."
        except Exception as e:
            LOG.error(f"Unexpected error testing {service_type}: {e}")
            return False, "An unknown error occurred while testing the service."

    async def return_service(
        self, service_type: Service
    ) -> (
        JellyfinService
        | EmbyService
        | PlexService
        | RadarrClient
        | SonarrClient
        | SeerrClient
        | None
    ):
        """Return the requested service instance."""
        if service_type is Service.JELLYFIN:
            return self._jellyfin
        elif service_type is Service.EMBY:
            return self._emby
        elif service_type is Service.PLEX:
            return self._plex
        elif service_type is Service.RADARR:
            return self._radarr
        elif service_type is Service.SONARR:
            return self._sonarr
        elif service_type is Service.SEERR:
            return self._seerr

    async def initialize_jellyfin(
        self, base_url: str, api_key: str, is_main: bool
    ) -> JellyfinService | None:
        """Initialize Jellyfin service with provided config."""
        try:
            self._jellyfin = JellyfinService(
                api_key=api_key,
                base_url=base_url,
            )
            if not await self._jellyfin.health():
                LOG.error(f"Jellyfin service health check failed: {base_url}")
                raise ValueError(f"Jellyfin service health check failed: {base_url}")
            LOG.info(f"Jellyfin service initialized: {base_url}")
            if is_main:
                self._main_media_server = self._jellyfin
            return self._jellyfin
        except Exception as e:
            LOG.error(f"Failed to initialize Jellyfin service: {e}")
            return None

    async def initialize_emby(
        self, base_url: str, api_key: str, is_main: bool
    ) -> EmbyService | None:
        """Initialize Emby service with provided config."""
        try:
            self._emby = EmbyService(
                api_key=api_key,
                base_url=base_url,
            )
            if not await self._emby.health():
                LOG.error(f"Emby service health check failed: {base_url}")
                raise ValueError(f"Emby service health check failed: {base_url}")
            LOG.info(f"Emby service initialized: {base_url}")
            if is_main:
                self._main_media_server = self._emby
            return self._emby
        except Exception as e:
            LOG.error(f"Failed to initialize Emby service: {e}")
            return None

    async def initialize_plex(
        self, base_url: str, token: str, is_main: bool
    ) -> PlexService | None:
        """Initialize Plex service with provided config."""
        try:
            self._plex = PlexService(
                token=token,
                plex_url=base_url,
            )
            if not await self._plex.health():
                LOG.error(f"Plex service health check failed: {base_url}")
                raise ValueError(f"Plex service health check failed: {base_url}")
            LOG.info(f"Plex service initialized: {base_url}")
            if is_main:
                self._main_media_server = self._plex
            return self._plex
        except Exception as e:
            LOG.error(f"Failed to initialize Plex service: {e}")
            return None

    async def initialize_radarr(
        self, base_url: str, api_key: str, timeout: int = 300
    ) -> RadarrClient | None:
        """Initialize Radarr service with provided config."""
        try:
            self._radarr = RadarrClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if not await self._radarr.health():
                LOG.error(f"Radarr service health check failed: {base_url}")
                raise ValueError(f"Radarr service health check failed: {base_url}")
            LOG.info(f"Radarr service initialized: {base_url}")
            return self._radarr
        except Exception as e:
            LOG.error(f"Failed to initialize Radarr service: {e}")
            return None

    async def initialize_sonarr(
        self, base_url: str, api_key: str, timeout: int = 300
    ) -> SonarrClient | None:
        """Initialize Sonarr service with provided config."""
        try:
            self._sonarr = SonarrClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if not await self._sonarr.health():
                LOG.error(f"Sonarr service health check failed: {base_url}")
                raise ValueError(f"Sonarr service health check failed: {base_url}")
            LOG.info(f"Sonarr service initialized: {base_url}")
            return self._sonarr
        except Exception as e:
            LOG.error(f"Failed to initialize Sonarr service: {e}")
            return None

    async def initialize_seerr(self, base_url: str, api_key: str) -> SeerrClient | None:
        """Initialize Seerr service with provided config."""
        try:
            self._seerr = SeerrClient(
                api_key=api_key,
                base_url=base_url,
            )
            if not await self._seerr.health():
                LOG.error(f"Seerr service health check failed: {base_url}")
                raise ValueError(f"Seerr service health check failed: {base_url}")
            LOG.info(f"Seerr service initialized: {base_url}")
            return self._seerr
        except Exception as e:
            LOG.error(f"Failed to initialize Seerr service: {e}")
            return None

    async def clear_jellyfin(self) -> None:
        """Clear Jellyfin service (call before reinitializing)."""
        if self._main_media_server is self._jellyfin:
            self._main_media_server = None
        if self._jellyfin and self._jellyfin.session:
            await self._jellyfin.session.close()
            LOG.info("Jellyfin service cleared")
        self._jellyfin = None

    async def clear_emby(self) -> None:
        """Clear Emby service (call before reinitializing)."""
        if self._main_media_server is self._emby:
            self._main_media_server = None
        if self._emby and self._emby.session:
            await self._emby.session.close()
            LOG.info("Emby service cleared")
        self._emby = None

    async def clear_plex(self) -> None:
        """Clear Plex service (call before reinitializing)."""
        if self._main_media_server is self._plex:
            self._main_media_server = None
        if self._plex and self._plex.session:
            await self._plex.session.close()
            LOG.info("Plex service cleared")
        self._plex = None

    async def clear_radarr(self) -> None:
        """Clear Radarr service (call before reinitializing)."""
        if self._radarr and self._radarr.session:
            await self._radarr.session.close()
            LOG.info("Radarr service cleared")
        self._radarr = None

    async def clear_sonarr(self) -> None:
        """Clear Sonarr service (call before reinitializing)."""
        if self._sonarr and self._sonarr.session:
            await self._sonarr.session.close()
            LOG.info("Sonarr service cleared")
        self._sonarr = None

    async def clear_seerr(self) -> None:
        """Clear Seerr service (call before reinitializing)."""
        if self._seerr and self._seerr.session:
            await self._seerr.session.close()
            LOG.info("Seerr service cleared")
        self._seerr = None

    async def clear_all(self) -> None:
        """Clear all clients (call before reinitializing from database)."""
        LOG.info("Clearing all clients")
        await self.clear_jellyfin()
        await self.clear_emby()
        await self.clear_plex()
        await self.clear_radarr()
        await self.clear_sonarr()
        await self.clear_seerr()


# global manager instance
service_manager = ServiceManager()
