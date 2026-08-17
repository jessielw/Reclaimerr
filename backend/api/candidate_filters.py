from __future__ import annotations

from typing import Any

from sqlalchemy import exists, func, select

from backend.database.models import ReclaimCandidate


def candidate_matches_rule_clause(rule_id: int) -> Any:
    """Return an exact SQLite JSON-array containment predicate for a rule ID."""
    matched_rules = (
        func.json_each(ReclaimCandidate.matched_rule_ids)
        .table_valued("value")
        .alias("matched_rules")
    )
    return exists(
        select(1).select_from(matched_rules).where(matched_rules.c.value == rule_id)
    )
