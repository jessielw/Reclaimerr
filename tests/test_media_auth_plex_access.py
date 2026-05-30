from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from niquests.exceptions import HTTPError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import ServiceConfig
from backend.enums import Service
from backend.services.media_auth import (
    PLEX_TV_USER_URL,
    PLEX_TV_USERS_URL,
    MediaAuthAccessDeniedError,
    MediaAuthProvider,
    MediaAuthProviderError,
    authenticate_plex_token,
)


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        if json_data is not None:
            self.content = b"json"
        elif text:
            self.content = text.encode("utf-8")
        else:
            self.content = b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"{self.status_code} error")

    def json(self) -> dict:
        if self._json_data is None:
            raise ValueError("No JSON payload")
        return self._json_data


class _FakeSession:
    def __init__(self, responder: Callable[..., _FakeResponse]) -> None:
        self._responder = responder

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, **kwargs) -> _FakeResponse:
        return self._responder(url, **kwargs)


def _new_plex_provider(service_config_id: int) -> MediaAuthProvider:
    return MediaAuthProvider(
        service_config_id=service_config_id,
        service_type=Service.PLEX,
        name="plex-main",
        base_url="https://plex.example.com",
        auth_mode="redirect",
    )


def _new_service_config() -> ServiceConfig:
    return ServiceConfig(
        service_type=Service.PLEX,
        name="plex-main",
        base_url="https://plex.example.com",
        api_key="owner-token",
        enabled=True,
    )


def test_authenticate_plex_token_allows_shared_user_with_server_access(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        def responder(url: str, **kwargs) -> _FakeResponse:
            token = (kwargs.get("headers") or {}).get("X-Plex-Token")
            if url == PLEX_TV_USER_URL and token == "user-token":
                return _FakeResponse(
                    json_data={
                        "id": "200",
                        "username": "friend",
                        "title": "Friend",
                        "email": "friend@example.com",
                    }
                )
            if url == PLEX_TV_USER_URL and token == "owner-token":
                return _FakeResponse(
                    json_data={
                        "id": "100",
                        "username": "owner",
                        "title": "Owner",
                        "email": "owner@example.com",
                    }
                )
            if url == "https://plex.example.com/identity":
                return _FakeResponse(
                    json_data={"MediaContainer": {"machineIdentifier": "abc123"}}
                )
            if url == PLEX_TV_USERS_URL:
                return _FakeResponse(
                    text=(
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        "<MediaContainer>"
                        '<User id="200" email="friend@example.com">'
                        '<Server machineIdentifier="abc123"/>'
                        "</User>"
                        "</MediaContainer>"
                    )
                )
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            "backend.services.media_auth.niquests.AsyncSession",
            lambda: _FakeSession(responder),
        )

        async with session_maker() as db:
            config = _new_service_config()
            db.add(config)
            await db.commit()
            await db.refresh(config)
            provider = _new_plex_provider(config.id)

            identity = await authenticate_plex_token(
                db, provider=provider, plex_user_token="user-token"
            )
            assert identity.source_user_id == "200"
            assert identity.raw["server_access_verified"] is True
            assert identity.raw["access_check_reason"] == "ok"
            assert identity.raw["target_machine_id"] == "abc123"

        await engine.dispose()

    asyncio.run(run())


def test_authenticate_plex_token_allows_owner_without_shared_users_lookup(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        users_lookup_called = False

        def responder(url: str, **kwargs) -> _FakeResponse:
            nonlocal users_lookup_called
            token = (kwargs.get("headers") or {}).get("X-Plex-Token")
            if url == PLEX_TV_USER_URL and token == "owner-token":
                return _FakeResponse(
                    json_data={
                        "id": "100",
                        "username": "owner",
                        "title": "Owner",
                        "email": "owner@example.com",
                    }
                )
            if url == "https://plex.example.com/identity":
                return _FakeResponse(
                    json_data={"MediaContainer": {"machineIdentifier": "abc123"}}
                )
            if url == PLEX_TV_USERS_URL:
                users_lookup_called = True
                raise AssertionError("Shared users lookup should be skipped for owner")
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            "backend.services.media_auth.niquests.AsyncSession",
            lambda: _FakeSession(responder),
        )

        async with session_maker() as db:
            config = _new_service_config()
            db.add(config)
            await db.commit()
            await db.refresh(config)
            provider = _new_plex_provider(config.id)

            identity = await authenticate_plex_token(
                db, provider=provider, plex_user_token="owner-token"
            )
            assert identity.source_user_id == "100"
            assert identity.raw["access_check_reason"] == "owner"
            assert users_lookup_called is False

        await engine.dispose()

    asyncio.run(run())


def test_authenticate_plex_token_denies_user_without_target_server_share(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        def responder(url: str, **kwargs) -> _FakeResponse:
            token = (kwargs.get("headers") or {}).get("X-Plex-Token")
            if url == PLEX_TV_USER_URL and token == "user-token":
                return _FakeResponse(
                    json_data={
                        "id": "200",
                        "username": "friend",
                        "title": "Friend",
                        "email": "friend@example.com",
                    }
                )
            if url == PLEX_TV_USER_URL and token == "owner-token":
                return _FakeResponse(
                    json_data={
                        "id": "100",
                        "username": "owner",
                        "title": "Owner",
                        "email": "owner@example.com",
                    }
                )
            if url == "https://plex.example.com/identity":
                return _FakeResponse(
                    json_data={"MediaContainer": {"machineIdentifier": "abc123"}}
                )
            if url == PLEX_TV_USERS_URL:
                return _FakeResponse(
                    text=(
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        "<MediaContainer>"
                        '<User id="200" email="friend@example.com">'
                        '<Server machineIdentifier="different-server"/>'
                        "</User>"
                        "</MediaContainer>"
                    )
                )
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            "backend.services.media_auth.niquests.AsyncSession",
            lambda: _FakeSession(responder),
        )

        async with session_maker() as db:
            config = _new_service_config()
            db.add(config)
            await db.commit()
            await db.refresh(config)
            provider = _new_plex_provider(config.id)

            with pytest.raises(MediaAuthAccessDeniedError):
                await authenticate_plex_token(
                    db, provider=provider, plex_user_token="user-token"
                )

        await engine.dispose()

    asyncio.run(run())


def test_authenticate_plex_token_fails_closed_when_users_lookup_fails(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        def responder(url: str, **kwargs) -> _FakeResponse:
            token = (kwargs.get("headers") or {}).get("X-Plex-Token")
            if url == PLEX_TV_USER_URL and token == "user-token":
                return _FakeResponse(
                    json_data={
                        "id": "200",
                        "username": "friend",
                        "title": "Friend",
                        "email": "friend@example.com",
                    }
                )
            if url == PLEX_TV_USER_URL and token == "owner-token":
                return _FakeResponse(
                    json_data={
                        "id": "100",
                        "username": "owner",
                        "title": "Owner",
                        "email": "owner@example.com",
                    }
                )
            if url == "https://plex.example.com/identity":
                return _FakeResponse(
                    json_data={"MediaContainer": {"machineIdentifier": "abc123"}}
                )
            if url == PLEX_TV_USERS_URL:
                return _FakeResponse(status_code=503, text="unavailable")
            raise AssertionError(f"Unexpected URL: {url}")

        monkeypatch.setattr(
            "backend.services.media_auth.niquests.AsyncSession",
            lambda: _FakeSession(responder),
        )

        async with session_maker() as db:
            config = _new_service_config()
            db.add(config)
            await db.commit()
            await db.refresh(config)
            provider = _new_plex_provider(config.id)

            with pytest.raises(MediaAuthProviderError, match="shared users lookup"):
                await authenticate_plex_token(
                    db, provider=provider, plex_user_token="user-token"
                )

        await engine.dispose()

    asyncio.run(run())
