from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from backend.api.routes.v1.candidates import (
    cancel_candidate_deletion,
    get_candidate,
    list_candidates,
    permanently_protect_candidate,
    postpone_candidate_deletion,
    reset_candidate_timer,
)
from backend.core.api_tokens import (
    API_TOKEN_CANDIDATES_MANAGE_SCOPE,
    API_TOKEN_CANDIDATES_READ_SCOPE,
    ApiPrincipal,
    generate_api_token,
    get_api_principal,
)
from backend.database import Base
from backend.database.models import (
    ApiToken,
    Episode,
    GeneralSettings,
    LifecycleEvent,
    Movie,
    MovieVersion,
    ProtectedMedia,
    ReclaimCandidate,
    ReclaimRule,
    Season,
    Series,
)
from backend.enums import MediaType, Service
from backend.models.api_v1 import (
    CandidateActionRequest,
    CandidatePostponeRequest,
)
from backend.services.candidate_lifecycle import record_candidate_event


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
            assert canceled.candidate.size_bytes == 100

            replayed = await cancel_candidate_deletion(
                candidate.id,
                CandidateActionRequest(reason="Ignored replay"),
                principal,
                db,
                "cancel-1",
            )
            assert replayed.replayed is True
            assert replayed.event_id == canceled.event_id
            assert replayed.candidate.size_bytes == 100

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
            events = (await db.scalars(select(LifecycleEvent))).all()
            assert all(
                event.payload["candidate"]["size_bytes"] == 100 for event in events
            )

        await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["movie", "version", "series", "season", "episode"])
@pytest.mark.parametrize(
    "size_source", ["estimate", "media", "zero_estimate", "zero_media", "unknown"]
)
def test_external_candidate_sizes_match_scope(scope: str, size_source: str) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        principal = ApiPrincipal(
            token_id=1,
            name="Reader",
            scopes=frozenset({API_TOKEN_CANDIDATES_READ_SCOPE}),
        )
        async with session_maker() as db:
            movie = Movie(title="Multiple versions", tmdb_id=550, size=60 * 1024**3)
            series = Series(title="Multiple seasons", tmdb_id=1399, size=100 * 1024**3)
            db.add_all([movie, series])
            await db.flush()
            version = MovieVersion(
                movie_id=movie.id,
                service=Service.PLEX,
                service_item_id="movie",
                service_media_id="version",
                library_id="1",
                library_name="Movies",
                size=10 * 1024**3,
            )
            season = Season(series_id=series.id, season_number=1, size=30 * 1024**3)
            db.add_all([version, season])
            await db.flush()
            episode = Episode(season_id=season.id, episode_number=1, size=4 * 1024**3)
            db.add(episode)
            await db.flush()

            target = {
                "movie": movie,
                "version": version,
                "series": series,
                "season": season,
                "episode": episode,
            }[scope]
            expected_size = target.size
            estimate = None
            if size_source == "estimate":
                estimate = expected_size = 3 * 1024**3
            elif size_source == "zero_estimate":
                estimate = expected_size = 0
            elif size_source == "zero_media":
                target.size = expected_size = 0
            elif size_source == "unknown":
                expected_size = None
                if scope != "version":
                    target.size = None

            is_movie = scope in {"movie", "version"}
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE if is_movie else MediaType.SERIES,
                movie_id=movie.id if is_movie else None,
                movie_version_id=(
                    version.id + 1000 if size_source == "unknown" else version.id
                )
                if scope == "version"
                else None,
                series_id=series.id if not is_movie else None,
                season_id=season.id if scope in {"season", "episode"} else None,
                episode_id=episode.id if scope == "episode" else None,
                matched_rule_ids=[],
                matched_criteria={},
                reason="Size test",
                estimated_space_bytes=estimate,
            )
            db.add(candidate)
            await db.commit()

            response = await list_candidates(
                principal,
                db,
                page=1,
                per_page=50,
                media_type=None,
                tmdb_id=None,
                series_id=None,
                season_number=None,
                episode_number=None,
                auto_delete_state=None,
                rule_id=None,
            )
            assert response.total == 1
            assert response.items[0].scope == scope
            assert (
                response.model_dump(mode="json")["items"][0]["size_bytes"]
                == expected_size
            )
            detail = await get_candidate(candidate.id, principal, db)
            assert detail.model_dump(mode="json")["size_bytes"] == expected_size
            event = await record_candidate_event(db, candidate, "candidate.scheduled")
            assert event.payload["candidate"]["size_bytes"] == expected_size

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


def test_external_candidate_list_filters_by_exact_rule_id() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        principal = ApiPrincipal(
            token_id=1,
            name="Reader",
            scopes=frozenset({API_TOKEN_CANDIDATES_READ_SCOPE}),
        )
        async with session_maker() as db:
            movies = [
                Movie(title="Rule Six", tmdb_id=601, size=100),
                Movie(title="Rule Sixteen", tmdb_id=1601, size=100),
            ]
            db.add_all(movies)
            await db.flush()
            candidates = [
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movies[0].id,
                    matched_rule_ids=[6, 8],
                    matched_criteria={},
                    reason="Rule Six: matched",
                ),
                ReclaimCandidate(
                    media_type=MediaType.MOVIE,
                    movie_id=movies[1].id,
                    matched_rule_ids=[16],
                    matched_criteria={},
                    reason="Rule Sixteen: matched",
                ),
            ]
            db.add_all(candidates)
            await db.commit()

            response = await list_candidates(
                principal,
                db,
                page=1,
                per_page=50,
                media_type=None,
                tmdb_id=None,
                series_id=None,
                season_number=None,
                episode_number=None,
                auto_delete_state=None,
                rule_id=6,
            )

            assert response.total == 1
            assert [item.id for item in response.items] == [candidates[0].id]
            assert response.items[0].matched_rule_ids == [6, 8]
        await engine.dispose()

    asyncio.run(run())
