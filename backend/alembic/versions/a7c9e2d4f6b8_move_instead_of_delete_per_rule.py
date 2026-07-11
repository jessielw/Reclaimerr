"""move instead of delete per rule

Revision ID: a7c9e2d4f6b8
Revises: f4d8c2a7b9e1
Create Date: 2026-07-11 00:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "a7c9e2d4f6b8"
down_revision = "f4d8c2a7b9e1"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table_name)}


def _tables() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def _json_load(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        loaded = json.loads(value)
        return dict(loaded) if isinstance(loaded, dict) else {}
    return {}


def _rule_rows() -> Iterable[sa.Row[Any]]:
    return op.get_bind().execute(sa.text("SELECT id, action FROM reclaim_rules"))


def _rules_table() -> sa.TableClause:
    return sa.table(
        "reclaim_rules",
        sa.column("id", sa.Integer()),
        sa.column("action", sa.JSON()),
    )


def upgrade() -> None:
    tables = _tables()
    if "general_settings" not in tables or "reclaim_rules" not in tables:
        return

    general_columns = _columns("general_settings")
    had_global_move = False
    if "move_enabled" in general_columns:
        had_global_move = bool(
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM general_settings "
                    "WHERE move_enabled = 1 LIMIT 1"
                )
            )
            .first()
        )

    if had_global_move:
        rules_table = _rules_table()
        for row in _rule_rows():
            action = _json_load(row.action)
            if action.get("outcome") == "protect" or action.get("candidate") is False:
                continue
            action["move_instead_of_delete"] = True
            op.get_bind().execute(
                rules_table.update()
                .where(rules_table.c.id == row.id)
                .values(action=action)
            )

    if "move_enabled" in general_columns:
        with op.batch_alter_table("general_settings") as batch_op:
            batch_op.drop_column("move_enabled")


def downgrade() -> None:
    tables = _tables()
    if "general_settings" not in tables:
        return

    if "move_enabled" not in _columns("general_settings"):
        with op.batch_alter_table("general_settings") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "move_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )

    if "reclaim_rules" in tables:
        any_rule_uses_move = bool(
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM reclaim_rules "
                    "WHERE action LIKE '%move_instead_of_delete%true%' LIMIT 1"
                )
            )
            .first()
        )
        if any_rule_uses_move:
            op.get_bind().execute(
                sa.text("UPDATE general_settings SET move_enabled = 1")
            )
