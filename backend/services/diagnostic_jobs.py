"""Bounded target associations from duplicate-job inputs, without provider calls."""

from collections.abc import Callable
from typing import Any

from backend.models.diagnostics import Target


def positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def duplicate_targets(
    payload: dict, query: Callable[..., list[dict[str, Any]]]
) -> tuple[list[Target], bool]:
    selected: list[Target] = []
    truncated = False

    def add(scope, identifier):
        nonlocal truncated
        if positive_id(identifier):
            target = Target(scope=scope, id=identifier)
            if target not in selected:
                if len(selected) < 100:
                    selected.append(target)
                else:
                    truncated = True

    items = payload.get("items")
    if isinstance(items, list):
        truncated |= len(items) > 100
        for item in items[:100]:
            if not isinstance(item, dict) or not positive_id(item.get("item_id")):
                continue
            if item.get("media_type") == "movie":
                versions = item.get("version_ids")
                if isinstance(versions, list) and versions:
                    truncated |= len(versions) > 100
                    for identifier in versions[:100]:
                        add("movie_version", identifier)
                else:
                    add("movie", item["item_id"])
            elif item.get("media_type") == "series":
                add("episode", item["item_id"])
    leftovers = payload.get("leftovers")
    if isinstance(leftovers, list):
        truncated |= len(leftovers) > 100
        ids = [
            v["id"]
            for v in leftovers[:100]
            if isinstance(v, dict) and positive_id(v.get("id"))
        ]
        if ids:
            for row in query(
                "SELECT movie_id FROM upgrade_leftovers WHERE id IN ("
                + ",".join("?" for _ in ids)
                + ") ORDER BY id LIMIT 100",
                tuple(ids),
            ):
                add("movie", row["movie_id"])
    return selected, truncated


def duplicate_target_filter(target: Target, context: dict) -> tuple[str, tuple]:
    # Restrict JSON work to the same cell budget used by the exported context.
    payload = "CASE WHEN length(payload)<=65536 AND json_valid(payload) THEN payload ELSE '{}' END"
    item = "CASE WHEN j.type='object' THEN j.value ELSE '{}' END"
    if target.scope in ("movie", "movie_version"):
        where = f"json_extract({item},'$.media_type')='movie' AND json_extract({item},'$.item_id')=?"
        params = (context["movie_id"],)
        if target.scope == "movie_version":
            where += f" AND EXISTS (SELECT 1 FROM json_each({item},'$.version_ids') v WHERE v.type='integer' AND v.value=?)"
            params += (target.id,)
    else:
        episodes = {
            "series": "SELECT e.id FROM episodes e JOIN seasons s ON s.id=e.season_id WHERE s.series_id=?",
            "season": "SELECT id FROM episodes WHERE season_id=?",
            "episode": "SELECT ?",
        }[target.scope]
        where = f"json_extract({item},'$.media_type')='series' AND json_extract({item},'$.item_id') IN ({episodes})"
        params = (target.id,)
    clause = f"EXISTS (SELECT 1 FROM json_each({payload},'$.items') j WHERE {where})"
    if target.scope == "movie":
        leftover = "CASE WHEN l.type='object' THEN l.value ELSE '{}' END"
        clause += f" OR EXISTS (SELECT 1 FROM json_each({payload},'$.leftovers') l JOIN upgrade_leftovers u ON u.id=json_extract({leftover},'$.id') WHERE u.movie_id=?)"
        params += (target.id,)
    return "job_type='DUPLICATE_DELETE' AND (" + clause + ")", params
