from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.rules import _normalize_rule_action
from backend.database import Base
from backend.database.models import (
    Movie,
    MovieArrRef,
    ReclaimCandidate,
    ReclaimHistory,
    ReclaimRule,
    Series,
    SeriesArrRef,
    ServiceConfig,
)
from backend.enums import MediaType, Service
from backend.models.services.radarr import RadarrMovie
from backend.models.services.sonarr import SonarrSeries
from backend.services.reclaimable import non_reclaiming_candidate_totals
from backend.tasks import cleanup

PROFILE_ACTION = "change_quality_profile"


# --------------------------------------------------------------------------
# rule action normalization
# --------------------------------------------------------------------------


def _action(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "arr_action": PROFILE_ACTION,
        "quality_profile_id": 7,
    }
    base.update(overrides)
    return base


def test_a_profile_change_keeps_its_profile_and_drops_the_delete_settings() -> None:
    normalized = _normalize_rule_action(
        _action(move_instead_of_delete=True), "Downgrade", "movie_version"
    )

    assert normalized["arr_action"] == PROFILE_ACTION
    assert normalized["quality_profile_id"] == 7
    assert normalized["trigger_search"] is True
    # nothing is removed or relocated, so these must not survive
    assert normalized["media_server_action"] is None
    assert normalized["move_instead_of_delete"] is False


def test_the_search_toggle_can_be_turned_off() -> None:
    normalized = _normalize_rule_action(
        _action(trigger_search=False), "Downgrade", "series"
    )

    assert normalized["trigger_search"] is False


def test_an_unknown_arr_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="arr_action must be one of"):
        _normalize_rule_action({"arr_action": "obliterate"}, "Nope", "series")


@pytest.mark.parametrize("scope", ["season", "episode"])
def test_a_season_or_episode_rule_cannot_change_the_profile(scope: str) -> None:
    # a Sonarr quality profile belongs to the series, so this would silently
    # re-profile the whole show
    with pytest.raises(ValueError, match="movie and whole-series rules"):
        _normalize_rule_action(_action(), "Downgrade", scope)


@pytest.mark.parametrize("profile_id", [None, 0, -1, "7", True])
def test_a_profile_change_without_a_usable_profile_is_rejected(
    profile_id: Any,
) -> None:
    with pytest.raises(ValueError, match="requires the profile"):
        _normalize_rule_action(
            _action(quality_profile_id=profile_id), "Downgrade", "series"
        )


def test_a_delete_rule_carries_no_profile() -> None:
    normalized = _normalize_rule_action(
        {"arr_action": "delete", "quality_profile_id": 7}, "Delete", "series"
    )

    assert normalized["quality_profile_id"] is None
    assert normalized["trigger_search"] is False


def test_a_protect_rule_carries_no_profile() -> None:
    normalized = _normalize_rule_action(
        _action(outcome="protect"), "Keep", "series"
    )

    assert normalized["quality_profile_id"] is None
    assert normalized["trigger_search"] is False


# --------------------------------------------------------------------------
# routing precedence
# --------------------------------------------------------------------------


def _rule(rule_id: int, **action: Any) -> ReclaimRule:
    rule = ReclaimRule(
        name=f"rule-{rule_id}",
        media_type=MediaType.MOVIE,
        enabled=True,
        target_scope="movie_version",
        definition=None,
        action=action,
    )
    rule.id = rule_id
    return rule


def _candidate(
    *, rule_ids: list[int], movie_id: int | None = None, series_id: int | None = None
) -> ReclaimCandidate:
    return ReclaimCandidate(
        media_type=MediaType.MOVIE if movie_id else MediaType.SERIES,
        matched_rule_ids=rule_ids,
        matched_criteria={},
        reason="test",
        movie_id=movie_id,
        series_id=series_id,
    )


def test_the_lowest_matched_rule_id_supplies_the_profile() -> None:
    rules = {
        5: _rule(5, arr_action=PROFILE_ACTION, quality_profile_id=50),
        2: _rule(2, arr_action=PROFILE_ACTION, quality_profile_id=20),
    }

    assert cleanup._quality_profile_target([5, 2], rules) == (20, True)


def test_a_rule_without_a_profile_change_supplies_nothing() -> None:
    rules = {1: _rule(1, arr_action="delete")}

    assert cleanup._quality_profile_target([1], rules) is None


def test_a_profile_change_outranks_every_removal_action() -> None:
    rules = {
        1: _rule(1, arr_action="delete"),
        2: _rule(2, arr_action="unmonitor_only"),
        3: _rule(3, arr_action=PROFILE_ACTION, quality_profile_id=9),
    }
    candidate = _candidate(rule_ids=[1, 2, 3], movie_id=1)

    assert cleanup._get_arr_action(candidate, rules) == PROFILE_ACTION
    assert cleanup._merge_arr_action("unmonitor_only", PROFILE_ACTION) == PROFILE_ACTION
    assert cleanup._merge_arr_action(PROFILE_ACTION, "delete") == PROFILE_ACTION


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


class FakeRadarr:
    def __init__(self, current_profile_id: int | None = 1) -> None:
        self.current_profile_id = current_profile_id
        self.profile_calls: list[tuple[list[int], int]] = []
        self.search_calls: list[list[int]] = []

    async def get_all_movies(self) -> list[RadarrMovie]:
        return [
            RadarrMovie(
                id=55,
                title="Arrival",
                title_slug="arrival",
                tmdb_id=1,
                imdb_id=None,
                year=2016,
                path="/movies/Arrival",
                has_file=True,
                monitored=True,
                tags=[],
                raw={"qualityProfileId": self.current_profile_id},
            )
        ]

    async def set_movies_quality_profile(
        self, movie_ids: list[int], quality_profile_id: int
    ) -> list[RadarrMovie]:
        self.profile_calls.append((movie_ids, quality_profile_id))
        self.current_profile_id = quality_profile_id
        return []

    async def search_movies(self, movie_ids: list[int]) -> None:
        self.search_calls.append(movie_ids)


class FailingRadarr(FakeRadarr):
    async def set_movies_quality_profile(
        self, movie_ids: list[int], quality_profile_id: int
    ) -> list[RadarrMovie]:
        raise RuntimeError("radarr unavailable")


class FakeSonarr:
    def __init__(self, current_profile_id: int | None = 1) -> None:
        self.current_profile_id = current_profile_id
        self.profile_calls: list[tuple[list[int], int]] = []
        self.search_calls: list[list[int]] = []

    async def get_all_series(self) -> list[SonarrSeries]:
        return [
            SonarrSeries(
                id=77,
                title="Severance",
                title_slug="severance",
                tvdb_id=9,
                tmdb_id=9,
                imdb_id=None,
                year=2022,
                path="/tv/Severance",
                monitored=True,
                status="continuing",
                season_count=2,
                seasons=[],
                tags=[],
                raw={"qualityProfileId": self.current_profile_id},
            )
        ]

    async def set_series_quality_profile(
        self, series_ids: list[int], quality_profile_id: int
    ) -> None:
        self.profile_calls.append((series_ids, quality_profile_id))
        self.current_profile_id = quality_profile_id

    async def search_series(self, series_ids: list[int]) -> None:
        self.search_calls.append(series_ids)


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    radarr: dict[int, Any] | None = None,
    sonarr: dict[int, Any] | None = None,
) -> None:
    monkeypatch.setattr(cleanup.service_manager, "_radarr", None)
    monkeypatch.setattr(cleanup.service_manager, "_radarr_clients", radarr or {})
    monkeypatch.setattr(cleanup.service_manager, "_sonarr", None)
    monkeypatch.setattr(cleanup.service_manager, "_sonarr_clients", sonarr or {})


async def _make_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(cleanup, "async_db", session_maker)
    return engine, session_maker


async def _seed_movie(session_maker: async_sessionmaker[AsyncSession]) -> int:
    async with session_maker() as db:
        config = ServiceConfig(
            service_type=Service.RADARR,
            name="Radarr",
            base_url="http://radarr.invalid",
            api_key="k",
            enabled=True,
        )
        db.add(config)
        movie = Movie(title="Arrival", tmdb_id=1, size=900)
        db.add(movie)
        await db.flush()
        db.add(
            MovieArrRef(
                movie_id=movie.id,
                service_config_id=config.id,
                arr_movie_id=55,
            )
        )
        candidate = _candidate(rule_ids=[1], movie_id=movie.id)
        db.add(candidate)
        await db.commit()
        return candidate.id


@pytest.mark.anyio
async def test_a_profile_change_is_applied_and_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_maker = await _make_session(monkeypatch)
    candidate_id = await _seed_movie(session_maker)
    radarr = FakeRadarr(current_profile_id=1)
    _patch_clients(monkeypatch, radarr={1: radarr})
    rules = {1: _rule(1, arr_action=PROFILE_ACTION, quality_profile_id=9)}

    changed, failed = await cleanup._change_quality_profile_candidates(
        [candidate_id], rules, approved_by="tester"
    )

    assert (changed, failed) == (1, 0)
    assert radarr.profile_calls == [([55], 9)]
    assert radarr.search_calls == [[55]]

    async with session_maker() as db:
        assert (await db.execute(select(ReclaimCandidate))).scalars().first() is None
        history = (await db.execute(select(ReclaimHistory))).scalars().all()
    await engine.dispose()

    assert len(history) == 1
    assert history[0].action == "profile_changed"
    assert history[0].name == "Arrival"
    assert history[0].approved_by == "tester"


@pytest.mark.anyio
async def test_the_search_is_skipped_when_the_rule_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_maker = await _make_session(monkeypatch)
    candidate_id = await _seed_movie(session_maker)
    radarr = FakeRadarr(current_profile_id=1)
    _patch_clients(monkeypatch, radarr={1: radarr})
    rules = {
        1: _rule(
            1,
            arr_action=PROFILE_ACTION,
            quality_profile_id=9,
            trigger_search=False,
        )
    }

    changed, _failed = await cleanup._change_quality_profile_candidates(
        [candidate_id], rules
    )
    await engine.dispose()

    assert changed == 1
    assert radarr.profile_calls == [([55], 9)]
    assert radarr.search_calls == []


@pytest.mark.anyio
async def test_an_item_already_on_the_target_profile_is_not_searched_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # without this the item keeps matching its rule and gets re-searched on
    # every pass, forever
    engine, session_maker = await _make_session(monkeypatch)
    candidate_id = await _seed_movie(session_maker)
    radarr = FakeRadarr(current_profile_id=9)
    _patch_clients(monkeypatch, radarr={1: radarr})
    rules = {1: _rule(1, arr_action=PROFILE_ACTION, quality_profile_id=9)}

    changed, failed = await cleanup._change_quality_profile_candidates(
        [candidate_id], rules
    )

    assert (changed, failed) == (1, 0)
    assert radarr.profile_calls == []
    assert radarr.search_calls == []

    async with session_maker() as db:
        # the candidate is still cleared, so it stops nagging
        assert (await db.execute(select(ReclaimCandidate))).scalars().first() is None
    await engine.dispose()


@pytest.mark.anyio
async def test_a_failing_arr_leaves_the_candidate_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_maker = await _make_session(monkeypatch)
    candidate_id = await _seed_movie(session_maker)
    _patch_clients(monkeypatch, radarr={1: FailingRadarr(current_profile_id=1)})
    rules = {1: _rule(1, arr_action=PROFILE_ACTION, quality_profile_id=9)}

    changed, failed = await cleanup._change_quality_profile_candidates(
        [candidate_id], rules
    )

    assert (changed, failed) == (0, 1)
    async with session_maker() as db:
        candidate = (
            await db.execute(select(ReclaimCandidate))
        ).scalars().one()
        assert candidate.last_delete_error is not None
        assert "radarr unavailable" in candidate.last_delete_error
        assert (await db.execute(select(ReclaimHistory))).scalars().first() is None
    await engine.dispose()


@pytest.mark.anyio
async def test_an_item_no_arr_holds_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_maker = await _make_session(monkeypatch)
    candidate_id = await _seed_movie(session_maker)
    _patch_clients(monkeypatch, radarr={})
    rules = {1: _rule(1, arr_action=PROFILE_ACTION, quality_profile_id=9)}

    changed, failed = await cleanup._change_quality_profile_candidates(
        [candidate_id], rules
    )

    async with session_maker() as db:
        candidate = (await db.execute(select(ReclaimCandidate))).scalars().one()
    await engine.dispose()

    assert (changed, failed) == (0, 1)
    assert "Radarr" in (candidate.last_delete_error or "")


@pytest.mark.anyio
async def test_a_series_profile_change_goes_through_sonarr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, session_maker = await _make_session(monkeypatch)
    async with session_maker() as db:
        config = ServiceConfig(
            service_type=Service.SONARR,
            name="Sonarr",
            base_url="http://sonarr.invalid",
            api_key="k",
            enabled=True,
        )
        db.add(config)
        series = Series(title="Severance", tmdb_id=9, size=5000)
        db.add(series)
        await db.flush()
        db.add(
            SeriesArrRef(
                series_id=series.id,
                service_config_id=config.id,
                arr_series_id=77,
            )
        )
        candidate = _candidate(rule_ids=[1], series_id=series.id)
        db.add(candidate)
        await db.commit()
        candidate_id = candidate.id

    sonarr = FakeSonarr(current_profile_id=1)
    _patch_clients(monkeypatch, sonarr={1: sonarr})
    rules = {1: _rule(1, arr_action=PROFILE_ACTION, quality_profile_id=4)}

    changed, failed = await cleanup._change_quality_profile_candidates(
        [candidate_id], rules
    )

    async with session_maker() as db:
        history = (await db.execute(select(ReclaimHistory))).scalars().one()
    await engine.dispose()

    assert (changed, failed) == (1, 0)
    assert sonarr.profile_calls == [([77], 4)]
    assert sonarr.search_calls == [[77]]
    assert history.media_type is MediaType.SERIES
    assert history.action == "profile_changed"


# --------------------------------------------------------------------------
# reclaimable accounting
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_profile_change_candidates_are_not_counted_as_reclaimable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

    async with session_maker() as db:
        db.add_all(
            [
                _rule(1, arr_action=PROFILE_ACTION, quality_profile_id=9),
                _rule(2, arr_action="delete"),
            ]
        )
        profile_candidate = _candidate(rule_ids=[1], movie_id=1)
        profile_candidate.estimated_space_bytes = 700
        delete_candidate = _candidate(rule_ids=[2], movie_id=2)
        delete_candidate.estimated_space_bytes = 300
        db.add_all([profile_candidate, delete_candidate])
        await db.flush()

        totals = await non_reclaiming_candidate_totals(db)
    await engine.dispose()

    assert totals == {MediaType.MOVIE: (1, 700)}


@pytest.mark.anyio
async def test_no_profile_rules_means_no_adjustment() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )

    async with session_maker() as db:
        db.add(_rule(1, arr_action="delete"))
        candidate = _candidate(rule_ids=[1], movie_id=1)
        candidate.estimated_space_bytes = 300
        db.add(candidate)
        await db.flush()

        totals = await non_reclaiming_candidate_totals(db)
    await engine.dispose()

    assert totals == {}
