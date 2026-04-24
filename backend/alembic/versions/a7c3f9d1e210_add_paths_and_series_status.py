"""add path criteria and series status

Revision ID: a7c3f9d1e210
Revises: a3f1c8e92b47
Create Date: 2026-04-20 18:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3f9d1e210'
down_revision: Union[str, None] = 'a3f1c8e92b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('reclaim_rules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paths', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('series_status', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('reclaim_rules', schema=None) as batch_op:
        batch_op.drop_column('series_status')
        batch_op.drop_column('paths')
