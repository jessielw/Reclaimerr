"""add episode added_at

Revision ID: d2e6f0b4a81c
Revises: c1d5e9a3f70b
Create Date: 2026-08-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2e6f0b4a81c"
down_revision: str | Sequence[str] | None = "c1d5e9a3f70b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "episodes"
COLUMN = "added_at"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return COLUMN in {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    if _has_column():
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.DateTime(), nullable=True))

    # Seed from the season so watch staleness keeps behaving as it did until the
    # next media sync records real per-episode dates.
    op.execute(
        sa.text(
            "UPDATE episodes SET added_at = ("
            "SELECT seasons.added_at FROM seasons WHERE seasons.id = episodes.season_id"
            ") WHERE added_at IS NULL"
        )
    )


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
