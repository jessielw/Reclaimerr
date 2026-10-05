from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import _normalize_rule_action, get_rules, import_rules
from backend.core.rule_actions import ARR_ACTION_MONITOR_NEW_SEASONS as ACTION
from backend.database import Base
from backend.database.models import ReclaimRule, User
from backend.enums import MediaType, UserRole
from backend.models.cleanup import CleanupRuleCreate, RuleImportPayload
from backend.services.sonarr import SonarrClient
from backend.tasks import cleanup


def test_action_normalization_and_scope():
    action = {"arr_action": ACTION, "move_instead_of_delete": True}
    normalized = _normalize_rule_action(action, "Clean watched seasons", "season")
    assert normalized["arr_action"] == ACTION
    assert normalized["move_instead_of_delete"] is True
    assert _normalize_rule_action(normalized, "Imported", "season") == normalized
    for scope in ["series", "episode", "movie_version", None]:
        with pytest.raises(ValueError, match="only available on season rules"):
            _normalize_rule_action(action, "Invalid", scope)


@pytest.mark.parametrize(
    "other,expected",
    [
        ("delete", ACTION),
        ("unmonitor", "unmonitor"),
        ("unmonitor_only", "unmonitor_only"),
        ("change_quality_profile", "change_quality_profile"),
    ],
)
def test_action_conflicts(other, expected):
    candidate = SimpleNamespace(id=1, matched_rule_ids=[1, 2])
    rules = {
        1: SimpleNamespace(action={"arr_action": ACTION}),
        2: SimpleNamespace(action={"arr_action": other}),
    }
    assert cleanup._get_arr_action(candidate, rules) == expected
    assert cleanup._merge_arr_action(ACTION, other) == expected
    assert cleanup._merge_arr_action(other, ACTION) == expected
    assert cleanup._merge_arr_action(None, ACTION) == ACTION
    assert cleanup._merge_arr_action("remove_if_empty", ACTION) == ACTION


@pytest.mark.parametrize(
    "season_number,latest,initially_monitored,new_items,enabled",
    [
        (2, 2, False, "none", True),
        (2, 2, True, "all", True),
        (1, 2, False, "none", False),
        (0, 2, False, "none", False),
        (0, 0, True, "all", False),
    ],
)
def test_live_latest_season_preserves_other_flags(
    season_number, latest, initially_monitored, new_items, enabled
):
    async def run():
        data = {
            "id": 42,
            "title": "Show",
            "monitored": initially_monitored,
            "monitorNewItems": new_items,
            "tags": [7],
            "qualityProfileId": 9,
            "seasons": [
                {
                    "seasonNumber": n,
                    "monitored": n != 1,
                    "statistics": {"episodeFileCount": 0},
                }
                for n in range(latest + 1)
            ],
        }
        original = deepcopy(data)

        async def request(method, endpoint, **kwargs):
            return 200, data if method == "GET" else kwargs["json"]

        mock = AsyncMock(side_effect=request)
        client = SonarrClient(api_key="key", base_url="http://sonarr")
        try:
            with patch.object(SonarrClient, "_make_request", mock):
                assert (
                    await client.enable_new_seasons_if_latest(42, season_number)
                    is enabled
                )
        finally:
            await client.session.close()
        assert data == original
        assert mock.await_count == (2 if enabled else 1)
        if enabled:
            payload = mock.call_args.kwargs["json"]
            assert payload["monitored"] is True
            assert payload["monitorNewItems"] == "all"
            assert payload["tags"] == [7] and payload["qualityProfileId"] == 9
            assert payload["seasons"][season_number]["monitored"] is False
            for n in range(latest + 1):
                if n != season_number:
                    assert payload["seasons"][n] == original["seasons"][n]

    asyncio.run(run())


def test_missing_season_blocks_preparation_and_finalization():
    async def run():
        client = SonarrClient(api_key="key", base_url="http://sonarr")
        request = AsyncMock(return_value=(200, {"id": 42, "seasons": []}))
        try:
            with patch.object(SonarrClient, "_make_request", request):
                with pytest.raises(ValueError, match="missing"):
                    await client.prepare_season_removal(42, 2)
                with pytest.raises(ValueError, match="missing"):
                    await client.enable_new_seasons_if_latest(42, 2)
        finally:
            await client.session.close()
        assert all(call.args[0] == "GET" for call in request.call_args_list)

    asyncio.run(run())


def test_unconfirmed_monitoring_update_is_an_error():
    async def run():
        client = SonarrClient(api_key="key", base_url="http://sonarr")
        request = AsyncMock(
            side_effect=lambda *args, **kwargs: (
                200,
                {
                    "id": 42,
                    "monitored": False,
                    "seasons": [{"seasonNumber": 1, "monitored": True}],
                },
            )
        )
        try:
            with patch.object(SonarrClient, "_make_request", request):
                with pytest.raises(ValueError, match="did not unmonitor"):
                    await client.prepare_season_removal(42, 1)
                with pytest.raises(ValueError, match="did not confirm"):
                    await client.enable_new_seasons_if_latest(42, 1)
        finally:
            await client.session.close()

    asyncio.run(run())


def test_season_file_deletion_deduplicates_and_ignores_missing_files():
    async def run():
        client = SonarrClient(api_key="key", base_url="http://sonarr")
        episodes = [
            {"seasonNumber": s, "episodeFileId": f}
            for s, f in [(1, 0), (1, None), (1, -1), (1, 50), (1, 50), (2, 60)]
        ]
        request = AsyncMock(side_effect=[(200, episodes), (200, None)])
        try:
            with patch.object(SonarrClient, "_make_request", request):
                await client.delete_season_files(42, 1)
        finally:
            await client.session.close()
        assert request.call_args.kwargs["json"] == {"episodeFileIds": [50]}

    asyncio.run(run())


def test_rule_import_export_round_trip(monkeypatch):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        monkeypatch.setattr(
            "backend.api.routes.rules._sync_stale_library_notice", AsyncMock()
        )
        user = User(username="admin", password_hash="x", role=UserRole.ADMIN)
        rule = CleanupRuleCreate(
            name="Watched seasons",
            media_type=MediaType.SERIES,
            target_scope="season",
            action={"arr_action": ACTION, "move_instead_of_delete": True},
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "media.size",
                            "operator": "greater_than",
                            "value": 0,
                        }
                    ],
                },
            },
        )
        try:
            async with sessions() as db:
                result = await import_rules(RuleImportPayload(rules=[rule]), user, db)
                assert result.imported == 1 and result.errors == []
                exported = (await get_rules(user, db))[0]
                assert exported.action["arr_action"] == ACTION
                assert exported.action["move_instead_of_delete"] is True
                imported = CleanupRuleCreate.model_validate(exported.model_dump())
                result = await import_rules(
                    RuleImportPayload(rules=[imported]), user, db
                )
                assert result.imported == 1 and result.errors == []
                stored = (await db.execute(select(ReclaimRule))).scalars().all()
                assert len(stored) == 2
                assert all(r.action["arr_action"] == ACTION for r in stored)
        finally:
            await engine.dispose()

    asyncio.run(run())
