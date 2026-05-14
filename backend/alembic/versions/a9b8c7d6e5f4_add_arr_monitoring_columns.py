"""add_arr_monitoring_columns

Revision ID: a9b8c7d6e5f4
Revises: de78ca5e17a3
Create Date: 2026-05-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, None] = "23235d08ddc8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("movies", sa.Column("is_monitored", sa.Boolean(), nullable=True))
    op.add_column("series", sa.Column("is_monitored", sa.Boolean(), nullable=True))
    op.add_column("seasons", sa.Column("is_monitored", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("seasons", "is_monitored")
    op.drop_column("series", "is_monitored")
    op.drop_column("movies", "is_monitored")
