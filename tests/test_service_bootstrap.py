from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core import service_bootstrap
from backend.enums import Service


def test_initialize_with_retry_retries_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    service = object()
    sleep_delays: list[float] = []

    async def initializer() -> object | None:
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            return service
        return None

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(service_bootstrap, "BOOTSTRAP_SERVICE_INIT_ATTEMPTS", 4)
    monkeypatch.setattr(
        service_bootstrap, "BOOTSTRAP_SERVICE_INIT_BACKOFF_SECONDS", (1.0, 3.0, 5.0)
    )
    monkeypatch.setattr(service_bootstrap.asyncio, "sleep", fake_sleep)

    result = asyncio.run(service_bootstrap._initialize_with_retry("Plex", initializer))

    assert result is service
    assert attempts == 3
    assert sleep_delays == [1.0, 3.0]


def test_initialize_with_retry_stops_after_attempt_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    sleep_delays: list[float] = []

    async def initializer() -> None:
        nonlocal attempts
        attempts += 1
        return None

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr(service_bootstrap, "BOOTSTRAP_SERVICE_INIT_ATTEMPTS", 3)
    monkeypatch.setattr(
        service_bootstrap, "BOOTSTRAP_SERVICE_INIT_BACKOFF_SECONDS", (1.0, 3.0)
    )
    monkeypatch.setattr(service_bootstrap.asyncio, "sleep", fake_sleep)

    result = asyncio.run(service_bootstrap._initialize_with_retry("Plex", initializer))

    assert result is None
    assert attempts == 3
    assert sleep_delays == [1.0, 3.0]


def test_load_enabled_services_overlaps_types_but_orders_same_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different service types initialize concurrently; same-type stays ordered.

    Concurrency is what stops N unreachable services from each charging their
    retry backoff to every boot and every task-child spawn. It has to stop at
    the type boundary: initialize_* assigns a last-one-wins singleton (_plex,
    _radarr, ...) that return_service still reads, so two configs of one type
    must keep resolving in database order.
    """
    started: list[str] = []
    finished: list[str] = []
    release: dict[str, asyncio.Event] = {}

    class FakeConfig:
        def __init__(self, service_type, config_id: int) -> None:
            self.service_type = service_type
            self.id = config_id
            self.base_url = f"http://service-{config_id}"
            self.api_key = "encrypted"
            self.is_main = False
            self.extra_settings = {}

    configs = [
        FakeConfig(Service.PLEX, 1),
        FakeConfig(Service.RADARR, 2),
        FakeConfig(Service.PLEX, 3),
    ]

    class FakeServiceManager:
        async def clear_all(self) -> None:
            return None

        async def _run(self, label: str) -> object:
            started.append(label)
            await release[label].wait()
            finished.append(label)
            return object()

        async def initialize_plex(self, base_url, api_key, is_main, config_id):
            return await self._run(f"plex-{config_id}")

        async def initialize_radarr(self, base_url, api_key, timeout, config_id):
            return await self._run(f"radarr-{config_id}")

    for label in ("plex-1", "radarr-2", "plex-3"):
        release[label] = asyncio.Event()

    class FakeSession:
        async def execute(self, _statement):
            class Result:
                @staticmethod
                def scalars():
                    return SimpleNamespace(all=lambda: configs)

            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(service_bootstrap, "async_db", FakeSession)
    monkeypatch.setattr(service_bootstrap, "service_manager", FakeServiceManager())
    monkeypatch.setattr(service_bootstrap, "fer_decrypt", lambda value: "key")

    async def scenario() -> None:
        task = asyncio.create_task(service_bootstrap.load_enabled_services())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The Plex and Radarr configs are in flight together, and the second
        # Plex config has not started while the first is still running.
        assert sorted(started) == ["plex-1", "radarr-2"]

        release["plex-1"].set()
        release["radarr-2"].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert "plex-3" in started

        release["plex-3"].set()
        await task

    asyncio.run(scenario())

    assert [label for label in finished if label.startswith("plex")] == [
        "plex-1",
        "plex-3",
    ]
