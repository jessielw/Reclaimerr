from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
async def test_get_libraries_uses_id_when_itemid_missing(service_class: type[EmbyService | JellyfinService]) -> None:
    """Virtual folders may carry only `Id`, only `ItemId`, or both."""
    client = service_class(api_key="key", base_url="http://media-server")

    async def _fake_request(endpoint: str, **kwargs: Any) -> list[dict[str, Any]]:
        if endpoint != "Library/VirtualFolders":
            raise AssertionError(f"Unexpected endpoint: {endpoint}")
        return [
            # Emby/Jellyfin may return either `Id` or `ItemId` for the folder ID.
            {"Name": "IdOnly", "Id": "id-1", "CollectionType": "movies"},
            {"Name": "ItemIdOnly", "ItemId": "itemid-2", "CollectionType": "movies"},
            {"Name": "Both", "ItemId": "itemid-3", "Id": "id-3", "CollectionType": "movies"},
            {"Name": "TV", "Id": "id-4", "CollectionType": "tvshows"},
            # Mixed/unknown collection types should be ignored.
            {"Name": "Music", "Id": "id-5", "CollectionType": "music"},
        ]

    try:
        with patch.object(client, "_make_request", side_effect=_fake_request):
            movie_libs = await client.get_movie_libraries()
            series_libs = await client.get_series_libraries()
    finally:
        await client.session.close()

    assert movie_libs == [
        {"id": "id-1", "name": "IdOnly"},
        {"id": "itemid-2", "name": "ItemIdOnly"},
        {"id": "itemid-3", "name": "Both"},
    ]
    assert series_libs == [{"id": "id-4", "name": "TV"}]
