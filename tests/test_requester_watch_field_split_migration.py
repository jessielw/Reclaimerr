"""The requester-watch split must not change what a saved rule matches.

`seerr.requester_has_watched` used to include the request-date gate. Splitting
that out silently redefines every rule that already used the field -- and for a
"delete once the requester has watched it" rule that means deleting more -- so
the migration moves saved conditions onto the field that kept the old meaning.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "alembic"
    / "versions"
    / "e3f7a1c85d92_split_requester_watch_request_gate.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("_split_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nested_definition(field: str) -> dict[str, Any]:
    """A rule shaped like the reporter's: the field nested inside an OR group."""
    return {
        "version": 1,
        "root": {
            "type": "group",
            "op": "and",
            "children": [
                {
                    "type": "condition",
                    "field": "media.days_since_added",
                    "operator": "greater_than",
                    "value": 180,
                },
                {
                    "type": "group",
                    "op": "or",
                    "children": [
                        {
                            "type": "condition",
                            "field": field,
                            "operator": "is_false",
                        },
                        {
                            "type": "condition",
                            "field": "arr.tags",
                            "operator": "contains",
                            "value": "delete",
                        },
                    ],
                },
            ],
        },
    }


def test_rewrite_reaches_nested_conditions_and_leaves_others_alone() -> None:
    module = _load_module()
    definition = _nested_definition(module.OLD_FIELD)

    assert module._rewrite(definition, module.OLD_FIELD, module.NEW_FIELD) is True

    or_group = definition["root"]["children"][1]
    assert or_group["children"][0]["field"] == module.NEW_FIELD
    # Untouched siblings keep their field and their value.
    assert or_group["children"][1]["field"] == "arr.tags"
    assert definition["root"]["children"][0]["value"] == 180
    # Nothing left to do the second time.
    assert module._rewrite(definition, module.OLD_FIELD, module.NEW_FIELD) is False


def test_upgrade_then_downgrade_round_trips_saved_rules(tmp_path: Path) -> None:
    module = _load_module()
    db_path = tmp_path / "rules.db"
    original = _nested_definition(module.OLD_FIELD)
    untouched = {
        "version": 1,
        "root": {
            "type": "condition",
            "field": "media.size",
            "operator": "greater_than",
            "value": 1,
        },
    }

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE reclaim_rules (id INTEGER, definition TEXT)")
        connection.execute(
            "INSERT INTO reclaim_rules VALUES (1, ?)", (json.dumps(original),)
        )
        connection.execute(
            "INSERT INTO reclaim_rules VALUES (2, ?)", (json.dumps(untouched),)
        )
        # A row that cannot be parsed must survive untouched rather than break
        # the upgrade or get rewritten into something unexpected.
        connection.execute("INSERT INTO reclaim_rules VALUES (3, 'not json')")
        connection.commit()

        def run(old: str, new: str) -> None:
            bind = sqlite3.connect(db_path)
            try:
                rows = bind.execute(
                    "SELECT id, definition FROM reclaim_rules "
                    "WHERE definition IS NOT NULL"
                ).fetchall()
                for rule_id, definition in rows:
                    try:
                        parsed = json.loads(definition)
                    except ValueError:
                        continue
                    if not module._rewrite(parsed, old, new):
                        continue
                    bind.execute(
                        "UPDATE reclaim_rules SET definition = ? WHERE id = ?",
                        (json.dumps(parsed), rule_id),
                    )
                bind.commit()
            finally:
                bind.close()

        run(module.OLD_FIELD, module.NEW_FIELD)
        stored = json.loads(
            connection.execute(
                "SELECT definition FROM reclaim_rules WHERE id = 1"
            ).fetchone()[0]
        )
        assert (
            stored["root"]["children"][1]["children"][0]["field"] == module.NEW_FIELD
        )

        run(module.NEW_FIELD, module.OLD_FIELD)
        assert (
            json.loads(
                connection.execute(
                    "SELECT definition FROM reclaim_rules WHERE id = 1"
                ).fetchone()[0]
            )
            == original
        )
        assert (
            json.loads(
                connection.execute(
                    "SELECT definition FROM reclaim_rules WHERE id = 2"
                ).fetchone()[0]
            )
            == untouched
        )
        assert (
            connection.execute(
                "SELECT definition FROM reclaim_rules WHERE id = 3"
            ).fetchone()[0]
            == "not json"
        )
    finally:
        connection.close()
