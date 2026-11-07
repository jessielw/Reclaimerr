"""Persist manual deletion schedules independently of cleanup candidates.

Revision ID: f7b2d4e9a651
Revises: e6a1c3d8f540
"""

import sqlalchemy as sa
from alembic import op

revision = "f7b2d4e9a651"
down_revision = "e6a1c3d8f540"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scheduled_deletions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("created_by_username", sa.String(255), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("background_jobs.id", ondelete="SET NULL"),
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_scheduled_deletions_scheduled_at", "scheduled_deletions", ["scheduled_at"]
    )
    op.create_index("ix_scheduled_deletions_state", "scheduled_deletions", ["state"])


def downgrade():
    op.drop_table("scheduled_deletions")
