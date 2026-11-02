"""Persist diagnostic history separately from cleanup's operational transaction."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select

from backend.core.diagnostic_trace import ScanRecorder, job_id
from backend.core.logger import LOG
from backend.core.operation_diagnostics import OperationRecorder
from backend.database import async_db
from backend.database.models import (
    ProtectedMedia,
    ReclaimCandidate,
    SupportDecisionTrace,
    SupportOperationEvent,
    SupportScanRun,
)


def error_category(value: object) -> str | None:
    if not value:
        return None
    text = str(value).lower()[:4096]
    for category, needles in (
        (
            "destination_collision",
            (
                "different content",
                "destination is a symlink",
                "destination already exists",
                "file exists",
            ),
        ),
        ("verification_failed", ("size mismatch", "verification failed")),
        (
            "database_busy",
            ("database is locked", "database is busy", "sqlite_busy", "sqlite_locked"),
        ),
        ("timeout", ("timed out", "timeout")),
        (
            "permission_denied",
            ("permission denied", "access denied", "forbidden", "403"),
        ),
        ("authentication_failed", ("unauthorized", "invalid api key", "401")),
        ("not_found", ("not found", "no such file", "404", "no longer exists")),
        ("connection_failed", ("connection", "resolve", "unreachable", "offline")),
        ("playback_active", ("active playback", "currently playing")),
    ):
        if any(word in text for word in needles):
            return category
    return "recorded_error"


def target_fields(row: Any) -> dict[str, Any] | None:
    for scope in ("episode", "movie_version", "season", "movie", "series"):
        identifier = getattr(row, scope + "_id", None)
        if identifier:
            return {
                "target_scope": scope,
                "target_id": identifier,
                "movie_id": row.movie_id,
                "series_id": row.series_id,
                "season_id": row.season_id,
                "candidate_id": row.id,
            }
    return None


async def trim_history(db) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=14)
    # Explicit child deletion also works on test/migration connections with FKs off.
    old_runs = select(SupportScanRun.id).where(SupportScanRun.started_at < cutoff)
    excess_runs = (
        select(SupportScanRun.id).order_by(SupportScanRun.started_at.desc()).offset(50)
    )
    for run_ids in (old_runs, excess_runs):
        await db.execute(
            delete(SupportDecisionTrace).where(
                SupportDecisionTrace.scan_id.in_(run_ids)
            )
        )
        await db.execute(delete(SupportScanRun).where(SupportScanRun.id.in_(run_ids)))
    await db.execute(
        delete(SupportDecisionTrace).where(
            SupportDecisionTrace.id.in_(
                select(SupportDecisionTrace.id)
                .order_by(SupportDecisionTrace.id.desc())
                .offset(5000)
            )
        )
    )
    await db.execute(
        delete(SupportOperationEvent).where(
            or_(
                SupportOperationEvent.occurred_at < cutoff,
                SupportOperationEvent.id.in_(
                    select(SupportOperationEvent.id)
                    .order_by(SupportOperationEvent.id.desc())
                    .offset(5000)
                ),
            )
        )
    )


async def persist_scan(db, recorder: ScanRecorder) -> None:
    """Called after operational scan commit; failure never undoes cleanup state."""
    try:
        scopes = {
            name: set()
            for name in ("movie", "movie_version", "series", "season", "episode")
        }
        for scope, identifier, _ in recorder.rows:
            scopes[scope].add(identifier)
        candidates = (
            list(
                (
                    await db.scalars(
                        select(ReclaimCandidate)
                        .where(
                            or_(
                                *[
                                    getattr(ReclaimCandidate, scope + "_id").in_(ids)
                                    for scope, ids in scopes.items()
                                    if ids
                                ]
                            )
                        )
                        .limit(5001)
                    )
                ).all()
            )
            if recorder.rows
            else []
        )
        candidate_map = {}
        for candidate in candidates:
            fields = target_fields(candidate)
            if fields:
                candidate_map[(fields["target_scope"], fields["target_id"])] = (
                    candidate.id
                )
        movie_ids = {
            row["movie_id"] for row in recorder.rows.values() if row["movie_id"]
        }
        series_ids = {
            row["series_id"] for row in recorder.rows.values() if row["series_id"]
        }
        protections = list(
            (
                await db.scalars(
                    select(ProtectedMedia)
                    .where(
                        or_(
                            ProtectedMedia.movie_id.in_(movie_ids),
                            ProtectedMedia.series_id.in_(series_ids),
                        )
                    )
                    .limit(5001)
                )
            ).all()
        )
        protection_map: dict[tuple[str, int], list[dict]] = {}
        for protection in protections:
            fields = target_fields(protection)
            if fields:
                key = (fields["target_scope"], fields["target_id"])
                protection_map.setdefault(key, []).append(
                    {
                        "id": protection.id,
                        "permanent": protection.permanent,
                        "expires_at": protection.expires_at.isoformat()
                        if protection.expires_at
                        else None,
                        "source_rule_id": protection.source_rule_id,
                    }
                )
        db.add(
            SupportScanRun(
                id=recorder.scan_id,
                started_at=recorder.started_at,
                completed_at=datetime.now(UTC),
                job_id=job_id(),
                dropped_traces=recorder.dropped,
                context=recorder.context,
            )
        )
        await db.flush()
        for values in recorder.rows.values():
            data = {k: v for k, v in values.items() if k != "_bytes"}
            keys = {(data["target_scope"], data["target_id"])}
            keys.update(
                (scope, data[scope + "_id"])
                for scope in ("movie", "series", "season")
                if data.get(scope + "_id")
            )
            applicable = [
                p for key in sorted(keys) for p in protection_map.get(key, [])
            ]
            data["payload"]["protection_checks"] = {
                "basis": "stored protection rows after scan reconciliation; pre-evaluation SQL filters are not individually traced",
                "protections": applicable[:100],
                "truncated": len(protections) > 5000 or len(applicable) > 100,
            }
            data["candidate_id"] = candidate_map.get(
                (data["target_scope"], data["target_id"])
            )
            db.add(SupportDecisionTrace(scan_id=recorder.scan_id, **data))
        await db.flush()
        await trim_history(db)
        await db.commit()
    except Exception:
        await db.rollback()
        LOG.warning(
            "Support decision history could not be recorded; scan result is unchanged"
        )


async def record_operations(
    candidates: list[dict], correlation_id: str, event_type: str, error: object = None
) -> None:
    if not candidates:
        return
    try:
        async with async_db() as db:
            for fields in candidates[:1000]:
                db.add(
                    SupportOperationEvent(
                        correlation_id=correlation_id,
                        event_type=event_type,
                        occurred_at=datetime.now(UTC),
                        job_id=job_id(),
                        error_category=error_category(error),
                        **fields,
                    )
                )
            await db.flush()
            await trim_history(db)
            await db.commit()
    except Exception:
        LOG.warning("Support operation history could not be recorded")


async def persist_operation_details(
    snapshots: list[dict], correlation_id: str, recorder: OperationRecorder
) -> None:
    """Flush observations only after the operation's own transactions finish."""
    targets = {row["candidate_id"]: row for row in snapshots}
    try:
        dropped = recorder.dropped + max(
            0,
            sum(
                candidate_id in targets
                for step in recorder.steps
                for candidate_id in step["candidate_ids"]
            )
            - 4000,
        )
        async with async_db() as db:
            count = 0
            # Reserve capacity for final per-candidate outcomes even in large batches.
            for event, values in (
                ("operation_step", recorder.steps),
                ("item_outcome", list(recorder.outcomes.values())),
            ):
                for value in values:
                    for candidate_id in value["candidate_ids"]:
                        if candidate_id not in targets or (
                            event == "operation_step" and count >= 4000
                        ):
                            continue
                        detail = {
                            key: value.get(key)
                            for key in (
                                "action",
                                "stage",
                                "outcome",
                                "subject",
                                "path",
                                "destination",
                                "provider",
                                "reference",
                            )
                        }
                        detail["shared_request"] = len(value["candidate_ids"]) > 1
                        detail["dropped_observations"] = dropped
                        db.add(
                            SupportOperationEvent(
                                correlation_id=correlation_id,
                                event_type=event,
                                occurred_at=value["occurred_at"],
                                job_id=job_id(),
                                error_category=error_category(value.get("error")),
                                detail=detail,
                                **targets[candidate_id],
                            )
                        )
                        count += 1
            await db.flush()
            await trim_history(db)
            await db.commit()
    except Exception:
        LOG.warning(
            "Support operation details could not be recorded; operation result is unchanged"
        )
