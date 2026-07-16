import multiprocessing

# must be the very first call when frozen
multiprocessing.freeze_support()

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from desktop.server import ReclaimerServer

TASK_CHILD_ARG = "--task-child"

server = ReclaimerServer()

if TASK_CHILD_ARG in sys.argv[1:]:
    server.prepare_env()
    from backend.core.task_child import run_task_child

    raise SystemExit(asyncio.run(run_task_child()))

from desktop.tray import create_icon

# Single instance enforcement. FileLock uses an OS level exclusive file lock
# that is automatically released if the process crashes (no stale lock files).
_lock_path = server.data_dir / "reclaimerr.lock"
_lock_path.parent.mkdir(parents=True, exist_ok=True)
_instance_lock = FileLock(str(_lock_path), timeout=0)
try:
    _instance_lock.acquire()
except Timeout:
    # another instance is already running so we can just exit silently
    sys.exit(0)

# set env vars before any backend module is imported.
server.prepare_env()

# expose a shutdown callback to the API so the web UI can trigger a clean exit
# (must happen AFTER prepare_env so backend env vars are set before import)
from backend.api.main import fastapi_app as _app

# hook into the callback
_app.state.shutdown_callback = server.stop

# write a PID file so power users can send signals from the CLI
_pid_path: Path | None = None
try:
    pid_path = server.data_dir / "reclaimerr.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))
    _pid_path = pid_path
except OSError:
    _pid_path = None

icon = create_icon(server)


# handle Ctrl+C / SIGTERM in dev (frozen: no console so these never fire).
def _handle_signal(_signum: int, _frame: Any) -> None:
    if icon is not None:
        icon.stop()  # removes tray icon immediately
    server.stop()  # signals uvicorn to exit -> unblocks serve()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# start the tray icon in a background thread (run_detached is non-blocking).
# On platforms without a system tray (e.g. headless Linux, Wayland without
# a compatible tray backend) pystray will raise - we catch it and continue
# without a tray so the app is still usable via the web UI.
try:
    if icon is not None:
        icon.run_detached()
except Exception as _tray_err:
    import warnings

    warnings.warn(
        f"System tray unavailable ({_tray_err}). "
        "Use the web UI Shutdown button or send SIGTERM to stop the process.",
        stacklevel=1,
    )
    icon = None

try:
    # block the main thread on the ASGI server.
    # returns when stop() is called (Quit menu item, web UI, or signal).
    server.serve()
finally:
    # ensure tray icon is always cleaned up on exit.
    if icon is not None:
        icon.stop()
    # remove PID file on clean exit
    if _pid_path is not None:
        try:
            _pid_path.unlink(missing_ok=True)
        except OSError:
            pass
