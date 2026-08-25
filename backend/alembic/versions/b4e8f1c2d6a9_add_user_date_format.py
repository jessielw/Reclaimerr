"""add user date format preference

Revision ID: b4e8f1c2d6a9
Revises: aa7d9e3f1b42
Create Date: 2026-08-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b4e8f1c2d6a9"
down_revision: str | Sequence[str] | None = "aa7d9e3f1b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "date_format",
                sa.String(length=8),
                nullable=False,
                server_default="mdy",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("date_format")
