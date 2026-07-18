from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routes.v1.dependencies import (
    candidate_or_404,
    ensure_no_active_file_operation,
)
from backend.core.api_tokens import (
    API_TOKEN_CANDIDATES_MANAGE_SCOPE,
    API_TOKEN_CANDIDATES_READ_SCOPE,
    ApiPrincipal,
    require_api_scope,
)
from backend.core.utils.datetime_utils import ensure_utc
from backend.core.workflow_locks import candidate_workflow_lock
from backend.database import get_db
from backend.database.models import (
    Episode,
    LifecycleEvent,
    Movie,
    ProtectedMedia,
    ReclaimCandidate,
    Season,
    Series,
)
from backend.enums import MediaType
from backend.models.api_v1 import (
    CandidateActionRequest,
    CandidateActionResponse,
    CandidateListResponse,
    CandidatePostponeRequest,
    CandidateStatusResponse,
)
from backend.models.api_v1.common import total_pages
from backend.services.candidate_lifecycle import (
    candidate_status_payload,
    record_candidate_event,
)
from backend.services.lifecycle_webhooks import enqueue_event_deliveries

router = APIRouter(tags=["v1:candidates"])


async def _idempotent_response(
    db: AsyncSession,
    principal: ApiPrincipal,
    idempotency_key: str | None,
) -> CandidateActionResponse | None:
    if not idempotency_key:
        return None
    event = (
        await db.execute(
            select(LifecycleEvent).where(
                LifecycleEvent.actor_api_token_id == principal.token_id,
                LifecycleEvent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if event is None or event.response_payload is None:
        return None
    payload = dict(event.response_payload)
    payload["replayed"] = True
    return CandidateActionResponse.model_validate(payload)


async def _finish_action(
    db: AsyncSession,
    candidate: ReclaimCandidate,
    event_type: str,
    principal: ApiPrincipal,
    idempotency_key: str | None,
    *,
    protection_id: int | None = None,
) -> CandidateActionResponse:
    event = await record_candidate_event(
        db,
        candidate,
        event_type,
        actor_api_token_id=principal.token_id,
        idempotency_key=idempotency_key,
    )
    response = CandidateActionResponse(
        candidate=CandidateStatusResponse.model_validate(
            await candidate_status_payload(db, candidate)
        ),
        event_id=event.event_id,
        protection_id=protection_id,
    )
    event.response_payload = jsonable_encoder(response)
    await db.commit()
    await enqueue_event_deliveries(event.id)
    return response


@router.get("/candidates", response_model=CandidateListResponse)
async def list_candidates(
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
    media_type: MediaType | None = None,
    tmdb_id: int | None = Query(default=None, ge=1),
    series_id: int | None = Query(default=None, ge=1),
    season_number: int | None = Query(default=None, ge=0),
    episode_number: int | None = Query(default=None, ge=0),
    auto_delete_state: str | None = Query(
        default=None,
        pattern="^(disabled|scheduled|eligible|postponed|canceled)$",
    ),
) -> CandidateListResponse:
    query = (
        select(ReclaimCandidate)
        .outerjoin(Movie, ReclaimCandidate.movie_id == Movie.id)
        .outerjoin(Series, ReclaimCandidate.series_id == Series.id)
        .outerjoin(Season, ReclaimCandidate.season_id == Season.id)
        .outerjoin(Episode, ReclaimCandidate.episode_id == Episode.id)
        .order_by(ReclaimCandidate.created_at.desc(), ReclaimCandidate.id.desc())
    )
    if media_type is not None:
        query = query.where(ReclaimCandidate.media_type == media_type)
    if tmdb_id is not None:
        query = query.where(or_(Movie.tmdb_id == tmdb_id, Series.tmdb_id == tmdb_id))
    if series_id is not None:
        query = query.where(ReclaimCandidate.series_id == series_id)
    if season_number is not None:
        query = query.where(Season.season_number == season_number)
    if episode_number is not None:
        query = query.where(Episode.episode_number == episode_number)

    candidates = (await db.execute(query)).scalars().unique().all()
    payloads: list[dict[str, Any]] = []
    for candidate in candidates:
        payload = await candidate_status_payload(db, candidate)
        if (
            auto_delete_state is None
            or payload["auto_delete_state"] == auto_delete_state
        ):
            payloads.append(payload)
    total = len(payloads)
    start = (page - 1) * per_page
    page_items = payloads[start : start + per_page]
    return CandidateListResponse(
        items=[CandidateStatusResponse.model_validate(item) for item in page_items],
        total=total,
        page=page,
        per_page=per_page,
        total_pages=total_pages(total, per_page),
    )


@router.get("/candidates/{candidate_id}", response_model=CandidateStatusResponse)
async def get_candidate(
    candidate_id: int,
    _principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_READ_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CandidateStatusResponse:
    candidate = await candidate_or_404(db, candidate_id)
    return CandidateStatusResponse.model_validate(
        await candidate_status_payload(db, candidate)
    )


@router.post(
    "/candidates/{candidate_id}/cancel", response_model=CandidateActionResponse
)
async def cancel_candidate_deletion(
    candidate_id: int,
    request: CandidateActionRequest,
    principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_MANAGE_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(max_length=120)] = None,
) -> CandidateActionResponse:
    async with candidate_workflow_lock:
        replay = await _idempotent_response(db, principal, idempotency_key)
        if replay is not None:
            return replay
        candidate = await candidate_or_404(db, candidate_id)
        await ensure_no_active_file_operation(db, candidate_id)
        now = datetime.now(UTC)
        if candidate.auto_delete_cancelled_at is None:
            candidate.auto_delete_cancelled_at = now
        candidate.lifecycle_reason = request.reason
        candidate.lifecycle_updated_at = now
        candidate.lifecycle_updated_by_api_token_id = principal.token_id
        return await _finish_action(
            db,
            candidate,
            "candidate.canceled",
            principal,
            idempotency_key,
        )


@router.post(
    "/candidates/{candidate_id}/postpone", response_model=CandidateActionResponse
)
async def postpone_candidate_deletion(
    candidate_id: int,
    request: CandidatePostponeRequest,
    principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_MANAGE_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(max_length=120)] = None,
) -> CandidateActionResponse:
    until = ensure_utc(request.until)
    if until <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Postponement timestamp must be in the future",
        )
    async with candidate_workflow_lock:
        replay = await _idempotent_response(db, principal, idempotency_key)
        if replay is not None:
            return replay
        candidate = await candidate_or_404(db, candidate_id)
        await ensure_no_active_file_operation(db, candidate_id)
        current_status = await candidate_status_payload(db, candidate)
        current_deadline = current_status["auto_delete_eligible_at"]
        if isinstance(current_deadline, datetime) and until <= ensure_utc(
            current_deadline
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Postponement must be later than the current deletion deadline",
            )
        now = datetime.now(UTC)
        candidate.auto_delete_cancelled_at = None
        candidate.auto_delete_postponed_until = until
        candidate.lifecycle_reason = request.reason
        candidate.lifecycle_updated_at = now
        candidate.lifecycle_updated_by_api_token_id = principal.token_id
        return await _finish_action(
            db,
            candidate,
            "candidate.postponed",
            principal,
            idempotency_key,
        )


@router.post(
    "/candidates/{candidate_id}/reset-timer",
    response_model=CandidateActionResponse,
)
async def reset_candidate_timer(
    candidate_id: int,
    request: CandidateActionRequest,
    principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_MANAGE_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(max_length=120)] = None,
) -> CandidateActionResponse:
    async with candidate_workflow_lock:
        replay = await _idempotent_response(db, principal, idempotency_key)
        if replay is not None:
            return replay
        candidate = await candidate_or_404(db, candidate_id)
        await ensure_no_active_file_operation(db, candidate_id)
        now = datetime.now(UTC)
        candidate.auto_delete_cancelled_at = None
        candidate.auto_delete_postponed_until = None
        candidate.auto_delete_timer_started_at = now
        candidate.lifecycle_reason = request.reason
        candidate.lifecycle_updated_at = now
        candidate.lifecycle_updated_by_api_token_id = principal.token_id
        return await _finish_action(
            db,
            candidate,
            "candidate.timer_reset",
            principal,
            idempotency_key,
        )


@router.post(
    "/candidates/{candidate_id}/protect", response_model=CandidateActionResponse
)
async def permanently_protect_candidate(
    candidate_id: int,
    request: CandidateActionRequest,
    principal: Annotated[
        ApiPrincipal, Depends(require_api_scope(API_TOKEN_CANDIDATES_MANAGE_SCOPE))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(max_length=120)] = None,
) -> CandidateActionResponse:
    async with candidate_workflow_lock:
        replay = await _idempotent_response(db, principal, idempotency_key)
        if replay is not None:
            return replay
        candidate = await candidate_or_404(db, candidate_id)
        await ensure_no_active_file_operation(db, candidate_id)
        query = select(ProtectedMedia).where(
            ProtectedMedia.media_type == candidate.media_type,
            ProtectedMedia.permanent.is_(True),
        )
        if candidate.media_type is MediaType.MOVIE:
            query = query.where(
                ProtectedMedia.movie_id == candidate.movie_id,
                or_(
                    ProtectedMedia.movie_version_id.is_(None),
                    ProtectedMedia.movie_version_id == candidate.movie_version_id,
                ),
            )
        else:
            query = query.where(
                ProtectedMedia.series_id == candidate.series_id,
                or_(
                    ProtectedMedia.season_id.is_(None),
                    ProtectedMedia.season_id == candidate.season_id,
                ),
                or_(
                    ProtectedMedia.episode_id.is_(None),
                    ProtectedMedia.episode_id == candidate.episode_id,
                ),
            )
        protection = (await db.execute(query.limit(1))).scalar_one_or_none()
        if protection is None:
            protection = ProtectedMedia(
                media_type=candidate.media_type,
                protected_by_user_id=None,
                protected_by_api_token_id=principal.token_id,
                movie_id=candidate.movie_id,
                movie_version_id=candidate.movie_version_id,
                series_id=candidate.series_id,
                season_id=candidate.season_id,
                episode_id=candidate.episode_id,
                source="manual",
                reason=request.reason
                or f"Protected through API token {principal.name}",
                permanent=True,
                expires_at=None,
            )
            db.add(protection)
            await db.flush()
        now = datetime.now(UTC)
        candidate.lifecycle_reason = request.reason
        candidate.lifecycle_updated_at = now
        candidate.lifecycle_updated_by_api_token_id = principal.token_id
        return await _finish_action(
            db,
            candidate,
            "candidate.protected",
            principal,
            idempotency_key,
            protection_id=protection.id,
        )
