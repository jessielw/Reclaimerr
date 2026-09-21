from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from enum import StrEnum
from typing import Any

from backend.core.logger import LOG
from backend.core.memory import cleanup_process_memory, log_memory_snapshot
from backend.core.service_manager import service_manager
from backend.core.task_env import TASK_CHILD_ARG, TASK_CHILD_ENV, TASK_ISOLATION_ENV
from backend.enums import Task

__all__ = [
    "TASK_CHILD_ARG",
    "TASK_CHILD_ENV",
    "TASK_ISOLATION_ENV",
    "TaskExecutionMode",
    "TaskIsolationUnavailable",
    "get_task_execution_mode",
    "run_task_in_subprocess",
    "run_task_job",
    "run_task_with_memory_cleanup",
    "should_cleanup_task_memory",
    "should_isolate_task",
]

# Flipped once a child proves isolation cannot work on this install (see #398).
# Every later task then runs inline instead of paying for a doomed spawn.
_isolation_unavailable = False


class TaskIsolationUnavailable(RuntimeError):
    """Raised when a task child never got far enough to run its task.

    That means the spawn itself is broken rather than the task, so the caller
    can safely retry inline - nothing ran yet, so nothing is repeated.
    """


class TaskExecutionMode(StrEnum):
    INLINE = "inline"
    ISOLATED = "isolated"


INLINE_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA_LIBRARIES,
        Task.TAG_CLEANUP_CANDIDATES,
        Task.WEEKLY_HOUSE_KEEPING,
        Task.CHECK_APP_UPDATES,
        Task.MDBLIST_RATINGS_REFRESH,
        Task.OMDB_RATINGS_REFRESH,
    }
)


ISOLATED_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA,
        Task.RESYNC_MEDIA,
        Task.SYNC_LINKED_DATA,
        Task.REFRESH_PLAYBACK_HISTORY,
        Task.SCAN_CLEANUP_CANDIDATES,
        Task.DELETE_CLEANUP_CANDIDATES,
        Task.IMDB_RATINGS_REFRESH,
        Task.ANILIST_RATINGS_REFRESH,
    }
)


def get_task_execution_mode(task: Task) -> TaskExecutionMode:
    if task in ISOLATED_TASKS:
        return TaskExecutionMode.ISOLATED
    if task in INLINE_TASKS:
        return TaskExecutionMode.INLINE
    return TaskExecutionMode.INLINE


def should_isolate_task(task: Task) -> bool:
    if _isolation_unavailable:
        return False
    if os.getenv(TASK_CHILD_ENV) == "1":
        return False
    if os.getenv(TASK_ISOLATION_ENV, "auto").strip().lower() == "off":
        return False
    return get_task_execution_mode(task) is TaskExecutionMode.ISOLATED


def should_cleanup_task_memory(task: Task) -> bool:
    return get_task_execution_mode(task) is TaskExecutionMode.ISOLATED


async def run_task_job(
    task: Task, service_config_id: int | None = None
) -> dict[str, Any] | None:
    """Run one queued task using the configured execution mode."""
    if should_isolate_task(task):
        return await run_task_in_subprocess(task, service_config_id)
    if should_cleanup_task_memory(task):
        return await run_task_with_memory_cleanup(task, service_config_id)

    from backend.core.task_runtime import execute_task

    return await execute_task(task, service_config_id)


async def run_task_with_memory_cleanup(
    task: Task, service_config_id: int | None = None
) -> dict[str, Any] | None:
    """Run a task inline and clean up transient memory afterwards."""
    from backend.core.task_runtime import execute_task

    context = task.friendly_name()
    log_memory_snapshot(f"before {context}")
    try:
        return await execute_task(task, service_config_id)
    finally:
        service_manager.clear_transient_caches()
        cleanup_process_memory(context=context)


async def run_task_in_subprocess(
    task: Task, service_config_id: int | None = None
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {"task": task.value}
    if service_config_id is not None:
        payload["service_config_id"] = service_config_id
    request = json.dumps(payload, separators=(",", ":")).encode()
    env = {**os.environ, TASK_CHILD_ENV: "1"}
    command = _task_child_command()

    try:
        if os.name == "nt":
            LOG.info(
                f"Running {task.friendly_name()} in a Windows thread-backed child process"
            )
            return await asyncio.to_thread(
                _run_task_in_blocking_subprocess,
                task,
                request,
                env,
                command,
            )
        return await _run_task_in_async_subprocess(task, request, env, command)
    except TaskIsolationUnavailable as exc:
        return await _fall_back_to_inline(task, service_config_id, str(exc))


async def _run_task_in_async_subprocess(
    task: Task,
    request: bytes,
    env: dict[str, str],
    command: list[str],
) -> dict[str, Any] | None:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except NotImplementedError as exc:
        raise TaskIsolationUnavailable(
            "this Python event loop cannot spawn subprocesses"
        ) from exc
    except OSError as exc:
        raise TaskIsolationUnavailable(
            f"{' '.join(command)} could not be started: {exc}"
        ) from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise TaskIsolationUnavailable(
            "the task child process pipes could not be opened"
        )

    process.stdin.write(request + b"\n")
    await process.stdin.drain()
    process.stdin.close()

    stdout_lines: list[str] = []
    stderr_tail: list[str] = []
    stdout_task = asyncio.create_task(_collect_stdout(process.stdout, stdout_lines))
    stderr_task = asyncio.create_task(_forward_stderr(process.stderr, stderr_tail))

    return_code = await process.wait()
    await stdout_task
    await stderr_task

    return _evaluate_child_outcome(
        task, command, return_code, stdout_lines, stderr_tail
    )


def _run_task_in_blocking_subprocess(
    task: Task,
    request: bytes,
    env: dict[str, str],
    command: list[str] | None = None,
) -> dict[str, Any] | None:
    child_command = command or _task_child_command()
    try:
        process = subprocess.Popen(
            child_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise TaskIsolationUnavailable(
            f"{' '.join(child_command)} could not be started: {exc}"
        ) from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise TaskIsolationUnavailable(
            "the task child process pipes could not be opened"
        )

    stderr_tail: list[str] = []
    try:
        process.stdin.write(request.decode("utf-8") + "\n")
        process.stdin.close()

        for line in process.stderr:
            decoded = line.rstrip()
            if not decoded:
                continue
            stderr_tail.append(decoded)
            del stderr_tail[:-50]
            LOG.forward(decoded)

        stdout_text = process.stdout.read()
        return_code = process.wait()
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    stdout_lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    return _evaluate_child_outcome(
        task, child_command, return_code, stdout_lines, stderr_tail
    )


def _evaluate_child_outcome(
    task: Task,
    command: list[str],
    return_code: int,
    stdout_lines: list[str],
    stderr_tail: list[str],
) -> dict[str, Any] | None:
    """Turn a finished task child into a result, or an error worth reading."""
    name = task.friendly_name()
    result = _parse_child_result(stdout_lines)
    tail = "\n".join(stderr_tail[-20:])

    # A child that printed nothing at all never reached the task body: it exited
    # before backend.core.task_child ever ran, so the spawn is what is broken.
    # Hand that back as a retry rather than failing a task nobody attempted.
    if result is None and not stdout_lines and not stderr_tail:
        raise TaskIsolationUnavailable(
            f"{' '.join(command)} exited with code {return_code} "
            "without writing anything to stdout or stderr"
        )

    if return_code != 0:
        detail = _child_error(result) or tail
        raise RuntimeError(
            f"Isolated task {name} failed with exit code {return_code}"
            + (f": {detail}" if detail else "")
        )

    # The child logged, so it got far enough that the task may already have done
    # work. Report that instead of quietly running the whole thing a second time.
    if result is None:
        raise RuntimeError(
            f"Isolated task {name} completed without a result payload "
            f"(exit code {return_code}, {len(stdout_lines)} stdout line(s))"
            + (f", last child output: {tail}" if tail else "")
        )

    if result.get("ok") is not True:
        detail = _child_error(result) or "unknown error"
        raise RuntimeError(f"Isolated task {name} failed: {detail}")

    result_payload = result.get("result")
    return result_payload if isinstance(result_payload, dict) else None


async def _fall_back_to_inline(
    task: Task, service_config_id: int | None, reason: str
) -> dict[str, Any] | None:
    """Run a task inline after isolation turned out to be unusable here."""
    global _isolation_unavailable

    already_reported = _isolation_unavailable
    _isolation_unavailable = True
    if not already_reported:
        LOG.warning(
            f"Task isolation is not working on this install: {reason}. Heavy "
            "tasks will run inline until the app is restarted, which reclaims "
            "less memory but keeps them working."
        )
    LOG.info(f"Running {task.friendly_name()} inline instead of in a child process")
    return await run_task_with_memory_cleanup(task, service_config_id)


def _task_child_command() -> list[str]:
    # The backend also runs in Docker/source deployments where the desktop
    # package is intentionally absent. Frozen desktop builds expose this on
    # ``sys`` directly, so task execution does not need a desktop dependency.
    if getattr(sys, "frozen", False):
        return [sys.executable, TASK_CHILD_ARG]
    return [sys.executable, "-m", "backend.core.task_child"]


async def _collect_stdout(
    stream: asyncio.StreamReader, stdout_lines: list[str]
) -> None:
    while line := await stream.readline():
        decoded = line.decode("utf-8", errors="replace").strip()
        if decoded:
            stdout_lines.append(decoded)


async def _forward_stderr(stream: asyncio.StreamReader, stderr_tail: list[str]) -> None:
    while line := await stream.readline():
        decoded = line.decode("utf-8", errors="replace").rstrip()
        if not decoded:
            continue
        stderr_tail.append(decoded)
        del stderr_tail[:-50]
        LOG.forward(decoded)


def _parse_child_result(stdout_lines: list[str]) -> dict[str, Any] | None:
    for line in reversed(stdout_lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            LOG.debug(f"Ignoring non-JSON task child stdout: {line}")
            continue
        return payload if isinstance(payload, dict) else None
    return None


def _child_error(result: dict[str, Any] | None) -> str | None:
    if result is None:
        return None
    error = result.get("error")
    return str(error) if error else None
