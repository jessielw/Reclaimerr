from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService


class EmbyFamilySeriesInventoryTests(unittest.IsolatedAsyncioTestCase):
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
