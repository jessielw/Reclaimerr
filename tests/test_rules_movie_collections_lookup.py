from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import get_genres, get_movie_collections
from backend.database import Base
from backend.database.models import Movie, Series, User
from backend.enums import MediaType, UserRole


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="hash",
        role=UserRole.ADMIN,
        permissions=[],
    )


def test_get_movie_collections_paginates_and_counts() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            db.add_all(
                [
                    Movie(
                        title="A1",
                        tmdb_id=1001,
                        tmdb_collection_id=1,
                        tmdb_collection_name="Alpha Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="A2",
                        tmdb_id=1002,
                        tmdb_collection_id=1,
                        tmdb_collection_name="Alpha Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="B1",
                        tmdb_id=1003,
                        tmdb_collection_id=2,
                        tmdb_collection_name="Beta Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="G1",
                        tmdb_id=1004,
                        tmdb_collection_id=3,
                        tmdb_collection_name="Gamma Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="G2",
                        tmdb_id=1005,
                        tmdb_collection_id=3,
                        tmdb_collection_name="Gamma Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="G3",
                        tmdb_id=1006,
                        tmdb_collection_id=3,
                        tmdb_collection_name="Gamma Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="NoCollection",
                        tmdb_id=1007,
                        tmdb_collection_id=None,
                        tmdb_collection_name=None,
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="BlankCollection",
                        tmdb_id=1008,
                        tmdb_collection_id=10,
                        tmdb_collection_name="   ",
                        tmdb_collection_checked=True,
                    ),
                ]
            )
            removed_alpha = Movie(
                title="RemovedAlpha",
                tmdb_id=1009,
                tmdb_collection_id=1,
                tmdb_collection_name="Alpha Collection",
                tmdb_collection_checked=True,
            )
            removed_alpha.removed_at = datetime(2026, 1, 1, tzinfo=UTC)
            db.add(removed_alpha)
            await db.commit()

            page_one = await get_movie_collections(
                admin,
                db,
                q="",
                page=1,
                per_page=2,
            )
            assert page_one.total == 3
            assert page_one.total_pages == 2
            assert [item.name for item in page_one.items] == [
                "Alpha Collection",
                "Beta Collection",
            ]
            assert [item.movie_count for item in page_one.items] == [2, 1]

            page_two = await get_movie_collections(
                admin,
                db,
                q="",
                page=2,
                per_page=2,
            )
            assert page_two.total == 3
            assert page_two.total_pages == 2
            assert len(page_two.items) == 1
            assert page_two.items[0].name == "Gamma Collection"
            assert page_two.items[0].movie_count == 3

        await engine.dispose()

    asyncio.run(run())


def test_get_movie_collections_search_is_case_insensitive() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            db.add_all(
                [
                    Movie(
                        title="One",
                        tmdb_id=2001,
                        tmdb_collection_id=1,
                        tmdb_collection_name="Star Wars Collection",
                        tmdb_collection_checked=True,
                    ),
                    Movie(
                        title="Two",
                        tmdb_id=2002,
                        tmdb_collection_id=2,
                        tmdb_collection_name="Die Hard Collection",
                        tmdb_collection_checked=True,
                    ),
                ]
            )
            await db.commit()

            response = await get_movie_collections(
                admin,
                db,
                q="sTaR",
                page=1,
                per_page=50,
            )
            assert response.total == 1
            assert len(response.items) == 1
            assert response.items[0].name == "Star Wars Collection"
            assert response.items[0].movie_count == 1

        await engine.dispose()

    asyncio.run(run())


def test_get_genres_paginates_counts_and_filters_by_media_type() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            action_one = Movie(title="Action One", tmdb_id=3001)
            action_two = Movie(title="Action Two", tmdb_id=3002)
            drama_show = Series(title="Drama Show", tmdb_id=4001)
            cast(Any, action_one).genres = [
                {"id": 28, "name": "Action"},
                {"id": 35, "name": "Comedy"},
            ]
            cast(Any, action_two).genres = [
                {"id": 28, "name": "action"},
                {"id": 878, "name": "Science Fiction"},
            ]
            cast(Any, drama_show).genres = [{"id": 18, "name": "Drama"}]
            db.add_all([action_one, action_two, drama_show])
            removed_movie = Movie(
                title="Removed Action",
                tmdb_id=3003,
            )
            cast(Any, removed_movie).genres = [{"id": 28, "name": "Action"}]
            removed_movie.removed_at = datetime(2026, 1, 1, tzinfo=UTC)
            db.add(removed_movie)
            await db.commit()

            page_one = await get_genres(
                admin,
                db,
                media_type=MediaType.MOVIE,
                q="",
                page=1,
                per_page=2,
            )
            assert page_one.total == 3
            assert page_one.total_pages == 2
            assert [(item.name, item.media_count) for item in page_one.items] == [
                ("Action", 2),
                ("Comedy", 1),
            ]

            search = await get_genres(
                admin,
                db,
                media_type=MediaType.MOVIE,
                q="science",
                page=1,
                per_page=50,
            )
            assert search.total == 1
            assert search.items[0].name == "Science Fiction"
            assert search.items[0].media_count == 1

            series_response = await get_genres(
                admin,
                db,
                media_type=MediaType.SERIES,
                q="",
                page=1,
                per_page=50,
            )
            assert series_response.total == 1
            assert series_response.items[0].name == "Drama"

        await engine.dispose()

    asyncio.run(run())
