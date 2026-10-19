from datetime import UTC, datetime, timedelta

import pytest

from backend.core.rule_engine import (
    _build_context,
    _evaluate_condition,
    validate_rule_definition,
)
from backend.database.models import Episode, Movie, MovieVersion, Season, Series
from backend.enums import Service


def test_first_import_rule_uses_movie_history_not_current_version():
    movie = Movie(title="Example", tmdb_id=1)
    movie.arr_first_import_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=280
    )
    version = MovieVersion(
        movie_id=1,
        service=Service.PLEX,
        service_item_id="item",
        service_media_id="media",
        library_id="library",
        library_name="Movies",
        added_at=datetime.now(UTC).replace(tzinfo=None),
        arr_added_at=datetime.now(UTC).replace(tzinfo=None),
    )
    context = _build_context(
        "movie_version", movie, version, None, None, compute_disk=False
    )
    assert context["arr.days_since_first_import"] == 280
    movie.arr_first_import_at = None
    context = _build_context(
        "movie_version", movie, version, None, None, compute_disk=False
    )
    assert context["arr.days_since_first_import"] is None


def test_first_import_field_is_movie_and_series_only():
    definition = {
        "root": {
            "type": "condition",
            "field": "arr.days_since_first_import",
            "operator": "greater_than",
            "value": 182,
        }
    }
    validate_rule_definition(definition, target_scope="movie_version")
    validate_rule_definition(definition, target_scope="series")
    for scope in ("season", "episode"):
        with pytest.raises(ValueError):
            validate_rule_definition(definition, target_scope=scope)


def test_first_import_rule_uses_series_history_not_new_season_or_episode():
    series = Series(title="Example", tmdb_id=1)
    series.arr_first_import_at = datetime.now(UTC) - timedelta(days=280)
    series.arr_added_at = datetime.now(UTC)
    season = Season(series_id=1, season_number=2)
    episode = Episode(season_id=1, episode_number=1)
    context = _build_context("series", None, None, series, None, compute_disk=False)
    condition = {
        "field": "arr.days_since_first_import",
        "operator": "greater_than",
        "value": 182,
    }
    assert context["arr.days_since_first_import"] == 280
    assert _evaluate_condition(condition, context, {}, [])
    assert context["arr.days_since_file_added"] == 0
    for scope in ("season", "episode"):
        context = _build_context(
            scope, None, None, series, season, episode, compute_disk=False
        )
        assert "arr.days_since_first_import" not in context
    series.arr_first_import_at = None
    context = _build_context("series", None, None, series, None, compute_disk=False)
    assert context["arr.days_since_first_import"] is None
    assert not _evaluate_condition(condition, context, {}, [])
