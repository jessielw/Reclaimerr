from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.rules import get_playback_users
from backend.core.encryption import fer_encrypt
from backend.core.rule_engine import (
    PLAYBACK_RULE_FIELDS,
    RULE_VALUE_UNAVAILABLE,
    PlaybackHistoryResolver,
)
from backend.database import Base
from backend.database.models import (
    Movie,
    MovieVersion,
    PlaybackHistoryAggregate,
    PlaybackHistoryEvent,
    Season,
    Series,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.services import playback_history
from backend.services.jellyfin import JellyfinService
from backend.services.playback_history import (
    PlaybackProviderStatus,
    PlaybackRefreshResult,
    _aggregate_values,
    _backfill_event_usernames,
    _config_api_key,
    _insert_new_events,
    _normalize_reporting_events,
    _normalize_tautulli_events,
    _playback_unavailable_reason,
    _refresh_reporting_config,
    _upsert_events,
    load_playback_rule_snapshot,
    log_playback_rule_coverage,
    rebuild_playback_history_aggregates,
)
from backend.services.tautulli import TautulliClient


class PlaybackHistoryNormalizationTests(unittest.TestCase):
    def test_config_api_key_decrypts_stored_key(self) -> None:
        config = ServiceConfig(
            service_type=Service.TAUTULLI,
            base_url="http://tautulli",
            api_key=fer_encrypt("plain-key"),
            enabled=True,
        )
        config.id = 4

        self.assertEqual(_config_api_key(config), "plain-key")

    def test_config_api_key_allows_plaintext_test_key(self) -> None:
        config = ServiceConfig(
            service_type=Service.TAUTULLI,
            base_url="http://tautulli",
            api_key="plain-test-key",
            enabled=True,
        )
        config.id = 4

        self.assertEqual(_config_api_key(config), "plain-test-key")

    def test_playback_reporting_filters_short_events(self) -> None:
        events = _normalize_reporting_events(
            [
                {
                    "DateCreated": "2026-06-20 12:00:00",
                    "UserId": "user-1",
                    "ItemId": "movie-1",
                    "ItemType": "Movie",
                    "PlayDuration": 15,
                },
                {
                    "DateCreated": "2026-06-20 12:01:00",
                    "UserId": "user-1",
                    "ItemId": "episode-1",
                    "ItemType": "Episode",
                    "PlayDuration": 6,
                },
            ],
            {"user-1": "Alice"},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_item_id, "movie-1")
        self.assertEqual(events[0].source_username, "Alice")

    def test_tautulli_uses_row_id_and_utc_timestamp(self) -> None:
        events = _normalize_tautulli_events(
            [
                {
                    "row_id": 42,
                    "media_type": "episode",
                    "rating_key": "9001",
                    "user_id": "7",
                    "user": "Alice",
                    "play_duration": 120,
                    "stopped": 1_750_000_000,
                }
            ]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_event_key, "42")
        self.assertEqual(events[0].source_username, "Alice")
        self.assertIsNone(events[0].played_at.tzinfo)

    def test_resolver_returns_unavailable_for_unobserved_target(self) -> None:
        resolver = PlaybackHistoryResolver(
            {
                ("series", 1): {
                    "playback.has_activity": True,
                    "playback.play_count": 3,
                }
            }
        )
        self.assertEqual(
            resolver.resolve("series", 1, "playback.play_count"),
            3,
        )
        self.assertIs(
            resolver.resolve("series", 2, "playback.play_count"),
            RULE_VALUE_UNAVAILABLE,
        )
        self.assertEqual(len(PLAYBACK_RULE_FIELDS), 8)

    def test_unknown_reason_distinguishes_provider_failure_and_no_provider(
        self,
    ) -> None:
        self.assertEqual(
            _playback_unavailable_reason(set(), PlaybackRefreshResult()),
            "no playback provider configured",
        )
        self.assertEqual(
            _playback_unavailable_reason(
                {Service.PLEX},
                PlaybackRefreshResult(
                    statuses=[
                        PlaybackProviderStatus(
                            config_id=4,
                            provider=Service.TAUTULLI,
                            observed_service=Service.PLEX,
                            available=False,
                            error="connection failed",
                        )
                    ]
                ),
            ),
            "playback provider refresh failed for plex",
        )


class TautulliHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_users_returns_tautulli_user_rows(self) -> None:
        client = TautulliClient(api_key="key", base_url="http://tautulli")
        response = {
            "response": {
                "data": [
                    {"user_id": "7", "username": "Alice"},
                    {"user_id": "8", "username": "Bob"},
                ]
            }
        }
        with patch.object(
            TautulliClient,
            "_make_request",
            new=AsyncMock(return_value=response),
        ) as request:
            users = await client.get_users()
        await client.session.close()

        self.assertEqual([user["username"] for user in users], ["Alice", "Bob"])
        request.assert_awaited_once_with("get_users")

    async def test_history_records_use_one_ungrouped_paginated_pass(self) -> None:
        client = TautulliClient(api_key="key", base_url="http://tautulli")
        responses = [
            {
                "response": {
                    "data": {
                        "recordsFiltered": 3,
                        "data": [
                            {"row_id": 1, "media_type": "movie"},
                            {"row_id": 2, "media_type": "episode"},
                        ],
                    }
                }
            },
            {
                "response": {
                    "data": {
                        "recordsFiltered": 3,
                        "data": [{"row_id": 3, "media_type": "track"}],
                    }
                }
            },
        ]
        with patch.object(
            TautulliClient,
            "_make_request",
            new=AsyncMock(side_effect=responses),
        ) as request:
            records = await client.get_history_records(page_size=2)
        await client.session.close()

        self.assertEqual([record["row_id"] for record in records], [1, 2])
        self.assertEqual(request.await_count, 2)
        first_call = request.await_args_list[0]
        self.assertEqual(first_call.kwargs["grouping"], 0)
        self.assertEqual(first_call.kwargs["start"], 0)
        self.assertEqual(request.await_args_list[1].kwargs["start"], 2)

    async def test_history_records_use_after_for_incremental_cutoff(self) -> None:
        client = TautulliClient(api_key="key", base_url="http://tautulli")
        response = {
            "response": {
                "data": {
                    "recordsFiltered": 0,
                    "data": [],
                }
            }
        }
        with patch.object(
            TautulliClient,
            "_make_request",
            new=AsyncMock(return_value=response),
        ) as request:
            await client.get_history_records(since=datetime(2026, 6, 23), page_size=2)
        await client.session.close()

        call = request.await_args_list[0]
        self.assertEqual(call.kwargs["after"], "2026-06-22")
        self.assertNotIn("start_date", call.kwargs)


class PlaybackReportingQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_query_avoids_sqlite_datetime_function(self) -> None:
        client = JellyfinService(api_key="key", base_url="http://jellyfin")
        with patch.object(
            JellyfinService,
            "_submit_playback_custom_query_rows",
            new=AsyncMock(return_value=[]),
        ) as request:
            await client.get_playback_reporting_events(
                since=datetime(2026, 6, 23, 13, 45),
                page_size=100,
            )
        await client.session.close()

        self.assertIsNotNone(request.await_args)
        assert request.await_args is not None
        query = request.await_args.args[0]
        self.assertIn(
            "SELECT DateCreated, UserId, ItemId, ItemType, PlayDuration",
            query,
        )
        self.assertIn("DateCreated >= '2026-06-23 13:45:00'", query)
        self.assertNotIn("datetime(", query)
        self.assertNotIn("DateCreatedUtc", query)


class PlaybackHistoryAggregateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp_root = Path("tests/.tmp")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = tmp_root / f"test_playback_{uuid4().hex}.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        self.sessionmaker = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db_patch = patch.object(
            playback_history,
            "async_db",
            self.sessionmaker,
        )
        self.db_patch.start()

    async def asyncTearDown(self) -> None:
        self.db_patch.stop()
        await self.engine.dispose()
        if self.db_path.exists():
            self.db_path.unlink()

    async def test_snapshot_logs_mixed_service_unknown_reason(self) -> None:
        async with self.sessionmaker() as db:
            plex_movie = Movie(title="Plex Movie", tmdb_id=101)
            jellyfin_movie = Movie(title="Jellyfin Movie", tmdb_id=102)
            db.add_all([plex_movie, jellyfin_movie])
            await db.flush()
            plex_version = MovieVersion(
                movie_id=plex_movie.id,
                service=Service.PLEX,
                service_item_id="plex-item",
                service_media_id="plex-media",
                library_id="plex-library",
                library_name="Movies",
            )
            jellyfin_version = MovieVersion(
                movie_id=jellyfin_movie.id,
                service=Service.JELLYFIN,
                service_item_id="jellyfin-item",
                service_media_id="jellyfin-media",
                library_id="jellyfin-library",
                library_name="Movies",
            )
            db.add_all([plex_version, jellyfin_version])
            await db.commit()
            plex_version_id = plex_version.id
            jellyfin_version_id = jellyfin_version.id

            refresh_result = PlaybackRefreshResult(
                statuses=[
                    PlaybackProviderStatus(
                        config_id=7,
                        provider=Service.TAUTULLI,
                        observed_service=Service.PLEX,
                        available=True,
                    )
                ]
            )
            snapshot = await load_playback_rule_snapshot(db, refresh_result)

        self.assertIn(("movie_version", plex_version_id), snapshot.available_targets)
        self.assertNotIn(
            ("movie_version", jellyfin_version_id), snapshot.available_targets
        )
        self.assertEqual(snapshot.target_counts["movie_version"], 2)
        self.assertEqual(snapshot.unavailable_count({"movie_version"}), 1)
        self.assertEqual(
            snapshot.unavailable_reasons["movie_version"],
            {
                "media service not covered by an available playback provider: jellyfin": 1
            },
        )
        self.assertEqual(
            snapshot.unavailable_target_samples["movie_version"],
            [jellyfin_version_id],
        )

        with (
            patch.object(playback_history.LOG, "warning") as warning,
            patch.object(playback_history.LOG, "debug") as debug,
        ):
            log_playback_rule_coverage(snapshot, {"movie_version"})

        debug_messages = [call.args[0] for call in debug.call_args_list]
        self.assertTrue(
            any("total=2, evaluable=1, unknown=1" in message for message in debug_messages)
        )
        self.assertIn(
            "media service not covered by an available playback provider: jellyfin=1",
            warning.call_args.args[0],
        )
        self.assertTrue(
            any(str(jellyfin_version_id) in message for message in debug_messages)
        )

    async def test_snapshot_explains_missing_season_identity(self) -> None:
        async with self.sessionmaker() as db:
            series = Series(title="Series", tmdb_id=201)
            db.add(series)
            await db.flush()
            season = Season(series_id=series.id, season_number=1)
            db.add(season)
            await db.commit()
            season_id = season.id

            snapshot = await load_playback_rule_snapshot(
                db,
                PlaybackRefreshResult(
                    statuses=[
                        PlaybackProviderStatus(
                            config_id=7,
                            provider=Service.TAUTULLI,
                            observed_service=Service.PLEX,
                            available=True,
                        )
                    ]
                ),
            )

        self.assertEqual(snapshot.unavailable_count({"season"}), 1)
        self.assertEqual(
            snapshot.unavailable_reasons["season"],
            {"missing media-server identity": 1},
        )
        self.assertEqual(snapshot.unavailable_target_samples["season"], [season_id])

    async def test_snapshot_treats_retained_history_as_evaluable(self) -> None:
        async with self.sessionmaker() as db:
            movie = Movie(title="Retained History", tmdb_id=301)
            db.add(movie)
            await db.flush()
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="plex-item",
                service_media_id="plex-media",
                library_id="plex-library",
                library_name="Movies",
            )
            db.add(version)
            await db.flush()
            db.add(
                PlaybackHistoryAggregate(
                    target_scope="movie_version",
                    target_id=version.id,
                    media_type=MediaType.MOVIE,
                    play_count=1,
                    total_duration_seconds=120,
                    longest_duration_seconds=120,
                    unique_user_count=1,
                    usernames=["Alice"],
                    usernames_complete=True,
                    first_activity_at=datetime(2026, 1, 1),
                    last_activity_at=datetime(2026, 1, 1),
                )
            )
            await db.commit()
            version_id = version.id

            snapshot = await load_playback_rule_snapshot(
                db,
                PlaybackRefreshResult(
                    statuses=[
                        PlaybackProviderStatus(
                            config_id=7,
                            provider=Service.TAUTULLI,
                            observed_service=Service.PLEX,
                            available=False,
                            error="connection failed",
                        )
                    ]
                ),
            )

        self.assertIn(("movie_version", version_id), snapshot.available_targets)
        self.assertEqual(snapshot.unavailable_count({"movie_version"}), 0)
        self.assertEqual(snapshot.unavailable_reasons["movie_version"], {})

    async def test_upsert_events_deduplicates_reporting_batch(self) -> None:
        reporting_row = {
            "DateCreated": "2026-06-28 16:58:19.712081",
            "UserId": "user-1",
            "ItemId": "episode-1",
            "ItemType": "Episode",
            "PlayDuration": 1214,
        }
        events = _normalize_reporting_events([reporting_row, reporting_row])

        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.flush()
            first_imported = await _upsert_events(
                db,
                config=config,
                provider=Service.JELLYFIN,
                observed_service=Service.JELLYFIN,
                events=events,
            )
            await db.commit()
            config_id = config.id

        async with self.sessionmaker() as db:
            config = await db.get(ServiceConfig, config_id)
            self.assertIsNotNone(config)
            assert config is not None
            second_imported = await _upsert_events(
                db,
                config=config,
                provider=Service.JELLYFIN,
                observed_service=Service.JELLYFIN,
                events=events,
            )
            await db.commit()
            stored_events = (
                (await db.execute(select(PlaybackHistoryEvent))).scalars().all()
            )

        self.assertEqual(first_imported, 1)
        self.assertEqual(second_imported, 0)
        self.assertEqual(len(stored_events), 1)
        self.assertEqual(stored_events[0].source_event_key, events[0].source_event_key)

    async def test_jellyfin_resolves_users_without_tautulli(self) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.commit()

        reporting_rows = [
            {
                "DateCreated": "2026-06-20 12:00:00",
                "UserId": "user-1",
                "ItemId": "movie-1",
                "ItemType": "Movie",
                "PlayDuration": 30,
            }
        ]
        with (
            patch.object(
                JellyfinService,
                "get_playback_reporting_plugin_status",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                JellyfinService,
                "get_users",
                new=AsyncMock(
                    return_value=[SimpleNamespace(id="user-1", name="Alice")]
                ),
            ),
            patch.object(
                JellyfinService,
                "get_playback_reporting_events",
                new=AsyncMock(return_value=reporting_rows),
            ),
        ):
            status = await _refresh_reporting_config(config)

        async with self.sessionmaker() as db:
            event = (await db.execute(select(PlaybackHistoryEvent))).scalar_one()

        self.assertTrue(status.available)
        self.assertEqual(event.source_username, "Alice")

    async def test_username_lookup_failure_keeps_playback_provider_available(
        self,
    ) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.commit()

        reporting_rows = [
            {
                "DateCreated": "2026-06-20 12:00:00",
                "UserId": "user-1",
                "ItemId": "movie-1",
                "ItemType": "Movie",
                "PlayDuration": 30,
            }
        ]
        with (
            patch.object(
                JellyfinService,
                "get_playback_reporting_plugin_status",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                JellyfinService,
                "get_users",
                new=AsyncMock(side_effect=RuntimeError("users unavailable")),
            ),
            patch.object(
                JellyfinService,
                "get_playback_reporting_events",
                new=AsyncMock(return_value=reporting_rows),
            ),
        ):
            status = await _refresh_reporting_config(config)

        async with self.sessionmaker() as db:
            event = (await db.execute(select(PlaybackHistoryEvent))).scalar_one()

        self.assertTrue(status.available)
        self.assertIsNone(event.source_username)

    async def test_insert_new_events_ignores_existing_event_conflict(self) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.flush()
            db.add(
                PlaybackHistoryEvent(
                    source_service=Service.JELLYFIN,
                    source_service_config_id=config.id,
                    source_event_key="existing-key",
                    source_item_id="episode-1",
                    provider_media_type="episode",
                    played_at=datetime(2026, 6, 28, 16, 58),
                    duration_seconds=1214,
                    source_user_id="user-1",
                )
            )
            await db.commit()

            inserted = await _insert_new_events(
                db,
                [
                    {
                        "source_service": Service.JELLYFIN,
                        "source_service_config_id": config.id,
                        "source_event_key": "existing-key",
                        "source_item_id": "episode-1",
                        "provider_media_type": "episode",
                        "played_at": datetime(2026, 6, 28, 16, 58),
                        "duration_seconds": 1214,
                        "source_user_id": "user-1",
                        "tmdb_id": None,
                        "season_number": None,
                        "episode_number": None,
                        "movie_id": None,
                        "series_id": None,
                        "season_id": None,
                        "episode_id": None,
                    }
                ],
            )
            await db.commit()
            stored_events = (
                (await db.execute(select(PlaybackHistoryEvent))).scalars().all()
            )

        self.assertEqual(inserted, 0)
        self.assertEqual(len(stored_events), 1)

    async def test_durable_totals_include_pre_readd_history_but_watch_does_not(
        self,
    ) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            movie = Movie(title="Movie", tmdb_id=123)
            db.add_all([config, movie])
            await db.flush()
            movie.added_at = datetime(2026, 1, 1)
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.JELLYFIN,
                service_item_id="movie-item",
                service_media_id="movie-source",
                library_id="library",
                library_name="Movies",
            )
            db.add(version)
            await db.flush()
            db.add_all(
                [
                    PlaybackHistoryEvent(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_event_key="old",
                        source_item_id="movie-item",
                        provider_media_type="movie",
                        played_at=datetime(2025, 1, 1),
                        duration_seconds=120,
                        source_user_id="user",
                        source_username="Alice",
                        tmdb_id=movie.tmdb_id,
                        movie_id=movie.id,
                    ),
                    PlaybackHistoryEvent(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_event_key="new",
                        source_item_id="movie-item",
                        provider_media_type="movie",
                        played_at=datetime(2026, 2, 1),
                        duration_seconds=180,
                        source_user_id="user",
                        source_username="Alice",
                        tmdb_id=movie.tmdb_id,
                        movie_id=movie.id,
                    ),
                ]
            )
            await db.commit()
            movie_id = movie.id
            version_id = version.id

        await rebuild_playback_history_aggregates()

        async with self.sessionmaker() as db:
            aggregate = (
                await db.execute(
                    select(PlaybackHistoryAggregate).where(
                        PlaybackHistoryAggregate.target_scope == "movie_version",
                        PlaybackHistoryAggregate.target_id == version_id,
                    )
                )
            ).scalar_one()
            movie = await db.get(Movie, movie_id)
            self.assertEqual(aggregate.media_type, MediaType.MOVIE)
            self.assertEqual(aggregate.play_count, 2)
            self.assertEqual(aggregate.total_duration_seconds, 300)
            self.assertEqual(aggregate.unique_user_count, 1)
            self.assertEqual(aggregate.usernames, ["Alice"])
            self.assertTrue(aggregate.usernames_complete)
            self.assertIsNotNone(movie)
            assert movie is not None
            self.assertEqual(movie.view_count, 1)
            self.assertEqual(movie.last_viewed_at, datetime(2026, 2, 1))

    async def test_unresolved_username_marks_only_username_field_unavailable(
        self,
    ) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            movie = Movie(title="Movie", tmdb_id=456)
            db.add_all([config, movie])
            await db.flush()
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.JELLYFIN,
                service_item_id="movie-item",
                service_media_id="movie-source",
                library_id="library",
                library_name="Movies",
            )
            db.add(version)
            await db.flush()
            db.add(
                PlaybackHistoryEvent(
                    source_service=Service.JELLYFIN,
                    source_service_config_id=config.id,
                    source_event_key="unresolved",
                    source_item_id="movie-item",
                    provider_media_type="movie",
                    played_at=datetime(2026, 2, 1),
                    duration_seconds=180,
                    source_user_id="missing-user",
                    tmdb_id=movie.tmdb_id,
                    movie_id=movie.id,
                )
            )
            await db.commit()
            version_id = version.id

        await rebuild_playback_history_aggregates()

        async with self.sessionmaker() as db:
            aggregate = (
                await db.execute(
                    select(PlaybackHistoryAggregate).where(
                        PlaybackHistoryAggregate.target_scope == "movie_version",
                        PlaybackHistoryAggregate.target_id == version_id,
                    )
                )
            ).scalar_one()

        values = _aggregate_values(aggregate)
        self.assertFalse(aggregate.usernames_complete)
        self.assertEqual(values["playback.play_count"], 1)
        self.assertNotIn("playback.usernames", values)

    async def test_partial_username_resolution_keeps_resolved_usernames_available(
        self,
    ) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            movie = Movie(title="Movie", tmdb_id=456)
            db.add_all([config, movie])
            await db.flush()
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.JELLYFIN,
                service_item_id="movie-item",
                service_media_id="movie-source",
                library_id="library",
                library_name="Movies",
            )
            db.add(version)
            await db.flush()
            db.add_all(
                [
                    PlaybackHistoryEvent(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_event_key="resolved",
                        source_item_id="movie-item",
                        provider_media_type="movie",
                        played_at=datetime(2026, 2, 1),
                        duration_seconds=180,
                        source_user_id="known-user",
                        source_username="norirara",
                        tmdb_id=movie.tmdb_id,
                        movie_id=movie.id,
                    ),
                    PlaybackHistoryEvent(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=config.id,
                        source_event_key="unresolved",
                        source_item_id="movie-item",
                        provider_media_type="movie",
                        played_at=datetime(2026, 2, 2),
                        duration_seconds=180,
                        source_user_id="missing-user",
                        tmdb_id=movie.tmdb_id,
                        movie_id=movie.id,
                    ),
                ]
            )
            await db.commit()
            version_id = version.id

        await rebuild_playback_history_aggregates()

        async with self.sessionmaker() as db:
            aggregate = (
                await db.execute(
                    select(PlaybackHistoryAggregate).where(
                        PlaybackHistoryAggregate.target_scope == "movie_version",
                        PlaybackHistoryAggregate.target_id == version_id,
                    )
                )
            ).scalar_one()

        values = _aggregate_values(aggregate)
        self.assertFalse(aggregate.usernames_complete)
        self.assertEqual(values["playback.usernames"], ["norirara"])

    async def test_backfills_retained_event_usernames(self) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.flush()
            event = PlaybackHistoryEvent(
                source_service=Service.JELLYFIN,
                source_service_config_id=config.id,
                source_event_key="retained",
                source_item_id="movie-item",
                provider_media_type="movie",
                played_at=datetime(2026, 2, 1),
                duration_seconds=180,
                source_user_id="user-1",
            )
            db.add(event)
            await db.commit()

            await _backfill_event_usernames(
                db,
                config_id=config.id,
                usernames_by_id={"user-1": "Alice"},
            )
            await db.commit()
            await db.refresh(event)

            self.assertEqual(event.source_username, "Alice")

    async def test_playback_user_lookup_deduplicates_case_and_sources(self) -> None:
        async with self.sessionmaker() as db:
            jellyfin = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            tautulli = ServiceConfig(
                service_type=Service.TAUTULLI,
                base_url="http://tautulli",
                api_key="key",
                enabled=True,
            )
            db.add_all([jellyfin, tautulli])
            await db.flush()
            db.add_all(
                [
                    PlaybackHistoryEvent(
                        source_service=Service.JELLYFIN,
                        source_service_config_id=jellyfin.id,
                        source_event_key="jf-alice",
                        source_item_id="movie-1",
                        provider_media_type="movie",
                        played_at=datetime(2026, 2, 1),
                        duration_seconds=180,
                        source_user_id="1",
                        source_username="Alice",
                    ),
                    PlaybackHistoryEvent(
                        source_service=Service.TAUTULLI,
                        source_service_config_id=tautulli.id,
                        source_event_key="tautulli-alice",
                        source_item_id="movie-2",
                        provider_media_type="movie",
                        played_at=datetime(2026, 2, 1),
                        duration_seconds=180,
                        source_user_id="2",
                        source_username="alice",
                    ),
                ]
            )
            await db.commit()

            admin = User(
                username="admin",
                password_hash="hash",
                role=UserRole.ADMIN,
                permissions=[],
            )
            users = await get_playback_users(admin, db, q="ali", limit=100)

        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].username, "Alice")
        self.assertEqual(users[0].source_services, ["jellyfin", "tautulli"])

    async def test_database_write_failure_is_not_reported_as_provider_failure(
        self,
    ) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.commit()
            config_id = config.id

        reporting_rows = [
            {
                "DateCreated": "2026-06-20 12:00:00",
                "UserId": "user-1",
                "ItemId": "movie-1",
                "ItemType": "Movie",
                "PlayDuration": 30,
            }
        ]
        write_error = OperationalError(
            "INSERT INTO playback_history_events ...",
            {},
            RuntimeError("database is locked"),
        )
        with (
            patch.object(
                JellyfinService,
                "get_playback_reporting_plugin_status",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                JellyfinService,
                "get_playback_reporting_events",
                new=AsyncMock(return_value=reporting_rows),
            ),
            patch.object(
                JellyfinService,
                "get_users",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                playback_history,
                "_upsert_events",
                new=AsyncMock(side_effect=write_error),
            ),
        ):
            with self.assertRaises(OperationalError):
                await _refresh_reporting_config(config)

        async with self.sessionmaker() as db:
            stored = await db.get(ServiceConfig, config_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertNotIn(
                playback_history.PLAYBACK_HISTORY_STATE_KEY,
                stored.extra_settings or {},
            )

    async def test_provider_failure_is_persisted_as_unavailable(self) -> None:
        async with self.sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.JELLYFIN,
                base_url="http://jellyfin",
                api_key="key",
                enabled=True,
            )
            db.add(config)
            await db.commit()
            config_id = config.id

        with patch.object(
            JellyfinService,
            "get_playback_reporting_plugin_status",
            new=AsyncMock(side_effect=RuntimeError("provider offline")),
        ):
            result = await _refresh_reporting_config(config)

        self.assertFalse(result.available)
        self.assertIn("provider offline", result.error or "")
        async with self.sessionmaker() as db:
            stored = await db.get(ServiceConfig, config_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            state = (stored.extra_settings or {})[
                playback_history.PLAYBACK_HISTORY_STATE_KEY
            ]
            self.assertFalse(state["available"])
            self.assertIn("provider offline", state["last_error"])


class PlaybackRefreshSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_are_serialized_and_force_is_preserved(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        forces: list[bool] = []
        active = 0
        max_active = 0

        async def fake_refresh(*, force: bool = False):
            nonlocal active, max_active
            forces.append(force)
            active += 1
            max_active = max(max_active, active)
            entered.set()
            await release.wait()
            active -= 1
            return playback_history.PlaybackRefreshResult()

        with (
            patch.object(playback_history, "_playback_refresh_lock", asyncio.Lock()),
            patch.object(
                playback_history,
                "_refresh_playback_history_once",
                new=fake_refresh,
            ),
        ):
            first = asyncio.create_task(
                playback_history.refresh_playback_history(force=False)
            )
            await entered.wait()
            second = asyncio.create_task(
                playback_history.refresh_playback_history(force=True)
            )
            await asyncio.sleep(0)
            self.assertEqual(forces, [False])
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(forces, [False, True])
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
