from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from backend.core.rule_engine import (
    TARGET_EPISODE,
    TARGET_MOVIE_VERSION,
    TARGET_SEASON,
    TARGET_SERIES,
    _build_context,
    validate_rule_definition,
)
from backend.core.utils.filesystem import paths_equivalent
from backend.database.models import Episode, Movie, MovieVersion, Season, Series
from backend.enums import Service
from backend.services.radarr import build_radarr_movie_from_dict
from backend.services.sonarr import SonarrClient


def test_radarr_movie_includes_file_path_and_utc_added_date() -> None:
    movie = build_radarr_movie_from_dict(
        {
            "id": 12,
            "title": "Example",
            "movieFile": {
                "path": r"D:\Movies\Example\Example.mkv",
                "dateAdded": "2026-06-28T18:30:00-04:00",
            },
        }
    )

    assert movie.file_path == r"D:\Movies\Example\Example.mkv"
    assert movie.file_added_at == datetime(2026, 6, 28, 22, 30)


def test_radarr_movie_ignores_invalid_file_added_date() -> None:
    movie = build_radarr_movie_from_dict(
        {"id": 12, "title": "Example", "movieFile": {"dateAdded": "invalid"}}
    )

    assert movie.file_added_at is None


def test_sonarr_episode_file_dates_filter_and_keep_latest_date() -> None:
    async def run() -> None:
        client = SonarrClient(api_key="key", base_url="http://sonarr")
        request = AsyncMock(
            return_value=(
                200,
                [
                    {
                        "seasonNumber": 2,
                        "episodeNumber": 3,
                        "hasFile": True,
                        "episodeFile": {"dateAdded": "2026-06-20T12:00:00Z"},
                    },
                    {
                        "seasonNumber": 2,
                        "episodeNumber": 3,
                        "hasFile": True,
                        "episodeFile": {"dateAdded": "2026-06-21T12:00:00Z"},
                    },
                    {
                        "seasonNumber": 2,
                        "episodeNumber": 4,
                        "hasFile": False,
                        "episodeFile": {"dateAdded": "2026-06-22T12:00:00Z"},
                    },
                    {
                        "seasonNumber": 2,
                        "episodeNumber": 5,
                        "hasFile": True,
                        "episodeFile": {"dateAdded": "invalid"},
                    },
                ],
            )
        )
        try:
            with patch.object(SonarrClient, "_make_request", request):
                dates = await client.get_episode_file_dates(42)
        finally:
            await client.session.close()

        assert dates == {(2, 3): datetime(2026, 6, 21, 12)}
        request.assert_awaited_once_with(
            "GET",
            "episode",
            params={"seriesId": 42, "includeEpisodeFile": True},
            timeout=60,
        )

    asyncio.run(run())


def test_paths_equivalent_uses_scoped_arr_mapping() -> None:
    mappings = [
        {
            "source_prefix": "/movies",
            "local_prefix": "/data/movies",
            "service_type": "radarr",
            "service_config_id": 4,
        }
    ]

    assert paths_equivalent(
        r"\data\movies\Example\Example.mkv",
        "/movies/Example/Example.mkv",
        mappings,
        left_service_type="jellyfin",
        right_service_type="radarr",
        right_service_config_id=4,
    )
    assert not paths_equivalent(
        "/data/movies/Other/Other.mkv",
        "/movies/Example/Example.mkv",
        mappings,
        right_service_type="radarr",
        right_service_config_id=4,
    )


def test_arr_days_since_file_added_is_valid_for_all_media_scopes() -> None:
    definition = {
        "version": 1,
        "root": {
            "type": "condition",
            "field": "arr.days_since_file_added",
            "operator": "greater_than_or_equal",
            "value": 30,
        },
    }

    for scope in (TARGET_MOVIE_VERSION, TARGET_SERIES, TARGET_SEASON, TARGET_EPISODE):
        validate_rule_definition(definition, target_scope=scope)


def test_rule_context_uses_each_scope_arr_file_added_date() -> None:
    added_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=40)
    movie = Movie(title="Movie", tmdb_id=1)
    version = MovieVersion(
        movie_id=1,
        service=Service.JELLYFIN,
        service_item_id="item",
        service_media_id="media",
        library_id="library",
        library_name="Movies",
        arr_added_at=added_at,
    )
    series = Series(title="Series", tmdb_id=2)
    season = Season(series_id=2, season_number=1)
    episode = Episode(season_id=1, episode_number=1)
    series.arr_added_at = added_at
    season.arr_added_at = added_at
    episode.arr_added_at = added_at

    contexts = (
        _build_context(
            TARGET_MOVIE_VERSION, movie, version, None, None, compute_disk=False
        ),
        _build_context(TARGET_SERIES, None, None, series, None, compute_disk=False),
        _build_context(TARGET_SEASON, None, None, series, season, compute_disk=False),
        _build_context(
            TARGET_EPISODE,
            None,
            None,
            series,
            season,
            episode,
            compute_disk=False,
        ),
    )

    assert all(context["arr.days_since_file_added"] == 40 for context in contexts)
