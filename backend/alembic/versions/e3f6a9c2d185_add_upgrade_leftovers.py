"""Add upgrade_leftovers, its scan summary and the SCAN_UPGRADE_LEFTOVERS task.

upgrade_leftovers holds files Radarr imported from its download folder and has
since replaced with an upgrade. The scan task rewrites it; nothing else writes
to it apart from the user's ignore flag.

Revision ID: e3f6a9c2d185
Revises: d1a5e8c3b7f2
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3f6a9c2d185"
down_revision: str | Sequence[str] | None = "d1a5e8c3b7f2"
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
)
_OLD_TASK = sa.Enum(*_TASKS, name="task")
_NEW_TASK = sa.Enum(*_TASKS, "SCAN_UPGRADE_LEFTOVERS", name="task")


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
    if "upgrade_leftovers" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "upgrade_leftovers",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("service_config_id", sa.Integer(), nullable=False),
            sa.Column("arr_movie_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("dropped_path", sa.String(length=2048), nullable=False),
            sa.Column("local_path", sa.String(length=2048), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("link_count", sa.Integer(), nullable=False),
            sa.Column("file_key", sa.String(length=64), nullable=True),
            sa.Column("movie_id", sa.Integer(), nullable=True),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("source_title", sa.String(length=1024), nullable=True),
            sa.Column("imported_at", sa.DateTime(), nullable=True),
            sa.Column("manual_reason", sa.String(length=255), nullable=True),
            sa.Column("ignored", sa.Boolean(), nullable=False),
            sa.ForeignKeyConstraint(
                ["service_config_id"], ["service_configs.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["movie_id"], ["movies.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "service_config_id", "dropped_path", name="uq_upgrade_leftover_path"
            ),
        )
        with op.batch_alter_table("upgrade_leftovers", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_upgrade_leftovers_service_config_id"),
                ["service_config_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f("ix_upgrade_leftovers_movie_id"), ["movie_id"], unique=False
            )

    if "upgrade_leftover_scan" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("upgrade_leftover_scan", sa.JSON(), nullable=True)
            )

    _alter_task_enum(_OLD_TASK, _NEW_TASK)


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM task_runs WHERE task = 'SCAN_UPGRADE_LEFTOVERS'"))
    op.execute(
        sa.text("DELETE FROM task_schedules WHERE task = 'SCAN_UPGRADE_LEFTOVERS'")
    )
    _alter_task_enum(_NEW_TASK, _OLD_TASK)

    if "upgrade_leftover_scan" in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("upgrade_leftover_scan")

    if "upgrade_leftovers" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("upgrade_leftovers")
