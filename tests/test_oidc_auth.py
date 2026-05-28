from __future__ import annotations

import asyncio

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
from backend.database.models import OIDCSettings, User, UserSession
from backend.enums import UserRole
from backend.models.settings import OIDCSettingsUpdate


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
