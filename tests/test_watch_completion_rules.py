"""Whole-series and per-user "fully watched" rule fields.

Two questions no existing field answered. `series.fully_watched` rolls Sonarr's
episode inventory up across every regular season so a rule can remove an ended
show once it has actually been finished. `playback.fully_watched_usernames`
names the people who individually finished a target, which the aggregate
`season.fully_watched` cannot: the media server unions every viewer's progress
together, so a season one person finished and another dipped into reads as
complete for both.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.core.rule_engine import (
    RULE_VALUE_UNAVAILABLE,
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    WatchCompletionResolver,
    evaluate_advanced_rule,
    evaluate_advanced_rule_state,
    validate_rule_definition,
)
from backend.database import Base
from backend.database.models import (
    Episode,
    MediaWatchUserEpisode,
    PlaybackHistoryEvent,
    ReclaimRule,
    Season,
    Series,
    SeriesServiceRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.models.cleanup import RulePreviewMatchMetadata
from backend.tasks import cleanup as cleanup_tasks

NOW = datetime(2026, 8, 1, tzinfo=UTC)
ADDED = datetime(2026, 1, 1, tzinfo=UTC)


def _condition(field: str, operator: str, value: object = None) -> dict[str, object]:
    condition: dict[str, object] = {
        "type": "condition",
        "field": field,
        "operator": operator,
    }
    if operator not in {"exists", "not_exists", "is_true", "is_false"}:
        condition["value"] = value
    return condition


def _rule(target_scope: str, *conditions: dict[str, object]) -> ReclaimRule:
    return ReclaimRule(
        name="watch completion rule",
        media_type=MediaType.SERIES,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": list(conditions)},
        },
        action={"candidate": True, "media_server_action": "delete"},
    )


def _episode(number: int, *, watched: bool) -> Episode:
    episode = Episode(
        season_id=1,
        episode_number=number,
        view_count=1 if watched else 0,
    )
    episode.added_at = ADDED
    if watched:
        episode.last_viewed_at = NOW
    return episode


def _season(
    number: int,
    *,
    inventory: list[int] | None,
    watched: set[int],
) -> Season:
    season = Season(series_id=1, season_number=number, size=1024)
    season.sonarr_episode_numbers = inventory
    season.added_at = ADDED
    season.episodes = [
        _episode(n, watched=n in watched) for n in (inventory or watched or [])
    ]
    return season


def _series(*seasons: Season) -> Series:
    series = Series(title="Show", tmdb_id=900, size=1024)
    series.added_at = ADDED
    series.service_refs = [
        SeriesServiceRef(
            series_id=1,
            service=Service.PLEX,
            service_id="plex-series",
            library_id="tv",
            library_name="TV",
        )
    ]
    series.seasons = list(seasons)
    return series


class SeriesWatchProgressTests(unittest.TestCase):
    """`series.fully_watched` rolls every regular season up into one verdict."""

    def _state(self, series: Series, condition: dict[str, object]) -> bool | None:
        return evaluate_advanced_rule_state(
            _rule(TARGET_SERIES, condition), target_scope=TARGET_SERIES, series=series
        )

    def test_true_when_every_regular_season_is_complete(self) -> None:
        series = _series(
            _season(1, inventory=[1, 2], watched={1, 2}),
            _season(2, inventory=[1], watched={1}),
        )
        self.assertIs(
            self._state(series, _condition("series.fully_watched", "is_true")), True
        )

    def test_false_when_one_season_is_short(self) -> None:
        series = _series(
            _season(1, inventory=[1, 2], watched={1, 2}),
            _season(2, inventory=[1, 2], watched={1}),
        )
        self.assertIs(
            self._state(series, _condition("series.fully_watched", "is_true")), False
        )
        # and the negative form does match, because this is a known "no"
        self.assertIs(
            self._state(series, _condition("series.fully_watched", "is_false")), True
        )

    def test_unaired_episodes_keep_the_series_incomplete(self) -> None:
        """Sonarr knows about an episode nobody can have watched yet."""
        series = _series(_season(1, inventory=[1, 2, 3], watched={1, 2}))
        self.assertIs(
            self._state(series, _condition("series.fully_watched", "is_true")), False
        )

    def test_unknown_when_a_regular_season_has_no_sonarr_inventory(self) -> None:
        """One unanswerable season must not be judged on the others.

        Answering from the seasons that did report would let an `is false`
        cleanup rule delete a finished show, and an `is true` one delete a show
        Sonarr simply has not been synced for.
        """
        series = _series(
            _season(1, inventory=[1], watched={1}),
            _season(2, inventory=None, watched={1}),
        )
        for operator in ("is_true", "is_false"):
            with self.subTest(operator=operator):
                self.assertIsNone(
                    self._state(series, _condition("series.fully_watched", operator))
                )

    def test_unknown_without_any_regular_season(self) -> None:
        series = _series(_season(0, inventory=[1], watched={1}))
        self.assertIsNone(
            self._state(series, _condition("series.fully_watched", "is_true"))
        )

    def test_specials_are_excluded(self) -> None:
        """A season 0 nobody watched cannot hold a finished show back."""
        series = _series(
            _season(0, inventory=[1, 2], watched=set()),
            _season(1, inventory=[1], watched={1}),
        )
        self.assertIs(
            self._state(series, _condition("series.fully_watched", "is_true")), True
        )

    def test_percent_is_episode_weighted_across_seasons(self) -> None:
        """Not an average of season percentages: a long season counts for more."""
        series = _series(
            _season(1, inventory=[1, 2, 3, 4, 5, 6, 7, 8, 9], watched=set()),
            _season(2, inventory=[1], watched={1}),
        )
        matched, criteria, _ = evaluate_advanced_rule(
            _rule(
                TARGET_SERIES,
                _condition("series.watched_percent", "greater_than_or_equal", 10),
            ),
            target_scope=TARGET_SERIES,
            series=series,
        )
        self.assertTrue(matched)
        # 1 of 10 episodes, not the 50% a mean of 0% and 100% would give
        self.assertEqual(criteria["series.watched_percent"], 10.0)

    def test_reason_names_the_percentage_behind_the_verdict(self) -> None:
        series = _series(_season(1, inventory=[1, 2], watched={1, 2}))
        _matched, _criteria, reasons = evaluate_advanced_rule(
            _rule(TARGET_SERIES, _condition("series.fully_watched", "is_true")),
            target_scope=TARGET_SERIES,
            series=series,
        )
        self.assertIn(
            "100% of Sonarr's episode list watched",
            " ".join(str(detail) for reason in reasons for detail in reason["details"]),
        )

    def test_series_fields_are_rejected_on_other_scopes(self) -> None:
        for scope in (TARGET_SEASON, TARGET_EPISODE, TARGET_MOVIE_VERSION):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError) as raised:
                    validate_rule_definition(
                        _rule(
                            scope, _condition("series.fully_watched", "is_true")
                        ).definition,
                        target_scope=scope,
                    )
                self.assertIn("series.fully_watched", str(raised.exception))


class WatchCompletionEvaluationTests(unittest.TestCase):
    """`Fully watched by users` compares each named person on their own."""

    def tearDown(self) -> None:
        WatchCompletionResolver._ctx.set(None)

    @staticmethod
    def _series_with_season() -> tuple[Series, Season]:
        series = _series(_season(1, inventory=[1, 2], watched={1, 2}))
        season = series.seasons[0]
        return series, season

    def _matches(self, operator: str, usernames: list[str]) -> bool:
        series, season = self._series_with_season()
        matched, _criteria, _reasons = evaluate_advanced_rule(
            _rule(
                TARGET_SEASON,
                _condition("playback.fully_watched_usernames", operator, usernames),
            ),
            target_scope=TARGET_SEASON,
            series=series,
            season=season,
        )
        return matched

    def test_reproduces_and_fixes_the_reported_false_positive(self) -> None:
        """alice finished the season; bob watched one episode of it.

        The aggregate `season.fully_watched` is true here -- between them they
        covered every episode -- and `Playback users matches all` is true too,
        because both of them played something. Neither is the question the rule
        meant to ask.
        """
        WatchCompletionResolver({(TARGET_SEASON, 900, 1, None): {"alice"}}).activate()

        self.assertFalse(self._matches("contains_all", ["alice", "bob"]))
        self.assertTrue(self._matches("contains_all", ["alice"]))
        self.assertTrue(self._matches("contains_any", ["alice", "bob"]))
        self.assertFalse(self._matches("not_contains_any", ["alice", "bob"]))

    def test_matches_all_when_both_finished(self) -> None:
        WatchCompletionResolver(
            {(TARGET_SEASON, 900, 1, None): {"alice", "bob"}}
        ).activate()
        self.assertTrue(self._matches("contains_all", ["alice", "bob"]))

    def test_names_are_matched_case_insensitively(self) -> None:
        WatchCompletionResolver({(TARGET_SEASON, 900, 1, None): {"alice"}}).activate()
        self.assertTrue(self._matches("contains_all", ["Alice"]))

    def test_nobody_finished_it_is_a_known_no(self) -> None:
        """An observable season nobody completed must satisfy `matches none`."""
        WatchCompletionResolver({(TARGET_SEASON, 900, 1, None): set()}).activate()
        self.assertTrue(self._matches("not_contains_any", ["alice", "bob"]))
        self.assertFalse(self._matches("contains_any", ["alice", "bob"]))

    def test_unknown_target_matches_nothing_at_all(self) -> None:
        """Including `matches none`, which would otherwise delete on missing data."""
        WatchCompletionResolver({}).activate()
        for operator in ("contains_all", "contains_any", "not_contains_any", "exists"):
            with self.subTest(operator=operator):
                self.assertFalse(self._matches(operator, ["alice", "bob"]))

        series, season = self._series_with_season()
        self.assertIsNone(
            evaluate_advanced_rule_state(
                _rule(
                    TARGET_SEASON,
                    _condition(
                        "playback.fully_watched_usernames",
                        "not_contains_any",
                        ["alice"],
                    ),
                ),
                target_scope=TARGET_SEASON,
                series=series,
                season=season,
            )
        )

    def test_unavailable_without_an_activated_resolver(self) -> None:
        series, season = self._series_with_season()
        self.assertIsNone(
            evaluate_advanced_rule_state(
                _rule(
                    TARGET_SEASON,
                    _condition(
                        "playback.fully_watched_usernames", "contains_all", ["alice"]
                    ),
                ),
                target_scope=TARGET_SEASON,
                series=series,
                season=season,
            )
        )

    def test_series_and_episode_scopes_read_their_own_targets(self) -> None:
        series = _series(_season(1, inventory=[1, 2], watched={1, 2}))
        season = series.seasons[0]
        episode = season.episodes[1]
        WatchCompletionResolver(
            {
                (TARGET_SERIES, 900, None, None): {"alice"},
                (TARGET_EPISODE, 900, 1, 2): {"bob"},
            }
        ).activate()

        series_matched, _c, _r = evaluate_advanced_rule(
            _rule(
                TARGET_SERIES,
                _condition(
                    "playback.fully_watched_usernames", "contains_all", ["alice"]
                ),
            ),
            target_scope=TARGET_SERIES,
            series=series,
        )
        episode_matched, _c, _r = evaluate_advanced_rule(
            _rule(
                TARGET_EPISODE,
                _condition("playback.fully_watched_usernames", "contains_all", ["bob"]),
            ),
            target_scope=TARGET_EPISODE,
            series=series,
            season=season,
            episode=episode,
        )
        self.assertTrue(series_matched)
        self.assertTrue(episode_matched)

    def test_resolver_reports_unavailable_for_an_unseeded_target(self) -> None:
        resolver = WatchCompletionResolver({(TARGET_SERIES, 900, None, None): {"a"}})
        self.assertEqual(resolver.resolve(TARGET_SERIES, 900), ["a"])
        self.assertIs(resolver.resolve(TARGET_SERIES, 901), RULE_VALUE_UNAVAILABLE)
        self.assertIs(resolver.resolve(TARGET_SERIES, None), RULE_VALUE_UNAVAILABLE)


class WatchCompletionActivationTests(unittest.IsolatedAsyncioTestCase):
    """What a scan actually loads, against a real database."""

    async def asyncSetUp(self) -> None:
        tmp_root = Path("tests/.tmp")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self._db_path = tmp_root / f"test_watch_completion_{uuid4().hex}.db"
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._async_db_patch = patch.object(
            cleanup_tasks, "async_db", self._sessionmaker
        )
        self._media_watch_async_db_patch = patch(
            "backend.services.media_watch_snapshot_cache.async_db",
            self._sessionmaker,
        )
        self._async_db_patch.start()
        self._media_watch_async_db_patch.start()

    async def asyncTearDown(self) -> None:
        WatchCompletionResolver._ctx.set(None)
        self._media_watch_async_db_patch.stop()
        self._async_db_patch.stop()
        await self._engine.dispose()
        if self._db_path.exists():
            self._db_path.unlink()

    async def _seed(
        self,
        *,
        inventory: list[int] | None = None,
        observable: bool = True,
    ) -> ServiceConfig:
        """One two-episode season on a Plex server, with no watches recorded."""
        async with self._sessionmaker() as db:
            plex = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex",
                api_key="key",
                enabled=True,
            )
            if observable:
                plex.extra_settings = {
                    "watch_snapshot_sync": {
                        "available": True,
                        "last_success_at": "2026-07-05T00:00:00Z",
                    }
                }
            series = Series(title="Show", tmdb_id=900, size=1024)
            db.add_all([plex, series])
            await db.flush()
            season = Season(series_id=series.id, season_number=1, size=1024)
            season.sonarr_episode_numbers = [1, 2] if inventory is None else inventory
            db.add(season)
            db.add(
                SeriesServiceRef(
                    series_id=series.id,
                    service=Service.PLEX,
                    service_id="plex-series",
                    library_id="tv",
                    library_name="TV",
                )
            )
            await db.flush()
            db.add_all(
                [
                    Episode(season_id=season.id, episode_number=1),
                    Episode(season_id=season.id, episode_number=2),
                ]
            )
            await db.commit()
            return plex

    async def _watched(
        self, plex: ServiceConfig, entries: list[tuple[str, int]]
    ) -> None:
        async with self._sessionmaker() as db:
            db.add_all(
                [
                    MediaWatchUserEpisode(
                        series_tmdb_id=900,
                        season_number=1,
                        episode_number=episode_number,
                        watch_user_key=username,
                        watch_user_key_normalized=username.casefold(),
                        source_service=Service.PLEX,
                        source_service_config_id=plex.id,
                        last_watched_at=NOW,
                    )
                    for username, episode_number in entries
                ]
            )
            await db.commit()

    async def _activate(
        self, rule: ReclaimRule
    ) -> tuple[WatchCompletionResolver | None, RulePreviewMatchMetadata]:
        metadata = RulePreviewMatchMetadata()
        with patch.object(
            type(cleanup_tasks.media_watch_snapshot_cache),
            "ensure_fresh_snapshot",
            new=AsyncMock(return_value=(True, None)),
        ):
            async with self._sessionmaker() as db:
                await cleanup_tasks._activate_watch_completion_for_rules(
                    db,
                    [rule],
                    require_fresh=False,
                    allow_stale_on_failure=True,
                    metadata=metadata,
                )
        return WatchCompletionResolver.current(), metadata

    @staticmethod
    def _completion_rule(scope: str = TARGET_SEASON) -> ReclaimRule:
        return _rule(
            scope,
            _condition(
                "playback.fully_watched_usernames", "contains_all", ["alice", "bob"]
            ),
        )

    async def test_only_whole_season_watchers_are_listed(self) -> None:
        plex = await self._seed()
        await self._watched(plex, [("alice", 1), ("alice", 2), ("bob", 1)])

        resolver, metadata = await self._activate(self._completion_rule())
        assert resolver is not None
        self.assertEqual(
            resolver.resolve(TARGET_SEASON, 900, season_number=1), ["alice"]
        )
        self.assertEqual(resolver.resolve(TARGET_SERIES, 900), ["alice"])
        # bob still finished the one episode he played
        self.assertEqual(
            resolver.resolve(TARGET_EPISODE, 900, season_number=1, episode_number=1),
            ["alice", "bob"],
        )
        self.assertEqual(
            resolver.resolve(TARGET_EPISODE, 900, season_number=1, episode_number=2),
            ["alice"],
        )
        self.assertEqual(metadata.watch_completion_unavailable_count, 0)

    async def test_durable_events_merge_with_native_watch_state(self) -> None:
        """Half the season from the snapshot, half from retained history."""
        plex = await self._seed()
        await self._watched(plex, [("alice", 1)])
        async with self._sessionmaker() as db:
            db.add(
                PlaybackHistoryEvent(
                    source_service=Service.TAUTULLI,
                    source_service_config_id=plex.id,
                    source_event_key="evt-1",
                    source_item_id="item-1",
                    provider_media_type="episode",
                    played_at=NOW,
                    duration_seconds=1200,
                    source_user_id="7",
                    source_username="Alice",
                    completed=True,
                    tmdb_id=900,
                    season_number=1,
                    episode_number=2,
                )
            )
            await db.commit()

        resolver, _metadata = await self._activate(self._completion_rule())
        assert resolver is not None
        self.assertEqual(
            resolver.resolve(TARGET_SEASON, 900, season_number=1), ["alice"]
        )

    async def test_an_unaired_episode_keeps_the_season_unfinished(self) -> None:
        """Sonarr's inventory is the denominator, not the episodes on disk."""
        plex = await self._seed(inventory=[1, 2, 3])
        await self._watched(plex, [("alice", 1), ("alice", 2)])

        resolver, _metadata = await self._activate(self._completion_rule())
        assert resolver is not None
        self.assertEqual(resolver.resolve(TARGET_SEASON, 900, season_number=1), [])

    async def test_a_season_without_inventory_is_unknown(self) -> None:
        plex = await self._seed(inventory=[])
        await self._watched(plex, [("alice", 1), ("alice", 2)])

        resolver, _metadata = await self._activate(self._completion_rule())
        assert resolver is not None
        self.assertIs(
            resolver.resolve(TARGET_SEASON, 900, season_number=1),
            RULE_VALUE_UNAVAILABLE,
        )
        self.assertIs(resolver.resolve(TARGET_SERIES, 900), RULE_VALUE_UNAVAILABLE)
        # the episodes themselves need no denominator, so they stay answerable
        self.assertEqual(
            resolver.resolve(TARGET_EPISODE, 900, season_number=1, episode_number=1),
            ["alice"],
        )

    async def test_an_unreadable_media_server_leaves_the_target_unknown(self) -> None:
        plex = await self._seed(observable=False)
        await self._watched(plex, [("alice", 1), ("alice", 2)])

        resolver, metadata = await self._activate(self._completion_rule())
        assert resolver is not None
        self.assertIs(
            resolver.resolve(TARGET_SEASON, 900, season_number=1),
            RULE_VALUE_UNAVAILABLE,
        )
        self.assertEqual(metadata.watch_completion_unavailable_count, 1)

    async def test_nothing_is_loaded_for_a_rule_that_does_not_ask(self) -> None:
        plex = await self._seed()
        await self._watched(plex, [("alice", 1), ("alice", 2)])

        resolver, _metadata = await self._activate(
            _rule(TARGET_SEASON, _condition("season.fully_watched", "is_true"))
        )
        assert resolver is not None
        self.assertIs(
            resolver.resolve(TARGET_SEASON, 900, season_number=1),
            RULE_VALUE_UNAVAILABLE,
        )


class WatchSensitivityTests(unittest.TestCase):
    """The new fields change answer when somebody watches something.

    Auto-delete re-checks watch-sensitive rules against fresh data immediately
    before deleting. A field left out of that set would authorise a deletion on
    the watch state the candidate was created with, days earlier.
    """

    def test_new_fields_are_rechecked_before_automatic_deletion(self) -> None:
        for scope, field in (
            (TARGET_SERIES, "series.fully_watched"),
            (TARGET_SERIES, "series.watched_percent"),
            (TARGET_SEASON, "playback.fully_watched_usernames"),
        ):
            with self.subTest(field=field):
                condition = (
                    _condition(field, "is_true")
                    if field == "series.fully_watched"
                    else _condition(
                        field,
                        "greater_than_or_equal"
                        if field.endswith("percent")
                        else "contains_all",
                        1 if field.endswith("percent") else ["alice"],
                    )
                )
                self.assertTrue(
                    cleanup_tasks._rule_uses_watch_sensitive_fields(
                        _rule(scope, condition)
                    )
                )

    def test_series_completion_loads_the_episodes_it_counts(self) -> None:
        """The seasons relationship is lazy="noload", so the gate has to fire."""
        self.assertTrue(
            cleanup_tasks._rules_use_season_episode_watch_fields(
                [_rule(TARGET_SERIES, _condition("series.fully_watched", "is_true"))]
            )
        )


if __name__ == "__main__":
    unittest.main()
