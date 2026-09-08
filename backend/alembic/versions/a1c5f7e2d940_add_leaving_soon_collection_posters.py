"""Add the custom Leaving Soon collection poster paths.

Revision ID: a1c5f7e2d940
Revises: c4a7e91d3b58
Create Date: 2026-09-08 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1c5f7e2d940"
down_revision: str | Sequence[str] | None = "c4a7e91d3b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "leaving_soon_movie_poster_path",
    "leaving_soon_series_poster_path",
)


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    # nullable with no backfill: an install that has never uploaded a poster
    # keeps whatever artwork its media server generates from the collection.
    existing = _cols("general_settings")
    missing = [column for column in _COLUMNS if column not in existing]
    if not missing:
        return
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        for column in missing:
            batch_op.add_column(
                sa.Column(column, sa.String(length=255), nullable=True)
            )


def downgrade() -> None:
    existing = _cols("general_settings")
    present = [column for column in _COLUMNS if column in existing]
    if not present:
        return
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        for column in present:
            batch_op.drop_column(column)
