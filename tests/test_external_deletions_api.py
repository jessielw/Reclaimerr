from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_delete_request_scheduling import seed_request
from test_scheduled_deletions import database, due_job, seed

from backend.api.routes.v1 import router
from backend.core.api_tokens import generate_api_token
from backend.database import get_db
from backend.database.models import (
    ApiToken,
    BackgroundJob,
    MovieVersion,
    ReclaimCandidate,
    ScheduledDeletion,
)
from backend.enums import BackgroundJobStatus, Service
from backend.jobs import candidate_file_ops
from backend.models.jobs import CandidateFileOpJobPayload
from backend.services import delete_request_workflow, schedule_workflow

WHEN = "2099-06-01T12:30:00-04:00"
REQUESTS = "/api/v1/delete-requests"
SCHEDULES = "/api/v1/scheduled-deletions"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@asynccontextmanager
async def api(monkeypatch) -> AsyncIterator:
    monkeypatch.setattr(schedule_workflow, "request_leaving_soon_sync", lambda: None)
    monkeypatch.setattr(delete_request_workflow, "notify_user", AsyncMock())
    monkeypatch.setattr(candidate_file_ops, "notify_user", AsyncMock())
    monkeypatch.setattr(candidate_file_ops, "notify_admins", AsyncMock())
    async with database(monkeypatch) as factory:
        app = FastAPI()
        app.include_router(router)

        async def session():
            async with factory() as db:
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

        app.dependency_overrides[get_db] = session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, factory


async def token(db, *scopes, name="Integration", **kwargs):
    secret, prefix, digest = generate_api_token()
    row = ApiToken(
        name=name, token_prefix=prefix, token_hash=digest, scopes=list(scopes), **kwargs
    )
    db.add(row)
    await db.flush()
    return row, {"Authorization": f"Bearer {secret}"}


@pytest.mark.anyio
async def test_deletion_routes_require_bearer_and_matching_scope(monkeypatch):
    async with api(monkeypatch) as (client, factory), factory() as db:
        _, read = await token(db, "delete_requests:read", "scheduled_deletions:read")
        _, unrelated = await token(db, "candidates:manage")
        _, expired = await token(
            db, "delete_requests:manage", expires_at=datetime.now(UTC)
        )
        _, revoked = await token(
            db, "scheduled_deletions:manage", revoked_at=datetime.now(UTC)
        )
        await db.commit()
        routes = [
            ("GET", REQUESTS, None),
            ("GET", f"{REQUESTS}/1", None),
            ("GET", f"{REQUESTS}/approval-options", None),
            ("POST", f"{REQUESTS}/1/approve", {}),
            ("POST", f"{REQUESTS}/1/deny", {}),
            ("POST", f"{REQUESTS}/1/retry", None),
            ("GET", SCHEDULES, None),
            ("GET", f"{SCHEDULES}/1", None),
            (
                "POST",
                SCHEDULES,
                {"media_type": "movie", "media_id": 1, "scheduled_at": WHEN},
            ),
            ("POST", f"{SCHEDULES}/1/reschedule", {"scheduled_at": WHEN}),
            ("POST", f"{SCHEDULES}/1/cancel", None),
        ]
        for method, url, body in routes:
            for headers, expected in (
                ({}, 401),
                (unrelated, 403),
                (expired, 401),
                (revoked, 401),
            ):
                response = await client.request(method, url, headers=headers, json=body)
                assert response.status_code == expected, (url, response.text)
            if method == "POST":
                assert (
                    await client.post(url, headers=read, json=body)
                ).status_code == 403


@pytest.mark.anyio
async def test_token_lookup_schedules_exact_movie_version_and_recovers_after_revocation(
    monkeypatch,
):
    async with api(monkeypatch) as (client, factory), factory() as db:
        _, movie, first, _, _ = await seed(db)
        first.video_resolution = "1080p"
        second = MovieVersion(
            movie_id=movie.id,
            service=Service.PLEX,
            service_item_id="movie",
            service_media_id="4k",
            library_id="movies",
            library_name="Movies",
            path="/movies/4k.mkv",
            size=0,
            video_resolution="4K",
        )
        db.add(second)
        original_token, original = await token(
            db, "media:read", "scheduled_deletions:manage"
        )
        replacement, manager = await token(
            db, "scheduled_deletions:manage", name="Replacement"
        )
        _, request_manager = await token(db, "delete_requests:manage")
        await db.commit()

        listed = await client.get(
            f"/api/v1/movies?tmdb_id={movie.tmdb_id}", headers=original
        )
        detail = await client.get(f"/api/v1/movies/{movie.id}", headers=original)
        versions = detail.json()["versions"]
        assert listed.json()["items"][0]["versions"] == versions
        assert [version["id"] for version in versions] == [first.id, second.id]
        assert versions[1] == {
            "id": second.id,
            "path": "/movies/4k.mkv",
            "service_item_id": "movie",
            "service_media_id": "4k",
            "size_bytes": 0,
            "resolution": "4K",
        }

        body = {
            "media_type": "movie",
            "media_id": movie.id,
            "movie_version_id": second.id,
            "scheduled_at": WHEN,
        }
        stale = await client.post(
            SCHEDULES, headers=original, json={**body, "movie_version_id": 9999}
        )
        assert stale.status_code == 409
        created = await client.post(SCHEDULES, headers=original, json=body)
        assert created.status_code == 201, created.text
        saved = created.json()
        assert saved["target"]["movie_version_id"] == second.id
        assert saved["scheduled_at"] == "2099-06-01T16:30:00Z"
        assert {"inventory", "routing_settings", "version_path"}.isdisjoint(
            saved["target"]
        )
        url = f"{SCHEDULES}/{saved['id']}"
        assert (
            await client.post(f"{url}/cancel", headers=request_manager)
        ).status_code == 403
        assert (
            await client.post(SCHEDULES, headers=original, json=body)
        ).status_code == 409

        executor = AsyncMock(return_value=(1, 0))
        monkeypatch.setattr(candidate_file_ops, "delete_specific_candidates", executor)
        original_token.revoked_at = datetime.now(UTC)
        await db.commit()
        old_job = await due_job(db, saved["id"])
        result = await candidate_file_ops.run_candidate_file_op_job(
            old_job.id, CandidateFileOpJobPayload.model_validate(old_job.payload)
        )
        assert result["failed"] == 1
        executor.assert_not_awaited()
        blocked = (await client.get(url, headers=manager)).json()
        assert blocked["state"] == "blocked"
        assert "revoked" in blocked["last_error"]

        recovered = await client.post(
            f"{url}/reschedule", headers=manager, json={"scheduled_at": WHEN}
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["created_by_api_token_id"] == replacement.id
        job = await due_job(db, saved["id"])

        async def delete_file(candidate_ids, **_):
            async with factory() as read_db:
                candidate = await read_db.get(ReclaimCandidate, candidate_ids[0])
                assert candidate.movie_version_id == second.id
            return 1, 0

        executor.side_effect = delete_file
        result = await candidate_file_ops.run_candidate_file_op_job(
            job.id, CandidateFileOpJobPayload.model_validate(job.payload)
        )
        assert result["succeeded"] == 1
        assert (await client.get(SCHEDULES, headers=manager)).json()["total"] == 0
        assert (
            await client.get(f"{SCHEDULES}?active_only=false", headers=manager)
        ).json()["total"] == 1


@pytest.mark.anyio
async def test_request_manager_can_review_and_manage_linked_schedule(monkeypatch):
    async with api(monkeypatch) as (client, factory), factory() as db:
        user, request, _, _ = await seed_request(db, days=3)
        reviewer, reviews = await token(db, "delete_requests:manage")
        _, schedules = await token(db, "scheduled_deletions:manage")
        await db.commit()
        detail = f"{REQUESTS}/{request.id}"
        page = await client.get(
            f"{REQUESTS}?status=pending&per_page=1", headers=reviews
        )
        assert page.json()["total"] == page.json()["total_pages"] == 1
        assert page.json()["items"][0]["requested_by_user_id"] == user.id
        options = await client.get(f"{REQUESTS}/approval-options", headers=reviews)
        assert options.json()["delete_request_grace_period_days"] == 3

        approved = await client.post(f"{detail}/approve", headers=reviews, json={})
        assert approved.status_code == 200, approved.text
        response = approved.json()
        assert response["reviewed_by_api_token_id"] == reviewer.id
        assert response["reviewed_by_api_token_name"] == reviewer.name
        assert response["reviewed_by_user_id"] is None
        assert response["schedule_state"] == "scheduled"
        schedule_id = response["scheduled_deletion_id"]
        url = f"{SCHEDULES}/{schedule_id}"
        assert (
            await client.post(f"{detail}/retry", headers=reviews)
        ).status_code == 409
        reschedule = {"scheduled_at": WHEN}
        assert (
            await client.post(f"{url}/reschedule", headers=schedules, json=reschedule)
        ).status_code == 403
        assert (
            await client.post(f"{url}/cancel", headers=schedules)
        ).status_code == 403
        changed = await client.post(
            f"{url}/reschedule", headers=reviews, json=reschedule
        )
        assert changed.status_code == 200
        assert changed.json()["delete_request_id"] == request.id
        assert (await client.post(f"{url}/cancel", headers=reviews)).status_code == 200
        updated = await client.get(detail, headers=reviews)
        assert updated.json()["schedule_state"] == "canceled"


@pytest.mark.anyio
async def test_expired_immediate_approval_cannot_execute_and_can_be_retried(
    monkeypatch,
):
    async with api(monkeypatch) as (client, factory), factory() as db:
        _, request, _, _ = await seed_request(db, days=3)
        reviewer, approval = await token(db, "delete_requests:manage")
        retrier, retry = await token(db, "delete_requests:manage", name="Retry token")
        await db.commit()
        url = f"{REQUESTS}/{request.id}"
        approved = await client.post(
            f"{url}/approve", headers=approval, json={"scheduled_at": None}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["scheduled_deletion_id"] is None
        job = await db.scalar(select(BackgroundJob))
        assert job.payload["requested_by_api_token_id"] == reviewer.id
        assert job.payload["requested_by_user_id"] is None

        reviewer.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        executor = AsyncMock()
        monkeypatch.setattr(candidate_file_ops, "delete_specific_candidates", executor)
        result = await candidate_file_ops.run_candidate_file_op_job(
            job.id, CandidateFileOpJobPayload.model_validate(job.payload)
        )
        assert result["failed"] == 1
        executor.assert_not_awaited()
        assert (
            "expired"
            in (await client.get(url, headers=retry)).json()["execution_error"]
        )
        job.status = BackgroundJobStatus.FAILED
        await db.commit()

        retried = await client.post(f"{url}/retry", headers=retry)
        assert retried.status_code == 200, retried.text
        assert retried.json()["execution_error"] is None
        assert retried.json()["reviewed_by_api_token_id"] == reviewer.id
        new_job = await db.scalar(
            select(BackgroundJob).order_by(BackgroundJob.id.desc())
        )
        assert new_job.payload["requested_by_api_token_id"] == retrier.id
        assert (await client.post(f"{url}/retry", headers=retry)).status_code == 409
        assert await db.scalar(select(ScheduledDeletion)) is None


@pytest.mark.anyio
async def test_immediate_approval_and_queue_commit_together(monkeypatch):
    async with api(monkeypatch) as (client, factory), factory() as db:
        _, request, _, _ = await seed_request(db)
        reviewer, headers = await token(db, "delete_requests:manage")
        await db.commit()
        url = f"{REQUESTS}/{request.id}"
        monkeypatch.setattr(
            delete_request_workflow,
            "queue_candidate_file_op_job",
            AsyncMock(side_effect=RuntimeError("queue write failed")),
        )
        with pytest.raises(RuntimeError, match="queue write failed"):
            await client.post(f"{url}/approve", headers=headers, json={})
        pending = (await client.get(url, headers=headers)).json()
        assert pending["status"] == "pending"
        assert pending["reviewed_by_api_token_id"] is None
        assert await db.scalar(select(BackgroundJob)) is None
        assert (
            await db.scalar(
                select(ReclaimCandidate).where(
                    ReclaimCandidate.delete_request_id == request.id
                )
            )
            is None
        )

        denied = await client.post(
            f"{url}/deny", headers=headers, json={"admin_notes": "Keep this"}
        )
        assert denied.status_code == 200, denied.text
        assert denied.json()["status"] == "denied"
        assert denied.json()["reviewed_by_api_token_id"] == reviewer.id
        assert denied.json()["admin_notes"] == "Keep this"


@pytest.mark.anyio
async def test_queued_request_cannot_broaden_when_its_movie_version_disappears(
    monkeypatch,
):
    async with api(monkeypatch) as (client, factory), factory() as db:
        _, request, _, _ = await seed_request(db)
        _, headers = await token(db, "delete_requests:manage")
        await db.commit()
        url = f"{REQUESTS}/{request.id}"
        approved = await client.post(f"{url}/approve", headers=headers, json={})
        assert approved.status_code == 200, approved.text
        job = await db.scalar(select(BackgroundJob))
        version = await db.get(MovieVersion, request.movie_version_id)
        await db.delete(version)
        await db.commit()

        executor = AsyncMock()
        monkeypatch.setattr(candidate_file_ops, "delete_specific_candidates", executor)
        result = await candidate_file_ops.run_candidate_file_op_job(
            job.id, CandidateFileOpJobPayload.model_validate(job.payload)
        )
        assert result["failed"] == 1
        executor.assert_not_awaited()
        detail = (await client.get(url, headers=headers)).json()
        assert detail["target_available"] is False
        assert detail["executed_at"] is None
