"""add_episode_name

Revision ID: 23235d08ddc8
Revises: f3a4b5c6d7e8
Create Date: 2026-05-12 16:22:05.301417

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '23235d08ddc8'
down_revision: Union[str, None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('name', sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.drop_column('name')
