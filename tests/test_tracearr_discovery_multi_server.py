"""Regression tests for Tracearr discovery with more than one media server.

Discovery scored every (Tracearr server x media server config) pair into a
bucket keyed by service *type*, so with two Plex configs the picker listed each
Tracearr Plex server twice - and both configs inherited the same "confirmed"
recommendation, which the binding validator then rejects as a duplicate.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.settings import services
from backend.api.routes.settings.services import _tracearr_discovery_payload
from backend.database import Base
from backend.database.models import Movie, MovieVersion, ServiceConfig
from backend.enums import Service

ADDED_AT = datetime(2026, 8, 20, 12, 0, 0)


def _fake_tracearr_client(recently_added: dict[str, list[dict[str, object]]]):
    """Build a TracearrClient stand-in returning two Plex servers."""

    def _factory(**_: object) -> object:
        return SimpleNamespace(
            health=AsyncMock(return_value=True),
            discover_servers=AsyncMock(
                return_value=[
                    {"id": "srv-house", "name": "House Plex", "server_type": "plex"},
                    {"id": "srv-cabin", "name": "Cabin Plex", "server_type": "plex"},
                ]
            ),
            get_recently_added=AsyncMock(
                side_effect=lambda server_id, **_kw: recently_added.get(server_id, [])
            ),
            session=SimpleNamespace(close=AsyncMock()),
        )

    return _factory


async def _seed(db: AsyncSession) -> None:
    db.add_all(
        [
            ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex-house",
                api_key="k1",
                name="Plex House",
                enabled=True,
                is_main=True,
            ),
            ServiceConfig(
                service_type=Service.PLEX,
                base_url="http://plex-cabin",
                api_key="k2",
                name="Plex Cabin",
                enabled=True,
                is_main=False,
            ),
        ]
    )
    for index, title in enumerate(("Arrival", "Dune", "Heat"), start=1):
        movie = Movie(title=title, tmdb_id=1000 + index)
        db.add(movie)
        await db.flush()
        db.add(
            MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id=f"rk-{index}",
                service_media_id=f"media-{index}",
                library_id="1",
                library_name="Movies",
                added_at=ADDED_AT,
            )
        )
    await db.commit()


def _run_discovery(monkeypatch, recently_added):
    async def run() -> dict:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        monkeypatch.setattr(
            services, "TracearrClient", _fake_tracearr_client(recently_added)
        )
        async with session_maker() as db:
            await _seed(db)
            payload = await _tracearr_discovery_payload(
                db, base_url="http://tracearr", api_key="tk"
            )
        await engine.dispose()
        return payload

    return asyncio.run(run())


def _house_rows() -> list[dict[str, object]]:
    return [
        {"rating_key": "rk-1", "title": "Arrival", "added_at": ADDED_AT.isoformat()},
        {"rating_key": "rk-2", "title": "Dune", "added_at": ADDED_AT.isoformat()},
        {"rating_key": "rk-3", "title": "Heat", "added_at": ADDED_AT.isoformat()},
    ]


def test_each_tracearr_server_is_listed_once_per_media_server(monkeypatch) -> None:
    payload = _run_discovery(monkeypatch, {"srv-house": _house_rows()})

    assert [server["name"] for server in payload["media_servers"]] == [
        "Plex House",
        "Plex Cabin",
    ]
    for media_server in payload["media_servers"]:
        candidate_ids = [candidate["id"] for candidate in media_server["candidates"]]
        assert candidate_ids == ["srv-house", "srv-cabin"]


def test_only_the_main_config_gets_a_recommendation(monkeypatch) -> None:
    payload = _run_discovery(monkeypatch, {"srv-house": _house_rows()})

    house, cabin = payload["media_servers"]
    assert house["recommended_tracearr_server_id"] == "srv-house"
    # the probe compares against version rows only the main server writes, so a
    # non-main config has no evidence of its own and must not inherit main's
    assert cabin["recommended_tracearr_server_id"] is None
    assert all(
        candidate["match"] == "unverified" and candidate["checked"] == 0
        for candidate in cabin["candidates"]
    )


def test_no_recommendation_when_nothing_is_confirmed(monkeypatch) -> None:
    payload = _run_discovery(monkeypatch, {})

    for media_server in payload["media_servers"]:
        assert media_server["recommended_tracearr_server_id"] is None
