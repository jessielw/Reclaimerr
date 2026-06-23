from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.candidate_views import build_rule_preview_items
from backend.core.rule_engine import (
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    SeerrRequestResolver,
    evaluate_advanced_rule,
)
from backend.database import Base
from backend.database.models import (
    Episode,
    GeneralSettings,
    MediaFavorite,
    Movie,
    MovieArrRef,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service
from backend.services.seerr_cache import SeerrRequestSnapshot
from backend.tasks import cleanup as cleanup_tasks
from backend.tasks.cleanup import (
    _compute_requester_has_watched_for_key,
    _evaluate_movie_rule,
    _evaluate_movie_version_rule,
    _evaluate_rule_for_episode,
    _evaluate_rule_for_season,
    _process_series_episodes,
    _process_series_seasons,
    _reconcile_rule_managed_protections,
    _scan_with_db,
    collect_rule_preview_matches,
    collect_rule_preview_matches_with_metadata,
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


def _condition(
    field: str, operator: str, value: object | None = None
) -> dict[str, object]:
    node: dict[str, object] = {
        "type": "condition",
        "field": field,
        "operator": operator,
    }
    if operator not in {"exists", "not_exists", "is_true", "is_false"}:
        node["value"] = value
    return node


def _group(op: str, *children: dict[str, object]) -> dict[str, object]:
    return {"type": "group", "op": op, "children": list(children)}


def _rule_with_root(
    media_type: MediaType, target_scope: str, root: dict[str, object]
) -> ReclaimRule:
    return ReclaimRule(
        name="logical rule",
        media_type=media_type,
        enabled=True,
        target_scope=target_scope,
        definition={"version": 1, "root": root},
        action={"candidate": True, "media_server_action": "delete"},
    )


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


def _make_single_condition_rule(
    *,
    name: str,
    media_type: MediaType,
    target_scope: str,
    field: str,
    operator: str,
    value: object | None = None,
) -> ReclaimRule:
    condition: dict[str, object] = {
        "type": "condition",
        "field": field,
        "operator": operator,
    }
    if value is not None:
        condition["value"] = value
    return ReclaimRule(
        name=name,
        media_type=media_type,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": [condition]},
        },
        action={"candidate": True, "media_server_action": "delete"},
    )


def _make_multi_condition_rule(
    *,
    name: str,
    media_type: MediaType,
    target_scope: str,
    conditions: list[dict[str, object]],
) -> ReclaimRule:
    return ReclaimRule(
        name=name,
        media_type=media_type,
        enabled=True,
        target_scope=target_scope,
        definition={
            "version": 1,
            "root": {"type": "group", "op": "and", "children": conditions},
        },
        action={"candidate": True, "media_server_action": "delete"},
    )


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


class _SonarrTagRefreshClientFake:
    def __init__(
        self,
        *,
        series: list[SimpleNamespace] | None = None,
        tags: list[SimpleNamespace] | None = None,
        fail: bool = False,
    ) -> None:
        self._series = list(series or [])
        self._tags = list(tags or [])
        self._fail = fail

    async def get_all_series(self) -> list[SimpleNamespace]:
        if self._fail:
            raise RuntimeError("sonarr unavailable")
        return list(self._series)

    async def get_tags(self) -> list[SimpleNamespace]:
        if self._fail:
            raise RuntimeError("sonarr unavailable")
        return list(self._tags)


class _SonarrEpisodeStateClientFake:
    def __init__(
        self,
        *,
        series: list[SimpleNamespace] | None = None,
        episodes: dict[tuple[int, int], list[dict[str, object]]] | None = None,
        fail_series: bool = False,
        fail_episodes: bool = False,
    ) -> None:
        self._series = list(series or [])
        self._episodes = dict(episodes or {})
        self._fail_series = fail_series
        self._fail_episodes = fail_episodes
        self.series_calls = 0
        self.episode_calls: list[tuple[int, int | None]] = []

    async def get_all_series(self) -> list[SimpleNamespace]:
        self.series_calls += 1
        if self._fail_series:
            raise RuntimeError("sonarr series unavailable")
        return list(self._series)

    async def get_episodes(
        self,
        series_id: int,
        season_number: int | None = None,
    ) -> list[dict[str, object]]:
        self.episode_calls.append((series_id, season_number))
        if self._fail_episodes:
            raise RuntimeError("sonarr episodes unavailable")
        if season_number is None:
            return []
        return list(self._episodes.get((series_id, season_number), []))

    async def get_disk_space(self) -> list[dict[str, object]]:
        return []


class _SonarrSharedRuleDataClientFake(_SonarrEpisodeStateClientFake):
    def __init__(
        self,
        *,
        series: list[SimpleNamespace],
        tags: list[SimpleNamespace],
        episodes: dict[tuple[int, int], list[dict[str, object]]],
    ) -> None:
        super().__init__(series=series, episodes=episodes)
        self._tags = list(tags)

    async def get_tags(self) -> list[SimpleNamespace]:
        return list(self._tags)


class _RadarrTagRefreshClientFake:
    def __init__(
        self,
        *,
        movies: list[SimpleNamespace] | None = None,
        tags: list[SimpleNamespace] | None = None,
        fail: bool = False,
    ) -> None:
        self._movies = list(movies or [])
        self._tags = list(tags or [])
        self._fail = fail

    async def get_all_movies(self) -> list[SimpleNamespace]:
        if self._fail:
            raise RuntimeError("radarr unavailable")
        return list(self._movies)

    async def get_tags(self) -> list[SimpleNamespace]:
        if self._fail:
            raise RuntimeError("radarr unavailable")
        return list(self._tags)


class CleanupMediaRuleTests(unittest.TestCase):
    def test_movie_version_extended_metadata_fields_match(self) -> None:
        movie = Movie(
            title="Movie",
            tmdb_id=1,
            year=2005,
            original_language="en",
            origin_country=["US"],
            runtime=116,
        )
        version = _make_movie_version(
            service_media_id="media-1",
            service_item_id="item-1",
            service=Service.JELLYFIN,
            container="mkv",
            video_bitrate=12_000_000,
            video_bit_depth=10,
            audio_bitrate=640_000,
            subtitle_count=2,
            subtitle_has_forced=True,
            size=1,
        )
        other_version = _make_movie_version(
            service_media_id="media-2",
            service_item_id="item-2",
        )
        movie.versions = [version, other_version]
        rule = _rule_with_root(
            MediaType.MOVIE,
            TARGET_MOVIE_VERSION,
            _group(
                "and",
                _condition("media.year", "equals", 2005),
                _condition("media.container", "contains_any", ["MKV"]),
                _condition("tmdb.original_language", "contains_any", ["eng"]),
                _condition("tmdb.origin_country", "contains_any", ["us"]),
                _condition("tmdb.runtime_minutes", "equals", 116),
                _condition("video.bitrate_kbps", "equals", 12000),
                _condition("video.bit_depth", "equals", 10),
                _condition("audio.bitrate_kbps", "equals", 640),
                _condition("subtitle.track_count", "equals", 2),
                _condition("subtitle.has_forced", "is_true"),
                _condition("movie.version_count", "equals", 2),
            ),
        )

        self.assertTrue(_evaluate_movie_version_rule(movie, version, rule, {}, []))

    def test_plex_bitrate_values_are_already_kbps(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1)
        version = _make_movie_version(
            service_media_id="media-1",
            service_item_id="item-1",
            service=Service.PLEX,
            video_bitrate=8914,
            audio_bitrate=655,
            size=1,
        )
        movie.versions = [version]
        rule = _rule_with_root(
            MediaType.MOVIE,
            TARGET_MOVIE_VERSION,
            _group(
                "and",
                _condition("video.bitrate_kbps", "equals", 8914),
                _condition("audio.bitrate_kbps", "equals", 655),
            ),
        )

        self.assertTrue(_evaluate_movie_version_rule(movie, version, rule, {}, []))

    def test_missing_language_and_country_fail_negative_list_matches(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1)
        version = _make_movie_version(
            service_media_id="media-1",
            service_item_id="item-1",
            size=1,
        )
        movie.versions = [version]

        for field, value in [
            ("tmdb.original_language", ["eng"]),
            ("tmdb.origin_country", ["US"]),
        ]:
            with self.subTest(field=field):
                rule = _rule_with_root(
                    MediaType.MOVIE,
                    TARGET_MOVIE_VERSION,
                    _group("and", _condition(field, "not_contains_any", value)),
                )
                self.assertFalse(
                    _evaluate_movie_version_rule(movie, version, rule, {}, [])
                )

    def test_series_metadata_and_season_counts_are_inherited_by_all_scopes(
        self,
    ) -> None:
        series = Series(
            title="Series",
            tmdb_id=2,
            year=2024,
            original_language="ja",
            origin_country=["JP"],
        )
        series.season_count = 4
        special = Season(series_id=1, season_number=0)
        season_one = Season(series_id=1, season_number=1, size=1)
        season_two = Season(series_id=1, season_number=2, size=1)
        series.seasons = [special, season_one, season_two]
        episode = Episode(season_id=1, episode_number=1)
        conditions = _group(
            "and",
            _condition("media.year", "equals", 2024),
            _condition("tmdb.original_language", "contains_any", ["jpn"]),
            _condition("tmdb.origin_country", "contains_any", ["jp"]),
            _condition("series.tmdb_season_count", "equals", 4),
            _condition("series.library_season_count", "equals", 2),
        )

        series_rule = _rule_with_root(MediaType.SERIES, TARGET_SERIES, conditions)
        season_rule = _rule_with_root(MediaType.SERIES, TARGET_SEASON, conditions)
        episode_rule = _rule_with_root(MediaType.SERIES, TARGET_EPISODE, conditions)

        self.assertTrue(_evaluate_movie_rule(series, series_rule, {}, []))
        self.assertTrue(
            _evaluate_rule_for_season(series, season_one, season_rule, {}, [])
        )
        self.assertTrue(
            _evaluate_rule_for_episode(
                series, season_one, episode, episode_rule, {}, []
            )
        )

    def test_nested_and_or_movie_version_only_records_matching_or_branch(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        version = _make_movie_version(
            service_media_id="media-1",
            service_item_id="item-1",
            library_id="lib-1",
            size=10 * 1024**3,
        )
        movie.versions = [version]
        rule = _rule_with_root(
            MediaType.MOVIE,
            TARGET_MOVIE_VERSION,
            _group(
                "and",
                _condition("media.size", "greater_than", 1024),
                _group(
                    "or",
                    _condition("library.id", "contains_any", ["lib-1"]),
                    _condition("imdb.rating", "greater_than", 9),
                ),
            ),
        )
        matched_criteria: dict = {}
        reasons: list[dict[str, object]] = []

        self.assertTrue(
            _evaluate_movie_version_rule(
                movie, version, rule, matched_criteria, reasons
            )
        )

        self.assertIn("media.size", matched_criteria)
        self.assertIn("library.id", matched_criteria)
        self.assertNotIn("imdb.rating", matched_criteria)
        self.assertEqual(len(reasons[0]["conditions"]), 2)  # type: ignore[reportArgumentType]

    def test_nested_and_or_series_scope_requires_all_and_children(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3, status="ended")
        passing_rule = _rule_with_root(
            MediaType.SERIES,
            TARGET_SERIES,
            _group(
                "and",
                _condition("series.status", "equals", "ended"),
                _group(
                    "or",
                    _condition("media.size", "greater_than", 10),
                    _condition("arr.monitored", "is_true"),
                ),
            ),
        )
        failing_rule = _rule_with_root(
            MediaType.SERIES,
            TARGET_SERIES,
            _group(
                "and",
                _condition("series.status", "equals", "ended"),
                _group(
                    "or",
                    _condition("media.size", "less_than", 10),
                    _condition("arr.monitored", "is_true"),
                ),
            ),
        )

        self.assertTrue(_evaluate_movie_rule(series, passing_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(series, failing_rule, {}, []))

    def test_nested_and_or_season_scope_matches_expected_branch(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        season = Season(
            series_id=1,
            season_number=1,
            size=4 * 1024**3,
            episode_count=4,
        )
        rule = _rule_with_root(
            MediaType.SERIES,
            TARGET_SEASON,
            _group(
                "or",
                _group(
                    "and",
                    _condition("season.season_number", "equals", 1),
                    _condition("season.episode_count", "greater_than_or_equal", 4),
                ),
                _condition("season.season_number", "equals", 2),
            ),
        )
        matched_criteria: dict = {}
        reasons: list[dict[str, object]] = []

        self.assertTrue(
            _evaluate_rule_for_season(series, season, rule, matched_criteria, reasons)
        )

        self.assertEqual(matched_criteria["season.season_number"], 1)
        self.assertEqual(matched_criteria["season.episode_count"], 4)
        self.assertEqual(len(reasons[0]["conditions"]), 2)  # type: ignore[reportArgumentType]

    def test_nested_and_or_episode_scope_matches_expected_branch(self) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        episode = Episode(
            season_id=1,
            episode_number=3,
            size=1 * 1024**3,
            view_count=0,
        )
        rule = _rule_with_root(
            MediaType.SERIES,
            TARGET_EPISODE,
            _group(
                "and",
                _condition("episode.number", "equals", 3),
                _group(
                    "or",
                    _condition("watch.never_watched", "is_true"),
                    _condition("watch.view_count", "greater_than", 10),
                ),
            ),
        )
        matched, criteria, rule_reasons = evaluate_advanced_rule(
            rule,
            target_scope=TARGET_EPISODE,
            series=series,
            season=season,
            episode=episode,
        )

        self.assertTrue(matched)
        self.assertEqual(criteria["episode.number"], 3)
        self.assertTrue(criteria["watch.never_watched"])
        self.assertNotIn("watch.view_count", criteria)
        self.assertEqual(len(rule_reasons), 2)

    def test_malformed_runtime_group_fails_closed(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        version = _make_movie_version(
            service_media_id="media-1",
            service_item_id="item-1",
            size=10 * 1024**3,
        )
        rule = _rule_with_root(
            MediaType.MOVIE,
            TARGET_MOVIE_VERSION,
            _group("xor", _condition("media.size", "greater_than", 1024)),
        )

        matched, criteria, rule_reasons = evaluate_advanced_rule(
            rule,
            target_scope=TARGET_MOVIE_VERSION,
            movie=movie,
            version=version,
        )

        self.assertFalse(matched)
        self.assertEqual(criteria, {})
        self.assertEqual(rule_reasons, [])

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

    def test_evaluate_movie_rule_path_folder_literal_and_filename_regex_passes(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                path="/media/movies/action/Example.Movie.2024.mkv",
                file_name=None,
            )
        ]
        rule = _make_multi_condition_rule(
            name="path-folder-and-filename-regex",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            conditions=[
                {
                    "type": "condition",
                    "field": "media.path",
                    "operator": "equals",
                    "value": "/media/movies/action",
                },
                {
                    "type": "condition",
                    "field": "media.file_name",
                    "operator": "matches_any_regex",
                    "value": [r"example\.movie\.2024\.mkv$"],
                },
            ],
        )

        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_movie_rule_path_and_filename_regex_and_condition_uses_path_basename_fallback(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=1)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                path="/media/movies/action/Example.Movie.2024.mkv",
                file_name=None,
            )
        ]
        rule = _make_multi_condition_rule(
            name="path-and-filename-and",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            conditions=[
                {
                    "type": "condition",
                    "field": "media.path",
                    "operator": "matches_any_regex",
                    "value": [r"^/media/movies/action"],
                },
                {
                    "type": "condition",
                    "field": "media.file_name",
                    "operator": "matches_any_regex",
                    "value": [r"example\.movie\.2024\.mkv$"],
                },
            ],
        )

        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_series_rule_filename_regex_uses_series_path_basename(
        self,
    ) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.last_viewed_at = datetime.now(UTC) - timedelta(days=3)
        series.service_refs = [
            _make_series_ref(service_id="sr-1", path="/media/shows/Example.Series")
        ]
        rule = _make_single_condition_rule(
            name="series-filename-regex",
            media_type=MediaType.SERIES,
            target_scope="series",
            field="media.file_name",
            operator="matches_any_regex",
            value=[r"example\.series$"],
        )

        self.assertTrue(_evaluate_movie_rule(series, rule, {}, []))

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

    def test_evaluate_movie_rule_watch_never_watched_is_true_for_unwatched_movie(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.view_count = 0
        movie.last_viewed_at = None
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        rule = _make_single_condition_rule(
            name="movie-never-watched-true",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="watch.never_watched",
            operator="is_true",
        )

        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_movie_rule_watch_never_watched_is_false_for_watched_movie(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.view_count = 3
        movie.last_viewed_at = datetime.now(UTC) - timedelta(days=3)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        rule = _make_single_condition_rule(
            name="movie-never-watched-false",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="watch.never_watched",
            operator="is_false",
        )

        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_movie_rule_watch_never_watched_treats_readded_copy_as_unwatched(
        self,
    ) -> None:
        now = datetime.now(UTC)
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.view_count = 3
        movie.last_viewed_at = now - timedelta(days=90)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                added_at=now - timedelta(days=1),
            )
        ]
        rule = _make_single_condition_rule(
            name="movie-never-watched-stale-watch-data",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="watch.never_watched",
            operator="is_true",
        )

        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_series_rule_watch_never_watched_is_true_for_unwatched_series(
        self,
    ) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.view_count = 0
        series.last_viewed_at = None
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        rule = _make_single_condition_rule(
            name="series-never-watched-true",
            media_type=MediaType.SERIES,
            target_scope="series",
            field="watch.never_watched",
            operator="is_true",
        )

        self.assertTrue(_evaluate_movie_rule(series, rule, {}, []))

    def test_evaluate_rule_for_season_watch_never_watched_is_false_for_watched_season(
        self,
    ) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.view_count = 2
        season.last_viewed_at = now - timedelta(days=2)
        season.added_at = now - timedelta(days=30)
        rule = _make_single_condition_rule(
            name="season-never-watched-false",
            media_type=MediaType.SERIES,
            target_scope="season",
            field="watch.never_watched",
            operator="is_false",
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, rule, {}, []))

    def test_evaluate_rule_for_season_watch_never_watched_treats_readded_as_unwatched(
        self,
    ) -> None:
        now = datetime.now(UTC)
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.service_refs = [_make_series_ref(service_id="sr-1")]
        season = Season(series_id=1, season_number=1, size=4 * 1024**3)
        season.view_count = 2
        season.last_viewed_at = now - timedelta(days=90)
        season.added_at = now - timedelta(days=5)
        rule = _make_single_condition_rule(
            name="season-never-watched-stale-watch-data",
            media_type=MediaType.SERIES,
            target_scope="season",
            field="watch.never_watched",
            operator="is_true",
        )

        self.assertTrue(_evaluate_rule_for_season(series, season, rule, {}, []))

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

    def test_evaluate_movie_rule_tmdb_in_collection_true_false(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.tmdb_collection_checked = True
        movie.tmdb_collection_id = 10
        movie.tmdb_collection_name = "Die Hard Collection"
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]

        in_collection_rule = _make_single_condition_rule(
            name="movie-in-collection",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.in_collection",
            operator="is_true",
        )
        not_in_collection_rule = _make_single_condition_rule(
            name="movie-not-in-collection",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.in_collection",
            operator="is_false",
        )

        self.assertTrue(_evaluate_movie_rule(movie, in_collection_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, not_in_collection_rule, {}, []))

    def test_evaluate_movie_rule_tmdb_in_collection_unknown_does_not_match_false(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.tmdb_collection_checked = False
        movie.tmdb_collection_id = None
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        rule = _make_single_condition_rule(
            name="movie-not-in-collection",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.in_collection",
            operator="is_false",
        )

        self.assertFalse(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_movie_rule_tmdb_collection_name_matches(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.tmdb_collection_checked = True
        movie.tmdb_collection_id = 10
        movie.tmdb_collection_name = "Star Wars Collection"
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        match_rule = _make_single_condition_rule(
            name="movie-collection-name-match",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.collection_name",
            operator="contains_any",
            value=["star wars collection"],
        )
        miss_rule = _make_single_condition_rule(
            name="movie-collection-name-miss",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.collection_name",
            operator="equals",
            value="Die Hard Collection",
        )

        self.assertTrue(_evaluate_movie_rule(movie, match_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, miss_rule, {}, []))

    def test_evaluate_movie_rule_tmdb_collection_name_contains_all_and_negation(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.tmdb_collection_checked = True
        movie.tmdb_collection_id = 10
        movie.tmdb_collection_name = "Star Wars Collection"
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]

        contains_all_rule = _make_single_condition_rule(
            name="movie-collection-contains-all",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.collection_name",
            operator="contains_all",
            value=["star wars collection"],
        )
        missing_one_rule = _make_single_condition_rule(
            name="movie-collection-missing-one",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.collection_name",
            operator="contains_all",
            value=["star wars collection", "die hard"],
        )
        not_contains_all_rule = _make_single_condition_rule(
            name="movie-collection-not-contains-all",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.collection_name",
            operator="not_contains_all",
            value=["star wars collection", "die hard"],
        )

        self.assertTrue(_evaluate_movie_rule(movie, contains_all_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, missing_one_rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, not_contains_all_rule, {}, []))

    def test_evaluate_movie_rule_tmdb_genres_matches_tmdb_objects(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        cast(Any, movie).genres = [
            {"id": 28, "name": "Action"},
            {"id": 878, "name": "Science Fiction"},
        ]
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]

        match_rule = _make_single_condition_rule(
            name="movie-genre-match",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.genres",
            operator="contains_any",
            value=["science fiction"],
        )
        miss_rule = _make_single_condition_rule(
            name="movie-genre-miss",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.genres",
            operator="contains_all",
            value=["Action", "Comedy"],
        )
        not_all_rule = _make_single_condition_rule(
            name="movie-genre-not-all",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.genres",
            operator="not_contains_all",
            value=["Action", "Comedy"],
        )

        self.assertTrue(_evaluate_movie_rule(movie, match_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, miss_rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, not_all_rule, {}, []))

    def test_evaluate_series_season_episode_rules_use_series_tmdb_genres(
        self,
    ) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        series.genres = ["Drama", "Mystery"]
        season = Season(
            series_id=1,
            season_number=1,
            episode_count=1,
            size=5 * 1024**3,
            view_count=0,
        )
        episode = Episode(
            season_id=1,
            episode_number=1,
            size=1024,
            view_count=0,
        )
        series.seasons = [season]
        season.episodes = [episode]

        series_rule = _make_single_condition_rule(
            name="series-genre",
            media_type=MediaType.SERIES,
            target_scope="series",
            field="tmdb.genres",
            operator="contains_all",
            value=["Drama", "Mystery"],
        )
        season_rule = _make_single_condition_rule(
            name="season-genre",
            media_type=MediaType.SERIES,
            target_scope="season",
            field="tmdb.genres",
            operator="contains_any",
            value=["drama"],
        )
        episode_rule = _make_single_condition_rule(
            name="episode-genre",
            media_type=MediaType.SERIES,
            target_scope="episode",
            field="tmdb.genres",
            operator="not_contains_any",
            value=["Comedy"],
        )

        self.assertTrue(_evaluate_movie_rule(series, series_rule, {}, []))
        self.assertTrue(_evaluate_rule_for_season(series, season, season_rule, {}, []))
        self.assertTrue(
            _evaluate_rule_for_episode(
                series,
                season,
                episode,
                episode_rule,
                {},
                [],
            )
        )

    def test_evaluate_movie_rule_tmdb_genres_exists_and_missing(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.genres = []
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]

        exists_rule = _make_single_condition_rule(
            name="movie-genre-exists",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.genres",
            operator="exists",
        )
        missing_rule = _make_single_condition_rule(
            name="movie-genre-missing",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="tmdb.genres",
            operator="not_exists",
        )

        self.assertFalse(_evaluate_movie_rule(movie, exists_rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, missing_rule, {}, []))

    def test_evaluate_movie_rule_media_server_collections_matches_version(
        self,
    ) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        version = _make_movie_version(service_media_id="m1", service_item_id="i1")
        version.media_server_collection_names = ["Leaving Soon", "Holiday"]
        movie.versions = [version]

        match_rule = _make_single_condition_rule(
            name="movie-media-server-collection-match",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="media_server.collections",
            operator="contains_any",
            value=["leaving soon"],
        )
        miss_rule = _make_single_condition_rule(
            name="movie-media-server-collection-miss",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="media_server.collections",
            operator="contains_all",
            value=["Leaving Soon", "Kids"],
        )
        not_all_rule = _make_single_condition_rule(
            name="movie-media-server-collection-not-all",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="media_server.collections",
            operator="not_contains_all",
            value=["Leaving Soon", "Kids"],
        )

        self.assertTrue(_evaluate_movie_rule(movie, match_rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(movie, miss_rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, not_all_rule, {}, []))

    def test_evaluate_series_scopes_use_service_ref_media_server_collections(
        self,
    ) -> None:
        series = Series(title="Series", tmdb_id=2, size=20 * 1024**3)
        ref = _make_series_ref(service_id="series-1")
        ref.media_server_collection_names = ["Leaving Soon", "Favorites"]
        series.service_refs = [ref]
        season = Season(
            series_id=1,
            season_number=1,
            episode_count=1,
            size=5 * 1024**3,
            view_count=0,
        )
        episode = Episode(
            season_id=1,
            episode_number=1,
            size=1024,
            view_count=0,
        )
        series.seasons = [season]
        season.episodes = [episode]

        series_rule = _make_single_condition_rule(
            name="series-media-server-collection",
            media_type=MediaType.SERIES,
            target_scope="series",
            field="media_server.collections",
            operator="contains_all",
            value=["Leaving Soon", "Favorites"],
        )
        season_rule = _make_single_condition_rule(
            name="season-media-server-collection",
            media_type=MediaType.SERIES,
            target_scope="season",
            field="media_server.collections",
            operator="contains_any",
            value=["leaving soon"],
        )
        episode_rule = _make_single_condition_rule(
            name="episode-media-server-collection",
            media_type=MediaType.SERIES,
            target_scope="episode",
            field="media_server.collections",
            operator="not_contains_any",
            value=["Kids"],
        )

        self.assertTrue(_evaluate_movie_rule(series, series_rule, {}, []))
        self.assertTrue(_evaluate_rule_for_season(series, season, season_rule, {}, []))
        self.assertTrue(
            _evaluate_rule_for_episode(
                series,
                season,
                episode,
                episode_rule,
                {},
                [],
            )
        )

    def test_evaluate_movie_rule_media_server_collections_missing(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.versions = [
            _make_movie_version(service_media_id="m1", service_item_id="i1")
        ]
        rule = _make_single_condition_rule(
            name="movie-media-server-collection-missing",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="media_server.collections",
            operator="not_exists",
        )

        self.assertTrue(_evaluate_movie_rule(movie, rule, {}, []))

    def test_evaluate_movie_rule_media_path_contains_all_and_negation(self) -> None:
        movie = Movie(title="Movie", tmdb_id=1, size=10 * 1024**3)
        movie.versions = [
            _make_movie_version(
                service_media_id="m1",
                service_item_id="i1",
                path="/media/movies/star-wars-collection/movie.mkv",
            )
        ]

        contains_all_rule = _make_single_condition_rule(
            name="movie-path-contains-all",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="media.path",
            operator="contains_all",
            value=["/media/movies", "/media/movies/star-wars-collection"],
        )
        not_contains_all_rule = _make_single_condition_rule(
            name="movie-path-not-contains-all",
            media_type=MediaType.MOVIE,
            target_scope="movie_version",
            field="media.path",
            operator="not_contains_all",
            value=["/media/movies", "/media/movies/does-not-exist"],
        )

        self.assertTrue(_evaluate_movie_rule(movie, contains_all_rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(movie, not_contains_all_rule, {}, []))

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

    def test_evaluate_series_rule_arr_tags_legacy_not_equals_is_list_aware(
        self,
    ) -> None:
        rule = _make_single_condition_rule(
            name="not-garbage",
            media_type=MediaType.SERIES,
            target_scope="series",
            field="arr.tags",
            operator="not_equals",
            value="garbage",
        )

        untagged = Series(title="Untagged Series", tmdb_id=201, size=20 * 1024**3)
        untagged.arr_tags = []
        other_tag = Series(title="Other Tag Series", tmdb_id=202, size=20 * 1024**3)
        other_tag.arr_tags = ["rec-low-rated-barely-watched-ser"]
        missing_size = Series(title="Missing Size Series", tmdb_id=204)
        missing_size.arr_tags = []
        garbage = Series(title="Garbage Series", tmdb_id=203, size=20 * 1024**3)
        garbage.arr_tags = ["garbage"]

        self.assertTrue(_evaluate_movie_rule(untagged, rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(other_tag, rule, {}, []))
        self.assertTrue(_evaluate_movie_rule(missing_size, rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(garbage, rule, {}, []))

    def test_evaluate_series_rule_arr_tags_legacy_equals_is_list_aware(self) -> None:
        rule = _make_single_condition_rule(
            name="is-garbage",
            media_type=MediaType.SERIES,
            target_scope="series",
            field="arr.tags",
            operator="equals",
            value="garbage",
        )

        tagged = Series(title="Tagged Series", tmdb_id=204, size=20 * 1024**3)
        tagged.arr_tags = ["keep", "garbage"]
        untagged = Series(title="Untagged Series", tmdb_id=205, size=20 * 1024**3)
        untagged.arr_tags = []
        other_tag = Series(title="Other Tag Series", tmdb_id=206, size=20 * 1024**3)
        other_tag.arr_tags = ["keep"]

        self.assertTrue(_evaluate_movie_rule(tagged, rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(untagged, rule, {}, []))
        self.assertFalse(_evaluate_movie_rule(other_tag, rule, {}, []))

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
        self._async_db_patch = patch.object(
            cleanup_tasks,
            "async_db",
            self._sessionmaker,
        )
        self._async_db_patch.start()

    async def asyncTearDown(self) -> None:
        self._async_db_patch.stop()
        await self._engine.dispose()
        if self._db_path.exists():
            self._db_path.unlink()

    async def test_automated_protection_wins_over_matching_candidate(self) -> None:
        async with self._sessionmaker() as db:
            protection_rule = _make_rule(MediaType.MOVIE, min_size=1)
            protection_rule.name = "Protect large movies"
            protection_rule.action = {"outcome": "protect"}
            candidate_rule = _make_rule(MediaType.MOVIE, min_size=1)
            candidate_rule.name = "Delete large movies"
            movie = Movie(title="Protected Movie", tmdb_id=10001, size=1024)
            db.add_all([protection_rule, candidate_rule, movie])
            await db.flush()
            version = _make_movie_version(
                service_media_id="protected-version",
                service_item_id="protected-item",
            )
            version.movie_id = movie.id
            version.size = 1024
            db.add(version)
            await db.commit()

        async with self._sessionmaker() as db:
            result = await _scan_with_db(db)
            self.assertEqual(result, (0, 0, 0))
            candidates = (await db.execute(select(ReclaimCandidate))).scalars().all()
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(ProtectedMedia.source == "rule")
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(candidates, [])
            self.assertEqual(len(protections), 1)
            self.assertEqual(protections[0].source_rule_id, protection_rule.id)

    async def test_automated_protection_respects_library_scope(self) -> None:
        async with self._sessionmaker() as db:
            protection_rule = _make_rule(
                MediaType.MOVIE,
                library_ids=["protected-library"],
                min_size=1,
            )
            protection_rule.action = {"outcome": "protect"}
            protected_movie = Movie(
                title="Protected Library Movie",
                tmdb_id=10006,
                size=1024,
            )
            other_movie = Movie(
                title="Other Library Movie",
                tmdb_id=10007,
                size=1024,
            )
            db.add_all([protection_rule, protected_movie, other_movie])
            await db.flush()

            protected_version = _make_movie_version(
                service_media_id="protected-library-version",
                service_item_id="protected-library-item",
                library_id="protected-library",
            )
            protected_version.movie_id = protected_movie.id
            protected_version.size = 1024
            other_version = _make_movie_version(
                service_media_id="other-library-version",
                service_item_id="other-library-item",
                library_id="other-library",
            )
            other_version.movie_id = other_movie.id
            other_version.size = 1024
            db.add_all([protected_version, other_version])
            await db.commit()
            protected_version_id = protected_version.id

        async with self._sessionmaker() as db:
            await _scan_with_db(db)
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(ProtectedMedia.source == "rule")
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(protections), 1)
            self.assertEqual(
                protections[0].movie_version_id,
                protected_version_id,
            )

    async def test_rule_reconciliation_preserves_manual_protection(self) -> None:
        async with self._sessionmaker() as db:
            user = User(username="admin", password_hash="x")
            rule = _make_rule(MediaType.SERIES, min_size=1)
            rule.action = {"outcome": "protect"}
            series = Series(title="Protected Series", tmdb_id=10002, size=1024)
            db.add_all([user, rule, series])
            await db.flush()
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    protected_by_user_id=user.id,
                    source="manual",
                    reason="Keep manually",
                )
            )
            await db.commit()
            series_id = series.id

        async with self._sessionmaker() as db:
            await _scan_with_db(db)
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(
                            ProtectedMedia.series_id == series_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(
                {entry.source for entry in protections}, {"manual", "rule"}
            )

            series = await db.get(Series, series_id)
            self.assertIsNotNone(series)
            assert series is not None
            series.size = 0
            await db.commit()

        async with self._sessionmaker() as db:
            await _scan_with_db(db)
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(
                            ProtectedMedia.series_id == series_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(protections), 1)
            self.assertEqual(protections[0].source, "manual")

    async def test_multiple_protection_rules_create_separate_entries(self) -> None:
        async with self._sessionmaker() as db:
            first_rule = _make_rule(MediaType.SERIES, min_size=1)
            first_rule.name = "Protect one"
            first_rule.action = {"outcome": "protect"}
            second_rule = _make_rule(MediaType.SERIES, min_size=1)
            second_rule.name = "Protect two"
            second_rule.action = {"outcome": "protect"}
            series = Series(title="Twice Protected", tmdb_id=10003, size=1024)
            db.add_all([first_rule, second_rule, series])
            await db.commit()

        async with self._sessionmaker() as db:
            await _scan_with_db(db)
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(ProtectedMedia.source == "rule")
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(protections), 2)
            self.assertEqual(
                {entry.source_rule_id for entry in protections},
                {first_rule.id, second_rule.id},
            )

    async def test_skipped_protection_rule_can_preserve_existing_entries(self) -> None:
        async with self._sessionmaker() as db:
            rule = _make_rule(MediaType.SERIES, min_size=1)
            rule.action = {"outcome": "protect"}
            series = Series(title="Preserved Series", tmdb_id=10004, size=1024)
            db.add_all([rule, series])
            await db.flush()
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    protected_by_user_id=None,
                    source="rule",
                    source_rule_id=rule.id,
                    reason="Existing managed protection",
                )
            )
            await db.commit()
            rule_id = rule.id

        async with self._sessionmaker() as db:
            result = await _reconcile_rule_managed_protections(
                db,
                [],
                preserve_rule_ids={rule_id},
            )
            self.assertEqual(result, (0, 0, 0))
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(ProtectedMedia.source == "rule")
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(protections), 1)

    async def test_protection_preview_includes_already_protected_media(self) -> None:
        async with self._sessionmaker() as db:
            user = User(username="admin", password_hash="x")
            rule = _make_rule(MediaType.SERIES, min_size=1)
            rule.action = {"outcome": "protect"}
            series = Series(title="Preview Protected", tmdb_id=10005, size=1024)
            db.add_all([user, rule, series])
            await db.flush()
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=series.id,
                    protected_by_user_id=user.id,
                    reason="Manual protection",
                )
            )
            await db.commit()
            series_id = series.id

        async with self._sessionmaker() as db:
            rule = (await db.execute(select(ReclaimRule))).scalar_one()
            result = await collect_rule_preview_matches_with_metadata(db, [rule])
            self.assertEqual([match.series_id for match in result.matches], [series_id])
            self.assertEqual(result.metadata.skipped_protected_count, 0)

    async def test_sonarr_rule_fetches_only_latest_regular_season(self) -> None:
        async with self._sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-latest",
                api_key="x",
                name="sonarr-latest",
                enabled=True,
            )
            rule = _make_multi_condition_rule(
                name="latest-season-state",
                media_type=MediaType.SERIES,
                target_scope="series",
                conditions=[
                    {
                        "type": "condition",
                        "field": "sonarr.latest_season_has_unaired_episodes",
                        "operator": "is_true",
                    },
                    {
                        "type": "condition",
                        "field": "sonarr.latest_season_has_finale",
                        "operator": "is_true",
                    },
                ],
            )
            series = Series(title="Latest Season", tmdb_id=11001, size=1024)
            db.add_all([config, rule, series])
            await db.flush()
            db.add(
                SeriesArrRef(
                    series_id=series.id,
                    service_config_id=config.id,
                    arr_series_id=501,
                    tmdb_id=series.tmdb_id,
                )
            )
            await db.commit()
            config_id = config.id
            series_id = series.id

        future = datetime.now(UTC) + timedelta(days=7)
        fake_client = _SonarrEpisodeStateClientFake(
            series=[
                SimpleNamespace(
                    id=501,
                    seasons=[
                        SimpleNamespace(season_number=0, statistics={}),
                        SimpleNamespace(season_number=1, statistics={}),
                        SimpleNamespace(season_number=2, statistics={}),
                    ],
                )
            ],
            episodes={
                (501, 2): [
                    {
                        "seasonNumber": 2,
                        "episodeNumber": 1,
                        "airDateUtc": future.isoformat(),
                        "finaleType": "season",
                    }
                ]
            },
        )
        previous_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            config_id: fake_client
        }
        cleanup_tasks.service_manager._sonarr = None
        try:
            async with self._sessionmaker() as db:
                rule = (await db.execute(select(ReclaimRule))).scalar_one()
                result = await collect_rule_preview_matches_with_metadata(db, [rule])
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        self.assertEqual(fake_client.episode_calls, [(501, 2)])
        self.assertEqual([match.series_id for match in result.matches], [series_id])
        self.assertEqual(result.metadata.sonarr_unavailable_count, 0)

    async def test_sonarr_next_airing_proves_unaired_without_episode_fetch(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-next-airing",
                api_key="x",
                name="sonarr-next-airing",
                enabled=True,
            )
            rule = _make_single_condition_rule(
                name="has-unaired",
                media_type=MediaType.SERIES,
                target_scope="series",
                field="sonarr.latest_season_has_unaired_episodes",
                operator="is_true",
            )
            series = Series(title="Next Airing", tmdb_id=11002, size=1024)
            db.add_all([config, rule, series])
            await db.flush()
            db.add(
                SeriesArrRef(
                    series_id=series.id,
                    service_config_id=config.id,
                    arr_series_id=502,
                    tmdb_id=series.tmdb_id,
                )
            )
            await db.commit()
            config_id = config.id

        fake_client = _SonarrEpisodeStateClientFake(
            series=[
                SimpleNamespace(
                    id=502,
                    seasons=[
                        SimpleNamespace(
                            season_number=3,
                            statistics={
                                "nextAiring": (
                                    datetime.now(UTC) + timedelta(days=2)
                                ).isoformat()
                            },
                        )
                    ],
                )
            ]
        )
        previous_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            config_id: fake_client
        }
        cleanup_tasks.service_manager._sonarr = None
        try:
            async with self._sessionmaker() as db:
                rule = (await db.execute(select(ReclaimRule))).scalar_one()
                result = await collect_rule_preview_matches_with_metadata(db, [rule])
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        self.assertEqual(len(result.matches), 1)
        self.assertEqual(fake_client.episode_calls, [])

    async def test_sonarr_rule_refreshes_share_one_bulk_series_snapshot(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-shared-snapshot",
                api_key="x",
                name="sonarr-shared-snapshot",
                enabled=True,
            )
            rule = _make_multi_condition_rule(
                name="shared-sonarr-snapshot",
                media_type=MediaType.SERIES,
                target_scope="series",
                conditions=[
                    {
                        "type": "condition",
                        "field": "arr.tags",
                        "operator": "contains_any",
                        "value": ["keep"],
                    },
                    {
                        "type": "condition",
                        "field": "arr.monitored",
                        "operator": "is_true",
                    },
                    {
                        "type": "condition",
                        "field": "sonarr.latest_season_has_finale",
                        "operator": "is_true",
                    },
                ],
            )
            series = Series(title="Shared Snapshot", tmdb_id=11005, size=1024)
            db.add_all([config, rule, series])
            await db.flush()
            db.add(
                SeriesArrRef(
                    series_id=series.id,
                    service_config_id=config.id,
                    arr_series_id=505,
                    tmdb_id=series.tmdb_id,
                )
            )
            await db.commit()
            config_id = config.id
            series_id = series.id

        fake_client = _SonarrSharedRuleDataClientFake(
            series=[
                SimpleNamespace(
                    id=505,
                    monitored=True,
                    tags=[1],
                    seasons=[
                        SimpleNamespace(
                            season_number=1,
                            monitored=True,
                            statistics={},
                        )
                    ],
                )
            ],
            tags=[SimpleNamespace(id=1, label="keep")],
            episodes={
                (505, 1): [
                    {
                        "seasonNumber": 1,
                        "episodeNumber": 10,
                        "airDateUtc": datetime.now(UTC).isoformat(),
                        "finaleType": "season",
                    }
                ]
            },
        )
        previous_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            config_id: fake_client
        }
        cleanup_tasks.service_manager._sonarr = None
        try:
            async with self._sessionmaker() as db:
                rule = (await db.execute(select(ReclaimRule))).scalar_one()
                with patch.object(cleanup_tasks, "async_db", self._sessionmaker):
                    result = await collect_rule_preview_matches_with_metadata(
                        db, [rule]
                    )
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        self.assertEqual(fake_client.series_calls, 1)
        self.assertEqual(fake_client.episode_calls, [(505, 1)])
        self.assertEqual([match.series_id for match in result.matches], [series_id])

    async def test_sonarr_rule_skips_fetch_when_local_or_branch_matches(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-local-branch",
                api_key="x",
                name="sonarr-local-branch",
                enabled=True,
            )
            rule = _make_multi_condition_rule(
                name="local-or-sonarr",
                media_type=MediaType.SERIES,
                target_scope="series",
                conditions=[
                    {
                        "type": "condition",
                        "field": "media.size",
                        "operator": "greater_than",
                        "value": 1,
                    },
                    {
                        "type": "condition",
                        "field": "sonarr.latest_season_has_finale",
                        "operator": "is_true",
                    },
                ],
            )
            rule.definition["root"]["op"] = "or"
            series = Series(title="Local Match", tmdb_id=11003, size=1024)
            db.add_all([config, rule, series])
            await db.flush()
            db.add(
                SeriesArrRef(
                    series_id=series.id,
                    service_config_id=config.id,
                    arr_series_id=503,
                    tmdb_id=series.tmdb_id,
                )
            )
            await db.commit()
            config_id = config.id

        fake_client = _SonarrEpisodeStateClientFake(
            series=[
                SimpleNamespace(
                    id=503,
                    seasons=[SimpleNamespace(season_number=1, statistics={})],
                )
            ]
        )
        previous_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            config_id: fake_client
        }
        cleanup_tasks.service_manager._sonarr = None
        try:
            async with self._sessionmaker() as db:
                rule = (await db.execute(select(ReclaimRule))).scalar_one()
                result = await collect_rule_preview_matches_with_metadata(db, [rule])
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        self.assertEqual(len(result.matches), 1)
        self.assertEqual(fake_client.episode_calls, [])
        self.assertEqual(result.metadata.sonarr_unavailable_count, 0)

    async def test_sonarr_failure_preserves_affected_managed_protection(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-failure",
                api_key="x",
                name="sonarr-failure",
                enabled=True,
            )
            rule = _make_single_condition_rule(
                name="protect-unfinished",
                media_type=MediaType.SERIES,
                target_scope="series",
                field="sonarr.latest_season_has_finale",
                operator="is_false",
            )
            rule.action = {"outcome": "protect"}
            series = Series(title="Preserve on Failure", tmdb_id=11004, size=1024)
            db.add_all([config, rule, series])
            await db.flush()
            db.add_all(
                [
                    SeriesArrRef(
                        series_id=series.id,
                        service_config_id=config.id,
                        arr_series_id=504,
                        tmdb_id=series.tmdb_id,
                    ),
                    ProtectedMedia(
                        media_type=MediaType.SERIES,
                        series_id=series.id,
                        protected_by_user_id=None,
                        source="rule",
                        source_rule_id=rule.id,
                        reason="Existing Sonarr protection",
                    ),
                ]
            )
            await db.commit()
            config_id = config.id
            series_id = series.id

        fake_client = _SonarrEpisodeStateClientFake(fail_series=True)
        previous_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            config_id: fake_client
        }
        cleanup_tasks.service_manager._sonarr = None
        try:
            async with self._sessionmaker() as db:
                await _scan_with_db(db)
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        async with self._sessionmaker() as db:
            protections = (
                (
                    await db.execute(
                        select(ProtectedMedia).where(
                            ProtectedMedia.series_id == series_id,
                            ProtectedMedia.source == "rule",
                        )
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(protections), 1)

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
            movie.tmdb_collection_id = 77
            movie.tmdb_collection_name = "Preview Collection"
            movie.tmdb_collection_checked = True
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
            self.assertEqual(preview_items[0].tmdb_collection_id, 77)
            self.assertEqual(
                preview_items[0].tmdb_collection_name, "Preview Collection"
            )
            self.assertTrue(preview_items[0].tmdb_in_collection)
            self.assertTrue(preview_items[0].reason_tokens)

    async def test_collect_rule_preview_matches_arr_tags_legacy_not_equals(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_single_condition_rule(
                name="not-garbage-series",
                media_type=MediaType.SERIES,
                target_scope="series",
                field="arr.tags",
                operator="not_equals",
                value="garbage",
            )
            untagged = Series(
                title="Untagged Preview Series",
                tmdb_id=3101,
                size=20 * 1024**3,
                arr_tags=[],
            )
            other_tag = Series(
                title="Other Tag Preview Series",
                tmdb_id=3102,
                size=20 * 1024**3,
                arr_tags=["rec-low-rated-barely-watched-ser"],
            )
            missing_size = Series(
                title="Missing Size Preview Series",
                tmdb_id=3104,
                arr_tags=[],
            )
            garbage = Series(
                title="Garbage Preview Series",
                tmdb_id=3103,
                size=20 * 1024**3,
                arr_tags=["garbage"],
            )
            db.add_all([rule, untagged, other_tag, missing_size, garbage])
            await db.flush()

            matches = await collect_rule_preview_matches(db, [rule])

            matched_series_ids = {match.series_id for match in matches}
            self.assertEqual(
                matched_series_ids, {untagged.id, other_tag.id, missing_size.id}
            )

    async def test_collect_rule_preview_matches_metadata_reports_exclusions(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            settings = GeneralSettings(
                favorites_ignore_enabled=True,
                favorites_protect_all_users=True,
                favorites_usernames=["mason"],
            )
            user = User(username="admin", password_hash="x")
            plex_config = ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex-a",
                api_key="x",
                name="plex-a",
                enabled=True,
            )
            rule = _make_single_condition_rule(
                name="not-garbage-series",
                media_type=MediaType.SERIES,
                target_scope="series",
                field="arr.tags",
                operator="not_contains_any",
                value=["garbage"],
            )
            matched = Series(title="Matched Series", tmdb_id=3201, size=20 * 1024**3)
            favorite = Series(title="Favorite Series", tmdb_id=3202, size=20 * 1024**3)
            protected = Series(
                title="Protected Series", tmdb_id=3203, size=20 * 1024**3
            )
            garbage = Series(
                title="Garbage Series",
                tmdb_id=3204,
                size=20 * 1024**3,
                arr_tags=["garbage"],
            )
            db.add_all(
                [
                    settings,
                    user,
                    plex_config,
                    rule,
                    matched,
                    favorite,
                    protected,
                    garbage,
                ]
            )
            await db.flush()
            db.add(
                MediaFavorite(
                    media_type=MediaType.SERIES,
                    tmdb_id=favorite.tmdb_id,
                    username="mason",
                    username_normalized="mason",
                    source_service=Service.PLEX,
                    source_service_config_id=plex_config.id,
                )
            )
            db.add(
                ProtectedMedia(
                    media_type=MediaType.SERIES,
                    series_id=protected.id,
                    protected_by_user_id=user.id,
                    reason="protected",
                )
            )
            await db.commit()

            with patch.object(
                cleanup_tasks,
                "_ensure_favorites_snapshot_if_enabled",
                new=AsyncMock(return_value=(True, None)),
            ):
                result = await collect_rule_preview_matches_with_metadata(db, [rule])

            self.assertEqual(len(result.matches), 1)
            self.assertEqual(result.matches[0].series_id, matched.id)
            self.assertEqual(result.metadata.source_media_count, 4)
            self.assertEqual(result.metadata.skipped_favorites_count, 1)
            self.assertEqual(result.metadata.skipped_protected_count, 1)
            self.assertEqual(result.metadata.matched_count, 1)

    async def test_refresh_arr_tags_for_rules_uses_sonarr_series_and_tag_catalog(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_single_condition_rule(
                name="sonarr-tag-rule",
                media_type=MediaType.SERIES,
                target_scope="series",
                field="arr.tags",
                operator="contains_any",
                value=["garbage"],
            )
            sonarr_config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-a",
                api_key="x",
                name="sonarr-a",
                enabled=True,
            )
            tagged_series = Series(
                title="Tagged Series",
                tmdb_id=90001,
                arr_tags=["keep", "garbage"],
            )
            stale_series = Series(
                title="Stale Series",
                tmdb_id=90002,
                arr_tags=["keep2", "garbage"],
            )
            db.add_all([rule, sonarr_config, tagged_series, stale_series])
            await db.flush()
            db.add_all(
                [
                    SeriesArrRef(
                        series_id=tagged_series.id,
                        service_config_id=sonarr_config.id,
                        arr_series_id=1001,
                        tmdb_id=tagged_series.tmdb_id,
                    ),
                    SeriesArrRef(
                        series_id=stale_series.id,
                        service_config_id=sonarr_config.id,
                        arr_series_id=1002,
                        tmdb_id=stale_series.tmdb_id,
                    ),
                ]
            )
            await db.commit()
            config_id = sonarr_config.id
            tagged_series_id = tagged_series.id
            stale_series_id = stale_series.id

        fake_client = _SonarrTagRefreshClientFake(
            series=[
                SimpleNamespace(id=1001, tags=[1]),
                SimpleNamespace(id=1002, tags=[]),
            ],
            tags=[SimpleNamespace(id=1, label="garbage")],
        )
        previous_sonarr_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {config_id: fake_client}  # pyright: ignore[reportAttributeAccessIssue]
        cleanup_tasks.service_manager._sonarr = None
        try:
            with patch.object(cleanup_tasks, "async_db", self._sessionmaker):
                await cleanup_tasks._refresh_arr_tags_for_rules([rule])
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_sonarr_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        async with self._sessionmaker() as db:
            refreshed_tagged = (
                await db.execute(select(Series).where(Series.id == tagged_series_id))
            ).scalar_one()
            refreshed_stale = (
                await db.execute(select(Series).where(Series.id == stale_series_id))
            ).scalar_one()

            self.assertEqual(refreshed_tagged.arr_tags, ["garbage", "keep"])
            self.assertEqual(refreshed_stale.arr_tags, ["keep2"])

    async def test_refresh_arr_tags_for_rules_preserves_failed_sonarr_config_rows(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_single_condition_rule(
                name="sonarr-tag-rule-failure",
                media_type=MediaType.SERIES,
                target_scope="series",
                field="arr.tags",
                operator="contains_any",
                value=["garbage"],
            )
            good_config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-good",
                api_key="x",
                name="sonarr-good",
                enabled=True,
            )
            bad_config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="http://sonarr-bad",
                api_key="x",
                name="sonarr-bad",
                enabled=True,
            )
            good_series = Series(
                title="Good Config Series",
                tmdb_id=91001,
                arr_tags=["keep", "garbage"],
            )
            bad_series = Series(
                title="Bad Config Series",
                tmdb_id=91002,
                arr_tags=["keepbad", "garbage"],
            )
            db.add_all([rule, good_config, bad_config, good_series, bad_series])
            await db.flush()
            db.add_all(
                [
                    SeriesArrRef(
                        series_id=good_series.id,
                        service_config_id=good_config.id,
                        arr_series_id=2001,
                        tmdb_id=good_series.tmdb_id,
                    ),
                    SeriesArrRef(
                        series_id=bad_series.id,
                        service_config_id=bad_config.id,
                        arr_series_id=3001,
                        tmdb_id=bad_series.tmdb_id,
                    ),
                ]
            )
            await db.commit()
            good_config_id = good_config.id
            bad_config_id = bad_config.id
            good_series_id = good_series.id
            bad_series_id = bad_series.id

        good_client = _SonarrTagRefreshClientFake(
            series=[SimpleNamespace(id=2001, tags=[])],
            tags=[SimpleNamespace(id=1, label="garbage")],
        )
        bad_client = _SonarrTagRefreshClientFake(fail=True)
        previous_sonarr_clients = cleanup_tasks.service_manager._sonarr_clients
        previous_sonarr = cleanup_tasks.service_manager._sonarr
        cleanup_tasks.service_manager._sonarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            good_config_id: good_client,
            bad_config_id: bad_client,
        }
        cleanup_tasks.service_manager._sonarr = None
        try:
            with patch.object(cleanup_tasks, "async_db", self._sessionmaker):
                await cleanup_tasks._refresh_arr_tags_for_rules([rule])
        finally:
            cleanup_tasks.service_manager._sonarr_clients = previous_sonarr_clients
            cleanup_tasks.service_manager._sonarr = previous_sonarr

        async with self._sessionmaker() as db:
            refreshed_good = (
                await db.execute(select(Series).where(Series.id == good_series_id))
            ).scalar_one()
            unchanged_bad = (
                await db.execute(select(Series).where(Series.id == bad_series_id))
            ).scalar_one()

            self.assertEqual(refreshed_good.arr_tags, ["keep"])
            self.assertEqual(unchanged_bad.arr_tags, ["keepbad", "garbage"])

    async def test_refresh_arr_tags_for_rules_preserves_failed_radarr_config_rows(
        self,
    ) -> None:
        async with self._sessionmaker() as db:
            rule = _make_single_condition_rule(
                name="radarr-tag-rule-failure",
                media_type=MediaType.MOVIE,
                target_scope="movie_version",
                field="arr.tags",
                operator="contains_any",
                value=["garbage"],
            )
            good_config = ServiceConfig(
                service_type=Service.RADARR,
                base_url="http://radarr-good",
                api_key="x",
                name="radarr-good",
                enabled=True,
            )
            bad_config = ServiceConfig(
                service_type=Service.RADARR,
                base_url="http://radarr-bad",
                api_key="x",
                name="radarr-bad",
                enabled=True,
            )
            good_movie = Movie(
                title="Good Config Movie",
                tmdb_id=92001,
                arr_tags=["keep", "garbage"],
            )
            bad_movie = Movie(
                title="Bad Config Movie",
                tmdb_id=92002,
                arr_tags=["keepbad", "garbage"],
            )
            db.add_all([rule, good_config, bad_config, good_movie, bad_movie])
            await db.flush()
            db.add_all(
                [
                    MovieArrRef(
                        movie_id=good_movie.id,
                        service_config_id=good_config.id,
                        arr_movie_id=4001,
                        tmdb_id=good_movie.tmdb_id,
                    ),
                    MovieArrRef(
                        movie_id=bad_movie.id,
                        service_config_id=bad_config.id,
                        arr_movie_id=5001,
                        tmdb_id=bad_movie.tmdb_id,
                    ),
                ]
            )
            await db.commit()
            good_config_id = good_config.id
            bad_config_id = bad_config.id
            good_movie_id = good_movie.id
            bad_movie_id = bad_movie.id

        good_client = _RadarrTagRefreshClientFake(
            movies=[SimpleNamespace(id=4001, tags=[])],
            tags=[SimpleNamespace(id=1, label="garbage")],
        )
        bad_client = _RadarrTagRefreshClientFake(fail=True)
        previous_radarr_clients = cleanup_tasks.service_manager._radarr_clients
        previous_radarr = cleanup_tasks.service_manager._radarr
        cleanup_tasks.service_manager._radarr_clients = {  # pyright: ignore[reportAttributeAccessIssue]
            good_config_id: good_client,
            bad_config_id: bad_client,
        }
        cleanup_tasks.service_manager._radarr = None
        try:
            with patch.object(cleanup_tasks, "async_db", self._sessionmaker):
                await cleanup_tasks._refresh_arr_tags_for_rules([rule])
        finally:
            cleanup_tasks.service_manager._radarr_clients = previous_radarr_clients
            cleanup_tasks.service_manager._radarr = previous_radarr

        async with self._sessionmaker() as db:
            refreshed_good = (
                await db.execute(select(Movie).where(Movie.id == good_movie_id))
            ).scalar_one()
            unchanged_bad = (
                await db.execute(select(Movie).where(Movie.id == bad_movie_id))
            ).scalar_one()

            self.assertEqual(refreshed_good.arr_tags, ["keep"])
            self.assertEqual(unchanged_bad.arr_tags, ["keepbad", "garbage"])

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
