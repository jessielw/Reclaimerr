from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.duplicates import delete_leftovers, list_duplicates
from backend.api.routes.protected import create_protection_entry
from backend.api.routes.v1.protections import _resolve_target
from backend.core.media_locator import resolve_media_locator
from backend.core.protection_scope import detach_movie_version_references
from backend.database import Base
from backend.database.models import (
    Episode,
    EpisodeVersion,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    Season,
    Series,
    UpgradeLeftover,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.api_v1.protections import ProtectionCreateRequest
from backend.models.duplicates import LeftoverDeleteRequest
from backend.models.protect import CreateProtectedEntryRequest
from backend.services.candidate_lifecycle import candidate_deletion_blockers
from backend.services.duplicates import (
    DuplicateActionError,
    load_duplicate_groups,
    plan_duplicate_delete,
)
from backend.services.upgrade_leftovers import load_leftovers


@asynccontextmanager
async def catalog():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            user = User(username="admin", password_hash="x", role=UserRole.ADMIN)
            movie = Movie(title="Film", tmdb_id=550, imdb_id="tt0137523", anilist_id=10)
            series = Series(
                title="Show",
                tmdb_id=550,
                imdb_id="tt1234567",
                tvdb_id="123",
                anilist_id=20,
            )
            db.add_all([user, movie, series])
            await db.commit()
            yield db, user, movie, series
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "media_type,locator",
    [
        (MediaType.MOVIE, {"tmdb_id": 550}),
        (MediaType.MOVIE, {"imdb_id": "tt0137523"}),
        (MediaType.MOVIE, {"anilist_id": 10}),
        (MediaType.SERIES, {"tmdb_id": 550}),
        (MediaType.SERIES, {"imdb_id": "tt1234567"}),
        (MediaType.SERIES, {"tvdb_id": "123"}),
        (MediaType.SERIES, {"anilist_id": 20}),
    ],
)
def test_external_ids_resolve_whole_title_in_both_apis(media_type, locator):
    async def run():
        async with catalog() as (db, user, movie, series):
            expected = movie if media_type is MediaType.MOVIE else series
            target, version, season, episode = await _resolve_target(
                db, ProtectionCreateRequest(media_type=media_type, **locator)
            )
            assert target is expected
            assert (version, season, episode) == (None, None, None)
            response = await create_protection_entry(
                CreateProtectedEntryRequest(media_type=media_type, **locator), user, db
            )
            assert response.media_id == expected.id
            assert response.media_type is media_type
            assert response.permanent
            assert (
                response.movie_version_id,
                response.season_id,
                response.episode_id,
            ) == (None, None, None)

    asyncio.run(run())


@pytest.mark.parametrize(
    "model", [CreateProtectedEntryRequest, ProtectionCreateRequest]
)
@pytest.mark.parametrize(
    "locator",
    [
        {},
        {"tmdb_id": 550, "imdb_id": "tt0137523"},
        {"tmdb_id": 0},
        {"imdb_id": "550"},
        {"tvdb_id": "123"},
        {"anilist_id": -1},
    ],
)
def test_invalid_or_ambiguous_locator_is_rejected(model, locator):
    with pytest.raises(ValidationError):
        model(media_type=MediaType.MOVIE, **locator)


def test_missing_removed_and_nonunique_ids_do_not_protect_wrong_title():
    async def run():
        async with catalog() as (db, _, movie, _):
            with pytest.raises(HTTPException) as error:
                await resolve_media_locator(
                    db,
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE, imdb_id="tt1234567"
                    ),
                )
            assert error.value.status_code == 404
            movie.removed_at = datetime.now(UTC)
            await db.flush()
            with pytest.raises(HTTPException) as error:
                await resolve_media_locator(
                    db,
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE, tmdb_id=550
                    ),
                )
            assert error.value.status_code == 404
            movie.removed_at = None
            db.add(Movie(title="Different film", tmdb_id=551, anilist_id=10))
            await db.flush()
            with pytest.raises(HTTPException) as error:
                await resolve_media_locator(
                    db,
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE, anilist_id=10
                    ),
                )
            assert error.value.status_code == 409

    asyncio.run(run())


def test_title_survives_replacement_and_covers_new_libraries_and_leftovers():
    async def run():
        async with catalog() as (db, user, movie, _):

            def version(name, library):
                return MovieVersion(
                    movie_id=movie.id,
                    service=Service.PLEX,
                    service_item_id=name,
                    service_media_id=name,
                    library_id=library,
                    library_name=library,
                    path=f"/movies/{name}.mkv",
                    size=1000,
                )

            old = version("old", "L1")
            db.add(old)
            await db.commit()
            response = await create_protection_entry(
                CreateProtectedEntryRequest(
                    media_type=MediaType.MOVIE, imdb_id=movie.imdb_id
                ),
                user,
                db,
            )
            await detach_movie_version_references(db, [old.id])
            await db.delete(old)
            await db.flush()
            new = version("replacement", "L1")
            copy = version("4k-copy", "L2")
            db.add_all([new, copy])
            leftover = UpgradeLeftover(
                service_config_id=1,
                arr_movie_id=1,
                movie_id=movie.id,
                title=movie.title,
                dropped_path="/downloads/old.mkv",
                local_path="/downloads/old.mkv",
                size=1000,
                link_count=1,
            )
            db.add(leftover)
            await db.commit()
            assert (await db.get(ProtectedMedia, response.id)) is not None
            group = (await load_duplicate_groups(db))[0]
            assert group.fully_protected and group.reclaimable_size == 0
            with pytest.raises(DuplicateActionError, match="protected"):
                await plan_duplicate_delete(
                    db,
                    media_type=MediaType.MOVIE,
                    item_id=movie.id,
                    version_ids=[new.id],
                )
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                movie_version_id=new.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="test",
            )
            assert "protected" in await candidate_deletion_blockers(db, candidate)
            params = dict(
                page=1,
                per_page=25,
                media_type=None,
                search=None,
                sort_by="title",
                include_cross_library=True,
                include_manual=True,
            )
            hidden = await list_duplicates(user, db, include_ignored=False, **params)
            assert hidden.total == 0
            visible = await list_duplicates(user, db, include_ignored=True, **params)
            assert visible.total == 1 and visible.items[0].fully_protected
            assert visible.summary.actionable == 0
            views = await load_leftovers(db)
            assert views[0].protected and not views[0].actionable
            with pytest.raises(HTTPException, match="protected"):
                await delete_leftovers(
                    LeftoverDeleteRequest(ids=[leftover.id]), user, db
                )
            protection = await db.get(ProtectedMedia, response.id)
            protection.permanent = False
            protection.expires_at = datetime.now(UTC) - timedelta(days=1)
            await db.commit()
            assert not (await load_duplicate_groups(db))[0].fully_protected
            assert not (await load_leftovers(db))[0].protected
            assert "protected" not in await candidate_deletion_blockers(db, candidate)

    asyncio.run(run())


def test_series_title_covers_future_seasons_episodes_and_libraries():
    async def run():
        async with catalog() as (db, user, _, series):
            await create_protection_entry(
                CreateProtectedEntryRequest(
                    media_type=MediaType.SERIES, tvdb_id=series.tvdb_id
                ),
                user,
                db,
            )
            season = Season(series_id=series.id, season_number=2)
            db.add(season)
            await db.flush()
            episode = Episode(season_id=season.id, episode_number=1)
            db.add(episode)
            await db.flush()
            for name in ("TV", "4K TV"):
                db.add(
                    EpisodeVersion(
                        episode_id=episode.id,
                        service=Service.PLEX,
                        service_item_id=name,
                        service_media_id=name,
                        library_id=name,
                        library_name=name,
                        path=f"/{name}/episode.mkv",
                        size=1000,
                    )
                )
            await db.commit()
            group = (await load_duplicate_groups(db))[0]
            assert group.fully_protected and group.series_id == series.id
            candidate = ReclaimCandidate(
                media_type=MediaType.SERIES,
                series_id=series.id,
                season_id=season.id,
                episode_id=episode.id,
                matched_rule_ids=[],
                matched_criteria={},
                reason="test",
            )
            assert "protected" in await candidate_deletion_blockers(db, candidate)

    asyncio.run(run())
