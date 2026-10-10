"""Add requester leaving-soon warnings.

Revision ID: c3d9e2a7f4b1
Revises: b8c4e1f7a2d9
Create Date: 2026-10-09 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d9e2a7f4b1"
down_revision: str | Sequence[str] | None = "b8c4e1f7a2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    # on for existing destinations, so requesters who already get notifications
    # are warned without changing anything
    if "requester_leaving_soon" not in _cols("notification_settings"):
        with op.batch_alter_table("notification_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "requester_leaving_soon",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )
    if "requester_warning_days" not in _cols("general_settings"):
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "requester_warning_days",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("7"),
                )
            )
    if "requester_warned_at" not in _cols("reclaim_candidates"):
        with op.batch_alter_table("reclaim_candidates", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("requester_warned_at", sa.DateTime(), nullable=True)
            )


def downgrade() -> None:
    for table, column in (
        ("reclaim_candidates", "requester_warned_at"),
        ("general_settings", "requester_warning_days"),
        ("notification_settings", "requester_leaving_soon"),
    ):
        if column in _cols(table):
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.drop_column(column)
