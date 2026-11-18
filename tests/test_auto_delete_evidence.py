from datetime import UTC, datetime
from types import SimpleNamespace

from backend.core.auto_delete_diagnostics import AutoDeleteRecorder
from backend.services.auto_delete_evidence import (
    MAX_SAMPLES,
    MAX_SOURCES,
    playback_observation,
    record_playback_observation,
)


def test_playback_history_bounds_and_legacy_unknowns():
    assert playback_observation(None) is None
    assert playback_observation({"observed_at": "invalid"}) is None
    value = playback_observation(
        {
            "observed_at": datetime.now(UTC).isoformat(),
            "sources": [{"kind": "history", "provider": "tracearr", "available": False}]
            * (MAX_SOURCES + 1),
            "unknown_target_samples": [
                {"scope": "season", "id": i + 1} for i in range(MAX_SAMPLES + 1)
            ],
        }
    )
    assert len(value.sources) == MAX_SOURCES and value.sources_truncated
    assert len(value.unknown_target_samples) == MAX_SAMPLES and value.targets_truncated
    assert (
        value.library_unavailable_count
        is value.selected_unavailable_count
        is value.blocking
        is None
    )


def test_playback_recording_failure_is_nonfatal():
    recorder = AutoDeleteRecorder()
    record_playback_observation(
        recorder,
        SimpleNamespace(provider_statuses=None),
        fields_by_scope={},
        target_ids={},
        library_unavailable_count=0,
        selected_unavailable_count=0,
        blocking=False,
        error=None,
        native_refresh_error_category=None,
        require_fresh=True,
    )
    assert recorder.dropped == 1
    assert "playback" not in recorder.scan.context
