"""Add per-service Leaving Soon last-success titles to general settings.

Revision ID: 6c4d5e7f8a9b
Revises: 5f8a2c1d7e9b
Create Date: 2026-05-26 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "6c4d5e7f8a9b"
down_revision: str | Sequence[str] | None = "5f8a2c1d7e9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "leaving_soon_last_success_titles" not in cols:
            batch_op.add_column(
                sa.Column(
                    "leaving_soon_last_success_titles",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
        if "leaving_soon_stale_titles" in cols:
            batch_op.drop_column("leaving_soon_stale_titles")


def downgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "leaving_soon_last_success_titles" in cols:
            batch_op.drop_column("leaving_soon_last_success_titles")
