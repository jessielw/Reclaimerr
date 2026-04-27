"""add movie version media metadata columns

Revision ID: c5f60f5a7b29
Revises: 0ddab1295d26
Create Date: 2026-04-25 16:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f60f5a7b29"
down_revision: Union[str, None] = "0ddab1295d26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("movie_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("file_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("duration", sa.Float(), nullable=True))

        batch_op.add_column(sa.Column("video_track_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("video_codec", sa.String(length=80), nullable=True))
        batch_op.add_column(
            sa.Column("video_codec_family", sa.String(length=24), nullable=True)
        )
        batch_op.add_column(sa.Column("video_hdr", sa.Boolean(), nullable=True))
        batch_op.add_column(
            sa.Column("video_dolby_vision", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("video_dolby_vision_profile", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(sa.Column("video_bitrate", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("video_bit_depth", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("video_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("video_height", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("video_resolution", sa.String(length=20), nullable=True)
        )
        batch_op.add_column(
            sa.Column("video_color_primaries", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("video_color_space", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("video_color_transfer", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(sa.Column("video_fps", sa.Float(), nullable=True))

        batch_op.add_column(sa.Column("audio_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("audio_languages", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("audio_codec", sa.String(length=80), nullable=True))
        batch_op.add_column(
            sa.Column("audio_codec_family", sa.String(length=24), nullable=True)
        )
        batch_op.add_column(sa.Column("audio_title", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("audio_language", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("audio_channels", sa.SmallInteger(), nullable=True))
        batch_op.add_column(
            sa.Column("audio_channel_layout", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(sa.Column("audio_bitrate", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("audio_sample_rate", sa.Integer(), nullable=True))

        batch_op.add_column(sa.Column("subtitle_count", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("subtitle_has_forced", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(sa.Column("subtitle_languages", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("has_chapters", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("movie_versions", schema=None) as batch_op:
        batch_op.drop_column("has_chapters")
        batch_op.drop_column("subtitle_languages")
        batch_op.drop_column("subtitle_has_forced")
        batch_op.drop_column("subtitle_count")
        batch_op.drop_column("audio_sample_rate")
        batch_op.drop_column("audio_bitrate")
        batch_op.drop_column("audio_channel_layout")
        batch_op.drop_column("audio_channels")
        batch_op.drop_column("audio_language")
        batch_op.drop_column("audio_title")
        batch_op.drop_column("audio_codec_family")
        batch_op.drop_column("audio_codec")
        batch_op.drop_column("audio_languages")
        batch_op.drop_column("audio_count")
        batch_op.drop_column("video_fps")
        batch_op.drop_column("video_color_transfer")
        batch_op.drop_column("video_color_space")
        batch_op.drop_column("video_color_primaries")
        batch_op.drop_column("video_resolution")
        batch_op.drop_column("video_height")
        batch_op.drop_column("video_width")
        batch_op.drop_column("video_bit_depth")
        batch_op.drop_column("video_bitrate")
        batch_op.drop_column("video_dolby_vision_profile")
        batch_op.drop_column("video_dolby_vision")
        batch_op.drop_column("video_hdr")
        batch_op.drop_column("video_codec_family")
        batch_op.drop_column("video_codec")
        batch_op.drop_column("video_track_count")
        batch_op.drop_column("duration")
        batch_op.drop_column("file_name")
