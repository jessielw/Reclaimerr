"""Keep candidate clocks through unverified scans (#526)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.rule_engine import PlaybackHistoryResolver
from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
)
from backend.enums import MediaType, Service
from backend.models.cleanup import MatchedCandidateRecord
from backend.tasks import cleanup

SCOPES = ["movie_version", "series", "season", "episode"]
FLAGGED_AT = datetime(2026, 1, 1)


@asynccontextmanager
async def _database(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", sessions)
        yield sessions
    finally:
        await engine.dispose()


def _rule(scope, field="playback.play_count"):
    conditions = [
        {"type": "condition", "field": field, "operator": "less_than", "value": 6}
    ]
    if field == "seerr.requested":
        conditions = [{"type": "condition", "field": field, "operator": "is_true"}]
    return ReclaimRule(
        name=f"Cleanup {scope}",
        media_type=MediaType.MOVIE if scope == "movie_version" else MediaType.SERIES,
        target_scope=scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": conditions},
        },
        action={
            "candidate": True,
            "auto_delete_enabled": True,
            "auto_delete_delay_days": 14,
        },
    )


async def _seed(db, scope, field="playback.play_count"):
    movie = Movie(title="Movie", tmdb_id=101, size=1000)
    series = Series(title="Show", tmdb_id=102, size=1000)
    rule = _rule(scope, field)
    db.add_all([movie, series, rule])
    await db.flush()
    version = MovieVersion(
        movie_id=movie.id,
        service=Service.PLEX,
        service_item_id="item",
        service_media_id="media",
        library_id="movies",
        library_name="Movies",
        path="/media/movie.mkv",
        size=1000,
    )
    season = Season(
        series_id=series.id,
        season_number=1,
        path="/media/show/Season 01",
        size=1000,
        episode_count=1,
    )
    db.add_all([version, season])
    await db.flush()
    episode = Episode(
        season_id=season.id, episode_number=1, path="/media/show/episode.mkv", size=1000
    )
    db.add(episode)
    await db.flush()
    identities = {
        "movie_version": dict(movie_id=movie.id, movie_version_id=version.id),
        "series": dict(series_id=series.id),
        "season": dict(series_id=series.id, season_id=season.id),
        "episode": dict(
            series_id=series.id, season_id=season.id, episode_id=episode.id
        ),
    }[scope]
    target_id = {
        "movie_version": version.id,
        "series": series.id,
        "season": season.id,
        "episode": episode.id,
    }[scope]
    record = MatchedCandidateRecord(
        media_type=rule.media_type,
        matched_rule_ids=[rule.id],
        matched_criteria={},
        reason="matched",
        reason_data=[],
        estimated_space_bytes=1000,
        **identities,
    )
    candidate = ReclaimCandidate(
        media_type=rule.media_type,
        matched_rule_ids=[rule.id],
        matched_criteria={},
        reason="matched",
        **identities,
    )
    candidate.created_at = FLAGGED_AT
    candidate.auto_delete_timer_started_at = FLAGGED_AT + timedelta(days=1)
    candidate.auto_delete_postponed_until = FLAGGED_AT + timedelta(days=2)
    candidate.lifecycle_reason = "Original lifecycle controls"
    candidate.requester_warned_at = FLAGGED_AT
    candidate.approved_for_deletion = True
    db.add(candidate)
    await db.commit()
    return rule.id, candidate.id, target_id, record


async def _process(db, scope, rules):
    if scope in {"movie_version", "series"}:
        media_type = MediaType.MOVIE if scope == "movie_version" else MediaType.SERIES
        return await cleanup._process_media(db, rules, media_type)
    if scope == "season":
        return await cleanup._process_series_seasons(db, rules)
    return await cleanup._process_series_episodes(db, rules)


async def _assert_original(db, candidate_id):
    candidate = await db.get(ReclaimCandidate, candidate_id)
    assert candidate is not None
    assert candidate.created_at == FLAGGED_AT
    assert candidate.auto_delete_timer_started_at == FLAGGED_AT + timedelta(days=1)
    assert candidate.auto_delete_postponed_until == FLAGGED_AT + timedelta(days=2)
    assert candidate.lifecycle_reason == "Original lifecycle controls"
    assert candidate.requester_warned_at == FLAGGED_AT
    assert candidate.approved_for_deletion is True
    return candidate


@pytest.mark.parametrize("scope", SCOPES)
def test_unknown_item_data_preserves_candidate_until_verified(monkeypatch, scope):
    async def run():
        async with _database(monkeypatch) as sessions:
            async with sessions() as db:
                rule_id, candidate_id, target_id, _ = await _seed(db, scope)
            for _ in range(2):
                async with sessions() as db:
                    PlaybackHistoryResolver({}).activate()
                    rule = await db.get(ReclaimRule, rule_id)
                    await _process(db, scope, [rule])
                async with sessions() as db:
                    await _assert_original(db, candidate_id)
                assert (await cleanup._select_auto_delete_eligible_candidate_ids())[
                    0
                ] == []

            async with sessions() as db:
                PlaybackHistoryResolver(
                    {(scope, target_id): {"playback.play_count": 0}}
                ).activate()
                await _process(db, scope, [await db.get(ReclaimRule, rule_id)])
            async with sessions() as db:
                await _assert_original(db, candidate_id)
            assert (await cleanup._select_auto_delete_eligible_candidate_ids())[0] == [
                candidate_id
            ]

            async with sessions() as db:
                PlaybackHistoryResolver(
                    {(scope, target_id): {"playback.play_count": 10}}
                ).activate()
                await _process(db, scope, [await db.get(ReclaimRule, rule_id)])
            async with sessions() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None

            async with sessions() as db:
                PlaybackHistoryResolver(
                    {(scope, target_id): {"playback.play_count": 0}}
                ).activate()
                await _process(db, scope, [await db.get(ReclaimRule, rule_id)])
            async with sessions() as db:
                reflagged = (await db.scalars(select(ReclaimCandidate))).one()
                assert reflagged.created_at > FLAGGED_AT
                assert reflagged.auto_delete_timer_started_at is None
                assert reflagged.auto_delete_postponed_until is None

    asyncio.run(run())


@pytest.mark.parametrize("scope", SCOPES)
def test_unknown_data_does_not_create_new_candidates(monkeypatch, scope):
    async def run():
        async with _database(monkeypatch) as sessions:
            async with sessions() as db:
                rule_id, candidate_id, _, _ = await _seed(db, scope)
                await db.delete(await db.get(ReclaimCandidate, candidate_id))
                await db.commit()
            async with sessions() as db:
                PlaybackHistoryResolver({}).activate()
                assert await _process(
                    db, scope, [await db.get(ReclaimRule, rule_id)]
                ) == (0, 0, 0)
            async with sessions() as db:
                assert (await db.scalars(select(ReclaimCandidate))).all() == []

    asyncio.run(run())


@pytest.mark.parametrize("scope", SCOPES)
def test_known_match_does_not_discard_an_unknown_previous_rule(monkeypatch, scope):
    async def run():
        async with _database(monkeypatch) as sessions:
            async with sessions() as db:
                rule_id, candidate_id, target_id, _ = await _seed(db, scope)
                other = _rule(scope, "media.size")
                other.definition["root"]["children"][0]["value"] = 2000
                other.action["auto_delete_delay_days"] = 0
                db.add(other)
                await db.commit()
                other_id = other.id
            async with sessions() as db:
                PlaybackHistoryResolver({}).activate()
                rules = list((await db.scalars(select(ReclaimRule))).all())
                await _process(db, scope, rules)
            async with sessions() as db:
                candidate = await _assert_original(db, candidate_id)
                assert candidate.matched_rule_ids == [rule_id]
            assert (await cleanup._select_auto_delete_eligible_candidate_ids())[0] == []

            async with sessions() as db:
                PlaybackHistoryResolver(
                    {(scope, target_id): {"playback.play_count": 10}}
                ).activate()
                await _process(
                    db, scope, list((await db.scalars(select(ReclaimRule))).all())
                )
            async with sessions() as db:
                candidate = await _assert_original(db, candidate_id)
                assert candidate.matched_rule_ids == [other_id]
            assert (await cleanup._select_auto_delete_eligible_candidate_ids())[0] == [
                candidate_id
            ]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["and", "or"])
def test_known_rule_branch_resolves_unknown_data(monkeypatch, operation):
    async def run():
        async with _database(monkeypatch) as sessions:
            async with sessions() as db:
                rule_id, candidate_id, _, _ = await _seed(db, "movie_version")
                rule = await db.get(ReclaimRule, rule_id)
                definition = deepcopy(rule.definition)
                definition["root"]["op"] = operation
                definition["root"]["children"].append(
                    {
                        "type": "condition",
                        "field": "media.size",
                        "operator": "greater_than",
                        "value": 1 if operation == "or" else 2000,
                    }
                )
                rule.definition = definition
                await db.commit()
            async with sessions() as db:
                PlaybackHistoryResolver({}).activate()
                await _process(
                    db, "movie_version", [await db.get(ReclaimRule, rule_id)]
                )
            async with sessions() as db:
                if operation == "and":
                    assert await db.get(ReclaimCandidate, candidate_id) is None
                else:
                    await _assert_original(db, candidate_id)
            expected = [candidate_id] if operation == "or" else []
            assert (await cleanup._select_auto_delete_eligible_candidate_ids())[
                0
            ] == expected

    asyncio.run(run())


def test_disabling_rule_removes_held_candidate(monkeypatch):
    async def run():
        async with _database(monkeypatch) as sessions:
            async with sessions() as db:
                rule_id, candidate_id, _, _ = await _seed(db, "movie_version")
            async with sessions() as db:
                PlaybackHistoryResolver({}).activate()
                await _process(
                    db, "movie_version", [await db.get(ReclaimRule, rule_id)]
                )
            async with sessions() as db:
                await _assert_original(db, candidate_id)
                rule = await db.get(ReclaimRule, rule_id)
                rule.enabled = False
                await db.commit()
            _stub_scan_providers(monkeypatch)
            async with sessions() as db:
                await cleanup._scan_with_db_impl(db)
            async with sessions() as db:
                assert await db.get(ReclaimCandidate, candidate_id) is None

    asyncio.run(run())


def _stub_scan_providers(monkeypatch):
    for name in [
        "_refresh_arr_tags_for_rules",
        "_refresh_arr_monitoring_for_rules",
        "_activate_arr_id_rule_data_for_rules",
        "_activate_favorites_rule_data_for_rules",
        "_activate_rank_rule_data_for_rules",
        "_activate_collection_sibling_rule_data_for_rules",
        "_activate_watch_user_aliases_for_rules",
        "_activate_watch_completion_for_rules",
        "_sync_leaving_soon_collections",
        "enqueue_event_deliveries",
        "notify_admins",
    ]:
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
        "_activate_sonarr_rule_data_for_rules",
        AsyncMock(return_value=cleanup._SonarrRuleDataResult(set(), set())),
    )
    playback = AsyncMock(return_value=cleanup._PlaybackRuleDataResult(snapshot=None))
    seerr = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(cleanup, "_activate_playback_history_for_rules", playback)
    monkeypatch.setattr(cleanup, "_activate_seerr_request_resolver_for_rules", seerr)
    return playback, seerr


def test_scan_notifies_only_for_inserted_candidates(monkeypatch):
    async def run():
        async with _database(monkeypatch) as sessions:
            _stub_scan_providers(monkeypatch)
            send = AsyncMock()
            monkeypatch.setattr(cleanup, "notify_all_users", send)
            monkeypatch.setattr(cleanup, "warn_requesters_before_deletion", AsyncMock())
            monkeypatch.setattr(cleanup, "reconcile_candidate_arr_tags", AsyncMock())
            monkeypatch.setattr("backend.core.task_tracking.async_db", sessions)
            monkeypatch.setattr("backend.services.notifications.async_db", sessions)

            async with sessions() as db:
                _, candidate_id, _, _ = await _seed(db, "movie_version", "media.size")
                candidate = await db.get(ReclaimCandidate, candidate_id)
                candidate.created_at = datetime.now(UTC).replace(
                    tzinfo=None
                ) + timedelta(seconds=10)
                movie = Movie(title="New Movie", tmdb_id=103, size=2000)
                db.add(movie)
                await db.flush()
                db.add(
                    MovieVersion(
                        movie_id=movie.id,
                        service=Service.PLEX,
                        service_item_id="new-item",
                        service_media_id="new-media",
                        library_id="movies",
                        library_name="Movies",
                        path="/media/new-movie.mkv",
                        size=2000,
                    )
                )
                rule = (await db.scalars(select(ReclaimRule))).one()
                definition = deepcopy(rule.definition)
                definition["root"]["children"][0]["value"] = 3000
                rule.definition = definition
                await db.commit()

            await cleanup.scan_cleanup_candidates()
            send.assert_awaited_once()
            context = send.await_args.kwargs["context"]
            assert context["created_count"] == 1
            assert context["total_reclaimable_bytes"] == 2000
            assert [
                candidate["media_title"] for candidate in context["candidates"]
            ] == ["New Movie"]

            await cleanup.scan_cleanup_candidates()
            send.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("provider", ["playback", "seerr"])
@pytest.mark.parametrize("other_rule", [False, True])
def test_skipped_rule_preserves_candidate_across_scans_and_recovery(
    monkeypatch, scope, provider, other_rule
):
    async def run():
        async with _database(monkeypatch) as sessions:
            async with sessions() as db:
                field = (
                    "playback.play_count"
                    if provider == "playback"
                    else "seerr.requested"
                )
                _, candidate_id, _, record = await _seed(db, scope, field)
                if other_rule:
                    db.add(_rule(scope, "media.size"))
                    await db.commit()
            playback, seerr = _stub_scan_providers(monkeypatch)
            collector_name = {
                "movie_version": "_collect_movie_version_candidate_records",
                "series": "_collect_series_candidate_records",
                "season": "_collect_season_candidate_records",
                "episode": "_collect_episode_candidate_records",
            }[scope]
            collector = AsyncMock(return_value=[])
            monkeypatch.setattr(cleanup, collector_name, collector)
            if provider == "playback":
                playback.return_value = cleanup._PlaybackRuleDataResult(
                    snapshot=None, error="Provider offline", blocking=True
                )
            else:
                seerr.return_value = (False, "Provider offline")
            for _ in range(2):
                async with sessions() as db:
                    await cleanup._scan_with_db_impl(db)
                async with sessions() as db:
                    await _assert_original(db, candidate_id)
                assert (await cleanup._select_auto_delete_eligible_candidate_ids())[
                    0
                ] == []

            playback.return_value = cleanup._PlaybackRuleDataResult(snapshot=None)
            seerr.return_value = (True, None)
            collector.return_value = [record]
            async with sessions() as db:
                await cleanup._scan_with_db_impl(db)
            async with sessions() as db:
                await _assert_original(db, candidate_id)
            assert (await cleanup._select_auto_delete_eligible_candidate_ids())[0] == [
                candidate_id
            ]

    asyncio.run(run())
