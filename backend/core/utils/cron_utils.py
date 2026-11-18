"""Adapt standard crontab weekdays to APScheduler 3.x's Monday-based numbering."""

from __future__ import annotations

import re
from datetime import tzinfo

from apscheduler.triggers.cron import CronTrigger

_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_NUMERIC_WEEKDAY = re.compile(r"(?P<base>\*|[0-9]+(?:-[0-9]+)?)(?:/(?P<step>[0-9]+))?")
_NAMED_WEEKDAY = re.compile(r"[a-z]{3}(?:-[a-z]{3})?", re.IGNORECASE)


class InvalidCronSchedule(ValueError):
    """A task's cron expression cannot be scheduled."""


def _normalize_weekdays(expression: str) -> str:
    normalized: list[str] = []
    for part in expression.split(","):
        if part == "*" or _NAMED_WEEKDAY.fullmatch(part):
            # Leave existing named weekday ranges for APScheduler to validate.
            normalized.append(part)
            continue

        match = _NUMERIC_WEEKDAY.fullmatch(part)
        if match is None:
            raise ValueError(f"Invalid weekday expression: {part!r}")

        base, step_value = match.group("base", "step")
        step = int(step_value) if step_value is not None else 1
        if step < 1:
            raise ValueError("Weekday step must be greater than zero")

        if base == "*":
            first, last = 0, 7
        elif "-" in base:
            first, last = map(int, base.split("-"))
        else:
            first = int(base)
            last = 7 if step_value is not None else first
        if not 0 <= first <= last <= 7:
            raise ValueError(
                "Weekday values must be in ascending order between 0 and 7"
            )

        # Expand in crontab order before mapping: 0-6 spans Sunday through
        # Saturday, and */2 means Sunday, Tuesday, Thursday and Saturday.
        normalized.extend(_WEEKDAYS[day % 7] for day in range(first, last + 1, step))
    return ",".join(normalized)


def parse_cron_schedule(
    expression: str, *, timezone: str | tzinfo | None = None
) -> CronTrigger:
    """Parse five cron fields with 0/7=Sunday and 1=Monday through 6=Saturday.

    Only the weekday field is adapted; other fields and timezone behavior are
    delegated to APScheduler. Stored expressions are left unchanged.
    """
    try:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError(f"Expected 5 fields, got {len(fields)}")
        fields[4] = _normalize_weekdays(fields[4])
        return CronTrigger.from_crontab(" ".join(fields), timezone=timezone)
    except ValueError as error:
        raise InvalidCronSchedule(f"Invalid cron expression: {error}") from error
