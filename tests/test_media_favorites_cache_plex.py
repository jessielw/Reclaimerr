from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.services.media_favorites_cache as favorites_cache_module
from backend.core.encryption import fer_encrypt
from backend.database import Base
from backend.database.models import (
    MediaFavorite,
    MediaUserIdentity,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service, UserRole


def _new_user(username: str) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=UserRole.USER,
        permissions=[],
    )


def _new_plex_service(name: str = "plex-main") -> ServiceConfig:
    return ServiceConfig(
        service_type=Service.PLEX,
        name=name,
        base_url="http://plex.local",
        api_key="server-token",
        enabled=True,
    )


def test_refresh_snapshot_imports_plex_watchlist_for_linked_users(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        monkeypatch.setattr(favorites_cache_module, "async_db", session_maker)

        async with session_maker() as db_session:
            user = _new_user("alice")
            plex = _new_plex_service()
            db_session.add_all([user, plex])
            await db_session.commit()
            await db_session.refresh(user)
            await db_session.refresh(plex)

            db_session.add(
                MediaUserIdentity(
                    source_service=Service.PLEX,
                    source_service_config_id=plex.id,
                    source_user_id="plex-user-1",
                    username="alice",
                    username_normalized="alice",
                    user_id=user.id,
                    email=None,
                    display_name="Alice",
                    raw={"source": "plex"},
                    plex_auth_token=fer_encrypt("plex-token-alice"),
                    last_seen_at=datetime.now(UTC),
                )
            )
            await db_session.commit()

            async def fake_fetch(
                self: favorites_cache_module.MediaFavoritesSnapshotCache,
                plex_user_token: str,
            ) -> dict[MediaType, set[int]]:
                assert plex_user_token == "plex-token-alice"
                return {
                    MediaType.MOVIE: {101, 102},
                    MediaType.SERIES: {301},
                }

            monkeypatch.setattr(
                favorites_cache_module.MediaFavoritesSnapshotCache,
                "_fetch_plex_watchlist_tmdb_ids",
                fake_fetch,
            )

            cache = favorites_cache_module.MediaFavoritesSnapshotCache()
            ok, error = await cache.refresh_snapshot(all_servers=[plex])
            assert ok is True
            assert error is None

            rows = (
                (
                    await db_session.execute(
                        select(MediaFavorite).order_by(
                            MediaFavorite.media_type.asc(), MediaFavorite.tmdb_id.asc()
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [(row.media_type, row.tmdb_id) for row in rows] == [
                (MediaType.MOVIE, 101),
                (MediaType.MOVIE, 102),
                (MediaType.SERIES, 301),
            ]
            assert all(row.source_service is Service.PLEX for row in rows)
            assert all(row.username == "alice" for row in rows)

        await engine.dispose()

    asyncio.run(run())


def test_refresh_snapshot_keeps_stale_plex_rows_for_failed_users(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        monkeypatch.setattr(favorites_cache_module, "async_db", session_maker)

        async with session_maker() as db_session:
            alice = _new_user("alice")
            bob = _new_user("bob")
            plex = _new_plex_service()
            db_session.add_all([alice, bob, plex])
            await db_session.commit()
            await db_session.refresh(alice)
            await db_session.refresh(bob)
            await db_session.refresh(plex)

            db_session.add_all(
                [
                    MediaUserIdentity(
                        source_service=Service.PLEX,
                        source_service_config_id=plex.id,
                        source_user_id="plex-user-alice",
                        username="alice",
                        username_normalized="alice",
                        user_id=alice.id,
                        email=None,
                        display_name="Alice",
                        raw={"source": "plex"},
                        plex_auth_token=fer_encrypt("plex-token-alice"),
                        last_seen_at=datetime.now(UTC),
                    ),
                    MediaUserIdentity(
                        source_service=Service.PLEX,
                        source_service_config_id=plex.id,
                        source_user_id="plex-user-bob",
                        username="bob",
                        username_normalized="bob",
                        user_id=bob.id,
                        email=None,
                        display_name="Bob",
                        raw={"source": "plex"},
                        plex_auth_token=fer_encrypt("plex-token-bob"),
                        last_seen_at=datetime.now(UTC),
                    ),
                    MediaFavorite(
                        media_type=MediaType.MOVIE,
                        tmdb_id=9999,
                        username="bob",
                        username_normalized="bob",
                        source_service=Service.PLEX,
                        source_service_config_id=plex.id,
                    ),
                ]
            )
            await db_session.commit()

            async def fake_fetch(
                self: favorites_cache_module.MediaFavoritesSnapshotCache,
                plex_user_token: str,
            ) -> dict[MediaType, set[int]]:
                if plex_user_token == "plex-token-alice":
                    return {MediaType.MOVIE: {111}, MediaType.SERIES: set()}
                raise RuntimeError("watchlist unavailable")

            monkeypatch.setattr(
                favorites_cache_module.MediaFavoritesSnapshotCache,
                "_fetch_plex_watchlist_tmdb_ids",
                fake_fetch,
            )

            cache = favorites_cache_module.MediaFavoritesSnapshotCache()
            ok, error = await cache.refresh_snapshot(all_servers=[plex])
            assert ok is True
            assert error is None

            rows = (
                (
                    await db_session.execute(
                        select(MediaFavorite).order_by(
                            MediaFavorite.username_normalized.asc(),
                            MediaFavorite.tmdb_id.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [(row.username, row.tmdb_id) for row in rows] == [
                ("alice", 111),
                ("bob", 9999),
            ]

        await engine.dispose()

    asyncio.run(run())


def test_request_plex_watchlist_page_retries_smaller_container_sizes() -> None:
    class _FakeResponse:
        def __init__(
            self,
            *,
            status_code: int,
            text: str = "",
            payload: dict | None = None,
        ) -> None:
            self.status_code = status_code
            self.text = text
            self._payload = payload
            self.content = b"{}" if payload is not None else b""

        def json(self) -> dict:
            return self._payload or {}

    class _FakeHttp:
        def __init__(self) -> None:
            self.requested_sizes: list[int] = []

        async def get(
            self, _endpoint: str, *, headers: dict, params: dict, **_: object
        ) -> _FakeResponse:
            page_size = int(headers["X-Plex-Container-Size"])
            self.requested_sizes.append(page_size)
            assert params["includeCollections"] == "1"
            assert params["includeExternalMedia"] == "1"
            if page_size > 10:
                return _FakeResponse(
                    status_code=400,
                    text='{"errors":[{"message":"Invalid value provided for x-plex-container-size!"}]}',
                )
            return _FakeResponse(
                status_code=200,
                payload={
                    "MediaContainer": {
                        "Metadata": [],
                        "size": 0,
                        "totalSize": 0,
                    }
                },
            )

    async def run() -> None:
        cache = favorites_cache_module.MediaFavoritesSnapshotCache()
        fake_http = _FakeHttp()
        payload, used_size = await cache._request_plex_watchlist_page(
            http=fake_http,  # type: ignore[arg-type]
            endpoint="https://discover.provider.plex.tv/library/sections/watchlist/all",
            headers={},
            start=0,
        )
        assert isinstance(payload, dict)
        assert used_size == 10
        assert fake_http.requested_sizes == [20, 10]

    asyncio.run(run())


def test_build_plex_discover_headers_matches_plexapi_shape() -> None:
    headers = (
        favorites_cache_module.MediaFavoritesSnapshotCache._build_plex_discover_headers(
            "user-token"
        )
    )
    assert headers["X-Plex-Token"] == "user-token"
    assert headers["X-Plex-Provides"] == "controller"
    assert headers["X-Plex-Features"] == "external-media"
    assert headers["X-Plex-Sync-Version"] == "2"
