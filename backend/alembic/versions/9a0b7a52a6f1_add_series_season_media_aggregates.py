"""add season/series aggregate mediainfo columns

Revision ID: 9a0b7a52a6f1
Revises: c5f60f5a7b29
Create Date: 2026-04-26 13:25:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a0b7a52a6f1"
down_revision: Union[str, None] = "c5f60f5a7b29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # cleanup from interrupted SQLite batch migrations so retries are safe
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_series"))
    op.execute(sa.text("DROP TABLE IF EXISTS _alembic_tmp_seasons"))

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.add_column(sa.Column("has_hdr", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("has_dolby_vision", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("max_video_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_video_height", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("video_codec_families", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("audio_codec_families", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("max_audio_channels", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("subtitle_languages", sa.JSON(), nullable=True))

    with op.batch_alter_table("seasons", schema=None) as batch_op:
        batch_op.add_column(sa.Column("has_hdr", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("has_dolby_vision", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("max_video_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_video_height", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("video_codec_families", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("audio_codec_families", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("max_audio_channels", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("subtitle_languages", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("seasons", schema=None) as batch_op:
        batch_op.drop_column("subtitle_languages")
        batch_op.drop_column("max_audio_channels")
        batch_op.drop_column("audio_codec_families")
        batch_op.drop_column("video_codec_families")
        batch_op.drop_column("max_video_height")
        batch_op.drop_column("max_video_width")
        batch_op.drop_column("has_dolby_vision")
        batch_op.drop_column("has_hdr")

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.drop_column("subtitle_languages")
        batch_op.drop_column("max_audio_channels")
        batch_op.drop_column("audio_codec_families")
        batch_op.drop_column("video_codec_families")
        batch_op.drop_column("max_video_height")
        batch_op.drop_column("max_video_width")
        batch_op.drop_column("has_dolby_vision")
        batch_op.drop_column("has_hdr")
