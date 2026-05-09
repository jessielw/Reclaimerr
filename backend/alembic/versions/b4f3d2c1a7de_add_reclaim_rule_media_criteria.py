"""add media-info criteria fields to reclaim rules

Revision ID: b4f3d2c1a7de
Revises: 9a0b7a52a6f1
Create Date: 2026-04-26 23:05:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4f3d2c1a7de"
down_revision: Union[str, None] = "9a0b7a52a6f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # cleanup from interrupted SQLite batch migrations (we need to be sure retries are safe)
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_reclaim_rules"))

    with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("video_codec_families_in", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("video_codec_families_not_in", sa.JSON(), nullable=True)
        )
        batch_op.add_column(sa.Column("audio_codec_families_in", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("audio_codec_families_not_in", sa.JSON(), nullable=True)
        )
        batch_op.add_column(sa.Column("has_hdr", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("has_dolby_vision", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("min_video_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_video_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("min_video_height", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_video_height", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("min_audio_channels", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("max_audio_channels", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("min_duration", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("max_duration", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("min_audio_track_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_audio_track_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("audio_languages_in", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("audio_languages_not_in", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("subtitle_languages_in", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("subtitle_languages_not_in", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("video_color_spaces_in", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("video_color_spaces_not_in", sa.JSON(), nullable=True)
        )
        batch_op.add_column(sa.Column("video_color_transfers_in", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("video_color_transfers_not_in", sa.JSON(), nullable=True)
        )
        batch_op.add_column(sa.Column("video_color_primaries_in", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("video_color_primaries_not_in", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
        batch_op.drop_column("video_color_primaries_not_in")
        batch_op.drop_column("video_color_primaries_in")
        batch_op.drop_column("video_color_transfers_not_in")
        batch_op.drop_column("video_color_transfers_in")
        batch_op.drop_column("video_color_spaces_not_in")
        batch_op.drop_column("video_color_spaces_in")
        batch_op.drop_column("subtitle_languages_not_in")
        batch_op.drop_column("subtitle_languages_in")
        batch_op.drop_column("audio_languages_not_in")
        batch_op.drop_column("audio_languages_in")
        batch_op.drop_column("max_audio_track_count")
        batch_op.drop_column("min_audio_track_count")
        batch_op.drop_column("max_duration")
        batch_op.drop_column("min_duration")
        batch_op.drop_column("max_audio_channels")
        batch_op.drop_column("min_audio_channels")
        batch_op.drop_column("max_video_height")
        batch_op.drop_column("min_video_height")
        batch_op.drop_column("max_video_width")
        batch_op.drop_column("min_video_width")
        batch_op.drop_column("has_dolby_vision")
        batch_op.drop_column("has_hdr")
        batch_op.drop_column("audio_codec_families_not_in")
        batch_op.drop_column("audio_codec_families_in")
        batch_op.drop_column("video_codec_families_not_in")
        batch_op.drop_column("video_codec_families_in")
