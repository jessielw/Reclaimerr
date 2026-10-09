from __future__ import annotations

import unittest

from backend.core.rule_engine import (
    WatchUserAliasResolver,
    _evaluate_user_scoped_condition,
    _matches_operator,
)
from backend.enums import Service
from backend.services.watch_identity import aliases_by_name

BOB = frozenset({"176142613", "bobby", "bob smith"})
ALICE = frozenset({"alice"})


class PlaybackUsernameAliasMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        index = {
            Service.PLEX: {alias: person for person in (BOB,) for alias in person},
            Service.JELLYFIN: {"alice": ALICE},
        }
        WatchUserAliasResolver(aliases_by_name(index)).activate()
        self.addCleanup(WatchUserAliasResolver._ctx.set, None)

    def test_name_matches_plays_stored_under_account_number(self) -> None:
        self.assertTrue(
            _matches_operator(
                ["176142613"], "contains_any", ["Bob Smith"], field="playback.usernames"
            )
        )

    def test_existing_account_number_value_still_matches(self) -> None:
        for actual in (["176142613"], ["Bob Smith"]):
            self.assertTrue(
                _matches_operator(
                    actual,
                    "contains_any",
                    ["176142613"],
                    field="playback.fully_watched_usernames",
                )
            )

    def test_matches_all_needs_every_person_not_every_alias(self) -> None:
        field = "playback.usernames"
        self.assertTrue(
            _matches_operator(
                ["176142613", "Alice"], "contains_all", ["bobby", "alice"], field=field
            )
        )
        self.assertFalse(
            _matches_operator(
                ["176142613"], "contains_all", ["bobby", "alice"], field=field
            )
        )
        self.assertTrue(
            _matches_operator(
                ["176142613"], "not_contains_all", ["bobby", "alice"], field=field
            )
        )

    def test_matches_none_sees_through_aliases(self) -> None:
        self.assertFalse(
            _matches_operator(
                ["176142613"],
                "not_contains_any",
                ["Bob Smith"],
                field="playback.usernames",
            )
        )

    def test_unknown_names_compare_literally(self) -> None:
        field = "playback.usernames"
        self.assertTrue(
            _matches_operator(["999"], "contains_any", ["999"], field=field)
        )
        self.assertFalse(
            _matches_operator(["999"], "contains_any", ["Bob Smith"], field=field)
        )

    def test_user_scoped_totals_read_through_aliases(self) -> None:
        matched, value = _evaluate_user_scoped_condition(
            {"176142613": 90.0},
            {"usernames": ["Bob Smith"], "amount": 60},
            "greater_than",
            field="playback.user_watched_duration_minutes",
        )
        self.assertTrue(matched)
        self.assertEqual(value, 90.0)


class PlaybackUsernameWithoutResolverTests(unittest.TestCase):
    def test_without_resolver_names_compare_literally(self) -> None:
        self.assertFalse(
            _matches_operator(
                ["176142613"], "contains_any", ["Bob Smith"], field="playback.usernames"
            )
        )


if __name__ == "__main__":
    unittest.main()
