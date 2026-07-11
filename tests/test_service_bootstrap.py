from __future__ import annotations

import asyncio

import pytest

from backend.core import service_bootstrap


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
