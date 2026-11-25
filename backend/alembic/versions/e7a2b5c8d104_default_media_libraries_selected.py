"""Select every media library so Sync Media keeps syncing all of them.

`selected` used to default to False and Sync Media ignored it. Rows that are
True were saved by the old library-scope picker, which never limited sync, so
honoring them now would quietly drop libraries. Start everyone from "all
libraries" and let the new picker narrow it down.
"""

from alembic import op

revision = "e7a2b5c8d104"
down_revision = "d6f1c4a8b253"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE service_media_libraries SET selected = 1")


def downgrade():
    # Data-only change. The old values meant nothing to sync, so there is
    # nothing worth restoring.
    pass
