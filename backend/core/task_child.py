from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any

from backend.core.logger import LOG
from backend.core.service_bootstrap import load_enabled_services
from backend.core.service_manager import service_manager
from backend.core.task_process import run_task_with_memory_cleanup
from backend.database import close_db
from backend.enums import Task

SERVICE_BOOTSTRAP_TASKS: frozenset[Task] = frozenset(
    {
        Task.SYNC_MEDIA,
        Task.RESYNC_MEDIA,
        Task.SYNC_LINKED_DATA,
        Task.REFRESH_PLAYBACK_HISTORY,
        Task.SCAN_CLEANUP_CANDIDATES,
    }
)


async def run_task_child() -> int:
    try:
        raw_request = sys.stdin.readline()
        request = json.loads(raw_request)
        task = Task(str(request["task"]))
    except Exception as exc:
        _write_result({"ok": False, "error": f"Invalid task child request: {exc}"})
        return 2

    try:
        if task in SERVICE_BOOTSTRAP_TASKS:
            await load_enabled_services()
        result = await run_task_with_memory_cleanup(task)
        _write_result({"ok": True, "result": result})
        return 0
    except Exception as exc:
        LOG.error(
            f"Isolated task {task.friendly_name()} failed: {exc}",
            exc_info=True,
        )
        _write_result(
            {
                "ok": False,
                "error": str(exc) or exc.__class__.__name__,
                "traceback": traceback.format_exc(limit=20),
            }
        )
        return 1
    finally:
        await service_manager.clear_all()
        await close_db()
        LOG.stop()


def _write_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), default=str), flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_task_child()))
