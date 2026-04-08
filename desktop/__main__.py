import multiprocessing

# must be the very first call when frozen
multiprocessing.freeze_support()

import signal

from desktop.server import ReclaimerServer
from desktop.tray import PORT, create_icon

server = ReclaimerServer(port=PORT)

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
