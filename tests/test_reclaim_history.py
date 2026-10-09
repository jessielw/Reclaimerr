from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.media import get_reclaim_history
from backend.database import Base
from backend.database.models import ReclaimHistory, User
from backend.enums import MediaType, UserRole


def test_get_reclaim_history_includes_optional_attributes() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            user = User(username="viewer", password_hash="x", role=UserRole.USER)
            db_session.add(user)
            db_session.add_all(
                [
                    ReclaimHistory(
                        approved_by="manager",
                        media_type=MediaType.MOVIE,
                        tmdb_id=1,
                        name="Modern Movie",
                        size=123,
                        attributes={
                            "resolution": "2160p",
                            "hdr": True,
                            "dolby_vision": True,
                            "sonarr_monitor_new_seasons": "failed",
                            "sonarr_monitor_new_seasons_error": "Sonarr config 1: unavailable",
                        },
                    ),
                    ReclaimHistory(
                        approved_by="manager",
                        media_type=MediaType.SERIES,
                        tmdb_id=2,
                        name="Legacy Show",
                        size=456,
                        attributes=None,
                    ),
                ]
            )
            await db_session.commit()

            response = await get_reclaim_history(
                user,
                db_session,
                page=1,
                per_page=25,
                media_type=None,
                search=None,
                sort_order="desc",
            )

            assert response.total == 2
            modern = next(
                item for item in response.items if item.name == "Modern Movie"
            )
            legacy = next(item for item in response.items if item.name == "Legacy Show")
            assert modern.attributes is not None
            assert modern.attributes.resolution == "2160p"
            assert modern.attributes.hdr is True
            assert modern.attributes.dolby_vision is True
            assert modern.attributes.sonarr_monitor_new_seasons == "failed"
            assert (
                modern.attributes.sonarr_monitor_new_seasons_error
                == "Sonarr config 1: unavailable"
            )
            assert legacy.attributes is None

        await engine.dispose()

    asyncio.run(run())


def test_reclaim_history_export_filters_and_escapes_formulas() -> None:
    import csv
    import io

    from backend.api.routes.media import export_reclaim_history

    async def run() -> list[list[str]]:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            user = User(username="viewer", password_hash="x", role=UserRole.USER)
            db_session.add_all(
                [
                    ReclaimHistory(
                        approved_by="manager",
                        media_type=MediaType.MOVIE,
                        tmdb_id=1,
                        name='=HYPERLINK("http://evil","Dune")',
                        size=2 * 1024**3,
                        path="/movies/Dune (2021)/Dune.mkv",
                    ),
                    ReclaimHistory(
                        approved_by="manager",
                        media_type=MediaType.SERIES,
                        tmdb_id=2,
                        name="Some Show",
                        size=456,
                    ),
                ]
            )
            await db_session.commit()
            response = await export_reclaim_history(
                user,
                db_session,
                media_type=MediaType.MOVIE,
                search=None,
                sort_order="desc",
            )
        await engine.dispose()
        assert response.headers["content-type"].startswith("text/csv")
        return list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))

    header, *rows = asyncio.run(run())
    assert header[1:4] == ["Title", "Type", "Action"]
    assert len(rows) == 1
    title, media_type, action, size, approved_by, path = rows[0][1:7]
    assert title == '\'=HYPERLINK("http://evil","Dune")'
    assert (media_type, action, size, approved_by) == (
        "movie",
        "deleted",
        "2.0",
        "manager",
    )
    assert path == "/movies/Dune (2021)/Dune.mkv"
