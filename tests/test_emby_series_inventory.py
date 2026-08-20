from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService


class EmbyFamilySeriesInventoryTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_genres_support_both_emby_family_shapes(self) -> None:
        self.assertEqual(
            JellyfinService._genre_names(
                {"Genres": ["Anime", " anime ", "Animation"]}
            ),
            ["Animation", "Anime"],
        )
        self.assertEqual(
            EmbyService._genre_names(
                {"GenreItems": [{"Name": "Drama"}, {"Name": "Mystery"}]}
            ),
            ["Drama", "Mystery"],
        )

    async def test_movie_and_series_queries_request_genres(self) -> None:
        for service_class in (JellyfinService, EmbyService):
            with self.subTest(service=service_class.__name__):
                client = service_class(api_key="key", base_url="http://media-server")
                fields: list[str] = []

                async def request(
                    endpoint: str, *, params: dict[str, Any], timeout: int
                ) -> dict[str, Any]:
                    self.assertEqual(endpoint, "Items")
                    fields.append(str(params.get("Fields") or ""))
                    return {"Items": [], "TotalRecordCount": 0}

                try:
                    with patch.object(
                        service_class, "_make_request", side_effect=request
                    ):
                        await client.get_movies_for_user(
                            "user", "movies", "Movies"
                        )
                        await client.get_series_for_user("user", "shows", "Shows")
                finally:
                    await client.session.close()

                self.assertEqual(len(fields), 2)
                self.assertTrue(all("Genres" in value for value in fields))

    async def test_user_scoped_lookup_uses_items_route_for_jellyfin_v12(self) -> None:
        client = JellyfinService(api_key="key", base_url="http://jellyfin")
        calls: list[tuple[str, dict[str, Any]]] = []

        async def request(
            endpoint: str, *, params: dict[str, Any] | None = None, timeout: int = 300
        ) -> dict[str, Any] | list[dict[str, str]]:
            params = params or {}
            calls.append((endpoint, params))
            if endpoint == "Users":
                return [{"Id": "user-1", "Name": "Alice"}]
            if endpoint == "Items" and params.get("IsFavorite") == "true":
                return {
                    "Items": [{"ProviderIds": {"Tmdb": "123"}}],
                    "TotalRecordCount": 1,
                }
            if endpoint == "Items":
                return {
                    "Items": [
                        {
                            "Id": "episode-1",
                            "SeriesId": "series-1",
                            "SeasonId": "season-1",
                        }
                    ]
                }
            raise AssertionError(f"Unexpected endpoint: {endpoint}")

        try:
            with patch.object(JellyfinService, "_make_request", side_effect=request):
                favorites = await client.get_favorite_tmdb_ids_by_user("movie")
                parents = await client.get_parent_ids_for_episode_ids(["episode-1"])
        finally:
            await client.session.close()

        self.assertEqual(favorites, {"Alice": {123}})
        self.assertEqual(parents, {"episode-1": ("series-1", "season-1")})
        item_calls = [call for call in calls if call[0] == "Items"]
        self.assertEqual(len(item_calls), 2)
        self.assertTrue(all(call[1].get("userId") == "user-1" for call in item_calls))

    async def test_inventory_only_aggregates_physical_path_backed_episodes(
        self,
    ) -> None:
        for service_class in (JellyfinService, EmbyService):
            with self.subTest(service=service_class.__name__):
                client = service_class(api_key="key", base_url="http://media-server")
                seen_params: dict[str, Any] = {}

                async def request(
                    endpoint: str, *, params: dict[str, Any], timeout: int
                ) -> dict[str, Any]:
                    self.assertEqual(endpoint, "Items")
                    self.assertEqual(timeout, 60)
                    seen_params.update(params)
                    return {
                        "Items": [
                            {
                                "Id": "physical-sized",
                                "SeriesId": "series-1",
                                "SeasonId": "season-1",
                                "ParentIndexNumber": 1,
                                "IndexNumber": 1,
                                "MediaSources": [
                                    {
                                        "Path": "/media/Show/Season 01/S01E01.mkv",
                                        "Size": 100,
                                    }
                                ],
                                "UserData": {},
                            },
                            {
                                "Id": "physical-unknown-size",
                                "SeriesId": "series-1",
                                "SeasonId": "season-1",
                                "ParentIndexNumber": 1,
                                "IndexNumber": 2,
                                "MediaSources": [
                                    {"Path": "/media/Show/Season 01/S01E02.mkv"}
                                ],
                                "UserData": {},
                            },
                            {
                                "Id": "virtual-location",
                                "SeriesId": "series-1",
                                "ParentIndexNumber": 1,
                                "IndexNumber": 3,
                                "LocationType": "Virtual",
                                "MediaSources": [
                                    {
                                        "Path": "/virtual/S01E03.mkv",
                                        "Size": 1000,
                                    }
                                ],
                                "UserData": {},
                            },
                            {
                                "Id": "virtual-flag",
                                "SeriesId": "series-1",
                                "ParentIndexNumber": 1,
                                "IndexNumber": 4,
                                "IsVirtualItem": True,
                                "MediaSources": [
                                    {
                                        "Path": "/virtual/S01E04.mkv",
                                        "Size": 1000,
                                    }
                                ],
                                "UserData": {},
                            },
                            {
                                "Id": "pathless",
                                "SeriesId": "series-1",
                                "ParentIndexNumber": 1,
                                "IndexNumber": 5,
                                "MediaSources": [{"Size": 1000}],
                                "UserData": {},
                            },
                        ],
                        "TotalRecordCount": 5,
                    }

                try:
                    with patch.object(
                        service_class, "_make_request", side_effect=request
                    ):
                        (
                            series_sizes,
                            season_data,
                        ) = await client.get_series_sizes_for_library("library", "user")
                finally:
                    await client.session.close()

                self.assertEqual(seen_params["ExcludeLocationTypes"], "Virtual")
                self.assertEqual(seen_params["enableTotalRecordCount"], "true")
                self.assertIn("LocationType", str(seen_params["Fields"]))
                self.assertIn("IsVirtualItem", str(seen_params["Fields"]))
                self.assertEqual(series_sizes, {"series-1": 100})
                season = season_data[("series-1", 1)]
                self.assertEqual(season.size, 100)
                self.assertEqual(season.episode_count, 2)
                self.assertEqual(
                    season.episode_paths,
                    [
                        "/media/Show/Season 01/S01E01.mkv",
                        "/media/Show/Season 01/S01E02.mkv",
                    ],
                )
                self.assertEqual(
                    [episode.episode_number for episode in season.episode_data],
                    [1, 2],
                )
                self.assertEqual(
                    [episode.size for episode in season.episode_data],
                    [100, None],
                )

    async def test_malformed_inventory_response_fails_instead_of_looking_empty(
        self,
    ) -> None:
        client = JellyfinService(api_key="key", base_url="http://jellyfin")
        try:
            with patch.object(
                JellyfinService,
                "_make_request",
                return_value={"Items": "not-a-list"},
            ):
                with self.assertRaisesRegex(RuntimeError, "item list"):
                    await client.get_series_sizes_for_library("library", "user")
        finally:
            await client.session.close()
