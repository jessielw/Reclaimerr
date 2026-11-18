from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.auto_delete_diagnostics import AutoDeleteRecorder, current_auto_delete
from backend.core.rule_engine import (
    PlaybackHistoryResolver,
    PlaybackUserHistoryResolver,
)
from backend.database import Base
from backend.database.models import ReclaimCandidate, ReclaimRule, Season, Series
from backend.enums import MediaType, Service
from backend.models.diagnostics import BundleOptions, Target
from backend.services.playback_history import (
    PlaybackProviderStatus,
    PlaybackRefreshResult,
    PlaybackRuleSnapshot,
)
from backend.services.support_bundle import prepare_bundle
from backend.services.support_history import persist_auto_delete_history
from backend.tasks import cleanup


@pytest.mark.parametrize(
    "selected_missing,mixed_batch,provider_failed,native_failed,expected_retained",
    [
        (False, False, False, False, 1),
        (True, False, False, False, 0),
        (False, True, False, False, 1),
        (False, False, True, False, 0),
        (False, False, False, True, 0),
    ],
    ids=[
        "unrelated-unknown",
        "selected-unknown",
        "mixed-batch",
        "provider-failure",
        "native-refresh-failure",
    ],
)
def test_revalidation_uses_target_coverage_and_keeps_refresh_failures_blocked(
    monkeypatch,
    tmp_path,
    selected_missing,
    mixed_batch,
    provider_failed,
    native_failed,
    expected_retained,
):
    async def exercise():
        database_path = tmp_path / "revalidation-history.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        async with sessions() as db:
            series = Series(title="Scoped playback coverage", tmdb_id=105924, size=2048)
            rule = ReclaimRule(
                name="Old playback",
                media_type=MediaType.SERIES,
                enabled=True,
                target_scope="season",
                action={"auto_delete_enabled": True},
                definition={
                    "version": 1,
                    "root": {
                        "type": "group",
                        "op": "and",
                        "children": [
                            {
                                "type": "condition",
                                "field": "playback.days_since_last_activity",
                                "operator": "greater_than",
                                "value": 30,
                            },
                        ],
                    },
                },
            )
            db.add_all([series, rule])
            await db.flush()
            seasons = [
                Season(
                    series_id=series.id,
                    season_number=i,
                    size=1024,
                    path=f"/fixture/Season {i}",
                )
                for i in (1, 2)
            ]
            db.add_all(seasons)
            await db.flush()
            candidates = [
                ReclaimCandidate(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    matched_rule_ids=[rule.id],
                    matched_criteria={},
                    reason="Previously matched",
                )
                for season in seasons
            ]
            db.add_all(candidates)
            await db.commit()

        field = "playback.days_since_last_activity"
        first_key = ("season", seasons[0].id)
        status = PlaybackProviderStatus(
            config_id=8,
            provider=Service.TRACEARR,
            observed_service=Service.PLEX,
            observed_config_id=1,
            available=not provider_failed,
            error="connection failed PRIVATE_PROVIDER_SECRET"
            if provider_failed
            else None,
        )
        snapshot = PlaybackRuleSnapshot(
            values_by_target={} if selected_missing else {first_key: {field: 356}},
            available_targets=set() if selected_missing else {first_key},
            target_counts={"season": 2},
            errors=[status.error] if status.error else [],
            has_configured_provider=True,
            provider_statuses=[status],
            unavailable_reasons={
                "season": {
                    "missing playback target mapping": 2 if selected_missing else 1
                }
            },
            unavailable_target_samples={"season": [seasons[1].id]},
            available_fields_by_target={} if selected_missing else {first_key: {field}},
            target_ids_by_scope={"season": {season.id for season in seasons}},
        )
        # Exercise real playback activation, scoped rule collection and automatic
        # revalidation. Only external/provider preparation is replaced.
        for name in (
            "_refresh_arr_tags_for_rules",
            "_refresh_arr_monitoring_for_rules",
            "_activate_arr_id_rule_data_for_rules",
            "_activate_favorites_rule_data_for_rules",
            "_activate_rank_rule_data_for_rules",
            "_activate_collection_sibling_rule_data_for_rules",
            "_activate_watch_user_aliases_for_rules",
            "_activate_watch_completion_for_rules",
        ):
            monkeypatch.setattr(cleanup, name, AsyncMock())
        monkeypatch.setattr(
            cleanup,
            "_ensure_favorites_snapshot_if_enabled",
            AsyncMock(return_value=(True, None)),
        )
        monkeypatch.setattr(cleanup, "_load_arr_disk_space", AsyncMock(return_value=[]))
        monkeypatch.setattr(cleanup, "_load_path_mappings", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            cleanup,
            "_activate_seerr_request_resolver_for_rules",
            AsyncMock(return_value=(True, None)),
        )
        monkeypatch.setattr(
            cleanup,
            "_activate_sonarr_rule_data_for_rules",
            AsyncMock(return_value=cleanup._SonarrRuleDataResult(set(), set())),
        )
        monkeypatch.setattr(
            type(cleanup.media_watch_snapshot_cache),
            "ensure_fresh_snapshot",
            AsyncMock(
                return_value=(
                    not native_failed,
                    "timeout PRIVATE_NATIVE_SECRET" if native_failed else None,
                )
            ),
        )
        monkeypatch.setattr(
            cleanup,
            "refresh_playback_history",
            AsyncMock(return_value=PlaybackRefreshResult(statuses=[status])),
        )
        monkeypatch.setattr(
            cleanup, "load_playback_rule_snapshot", AsyncMock(return_value=snapshot)
        )

        requested = candidates if mixed_batch else candidates[:1]
        target_ids = {"season": {candidate.season_id for candidate in requested}}
        recorder = AutoDeleteRecorder()
        token = current_auto_delete.set(recorder)
        try:
            async with sessions() as db:
                preview = await cleanup.collect_rule_preview_matches_with_metadata(
                    db,
                    [rule],
                    target_ids_by_scope=target_ids,
                    require_fresh=True,
                )
            assert preview.metadata.playback_error  # Global warning still exists.
            assert preview.metadata.playback_blocking == (
                provider_failed or native_failed
            )
            assert preview.metadata.playback_unavailable_count == (
                len(requested) if selected_missing else 1 if mixed_batch else 0
            )
            result = await cleanup._revalidate_auto_delete_candidate_ids(
                [candidate.id for candidate in requested]
            )
            assert result.candidate_ids == (
                [candidates[0].id] if expected_retained else []
            )
            assert result.revalidated_out == len(requested) - expected_retained
            decision = recorder.decisions[candidates[0].id]["detail"]
            assert decision["coverage"]["playback_blocking"] == (
                provider_failed or native_failed
            )
            assert decision["outcome"] == (
                "authorized" if expected_retained else "skipped"
            )
            assert cleanup.refresh_playback_history.await_args.kwargs["force"] is True
            observation = recorder.scan.context["playback"]
            assert observation["library_unavailable_count"] == (
                2 if selected_missing else 1
            )
            assert observation["selected_unavailable_count"] == (
                len(requested) if selected_missing else 1 if mixed_batch else 0
            )
            assert observation["sources"][0]["config_id"] == 8
            assert observation["sources"][0]["available"] is (not provider_failed)
            assert observation["require_fresh"] is True
            assert observation["native_refresh_error_category"] == (
                "timeout" if native_failed else None
            )
            assert decision["playback_fields"]["requested"] == [field]
            assert decision["playback_fields"]["unavailable"] == (
                [field] if selected_missing else []
            )
            recorder.scan.context["status"] = "completed"
            await persist_auto_delete_history(recorder, session_factory=sessions)
            # Changing current provider/snapshot state must not rewrite the run.
            status.available = not status.available
            snapshot.values_by_target.clear()
            previous_refreshes = cleanup.refresh_playback_history.await_count
            exported = prepare_bundle(
                database_path,
                BundleOptions(
                    preset="deletion",
                    target=Target(scope="season", id=seasons[0].id),
                    include_sqlite=True,
                ),
                1,
            )
            saved_run = exported.report.sections.auto_delete_runs[0]
            assert saved_run.playback.model_dump(mode="json") == observation
            assert saved_run.revalidation_phase == "rule_evaluation"
            saved_decision = next(
                event.auto_delete for event in exported.timeline if event.auto_delete
            )
            assert (
                saved_decision.playback_fields.model_dump()
                == decision["playback_fields"]
            )
            assert "PRIVATE_PROVIDER_SECRET" not in exported.model_dump_json()
            assert "PRIVATE_NATIVE_SECRET" not in exported.model_dump_json()
            assert cleanup.refresh_playback_history.await_count == previous_refreshes
        finally:
            current_auto_delete.reset(token)
            PlaybackHistoryResolver({}).activate()
            PlaybackUserHistoryResolver({}, available_targets=set()).activate()
            await engine.dispose()

    asyncio.run(exercise())
