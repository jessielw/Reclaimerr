from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.account import update_profile
from backend.database import Base
from backend.database.models import User
from backend.enums import UserRole
from backend.models.auth import ChangeProfileInfoRequest, UserInfo


def _user() -> User:
    return User(
        username="date-user",
        password_hash="hashed",
        role=UserRole.USER,
        permissions=[],
    )


def test_user_info_defaults_date_format_for_legacy_user_objects() -> None:
    class LegacyUser:
        id = 1
        username = "legacy"
        display_name = None
        email = None
        avatar_path = None
        role = UserRole.USER
        permissions: list[str] = []
        allowed_pages = None
        created_at = datetime.now(UTC)
        password_hash = "hashed"
        require_password_change = False

    # A legacy object lacks the new attribute, but API serialization remains safe.
    assert UserInfo.from_user(LegacyUser()).date_format == "mdy"


def test_date_format_request_validation() -> None:
    assert ChangeProfileInfoRequest(date_format="iso").date_format == "iso"
    with pytest.raises(ValidationError):
        ChangeProfileInfoRequest.model_validate({"date_format": "month-first"})


def test_profile_date_format_is_persisted_without_resetting_profile_fields() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db:
            user = _user()
            user.display_name = "Date User"
            user.email = "date@example.com"
            db.add(user)
            await db.commit()

            response = await update_profile(
                ChangeProfileInfoRequest(date_format="dmy"), user, db
            )
            await db.refresh(user)

            assert response["date_format"] == "dmy"
            assert user.date_format == "dmy"
            assert user.display_name == "Date User"
            assert user.email == "date@example.com"

        await engine.dispose()

    asyncio.run(run())
