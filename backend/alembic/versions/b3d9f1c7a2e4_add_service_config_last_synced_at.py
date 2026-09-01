"""add last_synced_at to service_configs

Records when each media server was last synced. Before this, the dashboard
showed the last completed SYNC_MEDIA run against every media server row, so
two Plex servers both reported whichever one had been synced most recently.

Revision ID: b3d9f1c7a2e4
Revises: c2e5b8a1f4d7
Create Date: 2026-08-27 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d9f1c7a2e4"
down_revision: str | None = "c2e5b8a1f4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cols(table: str) -> set[str]:
    bind = op.get_bind()
    return {row[1] for row in bind.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    if "last_synced_at" not in _cols("service_configs"):
        with op.batch_alter_table("service_configs", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("last_synced_at", sa.DateTime(), nullable=True)
            )

    # Seed every media server with the last completed full media sync. That run
    # covered the main server's libraries and every linked server's watch data,
    # so it is the correct starting value for all of them; subsequent runs
    # record each server's own time.
    op.execute(
        sa.text(
            """
            UPDATE service_configs
            SET last_synced_at = (
                SELECT MAX(completed_at) FROM task_runs
                WHERE task = 'SYNC_MEDIA' AND status = 'COMPLETED'
            )
            WHERE service_type IN ('PLEX', 'JELLYFIN', 'EMBY')
              AND last_synced_at IS NULL
            """
        )
    )


def downgrade() -> None:
    if "last_synced_at" in _cols("service_configs"):
        with op.batch_alter_table("service_configs", schema=None) as batch_op:
            batch_op.drop_column("last_synced_at")
