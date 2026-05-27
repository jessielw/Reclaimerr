from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.candidate_views import build_rule_preview_items
from backend.core.rule_engine import SeerrRequestResolver
from backend.database import Base
from backend.database.models import (
    Episode,
    GeneralSettings,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SeriesServiceRef,
    User,
)
from backend.enums import MediaType, Service
from backend.services.seerr_cache import SeerrRequestSnapshot
from backend.tasks import cleanup as cleanup_tasks
from backend.tasks.cleanup import (
    _compute_requester_has_watched_for_key,
    _evaluate_movie_rule,
    _evaluate_rule_for_season,
    _process_series_episodes,
    _process_series_seasons,
    _scan_with_db,
    collect_rule_preview_matches,
)
from backend.utils.helpers import normalize_leaving_soon_collection_title


def _make_rule(media_type: MediaType, **overrides: object) -> ReclaimRule:
    target_scope = str(
        overrides.pop(
            "target_scope",
            "movie_version" if media_type == MediaType.MOVIE else "series",
        )
    )
    rule = ReclaimRule(
        name="rule",
        media_type=media_type,
        enabled=True,
        target_scope=target_scope,
        definition=_definition_from_overrides(overrides),
        action={"candidate": True, "media_server_action": "delete"},
    )
    return rule


def _definition_from_overrides(overrides: dict[str, object]) -> dict[str, object]:
    children: list[dict[str, object]] = []

    def add(field: str, operator: str, value: object | None = None) -> None:
        node: dict[str, object] = {
            "type": "condition",
            "field": field,
            "operator": operator,
        }
        if operator not in {"exists", "not_exists", "is_true", "is_false"}:
            node["value"] = value
        children.append(node)

    mapping = {
        "library_ids": ("library.id", "contains_any"),
        "paths": ("media.path", "matches_any_regex"),
        "min_popularity": ("tmdb.popularity", "greater_than_or_equal"),
        "max_popularity": ("tmdb.popularity", "less_than_or_equal"),
        "min_vote_average": ("tmdb.vote_average", "greater_than_or_equal"),
        "max_vote_average": ("tmdb.vote_average", "less_than_or_equal"),
        "min_vote_count": ("tmdb.vote_count", "greater_than_or_equal"),
        "max_vote_count": ("tmdb.vote_count", "less_than_or_equal"),
        "min_view_count": ("watch.view_count", "greater_than_or_equal"),
        "max_view_count": ("watch.view_count", "less_than_or_equal"),
        "min_days_since_added": ("media.days_since_added", "greater_than_or_equal"),
        "max_days_since_added": ("media.days_since_added", "less_than_or_equal"),
        "min_days_since_last_watched": (
            "watch.days_since_last_watched",
            "greater_than_or_equal",
        ),
        "max_days_since_last_watched": (
            "watch.days_since_last_watched",
            "less_than_or_equal",
        ),
        "min_days_since_release": ("tmdb.days_since_release", "greater_than_or_equal"),
        "max_days_since_release": ("tmdb.days_since_release", "less_than_or_equal"),
        "min_days_since_first_air_date": (
            "tmdb.days_since_first_air_date",
            "greater_than_or_equal",
        ),
        "max_days_since_first_air_date": (
            "tmdb.days_since_first_air_date",
            "less_than_or_equal",
        ),
        "min_days_since_last_air_date": (
            "tmdb.days_since_last_air_date",
            "greater_than_or_equal",
        ),
        "max_days_since_last_air_date": (
            "tmdb.days_since_last_air_date",
            "less_than_or_equal",
        ),
        "min_days_since_season_air_date": (
            "season.days_since_air_date",
            "greater_than_or_equal",
        ),
        "max_days_since_season_air_date": (
            "season.days_since_air_date",
            "less_than_or_equal",
        ),
        "min_size": ("media.size", "greater_than_or_equal"),
        "max_size": ("media.size", "less_than_or_equal"),
        "series_status": ("series.status", "contains_any"),
        "video_codec_families_in": ("video.codec_family", "contains_any"),
        "video_codec_families_not_in": ("video.codec_family", "not_contains_any"),
        "audio_codec_families_in": ("audio.codec_family", "contains_any"),
        "audio_codec_families_not_in": ("audio.codec_family", "not_contains_any"),
        "min_video_width": ("video.width", "greater_than_or_equal"),
        "max_video_width": ("video.width", "less_than_or_equal"),
        "min_video_height": ("video.height", "greater_than_or_equal"),
        "max_video_height": ("video.height", "less_than_or_equal"),
        "min_audio_channels": ("audio.channels", "greater_than_or_equal"),
        "max_audio_channels": ("audio.channels", "less_than_or_equal"),
        "min_duration": ("media.duration", "greater_than_or_equal"),
        "max_duration": ("media.duration", "less_than_or_equal"),
        "min_audio_track_count": ("audio.track_count", "greater_than_or_equal"),
        "max_audio_track_count": ("audio.track_count", "less_than_or_equal"),
        "audio_languages_in": ("audio.languages", "contains_any"),
        "audio_languages_not_in": ("audio.languages", "not_contains_any"),
        "subtitle_languages_in": ("subtitle.languages", "contains_any"),
        "subtitle_languages_not_in": ("subtitle.languages", "not_contains_any"),
        "video_color_spaces_in": ("video.color_space", "contains_any"),
        "video_color_spaces_not_in": ("video.color_space", "not_contains_any"),
        "video_color_transfers_in": ("video.color_transfer", "contains_any"),
        "video_color_transfers_not_in": ("video.color_transfer", "not_contains_any"),
        "video_color_primaries_in": ("video.color_primaries", "contains_any"),
        "video_color_primaries_not_in": (
            "video.color_primaries",
            "not_contains_any",
        ),
    }
    for key, (field, operator) in mapping.items():
        value = overrides.get(key)
        if value is not None and value != []:
            add(field, operator, value)

    if overrides.get("include_never_watched") is True:
        add("watch.last_viewed_at", "not_exists")
    elif "include_never_watched" in overrides:
        add("watch.last_viewed_at", "exists")
    if overrides.get("has_hdr") is not None:
        add("video.hdr", "is_true" if overrides["has_hdr"] else "is_false")
    if overrides.get("has_dolby_vision") is not None:
        add(
            "video.dolby_vision",
            "is_true" if overrides["has_dolby_vision"] else "is_false",
        )

    return {"version": 1, "root": {"type": "group", "op": "and", "children": children}}


def _make_movie_version(
    *,
    service_media_id: str,
    service_item_id: str,
    library_id: str = "lib-1",
    library_name: str = "Library 1",
    **overrides: object,
) -> MovieVersion:
    version = MovieVersion(
        movie_id=1,
        service=Service.PLEX,
        service_item_id=service_item_id,
        service_media_id=service_media_id,
        library_id=library_id,
        library_name=library_name,
    )
    for key, value in overrides.items():
        setattr(version, key, value)
    return version


def _make_series_ref(
    *,
    service_id: str,
    library_id: str = "lib-1",
    library_name: str = "Library 1",
    path: str | None = None,
) -> SeriesServiceRef:
    ref = SeriesServiceRef(
        series_id=1,
        service=Service.PLEX,
        service_id=service_id,
        library_id=library_id,
        library_name=library_name,
    )
    ref.path = path
    return ref


class _LeavingSoonSyncServiceFake:
    def __init__(
        self,
        *,
        fail_sync: bool = False,
        fail_delete_titles: set[str] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.delete_calls: list[str] = []
        self._fail_sync = fail_sync
        self._fail_delete_titles = set(fail_delete_titles or set())

    async def sync_leaving_soon_collections(
        self,
        *,
        base_title: str,
        movie_item_ids: set[str],
        series_item_ids: set[str],
    ) -> None:
        if self._fail_sync:
            raise RuntimeError("sync failure")
        self.calls.append(
            {
                "base_title": base_title,
                "movie_item_ids": set(movie_item_ids),
                "series_item_ids": set(series_item_ids),
            }
        )

    async def delete_leaving_soon_collections(self, *, base_title: str) -> None:
        normalized = normalize_leaving_soon_collection_title(base_title)
        self.delete_calls.append(normalized)
        if normalized in self._fail_delete_titles:
            raise RuntimeError(f"delete failure for {normalized}")


class CleanupMediaRuleTests(unittest.TestCase):
    def test_evaluate_movie_rule_library_filter_passes_and_fails(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                library_id="lib-1",
                path="/media/movies/test.mkv",
            )
        ]
        pass_rule = _make_rule(MediaType.MOVIE, library_ids=["lib-1"])
        fail_rule = _make_rule(MediaType.MOVIE, library_ids=["lib-2"])

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_library_filter_matches_any_selected_library(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                library_id="lib-2",
                path="/media/movies/test.mkv",
            )
        ]
        pass_rule = _make_rule(MediaType.MOVIE, library_ids=["lib-1", "lib-2"])
        fail_rule = _make_rule(MediaType.MOVIE, library_ids=["lib-1", "lib-3"])

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_path_regex_passes_and_fails(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                path="/media/movies/Example.Movie.2024.mkv",
            )
        ]
        pass_rule = _make_rule(MediaType.MOVIE, paths=[r"example\.movie\.2024"])
        fail_rule = _make_rule(MediaType.MOVIE, paths=[r"nomatch"])

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_tmdb_ranges_passes_and_fails(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.popularity = 50.0
        movie.vote_average = 7.5
        movie.vote_count = 1000
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        pass_rule = _make_rule(
            MediaType.MOVIE,
            min_popularity=10.0,
            max_popularity=100.0,
            min_vote_average=7.0,
            min_vote_count=100,
        )
        fail_rule = _make_rule(MediaType.MOVIE, max_vote_average=6.0)

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_watch_behavior_default_vs_include_never_watched(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        default_rule = _make_rule(MediaType.MOVIE, min_size=1)
        include_never_rule = _make_rule(
            MediaType.MOVIE,
            min_size=1,
            include_never_watched=True,
        )

        self.assertTrue(_evaluate_movie_rule(movie, default_rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, include_never_rule, {}, []))

    def test_evaluate_movie_rule_temporal_criteria_passes_and_fails(self) -> None:
        now = datetime.now(UTC)
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.added_at = now - timedelta(days=40)
        movie.last_viewed_at = now - timedelta(days=10)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        pass_rule = _make_rule(
            MediaType.MOVIE,
            min_days_since_added=30,
            max_days_since_added=60,
            min_days_since_last_watched=5,
            max_days_since_last_watched=20,
        )
        fail_rule = _make_rule(MediaType.MOVIE, min_days_since_last_watched=20)

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_days_since_release_passes_and_fails(self) -> None:
        now = datetime.now(UTC)
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.tmdb_release_date = now - timedelta(days=120)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        pass_rule = _make_rule(
            MediaType.MOVIE,
            min_days_since_release=90,
            max_days_since_release=150,
        )
        fail_rule = _make_rule(MediaType.MOVIE, min_days_since_release=200)

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_release_date_operator_passes_and_fails(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.tmdb_release_date = datetime(2024, 1, 15)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        pass_rule = ReclaimRule(
            name="rule",
            media_type=MediaType.MOVIE,
            enabled=True,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "tmdb.release_date",
                            "operator": "before",
                            "value": "2024-02-01",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )
        fail_rule = ReclaimRule(
            name="rule",
            media_type=MediaType.MOVIE,
            enabled=True,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "tmdb.release_date",
                            "operator": "after",
                            "value": "2024-02-01",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_size_criteria_passes_and_fails(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        pass_rule = _make_rule(
            MediaType.MOVIE,
            min_size=5 * 1024**3,
            max_size=20 * 1024**3,
        )
        fail_rule = _make_rule(MediaType.MOVIE, max_size=1 * 1024**3)

        self.assertTrue(_evaluate_movie_rule(movie, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, fail_rule, {}, []))

    def test_evaluate_movie_rule_series_status_passes_and_fails(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.status = "Returning Series"
        series.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        pass_rule = _make_rule(
            MediaType.SERIES,
            series_status=["Returning Series"],
        )
        fail_rule = _make_rule(MediaType.SERIES, series_status=["Ended"])

        self.assertTrue(_evaluate_movie_rule(series, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(series, fail_rule, {}, []))

    def test_evaluate_series_rule_days_since_air_dates_passes_and_fails(self) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.status = "Returning Series"
        series.tmdb_first_air_date = now - timedelta(days=365)
        series.tmdb_last_air_date = now - timedelta(days=30)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        pass_rule = _make_rule(
            MediaType.SERIES,
            min_days_since_first_air_date=300,
            max_days_since_last_air_date=60,
        )
        fail_rule = _make_rule(MediaType.SERIES, min_days_since_last_air_date=90)

        self.assertTrue(_evaluate_movie_rule(series, pass_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(series, fail_rule, {}, []))

    def test_evaluate_rule_for_season_applies_season_specific_fields(self) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.status = "Returning Series"
        series.service_refs = [
            _make_series_ref(
                service_id="sr-1", library_id="lib-1", path="/media/series"
            )
        ]
        series.popularity = 20.0
        series.vote_average = 8.0
        series.vote_count = 200

        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.view_count = 3
        season.added_at = now - timedelta(days=25)
        season.last_viewed_at = now - timedelta(days=7)

        pass_rule = _make_rule(
            MediaType.SERIES,
            target_scope="season",
            library_ids=["lib-1"],
            paths=[r"/media/series"],
            series_status=["Returning Series"],
            min_popularity=10.0,
            min_vote_average=7.0,
            min_vote_count=100,
            min_view_count=1,
            min_days_since_added=10,
            min_days_since_last_watched=5,
            min_size=1 * 1024**3,
        )
        fail_rule = _make_rule(
            MediaType.SERIES,
            target_scope="season",
            min_days_since_last_watched=20,
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, pass_rule, {}, []))
        self.assertFalse(_evaluate_rule_for_season(series, season, fail_rule, {}, []))

    def test_evaluate_rule_for_season_days_since_air_date_passes_and_fails(
        self,
    ) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.air_date = now - timedelta(days=45)

        pass_rule = _make_rule(
            MediaType.SERIES,
            target_scope="season",
            min_days_since_season_air_date=30,
            max_days_since_season_air_date=60,
        )
        fail_rule = _make_rule(
            MediaType.SERIES,
            target_scope="season",
            min_days_since_season_air_date=90,
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, pass_rule, {}, []))
        self.assertFalse(_evaluate_rule_for_season(series, season, fail_rule, {}, []))

    def test_evaluate_rule_for_season_air_date_operator_passes_and_fails(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.air_date = datetime(2024, 3, 1)

        pass_rule = ReclaimRule(
            name="rule",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.air_date",
                            "operator": "on_or_before",
                            "value": "2024-03-01",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )
        fail_rule = ReclaimRule(
            name="rule",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.air_date",
                            "operator": "after",
                            "value": "2024-04-01",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, pass_rule, {}, []))
        self.assertFalse(_evaluate_rule_for_season(series, season, fail_rule, {}, []))

    def test_evaluate_rule_for_season_fully_watched_matches_all_watched(self) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.added_at = now - timedelta(days=10)
        season.episodes = [
            Episode(season_id=1, episode_number=1, view_count=1),
            Episode(
                season_id=1,
                episode_number=2,
                view_count=0,
                last_viewed_at=now - timedelta(days=1),
            ),
        ]
        rule = ReclaimRule(
            name="season-fully-watched",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.fully_watched",
                            "operator": "is_true",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, rule, {}, []))

    def test_evaluate_rule_for_season_watched_percent_matches_partial(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.episodes = [
            Episode(season_id=1, episode_number=1, view_count=1),
            Episode(season_id=1, episode_number=2, view_count=0),
        ]
        pass_rule = ReclaimRule(
            name="season-watched-pct-pass",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.watched_percent",
                            "operator": "greater_than_or_equal",
                            "value": 50,
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )
        fail_rule = ReclaimRule(
            name="season-watched-pct-fail",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.watched_percent",
                            "operator": "equals",
                            "value": 100,
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, pass_rule, {}, []))
        self.assertFalse(_evaluate_rule_for_season(series, season, fail_rule, {}, []))

    def test_evaluate_rule_for_season_watched_progress_ignores_stale_watch_data(
        self,
    ) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.added_at = now
        season.episodes = [
            Episode(
                season_id=1,
                episode_number=1,
                view_count=1,
                last_viewed_at=now - timedelta(days=1),
            )
        ]
        fully_watched_rule = ReclaimRule(
            name="season-fully-watched-stale",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.fully_watched",
                            "operator": "is_true",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )
        watched_percent_zero_rule = ReclaimRule(
            name="season-watched-zero",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.watched_percent",
                            "operator": "equals",
                            "value": 0,
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        self.assertFalse(
            _evaluate_rule_for_season(series, season, fully_watched_rule, {}, [])
        )
        self.assertTrue(
            _evaluate_rule_for_season(series, season, watched_percent_zero_rule, {}, [])
        )

    def test_evaluate_rule_for_season_watched_progress_unknown_without_episodes(
        self,
    ) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)

        is_true_rule = ReclaimRule(
            name="season-fully-watched-unknown-true",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.fully_watched",
                            "operator": "is_true",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )
        not_exists_rule = ReclaimRule(
            name="season-fully-watched-unknown-missing",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="season",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "season.fully_watched",
                            "operator": "not_exists",
                        }
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        self.assertFalse(
            _evaluate_rule_for_season(series, season, is_true_rule, {}, [])
        )
        self.assertTrue(
            _evaluate_rule_for_season(series, season, not_exists_rule, {}, [])
        )

    def test_evaluate_movie_rule_combined_criteria_populates_reasons(self) -> None:
        now = datetime.now(UTC)
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.popularity = 30.0
        movie.vote_average = 7.8
        movie.vote_count = 500
        movie.view_count = 0
        movie.added_at = now - timedelta(days=120)
        movie.last_viewed_at = now - timedelta(days=45)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                path="/media/movies/title.mkv",
            )
        ]
        rule = _make_rule(
            MediaType.MOVIE,
            library_ids=["lib-1"],
            paths=[r"title\.mkv$"],
            min_popularity=10.0,
            min_vote_average=7.0,
            min_vote_count=100,
            min_days_since_added=30,
            min_days_since_last_watched=10,
            min_size=1 * 1024**3,
        )
        matched_criteria: dict[str, object] = {}
        reasons: list[dict[str, object]] = []

        matched = _evaluate_movie_rule(movie, rule, matched_criteria, reasons)

        self.assertTrue(matched)
        self.assertIn("tmdb.popularity", matched_criteria)
        self.assertIn("media.size", matched_criteria)
        self.assertTrue(reasons)

    def test_evaluate_movie_rule_dispatches_to_movie_media_path(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        v = _make_movie_version(
            service_media_id="m1",
            service_item_id="i1",
            video_codec_family="h265",
            audio_codec_family="eac3",
            video_hdr=True,
            video_dolby_vision=False,
            video_width=3840,
            video_height=2160,
            audio_channels=6,
            duration=7_200_000,
            audio_count=2,
            audio_languages=["eng"],
            subtitle_languages=["eng"],
            video_color_space="bt2020nc",
            video_color_transfer="smpte2084",
            video_color_primaries="bt2020",
        )
        v.id = 33
        movie.versions = [v]
        rule = _make_rule(
            MediaType.MOVIE,
            video_codec_families_in=["h265"],
            has_hdr=True,
            min_video_width=3000,
            include_never_watched=True,
        )
        matched_criteria: dict[str, object] = {}
        reasons: list[dict] = []

        matched = _evaluate_movie_rule(movie, rule, matched_criteria, reasons)

        self.assertTrue(matched)
        self.assertEqual(matched_criteria["media_version_id"], 33)
        self.assertTrue(any("Video codec" in str(r.get("text", "")) for r in reasons))

    def test_evaluate_movie_rule_dispatches_to_series_aggregate_path(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [
            _make_series_ref(service_id="sr-1", path="/media/series")
        ]
        series.video_codec_families = ["h265"]
        series.audio_codec_families = ["eac3"]
        series.has_hdr = True
        series.has_dolby_vision = False
        series.max_video_width = 3840
        series.max_video_height = 2160
        series.max_audio_channels = 6
        series.subtitle_languages = ["eng"]
        rule = _make_rule(
            MediaType.SERIES,
            video_codec_families_in=["h265"],
            has_hdr=True,
            min_video_width=3000,
            include_never_watched=True,
        )
        matched_criteria: dict[str, object] = {}
        reasons: list[dict[str, object]] = []

        matched = _evaluate_movie_rule(series, rule, matched_criteria, reasons)

        self.assertTrue(matched)
        self.assertEqual(matched_criteria["video.width"], 3840)
        self.assertTrue(any("Video codec" in str(r.get("text", "")) for r in reasons))

    def test_evaluate_movie_rule_fails_series_with_unsupported_aggregate_fields(
        self,
    ) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        series.video_codec_families = ["h265"]
        series.audio_codec_families = ["eac3"]
        series.has_hdr = True
        series.has_dolby_vision = False
        series.max_video_width = 3840
        series.max_video_height = 2160
        series.max_audio_channels = 6
        series.subtitle_languages = ["eng"]
        rule = _make_rule(
            MediaType.SERIES,
            video_codec_families_in=["h265"],
            min_duration=1_000,
            include_never_watched=True,
        )

        matched = _evaluate_movie_rule(series, rule, {}, [])

        self.assertFalse(matched)

    def test_evaluate_movie_rule_seerr_requested_matches(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        rule = ReclaimRule(
            name="seerr-rule",
            media_type=MediaType.MOVIE,
            enabled=True,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "seerr.requested",
                            "operator": "is_true",
                        },
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        SeerrRequestResolver({(MediaType.MOVIE, 1): {11}}).activate()
        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

        SeerrRequestResolver({(MediaType.MOVIE, 1): set()}).activate()
        self.assertFalse(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_movie_rule_seerr_requester_ids_matches(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        rule = ReclaimRule(
            name="seerr-requester-rule",
            media_type=MediaType.MOVIE,
            enabled=True,
            target_scope="movie_version",
            definition={
                "version": 1,
                "root": {
                    "type": "group",
                    "op": "and",
                    "children": [
                        {
                            "type": "condition",
                            "field": "seerr.requested_by_user_ids",
                            "operator": "contains_any",
                            "value": ["101", "303"],
                        },
                    ],
                },
            },
            action={"candidate": True, "media_server_action": "delete"},
        )

        SeerrRequestResolver({(MediaType.MOVIE, 1): {101, 202}}).activate()
        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

        SeerrRequestResolver({(MediaType.MOVIE, 1): {404}}).activate()
        self.assertFalse(_evaluate_movie_rule(movie, rule, {}, []))

    def test_compute_requester_has_watched_handles_mixed_timezone_datetimes(
        self,
    ) -> None:
        media_key: tuple[MediaType, int] = (MediaType.MOVIE, 1)
        snapshot = SeerrRequestSnapshot(
            requester_ids_by_key={media_key: {101}},
            latest_request_at_by_key_user={
                media_key: {101: datetime(2026, 1, 1, 12, 0, tzinfo=UTC)}
            },
            requester_identity_keys_by_user_id={101: {"alice"}},
        )
        watch_by_service_and_user: dict[
            tuple[MediaType, int], dict[Service, dict[str, datetime]]
        ] = {media_key: {Service.PLEX: {"alice": datetime(2026, 1, 1, 13, 0)}}}

        self.assertTrue(
            _compute_requester_has_watched_for_key(
                media_key=media_key,
                snapshot=snapshot,
                watch_by_service_and_user=watch_by_service_and_user,
                mappings=[],
            )
        )

        watch_by_service_and_user[media_key][Service.PLEX]["alice"] = datetime(
            2026, 1, 1, 11, 0
        )
        self.assertFalse(
            _compute_requester_has_watched_for_key(
                media_key=media_key,
                snapshot=snapshot,
                watch_by_service_and_user=watch_by_service_and_user,
                mappings=[],
            )
        )


if __name__ == "__main__":
    unittest.main()


class CleanupScanIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp_root = Path("tests/.tmp")
        tmp_root.mkdir(parents=True, exist_ok=True)
        self._db_path = tmp_root / f"test_cleanup_{uuid4().hex}.db"
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        if self._db_path.exists():
            self._db_path.unlink()

    async def test_scan_movie_candidate_create_update_remove(self) -> None:
        async with self._sessionmaker() as db:
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=1001, size=5 * 1024**3)
            db.add_all([rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-1",
                service_item_id="item-1",
            )
            version.movie_id = movie.id
            db.add(version)
            await db.flush()
            version_id = version.id
            await db.commit()
            movie_id = movie.id

        async with self._sessionmaker() as db:
            first = await _scan_with_db(db)
            self.assertIsNotNone(first)
            self.assertEqual(first, (1, 0, 0))

            candidates = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.MOVIE
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].movie_id, movie_id)

        async with self._sessionmaker() as db:
            second = await _scan_with_db(db)
            self.assertIsNotNone(second)
            self.assertEqual(second, (0, 1, 0))

        async with self._sessionmaker() as db:
            movie = (
                await db.execute(select(Movie).where(Movie.id == movie_id))
            ).scalar_one()
            movie.size = 0
            await db.commit()

        async with self._sessionmaker() as db:
            third = await _scan_with_db(db)
            self.assertIsNotNone(third)
            self.assertEqual(third, (0, 0, 1))

            remaining = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.MOVIE
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(remaining, [])

    async def test_process_series_season_candidate_create_update_remove(self) -> None:
        async with self._sessionmaker() as db:
            rule = _make_rule(
                MediaType.SERIES,
                target_scope="season",
                min_size=1,
                include_never_watched=True,
            )
            series = Series(title="Series", tmdb_id=2001, size=8 * 1024**3)
            db.add_all([rule, series])
            await db.flush()

            series_ref = _make_series_ref(
                service_id="series-1",
                library_id="lib-1",
                path="/media/series",
            )
            series_ref.series_id = series.id
            db.add(series_ref)

            season = Season(series_id=series.id, season_number=1, size=3 * 1024**3)
            db.add(season)
            await db.commit()
            season_id = season.id

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(
                        ReclaimRule.media_type == MediaType.SERIES
                    )
                )
            ).scalar_one()
            first = await _process_series_seasons(db, [rule])
            self.assertEqual(first, (1, 0, 0))

            season_candidates = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.SERIES,
                            ReclaimCandidate.season_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(season_candidates), 1)
            self.assertEqual(season_candidates[0].season_id, season_id)

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(
                        ReclaimRule.media_type == MediaType.SERIES
                    )
                )
            ).scalar_one()
            second = await _process_series_seasons(db, [rule])
            self.assertEqual(second, (0, 1, 0))

        async with self._sessionmaker() as db:
            season = (
                await db.execute(select(Season).where(Season.id == season_id))
            ).scalar_one()
            season.size = 0
            await db.commit()

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(
                        ReclaimRule.media_type == MediaType.SERIES
                    )
                )
            ).scalar_one()
            third = await _process_series_seasons(db, [rule])
            self.assertEqual(third, (0, 0, 1))

            remaining_season_candidates = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.SERIES,
                            ReclaimCandidate.season_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(remaining_season_candidates, [])

    async def test_process_series_episode_candidates_skip_protected_episode(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_rule(
                MediaType.SERIES,
                target_scope="episode",
                min_size=1,
            )
            user = User(username="admin", password_hash="x")
            series = Series(title="Series", tmdb_id=2101, size=8 * 1024**3)
            db.add_all([rule, user, series])
            await db.flush()

            season = Season(series_id=series.id, season_number=1, size=4 * 1024**3)
            db.add(season)
            await db.flush()

            protected_episode = Episode(
                season_id=season.id,
                episode_number=1,
                size=2 * 1024**3,
            )
            unprotected_episode = Episode(
                season_id=season.id,
                episode_number=2,
                size=2 * 1024**3,
            )
            db.add_all([protected_episode, unprotected_episode])
            await db.flush()

            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    episode_id=protected_episode.id,
                    protected_by_user_id=user.id,
                    reason="protected",
                )
            )
            await db.commit()
            unprotected_episode_id = unprotected_episode.id

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(
                        ReclaimRule.media_type == MediaType.SERIES
                    )
                )
            ).scalar_one()
            result = await _process_series_episodes(db, [rule])
            self.assertEqual(result, (1, 0, 0))

            candidates = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.SERIES,
                            ReclaimCandidate.episode_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].episode_id, unprotected_episode_id)

    async def test_process_series_episode_candidates_remove_stale_protected_episode(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_rule(
                MediaType.SERIES,
                target_scope="episode",
                min_size=1,
            )
            user = User(username="admin", password_hash="x")
            series = Series(title="Series", tmdb_id=2201, size=8 * 1024**3)
            db.add_all([rule, user, series])
            await db.flush()

            season = Season(series_id=series.id, season_number=1, size=4 * 1024**3)
            db.add(season)
            await db.flush()

            episode_one = Episode(
                season_id=season.id,
                episode_number=1,
                size=2 * 1024**3,
            )
            episode_two = Episode(
                season_id=season.id,
                episode_number=2,
                size=2 * 1024**3,
            )
            db.add_all([episode_one, episode_two])
            await db.commit()

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(
                        ReclaimRule.media_type == MediaType.SERIES
                    )
                )
            ).scalar_one()
            first = await _process_series_episodes(db, [rule])
            self.assertEqual(first, (2, 0, 0))

        async with self._sessionmaker() as db:
            episode_one = (
                await db.execute(select(Episode).where(Episode.episode_number == 1))
            ).scalar_one()
            season = (
                await db.execute(
                    select(Season).where(Season.id == episode_one.season_id)
                )
            ).scalar_one()
            series = (
                await db.execute(select(Series).where(Series.id == season.series_id))
            ).scalar_one()
            user = (await db.execute(select(User))).scalar_one()
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    season_id=season.id,
                    episode_id=episode_one.id,
                    protected_by_user_id=user.id,
                    reason="protected",
                )
            )
            await db.commit()

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(
                        ReclaimRule.media_type == MediaType.SERIES
                    )
                )
            ).scalar_one()
            second = await _process_series_episodes(db, [rule])
            self.assertEqual(second, (0, 1, 1))

            remaining_candidates = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.SERIES,
                            ReclaimCandidate.episode_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(remaining_candidates), 1)
            self.assertNotEqual(remaining_candidates[0].episode_id, episode_one.id)

    async def test_collect_rule_preview_matches_does_not_persist_candidates(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Preview Movie", tmdb_id=3001, size=6 * 1024**3)
            db.add_all([rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="preview-mv-1",
                service_item_id="preview-item-1",
                file_name="Preview.Movie.mkv",
                video_height=2160,
                video_resolution="2160p",
                video_hdr=True,
            )
            version.movie_id = movie.id
            db.add(version)
            await db.flush()
            version_id = version.id
            await db.commit()

        async with self._sessionmaker() as db:
            rule = (
                await db.execute(
                    select(ReclaimRule).where(ReclaimRule.media_type == MediaType.MOVIE)
                )
            ).scalar_one()
            matches = await collect_rule_preview_matches(db, [rule])

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].movie_version_id, version_id)

            candidates = (
                (
                    await db.execute(
                        select(ReclaimCandidate).where(
                            ReclaimCandidate.media_type == MediaType.MOVIE
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(candidates, [])

            preview_items = await build_rule_preview_items(db, matches)
            self.assertEqual(len(preview_items), 1)
            self.assertEqual(preview_items[0].media_title, "Preview Movie")
            self.assertEqual(preview_items[0].version_video_resolution, "2160p")
            self.assertTrue(preview_items[0].reason_tokens)

    async def test_scan_syncs_leaving_soon_collections_when_enabled(self) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=True,
                leaving_soon_collection_title="Leaving Soon",
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=5001, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-5001",
                service_item_id="plex-item-5001",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake()
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(len(fake_plex.calls), 1)
        call = fake_plex.calls[0]
        self.assertEqual(call["base_title"], "Leaving Soon")
        self.assertEqual(call["movie_item_ids"], {"plex-item-5001"})
        self.assertEqual(call["series_item_ids"], set())
        self.assertEqual(fake_plex.delete_calls, [])

        async with self._sessionmaker() as db:
            settings = (await db.execute(select(GeneralSettings))).scalar_one()
            self.assertEqual(
                settings.leaving_soon_last_success_titles,
                {Service.PLEX.value: "Leaving Soon"},
            )

    async def test_scan_skips_leaving_soon_sync_when_disabled(self) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=False,
                leaving_soon_collection_title="Leaving Soon",
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=5002, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-5002",
                service_item_id="plex-item-5002",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake()
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(fake_plex.calls, [])

    async def test_scan_cleans_leaving_soon_collections_when_disabled(self) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=False,
                leaving_soon_collection_title="Leaving Soon",
                leaving_soon_last_success_titles={Service.PLEX.value: "Leaving Soon"},
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=50021, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-50021",
                service_item_id="plex-item-50021",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake()
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(fake_plex.calls, [])
        self.assertEqual(fake_plex.delete_calls, ["Leaving Soon"])

        async with self._sessionmaker() as db:
            settings = (await db.execute(select(GeneralSettings))).scalar_one()
            self.assertEqual(settings.leaving_soon_last_success_titles, {})

    async def test_scan_keeps_leaving_soon_titles_when_disabled_cleanup_fails(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=False,
                leaving_soon_collection_title="Leaving Soon",
                leaving_soon_last_success_titles={Service.PLEX.value: "Leaving Soon"},
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=50022, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-50022",
                service_item_id="plex-item-50022",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake(fail_delete_titles={"Leaving Soon"})
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(fake_plex.calls, [])
        self.assertEqual(fake_plex.delete_calls, ["Leaving Soon"])

        async with self._sessionmaker() as db:
            settings = (await db.execute(select(GeneralSettings))).scalar_one()
            self.assertEqual(
                settings.leaving_soon_last_success_titles,
                {Service.PLEX.value: "Leaving Soon"},
            )

    async def test_scan_maps_season_candidates_to_series_leaving_soon_items(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=True,
                leaving_soon_collection_title="Leaving Soon",
            )
            rule = _make_rule(
                MediaType.SERIES,
                target_scope="season",
                min_size=1,
                include_never_watched=True,
            )
            series = Series(title="Series", tmdb_id=5003, size=5 * 1024**3)
            db.add_all([settings, rule, series])
            await db.flush()

            series_ref = _make_series_ref(
                service_id="plex-series-5003",
                library_id="lib-series",
                library_name="TV",
            )
            series_ref.series_id = series.id
            db.add(series_ref)
            db.add(
                Season(
                    series_id=series.id,
                    season_number=1,
                    size=2 * 1024**3,
                )
            )
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake()
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(len(fake_plex.calls), 1)
        call = fake_plex.calls[0]
        self.assertEqual(call["movie_item_ids"], set())
        self.assertEqual(call["series_item_ids"], {"plex-series-5003"})

    async def test_scan_renames_using_last_success_title(self) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=True,
                leaving_soon_collection_title="Latest Soon",
                leaving_soon_last_success_titles={Service.PLEX.value: "Leaving Soon"},
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=5004, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-5004",
                service_item_id="plex-item-5004",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake()
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(len(fake_plex.calls), 1)
        self.assertEqual(fake_plex.calls[0]["base_title"], "Latest Soon")
        self.assertEqual(fake_plex.delete_calls, ["Leaving Soon"])

        async with self._sessionmaker() as db:
            settings = (await db.execute(select(GeneralSettings))).scalar_one()
            self.assertEqual(
                settings.leaving_soon_last_success_titles,
                {Service.PLEX.value: "Latest Soon"},
            )

    async def test_scan_keeps_last_success_title_when_old_cleanup_fails(self) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=True,
                leaving_soon_collection_title="Latest Soon",
                leaving_soon_last_success_titles={Service.PLEX.value: "Old A"},
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=5005, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-5005",
                service_item_id="plex-item-5005",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake(fail_delete_titles={"Old A"})
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(fake_plex.delete_calls, ["Old A"])
        self.assertEqual(len(fake_plex.calls), 1)
        self.assertEqual(fake_plex.calls[0]["base_title"], "Latest Soon")

        async with self._sessionmaker() as db:
            settings = (await db.execute(select(GeneralSettings))).scalar_one()
            self.assertEqual(
                settings.leaving_soon_last_success_titles,
                {Service.PLEX.value: "Old A"},
            )

    async def test_scan_keeps_last_success_title_when_current_sync_fails(self) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                leaving_soon_enabled=True,
                leaving_soon_collection_title="Latest Soon",
                leaving_soon_last_success_titles={Service.PLEX.value: "Old A"},
            )
            rule = _make_rule(
                MediaType.MOVIE,
                min_size=1,
                include_never_watched=True,
            )
            movie = Movie(title="Movie", tmdb_id=5006, size=3 * 1024**3)
            db.add_all([settings, rule, movie])
            await db.flush()

            version = _make_movie_version(
                service_media_id="mv-5006",
                service_item_id="plex-item-5006",
            )
            version.movie_id = movie.id
            version.service = Service.PLEX
            db.add(version)
            await db.commit()

        fake_plex = _LeavingSoonSyncServiceFake(fail_sync=True)
        previous_plex = cleanup_tasks.service_manager._plex
        previous_jellyfin = cleanup_tasks.service_manager._jellyfin
        previous_emby = cleanup_tasks.service_manager._emby
        cleanup_tasks.service_manager._plex = fake_plex  # type: ignore[assignment]
        cleanup_tasks.service_manager._jellyfin = None
        cleanup_tasks.service_manager._emby = None
        try:
            async with self._sessionmaker() as db:
                result = await _scan_with_db(db)
                self.assertEqual(result, (1, 0, 0))
        finally:
            cleanup_tasks.service_manager._plex = previous_plex
            cleanup_tasks.service_manager._jellyfin = previous_jellyfin
            cleanup_tasks.service_manager._emby = previous_emby

        self.assertEqual(fake_plex.calls, [])
        self.assertEqual(fake_plex.delete_calls, ["Old A"])

        async with self._sessionmaker() as db:
            settings = (await db.execute(select(GeneralSettings))).scalar_one()
            self.assertEqual(
                settings.leaving_soon_last_success_titles,
                {Service.PLEX.value: "Old A"},
            )
