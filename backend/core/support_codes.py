"""Exact identifiers only; support lookup never interprets SQL, paths or URLs."""

import re
from uuid import UUID


def parse_support_code(raw: str) -> tuple[str, str]:
    value = raw.strip().lower()
    job = re.fullmatch(r"job[-=]([1-9][0-9]{0,14})", value)
    if job:
        return "job", job[1]
    kind = "correlation"
    for prefix in ("scan", "operation"):
        if value.startswith((prefix + "-", prefix + "=")):
            kind, value = prefix, value[len(prefix) + 1 :]
            break
    try:
        identifier = str(UUID(value))
    except ValueError:
        raise ValueError(
            "Enter job-123, scan-UUID, operation-UUID, or a correlation UUID."
        ) from None
    return kind, identifier


def normalize_support_code(raw: str) -> str:
    kind, identifier = parse_support_code(raw)
    return identifier if kind == "correlation" else kind + "-" + identifier
