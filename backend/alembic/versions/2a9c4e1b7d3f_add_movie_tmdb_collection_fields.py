"""add movie tmdb collection fields

Revision ID: 2a9c4e1b7d3f
Revises: b7c8d9e0f1a2
Create Date: 2026-05-30 14:15:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "2a9c4e1b7d3f"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _cols("movies")
    with op.batch_alter_table("movies", schema=None) as batch_op:
        if "tmdb_collection_id" not in cols:
            batch_op.add_column(
                sa.Column("tmdb_collection_id", sa.Integer(), nullable=True)
            )
        if "tmdb_collection_name" not in cols:
            batch_op.add_column(
                sa.Column("tmdb_collection_name", sa.String(length=255), nullable=True)
            )
        if "tmdb_collection_checked" not in cols:
            batch_op.add_column(
                sa.Column(
                    "tmdb_collection_checked",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )


def downgrade() -> None:
    cols = _cols("movies")
    with op.batch_alter_table("movies", schema=None) as batch_op:
        if "tmdb_collection_checked" in cols:
            batch_op.drop_column("tmdb_collection_checked")
        if "tmdb_collection_name" in cols:
            batch_op.drop_column("tmdb_collection_name")
        if "tmdb_collection_id" in cols:
            batch_op.drop_column("tmdb_collection_id")
