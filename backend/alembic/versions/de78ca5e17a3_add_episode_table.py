"""add_episode_table

Revision ID: de78ca5e17a3
Revises: f1a2b3c4d5e6
Create Date: 2026-05-12 11:33:59.810787

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'de78ca5e17a3'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('episodes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('season_id', sa.Integer(), nullable=False),
    sa.Column('episode_number', sa.Integer(), nullable=False),
    sa.Column('air_date', sa.DateTime(), nullable=True),
    sa.Column('size', sa.Integer(), nullable=True),
    sa.Column('path', sa.String(length=1024), nullable=True),
    sa.Column('view_count', sa.Integer(), nullable=False),
    sa.Column('last_viewed_at', sa.DateTime(), nullable=True),
    sa.Column('plex_rating_key', sa.String(length=100), nullable=True),
    sa.Column('jellyfin_episode_id', sa.String(length=100), nullable=True),
    sa.Column('emby_episode_id', sa.String(length=100), nullable=True),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['season_id'], ['seasons.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('season_id', 'episode_number', name='uq_episode_season_number')
    )
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_episodes_season_id'), ['season_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('episodes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_episodes_season_id'))

    op.drop_table('episodes')
