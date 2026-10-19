from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base
from backend.database.models import Movie, ReclaimCandidate, Series, ServiceConfig
from backend.enums import MediaType, Service
from backend.services.emby import EmbyService
from backend.services.jellyfin import JellyfinService
from backend.services.plex import PlexService
from backend.tasks import sync


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
@pytest.mark.parametrize("count", [1, 20])
@pytest.mark.parametrize("available", [True, False])
def test_empty_inventory_reconciles_only_confirmed_missing_media(
    monkeypatch, media_type, count, available
):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        model = Movie if media_type is MediaType.MOVIE else Series
        foreign_key = "movie_id" if media_type is MediaType.MOVIE else "series_id"
        gather = "gather_movies" if media_type is MediaType.MOVIE else "gather_series"
        execute = (
            sync.sync_movies if media_type is MediaType.MOVIE else sync.sync_series
        )
        monkeypatch.setattr(sync, "async_db", factory)
        monkeypatch.setattr(
            sync,
            gather,
            AsyncMock(
                return_value=(
                    ({} if media_type is MediaType.MOVIE else ({}, {}))
                    if available
                    else None
                )
            ),
        )
        try:
            async with factory() as db:
                db.add(
                    ServiceConfig(
                        service_type=Service.PLEX,
                        name="main",
                        base_url="http://unused",
                        api_key="unused",
                        enabled=True,
                        is_main=True,
                    )
                )
                for index in range(count):
                    media = model(title=f"Title {index}", tmdb_id=index + 1, size=100)
                    db.add(media)
                    await db.flush()
                    db.add(
                        ReclaimCandidate(
                            media_type=media_type,
                            matched_rule_ids=[],
                            matched_criteria={},
                            reason="test",
                            **{foreign_key: media.id},
                        )
                    )
                await db.commit()
            # Resync's explicit suppression must still be respected.
            await execute(allow_soft_delete=False)
            async with factory() as db:
                assert all(
                    m.removed_at is None
                    for m in (await db.scalars(select(model))).all()
                )
            await execute()
            async with factory() as db:
                removed = [
                    m.removed_at is not None
                    for m in (await db.scalars(select(model))).all()
                ]
                assert all(removed) == (available and count == 1)
            await execute()
            async with factory() as db:
                removed = [
                    m.removed_at is not None
                    for m in (await db.scalars(select(model))).all()
                ]
                assert all(removed) == available
                assert len((await db.scalars(select(ReclaimCandidate))).all()) == (
                    0 if available else count
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("client_class", [EmbyService, JellyfinService])
@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"Items": [], "TotalRecordCount": 0}, True),
        ({"Items": [], "TotalRecordCount": 1}, False),
        ({"Items": [{"Id": "unmatched"}], "TotalRecordCount": 1}, False),
        ({"Items": []}, False),
        ({}, False),
        ([], False),
    ],
)
def test_emby_empty_confirmation_uses_unfiltered_server_counts(
    monkeypatch, client_class, media_type, payload, expected
):
    async def run():
        client = client_class("unused", "http://unused")
        request = AsyncMock(return_value=payload)
        monkeypatch.setattr(client, "_make_request", request)
        try:
            assert await client.is_inventory_empty(media_type) is expected
            params = request.call_args.kwargs["params"]
            assert "UserId" not in params
            assert params["IncludeItemTypes"] == (
                "Movie" if media_type is MediaType.MOVIE else "Series"
            )
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "page,expected",
    [
        ({"MediaContainer": {"size": 0, "totalSize": 0}}, True),
        ({"MediaContainer": {"size": 0, "totalSize": 1}}, False),
        ({"MediaContainer": {"size": 1, "totalSize": 0}}, False),
        (
            {"MediaContainer": {"size": 1, "Metadata": [{"ratingKey": "unmatched"}]}},
            False,
        ),
        ({"MediaContainer": {}}, False),
        ({}, False),
    ],
)
def test_plex_empty_confirmation_requires_zero_library_counts(
    monkeypatch, page, expected
):
    async def run():
        client = PlexService("unused", "http://unused")
        monkeypatch.setattr(
            PlexService,
            "_make_request",
            AsyncMock(
                side_effect=[
                    (
                        {
                            "MediaContainer": {
                                "Directory": [{"type": "movie", "key": "1"}]
                            }
                        },
                        {},
                    ),
                    (page, {}),
                ]
            ),
        )
        try:
            assert await client.is_inventory_empty(MediaType.MOVIE) is expected
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"MediaContainer": {"size": 0}}, True),
        ({"MediaContainer": {"Directory": []}}, True),
        ({"MediaContainer": {"size": 1, "Directory": []}}, False),
        ({"MediaContainer": {}}, False),
    ],
)
def test_plex_removed_libraries_are_distinct_from_incomplete_sections(
    monkeypatch, media_type, payload, expected
):
    async def run():
        client = PlexService("unused", "http://unused")
        monkeypatch.setattr(
            PlexService, "_make_request", AsyncMock(return_value=(payload, {}))
        )
        try:
            assert await client.is_inventory_empty(media_type) is expected
        finally:
            await client.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.SERIES])
@pytest.mark.parametrize("confirmed", [True, False])
def test_gather_does_not_confuse_filtered_empty_results_with_empty_inventory(
    monkeypatch, media_type, confirmed
):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine)
        monkeypatch.setattr(sync, "async_db", factory)
        client = AsyncMock()
        client.get_aggregated_movies.return_value = []
        client.get_aggregated_series.return_value = []
        client.is_inventory_empty.return_value = confirmed
        monkeypatch.setattr(
            sync, "_get_media_service_instance", AsyncMock(return_value=client)
        )
        config = ServiceConfig(
            service_type=Service.PLEX,
            name="main",
            base_url="http://unused",
            api_key="unused",
        )
        try:
            gather = (
                sync.gather_movies
                if media_type is MediaType.MOVIE
                else sync.gather_series
            )
            result = await gather(config)
            assert (result is not None) == confirmed
            client.is_inventory_empty.assert_awaited_once_with(media_type)
        finally:
            await engine.dispose()

    asyncio.run(run())
