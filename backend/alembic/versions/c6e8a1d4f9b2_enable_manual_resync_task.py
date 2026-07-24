"""Enable the manual media resync task.

Revision ID: c6e8a1d4f9b2
Revises: b2d4f6a8c0e1
Create Date: 2026-07-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "c6e8a1d4f9b2"
down_revision = "b2d4f6a8c0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MANUAL controls APScheduler registration; enabled controls whether the
    # task may be queued. Resync is intentionally manual, not disabled.
    op.execute("UPDATE task_schedules SET enabled = 1 WHERE task = 'RESYNC_MEDIA'")


def downgrade() -> None:
    op.execute("UPDATE task_schedules SET enabled = 0 WHERE task = 'RESYNC_MEDIA'")
