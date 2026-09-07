"""Split the Leaving Soon base title into explicit movie/series collection titles.

Revision ID: d2b6f4a8c135
Revises: b7f0c3e5a294
Create Date: 2026-09-07 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d2b6f4a8c135"
down_revision: str | Sequence[str] | None = "b7f0c3e5a294"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MOVIE_SUFFIX = " [Movies]"
_SERIES_SUFFIX = " [Series]"
_DEFAULT_BASE = "Leaving Soon"


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _cols("general_settings")
    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "leaving_soon_movie_collection_title" not in cols:
            batch_op.add_column(
                sa.Column(
                    "leaving_soon_movie_collection_title",
                    sa.String(length=255),
                    nullable=False,
                    server_default=sa.text(f"'{_DEFAULT_BASE}{_MOVIE_SUFFIX}'"),
                )
            )
        if "leaving_soon_series_collection_title" not in cols:
            batch_op.add_column(
                sa.Column(
                    "leaving_soon_series_collection_title",
                    sa.String(length=255),
                    nullable=False,
                    server_default=sa.text(f"'{_DEFAULT_BASE}{_SERIES_SUFFIX}'"),
                )
            )

    # seed the new titles from the base title so an existing rename survives,
    # matching the `<base> [Movies]` / `<base> [Series]` names the media server
    # clients used to build themselves.
    if "leaving_soon_collection_title" in cols:
        bind = op.get_bind()
        bind.execute(
            sa.text(
                "UPDATE general_settings SET "
                "leaving_soon_movie_collection_title = "
                "  COALESCE(NULLIF(TRIM(leaving_soon_collection_title), ''), :base) "
                "  || :movie_suffix, "
                "leaving_soon_series_collection_title = "
                "  COALESCE(NULLIF(TRIM(leaving_soon_collection_title), ''), :base) "
                "  || :series_suffix"
            ),
            {
                "base": _DEFAULT_BASE,
                "movie_suffix": _MOVIE_SUFFIX,
                "series_suffix": _SERIES_SUFFIX,
            },
        )
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("leaving_soon_collection_title")

    # `leaving_soon_last_success_titles` is JSON and changes shape from
    # {config_id: <base title>} to {config_id: {"movies": ..., "series": ...}}.
    # It is migrated lazily on read (see normalize_leaving_soon_titles) so an
    # in-flight rename is still cleanable without touching JSON in SQL here.


def downgrade() -> None:
    cols = _cols("general_settings")
    if "leaving_soon_collection_title" not in cols:
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "leaving_soon_collection_title",
                    sa.String(length=255),
                    nullable=False,
                    server_default=sa.text(f"'{_DEFAULT_BASE}'"),
                )
            )
        if "leaving_soon_movie_collection_title" in cols:
            # recover the base title by stripping the suffix we appended above
            bind = op.get_bind()
            bind.execute(
                sa.text(
                    "UPDATE general_settings SET leaving_soon_collection_title = "
                    "CASE WHEN leaving_soon_movie_collection_title "
                    "          LIKE '%' || :movie_suffix "
                    "     THEN SUBSTR("
                    "       leaving_soon_movie_collection_title, 1, "
                    "       LENGTH(leaving_soon_movie_collection_title)"
                    "       - LENGTH(:movie_suffix)) "
                    "     ELSE :base END"
                ),
                {"base": _DEFAULT_BASE, "movie_suffix": _MOVIE_SUFFIX},
            )

    with op.batch_alter_table("general_settings", schema=None) as batch_op:
        if "leaving_soon_series_collection_title" in cols:
            batch_op.drop_column("leaving_soon_series_collection_title")
        if "leaving_soon_movie_collection_title" in cols:
            batch_op.drop_column("leaving_soon_movie_collection_title")
