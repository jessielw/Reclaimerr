"""Bounded observations of existing operations; never performs or retries work."""

from __future__ import annotations

import inspect
import shutil
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar, cast, overload

from backend.models.live_playback import PlaybackDeferred

STAGES = {
    "validation",
    "playback_guard",
    "prepare_destination",
    "destination_collision",
    "rename",
    "copy",
    "verify_copy",
    "remove_source",
    "directory_move",
    "filesystem_cleanup",
    "provider_delete",
    "provider_unmonitor",
    "provider_refresh",
    "record_cleanup",
    "completed",
    "unknown",
}
OUTCOMES = {"succeeded", "failed", "deferred", "unknown"}
SUBJECTS = {"file", "directory", "path", "candidate", "provider_request"}


@dataclass
class OperationRecorder:
    action: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    outcomes: dict[int, dict[str, Any]] = field(default_factory=dict)
    dropped: int = 0
    max_steps: int = 1000

    def append(self, value: dict[str, Any]) -> None:
        if len(self.steps) >= self.max_steps:
            self.dropped += 1
            if value["outcome"] in ("failed", "deferred"):
                for index, previous in enumerate(self.steps):
                    if previous["outcome"] == "succeeded":
                        self.steps.pop(index)
                        self.steps.append(value)
                        break
            return
        self.steps.append(value)


current_operation_recorder: ContextVar[OperationRecorder | None] = ContextVar(
    "operation_recorder", default=None
)
current_candidates: ContextVar[tuple[int, ...]] = ContextVar(
    "operation_candidates", default=()
)
current_group: ContextVar[tuple[int, ...]] = ContextVar("operation_group", default=())
current_action: ContextVar[str | None] = ContextVar("operation_action", default=None)
current_step: ContextVar[dict[str, Any] | None] = ContextVar(
    "operation_step", default=None
)


def select_candidate(candidate_id: int | None) -> None:
    if current_operation_recorder.get() is not None:
        current_candidates.set((candidate_id,) if candidate_id else ())


def operation_stage(stage: str) -> None:
    step = current_step.get()
    if step is not None and stage in STAGES:
        step["stage"] = stage


def _path(value: object) -> str | None:
    return str(value)[:4096] if value is not None else None


@contextmanager
def operation_step(
    stage: str,
    *,
    path=None,
    destination=None,
    subject="candidate",
    provider=None,
    reference=None,
):
    recorder = current_operation_recorder.get()
    if recorder is None:
        yield
        return
    step = {
        "candidate_ids": current_candidates.get() or current_group.get(),
        "stage": stage,
        "subject": subject,
        "path": _path(path),
        "destination": _path(destination),
        "provider": provider,
        "reference": _path(reference),
        "action": current_action.get() or recorder.action,
        "outcome": "unknown",
        "error": None,
    }
    token = current_step.set(step)
    try:
        yield
    except BaseException as exc:
        step["outcome"] = (
            "deferred"
            if isinstance(exc, PlaybackDeferred)
            else "unknown"
            if not isinstance(exc, Exception)
            else "failed"
        )
        step["error"] = str(exc)[:4096]
        if isinstance(exc, OSError) and exc.filename and subject == "directory":
            recorder.append(
                {
                    **step,
                    "subject": "path",
                    "path": _path(exc.filename),
                    "occurred_at": datetime.now(UTC),
                }
            )
        if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
            for item in exc.args[0][:100]:
                if isinstance(item, tuple) and len(item) == 3:
                    recorder.append(
                        {
                            **step,
                            "subject": "file",
                            "path": _path(item[0]),
                            "destination": _path(item[1]),
                            "error": str(item[2])[:4096],
                            "occurred_at": datetime.now(UTC),
                        }
                    )
        raise
    else:
        step["outcome"] = "succeeded"
    finally:
        current_step.reset(token)
        step["occurred_at"] = datetime.now(UTC)
        recorder.append(step)


def observe_step(
    stage: str,
    *,
    path: str | None = None,
    destination: str | None = None,
    subject="candidate",
    provider=None,
    reference: str | None = None,
):
    """Observe one existing call, including failures later handled by a fallback."""

    def decorate(function):
        signature = inspect.signature(function)

        def observation(args, kwargs):
            if current_operation_recorder.get() is None:
                return operation_step(stage)
            bound = signature.bind(*args, **kwargs).arguments
            return operation_step(
                stage,
                path=bound.get(path) if path else None,
                destination=bound.get(destination) if destination else None,
                subject=subject,
                provider=provider,
                reference=bound.get(reference) if reference else None,
            )

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args, **kwargs):
                with observation(args, kwargs):
                    return await function(*args, **kwargs)

            return async_wrapper

        @wraps(function)
        def wrapper(*args, **kwargs):
            with observation(args, kwargs):
                return function(*args, **kwargs)

        return wrapper

    return decorate


_AsyncFunction = TypeVar("_AsyncFunction", bound=Callable[..., Awaitable[Any]])


@overload
def candidate_scope(
    function: _AsyncFunction, *, action: str | None = None
) -> _AsyncFunction: ...


@overload
def candidate_scope(
    function: None = None, *, action: str | None = None
) -> Callable[[_AsyncFunction], _AsyncFunction]: ...


def candidate_scope(function=None, *, action: str | None = None):
    """Restore caller attribution when a route selects individual candidates."""

    def decorate(target: _AsyncFunction) -> _AsyncFunction:
        @wraps(target)
        async def wrapper(*args, **kwargs):
            token = current_candidates.set(current_candidates.get())
            action_token = current_action.set(action or current_action.get())
            try:
                return await target(*args, **kwargs)
            finally:
                current_action.reset(action_token)
                current_candidates.reset(token)

        return cast(_AsyncFunction, wrapper)

    return decorate(function) if function is not None else decorate


def record_outcome(
    candidate_id: int | None,
    outcome: str,
    *,
    error=None,
    stage="unknown",
    path=None,
    destination=None,
    action=None,
) -> None:
    recorder = current_operation_recorder.get()
    if recorder is None or candidate_id is None or outcome not in OUTCOMES:
        return
    if len(recorder.outcomes) >= 1000 and candidate_id not in recorder.outcomes:
        recorder.dropped += 1
        return
    prior = next(
        (
            step
            for step in reversed(recorder.steps)
            if step["candidate_ids"] == (candidate_id,) and step["outcome"] == "failed"
        ),
        None,
    )
    recorder.outcomes[candidate_id] = {
        "candidate_ids": (candidate_id,),
        "action": action or current_action.get() or recorder.action,
        "stage": prior["stage"] if stage == "unknown" and prior else stage,
        "outcome": outcome,
        "subject": "candidate",
        "error": str(error)[:4096] if error else None,
        "path": _path(path) if path is not None else prior["path"] if prior else None,
        "destination": _path(destination),
        "provider": None,
        "reference": None,
        "occurred_at": datetime.now(UTC),
    }
