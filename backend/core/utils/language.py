from __future__ import annotations

from functools import lru_cache
from typing import Any

from iso639 import Language, LanguageNotFoundError

UNKNOWN_LANGUAGE_VALUES = {
    "",
    "mis",
    "mul",
    "und",
    "undefined",
    "undetermined",
    "unknown",
}


@lru_cache(maxsize=512)
def normalize_language(value: Any) -> str | None:
    """Return a canonical ISO 639-3 code, preserving unknown custom codes."""
    raw = str(value or "").strip()
    normalized = raw.lower()
    if normalized in UNKNOWN_LANGUAGE_VALUES:
        return None

    candidates = [raw]
    tag_base = normalized.replace("_", "-").split("-", 1)[0]
    if tag_base and tag_base != normalized:
        candidates.append(tag_base)

    for candidate in candidates:
        try:
            return Language.match(candidate, strict_case=False).part3
        except LanguageNotFoundError:
            continue
    return normalized or None


def normalize_languages(values: list[Any] | None) -> list[str] | None:
    """Normalize and deduplicate language values while preserving their order."""
    normalized_values: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = normalize_language(value)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        normalized_values.append(normalized)
    return normalized_values or None
