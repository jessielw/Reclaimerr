"""What happens around a library the admin unchecked in Settings -> Media Servers.

Its media leaving Reclaimerr is the point of unchecking it, so the soft-delete
guard must not hold that back for a second sync. And a rule that only looks at
such a library quietly matches nothing, so an admin notice says so.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.api.routes.settings.services import update_library_selections
from backend.database import Base
from backend.database.models import (
    AdminNotice,
    Movie,
    MovieVersion,
    ReclaimRule,
    Series,
    SeriesServiceRef,
    ServiceConfig,
    ServiceMediaLibrary,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.settings import LibrarySelectionUpdate
from backend.services.admin_notices import NOTICE_KEY_UNCHECKED_LIBRARY_RULES
from backend.tasks.sync import _rows_to_soft_delete_now


async def _make_db() -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _seed_main(db: AsyncSession, *, discover_selected: bool) -> int:
    main = ServiceConfig(
        service_type=Service.JELLYFIN,
        name="Jellyfin",
        base_url="http://unused",
        api_key="unused",
        enabled=True,
        is_main=True,
    )
    db.add(main)
    await db.flush()
    for media_type in (MediaType.MOVIE, MediaType.SERIES):
        db.add_all(
            [
                ServiceMediaLibrary(
                    library_id=f"real-{media_type.value}",
                    library_name="Real",
                    media_type=media_type,
                    service_config_id=main.id,
                ),
                ServiceMediaLibrary(
                    library_id=f"discover-{media_type.value}",
                    library_name="Discover",
                    media_type=media_type,
                    service_config_id=main.id,
                    selected=discover_selected,
                ),
            ]
        )
    return main.id


async def _add_title(
    db: AsyncSession, media_type: MediaType, n: int, libraries: list[str]
) -> Any:
    """One movie or series with a stored copy in each named library."""
    if media_type is MediaType.MOVIE:
        row: Any = Movie(title=f"Movie {n}", tmdb_id=n, size=1)
    else:
        row = Series(title=f"Series {n}", tmdb_id=n, size=1)
    db.add(row)
    await db.flush()
    for index, prefix in enumerate(libraries):
        library_id = f"{prefix}-{media_type.value}"
        if media_type is MediaType.MOVIE:
            db.add(
                MovieVersion(
                    movie_id=row.id,
                    service=Service.JELLYFIN,
                    service_item_id=f"item-{n}-{index}",
                    service_media_id=f"media-{n}-{index}",
                    library_id=library_id,
                    library_name=prefix,
                )
            )
        else:
            # one ref per (series, service), so only the first library counts
            if index == 0:
                db.add(
                    SeriesServiceRef(
                        series_id=row.id,
                        service=Service.JELLYFIN,
                        service_id=f"item-{n}",
                        library_id=library_id,
                        library_name=prefix,
                    )
                )
    return row


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
def test_unchecked_library_rows_leave_in_one_sync_while_the_rest_stay_guarded(
    media_type: MediaType,
) -> None:
    async def run() -> None:
        session_maker, engine = await _make_db()
        try:
            async with session_maker() as db:
                config_id = await _seed_main(db, discover_selected=False)
                discover = [
                    await _add_title(db, media_type, n, ["discover"])
                    for n in range(1, 21)
                ]
                real = [
                    await _add_title(db, media_type, n, ["real"])
                    for n in range(101, 141)
                ]
                await db.commit()

                # 20 titles from the unchecked library plus 25 of the 40 real
                # ones: 45 of 60 is far past the guard's ratio
                proposed = [*discover, *real[:25]]
                cleared = await _rows_to_soft_delete_now(
                    db, config_id, media_type, proposed, live_count=60
                )

            # the unchecked library goes now; 25 of the remaining 40 real
            # titles still looks like a partial response and is held back
            assert {row.id for row in cleared} == {row.id for row in discover}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_a_movie_also_in_a_checked_library_is_still_guarded() -> None:
    async def run() -> None:
        session_maker, engine = await _make_db()
        try:
            async with session_maker() as db:
                config_id = await _seed_main(db, discover_selected=False)
                mixed = [
                    await _add_title(db, MediaType.MOVIE, n, ["discover", "real"])
                    for n in range(1, 31)
                ]
                await db.commit()

                cleared = await _rows_to_soft_delete_now(
                    db, config_id, MediaType.MOVIE, mixed, live_count=30
                )

            assert cleared == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def _rule(name: str, operator: str) -> ReclaimRule:
    return ReclaimRule(
        name=name,
        media_type=MediaType.MOVIE,
        definition={
            "version": 1,
            "root": {
                "type": "condition",
                "field": "library.id",
                "operator": operator,
                "value": ["discover-movie"],
            },
        },
        action={"outcome": "candidate"},
        enabled=True,
    )


def test_notice_names_rules_that_need_an_unchecked_library() -> None:
    async def run() -> None:
        session_maker, engine = await _make_db()
        admin = User(
            username="admin", password_hash="x", role=UserRole.ADMIN, permissions=[]
        )
        try:
            async with session_maker() as db:
                await _seed_main(db, discover_selected=True)
                db.add_all(
                    [
                        _rule("Discover only", "contains_any"),
                        _rule("Skip discover", "not_in"),
                    ]
                )
                await db.commit()
                discover_id = await db.scalar(
                    select(ServiceMediaLibrary.id).where(
                        ServiceMediaLibrary.library_id == "discover-movie"
                    )
                )
                assert discover_id is not None

            async def notice() -> AdminNotice | None:
                async with session_maker() as db:
                    return await db.scalar(
                        select(AdminNotice).where(
                            AdminNotice.dedupe_key
                            == NOTICE_KEY_UNCHECKED_LIBRARY_RULES,
                            AdminNotice.is_active.is_(True),
                        )
                    )

            async def set_discover(selected: bool) -> None:
                async with session_maker() as db:
                    await update_library_selections(
                        [LibrarySelectionUpdate(id=discover_id, selected=selected)],
                        admin,
                        db,
                    )

            await set_discover(False)
            raised = await notice()
            assert raised is not None
            # excluding a library that no longer syncs still works as written
            assert raised.context_json == {"rule_names": ["Discover only"]}

            await set_discover(True)
            assert await notice() is None
        finally:
            await engine.dispose()

    asyncio.run(run())
