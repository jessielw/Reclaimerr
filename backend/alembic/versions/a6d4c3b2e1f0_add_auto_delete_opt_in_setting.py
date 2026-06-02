"""Add automatic cleanup deletion opt-in to general settings.

Revision ID: a6d4c3b2e1f0
Revises: c9d1e2f3a4b5, e6f7a8b9c0d1
Create Date: 2026-06-01 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a6d4c3b2e1f0"
down_revision: str | Sequence[str] | None = ("c9d1e2f3a4b5", "e6f7a8b9c0d1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "auto_delete_enabled" not in cols:
            batch_op.add_column(
                sa.Column(
                    "auto_delete_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )


def downgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "auto_delete_enabled" in cols:
            batch_op.drop_column("auto_delete_enabled")
