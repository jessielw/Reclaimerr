from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

ArrServiceName = Literal["radarr", "sonarr"]


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
