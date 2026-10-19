from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import (
    Episode,
    ProtectedMedia,
    Season,
    Series,
    ServiceConfig,
)
from backend.enums import MediaType
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.tasks import sync


def item(number):
    return {
        "Id": str(number),
        "SeriesId": "show",
        "SeasonId": "season",
        "ParentIndexNumber": 1,
        "IndexNumber": number,
        "MediaSources": [{"Path": f"/Show/E{number}.mkv", "Size": 100}],
    }


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize(
    "last_page",
    [
        {"Items": [], "TotalRecordCount": 2},
        {"Items": []},
        {"Items": [], "TotalRecordCount": 0},
        {"Items": [item(1)], "TotalRecordCount": 2},
    ],
)
def test_partial_or_repeated_inventory_raises(monkeypatch, service_class, last_page):
    async def run():
        client = service_class("synthetic", "http://media.invalid")
        request = AsyncMock(
            side_effect=[{"Items": [item(1)], "TotalRecordCount": 2}, last_page]
        )
        monkeypatch.setattr(client, "_make_request", request)
        try:
            with pytest.raises(RuntimeError, match="incomplete|repeated"):
                await client.get_series_sizes_for_library("library")
            assert request.await_count == 2
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize(
    "pages,expected_count",
    [
        ([{"Items": [], "TotalRecordCount": 0}], 0),
        ([{"Items": [item(1)]}], 1),
        ([{"Items": [item(1)], "TotalRecordCount": 2}, {"Items": [item(2)]}], 2),
        (
            [
                {"Items": [item(1)], "TotalRecordCount": 2},
                {"Items": [item(2)], "TotalRecordCount": 2},
            ],
            2,
        ),
    ],
)
def test_complete_inventory_and_missing_later_total(
    monkeypatch, service_class, pages, expected_count
):
    async def run():
        client = service_class("synthetic", "http://media.invalid")
        request = AsyncMock(side_effect=pages)
        monkeypatch.setattr(client, "_make_request", request)
        try:
            sizes, seasons = await client.get_series_sizes_for_library("library")
            assert sum(sizes.values()) == expected_count * 100
            assert sum(s.episode_count for s in seasons.values()) == expected_count
            assert [
                c.kwargs["params"]["StartIndex"] for c in request.await_args_list
            ] == [str(n) for n in range(len(pages))]
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("service_class", [EmbyService, JellyfinService])
def test_truncated_sync_preserves_existing_inventory_and_protection(
    monkeypatch, service_class
):
    async def run():
        client = service_class("synthetic", "http://media.invalid")
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(sync, "async_db", factory)
            monkeypatch.setattr(
                sync, "_get_media_service_instance", AsyncMock(return_value=client)
            )
            monkeypatch.setattr(
                client, "_get_collection_names_by_item_id", AsyncMock(return_value={})
            )
            monkeypatch.setattr(
                client,
                "get_series_libraries",
                AsyncMock(return_value=[{"id": "library", "name": "TV"}]),
            )
            monkeypatch.setattr(client, "get_users", AsyncMock(return_value=[]))
            monkeypatch.setattr(
                client,
                "_make_request",
                AsyncMock(
                    side_effect=[
                        {"Items": [item(1)], "TotalRecordCount": 2},
                        {"Items": [], "TotalRecordCount": 2},
                    ]
                ),
            )
            async with factory() as db:
                db.add(
                    ServiceConfig(
                        service_type=client.service_type,
                        base_url="http://media.invalid",
                        api_key="synthetic",
                        name="Main",
                        is_main=True,
                        enabled=True,
                    )
                )
                series = Series(title="Existing", tmdb_id=123, size=200)
                db.add(series)
                await db.flush()
                season = Season(
                    series_id=series.id, season_number=1, episode_count=2, size=200
                )
                db.add(season)
                await db.flush()
                episodes = [
                    Episode(
                        season_id=season.id,
                        episode_number=n,
                        path=f"/Show/E{n}.mkv",
                        size=100,
                    )
                    for n in (1, 2)
                ]
                db.add_all(episodes)
                await db.flush()
                db.add(
                    ProtectedMedia(
                        media_type=MediaType.SERIES,
                        episode_id=episodes[1].id,
                        permanent=True,
                    )
                )
                await db.commit()
            with pytest.raises(RuntimeError, match="incomplete episode inventory"):
                await sync.sync_series()
            async with factory() as db:
                assert len((await db.scalars(select(Episode))).all()) == 2
                assert len((await db.scalars(select(ProtectedMedia))).all()) == 1
                assert (await db.get(Season, season.id)).size == 200
                assert (await db.get(Series, series.id)).removed_at is None
        finally:
            await client.session.close()
            await engine.dispose()

    asyncio.run(run())
