"""add refresh playback history task

Revision ID: bc34de56fa78
Revises: ab23cd45ef67
Create Date: 2026-06-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bc34de56fa78"
down_revision: str | Sequence[str] | None = "ab23cd45ef67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TASK = sa.Enum(
    "SYNC_MEDIA",
    "RESYNC_MEDIA",
    "SYNC_MEDIA_LIBRARIES",
    "SYNC_LINKED_DATA",
    "SCAN_CLEANUP_CANDIDATES",
    "TAG_CLEANUP_CANDIDATES",
    "DELETE_CLEANUP_CANDIDATES",
    "WEEKLY_HOUSE_KEEPING",
    "CHECK_APP_UPDATES",
    "IMDB_RATINGS_REFRESH",
    "ANILIST_RATINGS_REFRESH",
    name="task",
)
_NEW_TASK = sa.Enum(
    "SYNC_MEDIA",
    "RESYNC_MEDIA",
    "SYNC_MEDIA_LIBRARIES",
    "SYNC_LINKED_DATA",
    "SCAN_CLEANUP_CANDIDATES",
    "TAG_CLEANUP_CANDIDATES",
    "DELETE_CLEANUP_CANDIDATES",
    "WEEKLY_HOUSE_KEEPING",
    "CHECK_APP_UPDATES",
    "IMDB_RATINGS_REFRESH",
    "ANILIST_RATINGS_REFRESH",
    "REFRESH_PLAYBACK_HISTORY",
    name="task",
)


def upgrade() -> None:
    with op.batch_alter_table("task_schedules", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_OLD_TASK,
            type_=_NEW_TASK,
            existing_nullable=False,
        )

    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_OLD_TASK,
            type_=_NEW_TASK,
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM task_runs WHERE task = 'REFRESH_PLAYBACK_HISTORY'")
    )
    op.execute(
        sa.text("DELETE FROM task_schedules WHERE task = 'REFRESH_PLAYBACK_HISTORY'")
    )

    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_NEW_TASK,
            type_=_OLD_TASK,
            existing_nullable=False,
        )

    with op.batch_alter_table("task_schedules", schema=None) as batch_op:
        batch_op.alter_column(
            "task",
            existing_type=_NEW_TASK,
            type_=_OLD_TASK,
            existing_nullable=False,
        )
