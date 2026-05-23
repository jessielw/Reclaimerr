from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

ANIBRIDGE_RELEASE_API_URL = (
    "https://api.github.com/repos/anibridge/anibridge-mappings/releases/latest"
)
ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

_DESC_RE = re.compile(
    r"^(?P<provider>\w+):(?P<id>\w+)(?::(?P<scope>\w+))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Descriptor:
    provider: str
    provider_id: str
    scope: str | None = None


def build_conditional_headers(
    *, etag: str | None = None, last_modified: str | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_descriptor(raw: str) -> Descriptor | None:
    match = _DESC_RE.match((raw or "").strip())
    if not match:
        return None
    return Descriptor(
        provider=(match.group("provider") or "").lower(),
        provider_id=(match.group("id") or "").strip(),
        scope=((match.group("scope") or "").strip() or None),
    )


def extract_anilist_ids(targets: Any) -> list[int]:
    if not isinstance(targets, dict):
        return []

    ids: set[int] = set()
    for target_desc in targets:
        parsed = parse_descriptor(str(target_desc))
        if not parsed or parsed.provider != "anilist":
            continue
        try:
            ids.add(int(parsed.provider_id))
        except ValueError:
            continue
    return sorted(ids)


def choose_series_anilist_id(
    *,
    source_to_anilist_ids: dict[str, list[int]],
    tmdb_id: int | None,
    imdb_id: str | None,
) -> int | None:
    def _ids_for_source(
        provider: str, provider_id: str, scope: str | None
    ) -> list[int]:
        desc = f"{provider}:{provider_id}" + (f":{scope}" if scope else "")
        return source_to_anilist_ids.get(desc, [])

    def _best_for_show(provider: str, provider_id: str) -> int | None:
        s1_ids = _ids_for_source(provider, provider_id, "s1")
        if s1_ids:
            return s1_ids[0]

        counter: Counter[int] = Counter()
        for source_desc, anilist_ids in source_to_anilist_ids.items():
            parsed = parse_descriptor(source_desc)
            if not parsed:
                continue
            if parsed.provider != provider or parsed.provider_id != provider_id:
                continue
            for anilist_id in anilist_ids:
                counter[anilist_id] += 1
        if not counter:
            return None
        top_count = max(counter.values())
        top_ids = [
            anilist_id for anilist_id, count in counter.items() if count == top_count
        ]
        return min(top_ids)

    if tmdb_id is not None:
        resolved = _best_for_show("tmdb_show", str(tmdb_id))
        if resolved is not None:
            return resolved
    if imdb_id:
        resolved = _best_for_show("imdb_show", imdb_id)
        if resolved is not None:
            return resolved
    return None
