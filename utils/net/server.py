"""The host's WebSocket server.

An asyncio event loop on its own thread. The rest of the application is
synchronous and stays that way: everything crossing the boundary goes through
run_coroutine_threadsafe, and every public method here is safe to call from the
Flask thread.

Two things this module is strict about:

  Stoppability. The thread has a stop event, the loop is closed on the way out,
  and stop() joins with a timeout and reports whether the thread actually went.
  A listener that outlives the window is the failure that turns a clean exit
  into a port that is still bound on the next launch.

  Distrust of clients. Every inbound frame is size-checked and parsed
  defensively, host-only messages are refused from anyone who is not the host,
  and a connection that misbehaves is closed rather than argued with.

A spectator is a connection with no snake. It is registered separately from the
players, it is refused every message except the two it is allowed to send, and
it is routed the stream of whichever player it is watching. That last part is
the whole of why watching does not reopen the arena scoping: a spectator reads
exactly what one player reads, so the rule that says where somebody is in an
arena is private has one implementation rather than two.
"""

import asyncio
import logging
import threading
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from utils.game import match as match_module
from utils.game.engine import TickLoop
from utils.game.rules import RuleError, RuleSet
from utils.net import protocol
from utils.net import room as room_module
from utils.net.room import JoinRefused, Room, Spectator
from utils.ports import DEFAULT_WS_PORT

log = logging.getLogger("server")

# What a connection may send, and what an address may open.
#
# The input rate is the one that matters, and it is set from what the real
# client can emit rather than from what a player seems likely to press.
#
# An input goes out on a key press, and a press refused for arriving late is
# resent up to RETRY_LIMIT times inside RETRY_BUDGET_MS: three tries in 260
# milliseconds, as of this writing. A held arrow key repeating at the operating
# system rate, call it 30 a second, each press costing up to three messages, is
# therefore around 90 a second from a client doing nothing wrong at all.
#
# The first version of this was 30 a second, chosen from "a player sends about
# one per frame", and three ordinary stalls thirty seconds apart would have
# disconnected somebody. 120 leaves real headroom above what the client can
# produce while staying four orders of magnitude below the thing being
# defended against: measured before any of this existed, one connection was
# accepted at just under 23,000 inputs a second, with the host parsing and
# dispatching every one.
INPUT_RATE = 120.0
INPUT_BURST = 60.0

# Strikes rather than one. A single corrupt frame on a bad link should not end a
# match; a client producing garbage should stop costing the host anything.
#
# They decay, and that is not a detail. Without the window a strike is kept for
# the whole life of the connection, so a player whose link stalls three times
# over a twenty minute match is disconnected for it, having done nothing wrong
# on any of the three. Three strikes inside thirty seconds is a client
# misbehaving; three strikes across half an hour is a bad afternoon on the WiFi.
BAD_MESSAGE_STRIKES = 3
STRIKE_WINDOW = 30.0

# At most one flooding strike a second, and this is the difference between a
# limit that works and one that punishes the people it was meant to protect.
#
# Going over the bucket costs a strike per message would mean a single clump
# from a stalled link, which is one event and the design elsewhere says to fold
# rather than punish, arriving as forty strikes and closing the connection.
# Measured: a clump of 60 closed it immediately. Throttling the strike instead
# means a clump costs one, and only a client that keeps the bucket empty for
# three seconds running is closed.
FLOOD_STRIKE_INTERVAL = 1.0

# Per source address. A reconnect loop is the accidental denial of service the
# threat model names, and it is also what makes the room code honest: five bits
# of checksum is not a password, and this is the thing that stops it being
# brute forced at LAN speeds.
CONNECTIONS_PER_WINDOW = 5
CONNECTION_WINDOW = 10.0


class Bucket:
    """A token bucket. Refills at a rate, holds at most a burst.

    Deliberately not a hard per-message refusal. A client on a stalled link
    legitimately delivers several inputs at once, and the design elsewhere says
    to fold a clump rather than punish it, so going over costs a strike and only
    a persistent offender is closed.
    """

    def __init__(self, rate: float, burst: float):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.stamp = time.monotonic()

    def take(self, now: float = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(
            self.burst, self.tokens + (now - self.stamp) * self.rate
        )
        self.stamp = now

        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

BIND_HOST = "0.0.0.0"
# Once a second, not once every five. At five seconds the ping was the only
# traffic on an idle lobby connection, so every sample paid the cost of waking
# a sleeping WiFi radio and the reported latency was the wake-up time rather
# than the latency. A running match sends snapshots continuously and never has
# this problem, so the lobby was the one place measuring the worst case.
PING_SECONDS = 1.0
SHUTDOWN_TIMEOUT = 3.0
TEARDOWN_TIMEOUT = 2.0

# Best-effort courtesies, not guarantees. Connection handlers are asked to
# finish, and anything still running afterwards is cancelled. Waiting longer
# only delays the window closing; the listener is released either way.
WAIT_CLOSED_TIMEOUT = 0.75
CANCEL_TIMEOUT = 0.5


class HostServer:
    def __init__(self, room: Room, port: int = DEFAULT_WS_PORT):
        self.room = room
        self.port = port

        self._loop = None
        self._thread = None
        self._server = None
        self._ready = threading.Event()
        self._error = None

        # connection -> player id, and the reverse. Held only on the loop
        # thread, so no lock is needed for them.
        self._connections = {}
        self._by_player = {}

        # The same pair for spectators. A second registry rather than a flag in
        # the first, because every broadcast has to be able to answer "who is
        # holding a snake" without inspecting anything, and the two are routed
        # differently: a player is routed by where they are, a spectator by
        # where the person they are watching is.
        self._spectators = {}
        self._by_spectator = {}

        # Per connection: an input bucket and a count of messages that could not
        # be read. Keyed by the connection object, cleaned up in the handler's
        # finally alongside everything else it owns.
        self._buckets = {}

        # connection -> (count, when the last one was recorded). The stamp is
        # what makes them decay; see STRIKE_WINDOW.
        self._strikes = {}

        # connection -> when it was last struck for flooding, so one clump
        # costs one strike. See FLOOD_STRIKE_INTERVAL.
        self._flooded = {}

        # Per source address: when it last opened connections. A plain list of
        # timestamps, trimmed to the window on every attempt, because the number
        # of addresses on a LAN is small and this is simpler to read than a
        # counter that has to be expired on a timer.
        self._attempts = {}

        # The match, when there is one. Owned here rather than by the room
        # because the thing that has to run on a clock is the broadcast, and the
        # clock is this module's event loop.
        self.match = None
        self.match_loop = None
        self._match_task = None

        self.on_change = None

    # -- lifecycle --------------------------------------------------------

    def start(self, timeout: float = 5.0) -> None:
        """Start the listener. Raises OSError if it could not bind."""
        if self._thread and self._thread.is_alive():
            return

        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="ws-server", daemon=True)
        self._thread.start()

        if not self._ready.wait(timeout):
            raise OSError(f"the room listener did not start within {timeout}s")
        if self._error:
            raise self._error

        log.info("hosting on %s:%s", BIND_HOST, self.port)

    def stop(self, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
        """Close every connection and stop the thread. True if it stopped."""
        if not self._loop:
            return True

        loop = self._loop
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop)

        if self._thread:
            self._thread.join(timeout=timeout)
            stopped = not self._thread.is_alive()
            if not stopped:
                log.error("the room listener thread did not stop")
            self._thread = None
            self._loop = None
            return stopped

        return True

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        # The loop is held in a local as well as on the instance. stop() runs on
        # another thread and clears the instance attribute, and this method must
        # still be able to close the loop it created.
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._serve())
        except OSError as error:
            self._error = error
            self._ready.set()
            log.exception("could not bind port %s", self.port)
        except Exception as error:  # noqa: BLE001
            self._error = error
            self._ready.set()
            log.exception("the room listener stopped unexpectedly")
        finally:
            # Async generators hold the loop open otherwise, which is what the
            # "task was destroyed but it is pending" warnings were about.
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass

            try:
                loop.close()
            finally:
                self._loop = None
                self._ready.set()

    async def _serve(self) -> None:
        self._stopping = asyncio.Event()
        self._server = await serve(self._handle, BIND_HOST, self.port)
        self._ready.set()

        heartbeat = asyncio.create_task(self._heartbeat())

        try:
            await self._stopping.wait()
        finally:
            heartbeat.cancel()
            try:
                await asyncio.wait_for(self._teardown(), timeout=TEARDOWN_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("teardown did not finish in time; closing anyway")

    async def _teardown(self) -> None:
        """Close everything, then make sure the loop has nothing left to do.

        The explicit cancellation at the end is not tidiness. Without it a
        connection handler suspended on its inbound iterator keeps the loop
        alive, stop() times out, and the port is still bound after the window
        has closed.
        """
        self._connections.clear()
        self._by_player.clear()
        self._spectators.clear()
        self._by_spectator.clear()
        self._buckets.clear()
        self._strikes.clear()
        self._flooded.clear()
        self._attempts.clear()

        if self._match_task is not None:
            self._match_task.cancel()
            self._match_task = None

        # Server.close() closes the listener and every open connection, then
        # wait_closed() returns once each handler has finished. Closing the
        # connections individually from here instead deadlocks: the close
        # handshake cannot complete while this task is the one waiting for it
        # and the handler task is the one holding the inbound iterator.
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(
                    self._server.wait_closed(), timeout=WAIT_CLOSED_TIMEOUT
                )
            except (asyncio.TimeoutError, TimeoutError):
                log.info("connection handlers did not finish; cancelling them")
            self._server = None

        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        for task in pending:
            task.cancel()
        if pending:
            # Bounded, not gathered. A task that will not answer a cancellation
            # must not be able to hold the window open.
            await asyncio.wait(pending, timeout=CANCEL_TIMEOUT)

    def _every_connection(self) -> list:
        """Every open connection, players and spectators alike.

        Anything addressed to the room rather than to a position in it, the
        lobby, the ping, the room closing, goes to all of these. Only the match
        stream distinguishes between them.
        """
        return list(self._connections) + list(self._spectators)

    async def _shutdown(self) -> None:
        for connection in self._every_connection():
            try:
                await connection.send(protocol.encode(
                    protocol.reject(protocol.ROOM_CLOSED)
                ))
            except (ConnectionClosed, OSError):
                pass

        if getattr(self, "_stopping", None) is not None:
            self._stopping.set()

    # -- connections ------------------------------------------------------

    def _address_of(self, connection) -> str:
        """The source address, or a constant when the transport will not say.

        A constant rather than skipping the limit: an unknown address sharing
        one bucket with every other unknown address is a worse experience for
        nobody and a better one than no limit at all.
        """
        remote = getattr(connection, "remote_address", None)
        if isinstance(remote, (tuple, list)) and remote:
            return str(remote[0])
        return "unknown"

    def _too_many_attempts(self, connection) -> bool:
        """True when this address has opened too many connections lately."""
        address = self._address_of(connection)
        now = time.monotonic()

        recent = [
            stamp for stamp in self._attempts.get(address, [])
            if now - stamp < CONNECTION_WINDOW
        ]
        recent.append(now)
        self._attempts[address] = recent

        return len(recent) > CONNECTIONS_PER_WINDOW

    async def _handle(self, connection) -> None:
        member = None

        # Counted on connect, so an attempt that never says hello still counts
        # against the address. Refused after the hello rather than here, which
        # is not squeamishness: closing before the client has reached its first
        # receive means the reject is written to a socket nobody is reading, and
        # the person is told "the host closed the connection during the
        # handshake" instead of why. Measured that way before it was moved.
        #
        # The cost of waiting is one frame parsed from an over-limit connection,
        # against a refusal that names itself.
        over_limit = self._too_many_attempts(connection)

        try:
            self._buckets[connection] = Bucket(INPUT_RATE, INPUT_BURST)
            self._strikes[connection] = (0, 0.0)
            member = await self._handshake(connection, over_limit)
            if member is None:
                return

            async for raw in connection:
                await self._dispatch(connection, member, raw)

        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001
            log.exception("connection handler failed")
        finally:
            self._buckets.pop(connection, None)
            self._strikes.pop(connection, None)
            self._flooded.pop(connection, None)

            # Written as one guarded block rather than an early return: a
            # return inside a finally swallows whatever was being raised,
            # including the cancellation that closes this handler at teardown.
            if member is not None:
                if isinstance(member, Spectator):
                    self.room.leave_spectator(member.id)
                    self._spectators.pop(connection, None)
                    self._by_spectator.pop(member.id, None)
                    log.info("spectator %s left", member.username)
                else:
                    if self.match is not None:
                        self.match.remove_player(member.id)
                        # Anybody following this player is about to be moved
                        # onto another stream by the room, and a delta from a
                        # stream they have no base for means nothing to them.
                        if self.room.spectators:
                            self.match.request_keyframe()
                    self.room.leave(member.id)
                    self._connections.pop(connection, None)
                    self._by_player.pop(member.id, None)
                    log.info("player %s left", member.username)

                await self._broadcast_lobby()
                self._notify()

    async def _handshake(self, connection, over_limit: bool = False):
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=10.0)
        except (TimeoutError, asyncio.TimeoutError, ConnectionClosed):
            return None

        try:
            message = protocol.decode(raw, protocol.MAX_CLIENT_BYTES)
        except protocol.ProtocolError as error:
            await self._send(connection, protocol.reject(
                protocol.BAD_MESSAGE, str(error)
            ))
            await connection.close()
            return None

        if message.get("type") != protocol.HELLO:
            await self._send(connection, protocol.reject(
                protocol.BAD_MESSAGE, "The first message must be a hello."
            ))
            await connection.close()
            return None

        if over_limit:
            # After the hello, so this lands in the client's receive rather than
            # into a socket it has not started reading yet. Before the room is
            # touched, so a reconnect loop still costs nothing but a parse.
            await self._send(connection, protocol.reject(
                protocol.TOO_MANY_ATTEMPTS,
                window_seconds=int(CONNECTION_WINDOW),
            ))
            await connection.close()
            log.info(
                "refused a connection from %s: too many attempts",
                self._address_of(connection),
            )
            return None

        if message.get("spectate"):
            return await self._admit_spectator(connection, message)

        try:
            player = self.room.join(
                message.get("username"),
                message.get("colour"),
                message.get("protocol"),
            )
        except JoinRefused as refusal:
            await self._send(connection, protocol.reject(
                refusal.reason, **refusal.extra
            ))
            await connection.close()
            log.info("refused a join: %s", refusal.reason)
            return None

        self._connections[connection] = player.id
        self._by_player[player.id] = connection

        await self._send(connection, protocol.welcome(
            player.id, self.room.to_dict(), self.room.rules.to_dict()
        ))
        log.info("player %s joined as %s", player.username, player.id)

        # Someone arriving mid-match needs a keyframe before any delta means
        # anything to them. Rather than handing them a private one, which would
        # leave them the one client whose base does not match the stream's, the
        # next broadcast to everybody is promoted to a keyframe. It is at most
        # fifty milliseconds away.
        if self.match is not None:
            self.match.add_player(player.id, player.username, player.colour)
            self.match.request_keyframe()

        await self._broadcast_lobby()
        self._notify()
        return player

    async def _admit_spectator(self, connection, message):
        """The other half of the handshake: somebody who wants to watch.

        Its own path rather than a branch inside the player one, because almost
        nothing it does is the same. No colour, no seat, no snake in the match,
        and the keyframe it needs is asked for because it has no state at all
        rather than because it arrived mid-match.
        """
        try:
            spectator = self.room.join_spectator(
                message.get("username"), message.get("protocol")
            )
        except JoinRefused as refusal:
            await self._send(connection, protocol.reject(
                refusal.reason, **refusal.extra
            ))
            await connection.close()
            log.info("refused a spectator: %s", refusal.reason)
            return None

        self._spectators[connection] = spectator.id
        self._by_spectator[spectator.id] = connection

        await self._send(connection, protocol.welcome(
            spectator.id,
            self.room.to_dict(),
            self.room.rules.to_dict(),
            spectator=True,
            watching=spectator.watching,
        ))
        log.info("spectator %s joined as %s", spectator.username, spectator.id)

        # No snake is added to the match. The only thing a spectator needs from
        # a match already running is a base to apply deltas to, and promoting
        # the next broadcast to a keyframe is the same answer a joining player
        # gets: everybody's stream stays on one base rather than this one
        # connection being handed a private one.
        if self.match is not None:
            self.match.request_keyframe()

        await self._broadcast_lobby()
        self._notify()
        return spectator

    async def _dispatch(self, connection, member, raw) -> None:
        try:
            message = protocol.decode(raw, protocol.MAX_CLIENT_BYTES)
        except protocol.ProtocolError as error:
            await self._strike(connection, str(error))
            return

        kind = message.get("type")

        if kind not in protocol.CLIENT_TYPES:
            await self._strike(connection, "Unknown message type.")
            return

        # Counted for every message, not only inputs. An input is what a client
        # sends in volume, but a client sending a thousand readys a second costs
        # the host the same parse and the same dispatch.
        bucket = self._buckets.get(connection)
        if bucket is not None and not bucket.take():
            # Dropped either way. The strike is what escalates, and it is
            # throttled so that one clump is one strike.
            now = time.monotonic()
            if now - self._flooded.get(connection, 0.0) >= FLOOD_STRIKE_INTERVAL:
                self._flooded[connection] = now
                await self._strike(
                    connection, "Too many messages.", protocol.FLOODING
                )
            return

        # Before the host check and before every branch below. A spectator that
        # can reach start, rules or kick is worse than no spectators at all, so
        # the allowed set is consulted first and anything outside it is refused
        # rather than falling through to a branch that reads member.id as though
        # it were a player.
        spectating = isinstance(member, Spectator)

        if spectating:
            if kind not in protocol.SPECTATOR_TYPES:
                await self._send(connection, protocol.reject(
                    protocol.NOT_A_PLAYER
                ))
                return
        elif kind == protocol.SPECTATE:
            # The reverse guard. A player asking to be shown somebody else's
            # arena is asking for the one thing the scoping refuses.
            await self._send(connection, protocol.reject(
                protocol.BAD_MESSAGE, "Only a spectator can change who it watches."
            ))
            return

        if kind in protocol.HOST_ONLY_TYPES and member.id != self.room.host_id:
            await self._send(connection, protocol.reject(protocol.NOT_HOST))
            return

        if kind == protocol.SPECTATE:
            await self._watch(connection, member, message.get("player"))
            return

        player = member

        if kind == protocol.READY:
            self.room.set_ready(player.id, message.get("ready"))
            await self._broadcast_lobby()
            self._notify()
            return

        if kind == protocol.PONG:
            sent = message.get("stamp")
            if isinstance(sent, (int, float)):
                self.room.record_latency(
                    player.id, round((time.time() - sent) * 1000)
                )
            return

        if kind == protocol.RULES:
            try:
                ruleset = RuleSet.from_dict(message.get("rules") or {})
            except RuleError as error:
                await self._send(connection, protocol.reject(
                    protocol.BAD_MESSAGE, str(error)
                ))
                return
            self.room.set_rules(player.id, ruleset)
            await self._broadcast_lobby()
            self._notify()
            return

        if kind == protocol.KICK:
            try:
                self.room.kick(player.id, int(message.get("player", -1)))
            except (JoinRefused, TypeError, ValueError):
                return
            await self._broadcast_lobby()
            self._notify()
            return

        if kind == protocol.INPUT:
            # The only thing a client may say about the game. Not a position,
            # not a length, not a score: a direction it would like to face.
            self.apply_input(
                player.id,
                message.get("heading"),
                message.get("seq"),
                message.get("move"),
            )
            return

        if kind == protocol.START:
            self.start_match()
            return

        if kind == protocol.REMATCH:
            self.return_to_lobby()
            return

    async def _strike(self, connection, detail: str,
                      reason: str = protocol.BAD_MESSAGE) -> None:
        """Refuse a message, and close the connection if they keep coming.

        Previously this was a reject and nothing else, so a client could send
        garbage indefinitely at no cost while the host parsed every frame,
        built a reply and sent it. Measured before this existed: 25 malformed
        frames produced 25 rejects and a connection that was still open.
        """
        now = time.monotonic()
        count, last = self._strikes.get(connection, (0, 0.0))
        if now - last > STRIKE_WINDOW:
            count = 0
        count += 1
        self._strikes[connection] = (count, now)

        if count < BAD_MESSAGE_STRIKES:
            await self._send(connection, protocol.reject(reason, detail))
            return

        final = (
            protocol.FLOODING if reason == protocol.FLOODING
            else protocol.TOO_MANY_BAD_MESSAGES
        )
        await self._send(connection, protocol.reject(final))
        await connection.close()
        log.info(
            "closed a connection from %s: %s",
            self._address_of(connection),
            final,
        )

    async def _watch(self, connection, spectator, wanted) -> None:
        """Point a spectator at another player, and give them a base to draw.

        The keyframe is the whole of what makes this safe to do mid-match. The
        stream they are about to be moved onto has been sending deltas about an
        arena they have never seen, and a delta applied to the wrong arena is
        not a wrong picture, it is a client that drops everything and shows
        nothing until the next periodic keyframe seconds later.
        """
        try:
            wanted = None if wanted is None else int(wanted)
        except (TypeError, ValueError):
            await self._send(connection, protocol.reject(
                protocol.BAD_MESSAGE, "That is not a player id."
            ))
            return

        if not self.room.watch(spectator.id, wanted):
            await self._send(connection, protocol.reject(
                protocol.BAD_MESSAGE, "Nobody in this room has that id."
            ))
            return

        if self.match is not None:
            self.match.request_keyframe()

        await self._broadcast_lobby()
        self._notify()

    # -- sending ----------------------------------------------------------

    async def _send(self, connection, message: dict) -> None:
        try:
            await connection.send(protocol.encode(message))
        except (ConnectionClosed, OSError):
            pass

    async def _broadcast_lobby(self) -> None:
        await self.broadcast(protocol.lobby(
            self.room.to_dict(), self.room.rules.to_dict()
        ))

    async def broadcast(self, message: dict) -> None:
        payload = protocol.encode(message)
        for connection in self._every_connection():
            try:
                await connection.send(payload)
            except (ConnectionClosed, OSError):
                self._connections.pop(connection, None)
                self._spectators.pop(connection, None)

    async def broadcast_scoped(self, messages: dict, engine) -> None:
        """Send each player the message for the arena they are in.

        One message is built per arena and encoded once, not once per player, so
        a full room on a 2x2 costs four encodes rather than twelve. Which arena
        a player is in is asked at the moment of sending, so somebody who
        crossed between the message being built and it going out is sent the
        arena they are actually in.
        """
        encoded = {
            stream: protocol.encode(message)
            for stream, message in messages.items()
        }
        fallback = next(iter(encoded.values()), None)

        for connection, player_id in list(self._connections.items()):
            payload = encoded.get(engine.stream_for(player_id), fallback)
            if payload is None:
                continue
            try:
                await connection.send(payload)
            except (ConnectionClosed, OSError):
                self._connections.pop(connection, None)

        # A spectator is routed by the player it is watching, not by anything
        # about itself. That is the design: it receives one player's stream and
        # therefore sees exactly what that player sees, so the rule about what
        # is private on a grid is enforced once, here, by reusing it.
        for connection, spectator_id in list(self._spectators.items()):
            watching = self.room.spectators.get(spectator_id)
            target = watching.watching if watching is not None else None
            if target is None:
                continue
            payload = encoded.get(engine.stream_for(target), fallback)
            if payload is None:
                continue
            try:
                await connection.send(payload)
            except (ConnectionClosed, OSError):
                self._spectators.pop(connection, None)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(PING_SECONDS)
            await self.broadcast(protocol.ping(time.time()))

    # -- the match ---------------------------------------------------------

    def start_match(self) -> dict:
        """Begin a match with everyone currently in the room.

        Safe to call from the Flask thread: the engine and its tick loop are
        ordinary threads, and only the broadcast is handed to the event loop.
        """
        allowed, reason = self.room.can_start()
        if not allowed:
            return {"ok": False, "reason": protocol.CANNOT_START, "text": reason}

        with self.room.lock:
            roster = [
                (player.id, player.username, player.colour)
                for player in sorted(self.room.players.values(), key=lambda p: p.id)
            ]
            ruleset = self.room.rules

        engine = match_module.MatchEngine(ruleset, roster)
        engine.start()

        self.match = engine
        self.match_loop = TickLoop(engine)
        self.match_loop.start()

        self.room.set_phase(room_module.MATCH_COUNTDOWN)

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._begin_stream(engine), self._loop)

        self.notify_lobby()
        self._notify()

        return {"ok": True, "state": engine.keyframe()}

    def return_to_lobby(self) -> dict:
        """End whatever is running and put everyone back in the lobby."""
        self.stop_match()
        self.room.return_to_lobby()
        self.notify_lobby()
        self._notify()
        return {"ok": True}

    def stop_match(self) -> None:
        engine = self.match
        self.match = None

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cancel_stream(), self._loop)

        if self.match_loop is not None:
            self.match_loop.stop()
            self.match_loop = None

        if engine is not None:
            engine.stop()

    def apply_input(self, player_id, heading, seq=None, move=None) -> dict:
        engine = self.match
        if engine is None:
            return {"ok": False, "reason": "no_match"}
        return engine.set_heading(player_id, heading, seq, move)

    def match_state(self):
        engine = self.match
        if engine is None:
            return None
        # Scoped to the host's own arena, like anybody else's. Hosting is not
        # a vantage point.
        return engine.state(self.room.host_id)

    async def _begin_stream(self, engine) -> None:
        await self._cancel_stream()
        self._match_task = asyncio.create_task(self._stream(engine))

    async def _cancel_stream(self) -> None:
        task = self._match_task
        self._match_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _stream(self, engine) -> None:
        """Send the match to everyone, twenty times a second.

        The cadence is fixed rather than tied to the tick, and every message is
        built at the moment it is sent. A message queued behind a stall is
        therefore never a stale one waiting its turn: it does not exist until
        the socket is ready for it.
        """
        room_phase = {
            match_module.PHASE_COUNTDOWN: room_module.MATCH_COUNTDOWN,
            match_module.PHASE_RUNNING: room_module.MATCH_RUNNING,
            match_module.PHASE_OVER: room_module.MATCH_OVER,
        }

        # Deadlines rather than a fixed sleep. Sleeping for the interval means
        # every message leaves late by however long the last one took to build
        # and send, and that error accumulates. Sleeping until the next deadline
        # keeps the cadence even, and on a coarse system timer it is the
        # difference between a rate that is right on average and one that is
        # right each time.
        interval = match_module.SNAPSHOT_SECONDS
        deadline = time.monotonic()
        last_sent = None

        try:
            while self.match is engine:
                deadline += interval
                now = time.monotonic()

                if now < deadline:
                    await asyncio.sleep(deadline - now)
                elif now - deadline > interval * 4:
                    # A long stall, not drift. Catching up would send a burst.
                    deadline = now

                if self.match is not engine:
                    return

                sent_at = time.monotonic()
                if last_sent is not None:
                    engine.record_send_gap((sent_at - last_sent) * 1000.0)
                last_sent = sent_at

                messages = engine.next_messages()
                await self.broadcast_scoped(messages, engine)

                message = next(iter(messages.values()))
                phase = room_phase.get(message.get("phase"))
                if phase is not None and phase != self.room.phase:
                    self.room.set_phase(phase)
                    await self._broadcast_lobby()
                    self._notify()

                if message.get("phase") == match_module.PHASE_OVER:
                    # One last keyframe, so nobody is looking at a results
                    # screen built from a delta they may have discarded. Still
                    # scoped: the match being over does not open the other
                    # arenas up, and the results screen reads the leaderboard,
                    # which every stream carries in full.
                    await self.broadcast_scoped({
                        stream: engine.keyframe(stream)
                        for stream in messages
                    }, engine)
                    await self._stop_ticking()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            # Without this the exception is held on the task and surfaces, if at
            # all, as a line at garbage collection time. Meanwhile the match
            # thread keeps stepping and nobody is sent anything, which looks
            # from every client exactly like the host going quiet.
            log.exception("the match stream failed, ending the match")
            engine.fail(error)

            # One last keyframe on every stream, the same shape the ordinary
            # end of a match sends, so the results screen is built from a
            # complete state rather than from a delta somebody may have
            # discarded. Guarded in turn: if the thing that broke was the
            # message building itself, this fails too, and a failure to report
            # a failure must not replace the traceback that explains it.
            try:
                await self.broadcast_scoped({
                    stream: engine.keyframe(stream)
                    for stream in engine.streams()
                }, engine)
            except Exception:  # noqa: BLE001
                log.exception("could not tell anybody the match had failed")

            await self._stop_ticking()

    async def _stop_ticking(self) -> None:
        """Stop the tick thread without blocking the event loop to do it.

        TickLoop.stop joins with a timeout. Calling it directly from here would
        hold the loop, and the loop is what everyone else's connection is being
        served from.
        """
        loop = self.match_loop
        self.match_loop = None
        if loop is None:
            return
        await asyncio.get_running_loop().run_in_executor(None, loop.stop)

    # -- calls from the Flask thread ---------------------------------------

    def notify_lobby(self) -> None:
        """Broadcast the lobby after a change made outside the loop thread."""
        if not self._loop or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._broadcast_lobby(), self._loop)

    def disconnect(self, player_id: int, reason: str) -> None:
        if not self._loop or not self._loop.is_running():
            return

        async def close():
            connection = self._by_player.get(player_id) or \
                self._by_spectator.get(player_id)
            if connection is None:
                return
            await self._send(connection, protocol.reject(reason))
            await connection.close()

        asyncio.run_coroutine_threadsafe(close(), self._loop)

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.exception("a room change callback failed")
