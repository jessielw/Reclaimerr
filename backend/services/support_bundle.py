"""Prepare immutable, owner-bound support archives using sanitized records only."""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import JsonValue

from backend.core.operation_diagnostics import OUTCOMES, STAGES, SUBJECTS
from backend.core.rule_engine import FIELD_LABELS, OPERATOR_LABELS
from backend.core.support_codes import parse_support_code
from backend.enums import BackgroundJobStatus, BackgroundJobType
from backend.models import diagnostics as m
from backend.services.diagnostics import (
    Collector,
    enum_value,
    integer,
    now,
    object_json,
)
from backend.services.diagnostics_export import timestamp
from backend.services.log_reader import list_log_files, resolve_log_file, tail_log
from backend.services.support_history import error_category

GROUPS = {
    "services": ("services", "libraries"),
    "configuration": ("configuration",),
    "storage": ("storage", "children"),
    "playback": ("playback",),
    "decisions": ("decisions",),
    "tasks": ("schedules", "recent_runs", "active_jobs"),
    "timeline": (),
}
RETENTION_NOTE = "Decision traces start with this release: up to 1,000 evaluations / 4 MiB per scan, 5,000 retained traces, 50 scans and 14 days. SQL-filtered targets may have no trace."
ERROR_CATEGORIES = {
    "database_busy",
    "timeout",
    "permission_denied",
    "authentication_failed",
    "not_found",
    "connection_failed",
    "playback_active",
    "recorded_error",
    "destination_collision",
    "verification_failed",
}


def correlation(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


def history_filter(target: m.Target | None, alias: str = "") -> tuple[str, tuple]:
    if target is None:
        return "1", ()
    prefix = alias + "." if alias else ""
    if target.scope in ("movie", "series", "season"):
        return prefix + target.scope + "_id=?", (target.id,)
    return f"{prefix}target_scope=? AND {prefix}target_id=?", (target.scope, target.id)


class BundleCollector(Collector):
    def __init__(self, path: Path, options: m.BundleOptions):
        super().__init__(path, options, options.target)
        self.options = options
        self._defer_finish = True
        self.code = (
            parse_support_code(options.support_code) if options.support_code else None
        )
        self.start = self.report.generated_at - timedelta(days=options.history_days)
        self.limitations = [
            RETENTION_NOTE,
            "No credentials, sessions, raw provider responses, raw error messages or unselected sections are exported.",
            "This support database is deliberately not a restorable Reclaimerr backup.",
            "The history window limits event samples and timelines. Current configuration, candidate state and cumulative playback aggregates are not limited to that window. Schedules and recent task runs describe app-wide activity.",
        ]

    def finish(self) -> m.DiagnosticsReport:
        # Apply the report byte limit after section selection. Excluded sections
        # must not cause requested evidence to be discarded from a small bundle.
        return self.report if self._defer_finish else super().finish()

    def traces(self) -> list[m.DecisionTrace]:
        if self.options.target is None and self.code is None:
            return []
        where, args = history_filter(self.options.target, "t")
        if self.code:
            kind, identifier = self.code
            where += " AND " + (
                "s.job_id=?"
                if kind == "job"
                else "0"
                if kind == "operation"
                else "t.scan_id=?"
            )
            if kind != "operation":
                args += (int(identifier) if kind == "job" else identifier,)
        rows = self.rows(
            "decision_traces",
            "support_decision_traces t JOIN support_scan_runs s ON s.id=t.scan_id",
            "t.id,t.scan_id,t.target_scope,t.target_id,t.candidate_id,t.rule_id,t.rule_revision,t.observed_at,t.outcome,s.job_id,s.dropped_traces,substr(s.context,1,65536) AS context,substr(t.payload,1,65536) AS payload",
            where + " AND t.observed_at>=?",
            (*args, self.start.replace(tzinfo=None).isoformat(" ")),
            order="t.id DESC",
        )
        result = []
        for r in rows:
            scan_id = correlation(r["scan_id"])
            observed_at = timestamp(r["observed_at"])
            if (
                not observed_at
                or observed_at > self.report.generated_at
                or not scan_id
                or r["target_scope"]
                not in (
                    "movie",
                    "movie_version",
                    "series",
                    "season",
                    "episode",
                )
            ):
                continue
            payload = object_json(r["payload"]) or {}
            if not isinstance(payload, dict):
                self.limitations.append(
                    "A malformed decision trace payload was omitted."
                )
                continue
            raw_conditions = payload.get("conditions", [])
            if not isinstance(raw_conditions, list):
                self.limitations.append("Malformed trace conditions were omitted.")
                raw_conditions = []
            conditions = []
            for c in raw_conditions[:100]:
                if (
                    not isinstance(c, dict)
                    or not isinstance(c.get("field"), str)
                    or c["field"] not in FIELD_LABELS
                ):
                    continue
                field = c["field"]
                conditions.append(
                    m.TraceCondition(
                        field=field,
                        field_label=FIELD_LABELS[field],
                        operator_label=OPERATOR_LABELS.get(
                            c["operator"], "Unknown operator"
                        )
                        if isinstance(c.get("operator"), str)
                        else "Unknown operator",
                        rule_id=r["rule_id"],
                        operator=c.get("operator")
                        if isinstance(c.get("operator"), str)
                        and c["operator"] in OPERATOR_LABELS
                        else "unknown",
                        expected=self.export.literal(c.get("expected"), field),
                        actual=self.export.literal(c.get("actual"), field),
                        availability=c["availability"]
                        if c.get("availability")
                        in ("available", "unknown", "not_evaluated")
                        else "unknown",
                        outcome=c["outcome"]
                        if c.get("outcome")
                        in ("matched", "not_matched", "unknown", "not_evaluated")
                        else "unknown",
                    )
                )
            raw_checks = payload.get("protection_checks") or {}
            raw_checks = raw_checks if isinstance(raw_checks, dict) else {}
            raw_protections = raw_checks.get("protections", [])
            raw_protections = (
                raw_protections if isinstance(raw_protections, list) else []
            )
            protections = []
            for p in raw_protections[:100]:
                if isinstance(p, dict):
                    expires = timestamp(p.get("expires_at"))
                    protections.append(
                        {
                            "id": integer(p.get("id")),
                            "permanent": p.get("permanent") is True,
                            "expires_at": expires.isoformat() if expires else None,
                            "source_rule_id": integer(p.get("source_rule_id")),
                        }
                    )
            context = object_json(r["context"]) or {}
            context = context if isinstance(context, dict) else {}
            safe_context: dict[str, JsonValue] = {
                k: v
                for k, v in context.items()
                if k
                in (
                    "favorites_refresh_available",
                    "seerr_refresh_available",
                    "playback_blocking",
                    "playback_unavailable_targets",
                    "sonarr_unavailable_series",
                )
                and isinstance(v, (int, bool))
            }
            skipped = context.get("skipped_rule_ids", [])
            safe_context["skipped_rule_ids"] = (
                [v for v in skipped[:1000] if isinstance(v, int)]
                if isinstance(skipped, list)
                else []
            )
            safe_context["status"] = (
                context.get("status")
                if context.get("status") in ("completed", "failed")
                else "unknown"
            )
            safe_context["dropped_traces"] = r["dropped_traces"]
            result.append(
                m.DecisionTrace(
                    id=r["id"],
                    scan_id=scan_id,
                    job_id=r["job_id"],
                    target=m.Target(scope=r["target_scope"], id=r["target_id"]),
                    candidate_id=r["candidate_id"],
                    rule_id=r["rule_id"],
                    rule_revision=r["rule_revision"]
                    if isinstance(r["rule_revision"], str)
                    and re.fullmatch(r"[a-f0-9]{64}", r["rule_revision"])
                    else "omitted",
                    observed_at=observed_at,
                    outcome=r["outcome"]
                    if r["outcome"] in ("matched", "not_matched", "unknown")
                    else "unknown",
                    definition=self.export.rule(payload.get("definition")),
                    conditions=conditions,
                    protection_checks={
                        "basis": "stored protection rows after scan reconciliation",
                        "protections": protections,
                        "truncated": raw_checks.get("truncated") is True,
                    },
                    scan_context=safe_context,
                )
            )
        return result

    def runs(self) -> list[m.RunInfo]:
        return [
            run
            for run in super().runs()
            if run.started_at and run.started_at >= self.start
        ]

    def timeline(self, traces: list[m.DecisionTrace]) -> list[m.TimelineEntry]:
        entries = []

        def add(when, event, key, **kwargs):
            if when and self.start <= when <= self.report.generated_at:
                entries.append(
                    m.TimelineEntry(
                        occurred_at=when, event=event, correlation_id=key, **kwargs
                    )
                )

        for trace in traces:
            add(
                trace.observed_at,
                "rule_" + trace.outcome,
                trace.scan_id,
                target=trace.target,
                candidate_id=trace.candidate_id,
                job_id=trace.job_id,
            )
        decisions = self.report.sections.decisions if not self.code else None
        candidate_ids = set()
        if decisions:
            for candidate in decisions.candidates:
                candidate_ids.add(candidate.id)
                key = f"candidate-{candidate.id}"
                add(
                    candidate.recorded_at,
                    "candidate_recorded",
                    key,
                    target=candidate.target,
                    candidate_id=candidate.id,
                )
                add(
                    candidate.last_delete_attempt_at,
                    "last_delete_attempt",
                    key,
                    target=candidate.target,
                    candidate_id=candidate.id,
                    error_category="recorded_error" if candidate.delete_error else None,
                )
            for kind, reviews in (
                ("deletion", decisions.deletion_requests),
                ("protection", decisions.protection_requests),
            ):
                for review in reviews:
                    add(
                        review.reviewed_at,
                        kind + "_reviewed",
                        f"{kind}-request-{review.id}",
                        target=review.target,
                        note="Stored review timestamp; actor and free text excluded.",
                    )
                    add(
                        review.executed_at,
                        kind + "_executed",
                        f"{kind}-request-{review.id}",
                        target=review.target,
                    )
        where, args = history_filter(self.options.target)
        if self.code:
            kind, identifier = self.code
            where += " AND " + (
                "job_id=?"
                if kind == "job"
                else "0"
                if kind == "scan"
                else "correlation_id=?"
            )
            if kind != "scan":
                args += (int(identifier) if kind == "job" else identifier,)
        try:
            events = self.rows(
                "operation_events",
                "support_operation_events",
                "id,correlation_id,event_type,occurred_at,target_scope,target_id,candidate_id,job_id,error_category,substr(detail,1,65536) AS detail",
                where + " AND occurred_at>=?",
                (*args, self.start.replace(tzinfo=None).isoformat(" ")),
                order="CASE WHEN error_category IS NOT NULL THEN 0 ELSE 1 END,id DESC",
            )
            allowed = {
                op + suffix
                for op in ("delete", "move")
                for suffix in (
                    "_started",
                    "_failed",
                    "_batch_partial",
                    "_batch_completed",
                )
            }
            allowed.update({"operation_step", "item_outcome"})
            for r in events:
                key = correlation(r["correlation_id"])
                if not key or r["event_type"] not in allowed:
                    continue
                candidate_ids.add(r["candidate_id"])
                detail = object_json(r["detail"])
                operation = None
                if isinstance(detail, dict) and r["event_type"] in (
                    "operation_step",
                    "item_outcome",
                ):
                    operation = m.OperationDetail(
                        action=detail["action"]
                        if detail.get("action") in ("move", "delete")
                        else "unknown",
                        stage=detail["stage"]
                        if isinstance(detail.get("stage"), str)
                        and detail["stage"] in STAGES
                        else "unknown",
                        outcome=detail["outcome"]
                        if isinstance(detail.get("outcome"), str)
                        and detail["outcome"] in OUTCOMES
                        else "unknown",
                        subject=detail["subject"]
                        if isinstance(detail.get("subject"), str)
                        and detail["subject"] in SUBJECTS
                        else "candidate",
                        path=self.export.path(detail.get("path")),
                        destination=self.export.path(detail.get("destination")),
                        provider=detail["provider"]
                        if detail.get("provider")
                        in ("plex", "emby_or_jellyfin", "radarr", "sonarr")
                        else None,
                        reference=self.export.alias(
                            "provider-item",
                            (detail.get("provider"), detail.get("reference")),
                        )
                        if isinstance(detail.get("reference"), str)
                        else None,
                        shared_request=detail.get("shared_request") is True,
                        dropped_observations=max(
                            0, integer(detail.get("dropped_observations")) or 0
                        ),
                    )
                add(
                    timestamp(r["occurred_at"]),
                    r["event_type"],
                    key,
                    target=m.Target(scope=r["target_scope"], id=r["target_id"]),
                    candidate_id=r["candidate_id"],
                    job_id=r["job_id"],
                    error_category=r["error_category"]
                    if r["error_category"] in ERROR_CATEGORIES
                    else None,
                    operation=operation,
                    note="Observed step or candidate outcome. Provider requests can cover multiple files; successful requests are not filesystem verification."
                    if operation
                    else "Batch outcome; does not assert individual item success.",
                )
        except (sqlite3.Error, TimeoutError):
            self.limitations.append(
                "Operation history unavailable; older operations may not have been recorded."
            )
        job_where = "(created_at>=? OR started_at>=? OR completed_at>=?)"
        params: tuple = (self.start.replace(tzinfo=None).isoformat(" "),) * 3
        job_ids = {e.job_id for e in entries if e.job_id is not None}
        if self.code:
            kind, identifier = self.code
            if kind == "job":
                job_where += " AND id=?"
                params += (int(identifier),)
            else:
                clauses = []
                if kind in ("scan", "correlation"):
                    clauses.append(
                        "id IN (SELECT job_id FROM support_scan_runs WHERE id=?)"
                    )
                    params += (identifier,)
                if kind in ("operation", "correlation"):
                    clauses.append(
                        "id IN (SELECT job_id FROM support_operation_events WHERE correlation_id=?)"
                    )
                    params += (identifier,)
                job_where += " AND (" + " OR ".join(clauses) + ")"
        elif self.options.target:
            clauses = []
            if candidate_ids:
                placeholders = ",".join("?" for _ in candidate_ids)
                clauses.append(
                    f"EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(payload) THEN payload ELSE '{{}}' END, '$.candidate_ids') WHERE value IN ({placeholders}))"
                )
                params += tuple(candidate_ids)
            if job_ids:
                clauses.append("id IN (" + ",".join("?" for _ in job_ids) + ")")
                params += tuple(job_ids)
            job_where += " AND (" + (" OR ".join(clauses) or "0") + ")"
        for r in self.rows(
            "timeline_jobs",
            "background_jobs",
            "id,job_type,status,created_at,started_at,completed_at,substr(error_message,1,4096) AS error",
            job_where,
            params,
            limit=20,
            order="id DESC",
        ):
            key = f"job-{r['id']}"
            kind = enum_value(BackgroundJobType, r["job_type"])
            add(timestamp(r["created_at"]), kind + "_queued", key, job_id=r["id"])
            add(timestamp(r["started_at"]), kind + "_started", key, job_id=r["id"])
            add(
                timestamp(r["completed_at"]),
                kind + "_" + enum_value(BackgroundJobStatus, r["status"]),
                key,
                job_id=r["id"],
                error_category=error_category(r["error"]),
            )
        if len(entries) > 200:
            self.limitations.append("Timeline limited to the newest 200 events.")
        return sorted(entries, key=lambda e: e.occurred_at)[-200:]

    def logs(self, timeline: list[m.TimelineEntry]) -> list[m.LogExcerpt]:
        result = []
        identifiers = {e.correlation_id for e in timeline}
        identifiers.update(f"job={e.job_id}" for e in timeline if e.job_id is not None)
        if self.code:
            kind, identifier = self.code
            identifiers.add("job=" + identifier if kind == "job" else identifier)
        if self.options.target and not identifiers:
            self.limitations.append(
                "No correlated log identifiers are available for this target."
            )
            return []
        for file in list_log_files()[:3]:
            if time.monotonic() >= self.deadline:
                self.limitations.append("Log scan reached the collection deadline.")
                break
            tail = tail_log(
                resolve_log_file(file.name), limit=1000, scan_bytes=256 * 1024
            )
            for entry in tail.entries:
                if not entry.timestamp:
                    continue
                # Existing log timestamps use local wall time, not UTC.
                try:
                    when = datetime.strptime(
                        entry.timestamp, "%Y-%m-%d %H:%M:%S,%f"
                    ).astimezone(UTC)
                except ValueError:
                    continue
                if not self.start <= when <= self.report.generated_at:
                    continue
                key = next(
                    (
                        key
                        for key in sorted(identifiers)
                        if re.search(
                            r"(?<![\w-])" + re.escape(key) + r"(?![\w-])", entry.message
                        )
                    ),
                    None,
                )
                if (self.options.target or self.code) and key is None:
                    continue
                category = (
                    (error_category(entry.message) or "log_event")
                    if entry.level in ("WARNING", "ERROR", "CRITICAL")
                    else "log_event"
                )
                # Free text is never copied: preserve diagnostic categories, timing,
                # level and correlations without guessing whether arbitrary text is safe.
                result.append(
                    m.LogExcerpt(
                        timestamp=when.isoformat(),
                        level=entry.level
                        if entry.level
                        in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
                        else "UNKNOWN",
                        category=category,
                        message=category.replace("_", " ")
                        + "; original message omitted",
                        correlation_id=key,
                    )
                )
            if tail.scan_truncated:
                self.limitations.append(
                    "Log excerpts cover only bounded file tails, not the entire selected window."
                )
        self.limitations.append(
            "Log excerpts are structured summaries: arbitrary message text, tracebacks, addresses and credentials are excluded."
        )
        if len(result) > 100:
            self.limitations.append("Log excerpts limited to the newest 100 summaries.")
        return sorted(result, key=lambda e: e.timestamp)[-100:]

    def prepare(self):
        self.collect()
        self._defer_finish = False
        self.deadline = time.monotonic() + 5
        traces, timeline, logs = [], [], []
        try:
            self.db = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON").close()
            self.db.set_progress_handler(
                lambda: int(time.monotonic() >= self.deadline), 1000
            )
            if self.options.target and "tasks" in self.options.sections:
                self.section("schedules", self.schedules)
                self.section("recent_runs", self.runs)
            if (
                "decisions" in self.options.sections
                or "timeline" in self.options.sections
                or self.options.include_logs
            ):
                try:
                    traces = self.traces()
                except Exception:
                    self.limitations.append(
                        "Decision traces unavailable; the migration or historical evidence may be missing."
                    )
            if "timeline" in self.options.sections or self.options.include_logs:
                try:
                    timeline = self.timeline(traces)
                except Exception:
                    self.limitations.append(
                        "Timeline is incomplete because stored history could not be read within the budget."
                    )
        except (sqlite3.Error, OSError):
            self.limitations.append("Historical database sections unavailable.")
        finally:
            if self.db:
                self.db.close()
        if self.options.include_logs:
            try:
                logs = self.logs(timeline)
            except Exception:
                self.limitations.append("Log excerpts unavailable.")
        if "timeline" not in self.options.sections:
            timeline = []
        if "decisions" not in self.options.sections:
            traces = []
        selected = set(self.options.sections)
        for group, names in GROUPS.items():
            if group not in selected:
                for name in names:
                    setattr(self.report.sections, name, None)
        allowed = {"identity", "database"}
        if "decisions" in selected:
            allowed.update(
                {
                    "decision_traces",
                    "candidates",
                    "rules",
                    "protections",
                    "deletion_requests",
                    "protection_requests",
                }
            )
        if "timeline" in selected:
            allowed.update({"operation_events", "timeline_jobs"})
        allowed.update(name for group in selected for name in GROUPS[group])
        if "configuration" in selected:
            allowed.add("path_mappings")
        if "services" in selected:
            allowed.add("extra_settings")
        if "decisions" in selected:
            allowed.update({"definition", "action", "reason_data", "matched_rule_ids"})
        if "playback" in selected:
            allowed.update({"playback_events", "playback_native", "playback_imported"})
        if "storage" in selected:
            allowed.update({"linked_storage", "arr_storage"})
        for identity in [
            self.report.sections.identity,
            *(self.report.sections.children or []),
        ]:
            if identity:
                if "playback" not in selected:
                    identity.view_count = None
                    identity.last_viewed_at = None
                if "storage" not in selected:
                    identity.path = None
        collection = self.report.collection
        collection.observations = [
            o for o in collection.observations if o.section in allowed
        ]
        collection.truncated = [o for o in collection.truncated if o.section in allowed]
        collection.omitted = [
            o for o in collection.omitted if o.split(":", 1)[0] in allowed
        ]
        if "storage" not in selected:
            collection.children_total = None
            collection.children_offset = 0
        # Added task context also shares the ordinary report's byte budget.
        self.finish()
        # Summaries must describe the selected export, not disclose excluded data.
        self.report.summary = [
            "Scoped support export: review the inventory and selected evidence before sharing.",
            "Contains media identity context; never a full database or a restorable backup.",
        ]
        self.report.collection.completed_at = now()
        collection.partial = bool(
            collection.truncated
            or collection.omitted
            or any(o.state != "available" for o in collection.observations)
        )
        return self.report, traces, timeline, logs


def support_tables(
    report: m.DiagnosticsReport, traces, timeline, logs
) -> dict[str, list[dict]]:
    """Table names and columns come exclusively from typed export models."""
    result = {
        "identity": [report.sections.identity.model_dump(mode="json")]
        if report.sections.identity
        else []
    }
    for name in (
        "services",
        "libraries",
        "configuration",
        "children",
        "storage",
        "schedules",
        "recent_runs",
        "active_jobs",
        "database",
    ):
        section = getattr(report.sections, name)
        if section is not None:
            result[name] = (
                [v.model_dump(mode="json") for v in section]
                if isinstance(section, list)
                else [section.model_dump(mode="json")]
            )
    if report.sections.playback:
        result["playback_aggregates"] = [
            v.model_dump(mode="json") for v in report.sections.playback.aggregates
        ]
        result["playback_events"] = [
            v.model_dump(mode="json") for v in report.sections.playback.events
        ]
    if report.sections.decisions:
        for name in (
            "candidates",
            "rules",
            "protections",
            "deletion_requests",
            "protection_requests",
        ):
            result[name] = [
                v.model_dump(mode="json")
                for v in getattr(report.sections.decisions, name)
            ]
    result.update(
        {
            "decision_traces": [v.model_dump(mode="json") for v in traces],
            "timeline": [v.model_dump(mode="json") for v in timeline],
            "logs": [v.model_dump(mode="json") for v in logs],
        }
    )
    return result


def sqlite_extract(report_id: str, tables: dict[str, list[dict]]) -> bytes:
    # No source database pages, schema SQL, triggers, WAL, or secrets ever enter
    # this connection. The source is the already-sanitized, typed preview only.
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute(
            "CREATE TABLE support_metadata (report_id TEXT, format_version INTEGER, restorable INTEGER)"
        )
        db.execute("INSERT INTO support_metadata VALUES (?,1,0)", (report_id,))
        for name, rows in tables.items():
            if not rows:
                continue
            if not re.fullmatch(r"[a-z_]+", name):
                raise ValueError("Invalid support table")
            columns = list(rows[0])
            if not all(re.fullmatch(r"[a-z_]+", c) for c in columns):
                raise ValueError("Invalid support column")
            declaration = ",".join('"' + c + '"' for c in columns)
            db.execute(f'CREATE TABLE "support_{name}" ({declaration})')
            values = [
                [
                    json.dumps(row[c], ensure_ascii=False)
                    if isinstance(row[c], (dict, list))
                    else row[c]
                    for c in columns
                ]
                for row in rows
            ]
            db.executemany(
                f'INSERT INTO "support_{name}" VALUES ({",".join("?" for _ in columns)})',
                values,
            )
        db.commit()
        return db.serialize()


class BundleCache:
    def __init__(self, max_bytes=16 * 1024 * 1024, max_entries=8, ttl=600):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl
        self.entries: OrderedDict[str, tuple[int, float, bytes, str]] = OrderedDict()
        self.lock = threading.Lock()

    def _expire(self):
        for key in list(self.entries):
            if self.entries[key][1] <= time.monotonic():
                del self.entries[key]

    def put(self, owner: int, data: bytes, filename: str) -> tuple[str, datetime]:
        if len(data) > self.max_bytes:
            raise ValueError("Support bundle exceeds the size limit")
        with self.lock:
            self._expire()
            while self.entries and (
                len(self.entries) >= self.max_entries
                or sum(len(e[2]) for e in self.entries.values()) + len(data)
                > self.max_bytes
            ):
                self.entries.popitem(last=False)
            token = secrets.token_urlsafe(32)
            self.entries[token] = (owner, time.monotonic() + self.ttl, data, filename)
            return token, now() + timedelta(seconds=self.ttl)

    def get(self, owner: int, token: str) -> tuple[bytes, str] | None:
        with self.lock:
            self._expire()
            found = self.entries.get(token)
            return (found[2], found[3]) if found and found[0] == owner else None


bundle_cache = BundleCache()


def prepare_bundle(path: Path, options: m.BundleOptions, owner: int) -> m.BundlePreview:
    if (
        options.preset != "system"
        and options.target is None
        and not options.support_code
    ):
        raise ValueError("Choose a media item for this support preset")
    collector = BundleCollector(path, options)
    report, traces, timeline, logs = collector.prepare()
    # A hard cap applies to preview evidence, including optional history.
    while (
        len(
            json.dumps(
                {
                    "traces": [t.model_dump(mode="json") for t in traces],
                    "timeline": [e.model_dump(mode="json") for e in timeline],
                    "logs": [e.model_dump(mode="json") for e in logs],
                }
            ).encode()
        )
        > 1024 * 1024
    ):
        if traces:
            traces.pop()
        elif logs:
            logs.pop(0)
        else:
            timeline.pop(0)
        if (
            "Extra history truncated to the 1 MiB preview budget."
            not in collector.limitations
        ):
            collector.limitations.append(
                "Extra history truncated to the 1 MiB preview budget."
            )
    tables = support_tables(report, traces, timeline, logs)
    files = {
        "report.json": report.model_dump_json(indent=2).encode(),
        "evidence.json": json.dumps(
            {
                "traces": [t.model_dump(mode="json") for t in traces],
                "timeline": [e.model_dump(mode="json") for e in timeline],
                "logs": [e.model_dump(mode="json") for e in logs],
            },
            indent=2,
            ensure_ascii=False,
        ).encode(),
        "README.txt": (
            b"Reclaimerr support bundle\n\nReview manifest.json for coverage and omissions.\nThe SQLite extract contains ONLY sanitized support records. It cannot restore or run Reclaimerr.\nMedia titles, IDs and playback information may be included. No automatic upload occurs.\n"
        ),
    }
    if options.include_sqlite:
        files["support-extract.sqlite"] = sqlite_extract(report.report_id, tables)
    excluded = [name for name in GROUPS if name not in options.sections]
    if not options.include_logs:
        excluded.append("logs")
    if not options.include_file_paths:
        excluded.append("actual_file_paths")
    manifest = m.BundleManifest(
        support_code=options.support_code,
        report_id=report.report_id,
        preset=options.preset,
        generated_at=report.generated_at,
        window_start=collector.start,
        window_end=report.generated_at,
        inventory=[
            m.BundleInventory(section=name, records=len(rows))
            for name, rows in tables.items()
        ],
        files=[*files, "manifest.json"],
        excluded=excluded,
        limitations=list(dict.fromkeys(collector.limitations)),
        file_paths_included=options.include_file_paths,
    )
    files["manifest.json"] = manifest.model_dump_json(indent=2).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    data = out.getvalue()
    token, expires = bundle_cache.put(
        owner, data, f"reclaimerr-support-{report.report_id}.zip"
    )
    return m.BundlePreview(
        download_token=token,
        expires_at=expires,
        archive_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        manifest=manifest,
        report=report,
        traces=traces,
        timeline=timeline,
        logs=logs,
    )
