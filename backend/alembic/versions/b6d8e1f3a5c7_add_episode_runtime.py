"""add episode runtime

Revision ID: b6d8e1f3a5c7
Revises: a3f6b8d1c4e2
Create Date: 2026-08-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d8e1f3a5c7"
down_revision: str | Sequence[str] | None = "a3f6b8d1c4e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("episodes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("runtime", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("episodes", schema=None) as batch_op:
        batch_op.drop_column("runtime")
