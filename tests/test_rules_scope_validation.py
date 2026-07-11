from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import (
    _normalize_rule_action,
    create_rule,
    import_rules,
    preview_rule_matches,
    update_rule,
)
from backend.database import Base
from backend.database.models import ReclaimRule, User
from backend.enums import MediaType, UserRole
from backend.models.cleanup import (
    CleanupRuleCreate,
    CleanupRuleUpdate,
    RuleImportPayload,
)
from backend.models.rules import RulePreviewRequest


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


def test_legacy_rule_action_defaults_to_candidate() -> None:
    action = _normalize_rule_action(None, "Legacy Rule", "movie_version")
    assert action["outcome"] == "candidate"
    assert action["candidate"] is True
    assert action["auto_delete_enabled"] is False
    assert action["auto_delete_delay_days"] is None


@pytest.mark.parametrize(
    ("raw_delay", "expected"),
    [(0, 0), (3650, 3650), (-1, None), (3651, None), (True, None), ("7", None)],
)
def test_candidate_rule_action_normalizes_auto_delete_delay(
    raw_delay: object, expected: int | None
) -> None:
    action = _normalize_rule_action(
        {"auto_delete_delay_days": raw_delay},
        "Candidate Rule",
        "movie_version",
    )

    assert action["auto_delete_delay_days"] == expected


def test_candidate_rule_action_normalizes_auto_delete_enabled() -> None:
    enabled = _normalize_rule_action(
        {"auto_delete_enabled": True},
        "Candidate Rule",
        "movie_version",
    )
    disabled = _normalize_rule_action(
        {"auto_delete_enabled": "true"},
        "Candidate Rule",
        "movie_version",
    )

    assert enabled["auto_delete_enabled"] is True
    assert disabled["auto_delete_enabled"] is False


def test_protection_rule_action_disables_destructive_settings() -> None:
    action = _normalize_rule_action(
        {
            "outcome": "protect",
            "tag_enabled": True,
            "arr_tag": "rec-delete",
            "arr_action": "unmonitor",
            "media_server_action": "delete",
            "radarr_service_config_id": 7,
        },
        "Protect Rule",
        "movie_version",
    )
    assert action["outcome"] == "protect"
    assert action["candidate"] is False
    assert action["tag_enabled"] is False
    assert action["arr_tag"] is None
    assert action["media_server_action"] is None
    assert action["auto_delete_enabled"] is False
    assert action["auto_delete_delay_days"] is None
    assert action["radarr_service_config_id"] is None


def test_create_rule_rejects_incompatible_field_for_target_scope() -> None:
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
                name="bad-movie-scope-rule",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition("season.air_date", "exists"),
                action=None,
            )

            with pytest.raises(HTTPException) as exc:
                await create_rule(payload, admin, db)
            assert exc.value.status_code == 422
            assert "target_scope 'movie_version'" in str(exc.value.detail)
            assert "season.air_date" in str(exc.value.detail)

        await engine.dispose()

    asyncio.run(run())


def test_preview_rule_rejects_incompatible_field_for_target_scope() -> None:
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

            payload = RulePreviewRequest(
                name="bad-preview",
                media_type=MediaType.SERIES,
                target_scope="series",
                definition=_definition("tmdb.release_date", "exists"),
            )

            with pytest.raises(HTTPException) as exc:
                await preview_rule_matches(payload, admin, db)
            assert exc.value.status_code == 422
            assert "target_scope 'series'" in str(exc.value.detail)
            assert "tmdb.release_date" in str(exc.value.detail)

        await engine.dispose()

    asyncio.run(run())


def test_create_rule_rejects_movie_collection_field_for_series_scope() -> None:
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
                name="bad-series-collection-scope-rule",
                media_type=MediaType.SERIES,
                enabled=True,
                target_scope="series",
                definition=_definition(
                    "tmdb.collection_name",
                    "contains_any",
                    ["star wars collection"],
                ),
                action=None,
            )

            with pytest.raises(HTTPException) as exc:
                await create_rule(payload, admin, db)
            assert exc.value.status_code == 422
            assert "target_scope 'series'" in str(exc.value.detail)
            assert "tmdb.collection_name" in str(exc.value.detail)

        await engine.dispose()

    asyncio.run(run())


def test_import_rules_reports_scope_validation_errors() -> None:
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

            payload = RuleImportPayload(
                rules=[
                    CleanupRuleCreate(
                        name="bad-import",
                        media_type=MediaType.MOVIE,
                        enabled=True,
                        target_scope="movie_version",
                        definition=_definition("season.air_date", "exists"),
                        action=None,
                    )
                ]
            )

            result = await import_rules(payload, admin, db)
            assert result.imported == 0
            assert len(result.errors) == 1
            assert "target_scope 'movie_version'" in result.errors[0]
            assert "season.air_date" in result.errors[0]

        await engine.dispose()

    asyncio.run(run())


def test_update_rule_rejects_target_scope_change_with_incompatible_definition() -> None:
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
            await db.flush()

            existing_rule = ReclaimRule(
                name="series-status-rule",
                media_type=MediaType.SERIES,
                enabled=True,
                target_scope="series",
                definition=_definition("series.status", "contains_any", ["Ended"]),
                action={"candidate": True, "media_server_action": "delete"},
            )
            db.add(existing_rule)
            await db.commit()
            await db.refresh(existing_rule)

            payload = CleanupRuleUpdate(target_scope="movie_version")

            with pytest.raises(HTTPException) as exc:
                await update_rule(existing_rule.id, payload, admin, db)
            assert exc.value.status_code == 422
            assert "target_scope 'movie_version'" in str(exc.value.detail)
            assert "series.status" in str(exc.value.detail)

        await engine.dispose()

    asyncio.run(run())


def test_update_rule_allows_clearing_arr_instance_selection() -> None:
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
            await db.flush()

            existing_rule = ReclaimRule(
                name="movie-version-rule",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=_definition("media.size", "greater_than", 100),
                action={
                    "candidate": True,
                    "tag_enabled": True,
                    "arr_tag": "rec-movie-version-rule",
                    "arr_action": "delete",
                    "media_server_action": "delete",
                    "radarr_service_config_id": 12,
                    "sonarr_service_config_id": None,
                },
            )
            db.add(existing_rule)
            await db.commit()
            await db.refresh(existing_rule)

            payload = CleanupRuleUpdate(
                action={
                    "candidate": True,
                    "tag_enabled": True,
                    "arr_tag": "rec-movie-version-rule",
                    "arr_action": "delete",
                    "media_server_action": "delete",
                    "radarr_service_config_id": None,
                    "sonarr_service_config_id": None,
                }
            )

            updated = await update_rule(existing_rule.id, payload, admin, db)
            assert updated.action is not None
            assert updated.action["arr_action"] == "delete"
            assert updated.action["radarr_service_config_id"] is None
            assert updated.action["sonarr_service_config_id"] is None

            result = await db.execute(
                select(ReclaimRule).where(ReclaimRule.id == existing_rule.id)
            )
            refreshed_rule = result.scalar_one()
            assert refreshed_rule.action is not None
            assert refreshed_rule.action["radarr_service_config_id"] is None
            assert refreshed_rule.action["sonarr_service_config_id"] is None

        await engine.dispose()

    asyncio.run(run())
