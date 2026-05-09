"""Migrate reclaim candidate size from GB to bytes.

Revision ID: b6e1d9a4c2f3
Revises: a4b5c6d7e8f9
Create Date: 2026-05-07 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b6e1d9a4c2f3"
down_revision: Union[str, None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("reclaim_candidates") as batch_op:
        batch_op.add_column(sa.Column("estimated_space_bytes", sa.BigInteger(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE reclaim_candidates
            SET estimated_space_bytes = CAST(estimated_space_gb * 1073741824 AS BIGINT)
            WHERE estimated_space_gb IS NOT NULL AND estimated_space_bytes IS NULL
            """
        )
    )

    with op.batch_alter_table("reclaim_candidates") as batch_op:
        batch_op.drop_column("estimated_space_gb")


def downgrade() -> None:
    with op.batch_alter_table("reclaim_candidates") as batch_op:
        batch_op.add_column(sa.Column("estimated_space_gb", sa.Float(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE reclaim_candidates
            SET estimated_space_gb = estimated_space_bytes / 1073741824.0
            WHERE estimated_space_bytes IS NOT NULL AND estimated_space_gb IS NULL
            """
        )
    )

    with op.batch_alter_table("reclaim_candidates") as batch_op:
        batch_op.drop_column("estimated_space_bytes")
