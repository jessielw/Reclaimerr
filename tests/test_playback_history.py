from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.services import playback_history
from backend.services.jellyfin import JellyfinService
from backend.services.playback_history import (
    _config_api_key,
    _normalize_reporting_events,
    _normalize_tautulli_events,
    _refresh_reporting_config,
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
            ]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_item_id, "movie-1")

    def test_tautulli_uses_row_id_and_utc_timestamp(self) -> None:
        events = _normalize_tautulli_events(
            [
                {
                    "row_id": 42,
                    "media_type": "episode",
                    "rating_key": "9001",
                    "user_id": "7",
                    "play_duration": 120,
                    "stopped": 1_750_000_000,
                }
            ]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source_event_key, "42")
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
        self.assertEqual(len(PLAYBACK_RULE_FIELDS), 7)


class TautulliHistoryTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertIsNotNone(movie)
            assert movie is not None
            self.assertEqual(movie.view_count, 1)
            self.assertEqual(movie.last_viewed_at, datetime(2026, 2, 1))

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
