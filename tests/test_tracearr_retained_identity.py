from __future__ import annotations

import importlib
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    PlaybackHistoryAggregate,
    PlaybackHistoryEvent,
    Season,
    Series,
    ServiceConfig,
)
from backend.enums import Service
from backend.services import playback_history as history
from backend.services.selected_playback_history import load_selected_history_snapshot


def row(event_id="play", **extra):
    return {
        "id": event_id,
        "server_id": "remote",
        "server_type": "plex",
        "media_type": "episode",
        "rating_key": "old-key",
        "duration_ms": 600_000,
        "stopped_at": "2025-01-01T12:00:00Z",
        "watched": True,
        "tmdb_id": 12345,
        "media_id": "episode",
        "show_media_id": "show",
        "season_number": 1,
        "episode_number": 2,
        **extra,
    }


class TracearrIdentityNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_episode_identity_is_verified_before_recovering_parent(self):
        events = history._normalize_tracearr_events(
            [row(), row("second")], {}, server_id="remote"
        )
        self.assertIsNone(events[0].tmdb_id)
        catalog = {
            "episode": {
                "id": "episode",
                "media_type": "episode",
                "tmdb_id": 12345,
                "show_media_id": "show",
            },
            "show": {"id": "show", "media_type": "show", "tmdb_id": 300},
        }
        client = SimpleNamespace(
            get_media=AsyncMock(side_effect=lambda ref: catalog[ref])
        )
        await history._recover_tracearr_episode_identities(events, client)
        self.assertEqual(client.get_media.await_count, 2)  # cache shared across plays
        for event in events:
            self.assertEqual(
                (event.tmdb_id, event.season_number, event.episode_number), (300, 1, 2)
            )
            self.assertEqual(event.identity_evidence["session"]["tmdb_id"], 12345)
            self.assertEqual(
                event.identity_evidence["provenance"], "tracearr_episode_parent"
            )
            self.assertFalse(history._history_identity_blocked(event))

    async def test_stale_or_uncorroborated_links_never_supply_show_identity(self):
        for episode, session in (
            ({"media_type": "episode", "tmdb_id": 777, "show_media_id": "show"}, {}),
            (
                {
                    "media_type": "episode",
                    "tmdb_id": 12345,
                    "show_media_id": "different-show",
                },
                {},
            ),
            ({"media_type": "movie", "tmdb_id": 12345}, {}),
            ({"media_type": "episode", "show_media_id": "show"}, {}),
            (
                {"media_type": "episode", "tmdb_id": 12345, "show_media_id": "show"},
                {"tmdb_id": None},
            ),
            (None, {}),
        ):
            with self.subTest(episode=episode, session=session):
                events = history._normalize_tracearr_events(
                    [row(**session)], {}, server_id="remote"
                )
                client = SimpleNamespace(get_media=AsyncMock(return_value=episode))
                await history._recover_tracearr_episode_identities(events, client)
                self.assertIsNone(events[0].tmdb_id)
                self.assertFalse(history._has_stable_history_identity(events[0]))
                self.assertLessEqual(client.get_media.await_count, 1)

    async def test_catalog_transport_failure_is_not_treated_as_missing_identity(self):
        events = history._normalize_tracearr_events([row()], {}, server_id="remote")
        client = SimpleNamespace(
            get_media=AsyncMock(side_effect=RuntimeError("offline"))
        )
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await history._recover_tracearr_episode_identities(events, client)

    async def test_verified_episode_without_parent_tmdb_remains_unresolved(self):
        events = history._normalize_tracearr_events([row()], {}, server_id="remote")
        client = SimpleNamespace(
            get_media=AsyncMock(
                side_effect=[
                    {
                        "media_type": "episode",
                        "tmdb_id": 12345,
                        "show_media_id": "show",
                    },
                    {"media_type": "show", "tmdb_id": None},
                ]
            )
        )
        await history._recover_tracearr_episode_identities(events, client)
        self.assertTrue(history._history_identity_blocked(events[0]))
        self.assertIsNone(events[0].tmdb_id)


class PlaybackIdentityMigrationTests(unittest.TestCase):
    def test_upgrade_and_downgrade_preserve_retained_rows(self):
        migration = importlib.import_module(
            "backend.alembic.versions.e6a1c3d8f540_playback_identity_evidence"
        )
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE playback_history_events (id INTEGER PRIMARY KEY, tmdb_id INTEGER)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO playback_history_events (id, tmdb_id) VALUES (1, 100)"
                    )
                )
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    self.assertIn(
                        "identity_evidence",
                        {
                            column["name"]
                            for column in inspect(connection).get_columns(
                                "playback_history_events"
                            )
                        },
                    )
                    self.assertEqual(
                        connection.execute(
                            text(
                                "SELECT tmdb_id, identity_evidence FROM playback_history_events"
                            )
                        ).one(),
                        (100, None),
                    )
                    migration.downgrade()
                self.assertEqual(
                    connection.execute(
                        text("SELECT id, tmdb_id FROM playback_history_events")
                    ).one(),
                    (1, 100),
                )
                self.assertNotIn(
                    "identity_evidence",
                    {
                        column["name"]
                        for column in inspect(connection).get_columns(
                            "playback_history_events"
                        )
                    },
                )
        finally:
            engine.dispose()


class TracearrRetainedIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.server = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex",
                api_key="key",
                enabled=True,
                is_main=True,
            )
            self.provider = ServiceConfig(
                service_type=Service.TRACEARR,
                base_url="http://tracearr",
                api_key="key",
                enabled=True,
            )
            db.add_all([self.server, self.provider])
            await db.flush()
            self.provider.extra_settings = {
                "server_bindings": [
                    {
                        "service_config_id": self.server.id,
                        "server_type": "plex",
                        "tracearr_server_id": "remote",
                    }
                ]
            }
            self.movie = Movie(title="Current movie", tmdb_id=100)
            db.add(self.movie)
            await db.flush()
            self.version = MovieVersion(
                movie_id=self.movie.id,
                service=Service.PLEX,
                service_item_id="reused-key",
                service_media_id="file",
                library_id="lib",
                library_name="Movies",
            )
            db.add(self.version)
            await db.commit()
        self.binding = history._tracearr_bindings(self.provider)[0]
        self.db_patch = patch.object(history, "async_db", self.sessions)
        self.db_patch.start()

    async def asyncTearDown(self):
        self.db_patch.stop()
        await self.engine.dispose()

    async def ingest(self, db, rows):
        return await history._upsert_events(
            db,
            config=self.provider,
            provider=Service.TRACEARR,
            observed_service=Service.PLEX,
            observed_config_id=self.server.id,
            events=history._normalize_tracearr_events(rows, {}, server_id="remote"),
        )

    async def test_recovered_episode_survives_changed_keys_and_multi_episode_rebuilds(
        self,
    ):
        async with self.sessions() as db:
            series = Series(title="Show", tmdb_id=300)
            db.add(series)
            await db.flush()
            season = Season(series_id=series.id, season_number=1)
            db.add(season)
            await db.flush()
            episodes = [
                Episode(
                    season_id=season.id,
                    episode_number=number,
                    plex_rating_key="shared-file",
                )
                for number in (1, 2)
            ]
            db.add_all(episodes)
            await db.commit()
            events = history._normalize_tracearr_events([row()], {}, server_id="remote")
            catalog = {
                "episode": {
                    "id": "episode",
                    "media_type": "episode",
                    "tmdb_id": 12345,
                    "show_media_id": "show",
                },
                "show": {"id": "show", "media_type": "show", "tmdb_id": 300},
            }
            client = SimpleNamespace(
                get_media=AsyncMock(side_effect=lambda ref: catalog[ref])
            )
            await history._recover_tracearr_episode_identities(events, client)
            await history._upsert_events(
                db,
                config=self.provider,
                provider=Service.TRACEARR,
                observed_service=Service.PLEX,
                observed_config_id=self.server.id,
                events=events,
            )
            await db.commit()
            snapshot = await load_selected_history_snapshot(
                db,
                history.PlaybackRefreshResult(
                    [
                        history.PlaybackProviderStatus(
                            config_id=self.provider.id,
                            provider=Service.TRACEARR,
                            observed_service=Service.PLEX,
                            observed_config_id=self.server.id,
                            available=True,
                        )
                    ]
                ),
            )
            self.assertTrue(
                snapshot.values_by_target[("episode", episodes[1].id)][
                    "playback_history.has_completed_watch"
                ]
            )
        # No old item key remains; stable identity still resolves on this server.
        await history.rebuild_playback_history_aggregates()
        async with self.sessions() as db:
            event = await db.scalar(select(PlaybackHistoryEvent))
            self.assertEqual((event.tmdb_id, event.episode_number), (300, 2))
            event.source_item_id = "shared-file"
            await db.commit()
        # A combined file credits both episodes without changing retained identity.
        await history.rebuild_playback_history_aggregates()
        async with self.sessions() as db:
            event = await db.scalar(select(PlaybackHistoryEvent))
            self.assertEqual((event.tmdb_id, event.episode_number), (300, 2))
            aggregates = list(await db.scalars(select(PlaybackHistoryAggregate)))
            self.assertEqual(len(aggregates), 4)
            self.assertTrue(all(aggregate.play_count == 1 for aggregate in aggregates))

    async def test_session_movie_identity_survives_missing_and_reused_item_keys(self):
        async with self.sessions() as db:
            await self.ingest(
                db,
                [
                    row(
                        "movie",
                        media_type="movie",
                        rating_key="reused-key",
                        tmdb_id=999,
                    )
                ],
            )
            await db.commit()
            event = await db.scalar(select(PlaybackHistoryEvent))
            self.assertEqual(event.tmdb_id, 999)
            self.assertIsNone(event.movie_id)
            self.assertEqual(event.identity_evidence["provenance"], "tracearr_session")
        await history.rebuild_playback_history_aggregates()
        async with self.sessions() as db:
            event = await db.scalar(select(PlaybackHistoryEvent))
            self.assertEqual(event.tmdb_id, 999)
            self.assertIsNone(event.movie_id)
            self.assertEqual(
                list(await db.scalars(select(PlaybackHistoryAggregate))), []
            )

    async def test_conflicting_replay_preserves_evidence_and_cannot_credit_current_movie(
        self,
    ):
        async with self.sessions() as db:
            await self.ingest(
                db,
                [
                    row(
                        "movie",
                        media_type="movie",
                        rating_key="reused-key",
                        tmdb_id=999,
                    )
                ],
            )
            await db.commit()
            await self.ingest(
                db,
                [
                    row(
                        "movie",
                        media_type="movie",
                        rating_key="reused-key",
                        tmdb_id=100,
                    )
                ],
            )
            await db.commit()
            event = await db.scalar(select(PlaybackHistoryEvent))
            self.assertTrue(event.identity_evidence["conflict"])
            self.assertEqual(event.tmdb_id, 999)
            self.assertEqual(event.identity_evidence["session"]["tmdb_id"], 100)
            self.assertIsNone(event.movie_id)
            await self.ingest(
                db,
                [
                    row(
                        "movie",
                        media_type="movie",
                        rating_key="reused-key",
                        tmdb_id=100,
                    )
                ],
            )
            await db.commit()
            self.assertEqual(event.tmdb_id, 999)
            self.assertTrue(event.identity_evidence["conflict"])
        await history.rebuild_playback_history_aggregates()
        async with self.sessions() as db:
            self.assertEqual(
                list(await db.scalars(select(PlaybackHistoryAggregate))), []
            )

    async def test_blocked_rows_do_not_starve_identity_backfill(self):
        async with self.sessions() as db:
            for number, evidence in enumerate(
                ({"conflict": True}, {"unverified": True}, None)
            ):
                db.add(
                    PlaybackHistoryEvent(
                        source_service=Service.TRACEARR,
                        source_service_config_id=self.provider.id,
                        source_event_key=f"remote:{number}",
                        source_item_id="reused-key",
                        provider_media_type="movie",
                        observed_service=Service.PLEX,
                        observed_service_config_id=self.server.id,
                        played_at=datetime(2025, 1, 1),
                        duration_seconds=60,
                        identity_evidence=evidence,
                    )
                )
            await db.commit()
        self.assertEqual(
            await history.resolve_unmapped_event_identities(batch_size=1), 1
        )
        async with self.sessions() as db:
            events = list(
                await db.scalars(
                    select(PlaybackHistoryEvent).order_by(PlaybackHistoryEvent.id)
                )
            )
            self.assertEqual([event.tmdb_id for event in events], [None, None, 100])

    async def test_version_replay_crosses_known_pages_and_failed_replay_is_retryable(
        self,
    ):
        async with self.sessions() as db:
            await self.ingest(db, [row("known", media_type="movie", tmdb_id=100)])
            provider = await db.get(ServiceConfig, self.provider.id)
            history._set_tracearr_binding_state(
                provider, self.binding, available=True, error=None, imported_events=1
            )
            settings = dict(provider.extra_settings)
            settings[history.PLAYBACK_HISTORY_STATE_KEY]["bindings"]["remote"][
                "format_version"
            ] = 2
            provider.extra_settings = settings
            await db.commit()

        async def refresh(fail):
            async with self.sessions() as db:
                provider = await db.get(ServiceConfig, self.provider.id)
            page = AsyncMock(
                side_effect=[
                    ([row("known", media_type="movie", tmdb_id=100)], "older"),
                    RuntimeError("catalog offline")
                    if fail
                    else ([row("old", media_type="movie", tmdb_id=999)], None),
                ]
            )
            client = SimpleNamespace(
                get_users_page=AsyncMock(return_value=([], None)), get_history_page=page
            )
            status = await history._refresh_tracearr_binding(
                provider,
                self.binding,
                client,
                movie_min_seconds=15,
                episode_min_seconds=7,
            )
            self.assertEqual(page.await_count, 2)
            return status

        failed = await refresh(True)
        self.assertFalse(failed.available)
        async with self.sessions() as db:
            provider = await db.get(ServiceConfig, self.provider.id)
            self.assertEqual(
                history._tracearr_binding_state(provider, self.binding)[
                    "format_version"
                ],
                2,
            )
        self.assertTrue((await refresh(False)).available)
        async with self.sessions() as db:
            provider = await db.get(ServiceConfig, self.provider.id)
            self.assertEqual(
                history._tracearr_binding_state(provider, self.binding)[
                    "format_version"
                ],
                history.TRACEARR_PLAYBACK_FORMAT_VERSION,
            )
            self.assertEqual(
                set(await db.scalars(select(PlaybackHistoryEvent.source_event_key))),
                {"remote:known", "remote:old"},
            )
            event = await db.scalar(
                select(PlaybackHistoryEvent).where(
                    PlaybackHistoryEvent.source_event_key == "remote:old"
                )
            )
            self.assertEqual(event.tmdb_id, 999)
