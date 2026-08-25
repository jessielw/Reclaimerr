"""add reclaim rule descriptions

Revision ID: aa7d9e3f1b42
Revises: a8c1e4f7b2d5
Create Date: 2026-08-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "aa7d9e3f1b42"
down_revision: str | Sequence[str] | None = "a8c1e4f7b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
        batch_op.drop_column("description")
