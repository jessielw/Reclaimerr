from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.auto_delete import resolve_auto_delete_policy
from backend.core.utils.datetime_utils import ensure_utc
from backend.database.models import (
    DeleteRequest,
    Episode,
    GeneralSettings,
    LifecycleEvent,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ProtectionRequest,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
    WebhookDelivery,
    WebhookEndpoint,
)
from backend.enums import MediaType, ProtectionRequestStatus


def candidate_scope(
    candidate: ReclaimCandidate,
) -> Literal["movie", "version", "series", "season", "episode"]:
    if candidate.media_type is MediaType.MOVIE:
        return "version" if candidate.movie_version_id is not None else "movie"
    if candidate.episode_id is not None:
        return "episode"
    if candidate.season_id is not None:
        return "season"
    return "series"


async def _rule_context(
    db: AsyncSession, candidate: ReclaimCandidate
) -> tuple[dict[int, dict[str, Any] | None], int, int]:
    settings = (await db.execute(select(GeneralSettings))).scalars().first()
    movie_delay = settings.auto_delete_movie_delay_days if settings else 14
    series_delay = settings.auto_delete_series_delay_days if settings else 7
    rule_ids = list(candidate.matched_rule_ids or [])
    rules = (
        (await db.execute(select(ReclaimRule).where(ReclaimRule.id.in_(rule_ids))))
        .scalars()
        .all()
        if rule_ids
        else []
    )
    return {rule.id: rule.action for rule in rules}, movie_delay, series_delay


async def candidate_deletion_blockers(
    db: AsyncSession, candidate: ReclaimCandidate
) -> list[str]:
    now = datetime.now(UTC)
    active_protection = or_(
        ProtectedMedia.permanent.is_(True),
        ProtectedMedia.expires_at.is_(None),
        ProtectedMedia.expires_at > now,
    )
    if candidate.media_type is MediaType.MOVIE:
        protection_scope = and_(
            ProtectedMedia.movie_id == candidate.movie_id,
            or_(
                ProtectedMedia.movie_version_id.is_(None),
                ProtectedMedia.movie_version_id == candidate.movie_version_id,
            ),
        )
        request_scope = and_(
            ProtectionRequest.movie_id == candidate.movie_id,
            or_(
                ProtectionRequest.movie_version_id.is_(None),
                ProtectionRequest.movie_version_id == candidate.movie_version_id,
            ),
        )
        delete_scope = and_(
            DeleteRequest.movie_id == candidate.movie_id,
            or_(
                DeleteRequest.movie_version_id.is_(None),
                DeleteRequest.movie_version_id == candidate.movie_version_id,
            ),
        )
    else:

        def series_scope(model: Any) -> Any:
            whole_series = and_(model.season_id.is_(None), model.episode_id.is_(None))
            if candidate.episode_id is not None:
                overlap = or_(
                    whole_series,
                    and_(
                        model.season_id == candidate.season_id,
                        model.episode_id.is_(None),
                    ),
                    model.episode_id == candidate.episode_id,
                )
            elif candidate.season_id is not None:
                overlap = or_(whole_series, model.season_id == candidate.season_id)
            else:
                # Any protected/requested child prevents deleting the whole series.
                overlap = model.series_id == candidate.series_id
            return and_(model.series_id == candidate.series_id, overlap)

        protection_scope = series_scope(ProtectedMedia)
        request_scope = series_scope(ProtectionRequest)
        delete_scope = series_scope(DeleteRequest)

    blockers: list[str] = []
    protected = (
        await db.execute(
            select(ProtectedMedia.id)
            .where(active_protection, protection_scope)
            .limit(1)
        )
    ).scalar_one_or_none()
    if protected is not None:
        blockers.append("protected")
    pending_protection = (
        await db.execute(
            select(ProtectionRequest.id)
            .where(
                ProtectionRequest.status == ProtectionRequestStatus.PENDING,
                request_scope,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending_protection is not None:
        blockers.append("pending_protection_request")
    pending_delete = (
        await db.execute(
            select(DeleteRequest.id)
            .where(
                DeleteRequest.status == ProtectionRequestStatus.PENDING,
                delete_scope,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending_delete is not None:
        blockers.append("pending_delete_request")
    return blockers


async def candidate_status_payload(
    db: AsyncSession, candidate: ReclaimCandidate
) -> dict[str, Any]:
    actions, movie_delay, series_delay = await _rule_context(db, candidate)
    policy = resolve_auto_delete_policy(
        media_type=candidate.media_type,
        matched_rule_ids=candidate.matched_rule_ids,
        created_at=candidate.created_at,
        timer_started_at=candidate.auto_delete_timer_started_at,
        postponed_until=candidate.auto_delete_postponed_until,
        cancelled_at=candidate.auto_delete_cancelled_at,
        rule_actions_by_id=actions,
        movie_delay_days=movie_delay,
        series_delay_days=series_delay,
    )
    movie = await db.get(Movie, candidate.movie_id) if candidate.movie_id else None
    series = await db.get(Series, candidate.series_id) if candidate.series_id else None
    season = await db.get(Season, candidate.season_id) if candidate.season_id else None
    episode = (
        await db.get(Episode, candidate.episode_id) if candidate.episode_id else None
    )
    media = movie or series
    title = media.title if media else "Unknown media"
    if episode is not None:
        title = (
            f"{title} S{season.season_number:02d}E{episode.episode_number:02d}"
            if season
            else title
        )
    elif season is not None:
        title = f"{title} S{season.season_number:02d}"
    operation: Literal["delete", "move"] = "delete"
    if any(
        (actions.get(rule_id) or {}).get("move_instead_of_delete") is True
        for rule_id in candidate.matched_rule_ids
    ):
        operation = "move"
    return {
        "id": candidate.id,
        "media_type": candidate.media_type,
        "scope": candidate_scope(candidate),
        "media_id": candidate.movie_id or candidate.series_id or 0,
        "movie_version_id": candidate.movie_version_id,
        "series_id": candidate.series_id,
        "season_id": candidate.season_id,
        "season_number": season.season_number if season else None,
        "episode_id": candidate.episode_id,
        "episode_number": episode.episode_number if episode else None,
        "title": title,
        "year": media.year if media else None,
        "tmdb_id": media.tmdb_id if media else None,
        "matched_rule_ids": list(candidate.matched_rule_ids),
        "reason": candidate.reason,
        "delete_operation": operation,
        "created_at": ensure_utc(candidate.created_at),
        "auto_delete_state": policy.state,
        "auto_delete_delay_days": policy.delay_days,
        "auto_delete_eligible_at": policy.eligible_at,
        "auto_delete_is_active": policy.is_enabled,
        "auto_delete_is_eligible": policy.is_eligible,
        "auto_delete_cancelled_at": candidate.auto_delete_cancelled_at,
        "auto_delete_postponed_until": candidate.auto_delete_postponed_until,
        "auto_delete_timer_started_at": candidate.auto_delete_timer_started_at,
        "lifecycle_reason": candidate.lifecycle_reason,
        "lifecycle_updated_at": candidate.lifecycle_updated_at,
        "blockers": await candidate_deletion_blockers(db, candidate),
    }


async def record_candidate_event(
    db: AsyncSession,
    candidate: ReclaimCandidate,
    event_type: str,
    *,
    actor_api_token_id: int | None = None,
    actor_user_id: int | None = None,
    idempotency_key: str | None = None,
) -> LifecycleEvent:
    if actor_api_token_id is not None and idempotency_key:
        existing = (
            await db.execute(
                select(LifecycleEvent).where(
                    LifecycleEvent.actor_api_token_id == actor_api_token_id,
                    LifecycleEvent.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    now = datetime.now(UTC)
    public_event_id = str(uuid4())
    candidate_payload = jsonable_encoder(await candidate_status_payload(db, candidate))
    payload = {
        "schema_version": 1,
        "id": public_event_id,
        "type": event_type,
        "occurred_at": now.isoformat(),
        "candidate": candidate_payload,
        "links": {"self": f"/api/v1/candidates/{candidate.id}"},
    }
    event = LifecycleEvent(
        event_id=public_event_id,
        event_type=event_type,
        payload=payload,
        candidate_id=candidate.id,
        actor_api_token_id=actor_api_token_id,
        actor_user_id=actor_user_id,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    await db.flush()
    endpoints = (
        (
            await db.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.enabled.is_(True),
                    WebhookEndpoint.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for endpoint in endpoints:
        if event_type not in endpoint.event_types:
            continue
        if (
            endpoint.media_types
            and candidate.media_type.value not in endpoint.media_types
        ):
            continue
        db.add(
            WebhookDelivery(
                event_id=event.id,
                endpoint_id=endpoint.id,
                next_attempt_at=now,
            )
        )
    await db.flush()
    return event


async def reconcile_candidate_schedule_events(db: AsyncSession) -> list[int]:
    """Record one scheduled event per continuous transition into auto deletion."""
    candidates = (await db.execute(select(ReclaimCandidate))).scalars().all()
    settings = (await db.execute(select(GeneralSettings))).scalars().first()
    movie_delay = settings.auto_delete_movie_delay_days if settings else 14
    series_delay = settings.auto_delete_series_delay_days if settings else 7
    rule_ids = {
        rule_id
        for candidate in candidates
        for rule_id in (candidate.matched_rule_ids or [])
    }
    rules = (
        (await db.execute(select(ReclaimRule).where(ReclaimRule.id.in_(rule_ids))))
        .scalars()
        .all()
        if rule_ids
        else []
    )
    actions = {rule.id: rule.action for rule in rules}
    event_ids: list[int] = []
    for candidate in candidates:
        policy = resolve_auto_delete_policy(
            media_type=candidate.media_type,
            matched_rule_ids=candidate.matched_rule_ids,
            created_at=candidate.created_at,
            timer_started_at=candidate.auto_delete_timer_started_at,
            postponed_until=candidate.auto_delete_postponed_until,
            cancelled_at=candidate.auto_delete_cancelled_at,
            rule_actions_by_id=actions,
            movie_delay_days=movie_delay,
            series_delay_days=series_delay,
        )
        if policy.is_enabled and candidate.auto_delete_announced_at is None:
            candidate.auto_delete_announced_at = datetime.now(UTC)
            event = await record_candidate_event(db, candidate, "candidate.scheduled")
            event_ids.append(event.id)
        elif not policy.is_configured:
            candidate.auto_delete_announced_at = None
    return event_ids
