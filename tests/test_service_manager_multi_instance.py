"""Unit tests for ServiceManager's multi-instance media-server support.

Mirrors the existing _radarr_clients/_sonarr_clients dict-per-config_id
pattern being extended to Plex/Jellyfin/Emby: two ServiceConfig rows of the
SAME type must resolve to two independent, addressable clients rather than
one silently overwriting the other.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from backend.core.service_manager import ServiceManager
from backend.enums import Service


class _FakeMediaClientBase:
    """Stand-in base for PlexService/JellyfinService/EmbyService that skips
    real I/O. Subclassed per type so isinstance()-based checks (e.g.
    main_media_server_type) still work against the patched class names."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.health = AsyncMock(return_value=True)
        self.session = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakePlexService(_FakeMediaClientBase):
    pass


class _FakeJellyfinService(_FakeMediaClientBase):
    pass


class _FakeEmbyService(_FakeMediaClientBase):
    pass


def _isolated_manager(monkeypatch) -> ServiceManager:
    """A fresh ServiceManager with the class constructors patched to fakes."""
    manager = ServiceManager()
    monkeypatch.setattr(
        "backend.core.service_manager.PlexService", _FakePlexService
    )
    monkeypatch.setattr(
        "backend.core.service_manager.JellyfinService", _FakeJellyfinService
    )
    monkeypatch.setattr(
        "backend.core.service_manager.EmbyService", _FakeEmbyService
    )
    return manager


def test_two_same_type_plex_instances_are_independently_addressable(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)

        client_a = await manager.initialize_plex(
            "http://plex-a", "token-a", is_main=True, config_id=1
        )
        client_b = await manager.initialize_plex(
            "http://plex-b", "token-b", is_main=False, config_id=2
        )

        assert client_a is not None
        assert client_b is not None
        assert client_a is not client_b

        # each config resolves to its own client, not whichever loaded last
        assert manager.get_media_server(Service.PLEX, config_id=1) is client_a
        assert manager.get_media_server(Service.PLEX, config_id=2) is client_b
        assert manager.media_server_clients(Service.PLEX) == {1: client_a, 2: client_b}

        # is_main=True on config 1 makes it the main client, unambiguously
        assert manager.main_media_server is client_a
        assert manager.main_media_server_type is Service.PLEX

    asyncio.run(run())


def test_get_media_server_without_config_id_returns_last_initialized(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)

        client_a = await manager.initialize_plex(
            "http://plex-a", "token-a", is_main=True, config_id=1
        )
        client_b = await manager.initialize_plex(
            "http://plex-b", "token-b", is_main=False, config_id=2
        )

        # config_id=None mirrors return_service's singleton-fallback behavior
        assert manager.get_media_server(Service.PLEX) is client_b
        assert client_a is not client_b

    asyncio.run(run())


def test_clear_plex_by_config_id_only_removes_that_instance(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        client_a = await manager.initialize_plex(
            "http://plex-a", "token-a", is_main=True, config_id=1
        )
        client_b = await manager.initialize_plex(
            "http://plex-b", "token-b", is_main=False, config_id=2
        )

        await manager.clear_plex(config_id=2)

        assert manager.get_media_server(Service.PLEX, config_id=2) is None
        assert manager.get_media_server(Service.PLEX, config_id=1) is client_a
        # main was config 1, untouched by clearing config 2
        assert manager.main_media_server is client_a

    asyncio.run(run())


def test_clear_plex_of_the_main_instance_clears_main_media_server(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        await manager.initialize_plex(
            "http://plex-a", "token-a", is_main=True, config_id=1
        )
        await manager.initialize_plex(
            "http://plex-b", "token-b", is_main=False, config_id=2
        )

        await manager.clear_plex(config_id=1)

        assert manager.get_media_server(Service.PLEX, config_id=1) is None
        assert manager.main_media_server is None
        assert manager.main_media_server_type is None

    asyncio.run(run())


def test_clear_plex_with_no_config_id_clears_all_instances(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        await manager.initialize_plex(
            "http://plex-a", "token-a", is_main=True, config_id=1
        )
        await manager.initialize_plex(
            "http://plex-b", "token-b", is_main=False, config_id=2
        )

        await manager.clear_plex()

        assert manager.media_server_clients(Service.PLEX) == {}
        assert manager.plex is None
        assert manager.main_media_server is None

    asyncio.run(run())


def test_main_media_server_type_distinguishes_across_types(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        await manager.initialize_plex(
            "http://plex-a", "token-a", is_main=False, config_id=1
        )
        jellyfin_main = await manager.initialize_jellyfin(
            "http://jellyfin-a", "key-a", is_main=True, config_id=2
        )

        assert manager.main_media_server is jellyfin_main
        assert manager.main_media_server_type is Service.JELLYFIN

    asyncio.run(run())
