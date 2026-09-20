"""Flask application: the single-page shell and the local API.

This server is the UI transport only. It is bound to
loopback by main.py and is never the thing other players connect to; that is
the room's WebSocket listener, which is a different port and a different module.

A note on the single player transport. The engine runs here, in Python, because
single player and multiplayer must run the same engine and there must not be a
second game loop. The browser polls /api/solo/state at 20 Hz over loopback and
posts headings to /api/solo/input. The payloads are already shaped like the
messages the networked game sends, so moving single player onto the WebSocket is
a change of transport and nothing else.
"""

import logging
import os
import threading

from flask import Flask, jsonify, request, send_from_directory

from utils import branding, ports
from utils.game import rules as rules_module
from utils.game.engine import PHASE_OVER, SoloEngine, TickLoop
from utils.net import discovery, joincode, protocol
from utils.net.client import RoomClient
from utils.net.room import MATCH_RUNNING, JoinRefused, Room
from utils.net.server import HostServer
from utils.store import presets as preset_store
from utils.store import profile as profile_store
from utils.store import solo as solo_store

APP_VERSION = "0.1.0"

# Taken from the protocol module rather than written again. A second copy of a
# version number is a copy that eventually disagrees with the first.
PROTOCOL_VERSION = protocol.PROTOCOL_VERSION

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

log = logging.getLogger("app")


class SoloSession:
    """Owns the single running solo match.

    One at a time, by design: a second concurrent solo match has no meaning and
    allowing one would leave an orphaned tick thread behind.
    """

    def __init__(self):
        self.engine = None
        self.loop = None
        self.recorded = False
        self.result = None
        self.outcome = None
        self.lock = threading.Lock()

    def start(self, ruleset, colour: str = None) -> dict:
        with self.lock:
            self._stop_locked()

            self.engine = SoloEngine(ruleset, colour=colour)
            self.engine.start()
            self.loop = TickLoop(self.engine)
            self.loop.start()
            self.recorded = False
            self.result = None
            self.outcome = None

            solo_store.remember_rules(ruleset.to_dict())
            log.info(
                "solo match started, arena %sx%s, edge %s, speed %s",
                ruleset.arena_width,
                ruleset.arena_height,
                ruleset.edge_behaviour,
                ruleset.base_speed,
            )

            return self.engine.keyframe()

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.loop:
            self.loop.stop()
            self.loop = None
        if self.engine:
            self.engine.stop()
        self.engine = None

    def state(self) -> dict:
        if self.engine is None:
            return {"type": "snapshot", "phase": "idle", "snakes": [], "food": []}

        snapshot = self.engine.snapshot()

        # The result is recorded by the server from the engine's own state, so
        # a score cannot be reported by the browser. Recording happens once;
        # every snapshot of the finished match then carries the same result, so
        # it does not matter which of several in-flight polls the frontend sees
        # last.
        if snapshot["phase"] == PHASE_OVER:
            with self.lock:
                if not self.recorded and self.engine is not None:
                    self.recorded = True
                    self.result = self.engine.result()
                    self.outcome = solo_store.record_result(
                        self.result, self.engine.rules.to_dict()
                    )
                    if self.loop:
                        self.loop.stop()
                        self.loop = None

                if self.result:
                    snapshot["result"] = self.result
                    snapshot["best"] = self.outcome["best"]
                    snapshot["is_new_best"] = self.outcome["is_new_best"]

        if self.engine is not None and "best" not in snapshot:
            snapshot["best"] = solo_store.best_for(self.engine.rules.fingerprint())

        return snapshot


class NetSession:
    """Owns whichever side of a room this instance is on.

    Exactly one of hosting or joined at a time. Trying to be both is the sort of
    state that produces two lobbies and one confused player, so start() and
    join() each close the other first.
    """

    IDLE = "idle"
    HOSTING = "hosting"
    JOINED = "joined"

    def __init__(self):
        self.mode = self.IDLE
        self.room = None
        self.server = None
        self.client = None
        self.beacon = None
        self.listener = None
        self.lock = threading.Lock()

    # -- hosting ----------------------------------------------------------

    def host(self, ruleset, username: str, colour: str) -> dict:
        with self.lock:
            self._close_locked()

            self.room = Room(ruleset, username, colour)
            port = ports.DEFAULT_WS_PORT
            if not ports.is_free(port, "0.0.0.0"):
                port = ports.find_free(port + 2, attempts=10)

            self.server = HostServer(self.room, port=port)
            self.server.start()
            self.mode = self.HOSTING

            # Started for every room, public or not. _describe_room returns
            # None for a private one, and the beacon then opens no socket and
            # sends nothing, so a host who changes the visibility mid-lobby
            # starts or stops appearing within a second with no extra wiring
            # here to keep in step.
            self.beacon = discovery.Beacon(self._describe_room)
            self.beacon.start()

            log.info("hosting a room on port %s", port)
            return self.snapshot()

    def _describe_room(self):
        """The beacon payload, or None when nothing should be announced.

        Called on the beacon thread once a second. It reads self.room and
        self.server without the session lock deliberately: the lock is held
        across beacon.stop(), which joins this thread, so taking it here would
        deadlock closing the room. Both attributes are only ever replaced
        whole, so the worst case is describing a room one tick after it went.
        """
        room = self.room
        server = self.server
        if room is None or server is None:
            return None

        with room.lock:
            if room.rules.visibility != "public":
                return None

            host = room.players.get(room.host_id)
            host_name = host.username if host else ""
            players = len(room.players)
            cap = room.rules.player_cap

            return discovery.build_payload(
                port=server.port,
                name=room.rules.room_name.strip() or (
                    f"{host_name}'s room" if host_name else "Room"
                ),
                host_username=host_name,
                players=players,
                cap=cap,
                phase=room.phase,
                joinable=(
                    players < cap
                    and (
                        room.phase != MATCH_RUNNING
                        or room.rules.allow_join_in_progress
                    )
                ),
                code=room.rules.room_code,
                spectators=room.rules.allow_spectators,
            )

    # -- discovery --------------------------------------------------------

    def discovered(self) -> dict:
        """The rooms heard on the network.

        The listener is started on the first request rather than at launch, so
        a player who never opens multiplayer never binds the discovery port. It
        then stays up until the window closes, because the two seconds it takes
        to repopulate the list is worse than a socket sitting idle.
        """
        if self.listener is None:
            listener = discovery.Listener()
            try:
                listener.start()
            except OSError as error:
                # Almost always another program on 45882. Worth saying, because
                # the alternative is a list that is empty for no stated reason.
                log.warning("discovery is unavailable: %s", error)
                return {
                    "listening": False,
                    "rooms": [],
                    "detail": f"The discovery port could not be opened: {error}",
                }
            self.listener = listener

        return {"listening": True, "rooms": self.listener.rooms()}

    def stop_discovery(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None

    # -- joining ----------------------------------------------------------

    def address_for_code(self, typed: str):
        """The address of the room broadcasting this chosen code.

        None when nothing matches, which is the ordinary case: most of what is
        typed into that box is a generated code or an address, and this is asked
        first about all of them.

        The string "ambiguous" when more than one room answers to it. Nothing
        stops two hosts on one network from both picking TIGER, and quietly
        joining whichever was heard from first would be worse than saying so:
        the player would be in the wrong room with no way to tell.
        """
        wanted = (typed or "").strip().upper()
        if not wanted:
            return None

        rooms = self.discovered().get("rooms") or []
        matches = {
            room["target"] for room in rooms
            if (room.get("code") or "").upper() == wanted
        }

        if not matches:
            return None
        if len(matches) > 1:
            return "ambiguous"
        return matches.pop()

    def join(self, address: str, username: str, colour: str,
             spectate: bool = False) -> dict:
        with self.lock:
            self._close_locked()

            self.client = RoomClient()
            outcome = self.client.join(address, username, colour, spectate)

            if not outcome.get("ok"):
                self.client = None
                self.mode = self.IDLE
                return outcome

            self.mode = self.JOINED
            log.info(
                "%s a room at %s",
                "watching" if outcome.get("spectator") else "joined",
                address,
            )
            return outcome

    # -- watching ---------------------------------------------------------

    def spectating(self) -> bool:
        return bool(
            self.mode == self.JOINED
            and self.client
            and self.client.state().get("spectator")
        )

    def watch(self, player_id) -> bool:
        if not self.spectating():
            return False
        return self.client.watch(player_id)

    # -- shared -----------------------------------------------------------

    def close(self) -> None:
        with self.lock:
            self._close_locked()

    def _close_locked(self) -> None:
        # Before the server, so the last thing broadcast is not a room whose
        # listener has already gone.
        if self.server:
            self.server.stop_match()
        if self.beacon:
            self.beacon.stop()
            self.beacon = None
        if self.server:
            self.server.stop()
            self.server = None
        if self.client:
            self.client.leave()
            self.client = None
        self.room = None
        self.mode = self.IDLE

    def set_ready(self, is_ready: bool) -> bool:
        # Refused here as well as by the host. A spectator is not counted by
        # can_start, so a ready flag on one would be a control that reports a
        # state nothing reads.
        if self.spectating():
            return False
        if self.mode == self.HOSTING and self.room:
            self.room.set_ready(self.room.host_id, is_ready)
            self.server.notify_lobby()
            return True
        if self.mode == self.JOINED and self.client:
            return self.client.send(protocol.ready(is_ready))
        return False

    # -- the match --------------------------------------------------------

    def start_match(self) -> dict:
        if self.mode != self.HOSTING or not self.server:
            raise JoinRefused(protocol.NOT_HOST)
        return self.server.start_match()

    def return_to_lobby(self) -> dict:
        if self.mode != self.HOSTING or not self.server:
            raise JoinRefused(protocol.NOT_HOST)
        return self.server.return_to_lobby()

    def match_state(self):
        # The host reads its own engine, which is current by definition. A
        # joined player reads a drawing of the recent past, which is what makes
        # other players smooth, with their own snake taken from the newest
        # state so their own steering is not delayed with everybody else's.
        if self.mode == self.HOSTING and self.server:
            return self.server.match_state()
        if self.mode == self.JOINED and self.client:
            return self.client.match_state()
        return None

    def send_input(self, heading, seq, move) -> dict:
        """One direction, from whichever side of the room this instance is on.

        The host applies it to the engine and gets the answer at once, including
        the cell the head is now moving into, which is what lets the renderer
        stop sliding toward a cell the turn has just replaced. A joined client
        has no such answer to wait for, so it sends the intent and draws its own
        turn until the host confirms or contradicts it.
        """
        if self.mode == self.HOSTING and self.server and self.room:
            return self.server.apply_input(self.room.host_id, heading, seq, move)
        if self.spectating():
            return {"ok": False, "reason": "spectating"}
        if self.mode == self.JOINED and self.client:
            sent = self.client.send_input(heading, seq, move)
            return {"ok": bool(sent), "sent": bool(sent)}
        return {"ok": False, "reason": "not_in_a_room"}

    def can_set_rules(self) -> bool:
        return self.mode == self.HOSTING and bool(self.room)

    def set_rules(self, ruleset) -> None:
        if self.mode != self.HOSTING or not self.room:
            raise JoinRefused(protocol.NOT_HOST)
        self.room.set_rules(self.room.host_id, ruleset)
        self.server.notify_lobby()

    def kick(self, player_id: int) -> bool:
        if self.mode != self.HOSTING or not self.room:
            raise JoinRefused(protocol.NOT_HOST)
        removed = self.room.kick(self.room.host_id, player_id)
        if removed:
            self.server.disconnect(player_id, protocol.KICKED)
            self.server.notify_lobby()
        return removed

    def _code(self):
        """The short code a host reads out, or None if there is no address."""
        if not self.server:
            return None

        addresses = ports.local_addresses()
        if not addresses:
            return None

        try:
            return joincode.encode(addresses[0], self.server.port)
        except joincode.CodeError:
            return None

    def snapshot(self) -> dict:
        if self.mode == self.HOSTING and self.room:
            return {
                "mode": self.mode,
                "player_id": self.room.host_id,
                "room": self.room.to_dict(),
                "rules": self.room.rules.to_dict(),
                "port": self.server.port if self.server else None,
                "visible": self.room.rules.visibility == "public",
                "code": self._code(),
                "code_pretty": joincode.pretty(self._code()) if self._code() else None,
                # The host's own choice, shown beside the generated one rather
                # than instead of it. The generated code always works; a chosen
                # one only works for somebody on this network who can hear the
                # room, so replacing it would be taking away the reliable one.
                "chosen_code": (
                    self.room.rules.room_code.strip().upper()
                    if self.room and self.room.rules.visibility == "public"
                    else ""
                ),
                "addresses": [
                    f"{address}:{self.server.port}"
                    for address in ports.local_addresses()
                ] if self.server else [],
                "protocol": protocol.PROTOCOL_VERSION,
            }

        if self.mode == self.JOINED and self.client:
            state = self.client.state()
            return {
                "mode": self.mode,
                "player_id": state["player_id"],
                "room": state["room"],
                "rules": state["rules"],
                "address": state["address"],
                "closed_reason": state["closed_reason"],
                "spectator": state["spectator"],
                "watching": state["watching"],
                "protocol": protocol.PROTOCOL_VERSION,
            }

        return {"mode": self.IDLE, "protocol": protocol.PROTOCOL_VERSION}


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    session = SoloSession()
    net = NetSession()
    app.solo_session = session
    app.net_session = net

    # A policy rather than a meta tag, so it covers every response and not only
    # the one page that would have carried the tag.
    #
    # There is no reachable cross-site scripting in the frontend today: every
    # name is rendered with textContent and there is no innerHTML, no
    # insertAdjacentHTML and no eval anywhere in static/js. The policy is not a
    # fix for a hole, it is what makes the first one somebody opens fail loudly
    # instead of working.
    #
    # 'self' only, and no unsafe-inline, which is what forces scripts to stay in
    # files. connect-src includes the WebSocket schemes because the room client
    # is a socket to the host on the LAN, which is a different origin from the
    # page. frame-ancestors 'none' because nothing should ever embed this.
    POLICY = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("Content-Security-Policy", POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # -- shell ------------------------------------------------------------

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename):
        # send_from_directory refuses to escape STATIC_DIR, which is why it is
        # used here instead of building a path by hand.
        return send_from_directory(STATIC_DIR, filename)

    # -- meta -------------------------------------------------------------

    @app.route("/api/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "app_version": APP_VERSION,
                "protocol_version": PROTOCOL_VERSION,
            }
        )

    @app.route("/api/branding")
    def branding_info():
        # The frontend has no literal copy of the game name anywhere. It asks
        # for it here so that branding.py stays the single source of truth.

        return jsonify(
            {
                "app_name": branding.APP_NAME,
                "app_slug": branding.APP_SLUG,
                "tagline": branding.TAGLINE,
                "app_version": APP_VERSION,
            }
        )

    # -- profile ----------------------------------------------------------

    @app.route("/api/profile", methods=["GET"])
    def get_profile():
        return jsonify(profile_store.load())

    @app.route("/api/profile", methods=["POST"])
    def post_profile():
        payload = request.get_json(silent=True) or {}
        return jsonify(profile_store.save(payload))

    # -- rules ------------------------------------------------------------

    @app.route("/api/rules/schema")
    def rules_schema():
        # The rule editor is generated from this, so a new option added to
        # utils/game/rules.py appears in the interface with no frontend change.
        document = rules_module.schema_document()
        document["last_used"] = solo_store.last_rules()
        return jsonify(document)

    @app.route("/api/rules/validate", methods=["POST"])
    def rules_validate():
        # The editor calls this as the player types, so that a bad value is
        # named where it is entered rather than rejected on submit.
        payload = request.get_json(silent=True) or {}
        try:
            ruleset = rules_module.RuleSet.from_dict(
                payload.get("rules", {}), validate=False
            )
        except rules_module.RuleError as error:
            return jsonify({"valid": False, "errors": {"rules": str(error)}}), 200

        errors = ruleset.errors()

        # The derived intervals are only meaningful for a ruleset that passes.
        # Computing them anyway means an out-of-range speed raises inside the
        # endpoint whose whole job is to report that it is out of range.
        body = {
            "valid": not errors,
            "errors": errors,
            "rules": ruleset.to_dict(),
            "base_interval_ticks": None,
            "max_interval_ticks": None,
        }

        if not errors:
            body["fingerprint"] = ruleset.fingerprint()
            body["base_interval_ticks"] = ruleset.base_interval_ticks()
            body["max_interval_ticks"] = ruleset.max_interval_ticks()

        return jsonify(body)

    # -- presets ----------------------------------------------------------

    @app.route("/api/presets")
    def presets_list():
        return jsonify(
            {
                "presets": preset_store.listing(),
                "last_used": solo_store.last_rules(),
            }
        )

    @app.route("/api/presets", methods=["POST"])
    def presets_save():
        payload = request.get_json(silent=True) or {}
        try:
            ruleset = rules_module.RuleSet.from_dict(payload.get("rules", {}))
            saved = preset_store.save(payload.get("name"), ruleset)
        except rules_module.RuleError as error:
            return jsonify({"error": "invalid_rules", "detail": str(error)}), 400
        except preset_store.PresetError as error:
            return jsonify({"error": "invalid_preset", "detail": str(error)}), 400

        return jsonify(saved)

    @app.route("/api/presets/<path:name>", methods=["DELETE"])
    def presets_delete(name):
        try:
            removed = preset_store.delete(name)
        except preset_store.PresetError as error:
            return jsonify({"error": "invalid_preset", "detail": str(error)}), 400

        if not removed:
            return jsonify({"error": "not_found", "detail": name}), 404

        return jsonify({"ok": True})

    # -- solo -------------------------------------------------------------

    @app.route("/api/solo/start", methods=["POST"])
    def solo_start():
        payload = request.get_json(silent=True) or {}
        try:
            ruleset = rules_module.RuleSet.from_dict(payload.get("rules", {}))
        except rules_module.RuleError as error:
            return jsonify({"error": "invalid_rules", "detail": str(error)}), 400

        # Read here rather than taken from the request, so the colour on the
        # board is the one saved in the profile and there is no second place for
        # it to be set from and disagree.
        return jsonify(session.start(ruleset, profile_store.load().get("colour")))

    @app.route("/api/solo/input", methods=["POST"])
    def solo_input():
        payload = request.get_json(silent=True) or {}
        heading = payload.get("heading")

        if session.engine is None:
            return jsonify({"error": "no_match"}), 409
        if not session.engine.set_heading(heading):
            return jsonify({"error": "invalid_heading", "detail": str(heading)}), 400

        # The response carries the state the turn produced. A queued turn
        # changes which cell the head is moving into, and the renderer slides
        # toward that cell, so waiting for the next poll left it briefly
        # animating a move that was no longer going to happen.
        return jsonify({"ok": True, "state": session.state()})

    @app.route("/api/solo/state")
    def solo_state():
        return jsonify(session.state())

    @app.route("/api/solo/pause", methods=["POST"])
    def solo_pause():
        if session.engine is None:
            return jsonify({"error": "no_match"}), 409
        return jsonify({"phase": session.engine.toggle_pause()})

    @app.route("/api/solo/stop", methods=["POST"])
    def solo_stop():
        session.stop()
        return jsonify({"ok": True})

    @app.route("/api/solo/history")
    def solo_history():
        fingerprint = request.args.get("rules")
        return jsonify(solo_store.history(fingerprint))

    # -- rooms ------------------------------------------------------------

    def _identity():
        profile = profile_store.load()
        return profile["username"], profile["colour"]

    @app.route("/api/room")
    def room_state():
        return jsonify(net.snapshot())

    @app.route("/api/room/discover")
    def room_discover():
        # Polled while the chooser is on screen. It only reads a table that a
        # background thread fills, so it does no network work of its own and
        # answers immediately whether or not anything has been heard.
        payload = net.discovered()
        payload["protocol"] = protocol.PROTOCOL_VERSION
        return jsonify(payload)

    @app.route("/api/room/host", methods=["POST"])
    def room_host():
        payload = request.get_json(silent=True) or {}
        try:
            ruleset = rules_module.RuleSet.from_dict(payload.get("rules", {}))
        except rules_module.RuleError as error:
            return jsonify({"error": "invalid_rules", "detail": str(error)}), 400

        username, colour = _identity()

        try:
            return jsonify(net.host(ruleset, username, colour))
        except OSError as error:
            return jsonify({
                "error": "listener_failed",
                "detail": f"The room could not open a listener: {error}",
            }), 500

    @app.route("/api/room/join", methods=["POST"])
    def room_join():
        payload = request.get_json(silent=True) or {}
        username, colour = _identity()

        # A colour clash is answered by the host with the free colours, and the
        # frontend offers one to click. The override arrives here on the retry.
        colour = payload.get("colour") or colour
        username = payload.get("username") or username

        # Watching rather than playing. The same endpoint, because everything
        # before the handshake, resolving a code, resolving an address, naming
        # the refusal, is the same work either way.
        spectate = bool(payload.get("spectate"))

        # One box accepts either. Nobody should have to know whether the thing
        # they were given is a code or an address.
        target = (payload.get("address") or "").strip()

        # A chosen code first, then the generated one.
        #
        # That order matters. A chosen code is matched against rooms actually
        # broadcasting on the network, which is stronger evidence than any
        # decode: the generated form carries only a five-bit checksum, so about
        # one arbitrary five-character string in thirty-two decodes cleanly to
        # an address that belongs to nobody. Matching a real room first means a
        # host who picks a code that happens to look decodable still gets found.
        chosen = net.address_for_code(target)
        if chosen == "ambiguous":
            return jsonify({
                "ok": False,
                "reason": "ambiguous_code",
                "text": "More than one room on this network is using that "
                        "code. Use the generated code or the address instead.",
            }), 409

        if chosen:
            target = chosen
        elif joincode.looks_like_a_code(target):
            try:
                host, port = joincode.decode(target)
            except joincode.CodeError as error:
                return jsonify({"ok": False, "reason": "bad_code",
                                "text": str(error)}), 400
            target = f"{host}:{port}"

        try:
            outcome = net.join(target, username, colour, spectate)
        except ValueError as error:
            return jsonify({"ok": False, "reason": "bad_address",
                            "text": str(error)}), 400

        return jsonify(outcome), 200 if outcome.get("ok") else 409

    @app.route("/api/room/watch", methods=["POST"])
    def room_watch():
        payload = request.get_json(silent=True) or {}

        wanted = payload.get("player")
        if wanted is not None:
            try:
                wanted = int(wanted)
            except (TypeError, ValueError):
                return jsonify({"error": "bad_player"}), 400

        if not net.watch(wanted):
            return jsonify({"error": "not_spectating"}), 409

        return jsonify({"ok": True})

    @app.route("/api/room/leave", methods=["POST"])
    def room_leave():
        net.close()
        return jsonify({"ok": True})

    @app.route("/api/room/ready", methods=["POST"])
    def room_ready():
        payload = request.get_json(silent=True) or {}
        if not net.set_ready(bool(payload.get("ready"))):
            return jsonify({"error": "not_in_a_room"}), 409
        return jsonify({"ok": True})

    @app.route("/api/room/rules", methods=["POST"])
    def room_rules():
        payload = request.get_json(silent=True) or {}

        # Authority first. Validating first told a guest what was wrong with a
        # rule set they were never going to be allowed to set, which is a
        # confusing answer to the wrong question.
        if not net.can_set_rules():
            return jsonify({"error": protocol.NOT_HOST}), 403

        # Built without validating, then checked, so that the answer names every
        # field that is wrong instead of only the first. The editor marks them
        # where they are entered, which it cannot do with one sentence.
        try:
            ruleset = rules_module.RuleSet.from_dict(
                payload.get("rules", {}), validate=False
            )
        except rules_module.RuleError as error:
            return jsonify({
                "error": "invalid_rules",
                "errors": {"rules": str(error)},
            }), 400

        errors = ruleset.errors()
        if errors:
            return jsonify({"error": "invalid_rules", "errors": errors}), 400

        try:
            net.set_rules(ruleset)
        except JoinRefused as refusal:
            return jsonify({"error": refusal.reason}), 403

        return jsonify(net.snapshot())

    @app.route("/api/room/start", methods=["POST"])
    def room_start():
        try:
            outcome = net.start_match()
        except JoinRefused as refusal:
            return jsonify({"error": refusal.reason}), 403

        return jsonify(outcome), 200 if outcome.get("ok") else 409

    @app.route("/api/room/rematch", methods=["POST"])
    def room_rematch():
        try:
            return jsonify(net.return_to_lobby())
        except JoinRefused as refusal:
            return jsonify({"error": refusal.reason}), 403

    # -- match ------------------------------------------------------------

    @app.route("/api/match/state")
    def match_state():
        # Polled at 20 Hz over loopback by the browser, exactly as single player
        # is. The host reads its own engine; a joined player reads the state its
        # client folded from whatever the host has sent, which is always the
        # newest and never a backlog.
        state = net.match_state()
        if state is None:
            return jsonify({"phase": "none"})
        return jsonify(state)

    @app.route("/api/match/input", methods=["POST"])
    def match_input():
        payload = request.get_json(silent=True) or {}

        try:
            seq = int(payload.get("seq", 0))
            move = int(payload.get("move", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "bad_input"}), 400

        outcome = net.send_input(payload.get("heading"), seq, move)
        return jsonify(outcome)

    @app.route("/api/room/kick", methods=["POST"])
    def room_kick():
        payload = request.get_json(silent=True) or {}
        try:
            removed = net.kick(int(payload.get("player", -1)))
        except JoinRefused as refusal:
            return jsonify({"error": refusal.reason}), 403
        except (TypeError, ValueError):
            return jsonify({"error": "bad_player"}), 400

        if not removed:
            return jsonify({"error": "not_found"}), 404

        return jsonify({"ok": True})

    # -- responses --------------------------------------------------------

    @app.after_request
    def no_store(response):
        # The UI is served from disk to a single local window. Caching it only
        # produces confusing stale-asset behaviour during development.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "not_found"}), 404

    def shutdown():
        """Stop every thread this app owns. Called when the window closes."""
        session.stop()
        net.close()
        net.stop_discovery()
        log.info("application threads stopped")

    app.shutdown_threads = shutdown

    log.info("flask app created, static dir %s", STATIC_DIR)
    return app
