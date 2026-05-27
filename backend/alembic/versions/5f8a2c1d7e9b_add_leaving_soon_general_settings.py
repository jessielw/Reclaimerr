"""Add leaving-soon collection settings to general settings.

Revision ID: 5f8a2c1d7e9b
Revises: 4c7d9e1f2a3b
Create Date: 2026-05-24 12:10:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "5f8a2c1d7e9b"
down_revision: str | Sequence[str] | None = "4c7d9e1f2a3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "leaving_soon_enabled" not in cols:
            batch_op.add_column(
                sa.Column(
                    "leaving_soon_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if "leaving_soon_collection_title" not in cols:
            batch_op.add_column(
                sa.Column(
                    "leaving_soon_collection_title",
                    sa.String(length=255),
                    nullable=False,
                    server_default=sa.text("'Leaving Soon'"),
                )
            )


def downgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "leaving_soon_collection_title" in cols:
            batch_op.drop_column("leaving_soon_collection_title")
        if "leaving_soon_enabled" in cols:
            batch_op.drop_column("leaving_soon_enabled")
