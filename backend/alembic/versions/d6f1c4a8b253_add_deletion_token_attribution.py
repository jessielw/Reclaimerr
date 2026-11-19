"""Attribute deletion request reviews and schedules to API tokens."""

import sqlalchemy as sa
from alembic import op

revision = "d6f1c4a8b253"
down_revision = "c5e0a3b7d942"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite can add a nullable referencing column in place. Rebuilding
    # delete_requests would touch its dependent schedules and candidates.
    op.execute(
        "ALTER TABLE delete_requests ADD COLUMN reviewed_by_api_token_id "
        "INTEGER REFERENCES api_tokens (id) ON DELETE SET NULL"
    )
    op.add_column(
        "delete_requests", sa.Column("reviewed_by_api_token_name", sa.String(100))
    )
    op.execute(
        "ALTER TABLE scheduled_deletions ADD COLUMN created_by_api_token_id "
        "INTEGER REFERENCES api_tokens (id) ON DELETE SET NULL"
    )


def downgrade():
    op.execute("ALTER TABLE scheduled_deletions DROP COLUMN created_by_api_token_id")
    op.execute("ALTER TABLE delete_requests DROP COLUMN reviewed_by_api_token_name")
    op.execute("ALTER TABLE delete_requests DROP COLUMN reviewed_by_api_token_id")
