"""Two Seerr configs must resolve to two independent, addressable clients.

Seerr was the last service type still holding a single client slot, so saving a
second instance silently replaced the first. This mirrors the media-server and
arr coverage: same type, two configs, neither shadowing the other.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from backend.core.service_manager import ServiceManager
from backend.enums import Service


class _FakeSeerrClient:
    """Stand-in for SeerrClient that skips real I/O."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.base_url = str(kwargs.get("base_url", ""))
        self.health = AsyncMock(return_value=True)
        self.session = None


def _isolated_manager(monkeypatch) -> ServiceManager:
    manager = ServiceManager()
    monkeypatch.setattr("backend.core.service_manager.SeerrClient", _FakeSeerrClient)
    return manager


def test_two_seerr_instances_are_independently_addressable(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)

        client_a = await manager.initialize_seerr("http://overseerr", "key-a", 1)
        client_b = await manager.initialize_seerr("http://jellyseerr", "key-b", 2)

        assert client_a is not None
        assert client_b is not None
        assert client_a is not client_b
        assert manager.get_seerr(1) is client_a
        assert manager.get_seerr(2) is client_b
        assert manager.seerr_clients() == {1: client_a, 2: client_b}
        assert manager.has_seerr is True

    asyncio.run(run())


def test_seerr_clients_returns_a_copy(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        await manager.initialize_seerr("http://overseerr", "key-a", 1)

        clients = manager.seerr_clients()
        clients.clear()

        assert len(manager.seerr_clients()) == 1

    asyncio.run(run())


def test_clearing_one_instance_leaves_the_other(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        client_a = await manager.initialize_seerr("http://overseerr", "key-a", 1)
        client_b = await manager.initialize_seerr("http://jellyseerr", "key-b", 2)

        await manager.clear_seerr(1)

        assert manager.get_seerr(1) is None
        assert manager.get_seerr(2) is client_b
        assert manager.seerr_clients() == {2: client_b}
        assert manager.has_seerr is True
        assert client_a is not None

    asyncio.run(run())


def test_clearing_the_default_repoints_it_at_a_survivor(monkeypatch):
    """`seerr` is last-initialized; clearing that one must not leave it dangling."""

    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        client_a = await manager.initialize_seerr("http://overseerr", "key-a", 1)
        await manager.initialize_seerr("http://jellyseerr", "key-b", 2)

        assert manager.seerr is manager.get_seerr(2)
        await manager.clear_seerr(2)

        assert manager.seerr is client_a

    asyncio.run(run())


def test_clearing_without_a_config_id_clears_every_instance(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        await manager.initialize_seerr("http://overseerr", "key-a", 1)
        await manager.initialize_seerr("http://jellyseerr", "key-b", 2)

        await manager.clear_seerr()

        assert manager.seerr_clients() == {}
        assert manager.seerr is None
        assert manager.has_seerr is False

    asyncio.run(run())


def test_status_and_return_service_see_every_instance(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)
        assert (await manager.get_status())["seerr"] is False

        await manager.initialize_seerr("http://overseerr", "key-a", 1)

        assert (await manager.get_status())["seerr"] is True
        assert await manager.return_service(Service.SEERR) is manager.get_seerr(1)

    asyncio.run(run())


def test_a_failed_health_check_registers_no_client(monkeypatch):
    async def run() -> None:
        manager = _isolated_manager(monkeypatch)

        class _Unhealthy(_FakeSeerrClient):
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)
                self.health = AsyncMock(return_value=False)

        monkeypatch.setattr("backend.core.service_manager.SeerrClient", _Unhealthy)

        assert await manager.initialize_seerr("http://overseerr", "key-a", 1) is None
        assert manager.seerr_clients() == {}
        assert manager.has_seerr is False

    asyncio.run(run())


def test_unscoped_mapping_matches_by_name_not_by_borrowed_user_id() -> None:
    """A requester mapping left on "any instance" must match by username.

    A Seerr user id only identifies a person inside the Seerr that issued it.
    Spreading one across instances would map a different person on each -- the
    confusion the qualified requester format exists to prevent -- so an unscoped
    mapping matches the name, and falls back to the bare id only when it carries
    no name at all (the shape a single-Seerr install wrote before instances were
    recorded).
    """
    from backend.tasks.cleanup import _extract_requester_mapping_identity

    # user 3 on Seerr 1, named "alice"
    requester_key = "1:3"
    identities = {"alice"}

    # unscoped + named: matches the name on any instance ...
    assert _extract_requester_mapping_identity(
        requester_key, identities, {"seerr_username": "alice", "seerr_user_id": 3}
    )
    # ... and does not follow the id onto a requester who is someone else
    assert not _extract_requester_mapping_identity(
        "2:3", {"bob"}, {"seerr_username": "alice", "seerr_user_id": 3}
    )

    # unscoped with no name at all still matches by bare id (legacy shape)
    assert _extract_requester_mapping_identity(
        requester_key, set(), {"seerr_user_id": 3}
    )

    # scoped: the id is authoritative inside the instance it names
    assert _extract_requester_mapping_identity(
        requester_key,
        identities,
        {"seerr_service_config_id": 1, "seerr_user_id": 3, "seerr_username": "bob"},
    )
    assert not _extract_requester_mapping_identity(
        "2:3",
        identities,
        {"seerr_service_config_id": 1, "seerr_user_id": 3},
    )
