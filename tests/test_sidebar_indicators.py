from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.info.info import get_sidebar_indicators, get_ui_indicators
from backend.database import Base
from backend.database.models import (
    AppUpdateState,
    DeleteRequest,
    ProtectionRequest,
    ReclaimCandidate,
    User,
)
from backend.enums import MediaType, Permission, ProtectionRequestStatus, UserRole


def _user(
    username: str,
    *,
    role: UserRole = UserRole.USER,
    permissions: list[str] | None = None,
) -> User:
    return User(
        username=username,
        password_hash="x",
        role=role,
        permissions=permissions or [],
    )


def _candidate() -> ReclaimCandidate:
    return ReclaimCandidate(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[1],
        matched_criteria={},
        reason="candidate",
        reason_data=[],
    )


def _protection_request(
    user_id: int,
    *,
    status: ProtectionRequestStatus = ProtectionRequestStatus.PENDING,
) -> ProtectionRequest:
    return ProtectionRequest(
        media_type=MediaType.MOVIE,
        requested_by_user_id=user_id,
        status=status,
    )


def _delete_request(
    user_id: int,
    *,
    status: ProtectionRequestStatus = ProtectionRequestStatus.PENDING,
) -> DeleteRequest:
    return DeleteRequest(
        media_type=MediaType.SERIES,
        requested_by_user_id=user_id,
        status=status,
    )


def test_sidebar_indicators_empty_and_candidates_presence() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = _user("viewer")
            db_session.add(user)
            await db_session.commit()
            await db_session.refresh(user)

            empty_response = await get_sidebar_indicators(user, db_session)
            assert empty_response.has_candidates is False
            assert empty_response.has_pending_requests is False
            assert empty_response.has_pending_protection_requests is False
            assert empty_response.has_pending_delete_requests is False

            db_session.add(_candidate())
            await db_session.commit()

            populated_response = await get_sidebar_indicators(user, db_session)
            assert populated_response.has_candidates is True
            assert populated_response.has_pending_requests is False
            assert populated_response.has_pending_protection_requests is False
            assert populated_response.has_pending_delete_requests is False

        await engine.dispose()

    asyncio.run(run())


def test_sidebar_indicators_pending_requests_are_role_aware() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            manager = _user(
                "manager",
                permissions=[Permission.MANAGE_REQUESTS.value],
            )
            user_a = _user("user_a")
            user_b = _user("user_b")
            user_c = _user("user_c")
            db_session.add_all([manager, user_a, user_b, user_c])
            await db_session.flush()

            db_session.add_all(
                [
                    _protection_request(user_a.id),
                    _protection_request(
                        user_a.id, status=ProtectionRequestStatus.DENIED
                    ),
                    _delete_request(user_b.id),
                    _delete_request(user_b.id, status=ProtectionRequestStatus.APPROVED),
                ]
            )
            await db_session.commit()

            manager_response = await get_sidebar_indicators(manager, db_session)
            assert manager_response.has_pending_requests is True
            assert manager_response.has_pending_protection_requests is True
            assert manager_response.has_pending_delete_requests is True

            user_a_response = await get_sidebar_indicators(user_a, db_session)
            assert user_a_response.has_pending_requests is True
            assert user_a_response.has_pending_protection_requests is True
            assert user_a_response.has_pending_delete_requests is False

            user_b_response = await get_sidebar_indicators(user_b, db_session)
            assert user_b_response.has_pending_requests is True
            assert user_b_response.has_pending_protection_requests is False
            assert user_b_response.has_pending_delete_requests is True

            user_c_response = await get_sidebar_indicators(user_c, db_session)
            assert user_c_response.has_pending_requests is False
            assert user_c_response.has_pending_protection_requests is False
            assert user_c_response.has_pending_delete_requests is False

        await engine.dispose()

    asyncio.run(run())


def test_ui_indicators_includes_update_status_with_role_visibility() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            admin = _user("admin", role=UserRole.ADMIN)
            user = _user("viewer")
            db_session.add_all([admin, user])
            await db_session.flush()

            checked_at = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
            db_session.add(
                AppUpdateState(
                    current_version="0.1.0-beta.21",
                    latest_version="0.1.0-beta.22",
                    latest_release_url="https://example.com/release",
                    last_checked_at=checked_at,
                    update_available=True,
                )
            )
            await db_session.commit()

            admin_response = await get_ui_indicators(admin, db_session)
            assert admin_response.update_available is True
            assert admin_response.latest_version == "0.1.0-beta.22"
            assert admin_response.latest_release_url == "https://example.com/release"
            assert admin_response.last_checked_at is not None

            user_response = await get_ui_indicators(user, db_session)
            assert user_response.update_available is False
            assert user_response.latest_version is None
            assert user_response.latest_release_url is None
            assert user_response.last_checked_at is not None

        await engine.dispose()

    asyncio.run(run())
