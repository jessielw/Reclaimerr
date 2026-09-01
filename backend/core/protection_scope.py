from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.database.models import (
    DeleteRequest,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
)
from backend.enums import MediaType, ProtectionRequestStatus

__all__ = [
    "ScopedModel",
    "active_protection_clause",
    "detach_movie_version_references",
    "movie_scope_overlap_clause",
    "series_scope_overlap_clause",
    "upsert_protection",
]

# every table that carries the same movie_version_id/season_id/episode_id scope columns
ScopedModel = (
    type[ProtectedMedia]
    | type[ProtectionRequest]
    | type[DeleteRequest]
    | type[ReclaimCandidate]
)


def movie_scope_overlap_clause(
    model: ScopedModel,
    *,
    movie_version_id: int | None,
) -> ColumnElement[bool]:
    """Match rows whose movie scope overlaps the given one.

    Mirrors :func:`series_scope_overlap_clause`: a whole-movie row covers every
    version of that movie, so it overlaps a version-scoped target, while a
    version-scoped row covers only its own file and so does not stand in the way
    of protecting the whole movie.
    """
    if movie_version_id is not None:
        return or_(
            model.movie_version_id.is_(None),
            model.movie_version_id == movie_version_id,
        )
    return model.movie_version_id.is_(None)


def series_scope_overlap_clause(
    model: ScopedModel,
    *,
    season_id: int | None,
    episode_id: int | None,
) -> ColumnElement[bool]:
    """Match rows whose season/episode scope overlaps the given one."""
    if episode_id is not None:
        return or_(
            and_(model.season_id.is_(None), model.episode_id.is_(None)),
            and_(model.season_id == season_id, model.episode_id.is_(None)),
            model.episode_id == episode_id,
        )
    if season_id is not None:
        return or_(
            and_(model.season_id.is_(None), model.episode_id.is_(None)),
            and_(model.season_id == season_id, model.episode_id.is_(None)),
        )
    return and_(model.season_id.is_(None), model.episode_id.is_(None))


def active_protection_clause(now: datetime) -> ColumnElement[bool]:
    """Match protections that are still in force at ``now``.

    An expired row protects nothing -- every enforcement query already ignores
    it -- so it must not be listed as protection, nor stand in the way of
    protecting the same target again.
    """
    return or_(
        ProtectedMedia.permanent.is_(True),
        ProtectedMedia.expires_at.is_(None),
        ProtectedMedia.expires_at > now,
    )


async def upsert_protection(
    db: AsyncSession,
    *,
    media_type: MediaType,
    movie_id: int | None,
    movie_version_id: int | None,
    series_id: int | None,
    season_id: int | None,
    episode_id: int | None,
    reason: str | None,
    protected_by_user_id: int | None,
    permanent: bool,
    expires_at: datetime | None,
) -> tuple[ProtectedMedia, bool]:
    """Protect a target, reusing an active protection that already covers it.

    Returns the row and whether it was newly created. Approving a request must
    never fail because someone else's request for the same target was approved
    first -- that would strand a legitimate request in PENDING with no way out --
    so an existing protection is extended rather than duplicated. Protection only
    ever widens here: permanent wins, and an expiry only moves later.
    """
    query = select(ProtectedMedia).where(
        ProtectedMedia.source != "rule",
        ProtectedMedia.media_type == media_type,
        active_protection_clause(datetime.now(UTC)),
    )
    if media_type is MediaType.MOVIE:
        query = query.where(
            ProtectedMedia.movie_id == movie_id,
            movie_scope_overlap_clause(
                ProtectedMedia, movie_version_id=movie_version_id
            ),
        )
    else:
        query = query.where(
            ProtectedMedia.series_id == series_id,
            series_scope_overlap_clause(
                ProtectedMedia, season_id=season_id, episode_id=episode_id
            ),
        )

    existing = (await db.execute(query.limit(1))).scalar_one_or_none()
    if existing is not None:
        if permanent and not existing.permanent:
            existing.permanent = True
            existing.expires_at = None
        elif (
            not existing.permanent
            and expires_at is not None
            and (existing.expires_at is None or expires_at > existing.expires_at)
        ):
            existing.expires_at = expires_at
        return existing, False

    entry = ProtectedMedia(
        media_type=media_type,
        movie_id=movie_id,
        movie_version_id=movie_version_id,
        series_id=series_id,
        season_id=season_id,
        episode_id=episode_id,
        reason=reason,
        protected_by_user_id=protected_by_user_id,
        permanent=permanent,
        expires_at=expires_at,
    )
    db.add(entry)
    return entry, True


async def detach_movie_version_references(
    session: AsyncSession,
    version_ids: Collection[int],
) -> None:
    """Clear everything scoped to movie version rows that are about to be deleted.

    The schema declares ``ondelete="SET NULL"`` on these foreign keys, but
    ``PRAGMA foreign_keys`` is only turned on for request-scoped sessions, so a
    background task that deletes a version would otherwise leave dangling ids
    behind -- rows that protect nothing yet still occupy the Protected page and
    collide with protecting the file that replaced them.

    A per-version protection or candidate covers one physical file and dies with
    it. Requests still awaiting a decision go the same way; decided ones are
    history worth keeping, so they are only detached.
    """
    ids = list(version_ids)
    if not ids:
        return

    await session.execute(
        delete(ReclaimCandidate).where(ReclaimCandidate.movie_version_id.in_(ids))
    )
    await session.execute(
        delete(ProtectedMedia).where(ProtectedMedia.movie_version_id.in_(ids))
    )
    for request_model in (ProtectionRequest, DeleteRequest):
        await session.execute(
            delete(request_model).where(
                request_model.movie_version_id.in_(ids),
                request_model.status == ProtectionRequestStatus.PENDING,
            )
        )
        await session.execute(
            update(request_model)
            .where(
                request_model.movie_version_id.in_(ids),
                request_model.status != ProtectionRequestStatus.PENDING,
            )
            .values(movie_version_id=None)
        )
