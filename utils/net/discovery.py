"""LAN discovery: the beacon a host broadcasts, and the listener that hears it.

UDP, because one host has to reach every machine on the network without knowing
any of their addresses, and broadcast is the only thing that does that. TCP is
one to one, so finding rooms over TCP would mean connecting to every address on
the subnet in turn, which is slow and indistinguishable from a port scan.

A lost datagram costs one second, because the beacon repeats. Nothing here
retries, acknowledges or orders anything, and nothing needs to.

Four things this module is strict about:

  Distrust of the network. Port 45882 belongs to nobody. Anything on the
  network can send to it, so every datagram is parsed defensively and every
  field is checked for type and range before it reaches the interface.

  The address comes from the sender, never from the payload. A host that works
  out its own address wrongly is still reachable, and no datagram can advertise
  a room on somebody else's machine.

  Stoppability. Both threads have a stop event and a socket timeout, so they
  wake regularly to check it. A blocking recvfrom with no timeout is a thread
  that cannot be stopped and a port that stays bound after the window closes.

  Silence for private rooms. A private room does not send a quieter beacon, it
  sends nothing, and opens no socket to send it from. The describe callback
  returning None is the whole mechanism.
"""

import json
import logging
import secrets
import socket
import threading
import time

from utils.net import protocol
from utils.net.room import LOBBY_WAITING, MATCH_RUNNING
from utils.ports import DEFAULT_DISCOVERY_PORT, local_addresses

log = logging.getLogger("net.discovery")

# Two jobs in one string. It marks a datagram as belonging to this game, so the
# listener can discard the rest of what arrives on a shared port. The trailing
# digit is the beacon format version: an incompatible change to the payload
# becomes LANSNK2, and older builds ignore it rather than misreading it.
#
# It deliberately does not contain the game's name. A renamed build has to keep
# finding rooms hosted by installed older builds, which it could not do if the
# magic changed with the name. utils/branding.py names this as one of the three
# identifiers held back from a rename.
MAGIC = "LANSNK1"

# Kept short so it is said across a room rather than read out.
MAX_ROOM_CODE = 12

BROADCAST = "255.255.255.255"

BEACON_INTERVAL = 1.0

# How long a room stays in the list after its last beacon. Five intervals, so a
# room does not flicker out of the list because four datagrams in a row were
# lost, which on a busy network is not unusual.
ROOM_TIMEOUT = 5.0

# Bounds how late a stop can be: the listener thread is inside recvfrom for at
# most this long before it checks the stop event again.
RECV_TIMEOUT = 0.4

# Ask for more than any beacon will ever be. Unlike TCP, a UDP read that is
# smaller than the datagram discards the remainder rather than returning it
# next time.
MAX_DATAGRAM = 2048

MAX_ROOM_NAME = 24
MAX_PLAYERS = 64

PHASES = (LOBBY_WAITING, MATCH_RUNNING)

# Per process, not per room. It travels in the beacon so a listener can drop
# the host's own datagrams, which come back to it because broadcast reaches the
# sending machine as well. Being per process rather than per machine is what
# lets two copies of the game on one computer still find each other, which is
# how this gets tested without a second machine.
INSTANCE_ID = secrets.token_hex(6)


# -- payload ----------------------------------------------------------------


def build_payload(port, name, host_username, players, cap, phase, joinable,
                  origin=None, code=None, spectators=False) -> dict:
    return {
        "magic": MAGIC,
        "protocol": protocol.PROTOCOL_VERSION,
        "origin": origin or INSTANCE_ID,
        "port": int(port),
        "name": str(name)[:MAX_ROOM_NAME],
        "host": str(host_username)[: protocol.MAX_USERNAME],
        "players": int(players),
        "cap": int(cap),
        "phase": str(phase),
        "joinable": bool(joinable),
        # The host's chosen code, if it set one. Unlike the generated code this
        # carries no address, so it is only resolvable by finding the room that
        # is broadcasting it. That is the whole of why a chosen code cannot
        # reach a private room: a private room broadcasts nothing.
        "code": str(code or "")[:MAX_ROOM_CODE].upper(),
        # Whether this room would take a watcher. Carried so the room list can
        # offer Watch on a room that is full or already playing, which is
        # exactly the room somebody is most likely to want to watch.
        "spectators": bool(spectators),
    }


def encode(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _bounded_int(value, low, high, default=None):
    # bool is a subclass of int, so True would otherwise pass as 1 and a
    # payload of {"port": true} would be accepted as port 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < low or value > high:
        return default
    return value


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Collapsed rather than merely truncated: a name made of newlines or tabs
    # would otherwise reach the interface and break the row it is drawn in.
    return " ".join(value.split())[:limit]


def parse(datagram: bytes, sender_address: str):
    """Turn a received datagram into a room entry, or None if it is not ours.

    None rather than an exception for everything unrecognised. On a port that
    belongs to nobody, junk is the ordinary case and not a failure worth
    raising, logging, or counting.
    """
    if not isinstance(datagram, bytes) or len(datagram) > MAX_DATAGRAM:
        return None

    try:
        message = json.loads(datagram.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None

    if not isinstance(message, dict) or message.get("magic") != MAGIC:
        return None

    port = _bounded_int(message.get("port"), 1, 65535)
    if port is None:
        return None

    version = _bounded_int(message.get("protocol"), 0, 9999, default=-1)
    cap = _bounded_int(message.get("cap"), 1, MAX_PLAYERS, default=0)
    players = _bounded_int(message.get("players"), 0, MAX_PLAYERS, default=0)

    phase = message.get("phase")
    if phase not in PHASES:
        phase = LOBBY_WAITING

    origin = _text(message.get("origin"), 32)
    name = _text(message.get("name"), MAX_ROOM_NAME) or "Room"

    return {
        "address": sender_address,
        "port": port,
        # The string a join is made with. Assembling it here means nothing
        # downstream has to know how an endpoint is spelled.
        "target": f"{sender_address}:{port}",
        "origin": origin,
        "name": name,
        "host": _text(message.get("host"), protocol.MAX_USERNAME),
        "code": _text(message.get("code"), MAX_ROOM_CODE).upper(),
        "players": players,
        "cap": cap,
        "phase": phase,
        "joinable": bool(message.get("joinable")),
        "spectators": bool(message.get("spectators")),
        "protocol": version,
        # Worked out here rather than in the browser, so a room running a
        # different release is shown as unjoinable instead of being offered and
        # then refused after a connection.
        "compatible": version == protocol.PROTOCOL_VERSION,
    }


# -- the table --------------------------------------------------------------


class RoomTable:
    """The rooms heard from recently, keyed by endpoint.

    Keyed by address and port rather than by anything in the payload, so a host
    that restarts replaces its own entry at once instead of appearing twice
    until the first copy expires.

    No sockets and no threads of its own, and every method takes the current
    time, so expiry is testable without waiting in real time.
    """

    def __init__(self, timeout: float = ROOM_TIMEOUT):
        self.timeout = timeout
        self._entries = {}
        self._lock = threading.Lock()

    def record(self, entry: dict, now=None) -> None:
        stamped = dict(entry)
        stamped["heard_at"] = time.monotonic() if now is None else now
        with self._lock:
            self._entries[stamped["target"]] = stamped

    def listing(self, now=None) -> list:
        moment = time.monotonic() if now is None else now

        with self._lock:
            stale = [
                key
                for key, entry in self._entries.items()
                if moment - entry["heard_at"] > self.timeout
            ]
            for key in stale:
                del self._entries[key]

            rooms = []
            for entry in self._entries.values():
                item = dict(entry)
                item["age_ms"] = int((moment - item.pop("heard_at")) * 1000)
                rooms.append(item)

        # Sorted by name so the list does not reshuffle itself every two
        # seconds as datagrams arrive in a different order.
        rooms.sort(key=lambda item: (item["name"].lower(), item["target"]))
        return rooms

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# -- the beacon -------------------------------------------------------------


class Beacon:
    """Broadcasts one datagram a second describing the room being hosted.

    describe is called on every tick rather than once at the start, so the
    player count in somebody else's list is the live one rather than the count
    at the moment hosting began. Returning None from it sends nothing at all,
    which is how a private room stays silent.
    """

    def __init__(self, describe, port: int = DEFAULT_DISCOVERY_PORT,
                 address: str = BROADCAST, interval: float = BEACON_INTERVAL,
                 interfaces=None):
        self.describe = describe
        self.port = port
        self.address = address
        self.interval = interval

        # None means "ask the machine each tick". A fixed list is for tests,
        # which target loopback rather than the broadcast address.
        self.interfaces = interfaces

        self._stop = threading.Event()
        self._thread = None
        self._sockets = []
        self._bound_to = None

    def start(self) -> None:
        if self.running:
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="discovery-beacon", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()

        if self._thread is None:
            return True

        self._thread.join(timeout=timeout)
        stopped = not self._thread.is_alive()
        if not stopped:
            log.error("the discovery beacon thread did not stop")
        self._thread = None
        return stopped

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._send_once()
                # wait rather than sleep, so stopping takes effect at once
                # instead of up to a whole interval later.
                self._stop.wait(self.interval)
        finally:
            self._close_sockets()

    def _send_once(self) -> None:
        try:
            payload = self.describe()
        except Exception:  # noqa: BLE001
            # A broken describe must not kill the thread: the room itself is
            # still working, and the failure belongs in the log rather than in
            # a listener that stops for the rest of the session.
            log.exception("the beacon could not describe the room")
            return

        if not payload:
            # Nothing to announce. No socket is opened, so a private room is
            # silent rather than quiet.
            return

        datagram = encode(payload)
        for sock in self._sockets_for_now():
            try:
                sock.sendto(datagram, (self.address, self.port))
            except OSError as error:
                # Normal on a machine whose network came and went. The next
                # tick rebuilds the sockets.
                log.debug("beacon send failed: %s", error)

    def _sockets_for_now(self) -> list:
        """One socket per local address, rebuilt when the addresses change.

        A laptop with WiFi, ethernet and a VPN adapter has three addresses, and
        an unbound socket broadcasts from whichever one the routing table
        prefers, which is often not the one the other players are on. Binding a
        socket to each address makes the kernel use that interface, and needs
        no knowledge of netmasks to do it.
        """
        addresses = (
            self.interfaces if self.interfaces is not None else local_addresses()
        )

        if addresses == self._bound_to and self._sockets:
            return self._sockets

        self._close_sockets()
        self._bound_to = addresses

        # None here means one unbound socket, which is the right behaviour on a
        # machine with no LAN address at all: nothing will hear it, but nothing
        # raises either.
        for address in addresses or [None]:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # Without this the kernel refuses a send to a broadcast
                # address outright, so that no program broadcasts by accident.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if address:
                    sock.bind((address, 0))
                self._sockets.append(sock)
            except OSError as error:
                log.debug("no beacon socket for %s: %s", address, error)

        return self._sockets

    def _close_sockets(self) -> None:
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._sockets = []
        self._bound_to = None


# -- the listener -----------------------------------------------------------


class Listener:
    """Collects beacons from the network into a table of rooms.

    Bound to every interface, which is not a default worth changing. A socket
    bound to this machine's own LAN address does not receive datagrams sent to
    the broadcast address on most systems, and that is the usual reason a
    listener that looks correct hears nothing at all.
    """

    def __init__(self, port: int = DEFAULT_DISCOVERY_PORT,
                 timeout: float = ROOM_TIMEOUT, ignore_origin: str = None):
        self.port = port
        self.table = RoomTable(timeout)
        self.ignore_origin = INSTANCE_ID if ignore_origin is None else ignore_origin

        self._stop = threading.Event()
        self._thread = None
        self._ready = threading.Event()
        self._error = None

    def start(self, timeout: float = 2.0) -> None:
        """Bind and begin listening. Raises OSError if the port cannot be bound."""
        if self.running:
            return

        self._stop.clear()
        self._ready.clear()
        self._error = None

        self._thread = threading.Thread(
            target=self._run, name="discovery-listener", daemon=True
        )
        self._thread.start()

        # Binding happens on the thread, so the caller has to be told whether
        # it worked. Reporting a failed bind as a failed start is the
        # difference between an empty room list and an empty room list with a
        # reason attached.
        if not self._ready.wait(timeout):
            raise OSError(f"the discovery listener did not start within {timeout}s")
        if self._error:
            raise self._error

    def stop(self, timeout: float = 2.0) -> bool:
        self._stop.set()

        if self._thread is None:
            return True

        # The socket is closed by the thread that owns it rather than from
        # here. Closing it under a recvfrom running on another thread is a race
        # against file descriptor reuse, and the receive timeout already bounds
        # how long this wait can be.
        self._thread.join(timeout=timeout)
        stopped = not self._thread.is_alive()
        if not stopped:
            log.error("the discovery listener thread did not stop")
        self._thread = None
        return stopped

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def rooms(self) -> list:
        return self.table.listing()

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Two copies of the game on one machine is the ordinary way to test
            # this, and both want the same port. SO_REUSEPORT is what actually
            # lets both of them receive on Linux and macOS. It does not exist
            # on Windows, where SO_REUSEADDR alone is enough.
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass

            sock.settimeout(RECV_TIMEOUT)
            sock.bind(("", self.port))
        except OSError as error:
            self._error = error
            self._ready.set()
            log.warning("could not bind the discovery port %s: %s", self.port, error)
            return

        self._ready.set()
        log.info("listening for rooms on udp %s", self.port)

        try:
            while not self._stop.is_set():
                try:
                    datagram, sender = sock.recvfrom(MAX_DATAGRAM)
                except TimeoutError:
                    # The receive timeout expiring is the ordinary case, not a
                    # failure: it is what lets this loop check the stop event.
                    continue
                except OSError:
                    break

                entry = parse(datagram, sender[0])
                if entry is None:
                    continue

                # Our own beacon arrives here too: a broadcast reaches the
                # machine that sent it. The origin is per process, so a second
                # copy of the game on this machine is still listed.
                if entry["origin"] and entry["origin"] == self.ignore_origin:
                    continue

                self.table.record(entry)
        finally:
            sock.close()
            log.info("stopped listening for rooms")
