"""Port probing.

The default ports:

    45880  HTTP  (the local UI server, bound to loopback only)
    45881  WS    (the game channel, bound to 0.0.0.0 while hosting)
    45882  UDP   (the discovery beacon)

A second copy of the game on the same machine is a normal thing to do while
testing, so the UI port is probed rather than assumed. The WS and UDP ports are
fixed by the protocol and are not probed here; a second host on one machine is
a concern for whenever that is supported.
"""

import socket

DEFAULT_HTTP_PORT = 45880
DEFAULT_WS_PORT = 45881
DEFAULT_DISCOVERY_PORT = 45882

LOOPBACK = "127.0.0.1"


def is_free(port: int, host: str = LOOPBACK) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def find_free(start: int = DEFAULT_HTTP_PORT, attempts: int = 20) -> int:
    """Return the first free port at or after start.

    Raises OSError if every candidate is taken, which is a real failure worth
    surfacing rather than falling back to an ephemeral port the user cannot
    predict.
    """
    for offset in range(attempts):
        candidate = start + offset
        if is_free(candidate):
            return candidate
    raise OSError(
        f"no free TCP port found in range {start}-{start + attempts - 1}"
    )


def local_addresses() -> list:
    """The addresses another machine on the LAN could use to reach this one.

    Loopback is excluded: it is the one address that is guaranteed not to work
    for anyone else, and offering it as a join target invites the confusion of a
    room nobody can find.
    """
    import socket as socket_module

    found = []

    # The usual trick: opening a UDP socket toward an off-link address makes the
    # kernel pick the interface it would actually route through, without
    # sending anything.
    probe = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for info in socket_module.getaddrinfo(
            socket_module.gethostname(), None, socket_module.AF_INET
        ):
            address = info[4][0]
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass

    return found
