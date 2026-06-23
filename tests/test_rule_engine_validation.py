from __future__ import annotations

import unittest

from backend.core.rule_engine import (
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    derive_path_scope_library_ids,
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
    def test_accepts_extended_metadata_fields_for_supported_scopes(self) -> None:
        cases = [
            (TARGET_MOVIE_VERSION, "media.year", "equals", 2005),
            (TARGET_MOVIE_VERSION, "media.container", "contains_any", ["mkv"]),
            (
                TARGET_MOVIE_VERSION,
                "tmdb.original_language",
                "contains_any",
                ["eng"],
            ),
            (
                TARGET_MOVIE_VERSION,
                "tmdb.origin_country",
                "contains_any",
                ["US"],
            ),
            (
                TARGET_MOVIE_VERSION,
                "tmdb.runtime_minutes",
                "greater_than",
                90,
            ),
            (
                TARGET_MOVIE_VERSION,
                "video.bitrate_kbps",
                "greater_than",
                8000,
            ),
            (TARGET_MOVIE_VERSION, "video.bit_depth", "equals", 10),
            (
                TARGET_MOVIE_VERSION,
                "audio.bitrate_kbps",
                "greater_than",
                500,
            ),
            (TARGET_MOVIE_VERSION, "subtitle.track_count", "greater_than", 0),
            (TARGET_MOVIE_VERSION, "subtitle.has_forced", "is_true", None),
            (TARGET_MOVIE_VERSION, "movie.version_count", "greater_than", 1),
            (TARGET_SERIES, "series.tmdb_season_count", "greater_than", 2),
            (
                TARGET_SERIES,
                "sonarr.latest_season_has_unaired_episodes",
                "is_true",
                None,
            ),
            (
                TARGET_SERIES,
                "sonarr.latest_season_has_finale",
                "is_false",
                None,
            ),
            (TARGET_SEASON, "series.library_season_count", "greater_than", 2),
            (TARGET_EPISODE, "tmdb.original_language", "contains_any", ["jpn"]),
            (TARGET_MOVIE_VERSION, "playback.has_activity", "is_true", None),
            (TARGET_SERIES, "playback.play_count", "greater_than", 2),
            (
                TARGET_SEASON,
                "playback.total_duration_minutes",
                "greater_than",
                60,
            ),
            (
                TARGET_EPISODE,
                "playback.last_activity_at",
                "before",
                "2026-01-01",
            ),
        ]
        for scope, field, operator, value in cases:
            with self.subTest(scope=scope, field=field):
                validate_rule_definition(
                    _definition(field, operator, value),
                    target_scope=scope,
                )

    def test_rejects_version_only_metadata_for_series_scope(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Rule field\\(s\\) not available for target_scope 'series': "
            "'video.bitrate_kbps'",
        ):
            validate_rule_definition(
                _definition("video.bitrate_kbps", "greater_than", 8000),
                target_scope=TARGET_SERIES,
            )

    def test_rejects_sonarr_episode_state_for_non_series_scope(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Rule field\\(s\\) not available for target_scope 'season'",
        ):
            validate_rule_definition(
                _definition(
                    "sonarr.latest_season_has_unaired_episodes",
                    "is_true",
                ),
                target_scope=TARGET_SEASON,
            )

    def test_accepts_nested_and_or_groups(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "and",
                "children": [
                    {
                        "type": "condition",
                        "field": "media.size",
                        "operator": "greater_than",
                        "value": 1,
                    },
                    {
                        "type": "group",
                        "op": "or",
                        "children": [
                            {
                                "type": "condition",
                                "field": "video.hdr",
                                "operator": "is_true",
                            },
                            {
                                "type": "condition",
                                "field": "video.dolby_vision",
                                "operator": "is_true",
                            },
                        ],
                    },
                ],
            },
        }

        validate_rule_definition(definition, target_scope=TARGET_MOVIE_VERSION)

    def test_rejects_invalid_group_operator(self) -> None:
        definition = {
            "version": 1,
            "root": {
                "type": "group",
                "op": "not",
                "children": [
                    {
                        "type": "condition",
                        "field": "media.size",
                        "operator": "greater_than",
                        "value": 1,
                    }
                ],
            },
        }

        with self.assertRaisesRegex(
            ValueError, "Rule group operator must be AND or OR"
        ):
            validate_rule_definition(definition)

    def test_rejects_empty_group(self) -> None:
        definition = {
            "version": 1,
            "root": {"type": "group", "op": "and", "children": []},
        }

        with self.assertRaisesRegex(
            ValueError, "Rule group must include at least one condition"
        ):
            validate_rule_definition(definition)

    def test_rejects_non_object_group_child(self) -> None:
        definition = {
            "version": 1,
            "root": {"type": "group", "op": "and", "children": ["bad"]},
        }

        with self.assertRaisesRegex(ValueError, "Rule group child must be an object"):
            validate_rule_definition(definition)

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

    def test_accepts_tmdb_in_collection_boolean_operator(self) -> None:
        validate_rule_definition(_definition("tmdb.in_collection", "is_true"))

    def test_rejects_tmdb_in_collection_list_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'contains_any' for field 'tmdb.in_collection'",
        ):
            validate_rule_definition(
                _definition("tmdb.in_collection", "contains_any", ["true"]),
            )

    def test_accepts_tmdb_collection_name_text_operator(self) -> None:
        validate_rule_definition(
            _definition("tmdb.collection_name", "contains_any", ["Star Wars"]),
        )

    def test_accepts_tmdb_collection_name_contains_all_operator(self) -> None:
        validate_rule_definition(
            _definition(
                "tmdb.collection_name",
                "contains_all",
                ["Star Wars Collection", "Collection"],
            ),
        )

    def test_rejects_tmdb_collection_name_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'tmdb.collection_name'",
        ):
            validate_rule_definition(
                _definition("tmdb.collection_name", "greater_than", 1),
            )

    def test_accepts_tmdb_genres_multi_value_operator(self) -> None:
        validate_rule_definition(
            _definition("tmdb.genres", "contains_all", ["Action", "Comedy"]),
            target_scope=TARGET_MOVIE_VERSION,
        )

    def test_rejects_tmdb_genres_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'tmdb.genres'",
        ):
            validate_rule_definition(
                _definition("tmdb.genres", "greater_than", 1),
                target_scope=TARGET_MOVIE_VERSION,
            )

    def test_accepts_tmdb_genres_for_season_scope(self) -> None:
        validate_rule_definition(
            _definition("tmdb.genres", "contains_any", ["Drama"]),
            target_scope=TARGET_SEASON,
        )

    def test_accepts_media_server_collections_multi_value_operator(self) -> None:
        validate_rule_definition(
            _definition(
                "media_server.collections",
                "contains_all",
                ["Leaving Soon", "Holiday"],
            ),
            target_scope=TARGET_MOVIE_VERSION,
        )

    def test_rejects_media_server_collections_numeric_operator(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Unsupported rule operator 'greater_than' for field 'media_server.collections'",
        ):
            validate_rule_definition(
                _definition("media_server.collections", "greater_than", 1),
                target_scope=TARGET_MOVIE_VERSION,
            )

    def test_accepts_media_server_collections_for_season_scope(self) -> None:
        validate_rule_definition(
            _definition(
                "media_server.collections",
                "contains_any",
                ["Leaving Soon"],
            ),
            target_scope=TARGET_SEASON,
        )

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

    def test_accepts_library_contains_all_operator(self) -> None:
        validate_rule_definition(
            _definition("library.id", "contains_all", ["lib-1", "lib-2"]),
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

    def test_derive_path_scope_library_ids_accepts_contains_all_operator(self) -> None:
        self.assertEqual(
            derive_path_scope_library_ids(
                _definition("library.id", "contains_all", ["lib-1", "lib-2"])
            ),
            ["lib-1", "lib-2"],
        )

    def test_derive_path_scope_library_ids_rejects_not_contains_all_operator(
        self,
    ) -> None:
        self.assertIsNone(
            derive_path_scope_library_ids(
                _definition("library.id", "not_contains_all", ["lib-1", "lib-2"])
            )
        )

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
