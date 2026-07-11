"""Drop global auto-delete enabled setting.

Revision ID: f4d8c2a7b9e1
Revises: e2b7c9d4a1f8
Create Date: 2026-07-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "f4d8c2a7b9e1"
down_revision: str | None = "e2b7c9d4a1f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    return {column["name"] for column in inspect(bind).get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns("general_settings")
    if "auto_delete_enabled" in cols:
        inspector = inspect(bind)
        if inspector.has_table("admin_notices"):
            had_enabled_auto_delete = bool(
                bind.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM general_settings "
                        "WHERE auto_delete_enabled = 1"
                    )
                ).scalar()
            )
            if had_enabled_auto_delete:
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO admin_notices (
                            kind,
                            title,
                            message,
                            severity,
                            action_label,
                            action_href,
                            is_active,
                            is_read,
                            dedupe_key
                        )
                        SELECT
                            'auto_delete_rule_opt_in',
                            'Auto-delete is now configured per rule',
                            'The global auto-delete switch has been removed. The Delete Cleanup Candidates task can still be scheduled, but it only deletes candidates from cleanup rules that explicitly enable automatic deletion. Review your rules and enable auto-delete on the specific rules that should delete candidates after their review period.',
                            'warning',
                            'Review rules',
                            '/rules',
                            1,
                            0,
                            'auto_delete_rule_opt_in_required'
                        WHERE NOT EXISTS (
                            SELECT 1 FROM admin_notices
                            WHERE dedupe_key = 'auto_delete_rule_opt_in_required'
                        )
                        """
                    )
                )
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.drop_column("auto_delete_enabled")


def downgrade() -> None:
    cols = _columns("general_settings")
    if "auto_delete_enabled" not in cols:
        with op.batch_alter_table("general_settings", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "auto_delete_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
