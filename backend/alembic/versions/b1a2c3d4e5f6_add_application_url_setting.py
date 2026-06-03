"""Add application URL to general settings and merge heads.

Revision ID: b1a2c3d4e5f6
Revises: f9e8d7c6b5a4
Create Date: 2026-06-02 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b1a2c3d4e5f6"
down_revision: str | Sequence[str] | None = ("f9e8d7c6b5a4", "a6d4c3b2e1f0")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "application_url" not in cols:
            batch_op.add_column(
                sa.Column("application_url", sa.String(length=500), nullable=True)
            )


def downgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "application_url" in cols:
            batch_op.drop_column("application_url")
