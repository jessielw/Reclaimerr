"""repair legacy external ratings task data

Revision ID: 5a8c2e7d9f10
Revises: 1d7c9e4a6b2f
Create Date: 2026-06-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5a8c2e7d9f10"
down_revision: str | Sequence[str] | None = "1d7c9e4a6b2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_TASK = "REFRESH_EXTERNAL_RATINGS"
_MDBLIST_TASK = "MDBLIST_RATINGS_REFRESH"


def upgrade() -> None:
    bind = op.get_bind()

    # The combined provider task cannot be represented accurately by either
    # replacement task, so remove its historical runs as the original split
    # migration intended.
    bind.execute(
        sa.text("DELETE FROM task_runs WHERE task = :legacy_task"),
        {"legacy_task": _LEGACY_TASK},
    )

    # Keep durable job history readable by the current task payload model.
    bind.execute(
        sa.text(
            "UPDATE background_jobs SET "
            "payload = json_set(payload, '$.task', 'mdblist_ratings_refresh'), "
            "dedupe_key = CASE "
            "WHEN dedupe_key = 'task-run-refresh_external_ratings' "
            "THEN 'task-run-mdblist_ratings_refresh' ELSE dedupe_key END "
            "WHERE job_type = 'TASK_RUN' "
            "AND json_extract(payload, '$.task') = 'refresh_external_ratings'"
        )
    )

    legacy_schedules = bind.execute(
        sa.text(
            "SELECT id FROM task_schedules "
            "WHERE task = :legacy_task ORDER BY id"
        ),
        {"legacy_task": _LEGACY_TASK},
    ).scalars().all()
    if not legacy_schedules:
        return

    replacement_schedule_id = bind.execute(
        sa.text(
            "SELECT id FROM task_schedules "
            "WHERE task = :replacement_task ORDER BY id LIMIT 1"
        ),
        {"replacement_task": _MDBLIST_TASK},
    ).scalar_one_or_none()
    if replacement_schedule_id is None:
        replacement_schedule_id = legacy_schedules[0]
        bind.execute(
            sa.text(
                "UPDATE task_schedules SET task = :replacement_task, "
                "description = :description WHERE id = :schedule_id"
            ),
            {
                "replacement_task": _MDBLIST_TASK,
                "description": (
                    "Refreshes ratings from the configured MDBList provider"
                ),
                "schedule_id": legacy_schedules[0],
            },
        )

    bind.execute(
        sa.text(
            "UPDATE task_runs SET task_schedule_id = :replacement_schedule_id "
            "WHERE task_schedule_id IN ("
            "SELECT id FROM task_schedules WHERE task = :legacy_task)"
        ),
        {
            "replacement_schedule_id": replacement_schedule_id,
            "legacy_task": _LEGACY_TASK,
        },
    )

    # Remove duplicate legacy schedules. Scheduler initialization will create
    # either provider schedule if it is still missing.
    bind.execute(
        sa.text("DELETE FROM task_schedules WHERE task = :legacy_task"),
        {"legacy_task": _LEGACY_TASK},
    )


def downgrade() -> None:
    # Deleted combined-provider history cannot be reconstructed accurately.
    # The preceding split migration owns conversion back to the legacy task.
    pass
