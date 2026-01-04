import sys
from pathlib import Path

import platformdirs


def _get_working_directories() -> tuple[Path, Path, bool]:
    """
    Used to determine the correct working directory automatically.
    This way we can utilize files/relative paths easily.

    Returns:
        (Path, Path, bool): Current working directory, runtime directory, frozen.
    """
    # we're in a pyinstaller bundle
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        path = Path(sys.executable).parent
        return path, path / "bundle" / "runtime", True

    # we're running from a *.py file
    else:
        path = Path.cwd()
        return path, path / "runtime", False


def _get_config_directory() -> Path:
    """
    Get the appropriate config directory based on portable mode.

    Portable mode (runtime dir) is used when:
    - Running from source (dev mode)
    - PORTABLE_MODE environment variable is set
    - Useful for Docker containers where config should be in a known mounted location

    Returns:
        Path: Config directory (runtime dir for portable, appdata for installed)
    """
    import os

    # check if portable mode is requested or if running from source
    if not IS_FROZEN or os.getenv("PORTABLE_MODE", "").lower() in ("1", "true", "yes"):
        # portable mode: use local runtime directory
        return RUNTIME_DIR
    else:
        # installed mode: use platformdirs for user config
        config_dir = Path(platformdirs.user_config_dir("Vacuumarr", "Vacuumarr"))
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir


CURRENT_DIR, RUNTIME_DIR, IS_FROZEN = _get_working_directories()
CONFIG_DIR = _get_config_directory()
IS_PORTABLE_MODE = CONFIG_DIR == RUNTIME_DIR
