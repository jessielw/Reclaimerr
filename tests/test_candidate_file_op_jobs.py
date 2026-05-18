from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.routes.background_jobs import (
    list_candidate_file_op_jobs,
    list_history,
)
from backend.api.routes.delete_requests import approve_delete_request
from backend.api.routes.media import delete_candidates
from backend.database import Base
from backend.database.models import (
    BackgroundJob,
    DeleteRequest,
    Movie,
    ReclaimCandidate,
    User,
)
from backend.enums import (
    BackgroundJobStatus,
    BackgroundJobType,
    CandidateFileOpOperation,
    MediaType,
    Permission,
    ProtectionRequestStatus,
    UserRole,
)
from backend.jobs.candidate_file_ops import run_candidate_file_op_job
from backend.models.jobs import CandidateFileOpJobItem, CandidateFileOpJobPayload
from backend.models.media import DeleteCandidatesRequest
from backend.models.requests import ReviewDeleteRequest


def _user(
    *,
    username: str,
    role: UserRole = UserRole.USER,
    permissions: list[str] | None = None,
) -> User:
    return User(
        username=username,
        password_hash="x",
        role=role,
        permissions=permissions or [],
    )


def _patch_job_sessions(monkeypatch, session_maker) -> None:
    monkeypatch.setattr("backend.jobs.queue.async_db", session_maker)
    monkeypatch.setattr("backend.jobs.candidate_file_ops.async_db", session_maker)


def test_delete_candidates_queues_background_job(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        _patch_job_sessions(monkeypatch, session_maker)

        async with session_maker() as db_session:
            manager = _user(
                username="manager",
                permissions=[Permission.MANAGE_RECLAIM.value],
            )
            movie = Movie(title="Queued Movie", tmdb_id=101, year=2001, size=123)
            db_session.add_all([manager, movie])
            await db_session.flush()
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="cleanup",
                reason_data=[],
                movie_id=movie.id,
                estimated_space_bytes=123,
            )
            db_session.add(candidate)
            await db_session.commit()

            response = await delete_candidates(
                DeleteCandidatesRequest(candidate_ids=[candidate.id]),
                manager,
                db_session,
            )

            assert response.status == "queued"
            assert response.job_id is not None

            queued_job = (
                await db_session.execute(
                    select(BackgroundJob).where(BackgroundJob.id == response.job_id)
                )
            ).scalar_one()
            assert queued_job.job_type is BackgroundJobType.CANDIDATE_FILE_OP
            assert queued_job.payload["operation"] == "delete"
            assert queued_job.payload["candidate_ids"] == [candidate.id]
            assert queued_job.payload["requested_by_username"] == "manager"
            assert queued_job.payload["item_labels"] == ["Queued Movie (2001)"]
            assert queued_job.payload["item_label_total"] == 1
            assert (
                queued_job.payload["item_details"][0]["display_label"]
                == "Queued Movie (2001)"
            )
            assert queued_job.payload["item_details"][0]["candidate_id"] == candidate.id
            assert queued_job.payload["progress"] == {
                "total_items": 1,
                "completed_items": 0,
                "failed_items": 0,
                "current_item_label": None,
                "percent": 0,
            }

        await engine.dispose()

    asyncio.run(run())


def test_list_candidate_file_op_jobs_only_returns_visible_jobs(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            viewer = _user(username="viewer")
            other = _user(username="other")
            admin = _user(username="admin", role=UserRole.ADMIN)
            db_session.add_all([viewer, other, admin])
            await db_session.flush()

            db_session.add_all(
                [
                    BackgroundJob(
                        job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                        scheduled_at=datetime.now(UTC),
                        status=BackgroundJobStatus.PENDING,
                        payload={
                            "operation": "delete",
                            "candidate_ids": [1],
                            "requested_by_user_id": viewer.id,
                            "requested_by_username": viewer.username,
                            "item_labels": ["Viewer Movie"],
                            "item_label_total": 1,
                        },
                    ),
                    BackgroundJob(
                        job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                        scheduled_at=datetime.now(UTC),
                        status=BackgroundJobStatus.PENDING,
                        payload={
                            "operation": "move",
                            "candidate_ids": [2, 3],
                            "requested_by_user_id": other.id,
                            "requested_by_username": other.username,
                            "item_labels": ["Other Movie", "Other Show"],
                            "item_label_total": 2,
                        },
                    ),
                    BackgroundJob(
                        job_type=BackgroundJobType.TASK_RUN,
                        scheduled_at=datetime.now(UTC),
                        status=BackgroundJobStatus.PENDING,
                        payload={"task": "sync_media"},
                    ),
                ]
            )
            await db_session.commit()

            visible_to_viewer = await list_candidate_file_op_jobs(
                viewer, db_session, 10
            )
            assert [
                job["payload"]["requested_by_username"]
                for job in visible_to_viewer["jobs"]
            ] == ["viewer"]
            assert "Viewer Movie" in visible_to_viewer["jobs"][0]["summary"]

            visible_to_admin = await list_candidate_file_op_jobs(admin, db_session, 10)
            assert len(visible_to_admin["jobs"]) == 2

        await engine.dispose()

    asyncio.run(run())


def test_list_history_scopes_jobs_by_role() -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

        async with session_maker() as db_session:
            admin = _user(username="admin", role=UserRole.ADMIN)
            manager = _user(
                username="manager",
                permissions=[Permission.MANAGE_RECLAIM.value],
            )
            viewer = _user(username="viewer")
            db_session.add_all([admin, manager, viewer])
            await db_session.flush()

            db_session.add_all(
                [
                    BackgroundJob(
                        job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                        scheduled_at=datetime.now(UTC),
                        status=BackgroundJobStatus.PENDING,
                        payload={
                            "operation": "delete",
                            "candidate_ids": [1],
                            "requested_by_user_id": manager.id,
                            "requested_by_username": manager.username,
                            "item_labels": ["Managed Movie"],
                            "item_label_total": 1,
                        },
                    ),
                    BackgroundJob(
                        job_type=BackgroundJobType.TASK_RUN,
                        scheduled_at=datetime.now(UTC),
                        status=BackgroundJobStatus.COMPLETED,
                        payload={"task": "sync_media"},
                    ),
                    BackgroundJob(
                        job_type=BackgroundJobType.SERVICE_TOGGLE,
                        scheduled_at=datetime.now(UTC),
                        status=BackgroundJobStatus.RUNNING,
                        payload={"service_type": "plex", "enabled": True},
                    ),
                ]
            )
            await db_session.commit()

            manager_response = await list_history(
                manager,
                db_session,
                page=1,
                per_page=25,
                job_status=None,
                job_type=None,
            )
            assert manager_response["total"] == 1
            assert (
                manager_response["items"][0]["job_type"]
                == BackgroundJobType.CANDIDATE_FILE_OP
            )

            admin_response = await list_history(
                admin,
                db_session,
                page=1,
                per_page=25,
                job_status=None,
                job_type=None,
            )
            assert admin_response["total"] == 3

            viewer_response = await list_history(
                viewer,
                db_session,
                page=1,
                per_page=25,
                job_status=None,
                job_type=None,
            )
            assert viewer_response["total"] == 0
            assert viewer_response["items"] == []

        await engine.dispose()

    asyncio.run(run())


def test_approve_delete_request_queues_execution(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        _patch_job_sessions(monkeypatch, session_maker)
        monkeypatch.setattr(
            "backend.api.routes.delete_requests.notify_user", AsyncMock()
        )

        async with session_maker() as db_session:
            requester = _user(username="requester")
            manager = _user(
                username="manager",
                role=UserRole.ADMIN,
                permissions=[Permission.MANAGE_REQUESTS.value],
            )
            movie = Movie(title="Queued Delete", tmdb_id=321, year=2005, size=222)
            db_session.add_all([requester, manager, movie])
            await db_session.flush()

            delete_request = DeleteRequest(
                media_type=MediaType.MOVIE,
                requested_by_user_id=requester.id,
                reason="cleanup",
                movie_id=movie.id,
            )
            db_session.add(delete_request)
            await db_session.commit()

            response = await approve_delete_request(
                delete_request.id,
                ReviewDeleteRequest(admin_notes="approved"),
                manager,
                db_session,
            )

            assert response.status is ProtectionRequestStatus.APPROVED
            assert response.executed_at is None
            assert response.execution_error is None

            queued_job = (
                await db_session.execute(
                    select(BackgroundJob).where(
                        BackgroundJob.job_type == BackgroundJobType.CANDIDATE_FILE_OP
                    )
                )
            ).scalar_one()
            assert queued_job.payload["delete_request_id"] == delete_request.id
            assert queued_job.payload["operation"] == "delete"
            assert queued_job.payload["item_labels"] == ["Queued Delete (2005)"]
            assert (
                queued_job.payload["item_details"][0]["display_label"]
                == "Queued Delete (2005)"
            )
            assert queued_job.payload["progress"]["total_items"] == 1

            candidate = (
                await db_session.execute(select(ReclaimCandidate))
            ).scalar_one()
            assert queued_job.payload["item_details"][0]["candidate_id"] == candidate.id
            assert candidate.movie_id == movie.id
            assert candidate.approved_for_deletion is True

        await engine.dispose()

    asyncio.run(run())


def test_run_candidate_file_op_job_marks_delete_request_executed(monkeypatch) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_maker = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        _patch_job_sessions(monkeypatch, session_maker)
        monkeypatch.setattr(
            "backend.jobs.candidate_file_ops.delete_specific_candidates",
            AsyncMock(return_value=(1, 0)),
        )

        async with session_maker() as db_session:
            requester = _user(username="requester")
            movie = Movie(title="Executed Delete", tmdb_id=654, year=2007, size=333)
            db_session.add_all([requester, movie])
            await db_session.flush()

            delete_request = DeleteRequest(
                media_type=MediaType.MOVIE,
                requested_by_user_id=requester.id,
                reason="cleanup",
                movie_id=movie.id,
                status=ProtectionRequestStatus.APPROVED,
            )
            candidate = ReclaimCandidate(
                media_type=MediaType.MOVIE,
                matched_rule_ids=[1],
                matched_criteria={},
                reason="approved delete",
                reason_data=[],
                movie_id=movie.id,
                approved_for_deletion=True,
            )
            db_session.add_all([delete_request, candidate])
            job = BackgroundJob(
                job_type=BackgroundJobType.CANDIDATE_FILE_OP,
                scheduled_at=datetime.now(UTC),
                status=BackgroundJobStatus.RUNNING,
                payload={},
            )
            db_session.add(job)
            await db_session.commit()

            result = await run_candidate_file_op_job(
                job.id,
                CandidateFileOpJobPayload(
                    operation=CandidateFileOpOperation.DELETE,
                    candidate_ids=[candidate.id],
                    requested_by_user_id=requester.id,
                    requested_by_username=requester.username,
                    delete_request_id=delete_request.id,
                    item_labels=["Executed Delete (2007)"],
                    item_label_total=1,
                    item_details=[
                        CandidateFileOpJobItem(
                            candidate_id=candidate.id,
                            media_type=MediaType.MOVIE,
                            scope="movie",
                            title=movie.title,
                            year=movie.year,
                            tmdb_id=movie.tmdb_id,
                            display_label="Executed Delete (2007)",
                        )
                    ],
                ),
            )

            assert result["succeeded"] == 1
            refreshed_job = await db_session.get(BackgroundJob, job.id)
            assert refreshed_job is not None
            await db_session.refresh(refreshed_job)
            assert refreshed_job.payload["progress"] == {
                "total_items": 1,
                "completed_items": 1,
                "failed_items": 0,
                "current_item_label": None,
                "percent": 100,
            }

            refreshed_request = await db_session.get(DeleteRequest, delete_request.id)
            assert refreshed_request is not None
            await db_session.refresh(refreshed_request)
            assert refreshed_request.executed_at is not None
            assert refreshed_request.execution_error is None

        await engine.dispose()

    asyncio.run(run())
