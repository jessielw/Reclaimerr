from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from backend.api.routes.v1.candidates import (
    cancel_candidate_deletion,
    permanently_protect_candidate,
    postpone_candidate_deletion,
    reset_candidate_timer,
)
from backend.core.api_tokens import (
    API_TOKEN_CANDIDATES_MANAGE_SCOPE,
    generate_api_token,
    get_api_principal,
)
from backend.database import Base
from backend.database.models import (
    ApiToken,
    GeneralSettings,
    LifecycleEvent,
    Movie,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
)
from backend.enums import MediaType
from backend.models.api_v1 import (
    CandidateActionRequest,
    CandidatePostponeRequest,
)


def test_api_token_authentication_and_candidate_lifecycle(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        plaintext, prefix, digest = generate_api_token()
        async with session_maker() as db:
            movie = Movie(title="API Movie", tmdb_id=550, size=100)
            rule = ReclaimRule(
                name="API cleanup",
                media_type=MediaType.MOVIE,
                enabled=True,
                target_scope="movie_version",
                definition=None,
                action={"auto_delete_enabled": True, "auto_delete_delay_days": 14},
            )
            token = ApiToken(
                name="Automation",
                token_prefix=prefix,
                token_hash=digest,
                scopes=[API_TOKEN_CANDIDATES_MANAGE_SCOPE],
            )
            db.add_all(
                [
                    GeneralSettings(
                        auto_delete_movie_delay_days=14,
                        auto_delete_series_delay_days=7,
                    ),
                    movie,
                    rule,
                    token,
                ]
            )
            await db.flush()
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                movie_id=movie.id,
                matched_rule_ids=[rule.id],
                matched_criteria={},
                reason="Matched API cleanup",
            )
            db.add(candidate)
            await db.commit()

            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/v1/candidates",
                    "headers": [],
                }
            )
            principal = await get_api_principal(
                request,
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=plaintext),
                db,
            )
            assert principal.token_id == token.id
            assert principal.has_scope("candidates:read") is True

            enqueue_mock = AsyncMock()
            monkeypatch.setattr(
                "backend.api.routes.v1.candidates.enqueue_event_deliveries",
                enqueue_mock,
            )

            canceled = await cancel_candidate_deletion(
                candidate.id,
                CandidateActionRequest(reason="Requester chose keep"),
                principal,
                db,
                "cancel-1",
            )
            assert canceled.candidate.auto_delete_state == "canceled"
            assert canceled.candidate.auto_delete_is_active is False

            replayed = await cancel_candidate_deletion(
                candidate.id,
                CandidateActionRequest(reason="Ignored replay"),
                principal,
                db,
                "cancel-1",
            )
            assert replayed.replayed is True
            assert replayed.event_id == canceled.event_id

            postponed = await postpone_candidate_deletion(
                candidate.id,
                CandidatePostponeRequest(
                    reason="Give the requester more time",
                    until=datetime.now(UTC) + timedelta(days=30),
                ),
                principal,
                db,
                "postpone-1",
            )
            assert postponed.candidate.auto_delete_state == "postponed"
            assert postponed.candidate.auto_delete_is_active is True

            reset = await reset_candidate_timer(
                candidate.id,
                CandidateActionRequest(reason="Restart review period"),
                principal,
                db,
                "reset-1",
            )
            assert reset.candidate.auto_delete_state == "scheduled"
            assert reset.candidate.auto_delete_timer_started_at is not None
            assert reset.candidate.auto_delete_postponed_until is None

            protected = await permanently_protect_candidate(
                candidate.id,
                CandidateActionRequest(reason="Keep permanently"),
                principal,
                db,
                "protect-1",
            )
            assert protected.protection_id is not None
            assert "protected" in protected.candidate.blockers

            protection_count = await db.scalar(
                select(func.count()).select_from(ProtectedMedia)
            )
            event_count = await db.scalar(
                select(func.count()).select_from(LifecycleEvent)
            )
            assert protection_count == 1
            assert event_count == 4
            assert enqueue_mock.await_count == 4

        await engine.dispose()

    asyncio.run(run())


def test_cancelled_candidate_policy_survives_rule_reconciliation() -> None:
    from backend.core.auto_delete import resolve_auto_delete_policy

    now = datetime.now(UTC)
    policy = resolve_auto_delete_policy(
        media_type=MediaType.MOVIE,
        matched_rule_ids=[1],
        created_at=now - timedelta(days=30),
        cancelled_at=now - timedelta(days=1),
        rule_actions_by_id={1: {"auto_delete_enabled": True}},
        movie_delay_days=14,
        series_delay_days=7,
        now=now,
    )
    assert policy.is_configured is True
    assert policy.is_enabled is False
    assert policy.is_eligible is False
    assert policy.state == "canceled"
