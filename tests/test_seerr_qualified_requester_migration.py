"""Qualifying requester ids must not change what a saved rule matches.

A bare Seerr user id names a different person on every configured Seerr, so the
saved values are rewritten to point at the instance that issued them. If the
migration missed a value the rule would silently match nobody -- and paired with
an `is false` condition, a protect rule becomes a delete rule.
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
    / "d7f3b2a9c604_qualify_seerr_requester_ids_with_config.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("_qualify_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connection(*, seerr_config_ids: tuple[int, ...] = (7,)) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE service_configs (id INTEGER PRIMARY KEY, service_type TEXT)")
    conn.execute("CREATE TABLE reclaim_rules (id INTEGER PRIMARY KEY, definition TEXT)")
    conn.execute(
        "CREATE TABLE general_settings "
        "(id INTEGER PRIMARY KEY, requester_watch_user_mappings TEXT)"
    )
    # A media server row proves the lookup filters on service_type.
    conn.execute("INSERT INTO service_configs (id, service_type) VALUES (1, 'PLEX')")
    for config_id in seerr_config_ids:
        conn.execute(
            "INSERT INTO service_configs (id, service_type) VALUES (?, 'SEERR')",
            (config_id,),
        )
    return conn


class _Op:
    """Minimal stand-in for alembic's `op`, bound to a sqlite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_bind(self) -> Any:
        return self

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        if params:
            for key, value in params.items():
                sql = sql.replace(f":{key}", "?")
            ordered = [params[key] for key in _param_order(str(statement))]
            return self._conn.execute(sql, ordered)
        return self._conn.execute(sql)


def _param_order(sql: str) -> list[str]:
    import re

    return re.findall(r":(\w+)", sql)


def _run(module: Any, conn: sqlite3.Connection, direction: str) -> None:
    module.op = _Op(conn)
    getattr(module, direction)()


def _definition(values: Any, *, disabled: bool = False) -> dict[str, Any]:
    """A rule shaped like a real one: the requester condition inside an OR group."""
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
                    "disabled": disabled,
                    "children": [
                        {
                            "type": "condition",
                            "field": "seerr.requested_by_user_ids",
                            "operator": "contains_any",
                            "value": values,
                            "disabled": disabled,
                        }
                    ],
                },
            ],
        },
    }


def _stored_definition(conn: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT definition FROM reclaim_rules WHERE id = ?", (rule_id,)
    ).fetchone()
    return json.loads(row[0])


def _condition(definition: dict[str, Any]) -> dict[str, Any]:
    return definition["root"]["children"][1]["children"][0]


def test_bare_ids_are_qualified_with_the_configured_seerr() -> None:
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3", "12"])),),
    )

    _run(module, conn, "upgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["7:3", "7:12"]


def test_integer_and_scalar_values_are_qualified() -> None:
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition([3, 12])),),
    )
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (2, ?)",
        (json.dumps(_definition(3)),),
    )

    _run(module, conn, "upgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["7:3", "7:12"]
    assert _condition(_stored_definition(conn, 2))["value"] == ["7:3"]


def test_disabled_conditions_are_qualified_too() -> None:
    """A skipped disabled node would change meaning the moment it is re-enabled."""
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3"], disabled=True)),),
    )

    _run(module, conn, "upgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["7:3"]


def test_running_twice_does_not_double_prefix() -> None:
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3"])),),
    )

    _run(module, conn, "upgrade")
    _run(module, conn, "upgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["7:3"]


def test_no_seerr_configured_leaves_values_alone() -> None:
    module = _load_module()
    conn = _connection(seerr_config_ids=())
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3"])),),
    )

    _run(module, conn, "upgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["3"]


def test_lowest_seerr_id_wins_when_several_exist() -> None:
    module = _load_module()
    conn = _connection(seerr_config_ids=(9, 4))
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3"])),),
    )

    _run(module, conn, "upgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["4:3"]


def test_unparseable_json_is_left_untouched() -> None:
    module = _load_module()
    conn = _connection()
    conn.execute("INSERT INTO reclaim_rules (id, definition) VALUES (1, 'not json')")

    _run(module, conn, "upgrade")

    row = conn.execute("SELECT definition FROM reclaim_rules WHERE id = 1").fetchone()
    assert row[0] == "not json"


def test_mappings_are_stamped_with_the_instance() -> None:
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO general_settings (id, requester_watch_user_mappings) VALUES (1, ?)",
        (
            json.dumps(
                [
                    {"seerr_user_id": 3, "media_user_key": "alice"},
                    {"seerr_username": "bob", "media_user_key": "bob"},
                ]
            ),
        ),
    )

    _run(module, conn, "upgrade")

    row = conn.execute(
        "SELECT requester_watch_user_mappings FROM general_settings WHERE id = 1"
    ).fetchone()
    mappings = json.loads(row[0])
    assert [m["seerr_service_config_id"] for m in mappings] == [7, 7]


def test_downgrade_round_trips() -> None:
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3", "12"])),),
    )
    conn.execute(
        "INSERT INTO general_settings (id, requester_watch_user_mappings) VALUES (1, ?)",
        (json.dumps([{"seerr_user_id": 3, "media_user_key": "alice"}]),),
    )

    _run(module, conn, "upgrade")
    _run(module, conn, "downgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["3", "12"]
    row = conn.execute(
        "SELECT requester_watch_user_mappings FROM general_settings WHERE id = 1"
    ).fetchone()
    assert "seerr_service_config_id" not in json.loads(row[0])[0]


def test_downgrade_leaves_already_bare_values_alone() -> None:
    """A partial downgrade must not empty a condition it has already handled."""
    module = _load_module()
    conn = _connection()
    conn.execute(
        "INSERT INTO reclaim_rules (id, definition) VALUES (1, ?)",
        (json.dumps(_definition(["3"])),),
    )

    _run(module, conn, "downgrade")

    assert _condition(_stored_definition(conn, 1))["value"] == ["3"]
