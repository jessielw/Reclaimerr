from __future__ import annotations

import asyncio
from io import StringIO

from backend.core import task_child, task_process
from backend.enums import Task


def test_should_isolate_heavy_task_by_default(monkeypatch) -> None:
    monkeypatch.delenv(task_process.TASK_CHILD_ENV, raising=False)
    monkeypatch.delenv(task_process.TASK_ISOLATION_ENV, raising=False)

    assert task_process.should_isolate_task(Task.IMDB_RATINGS_REFRESH) is True


def test_should_not_isolate_light_task_by_default(monkeypatch) -> None:
    monkeypatch.delenv(task_process.TASK_CHILD_ENV, raising=False)
    monkeypatch.delenv(task_process.TASK_ISOLATION_ENV, raising=False)

    assert task_process.should_isolate_task(Task.CHECK_APP_UPDATES) is False


def test_task_execution_modes_are_explicit() -> None:
    assert (
        task_process.get_task_execution_mode(Task.IMDB_RATINGS_REFRESH)
        is task_process.TaskExecutionMode.ISOLATED
    )
    assert (
        task_process.get_task_execution_mode(Task.CHECK_APP_UPDATES)
        is task_process.TaskExecutionMode.INLINE
    )
    assert (
        task_process.get_task_execution_mode(Task.DELETE_CLEANUP_CANDIDATES)
        is task_process.TaskExecutionMode.ISOLATED
    )


def test_task_isolation_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv(task_process.TASK_CHILD_ENV, raising=False)
    monkeypatch.setenv(task_process.TASK_ISOLATION_ENV, "off")

    assert task_process.should_isolate_task(Task.IMDB_RATINGS_REFRESH) is False


def test_task_child_guard_prevents_recursive_isolation(monkeypatch) -> None:
    monkeypatch.setenv(task_process.TASK_CHILD_ENV, "1")
    monkeypatch.delenv(task_process.TASK_ISOLATION_ENV, raising=False)

    assert task_process.should_isolate_task(Task.IMDB_RATINGS_REFRESH) is False


def test_delete_task_child_bootstraps_services_before_execution(monkeypatch) -> None:
    events: list[str] = []
    written_results: list[dict[str, object]] = []

    async def load_services() -> None:
        events.append("bootstrap")

    async def run_task(task: Task) -> dict[str, int]:
        assert task is Task.DELETE_CLEANUP_CANDIDATES
        events.append("execute")
        return {"deleted": 1}

    async def clear_services() -> None:
        events.append("clear")

    async def close_database() -> None:
        events.append("close_db")

    monkeypatch.setattr(
        task_child.sys,
        "stdin",
        StringIO(f'{{"task":"{Task.DELETE_CLEANUP_CANDIDATES.value}"}}\n'),
    )
    monkeypatch.setattr(task_child, "load_enabled_services", load_services)
    monkeypatch.setattr(task_child, "run_task_with_memory_cleanup", run_task)
    monkeypatch.setattr(task_child.service_manager, "clear_all", clear_services)
    monkeypatch.setattr(task_child, "close_db", close_database)
    monkeypatch.setattr(task_child.LOG, "stop", lambda: None)
    monkeypatch.setattr(task_child, "_write_result", written_results.append)

    exit_code = asyncio.run(task_child.run_task_child())

    assert exit_code == 0
    assert events == ["bootstrap", "execute", "clear", "close_db"]
    assert written_results == [{"ok": True, "result": {"deleted": 1}}]


def test_parse_child_result_uses_last_valid_json() -> None:
    result = task_process._parse_child_result(
        [
            "not json",
            '{"ok":false,"error":"old"}',
            '{"ok":true,"result":{"deleted":1}}',
        ]
    )

    assert result == {"ok": True, "result": {"deleted": 1}}


def test_blocking_subprocess_reads_stdout_result_and_streams_stderr(
    monkeypatch,
) -> None:
    captured_command: list[str] | None = None

    class FakeInput:
        closed = False

        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, value: str) -> None:
            self.writes.append(value)

        def close(self) -> None:
            self.closed = True

    class FakeOutput:
        closed = False

        def __init__(self, value: str) -> None:
            self.value = value

        def read(self) -> str:
            return self.value

        def close(self) -> None:
            self.closed = True

    class FakeError:
        closed = False

        def __iter__(self):
            return iter(["child log line\n"])

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self, args, **_kwargs) -> None:
            nonlocal captured_command
            captured_command = args
            self.stdin = FakeInput()
            self.stdout = FakeOutput('{"ok":true,"result":{"rows":1}}\n')
            self.stderr = FakeError()
            self.returncode = 0

        def wait(self) -> int:
            return self.returncode

    monkeypatch.setattr(task_process.subprocess, "Popen", FakeProcess)

    result = task_process._run_task_in_blocking_subprocess(
        Task.IMDB_RATINGS_REFRESH,
        b'{"task":"imdb_ratings_refresh"}',
        {},
        ["python", "-m", "backend.core.task_child"],
    )

    assert result == {"rows": 1}
    assert captured_command == ["python", "-m", "backend.core.task_child"]


def test_run_task_in_subprocess_uses_blocking_child_on_windows(
    monkeypatch,
) -> None:
    def blocking_child(
        task: Task,
        _request: bytes,
        _env: dict[str, str],
        _command: list[str],
    ):
        return {"task": task.value}

    def unexpected_async_subprocess(*_args, **_kwargs):
        raise AssertionError("Windows path should not use asyncio subprocesses")

    monkeypatch.setattr(task_process.os, "name", "nt")
    monkeypatch.setattr(
        task_process.asyncio,
        "create_subprocess_exec",
        unexpected_async_subprocess,
    )
    monkeypatch.setattr(
        task_process,
        "_run_task_in_blocking_subprocess",
        blocking_child,
    )

    result = asyncio.run(task_process.run_task_in_subprocess(Task.IMDB_RATINGS_REFRESH))

    assert result == {"task": Task.IMDB_RATINGS_REFRESH.value}


def test_task_child_command_uses_python_module_in_source(monkeypatch) -> None:
    monkeypatch.setattr(task_process.sys, "frozen", False, raising=False)
    monkeypatch.setattr(task_process.sys, "executable", "python")

    assert task_process._task_child_command() == [
        "python",
        "-m",
        "backend.core.task_child",
    ]


def test_task_child_command_uses_desktop_child_arg_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(task_process.sys, "frozen", True, raising=False)
    monkeypatch.setattr(task_process.sys, "executable", "reclaimerr.exe")

    assert task_process._task_child_command() == [
        "reclaimerr.exe",
        task_process.TASK_CHILD_ARG,
    ]


def test_run_task_in_subprocess_falls_back_inline_for_unsupported_non_windows_loop(
    monkeypatch,
) -> None:
    async def unsupported_subprocess(*_args, **_kwargs):
        raise NotImplementedError

    async def inline_fallback(task: Task):
        return {"task": task.value}

    monkeypatch.setattr(task_process.os, "name", "posix")
    monkeypatch.setattr(
        task_process.asyncio,
        "create_subprocess_exec",
        unsupported_subprocess,
    )
    monkeypatch.setattr(task_process, "run_task_with_memory_cleanup", inline_fallback)

    result = asyncio.run(task_process.run_task_in_subprocess(Task.IMDB_RATINGS_REFRESH))

    assert result == {"task": Task.IMDB_RATINGS_REFRESH.value}
