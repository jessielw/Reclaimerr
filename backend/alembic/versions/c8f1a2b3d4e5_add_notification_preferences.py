"""add notification preferences json field

Revision ID: c8f1a2b3d4e5
Revises: a9b8c7d6e5f4
Create Date: 2026-05-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c8f1a2b3d4e5"
down_revision: Union[str, None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("notification_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("preferences", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notification_settings", schema=None) as batch_op:
        batch_op.drop_column("preferences")
