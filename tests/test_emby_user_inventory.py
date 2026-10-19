from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from backend.models.services.emby_base import EmbyUserBase
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize(
    "users",
    [
        ["restricted", "alice", "bob"],
        ["bob", "alice", "restricted"],
        ["restricted"],
        [],
    ],
)
def test_inventory_is_unscoped_and_watch_data_includes_every_user(service_class, users):
    async def run():
        client = service_class("key", "http://media.invalid")
        calls = []
        watch_data = {
            "alice": {
                "PlayCount": 2,
                "LastPlayedDate": "2026-10-10T00:00:00+00:00",
                "PersonalRating": 4,
            },
            "bob": {
                "PlayCount": 3,
                "LastPlayedDate": "2026-10-19T00:00:00+00:00",
                "PersonalRating": 8,
            },
        }

        async def request(endpoint, params=None, **kwargs):
            assert endpoint == "Items"
            calls.append(params)
            user = params.get("userId")
            if user == "restricted":
                return {"Items": [], "TotalRecordCount": 0}
            kind = params.get("includeItemTypes", params.get("IncludeItemTypes"))
            data = watch_data.get(user, {})
            if kind == "Series":
                item = {
                    "Id": "show",
                    "Name": "Show",
                    "ProviderIds": {"Tmdb": "123"},
                    "DateCreated": "2026-01-01T00:00:00Z",
                    "UserData": data,
                }
            else:
                item = {
                    "Id": "ep",
                    "SeriesId": "show",
                    "SeasonId": "season",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 1,
                    "UserData": data,
                }
                if "MediaSources" in params["Fields"]:
                    item["MediaSources"] = [
                        {"Id": "file", "Path": "/Show/S01E01.mkv", "Size": 100}
                    ]
            return {"Items": [item], "TotalRecordCount": 1}

        async def watched(user_id):
            data = watch_data.get(user_id)
            return (
                {"show": datetime.fromisoformat(data["LastPlayedDate"])} if data else {}
            )

        try:
            with (
                patch.object(client, "_make_request", side_effect=request),
                patch.object(
                    client,
                    "get_users",
                    AsyncMock(return_value=[EmbyUserBase(id=u, name=u) for u in users]),
                ),
                patch.object(
                    client,
                    "get_series_libraries",
                    AsyncMock(return_value=[{"id": "library", "name": "TV"}]),
                ),
                patch.object(
                    client,
                    "_get_collection_names_by_item_id",
                    AsyncMock(return_value={}),
                ),
                patch.object(
                    client, "get_all_watched_episodes_for_user", side_effect=watched
                ),
            ):
                (show,) = await client.get_aggregated_series()
        finally:
            await client.session.close()
        (season,) = show.season_data
        (episode,) = season.episode_data
        assert show.size == season.size == episode.size == 100
        assert season.episode_count == 1
        assert len(episode.versions) == 1
        assert (
            show.view_count
            == season.view_count
            == episode.view_count
            == (5 if "alice" in users else 0)
        )
        expected_date = (
            datetime.fromisoformat(watch_data["bob"]["LastPlayedDate"])
            if "bob" in users
            else None
        )
        assert (
            show.last_viewed_at
            == season.last_viewed_at
            == episode.last_viewed_at
            == expected_date
        )
        assert (
            episode.media_server_user_rating
            == season.media_server_user_rating
            == (8 if "bob" in users else None)
        )
        physical = [p for p in calls if "MediaSources" in p["Fields"]]
        assert len(physical) == 1
        assert "userId" not in physical[0]
        assert physical[0]["EnableUserData"] == "false"

    asyncio.run(run())


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"Items": "bad"},
        {"Items": [None]},
        {"Items": [{"Id": "ep", "UserData": None}]},
    ],
)
def test_invalid_user_data_aborts_instead_of_resetting_watch_state(
    service_class, payload
):
    async def run():
        client = service_class("key", "http://media.invalid")
        try:
            with patch.object(client, "_make_request", AsyncMock(return_value=payload)):
                with pytest.raises(RuntimeError):
                    await client._get_aggregated_episode_user_data(
                        "library", [EmbyUserBase(id="u", name="User")]
                    )
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
def test_user_data_paginates_and_preserves_missing_dates(service_class):
    async def run():
        client = service_class("key", "http://media.invalid")
        request = AsyncMock(
            side_effect=[
                {
                    "Items": [
                        {"Id": str(i), "UserData": {"PlayCount": 1}} for i in range(500)
                    ],
                    "TotalRecordCount": 501,
                },
                {
                    "Items": [{"Id": "500", "UserData": {"PlayCount": 2}}],
                    "TotalRecordCount": 501,
                },
            ]
        )
        try:
            with patch.object(client, "_make_request", request):
                data = await client._get_aggregated_episode_user_data(
                    "library", [EmbyUserBase(id="u", name="User")]
                )
        finally:
            await client.session.close()
        assert len(data) == 501
        assert data["500"] == {"PlayCount": 2}
        assert [
            call.kwargs["params"]["StartIndex"] for call in request.await_args_list
        ] == [0, 500]

    asyncio.run(run())
