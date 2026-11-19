from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.account import router as account_router
from backend.api.routes.account import link_media_identity, unlink_media_identity
from backend.api.routes.v1 import router as api_router
from backend.core.api_tokens import ApiPrincipal, get_api_principal
from backend.core.auth import get_current_user
from backend.database import Base, get_db
from backend.database.models import (
    GeneralSettings,
    MediaUserIdentity,
    ServiceConfig,
    User,
)
from backend.enums import Permission, Service, UserRole
from backend.models.auth import MediaIdentityLinkRequest
from backend.models.media_user_import import MediaImportLink, MediaUserImportRequest
from backend.services import media_user_import as imports
from backend.services.media_auth import (
    MediaAuthProvider,
    resolve_or_create_user_for_identity,
)


def local_user(name="existing_user", **kwargs):
    return User(username=name, password_hash="original", **kwargs)


def config(service=Service.JELLYFIN, name="main"):
    return ServiceConfig(
        service_type=service,
        name=name,
        base_url="https://media.example.com",
        api_key="api-secret",
        enabled=True,
    )


@asynccontextmanager
async def database(url="sqlite+aiosqlite:///:memory:"):
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessions() as db:
            server = config()
            actor = local_user("admin_user", role=UserRole.ADMIN)
            db.add_all(
                [server, actor, GeneralSettings(default_allowed_pages=["movies"])]
            )
            await db.commit()
            yield db, server, actor, sessions
    finally:
        await engine.dispose()


def source(server, id="new-id", name="new_viewer", email=None, disabled=False):
    provider = MediaAuthProvider(
        server.id, server.service_type, server.name, server.base_url, "credentials"
    )
    return imports._source(
        provider,
        {
            "Id": id,
            "Name": name,
            "Email": email,
            "Policy": {"IsDisabled": disabled, "IsAdministrator": True},
        },
    )


def mock_discovery(monkeypatch, sources):
    mocked = AsyncMock(return_value=sources)
    monkeypatch.setattr(imports, "discover_import_users", mocked)
    return mocked


async def run_import(db, actor, server, *, preview=False, **kwargs):
    try:
        return await imports.import_media_users(
            db,
            MediaUserImportRequest(service_config_id=server.id, **kwargs),
            preview=preview,
            actor=actor,
            actor_label="test",
        )
    finally:
        # Production requests get fresh sessions; tests deliberately reuse one
        # after rollback, which expires ORM objects even with expire_on_commit=False.
        for instance in list(db.identity_map.values()):
            if inspect(instance).expired:
                await db.refresh(instance)


def test_preview_create_reimport_and_subsequent_media_login(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            item = source(server, name="短い")
            mock_discovery(monkeypatch, [item])
            preview = await run_import(db, actor, server, preview=True)
            assert preview.ready == 1
            assert not (await db.execute(select(MediaUserIdentity))).scalars().all()
            assert len((await db.execute(select(User))).scalars().all()) == 1
            response = await run_import(
                db,
                actor,
                server,
                expected_revisions={
                    item.identity.source_user_id: preview.rows[0].revision
                },
            )
            assert (response.created, response.linked, response.ready) == (1, 0, 0)
            row = response.rows[0]
            user = await db.get(User, row.account.id)
            assert (
                user.role is UserRole.USER
            )  # Provider admin policy does not grant admin.
            assert user.permissions == ["request"] and user.allowed_pages == ["movies"]
            assert user.password_hash == "" and not user.require_password_change
            assert row.local_username.isascii()
            logged_in = await resolve_or_create_user_for_identity(
                db, identity=item.identity
            )
            assert logged_in.id == user.id
            repeated = await run_import(db, actor, server)
            assert repeated.skipped == 1 and repeated.created == repeated.linked == 0
            assert len((await db.execute(select(User))).scalars().all()) == 2

    asyncio.run(run())


def test_existing_account_requires_explicit_link_and_preserves_every_setting(
    monkeypatch,
):
    async def run():
        async with database() as (db, server, actor, _):
            existing = local_user(
                email="viewer@example.com",
                permissions=["manage_reclaim"],
                allowed_pages=[],
                is_active=False,
            )
            db.add(existing)
            await db.commit()
            original = (
                existing.password_hash,
                existing.role,
                existing.permissions,
                existing.allowed_pages,
                existing.is_active,
            )
            item = source(server, email="VIEWER@example.com")
            mock_discovery(monkeypatch, [item])
            preview = await run_import(db, actor, server, preview=True)
            assert preview.rows[0].status == "needs_review"
            assert (await run_import(db, actor, server)).skipped == 1
            assert not (await db.execute(select(MediaUserIdentity))).scalars().all()
            link = MediaImportLink(
                source_user_id=item.identity.source_user_id, target_user_id=existing.id
            )
            reviewed = await run_import(db, actor, server, preview=True, links=[link])
            assert reviewed.rows[0].status == "ready_to_link"
            response = await run_import(
                db, actor, server, links=[link], role=UserRole.ADMIN
            )
            assert response.linked == 1 and response.created == 0
            assert original == (
                existing.password_hash,
                existing.role,
                existing.permissions,
                existing.allowed_pages,
                existing.is_active,
            )
            assert (await run_import(db, actor, server, links=[link])).skipped == 1

    asyncio.run(run())


def test_selected_conflict_blocks_batch_but_unselected_conflict_does_not(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            db.add_all(
                [
                    local_user("name_match", email="one@example.com"),
                    local_user("email_match", email="two@example.com"),
                ]
            )
            await db.commit()
            sources = [
                source(server, "a", "new_viewer"),
                source(server, "b", "name_match", "two@example.com"),
            ]
            mock_discovery(monkeypatch, sources)
            for links in (
                [],
                [MediaImportLink(source_user_id="b", target_user_id=actor.id)],
            ):
                with pytest.raises(HTTPException) as exc:
                    await run_import(
                        db, actor, server, source_user_ids=["a", "b"], links=links
                    )
                assert exc.value.status_code == 409
                assert not (await db.execute(select(MediaUserIdentity))).scalars().all()
            response = await run_import(db, actor, server, source_user_ids=["a"])
            assert response.created == 1 and response.skipped == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", ["source", "target", "settings"])
def test_stale_preview_leaves_no_changes(monkeypatch, change):
    async def run():
        async with database() as (db, server, actor, _):
            items = [source(server)]
            mocked = mock_discovery(monkeypatch, items)
            preview = await run_import(db, actor, server, preview=True)
            if change == "source":
                mocked.return_value = [source(server, name="renamed_viewer")]
            elif change == "target":
                db.add(local_user("new_viewer"))
                await db.commit()
            else:
                settings = (await db.execute(select(GeneralSettings))).scalar_one()
                settings.default_allowed_pages = ["settings"]
                await db.commit()
            with pytest.raises(HTTPException) as exc:
                await run_import(
                    db,
                    actor,
                    server,
                    source_user_ids=["new-id"],
                    expected_revisions={"new-id": preview.rows[0].revision},
                )
            assert exc.value.status_code == 409
            assert not (await db.execute(select(MediaUserIdentity))).scalars().all()

    asyncio.run(run())


def test_failure_after_first_account_rolls_back_accounts_and_links(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            mock_discovery(
                monkeypatch,
                [
                    source(server, "a", "first_viewer"),
                    source(server, "b", "second_viewer"),
                ],
            )
            calls = 0

            async def fail(db, user):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected failure")

            monkeypatch.setattr(imports, "auto_enable_system_email", fail)
            with pytest.raises(RuntimeError, match="injected"):
                await run_import(db, actor, server)
            assert len((await db.execute(select(User))).scalars().all()) == 1
            assert not (await db.execute(select(MediaUserIdentity))).scalars().all()

    asyncio.run(run())


def test_no_selection_unknown_ids_disabled_users_and_duplicate_provider_email(
    monkeypatch,
):
    async def run():
        async with database() as (db, server, actor, _):
            mock_discovery(monkeypatch, [source(server)])
            assert (
                await run_import(db, actor, server, source_user_ids=[])
            ).created == 0
            with pytest.raises(HTTPException) as exc:
                await run_import(db, actor, server, source_user_ids=["unknown"])
            assert exc.value.status_code == 409
            mock_discovery(monkeypatch, [source(server, disabled=True)])
            assert (await run_import(db, actor, server)).created == 0
            mock_discovery(
                monkeypatch,
                [
                    source(server, "a", "first_viewer", "same@example.com"),
                    source(server, "b", "other_viewer", "same@example.com"),
                ],
            )
            preview = await run_import(db, actor, server, preview=True)
            assert preview.errors == 2 and preview.ready == 0

    asyncio.run(run())


def test_admin_import_authority_and_member_api_restrictions(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            mock_discovery(monkeypatch, [source(server)])
            with pytest.raises(HTTPException) as exc:
                await run_import(db, None, server, role=UserRole.ADMIN)
            assert exc.value.status_code == 403
            response = await run_import(
                db,
                None,
                server,
                links=[
                    MediaImportLink(source_user_id="new-id", target_user_id=actor.id)
                ],
                preview=True,
            )
            assert response.rows[0].status == "conflict"
            created = await run_import(db, actor, server, role=UserRole.ADMIN)
            user = await db.get(User, created.rows[0].account.id)
            assert user.role is UserRole.ADMIN and user.allowed_pages is None

    asyncio.run(run())


def test_manager_grant_limits_and_existing_account_link_limits(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            manager = local_user(
                "user_manager",
                permissions=["manage_users", "request"],
                allowed_pages=["movies"],
            )
            target = local_user(
                "powerful_user",
                permissions=["manage_reclaim"],
                allowed_pages=["movies"],
            )
            db.add_all([manager, target])
            await db.commit()
            mock_discovery(monkeypatch, [source(server)])
            for kwargs in (
                {"role": UserRole.ADMIN},
                {"permissions": [Permission.MANAGE_RECLAIM]},
                {"use_default_page_access": False, "allowed_pages": None},
            ):
                with pytest.raises(HTTPException) as exc:
                    await run_import(db, manager, server, **kwargs)
                assert exc.value.status_code == 403
            for target_id in [actor.id, target.id]:
                response = await run_import(
                    db,
                    manager,
                    server,
                    links=[
                        MediaImportLink(
                            source_user_id="new-id", target_user_id=target_id
                        )
                    ],
                    preview=True,
                )
                assert response.rows[0].status == "conflict"
            assert (await run_import(db, manager, server)).created == 1

    asyncio.run(run())


def test_same_source_id_on_two_servers_has_independent_links(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            mock_discovery(monkeypatch, [source(server)])
            first = await run_import(db, actor, server)
            other = config(name="other")
            db.add(other)
            await db.commit()
            mock_discovery(monkeypatch, [source(other)])
            preview = await run_import(db, actor, other, preview=True)
            assert preview.rows[0].status == "needs_review"
            response = await run_import(
                db,
                actor,
                other,
                links=[
                    MediaImportLink(
                        source_user_id="new-id", target_user_id=first.rows[0].account.id
                    )
                ],
            )
            assert response.linked == 1
            identities = (await db.execute(select(MediaUserIdentity))).scalars().all()
            assert {i.source_service_config_id for i in identities} == {
                server.id,
                other.id,
            }

    asyncio.run(run())


def test_concurrent_imports_create_one_account(monkeypatch, tmp_path):
    async def run():
        async with database(
            "sqlite+aiosqlite:///" + (tmp_path / "import.db").as_posix()
        ) as (db, server, actor, sessions):
            mock_discovery(monkeypatch, [source(server)])
            server_id = server.id

            async def execute():
                async with sessions() as other_db:
                    return await imports.import_media_users(
                        other_db,
                        MediaUserImportRequest(service_config_id=server_id),
                        preview=False,
                        actor=None,
                        actor_label="concurrent",
                    )

            responses = await asyncio.gather(execute(), execute())
            assert sum(r.created for r in responses) == 1
            assert sum(r.skipped for r in responses) == 1
            assert (
                len((await db.execute(select(MediaUserIdentity))).scalars().all()) == 1
            )

    asyncio.run(run())


def test_manual_conflict_resolution_can_stage_a_new_identity_without_creating_accounts(
    monkeypatch,
):
    async def run():
        async with database() as (db, server, actor, _):
            target = local_user("target_user", email="target@example.com")
            other = local_user("other_user", email="other@example.com")
            db.add_all([target, other])
            await db.commit()
            mock_discovery(
                monkeypatch,
                [source(server, name="target_user", email="other@example.com")],
            )
            assert (await run_import(db, actor, server, preview=True)).rows[
                0
            ].status == "conflict"
            assert not (await db.execute(select(MediaUserIdentity))).scalars().all()
            identity_id = await imports.stage_media_identity(
                db, service_config_id=server.id, source_user_id="new-id"
            )
            assert (await db.get(MediaUserIdentity, identity_id)).user_id is None
            await link_media_identity(
                identity_id,
                MediaIdentityLinkRequest(target_user_id=target.id),
                actor,
                db,
            )
            repeated_id = await imports.stage_media_identity(
                db, service_config_id=server.id, source_user_id="new-id"
            )
            assert repeated_id == identity_id
            assert (await db.get(MediaUserIdentity, identity_id)).user_id == target.id
            assert (await run_import(db, actor, server, preview=True)).rows[
                0
            ].status == "skipped"
            assert len((await db.execute(select(User))).scalars().all()) == 3

    asyncio.run(run())


def test_manual_link_and_unlink_cannot_bypass_import_authority_limits(monkeypatch):
    async def run():
        async with database() as (db, server, actor, _):
            manager = local_user(
                "user_manager", permissions=["manage_users"], allowed_pages=[]
            )
            db.add(manager)
            await db.commit()
            mock_discovery(monkeypatch, [source(server)])
            identity_id = await imports.stage_media_identity(
                db, service_config_id=server.id, source_user_id="new-id"
            )
            with pytest.raises(HTTPException) as exc:
                await link_media_identity(
                    identity_id,
                    MediaIdentityLinkRequest(target_user_id=actor.id),
                    manager,
                    db,
                )
            assert exc.value.status_code == 403
            await link_media_identity(
                identity_id,
                MediaIdentityLinkRequest(target_user_id=actor.id),
                actor,
                db,
            )
            with pytest.raises(HTTPException) as exc:
                await unlink_media_identity(identity_id, manager, db)
            assert exc.value.status_code == 403
            with pytest.raises(HTTPException) as exc:
                await link_media_identity(
                    identity_id,
                    MediaIdentityLinkRequest(target_user_id=manager.id),
                    manager,
                    db,
                )
            assert exc.value.status_code == 403

    asyncio.run(run())


@pytest.mark.parametrize("change", ["login", "config", "actor"])
def test_changes_during_provider_discovery_are_revalidated(
    monkeypatch, tmp_path, change
):
    async def run():
        async with database(
            "sqlite+aiosqlite:///" + (tmp_path / "race.db").as_posix()
        ) as (db, server, actor, sessions):
            item = source(server)
            actor_id, config_id = actor.id, server.id

            async def discover(*_args):
                async with sessions() as other_db:
                    if change == "login":
                        await resolve_or_create_user_for_identity(
                            other_db, identity=item.identity
                        )
                    elif change == "config":
                        current = await other_db.get(ServiceConfig, config_id)
                        current.enabled = False
                    else:
                        current = await other_db.get(User, actor_id)
                        current.role = UserRole.USER
                    await other_db.commit()
                return [item]

            monkeypatch.setattr(imports, "discover_import_users", discover)
            if change == "login":
                response = await run_import(db, actor, server)
                assert response.skipped == 1 and response.created == 0
                assert (
                    len((await db.execute(select(MediaUserIdentity))).scalars().all())
                    == 1
                )
            else:
                with pytest.raises(HTTPException) as exc:
                    await run_import(db, actor, server)
                assert exc.value.status_code == (409 if change == "config" else 403)
                assert not (await db.execute(select(MediaUserIdentity))).scalars().all()

    asyncio.run(run())


class FakeResponse:
    def __init__(self, payload=None, xml="", error=False):
        self.payload, self.text, self.error = payload, xml, error

    def raise_for_status(self):
        if self.error:
            raise RuntimeError("api-secret should never be exposed")

    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self, responses):
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, url, **kwargs):
        return self.responses[url]


@pytest.mark.parametrize("service", [Service.JELLYFIN, Service.EMBY])
def test_provider_discovery_keeps_metadata_and_disabled_status(monkeypatch, service):
    provider = MediaAuthProvider(
        1, service, "main", "https://media.example.com", "credentials"
    )
    response = FakeResponse(
        [
            {"Id": "a", "Name": "viewer", "ConnectUserName": "VIEWER@example.com"},
            {
                "Id": "b",
                "Name": "no_email",
                "Email": "not-email",
                "Policy": {"IsDisabled": True},
            },
        ]
    )
    monkeypatch.setattr(
        imports.niquests,
        "AsyncSession",
        lambda: FakeHTTP({"https://media.example.com/Users": response}),
    )
    sources = asyncio.run(imports.discover_import_users(provider, "api-secret"))
    assert sources[0].identity.email.lower() == "viewer@example.com"
    assert sources[1].identity.email is None and sources[1].skip_reason


def test_plex_discovery_filters_machine_and_managed_profiles_and_includes_owner(
    monkeypatch,
):
    provider = MediaAuthProvider(
        1, Service.PLEX, "plex", "https://media.example.com", "redirect"
    )
    responses = {
        "https://media.example.com/identity": FakeResponse(
            {"MediaContainer": {"machineIdentifier": "mine"}}
        ),
        imports.PLEX_TV_USER_URL: FakeResponse(
            {
                "id": 99,
                "username": "owner",
                "email": "owner@example.com",
                "authToken": "never-store",
            }
        ),
        imports.PLEX_TV_USERS_URL: FakeResponse(
            xml="""<MediaContainer>
            <User id="1" username="viewer" email="viewer@example.com"><Server machineIdentifier="mine"/></User>
            <User id="2" title="kid"><Server machineIdentifier="mine"/></User>
            <User id="3" username="other" email="other@example.com"><Server machineIdentifier="elsewhere"/></User>
        </MediaContainer>"""
        ),
    }
    monkeypatch.setattr(imports.niquests, "AsyncSession", lambda: FakeHTTP(responses))
    sources = asyncio.run(imports.discover_import_users(provider, "api-secret"))
    assert {s.identity.source_user_id for s in sources} == {"1", "2", "99"}
    assert next(s for s in sources if s.identity.source_user_id == "2").skip_reason
    assert all("never-store" not in str(s.identity.raw) for s in sources)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(error=True),
        FakeResponse({}),
        FakeResponse([{"Name": "missing-id"}]),
        FakeResponse([{"Id": "same", "Name": "one"}, {"Id": "same", "Name": "two"}]),
    ],
)
def test_provider_failures_are_not_empty_success_and_do_not_leak_credentials(
    monkeypatch, response
):
    provider = MediaAuthProvider(
        1, Service.JELLYFIN, "main", "https://media.example.com", "credentials"
    )
    monkeypatch.setattr(
        imports.niquests,
        "AsyncSession",
        lambda: FakeHTTP({"https://media.example.com/Users": response}),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(imports.discover_import_users(provider, "api-secret"))
    assert exc.value.status_code == 502
    assert "api-secret" not in str(exc.value.detail)


def test_duplicate_or_unselected_explicit_links_are_rejected():
    link = {"source_user_id": "a", "target_user_id": 1}
    for payload in ({"links": [link, link]}, {"links": [link], "source_user_ids": []}):
        with pytest.raises(ValidationError):
            MediaUserImportRequest(service_config_id=1, **payload)


def test_ui_and_external_routes_enforce_authority_and_publish_contracts(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            server = config()
            db.add(server)
            db.add(local_user("admin_user", role=UserRole.ADMIN))
            await db.commit()
            return server

    server = asyncio.run(setup())
    mock_discovery(monkeypatch, [source(server)])
    principal = ApiPrincipal(1, "token", frozenset())
    ui_actor = local_user("ordinary_user")

    async def get_principal():
        return principal

    async def current_user():
        return ui_actor

    async def session():
        async with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(account_router)
    app.include_router(api_router)
    app.dependency_overrides[get_api_principal] = get_principal
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = session
    body = {"service_config_id": server.id}
    with TestClient(app) as client:
        assert (
            client.get(
                "/api/account/users/import-from-media-server/providers"
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/account/users/import-from-media-server", json=body
            ).status_code
            == 403
        )
        assert client.get("/api/v1/users").status_code == 403
        assert (
            client.post("/api/v1/users/import-from-media-server", json=body).status_code
            == 403
        )
        principal = ApiPrincipal(1, "token", frozenset({"users:read"}))
        providers = client.get("/api/v1/users/import-from-media-server/providers")
        assert (
            providers.status_code == 200
            and "api-secret" not in providers.text
            and "base_url" not in providers.text
        )
        assert (
            client.post("/api/v1/users/import-from-media-server", json=body).json()[
                "ready"
            ]
            == 1
        )
        assert (
            client.post(
                "/api/v1/users/import-from-media-server?preview=false", json=body
            ).status_code
            == 403
        )
        principal = ApiPrincipal(1, "token", frozenset({"users:manage"}))
        assert (
            client.post(
                "/api/v1/users/import-from-media-server?preview=false",
                json={**body, "role": "admin"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/users/import-from-media-server?preview=false", json=body
            ).json()["created"]
            == 1
        )
        accounts = client.get("/api/v1/users?q=new_viewer").json()
        assert len(accounts) == 1 and "password_hash" not in accounts[0]
        schema = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/users/import-from-media-server" in schema["paths"]
        assert "users" in client.get("/api/v1").json()["resources"]
    asyncio.run(engine.dispose())
