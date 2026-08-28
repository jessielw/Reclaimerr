from __future__ import annotations

from typing import Any

import niquests.exceptions as niq_exceptions
import urllib3.exceptions as url3_exceptions

from backend.core.logger import LOG
from backend.enums import Service
from backend.services.emby import EmbyService
from backend.services.external_ratings import MDBListClient, OMDbClient
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.services.radarr import RadarrClient
from backend.services.seerr import SeerrClient
from backend.services.sonarr import SonarrClient
from backend.services.tautulli import TautulliClient
from backend.services.tracearr import TracearrClient


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
        self._jellyfin_clients: dict[int, JellyfinService] = {}
        self._emby_clients: dict[int, EmbyService] = {}
        self._plex_clients: dict[int, PlexService] = {}
        self._radarr: RadarrClient | None = None
        self._sonarr: SonarrClient | None = None
        self._radarr_clients: dict[int, RadarrClient] = {}
        self._sonarr_clients: dict[int, SonarrClient] = {}
        self._seerr: SeerrClient | None = None
        self._seerr_clients: dict[int, SeerrClient] = {}
        self._tautulli: TautulliClient | None = None
        self._tracearr: TracearrClient | None = None

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
    def main_media_server_type(self) -> Service | None:
        """Get the Service type of the current main media server.

        The single source of truth for "what type is main" - use this instead
        of identity-comparing against `service_manager.jellyfin`/`.emby`, which
        is ambiguous once a type has multiple configured instances.
        """
        main = self._main_media_server
        if main is None:
            return None
        if isinstance(main, JellyfinService):
            return Service.JELLYFIN
        if isinstance(main, EmbyService):
            return Service.EMBY
        if isinstance(main, PlexService):
            return Service.PLEX
        # Fallback for objects that aren't real client subclasses (e.g. test
        # doubles): identity-match against each type's default client, same
        # mechanism the old call sites used directly.
        if main is self._jellyfin:
            return Service.JELLYFIN
        if main is self._emby:
            return Service.EMBY
        if main is self._plex:
            return Service.PLEX
        return None

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

    def get_media_server(
        self, service_type: Service, config_id: int | None = None
    ) -> JellyfinService | EmbyService | PlexService | None:
        """Get a media server client by service type and config ID (must be
        initialized first). If config_id is None, return the type's default
        (last-initialized) client, matching `return_service`'s behavior."""
        if service_type is Service.JELLYFIN:
            return (
                self._jellyfin_clients.get(config_id) if config_id else self._jellyfin
            )
        if service_type is Service.EMBY:
            return self._emby_clients.get(config_id) if config_id else self._emby
        if service_type is Service.PLEX:
            return self._plex_clients.get(config_id) if config_id else self._plex
        return None

    def media_server_clients(
        self, service_type: Service
    ) -> dict[int, JellyfinService | EmbyService | PlexService]:
        """Get all clients of a media server type as a dict of config_id to client."""
        if service_type is Service.JELLYFIN:
            return dict(self._jellyfin_clients)
        if service_type is Service.EMBY:
            return dict(self._emby_clients)
        if service_type is Service.PLEX:
            return dict(self._plex_clients)
        return {}

    @property
    def radarr(self) -> RadarrClient | None:
        """Get Radarr service (must be initialized first)."""
        return self._radarr or next(iter(self._radarr_clients.values()), None)

    @property
    def sonarr(self) -> SonarrClient | None:
        """Get Sonarr service (must be initialized first)."""
        return self._sonarr or next(iter(self._sonarr_clients.values()), None)

    def get_radarr(self, config_id: int | None = None) -> RadarrClient | None:
        """Get Radarr service by config ID (must be initialized first). If config_id is None,
        return the main Radarr client."""
        if config_id is None:
            return self.radarr
        return self._radarr_clients.get(config_id)

    def get_sonarr(self, config_id: int | None = None) -> SonarrClient | None:
        """Get Sonarr service by config ID (must be initialized first). If config_id is None,
        return the main Sonarr client."""
        if config_id is None:
            return self.sonarr
        return self._sonarr_clients.get(config_id)

    def radarr_clients(self) -> dict[int, RadarrClient]:
        """Get all Radarr clients as a dict of config_id to client."""
        return dict(self._radarr_clients)

    def sonarr_clients(self) -> dict[int, SonarrClient]:
        """Get all Sonarr clients as a dict of config_id to client."""
        return dict(self._sonarr_clients)

    @property
    def seerr(self) -> SeerrClient | None:
        """Get Seerr service (must be initialized first).

        With several Seerrs configured this is an arbitrary one of them, kept
        only for callers that just need to know whether any Seerr is reachable.
        Anything that reads request data must go through `seerr_clients()` so it
        sees every instance.
        """
        return self._seerr or next(iter(self._seerr_clients.values()), None)

    @property
    def has_seerr(self) -> bool:
        """Whether any Seerr instance is initialized."""
        return bool(self._seerr_clients) or self._seerr is not None

    def get_seerr(self, config_id: int | None = None) -> SeerrClient | None:
        """Get a Seerr client by config ID (must be initialized first). If
        config_id is None, return the type's default client."""
        if config_id is None:
            return self.seerr
        return self._seerr_clients.get(config_id)

    def seerr_clients(self) -> dict[int, SeerrClient]:
        """Get all Seerr clients as a dict of config_id to client."""
        return dict(self._seerr_clients)

    @property
    def tautulli(self) -> TautulliClient | None:
        """Get Tautulli service (must be initialized first)."""
        return self._tautulli

    @property
    def tracearr(self) -> TracearrClient | None:
        """Get Tracearr service (must be initialized first)."""
        return self._tracearr

    async def get_status(self) -> dict[str, bool]:
        """Get connection status of all clients."""
        return {
            "jellyfin": self._jellyfin is not None,
            "emby": self._emby is not None,
            "plex": self._plex is not None,
            "radarr": self.radarr is not None,
            "sonarr": self.sonarr is not None,
            "seerr": self.has_seerr,
            "tautulli": self._tautulli is not None,
            "tracearr": self._tracearr is not None,
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
            elif service_type is Service.TAUTULLI:
                return await TautulliClient.test_service(url, api_key), ""
            elif service_type is Service.TRACEARR:
                return await TracearrClient.test_service(url, api_key), ""
            elif service_type is Service.MDBLIST:
                return await MDBListClient.test_service(url, api_key), ""
            elif service_type is Service.OMDB:
                return await OMDbClient.test_service(url, api_key), ""
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
        | TautulliClient
        | TracearrClient
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
            return self.seerr
        elif service_type is Service.TAUTULLI:
            return self._tautulli
        elif service_type is Service.TRACEARR:
            return self._tracearr
        return None

    async def initialize_jellyfin(
        self,
        base_url: str,
        api_key: str,
        is_main: bool,
        config_id: int | None = None,
    ) -> JellyfinService | None:
        """Initialize Jellyfin service with provided config."""
        try:
            client = JellyfinService(
                api_key=api_key,
                base_url=base_url,
            )
            if not await client.health():
                LOG.error(f"Jellyfin service health check failed: {base_url}")
                raise ValueError(f"Jellyfin service health check failed: {base_url}")
            if config_id is not None:
                self._jellyfin_clients[config_id] = client
            self._jellyfin = client
            LOG.info(f"Jellyfin service initialized: {base_url}")
            if is_main:
                self._main_media_server = client
            return client
        except Exception as e:
            LOG.error(f"Failed to initialize Jellyfin service: {e}")
            return None

    async def initialize_emby(
        self,
        base_url: str,
        api_key: str,
        is_main: bool,
        config_id: int | None = None,
    ) -> EmbyService | None:
        """Initialize Emby service with provided config."""
        try:
            client = EmbyService(
                api_key=api_key,
                base_url=base_url,
            )
            if not await client.health():
                LOG.error(f"Emby service health check failed: {base_url}")
                raise ValueError(f"Emby service health check failed: {base_url}")
            if config_id is not None:
                self._emby_clients[config_id] = client
            self._emby = client
            LOG.info(f"Emby service initialized: {base_url}")
            if is_main:
                self._main_media_server = client
            return client
        except Exception as e:
            LOG.error(f"Failed to initialize Emby service: {e}")
            return None

    async def initialize_plex(
        self,
        base_url: str,
        token: str,
        is_main: bool,
        config_id: int | None = None,
    ) -> PlexService | None:
        """Initialize Plex service with provided config."""
        try:
            client = PlexService(
                token=token,
                plex_url=base_url,
            )
            if not await client.health():
                LOG.error(f"Plex service health check failed: {base_url}")
                raise ValueError(f"Plex service health check failed: {base_url}")
            if config_id is not None:
                self._plex_clients[config_id] = client
            self._plex = client
            LOG.info(f"Plex service initialized: {base_url}")
            if is_main:
                self._main_media_server = client
            return client
        except Exception as e:
            LOG.error(f"Failed to initialize Plex service: {e}")
            return None

    async def initialize_radarr(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 300,
        config_id: int | None = None,
    ) -> RadarrClient | None:
        """Initialize Radarr service with provided config."""
        try:
            client = RadarrClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if not await client.health():
                LOG.error(f"Radarr service health check failed: {base_url}")
                raise ValueError(f"Radarr service health check failed: {base_url}")
            if config_id is not None:
                self._radarr_clients[config_id] = client
            self._radarr = client
            LOG.info(f"Radarr service initialized: {base_url}")
            return client
        except Exception as e:
            LOG.error(f"Failed to initialize Radarr service: {e}")
            return None

    async def initialize_sonarr(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 300,
        config_id: int | None = None,
    ) -> SonarrClient | None:
        """Initialize Sonarr service with provided config."""
        try:
            client = SonarrClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if not await client.health():
                LOG.error(f"Sonarr service health check failed: {base_url}")
                raise ValueError(f"Sonarr service health check failed: {base_url}")
            if config_id is not None:
                self._sonarr_clients[config_id] = client
            self._sonarr = client
            LOG.info(f"Sonarr service initialized: {base_url}")
            return client
        except Exception as e:
            LOG.error(f"Failed to initialize Sonarr service: {e}")
            return None

    async def initialize_seerr(
        self,
        base_url: str,
        api_key: str,
        config_id: int | None = None,
    ) -> SeerrClient | None:
        """Initialize Seerr service with provided config."""
        try:
            client = SeerrClient(
                api_key=api_key,
                base_url=base_url,
            )
            if not await client.health():
                LOG.error(f"Seerr service health check failed: {base_url}")
                raise ValueError(f"Seerr service health check failed: {base_url}")
            if config_id is not None:
                self._seerr_clients[config_id] = client
            self._seerr = client
            LOG.info(f"Seerr service initialized: {base_url}")
            return client
        except Exception as e:
            LOG.error(f"Failed to initialize Seerr service: {e}")
            return None

    async def initialize_tautulli(
        self, base_url: str, api_key: str, timeout: int = 30
    ) -> TautulliClient | None:
        """Initialize Tautulli service with provided config."""
        try:
            self._tautulli = TautulliClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if not await self._tautulli.health():
                LOG.error(f"Tautulli service health check failed: {base_url}")
                raise ValueError(f"Tautulli service health check failed: {base_url}")
            LOG.info(f"Tautulli service initialized: {base_url}")
            return self._tautulli
        except Exception as e:
            LOG.error(f"Failed to initialize Tautulli service: {e}")
            return None

    async def initialize_tracearr(
        self, base_url: str, api_key: str, timeout: int = 30
    ) -> TracearrClient | None:
        """Initialize Tracearr with a public API v2 key."""
        client: TracearrClient | None = None
        try:
            client = TracearrClient(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
            )
            if not await client.health():
                LOG.error(f"Tracearr service health check failed: {base_url}")
                raise ValueError(f"Tracearr service health check failed: {base_url}")
            self._tracearr = client
            LOG.info(f"Tracearr service initialized: {base_url}")
            return self._tracearr
        except Exception as e:
            if client is not None:
                await client.session.close()
            self._tracearr = None
            LOG.error(f"Failed to initialize Tracearr service: {e}")
            return None

    async def clear_jellyfin(self, config_id: int | None = None) -> None:
        """Clear Jellyfin service(s) (call before reinitializing).

        If config_id is given, clear only that instance; otherwise clear all
        Jellyfin instances (mirrors clear_radarr/clear_sonarr)."""
        if config_id is not None:
            client = self._jellyfin_clients.pop(config_id, None)
            if self._main_media_server is client and client is not None:
                self._main_media_server = None
            if client and client.session:
                await client.session.close()
            if self._jellyfin is client:
                self._jellyfin = next(iter(self._jellyfin_clients.values()), None)
            return

        if isinstance(self._main_media_server, JellyfinService):
            self._main_media_server = None
        if self._jellyfin and self._jellyfin.session:
            await self._jellyfin.session.close()
        for client in self._jellyfin_clients.values():
            if client is not self._jellyfin and client.session:
                await client.session.close()
        if self._jellyfin or self._jellyfin_clients:
            LOG.info("Jellyfin service cleared")
        self._jellyfin_clients = {}
        self._jellyfin = None

    async def clear_emby(self, config_id: int | None = None) -> None:
        """Clear Emby service(s) (call before reinitializing).

        If config_id is given, clear only that instance; otherwise clear all
        Emby instances (mirrors clear_radarr/clear_sonarr)."""
        if config_id is not None:
            client = self._emby_clients.pop(config_id, None)
            if self._main_media_server is client and client is not None:
                self._main_media_server = None
            if client and client.session:
                await client.session.close()
            if self._emby is client:
                self._emby = next(iter(self._emby_clients.values()), None)
            return

        if isinstance(self._main_media_server, EmbyService):
            self._main_media_server = None
        if self._emby and self._emby.session:
            await self._emby.session.close()
        for client in self._emby_clients.values():
            if client is not self._emby and client.session:
                await client.session.close()
        if self._emby or self._emby_clients:
            LOG.info("Emby service cleared")
        self._emby_clients = {}
        self._emby = None

    async def clear_plex(self, config_id: int | None = None) -> None:
        """Clear Plex service(s) (call before reinitializing).

        If config_id is given, clear only that instance; otherwise clear all
        Plex instances (mirrors clear_radarr/clear_sonarr)."""
        if config_id is not None:
            client = self._plex_clients.pop(config_id, None)
            if self._main_media_server is client and client is not None:
                self._main_media_server = None
            if client and client.session:
                await client.session.close()
            if self._plex is client:
                self._plex = next(iter(self._plex_clients.values()), None)
            return

        if isinstance(self._main_media_server, PlexService):
            self._main_media_server = None
        if self._plex and self._plex.session:
            await self._plex.session.close()
        for client in self._plex_clients.values():
            if client is not self._plex and client.session:
                await client.session.close()
        if self._plex or self._plex_clients:
            LOG.info("Plex service cleared")
        self._plex_clients = {}
        self._plex = None

    async def clear_radarr(self, config_id: int | None = None) -> None:
        """Clear Radarr services (call before reinitializing)."""
        if config_id is not None:
            client = self._radarr_clients.pop(config_id, None)
            if client and client.session:
                await client.session.close()
            if self._radarr is client:
                self._radarr = next(iter(self._radarr_clients.values()), None)
            return

        if self._radarr and self._radarr.session:
            await self._radarr.session.close()
            LOG.info("Radarr service cleared")
        for client in self._radarr_clients.values():
            if client is not self._radarr and client.session:
                await client.session.close()
        self._radarr_clients = {}
        self._radarr = None

    async def clear_sonarr(self, config_id: int | None = None) -> None:
        """Clear Sonarr services (call before reinitializing)."""
        if config_id is not None:
            client = self._sonarr_clients.pop(config_id, None)
            if client and client.session:
                await client.session.close()
            if self._sonarr is client:
                self._sonarr = next(iter(self._sonarr_clients.values()), None)
            return

        if self._sonarr and self._sonarr.session:
            await self._sonarr.session.close()
            LOG.info("Sonarr service cleared")
        for client in self._sonarr_clients.values():
            if client is not self._sonarr and client.session:
                await client.session.close()
        self._sonarr_clients = {}
        self._sonarr = None

    async def clear_seerr(self, config_id: int | None = None) -> None:
        """Clear Seerr service(s) (call before reinitializing).

        If config_id is given, clear only that instance; otherwise clear all
        Seerr instances (mirrors clear_radarr/clear_sonarr)."""
        if config_id is not None:
            client = self._seerr_clients.pop(config_id, None)
            if client and client.session:
                await client.session.close()
            if self._seerr is client:
                self._seerr = next(iter(self._seerr_clients.values()), None)
            return

        if self._seerr and self._seerr.session:
            await self._seerr.session.close()
            LOG.info("Seerr service cleared")
        for client in self._seerr_clients.values():
            if client is not self._seerr and client.session:
                await client.session.close()
        self._seerr_clients = {}
        self._seerr = None

    async def clear_tautulli(self) -> None:
        """Clear Tautulli service (call before reinitializing)."""
        if self._tautulli and self._tautulli.session:
            await self._tautulli.session.close()
            LOG.info("Tautulli service cleared")
        self._tautulli = None

    async def clear_tracearr(self) -> None:
        """Clear Tracearr service (call before reinitializing)."""
        if self._tracearr and self._tracearr.session:
            await self._tracearr.session.close()
            LOG.info("Tracearr service cleared")
        self._tracearr = None

    async def clear_all(self) -> None:
        """Clear all clients (call before reinitializing from database)."""
        LOG.info("Clearing all clients")
        await self.clear_jellyfin()
        await self.clear_emby()
        await self.clear_plex()
        await self.clear_radarr()
        await self.clear_sonarr()
        await self.clear_seerr()
        await self.clear_tautulli()
        await self.clear_tracearr()

    def clear_transient_caches(self) -> None:
        """Clear large transient caches on long-lived service clients."""
        seen: set[int] = set()
        clients: list[Any] = [
            self._main_media_server,
            self._jellyfin,
            self._emby,
            self._plex,
            *self._jellyfin_clients.values(),
            *self._emby_clients.values(),
            *self._plex_clients.values(),
        ]
        for client in clients:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            clear_method = getattr(client, "clear_transient_caches", None)
            if callable(clear_method):
                clear_method()


# global manager instance
service_manager = ServiceManager()
