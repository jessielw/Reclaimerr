"""Add observed per-item operation details.

Revision ID: d5f0b2c7e439
Revises: c4e9a1b6d328
"""
import sqlalchemy as sa
from alembic import op

revision = "d5f0b2c7e439"
down_revision = "c4e9a1b6d328"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("support_operation_events", sa.Column("detail", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("support_operation_events") as batch:
        batch.drop_column("detail")
