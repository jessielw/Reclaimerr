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
from backend.enums import Task
from desktop.utils import is_bundled

TASK_CHILD_ENV = "RECLAIMERR_TASK_CHILD"
TASK_ISOLATION_ENV = "RECLAIMERR_TASK_ISOLATION"
TASK_CHILD_ARG = "--task-child"


class TaskExecutionMode(StrEnum):
    INLINE = "inline"
    ISOLATED = "isolated"


INLINE_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA_LIBRARIES,
        Task.TAG_CLEANUP_CANDIDATES,
        Task.DELETE_CLEANUP_CANDIDATES,
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
    if os.getenv(TASK_CHILD_ENV) == "1":
        return False
    if os.getenv(TASK_ISOLATION_ENV, "auto").strip().lower() == "off":
        return False
    return get_task_execution_mode(task) is TaskExecutionMode.ISOLATED


def should_cleanup_task_memory(task: Task) -> bool:
    return get_task_execution_mode(task) is TaskExecutionMode.ISOLATED


async def run_task_job(task: Task) -> dict[str, Any] | None:
    """Run one queued task using the configured execution mode."""
    if should_isolate_task(task):
        return await run_task_in_subprocess(task)
    if should_cleanup_task_memory(task):
        return await run_task_with_memory_cleanup(task)

    from backend.core.task_runtime import execute_task

    return await execute_task(task)


async def run_task_with_memory_cleanup(task: Task) -> dict[str, Any] | None:
    """Run a task inline and clean up transient memory afterwards."""
    from backend.core.task_runtime import execute_task

    context = task.friendly_name()
    log_memory_snapshot(f"before {context}")
    try:
        return await execute_task(task)
    finally:
        service_manager.clear_transient_caches()
        cleanup_process_memory(context=context)


async def run_task_in_subprocess(task: Task) -> dict[str, Any] | None:
    request = json.dumps({"task": task.value}, separators=(",", ":")).encode()
    env = {**os.environ, TASK_CHILD_ENV: "1"}
    command = _task_child_command()
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

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except NotImplementedError:
        LOG.warning(
            f"Task isolation is unavailable in this Python event loop; "
            f"running {task.friendly_name()} inline with memory cleanup"
        )
        return await run_task_with_memory_cleanup(task)
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("Could not open task child process pipes")

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

    result = _parse_child_result(stdout_lines)
    if return_code != 0:
        error = _child_error(result) or "\n".join(stderr_tail[-20:])
        raise RuntimeError(
            f"Isolated task {task.friendly_name()} failed"
            + (f": {error}" if error else "")
        )

    if result is None:
        raise RuntimeError(
            f"Isolated task {task.friendly_name()} completed without a result payload"
        )
    if result.get("ok") is not True:
        error = _child_error(result) or "unknown error"
        raise RuntimeError(f"Isolated task {task.friendly_name()} failed: {error}")

    payload = result.get("result")
    return payload if isinstance(payload, dict) else None


def _run_task_in_blocking_subprocess(
    task: Task,
    request: bytes,
    env: dict[str, str],
    command: list[str] | None = None,
) -> dict[str, Any] | None:
    process = subprocess.Popen(
        command or _task_child_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("Could not open task child process pipes")

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
            LOG.info(f"[task-child] {decoded}")

        stdout_text = process.stdout.read()
        return_code = process.wait()
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    result = _parse_child_result(
        [line.strip() for line in stdout_text.splitlines() if line.strip()]
    )
    if return_code != 0:
        error = _child_error(result) or "\n".join(stderr_tail[-20:])
        raise RuntimeError(
            f"Isolated task {task.friendly_name()} failed"
            + (f": {error}" if error else "")
        )
    if result is None:
        raise RuntimeError(
            f"Isolated task {task.friendly_name()} completed without a result payload"
        )
    if result.get("ok") is not True:
        error = _child_error(result) or "unknown error"
        raise RuntimeError(f"Isolated task {task.friendly_name()} failed: {error}")

    payload = result.get("result")
    return payload if isinstance(payload, dict) else None


def _task_child_command() -> list[str]:
    if is_bundled is not None:
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
        LOG.info(f"[task-child] {decoded}")


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
