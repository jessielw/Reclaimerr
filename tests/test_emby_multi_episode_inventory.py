from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from backend.core.rule_engine import _season_watch_progress
from backend.database import Base
from backend.database.models import (
    Episode,
    EpisodeVersion,
    NativePlaybackUser,
    PlaybackHistoryAggregate,
    PlaybackHistoryEvent,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
)
from backend.enums import MediaType
from backend.models.media import AggregatedSeriesData, ExternalIDs, MediaWatchSnapshot
from backend.services import playback_history
from backend.services.emby import EmbyService
from backend.services.episode_ordering import EpisodeOrderingSnapshot
from backend.services.jellyfin import JellyfinService
from backend.services.media_watch_snapshot_cache import MediaWatchSnapshotCache
from backend.services.selected_playback_history import load_selected_history_snapshot
from backend.tasks.sync import _build_series_supplemental_matches, _sync_seasons


def combined_item(end=2):
    return {
        "Id": "combined",
        "SeriesId": "show",
        "SeasonId": "season",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "IndexNumberEnd": end,
        "MediaSources": [
            {
                "Id": "physical-file",
                "Path": "/Show/S01E01-E02.mkv",
                "Size": 100,
                "RunTimeTicks": 24_000_000_000,
            }
        ],
        "UserData": {"PlayCount": 3, "LastPlayedDate": "2026-10-19T00:00:00Z"},
    }


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize(
    "end, expected",
    [(2, [1, 2]), (4, [1, 2, 3, 4]), (None, [1]), ("invalid", [1]), (0, [1])],
)
def test_combined_file_expands_episode_numbers_without_multiplying_bytes(
    service_class, end, expected
):
    async def run():
        client = service_class("key", "http://media.invalid")
        request = AsyncMock(
            return_value={"Items": [combined_item(end)], "TotalRecordCount": 1}
        )
        try:
            with patch.object(client, "_make_request", request):
                sizes, seasons = await client.get_series_sizes_for_library(
                    "library", "user"
                )
        finally:
            await client.session.close()
        season = seasons[("show", 1)]
        assert sizes == {"show": 100}
        assert season.size == 100
        assert season.episode_count == len(expected)
        assert season.view_count == 3
        assert season.episode_paths == ["/Show/S01E01-E02.mkv"]
        assert [e.episode_number for e in season.episode_data] == expected
        for episode in season.episode_data:
            assert episode.view_count == 3
            assert (
                episode.jellyfin_episode_id or episode.emby_episode_id
            ) == "combined"
            assert episode.versions[0].service_media_id == "physical-file"
        assert "IndexNumberEnd" in request.call_args.kwargs["params"]["Fields"]

    asyncio.run(run())


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize("linked", [False, True])
def test_combined_file_sync_and_playback_cover_both_episodes(
    monkeypatch, service_class, linked
):
    async def run():
        client = service_class("key", "http://media.invalid")
        service = client.service_type
        try:
            with patch.object(
                client,
                "_make_request",
                AsyncMock(
                    return_value={"Items": [combined_item()], "TotalRecordCount": 1}
                ),
            ):
                _, seasons = await client.get_series_sizes_for_library(
                    "library", "user"
                )
        finally:
            await client.session.close()
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(playback_history, "async_db", factory)
            async with factory() as db:
                config = ServiceConfig(
                    service_type=service,
                    base_url="http://media.invalid",
                    api_key="synthetic",
                    name="Main",
                    enabled=True,
                    is_main=True,
                )
                series = Series(title="Show", tmdb_id=123)
                db.add_all([config, series])
                await db.flush()
                await _sync_seasons(db, series.id, list(seasons.values()), service)
                await db.flush()
                season = await db.scalar(
                    select(Season).options(selectinload(Season.episodes))
                )
                season.sonarr_episode_numbers = [1, 2]
                assert _season_watch_progress(season) == (True, 100.0)
                assert {e.episode_number for e in season.episodes} == {1, 2}
                versions = list((await db.scalars(select(EpisodeVersion))).all())
                assert len(versions) == 2
                assert {v.service_media_id for v in versions} == {"physical-file"}
                assert season.size == 100

                source_item_id = "combined"
                if linked:
                    config = ServiceConfig(
                        service_type=service,
                        base_url="http://linked.invalid",
                        api_key="synthetic",
                        name="Linked",
                        enabled=True,
                    )
                    db.add(config)
                    db.add(
                        SeriesServiceRef(
                            series_id=series.id,
                            service=service,
                            service_id="main-show",
                            library_id="library",
                            library_name="TV",
                        )
                    )
                    await db.flush()
                    for name in ("primary_order", "media_order", "linked_order"):
                        monkeypatch.setattr(
                            EpisodeOrderingSnapshot,
                            name,
                            AsyncMock(return_value="aired"),
                        )
                    source_item_id = "linked-combined"
                    linked_seasons = [
                        replace(
                            s,
                            episode_data=[
                                replace(
                                    e,
                                    jellyfin_episode_id=source_item_id
                                    if e.jellyfin_episode_id
                                    else None,
                                    emby_episode_id=source_item_id
                                    if e.emby_episode_id
                                    else None,
                                )
                                for e in s.episode_data
                            ],
                        )
                        for s in seasons.values()
                    ]
                    matches = await _build_series_supplemental_matches(
                        db,
                        config,
                        [
                            AggregatedSeriesData(
                                id="linked-show",
                                name="Show",
                                year=None,
                                service=service,
                                library_name="TV",
                                library_id="library",
                                path=None,
                                added_at=None,
                                external_ids=ExternalIDs(
                                    tmdb=123, tvdb=None, imdb=None, tmdb_collection=None
                                ),
                                size=100,
                                view_count=3,
                                last_viewed_at=None,
                                season_data=linked_seasons,
                            )
                        ],
                    )
                    episode_matches = [m for m in matches if m.episode_id is not None]
                    assert len(episode_matches) == 1
                    assert episode_matches[0].signals["episode_numbers"] == [1, 2]
                    db.add_all(matches)
                    await db.flush()

                (
                    watch_rows,
                    unmatched,
                ) = await MediaWatchSnapshotCache._build_episode_watch_rows(
                    session=db,
                    source_service=service,
                    source_service_config_id=config.id,
                    snapshots=[
                        MediaWatchSnapshot(
                            media_type=MediaType.SERIES,
                            tmdb_id=123,
                            watch_user_key="Alice",
                            source_item_id=source_item_id,
                            last_watched_at=datetime(2026, 10, 19, tzinfo=UTC),
                            play_count=3,
                        )
                    ],
                )
                assert unmatched == 0
                assert {r.episode_number for r in watch_rows} == {1, 2}
                targets = await MediaWatchSnapshotCache._load_native_playback_targets(
                    db
                )
                native = MediaWatchSnapshotCache._build_native_playback_aggregate_rows(
                    targets,
                    [
                        NativePlaybackUser(
                            source_service=service,
                            source_service_config_id=config.id,
                            source_item_id=source_item_id,
                            provider_media_type="episode",
                            source_user_id="alice",
                            source_username="Alice",
                            play_count=3,
                            completed=True,
                        )
                    ],
                )
                assert len(native) == 4
                assert all(row.play_count == 3 for row in native)
                assert {r.target_id for r in native if r.target_scope == "episode"} == {
                    e.id for e in season.episodes
                }

                db.add(
                    PlaybackHistoryEvent(
                        source_service=service,
                        source_service_config_id=config.id,
                        observed_service=service,
                        observed_service_config_id=config.id,
                        source_event_key="one-file-play",
                        source_item_id=source_item_id,
                        provider_media_type="episode",
                        played_at=datetime(2026, 10, 19),
                        duration_seconds=2400,
                        completed=True,
                        source_user_id="alice",
                        source_username="Alice",
                    )
                )
                await db.commit()
            await playback_history.rebuild_playback_history_aggregates()
            async with factory() as db:
                events = list((await db.scalars(select(PlaybackHistoryEvent))).all())
                aggregates = list(
                    (await db.scalars(select(PlaybackHistoryAggregate))).all()
                )
                assert len(events) == 1
                assert events[0].episode_number == 1
                assert len(aggregates) == 4
                assert all(
                    a.play_count == 1 and a.total_duration_seconds == 2400
                    for a in aggregates
                )
                assert len([a for a in aggregates if a.target_scope == "episode"]) == 2
                configs = (await db.scalars(select(ServiceConfig))).all()
                snapshot = await load_selected_history_snapshot(
                    db,
                    playback_history.PlaybackRefreshResult(
                        [
                            playback_history.PlaybackProviderStatus(
                                config_id=c.id,
                                provider=service,
                                observed_service=service,
                                observed_config_id=c.id,
                                available=True,
                            )
                            for c in configs
                        ]
                    ),
                )
                assert len(snapshot.values_by_target) == 4
                for values in snapshot.values_by_target.values():
                    assert values["playback_history.play_count"] == 1
                    assert values["playback_history.total_duration_minutes"] == 40
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
def test_overlapping_files_count_logical_episodes_once(service_class):
    async def run():
        client = service_class("key", "http://media.invalid")
        second = {**combined_item(3), "Id": "second", "IndexNumber": 2}
        try:
            with patch.object(
                client,
                "_make_request",
                AsyncMock(
                    return_value={
                        "Items": [combined_item(), second],
                        "TotalRecordCount": 2,
                    }
                ),
            ):
                sizes, seasons = await client.get_series_sizes_for_library(
                    "library", "user"
                )
            assert sizes == {"show": 200}
            assert seasons[("show", 1)].episode_count == 3
        finally:
            await client.session.close()

    asyncio.run(run())
