"""add_episode_id_to_candidates

Revision ID: f3a4b5c6d7e8
Revises: de78ca5e17a3
Create Date: 2026-05-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'de78ca5e17a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('reclaim_candidates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('episode_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_reclaim_candidates_episode_id'), ['episode_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_reclaim_candidates_episode_id',
            'episodes',
            ['episode_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('reclaim_candidates', schema=None) as batch_op:
        batch_op.drop_constraint('fk_reclaim_candidates_episode_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_reclaim_candidates_episode_id'))
        batch_op.drop_column('episode_id')
