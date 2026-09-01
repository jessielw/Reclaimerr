"""qualify saved Seerr requester ids with the instance that issued them

A Seerr user id is only unique inside the Seerr that issued it. With one Seerr
configured that never mattered, so rules stored a bare ``"3"`` and requester
watch mappings stored a bare ``seerr_user_id``. Now that several Seerrs can be
configured at once, ``3`` names a different person on each of them.

Saved values are rewritten to ``"<service_config_id>:<user_id>"`` and mappings
gain ``seerr_service_config_id``, both pointing at the Seerr that is configured
today -- so no existing rule changes what it matches.

Two deliberate choices:

* Disabled condition nodes are rewritten too. Skipping them would leave a bare
  id that matches nothing, so re-enabling the rule later would silently change
  what it does.
* With no Seerr configured there is nothing to qualify against, so the rewrite
  is skipped rather than guessed at. Those rules already match nothing without a
  Seerr, and the user re-picks requesters when they add one.

Revision ID: d7f3b2a9c604
Revises: b3d9f1c7a2e4
Create Date: 2026-08-27 00:00:00.000000
"""

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "d7f3b2a9c604"
down_revision: str | Sequence[str] | None = "b3d9f1c7a2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUESTER_FIELD = "seerr.requested_by_user_ids"
QUALIFIED = re.compile(r"^(\d+):(\d+)$")
BARE = re.compile(r"^\d+$")


def _seerr_config_id() -> int | None:
    """The Seerr every saved requester id must have come from, if there is one."""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT id FROM service_configs "
            "WHERE service_type = 'SEERR' ORDER BY id LIMIT 1"
        )
    ).fetchone()
    return int(row[0]) if row else None


def _qualify_value(value: Any, config_id: int) -> Any:
    text = str(value).strip()
    if QUALIFIED.match(text):
        return text  # already qualified; a re-run must not double-prefix
    if BARE.match(text):
        return f"{config_id}:{text}"
    return None  # unparseable; a value that could never match is worse than none


def _unqualify_value(value: Any) -> Any:
    text = str(value).strip()
    match = QUALIFIED.match(text)
    # Anything not qualified is passed through: on a partial downgrade a bare id
    # is already in the target shape, and dropping it would empty the condition.
    return match.group(2) if match else text


def _rewrite_conditions(node: Any, transform) -> bool:
    """Apply ``transform`` to every requester condition value, in place."""
    changed = False
    if isinstance(node, dict):
        if node.get("field") == REQUESTER_FIELD and node.get("value") is not None:
            raw = node["value"]
            values = raw if isinstance(raw, list) else [raw]
            rewritten = [
                result for value in values if (result := transform(value)) is not None
            ]
            if rewritten != [str(value).strip() for value in values]:
                node["value"] = rewritten
                changed = True
        for value in node.values():
            changed |= _rewrite_conditions(value, transform)
    elif isinstance(node, list):
        for item in node:
            changed |= _rewrite_conditions(item, transform)
    return changed


def _migrate_rule_definitions(transform) -> None:
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

        if not _rewrite_conditions(parsed, transform):
            continue
        bind.execute(
            sa.text("UPDATE reclaim_rules SET definition = :definition WHERE id = :id"),
            {"definition": json.dumps(parsed), "id": rule_id},
        )


def _migrate_requester_mappings(config_id: int | None) -> None:
    """Stamp (or strip) the instance on every requester watch mapping."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, requester_watch_user_mappings FROM general_settings "
            "WHERE requester_watch_user_mappings IS NOT NULL"
        )
    ).fetchall()
    for settings_id, raw in rows:
        if isinstance(raw, str):
            try:
                mappings = json.loads(raw)
            except ValueError:
                continue
        elif isinstance(raw, list):
            mappings = raw
        else:
            continue
        if not isinstance(mappings, list):
            continue

        changed = False
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            if config_id is None:
                if mapping.pop("seerr_service_config_id", None) is not None:
                    changed = True
            elif mapping.get("seerr_service_config_id") is None:
                mapping["seerr_service_config_id"] = config_id
                changed = True

        if not changed:
            continue
        bind.execute(
            sa.text(
                "UPDATE general_settings SET requester_watch_user_mappings = :value "
                "WHERE id = :id"
            ),
            {"value": json.dumps(mappings), "id": settings_id},
        )


def upgrade() -> None:
    config_id = _seerr_config_id()
    if config_id is None:
        return
    _migrate_rule_definitions(lambda value: _qualify_value(value, config_id))
    _migrate_requester_mappings(config_id)


def downgrade() -> None:
    _migrate_rule_definitions(_unqualify_value)
    _migrate_requester_mappings(None)
