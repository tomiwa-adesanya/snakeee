"""Entry point.

Order matters here:

  1. Configure file logging before anything that can fail.
  2. Start waitress on a loopback port in a daemon thread.
  3. Poll /api/health until the server answers, with a hard timeout.
  4. Create the pywebview window, with background_color set so there is no
     white flash before the splash paints.

The window bootstrap is written for this repository. It borrows nothing.
"""

import argparse
import logging
import sys
import threading
import time
import urllib.error
import urllib.request

from utils import logging as applog

LOG_PATH = applog.configure()

from app import APP_VERSION, create_app  # noqa: E402
from utils import branding, clock, ports  # noqa: E402

log = logging.getLogger("main")

WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 760
WINDOW_MIN_WIDTH = 940
WINDOW_MIN_HEIGHT = 620

BACKGROUND_COLOR = "#0e1015"

HEALTH_TIMEOUT_SECONDS = 20.0
HEALTH_POLL_SECONDS = 0.05

_server_error: list = []


def serve(app, port: int) -> None:
    from waitress import serve as waitress_serve

    try:
        waitress_serve(
            app,
            host=ports.LOOPBACK,
            port=port,
            threads=8,
            _quiet=True,
        )
    except Exception as error:  # noqa: BLE001
        _server_error.append(error)
        log.exception("the UI server stopped")


def wait_for_health(port: int, timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    url = f"http://{ports.LOOPBACK}:{port}/api/health"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if _server_error:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(HEALTH_POLL_SECONDS)

    return False


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--port",
        type=int,
        default=ports.DEFAULT_HTTP_PORT,
        help="first TCP port to try for the local UI server",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="run the server only, without creating a window (for testing)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    try:
        return _run(parse_args(argv if argv is not None else sys.argv[1:]))
    finally:
        clock.restore_resolution()


def _run(options) -> int:

    log.info("starting version %s, log file %s", APP_VERSION, LOG_PATH)

    # Before anything that sleeps on a clock, which on this platform is the
    # simulation and the match broadcast. Released in the finally below, because
    # it is a machine-wide setting rather than one belonging to this process.
    clock.raise_resolution()

    try:
        port = ports.find_free(options.port)
    except OSError as error:
        applog.report_fatal(str(error))
        return 1

    app = create_app()

    server_thread = threading.Thread(
        target=serve,
        args=(app, port),
        name="ui-server",
        daemon=True,
    )
    server_thread.start()

    if not wait_for_health(port):
        detail = str(_server_error[0]) if _server_error else "timed out"
        applog.report_fatal(
            f"The user interface server did not start ({detail}).\n\n"
            f"The log file is at:\n{LOG_PATH}"
        )
        return 1

    log.info("ui server ready on %s:%s", ports.LOOPBACK, port)

    if options.no_window:
        log.info("running without a window, press ctrl-c to stop")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            app.shutdown_threads()
            return 0

    try:
        import webview
    except ImportError as error:
        applog.report_fatal(
            f"pywebview is not installed ({error}).\n\n"
            "Run: pip install -r requirements.txt"
        )
        return 1

    try:
        webview.create_window(
            branding.WINDOW_TITLE,
            f"http://{ports.LOOPBACK}:{port}/",
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
            background_color=BACKGROUND_COLOR,
        )
        webview.start()
    except Exception as error:  # noqa: BLE001
        applog.report_fatal(
            f"The window could not be created ({error}).\n\n"
            "On Windows this usually means the WebView2 Runtime is missing.\n"
            "On Linux it usually means the GTK WebKit packages are missing;\n"
            "see the README.\n\n"
            f"The log file is at:\n{LOG_PATH}"
        )
        app.shutdown_threads()
        return 1

    log.info("window closed, stopping threads")

    # Closing the window must terminate every thread and release every port.
    # The room listener in particular has to go, or the next launch finds the
    # port still bound.
    try:
        app.shutdown_threads()
    except Exception:  # noqa: BLE001
        log.exception("failed to stop application threads cleanly")

    log.info("exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
