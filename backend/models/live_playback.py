"""Live playback contracts, deliberately separate from imported watch history."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class LiveSession:
    item_id: str | None
    media_id: str | None = None
    paths: tuple[str, ...] = ()
    parent_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlaybackSnapshot:
    sessions: tuple[LiveSession, ...] = ()
    available: bool = True


@dataclass(frozen=True)
class PlaybackDeferral:
    code: Literal["currently_playing", "playback_unavailable"]
    checked_at: str
    server_config_ids: tuple[int, ...]
    completed_steps: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        message = (
            "Deferred: currently playing"
            if self.code == "currently_playing"
            else "Deferred: playback status unavailable"
        )
        if self.completed_steps:
            message += f"; {len(self.completed_steps)} earlier step(s) completed"
        return message

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "checked_at": self.checked_at,
            "server_config_ids": list(self.server_config_ids),
            "completed_steps": list(self.completed_steps),
        }


class PlaybackDeferred(Exception):
    def __init__(self, detail: PlaybackDeferral):
        self.detail = detail
        self.freed_bytes = 0
        super().__init__(detail.message)


@dataclass
class CandidateOperationResult:
    succeeded: int = 0
    failed: int = 0
    deferrals: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def deferred(self) -> int:
        return len(self.deferrals)

    def __iter__(self) -> Iterator[int]:
        # Preserve internal two-counter consumers while exposing explicit deferrals.
        yield self.succeeded
        yield self.failed
