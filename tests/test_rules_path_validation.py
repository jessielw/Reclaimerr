from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import create_rule, validate_paths_against_scope
from backend.database import Base
from backend.database.models import Movie, MovieVersion, User
from backend.enums import MediaType, Service, UserRole
from backend.models.cleanup import CleanupRuleCreate
from backend.models.rules import (
    ValidatePathCondition,
    ValidatePathsRequest,
)


def _definition(field: str, operator: str, value: object = 1) -> dict[str, object]:
    condition: dict[str, object] = {
        "type": "condition",
        "field": field,
        "operator": operator,
    }
    if operator not in {"exists", "not_exists", "is_true", "is_false"}:
        condition["value"] = value
    return {
        "version": 1,
        "root": {"type": "group", "op": "and", "children": [condition]},
    }


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="hash",
        role=UserRole.ADMIN,
        permissions=[],
    )


async def _seed_movie_version(
    db: AsyncSession,
    *,
    path: str,
    file_name: str | None = None,
) -> None:
    movie = Movie(title="Movie", tmdb_id=9001, size=1024)
    db.add(movie)
    await db.flush()
    db.add(
        MovieVersion(
            movie_id=movie.id,
            service=Service.PLEX,
            service_item_id="plex-item-1",
            service_media_id="plex-media-1",
            library_id="lib-1",
            library_name="Movies",
            path=path,
            file_name=file_name or path.rsplit("/", 1)[-1],
        )
    )
    await db.commit()


def test_create_rule_accepts_literal_media_path_with_regex_chars() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        literal_path = "/media/movies/[4K]/Movie.Title.2024.mkv"

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            await db.commit()

            await _seed_movie_version(db, path=literal_path)

            payload = CleanupRuleCreate(
                name="literal-path-equals",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition("media.path", "equals", literal_path),
                action=None,
            )

            created = await create_rule(payload, admin, db)
            rule_condition = created.definition["root"]["children"][0]
            assert rule_condition["field"] == "media.path"
            assert rule_condition["operator"] == "equals"
            assert rule_condition["value"] == literal_path

        await engine.dispose()

    asyncio.run(run())


def test_create_rule_accepts_literal_media_path_folder_prefix() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        media_path = "/media/movies/action/Example.Movie.2025.mkv"
        folder_path = "/media/movies/action"

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            await db.commit()

            await _seed_movie_version(db, path=media_path)

            payload = CleanupRuleCreate(
                name="literal-path-folder-prefix",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition("media.path", "equals", folder_path),
                action=None,
            )

            created = await create_rule(payload, admin, db)
            rule_condition = created.definition["root"]["children"][0]
            assert rule_condition["field"] == "media.path"
            assert rule_condition["operator"] == "equals"
            assert rule_condition["value"] == folder_path

        await engine.dispose()

    asyncio.run(run())


def test_create_rule_accepts_literal_media_filename() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        path = "/media/movies/Example.Movie.2025.mkv"

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            await db.commit()

            await _seed_movie_version(db, path=path)

            payload = CleanupRuleCreate(
                name="literal-file-name-equals",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition(
                    "media.file_name", "equals", "Example.Movie.2025.mkv"
                ),
                action=None,
            )

            created = await create_rule(payload, admin, db)
            rule_condition = created.definition["root"]["children"][0]
            assert rule_condition["field"] == "media.file_name"
            assert rule_condition["operator"] == "equals"
            assert rule_condition["value"] == "Example.Movie.2025.mkv"

        await engine.dispose()

    asyncio.run(run())


def test_create_rule_rejects_invalid_path_regex() -> None:
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
            await db.commit()

            await _seed_movie_version(db, path="/media/movies/Example.Movie.2025.mkv")

            payload = CleanupRuleCreate(
                name="bad-regex",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition("media.path", "matches_any_regex", "[invalid"),
                action=None,
            )

            with pytest.raises(HTTPException) as exc:
                await create_rule(payload, admin, db)

            assert exc.value.status_code == 400
            assert "Invalid regex pattern" in str(exc.value.detail)

        await engine.dispose()

    asyncio.run(run())


def test_create_rule_skips_existence_checks_for_path_negation_operator() -> None:
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
            await db.commit()

            payload = CleanupRuleCreate(
                name="path-not-in",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition(
                    "media.path", "not_in", ["/media/movies/does-not-exist.mkv"]
                ),
                action=None,
            )

            created = await create_rule(payload, admin, db)
            rule_condition = created.definition["root"]["children"][0]
            assert rule_condition["operator"] == "not_in"

        await engine.dispose()

    asyncio.run(run())


def test_validate_paths_endpoint_supports_structured_and_legacy_payloads() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        media_path = "/media/movies/Example.Movie.2026.mkv"

        async with session_maker() as db:
            admin = _admin_user()
            db.add(admin)
            await db.commit()

            await _seed_movie_version(db, path=media_path)

            structured = ValidatePathsRequest(
                media_type=MediaType.MOVIE,
                library_ids=["lib-1"],
                conditions=[
                    ValidatePathCondition(
                        field="media.path",
                        operator="equals",
                        value=media_path,
                    ),
                    ValidatePathCondition(
                        field="media.file_name",
                        operator="equals",
                        value="Example.Movie.2026.mkv",
                    ),
                    ValidatePathCondition(
                        field="media.path",
                        operator="matches_any_regex",
                        value="[invalid",
                    ),
                    ValidatePathCondition(
                        field="media.path",
                        operator="not_in",
                        value="/media/movies/never-used.mkv",
                    ),
                ],
            )

            structured_result = await validate_paths_against_scope(
                structured, admin, db
            )
            invalid_values = {
                entry.value for entry in structured_result.invalid_conditions
            }
            assert invalid_values == {"[invalid"}
            valid_entries = {
                (entry.field, entry.operator, entry.value)
                for entry in structured_result.valid_conditions
            }
            assert (
                "media.path",
                "equals",
                media_path,
            ) in valid_entries
            assert (
                "media.file_name",
                "equals",
                "Example.Movie.2026.mkv",
            ) in valid_entries
            assert (
                "media.path",
                "not_in",
                "/media/movies/never-used.mkv",
            ) in valid_entries

            folder_criterion = ValidatePathsRequest(
                media_type=MediaType.MOVIE,
                library_ids=["lib-1"],
                conditions=[
                    ValidatePathCondition(
                        field="media.path",
                        operator="equals",
                        value="/media/movies",
                    )
                ],
            )
            folder_result = await validate_paths_against_scope(
                folder_criterion, admin, db
            )
            folder_valid = {
                (entry.field, entry.operator, entry.value)
                for entry in folder_result.valid_conditions
            }
            assert ("media.path", "equals", "/media/movies") in folder_valid

            legacy = ValidatePathsRequest(
                media_type=MediaType.MOVIE,
                library_ids=["lib-1"],
                paths=[r"example\.movie\.2026", r"does-not-match"],
            )
            legacy_result = await validate_paths_against_scope(legacy, admin, db)
            assert legacy_result.valid_paths == [r"example\.movie\.2026"]
            assert legacy_result.invalid_paths == [r"does-not-match"]

        await engine.dispose()

    asyncio.run(run())
