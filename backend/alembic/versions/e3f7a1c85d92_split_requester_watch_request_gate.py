"""split the request-date gate out of seerr.requester_has_watched

`seerr.requester_has_watched` used to mean "a requester watched every required
episode **after** requesting it". Those are two different questions, and fusing
them meant a season a requester demonstrably finished could report false with
the deciding date nowhere on screen.

The field now answers only the first question. Saved rules are rewritten to the
new `seerr.requester_watched_after_request`, which keeps the fused meaning, so
no existing rule changes what it matches.

Revision ID: e3f7a1c85d92
Revises: d2e6f0b4a81c
Create Date: 2026-08-23 00:00:00.000000
"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "e3f7a1c85d92"
down_revision: str | Sequence[str] | None = "d2e6f0b4a81c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_FIELD = "seerr.requester_has_watched"
NEW_FIELD = "seerr.requester_watched_after_request"


def _rewrite(node: Any, old: str, new: str) -> bool:
    """Rename condition fields in place, reporting whether anything changed."""
    changed = False
    if isinstance(node, dict):
        if node.get("field") == old:
            node["field"] = new
            changed = True
        for value in node.values():
            changed |= _rewrite(value, old, new)
    elif isinstance(node, list):
        for item in node:
            changed |= _rewrite(item, old, new)
    return changed


def _migrate_definitions(old: str, new: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, definition FROM reclaim_rules WHERE definition IS NOT NULL")
    ).fetchall()
    for rule_id, definition in rows:
        # Raw SQL bypasses the JSON column type, so SQLite hands back the stored
        # text. A rule that will not parse is left exactly as it is rather than
        # risking a rewrite of something unexpected.
        if isinstance(definition, str):
            try:
                parsed = json.loads(definition)
            except ValueError:
                continue
        elif isinstance(definition, (dict, list)):
            parsed = definition
        else:
            continue

        if not _rewrite(parsed, old, new):
            continue
        bind.execute(
            sa.text("UPDATE reclaim_rules SET definition = :definition WHERE id = :id"),
            {"definition": json.dumps(parsed), "id": rule_id},
        )


def upgrade() -> None:
    _migrate_definitions(OLD_FIELD, NEW_FIELD)


def downgrade() -> None:
    _migrate_definitions(NEW_FIELD, OLD_FIELD)
