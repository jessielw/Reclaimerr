from __future__ import annotations

import unittest

from backend.core.rule_engine import (
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    validate_rule_definition,
)


def _definition(field: str, operator: str, value: object = 1) -> dict[str, object]:
    condition: dict[str, object] = {
        "type": "condition",
        "field": field,
        "operator": operator,
    }
    if operator not in {"exists", "not_exists", "is_true", "is_false"}:
        condition["value"] = value
    return {
        "version": 1,
        "root": {"type": "group", "op": "and", "children": [condition]},
    }


class RuleDefinitionValidationTests(unittest.TestCase):
    def test_accepts_numeric_field_with_numeric_operator(self) -> None:
        validate_rule_definition(
            _definition("media.size", "greater_than_or_equal", 1024),
        )

    def test_rejects_numeric_field_with_list_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'contains_any' for field 'media.size'",
        ):
            validate_rule_definition(
                _definition("media.size", "contains_any", ["1024"]),
            )

    def test_accepts_path_regex_operator(self) -> None:
        validate_rule_definition(
            _definition("media.path", "matches_any_regex", [r"movies/.+\\.mkv"]),
        )

    def test_rejects_boolean_field_with_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'video.hdr'",
        ):
            validate_rule_definition(_definition("video.hdr", "greater_than", 1))

    def test_accepts_temporal_field_exists_operator(self) -> None:
        validate_rule_definition(_definition("watch.last_viewed_at", "exists"))

    def test_accepts_tmdb_release_temporal_exists_operator(self) -> None:
        validate_rule_definition(_definition("tmdb.release_date", "exists"))

    def test_accepts_temporal_field_before_operator(self) -> None:
        validate_rule_definition(
            _definition("tmdb.release_date", "before", "2026-01-01")
        )

    def test_rejects_temporal_field_equals_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'equals' for field 'watch.last_viewed_at'",
        ):
            validate_rule_definition(
                _definition("watch.last_viewed_at", "equals", "2026-01-01T00:00:00Z"),
            )

    def test_accepts_tmdb_days_since_release_numeric_operator(self) -> None:
        validate_rule_definition(
            _definition("tmdb.days_since_release", "greater_than_or_equal", 30),
        )

    def test_rejects_tmdb_days_since_release_with_list_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'contains_any' for field 'tmdb.days_since_release'",
        ):
            validate_rule_definition(
                _definition("tmdb.days_since_release", "contains_any", ["30"]),
            )

    def test_accepts_imdb_rating_numeric_operator(self) -> None:
        validate_rule_definition(
            _definition("imdb.rating", "greater_than_or_equal", 7.5),
        )

    def test_rejects_imdb_rating_temporal_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'before' for field 'imdb.rating'",
        ):
            validate_rule_definition(
                _definition("imdb.rating", "before", "2026-01-01"),
            )

    def test_accepts_anilist_score_numeric_operator(self) -> None:
        validate_rule_definition(
            _definition("anilist.score", "greater_than_or_equal", 80),
        )

    def test_rejects_anilist_score_temporal_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'before' for field 'anilist.score'",
        ):
            validate_rule_definition(
                _definition("anilist.score", "before", "2026-01-01"),
            )

    def test_rejects_numeric_field_with_temporal_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'before' for field 'tmdb.days_since_release'",
        ):
            validate_rule_definition(
                _definition("tmdb.days_since_release", "before", "2026-01-01"),
            )

    def test_accepts_library_contains_any_operator(self) -> None:
        validate_rule_definition(
            _definition("library.id", "contains_any", ["lib-1", "lib-2"]),
        )

    def test_rejects_library_equals_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'equals' for field 'library.id'",
        ):
            validate_rule_definition(_definition("library.id", "equals", "lib-1"))

    def test_accepts_season_fully_watched_boolean_operator(self) -> None:
        validate_rule_definition(_definition("season.fully_watched", "is_true"))

    def test_rejects_season_fully_watched_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'season.fully_watched'",
        ):
            validate_rule_definition(
                _definition("season.fully_watched", "greater_than", 1)
            )

    def test_accepts_season_watched_percent_numeric_operator(self) -> None:
        validate_rule_definition(
            _definition("season.watched_percent", "greater_than_or_equal", 100),
        )

    def test_rejects_season_watched_percent_list_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'contains_any' for field 'season.watched_percent'",
        ):
            validate_rule_definition(
                _definition("season.watched_percent", "contains_any", ["100"]),
            )

    def test_accepts_seerr_requested_boolean_operator(self) -> None:
        validate_rule_definition(_definition("seerr.requested", "is_true"))

    def test_rejects_seerr_requested_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'seerr.requested'",
        ):
            validate_rule_definition(_definition("seerr.requested", "greater_than", 1))

    def test_accepts_seerr_requester_ids_list_operator(self) -> None:
        validate_rule_definition(
            _definition("seerr.requested_by_user_ids", "contains_any", ["10", "22"])
        )

    def test_rejects_seerr_requester_ids_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'seerr.requested_by_user_ids'",
        ):
            validate_rule_definition(
                _definition("seerr.requested_by_user_ids", "greater_than", 1)
            )

    def test_rejects_empty_library_list_condition(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Library conditions require at least one library id",
        ):
            validate_rule_definition(_definition("library.id", "contains_any", []))

    def test_accepts_scope_compatible_field_for_target(self) -> None:
        validate_rule_definition(
            _definition("season.fully_watched", "is_true"),
            target_scope=TARGET_SEASON,
        )

    def test_rejects_scope_incompatible_field_for_target(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Rule field\\(s\\) not available for target_scope 'movie_version'",
        ):
            validate_rule_definition(
                _definition("season.fully_watched", "is_true"),
                target_scope=TARGET_MOVIE_VERSION,
            )

    def test_rejects_all_incompatible_fields_for_target(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    {
                        "type": "condition",
                        "field": "episode.number",
                        "operator": "equals",
                        "value": 1,
                    },
                    {
                        "type": "condition",
                        "field": "season.air_date",
                        "operator": "exists",
                    },
                ],
            },
        }
        with self.assertRaises(ValueError) as exc:
            validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

        message = str(exc.exception)
        self.assertIn("episode.number", message)
        self.assertIn("season.air_date", message)

    def test_rejects_invalid_target_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported target_scope"):
            validate_rule_definition(
                _definition("media.size", "greater_than", 1),
                target_scope="movies",
            )


if __name__ == "__main__":
    unittest.main()
