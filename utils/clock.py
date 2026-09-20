"""System timer resolution.

Windows runs its scheduler on a 15.6 millisecond tick by default. Every sleep in
this application is rounded up to that, which is a problem for two of them in
particular.

The simulation ticks at 60 Hz, so it wants to wait about 16.7 milliseconds
between steps. Ask Windows for that and you wait 15.6, then 15.6 again, and the
loop notices it is behind and takes two steps with no wait at all. The
simulation still advances at the right average rate, which is why nothing looks
wrong in a test, but it advances in bursts. On the host that is everybody's
game.

The match broadcast wants 50 milliseconds and gets 46.9 or 62.5, so every
snapshot leaves at a slightly wrong time. A client that measures how stale its
state is sees that jitter and cannot tell it from the network.

Asking for a one millisecond timer removes both, and is what media players and
games have always done on Windows. It is a global setting, so it is released on
the way out rather than left raised for whatever runs next.

Nothing here does anything on Linux or macOS, where sleeps are already accurate
to well under a millisecond.
"""

import logging
import sys

log = logging.getLogger("clock")

_raised = False


def raise_resolution() -> bool:
    """Ask for a one millisecond system timer. True if the request was granted.

    Safe to call more than once, and safe to call on any platform. A failure is
    logged and ignored: a coarse timer is a worse game, not a broken one.
    """
    global _raised

    if _raised or not sys.platform.startswith("win"):
        return False

    try:
        import ctypes

        # TIMERR_NOERROR is 0. Anything else means the period was refused.
        if ctypes.WinDLL("winmm").timeBeginPeriod(1) != 0:
            log.warning("the system refused a one millisecond timer")
            return False
    except Exception as error:  # noqa: BLE001
        log.warning("could not raise the timer resolution: %s", error)
        return False

    _raised = True
    log.info("timer resolution raised to one millisecond")
    return True


def restore_resolution() -> None:
    """Give the timer back. Paired with every successful raise."""
    global _raised

    if not _raised:
        return

    try:
        import ctypes

        ctypes.WinDLL("winmm").timeEndPeriod(1)
    except Exception as error:  # noqa: BLE001
        log.warning("could not restore the timer resolution: %s", error)

    _raised = False
