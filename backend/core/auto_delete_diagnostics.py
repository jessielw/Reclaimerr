"""Bounded observations of automatic deletion decisions, without rerunning them."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.core.diagnostic_trace import ScanRecorder

REASONS = {
    "eligible",
    "authorized",
    "review_period",
    "postponed",
    "canceled",
    "auto_delete_disabled",
    "active_protection",
    "pending_protection_request",
    "pending_delete_request",
    "manual_schedule",
    "scheduled_deletion",
    "resync_pending",
    "rule_data_unavailable",
    "playback_data_unavailable",
    "revalidation_failed",
    "revalidation_not_matched",
    "revalidation_data_unavailable",
}
STAGES = {"selection", "revalidation", "dispatch"}
OUTCOMES = {"eligible", "waiting", "skipped", "authorized"}
SUMMARY_FIELDS = {
    "eligible",
    "waiting",
    "skipped",
    "revalidated_out",
    "playback_unavailable",
    "deleted",
    "failed",
    "deferred",
}
COVERAGE_FIELDS = {
    "playback_unavailable_count",
    "sonarr_unavailable_count",
    "season_inventory_unavailable_count",
    "requester_watch_unavailable_count",
    "watch_completion_unavailable_count",
    "skipped_favorites_count",
    "skipped_protected_count",
}


@dataclass
class AutoDeleteRecorder:
    scan: ScanRecorder = field(
        default_factory=lambda: ScanRecorder(
            context={"purpose": "auto_delete", "status": "interrupted"}
        )
    )
    decisions: dict[int, dict[str, Any]] = field(default_factory=dict)
    max_records: int = 1000
    dropped: int = 0

    def capture(
        self, candidate: Any, stage: str, outcome: str, reason: str, **detail
    ) -> None:
        if (
            candidate.id not in self.decisions
            and len(self.decisions) >= self.max_records
        ):
            self.dropped += 1
            return
        scope = next(
            (
                name
                for name in ("episode", "movie_version", "season", "movie", "series")
                if getattr(candidate, name + "_id", None)
            ),
            None,
        )
        if scope is None:
            return
        previous = self.decisions.get(candidate.id, {})
        self.decisions[candidate.id] = {
            "target_scope": scope,
            "target_id": getattr(candidate, scope + "_id"),
            "candidate_id": candidate.id,
            "movie_id": candidate.movie_id,
            "series_id": candidate.series_id,
            "season_id": candidate.season_id,
            "occurred_at": datetime.now(UTC),
            "detail": {
                **previous.get("detail", {}),
                "stage": stage,
                "outcome": outcome,
                "reason": reason,
                "rule_ids": list(candidate.matched_rule_ids or [])[:100],
                **detail,
            },
        }


current_auto_delete: ContextVar[AutoDeleteRecorder | None] = ContextVar(
    "support_auto_delete", default=None
)


def capture_auto_delete(
    candidate: Any, stage: str, outcome: str, reason: str, **detail
) -> None:
    recorder = current_auto_delete.get()
    if recorder is not None:
        try:
            recorder.capture(candidate, stage, outcome, reason, **detail)
        except Exception:
            # Diagnostic failure must never change deletion authorization.
            recorder.dropped += 1
