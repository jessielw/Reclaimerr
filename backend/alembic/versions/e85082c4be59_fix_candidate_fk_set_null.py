"""fix_fk_ondelete

Fixes missing ON DELETE actions on several foreign keys:
  - notification_settings.user_id          -> ON DELETE CASCADE
  - general_settings.updated_by_user_id    -> ON DELETE SET NULL
  - protected_media.protected_by_user_id   -> ON DELETE CASCADE
  - protection_requests.candidate_id       -> ON DELETE SET NULL
  - protection_requests.requested_by_user_id -> ON DELETE CASCADE
  - protection_requests.reviewed_by_user_id  -> ON DELETE SET NULL

SQLite does not support ALTER COLUMN, so each affected table is recreated
using batch_alter_table(copy_from=..., recreate='always').

Revision ID: e85082c4be59
Revises: 8b972fb39ea9
Create Date: 2026-04-14 14:16:37.654614
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e85082c4be59'
down_revision: Union[str, None] = '8b972fb39ea9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _notification_settings_table(cascade_user: bool) -> sa.Table:
    user_fk = sa.ForeignKeyConstraint(
        ['user_id'], ['users.id'], ondelete='CASCADE' if cascade_user else None
    )
    return sa.Table(
        'notification_settings',
        sa.MetaData(),
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('new_cleanup_candidates', sa.Boolean(), nullable=False),
        sa.Column('request_approved', sa.Boolean(), nullable=False),
        sa.Column('request_declined', sa.Boolean(), nullable=False),
        sa.Column('admin_message', sa.Boolean(), nullable=False),
        sa.Column('task_failure', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        user_fk,
        sa.PrimaryKeyConstraint('id'),
    )


def _general_settings_table(set_null_user: bool) -> sa.Table:
    user_fk = sa.ForeignKeyConstraint(
        ['updated_by_user_id'], ['users.id'], ondelete='SET NULL' if set_null_user else None
    )
    return sa.Table(
        'general_settings',
        sa.MetaData(),
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('auto_tag_enabled', sa.Boolean(), nullable=False),
        sa.Column('cleanup_tag_suffix', sa.String(length=15), nullable=False),
        sa.Column('worker_poll_min_seconds', sa.Float(), nullable=True),
        sa.Column('worker_poll_max_seconds', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        user_fk,
        sa.PrimaryKeyConstraint('id'),
    )


def _protected_media_table(cascade_user: bool) -> sa.Table:
    return sa.Table(
        'protected_media',
        sa.MetaData(),
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('media_type', sa.Enum('MOVIE', 'SERIES', name='mediatype'), nullable=False),
        sa.Column('protected_by_user_id', sa.Integer(), nullable=False),
        sa.Column('movie_id', sa.Integer(), nullable=True),
        sa.Column('series_id', sa.Integer(), nullable=True),
        sa.Column('season_id', sa.Integer(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('permanent', sa.Boolean(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['movie_id'], ['movies.id']),
        sa.ForeignKeyConstraint(
            ['protected_by_user_id'], ['users.id'],
            ondelete='CASCADE' if cascade_user else None,
        ),
        sa.ForeignKeyConstraint(['season_id'], ['seasons.id']),
        sa.ForeignKeyConstraint(['series_id'], ['series.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('ix_protected_media_season_id', 'season_id'),
    )


def _protection_requests_table(set_null_candidate: bool, cascade_requester: bool, set_null_reviewer: bool) -> sa.Table:
    return sa.Table(
        'protection_requests',
        sa.MetaData(),
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('media_type', sa.Enum('MOVIE', 'SERIES', name='mediatype'), nullable=False),
        sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('requested_expires_at', sa.DateTime(), nullable=True),
        sa.Column('movie_id', sa.Integer(), nullable=True),
        sa.Column('series_id', sa.Integer(), nullable=True),
        sa.Column('season_id', sa.Integer(), nullable=True),
        sa.Column('candidate_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.Enum('PENDING', 'APPROVED', 'DENIED', name='protectionrequeststatus'), nullable=False),
        sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('admin_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(
            ['candidate_id'], ['reclaim_candidates.id'],
            ondelete='SET NULL' if set_null_candidate else None,
        ),
        sa.ForeignKeyConstraint(['movie_id'], ['movies.id']),
        sa.ForeignKeyConstraint(
            ['requested_by_user_id'], ['users.id'],
            ondelete='CASCADE' if cascade_requester else None,
        ),
        sa.ForeignKeyConstraint(
            ['reviewed_by_user_id'], ['users.id'],
            ondelete='SET NULL' if set_null_reviewer else None,
        ),
        sa.ForeignKeyConstraint(['season_id'], ['seasons.id']),
        sa.ForeignKeyConstraint(['series_id'], ['series.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('ix_protection_requests_season_id', 'season_id'),
    )


def upgrade() -> None:
    with op.batch_alter_table('notification_settings', recreate='always',
                              copy_from=_notification_settings_table(cascade_user=True)) as batch_op:
        pass

    with op.batch_alter_table('general_settings', recreate='always',
                              copy_from=_general_settings_table(set_null_user=True)) as batch_op:
        pass

    with op.batch_alter_table('protected_media', recreate='always',
                              copy_from=_protected_media_table(cascade_user=True)) as batch_op:
        pass

    with op.batch_alter_table('protection_requests', recreate='always',
                              copy_from=_protection_requests_table(
                                  set_null_candidate=True,
                                  cascade_requester=True,
                                  set_null_reviewer=True,
                              )) as batch_op:
        pass


def downgrade() -> None:
    with op.batch_alter_table('protection_requests', recreate='always',
                              copy_from=_protection_requests_table(
                                  set_null_candidate=False,
                                  cascade_requester=False,
                                  set_null_reviewer=False,
                              )) as batch_op:
        pass

    with op.batch_alter_table('protected_media', recreate='always',
                              copy_from=_protected_media_table(cascade_user=False)) as batch_op:
        pass

    with op.batch_alter_table('general_settings', recreate='always',
                              copy_from=_general_settings_table(set_null_user=False)) as batch_op:
        pass

    with op.batch_alter_table('notification_settings', recreate='always',
                              copy_from=_notification_settings_table(cascade_user=False)) as batch_op:
        pass
