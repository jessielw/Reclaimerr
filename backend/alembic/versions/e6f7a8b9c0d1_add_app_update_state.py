"""add app update state table

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-05-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_update_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("current_version", sa.String(length=64), nullable=True),
        sa.Column("latest_version", sa.String(length=64), nullable=True),
        sa.Column("latest_release_url", sa.String(length=500), nullable=True),
        sa.Column("latest_release_published_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("update_available", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("last_check_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("app_update_state")
