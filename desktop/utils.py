from __future__ import annotations

import socket
import sys
from pathlib import Path

__all__ = ["read_env_file", "find_free_port", "is_bundled"]


def read_env_file(path: Path) -> dict[str, str]:
    """Read a simple KEY=VALUE .env file into a dictionary."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def find_free_port(api_host: str, start: int, max_attempts: int = 10) -> int:
    """Return the first free TCP port at or after `start`."""
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((api_host, port))
                return port
            except OSError:
                continue
    raise OSError(
        f"Could not find a free port in range {start}-{start + max_attempts - 1}. "
        "Close other applications and try again."
    )


class Bundled:
    """Class to avoid using globals."""

    __slots__ = ["bundled"]

    def __init__(self) -> None:
        self.bundled = self.is_bundled()

    @staticmethod
    def is_bundled() -> Path | None:
        """Detect if running in a PyInstaller bundle and return the bundle's temp path if so."""
        is_meipass = getattr(sys, "_MEIPASS", None)
        if getattr(sys, "frozen", False) and is_meipass:
            if is_meipass:
                return Path(is_meipass)
        return None


# we should always use this instance instead of the class Bundled directly
is_bundled = Bundled().bundled
