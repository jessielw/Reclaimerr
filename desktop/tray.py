import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import pystray
from PIL import Image
from pystray._base import Icon as PystrayIcon

from desktop.utils import is_bundled

if TYPE_CHECKING:
    from desktop.server import ReclaimerServer


def _resource(relative: str) -> Path:
    """Resolve a bundled resource path that works in both dev and PyInstaller onedir."""
    if is_bundled:
        return is_bundled / relative
    return Path(__file__).resolve().parent.parent / relative


def create_icon(server: "ReclaimerServer") -> PystrayIcon:
    """Create the system tray icon with menu actions."""

    def open_browser(_icon: PystrayIcon, _item: pystray.MenuItem) -> None:
        webbrowser.open(f"http://localhost:{server.port}")

    def quit_app(_icon: PystrayIcon, _item: pystray.MenuItem) -> None:
        server.stop()

    icon_image = Image.open(_resource("frontend/static/favicon.ico"))
    menu = pystray.Menu(
        pystray.MenuItem("Open Reclaimerr", open_browser, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    return pystray.Icon("Reclaimerr", icon_image, "Reclaimerr", menu)
