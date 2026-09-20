"""The outbound client: joining someone else's room.

Also an asyncio loop on its own thread, with the same rule as the server about
crossing the boundary. The Flask thread calls join(), reads state(), and calls
leave(); nothing else touches the loop.

join() is synchronous from the caller's point of view and returns either the
welcome or the refusal. Making the caller poll to find out whether a join
succeeded is how "joining a full room" turns into a timeout instead of an
answer.
"""

import asyncio
import logging
import threading

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

from utils.game.match import MatchView
from utils.net import protocol
from utils.ports import DEFAULT_WS_PORT

log = logging.getLogger("net.client")

CONNECT_TIMEOUT = 6.0
JOIN_TIMEOUT = 8.0


def parse_address(address: str, default_port: int = DEFAULT_WS_PORT) -> tuple:
    """Split "host" or "host:port" into (host, port). Raises ValueError."""
    if not isinstance(address, str) or not address.strip():
        raise ValueError("enter the host's address")

    text = address.strip()
    for prefix in ("ws://", "http://", "https://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("/")

    if ":" in text:
        host, _, port_text = text.rpartition(":")
        try:
            port = int(port_text)
        except ValueError as error:
            raise ValueError("the port must be a number") from error
    else:
        host, port = text, default_port

    if not host:
        raise ValueError("enter the host's address")
    if not 1 <= port <= 65535:
        raise ValueError("the port must be between 1 and 65535")

    return host, port


class RoomClient:
    def __init__(self):
        self._loop = None
        self._thread = None
        self._connection = None
        self._stop = threading.Event()

        self._lock = threading.Lock()
        self._room = None
        self._rules = None
        self._player_id = None
        self._closed_reason = None
        self._address = None

        # Watching rather than playing, and who. Both are answered by the host:
        # the flag arrives in the welcome, and the target arrives there and then
        # again in every lobby update, so a spectator reads who it is watching
        # from the room rather than from what it last asked for.
        self._spectator = False
        self._watching = None

        # The match as this client understands it. Keyframes and deltas are
        # folded in as they arrive; nothing is queued for later and nothing is
        # replayed. See MatchView for why that is the whole point.
        self._match = MatchView()

        self.on_change = None

    # -- state ------------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "address": self._address,
                "player_id": self._player_id,
                "room": self._room,
                "rules": self._rules,
                "closed_reason": self._closed_reason,
                "spectator": self._spectator,
                "watching": self._watching,
            }

    @property
    def connected(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._connection)

    # -- lifecycle --------------------------------------------------------

    def join(self, address: str, username: str, colour: str,
             spectate: bool = False) -> dict:
        """Connect and complete the handshake.

        Returns {"ok": True, ...} on success, or {"ok": False, "reason": ...,
        "text": ...} with the host's refusal or a local failure.

        spectate asks for a connection with no snake. It is one flag rather than
        a second method because everything from the connect to the refusal to
        the receive loop is identical; what differs is what the host does with
        it, and what this instance is then allowed to send.
        """
        self.leave()

        host, port = parse_address(address)
        self._stop.clear()
        self._closed_reason = None
        self._address = f"{host}:{port}"
        self._spectator = bool(spectate)
        self._watching = None

        outcome = {}
        settled = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(
                    self._session(
                        host, port, username, colour, spectate, outcome, settled
                    )
                )
            except Exception as error:  # noqa: BLE001
                outcome.setdefault("ok", False)
                outcome.setdefault("reason", "connect_failed")
                outcome.setdefault("text", str(error))
                log.exception("the client session failed")
            finally:
                settled.set()
                try:
                    self._loop.close()
                finally:
                    self._loop = None
                    self._connection = None

        self._thread = threading.Thread(target=run, name="ws-client", daemon=True)
        self._thread.start()

        if not settled.wait(JOIN_TIMEOUT):
            self.leave()
            return {
                "ok": False,
                "reason": "timeout",
                "text": "The host did not answer. Check the address and that "
                        "the room is open.",
            }

        return outcome or {
            "ok": False,
            "reason": "connect_failed",
            "text": "Could not reach that address.",
        }

    def leave(self, timeout: float = 3.0) -> bool:
        self._stop.set()

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close(), self._loop)

        if self._thread:
            self._thread.join(timeout=timeout)

            if self._thread.is_alive() and self._loop:
                # Last resort: stop the loop from outside so the thread ends and
                # its socket goes with it.
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except RuntimeError:
                    pass
                self._thread.join(timeout=1.0)

            stopped = not self._thread.is_alive()
            self._thread = None
            if not stopped:
                log.error("the client thread did not stop")
            return stopped

        return True

    async def _close(self) -> None:
        await self._terminate(self._connection)

    @staticmethod
    async def _terminate(connection) -> None:
        """Close politely, then make sure the socket is really gone.

        The abort at the end is the important part. Closing the event loop does
        not close the transports running on it, so a client whose loop shut down
        first could leave the TCP connection open, and the host would keep the
        seat occupied by a player who had already left. Once the close handshake
        has completed the abort is a no-op; when it has not, it is what frees
        the seat.
        """
        if connection is None:
            return

        try:
            await asyncio.wait_for(connection.close(), timeout=1.0)
        except (ConnectionClosed, OSError, asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            transport = getattr(connection, "transport", None)
            if transport is not None:
                try:
                    transport.abort()
                except Exception:  # noqa: BLE001
                    pass

    # -- the session ------------------------------------------------------

    async def _session(self, host, port, username, colour, spectate,
                       outcome, settled) -> None:
        uri = f"ws://{host}:{port}"

        try:
            connection = await asyncio.wait_for(
                connect(uri, open_timeout=CONNECT_TIMEOUT), timeout=CONNECT_TIMEOUT
            )
        except (OSError, InvalidURI, WebSocketException, asyncio.TimeoutError,
                TimeoutError) as error:
            outcome.update({
                "ok": False,
                "reason": "connect_failed",
                "text": f"Could not reach {uri}. Check the address, and that the "
                        "host has opened a room.",
                "detail": str(error),
            })
            return

        self._connection = connection

        try:
            await connection.send(protocol.encode(
                protocol.hello(username, colour, spectate)
            ))
            raw = await asyncio.wait_for(connection.recv(), timeout=CONNECT_TIMEOUT)
            message = protocol.decode(raw)
        except (ConnectionClosed, protocol.ProtocolError, asyncio.TimeoutError,
                TimeoutError) as error:
            outcome.update({
                "ok": False,
                "reason": "handshake_failed",
                "text": "The host closed the connection during the handshake.",
                "detail": str(error),
            })
            await self._close()
            return

        if message.get("type") == protocol.REJECT:
            # Everything the host attached travels with the refusal. It knows
            # what the client needs to act on it: free colours to pick from, the
            # cap it was refused against, both protocol numbers.
            refusal = {key: value for key, value in message.items() if key != "type"}
            refusal.setdefault("reason", "rejected")
            refusal.setdefault("text", "The host refused the join.")
            refusal["ok"] = False
            outcome.update(refusal)
            await self._close()
            return

        if message.get("type") != protocol.WELCOME:
            outcome.update({
                "ok": False,
                "reason": "handshake_failed",
                "text": "The host sent something unexpected during the handshake.",
            })
            await self._close()
            return

        with self._lock:
            self._player_id = message.get("player")
            self._room = message.get("room")
            self._rules = message.get("rules")
            # Taken from the host's answer rather than from what was asked for.
            # A host that admitted this connection as a player after it asked to
            # watch would otherwise leave the client believing it has no snake.
            self._spectator = bool(message.get("spectator"))
            self._watching = message.get("watching")

        outcome.update({
            "ok": True,
            "player_id": self._player_id,
            "room": self._room,
            "rules": self._rules,
            "spectator": self._spectator,
            "watching": self._watching,
        })
        settled.set()
        self._notify()

        await self._listen(connection)

    async def _listen(self, connection) -> None:
        try:
            async for raw in connection:
                if self._stop.is_set():
                    break

                try:
                    message = protocol.decode(raw)
                except protocol.ProtocolError:
                    continue

                kind = message.get("type")

                if kind == protocol.LOBBY:
                    self.apply_lobby(message)
                elif kind in (protocol.KEYFRAME, protocol.SNAPSHOT):
                    self._match.apply(message)
                elif kind == protocol.PING:
                    await connection.send(
                        protocol.encode(protocol.pong(message.get("stamp")))
                    )
                elif kind == protocol.REJECT:
                    reason = message.get("reason")
                    if reason not in protocol.CLOSING_REASONS:
                        # A refusal of one request, not of this connection.
                        # Breaking here used to drop the client out of a room
                        # it was still welcome in.
                        log.info("the host refused a request: %s", reason)
                        continue
                    with self._lock:
                        self._closed_reason = {
                            "reason": reason,
                            "text": message.get("text"),
                        }
                    self._notify()
                    break

        except ConnectionClosed:
            with self._lock:
                if self._closed_reason is None:
                    self._closed_reason = {
                        "reason": "disconnected",
                        "text": "The connection to the host was lost.",
                    }
            self._notify()
        finally:
            await self._terminate(connection)
            self._connection = None

    def apply_lobby(self, message: dict) -> None:
        """Fold a lobby update into what this client knows about the room.

        A method rather than a branch inside the receive loop so that it can be
        exercised without a socket. The rules arriving here at all is the fix
        for a host changing them while somebody is sitting in the room.
        """
        room = message.get("room")
        rules = message.get("rules")

        with self._lock:
            self._room = room
            # Only when the message carries them, so a host running an older
            # build that sends a lobby without rules leaves the ones from the
            # welcome in place rather than replacing them with nothing.
            if isinstance(rules, dict):
                self._rules = rules

            # Who this spectator is watching is the host's answer, and it can
            # change without this instance asking: the player being watched may
            # have left, in which case the host moved it. Reading it back from
            # the room means there is one answer rather than a local guess that
            # can disagree with the stream actually arriving.
            moved = False
            if self._spectator and isinstance(room, dict):
                for entry in room.get("spectators") or []:
                    if entry.get("id") == self._player_id:
                        moved = entry.get("watching") != self._watching
                        self._watching = entry.get("watching")
                        break

        # The one place a spectator's folded state is dropped, for both ways it
        # can be moved: asking to follow somebody else, and the host moving it
        # because the player it was following left. What is held is a fold of
        # one arena and what comes next is a keyframe of another, and blending
        # the two would draw a frame made of two arenas.
        #
        # Safe to do on the lobby rather than on the request because the host
        # sends the lobby before it sends the keyframe, and a socket delivers
        # them in that order.
        if moved:
            self._match.clear()

        # A room back in the lobby has no match to draw. Holding the last one
        # would show a finished match behind the next countdown.
        if isinstance(room, dict) and room.get("phase") == "waiting":
            self._match.clear()

        self._notify()

    # -- the match ---------------------------------------------------------

    def match_state(self):
        # Their own snake is exempt from the render delay, which needs to know
        # which one is theirs.
        return self._match.state(self._player_id)

    def send_input(self, heading, seq, move) -> bool:
        if self._spectator:
            return False
        return self.send(protocol.input_intent(seq, heading, move))

    def watch(self, player_id) -> bool:
        """Ask to be moved onto another player's stream.

        Nothing is dropped here. The host answers with a lobby carrying the new
        target, and apply_lobby drops the folded state on seeing it change. That
        is one rule in one place, and it covers the case this method cannot: the
        host moving a spectator because the player it was following left.
        """
        if not self._spectator:
            return False
        return self.send(protocol.spectate(player_id))

    # -- outbound ---------------------------------------------------------

    def send(self, message: dict) -> bool:
        if not self._loop or not self._loop.is_running() or self._connection is None:
            return False

        async def deliver():
            if self._connection is not None:
                try:
                    await self._connection.send(protocol.encode(message))
                except (ConnectionClosed, OSError):
                    pass

        asyncio.run_coroutine_threadsafe(deliver(), self._loop)
        return True

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.exception("a client change callback failed")
