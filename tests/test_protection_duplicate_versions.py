"""Regression coverage for issue #372 - the same title protected twice.

Three separate defects produced a duplicate: a movie version pruned by sync left
its protection behind pointing at a row that no longer existed, movie protections
had no scope-overlap rule so a whole-movie and a per-version protection never saw
each other, and the approve endpoint inserted a protection with no dedupe at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio.engine import AsyncEngine

from backend.api.routes.protected import create_protection_entry, get_protected_entries
from backend.api.routes.requests import approve_request, create_protection_request
from backend.database import Base
from backend.database.models import (
    Movie,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    User,
)
from backend.enums import MediaType, ProtectionRequestStatus, Service, UserRole
from backend.models.media import MovieVersionData
from backend.models.protect import (
    CreateProtectedEntryRequest,
    PaginatedProtectedResponse,
)
from backend.models.requests import CreateProtectionRequest, ReviewProtectionRequest
from backend.tasks.sync import _upsert_movie_versions


def _admin_user(username: str = "admin") -> User:
    return User(
        username=username,
        password_hash="x",
        role=UserRole.ADMIN,
        permissions=[],
    )


async def _make_session() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


def _version_row(movie_id: int, media_id: str, **overrides: object) -> MovieVersion:
    fields: dict[str, object] = {
        "movie_id": movie_id,
        "service": Service.PLEX,
        "service_item_id": "item-1",
        "service_media_id": media_id,
        "library_id": "L1",
        "library_name": "Movies",
        "path": f"/movies/{media_id}.mkv",
        "size": 10_000,
        "file_name": f"{media_id}.mkv",
        "video_codec": "h264",
        "video_resolution": "1080p",
        "video_width": 1920,
        "video_height": 1080,
        "video_hdr": False,
        "video_dolby_vision": False,
        "container": "mkv",
    }
    fields.update(overrides)
    return MovieVersion(**fields)  # type: ignore[arg-type]


def _version_data(media_id: str, **overrides: object) -> MovieVersionData:
    fields: dict[str, object] = {
        "service": Service.PLEX,
        "service_item_id": "item-1",
        "service_media_id": media_id,
        "library_id": "L1",
        "library_name": "Movies",
        "path": f"/movies/{media_id}.mkv",
        "size": 10_000,
        "added_at": None,
        "file_name": f"{media_id}.mkv",
        "video_codec": "h264",
        "video_resolution": "1080p",
        "video_width": 1920,
        "video_height": 1080,
        "video_hdr": False,
        "video_dolby_vision": False,
        "container": "mkv",
    }
    fields.update(overrides)
    return MovieVersionData(**fields)  # type: ignore[arg-type]


async def _list_protected(user: User, db: AsyncSession) -> PaginatedProtectedResponse:
    """Call the list route directly - its Query() defaults are not real defaults."""
    return await get_protected_entries(
        _user=user,
        db=db,
        page=1,
        per_page=25,
        search=None,
        sort_by="created_at",
        sort_order="desc",
        media_type=None,
    )


async def _seed_movie(db: AsyncSession) -> tuple[User, Movie, MovieVersion]:
    admin = _admin_user()
    movie = Movie(title="Replaced Film", tmdb_id=4242, year=2024, size=10_000)
    db.add_all([admin, movie])
    await db.flush()
    version = _version_row(movie.id, "media-old")
    db.add(version)
    await db.commit()
    return admin, movie, version


# --- the reported scenario -------------------------------------------------


def test_replacing_a_protected_file_does_not_leave_a_duplicate() -> None:
    """Protect a file, replace it on disk, protect the new one: one entry, not two."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, old_version = await _seed_movie(db)
                await create_protection_request(
                    CreateProtectionRequest(
                        media_type=MediaType.MOVIE,
                        media_id=movie.id,
                        movie_version_id=old_version.id,
                        reason="keep this cut",
                    ),
                    admin,
                    db,
                )
                assert (await db.scalar(select(func.count(ProtectedMedia.id)))) == 1

                # the file is replaced by a different release: a new service media
                # id AND different codec/size, so the rename fingerprint deliberately
                # misses and the old row is pruned rather than updated in place
                await _upsert_movie_versions(
                    db,
                    movie,
                    [
                        _version_data(
                            "media-new",
                            size=20_000,
                            video_codec="hevc",
                            video_resolution="2160p",
                            video_width=3840,
                            video_height=2160,
                        )
                    ],
                )
                await db.commit()

                versions = (await db.execute(select(MovieVersion))).scalars().all()
                assert len(versions) == 1
                assert versions[0].service_media_id == "media-new"

                # the protection for the file that is gone went with it, and
                # nothing is left pointing at a row that no longer exists
                assert (await db.scalar(select(func.count(ProtectedMedia.id)))) == 0
                dangling = await db.scalar(
                    select(func.count(ProtectedMedia.id)).where(
                        ProtectedMedia.movie_version_id.is_not(None),
                        ProtectedMedia.movie_version_id.not_in(select(MovieVersion.id)),
                    )
                )
                assert dangling == 0

                # protecting the replacement leaves exactly one entry
                await create_protection_request(
                    CreateProtectionRequest(
                        media_type=MediaType.MOVIE,
                        media_id=movie.id,
                        movie_version_id=versions[0].id,
                        reason="keep the upgrade",
                    ),
                    admin,
                    db,
                )
                assert (await db.scalar(select(func.count(ProtectedMedia.id)))) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pruning_a_version_clears_its_candidate_and_pending_request() -> None:
    """Everything scoped to the departing file goes with it, not just protections."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, old_version = await _seed_movie(db)
                db.add(
                    ReclaimCandidate(
                        media_type=MediaType.MOVIE,
                        matched_rule_ids=[1],
                        matched_criteria={},
                        reason="stale",
                        reason_data=[],
                        movie_id=movie.id,
                        movie_version_id=old_version.id,
                        estimated_space_bytes=10_000,
                    )
                )
                pending = ProtectionRequest(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=old_version.id,
                    requested_by_user_id=admin.id,
                    reason="pending",
                )
                decided = ProtectionRequest(
                    media_type=MediaType.MOVIE,
                    movie_id=movie.id,
                    movie_version_id=old_version.id,
                    requested_by_user_id=admin.id,
                    reason="already denied",
                )
                decided.status = ProtectionRequestStatus.DENIED
                db.add_all([pending, decided])
                await db.commit()

                await _upsert_movie_versions(
                    db,
                    movie,
                    [_version_data("media-new", size=20_000, video_codec="hevc")],
                )
                await db.commit()

                assert (await db.scalar(select(func.count(ReclaimCandidate.id)))) == 0
                assert await db.get(ProtectionRequest, pending.id) is None
                # a decided request is history worth keeping, just detached
                surviving = await db.get(ProtectionRequest, decided.id)
                assert surviving is not None
                await db.refresh(surviving)
                assert surviving.movie_version_id is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_renamed_file_keeps_its_protection() -> None:
    """The fingerprint fallback still rescues a pure rename - nothing is dropped."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, old_version = await _seed_movie(db)
                await create_protection_request(
                    CreateProtectionRequest(
                        media_type=MediaType.MOVIE,
                        media_id=movie.id,
                        movie_version_id=old_version.id,
                        reason="keep",
                    ),
                    admin,
                    db,
                )

                # same physical file, new service media id (a Jellyfin/Emby rename)
                await _upsert_movie_versions(
                    db, movie, [_version_data("media-renamed")]
                )
                await db.commit()

                protection = (await db.execute(select(ProtectedMedia))).scalar_one()
                version = (await db.execute(select(MovieVersion))).scalar_one()
                assert protection.movie_version_id == version.id
                assert version.service_media_id == "media-renamed"
        finally:
            await engine.dispose()

    asyncio.run(run())


# --- movie scope overlap ---------------------------------------------------


def test_whole_movie_protection_blocks_a_version_protection() -> None:
    """A whole-movie protection already covers every version of that movie."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, version = await _seed_movie(db)
                await create_protection_entry(
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE, media_id=movie.id
                    ),
                    admin,
                    db,
                )
                with pytest.raises(HTTPException) as excinfo:
                    await create_protection_entry(
                        CreateProtectedEntryRequest(
                            media_type=MediaType.MOVIE,
                            media_id=movie.id,
                            movie_version_id=version.id,
                        ),
                        admin,
                        db,
                    )
                assert excinfo.value.status_code == 400
                assert (await db.scalar(select(func.count(ProtectedMedia.id)))) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_version_protection_does_not_block_the_whole_movie() -> None:
    """Widening from one file to the whole movie stays allowed, as it is for series."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, version = await _seed_movie(db)
                await create_protection_entry(
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE,
                        media_id=movie.id,
                        movie_version_id=version.id,
                    ),
                    admin,
                    db,
                )
                await create_protection_entry(
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE, media_id=movie.id
                    ),
                    admin,
                    db,
                )
                assert (await db.scalar(select(func.count(ProtectedMedia.id)))) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_expired_protection_neither_blocks_nor_is_listed() -> None:
    """An expired row protects nothing, so it must not block or show up."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, version = await _seed_movie(db)
                expired = ProtectedMedia(media_type=MediaType.MOVIE)
                expired.movie_id = movie.id
                expired.movie_version_id = version.id
                expired.source = "manual"
                expired.permanent = False
                expired.expires_at = datetime.now(UTC) - timedelta(days=1)
                db.add(expired)
                await db.commit()

                listed = await _list_protected(admin, db)
                assert listed.total == 0
                assert listed.items == []

                await create_protection_entry(
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE,
                        media_id=movie.id,
                        movie_version_id=version.id,
                    ),
                    admin,
                    db,
                )
                refreshed = await _list_protected(admin, db)
                assert refreshed.total == 1
                assert len(refreshed.items) == refreshed.total
        finally:
            await engine.dispose()

    asyncio.run(run())


# --- approving two requests for the same target ----------------------------


def test_two_approved_requests_yield_one_protection() -> None:
    """Two people can hold a pending request for the same title; approving both
    must widen one protection rather than create a second."""

    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, version = await _seed_movie(db)
                other = _admin_user("second")
                db.add(other)
                await db.flush()

                requests = []
                for user in (admin, other):
                    request = ProtectionRequest(
                        media_type=MediaType.MOVIE,
                        movie_id=movie.id,
                        movie_version_id=version.id,
                        requested_by_user_id=user.id,
                        reason=f"{user.username} wants this kept",
                    )
                    db.add(request)
                    requests.append(request)
                await db.commit()

                await approve_request(
                    requests[0].id, ReviewProtectionRequest(approved_permanent=True), admin, db
                )
                # the second approval must succeed - refusing it would strand a
                # legitimate request in PENDING forever
                await approve_request(
                    requests[1].id,
                    ReviewProtectionRequest(approved_duration_days=7),
                    admin,
                    db,
                )

                for request in requests:
                    await db.refresh(request)
                    assert request.status is ProtectionRequestStatus.APPROVED

                protection = (await db.execute(select(ProtectedMedia))).scalar_one()
                # protection only ever widens: permanent is not downgraded to 7 days
                assert protection.permanent is True
                assert protection.expires_at is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# --- the file a protection covers is named on the page ---------------------


def test_protected_entries_report_the_file_they_cover() -> None:
    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, version = await _seed_movie(db)
                await create_protection_entry(
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE,
                        media_id=movie.id,
                        movie_version_id=version.id,
                    ),
                    admin,
                    db,
                )
                entry = (await _list_protected(admin, db)).items[0]
                assert entry.version_file_name == "media-old.mkv"
                assert entry.version_resolution == "1080p"
                assert entry.version_video_codec == "h264"
                assert entry.version_size == 10_000
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_whole_movie_protection_reports_no_version_details() -> None:
    async def run() -> None:
        engine, session_maker = await _make_session()
        try:
            async with session_maker() as db:
                admin, movie, _version = await _seed_movie(db)
                await create_protection_entry(
                    CreateProtectedEntryRequest(
                        media_type=MediaType.MOVIE, media_id=movie.id
                    ),
                    admin,
                    db,
                )
                entry = (await _list_protected(admin, db)).items[0]
                assert entry.movie_version_id is None
                assert entry.version_file_name is None
                assert entry.version_size is None
        finally:
            await engine.dispose()

    asyncio.run(run())
