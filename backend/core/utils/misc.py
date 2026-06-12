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

    return names or None
