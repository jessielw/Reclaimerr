"""Add the BACKUP_DATABASE task and its retention setting.

Revision ID: b8c4e1f7a2d9
Revises: a6f3d8b2c901
Create Date: 2026-10-09 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c4e1f7a2d9"
down_revision: str | Sequence[str] | None = "a6f3d8b2c901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TASKS = (
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
    "MDBLIST_RATINGS_REFRESH",
    "OMDB_RATINGS_REFRESH",
    "REFRESH_PLAYBACK_HISTORY",
    "SCAN_UPGRADE_LEFTOVERS",
)
_OLD_TASK = sa.Enum(*_TASKS, name="task")
_NEW_TASK = sa.Enum(*_TASKS, "BACKUP_DATABASE", name="task")


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def _alter_task_enum(old: sa.Enum, new: sa.Enum) -> None:
    for table in ("task_schedules", "task_runs"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                "task", existing_type=old, type_=new, existing_nullable=False
            )


def upgrade() -> None:
    if "database_backup_retention" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "database_backup_retention",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("4"),
                )
            )

    _alter_task_enum(_OLD_TASK, _NEW_TASK)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM task_runs WHERE task = 'BACKUP_DATABASE'"))
    op.execute(sa.text("DELETE FROM task_schedules WHERE task = 'BACKUP_DATABASE'"))
    _alter_task_enum(_NEW_TASK, _OLD_TASK)

    if "database_backup_retention" in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("database_backup_retention")
