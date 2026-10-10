from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.service_manager import service_manager
from backend.database import Base
from backend.database.models import ReclaimHistory, Series, ServiceConfig
from backend.enums import MediaType, Service
from backend.models.media import ArrQualityProfile, ReAddRequest
from backend.services import arr_readd
from backend.services.arr_readd import ReAddError, get_readd_options, readd_title
from backend.services.reclaim_stats import load_reclaim_totals


class FakeArr:
    def __init__(self, lookup: Mapping[str, object] | None, exclusion: bool = True):
        self.lookup = lookup
        self.exclusion = exclusion
        self.added: list[dict[str, Any]] = []
        self.exclusions_removed: list[int] = []
        self.lookups: list[int] = []

    async def _lookup(self, arr_id: int) -> Mapping[str, object] | None:
        self.lookups.append(arr_id)
        return self.lookup

    lookup_movie = _lookup
    lookup_series = _lookup

    async def get_root_folders(self) -> list[str]:
        return ["/media/movies"]

    async def get_quality_profiles(self) -> list[ArrQualityProfile]:
        return [ArrQualityProfile(id=4, name="HD-1080p")]

    async def _add(self, lookup: Mapping[str, object], **kwargs: Any) -> None:
        self.added.append({"lookup": lookup, **kwargs})

    add_movie = _add
    add_series = _add

    async def remove_import_exclusion(self, arr_id: int) -> bool:
        self.exclusions_removed.append(arr_id)
        return self.exclusion


def _run(
    monkeypatch: pytest.MonkeyPatch,
    clients: dict[Service, dict[int, FakeArr]],
    body: Any,
) -> Any:
    monkeypatch.setattr(
        service_manager, "radarr_clients", lambda: clients.get(Service.RADARR, {})
    )
    monkeypatch.setattr(
        service_manager, "sonarr_clients", lambda: clients.get(Service.SONARR, {})
    )

    async def run() -> Any:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db:
            result = await body(db)
        await engine.dispose()
        return result

    return asyncio.run(run())


def _deleted_movie(db: AsyncSession) -> ReclaimHistory:
    row = ReclaimHistory(
        approved_by="admin",
        media_type=MediaType.MOVIE,
        tmdb_id=438631,
        name="Dune",
        size=10,
    )
    db.add(row)
    return row


DUNE = {"title": "Dune", "year": 2021, "tmdbId": 438631, "id": 0}
REQUEST = ReAddRequest(
    service_config_id=1, root_folder_path="/media/movies", quality_profile_id=4
)


def test_a_movie_re_add_adds_it_clears_the_exclusion_and_records_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    radarr = FakeArr(DUNE)

    async def body(db: AsyncSession) -> Any:
        row = _deleted_movie(db)
        await db.commit()
        response = await readd_title(db, row, REQUEST, username="admin")
        rows = (
            (await db.execute(select(ReclaimHistory).order_by(ReclaimHistory.id)))
            .scalars()
            .all()
        )
        return response, rows

    response, rows = _run(monkeypatch, {Service.RADARR: {1: radarr}}, body)

    assert response.title == "Dune (2021)"
    assert response.exclusion_removed is True
    assert radarr.added == [
        {
            "lookup": DUNE,
            "root_folder_path": "/media/movies",
            "quality_profile_id": 4,
            "search": True,
        }
    ]
    assert radarr.exclusions_removed == [438631]
    assert [(r.action, r.name, r.size) for r in rows] == [
        ("deleted", "Dune", 10),
        (arr_readd.READD_ACTION, "Dune (2021)", None),
    ]


def test_a_title_already_in_the_arr_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    radarr = FakeArr({**DUNE, "id": 12})

    async def body(db: AsyncSession) -> None:
        row = _deleted_movie(db)
        await db.commit()
        with pytest.raises(ReAddError, match="already in Radarr"):
            await readd_title(db, row, REQUEST, username="admin")

    _run(monkeypatch, {Service.RADARR: {1: radarr}}, body)
    assert radarr.added == []


def test_options_flag_the_instance_that_already_has_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body(db: AsyncSession) -> Any:
        db.add_all(
            [
                ServiceConfig(
                    service_type=Service.RADARR,
                    name="Radarr 4K",
                    base_url="http://r",
                    api_key="k",
                ),
                ServiceConfig(
                    service_type=Service.RADARR,
                    name="Radarr HD",
                    base_url="http://r2",
                    api_key="k",
                ),
            ]
        )
        row = _deleted_movie(db)
        await db.commit()
        return await get_readd_options(db, row)

    options = _run(
        monkeypatch,
        {Service.RADARR: {1: FakeArr({**DUNE, "id": 3}), 2: FakeArr(DUNE)}},
        body,
    )

    assert options.unavailable_reason is None
    assert options.title == "Dune (2021)"
    assert [(i.service_name, i.already_added) for i in options.instances] == [
        ("Radarr 4K", True),
        ("Radarr HD", False),
    ]
    assert options.instances[1].root_folders == ["/media/movies"]
    assert options.instances[1].quality_profiles[0].name == "HD-1080p"


def test_a_series_is_looked_up_by_its_tvdb_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sonarr = FakeArr({"title": "Severance", "year": 2022, "tvdbId": 371980})

    async def body(db: AsyncSession) -> Any:
        db.add(Series(title="Severance", tmdb_id=95396, tvdb_id="371980"))
        row = ReclaimHistory(
            approved_by="admin",
            media_type=MediaType.SERIES,
            tmdb_id=95396,
            name="Severance",
        )
        db.add(row)
        await db.commit()
        return await readd_title(
            db,
            row,
            ReAddRequest(
                service_config_id=1,
                root_folder_path="/media/tv",
                quality_profile_id=4,
                search=False,
            ),
            username="admin",
        )

    response = _run(monkeypatch, {Service.SONARR: {1: sonarr}}, body)

    assert response.title == "Severance (2022)"
    assert sonarr.lookups == [371980]
    assert sonarr.exclusions_removed == [371980]
    assert sonarr.added[0]["search"] is False


def test_a_series_without_a_tvdb_id_is_unavailable_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sonarr = FakeArr({"title": "Severance"})

    async def body(db: AsyncSession) -> Any:
        row = ReclaimHistory(
            approved_by="admin",
            media_type=MediaType.SERIES,
            tmdb_id=95396,
            name="Severance",
        )
        db.add(row)
        await db.commit()
        return await get_readd_options(db, row)

    options = _run(monkeypatch, {Service.SONARR: {1: sonarr}}, body)

    assert options.instances == []
    assert options.unavailable_reason is not None
    assert "TVDB" in options.unavailable_reason
    assert sonarr.lookups == []


def test_only_deleted_rows_can_be_re_added(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(db: AsyncSession) -> Any:
        row = _deleted_movie(db)
        row.action = "moved"
        await db.commit()
        return await get_readd_options(db, row)

    options = _run(monkeypatch, {Service.RADARR: {1: FakeArr(DUNE)}}, body)
    assert options.unavailable_reason == "Only deleted titles can be re-added."


def test_re_added_rows_do_not_count_as_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def body(db: AsyncSession) -> Any:
        _deleted_movie(db)
        db.add(
            ReclaimHistory(
                approved_by="admin",
                media_type=MediaType.MOVIE,
                tmdb_id=438631,
                name="Dune (2021)",
                action=arr_readd.READD_ACTION,
            )
        )
        await db.commit()
        return await load_reclaim_totals(db)

    totals = _run(monkeypatch, {}, body)
    assert totals.reclaimed_movies == 1
    assert totals.reclaimed_bytes == 10
