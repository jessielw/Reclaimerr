"""Read-only lookup of bounded, exact scan, job and operation identifiers."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from backend.core.support_codes import parse_support_code
from backend.models import diagnostics as m
from backend.services.diagnostics import TABLES, Collector, object_json
from backend.services.diagnostics_export import timestamp


def lookup_support(path: Path, code: str) -> m.SupportLookupResult:
    kind, identifier = parse_support_code(code)
    result = m.SupportLookupResult(state="not_found")
    collector = Collector(path, m.DiagnosticsOptions(), None)

    def targets(sql, params):
        try:
            rows = collector.query(sql + " LIMIT 101", params)
            return [
                m.Target(scope=r["target_scope"], id=r["target_id"])
                for r in rows[:100]
                if r["target_scope"] in TABLES
                and isinstance(r["target_id"], int)
                and r["target_id"] > 0
            ], len(rows) > 100
        except (sqlite3.Error, TimeoutError):
            result.limitations.append(
                "Some target associations are unavailable; this does not establish that the operation had no targets."
            )
            return [], False

    def add(match_kind, when, selected, truncated):
        canonical = match_kind + "-" + identifier
        preset = (
            "candidate"
            if match_kind == "scan"
            else "deletion"
            if match_kind == "operation"
            else "system"
        )
        suggested = m.BundleOptions(
            support_code=canonical,
            target=selected[0] if len(selected) == 1 and not truncated else None,
            preset=preset,
            history_days=30,
            sections=["services", "configuration", "decisions", "timeline", "tasks"],
        )
        result.matches.append(
            m.SupportMatch(
                code=canonical,
                kind=match_kind,
                occurred_at=timestamp(when),
                targets=selected,
                targets_truncated=truncated,
                suggested=suggested,
            )
        )

    try:
        with closing(
            sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
        ) as db:
            collector.db = db
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON").close()
            db.set_progress_handler(
                lambda: int(time.monotonic() >= collector.deadline), 1000
            )
            if kind in ("scan", "correlation"):
                try:
                    rows = collector.query(
                        "SELECT started_at FROM support_scan_runs WHERE id=? LIMIT 1",
                        (identifier,),
                    )
                    if rows:
                        selected, truncated = targets(
                            "SELECT DISTINCT target_scope,target_id FROM support_decision_traces WHERE scan_id=? ORDER BY target_scope,target_id",
                            (identifier,),
                        )
                        add("scan", rows[0]["started_at"], selected, truncated)
                except (sqlite3.Error, TimeoutError):
                    result.limitations.append("Scan history could not be searched.")
            if kind in ("operation", "correlation"):
                try:
                    rows = collector.query(
                        "SELECT occurred_at FROM support_operation_events WHERE correlation_id=? ORDER BY id DESC LIMIT 1",
                        (identifier,),
                    )
                    if rows:
                        selected, truncated = targets(
                            "SELECT DISTINCT target_scope,target_id FROM support_operation_events WHERE correlation_id=? ORDER BY target_scope,target_id",
                            (identifier,),
                        )
                        add("operation", rows[0]["occurred_at"], selected, truncated)
                except (sqlite3.Error, TimeoutError):
                    result.limitations.append(
                        "Operation history could not be searched."
                    )
            if kind == "job":
                rows = collector.query(
                    "SELECT created_at,substr(payload,1,65536) AS payload FROM background_jobs WHERE id=? LIMIT 1",
                    (int(identifier),),
                )
                if rows:
                    selected, truncated = targets(
                        "SELECT t.target_scope,t.target_id FROM support_decision_traces t JOIN support_scan_runs s ON s.id=t.scan_id WHERE s.job_id=? UNION SELECT target_scope,target_id FROM support_operation_events WHERE job_id=? ORDER BY target_scope,target_id",
                        (int(identifier), int(identifier)),
                    )
                    if not selected:
                        payload = object_json(rows[0]["payload"])
                        ids = (
                            payload.get("candidate_ids")
                            if isinstance(payload, dict)
                            else None
                        )
                        if isinstance(ids, list):
                            truncated |= len(ids) > 100
                            ids = [
                                i
                                for i in ids[:100]
                                if isinstance(i, int)
                                and not isinstance(i, bool)
                                and 0 < i < 2**63
                            ]
                            if ids:
                                candidates = collector.query(
                                    "SELECT movie_id,movie_version_id,series_id,season_id,episode_id FROM reclaim_candidates WHERE id IN ("
                                    + ",".join("?" for _ in ids)
                                    + ") ORDER BY id LIMIT 100",
                                    tuple(ids),
                                )
                                for row in candidates:
                                    for scope in (
                                        "episode",
                                        "movie_version",
                                        "season",
                                        "movie",
                                        "series",
                                    ):
                                        if row[scope + "_id"]:
                                            target = m.Target(
                                                scope=scope, id=row[scope + "_id"]
                                            )
                                            if target not in selected:
                                                selected.append(target)
                                            break
                    add("job", rows[0]["created_at"], selected, truncated)
    except (sqlite3.Error, OSError, TimeoutError):
        result.limitations.append(
            "The stored diagnostic history is currently unavailable."
        )
    result.state = (
        "found"
        if result.matches
        else "unavailable"
        if result.limitations
        else "not_found"
    )
    result.limitations.append(
        "Lookup uses exact recorded IDs. Expired history and work from before recording was introduced may have no match. Bundle evidence is limited to the last 30 days; logs and file paths remain opt-in."
    )
    return result
