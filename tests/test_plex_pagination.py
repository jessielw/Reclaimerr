from __future__ import annotations

import asyncio
from typing import Any

from backend.services.plex import PlexService


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
