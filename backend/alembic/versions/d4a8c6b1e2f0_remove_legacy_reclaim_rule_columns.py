"""remove legacy reclaim rule columns

Revision ID: d4a8c6b1e2f0
Revises: b19b2f1f0f7a
Create Date: 2026-05-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "d4a8c6b1e2f0"
down_revision: Union[str, None] = "b19b2f1f0f7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


LEGACY_RULE_COLUMNS: tuple[str, ...] = (
    "library_ids",
    "min_popularity",
    "max_popularity",
    "min_vote_average",
    "max_vote_average",
    "min_vote_count",
    "max_vote_count",
    "min_view_count",
    "max_view_count",
    "include_never_watched",
    "min_days_since_added",
    "max_days_since_added",
    "min_days_since_last_watched",
    "max_days_since_last_watched",
    "min_size",
    "max_size",
    "paths",
    "series_status",
    "video_codec_families_in",
    "video_codec_families_not_in",
    "audio_codec_families_in",
    "audio_codec_families_not_in",
    "has_hdr",
    "has_dolby_vision",
    "min_video_width",
    "max_video_width",
    "min_video_height",
    "max_video_height",
    "min_audio_channels",
    "max_audio_channels",
    "min_duration",
    "max_duration",
    "min_audio_track_count",
    "max_audio_track_count",
    "audio_languages_in",
    "audio_languages_not_in",
    "subtitle_languages_in",
    "subtitle_languages_not_in",
    "video_color_spaces_in",
    "video_color_spaces_not_in",
    "video_color_transfers_in",
    "video_color_transfers_not_in",
    "video_color_primaries_in",
    "video_color_primaries_not_in",
)


def _legacy_rule_column_defs() -> tuple[sa.Column, ...]:
    return (
        sa.Column("library_ids", sa.JSON(), nullable=True),
        sa.Column("min_popularity", sa.Float(), nullable=True),
        sa.Column("max_popularity", sa.Float(), nullable=True),
        sa.Column("min_vote_average", sa.Float(), nullable=True),
        sa.Column("max_vote_average", sa.Float(), nullable=True),
        sa.Column("min_vote_count", sa.Integer(), nullable=True),
        sa.Column("max_vote_count", sa.Integer(), nullable=True),
        sa.Column("min_view_count", sa.Integer(), nullable=True),
        sa.Column("max_view_count", sa.Integer(), nullable=True),
        sa.Column(
            "include_never_watched",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("min_days_since_added", sa.Integer(), nullable=True),
        sa.Column("max_days_since_added", sa.Integer(), nullable=True),
        sa.Column("min_days_since_last_watched", sa.Integer(), nullable=True),
        sa.Column("max_days_since_last_watched", sa.Integer(), nullable=True),
        sa.Column("min_size", sa.Integer(), nullable=True),
        sa.Column("max_size", sa.Integer(), nullable=True),
        sa.Column("paths", sa.JSON(), nullable=True),
        sa.Column("series_status", sa.JSON(), nullable=True),
        sa.Column("video_codec_families_in", sa.JSON(), nullable=True),
        sa.Column("video_codec_families_not_in", sa.JSON(), nullable=True),
        sa.Column("audio_codec_families_in", sa.JSON(), nullable=True),
        sa.Column("audio_codec_families_not_in", sa.JSON(), nullable=True),
        sa.Column("has_hdr", sa.Boolean(), nullable=True),
        sa.Column("has_dolby_vision", sa.Boolean(), nullable=True),
        sa.Column("min_video_width", sa.Integer(), nullable=True),
        sa.Column("max_video_width", sa.Integer(), nullable=True),
        sa.Column("min_video_height", sa.Integer(), nullable=True),
        sa.Column("max_video_height", sa.Integer(), nullable=True),
        sa.Column("min_audio_channels", sa.SmallInteger(), nullable=True),
        sa.Column("max_audio_channels", sa.SmallInteger(), nullable=True),
        sa.Column("min_duration", sa.Float(), nullable=True),
        sa.Column("max_duration", sa.Float(), nullable=True),
        sa.Column("min_audio_track_count", sa.Integer(), nullable=True),
        sa.Column("max_audio_track_count", sa.Integer(), nullable=True),
        sa.Column("audio_languages_in", sa.JSON(), nullable=True),
        sa.Column("audio_languages_not_in", sa.JSON(), nullable=True),
        sa.Column("subtitle_languages_in", sa.JSON(), nullable=True),
        sa.Column("subtitle_languages_not_in", sa.JSON(), nullable=True),
        sa.Column("video_color_spaces_in", sa.JSON(), nullable=True),
        sa.Column("video_color_spaces_not_in", sa.JSON(), nullable=True),
        sa.Column("video_color_transfers_in", sa.JSON(), nullable=True),
        sa.Column("video_color_transfers_not_in", sa.JSON(), nullable=True),
        sa.Column("video_color_primaries_in", sa.JSON(), nullable=True),
        sa.Column("video_color_primaries_not_in", sa.JSON(), nullable=True),
    )


def _existing_columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    required_tables = {"reclaim_candidates", "reclaim_rules"}
    missing = sorted(required_tables - table_names)
    if missing:
        raise RuntimeError(
            "Migration d4a8c6b1e2f0 expected tables to exist but missing: "
            + ", ".join(missing)
        )

    op.execute(sa.text("DELETE FROM reclaim_candidates"))
    op.execute(sa.text("DELETE FROM reclaim_rules"))

    rule_columns = _existing_columns(inspector, "reclaim_rules")
    columns_to_drop = [name for name in LEGACY_RULE_COLUMNS if name in rule_columns]
    if not columns_to_drop:
        return

    with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
        for column_name in columns_to_drop:
            batch_op.drop_column(column_name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "reclaim_rules" not in inspector.get_table_names():
        return

    existing = _existing_columns(inspector, "reclaim_rules")

    with op.batch_alter_table("reclaim_rules", schema=None) as batch_op:
        for column in _legacy_rule_column_defs():
            if column.name not in existing:
                batch_op.add_column(column)
