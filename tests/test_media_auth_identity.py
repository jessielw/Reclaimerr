from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.account import change_password as change_password_route
from backend.api.routes.account import link_media_identity, unlink_media_identity
from backend.core.auth import verify_password
from backend.core.encryption import fer_decrypt
from backend.database import Base
from backend.database.models import AdminNotice, MediaUserIdentity, ServiceConfig, User
from backend.enums import Permission, Service, UserRole
from backend.models.auth import ChangePasswordRequest, MediaIdentityLinkRequest
from backend.services.admin_notices import upsert_singleton_notice
from backend.services.media_auth import (
    DiscoveredMediaUser,
    media_auth_conflict_notice_key_for_source,
    persist_plex_identity_token,
    resolve_or_create_user_for_identity,
)


def _new_user(
    username: str,
    *,
    role: UserRole = UserRole.USER,
    password_hash: str = "hashed",
    email: str | None = None,
    permissions: list[str] | None = None,
) -> User:
    return User(
        username=username,
        password_hash=password_hash,
        email=email,
        role=role,
        permissions=permissions or [],
    )


def _new_service_config(
    *,
    service_type: Service,
    name: str,
    enabled: bool = True,
) -> ServiceConfig:
    return ServiceConfig(
        service_type=service_type,
        name=name,
        base_url=f"https://{name}.example.com",
        api_key="key",
        enabled=enabled,
    )


def test_change_password_allows_passwordless_user_without_old_password() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = _new_user("passwordless_user", password_hash="")
            db_session.add(user)
            await db_session.commit()
            await db_session.refresh(user)

            response = Response()
            result = await change_password_route(
                request=ChangePasswordRequest(new_password="new-password-123"),
                current_user=user,
                response=response,
                db=db_session,
            )

            assert result["message"] == "Password changed successfully"
            assert user.password_hash
            assert verify_password("new-password-123", user.password_hash)
            assert user.require_password_change is False

        await engine.dispose()

    asyncio.run(run())


def test_resolve_or_create_user_aggregates_case_insensitive_usernames() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            local_user = _new_user("Norah")
            jellyfin = _new_service_config(
                service_type=Service.JELLYFIN,
                name="jelly-main",
            )
            plex = _new_service_config(
                service_type=Service.PLEX,
                name="plex-main",
            )
            db_session.add_all([local_user, jellyfin, plex])
            await db_session.commit()
            await db_session.refresh(local_user)
            await db_session.refresh(jellyfin)
            await db_session.refresh(plex)

            jellyfin_identity = DiscoveredMediaUser(
                source_service=Service.JELLYFIN,
                source_service_config_id=jellyfin.id,
                source_service_name=jellyfin.name,
                source_user_id="jf-user-1",
                username="norah",
                username_normalized="norah",
                email=None,
                display_name="Norah",
                raw={"source": "jellyfin"},
            )
            plex_identity = DiscoveredMediaUser(
                source_service=Service.PLEX,
                source_service_config_id=plex.id,
                source_service_name=plex.name,
                source_user_id="plex-user-1",
                username="NORAH",
                username_normalized="norah",
                email=None,
                display_name="Norah",
                raw={"source": "plex"},
            )

            resolved_a = await resolve_or_create_user_for_identity(
                db_session, identity=jellyfin_identity
            )
            resolved_b = await resolve_or_create_user_for_identity(
                db_session, identity=plex_identity
            )
            await db_session.commit()

            assert resolved_a.id == local_user.id
            assert resolved_b.id == local_user.id

            identities = (
                (
                    await db_session.execute(
                        select(MediaUserIdentity).order_by(MediaUserIdentity.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert len(identities) == 2
            assert all(identity.user_id == local_user.id for identity in identities)

        await engine.dispose()

    asyncio.run(run())


def test_admin_can_link_and_unlink_media_identity_and_resolve_notice() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            service = _new_service_config(
                service_type=Service.JELLYFIN,
                name="jelly-main",
            )
            actor = _new_user(
                "admin_user",
                role=UserRole.ADMIN,
                permissions=[Permission.MANAGE_USERS.value],
            )
            target_user = _new_user("target_user")
            db_session.add_all([service, actor, target_user])
            await db_session.commit()
            await db_session.refresh(service)
            await db_session.refresh(actor)
            await db_session.refresh(target_user)

            identity = MediaUserIdentity(
                source_service=Service.JELLYFIN,
                source_service_config_id=service.id,
                source_user_id="jf-user-88",
                username="jfuser",
                username_normalized="jfuser",
                user_id=None,
                email="jfuser@example.com",
                display_name="Jf User",
                raw={"source": "jellyfin"},
                last_seen_at=datetime.now(UTC),
            )
            db_session.add(identity)
            await db_session.commit()
            await db_session.refresh(identity)

            notice_key = media_auth_conflict_notice_key_for_source(
                identity.source_service,
                identity.source_service_config_id,
                identity.source_user_id,
            )
            await upsert_singleton_notice(
                db_session,
                dedupe_key=notice_key,
                kind="media_auth_conflict",
                severity="warning",
                title="Needs Link",
                message="Test notice",
                action_label=None,
                action_href=None,
                context_json=None,
            )
            await db_session.commit()

            linked = await link_media_identity(
                identity_id=identity.id,
                body=MediaIdentityLinkRequest(target_user_id=target_user.id),
                _actor=actor,
                db=db_session,
            )
            assert linked.user_id == target_user.id

            notice = (
                await db_session.execute(
                    select(AdminNotice).where(AdminNotice.dedupe_key == notice_key)
                )
            ).scalar_one()
            assert notice.is_active is False

            unlinked = await unlink_media_identity(
                identity_id=identity.id,
                _actor=actor,
                db=db_session,
            )
            assert unlinked.user_id is None

        await engine.dispose()

    asyncio.run(run())


def test_persist_plex_identity_token_updates_linked_identity() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            local_user = _new_user("plex_user", email="plex_user@example.com")
            plex = _new_service_config(service_type=Service.PLEX, name="plex-main")
            db_session.add_all([local_user, plex])
            await db_session.commit()
            await db_session.refresh(local_user)
            await db_session.refresh(plex)

            identity = DiscoveredMediaUser(
                source_service=Service.PLEX,
                source_service_config_id=plex.id,
                source_service_name=plex.name,
                source_user_id="plex-user-77",
                username="plex_user",
                username_normalized="plex_user",
                email="plex_user@example.com",
                display_name="Plex User",
                raw={"source": "plex"},
            )
            await resolve_or_create_user_for_identity(db_session, identity=identity)
            await persist_plex_identity_token(
                db_session,
                identity=identity,
                plex_user_token="user-token-abc",
            )
            await db_session.commit()

            row = (
                await db_session.execute(
                    select(MediaUserIdentity).where(
                        MediaUserIdentity.source_service == Service.PLEX,
                        MediaUserIdentity.source_service_config_id == plex.id,
                        MediaUserIdentity.source_user_id == "plex-user-77",
                    )
                )
            ).scalar_one()
            assert row.plex_auth_token is not None
            assert fer_decrypt(row.plex_auth_token) == "user-token-abc"
            assert row.plex_auth_token_updated_at is not None

        await engine.dispose()

    asyncio.run(run())
