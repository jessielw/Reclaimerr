"""add plex auth token fields to media user identities

Revision ID: c9d1e2f3a4b5
Revises: 2a9c4e1b7d3f
Create Date: 2026-06-01 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d1e2f3a4b5"
down_revision: str | Sequence[str] | None = "2a9c4e1b7d3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    table = "media_user_identities"
    if not _column_exists(table, "plex_auth_token"):
        op.add_column(table, sa.Column("plex_auth_token", sa.String(length=2048)))
    if not _column_exists(table, "plex_auth_token_updated_at"):
        op.add_column(table, sa.Column("plex_auth_token_updated_at", sa.DateTime()))


def downgrade() -> None:
    table = "media_user_identities"
    if _column_exists(table, "plex_auth_token_updated_at"):
        op.drop_column(table, "plex_auth_token_updated_at")
    if _column_exists(table, "plex_auth_token"):
        op.drop_column(table, "plex_auth_token")

