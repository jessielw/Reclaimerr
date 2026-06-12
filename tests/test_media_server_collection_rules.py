from __future__ import annotations

import asyncio
from typing import Any

from backend.services.emby import EmbyService
from backend.services.plex import PlexService


def test_plex_collection_name_mapping_dedupes_children() -> None:
    class TestPlexService(PlexService):
        async def _get_section_metadata_items(
            self,
            *,
            section_id: str,
            params: dict[str, Any] | None = None,
            page_size: int = 1000,
            timeout: int = 300,
        ) -> list[dict[str, Any]]:
            assert section_id == "1"
            assert params == {"type": 18}
            return [
                {"ratingKey": "c1", "title": "Leaving Soon"},
                {"ratingKey": "c2", "title": "leaving soon"},
                {"ratingKey": "c3", "title": "Holiday"},
            ]

        async def _get_collection_child_ids(self, collection_id: str) -> set[str]:
            return {
                "c1": {"m1", "m2"},
                "c2": {"m1"},
                "c3": {"m2"},
            }[collection_id]

    async def run() -> None:
        service = TestPlexService(token="token", plex_url="http://plex")

        try:
            result = await service._get_collection_names_by_item_id("1")
        finally:
            await service.session.close()

        assert result == {
            "m1": ["Leaving Soon"],
            "m2": ["Holiday", "Leaving Soon"],
        }

    asyncio.run(run())


def test_emby_collection_name_mapping_dedupes_children() -> None:
    class TestEmbyService(EmbyService):
        async def _make_request(
            self,
            endpoint: str,
            params: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            assert endpoint == "Items"
            assert params is not None
            if params.get("IncludeItemTypes") == "BoxSet":
                return {
                    "Items": [
                        {"Id": "c1", "Name": "Leaving Soon"},
                        {"Id": "c2", "Name": "leaving soon"},
                        {"Id": "c3", "Name": "Holiday"},
                    ]
                }
            return {
                "Items": {
                    "c1": [{"Id": "m1"}, {"Id": "m2"}],
                    "c2": [{"Id": "m1"}],
                    "c3": [{"Id": "m2"}],
                }[str(params.get("ParentId"))]
            }

    async def run() -> None:
        service = TestEmbyService(api_key="token", base_url="http://emby")

        try:
            result = await service._get_collection_names_by_item_id(
                include_item_types="Movie"
            )
        finally:
            await service.session.close()

        assert result == {
            "m1": ["Leaving Soon"],
            "m2": ["Holiday", "Leaving Soon"],
        }

    asyncio.run(run())
