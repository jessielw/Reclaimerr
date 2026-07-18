"""Add durable background job priority.

Revision ID: b2d4f6a8c0e1
Revises: aa17c9d4e6f2
Create Date: 2026-07-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2d4f6a8c0e1"
down_revision = "aa17c9d4e6f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("background_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "priority",
                sa.Enum("LOW", "NORMAL", "HIGH", name="backgroundjobpriority"),
                nullable=False,
                server_default="NORMAL",
            )
        )
        batch_op.create_index(
            "ix_background_jobs_claimable",
            ["status", "scheduled_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("background_jobs") as batch_op:
        batch_op.drop_index("ix_background_jobs_claimable")
        batch_op.drop_column("priority")
