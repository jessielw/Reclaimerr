from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.media import (
    get_candidate_rule_filter_options,
    get_candidates,
    get_candidates_presence,
)
from backend.database import Base
from backend.database.models import (
    Episode,
    Movie,
    MovieArrRef,
    MovieVersion,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    SeriesArrRef,
    SeriesServiceRef,
    ServiceConfig,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.services.seerr import SeerrUser
from backend.services.seerr_cache import SeerrRequestSnapshot


class SeededCandidateIds(TypedDict):
    alpha_movie_id: int
    alpha_candidate_id: int
    bravo_candidate_ids: list[int]
    charlie_series_id: int
    charlie_candidate_ids: list[int]
    delta_candidate_id: int


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


def _candidate(
    *,
    media_type: MediaType,
    reason: str,
    created_at: datetime,
    estimated_space_bytes: int,
    movie_id: int | None = None,
    movie_version_id: int | None = None,
    series_id: int | None = None,
    season_id: int | None = None,
    episode_id: int | None = None,
) -> ReclaimCandidate:
    entry = ReclaimCandidate(
        media_type=media_type,
        matched_rule_ids=[1],
        matched_criteria={},
        reason=reason,
        reason_data=[],
        movie_id=movie_id,
        movie_version_id=movie_version_id,
        series_id=series_id,
        season_id=season_id,
        episode_id=episode_id,
        estimated_space_bytes=estimated_space_bytes,
    )
    entry.created_at = created_at
    return entry


async def _seed_candidates(db: AsyncSession) -> SeededCandidateIds:
    db.add(
        ReclaimRule(
            name="Auto-delete rule",
            media_type=MediaType.SERIES,
            enabled=True,
            target_scope="series",
            definition=None,
            action={"auto_delete_enabled": True},
        )
    )
    await db.flush()

    alpha = Movie(title="Alpha Movie", tmdb_id=101, year=2001, size=700)
    bravo = Movie(title="Bravo Movie", tmdb_id=102, year=2002, size=1000)
    charlie = Series(title="Charlie Show", tmdb_id=201, year=2010, size=500)
    delta = Series(title="Delta Show", tmdb_id=202, year=2011, size=900)
    alpha.added_at = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
    alpha.last_viewed_at = datetime(2025, 5, 1, 10, 0, tzinfo=UTC)
    alpha.view_count = 3
    bravo.added_at = datetime(2025, 1, 2, 10, 0, tzinfo=UTC)
    bravo.last_viewed_at = datetime(2025, 5, 2, 10, 0, tzinfo=UTC)
    bravo.view_count = 4
    charlie.added_at = datetime(2025, 1, 3, 10, 0, tzinfo=UTC)
    charlie.last_viewed_at = datetime(2025, 5, 3, 10, 0, tzinfo=UTC)
    charlie.view_count = 5
    delta.added_at = datetime(2025, 1, 4, 10, 0, tzinfo=UTC)
    delta.last_viewed_at = datetime(2025, 5, 4, 10, 0, tzinfo=UTC)
    delta.view_count = 6
    alpha.imdb_id = "tt0000101"
    alpha.imdb_rating = 7.4
    alpha.imdb_vote_count = 12500
    alpha.anilist_id = 16498
    alpha.anilist_score = 85
    alpha.anilist_popularity = 998866
    alpha.anilist_favourites = 79392
    alpha.tmdb_collection_id = 10
    alpha.tmdb_collection_name = "Alpha Collection"
    alpha.tmdb_collection_checked = True
    delta.imdb_id = "tt0000202"
    delta.imdb_rating = 8.1
    delta.imdb_vote_count = 9200
    delta.anilist_id = 21459
    delta.anilist_score = 90
    delta.anilist_popularity = 543210
    delta.anilist_favourites = 40000
    db.add_all([alpha, bravo, charlie, delta])
    await db.flush()

    bravo_v1 = MovieVersion(
        movie_id=bravo.id,
        service=Service.PLEX,
        service_item_id="bravo-item-1",
        service_media_id="bravo-media-1",
        library_id="movies",
        library_name="Movies",
        size=400,
        added_at=datetime(2025, 2, 1, 10, 0, tzinfo=UTC),
        file_name="bravo-1.mkv",
    )
    bravo_v2 = MovieVersion(
        movie_id=bravo.id,
        service=Service.PLEX,
        service_item_id="bravo-item-2",
        service_media_id="bravo-media-2",
        library_id="movies",
        library_name="Movies",
        size=600,
        added_at=datetime(2025, 2, 2, 10, 0, tzinfo=UTC),
        file_name="bravo-2.mkv",
    )
    alpha_v1 = MovieVersion(
        movie_id=alpha.id,
        service=Service.PLEX,
        service_item_id="alpha-item-1",
        service_media_id="alpha-media-1",
        library_id="movies",
        library_name="Movies",
        size=700,
        added_at=datetime(2025, 2, 3, 10, 0, tzinfo=UTC),
        file_name="alpha-1.mkv",
    )
    db.add_all([alpha_v1, bravo_v1, bravo_v2])
    await db.flush()

    charlie_s1 = Season(series_id=charlie.id, season_number=1, size=300)
    charlie_s1.added_at = datetime(2025, 3, 1, 10, 0, tzinfo=UTC)
    charlie_s1.last_viewed_at = datetime(2025, 5, 5, 10, 0, tzinfo=UTC)
    charlie_s1.view_count = 7
    db.add(charlie_s1)
    await db.flush()

    charlie_ep1 = Episode(
        season_id=charlie_s1.id,
        episode_number=1,
        name="Pilot",
        size=100,
        last_viewed_at=datetime(2025, 5, 6, 10, 0, tzinfo=UTC),
        view_count=8,
    )
    charlie_ref = SeriesServiceRef(
        series_id=charlie.id,
        service=Service.PLEX,
        service_id="charlie-show",
        library_id="series",
        library_name="TV Shows",
    )
    delta_ref = SeriesServiceRef(
        series_id=delta.id,
        service=Service.PLEX,
        service_id="delta-show",
        library_id="series",
        library_name="TV Shows",
    )
    db.add_all([charlie_ep1, charlie_ref, delta_ref])
    await db.flush()

    created = lambda day: datetime(2026, 1, day, 12, 0, tzinfo=UTC)
    alpha_candidate = _candidate(
        media_type=MediaType.MOVIE,
        movie_id=alpha.id,
        reason="alpha",
        created_at=created(1),
        estimated_space_bytes=700,
    )
    bravo_candidates = [
        _candidate(
            media_type=MediaType.MOVIE,
            movie_id=bravo.id,
            movie_version_id=bravo_v1.id,
            reason="bravo v1",
            created_at=created(4),
            estimated_space_bytes=400,
        ),
        _candidate(
            media_type=MediaType.MOVIE,
            movie_id=bravo.id,
            movie_version_id=bravo_v2.id,
            reason="bravo v2",
            created_at=created(5),
            estimated_space_bytes=600,
        ),
    ]
    charlie_candidates = [
        _candidate(
            media_type=MediaType.SERIES,
            series_id=charlie.id,
            reason="charlie whole",
            created_at=created(2),
            estimated_space_bytes=500,
        ),
        _candidate(
            media_type=MediaType.SERIES,
            series_id=charlie.id,
            season_id=charlie_s1.id,
            reason="charlie season",
            created_at=created(6),
            estimated_space_bytes=300,
        ),
        _candidate(
            media_type=MediaType.SERIES,
            series_id=charlie.id,
            season_id=charlie_s1.id,
            episode_id=charlie_ep1.id,
            reason="charlie episode",
            created_at=created(7),
            estimated_space_bytes=100,
        ),
    ]
    delta_candidate = _candidate(
        media_type=MediaType.SERIES,
        series_id=delta.id,
        reason="delta",
        created_at=created(3),
        estimated_space_bytes=900,
    )
    db.add_all(
        [alpha_candidate, *bravo_candidates, *charlie_candidates, delta_candidate]
    )
    await db.commit()

    return {
        "alpha_movie_id": alpha.id,
        "alpha_candidate_id": alpha_candidate.id,
        "bravo_candidate_ids": [entry.id for entry in bravo_candidates],
        "charlie_series_id": charlie.id,
        "charlie_candidate_ids": [entry.id for entry in charlie_candidates],
        "delta_candidate_id": delta_candidate.id,
    }


def test_get_candidates_all_paginates_by_display_groups() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=2,
                sort_by="created_at",
                sort_order="desc",
                search=None,
                media_type=None,
            )

            assert response.total == 4
            assert response.total_pages == 2
            returned_ids = {item.id for item in response.items}
            assert set(ids["charlie_candidate_ids"]).issubset(returned_ids)
            assert set(ids["bravo_candidate_ids"]).issubset(returned_ids)
            assert ids["alpha_candidate_id"] not in returned_ids
            assert ids["delta_candidate_id"] not in returned_ids
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_movies_filter_keeps_version_group_together() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=1,
                sort_by="estimated_space_bytes",
                sort_order="desc",
                search=None,
                media_type=MediaType.MOVIE,
            )

            assert response.total == 2
            assert response.total_pages == 2
            assert {item.id for item in response.items} == set(
                ids["bravo_candidate_ids"]
            )
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_sorts_groups_by_auto_delete_date() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=1,
                sort_by="auto_delete_eligible_at",
                sort_order="asc",
                search=None,
                media_type=None,
            )

            assert {item.id for item in response.items} == set(
                ids["charlie_candidate_ids"]
            )
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_search_keeps_series_group_intact() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search="Charlie",
                media_type=None,
            )

            assert response.total == 1
            assert response.total_pages == 1
            assert {item.id for item in response.items} == set(
                ids["charlie_candidate_ids"]
            )
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_rule_filter_uses_exact_json_containment_before_grouping() -> (
    None
):
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            movie = Movie(title="Rule Filter Movie", tmdb_id=301, size=1000)
            db_session.add(movie)
            await db_session.flush()
            versions = [
                MovieVersion(
                    movie_id=movie.id,
                    service=Service.PLEX,
                    service_item_id=f"rule-filter-item-{index}",
                    service_media_id=f"rule-filter-media-{index}",
                    library_id="movies",
                    library_name="Movies",
                    size=500,
                )
                for index in range(2)
            ]
            db_session.add_all(versions)
            await db_session.flush()
            matches_rule_six = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=versions[0].id,
                matched_rule_ids=[6, 8],
                matched_criteria={},
                reason="Rule Six: Days since added >= 365 (940)",
                reason_data=[],
                estimated_space_bytes=500,
            )
            only_matches_rule_sixteen = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=versions[1].id,
                matched_rule_ids=[16],
                matched_criteria={},
                reason="Rule Sixteen: Days since added >= 365 (941)",
                reason_data=[],
                estimated_space_bytes=500,
            )
            db_session.add_all([matches_rule_six, only_matches_rule_sixteen])
            await db_session.commit()

            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search=None,
                media_type=None,
                rule_id=6,
            )

            assert response.total == 1
            assert [item.id for item in response.items] == [matches_rule_six.id]
            assert response.items[0].matched_rule_ids == [6, 8]
            assert response.items[0].reason == matches_rule_six.reason

            empty_response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search=None,
                media_type=None,
                rule_id=999,
            )
            assert empty_response.total == 0
            assert empty_response.items == []
        await engine.dispose()

    asyncio.run(run())


def test_candidate_rule_filter_options_include_disabled_candidate_rules_only() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            db_session.add_all(
                [
                    ReclaimRule(
                        name="Enabled candidate",
                        media_type=MediaType.MOVIE,
                        enabled=True,
                        target_scope="movie_version",
                        definition=None,
                        action={"outcome": "candidate"},
                    ),
                    ReclaimRule(
                        name="Disabled candidate",
                        media_type=MediaType.SERIES,
                        enabled=False,
                        target_scope="series",
                        definition=None,
                        action={"outcome": "candidate"},
                    ),
                    ReclaimRule(
                        name="Protection rule",
                        media_type=MediaType.MOVIE,
                        enabled=True,
                        target_scope="movie_version",
                        definition=None,
                        action={"outcome": "protect"},
                    ),
                ]
            )
            await db_session.commit()

            options = await get_candidate_rule_filter_options(_admin_user(), db_session)

            assert [(option.name, option.enabled) for option in options] == [
                ("Disabled candidate", False),
                ("Enabled candidate", True),
            ]
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_presence_reports_existing_candidates() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            empty_response = await get_candidates_presence(_admin_user(), db_session)
            assert empty_response.has_candidates is False

            await _seed_candidates(db_session)
            populated_response = await get_candidates_presence(
                _admin_user(), db_session
            )
            assert populated_response.has_candidates is True
        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_includes_imdb_fields() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)

            movie_response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search="Alpha",
                media_type=MediaType.MOVIE,
            )
            assert movie_response.total == 1
            movie_entry = movie_response.items[0]
            assert movie_entry.id == ids["alpha_candidate_id"]
            assert movie_entry.imdb_id == "tt0000101"
            assert movie_entry.imdb_rating == 7.4
            assert movie_entry.imdb_vote_count == 12500
            assert movie_entry.anilist_id == 16498
            assert movie_entry.anilist_score == 85
            assert movie_entry.anilist_popularity == 998866
            assert movie_entry.anilist_favourites == 79392
            assert movie_entry.tmdb_collection_id == 10
            assert movie_entry.tmdb_collection_name == "Alpha Collection"
            assert movie_entry.tmdb_in_collection is True

            series_response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search="Delta",
                media_type=MediaType.SERIES,
            )
            assert series_response.total == 1
            series_entry = series_response.items[0]
            assert series_entry.id == ids["delta_candidate_id"]
            assert series_entry.imdb_id == "tt0000202"
            assert series_entry.imdb_rating == 8.1
            assert series_entry.imdb_vote_count == 9200
            assert series_entry.anilist_id == 21459
            assert series_entry.anilist_score == 90
            assert series_entry.anilist_popularity == 543210
            assert series_entry.anilist_favourites == 40000
            assert series_entry.tmdb_collection_id is None
            assert series_entry.tmdb_collection_name is None
            assert series_entry.tmdb_in_collection is None

        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_includes_media_page_metadata() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)

            response = await get_candidates(
                _admin_user(),
                db_session,
                page=1,
                per_page=10,
                sort_by="created_at",
                sort_order="desc",
                search=None,
                media_type=None,
            )
            by_id = {item.id: item for item in response.items}

            alpha = by_id[ids["alpha_candidate_id"]]
            assert [lib.library_name for lib in alpha.media_libraries or []] == [
                "Movies"
            ]
            assert alpha.media_added_at == "2025-01-01T10:00:00+00:00"
            assert alpha.media_last_viewed_at == "2025-05-01T10:00:00+00:00"
            assert alpha.media_view_count == 3

            bravo_v1 = by_id[ids["bravo_candidate_ids"][0]]
            assert [lib.library_name for lib in bravo_v1.media_libraries or []] == [
                "Movies"
            ]
            assert bravo_v1.media_added_at == "2025-02-01T10:00:00+00:00"
            assert bravo_v1.media_last_viewed_at == "2025-05-02T10:00:00+00:00"
            assert bravo_v1.media_view_count == 4

            charlie_whole = by_id[ids["charlie_candidate_ids"][0]]
            assert [
                lib.library_name for lib in charlie_whole.media_libraries or []
            ] == ["TV Shows"]
            assert charlie_whole.media_added_at == "2025-01-03T10:00:00+00:00"
            assert charlie_whole.media_last_viewed_at == "2025-05-03T10:00:00+00:00"
            assert charlie_whole.media_view_count == 5

            charlie_season = by_id[ids["charlie_candidate_ids"][1]]
            assert [
                lib.library_name for lib in charlie_season.media_libraries or []
            ] == ["TV Shows"]
            assert charlie_season.media_added_at == "2025-03-01T10:00:00+00:00"
            assert charlie_season.media_last_viewed_at == "2025-05-05T10:00:00+00:00"
            assert charlie_season.media_view_count == 7

            charlie_episode = by_id[ids["charlie_candidate_ids"][2]]
            assert [
                lib.library_name for lib in charlie_episode.media_libraries or []
            ] == ["TV Shows"]
            assert charlie_episode.media_added_at is None
            assert charlie_episode.media_last_viewed_at == "2025-05-06T10:00:00+00:00"
            assert charlie_episode.media_view_count == 8

        await engine.dispose()

    asyncio.run(run())


def test_get_candidates_includes_origin_metadata() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        async with session_maker() as db_session:
            ids = await _seed_candidates(db_session)
            alpha = await db_session.get(Movie, ids["alpha_movie_id"])
            charlie = await db_session.get(Series, ids["charlie_series_id"])
            assert alpha is not None
            assert charlie is not None
            alpha.arr_tags = ["requested", "keep"]
            charlie.arr_tags = ["anime"]

            radarr_config = ServiceConfig(
                service_type=Service.RADARR,
                base_url="https://radarr.example/radarr/",
                api_key="radarr-key",
                name="4K Radarr",
                enabled=True,
            )
            sonarr_config = ServiceConfig(
                service_type=Service.SONARR,
                base_url="https://sonarr.example/sonarr",
                api_key="sonarr-key",
                name="Anime Sonarr",
                enabled=True,
            )
            seerr_config = ServiceConfig(
                service_type=Service.SEERR,
                base_url="https://seerr.example/seerr/",
                api_key="seerr-key",
                name="Seerr",
                enabled=True,
            )
            db_session.add_all([radarr_config, sonarr_config, seerr_config])
            await db_session.flush()
            db_session.add_all(
                [
                    MovieArrRef(
                        movie_id=alpha.id,
                        service_config_id=radarr_config.id,
                        arr_movie_id=11,
                        arr_title_slug="alpha-movie-101",
                        tmdb_id=alpha.tmdb_id,
                    ),
                    SeriesArrRef(
                        series_id=charlie.id,
                        service_config_id=sonarr_config.id,
                        arr_series_id=22,
                        arr_title_slug="charlie-show",
                        tmdb_id=charlie.tmdb_id,
                    ),
                ]
            )
            await db_session.commit()

            seerr_id = seerr_config.id

            def qualified(user_id: int) -> str:
                return f"{seerr_id}:{user_id}"

            snapshot = SeerrRequestSnapshot(
                requester_ids_by_key={
                    (MediaType.MOVIE, 101): {qualified(7), qualified(8)},
                    (MediaType.SERIES, 201): {qualified(9)},
                },
                first_request_at_by_key_user={},
                requester_identity_keys_by_user_id={},
                latest_active_request_at_by_key={},
                requester_ids_by_series_season={(201, 1): {qualified(10)}},
                requester_users_by_id={
                    qualified(7): SeerrUser(
                        id=7,
                        username="alex",
                        display_name="Alex Smith",
                    ),
                    qualified(8): SeerrUser(id=8, username="bea", display_name=None),
                    qualified(9): SeerrUser(
                        id=9, username="casey", display_name="Casey"
                    ),
                    qualified(10): SeerrUser(
                        id=10, username="devon", display_name="Devon"
                    ),
                },
            )
            get_snapshot = AsyncMock(return_value=(snapshot, None))
            with patch(
                "backend.services.media_origins.seerr_snapshot_cache",
                new=SimpleNamespace(get_request_snapshot=get_snapshot),
            ):
                response = await get_candidates(
                    _admin_user(),
                    db_session,
                    page=1,
                    per_page=10,
                    sort_by="created_at",
                    sort_order="desc",
                    search=None,
                    media_type=None,
                )

            by_id = {item.id: item for item in response.items}
            alpha_entry = by_id[ids["alpha_candidate_id"]]
            assert alpha_entry.arr_tags == ["requested", "keep"]
            assert [link.item_url for link in alpha_entry.seerr_links] == [
                "https://seerr.example/seerr/movie/101"
            ]
            assert alpha_entry.seerr_links[0].service_name == "Seerr"
            assert [item.key for item in alpha_entry.seerr_requesters] == [
                qualified(7),
                qualified(8),
            ]
            assert [item.display_name for item in alpha_entry.seerr_requesters] == [
                "Alex Smith",
                "bea",
            ]
            assert len(alpha_entry.arr_refs) == 1
            assert alpha_entry.arr_refs[0].service_name == "4K Radarr"
            assert (
                alpha_entry.arr_refs[0].item_url
                == "https://radarr.example/radarr/movie/alpha-movie-101"
            )

            charlie_whole = by_id[ids["charlie_candidate_ids"][0]]
            assert charlie_whole.arr_tags == ["anime"]
            assert [link.item_url for link in charlie_whole.seerr_links] == [
                "https://seerr.example/seerr/tv/201"
            ]
            assert [item.display_name for item in charlie_whole.seerr_requesters] == [
                "Casey"
            ]
            assert charlie_whole.arr_refs[0].service_name == "Anime Sonarr"
            assert (
                charlie_whole.arr_refs[0].item_url
                == "https://sonarr.example/sonarr/series/charlie-show"
            )

            charlie_season = by_id[ids["charlie_candidate_ids"][1]]
            charlie_episode = by_id[ids["charlie_candidate_ids"][2]]
            assert [item.display_name for item in charlie_season.seerr_requesters] == [
                "Devon"
            ]
            assert [item.display_name for item in charlie_episode.seerr_requesters] == [
                "Devon"
            ]
            get_snapshot.assert_awaited_once_with(
                require_fresh=False,
                allow_stale_on_failure=True,
            )

        await engine.dispose()

    asyncio.run(run())
