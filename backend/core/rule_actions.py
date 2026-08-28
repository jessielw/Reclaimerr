from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from backend.core.seerr_identity import (
    is_qualified_seerr_user_id,
    qualify_seerr_user_id,
    seerr_config_id_of,
)

ArrServiceName = Literal["radarr", "sonarr"]

SEERR_REQUESTER_FIELD = "seerr.requested_by_user_ids"


def _arr_target_keys(service: ArrServiceName) -> tuple[str, str]:
    return (
        f"{service}_service_config_ids",
        f"{service}_service_config_id",
    )


def get_arr_service_config_ids(
    action: Mapping[str, Any] | None,
    service: ArrServiceName,
) -> list[int]:
    """Return deduplicated rule targets, accepting plural and legacy scalar keys.

    The plural key is authoritative when present, including when it is an empty
    list.  This lets newer clients explicitly clear a selection without a stale
    legacy scalar value restoring it.
    """
    if not action:
        return []

    plural_key, singular_key = _arr_target_keys(service)
    raw_values: list[Any]
    if plural_key in action:
        raw_plural = action.get(plural_key)
        raw_values = list(raw_plural) if isinstance(raw_plural, (list, tuple)) else []
    else:
        raw_values = [action.get(singular_key)]

    result: list[int] = []
    seen: set[int] = set()
    for value in raw_values:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize_arr_service_config_ids(
    action: dict[str, Any],
    service: ArrServiceName,
) -> list[int]:
    """Write canonical plural targets and a compatible single-target scalar."""
    plural_key, singular_key = _arr_target_keys(service)
    config_ids = get_arr_service_config_ids(action, service)
    action[plural_key] = config_ids
    action[singular_key] = config_ids[0] if len(config_ids) == 1 else None
    return config_ids


def strip_seerr_config_from_definition(
    node: Any, service_config_id: int
) -> tuple[bool, bool]:
    """Drop one Seerr instance out of every requester condition in a rule.

    Returns ``(changed, has_empty_condition)``. A condition left with no values
    is reported rather than removed: an empty value list is not "matches
    nothing" -- `not_contains_any` against it matches *everything*, which would
    silently turn a protect rule into a delete rule. The caller disables the
    rule instead. Disabled nodes are rewritten too, so re-enabling one later
    cannot resurrect a deleted instance.
    """
    changed = False
    emptied = False

    if isinstance(node, dict):
        if node.get("field") == SEERR_REQUESTER_FIELD and "value" in node:
            raw = node["value"]
            values = raw if isinstance(raw, list) else [raw]
            kept = [
                value
                for value in values
                if seerr_config_id_of(value) != service_config_id
            ]
            if len(kept) != len(values):
                changed = True
                # Always a list once rewritten: the evaluator normalizes either
                # shape, and a scalar has nowhere to put "no values left".
                node["value"] = kept
                if not kept:
                    emptied = True
        for value in node.values():
            child_changed, child_emptied = strip_seerr_config_from_definition(
                value, service_config_id
            )
            changed |= child_changed
            emptied |= child_emptied
    elif isinstance(node, list):
        for item in node:
            child_changed, child_emptied = strip_seerr_config_from_definition(
                item, service_config_id
            )
            changed |= child_changed
            emptied |= child_emptied

    return changed, emptied


def _seerr_requester_values(node: Any) -> list[Any]:
    """The requester ids a single condition node names, in list form."""
    raw = node["value"]
    return raw if isinstance(raw, list) else [raw]


def has_unqualified_seerr_requesters(node: Any) -> bool:
    """Whether any requester condition still names a user by a bare id.

    A bare id is what rules exported before Seerr became multi-instance carry.
    It is not an identity -- it names a different person on every instance -- so
    the caller either qualifies it or refuses the rule.
    """
    if isinstance(node, dict):
        if node.get("field") == SEERR_REQUESTER_FIELD and "value" in node:
            for value in _seerr_requester_values(node):
                if value is None or not str(value).strip():
                    continue
                if not is_qualified_seerr_user_id(value):
                    return True
        return any(has_unqualified_seerr_requesters(v) for v in node.values())
    if isinstance(node, list):
        return any(has_unqualified_seerr_requesters(item) for item in node)
    return False


def collect_seerr_config_ids(node: Any) -> set[int]:
    """Every Seerr instance a rule's requester conditions point at."""
    found: set[int] = set()

    if isinstance(node, dict):
        if node.get("field") == SEERR_REQUESTER_FIELD and "value" in node:
            for value in _seerr_requester_values(node):
                config_id = seerr_config_id_of(value)
                if config_id is not None:
                    found.add(config_id)
        for value in node.values():
            found |= collect_seerr_config_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= collect_seerr_config_ids(item)

    return found


def qualify_seerr_requesters_in_definition(node: Any, service_config_id: int) -> bool:
    """Attach an instance to every bare requester id in a rule definition.

    Migration ``d7f3b2a9c604`` does this for rules already in the database. A
    rule arriving through import is the same rewrite coming through a different
    door, so it follows the same rule: qualify against the one configured Seerr,
    and never guess when there are several.

    Returns whether anything changed. Idempotent -- an already-qualified value is
    left alone, so re-importing a file this has touched is a no-op. Values that
    are neither qualified nor a bare id are left as they are, so validation
    rejects the rule and names them, rather than this quietly dropping them.
    Disabled nodes are rewritten too, for the reason the migration gives: a bare
    id left behind matches nobody, and re-enabling the node later would silently
    change what the rule does.
    """
    changed = False

    if isinstance(node, dict):
        if node.get("field") == SEERR_REQUESTER_FIELD and "value" in node:
            values = _seerr_requester_values(node)
            qualified: list[Any] = []
            for value in values:
                text = str(value).strip() if value is not None else ""
                if text and not is_qualified_seerr_user_id(text) and text.isdigit():
                    qualified.append(
                        qualify_seerr_user_id(service_config_id, int(text))
                    )
                    changed = True
                else:
                    qualified.append(value)
            if changed:
                # Always a list once rewritten, matching how the strip helper
                # normalizes: the evaluator accepts either shape.
                node["value"] = qualified
        for value in node.values():
            changed |= qualify_seerr_requesters_in_definition(value, service_config_id)
    elif isinstance(node, list):
        for item in node:
            changed |= qualify_seerr_requesters_in_definition(item, service_config_id)

    return changed
