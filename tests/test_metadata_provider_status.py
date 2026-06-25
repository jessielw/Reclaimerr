from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings.services import _metadata_provider_status_payload
from backend.database import Base
from backend.database.models import (
    ExternalRatingsIngestState,
    Movie,
    Series,
    ServiceConfig,
)
from backend.enums import Service
from backend.tasks.external_ratings import _request_delay_seconds


@pytest.mark.anyio
async def test_metadata_provider_status_reports_refresh_usage_and_coverage() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        db.add_all(
            [
                ServiceConfig(
                    service_type=Service.MDBLIST,
                    name="MDBList",
                    base_url="https://api.mdblist.com",
                    api_key="encrypted",
                    enabled=True,
                    is_main=False,
                    extra_settings={
                        "request_limit": 333,
                        "supporter_mode": True,
                        "request_delay_seconds": 0.2,
                    },
                ),
                ServiceConfig(
                    service_type=Service.OMDB,
                    name="OMDb",
                    base_url="https://www.omdbapi.com",
                    api_key="encrypted",
                    enabled=True,
                    is_main=False,
                    extra_settings=None,
                ),
                ExternalRatingsIngestState(
                    provider_summary={
                        "mdblist": {
                            "requests_used": 12,
                            "request_limit": 333,
                            "disabled_reason": None,
                        },
                        "omdb": {
                            "requests_used": 2,
                            "request_limit": 500,
                            "disabled_reason": "OMDb disabled for test",
                        },
                    },
                    last_checked_at=datetime(2026, 6, 24, 12, tzinfo=UTC),
                    last_successful_refresh_at=datetime(2026, 6, 24, 12, tzinfo=UTC),
                ),
                Movie(
                    title="MDBList Movie",
                    tmdb_id=1,
                    external_ratings_source="mdblist",
                ),
                Movie(
                    title="Combined Movie",
                    tmdb_id=2,
                    external_ratings_source="mdblist+omdb",
                ),
                Movie(
                    title="OMDb Movie",
                    tmdb_id=3,
                    external_ratings_source="omdb",
                ),
                Movie(title="Uncovered Movie", tmdb_id=4),
                Series(
                    title="Combined Series",
                    tmdb_id=5,
                    external_ratings_source="mdblist+omdb",
                ),
                Series(
                    title="OMDb Series",
                    tmdb_id=6,
                    external_ratings_source="omdb",
                ),
            ]
        )
        removed_movie = Movie(
            title="Removed Movie",
            tmdb_id=7,
            external_ratings_source="mdblist",
        )
        removed_movie.removed_at = datetime.now(UTC)
        db.add(removed_movie)
        await db.commit()

        response = await _metadata_provider_status_payload(db)

    await engine.dispose()

    providers = {item["service_type"]: item for item in response["providers"]}
    mdblist = providers["mdblist"]
    omdb = providers["omdb"]

    assert mdblist["configured"] is True
    assert mdblist["enabled"] is True
    assert mdblist["request_limit"] == 333
    assert mdblist["request_delay_seconds"] == 0.2
    assert mdblist["last_run_requests"] == 12
    assert mdblist["last_run_request_limit"] == 333
    assert mdblist["coverage"]["movies"] == {
        "covered": 2,
        "total": 4,
        "percent": 50.0,
    }
    assert mdblist["coverage"]["series"] == {
        "covered": 1,
        "total": 2,
        "percent": 50.0,
    }
    assert mdblist["coverage"]["total"] == {
        "covered": 3,
        "total": 6,
        "percent": 50.0,
    }

    assert omdb["request_limit"] == 500
    assert omdb["request_delay_seconds"] == 0.0
    assert omdb["disabled_reason"] == "OMDb disabled for test"
    assert omdb["coverage"]["total"] == {
        "covered": 4,
        "total": 6,
        "percent": 66.7,
    }


@pytest.mark.anyio
async def test_metadata_provider_status_returns_defaults_without_configuration() -> (
    None
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as db:
        response = await _metadata_provider_status_payload(db)

    await engine.dispose()

    providers = {item["service_type"]: item for item in response["providers"]}
    assert providers["mdblist"]["configured"] is False
    assert providers["mdblist"]["enabled"] is False
    assert providers["mdblist"]["request_limit"] == 250
    assert providers["mdblist"]["request_delay_seconds"] == 1.0
    assert providers["mdblist"]["coverage"]["total"] == {
        "covered": 0,
        "total": 0,
        "percent": 0,
    }
    assert providers["omdb"]["request_limit"] == 500
    assert providers["omdb"]["request_delay_seconds"] == 0.0


def test_mdblist_request_delay_defaults_and_supporter_mode() -> None:
    standard = ServiceConfig(
        service_type=Service.MDBLIST,
        name="MDBList",
        base_url="https://api.mdblist.com",
        api_key="encrypted",
        enabled=True,
        is_main=False,
        extra_settings=None,
    )
    supporter = ServiceConfig(
        service_type=Service.MDBLIST,
        name="MDBList",
        base_url="https://api.mdblist.com",
        api_key="encrypted",
        enabled=True,
        is_main=False,
        extra_settings={"supporter_mode": True},
    )
    custom = ServiceConfig(
        service_type=Service.MDBLIST,
        name="MDBList",
        base_url="https://api.mdblist.com",
        api_key="encrypted",
        enabled=True,
        is_main=False,
        extra_settings={"request_delay_seconds": 1.7},
    )

    assert _request_delay_seconds(standard) == 1.0
    assert _request_delay_seconds(supporter) == 0.2
    assert _request_delay_seconds(custom) == 1.7
