from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from backend.enums import MediaType
from backend.models.media import MediaWatchSnapshot
from backend.services.plex import (
    PlexService,
    _AnimeListIDs,
    _history_record_rating_key,
)


def test_external_id_parser_supports_modern_and_legacy_plex_agents() -> None:
    cases = [
        ({"Guid": [{"id": "tmdb://123"}]}, {"tmdb": 123}),
        (
            {"guid": "com.plexapp.agents.themoviedb://456?lang=en"},
            {"tmdb": 456},
        ),
        (
            {"guid": "com.plexapp.agents.tmdb://789?lang=en"},
            {"tmdb": 789},
        ),
        (
            {"guid": "com.plexapp.agents.thetvdb://321?lang=en"},
            {"tvdb": "321"},
        ),
        (
            {"guid": "com.plexapp.agents.imdb://tt0123456?lang=en"},
            {"imdb": "tt0123456"},
        ),
    ]

    for media, expected in cases:
        parsed = PlexService._parse_external_id_candidates(media)
        for field, value in expected.items():
            assert getattr(parsed, field) == value


def test_external_id_parser_supports_hama_guid_modes() -> None:
    cases = [
        ("anidb-11638", {"anidb": "11638"}),
        ("anidb4-11638", {"anidb": "11638"}),
        ("tvdb-305074", {"tvdb": "305074"}),
        ("tvdb4-305074", {"tvdb": "305074"}),
        ("tmdb-372058", {"tmdb": 372058}),
        ("tsdb-65930", {"tmdb": 65930}),
        ("imdb-tt0988824", {"imdb": "tt0988824"}),
    ]

    for hama_id, expected in cases:
        parsed = PlexService._parse_external_id_candidates(
            {"guid": f"com.plexapp.agents.hama://{hama_id}?lang=en"}
        )
        for field, value in expected.items():
            assert getattr(parsed, field) == value


def test_external_id_parser_always_checks_top_level_legacy_guid() -> None:
    parsed = PlexService._parse_external_id_candidates(
        {
            "Guid": [{"id": "plex://show/5d9c086c7d06d9001ffd27aa"}],
            "guid": "com.plexapp.agents.hama://anidb-11638?lang=en",
        }
    )

    assert parsed.anidb == "11638"


def test_parse_anidb_mappings_extracts_movie_and_series_ids() -> None:
    mappings = PlexService._parse_anidb_mappings(
        b"""<?xml version=\"1.0\" encoding=\"utf-8\"?>
        <anime-list>
          <anime anidbid=\"1\" tvdbid=\"72025\" tmdbtv=\"26209\" />
          <anime anidbid=\"7\" tvdbid=\"movie\" imdbid=\"tt0119698\" />
          <anime anidbid=\"11\" tvdbid=\"70900\" tmdbid=\"1390599\"
                 imdbid=\"tt7941838\" />
        </anime-list>"""
    )

    assert mappings["1"] == _AnimeListIDs(tmdb_series=26209, tvdb="72025")
    assert mappings["7"] == _AnimeListIDs(imdb="tt0119698")
    assert mappings["11"] == _AnimeListIDs(
        tmdb_movie=1390599,
        imdb="tt7941838",
        tvdb="70900",
    )


def test_hama_anidb_guid_resolves_to_tmdb_without_per_item_api_call(
    monkeypatch,
) -> None:
    async def run() -> None:
        async def fake_load_mappings(
            self: PlexService,
        ) -> dict[str, _AnimeListIDs]:
            return {
                "11638": _AnimeListIDs(
                    tmdb_series=65930,
                    imdb="tt0988824",
                    tvdb="305074",
                )
            }

        monkeypatch.setattr(PlexService, "_load_anidb_mappings", fake_load_mappings)
        service = PlexService("token", "http://plex.local")

        resolved = await service._resolve_external_ids(
            {"guid": ("com.plexapp.agents.hama://anidb-11638?lang=en")},
            MediaType.SERIES,
        )

        assert resolved is not None
        assert resolved.tmdb == 65930
        assert resolved.imdb == "tt0988824"
        assert resolved.tvdb == "305074"

    asyncio.run(run())


def test_get_series_keeps_hama_items_in_scan_results(monkeypatch) -> None:
    async def run() -> None:
        async def fake_get_sections(self: PlexService) -> list[dict[str, object]]:
            return [
                {
                    "key": "2",
                    "uuid": "anime-library-uuid",
                    "title": "Anime",
                    "type": "show",
                }
            ]

        async def fake_get_collections(
            self: PlexService, section_id: str
        ) -> dict[str, list[str]]:
            assert section_id == "2"
            return {}

        async def fake_get_episode_data(
            self: PlexService, section_id: str
        ) -> tuple[dict[str, int], dict[str, str], dict[object, object]]:
            assert section_id == "2"
            return {"99": 1234}, {"99": "/anime/Example"}, {}

        async def fake_get_items(
            self: PlexService,
            section_id: str,
            **kwargs: object,
        ) -> list[dict[str, object]]:
            assert section_id == "2"
            return [
                {
                    "type": "show",
                    "ratingKey": "99",
                    "title": "Example Anime",
                    "year": 2024,
                    "guid": "com.plexapp.agents.hama://anidb-11638?lang=en",
                    "Genre": [{"tag": "Anime"}, {"tag": "Animation"}],
                }
            ]

        async def fake_load_mappings(
            self: PlexService,
        ) -> dict[str, _AnimeListIDs]:
            return {
                "11638": _AnimeListIDs(
                    tmdb_series=65930,
                    imdb="tt0988824",
                    tvdb="305074",
                )
            }

        monkeypatch.setattr(PlexService, "get_library_sections", fake_get_sections)
        monkeypatch.setattr(
            PlexService, "_get_collection_names_by_item_id", fake_get_collections
        )
        monkeypatch.setattr(
            PlexService, "_get_episode_data_for_section", fake_get_episode_data
        )
        monkeypatch.setattr(PlexService, "_get_section_metadata_items", fake_get_items)
        monkeypatch.setattr(PlexService, "_load_anidb_mappings", fake_load_mappings)

        series = await PlexService("token", "http://plex.local").get_series()

        assert len(series) == 1
        assert series[0].name == "Example Anime"
        assert series[0].external_ids.tmdb == 65930
        assert series[0].size == 1234
        assert series[0].media_server_genres == ["Animation", "Anime"]

    asyncio.run(run())


def test_get_section_metadata_items_paginates_until_total_size(monkeypatch) -> None:
    async def run() -> None:
        calls: list[dict[str, Any]] = []
        pages = [
            {
                "MediaContainer": {
                    "size": 2,
                    "totalSize": 5,
                    "Metadata": [{"ratingKey": "1"}, {"ratingKey": "2"}],
                }
            },
            {
                "MediaContainer": {
                    "size": 2,
                    "totalSize": 5,
                    "Metadata": [{"ratingKey": "3"}, {"ratingKey": "4"}],
                }
            },
            {
                "MediaContainer": {
                    "size": 1,
                    "totalSize": 5,
                    "Metadata": [{"ratingKey": "5"}],
                }
            },
        ]

        async def fake_make_request(
            self: PlexService,
            endpoint: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> tuple[dict[str, Any], int]:
            assert endpoint == "library/sections/1/all"
            assert kwargs["timeout"] == 123
            assert params is not None
            calls.append(dict(params))
            return pages[len(calls) - 1], 200

        monkeypatch.setattr(PlexService, "_make_request", fake_make_request)
        service = PlexService("token", "http://plex.local")

        items = await service._get_section_metadata_items(
            section_id="1",
            params={"type": 4},
            page_size=2,
            timeout=123,
        )

        assert [item["ratingKey"] for item in items] == ["1", "2", "3", "4", "5"]
        assert calls == [
            {
                "type": 4,
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": 2,
            },
            {
                "type": 4,
                "X-Plex-Container-Start": 2,
                "X-Plex-Container-Size": 2,
            },
            {
                "type": 4,
                "X-Plex-Container-Start": 4,
                "X-Plex-Container-Size": 2,
            },
        ]

    asyncio.run(run())


def test_history_record_rating_key_falls_back_to_metadata_paths() -> None:
    record = {
        "key": "/library/metadata/62906",
        "parentKey": "/library/metadata/62000",
        "grandparentKey": "/library/metadata/61155",
    }

    assert _history_record_rating_key(record, "ratingKey") == "62906"
    assert _history_record_rating_key(record, "parentRatingKey") == "62000"
    assert _history_record_rating_key(record, "grandparentRatingKey") == "61155"


def test_watched_user_snapshots_accept_grandparent_key_path(monkeypatch) -> None:
    async def run() -> None:
        watched_at = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

        async def fake_get_movies(
            self: PlexService, included_libraries: list[str] | None = None
        ) -> list[object]:
            return []

        async def fake_get_series(
            self: PlexService, included_libraries: list[str] | None = None
        ) -> list[object]:
            return [
                SimpleNamespace(
                    id="61155",
                    external_ids=SimpleNamespace(tmdb=12345),
                )
            ]

        async def fake_get_sections(self: PlexService) -> list[dict[str, str]]:
            return [{"key": "2", "title": "TV Shows", "type": "show"}]

        async def fake_get_history(
            self: PlexService, **kwargs: object
        ) -> list[dict[str, object]]:
            return [
                {
                    "type": "episode",
                    "ratingKey": "62906",
                    "grandparentKey": "/library/metadata/61155",
                    "accountID": "490001441",
                    "viewedAt": int(watched_at.timestamp()),
                }
            ]

        async def fake_get_users(self: PlexService) -> dict[str, str]:
            return {"490001441": "alice"}

        monkeypatch.setattr(PlexService, "get_movies", fake_get_movies)
        monkeypatch.setattr(PlexService, "get_series", fake_get_series)
        monkeypatch.setattr(PlexService, "get_library_sections", fake_get_sections)
        monkeypatch.setattr(PlexService, "_get_all_history_records", fake_get_history)
        monkeypatch.setattr(PlexService, "_get_plex_tv_user_map", fake_get_users)

        service = PlexService("token", "http://plex.local")
        (
            snapshots,
            max_viewed_at,
        ) = await service.get_watched_user_snapshots_with_cursor()

        assert snapshots == [
            MediaWatchSnapshot(
                media_type=MediaType.SERIES,
                tmdb_id=12345,
                watch_user_key="alice",
                last_watched_at=watched_at,
                source_item_id="62906",
            )
        ]
        assert max_viewed_at == watched_at

    asyncio.run(run())
