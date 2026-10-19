from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from niquests.exceptions import HTTPError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import GeneralSettings, ServiceConfig
from backend.enums import Service
from backend.models.live_playback import LiveSession, PlaybackDeferred, PlaybackSnapshot
from backend.services.live_sessions import native_item_paths, resolve_session_paths
from backend.services.playback_guard import (
    PlaybackGuard,
    PlaybackOperation,
    PlaybackTarget,
    current_playback_operation,
    guard_arr_removal,
    guard_native_removal,
)


@pytest.mark.parametrize("service", [Service.EMBY, Service.JELLYFIN])
def test_emby_family_paths_use_api_key_compatible_item_query(service):
    async def run():
        client = SimpleNamespace(
            _make_request=AsyncMock(
                return_value={
                    "Items": [
                        {
                            "Id": "7",
                            "Type": "Movie",
                            "Path": "/wrong-version.mkv",
                            "MediaSources": [
                                {"Id": "9", "Path": "/selected.mkv"},
                                {"Id": "10", "Path": "/other.mkv"},
                            ],
                        }
                    ]
                }
            )
        )
        assert await native_item_paths(client, service, "7", "9") == [
            ("/selected.mkv", False)
        ]
        client._make_request.assert_awaited_once_with(
            "Items",
            params={
                "Ids": "7",
                "Fields": "Path,MediaSources,DisplayOrder"
                if service is Service.EMBY
                else "Path,MediaSources",
                "EnableUserData": False,
            },
            timeout=10,
        )
        assert await native_item_paths(client, service, "7", "absent-version") == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"Items": []},
        {"Items": [None]},
        {"Items": [{"Id": "other", "Path": "/wrong.mkv"}]},
        {"Items": [{"Id": "7"}, {"Id": "7"}]},
    ],
)
@pytest.mark.parametrize("service", [Service.EMBY, Service.JELLYFIN])
def test_emby_family_rejects_invalid_or_mismatched_item_lookup(payload, service):
    async def run():
        client = SimpleNamespace(_make_request=AsyncMock(return_value=payload))
        with pytest.raises(ValueError):
            await native_item_paths(client, service, "7")

    asyncio.run(run())


@pytest.mark.parametrize("route", ["native", "arr", "arr-file"])
@pytest.mark.parametrize("failure", ["request", "missing-path"])
@pytest.mark.parametrize("defer", [True, False])
def test_final_path_lookup_obeys_unavailable_policy(monkeypatch, route, failure, defer):
    async def run():
        target = PlaybackTarget(relevant_configs={1})
        guard = PlaybackGuard(True, [], {1: Service.PLEX}, defer_when_unavailable=defer)
        monkeypatch.setattr(
            guard,
            "_snapshot",
            AsyncMock(return_value=PlaybackSnapshot(available=False)),
        )
        client = SimpleNamespace(
            _make_request=AsyncMock(return_value=(200, {})),
            get_movie=AsyncMock(return_value=SimpleNamespace(path=None)),
        )
        if route == "native":
            monkeypatch.setattr(
                "backend.services.live_sessions.native_item_paths",
                AsyncMock(
                    side_effect=HTTPError("private token=secret")
                    if failure == "request"
                    else None,
                    return_value=[],
                ),
            )
        elif failure == "request":
            client._make_request.side_effect = HTTPError("private token=secret")
            client.get_movie.side_effect = HTTPError("private token=secret")
        token = current_playback_operation.set(PlaybackOperation(guard, target))
        try:

            async def check():
                if route == "native":
                    await guard_native_removal(client, Service.PLEX, "7")
                else:
                    await guard_arr_removal(
                        client, Service.RADARR, [55], files=route == "arr-file"
                    )

            if defer:
                with pytest.raises(PlaybackDeferred, match="unavailable"):
                    await check()
            else:
                await check()
        finally:
            current_playback_operation.reset(token)

    asyncio.run(run())


@pytest.mark.parametrize("route", ["native", "arr"])
def test_failed_path_lookup_still_protects_known_playback(monkeypatch, route):
    async def run():
        guard = PlaybackGuard(True, [], {1: Service.PLEX}, defer_when_unavailable=False)
        monkeypatch.setattr(
            guard,
            "_snapshot",
            AsyncMock(return_value=PlaybackSnapshot((LiveSession("7"),))),
        )
        client = SimpleNamespace(
            get_movie=AsyncMock(side_effect=ValueError("unavailable"))
        )
        monkeypatch.setattr(service_manager, "_plex_clients", {1: client})
        monkeypatch.setattr(
            "backend.services.live_sessions.native_item_paths",
            AsyncMock(side_effect=ValueError("unavailable")),
        )
        target = (
            PlaybackTarget()
            if route == "native"
            else PlaybackTarget(identities={(1, "7", None)})
        )
        token = current_playback_operation.set(PlaybackOperation(guard, target))
        try:
            with pytest.raises(PlaybackDeferred, match="currently playing"):
                if route == "native":
                    await guard_native_removal(client, Service.PLEX, "7")
                else:
                    await guard_arr_removal(client, Service.RADARR, [55])
        finally:
            current_playback_operation.reset(token)

    asyncio.run(run())


def test_disabled_server_does_not_block_shared_path(monkeypatch):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                main = ServiceConfig(
                    service_type=Service.PLEX,
                    base_url="http://fake",
                    api_key="fake",
                    enabled=True,
                    is_main=True,
                )
                disabled = ServiceConfig(
                    service_type=Service.JELLYFIN,
                    base_url="http://disabled",
                    api_key="fake",
                    enabled=False,
                )
                db.add_all([main, disabled])
                await db.flush()
                db.add(
                    GeneralSettings(
                        path_mappings=[
                            {
                                "service_type": "jellyfin",
                                "service_config_id": disabled.id,
                                "remote_prefix": "/remote",
                                "local_prefix": "/shared",
                            }
                        ]
                    )
                )
                await db.commit()
                guard = await PlaybackGuard.load(db)
                assert guard.configs == {main.id: Service.PLEX}
            client = SimpleNamespace(
                get_live_sessions=AsyncMock(return_value=PlaybackSnapshot())
            )
            monkeypatch.setattr(
                type(service_manager),
                "get_media_server",
                lambda self, service, cid: client if cid == main.id else None,
            )
            await guard.check(
                PlaybackTarget(
                    paths=[("/shared/movie.mkv", None, None, False)],
                    relevant_configs={main.id, disabled.id},
                )
            )
            client.get_live_sessions.assert_awaited_once()
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["http", "timeout", "invalid", "missing-client"])
def test_session_failure_logs_sanitized_diagnosis(monkeypatch, caplog, failure):
    async def run():
        error = (
            HTTPError(
                "https://private/?token=super-secret",
                response=SimpleNamespace(status_code=401),
            )
            if failure == "http"
            else TimeoutError("super-secret")
        )
        client = SimpleNamespace(
            get_live_sessions=AsyncMock(
                side_effect=error if failure in {"http", "timeout"} else None,
                return_value={"secret": "super-secret"},
            )
        )
        monkeypatch.setattr(
            type(service_manager),
            "get_media_server",
            lambda *args: None if failure == "missing-client" else client,
        )
        guard = PlaybackGuard(True, [], {1: Service.PLEX})
        with caplog.at_level(logging.WARNING, logger="reclaimerr"):
            assert not (await guard._snapshot(1)).available
            assert not (await guard._snapshot(1)).available
        messages = [
            r.message
            for r in caplog.records
            if "Playback check unavailable" in r.message
        ]
        assert len(messages) == 1
        assert "plex config 1" in messages[0]
        assert "super-secret" not in messages[0]
        assert "https://private" not in messages[0]
        if failure == "http":
            assert "HTTP 401" in messages[0]
        elif failure == "timeout":
            assert "TimeoutError" in messages[0]
        elif failure == "missing-client":
            assert "not initialized" in messages[0]
        else:
            assert "invalid session data" in messages[0]

    asyncio.run(run())


@pytest.mark.parametrize("defer", [True, False])
@pytest.mark.parametrize("target_id", ["known", "unresolved"])
def test_failed_session_enrichment_preserves_known_playback(
    monkeypatch, defer, target_id
):
    async def run():
        error = HTTPError(
            "https://private/?token=super-secret",
            response=SimpleNamespace(status_code=400),
        )
        client = SimpleNamespace(_make_request=AsyncMock(side_effect=error))
        snapshot = await resolve_session_paths(
            client,
            Service.JELLYFIN,
            PlaybackSnapshot(
                (
                    LiveSession("known", paths=("/playing.mkv",)),
                    LiveSession("unresolved"),
                )
            ),
        )
        assert not snapshot.available
        assert len(snapshot.sessions) == 2
        assert snapshot.sessions[0].paths == ("/playing.mkv",)
        assert "HTTP 400" in snapshot.unavailable_reason
        assert "super-secret" not in snapshot.unavailable_reason
        guard = PlaybackGuard(
            True, [], {1: Service.JELLYFIN}, defer_when_unavailable=defer
        )
        monkeypatch.setattr(guard, "_snapshot", AsyncMock(return_value=snapshot))
        with pytest.raises(PlaybackDeferred, match="currently playing"):
            await guard.check(PlaybackTarget(identities={(1, target_id, None)}))

    asyncio.run(run())


def test_arr_scope_continues_after_unknown_path_when_unavailable_is_allowed(
    monkeypatch,
):
    async def run():
        client = SimpleNamespace(
            get_movie=AsyncMock(
                side_effect=[
                    ValueError("unavailable"),
                    SimpleNamespace(path="/playing"),
                ]
            )
        )
        guard = PlaybackGuard(True, [], {1: Service.PLEX}, defer_when_unavailable=False)
        monkeypatch.setattr(
            guard,
            "_snapshot",
            AsyncMock(
                return_value=PlaybackSnapshot(
                    (LiveSession("other", paths=("/playing/movie.mkv",)),)
                )
            ),
        )
        token = current_playback_operation.set(
            PlaybackOperation(guard, PlaybackTarget())
        )
        try:
            with pytest.raises(PlaybackDeferred, match="currently playing"):
                await guard_arr_removal(client, Service.RADARR, [1, 2])
            assert client.get_movie.await_count == 2
        finally:
            current_playback_operation.reset(token)

    asyncio.run(run())


def test_late_missing_path_deferral_keeps_completed_steps(monkeypatch):
    async def run():
        client = SimpleNamespace(
            get_movie=AsyncMock(return_value=SimpleNamespace(path=None))
        )
        guard = PlaybackGuard(True, [], {1: Service.PLEX})
        token = current_playback_operation.set(
            PlaybackOperation(
                guard,
                PlaybackTarget(relevant_configs={1}),
                completed_steps=["Removed first file"],
            )
        )
        try:
            with pytest.raises(PlaybackDeferred) as error:
                await guard_arr_removal(client, Service.RADARR, [55])
            assert error.value.detail.completed_steps == ("Removed first file",)
        finally:
            current_playback_operation.reset(token)

    asyncio.run(run())
