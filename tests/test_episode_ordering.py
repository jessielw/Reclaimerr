from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.enums import Service
from backend.services.episode_ordering import (
    EpisodeOrderingSnapshot,
    normalize_episode_order,
    ordering_error,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("official", "aired"),
        ("aired", "aired"),
        ("tmdbAiring", "aired"),
        ("originalAirDate", "aired"),
        (" DVD ", "dvd"),
        ("absolute", "absolute"),
        (None, None),
        ("", None),
        ("digital", None),
    ],
)
def test_normalize_episode_order(raw, expected):
    assert normalize_episode_order(raw) == expected


@pytest.mark.parametrize("order", ["aired", "dvd", "absolute"])
def test_matching_orders_are_supported(order):
    assert ordering_error(order, order) is None


@pytest.mark.parametrize(
    "sonarr, media",
    [("dvd", "aired"), ("aired", "dvd"), ("dvd", None), (None, "aired")],
)
def test_mismatched_or_unknown_orders_are_blocked(sonarr, media):
    assert ordering_error(sonarr, media)


def test_snapshot_caches_only_within_a_run():
    async def run():
        client = SimpleNamespace(get_series_episode_order=AsyncMock(return_value="dvd"))
        snapshot = EpisodeOrderingSnapshot()
        assert await snapshot.media_order(client, "show") == "dvd"
        assert await snapshot.media_order(client, "show") == "dvd"
        client.get_series_episode_order.assert_awaited_once_with(
            "show", library_orders={}
        )
        client.get_series_episode_order.return_value = "aired"
        assert await EpisodeOrderingSnapshot().media_order(client, "show") == "aired"

    asyncio.run(run())


def test_metadata_failure_is_unknown():
    async def run():
        client = SimpleNamespace(
            get_series_episode_order=AsyncMock(side_effect=ValueError("offline"))
        )
        assert await EpisodeOrderingSnapshot().media_order(client, "show") is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "source_order, expected", [("dvd", 1), ("aired", 0), (None, 0)]
)
def test_linked_watch_coordinates_require_matching_order(
    monkeypatch, source_order, expected
):
    from datetime import UTC, datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.core.service_manager import service_manager
    from backend.database import Base
    from backend.database.models import (
        Episode,
        Season,
        Series,
        SeriesServiceRef,
        ServiceConfig,
        SupplementalMediaMatch,
    )
    from backend.enums import MediaType, Service
    from backend.models.media import MediaWatchSnapshot
    from backend.services.media_watch_snapshot_cache import MediaWatchSnapshotCache

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            main = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://main.invalid",
                api_key="synthetic",
                name="Main",
                enabled=True,
                is_main=True,
            )
            linked = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://linked.invalid",
                api_key="synthetic",
                name="Linked",
                enabled=True,
            )
            series = Series(title="Example", tmdb_id=123)
            db.add_all([main, linked, series])
            await db.flush()
            season = Season(series_id=series.id, season_number=1)
            db.add(season)
            await db.flush()
            db.add_all(
                [
                    Episode(
                        season_id=season.id,
                        episode_number=1,
                        plex_rating_key="primary-episode",
                    ),
                    SeriesServiceRef(
                        series_id=series.id,
                        service=Service.PLEX,
                        service_id="primary-show",
                        library_id="library",
                        library_name="TV",
                    ),
                    SupplementalMediaMatch(
                        source_service=Service.PLEX,
                        source_service_config_id=linked.id,
                        source_item_id="linked-show",
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                    ),
                ]
            )
            await db.flush()
            primary = SimpleNamespace(
                get_series_episode_order=AsyncMock(return_value="dvd")
            )
            secondary = SimpleNamespace(
                get_series_episode_order=AsyncMock(return_value=source_order)
            )
            monkeypatch.setattr(
                type(service_manager),
                "get_media_server",
                lambda self, service, config_id: (
                    primary if config_id == main.id else secondary
                ),
            )
            rows, unmatched = await MediaWatchSnapshotCache._build_episode_watch_rows(
                session=db,
                source_service=Service.PLEX,
                source_service_config_id=linked.id,
                snapshots=[
                    MediaWatchSnapshot(
                        media_type=MediaType.SERIES,
                        tmdb_id=123,
                        watch_user_key="Alice",
                        last_watched_at=datetime.now(UTC),
                        source_item_id="linked-episode",
                        season_number=1,
                        episode_number=1,
                    )
                ],
            )
            assert len(rows) == expected
            assert unmatched == 1 - expected
        await engine.dispose()

    asyncio.run(run())


def test_plex_inherited_order_is_cached_and_override_takes_precedence():
    from backend.services.plex import PlexService

    async def run():
        client = SimpleNamespace(
            _make_request=AsyncMock(
                side_effect=[
                    ({"MediaContainer": {"Metadata": [{"librarySectionID": 7}]}}, 200),
                    (
                        {
                            "MediaContainer": {
                                "Setting": [{"id": "showOrdering", "value": "dvd"}]
                            }
                        },
                        200,
                    ),
                    ({"MediaContainer": {"Metadata": [{"librarySectionID": 7}]}}, 200),
                    (
                        {
                            "MediaContainer": {
                                "Metadata": [
                                    {"librarySectionID": 7, "showOrdering": "aired"}
                                ]
                            }
                        },
                        200,
                    ),
                ]
            )
        )
        libraries = {}
        assert (
            await PlexService.get_series_episode_order(
                client, "a", library_orders=libraries
            )
            == "dvd"
        )
        assert (
            await PlexService.get_series_episode_order(
                client, "b", library_orders=libraries
            )
            == "dvd"
        )
        assert (
            await PlexService.get_series_episode_order(
                client, "c", library_orders=libraries
            )
            == "aired"
        )
        assert [c.args[0] for c in client._make_request.call_args_list] == [
            "library/metadata/a",
            "library/sections/7/prefs",
            "library/metadata/b",
            "library/metadata/c",
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", "aired"),
        (None, "aired"),
        ("Aired", "aired"),
        ("DVD", "dvd"),
        ("Absolute", "absolute"),
        ("digital", None),
    ],
)
@pytest.mark.parametrize("service", [Service.EMBY, Service.JELLYFIN])
def test_emby_and_jellyfin_effective_order(raw, expected, service):
    from backend.services.emby_base import EmbyServiceBase

    async def run():
        item = {"Id": "a", "Type": "Series", "DisplayOrder": raw}
        client = SimpleNamespace(
            service_type=service,
            _make_request=AsyncMock(return_value={"Items": [item]}),
        )
        assert await EmbyServiceBase.get_series_episode_order(client, "a") == expected
        client._make_request.assert_awaited_once_with(
            "Items",
            params={
                "Ids": "a",
                "Fields": "Path,MediaSources,DisplayOrder"
                if service is Service.EMBY
                else "Path,MediaSources",
                "EnableUserData": False,
            },
            timeout=10,
        )

    asyncio.run(run())


@pytest.mark.parametrize("service", [Service.EMBY, Service.JELLYFIN])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"Items": []},
        {"Items": "invalid"},
        {"Items": [None]},
        {"Items": [{"Id": "other", "Type": "Series", "DisplayOrder": "Aired"}]},
        {"Items": [{"Id": "a", "Type": "Movie"}]},
        {"Items": [{"Id": "a", "Type": "Series"}] * 2},
    ],
)
def test_emby_family_invalid_series_lookup_keeps_order_unknown(
    monkeypatch, service, payload
):
    from backend.services.emby_base import EmbyServiceBase

    async def run():
        client = EmbyServiceBase("synthetic", "http://media.invalid", service)
        monkeypatch.setattr(
            EmbyServiceBase, "_make_request", AsyncMock(return_value=payload)
        )
        try:
            order = await EpisodeOrderingSnapshot().media_order(client, "a")
            assert order is None
            assert ordering_error("aired", order) is not None
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "moving, action, media_order",
    [
        (False, "delete", "aired"),
        (True, "delete", "aired"),
        (False, "unmonitor_only", "aired"),
        (False, "change_quality_profile", "aired"),
        (False, "delete", "dvd"),
        (True, "delete", "dvd"),
    ],
)
@pytest.mark.parametrize("targeted", [False, True])
def test_ordering_controls_preview_and_cleanup_before_any_route(
    monkeypatch, moving, action, media_order, targeted
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.database import Base
    from backend.database.models import (
        ReclaimCandidate,
        ReclaimRule,
        Season,
        Series,
        SeriesArrRef,
        SeriesServiceRef,
        ServiceConfig,
    )
    from backend.enums import MediaType, Service
    from backend.models.cleanup import MatchedCandidateRecord, RulePreviewMatchMetadata
    from backend.services.playback_guard import PlaybackGuard, PlaybackTarget
    from backend.tasks import cleanup

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        db_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(cleanup, "async_db", db_factory)
        async with db_factory() as db:
            main = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex.invalid",
                api_key="synthetic",
                name="Main",
                enabled=True,
                is_main=True,
            )
            arr = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr.invalid",
                api_key="synthetic",
                name="Arr",
                enabled=True,
            )
            series = Series(title="Example", tmdb_id=123)
            rule = ReclaimRule(
                name="Cleanup",
                media_type=MediaType.SERIES,
                target_scope="season",
                definition={
                    "version": 1,
                    "root": {"type": "group", "op": "and", "children": []},
                },
                action={"arr_action": action, "quality_profile_id": 9},
            )
            db.add_all([main, arr, series, rule])
            await db.flush()
            season = Season(series_id=series.id, season_number=1)
            db.add(season)
            await db.flush()
            if targeted:
                secondary = ServiceConfig(
                    service_type=Service.SONARR,
                    base_url="http://secondary.invalid",
                    api_key="synthetic",
                    name="Secondary",
                    enabled=True,
                )
                db.add(secondary)
                await db.flush()
                db.add(
                    SeriesArrRef(
                        series_id=series.id,
                        service_config_id=secondary.id,
                        arr_series_id=43,
                    )
                )
                rule.action = {**rule.action, "sonarr_service_config_ids": [arr.id]}
            candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season.id,
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="test",
            )
            db.add_all(
                [
                    candidate,
                    SeriesServiceRef(
                        series_id=series.id,
                        service=Service.PLEX,
                        service_id="show",
                        library_id="library",
                        library_name="TV",
                    ),
                    SeriesArrRef(
                        series_id=series.id, service_config_id=arr.id, arr_series_id=42
                    ),
                ]
            )
            await db.commit()
            candidate_id = candidate.id
        media = SimpleNamespace(
            get_series_episode_order=AsyncMock(return_value=media_order)
        )
        sonarr = SimpleNamespace(get_series_episode_order=AsyncMock(return_value="dvd"))
        other_sonarr = SimpleNamespace(
            get_series_episode_order=AsyncMock(return_value="absolute")
        )
        monkeypatch.setattr(
            type(cleanup.service_manager), "get_media_server", lambda *args: media
        )
        monkeypatch.setattr(
            type(cleanup.service_manager),
            "get_sonarr",
            lambda self, config_id: sonarr if config_id == arr.id else other_sonarr,
        )
        monkeypatch.setattr(
            cleanup,
            "candidate_playback_target",
            AsyncMock(return_value=PlaybackTarget()),
        )
        delete = AsyncMock(return_value=(1, 0))
        move = AsyncMock(return_value=(1, 0))
        monkeypatch.setattr(cleanup, "_delete_candidates_prepared", delete)
        monkeypatch.setattr(cleanup, "_move_candidates_prepared", move)
        try:
            record = MatchedCandidateRecord(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season.id,
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="test",
                reason_data=[],
                estimated_space_bytes=0,
            )
            monkeypatch.setattr(
                cleanup,
                "_collect_season_candidate_records",
                AsyncMock(return_value=[record]),
            )
            metadata = RulePreviewMatchMetadata()
            async with db_factory() as db:
                matches = await cleanup._collect_rule_match_records(
                    db, [rule], preview_metadata=metadata
                )
            assert bool(matches) is (media_order == "dvd")
            assert metadata.sonarr_unavailable_count == (
                0 if media_order == "dvd" else 1
            )
            result = await cleanup._run_playback_guarded_candidates(
                [candidate_id],
                approved_by="test",
                moving=moving,
                playback_guard=PlaybackGuard(False, [], {}),
            )
            if media_order == "dvd":
                assert result.failed == 0 and result.succeeded == 1
                (move if moving else delete).assert_awaited_once()
            else:
                assert result.failed == 1 and result.succeeded == 0
                delete.assert_not_awaited()
                move.assert_not_awaited()
            other_sonarr.get_series_episode_order.assert_not_awaited()
            async with db_factory() as db:
                remaining = await db.get(ReclaimCandidate, candidate_id)
                assert remaining is not None
                if media_order != "dvd":
                    assert "does not match" in remaining.last_delete_error
                else:
                    assert remaining.last_delete_error is None
        finally:
            await engine.dispose()

    asyncio.run(run())
