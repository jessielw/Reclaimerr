from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.enums import MediaType

MAX_AUTO_DELETE_DELAY_DAYS = 3650


@dataclass(slots=True, frozen=True)
class AutoDeletePolicy:
    is_enabled: bool
    delay_days: int
    eligible_at: datetime
    is_eligible: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _valid_override(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_AUTO_DELETE_DELAY_DAYS:
        return None
    return value


def resolve_auto_delete_policy(
    *,
    media_type: MediaType,
    matched_rule_ids: Sequence[int],
    created_at: datetime,
    rule_actions_by_id: Mapping[int, Mapping[str, Any] | None],
    movie_delay_days: int,
    series_delay_days: int,
    now: datetime | None = None,
) -> AutoDeletePolicy:
    """Resolve the current auto-delete deadline for a candidate."""

    global_delay = (
        movie_delay_days if media_type is MediaType.MOVIE else series_delay_days
    )
    global_delay = min(max(global_delay, 0), MAX_AUTO_DELETE_DELAY_DAYS)
    matched_delays: list[int] = []
    for rule_id in matched_rule_ids:
        action = rule_actions_by_id.get(rule_id)
        if not action or action.get("auto_delete_enabled") is not True:
            continue
        override = _valid_override(action.get("auto_delete_delay_days"))
        matched_delays.append(override if override is not None else global_delay)

    is_enabled = bool(matched_delays)
    delay_days = max(matched_delays, default=global_delay)
    eligible_at = _as_utc(created_at) + timedelta(days=delay_days)
    effective_now = _as_utc(now) if now is not None else datetime.now(UTC)
    return AutoDeletePolicy(
        is_enabled=is_enabled,
        delay_days=delay_days,
        eligible_at=eligible_at,
        is_eligible=is_enabled and effective_now >= eligible_at,
    )
