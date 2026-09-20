"""Where the game keeps its files, per operating system.

The only module that knows how the data directory is spelled. The name comes
from branding, so renaming the game does not mean editing paths.

There is no migration code here on purpose. Moving an old directory to a new one
is only needed once a build has shipped under the old name, and code that
handles a situation which has not happened is code nobody can test.
"""

import os
import sys
from pathlib import Path

from utils import branding


def app_data_dir() -> Path:
    """Return the per-user data directory, creating it if needed."""
    directory = _resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def log_dir() -> Path:
    directory = app_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _resolve() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / branding.DATA_DIR_NAME
        return Path.home() / "AppData" / "Roaming" / branding.DATA_DIR_NAME

    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / branding.DATA_DIR_NAME
        )

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / branding.DATA_DIR_NAME_POSIX
    return Path.home() / ".local" / "share" / branding.DATA_DIR_NAME_POSIX


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically.

    write to a temporary file in the same directory,
    then os.replace. Same-directory matters, because os.replace is only atomic
    within a single filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
