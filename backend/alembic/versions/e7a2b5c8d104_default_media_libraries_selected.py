"""Default existing media libraries to selected for Sync Media."""

from alembic import op

revision = "e7a2b5c8d104"
down_revision = "d6f1c4a8b253"
branch_labels = None
depends_on = None


def upgrade():
    # Previously `selected` defaulted to False and was ignored during Sync Media.
    # Backfill existing rows to True when no library has been selected yet so
    # upgrading preserves existing full-library sync behavior.
    op.execute(
        """
        UPDATE service_media_libraries
        SET selected = 1
        WHERE NOT EXISTS (
            SELECT 1 FROM service_media_libraries WHERE selected = 1
        )
        """
    )


def downgrade():
    # Data-only backfill; existing selections cannot be reliably reverted.
    pass
