from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings.general import (
    get_general_settings,
    update_general_settings,
)
from backend.database import Base
from backend.database.models import GeneralSettings, User
from backend.enums import UserRole
from backend.models.settings import GeneralSettingsResponse


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        permissions=[],
    )


def test_general_settings_application_url_round_trips() -> None:
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
            admin = _admin_user()
            db_session.add(admin)
            await db_session.commit()
            await db_session.refresh(admin)

            settings_before = await get_general_settings(admin, db_session)
            assert settings_before.application_url is None

            response = await update_general_settings(
                request=GeneralSettingsResponse(
                    application_url=" https://public.example.com/ ",
                ),
                admin=admin,
                db=db_session,
            )

            assert response.application_url == "https://public.example.com"

            row = (await db_session.execute(select(GeneralSettings))).scalar_one()
            assert row.application_url == "https://public.example.com"

            settings_after = await get_general_settings(admin, db_session)
            assert settings_after.application_url == "https://public.example.com"

        await engine.dispose()

    asyncio.run(run())


def test_general_settings_application_url_rejects_non_http_urls() -> None:
    with pytest.raises(ValidationError) as exc:
        GeneralSettingsResponse(application_url="example.com")

    assert "Application URL must be an absolute http:// or https:// URL" in str(
        exc.value
    )
