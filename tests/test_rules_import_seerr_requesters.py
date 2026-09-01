"""Importing a rules file written before Seerr became multi-instance.

Rules exported back then name requesters by a bare user id. Migration
d7f3b2a9c604 rewrote the ones already in the database; a rule arriving through
``POST /api/rules/import`` is the same rewrite coming through a different door,
and it has to follow the same rule -- qualify against the single configured
Seerr, refuse rather than guess when there is not exactly one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import import_rules
from backend.core.rule_engine import TARGET_MOVIE_VERSION
from backend.database import Base
from backend.database.models import ReclaimRule, ServiceConfig, User
from backend.enums import MediaType, Service, UserRole
from backend.models.cleanup import CleanupRuleCreate, RuleImportPayload


def _admin_user() -> User:
    return User(
        username="admin", password_hash="x", role=UserRole.ADMIN, permissions=[]
    )


def _definition(value: object) -> dict[str, Any]:
    return {
        "version": 1,
        "root": {
            "type": "group",
            "op": "and",
            "children": [
                {
                    "type": "condition",
                    "field": "seerr.requested_by_user_ids",
                    "operator": "contains_any",
                    "value": value,
                }
            ],
        },
    }


def _rule(value: object, name: str = "Legacy requester rule") -> CleanupRuleCreate:
    return CleanupRuleCreate(
        name=name,
        media_type=MediaType.MOVIE,
        target_scope=TARGET_MOVIE_VERSION,
        definition=_definition(value),
    )


def _seerr(name: str) -> ServiceConfig:
    return ServiceConfig(
        service_type=Service.SEERR,
        name=name,
        base_url=f"http://{name.lower()}.local",
        api_key="encrypted",
        enabled=True,
    )


async def _run_import(seerr_names: list[str], rules: list[CleanupRuleCreate]):
    """Import ``rules`` against an install holding ``seerr_names`` Seerrs."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with session_maker() as db:
            configs = [_seerr(name) for name in seerr_names]
            db.add_all(configs)
            await db.commit()
            for config in configs:
                await db.refresh(config)

            response = await import_rules(
                RuleImportPayload(rules=rules), _admin_user(), db
            )
            stored = (await db.execute(select(ReclaimRule))).scalars().all()
            return response, stored, [config.id for config in configs]
    finally:
        await engine.dispose()


def _stored_values(rule: ReclaimRule) -> Any:
    return rule.definition["root"]["children"][0]["value"]


def test_bare_id_is_qualified_onto_the_only_configured_seerr() -> None:
    async def run() -> None:
        response, stored, config_ids = await _run_import(["Overseerr"], [_rule(["3"])])

        assert response.errors == []
        assert response.warnings == []
        assert response.imported == 1
        assert len(stored) == 1
        assert _stored_values(stored[0]) == [f"{config_ids[0]}:3"]

    asyncio.run(run())


def test_bare_id_is_refused_when_two_seerrs_are_configured() -> None:
    """Two instances number different people 3; picking one would be a guess."""

    async def run() -> None:
        response, stored, _ = await _run_import(
            ["Overseerr", "Jellyseerr"], [_rule(["3"])]
        )

        assert response.imported == 0
        assert stored == []
        assert len(response.errors) == 1
        assert "2 Seerr instances are configured" in response.errors[0]
        assert "bare user ID" in response.errors[0]

    asyncio.run(run())


def test_bare_id_is_refused_when_no_seerr_is_configured() -> None:
    async def run() -> None:
        response, stored, _ = await _run_import([], [_rule(["3"])])

        assert response.imported == 0
        assert stored == []
        assert len(response.errors) == 1
        assert "no Seerr is configured" in response.errors[0]

    asyncio.run(run())


def test_already_qualified_ids_import_unchanged() -> None:
    """Re-importing a file this has already rewritten must be a no-op."""

    async def run() -> None:
        response, stored, config_ids = await _run_import(
            ["Overseerr"], [_rule([f"{1}:3"])]
        )

        assert response.errors == []
        assert response.imported == 1
        assert _stored_values(stored[0]) == [f"{config_ids[0]}:3"]

    asyncio.run(run())


def test_mixed_bare_and_qualified_values_only_rewrite_the_bare_ones() -> None:
    async def run() -> None:
        response, stored, config_ids = await _run_import(
            ["Overseerr"], [_rule(["3", "1:9"])]
        )

        assert response.errors == []
        assert _stored_values(stored[0]) == [f"{config_ids[0]}:3", "1:9"]

    asyncio.run(run())


def test_id_naming_an_unconfigured_instance_imports_with_a_warning() -> None:
    """Someone else's export: syntactically fine, but it matches nobody here."""

    async def run() -> None:
        response, stored, config_ids = await _run_import(
            ["Overseerr"], [_rule(["4242:3"])]
        )

        assert response.errors == []
        assert response.imported == 1
        assert len(response.warnings) == 1
        assert "4242" in response.warnings[0]
        assert "not" in response.warnings[0]
        # left alone, not silently retargeted at the configured Seerr
        assert _stored_values(stored[0]) == ["4242:3"]
        assert config_ids[0] != 4242

    asyncio.run(run())


def test_bare_id_inside_a_disabled_node_is_qualified_too() -> None:
    """A bare id left in a disabled node changes the rule when re-enabled."""

    async def run() -> None:
        definition = _definition(["3"])
        definition["root"]["children"][0]["enabled"] = False
        # A rule needs one enabled condition to validate at all; the point here
        # is that the disabled sibling is rewritten anyway.
        definition["root"]["children"].append(
            {
                "type": "condition",
                "field": "seerr.requested",
                "operator": "is_true",
            }
        )
        rule = CleanupRuleCreate(
            name="Disabled requester condition",
            media_type=MediaType.MOVIE,
            target_scope=TARGET_MOVIE_VERSION,
            definition=definition,
        )
        response, stored, config_ids = await _run_import(["Overseerr"], [rule])

        assert response.errors == []
        assert _stored_values(stored[0]) == [f"{config_ids[0]}:3"]

    asyncio.run(run())


def test_one_bad_rule_does_not_block_the_rest_of_the_file() -> None:
    async def run() -> None:
        response, stored, config_ids = await _run_import(
            ["Overseerr", "Jellyseerr"],
            [_rule(["3"], name="Legacy"), _rule(["1:7"], name="Already qualified")],
        )

        assert response.imported == 1
        assert len(response.errors) == 1
        assert response.errors[0].startswith("Legacy:")
        assert [rule.name for rule in stored] == ["Already qualified"]
        assert config_ids

    asyncio.run(run())
