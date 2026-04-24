from __future__ import annotations

from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    """Treat naive datetimes as UTC and normalize aware values to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_utc_isoformat(value: datetime | None) -> str | None:
    """Convert a datetime to ISO format in UTC, treating naive datetimes as UTC."""
    if value is None:
        return None
    return ensure_utc(value).isoformat()
