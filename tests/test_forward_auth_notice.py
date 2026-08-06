from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.database import Base
from backend.database.models import AdminNotice
from backend.services.admin_notices import (
    NOTICE_KEY_FORWARD_AUTH_FALLBACK,
    sync_forward_auth_fallback_notice,
)


async def _session_maker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


async def _fetch_notice(db: AsyncSession) -> AdminNotice | None:
    return (
        await db.execute(
            select(AdminNotice).where(
                AdminNotice.dedupe_key == NOTICE_KEY_FORWARD_AUTH_FALLBACK
            )
        )
    ).scalar_one_or_none()


def test_notice_is_raised_while_recovery_mode_is_active() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            await sync_forward_auth_fallback_notice(db, fallback_active=True)
            await db.commit()

            notice = await _fetch_notice(db)
            assert notice is not None
            assert notice.is_active is True
            assert notice.severity == "warning"
            assert "FORWARD_AUTH_ALLOW_LOCAL_FALLBACK" in notice.message
        await engine.dispose()

    asyncio.run(run())


def test_notice_resolves_when_recovery_mode_is_switched_off() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            await sync_forward_auth_fallback_notice(db, fallback_active=True)
            await db.commit()

            await sync_forward_auth_fallback_notice(db, fallback_active=False)
            await db.commit()

            notice = await _fetch_notice(db)
            assert notice is not None
            assert notice.is_active is False
        await engine.dispose()

    asyncio.run(run())


def test_no_notice_is_created_when_recovery_mode_was_never_on() -> None:
    async def run() -> None:
        engine, session_maker = await _session_maker()
        async with session_maker() as db:
            await sync_forward_auth_fallback_notice(db, fallback_active=False)
            await db.commit()

            assert await _fetch_notice(db) is None
        await engine.dispose()

    asyncio.run(run())
