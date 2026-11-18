"""Capture and export bounded playback provenance without provider calls."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from backend.core.auto_delete_diagnostics import AutoDeleteRecorder
from backend.core.rule_engine import FIELD_LABELS
from backend.enums import Service
from backend.models import diagnostics as m
from backend.services.diagnostics_export import timestamp
from backend.services.support_history import error_category

if TYPE_CHECKING:
    from backend.services.playback_history import PlaybackRuleSnapshot

MAX_SOURCES = 20
MAX_SAMPLES = 20
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
SERVICES = {item.value for item in Service}
SCOPES = {"movie", "movie_version", "series", "season", "episode"}


def _category(value):
    return value if isinstance(value, str) and value in ERROR_CATEGORIES else None


def _count(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _identifier(value):
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63
        else None
    )


def _service(value):
    return value if isinstance(value, str) and value in SERVICES else "unknown"


def playback_observation(raw: object) -> m.PlaybackCoverageObservation | None:
    if (
        not isinstance(raw, dict)
        or (observed := timestamp(raw.get("observed_at"))) is None
    ):
        return None
    sources = raw.get("sources")
    samples = raw.get("unknown_target_samples")
    safe_sources = []
    for value in sources[:MAX_SOURCES] if isinstance(sources, list) else []:
        if not isinstance(value, dict) or value.get("kind") not in (
            "history",
            "native",
        ):
            continue
        safe_sources.append(
            m.PlaybackSourceObservation(
                kind=value["kind"],
                config_id=_identifier(value.get("config_id")),
                provider=_service(value.get("provider")),
                observed_service=_service(value.get("observed_service"))
                if value.get("observed_service") is not None
                else None,
                observed_config_id=_identifier(value.get("observed_config_id")),
                available=value.get("available")
                if isinstance(value.get("available"), bool)
                else None,
                error_category=_category(value.get("error_category")),
            )
        )
    safe_samples = []
    for value in samples[:MAX_SAMPLES] if isinstance(samples, list) else []:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("scope"), str)
            or value["scope"] not in SCOPES
            or _identifier(value.get("id")) is None
        ):
            continue
        safe_samples.append(m.Target(scope=value["scope"], id=value["id"]))
    return m.PlaybackCoverageObservation(
        observed_at=observed,
        require_fresh=raw.get("require_fresh")
        if isinstance(raw.get("require_fresh"), bool)
        else None,
        blocking=raw.get("blocking") if isinstance(raw.get("blocking"), bool) else None,
        library_unavailable_count=_count(raw.get("library_unavailable_count")),
        selected_unavailable_count=_count(raw.get("selected_unavailable_count")),
        error_category=_category(raw.get("error_category")),
        native_refresh_error_category=_category(
            raw.get("native_refresh_error_category")
        ),
        sources=safe_sources,
        unknown_target_samples=safe_samples,
        sources_truncated=raw.get("sources_truncated") is True
        or isinstance(sources, list)
        and len(sources) > MAX_SOURCES,
        targets_truncated=raw.get("targets_truncated") is True
        or isinstance(samples, list)
        and len(samples) > MAX_SAMPLES,
    )


def playback_fields(raw: object) -> m.PlaybackFieldObservation | None:
    if not isinstance(raw, dict):
        return None

    def fields(key):
        value = raw.get(key)
        return (
            sorted(
                {
                    name
                    for name in value[:100]
                    if isinstance(name, str) and name in FIELD_LABELS
                }
            )
            if isinstance(value, list)
            else []
        )

    return m.PlaybackFieldObservation(
        requested=fields("requested"),
        available=fields("available"),
        unavailable=fields("unavailable"),
    )


def record_playback_observation(
    recorder: AutoDeleteRecorder,
    snapshot: "PlaybackRuleSnapshot | None",
    *,
    fields_by_scope: dict[str, set[str]],
    target_ids: dict[str, set[int]],
    library_unavailable_count: int,
    selected_unavailable_count: int,
    blocking: bool,
    error: str | None,
    native_refresh_error_category: str | None,
    require_fresh: bool,
) -> None:
    """Save batch provenance once; fields are attached to each candidate later."""
    if snapshot is None:
        return
    try:
        sources = []
        samples = []
        if snapshot is not None:
            for value in sorted(
                snapshot.provider_statuses,
                key=lambda value: not (value.error or not value.available),
            )[: MAX_SOURCES + 1]:
                sources.append(
                    {
                        "kind": "history",
                        "config_id": value.config_id,
                        "provider": value.provider.value,
                        "observed_service": value.observed_service.value,
                        "observed_config_id": value.observed_config_id,
                        "available": value.available,
                        "error_category": error_category(value.error),
                    }
                )
            for value in sorted(
                snapshot.native_statuses,
                key=lambda value: not (value.error or not value.available),
            )[: MAX_SOURCES + 1]:
                sources.append(
                    {
                        "kind": "native",
                        "config_id": value.config_id,
                        "provider": value.service.value,
                        "observed_service": value.service.value,
                        "observed_config_id": value.config_id,
                        "available": value.available,
                        "error_category": error_category(value.error),
                    }
                )
            for scope, identifiers in snapshot.unavailable_target_samples.items():
                if scope not in fields_by_scope:
                    continue
                samples.extend(
                    {"scope": scope, "id": identifier}
                    for identifier in identifiers[: MAX_SAMPLES + 1]
                )
            count = 0
            for scope, identifiers in target_ids.items():
                requested = fields_by_scope.get(scope, set())
                for identifier in sorted(identifiers):
                    if count >= recorder.max_records:
                        break
                    available = snapshot.available_fields_by_target.get(
                        (scope, identifier), set()
                    )
                    recorder.playback_fields[(scope, identifier)] = {
                        "requested": sorted(requested),
                        "available": sorted(requested & available),
                        "unavailable": sorted(requested - available),
                    }
                    count += 1
        sources.sort(
            key=lambda value: (
                value["error_category"] is None and value["available"] is not False
            )
        )
        value = playback_observation(
            {
                "observed_at": datetime.now(UTC).isoformat(),
                "require_fresh": require_fresh,
                "blocking": blocking,
                "library_unavailable_count": library_unavailable_count,
                "selected_unavailable_count": selected_unavailable_count,
                "error_category": error_category(error),
                "native_refresh_error_category": native_refresh_error_category,
                "sources": sources,
                "unknown_target_samples": samples,
                "targets_truncated": sum(map(len, target_ids.values()))
                > recorder.max_records,
            }
        )
        if value is not None:
            recorder.scan.context["playback"] = value.model_dump(mode="json")
    except Exception:
        # Recording must never change the authorization result.
        recorder.dropped += 1
