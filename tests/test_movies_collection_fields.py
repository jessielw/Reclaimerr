from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.media import get_movies
from backend.database import Base
from backend.database.models import Movie, User
from backend.enums import UserRole
from backend.models.media import MovieWithStatus


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="hash",
        role=UserRole.ADMIN,
        permissions=[],
    )


def test_get_movies_includes_collection_fields() -> None:
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

            movie_in_collection = Movie(
                title="Collection Movie",
                tmdb_id=3001,
                tmdb_collection_id=99,
                tmdb_collection_name="Collection Name",
                tmdb_collection_checked=True,
            )
            movie_unknown = Movie(
                title="Unknown Collection State",
                tmdb_id=3002,
                tmdb_collection_id=None,
                tmdb_collection_name=None,
                tmdb_collection_checked=False,
            )
            db.add_all([movie_in_collection, movie_unknown])
            await db.commit()

            response = await get_movies(
                admin,
                db,
                page=1,
                per_page=50,
                sort_by="title",
                sort_order="asc",
                search=None,
                candidates_only=False,
            )
            assert response.total == 2

            by_title = {item.title: item for item in response.items}
            in_collection = by_title["Collection Movie"]
            assert isinstance(in_collection, MovieWithStatus)
            assert in_collection.tmdb_collection_id == 99
            assert in_collection.tmdb_collection_name == "Collection Name"
            assert in_collection.tmdb_in_collection is True

            unknown = by_title["Unknown Collection State"]
            assert isinstance(unknown, MovieWithStatus)
            assert unknown.tmdb_collection_id is None
            assert unknown.tmdb_collection_name is None
            assert unknown.tmdb_in_collection is None

        await engine.dispose()

    asyncio.run(run())
