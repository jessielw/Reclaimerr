"""drop never_watched columns from movies, series, seasons

Revision ID: a3f1c8e92b47
Revises: 20666951d75d
Create Date: 2026-04-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3f1c8e92b47'
down_revision: Union[str, None] = '20666951d75d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _movies_table(include_never_watched: bool) -> sa.Table:
    cols = [
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.SmallInteger(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('radarr_id', sa.Integer(), nullable=True),
        sa.Column('imdb_id', sa.String(length=20), nullable=True),
        sa.Column('tmdb_title', sa.String(length=512), nullable=True),
        sa.Column('original_title', sa.String(length=512), nullable=True),
        sa.Column('tmdb_release_date', sa.DateTime(), nullable=True),
        sa.Column('original_language', sa.String(length=10), nullable=True),
        sa.Column('homepage', sa.String(length=500), nullable=True),
        sa.Column('origin_country', sa.JSON(), nullable=True),
        sa.Column('poster_url', sa.String(length=500), nullable=True),
        sa.Column('backdrop_url', sa.String(length=500), nullable=True),
        sa.Column('overview', sa.Text(), nullable=True),
        sa.Column('genres', sa.JSON(), nullable=True),
        sa.Column('popularity', sa.Float(), nullable=True),
        sa.Column('vote_average', sa.Float(), nullable=True),
        sa.Column('vote_count', sa.Integer(), nullable=True),
        sa.Column('revenue', sa.Integer(), nullable=True),
        sa.Column('runtime', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('tagline', sa.String(length=255), nullable=True),
        sa.Column('last_viewed_at', sa.DateTime(), nullable=True),
        sa.Column('view_count', sa.Integer(), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.Column('removed_at', sa.DateTime(), nullable=True),
        sa.Column('last_metadata_refresh_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tmdb_id'),
        sa.UniqueConstraint('radarr_id'),
        sa.UniqueConstraint('imdb_id'),
        sa.Index('ix_movies_tmdb_id', 'tmdb_id'),
        sa.Index('ix_movies_radarr_id', 'radarr_id'),
        sa.Index('ix_movies_imdb_id', 'imdb_id'),
    ]
    if include_never_watched:
        cols.append(sa.Column('never_watched', sa.Boolean(), nullable=False, server_default='1'))
    return sa.Table('movies', sa.MetaData(), *cols)


def _series_table(include_never_watched: bool) -> sa.Table:
    cols = [
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.SmallInteger(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('sonarr_id', sa.Integer(), nullable=True),
        sa.Column('imdb_id', sa.String(length=20), nullable=True),
        sa.Column('tvdb_id', sa.String(length=20), nullable=True),
        sa.Column('tmdb_title', sa.String(length=512), nullable=True),
        sa.Column('original_title', sa.String(length=512), nullable=True),
        sa.Column('tmdb_first_air_date', sa.DateTime(), nullable=True),
        sa.Column('tmdb_last_air_date', sa.DateTime(), nullable=True),
        sa.Column('original_language', sa.String(length=10), nullable=True),
        sa.Column('homepage', sa.String(length=500), nullable=True),
        sa.Column('origin_country', sa.JSON(), nullable=True),
        sa.Column('poster_url', sa.String(length=500), nullable=True),
        sa.Column('backdrop_url', sa.String(length=500), nullable=True),
        sa.Column('overview', sa.Text(), nullable=True),
        sa.Column('genres', sa.JSON(), nullable=True),
        sa.Column('popularity', sa.Float(), nullable=True),
        sa.Column('vote_average', sa.Float(), nullable=True),
        sa.Column('vote_count', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=True),
        sa.Column('tagline', sa.String(length=255), nullable=True),
        sa.Column('season_count', sa.Integer(), nullable=True),
        sa.Column('last_viewed_at', sa.DateTime(), nullable=True),
        sa.Column('view_count', sa.Integer(), nullable=False),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.Column('removed_at', sa.DateTime(), nullable=True),
        sa.Column('last_metadata_refresh_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tmdb_id'),
        sa.UniqueConstraint('sonarr_id'),
        sa.UniqueConstraint('imdb_id'),
        sa.UniqueConstraint('tvdb_id'),
        sa.Index('ix_series_tmdb_id', 'tmdb_id'),
        sa.Index('ix_series_sonarr_id', 'sonarr_id'),
        sa.Index('ix_series_imdb_id', 'imdb_id'),
        sa.Index('ix_series_tvdb_id', 'tvdb_id'),
    ]
    if include_never_watched:
        cols.append(sa.Column('never_watched', sa.Boolean(), nullable=False, server_default='1'))
    return sa.Table('series', sa.MetaData(), *cols)


def _seasons_table(include_never_watched: bool) -> sa.Table:
    cols = [
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('series_id', sa.Integer(), nullable=False),
        sa.Column('season_number', sa.SmallInteger(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('episode_count', sa.Integer(), nullable=True),
        sa.Column('view_count', sa.Integer(), nullable=True),
        sa.Column('last_viewed_at', sa.DateTime(), nullable=True),
        sa.Column('air_date', sa.DateTime(), nullable=True),
        sa.Column('jellyfin_season_id', sa.String(length=100), nullable=True),
        sa.Column('emby_season_id', sa.String(length=100), nullable=True),
        sa.Column('plex_season_rating_key', sa.String(length=100), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['series_id'], ['series.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('series_id', 'season_number', name='uq_season_series_number'),
        sa.Index('ix_seasons_series_id', 'series_id'),
    ]
    if include_never_watched:
        cols.append(sa.Column('never_watched', sa.Boolean(), nullable=True))
    return sa.Table('seasons', sa.MetaData(), *cols)


def upgrade() -> None:
    conn = op.get_bind()
    for tbl in ('_alembic_tmp_movies', '_alembic_tmp_series', '_alembic_tmp_seasons'):
        conn.execute(sa.text(f'DROP TABLE IF EXISTS "{tbl}"'))

    conn.execute(sa.text('PRAGMA foreign_keys=OFF'))
    try:
        with op.batch_alter_table('movies', recreate='always',
                                  copy_from=_movies_table(include_never_watched=True)) as batch_op:
            batch_op.drop_column('never_watched')

        with op.batch_alter_table('series', recreate='always',
                                  copy_from=_series_table(include_never_watched=True)) as batch_op:
            batch_op.drop_column('never_watched')

        with op.batch_alter_table('seasons', recreate='always',
                                  copy_from=_seasons_table(include_never_watched=True)) as batch_op:
            batch_op.drop_column('never_watched')
    finally:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text('PRAGMA foreign_keys=OFF'))
    try:
        with op.batch_alter_table('seasons', recreate='always',
                                  copy_from=_seasons_table(include_never_watched=False)) as batch_op:
            batch_op.add_column(sa.Column('never_watched', sa.Boolean(), nullable=True))

        with op.batch_alter_table('series', recreate='always',
                                  copy_from=_series_table(include_never_watched=False)) as batch_op:
            batch_op.add_column(sa.Column('never_watched', sa.Boolean(), nullable=False, server_default='1'))

        with op.batch_alter_table('movies', recreate='always',
                                  copy_from=_movies_table(include_never_watched=False)) as batch_op:
            batch_op.add_column(sa.Column('never_watched', sa.Boolean(), nullable=False, server_default='1'))
    finally:
        conn.execute(sa.text('PRAGMA foreign_keys=ON'))