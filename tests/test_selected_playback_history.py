from __future__ import annotations

import importlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.encryption import fer_encrypt
from backend.core.playback_history_fields import HISTORY_RULE_FIELDS
from backend.core.rule_engine import (
    FIELD_ALLOWED_OPERATORS,
    RULE_VALUE_UNAVAILABLE,
    TARGET_SCOPE_ALLOWED_FIELDS,
    PlaybackHistoryResolver,
    _playback_context,
    evaluate_advanced_rule,
    evaluate_advanced_rule_state,
)
from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    MovieVersion,
    PlaybackHistoryEvent,
    ReclaimRule,
    Season,
    Series,
    ServiceConfig,
    SupplementalMediaMatch,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.settings import ServiceConfigUpdate
from backend.services.playback_history import (
    PlaybackProviderStatus,
    PlaybackRefreshResult,
    active_playback_event_clause,
    load_active_tracearr_sources,
)
from backend.services.playback_selection import resolve_history_sources
from backend.services.selected_playback_history import (
    history_values,
    load_selected_history_snapshot,
)


def config(
    config_id: int,
    service: Service,
    *,
    extra: dict[str, Any] | None = None,
    main: bool = False,
    enabled: bool = True,
) -> ServiceConfig:
    instance = ServiceConfig(
        service_type=service,
        name=f"server-{config_id}",
        base_url="http://test",
        api_key="test-key",
        extra_settings=extra or {},
        is_main=main,
        enabled=enabled,
    )
    instance.id = config_id
    return instance


def admin_user() -> User:
    return User(
        username="admin", password_hash="unused", role=UserRole.ADMIN, permissions=[]
    )


class ProviderSelectionTests(unittest.TestCase):
    def test_precedence_is_per_server_and_explicit_preference_wins(self) -> None:
        first = config(1, Service.PLEX, main=True)
        second = config(2, Service.PLEX)
        tautulli = config(3, Service.TAUTULLI, extra={"plex_service_config_id": 2})
        tracearr = config(
            4,
            Service.TRACEARR,
            extra={
                "server_bindings": [
                    {
                        "service_config_id": 1,
                        "server_type": "plex",
                        "tracearr_server_id": "remote",
                    }
                ]
            },
        )
        selected = resolve_history_sources([first, second, tautulli, tracearr])
        self.assertEqual(selected[1].provider, Service.TRACEARR)
        self.assertEqual(selected[2].provider, Service.TAUTULLI)
        first.extra_settings = {"playback_history_provider": "tautulli"}
        selected = resolve_history_sources([first, second, tautulli, tracearr])
        self.assertIsNone(selected[1].provider)
        self.assertIsNotNone(selected[1].error)

    def test_reporting_fallback_and_missing_tracearr_do_not_use_native(self) -> None:
        server = config(1, Service.JELLYFIN)
        self.assertEqual(
            resolve_history_sources([server])[1].provider, Service.JELLYFIN
        )
        server.extra_settings = {"playback_history_provider": "tracearr"}
        self.assertIsNotNone(resolve_history_sources([server])[1].error)

    def test_explicitly_unbound_tautulli_is_not_assigned_to_main(self) -> None:
        server = config(1, Service.PLEX, main=True)
        legacy = config(2, Service.TAUTULLI)
        self.assertEqual(
            resolve_history_sources([server, legacy])[1].provider, Service.TAUTULLI
        )
        legacy.extra_settings = {"plex_service_config_id": None}
        self.assertIsNotNone(resolve_history_sources([server, legacy])[1].error)

    def test_settings_reject_incompatible_preferences_and_invalid_binding_ids(
        self,
    ) -> None:
        for service, extra in [
            (Service.JELLYFIN, {"playback_history_provider": "tautulli"}),
            (Service.PLEX, {"playback_history_provider": "playback_reporting"}),
            (Service.TAUTULLI, {"plex_service_config_id": True}),
        ]:
            with self.assertRaises(ValidationError):
                ServiceConfigUpdate(
                    service_type=service,
                    enabled=True,
                    base_url="http://test",
                    extra_settings=extra,
                )

    def test_all_new_fields_are_valid_in_all_scopes(self) -> None:
        for field in HISTORY_RULE_FIELDS:
            self.assertIn(field, FIELD_ALLOWED_OPERATORS)
            for fields in TARGET_SCOPE_ALLOWED_FIELDS.values():
                self.assertIn(field, fields)
        context = _playback_context(PlaybackHistoryResolver({}), "movie_version", 1)
        self.assertIs(context["playback_history.has_activity"], RULE_VALUE_UNAVAILABLE)


class CompletionValueTests(unittest.TestCase):
    def event(self, when: datetime, completed: bool | None) -> PlaybackHistoryEvent:
        return PlaybackHistoryEvent(
            source_service=Service.TAUTULLI,
            source_event_key="completion-test",
            source_item_id="movie-test",
            provider_media_type="movie",
            played_at=when,
            completed=completed,
            duration_seconds=60,
            source_service_config_id=1,
            source_user_id="1",
            source_username="Alice",
        )

    def test_unknown_completion_cannot_prove_absence_or_latest_completion(self) -> None:
        when = datetime(2025, 1, 1)
        events = [self.event(when, True), self.event(when + timedelta(days=1), None)]
        values = history_values(events, completion_supported=True)
        self.assertTrue(values["playback_history.has_completed_watch"])
        self.assertNotIn("playback_history.last_completed_at", values)
        values = history_values([events[1]], completion_supported=True)
        self.assertNotIn("playback_history.has_completed_watch", values)

    def test_older_unknown_event_does_not_hide_known_latest_completion(self) -> None:
        when = datetime(2025, 1, 1)
        values = history_values(
            [self.event(when, None), self.event(when + timedelta(days=1), True)],
            completion_supported=True,
        )
        self.assertEqual(
            values["playback_history.last_completed_at"], when + timedelta(days=1)
        )

    def test_positive_completion_survives_another_source_without_completion_support(
        self,
    ) -> None:
        values = history_values(
            [self.event(datetime(2025, 1, 1), True)], completion_supported=False
        )
        self.assertTrue(values["playback_history.has_completed_watch"])
        self.assertNotIn("playback_history.last_completed_at", values)

    def test_empty_history_differs_from_unsupported_completion(self) -> None:
        values = history_values([], completion_supported=True)
        self.assertFalse(values["playback_history.has_activity"])
        self.assertFalse(values["playback_history.has_completed_watch"])
        self.assertIsNone(values["playback_history.last_completed_at"])
        self.assertNotIn(
            "playback_history.has_completed_watch",
            history_values([], completion_supported=False),
        )

    def test_unmatched_history_preserves_only_provable_fields(self):
        when = datetime(2025, 1, 2)
        known = self.event(when, True)
        for offset, completion, date_known in (
            (-1, True, True),
            (-1, None, True),
            (0, None, True),
            (1, True, False),
            (1, None, False),
            (1, False, True),
        ):
            with self.subTest(offset=offset, completion=completion):
                uncertain = self.event(when + timedelta(days=offset), completion)
                values = history_values(
                    [known], completion_supported=True, uncertain_events=[uncertain]
                )
                self.assertTrue(values["playback_history.has_completed_watch"])
                self.assertTrue(values["playback_history.has_activity"])
                self.assertNotIn("playback_history.play_count", values)
                self.assertNotIn("playback_history.total_duration_minutes", values)
                self.assertNotIn("playback_history.unique_user_count", values)
                self.assertEqual(
                    "playback_history.last_completed_at" in values, date_known
                )
                self.assertEqual(
                    "playback_history.days_since_last_completed" in values, date_known
                )
                self.assertEqual(
                    "playback_history.last_activity_at" in values, offset <= 0
                )

    def test_unresolved_partial_play_can_prove_no_completion_but_not_no_activity(self):
        values = history_values(
            [],
            completion_supported=True,
            uncertain_events=[self.event(datetime(2025, 1, 1), False)],
        )
        self.assertFalse(values["playback_history.has_completed_watch"])
        self.assertIsNone(values["playback_history.last_completed_at"])
        self.assertNotIn("playback_history.has_activity", values)
        self.assertNotIn("playback_history.play_count", values)


class SelectedHistorySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.plex = ServiceConfig(
                service_type=Service.PLEX,
                name="Main Plex",
                enabled=True,
                is_main=True,
                base_url="http://plex",
                api_key=fer_encrypt("key"),
                extra_settings={"playback_history_provider": "tautulli"},
            )
            db.add(self.plex)
            await db.flush()
            self.tautulli = ServiceConfig(
                service_type=Service.TAUTULLI,
                enabled=True,
                base_url="http://tautulli",
                api_key=fer_encrypt("key"),
                extra_settings={"plex_service_config_id": self.plex.id},
            )
            self.tracearr = ServiceConfig(
                service_type=Service.TRACEARR,
                enabled=True,
                base_url="http://tracearr",
                api_key=fer_encrypt("key"),
                extra_settings={
                    "server_bindings": [
                        {
                            "service_config_id": self.plex.id,
                            "server_type": "plex",
                            "tracearr_server_id": "remote",
                        }
                    ]
                },
            )
            self.movie = Movie(title="Example", tmdb_id=100)
            self.movie.added_at = datetime(2026, 6, 1)
            self.movie.last_viewed_at = datetime(2026, 10, 1)
            self.movie.view_count = 100
            db.add_all([self.tautulli, self.tracearr, self.movie])
            await db.flush()
            self.version = MovieVersion(
                movie_id=self.movie.id,
                service=Service.PLEX,
                service_item_id="same-key",
                service_media_id="file",
                library_id="lib",
                library_name="Movies",
            )
            db.add(self.version)
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def event(
        self,
        provider: ServiceConfig,
        key: str,
        when: datetime,
        *,
        completed: bool | None = True,
        server_id: int | None = None,
        item: str = "same-key",
    ) -> PlaybackHistoryEvent:
        return PlaybackHistoryEvent(
            source_service=provider.service_type,
            source_service_config_id=provider.id,
            source_event_key=key,
            source_item_id=item,
            observed_service=Service.PLEX,
            observed_service_config_id=server_id or self.plex.id,
            provider_media_type="movie",
            played_at=when,
            duration_seconds=600,
            completed=completed,
            source_user_id="alice",
            source_username="Alice",
        )

    def refresh(self, *, available: bool = True) -> PlaybackRefreshResult:
        return PlaybackRefreshResult(
            [
                PlaybackProviderStatus(
                    config_id=self.tautulli.id,
                    provider=Service.TAUTULLI,
                    observed_service=Service.PLEX,
                    observed_config_id=self.plex.id,
                    available=available,
                )
            ]
        )

    async def test_history_ignores_native_and_superseded_provider_and_keeps_old_watches(
        self,
    ) -> None:
        when = datetime(2025, 1, 1)
        async with self.sessions() as db:
            db.add_all(
                [
                    self.event(self.tautulli, "1", when),
                    self.event(self.tracearr, "remote:2", datetime(2026, 10, 1)),
                ]
            )
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            values = snapshot.values_by_target[("movie_version", self.version.id)]
            self.assertEqual(values["playback_history.play_count"], 1)
            self.assertEqual(values["playback_history.last_completed_at"], when)
            self.assertEqual(
                values["_history_sources"],
                [{"server": "Main Plex", "provider": "tautulli"}],
            )
            # SQL consumers use the same server-specific selection.
            active = await load_active_tracearr_sources(db)
            events = (
                await db.scalars(
                    sa.select(PlaybackHistoryEvent).where(
                        active_playback_event_clause(active)
                    )
                )
            ).all()
            self.assertEqual([event.source_event_key for event in events], ["1"])

    async def test_outage_is_unknown_even_with_retained_events(self) -> None:
        async with self.sessions() as db:
            db.add(self.event(self.tautulli, "1", datetime(2025, 1, 1)))
            await db.commit()
            snapshot = await load_selected_history_snapshot(
                db, self.refresh(available=False)
            )
            self.assertNotIn(
                ("movie_version", self.version.id), snapshot.values_by_target
            )

    async def test_empty_history_is_zero_and_no_completion_not_native_watch_count(
        self,
    ) -> None:
        async with self.sessions() as db:
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            values = snapshot.values_by_target[("movie_version", self.version.id)]
            self.assertEqual(values["playback_history.play_count"], 0)
            self.assertFalse(values["playback_history.has_completed_watch"])

    async def test_rule_activation_uses_history_only_and_keeps_source_explanations(
        self,
    ) -> None:
        from backend.api.candidate_views import normalize_reason_parts
        from backend.tasks import cleanup

        rule = ReclaimRule(
            name="Old completed watches",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "playback_history.days_since_last_completed",
                            "operator": "greater_than",
                            "value": 180,
                        }
                    ],
                },
            },
        )
        async with self.sessions() as db:
            db.add(self.event(self.tautulli, "old", datetime(2025, 1, 1)))
            await db.commit()
            with (
                patch.object(
                    cleanup,
                    "refresh_playback_history",
                    new=AsyncMock(return_value=self.refresh()),
                ),
                patch.object(
                    cleanup, "load_playback_rule_snapshot", new=AsyncMock()
                ) as merged,
                patch.object(
                    type(cleanup.media_watch_snapshot_cache),
                    "ensure_fresh_snapshot",
                    new=AsyncMock(),
                ) as native,
            ):
                result = await cleanup._activate_playback_history_for_rules(
                    db, [rule], require_fresh=True
                )
            self.assertEqual(result.unavailable_count, 0)
            native.assert_not_awaited()
            merged.assert_not_awaited()
            matched, _, reasons = evaluate_advanced_rule(
                rule,
                target_scope="movie_version",
                movie=self.movie,
                version=self.version,
            )
            self.assertTrue(matched)
            self.assertIn("Main Plex (Tautulli)", reasons[0]["display"])
            parts = normalize_reason_parts(
                [
                    {
                        "rule_name": rule.name,
                        "target_scope": "movie_version",
                        "conditions": reasons,
                    }
                ],
                {},
            )
            self.assertEqual(
                parts[0].conditions[0].history_sources,
                [{"server": "Main Plex", "provider": "tautulli"}],
            )
            self.assertIn("Main Plex (Tautulli)", parts[0].conditions[0].display)
            self.assertTrue(cleanup._rule_uses_watch_sensitive_fields(rule))
            with patch.object(
                cleanup,
                "refresh_playback_history",
                new=AsyncMock(return_value=self.refresh(available=False)),
            ):
                await cleanup._activate_playback_history_for_rules(
                    db, [rule], require_fresh=True
                )
            self.assertIsNone(
                evaluate_advanced_rule_state(
                    rule,
                    target_scope="movie_version",
                    movie=self.movie,
                    version=self.version,
                )
            )
        PlaybackHistoryResolver({}).activate()

    async def test_provider_switch_immediately_changes_history_without_native_merge(
        self,
    ) -> None:
        async with self.sessions() as db:
            db.add_all(
                [
                    self.event(self.tautulli, "old", datetime(2025, 1, 1)),
                    self.event(self.tracearr, "remote:new", datetime(2026, 1, 1)),
                ]
            )
            server = await db.get(ServiceConfig, self.plex.id)
            assert server is not None
            server.extra_settings = {"playback_history_provider": "tracearr"}
            await db.commit()
            refresh = PlaybackRefreshResult(
                [
                    PlaybackProviderStatus(
                        config_id=self.tracearr.id,
                        provider=Service.TRACEARR,
                        observed_service=Service.PLEX,
                        observed_config_id=self.plex.id,
                        available=True,
                    )
                ]
            )
            snapshot = await load_selected_history_snapshot(db, refresh)
            values = snapshot.values_by_target[("movie_version", self.version.id)]
            self.assertEqual(values["playback_history.play_count"], 1)
            self.assertEqual(
                values["playback_history.last_completed_at"], datetime(2026, 1, 1)
            )
            # A status for the previously selected provider is not fresh evidence.
            stale = await load_selected_history_snapshot(db, self.refresh())
            self.assertNotIn(("movie_version", self.version.id), stale.values_by_target)

    async def test_episode_completion_rolls_up_to_season_and_series_activity(
        self,
    ) -> None:
        async with self.sessions() as db:
            series = Series(title="Show", tmdb_id=300)
            db.add(series)
            await db.flush()
            season = Season(series_id=series.id, season_number=1)
            db.add(season)
            await db.flush()
            episode = Episode(
                season_id=season.id, episode_number=1, plex_rating_key="ep-1"
            )
            db.add(episode)
            await db.flush()
            event = self.event(
                self.tautulli, "episode", datetime(2025, 1, 1), item="ep-1"
            )
            event.provider_media_type = "episode"
            db.add(event)
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            for key in [
                ("episode", episode.id),
                ("season", season.id),
                ("series", series.id),
            ]:
                self.assertEqual(
                    snapshot.values_by_target[key]["playback_history.play_count"], 1
                )
                self.assertTrue(
                    snapshot.values_by_target[key][
                        "playback_history.has_completed_watch"
                    ]
                )

    async def test_unidentified_history_prevents_returning_partial_totals(self) -> None:
        async with self.sessions() as db:
            db.add_all(
                [
                    self.event(self.tautulli, "mapped", datetime(2025, 1, 1)),
                    self.event(
                        self.tautulli,
                        "unmapped",
                        datetime(2026, 1, 1),
                        item="unknown-key",
                    ),
                ]
            )
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            values = snapshot.values_by_target[("movie_version", self.version.id)]
            self.assertNotIn("playback_history.play_count", values)
            self.assertTrue(values["playback_history.has_completed_watch"])
            self.assertNotIn("playback_history.days_since_last_completed", values)

    async def test_unresolved_episode_does_not_hide_movie_history_for_either_provider(
        self,
    ):
        async with self.sessions() as db:
            for provider in (self.tautulli, self.tracearr):
                with self.subTest(provider=provider.service_type):
                    server = await db.get(ServiceConfig, self.plex.id)
                    server.extra_settings = {
                        "playback_history_provider": provider.service_type.value
                    }
                    prefix = "remote:" if provider is self.tracearr else ""
                    known = self.event(provider, prefix + "known", datetime(2025, 1, 1))
                    unknown = self.event(
                        provider,
                        prefix + "unknown",
                        datetime(2026, 1, 1),
                        item="removed-episode",
                    )
                    unknown.provider_media_type = "episode"
                    db.add_all([known, unknown])
                    await db.commit()
                    refresh = PlaybackRefreshResult(
                        [
                            PlaybackProviderStatus(
                                config_id=provider.id,
                                provider=provider.service_type,
                                observed_service=Service.PLEX,
                                observed_config_id=self.plex.id,
                                available=True,
                            )
                        ]
                    )
                    snapshot = await load_selected_history_snapshot(db, refresh)
                    values = snapshot.values_by_target[
                        ("movie_version", self.version.id)
                    ]
                    self.assertEqual(values["playback_history.play_count"], 1)
                    self.assertTrue(values["playback_history.has_completed_watch"])
                    self.assertEqual(
                        values["playback_history.last_completed_at"], known.played_at
                    )

    async def test_unresolved_movie_does_not_hide_episode_rollups(self):
        async with self.sessions() as db:
            series = Series(title="Show", tmdb_id=300)
            db.add(series)
            await db.flush()
            season = Season(series_id=series.id, season_number=1)
            db.add(season)
            await db.flush()
            episode = Episode(
                season_id=season.id, episode_number=1, plex_rating_key="ep-1"
            )
            db.add(episode)
            known = self.event(
                self.tautulli, "known-episode", datetime(2025, 1, 1), item="ep-1"
            )
            known.provider_media_type = "episode"
            db.add_all(
                [
                    known,
                    self.event(
                        self.tautulli,
                        "unknown-movie",
                        datetime(2026, 1, 1),
                        item="removed",
                    ),
                ]
            )
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            for scope, target in (
                ("episode", episode),
                ("season", season),
                ("series", series),
            ):
                self.assertEqual(
                    snapshot.values_by_target[(scope, target.id)][
                        "playback_history.play_count"
                    ],
                    1,
                )

    async def test_reused_item_key_uses_retained_identity_on_the_same_server(self):
        async with self.sessions() as db:
            old_movie = Movie(title="Original movie", tmdb_id=999)
            db.add(old_movie)
            await db.flush()
            old_version = MovieVersion(
                movie_id=old_movie.id,
                service=Service.PLEX,
                service_item_id="new-key-for-original",
                service_media_id="original-file",
                library_id="lib",
                library_name="Movies",
            )
            db.add(old_version)
            old_play = self.event(
                self.tautulli, "before-key-reuse", datetime(2025, 1, 1)
            )
            old_play.tmdb_id = 999
            db.add(old_play)
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            self.assertEqual(
                snapshot.values_by_target[("movie_version", self.version.id)][
                    "playback_history.play_count"
                ],
                0,
            )
            self.assertEqual(
                snapshot.values_by_target[("movie_version", old_version.id)][
                    "playback_history.play_count"
                ],
                1,
            )

    async def test_unresolved_history_never_makes_an_unwatched_rule_match(self):
        async with self.sessions() as db:
            db.add(
                self.event(
                    self.tautulli, "unknown", datetime(2025, 1, 1), item="missing"
                )
            )
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            PlaybackHistoryResolver(snapshot.values_by_target).activate()
            try:
                for field, operator, value in (
                    ("playback_history.play_count", "equals", 0),
                    ("playback_history.has_completed_watch", "is_false", None),
                    ("playback_history.last_completed_at", "not_exists", None),
                ):
                    rule = ReclaimRule(
                        name="No unsafe deletion",
                        media_type=MediaType.MOVIE,
                        target_scope="movie_version",
                        definition={
                            "version": 1,
                            "root": {
                                "type": "condition",
                                "field": field,
                                "operator": operator,
                                "value": value,
                            },
                        },
                    )
                    self.assertIsNone(
                        evaluate_advanced_rule_state(
                            rule,
                            target_scope="movie_version",
                            movie=self.movie,
                            version=self.version,
                        )
                    )
            finally:
                PlaybackHistoryResolver({}).activate()

    async def test_conflicted_identity_remains_unknown_even_with_a_matching_item_key(
        self,
    ):
        async with self.sessions() as db:
            event = self.event(self.tautulli, "conflict", datetime(2026, 1, 1))
            event.tmdb_id = self.movie.tmdb_id
            event.identity_evidence = {"conflict": True}
            db.add(event)
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, self.refresh())
            values = snapshot.values_by_target.get(
                ("movie_version", self.version.id), {}
            )
            self.assertNotIn("playback_history.play_count", values)
            self.assertNotIn("playback_history.has_completed_watch", values)

    async def test_settings_roundtrip_preserves_preferences_and_invalidates_stale_state(
        self,
    ) -> None:
        from backend.api.routes.settings import services

        async with self.sessions() as db:
            server = await db.get(ServiceConfig, self.plex.id)
            assert server is not None
            server.extra_settings = {
                **(server.extra_settings or {}),
                "custom_setting": 42,
                "playback_history_sync": {
                    "last_attempt_at": "2026-01-01",
                    "aggregate_format_version": 2,
                },
            }
            await db.commit()
            with (
                patch.object(
                    services.service_manager,
                    "test_service",
                    new=AsyncMock(return_value=(True, "")),
                ),
                patch.object(
                    services,
                    "enqueue_background_job",
                    new=AsyncMock(return_value=SimpleNamespace(id=1)),
                ),
                patch.object(services, "enqueue_task_run", new=AsyncMock()) as refresh,
            ):
                response = await services.set_service_settings(
                    ServiceConfigUpdate(
                        id=self.plex.id,
                        name="Main Plex",
                        service_type=Service.PLEX,
                        base_url="http://plex",
                        enabled=True,
                        is_main=True,
                        extra_settings={
                            "playback_history_provider": "tracearr",
                            "playback_history_sync": {"last_attempt_at": "2099-01-01"},
                        },
                    ),
                    admin_user(),
                    db,
                )
                self.assertEqual(
                    response["data"]["extra_settings"]["custom_setting"], 42
                )
                self.assertNotIn(
                    "last_attempt_at",
                    response["data"]["extra_settings"]["playback_history_sync"],
                )
                refresh.assert_awaited()
                # Older clients and the promote-to-main action omit extras.
                await services.set_service_settings(
                    ServiceConfigUpdate(
                        id=self.plex.id,
                        name="Main Plex",
                        service_type=Service.PLEX,
                        base_url="http://plex",
                        enabled=True,
                        is_main=True,
                    ),
                    admin_user(),
                    db,
                )
            result = await services.get_service_settings(admin_user(), db)
            payload = result[Service.PLEX]["instances"][0]
            self.assertEqual(
                payload["extra_settings"]["playback_history_provider"], "tracearr"
            )
            self.assertEqual(payload["history_source"]["provider"], "tracearr")

    async def test_tautulli_rebinding_resets_ownership_mapping_and_cursor(self) -> None:
        from backend.api.routes.settings import services

        async with self.sessions() as db:
            linked = ServiceConfig(
                service_type=Service.PLEX,
                name="Linked",
                enabled=True,
                base_url="http://linked",
                api_key=fer_encrypt("key"),
            )
            db.add(linked)
            event = self.event(self.tautulli, "retained", datetime(2025, 1, 1))
            event.movie_id, event.tmdb_id = self.movie.id, self.movie.tmdb_id
            db.add(event)
            await db.flush()
            provider = await db.get(ServiceConfig, self.tautulli.id)
            assert provider is not None
            provider.extra_settings = {
                **(provider.extra_settings or {}),
                "playback_history_sync": {
                    "last_success_at": "2026-01-01",
                    "last_attempt_at": "2026-01-01",
                },
            }
            await db.commit()
            with (
                patch.object(
                    services.service_manager,
                    "test_service",
                    new=AsyncMock(return_value=(True, "")),
                ),
                patch.object(
                    services,
                    "enqueue_background_job",
                    new=AsyncMock(return_value=SimpleNamespace(id=1)),
                ),
                patch.object(services, "enqueue_task_run", new=AsyncMock()),
            ):
                response = await services.set_service_settings(
                    ServiceConfigUpdate(
                        id=provider.id,
                        service_type=Service.TAUTULLI,
                        base_url="http://tautulli",
                        enabled=True,
                        extra_settings={"plex_service_config_id": linked.id},
                    ),
                    admin_user(),
                    db,
                )
            await db.refresh(event)
            self.assertEqual(event.observed_service_config_id, linked.id)
            self.assertIsNone(event.movie_id)
            self.assertIsNone(event.tmdb_id)
            self.assertNotIn(
                "last_success_at",
                response["data"]["extra_settings"]["playback_history_sync"],
            )

    async def test_ingestion_uses_only_selected_provider_and_supports_two_plex_servers(
        self,
    ) -> None:
        from backend.services import playback_history

        with (
            patch.object(playback_history, "async_db", self.sessions),
            patch.object(
                playback_history,
                "_refresh_tautulli_config",
                new=AsyncMock(return_value=self.refresh().statuses[0]),
            ) as tautulli,
            patch.object(
                playback_history,
                "_refresh_tracearr_config",
                new=AsyncMock(return_value=([], True)),
            ) as tracearr,
            patch.object(
                playback_history, "resolve_unmapped_event_identities", new=AsyncMock()
            ),
            patch.object(
                playback_history, "rebuild_playback_history_aggregates", new=AsyncMock()
            ),
            patch.object(
                playback_history, "_mark_aggregate_format_current", new=AsyncMock()
            ),
        ):
            await playback_history.refresh_playback_history(force=True)
            tautulli.assert_awaited_once()
            tracearr.assert_not_awaited()
            async with self.sessions() as db:
                main = await db.get(ServiceConfig, self.plex.id)
                assert main is not None
                main.extra_settings = {"playback_history_provider": "automatic"}
                linked = ServiceConfig(
                    service_type=Service.PLEX,
                    name="Tautulli Plex",
                    enabled=True,
                    base_url="http://linked",
                    api_key=fer_encrypt("key"),
                )
                db.add(linked)
                await db.flush()
                provider = await db.get(ServiceConfig, self.tautulli.id)
                assert provider is not None
                provider.extra_settings = {"plex_service_config_id": linked.id}
                await db.commit()
            tautulli.reset_mock()
            await playback_history.refresh_playback_history(force=True)
            tautulli.assert_awaited_once()
            tracearr.assert_awaited_once()
            self.assertEqual(
                [
                    binding.service_config_id
                    for binding in tracearr.call_args.kwargs["bindings"]
                ],
                [self.plex.id],
            )

    async def test_same_type_server_selection_and_item_id_collision_are_isolated(
        self,
    ) -> None:
        async with self.sessions() as db:
            linked = ServiceConfig(
                service_type=Service.PLEX,
                name="Linked Plex",
                enabled=True,
                base_url="http://linked",
                api_key=fer_encrypt("key"),
                extra_settings={"playback_history_provider": "tracearr"},
            )
            other = Movie(title="Other", tmdb_id=200)
            db.add_all([linked, other])
            await db.flush()
            other_version = MovieVersion(
                movie_id=other.id,
                service=Service.PLEX,
                service_item_id="other-key",
                service_media_id="other-file",
                library_id="lib",
                library_name="Movies",
            )
            db.add(other_version)
            await db.flush()
            db.add(
                SupplementalMediaMatch(
                    source_service=Service.PLEX,
                    source_service_config_id=linked.id,
                    source_item_id="same-key",
                    media_type=MediaType.MOVIE,
                    movie_id=other.id,
                )
            )
            tracearr = await db.get(ServiceConfig, self.tracearr.id)
            assert tracearr is not None
            tracearr.extra_settings = {
                "server_bindings": [
                    {
                        "service_config_id": linked.id,
                        "server_type": "plex",
                        "tracearr_server_id": "linked",
                    }
                ]
            }
            db.add_all(
                [
                    self.event(self.tautulli, "1", datetime(2025, 1, 1)),
                    self.event(
                        self.tracearr,
                        "linked:2",
                        datetime(2025, 2, 1),
                        server_id=linked.id,
                    ),
                ]
            )
            await db.commit()
            refresh = self.refresh()
            refresh.statuses.append(
                PlaybackProviderStatus(
                    config_id=self.tracearr.id,
                    provider=Service.TRACEARR,
                    observed_service=Service.PLEX,
                    observed_config_id=linked.id,
                    available=True,
                )
            )
            snapshot = await load_selected_history_snapshot(db, refresh)
            self.assertEqual(
                snapshot.values_by_target[("movie_version", self.version.id)][
                    "playback_history.last_activity_at"
                ],
                datetime(2025, 1, 1),
            )
            self.assertEqual(
                snapshot.values_by_target[("movie_version", other_version.id)][
                    "playback_history.last_activity_at"
                ],
                datetime(2025, 2, 1),
            )
            partial = await load_selected_history_snapshot(db, self.refresh())
            self.assertIn(("movie_version", self.version.id), partial.values_by_target)
            self.assertNotIn(
                ("movie_version", other_version.id), partial.values_by_target
            )
            # Unknown plays on the linked Plex must not hide the main Plex's totals.
            db.add(
                self.event(
                    self.tracearr,
                    "linked:missing",
                    datetime(2026, 1, 1),
                    server_id=linked.id,
                    item="removed",
                )
            )
            await db.commit()
            snapshot = await load_selected_history_snapshot(db, refresh)
            self.assertEqual(
                snapshot.values_by_target[("movie_version", self.version.id)][
                    "playback_history.play_count"
                ],
                1,
            )
            linked_values = snapshot.values_by_target[
                ("movie_version", other_version.id)
            ]
            self.assertTrue(linked_values["playback_history.has_completed_watch"])
            self.assertNotIn("playback_history.play_count", linked_values)

    async def test_mixed_rule_keeps_history_unavailability_and_its_error(self) -> None:
        from backend.tasks import cleanup

        async with self.sessions() as db:
            base = await load_selected_history_snapshot(db, self.refresh())
            key = ("movie_version", self.version.id)
            base.values_by_target = {key: {"playback.total_duration_minutes": 10}}
            base.available_fields_by_target = {key: {"playback.total_duration_minutes"}}
            server = await db.get(ServiceConfig, self.plex.id)
            assert server is not None
            server.extra_settings = {"playback_history_provider": "tracearr"}
            provider = await db.get(ServiceConfig, self.tracearr.id)
            assert provider is not None
            provider.enabled = False
            await db.commit()
            rule = ReclaimRule(
                name="Mixed",
                media_type=MediaType.MOVIE,
                target_scope="movie_version",
                definition={
                    "version": 1,
                    "root": {
                        "type": "group",
                        "op": "and",
                        "children": [
                            {
                                "type": "condition",
                                "field": "playback.total_duration_minutes",
                                "operator": "greater_than",
                                "value": 1,
                            },
                            {
                                "type": "condition",
                                "field": "playback_history.play_count",
                                "operator": "equals",
                                "value": 0,
                            },
                        ],
                    },
                },
            )
            with (
                patch.object(
                    cleanup,
                    "refresh_playback_history",
                    new=AsyncMock(return_value=PlaybackRefreshResult()),
                ),
                patch.object(
                    cleanup,
                    "load_playback_rule_snapshot",
                    new=AsyncMock(return_value=base),
                ),
            ):
                result = await cleanup._activate_playback_history_for_rules(db, [rule])
            self.assertTrue(result.blocking)
            assert result.error is not None
            self.assertIn("not configured", result.error)
            self.assertIsNone(
                evaluate_advanced_rule_state(
                    rule,
                    target_scope="movie_version",
                    movie=self.movie,
                    version=self.version,
                )
            )
        PlaybackHistoryResolver({}).activate()


class TautulliBindingMigrationTests(unittest.TestCase):
    def test_migration_binds_only_unambiguous_legacy_settings_and_is_idempotent(
        self,
    ) -> None:
        migration = importlib.import_module(
            "backend.alembic.versions.c9e4f7a2b601_bind_tautulli_playback_history"
        )
        engine = sa.create_engine("sqlite:///:memory:")
        metadata = sa.MetaData()
        configs = sa.Table(
            "service_configs",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("service_type", sa.String),
            sa.Column("enabled", sa.Boolean),
            sa.Column("is_main", sa.Boolean),
            sa.Column("extra_settings", sa.JSON),
        )
        events = sa.Table(
            "playback_history_events",
            metadata,
            sa.Column("source_service_config_id", sa.Integer),
            sa.Column("observed_service_config_id", sa.Integer),
        )
        metadata.create_all(engine)
        with engine.begin() as db:
            db.execute(
                configs.insert(),
                [
                    {
                        "id": 1,
                        "service_type": "PLEX",
                        "enabled": True,
                        "is_main": True,
                        "extra_settings": {},
                    },
                    {
                        "id": 2,
                        "service_type": "TAUTULLI",
                        "enabled": True,
                        "is_main": False,
                        "extra_settings": {"timeout": 30},
                    },
                ],
            )
            db.execute(
                events.insert().values(
                    source_service_config_id=2, observed_service_config_id=None
                )
            )
            with patch.object(migration.op, "get_bind", return_value=db):
                migration.upgrade()
                migration.upgrade()
            settings = db.scalar(
                sa.select(configs.c.extra_settings).where(configs.c.id == 2)
            )
            assert settings is not None
            self.assertEqual(settings["plex_service_config_id"], 1)
            self.assertEqual(settings["timeout"], 30)
            self.assertEqual(
                db.scalar(sa.select(events.c.observed_service_config_id)), 1
            )
            # Two Plex servers without a main must not be guessed.
            db.execute(configs.update().where(configs.c.id == 1).values(is_main=False))
            db.execute(
                configs.insert().values(
                    id=3,
                    service_type="PLEX",
                    enabled=True,
                    is_main=False,
                    extra_settings={},
                )
            )
            db.execute(
                configs.update()
                .where(configs.c.id == 2)
                .values(extra_settings={"timeout": 30})
            )
            db.execute(events.update().values(observed_service_config_id=None))
            with patch.object(migration.op, "get_bind", return_value=db):
                migration.upgrade()
            settings = db.scalar(
                sa.select(configs.c.extra_settings).where(configs.c.id == 2)
            )
            assert settings is not None
            self.assertIsNone(settings["plex_service_config_id"])
            self.assertIsNone(db.scalar(sa.select(events.c.observed_service_config_id)))
        engine.dispose()
