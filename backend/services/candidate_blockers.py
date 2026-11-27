from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from backend.core.protection_scope import ScopedModel, active_protection_clause
from backend.database.models import (
    CandidateResyncIdentity,
    DeleteRequest,
    Episode,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    Season,
)
from backend.enums import MediaType, ProtectionRequestStatus
from backend.models.cleanup import (
    CandidateDeletionBlocker,
    CandidateDeletionBlockerCode,
    CandidateScope,
)

UNAVAILABLE_RULE_IDS_KEY = "_unavailable_rule_ids"


class CandidateBlockers:
    def __init__(self, resync_ids: set[int]) -> None:
        self.resync_ids = resync_ids
        self.exact: dict[tuple[CandidateScope, int], list[CandidateDeletionBlocker]] = (
            defaultdict(list)
        )
        self.descendants: dict[
            tuple[CandidateScope, int], list[CandidateDeletionBlocker]
        ] = defaultdict(list)

    def overlapping(
        self, candidate: ReclaimCandidate, *, for_auto_delete: bool = True
    ) -> list[CandidateDeletionBlocker]:
        # Each child inherits exact ancestor holds. Descendant holds apply only
        # when the operation deletes their entire parent.
        holds: list[CandidateDeletionBlocker] = []
        if candidate.media_type is MediaType.MOVIE:
            if candidate.movie_id is not None:
                holds += self.exact.get(("movie", candidate.movie_id), [])
                # Automatic deletion already holds all versions of this movie.
                if candidate.movie_version_id is None or for_auto_delete:
                    holds += self.descendants.get(("movie", candidate.movie_id), [])
            if candidate.movie_version_id is not None:
                holds += [
                    hold
                    for hold in self.exact.get(
                        ("version", candidate.movie_version_id), []
                    )
                    if hold not in holds
                ]
            return holds

        if candidate.series_id is not None:
            holds += self.exact.get(("series", candidate.series_id), [])
        if candidate.season_id is not None:
            holds += self.exact.get(("season", candidate.season_id), [])
        if candidate.episode_id is not None:
            holds += self.exact.get(("episode", candidate.episode_id), [])
        elif candidate.season_id is not None:
            holds += self.descendants.get(("season", candidate.season_id), [])
        elif candidate.series_id is not None:
            holds += self.descendants.get(("series", candidate.series_id), [])
        return holds

    def candidate_hold(
        self, candidate: ReclaimCandidate
    ) -> CandidateDeletionBlocker | None:
        if candidate.delete_request_id is not None:
            return CandidateDeletionBlocker(code="pending_delete_request")
        if candidate.id in self.resync_ids:
            return CandidateDeletionBlocker(code="resync_pending")
        criteria = candidate.matched_criteria or {}
        if criteria.get(UNAVAILABLE_RULE_IDS_KEY):
            return CandidateDeletionBlocker(code="rule_data_unavailable")
        if criteria.get("scheduled_deletion_id"):
            return CandidateDeletionBlocker(code="scheduled_deletion")
        return None


async def load_candidate_blockers(
    db: AsyncSession,
    now: datetime | None = None,
    *,
    candidate: ReclaimCandidate | None = None,
) -> CandidateBlockers:
    resync_query = select(CandidateResyncIdentity.candidate_id)
    if candidate is not None:
        resync_query = resync_query.where(
            CandidateResyncIdentity.candidate_id == candidate.id
        )
    index = CandidateBlockers(set(await db.scalars(resync_query)))
    sources: tuple[
        tuple[CandidateDeletionBlockerCode, ScopedModel, ColumnElement[bool]], ...
    ] = (
        (
            "active_protection",
            ProtectedMedia,
            active_protection_clause(now or datetime.now(UTC)),
        ),
        (
            "pending_protection_request",
            ProtectionRequest,
            ProtectionRequest.status == ProtectionRequestStatus.PENDING,
        ),
        (
            "pending_delete_request",
            DeleteRequest,
            DeleteRequest.status == ProtectionRequestStatus.PENDING,
        ),
    )
    for code, model, condition in sources:
        movie_parent = func.coalesce(MovieVersion.movie_id, model.movie_id)
        series_parent = func.coalesce(Season.series_id, model.series_id)
        season_parent = func.coalesce(Episode.season_id, model.season_id)
        query = (
            select(
                model.media_type,
                movie_parent,
                model.movie_version_id,
                series_parent,
                season_parent,
                model.episode_id,
                Season.season_number,
                Episode.episode_number,
            )
            .select_from(model)
            .outerjoin(MovieVersion, MovieVersion.id == model.movie_version_id)
            .outerjoin(Episode, Episode.id == model.episode_id)
            .outerjoin(Season, Season.id == season_parent)
            .where(condition)
        )
        if candidate is not None:
            targets_for_candidate = (
                (
                    (movie_parent, candidate.movie_id),
                    (model.movie_version_id, candidate.movie_version_id),
                )
                if candidate.media_type is MediaType.MOVIE
                else (
                    (series_parent, candidate.series_id),
                    (season_parent, candidate.season_id),
                    (model.episode_id, candidate.episode_id),
                )
            )
            matches = [
                column == target_id
                for column, target_id in targets_for_candidate
                if target_id is not None
            ]
            if not matches:
                continue
            query = query.where(model.media_type == candidate.media_type, or_(*matches))
        rows = await db.execute(query.distinct())
        for (
            media_type,
            movie_id,
            version_id,
            series_id,
            season_id,
            episode_id,
            season_number,
            episode_number,
        ) in rows.tuples():
            if media_type is MediaType.MOVIE:
                targets: tuple[tuple[CandidateScope, int | None], ...] = (
                    ("version", version_id),
                    ("movie", movie_id),
                )
            else:
                targets = (
                    ("episode", episode_id),
                    ("season", season_id),
                    ("series", series_id),
                )
            keys: list[tuple[CandidateScope, int]] = [
                (scope, target_id)
                for scope, target_id in targets
                if target_id is not None
            ]
            if not keys:
                continue
            scope, target_id = keys[0]
            blocker = CandidateDeletionBlocker(
                code=code,
                scope=scope,
                target_id=target_id,
                season_number=season_number,
                episode_number=episode_number,
            )
            index.exact[(scope, target_id)].append(blocker)
            for key in keys[1:]:
                index.descendants[key].append(blocker)
    return index
