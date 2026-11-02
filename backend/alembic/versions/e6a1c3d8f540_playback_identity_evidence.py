"""Retain playback identity provenance and conflicts.

Revision ID: e6a1c3d8f540
Revises: d5f0b2c7e439
"""

import sqlalchemy as sa
from alembic import op

revision = "e6a1c3d8f540"
down_revision = "d5f0b2c7e439"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "playback_history_events",
        sa.Column("identity_evidence", sa.JSON(), nullable=True),
    )


def downgrade():
    with op.batch_alter_table("playback_history_events") as batch:
        batch.drop_column("identity_evidence")
