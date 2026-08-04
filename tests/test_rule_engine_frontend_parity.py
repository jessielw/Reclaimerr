from __future__ import annotations

import re
import unittest
from pathlib import Path

from backend.core.rule_engine import (
    FIELD_LABELS,
    FIELD_NUMERIC_BOUNDS,
    USER_SCOPED_PLAYBACK_FIELDS,
)

EDITOR = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "lib"
    / "components"
    / "settings"
    / "rules"
    / "rule-node-editor.svelte"
)

ENTRY = re.compile(
    r'"(?P<field>[^"]+)":\s*\{\s*'
    r"min:\s*(?P<min>-?\d+(?:\.\d+)?),\s*"
    r"max:\s*(?P<max>null|-?\d+(?:\.\d+)?),\s*"
    r'step:\s*"(?P<step>1|any)"\s*\}'
)


def _frontend_bounds() -> dict[str, tuple[float, float | None, bool]]:
    source = EDITOR.read_text(encoding="utf-8")
    start = source.index("const NUMERIC_FIELD_BOUNDS")
    end = source.index("};", start)
    block = source[start:end]
    bounds: dict[str, tuple[float, float | None, bool]] = {}
    for match in ENTRY.finditer(block):
        maximum = None if match["max"] == "null" else float(match["max"])
        bounds[match["field"]] = (
            float(match["min"]),
            maximum,
            match["step"] == "1",
        )
    return bounds


FIELD_ENTRY = re.compile(
    r'value:\s*"(?P<field>[^"]+)",\s*\n\s*label:\s*"(?P<label>[^"]+)",\s*\n\s*'
    r'kind:\s*"(?P<kind>[^"]+)"'
)


def _frontend_field_entries() -> dict[str, tuple[str, str]]:
    """Return {field: (label, kind)} parsed from the `fields` array."""
    source = EDITOR.read_text(encoding="utf-8")
    start = source.index("const fields: FieldConfig[] = [")
    end = source.index("\n  ];", start)
    block = source[start:end]
    return {
        match["field"]: (match["label"], match["kind"])
        for match in FIELD_ENTRY.finditer(block)
    }


class FrontendBoundsParityTests(unittest.TestCase):
    def test_editor_file_exists(self) -> None:
        self.assertTrue(EDITOR.is_file(), f"missing {EDITOR}")

    def test_frontend_block_parses(self) -> None:
        self.assertTrue(
            _frontend_bounds(),
            "parsed no entries from NUMERIC_FIELD_BOUNDS; the map's formatting "
            "changed and this test's regex needs updating",
        )

    def test_frontend_and_backend_bounds_match(self) -> None:
        self.assertEqual(_frontend_bounds(), dict(FIELD_NUMERIC_BOUNDS))

    def test_user_scoped_playback_fields_match_frontend(self) -> None:
        entries = _frontend_field_entries()
        self.assertTrue(
            entries,
            "parsed no entries from the `fields` array; its formatting changed "
            "and this test's regex needs updating",
        )
        for field in USER_SCOPED_PLAYBACK_FIELDS:
            self.assertIn(field, entries, f"{field} missing from frontend fields")
            label, kind = entries[field]
            self.assertEqual(label, FIELD_LABELS[field])
            self.assertEqual(kind, "user_scoped_number")
