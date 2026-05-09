from __future__ import annotations

import unittest

from backend.core.rule_engine import validate_rule_definition


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
        validate_rule_definition(_definition("tmdb.release_date", "before", "2026-01-01"))

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

    def test_rejects_empty_library_list_condition(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Library conditions require at least one library id",
        ):
            validate_rule_definition(_definition("library.id", "contains_any", []))


if __name__ == "__main__":
    unittest.main()
