import multiprocessing

# must be the very first call when frozen
multiprocessing.freeze_support()

import signal
import sys

from filelock import FileLock, Timeout

from desktop.server import ReclaimerServer
from desktop.tray import PORT, create_icon

server = ReclaimerServer(port=PORT)

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

icon = create_icon(server)


# handle Ctrl+C / SIGTERM in dev (frozen: no console so these never fire).
def _handle_signal(_signum, _frame) -> None:
    icon.stop()  # removes tray icon immediately
    server.stop()  # signals uvicorn to exit -> unblocks serve()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# start the tray icon in a background thread (run_detached is non blocking).
icon.run_detached()

try:
    # block the main thread on the ASGI server.
    # returns when stop() is called (Quit menu item or signal).
    server.serve()
finally:
    # ensure tray icon is always removed on exit, even on unexpected errors.
    icon.stop()
