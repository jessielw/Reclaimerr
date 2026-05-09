"""Split move_destination_root into per-media-type columns

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("move_destination_movies", sa.String(length=1024), nullable=True)
        )
        batch_op.add_column(
            sa.Column("move_destination_series", sa.String(length=1024), nullable=True)
        )
        batch_op.drop_column("move_destination_root")


def downgrade() -> None:
    # Step 1: add root column back while movies/series columns still exist
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("move_destination_root", sa.String(length=1024), nullable=True)
        )

    # Step 2: copy data while both old and new columns coexist
    op.execute(
        "UPDATE general_settings SET move_destination_root = move_destination_movies "
        "WHERE move_destination_movies IS NOT NULL"
    )

    # Step 3: drop the split columns
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        batch_op.drop_column("move_destination_series")
        batch_op.drop_column("move_destination_movies")
