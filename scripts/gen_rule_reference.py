"""Generate docs/reference/rule-fields.md from the rule engine tables.

Run from the repo root:

    uv run python -m scripts.gen_rule_reference

tests/test_rule_reference_docs.py fails when the committed page is stale.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.rule_engine import (
    BOOLEAN_FIELDS,
    BOOLEAN_OPERATORS,
    FIELD_ALLOWED_OPERATORS,
    FIELD_LABELS,
    FIELD_NUMERIC_BOUNDS,
    LIBRARY_FIELDS,
    LIBRARY_OPERATORS,
    MULTI_VALUE_TEXT_OPERATORS,
    NUMERIC_OPERATORS,
    PATH_FIELDS,
    PATH_OPERATORS,
    REGEX_OPERATORS,
    RESCALED_FIELD_NOTES,
    SEERR_REQUESTER_FIELD,
    TAG_SUBSTRING_OPERATORS,
    TARGET_SCOPE_ALLOWED_FIELDS,
    TEMPORAL_FIELDS,
    TEMPORAL_OPERATORS,
    TEXT_OPERATORS,
    USER_SCOPED_PLAYBACK_FIELDS,
    USER_SCOPED_PLAYBACK_OPERATORS,
    VALID_TARGET_SCOPES,
)

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "reference" / "rule-fields.md"

SCOPE_ORDER = ("movie_version", "series", "season", "episode")

# Each field's allowed-operator set must match exactly one group. A new set
# raises below, so it gets a name and an explanation instead of a silent table.
OPERATOR_GROUPS: tuple[tuple[str, frozenset[str], str], ...] = (
    (
        "Numbers",
        frozenset(NUMERIC_OPERATORS),
        "`value` is a single number.",
    ),
    (
        "Dates",
        frozenset(TEMPORAL_OPERATORS),
        '`value` is an ISO date string such as `"2024-01-31"`. A value that is '
        "not a valid date never matches.",
    ),
    (
        "True / false",
        frozenset(BOOLEAN_OPERATORS),
        "Every operator here is valueless: omit `value`.",
    ),
    (
        "Text",
        frozenset(TEXT_OPERATORS),
        "`equals` / `not_equals` take one string. The list operators take an "
        "array of strings. Comparisons ignore case.",
    ),
    (
        "Multi-value text",
        frozenset(MULTI_VALUE_TEXT_OPERATORS),
        "The item can hold several values (for example several genres). "
        "`value` is an array of strings.",
    ),
    (
        "ID lists",
        frozenset(LIBRARY_OPERATORS),
        "`value` is an array of strings.",
    ),
    (
        "Paths",
        frozenset(PATH_OPERATORS),
        "Like Text, plus `matches_any_regex` with an array of patterns.",
    ),
    (
        "Arr tags",
        frozenset(TEXT_OPERATORS | TAG_SUBSTRING_OPERATORS | REGEX_OPERATORS),
        "Like Text, plus substring and regex operators. Substring and regex "
        "operators take an array of strings.",
    ),
    (
        "Per-user playback",
        frozenset(USER_SCOPED_PLAYBACK_OPERATORS),
        '`value` is an object: `{"usernames": ["alice"], "amount": 30}`. At '
        "least one username is required; `amount` is 0 or greater.",
    ),
)

# Units and formats the field key alone does not reveal.
VALUE_NOTES: dict[str, str] = {
    "media.size": "bytes",
    "disk.free_bytes": "bytes",
    "media.duration": "milliseconds",
    "media.path": "`equals` matches a folder prefix",
    "disk.free_percent": "0-100",
    "season.watched_percent": "0-100",
    "series.watched_percent": "0-100",
    "playback.user_watched_percent": "amount is 0-100",
    "playback.user_watched_duration_minutes": "amount is minutes, max 100000",
    "video.bitrate_kbps": "kbps",
    "audio.bitrate_kbps": "kbps",
    "library.id": "install-specific library IDs",
    "arr.movie_ids": "install-specific Radarr IDs",
    "arr.series_ids": "install-specific Sonarr IDs",
    SEERR_REQUESTER_FIELD: '`"instanceId:userId"`, install-specific',
    "playback.usernames": "install-specific usernames",
    "playback_history.usernames": "install-specific usernames; selected provider only",
    "playback_history.has_completed_watch": "explicit provider completion; episode completion for TV targets",
    "playback_history.last_completed_at": "latest explicit completion; episode completion for TV targets",
    "playback.fully_watched_usernames": "install-specific usernames",
    "season.partial_viewer_usernames": "usernames with some but not all episodes of this or an earlier regular season watched; `not_exists` is portable",
    "favorites.usernames": "install-specific usernames",
    "audio.languages": "ISO 639 codes, e.g. `eng`",
    "subtitle.languages": "ISO 639 codes, e.g. `eng`",
    "tmdb.original_language": "ISO 639 codes, e.g. `en`",
    "tmdb.origin_country": "ISO 3166 codes, e.g. `US`",
}


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _value_note(field: str) -> str:
    notes: list[str] = []
    if field in FIELD_NUMERIC_BOUNDS:
        minimum, maximum, integer_required = FIELD_NUMERIC_BOUNDS[field]
        kind = "whole number" if integer_required else "number"
        if maximum is None:
            notes.append(f"{kind} >= {_format_number(minimum)}")
        else:
            notes.append(f"{kind} {_format_number(minimum)}-{_format_number(maximum)}")
    if field in RESCALED_FIELD_NOTES:
        notes.append(RESCALED_FIELD_NOTES[field])
    if field in VALUE_NOTES:
        notes.append(VALUE_NOTES[field])
    elif field.endswith("_minutes"):
        notes.append("minutes")
    elif ".days_since_" in field:
        notes.append("days")
    return "; ".join(notes)


def _group_for(field: str) -> str:
    operators = frozenset(FIELD_ALLOWED_OPERATORS.get(field, set()))
    for name, group_operators, _ in OPERATOR_GROUPS:
        if operators == group_operators:
            return name
    raise ValueError(
        f"{field} has an operator set with no group in OPERATOR_GROUPS: "
        f"{sorted(operators)}"
    )


def _scopes_for(field: str) -> str:
    scopes = [
        scope
        for scope in SCOPE_ORDER
        if field in TARGET_SCOPE_ALLOWED_FIELDS.get(scope, set())
    ]
    if len(scopes) == len(SCOPE_ORDER):
        return "all"
    return ", ".join(scopes) or "none"


def render() -> str:
    """Render the full markdown page."""
    if set(SCOPE_ORDER) != VALID_TARGET_SCOPES:
        raise ValueError("SCOPE_ORDER is out of sync with VALID_TARGET_SCOPES")
    # sanity: these sets must still map to the groups their notes describe
    assert all(_group_for(f) == "True / false" for f in BOOLEAN_FIELDS)
    assert all(_group_for(f) == "Dates" for f in TEMPORAL_FIELDS)
    assert all(_group_for(f) == "Paths" for f in PATH_FIELDS)
    assert all(_group_for(f) == "ID lists" for f in LIBRARY_FIELDS)
    assert all(
        _group_for(f) == "Per-user playback" for f in USER_SCOPED_PLAYBACK_FIELDS
    )

    fields_by_group: dict[str, list[str]] = {name: [] for name, _, _ in OPERATOR_GROUPS}
    for field in sorted(FIELD_LABELS):
        fields_by_group[_group_for(field)].append(field)

    lines = [
        "<!-- Generated by scripts/gen_rule_reference.py. Do not edit by hand. -->",
        "",
        "# Rule Fields",
        "",
        "Every field a rule condition can use, generated from the rule engine. "
        "See [Rule Format](rule-format.md) for how conditions fit into a rule file.",
        "",
        "Fields are grouped by the operators they accept. The **Scopes** column "
        "lists the `target_scope` values where the field is allowed (`all` means "
        "every scope); import rejects a rule that uses a field outside its scope.",
        "",
        "Fields marked install-specific hold IDs or names that differ between "
        "Reclaimerr installs. Rules that use them must be edited after import.",
        "",
    ]
    for name, operators, description in OPERATOR_GROUPS:
        fields = fields_by_group[name]
        if not fields:
            continue
        operator_list = ", ".join(f"`{op}`" for op in sorted(operators))
        lines += [
            f"## {name}",
            "",
            f"Operators: {operator_list}",
            "",
            description,
            "",
            "| Field | Label | Scopes | Value notes |",
            "| --- | --- | --- | --- |",
        ]
        for field in fields:
            lines.append(
                f"| `{field}` | {FIELD_LABELS[field]} | {_scopes_for(field)} "
                f"| {_value_note(field)} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUTPUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT}")
    print('Run "npm run format:docs" to align the tables.')


if __name__ == "__main__":
    main()
