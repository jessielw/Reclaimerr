from typing import Any


def as_float(value: Any) -> float | None:
    """Convert a value to a float, returning None if the value is None, empty, or cannot be converted."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    """Convert a value to an int, returning None if the value is None, empty, or cannot be converted."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
