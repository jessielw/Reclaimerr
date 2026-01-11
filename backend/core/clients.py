from __future__ import annotations

from backend.core.logger import LOG
from backend.services.jellyfin import JellyfinBackend
from backend.services.plex import PlexBackend
from backend.services.radarr import RadarrClient
from backend.services.seerr import SeerrClient
from backend.services.sonarr import SonarrClient


class ClientManager:
    """Manages all service client instances.

    Service configurations should be loaded from the database (ServiceConfig table)
    and passed to the initialize_* methods.
    """

    def __init__(self) -> None:
        """Initialize client manager with no active clients."""
        self._jellyfin: JellyfinBackend | None = None
        self._plex: PlexBackend | None = None
        self._radarr: RadarrClient | None = None
        self._sonarr: SonarrClient | None = None
        self._seerr: SeerrClient | None = None

        LOG.info("ClientManager initialized")

    @property
    def jellyfin(self) -> JellyfinBackend | None:
        """Get Jellyfin client (must be initialized first)."""
        return self._jellyfin

    @property
    def plex(self) -> PlexBackend | None:
        """Get Plex client (must be initialized first)."""
        return self._plex

    @property
    def radarr(self) -> RadarrClient | None:
        """Get Radarr client (must be initialized first)."""
        return self._radarr

    @property
    def sonarr(self) -> SonarrClient | None:
        """Get Sonarr client (must be initialized first)."""
        return self._sonarr

    @property
    def seerr(self) -> SeerrClient | None:
        """Get Seerr client (must be initialized first)."""
        return self._seerr

    async def initialize_jellyfin(
        self, base_url: str, api_key: str
    ) -> JellyfinBackend | None:
        """Initialize Jellyfin client with provided config."""
        try:
            self._jellyfin = JellyfinBackend(
                api_key=api_key,
                jellyfin_url=base_url,
            )
            if not await self._jellyfin.health():
                LOG.error(f"Jellyfin client health check failed: {base_url}")
                raise ValueError(f"Jellyfin client health check failed: {base_url}")
            LOG.info(f"Jellyfin client initialized: {base_url}")
            return self._jellyfin
        except Exception as e:
            LOG.error(f"Failed to initialize Jellyfin client: {e}")
            return None

    async def initialize_plex(self, base_url: str, token: str) -> PlexBackend | None:
        """Initialize Plex client with provided config."""
        try:
            self._plex = PlexBackend(
                token=token,
                plex_url=base_url,
            )
            if not await self._plex.health():
                LOG.error(f"Plex client health check failed: {base_url}")
                raise ValueError(f"Plex client health check failed: {base_url}")
            LOG.info(f"Plex client initialized: {base_url}")
            return self._plex
        except Exception as e:
            LOG.error(f"Failed to initialize Plex client: {e}")
            return None

    async def initialize_radarr(
        self, base_url: str, api_key: str
    ) -> RadarrClient | None:
        """Initialize Radarr client with provided config."""
        try:
            self._radarr = RadarrClient(
                api_key=api_key,
                base_url=base_url,
            )
            if not await self._radarr.health():
                LOG.error(f"Radarr client health check failed: {base_url}")
                raise ValueError(f"Radarr client health check failed: {base_url}")
            LOG.info(f"Radarr client initialized: {base_url}")
            return self._radarr
        except Exception as e:
            LOG.error(f"Failed to initialize Radarr client: {e}")
            return None

    async def initialize_sonarr(
        self, base_url: str, api_key: str
    ) -> SonarrClient | None:
        """Initialize Sonarr client with provided config."""
        try:
            self._sonarr = SonarrClient(
                api_key=api_key,
                base_url=base_url,
            )
            if not await self._sonarr.health():
                LOG.error(f"Sonarr client health check failed: {base_url}")
                raise ValueError(f"Sonarr client health check failed: {base_url}")
            LOG.info(f"Sonarr client initialized: {base_url}")
            return self._sonarr
        except Exception as e:
            LOG.error(f"Failed to initialize Sonarr client: {e}")
            return None

    async def initialize_seerr(self, base_url: str, api_key: str) -> SeerrClient | None:
        """Initialize Seerr client with provided config."""
        try:
            self._seerr = SeerrClient(
                api_key=api_key,
                base_url=base_url,
            )
            if not await self._seerr.health():
                LOG.error(f"Seerr client health check failed: {base_url}")
                raise ValueError(f"Seerr client health check failed: {base_url}")
            LOG.info(f"Seerr client initialized: {base_url}")
            return self._seerr
        except Exception as e:
            LOG.error(f"Failed to initialize Seerr client: {e}")
            return None

    async def clear_jellyfin(self) -> None:
        """Clear Jellyfin client (call before reinitializing)."""
        if self._jellyfin and self._jellyfin.session:
            await self._jellyfin.session.close()
            LOG.info("Jellyfin client cleared")
        self._jellyfin = None

    async def clear_plex(self) -> None:
        """Clear Plex client (call before reinitializing)."""
        if self._plex and self._plex.session:
            await self._plex.session.close()
            LOG.info("Plex client cleared")
        self._plex = None

    async def clear_radarr(self) -> None:
        """Clear Radarr client (call before reinitializing)."""
        if self._radarr and self._radarr.session:
            await self._radarr.session.close()
            LOG.info("Radarr client cleared")
        self._radarr = None

    async def clear_sonarr(self) -> None:
        """Clear Sonarr client (call before reinitializing)."""
        if self._sonarr and self._sonarr.session:
            await self._sonarr.session.close()
            LOG.info("Sonarr client cleared")
        self._sonarr = None

    async def clear_seerr(self) -> None:
        """Clear Seerr client (call before reinitializing)."""
        if self._seerr and self._seerr.session:
            await self._seerr.session.close()
            LOG.info("Seerr client cleared")
        self._seerr = None

    async def clear_all(self) -> None:
        """Clear all clients (call before reinitializing from database)."""
        LOG.info("Clearing all clients")
        await self.clear_jellyfin()
        await self.clear_plex()
        await self.clear_radarr()
        await self.clear_sonarr()
        await self.clear_seerr()

    def get_status(self) -> dict[str, bool]:
        """Get connection status of all clients."""
        return {
            "jellyfin": self._jellyfin is not None,
            "plex": self._plex is not None,
            "radarr": self._radarr is not None,
            "sonarr": self._sonarr is not None,
            "seerr": self._seerr is not None,
        }


# global manager instance
client_manager = ClientManager()
