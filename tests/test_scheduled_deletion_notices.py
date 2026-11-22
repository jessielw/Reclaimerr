"""The person who scheduled a deletion directly is told how it went."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_scheduled_deletions import (
    database,
    due_job,
    media_request,
    schedule_media_deletion,
    seed,
)

from backend.database.models import ScheduledDeletion
from backend.enums import NotificationType
from backend.jobs import candidate_file_ops
from backend.models.jobs import CandidateFileOpJobPayload
from backend.services import scheduled_deletion_notices as notices


async def _run_due(db, schedule_id, monkeypatch, *, fails=False):
    job = await due_job(db, schedule_id)
    delete_files = (
        AsyncMock(side_effect=RuntimeError("disk is read only"))
        if fails
        else AsyncMock(return_value=(1, 0))
    )
    monkeypatch.setattr(candidate_file_ops, "delete_specific_candidates", delete_files)
    await candidate_file_ops.run_candidate_file_op_job(
        job.id, CandidateFileOpJobPayload.model_validate(job.payload)
    )


@pytest.mark.parametrize("fails", [False, True])
def test_the_person_who_scheduled_a_deletion_is_told_how_it_went(monkeypatch, fails):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            monkeypatch.setattr(notices, "async_db", factory)
            notify = AsyncMock()
            monkeypatch.setattr(notices, "notify_user", notify)
            monkeypatch.setattr(candidate_file_ops, "notify_user", AsyncMock())
            monkeypatch.setattr(candidate_file_ops, "notify_admins", AsyncMock())
            user, movie, version, candidate, _ = await seed(db)
            await db.delete(candidate)
            await db.commit()
            saved = (
                await schedule_media_deletion(media_request(movie, version), user, db)
            )[0]

            await _run_due(db, saved.id, monkeypatch, fails=fails)

            notify.assert_awaited_once()
            args, kwargs = notify.await_args
            assert args[0] == user.id
            expected = (
                NotificationType.DELETE_REQUEST_EXECUTION_FAILED
                if fails
                else NotificationType.DELETE_REQUEST_EXECUTION_SUCCEEDED
            )
            assert args[1] is expected
            context = kwargs["context"]
            assert context["media_title"] == "Scheduled movie"
            assert context["media_year"] == 2020
            assert context["request_type"] == "Deletion"
            assert (context["error"] is not None) == fails

    asyncio.run(run())


def test_a_deletion_with_a_request_is_not_notified_twice(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            monkeypatch.setattr(notices, "async_db", factory)
            notify = AsyncMock()
            monkeypatch.setattr(notices, "notify_user", notify)
            user, _, _, _, _ = await seed(db)

            def row(**extra):
                return ScheduledDeletion(
                    scheduled_at=datetime.now(UTC),
                    target={"title": "T", "media_type": "movie", "scope": "movie"},
                    created_by_username="x",
                    state="completed",
                    **extra,
                )

            db.add_all([row(created_by_user_id=None), row(created_by_user_id=user.id)])
            await db.commit()
            ids = [r.id for r in (await db.scalars(select(ScheduledDeletion))).all()]

            await notices.notify_schedule_creator(ids[0], succeeded=True)  # no user
            notify.assert_not_awaited()
            await notices.notify_schedule_creator(ids[1], succeeded=True)
            notify.assert_awaited_once()
            await notices.notify_schedule_creator(9999, succeeded=True)  # gone
            notify.assert_awaited_once()

    asyncio.run(run())


def test_a_failing_notification_never_breaks_the_deletion(monkeypatch):
    async def run():
        async with database(monkeypatch) as factory, factory() as db:
            monkeypatch.setattr(notices, "async_db", factory)
            monkeypatch.setattr(
                notices, "notify_user", AsyncMock(side_effect=RuntimeError("smtp down"))
            )
            user, _, _, _, _ = await seed(db)
            db.add(
                ScheduledDeletion(
                    scheduled_at=datetime.now(UTC),
                    target={"title": "T", "media_type": "movie", "scope": "movie"},
                    created_by_username="x",
                    created_by_user_id=user.id,
                    state="completed",
                )
            )
            await db.commit()
            schedule_id = await db.scalar(select(ScheduledDeletion.id))

            await notices.notify_schedule_creator(schedule_id, succeeded=True)

    asyncio.run(run())
