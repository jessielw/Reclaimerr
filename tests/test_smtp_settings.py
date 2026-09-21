from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings.smtp import (
    enable_email_for_all_users,
    get_smtp_coverage,
    get_smtp_settings,
    update_smtp_settings,
)
from backend.core.auth import require_admin
from backend.core.encryption import fer_decrypt
from backend.database import Base
from backend.database.models import NotificationSetting, SMTPSettings, User
from backend.enums import NotificationChannel, UserRole
from backend.models.settings import (
    NotificationSettingItem,
    SMTPSettingsUpdate,
)

T = TypeVar("T")


def _run(test: Callable[[AsyncSession, User], Awaitable[T]]) -> T:
    """Run a coroutine against a throwaway in-memory database."""

    async def main() -> T:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            session_maker = async_sessionmaker(
                engine, expire_on_commit=False, class_=AsyncSession
            )
            async with session_maker() as session:
                admin = User(
                    username="admin",
                    password_hash="hashed",
                    email="admin@example.com",
                    role=UserRole.ADMIN,
                    permissions=[],
                )
                session.add(admin)
                await session.commit()
                await session.refresh(admin)
                return await test(session, admin)
        finally:
            # aiosqlite runs each connection on its own thread. Without this the
            # thread outlives asyncio.run() and resolves its future against a
            # closed loop, which pytest reports as an unhandled thread exception.
            await engine.dispose()

    return asyncio.run(main())


def _valid_update(**overrides: object) -> SMTPSettingsUpdate:
    base: dict[str, object] = {
        "enabled": True,
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "notify@example.com",
        "password": "hunter2",
        "from_address": "notify@example.com",
        "from_name": "Reclaimerr",
    }
    base.update(overrides)
    return SMTPSettingsUpdate(**base)  # pyright: ignore[reportArgumentType]


def test_get_creates_the_row_and_hides_the_password() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        before = await get_smtp_settings(admin, session)
        assert before.enabled is False
        assert before.password_configured is False
        # the response model has no password field at all to leak
        assert "password" not in before.model_dump()

        after = await update_smtp_settings(_valid_update(), admin, session)
        assert after.password_configured is True
        assert "password" not in after.model_dump()

    _run(run)


def test_any_admin_can_read_and_update_the_settings() -> None:
    """SMTP config is admin-wide, not reserved to the first/bootstrap admin.

    Nothing in the codebase distinguishes an "owner" from any other admin, and
    nothing here should start: a second admin must be able to take over the
    mail configuration, and the row records which one of them did it.
    """

    async def run(session: AsyncSession, first_admin: User) -> None:
        second_admin = User(
            username="second_admin",
            password_hash="hashed",
            email="second@example.com",
            role=UserRole.ADMIN,
            permissions=[],
        )
        regular = User(
            username="jess",
            password_hash="hashed",
            role=UserRole.USER,
            permissions=[],
        )
        session.add_all([second_admin, regular])
        await session.commit()
        await session.refresh(second_admin)

        await update_smtp_settings(
            _valid_update(host="first.example.com"), first_admin, session
        )
        response = await update_smtp_settings(
            _valid_update(host="second.example.com", password=None),
            second_admin,
            session,
        )

        assert response.host == "second.example.com"
        # the password the first admin saved is still usable by the second
        assert response.password_configured is True
        assert (await get_smtp_settings(second_admin, session)).host == (
            "second.example.com"
        )

        row = (await session.execute(select(SMTPSettings))).scalar_one()
        assert row.updated_by_user_id == second_admin.id

        # and it is still closed to everyone who is not an admin
        with pytest.raises(HTTPException) as exc:
            await require_admin(regular)
        assert exc.value.status_code == 403

    _run(run)


def test_password_is_encrypted_at_rest() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        await update_smtp_settings(_valid_update(), admin, session)

        row = (await session.execute(select(SMTPSettings))).scalar_one()
        assert row.password != "hunter2"
        assert fer_decrypt(row.password) == "hunter2"
        assert row.updated_by_user_id == admin.id

    _run(run)


def test_null_password_keeps_the_stored_one() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        await update_smtp_settings(_valid_update(), admin, session)
        response = await update_smtp_settings(
            _valid_update(password=None, from_name="Reclaimerr Mail"), admin, session
        )

        assert response.from_name == "Reclaimerr Mail"
        assert response.password_configured is True
        row = (await session.execute(select(SMTPSettings))).scalar_one()
        assert fer_decrypt(row.password) == "hunter2"

    _run(run)


def test_blank_password_keeps_the_stored_one() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        await update_smtp_settings(_valid_update(), admin, session)
        # An empty string normalizes to None, which means "keep", so a blank
        # box never wipes a saved password by accident. Moving to an
        # unauthenticated relay is done by clearing the username instead, and
        # build_mailto_url then omits the credentials entirely.
        response = await update_smtp_settings(
            _valid_update(password=""), admin, session
        )

        assert response.password_configured is True

    _run(run)


def test_clearing_the_username_drops_the_credentials_from_the_url() -> None:
    from backend.services.smtp import build_mailto_url, load_smtp_config

    async def run(session: AsyncSession, admin: User) -> None:
        await update_smtp_settings(_valid_update(), admin, session)
        await update_smtp_settings(
            _valid_update(username="", password=None, security="insecure", port=25),
            admin,
            session,
        )

        config = await load_smtp_config(session)
        assert config is not None
        url = build_mailto_url(config, "user@inbox.net")
        assert "hunter2" not in url
        assert "@smtp.example.com" not in url

    _run(run)


@pytest.mark.parametrize("missing", ["host", "from_address"])
def test_enabling_without_required_fields_is_rejected(missing: str) -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        with pytest.raises(HTTPException) as exc:
            await update_smtp_settings(_valid_update(**{missing: ""}), admin, session)
        assert exc.value.status_code == 400

    _run(run)


def test_disabled_settings_may_be_saved_incomplete() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        response = await update_smtp_settings(
            _valid_update(enabled=False, host="", from_address=""), admin, session
        )
        assert response.enabled is False
        assert response.host == ""

    _run(run)


def test_invalid_from_address_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _valid_update(from_address="not-an-address")


def test_coverage_counts_only_active_users_with_an_email() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        session.add_all(
            [
                User(username="a", password_hash="h", email="a@example.com"),
                User(username="b", password_hash="h", email=None),
                User(
                    username="c",
                    password_hash="h",
                    email="c@example.com",
                    is_active=False,
                ),
            ]
        )
        await session.commit()

        coverage = await get_smtp_coverage(admin, session)
        # admin + a are active with an address; b has none, c is inactive
        assert coverage.total_users == 3
        assert coverage.with_email == 2
        assert coverage.already_enabled == 0
        assert coverage.eligible == 2

    _run(run)


def test_enable_all_seeds_destinations_and_is_idempotent() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        session.add_all(
            [
                User(username="a", password_hash="h", email="a@example.com"),
                User(username="b", password_hash="h", email=None),
            ]
        )
        await session.commit()

        first = await enable_email_for_all_users(admin, session)
        assert first.created == 2
        assert first.skipped_no_email == 1

        second = await enable_email_for_all_users(admin, session)
        assert second.created == 0

        rows = (
            (
                await session.execute(
                    select(NotificationSetting).where(
                        NotificationSetting.channel
                        == NotificationChannel.SYSTEM_EMAIL
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(row.url is None for row in rows)
        assert all(row.enabled for row in rows)
        assert all(row.new_cleanup_candidates for row in rows)

        coverage = await get_smtp_coverage(admin, session)
        assert coverage.eligible == 0
        assert coverage.already_enabled == 2

    _run(run)


def test_enable_all_gives_admins_the_admin_only_types() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        session.add(User(username="a", password_hash="h", email="a@example.com"))
        await session.commit()

        await enable_email_for_all_users(admin, session)

        rows = {
            row.user_id: row
            for row in (
                (
                    await session.execute(
                        select(NotificationSetting).where(
                            NotificationSetting.channel
                            == NotificationChannel.SYSTEM_EMAIL
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        assert rows[admin.id].task_failure is True
        regular = next(row for uid, row in rows.items() if uid != admin.id)
        assert regular.task_failure is False
        assert regular.request_approved is True

    _run(run)


def test_enable_all_leaves_an_existing_destination_alone() -> None:
    async def run(session: AsyncSession, admin: User) -> None:
        session.add(
            NotificationSetting(
                user_id=admin.id,
                enabled=False,
                name="Email",
                channel=NotificationChannel.SYSTEM_EMAIL,
                target_email="custom@example.com",
            )
        )
        await session.commit()

        result = await enable_email_for_all_users(admin, session)
        assert result.created == 0

        row = (
            (
                await session.execute(
                    select(NotificationSetting).where(
                        NotificationSetting.user_id == admin.id
                    )
                )
            )
            .scalars()
            .one()
        )
        assert row.enabled is False
        assert row.target_email == "custom@example.com"

    _run(run)


def test_email_destination_needs_no_url() -> None:
    item = NotificationSettingItem(channel=NotificationChannel.SYSTEM_EMAIL)

    assert item.url is None
    assert item.target_email is None


def test_email_destination_drops_a_supplied_url() -> None:
    item = NotificationSettingItem(
        channel=NotificationChannel.SYSTEM_EMAIL,
        url="discord://token",
        target_email="  someone@example.com  ",
    )

    assert item.url is None
    assert item.target_email == "someone@example.com"


def test_apprise_destination_requires_a_url() -> None:
    with pytest.raises(ValidationError):
        NotificationSettingItem()


def test_apprise_destination_drops_a_target_email() -> None:
    item = NotificationSettingItem(
        url="discord://token", target_email="someone@example.com"
    )

    assert item.target_email is None


def test_email_destination_rejects_a_malformed_address() -> None:
    with pytest.raises(ValidationError):
        NotificationSettingItem(
            channel=NotificationChannel.SYSTEM_EMAIL, target_email="nope"
        )
