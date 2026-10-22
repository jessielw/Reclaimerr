from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.api.routes.setup as setup_routes
from backend.core.setup_state import setup_state
from backend.database import Base
from backend.database.models import User
from backend.models.setup import SetupRequest


def test_concurrent_setup_requests_create_one_admin(monkeypatch) -> None:
    # Unauthenticated first-run route: a second request racing the first must be
    # refused rather than slip past the needs_setup check.
    monkeypatch.setattr(setup_state, "needs_setup", True)
    monkeypatch.setattr(setup_routes, "_setup_lock", asyncio.Lock(), raising=True)

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        body = SetupRequest(
            password="a-Strong-passw0rd!", confirm_password="a-Strong-passw0rd!"
        )

        async def attempt() -> None:
            async with session_maker() as db:
                await setup_routes.complete_setup(body, db)

        try:
            results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            assert len(failures) == 1
            assert isinstance(failures[0], HTTPException)
            assert failures[0].status_code == 409

            async with session_maker() as db:
                assert await db.scalar(select(func.count()).select_from(User)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_setup_is_refused_once_complete(monkeypatch) -> None:
    monkeypatch.setattr(setup_state, "needs_setup", False)
    body = SetupRequest(password="x", confirm_password="y")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(setup_routes.complete_setup(body, None))  # type: ignore[arg-type]
    assert exc.value.status_code == 409
