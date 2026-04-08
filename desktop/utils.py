import sys
from pathlib import Path

__all__ = ["is_bundled"]


class Bundled:
    """Class to avoid using globals."""

    __slots__ = ["bundled"]

    def __init__(self) -> None:
        self.bundled = self.is_bundled()

    @staticmethod
    def is_bundled() -> Path | None:
        """Detect if running in a PyInstaller bundle and return the bundle's temp path if so."""
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)  # type: ignore[reportAttributeAccessIssue]
        return None


# we should always use this instance instead of the class Bundled directly
is_bundled = Bundled().bundled
