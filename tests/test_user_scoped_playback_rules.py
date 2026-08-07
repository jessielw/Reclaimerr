from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.core.rule_engine import (
    RULE_VALUE_UNAVAILABLE,
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    PlaybackUserHistoryResolver,
    evaluate_advanced_rule,
    evaluate_advanced_rule_state,
    validate_rule_definition,
)
from backend.database.models import Episode, Movie, MovieVersion, ReclaimRule, Season, Series
from backend.enums import MediaType, Service
from backend.services.playback_history import PlaybackRuleSnapshot
from backend.tasks import cleanup as cleanup_tasks


def _condition(field: str, operator: str, value: object) -> dict[str, object]:
    return {"type": "condition", "field": field, "operator": operator, "value": value}


def _rule(target_scope: str, media_type: MediaType, condition: dict[str, object]) -> ReclaimRule:
    return ReclaimRule(
        name="user-scoped playback rule",
        media_type=media_type,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": [condition]},
        },
        action={"candidate": True, "media_server_action": "delete"},
    )


def _movie_version(*, duration_ms: float | None) -> tuple[Movie, MovieVersion]:
    movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
    version = MovieVersion(
        movie_id=1,
        service=Service.PLEX,
        service_item_id="item-1",
        service_media_id="media-1",
        library_id="lib-1",
        library_name="Library 1",
        duration=duration_ms,
    )
    return movie, version


class UserScopedPlaybackValidationTests(unittest.TestCase):
    def test_valid_condition_passes(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    _condition(
                        "playback.user_watched_percent",
                        "greater_than_or_equal",
                        {"usernames": ["alice"], "amount": 50},
                    )
                ],
            },
        }
        validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

    def test_empty_usernames_rejected(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    _condition(
                        "playback.user_watched_percent",
                        "greater_than_or_equal",
                        {"usernames": [], "amount": 50},
                    )
                ],
            },
        }
        with self.assertRaises(ValueError):
            validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

    def test_non_dict_value_rejected(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    _condition(
                        "playback.user_watched_percent", "greater_than_or_equal", 50
                    )
                ],
            },
        }
        with self.assertRaises(ValueError):
            validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

    def test_percent_out_of_bounds_rejected(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    _condition(
                        "playback.user_watched_percent",
                        "greater_than_or_equal",
                        {"usernames": ["alice"], "amount": 150},
                    )
                ],
            },
        }
        with self.assertRaises(ValueError):
            validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

    def test_negative_amount_rejected(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    _condition(
                        "playback.user_watched_duration_minutes",
                        "greater_than_or_equal",
                        {"usernames": ["alice"], "amount": -1},
                    )
                ],
            },
        }
        with self.assertRaises(ValueError):
            validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

    def test_percent_field_rejected_for_series_scope(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    _condition(
                        "playback.user_watched_percent",
                        "greater_than_or_equal",
                        {"usernames": ["alice"], "amount": 50},
                    )
                ],
            },
        }
        with self.assertRaises(ValueError):
            validate_rule_definition(definition, target_scope="series")

    def test_exists_operator_rejected(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    {
                        "type": "condition",
                        "field": "playback.user_watched_percent",
                        "operator": "exists",
                    }
                ],
            },
        }
        with self.assertRaises(ValueError):
            validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)


class UserScopedPlaybackEvaluationTests(unittest.TestCase):
    def tearDown(self) -> None:
        # avoid leaking an activated resolver into other test modules that
        # share this process/thread's contextvars
        PlaybackUserHistoryResolver._ctx.set(None)

    def test_reproduces_and_fixes_the_reported_false_positive(self) -> None:
        """userA watched 25s of a 2hr movie; userB watched the whole thing.

        A rule scoped to userA alone must not match, even though an
        aggregate 'anyone watched most of it' signal would.
        """
        movie, version = _movie_version(duration_ms=7200 * 1000)
        PlaybackUserHistoryResolver(
            {
                ("movie_version", 0): {
                    "usera": {
                        "display_username": "UserA",
                        "total_duration_seconds": 25,
                        "play_count": 1,
                        "last_activity_at": None,
                    },
                    "userb": {
                        "display_username": "UserB",
                        "total_duration_seconds": 7200,
                        "play_count": 1,
                        "last_activity_at": None,
                    },
                },
            }
        ).activate()
        version.id = 0

        rule_a = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["UserA"], "amount": 50},
            ),
        )
        rule_b = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["UserB"], "amount": 50},
            ),
        )

        matched_a, criteria_a, _ = evaluate_advanced_rule(
            rule_a, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        matched_b, criteria_b, _ = evaluate_advanced_rule(
            rule_b, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )

        self.assertFalse(matched_a)
        self.assertTrue(matched_b)
        self.assertEqual(criteria_b["playback.user_watched_percent"], 100.0)

        # three-valued path must agree
        self.assertFalse(
            evaluate_advanced_rule_state(
                rule_a, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )
        self.assertTrue(
            evaluate_advanced_rule_state(
                rule_b, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )

    def test_multiple_selected_users_match_if_any_meets_threshold(self) -> None:
        movie, version = _movie_version(duration_ms=100 * 1000)
        PlaybackUserHistoryResolver(
            {
                ("movie_version", 1): {
                    "alice": {
                        "display_username": "alice",
                        "total_duration_seconds": 90,
                        "play_count": 1,
                        "last_activity_at": None,
                    },
                    "bob": {
                        "display_username": "bob",
                        "total_duration_seconds": 10,
                        "play_count": 1,
                        "last_activity_at": None,
                    },
                }
            }
        ).activate()
        version.id = 1

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["alice", "bob"], "amount": 50},
            ),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertTrue(matched)
        self.assertEqual(criteria["playback.user_watched_percent"], 90.0)

    def test_selected_user_with_no_activity_is_a_concrete_zero_not_unavailable(
        self,
    ) -> None:
        movie, version = _movie_version(duration_ms=100 * 1000)
        PlaybackUserHistoryResolver({("movie_version", 2): {}}).activate()
        version.id = 2

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["nobody"], "amount": 1},
            ),
        )
        matched, _, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        # a concrete "false" (0 < 1), not swallowed as unavailable/unknown
        self.assertFalse(matched)
        self.assertFalse(
            evaluate_advanced_rule_state(
                rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
            is None
        )

    def test_missing_runtime_makes_percent_unavailable_not_false(self) -> None:
        movie, version = _movie_version(duration_ms=None)
        PlaybackUserHistoryResolver(
            {
                ("movie_version", 3): {
                    "alice": {
                        "display_username": "alice",
                        "total_duration_seconds": 500,
                        "play_count": 1,
                        "last_activity_at": None,
                    }
                }
            }
        ).activate()
        version.id = 3

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["alice"], "amount": 1},
            ),
        )
        matched, _, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertFalse(matched)  # fails closed for the boolean path
        self.assertIsNone(
            evaluate_advanced_rule_state(
                rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )  # but unknown, not a hard false, for three-valued scan logic

    def test_resolver_never_activated_is_unavailable(self) -> None:
        movie, version = _movie_version(duration_ms=7200 * 1000)
        version.id = 4
        # deliberately do not activate any resolver
        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["alice"], "amount": 1},
            ),
        )
        matched, _, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertFalse(matched)
        self.assertIsNone(
            evaluate_advanced_rule_state(
                rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )

    def test_episode_scope_percent_uses_episode_runtime(self) -> None:
        series = Series(title="Show", tmdb_id=10, size=1)
        season = Season(series_id=1, season_number=1)
        episode = Episode(season_id=1, episode_number=1, runtime=1200)
        episode.id = 5
        PlaybackUserHistoryResolver(
            {
                ("episode", 5): {
                    "alice": {
                        "display_username": "alice",
                        "total_duration_seconds": 600,
                        "play_count": 1,
                        "last_activity_at": None,
                    }
                }
            }
        ).activate()

        rule = _rule(
            TARGET_EPISODE,
            MediaType.SERIES,
            _condition(
                "playback.user_watched_percent",
                "greater_than_or_equal",
                {"usernames": ["alice"], "amount": 50},
            ),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            rule,
            target_scope=TARGET_EPISODE,
            series=series,
            season=season,
            episode=episode,
        )
        self.assertTrue(matched)
        self.assertEqual(criteria["playback.user_watched_percent"], 50.0)

    def test_duration_minutes_field_sums_across_sessions(self) -> None:
        movie, version = _movie_version(duration_ms=None)
        PlaybackUserHistoryResolver(
            {
                ("movie_version", 6): {
                    "alice": {
                        "display_username": "alice",
                        "total_duration_seconds": 5400,  # 90 minutes, summed
                        "play_count": 3,
                        "last_activity_at": None,
                    }
                }
            }
        ).activate()
        version.id = 6

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "greater_than_or_equal",
                {"usernames": ["alice"], "amount": 90},
            ),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertTrue(matched)
        self.assertEqual(criteria["playback.user_watched_duration_minutes"], 90.0)

    def _activate_heavy_and_light_viewer(self, version_id: int) -> None:
        """alice watched 90 minutes of the target, bob watched 1."""
        PlaybackUserHistoryResolver(
            {
                ("movie_version", version_id): {
                    "alice": {
                        "display_username": "alice",
                        "total_duration_seconds": 5400,
                        "play_count": 1,
                        "last_activity_at": None,
                    },
                    "bob": {
                        "display_username": "bob",
                        "total_duration_seconds": 60,
                        "play_count": 1,
                        "last_activity_at": None,
                    },
                }
            }
        ).activate()

    def test_less_than_matches_when_any_selected_user_is_under_the_threshold(
        self,
    ) -> None:
        movie, version = _movie_version(duration_ms=None)
        version.id = 7
        self._activate_heavy_and_light_viewer(7)

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "less_than",
                {"usernames": ["alice", "bob"], "amount": 5},
            ),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        # bob watched 1 minute, so "under 5 minutes by alice or bob" is true of
        # bob even though alice watched 90
        self.assertTrue(matched)
        self.assertEqual(criteria["playback.user_watched_duration_minutes"], 1.0)
        self.assertTrue(
            evaluate_advanced_rule_state(
                rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )

    def test_equals_matches_the_selected_user_that_holds_the_value(self) -> None:
        movie, version = _movie_version(duration_ms=None)
        version.id = 8
        self._activate_heavy_and_light_viewer(8)

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "equals",
                {"usernames": ["alice", "bob"], "amount": 1},
            ),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertTrue(matched)
        self.assertEqual(criteria["playback.user_watched_duration_minutes"], 1.0)

    def test_no_selected_user_meeting_the_threshold_does_not_match(self) -> None:
        movie, version = _movie_version(duration_ms=None)
        version.id = 9
        self._activate_heavy_and_light_viewer(9)

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "greater_than_or_equal",
                {"usernames": ["alice", "bob"], "amount": 120},
            ),
        )
        matched, _, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertFalse(matched)
        self.assertFalse(
            evaluate_advanced_rule_state(
                rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )

    def test_target_no_playback_source_can_observe_is_unavailable(self) -> None:
        """No configured source covers the target, so "nobody watched it" is unknown.

        Without this the target would read as a concrete 0 for every selected
        user, and a "watched less than X" rule would match the whole library
        whenever the playback provider is missing.
        """
        movie, version = _movie_version(duration_ms=None)
        version.id = 10
        PlaybackUserHistoryResolver({}, available_targets=set()).activate()

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "less_than",
                {"usernames": ["alice"], "amount": 30},
            ),
        )
        matched, _, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertFalse(matched)
        self.assertIsNone(
            evaluate_advanced_rule_state(
                rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
            )
        )

    def test_observable_target_with_no_events_is_still_a_concrete_zero(self) -> None:
        movie, version = _movie_version(duration_ms=None)
        version.id = 11
        PlaybackUserHistoryResolver(
            {}, available_targets={("movie_version", 11)}
        ).activate()

        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "less_than",
                {"usernames": ["alice"], "amount": 30},
            ),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            rule, target_scope=TARGET_MOVIE_VERSION, movie=movie, version=version
        )
        self.assertTrue(matched)
        self.assertEqual(criteria["playback.user_watched_duration_minutes"], 0)


class UserScopedPlaybackActivationTests(unittest.IsolatedAsyncioTestCase):
    """Cover what a cleanup scan loads and activates for these fields."""

    def tearDown(self) -> None:
        PlaybackUserHistoryResolver._ctx.set(None)

    @staticmethod
    def _snapshot() -> PlaybackRuleSnapshot:
        return PlaybackRuleSnapshot(
            values_by_target={},
            available_targets={("movie_version", 1), ("movie_version", 2)},
            target_counts={},
            errors=[],
            has_configured_provider=True,
            provider_statuses=[],
            unavailable_reasons={},
            unavailable_target_samples={},
            available_fields_by_target={
                # imported history covers version 1; version 2 is observable
                # only through the media server's own watch state, which
                # carries no play durations to split by user
                ("movie_version", 1): {"playback.total_duration_minutes"},
                ("movie_version", 2): {"playback.has_activity"},
            },
        )

    async def _activate(self, rule: ReclaimRule) -> AsyncMock:
        totals = AsyncMock(return_value={("movie_version", 1): {}})
        with (
            patch.object(cleanup_tasks, "refresh_playback_history", new=AsyncMock()),
            patch.object(
                cleanup_tasks,
                "load_playback_rule_snapshot",
                new=AsyncMock(return_value=self._snapshot()),
            ),
            patch.object(cleanup_tasks, "load_user_playback_totals", new=totals),
        ):
            await cleanup_tasks._activate_playback_history_for_rules(
                AsyncMock(), [rule]
            )
        return totals

    async def test_user_scoped_rule_loads_totals_and_carries_observability(
        self,
    ) -> None:
        totals = await self._activate(
            _rule(
                TARGET_MOVIE_VERSION,
                MediaType.MOVIE,
                _condition(
                    "playback.user_watched_duration_minutes",
                    "greater_than_or_equal",
                    {"usernames": ["alice"], "amount": 30},
                ),
            )
        )
        totals.assert_awaited_once()
        resolver = PlaybackUserHistoryResolver.current()
        assert resolver is not None
        self.assertTrue(resolver.is_available(TARGET_MOVIE_VERSION, 1))
        # no imported history for version 2, so "nobody watched it" is unknown
        # rather than zero for every user
        self.assertFalse(resolver.is_available(TARGET_MOVIE_VERSION, 2))
        self.assertFalse(resolver.is_available(TARGET_MOVIE_VERSION, 3))

    async def test_aggregate_only_rule_does_not_load_per_user_totals(self) -> None:
        totals = await self._activate(
            _rule(
                TARGET_MOVIE_VERSION,
                MediaType.MOVIE,
                _condition("playback.longest_duration_minutes", "greater_than", 30),
            )
        )
        totals.assert_not_awaited()
        # nothing in the scan reads per-user totals, so the activated resolver
        # must not answer "nobody watched this" for targets it never loaded
        resolver = PlaybackUserHistoryResolver.current()
        assert resolver is not None
        self.assertFalse(resolver.is_available(TARGET_MOVIE_VERSION, 1))


class PlaybackPreviewUnavailableCountTests(unittest.TestCase):
    """A preview must report the targets a playback rule cannot read."""

    @staticmethod
    def _snapshot() -> PlaybackRuleSnapshot:
        return PlaybackRuleSnapshot(
            values_by_target={},
            available_targets={("movie_version", 1), ("movie_version", 2)},
            target_counts={},
            errors=[],
            has_configured_provider=True,
            provider_statuses=[],
            unavailable_reasons={},
            unavailable_target_samples={},
            available_fields_by_target={
                ("movie_version", 1): {"playback.longest_duration_minutes"},
                ("movie_version", 2): {"playback.has_activity"},
            },
        )

    def test_user_scoped_rule_counts_targets_without_imported_history(self) -> None:
        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition(
                "playback.user_watched_duration_minutes",
                "less_than",
                {"usernames": ["alice"], "amount": 30},
            ),
        )
        count = cleanup_tasks._playback_unavailable_target_count(
            self._snapshot(),
            [rule],
            {"movie_version": {1, 2, 3}},
        )
        # version 1 has imported history; version 2 is observable but only
        # through native watch state, and version 3 not at all
        self.assertEqual(count, 2)

    def test_aggregate_rule_counts_targets_missing_the_requested_field(self) -> None:
        rule = _rule(
            TARGET_MOVIE_VERSION,
            MediaType.MOVIE,
            _condition("playback.total_duration_minutes", "greater_than", 30),
        )
        count = cleanup_tasks._playback_unavailable_target_count(
            self._snapshot(),
            [rule],
            {"movie_version": {1, 2}},
        )
        # neither target carries the requested field, even though both are
        # observable
        self.assertEqual(count, 2)
