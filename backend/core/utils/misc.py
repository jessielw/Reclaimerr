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


def normalize_genre_names(genres: Any) -> list[str] | None:
    """Extract genre names from TMDB genre objects or plain string lists."""
    if not genres:
        return None

    names: list[str] = []
    for genre in genres if isinstance(genres, list) else [genres]:
        if isinstance(genre, dict):
            value = genre.get("name")
        else:
            value = genre
        name = str(value or "").strip()
        if name:
            names.append(name)

    return normalize_name_list(names)


def normalize_name_list(values: Any) -> list[str] | None:
    """Normalize arbitrary name values into a sorted, case-insensitive unique list."""
    if values is None:
        return None

    seen: dict[str, str] = {}
    for value in values if isinstance(values, list) else [values]:
        name = str(value or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen[key] = name

    names = sorted(seen.values(), key=lambda item: item.lower())
    return names or None
