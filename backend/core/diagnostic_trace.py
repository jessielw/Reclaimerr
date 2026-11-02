"""Bounded observation of real evaluations; never evaluates a rule itself."""

from __future__ import annotations

import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from itertools import islice
from typing import Any
from uuid import uuid4

JOB_ENV = "RECLAIMERR_SUPPORT_JOB_ID"
current_job_id: ContextVar[int | None] = ContextVar("support_job_id", default=None)
current_operation_id: ContextVar[str | None] = ContextVar(
    "support_operation_id", default=None
)


def job_id() -> int | None:
    value = current_job_id.get()
    if value is not None:
        return value
    raw = os.getenv(JOB_ENV, "")
    return int(raw) if raw.isdecimal() and len(raw) < 16 else None


def bounded_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return {"omitted": "depth_limit"}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, (list, tuple, set)):
        return [bounded_value(v, depth + 1) for v in islice(value, 100)]
    if isinstance(value, dict):
        return {
            str(k)[:128]: bounded_value(v, depth + 1)
            for k, v in islice(value.items(), 100)
        }
    return None


@dataclass
class ScanRecorder:
    scan_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    rows: dict[tuple[str, int, int], dict[str, Any]] = field(default_factory=dict)
    dropped: int = 0
    max_records: int = 1000
    max_bytes: int = 4 * 1024 * 1024
    used_bytes: int = 0
    context: dict[str, Any] = field(default_factory=dict)

    def capture(
        self,
        rule: Any,
        scope: str,
        models: dict[str, Any],
        context: dict,
        outcome: bool | None,
        unavailable: Any,
    ) -> None:
        target = models.get(scope)
        if target is None or getattr(target, "id", None) is None or rule.id is None:
            return
        key = (scope, target.id, rule.id)
        if key not in self.rows and len(self.rows) >= self.max_records:
            self.dropped += 1
            return
        conditions = []

        def walk(node: Any, disabled: bool = False, depth: int = 0):
            if not isinstance(node, dict) or depth > 10 or len(conditions) >= 100:
                return
            disabled = disabled or node.get("enabled") is False
            if node.get("type") == "group":
                for child in node.get("children", [])[:100]:
                    walk(child, disabled, depth + 1)
            elif node.get("type") == "condition":
                name = node.get("field")
                observed = context.get("_support_conditions", {}).get(id(node))
                actual = (
                    observed[1]
                    if observed
                    else (
                        context.get(name, unavailable)
                        if isinstance(name, str)
                        else unavailable
                    )
                )
                conditions.append(
                    {
                        "field": name,
                        "operator": node.get("operator"),
                        "expected": bounded_value(node.get("value")),
                        "actual": None
                        if actual is unavailable or observed is None
                        else bounded_value(actual),
                        "availability": "not_evaluated"
                        if disabled or observed is None
                        else "unknown"
                        if actual is unavailable
                        else "available",
                        "outcome": "not_evaluated"
                        if observed is None
                        else "unknown"
                        if observed[0] is None
                        else "matched"
                        if observed[0]
                        else "not_matched",
                    }
                )

        walk(context.get("_support_definition", rule.definition or {}).get("root"))
        revision = hashlib.sha256(
            json.dumps(
                {
                    "definition": rule.definition,
                    "action": rule.action,
                    "scope": rule.target_scope,
                    "enabled": rule.enabled,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        payload = {
            "definition": bounded_value(rule.definition),
            "conditions": conditions,
            "protection_checks": {
                "coverage": "evaluation_only; SQL-filtered targets are not individually traced"
            },
        }
        size = len(json.dumps(payload, default=str).encode())
        if size > 32768 or self.used_bytes + size > self.max_bytes:
            self.dropped += 1
            return
        previous = self.rows.get(key)
        if previous:
            self.used_bytes -= previous.pop("_bytes", 0)
        self.used_bytes += size
        self.rows[key] = {
            "target_scope": scope,
            "target_id": target.id,
            "movie_id": getattr(models.get("movie"), "id", None),
            "series_id": getattr(models.get("series"), "id", None),
            "season_id": getattr(models.get("season"), "id", None),
            "rule_id": rule.id,
            "rule_revision": revision,
            "observed_at": datetime.now(UTC),
            "outcome": "unknown"
            if outcome is None
            else "matched"
            if outcome
            else "not_matched",
            "payload": payload,
            "_bytes": size,
        }


current_scan: ContextVar[ScanRecorder | None] = ContextVar("support_scan", default=None)


def capture_evaluation(
    rule: Any,
    scope: str,
    models: dict,
    context: dict,
    outcome: bool | None,
    unavailable: Any,
) -> None:
    recorder = current_scan.get()
    if recorder is None:
        return
    try:
        recorder.capture(rule, scope, models, context, outcome, unavailable)
    except Exception:
        # Observability must never change the cleanup decision.
        recorder.dropped += 1
