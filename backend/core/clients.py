from __future__ import annotations

from backend.core.logger import LOG
from backend.core.settings import settings
from backend.services.jellyfin import JellyfinBackend
from backend.services.plex import PlexBackend
from backend.services.radarr import RadarrClient


class ClientManager:
    """Manages all service client instances with lazy initialization."""

    def __init__(self):
        """Initialize client manager with no active clients."""
        self._jellyfin: JellyfinBackend | None = None
        self._plex: PlexBackend | None = None
        self._radarr: RadarrClient | None = None
        # TODO: self._sonarr: SonarrClient | None = None

        LOG.info("ClientManager initialized")

    @property
    def jellyfin(self) -> JellyfinBackend | None:
        """Get Jellyfin client, lazy-initializing if configured."""
        if self._jellyfin is None and settings.jellyfin_api_key:
            try:
                from backend.services.jellyfin import JellyfinBackend

                self._jellyfin = JellyfinBackend(
                    api_key=settings.jellyfin_api_key,
                    jellyfin_url=settings.jellyfin_url,
                )
                LOG.info(f"Jellyfin client initialized: {settings.jellyfin_url}")
            except Exception as e:
                LOG.error(f"Failed to initialize Jellyfin client: {e}")
        return self._jellyfin

    @property
    def plex(self) -> PlexBackend | None:
        """Get Plex client, lazy-initializing if configured."""
        if self._plex is None and settings.plex_token:
            try:
                from backend.services.plex import PlexBackend

                self._plex = PlexBackend(
                    token=settings.plex_token,
                    plex_url=settings.plex_url,
                )
                LOG.info(f"Plex client initialized: {settings.plex_url}")
            except Exception as e:
                LOG.error(f"Failed to initialize Plex client: {e}")
        return self._plex

    @property
    def radarr(self) -> RadarrClient | None:
        """Get Radarr client, lazy-initializing if configured."""
        if self._radarr is None and settings.radarr_api_key:
            try:
                from backend.services.radarr import RadarrClient

                self._radarr = RadarrClient(
                    api_key=settings.radarr_api_key,
                    base_url=settings.radarr_url,
                )
                LOG.info(f"Radarr client initialized: {settings.radarr_url}")
            except Exception as e:
                LOG.error(f"Failed to initialize Radarr client: {e}")
        return self._radarr

    def reload_jellyfin(self) -> JellyfinBackend | None:
        """Force reload Jellyfin client (useful after config changes)."""
        self._jellyfin = None
        return self.jellyfin

    def reload_plex(self) -> PlexBackend | None:
        """Force reload Plex client (useful after config changes)."""
        self._plex = None
        return self.plex

    def reload_radarr(self) -> RadarrClient | None:
        """Force reload Radarr client (useful after config changes)."""
        self._radarr = None
        return self.radarr

    def reload_all(self) -> None:
        """Force reload all clients (useful after config changes)."""
        LOG.info("Reloading all clients")
        self._jellyfin = None
        self._plex = None
        self._radarr = None

    def get_status(self) -> dict[str, bool]:
        """Get connection status of all clients."""
        return {
            "jellyfin": self._jellyfin is not None,
            "plex": self._plex is not None,
            "radarr": self._radarr is not None,
        }

    # def cleanup(self) -> None:
    #     """Cleanup all client connections."""
    #     LOG.info("Cleaning up all clients")

    #     # close Radarr session
    #     if self._radarr is not None:
    #         try:
    #             if hasattr(self._radarr, "session"):
    #                 self._radarr.session.close()
    #         except Exception as e:
    #             LOG.warning(f"Error closing Radarr session: {e}")

    #     # Reset all clients
    #     self._jellyfin = None
    #     self._plex = None
    #     self._radarr = None


# global manager instance
client_manager = ClientManager()
