"""Persist large inventory deletion proposals across sync processes.

Revision ID: a2c7e9f4b106
Revises: f1a6c8d3e205
"""

import sqlalchemy as sa
from alembic import op

revision = "a2c7e9f4b106"
down_revision = "f1a6c8d3e205"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sync_deletion_proposals",
        sa.Column(
            "service_config_id",
            sa.Integer(),
            sa.ForeignKey("service_configs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "media_type", sa.Enum("MOVIE", "SERIES", name="mediatype"), primary_key=True
        ),
        sa.Column("row_ids", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("sync_deletion_proposals")
