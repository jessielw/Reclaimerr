from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

import backend.api.routes.auth as auth_routes
from backend.api.routes.settings.oidc import update_oidc_settings
from backend.core.encryption import fer_encrypt
from backend.core.oidc import OIDCProviderMetadata, extract_userinfo
from backend.database import Base
from backend.database.models import (
    GeneralSettings,
    OIDCSettings,
    ServiceConfig,
    User,
    UserSession,
)
from backend.enums import Service, UserRole
from backend.models.settings import OIDCSettingsUpdate
from backend.services.media_auth import (
    DiscoveredMediaUser,
    PlexPendingAuth,
)


def _make_callback_request() -> Request:
    headers = [
        (b"host", b"testserver"),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/oidc/callback",
        "headers": headers,
        "query_string": b"code=test-code&state=expected-state",
        "client": ("127.0.0.1", 4242),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def _make_plex_callback_request(state: str = "expected-state") -> Request:
    headers = [
        (b"host", b"testserver"),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/media/plex/callback",
        "headers": headers,
        "query_string": f"state={state}".encode(),
        "client": ("127.0.0.1", 4242),
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope)


def _make_oidc_start_request(*, scheme: str = "http") -> Request:
    headers = [
        (b"host", b"testserver"),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/oidc/start",
        "headers": headers,
        "query_string": b"",
        "client": ("127.0.0.1", 4242),
        "scheme": scheme,
        "server": ("testserver", 443 if scheme == "https" else 80),
        "router": auth_routes.router,
    }
    return Request(scope)


def _make_plex_start_request(*, scheme: str = "http") -> Request:
    headers = [
        (b"host", b"testserver"),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/media/plex/start",
        "headers": headers,
        "query_string": b"service_config_id=1",
        "client": ("127.0.0.1", 4242),
        "scheme": scheme,
        "server": ("testserver", 443 if scheme == "https" else 80),
        "router": auth_routes.router,
    }
    return Request(scope)


def _new_user(*, username: str, email: str | None, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="hashed",
        email=email,
        role=role,
        permissions=[],
    )


def test_update_oidc_settings_requires_secret_when_enabling() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            admin = _new_user(
                username="admin",
                email="admin@example.com",
                role=UserRole.ADMIN,
            )
            db_session.add(admin)
            await db_session.commit()
            await db_session.refresh(admin)

            with pytest.raises(HTTPException) as exc:
                await update_oidc_settings(
                    request=OIDCSettingsUpdate(
                        enabled=True,
                        issuer_url="https://auth.example.com",
                        client_id="reclaimerr",
                        client_secret=None,
                        scopes="openid email profile",
                        email_claim="email",
                    ),
                    admin=admin,
                    db=db_session,
                )
            assert exc.value.status_code == 400
            assert "client secret" in str(exc.value.detail).lower()

        await engine.dispose()

    asyncio.run(run())


def test_update_oidc_settings_persists_token_endpoint_auth_method() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            admin = _new_user(
                username="admin",
                email="admin@example.com",
                role=UserRole.ADMIN,
            )
            db_session.add(admin)
            await db_session.commit()
            await db_session.refresh(admin)

            response = await update_oidc_settings(
                request=OIDCSettingsUpdate(
                    enabled=True,
                    issuer_url="https://auth.example.com",
                    client_id="reclaimerr",
                    client_secret="secret",
                    scopes="openid email profile",
                    email_claim="email",
                    token_endpoint_auth_method="client_secret_post",
                ),
                admin=admin,
                db=db_session,
            )

            assert response.token_endpoint_auth_method == "client_secret_post"
            settings_row = (await db_session.execute(select(OIDCSettings))).scalar_one()
            assert settings_row.token_endpoint_auth_method == "client_secret_post"

        await engine.dispose()

    asyncio.run(run())


def test_oidc_callback_redirect_uri_uses_override() -> None:
    request = _make_oidc_start_request(scheme="https")
    settings_row = OIDCSettings(
        redirect_uri_override="https://login.example.com/callback",
    )

    callback_uri = auth_routes._oidc_callback_redirect_uri(request, settings_row)  # pyright: ignore[reportPrivateUsage]

    assert callback_uri == "https://login.example.com/callback"


def test_oidc_callback_redirect_uri_uses_request_scheme() -> None:
    request = _make_oidc_start_request(scheme="https")
    settings_row = OIDCSettings(redirect_uri_override=None)

    callback_uri = auth_routes._oidc_callback_redirect_uri(request, settings_row)  # pyright: ignore[reportPrivateUsage]

    assert callback_uri == "https://testserver/api/auth/oidc/callback"


def test_oidc_callback_redirect_uri_prefers_application_url() -> None:
    request = _make_oidc_start_request(scheme="http")
    settings_row = OIDCSettings(redirect_uri_override=None)

    callback_uri = auth_routes._oidc_callback_redirect_uri(  # pyright: ignore[reportPrivateUsage]
        request,
        settings_row,
        application_url="https://public.example.com/",
    )

    assert callback_uri == "https://public.example.com/api/auth/oidc/callback"


def test_oidc_start_stores_sanitized_return_to(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            db_session.add(
                OIDCSettings(
                    enabled=True,
                    issuer_url="https://auth.example.com",
                    client_id="reclaimerr",
                    client_secret=fer_encrypt("secret"),
                    scopes="openid profile email",
                    email_claim="email",
                )
            )
            await db_session.commit()

            class FakeOIDCClient:
                async def authorize_redirect(
                    self,
                    request: Request,
                    callback_uri: str,
                ):
                    assert callback_uri == ("http://testserver/api/auth/oidc/callback")
                    return auth_routes.RedirectResponse("https://auth.example.com")

            monkeypatch.setattr(
                auth_routes,
                "_create_configured_oidc_client",
                lambda _settings_row: FakeOIDCClient(),
            )

            request = _make_oidc_start_request()
            request.scope["session"] = {}
            response = await auth_routes.oidc_start(
                request=request,
                return_to="/#/auth/complete",
                db=db_session,
            )

            assert response.status_code == 307
            assert request.scope["session"]["oidc_return_to"] == "/#/auth/complete"

        await engine.dispose()

    asyncio.run(run())


def test_oidc_start_uses_absolute_return_to_origin_for_callback(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            db_session.add(
                OIDCSettings(
                    enabled=True,
                    issuer_url="https://auth.example.com",
                    client_id="reclaimerr",
                    client_secret=fer_encrypt("secret"),
                    scopes="openid profile email",
                    email_claim="email",
                )
            )
            await db_session.commit()

            captured: dict[str, str] = {}

            class FakeOIDCClient:
                async def authorize_redirect(
                    self,
                    request: Request,
                    callback_uri: str,
                ):
                    captured["callback_uri"] = callback_uri
                    return auth_routes.RedirectResponse("https://auth.example.com")

            monkeypatch.setattr(
                auth_routes,
                "_create_configured_oidc_client",
                lambda _settings_row: FakeOIDCClient(),
            )

            request = _make_oidc_start_request()
            request.scope["session"] = {}
            response = await auth_routes.oidc_start(
                request=request,
                return_to="http://localhost:3000/#/auth/complete",
                db=db_session,
            )

            assert response.status_code == 307
            assert captured["callback_uri"] == (
                "http://localhost:3000/api/auth/oidc/callback"
            )
            assert request.scope["session"]["oidc_return_to"] == (
                "http://localhost:3000/#/auth/complete"
            )

        await engine.dispose()

    asyncio.run(run())


def test_oidc_callback_uses_session_return_to(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            user = _new_user(
                username="member",
                email="member@example.com",
                role=UserRole.USER,
            )
            db_session.add(user)
            db_session.add(
                OIDCSettings(
                    enabled=True,
                    issuer_url="https://auth.example.com",
                    client_id="reclaimerr",
                    client_secret=fer_encrypt("secret"),
                    scopes="openid profile email",
                    email_claim="email",
                    redirect_uri_override="http://testserver/api/auth/oidc/callback",
                )
            )
            await db_session.commit()
            await db_session.refresh(user)

            async def fake_load_provider_metadata(
                _client: object, **kwargs: object
            ) -> OIDCProviderMetadata:
                issuer_url = str(kwargs["issuer_url"])
                return OIDCProviderMetadata(
                    issuer=issuer_url,
                    authorization_endpoint=f"{issuer_url}/authorize",
                    token_endpoint=f"{issuer_url}/token",
                    jwks_uri=f"{issuer_url}/jwks",
                    userinfo_endpoint=f"{issuer_url}/userinfo",
                )

            class FakeOIDCClient:
                async def authorize_access_token(
                    self, request: Request
                ) -> dict[str, object]:
                    return {
                        "access_token": "access-token",
                        "userinfo": {
                            "sub": "oidc-subject-1",
                            "email": "member@example.com",
                        },
                    }

            monkeypatch.setattr(
                auth_routes,
                "create_oidc_client",
                lambda **_: FakeOIDCClient(),
            )
            monkeypatch.setattr(
                auth_routes,
                "load_provider_metadata",
                fake_load_provider_metadata,
            )

            request = _make_callback_request()
            request.scope["session"] = {"oidc_return_to": "/#/auth/complete"}
            response = await auth_routes.oidc_callback(
                request=request,
                code="test-code",
                state="expected-state",
                error=None,
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location") == "/#/auth/complete"
            assert "oidc_return_to" not in request.scope["session"]

        await engine.dispose()

    asyncio.run(run())


def test_post_auth_redirect_uses_application_url_default() -> None:
    redirect = auth_routes._resolve_post_auth_redirect(  # pyright: ignore[reportPrivateUsage]
        None,
        application_url="https://public.example.com",
    )

    assert redirect == "https://public.example.com/"


def test_post_auth_redirect_rejects_localhost_when_application_url_is_public() -> None:
    redirect = auth_routes._resolve_post_auth_redirect(  # pyright: ignore[reportPrivateUsage]
        "http://localhost:3000/",
        application_url="https://public.example.com",
    )

    assert redirect == "https://public.example.com/"


def test_post_auth_redirect_allows_localhost_without_application_url() -> None:
    redirect = auth_routes._resolve_post_auth_redirect(  # pyright: ignore[reportPrivateUsage]
        "http://localhost:3000/",
    )

    assert redirect == "http://localhost:3000/"


def test_post_auth_redirect_allows_relative_path() -> None:
    redirect = auth_routes._resolve_post_auth_redirect(  # pyright: ignore[reportPrivateUsage]
        "/",
        application_url="https://public.example.com",
    )

    assert redirect == "/"


def test_extract_userinfo_fetches_userinfo_when_email_missing() -> None:
    async def run() -> None:
        class FakeOIDCClient:
            async def userinfo(self, token: dict[str, object]) -> dict[str, str]:
                assert token["access_token"] == "access-token"
                return {"sub": "oidc-subject-1", "email": "member@example.com"}

        claims = await extract_userinfo(
            FakeOIDCClient(),
            {
                "access_token": "access-token",
                "userinfo": {"sub": "oidc-subject-1"},
            },
            required_claim="email",
        )

        assert claims == {
            "sub": "oidc-subject-1",
            "email": "member@example.com",
        }

    asyncio.run(run())


def test_oidc_callback_links_existing_user_by_email(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            user = _new_user(
                username="member",
                email="member@example.com",
                role=UserRole.USER,
            )
            db_session.add(user)
            db_session.add(
                OIDCSettings(
                    enabled=True,
                    issuer_url="https://auth.example.com",
                    client_id="reclaimerr",
                    client_secret=fer_encrypt("secret"),
                    scopes="openid profile email",
                    email_claim="email",
                    redirect_uri_override="http://testserver/api/auth/oidc/callback",
                )
            )
            await db_session.commit()
            await db_session.refresh(user)

            async def fake_load_provider_metadata(
                _client: object, **kwargs: object
            ) -> OIDCProviderMetadata:
                issuer_url = str(kwargs["issuer_url"])
                return OIDCProviderMetadata(
                    issuer=issuer_url,
                    authorization_endpoint=f"{issuer_url}/authorize",
                    token_endpoint=f"{issuer_url}/token",
                    jwks_uri=f"{issuer_url}/jwks",
                    userinfo_endpoint=f"{issuer_url}/userinfo",
                )

            class FakeOIDCClient:
                async def authorize_access_token(
                    self, request: Request
                ) -> dict[str, object]:
                    return {
                        "access_token": "access-token",
                        "userinfo": {
                            "sub": "oidc-subject-1",
                            "email": "member@example.com",
                        },
                    }

            monkeypatch.setattr(
                auth_routes,
                "create_oidc_client",
                lambda **_: FakeOIDCClient(),
            )
            monkeypatch.setattr(
                auth_routes,
                "load_provider_metadata",
                fake_load_provider_metadata,
            )

            request = _make_callback_request()
            response = await auth_routes.oidc_callback(
                request=request,
                code="test-code",
                state="expected-state",
                error=None,
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location") == "/"

            refreshed_user = (
                await db_session.execute(select(User).where(User.id == user.id))
            ).scalar_one()
            assert refreshed_user.oidc_issuer == "https://auth.example.com"
            assert refreshed_user.oidc_subject == "oidc-subject-1"

            sessions = (
                (
                    await db_session.execute(
                        select(UserSession).where(UserSession.user_id == user.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(sessions) == 1

        await engine.dispose()

    asyncio.run(run())


def test_media_plex_callback_redirects_to_localhost_without_application_url(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            user = _new_user(
                username="plexmember",
                email="plexmember@example.com",
                role=UserRole.USER,
            )
            service = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex.local",
                api_key="server-token",
                name="plex-main",
                enabled=True,
            )
            db_session.add_all([user, service])
            await db_session.commit()
            await db_session.refresh(user)
            await db_session.refresh(service)

            def fake_pop_pending_plex_auth(state: str) -> PlexPendingAuth:
                return PlexPendingAuth(
                    state=state,
                    service_config_id=service.id,
                    client_identifier="plex-client-id",
                    pin_id=123,
                    pin_code="abcd",
                    return_to="http://localhost:3000/",
                    created_at=datetime.now(UTC),
                )

            async def fake_exchange_plex_pin_for_token(
                pending: PlexPendingAuth,
            ) -> str:
                assert pending.service_config_id == service.id
                return "plex-user-token"

            async def fake_authenticate_plex_token(
                db: AsyncSession,
                *,
                provider: object,
                plex_user_token: str,
            ) -> DiscoveredMediaUser:
                assert db is db_session
                assert plex_user_token == "plex-user-token"
                return DiscoveredMediaUser(
                    source_service=Service.PLEX,
                    source_service_config_id=service.id,
                    source_service_name="plex-main",
                    source_user_id="plex-user-42",
                    username="plexmember",
                    username_normalized="plexmember",
                    email="plexmember@example.com",
                    display_name="Plex Member",
                    raw={"source": "plex"},
                )

            async def fake_resolve_or_create_user_for_identity(
                db: AsyncSession,
                *,
                identity: DiscoveredMediaUser,
            ) -> User:
                assert db is db_session
                assert identity.source_service == Service.PLEX
                return user

            async def fake_issue_login_session(**_: object) -> None:
                return None

            monkeypatch.setattr(
                auth_routes,
                "pop_pending_plex_auth",
                fake_pop_pending_plex_auth,
            )
            monkeypatch.setattr(
                auth_routes,
                "exchange_plex_pin_for_token",
                fake_exchange_plex_pin_for_token,
            )
            monkeypatch.setattr(
                auth_routes,
                "authenticate_plex_token",
                fake_authenticate_plex_token,
            )
            monkeypatch.setattr(
                auth_routes,
                "resolve_or_create_user_for_identity",
                fake_resolve_or_create_user_for_identity,
            )
            monkeypatch.setattr(
                auth_routes,
                "_issue_login_session",
                fake_issue_login_session,
            )

            request = _make_plex_callback_request()
            response = await auth_routes.media_plex_callback(
                request=request,
                state="expected-state",
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location") == "http://localhost:3000/"

        await engine.dispose()

    asyncio.run(run())


def test_media_plex_callback_uses_application_url_when_return_to_is_localhost(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            user = _new_user(
                username="plexmember",
                email="plexmember@example.com",
                role=UserRole.USER,
            )
            service = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex.local",
                api_key="server-token",
                name="plex-main",
                enabled=True,
            )
            db_session.add_all(
                [
                    user,
                    service,
                    GeneralSettings(application_url="https://public.example.com/"),
                ]
            )
            await db_session.commit()
            await db_session.refresh(user)
            await db_session.refresh(service)

            def fake_pop_pending_plex_auth(state: str) -> PlexPendingAuth:
                return PlexPendingAuth(
                    state=state,
                    service_config_id=service.id,
                    client_identifier="plex-client-id",
                    pin_id=123,
                    pin_code="abcd",
                    return_to="http://localhost:3000/",
                    created_at=datetime.now(UTC),
                )

            async def fake_exchange_plex_pin_for_token(
                pending: PlexPendingAuth,
            ) -> str:
                assert pending.service_config_id == service.id
                return "plex-user-token"

            async def fake_authenticate_plex_token(
                db: AsyncSession,
                *,
                provider: object,
                plex_user_token: str,
            ) -> DiscoveredMediaUser:
                assert db is db_session
                assert plex_user_token == "plex-user-token"
                return DiscoveredMediaUser(
                    source_service=Service.PLEX,
                    source_service_config_id=service.id,
                    source_service_name="plex-main",
                    source_user_id="plex-user-42",
                    username="plexmember",
                    username_normalized="plexmember",
                    email="plexmember@example.com",
                    display_name="Plex Member",
                    raw={"source": "plex"},
                )

            async def fake_resolve_or_create_user_for_identity(
                db: AsyncSession,
                *,
                identity: DiscoveredMediaUser,
            ) -> User:
                assert db is db_session
                assert identity.source_service == Service.PLEX
                return user

            async def fake_issue_login_session(**_: object) -> None:
                return None

            monkeypatch.setattr(
                auth_routes,
                "pop_pending_plex_auth",
                fake_pop_pending_plex_auth,
            )
            monkeypatch.setattr(
                auth_routes,
                "exchange_plex_pin_for_token",
                fake_exchange_plex_pin_for_token,
            )
            monkeypatch.setattr(
                auth_routes,
                "authenticate_plex_token",
                fake_authenticate_plex_token,
            )
            monkeypatch.setattr(
                auth_routes,
                "resolve_or_create_user_for_identity",
                fake_resolve_or_create_user_for_identity,
            )
            monkeypatch.setattr(
                auth_routes,
                "_issue_login_session",
                fake_issue_login_session,
            )

            request = _make_plex_callback_request()
            response = await auth_routes.media_plex_callback(
                request=request,
                state="expected-state",
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location") == "https://public.example.com/"

        await engine.dispose()

    asyncio.run(run())


def test_media_plex_callback_expired_state_uses_application_url_error() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            db_session.add(
                GeneralSettings(application_url="https://public.example.com")
            )
            await db_session.commit()

            request = _make_plex_callback_request()
            response = await auth_routes.media_plex_callback(
                request=request,
                state="expired-state",
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location", "").startswith(
                "https://public.example.com/?auth_error="
            )

        await engine.dispose()

    asyncio.run(run())


def test_media_plex_start_uses_application_url(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            db_session.add(
                GeneralSettings(application_url="https://public.example.com/")
            )
            await db_session.commit()

            provider = auth_routes.MediaAuthProviderConfig(
                service_config_id=1,
                service_type=Service.PLEX,
                name="plex-main",
                base_url="http://plex.local",
                auth_mode="redirect",
            )
            captured: dict[str, str] = {}

            async def fake_get_media_auth_provider(
                _db: AsyncSession,
                *,
                service_config_id: int,
            ) -> object | None:
                assert service_config_id == 1
                return provider

            async def fake_start_plex_pin_flow(
                *,
                provider: object,
                callback_url: str,
                return_to: str | None = None,
            ) -> str:
                captured["callback_url"] = callback_url
                captured["return_to"] = return_to or ""
                assert isinstance(provider, auth_routes.MediaAuthProviderConfig)
                assert provider.service_type is Service.PLEX
                return "http://plex.local/auth"

            monkeypatch.setattr(
                auth_routes,
                "get_media_auth_provider",
                fake_get_media_auth_provider,
            )
            monkeypatch.setattr(
                auth_routes,
                "start_plex_pin_flow",
                fake_start_plex_pin_flow,
            )

            request = _make_plex_start_request()
            response = await auth_routes.media_plex_start(
                request=request,
                service_config_id=1,
                return_to="http://localhost:3000/",
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location") == "http://plex.local/auth"
            assert captured["callback_url"] == (
                "https://public.example.com/api/auth/media/plex/callback"
            )
            assert captured["return_to"] == "https://public.example.com/"

        await engine.dispose()

    asyncio.run(run())


def test_media_plex_start_uses_absolute_return_to_origin_without_application_url(
    monkeypatch,
) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        async with session_maker() as db_session:
            provider = auth_routes.MediaAuthProviderConfig(
                service_config_id=1,
                service_type=Service.PLEX,
                name="plex-main",
                base_url="http://plex.local",
                auth_mode="redirect",
            )
            captured: dict[str, str] = {}

            async def fake_get_media_auth_provider(
                _db: AsyncSession,
                *,
                service_config_id: int,
            ) -> object | None:
                assert service_config_id == 1
                return provider

            async def fake_start_plex_pin_flow(
                *,
                provider: object,
                callback_url: str,
                return_to: str | None = None,
            ) -> str:
                captured["callback_url"] = callback_url
                captured["return_to"] = return_to or ""
                assert isinstance(provider, auth_routes.MediaAuthProviderConfig)
                return "http://plex.local/auth"

            monkeypatch.setattr(
                auth_routes,
                "get_media_auth_provider",
                fake_get_media_auth_provider,
            )
            monkeypatch.setattr(
                auth_routes,
                "start_plex_pin_flow",
                fake_start_plex_pin_flow,
            )

            request = _make_plex_start_request()
            response = await auth_routes.media_plex_start(
                request=request,
                service_config_id=1,
                return_to="http://localhost:3000/#/auth/complete",
                db=db_session,
            )

            assert response.status_code == 302
            assert response.headers.get("location") == "http://plex.local/auth"
            assert captured["callback_url"] == (
                "http://localhost:3000/api/auth/media/plex/callback"
            )
            assert captured["return_to"] == "http://localhost:3000/#/auth/complete"

        await engine.dispose()

    asyncio.run(run())
