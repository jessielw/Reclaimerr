from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.api.routes import auth as auth_routes
from backend.api.routes.account import router as account_router
from backend.api.routes.settings.oidc import router as oidc_router
from backend.api.routes.settings.oidc import update_oidc_settings
from backend.api.routes.settings.services import (
    _upsert_service_config,
    delete_service_settings,
)
from backend.core.auth import COOKIE_NAME, get_current_user, get_password_hash
from backend.core.encryption import fer_encrypt
from backend.core.login_policy import get_login_methods
from backend.core.settings import settings
from backend.database import Base, get_db
from backend.database.models import (
    AdminNotice,
    OIDCSettings,
    ServiceConfig,
    User,
    UserSession,
)
from backend.enums import Service, UserRole
from backend.models.settings import OIDCSettingsUpdate, ServiceConfigUpdate
from backend.services.admin_notices import (
    NOTICE_KEY_LOGIN_RECOVERY,
    sync_login_recovery_notice,
)
from backend.services.media_auth import PlexPendingAuth


@asynccontextmanager
async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


def _user(role: UserRole = UserRole.ADMIN) -> User:
    return User(
        username="admin",
        password_hash=get_password_hash("test-password"),
        role=role,
        permissions=[],
    )


def _sso_settings(**kwargs) -> OIDCSettings:
    return OIDCSettings(
        enabled=True,
        issuer_url="https://auth.example.com",
        client_id="reclaimerr",
        client_secret=fer_encrypt("secret"),
        **kwargs,
    )


def _provider(name: str = "Plex") -> ServiceConfig:
    return ServiceConfig(
        service_type=Service.PLEX,
        name=name,
        base_url="http://plex",
        api_key=fer_encrypt("server-token"),
        enabled=True,
        is_main=False,
    )


def _app(db: AsyncSession, user: User | None = None) -> FastAPI:
    app = FastAPI()
    app.state.limiter = auth_routes.limiter
    auth_routes.limiter.reset()
    app.include_router(auth_routes.router)
    app.include_router(account_router)
    app.include_router(oidc_router, prefix="/api/settings")
    app.dependency_overrides[get_db] = lambda: db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


def test_default_login_methods_preserve_existing_behavior(monkeypatch):
    monkeypatch.setattr(settings, "auth_force_local_login", False)

    async def run():
        async with _database() as db:
            methods = await get_login_methods(db)
            assert methods.model_dump() == {
                "local_enabled": True,
                "media_enabled": True,
                "sso_enabled": True,
                "local_recovery_active": False,
            }

    asyncio.run(run())


@pytest.mark.parametrize(
    "flags",
    [
        (False, False, False),
        (False, True, False),
        (False, False, True),
        (False, True, True),
    ],
)
def test_rejects_policy_without_a_configured_method(flags, monkeypatch):
    monkeypatch.setattr(settings, "auth_force_local_login", True)

    async def run():
        async with _database() as db:
            admin = _user()
            db.add(admin)
            await db.commit()
            with pytest.raises(HTTPException, match="configured login method"):
                await update_oidc_settings(
                    OIDCSettingsUpdate(
                        allow_local_login=flags[0],
                        allow_media_login=flags[1],
                        allow_sso_login=flags[2],
                    ),
                    admin,
                    db,
                )
            row = (await db.execute(select(OIDCSettings))).scalar_one()
            assert (
                row.allow_local_login
            )  # Temporary recovery cannot relax saved-policy validation.

    asyncio.run(run())


@pytest.mark.parametrize("method", ["local", "media", "sso"])
def test_single_configured_method_can_be_saved(method, monkeypatch):
    monkeypatch.setattr(settings, "auth_force_local_login", False)

    async def run():
        async with _database() as db:
            admin = _user()
            db.add(admin)
            if method == "media":
                db.add(_provider())
            await db.commit()
            response = await update_oidc_settings(
                OIDCSettingsUpdate(
                    enabled=method == "sso",
                    issuer_url="https://auth.example.com" if method == "sso" else "",
                    client_id="reclaimerr" if method == "sso" else "",
                    client_secret="secret" if method == "sso" else None,
                    allow_local_login=method == "local",
                    allow_media_login=method == "media",
                    allow_sso_login=method == "sso",
                ),
                admin,
                db,
            )
            assert response.allow_local_login == (method == "local")
            assert response.allow_media_login == (method == "media")
            assert response.allow_sso_login == (method == "sso")
            db.expunge_all()
            methods = await get_login_methods(db)
            assert methods.local_enabled == (method == "local")
            assert methods.media_enabled == (method == "media")
            assert methods.sso_enabled == (method == "sso")

    asyncio.run(run())


def test_disabled_methods_are_hidden_and_rejected_by_http(monkeypatch):
    monkeypatch.setattr(settings, "auth_force_local_login", False)

    async def run():
        async with _database() as db:
            admin = _user()
            db.add_all(
                [
                    admin,
                    _provider(),
                    _sso_settings(allow_media_login=False, allow_sso_login=False),
                ]
            )
            await db.commit()
            async with AsyncClient(
                transport=ASGITransport(app=_app(db)), base_url="http://test"
            ) as client:
                providers = await client.get("/api/auth/media/providers")
                assert providers.json()["providers"] == []
                status = await client.get("/api/auth/oidc/status")
                assert status.json()["enabled"] is False
                for url, body in [
                    ("/api/auth/oidc/start", None),
                    ("/api/auth/media/plex/start?service_config_id=1", None),
                    (
                        "/api/auth/media/login",
                        {
                            "service_config_id": 1,
                            "username": "admin",
                            "password": "test-password",
                        },
                    ),
                ]:
                    response = (
                        await client.get(url)
                        if body is None
                        else await client.post(url, json=body)
                    )
                    assert response.status_code == 403
                    assert "disabled" in response.json()["detail"]
                assert (await db.execute(select(UserSession))).scalars().all() == []

    asyncio.run(run())


def test_local_recovery_requires_credentials_and_does_not_change_saved_policy(
    monkeypatch,
):
    monkeypatch.setattr(settings, "auth_force_local_login", False)

    async def run():
        async with _database() as db:
            admin = _user()
            row = _sso_settings(allow_local_login=False, allow_media_login=False)
            db.add_all([admin, row])
            await db.commit()
            async with AsyncClient(
                transport=ASGITransport(app=_app(db)), base_url="http://test"
            ) as client:
                credentials = {"username": "admin", "password": "test-password"}
                assert (
                    await client.post("/api/auth/login", json=credentials)
                ).status_code == 403
                monkeypatch.setattr(settings, "auth_force_local_login", True)
                methods = (await client.get("/api/auth/methods")).json()
                assert methods["local_enabled"] and methods["local_recovery_active"]
                wrong = await client.post(
                    "/api/auth/login", json={**credentials, "password": "wrong"}
                )
                assert wrong.status_code == 401
                successful = await client.post("/api/auth/login", json=credentials)
                assert successful.status_code == 200
                assert COOKIE_NAME in successful.cookies
                assert row.allow_local_login is False
                # Recovery sessions remain usable after disabling the override.
                monkeypatch.setattr(settings, "auth_force_local_login", False)
                assert (await client.get("/api/account/me")).status_code == 200
                assert (
                    await client.post("/api/auth/login", json=credentials)
                ).status_code == 403

    asyncio.run(run())


def test_pending_redirect_flows_cannot_complete_after_method_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_force_local_login", False)
    issue_session = AsyncMock()
    exchange = AsyncMock()
    monkeypatch.setattr(auth_routes, "_issue_login_session", issue_session)
    monkeypatch.setattr(auth_routes, "exchange_plex_pin_for_token", exchange)
    monkeypatch.setattr(auth_routes, "poll_plex_pin_for_token", exchange)
    pending = PlexPendingAuth(
        state="test-state",
        service_config_id=1,
        client_identifier="test-client",
        pin_id=123,
        pin_code="abcd",
        return_to="/",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(auth_routes, "pop_pending_plex_auth", lambda _: pending)
    monkeypatch.setattr(auth_routes, "peek_pending_plex_auth", lambda _: pending)

    async def run():
        async with _database() as db:
            db.add(_sso_settings(allow_media_login=False, allow_sso_login=False))
            await db.commit()
            async with AsyncClient(
                transport=ASGITransport(app=_app(db)), base_url="http://test"
            ) as client:
                sso = await client.get("/api/auth/oidc/callback?code=code&state=state")
                plex = await client.get(
                    "/api/auth/media/plex/callback?state=test-state",
                    cookies={auth_routes.PLEX_FLOW_COOKIE: "test-state"},
                )
                for response in (sso, plex):
                    assert "disabled" in response.headers["location"]
                    assert COOKIE_NAME not in response.cookies
                client.cookies.set(auth_routes.PLEX_FLOW_COOKIE, "test-state")
                poll = await client.get("/api/auth/media/plex/poll")
                assert poll.json()["status"] == "error"
                assert "disabled" in poll.json()["message"]
                assert COOKIE_NAME not in poll.cookies
            issue_session.assert_not_awaited()
            exchange.assert_not_awaited()

    asyncio.run(run())


def test_cannot_disable_only_allowed_sso_provider():
    async def run():
        async with _database() as db:
            admin = _user()
            db.add_all(
                [admin, _sso_settings(allow_local_login=False, allow_media_login=False)]
            )
            await db.commit()
            with pytest.raises(HTTPException, match="configured login method"):
                await update_oidc_settings(
                    OIDCSettingsUpdate(
                        enabled=False, allow_local_login=False, allow_media_login=False
                    ),
                    admin,
                    db,
                )
            assert (await db.execute(select(OIDCSettings))).scalar_one().enabled
            # Re-enable Local in the same save to safely turn off the provider.
            result = await update_oidc_settings(
                OIDCSettingsUpdate(enabled=False), admin, db
            )
            assert result.allow_local_login and not result.enabled

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["disable", "delete"])
def test_cannot_remove_last_media_provider_but_can_remove_one_of_two(
    operation, monkeypatch
):
    monkeypatch.setattr(settings, "auth_force_local_login", True)

    async def run():
        async with _database() as db:
            admin = _user()
            provider = _provider()
            db.add_all(
                [
                    admin,
                    provider,
                    OIDCSettings(allow_local_login=False, allow_sso_login=False),
                ]
            )
            await db.commit()
            provider_id = provider.id
            with pytest.raises(HTTPException, match="configured login method"):
                if operation == "delete":
                    await delete_service_settings(provider_id, admin, db)
                else:
                    await _upsert_service_config(
                        db,
                        ServiceConfigUpdate(
                            id=provider_id,
                            service_type=Service.PLEX,
                            name="Plex",
                            base_url="http://plex",
                            enabled=False,
                        ),
                    )
            remaining = (await db.execute(select(ServiceConfig))).scalar_one()
            assert remaining.enabled
            db.add(_provider("Second Plex"))
            await db.commit()
            if operation == "disable":
                await _upsert_service_config(
                    db,
                    ServiceConfigUpdate(
                        id=provider_id,
                        service_type=Service.PLEX,
                        name="Plex",
                        base_url="http://plex",
                        enabled=False,
                    ),
                )
                assert not (await db.get(ServiceConfig, provider_id)).enabled
            else:
                from backend.api.routes.settings import services
                from backend.core import service_runtime

                monkeypatch.setattr(
                    service_runtime, "clear_deleted_service_runtime", AsyncMock()
                )
                monkeypatch.setattr(services, "enqueue_task_run", AsyncMock())
                await delete_service_settings(provider_id, admin, db)
                assert await db.get(ServiceConfig, provider_id) is None
            assert (
                len((await auth_routes.list_media_auth_login_providers(db)).providers)
                == 1
            )

    asyncio.run(run())


@pytest.mark.parametrize("role", [UserRole.USER, None])
def test_authentication_settings_are_admin_only(role):
    async def run():
        async with _database() as db:
            user = _user(role) if role is not None else None
            app = _app(db, user)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                for response in [
                    await client.get("/api/settings/oidc"),
                    await client.put(
                        "/api/settings/oidc", json={"allow_local_login": False}
                    ),
                ]:
                    assert response.status_code == (403 if role is not None else 401)
            assert (await db.execute(select(OIDCSettings))).scalars().all() == []

    asyncio.run(run())


def test_recovery_notice_is_raised_and_resolved():
    async def run():
        async with _database() as db:
            await sync_login_recovery_notice(db, recovery_active=False)
            assert (await db.execute(select(AdminNotice))).scalars().all() == []
            await sync_login_recovery_notice(db, recovery_active=True)
            await db.commit()
            notice = (
                await db.execute(
                    select(AdminNotice).where(
                        AdminNotice.dedupe_key == NOTICE_KEY_LOGIN_RECOVERY
                    )
                )
            ).scalar_one()
            assert notice.is_active and "AUTH_FORCE_LOCAL_LOGIN" in notice.message
            await sync_login_recovery_notice(db, recovery_active=False)
            await db.commit()
            await db.refresh(notice)
            assert not notice.is_active

    asyncio.run(run())


def test_login_method_migration_preserves_settings_and_allows_existing_methods():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE oidc_settings (id INTEGER PRIMARY KEY, "
                "client_secret TEXT NOT NULL, sso_tab_label TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO oidc_settings VALUES (1, 'existing-encrypted-secret', 'Company')"
            )
        )
        config = Config()
        config.set_main_option("script_location", "backend/alembic")
        config.attributes["connection"] = connection
        command.stamp(config, "d8a3f1c6b902")
        command.upgrade(config, "e4b7c2a9f013")
        assert connection.execute(
            text(
                "SELECT client_secret, sso_tab_label, allow_local_login, "
                "allow_media_login, allow_sso_login FROM oidc_settings"
            )
        ).one() == ("existing-encrypted-secret", "Company", 1, 1, 1)
        command.downgrade(config, "d8a3f1c6b902")
        assert connection.execute(
            text("SELECT client_secret, sso_tab_label FROM oidc_settings")
        ).one() == ("existing-encrypted-secret", "Company")
    engine.dispose()


def test_method_disabled_during_provider_request_cannot_issue_a_session(monkeypatch):
    monkeypatch.setattr(settings, "auth_force_local_login", False)

    async def run():
        async with _database() as db:
            admin = _user()
            provider = _provider()
            provider.service_type = Service.JELLYFIN
            row = OIDCSettings()
            db.add_all([admin, provider, row])
            await db.commit()

            async def disable_during_authentication(**kwargs):
                row.allow_media_login = False
                await db.commit()
                return object()

            monkeypatch.setattr(
                auth_routes,
                "authenticate_emby_family_credentials",
                disable_during_authentication,
            )
            monkeypatch.setattr(
                auth_routes,
                "resolve_or_create_user_for_identity",
                AsyncMock(return_value=admin),
            )
            async with AsyncClient(
                transport=ASGITransport(app=_app(db)), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/auth/media/login",
                    json={
                        "service_config_id": provider.id,
                        "username": "admin",
                        "password": "test-password",
                    },
                )
                assert response.status_code == 403
                assert "disabled" in response.json()["detail"]
                assert COOKIE_NAME not in response.cookies
                assert (await db.execute(select(UserSession))).scalars().all() == []

    asyncio.run(run())


def test_login_runs_one_password_check_for_known_and_unknown_usernames(monkeypatch):
    # Skipping Argon2 for an unknown username made it answer measurably faster
    # than a wrong password, which revealed which usernames exist.
    monkeypatch.setattr(settings, "auth_force_local_login", False)
    checked_hashes: list[str] = []
    real_verify = auth_routes.verify_password

    def counting_verify(password: str, hashed: str) -> bool:
        checked_hashes.append(hashed)
        return real_verify(password, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", counting_verify)

    async def run():
        async with _database() as db:
            db.add(_user())
            await db.commit()
            async with AsyncClient(
                transport=ASGITransport(app=_app(db)), base_url="http://test"
            ) as client:
                for username in ("admin", "nobody"):
                    response = await client.post(
                        "/api/auth/login",
                        json={"username": username, "password": "wrong"},
                    )
                    assert response.status_code == 401
                    assert response.json()["detail"] == "Invalid credentials"

    asyncio.run(run())
    assert len(checked_hashes) == 2
    assert checked_hashes[1] == auth_routes.dummy_password_hash()
