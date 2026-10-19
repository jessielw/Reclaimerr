"""Persist file identities across interrupted resyncs.

Revision ID: b3d8f0a5c217
Revises: a2c7e9f4b106
"""

import sqlalchemy as sa
from alembic import op

revision = "b3d8f0a5c217"
down_revision = "a2c7e9f4b106"
branch_labels = None
depends_on = None


def upgrade():
    for table, key, parent in (
        ("candidate_resync_identities", "candidate_id", "reclaim_candidates"),
        ("delete_request_resync_identities", "delete_request_id", "delete_requests"),
    ):
        op.create_table(
            table,
            sa.Column(
                key,
                sa.Integer(),
                sa.ForeignKey(f"{parent}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "movie_id",
                sa.Integer(),
                sa.ForeignKey("movies.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("path", sa.String(1024), nullable=False),
        )


def downgrade():
    op.drop_table("delete_request_resync_identities")
    op.drop_table("candidate_resync_identities")
