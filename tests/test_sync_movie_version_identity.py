"""Isolated audit reproducers; assertions describe the required safe behavior."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routes.media import get_candidates
from backend.database import Base
from backend.database.models import (
    Movie,
    MovieVersion,
    ReclaimCandidate,
    User,
)
from backend.enums import MediaType, Service, UserRole
from backend.models.media import MovieVersionData
from backend.services.plex import PlexService
from backend.tasks import sync


@asynccontextmanager
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


def version(movie_id, key):
    return MovieVersion(
        movie_id=movie_id,
        service=Service.PLEX,
        service_item_id="item-" + key,
        service_media_id=key,
        library_id="library",
        library_name="Movies",
        path=f"/movies/{key}/film.mkv",
        size=1000,
        video_width=1920,
        video_height=1080,
        video_codec="h264",
    )


def incoming(key):
    return MovieVersionData(
        service=Service.PLEX,
        service_item_id="item-" + key,
        service_media_id=key,
        library_id="library",
        library_name="Movies",
        path=f"/movies/{key}/film.mkv",
        size=1000,
        added_at=None,
        video_width=1920,
        video_height=1080,
        video_codec="h264",
    )


@pytest.mark.parametrize("keys", [("original", "copy"), ("copy", "original")])
def test_identical_new_copy_must_not_steal_existing_candidate(keys):
    async def run():
        async with database() as factory:
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.flush()
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="original selected",
                    reason_data=[],
                )
                db.add(candidate)
                await db.commit()
                await sync._upsert_movie_versions(
                    db, movie, [incoming(key) for key in keys]
                )
                await db.commit()
                rows = (await db.scalars(select(MovieVersion))).all()
                assert len(rows) == 2, [
                    (v.id, v.service_media_id, v.path) for v in rows
                ]
                assert (
                    await db.get(MovieVersion, candidate.movie_version_id)
                ).path == "/movies/original/film.mkv"

    asyncio.run(run())


@pytest.mark.parametrize("keys", [("renamed",), ("renamed", "ambiguous-copy")])
def test_rename_preserves_scope_only_when_unambiguous(keys):
    async def run():
        async with database() as factory:
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.flush()
                candidate = ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=original.id,
                    matched_rule_ids=[],
                    matched_criteria={},
                    reason="original selected",
                    reason_data=[],
                )
                db.add(candidate)
                await db.commit()
                original_id, candidate_id = original.id, candidate.id
                await sync._upsert_movie_versions(
                    db, movie, [incoming(key) for key in keys]
                )
                await db.commit()
            async with factory() as db:
                rows = (await db.scalars(select(MovieVersion))).all()
                assert {v.service_media_id for v in rows} == set(keys)
                candidate = await db.get(ReclaimCandidate, candidate_id)
                if len(keys) == 1:
                    assert rows[0].id == original_id
                    assert candidate.movie_version_id == original_id
                else:
                    assert candidate is None

    asyncio.run(run())


def test_direct_match_refreshes_item_id():
    async def run():
        from dataclasses import replace

        async with database() as factory:
            async with factory() as db:
                movie = Movie(title="Audit", tmdb_id=123, size=1000)
                db.add(movie)
                await db.flush()
                original = version(movie.id, "original")
                db.add(original)
                await db.commit()
                await sync._upsert_movie_versions(
                    db,
                    movie,
                    [replace(incoming("original"), service_item_id="new-item")],
                )
                await db.commit()
                assert original.service_item_id == "new-item"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("trash_marker_in_details", "storage_unavailable"),
    [
        pytest.param(False, False, id="section-marker"),
        pytest.param(True, False, id="detail-marker"),
        pytest.param(False, True, id="all-copies-trashed"),
    ],
)
def test_plex_sync_prunes_trashed_version_candidates(
    monkeypatch, trash_marker_in_details, storage_unavailable
):
    async def run():
        movie_item = {
            "type": "movie",
            "ratingKey": "1",
            "title": "Upgrade",
            "Guid": [{"id": "tmdb://123"}],
        }
        original = {
            "id": "original",
            "Part": [{"file": "/movies/original/film.mkv", "size": 1000}],
        }
        upgrade = {
            "id": "upgrade",
            "deletedAt": 1795228800 if storage_unavailable else 0,
            "Part": [{"file": "/movies/upgrade/film.mkv", "size": 2000}],
        }
        trashed_original = dict(original, deletedAt="1795228800")
        section_item = dict(
            movie_item,
            Media=[original if trash_marker_in_details else trashed_original, upgrade],
        )
        detail_item = dict(
            movie_item,
            Media=[trashed_original if trash_marker_in_details else original, upgrade],
        )

        async def make_request(self, path, *, params=None, **kwargs):
            if path == "library/sections/1/all":
                items = [section_item] if params["type"] == 1 else []
                return {"MediaContainer": {"Metadata": items}}, 200
            containers = {
                "library/sections": {
                    "Directory": [{"type": "movie", "key": "1", "uuid": "library"}]
                },
                "library/metadata/1": {"Metadata": [detail_item]},
                "status/sessions/history/all": {"Metadata": []},
            }
            return {"MediaContainer": containers[path]}, 200

        monkeypatch.setattr(PlexService, "_make_request", make_request)
        plex = PlexService("token", "http://plex.local")
        [incoming_movie] = await plex.get_aggregated_movies()

        async with database() as factory, factory() as db:
            movie = Movie(title="Upgrade", tmdb_id=123, size=3000)
            db.add(movie)
            await db.flush()
            stored_original = version(movie.id, "original")
            stored_upgrade = version(movie.id, "upgrade")
            stored_upgrade.size = 2000
            db.add_all([stored_original, stored_upgrade])
            await db.flush()
            for stored in (stored_original, stored_upgrade):
                db.add(
                    ReclaimCandidate(
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                        movie_version_id=stored.id,
                        estimated_space_bytes=stored.size,
                        matched_rule_ids=[],
                        matched_criteria={},
                        reason="Selected by cleanup rule",
                        reason_data=[],
                    )
                )
            await db.commit()

            await sync._upsert_movie_versions(db, movie, incoming_movie.versions)
            await db.commit()

            response = await get_candidates(
                User(username="admin", password_hash="x", role=UserRole.ADMIN),
                db,
                page=1,
                per_page=25,
                sort_by="estimated_space_bytes",
                sort_order="desc",
                search=None,
                media_type=MediaType.MOVIE,
            )
            expected = {stored_upgrade.id: 2000}
            if storage_unavailable:
                expected[stored_original.id] = 1000
            assert {
                item.movie_version_id: item.estimated_space_bytes
                for item in response.items
            } == expected
            assert movie.size == sum(expected.values())

    asyncio.run(run())
