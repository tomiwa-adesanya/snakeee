"""File logging, and the fatal-startup-error escape hatch.

the release binary is built with the console disabled,
so a windowless failure produces no output at all. Logging is therefore
configured on the very first line of main.py, before anything that can fail,
and any fatal startup error is surfaced through a native message box as well as
the log.

report_fatal lives here rather than in its own module because it exists purely
to make a logged fatal error visible to a user who has no console.
"""

import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path

from utils import branding
from utils.store import paths

LOG_FILENAME = "app.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_configured = False


def configure() -> Path:
    """Configure root logging to a rotating file. Returns the log file path."""
    global _configured

    log_path = paths.log_dir() / LOG_FILENAME

    if _configured:
        return log_path

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # A console handler is useful when running from source and harmless when
    # there is no console attached.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(console)

    logging.getLogger("waitress").setLevel(logging.WARNING)

    _configured = True
    return log_path


def report_fatal(message: str) -> None:
    """Log a fatal error and try to show it natively. Never raises."""
    logging.getLogger("fatal").critical(message)

    title = branding.APP_NAME + " failed to start"
    try:
        _native_message(title, message)
    except Exception:
        logging.getLogger("fatal").exception("could not show a native dialog")


def _native_message(title: str, message: str) -> None:
    if sys.platform.startswith("win"):
        import ctypes

        mb_iconerror = 0x10
        ctypes.windll.user32.MessageBoxW(None, message, title, mb_iconerror)
        return

    if sys.platform == "darwin":
        script = (
            "display dialog "
            + _applescript_quote(message)
            + " with title "
            + _applescript_quote(title)
            + ' buttons {"OK"}'
        )
        subprocess.run(["osascript", "-e", script], timeout=30, check=False)
        return

    for command in (
        ["zenity", "--error", "--title", title, "--text", message],
        ["kdialog", "--error", message, "--title", title],
        ["xmessage", "-center", title + "\n\n" + message],
    ):
        try:
            subprocess.run(command, timeout=30, check=False)
            return
        except FileNotFoundError:
            continue

    print(title + ": " + message, file=sys.stderr)


def _applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
