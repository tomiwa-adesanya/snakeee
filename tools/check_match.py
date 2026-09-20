"""Drive a whole shared match between two local instances and check it.

Run it with no arguments, from the project root:

    python tools/check_match.py

It starts three copies of the game with separate data directories, hosts a room
from one, joins from the second, watches from the third, plays a match, and
checks the things that are worth checking about it. Then it stops all of them
and reports. Nothing is left behind and nothing needs typing.

Why this exists. Several real bugs in this project passed the whole test suite
and were caught by driving a running server. That is worth doing on every change
to the network or the match, and it is not worth doing by hand twenty commands
at a time.

No third-party dependencies on purpose: a bare Python install must be able to
run it, the same rule check_ascii.py follows.

This directory is not imported by the application and can be deleted before a
release without touching the source.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# Kept in step with utils/net/protocol.py by hand. The harness speaks the wire
# directly in one place, and a mismatch here would be refused at the handshake
# with a message naming both numbers, so it fails loudly rather than silently.
PROTOCOL = 3

# Kept in step with utils/net/server.py by hand, for the same reason as above.
CONNECTION_WINDOW = 10.0

HTTP_A = 45880
HTTP_B = 45890
HTTP_C = 45900
WS_PORT = 45881

STARTUP_TIMEOUT = 25.0
COUNTDOWN = 3

# One definition, used to open the room and to put it back after the rule
# editing checks have changed it.
ROOM_RULES = {
    "arena_width": 26,
    "arena_height": 26,
    "base_speed": 5,
    "start_countdown": COUNTDOWN,
    "min_players": 2,
    "lives": 3,
    "respawn_delay": 1,
    "win_condition": "endless",
    "allow_join_in_progress": True,

    # Off here, and turned on by the section that is about them. The defaults
    # put poisons and pickups on the floor, and a snake that ate a confusion
    # poison made the reversal check fail: pressing backwards while confused
    # produces forwards, which is legal and accepted. The mechanic was right
    # and the check had quietly stopped testing what it meant to.
    "poisons": "off",
    "pickups": "off",
}


def repository_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- the two instances ------------------------------------------------------


def isolated_environment(directory):
    """A copy of the environment pointing the game at its own data directory.

    Each platform reads a different variable, which is why this is not simply
    HOME. Getting it wrong on Windows does not fail loudly: both instances would
    quietly share one profile, and the second would take the first's username
    and then be refused for a duplicate name.
    """
    environment = dict(os.environ)
    os.makedirs(directory, exist_ok=True)

    if sys.platform.startswith("win"):
        environment["APPDATA"] = directory
    elif sys.platform == "darwin":
        environment["HOME"] = directory
    else:
        environment["XDG_DATA_HOME"] = directory

    return environment


class Instance:
    def __init__(self, name, port, directory):
        self.name = name
        self.port = port
        self.directory = directory
        self.process = None
        self.log = None

    def start(self):
        os.makedirs(self.directory, exist_ok=True)
        self.log = open(os.path.join(self.directory, "run.log"), "w")
        self.process = subprocess.Popen(
            [sys.executable, "main.py", "--no-window", "--port", str(self.port)],
            cwd=repository_root(),
            env=isolated_environment(self.directory),
            stdout=self.log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

    def wait_until_ready(self, timeout=STARTUP_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.name} exited during startup; see {self.log_path()}"
                )
            try:
                self.get("/api/health")
                return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        raise RuntimeError(
            f"{self.name} did not answer within {timeout:.0f}s; "
            f"see {self.log_path()}"
        )

    def log_path(self):
        return os.path.join(self.directory, "run.log")

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        request = urllib.request.Request(self.url(path))
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path, body=None):
        payload = json.dumps(body or {}).encode("utf-8")
        request = urllib.request.Request(
            self.url(path),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # A refusal is an answer, not a failure. Several of the checks below
            # are specifically about what the refusal says.
            return json.loads(error.read().decode("utf-8"))

    def stop(self):
        if self.process is None:
            return
        try:
            self.post("/api/room/leave")
        except Exception:  # noqa: BLE001
            pass

        self.process.terminate()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

        if self.log:
            self.log.close()


# -- reporting --------------------------------------------------------------


class Report:
    def __init__(self):
        self.results = []

    def check(self, description, condition, detail=""):
        passed = bool(condition)
        self.results.append((description, passed, detail))
        mark = "PASS" if passed else "FAIL"
        line = f"  {mark}  {description}"
        if detail and not passed:
            line += f"\n          {detail}"
        print(line, flush=True)
        return passed

    def note(self, text):
        print(f"        {text}", flush=True)

    def failures(self):
        return [entry for entry in self.results if not entry[1]]


def heading(text):
    print(f"\n{text}", flush=True)


# -- the checks -------------------------------------------------------------


def toward_item(snake, items):
    """A heading that takes this snake closer to the nearest item.

    Confusion is on the floor here, so what the host does with a press is not
    always what was pressed. That is fine for this: the aim is to make contact
    with items often, not to steer perfectly, and a reversed press still moves
    the snake somewhere.

    Returns None when there is nothing to aim at or the only way there is
    straight backwards, which the host would refuse anyway.
    """
    body = snake.get("b") or []
    if not items or not body:
        return None

    head = body[0]
    nearest = min(
        items,
        key=lambda item: abs(item[0] - head[0]) + abs(item[1] - head[1]),
    )

    dx = nearest[0] - head[0]
    dy = nearest[1] - head[1]
    facing = snake.get("h") or [1, 0]

    wanted = []
    if abs(dx) >= abs(dy) and dx != 0:
        wanted.append("right" if dx > 0 else "left")
    if dy != 0:
        wanted.append("down" if dy > 0 else "up")
    if dx != 0:
        wanted.append("right" if dx > 0 else "left")

    for name in wanted:
        step = {"up": (0, -1), "down": (0, 1),
                "left": (-1, 0), "right": (1, 0)}[name]
        if step[0] == -facing[0] and step[1] == -facing[1]:
            continue
        return name

    return None


def snake_by_id(state, snake_id):
    for snake in state.get("snakes") or []:
        if snake["id"] == snake_id:
            return snake
    return None


def wait_for(condition, timeout=6.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    return None


def port_is_free(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def hardening_checks(host, watcher, ws_port, report):
    """The throttles, against a host running in another process.

    Raw sockets rather than the HTTP API, because there is no endpoint that
    sends a malformed frame and there should not be one. This script opens
    connections to the real room the way a modified client would, which is the
    case the limits exist for: the source is public and changing a client to
    misbehave is a two line edit.

    Runs last. It deliberately uses up an address's connection allowance, so
    anything after it would be refused for reasons that have nothing to do with
    what it was checking.
    """
    heading("What a misbehaving client costs the host")

    # Everything above this point has been opening connections from 127.0.0.1,
    # and the connection limit counts by address. Without this wait the first
    # socket below is refused for the previous section's joins and the check
    # reports a throttle failure that is really a scheduling failure. Found by
    # running it: two checks failed with zero messages accepted.
    time.sleep(CONNECTION_WINDOW + 1.0)

    try:
        import asyncio

        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        report.check(
            "the throttle checks could run",
            False,
            "websockets is not importable from this script",
        )
        return

    async def hello(connection, name):
        await connection.send(json.dumps({
            "type": "hello", "protocol": PROTOCOL, "username": name,
            "colour": "#c878f0", "spectate": False,
        }))
        await asyncio.sleep(0.3)
        while True:
            try:
                await asyncio.wait_for(connection.recv(), timeout=0.15)
            except Exception:  # noqa: BLE001
                return

    async def push(connection, payloads, pause):
        sent = 0
        for payload in payloads:
            try:
                await connection.send(payload)
            except ConnectionClosed:
                break
            sent += 1
            await asyncio.sleep(pause)

        reasons = []
        try:
            while True:
                raw = await asyncio.wait_for(connection.recv(), timeout=0.4)
                message = json.loads(raw)
                if message.get("type") == "reject":
                    reasons.append(message.get("reason"))
        except Exception:  # noqa: BLE001
            pass

        return sent, reasons, connection.state.name != "OPEN"

    def an_input(index):
        return json.dumps({
            "type": "input", "seq": index, "heading": "left", "move": 0,
        })

    async def run():
        results = {}
        url = f"ws://127.0.0.1:{ws_port}"

        async with connect(url) as connection:
            await hello(connection, "garbage")
            results["garbage"] = await push(
                connection, ["not json at all"] * 25, 0.01
            )

        async with connect(url) as connection:
            await hello(connection, "nonstop")
            results["flood"] = await push(
                connection, [an_input(i) for i in range(4000)], 0.0002
            )

        # A held key, resent up to three times inside 260ms, is around 90 a
        # second from a client doing nothing wrong. This must survive.
        async with connect(url) as connection:
            await hello(connection, "masher")
            start = time.monotonic()
            index = 0
            while time.monotonic() - start < 2.0:
                try:
                    await connection.send(an_input(index))
                except ConnectionClosed:
                    break
                index += 1
                await asyncio.sleep(1 / 90.0)
            results["masher"] = (index, [], connection.state.name != "OPEN")

        return results

    try:
        found = asyncio.run(run())
    except Exception as error:  # noqa: BLE001
        report.check("the throttle checks could run", False, repr(error))
        return

    sent, reasons, closed = found["garbage"]
    report.check(
        "a client sending nonsense is closed rather than answered forever",
        closed and reasons and reasons[-1] == "too_many_bad_messages",
        f"{sent} accepted, reasons {reasons}, closed {closed}",
    )

    sent, reasons, closed = found["flood"]
    report.check(
        "a client that will not stop is closed",
        closed and "flooding" in reasons,
        f"{sent} accepted, reasons {reasons}, closed {closed}",
    )

    sent, _reasons, closed = found["masher"]
    report.check(
        "and a real client at full tilt is left alone",
        not closed and sent > 150,
        f"closed after {sent} messages at 90 a second",
    )

    # Now spend what is left of the address's allowance and check the refusal
    # names itself. In the first version this was driven from the host instance,
    # which closes its own room before it joins anything, so the check reported
    # that nothing was listening on a port it had just shut. The watcher is the
    # instance with no room of its own.
    turned_away = None
    for _ in range(8):
        answer = watcher.post("/api/room/join", {
            "address": f"127.0.0.1:{ws_port}",
        })
        if answer.get("ok"):
            watcher.post("/api/room/leave")
            continue
        turned_away = answer
        if answer.get("reason") == "too_many_attempts":
            break

    report.check(
        "connections from one address run out, and say so",
        bool(turned_away)
        and turned_away.get("reason") == "too_many_attempts",
        str(turned_away)[:160],
    )
    watcher.post("/api/room/leave")


def spectator_checks(host, client, watcher, ws_port, report):
    """Watching a real room from a third process.

    Everything here was writable as a unit test and would have proved less. The
    thing that goes wrong with a spectator is that it takes a seat, holds up a
    start, or reaches a message it should not, and all three of those are about
    a running host with real connections on it rather than about a Room object.
    """
    heading("Watching a room")

    watcher.post("/api/profile", {"username": "cass", "colour": "#f0b950"})

    watching = watcher.post("/api/room/join", {
        "address": f"127.0.0.1:{ws_port}",
        "spectate": True,
    })
    report.check(
        "a third instance can watch the room",
        watching.get("ok") is True and watching.get("spectator") is True,
        f"{watching.get('reason')}: {watching.get('text')}",
    )

    room = wait_for(
        lambda: host.get("/api/room").get("room")
        if len(host.get("/api/room")["room"].get("spectators") or []) == 1
        else None,
        timeout=5,
    )
    report.check(
        "the host sees one spectator",
        bool(room),
        str((host.get("/api/room").get("room") or {}).get("spectators")),
    )

    if room:
        report.check(
            "and it did not take a seat",
            len(room["players"]) == 2,
            str([player["username"] for player in room["players"]]),
        )
        report.check(
            "it is following somebody rather than nobody",
            room["spectators"][0]["watching"] == room["host_id"],
            str(room["spectators"][0]),
        )

    # A spectator must not be able to hold up a start and must not be able to
    # let one through. Both halves are asked, because getting either wrong is
    # a different and equally bad bug.
    #
    # Held up: the room is waiting on the two players and says so by name, and
    # the spectator's name must not be among them.
    blocked = (room or {}).get("start_blocked_by") or ""
    report.check(
        "a spectator is never what a start is waiting for",
        "cass" not in blocked.lower(),
        blocked,
    )

    # Let through: with the minimum raised above the number of players, the
    # room must still refuse to start even though the spectator brings the
    # number of connections up to it.
    host.post("/api/room/rules", {"rules": dict(ROOM_RULES, min_players=3)})
    short = wait_for(
        lambda: host.get("/api/room")["room"]
        if host.get("/api/room")["room"]["min_players"] == 3 else None,
        timeout=4,
    )
    report.check(
        "and never makes up the numbers for one",
        bool(short) and short["can_start"] is False
        and "3 players" in (short["start_blocked_by"] or ""),
        str((short or {}).get("start_blocked_by")),
    )
    host.post("/api/room/rules", {"rules": dict(ROOM_RULES)})

    heading("What a spectator is refused")

    refused_ready = watcher.post("/api/room/ready", {"ready": True})
    report.check(
        "it cannot declare itself ready",
        refused_ready.get("error") is not None,
        str(refused_ready),
    )

    refused_rules = watcher.post("/api/room/rules", {"rules": dict(ROOM_RULES)})
    report.check(
        "it cannot change the rules",
        refused_rules.get("error") is not None,
        str(refused_rules)[:160],
    )

    refused_start = watcher.post("/api/room/start")
    report.check(
        "it cannot start the match",
        refused_start.get("ok") is not True,
        str(refused_start)[:160],
    )

    refused_kick = watcher.post("/api/room/kick", {"player": 0})
    report.check(
        "it cannot remove anybody",
        refused_kick.get("error") is not None,
        str(refused_kick),
    )

    refused_input = watcher.post(
        "/api/match/input", {"heading": "left", "seq": 1, "move": 0}
    )
    report.check(
        "it cannot steer",
        refused_input.get("ok") is False,
        str(refused_input),
    )

    # And the room is untouched by any of it.
    after = host.get("/api/room")["room"]
    report.check(
        "none of that changed the room",
        after["phase"] == "waiting" and len(after["players"]) == 2,
        f"{after['phase']}, {len(after['players'])} players",
    )

    heading("Watching a match that is running")

    host.post("/api/room/ready", {"ready": True})
    client.post("/api/room/ready", {"ready": True})
    started = wait_for(
        lambda: host.post("/api/room/start") if
        host.get("/api/room")["room"]["can_start"] else None,
        timeout=6,
    )
    report.check(
        "the match starts with a spectator in the room",
        bool(started) and started.get("ok") is True,
        str(started)[:160],
    )

    running = wait_for(
        lambda: host.get("/api/match/state")
        if host.get("/api/match/state").get("phase") == "running" else None,
        timeout=COUNTDOWN + 6,
    )
    report.check("the match reaches running", bool(running))

    seen = wait_for(
        lambda: watcher.get("/api/match/state")
        if (watcher.get("/api/match/state").get("snakes") or []) else None,
        timeout=6,
    )
    report.check(
        "the spectator is sent the match",
        bool(seen),
        str(watcher.get("/api/match/state"))[:160],
    )

    if seen:
        # The stream it is on is a player's stream, so it carries every snake's
        # name and score and the body of at least the one being followed. This
        # is a single arena, so every body is in it; the scoping itself is
        # checked in the test suite where a grid can be built directly.
        report.check(
            "and it carries the same snakes the players are sent",
            len(seen.get("snakes") or []) == len(running.get("snakes") or []),
            f"{len(seen.get('snakes') or [])} against "
            f"{len(running.get('snakes') or [])}",
        )
        report.check(
            "with no snake belonging to the spectator",
            all(
                snake["id"] != watching.get("player_id")
                for snake in seen.get("snakes") or []
            ),
            str(sorted(snake["id"] for snake in seen.get("snakes") or [])),
        )

    # Switching who is followed. The id comes from the room rather than being
    # assumed, because a room hands out a fresh id on every join and a harness
    # that guesses them has already been the cause of a check that silently
    # stopped checking anything.
    guest_id = client.get("/api/room").get("player_id")
    switched = watcher.post("/api/room/watch", {"player": guest_id})
    report.check(
        "a spectator can follow somebody else",
        switched.get("ok") is True,
        str(switched),
    )

    moved = wait_for(
        lambda: watcher.get("/api/room").get("watching") == guest_id,
        timeout=5,
    )
    report.check(
        "and the host reports the new target back to it",
        bool(moved),
        str(watcher.get("/api/room").get("watching")),
    )

    still_drawing = wait_for(
        lambda: (watcher.get("/api/match/state").get("snakes") or []) and True,
        timeout=6,
    )
    report.check(
        "it is still being sent a match after the switch",
        bool(still_drawing),
        "the stream stopped after changing who was watched",
    )

    # Asking to follow somebody who is not there. The host refuses it, and the
    # refusal must not cost the spectator its place in the room.
    #
    # This is what that check was for, and the first version of it asserted only
    # that the local request returned ok, which was true whatever the host did
    # with it. Written properly, it failed: a refusal on an open connection was
    # being read as the connection being closed, and a spectator that mistyped
    # an id disconnected itself over it.
    watcher.post("/api/room/watch", {"player": 4242})
    time.sleep(0.8)

    report.check(
        "a bad target leaves the spectator on the player it had",
        watcher.get("/api/room").get("watching") == guest_id,
        str(watcher.get("/api/room").get("watching")),
    )
    report.check(
        "and does not cost it its place in the room",
        len(host.get("/api/room")["room"].get("spectators") or []) == 1
        and watcher.get("/api/room").get("closed_reason") is None,
        str(watcher.get("/api/room").get("closed_reason")),
    )
    report.check(
        "and it is still being sent the match",
        bool(wait_for(
            lambda: (watcher.get("/api/match/state").get("snakes") or []) and True,
            timeout=5,
        )),
        "the stream stopped after a refused request",
    )

    heading("A room that does not want to be watched")

    watcher.post("/api/room/leave")
    host.post("/api/room/rematch")
    time.sleep(0.6)

    host.post("/api/room/rules", {"rules": dict(
        ROOM_RULES, allow_spectators=False
    )})

    turned_away = watcher.post("/api/room/join", {
        "address": f"127.0.0.1:{ws_port}",
        "spectate": True,
    })
    report.check(
        "a spectator is refused when the rule is off",
        turned_away.get("ok") is False
        and turned_away.get("reason") == "spectators_not_allowed",
        str(turned_away)[:160],
    )

    host.post("/api/room/rules", {"rules": dict(ROOM_RULES)})

    heading("A full room can still be watched")

    # Two players and a cap of two. A player is refused and a spectator is not,
    # which is the whole reason a spectator is held outside the player list.
    host.post("/api/room/rules", {"rules": dict(ROOM_RULES, player_cap=2)})

    full = watcher.post("/api/room/join", {"address": f"127.0.0.1:{ws_port}"})
    report.check(
        "a third player is refused",
        full.get("ok") is False and full.get("reason") == "room_full",
        str(full)[:160],
    )

    admitted = watcher.post("/api/room/join", {
        "address": f"127.0.0.1:{ws_port}",
        "spectate": True,
    })
    report.check(
        "and a spectator is admitted anyway",
        admitted.get("ok") is True and admitted.get("spectator") is True,
        str(admitted)[:160],
    )

    watcher.post("/api/room/leave")
    host.post("/api/room/rules", {"rules": dict(ROOM_RULES)})


def run_checks(host, client, watcher, report):
    heading("Both instances answer")

    health_a = host.get("/api/health")
    health_b = client.get("/api/health")
    report.check(
        "the host is up",
        health_a.get("status") == "ok",
        str(health_a),
    )
    report.check(
        "the joining instance is up",
        health_b.get("status") == "ok",
        str(health_b),
    )
    report.check(
        "both speak the same protocol",
        health_a.get("protocol_version") == health_b.get("protocol_version"),
        f"{health_a.get('protocol_version')} against "
        f"{health_b.get('protocol_version')}",
    )
    report.note(f"protocol version {health_a.get('protocol_version')}")

    heading("A room, and somebody in it")

    host.post("/api/profile", {"username": "ann", "colour": "#ef6461"})
    client.post("/api/profile", {"username": "bob", "colour": "#3ecf8e"})

    hosted = host.post("/api/room/host", {"rules": ROOM_RULES})
    report.check(
        "the room opened",
        hosted.get("mode") == "hosting",
        str(hosted)[:200],
    )

    ws_port = hosted.get("port") or WS_PORT
    joined = client.post("/api/room/join", {"address": f"127.0.0.1:{ws_port}"})
    report.check(
        "the second instance joined",
        joined.get("ok") is True,
        f"{joined.get('reason')}: {joined.get('text')}",
    )

    heading("The rules the room plays by")

    keys = ("arena_width", "base_speed", "lives", "win_condition", "kill_target")

    opened = host.get("/api/room")
    report.check(
        "the host can read the rules the editor opens with",
        all(key in (opened.get("rules") or {}) for key in keys),
        str(sorted(opened.get("rules") or {}))[:120],
    )

    contradictory = host.post("/api/room/rules", {"rules": {
        "win_condition": "last_standing",
        "lives": 0,
        "arena_width": 26,
        "arena_height": 26,
    }})
    report.check(
        "a rule set that cannot end a match is refused",
        contradictory.get("error") == "invalid_rules",
        str(contradictory),
    )
    report.check(
        "the refusal names the field, so the editor can mark it",
        "lives" in (contradictory.get("errors") or {}),
        str(contradictory.get("errors")),
    )

    changed = host.post("/api/room/rules", {"rules": {
        "arena_width": 40,
        "arena_height": 30,
        "base_speed": 8,
        "lives": 5,
        "win_condition": "first_to_kills",
        "kill_target": 4,
    }})
    report.check(
        "the host can change them",
        (changed.get("rules") or {}).get("base_speed") == 8,
        str(changed.get("error") or changed.get("rules", {}).get("base_speed")),
    )

    # The rules used to travel only with the welcome message, so a guest who
    # was already in the room kept the ones they joined under and would have
    # played a match under rules they were never shown.
    seen = wait_for(
        lambda: (
            host.get("/api/room")
            and client.get("/api/room")
            if (client.get("/api/room").get("rules") or {}).get("base_speed") == 8
            else None
        ),
        timeout=4,
    )
    report.check("a guest is told when they change", bool(seen))

    if seen:
        guest_rules = client.get("/api/room")["rules"]
        report.check(
            "the guest agrees on every rule, not just the one that was watched",
            all(guest_rules[key] == changed["rules"][key] for key in keys),
            str({key: guest_rules[key] for key in keys}),
        )

    refused_guest = client.post("/api/room/rules", {"rules": {"arena_width": 30}})
    report.check(
        "a guest cannot change them, and is told why rather than critiqued",
        refused_guest.get("error") == "not_host",
        str(refused_guest),
    )

    # Back to what the rest of this run expects. Everything below reads the
    # countdown and the snake positions, and would otherwise be checking a
    # different match from the one it was written for.
    host.post("/api/room/rules", {"rules": ROOM_RULES})

    heading("Saved setups")

    listing = host.get("/api/presets").get("presets") or []
    report.check(
        "there are setups to load",
        len(listing) >= 3,
        str(len(listing)),
    )
    report.check(
        "a setup carries the multiplayer rules, not only the arena",
        listing and all(
            key in listing[0]["rules"]
            for key in ("severing", "win_condition", "lives", "sever_score_cost")
        ),
    )

    stored = host.post("/api/presets", {"name": "Check setup", "rules": {
        "arena_width": 40, "arena_height": 30,
        "win_condition": "first_to_kills", "kill_target": 6,
    }})
    report.check(
        "a setup can be saved from the room rules",
        not stored.get("error"),
        str(stored.get("detail") or stored.get("error")),
    )

    saved = [
        row for row in (host.get("/api/presets").get("presets") or [])
        if row["name"] == "Check setup"
    ]
    report.check(
        "and comes back with what was in it",
        bool(saved) and saved[0]["rules"]["kill_target"] == 6,
        str(saved[:1]),
    )
    report.check(
        "and is not marked as one of the shipped ones",
        bool(saved) and saved[0]["builtin"] is False,
    )

    heading("Starting is refused until the room is ready")

    refused = host.post("/api/room/start")
    report.check(
        "a start with somebody not ready is refused",
        refused.get("ok") is False and refused.get("reason") == "cannot_start",
        str(refused),
    )
    report.check(
        "the refusal says who it is waiting for",
        "bob" in (refused.get("text") or ""),
        str(refused.get("text")),
    )

    client.post("/api/room/ready", {"ready": True})
    started = wait_for(
        lambda: host.post("/api/room/start") if host.get(
            "/api/room"
        )["room"]["can_start"] else None
    )
    report.check(
        "the match started",
        bool(started) and started.get("ok") is True,
        str(started)[:200],
    )

    if not started or not started.get("ok"):
        return

    heading("The countdown")

    counting = host.get("/api/match/state")
    report.check(
        "the match opens on a countdown",
        counting.get("phase") == "countdown",
        str(counting.get("phase")),
    )
    report.check(
        "the countdown is roughly the length that was asked for",
        0 < counting.get("countdown_ms", 0) <= COUNTDOWN * 1000,
        f"{counting.get('countdown_ms')}ms against {COUNTDOWN * 1000}ms",
    )

    # Timed rather than sampled once. There is a real gap between the host
    # starting a match and the first broadcast reaching anybody, and the useful
    # question is how long it is, not whether one particular poll landed inside
    # it. A player who waits a whole second on a black arena would notice.
    began = time.monotonic()
    mirrored = wait_for(
        lambda: (
            client.get("/api/match/state")
            if client.get("/api/match/state").get("arena")
            else None
        ),
        timeout=2.0,
        interval=0.02,
    )
    waited_ms = round((time.monotonic() - began) * 1000)

    report.check(
        "the joining instance is given the opening state",
        bool(mirrored),
        "nothing arrived within two seconds",
    )

    if mirrored:
        # Reported rather than asserted. The expectation is one broadcast beat,
        # about 50 ms, because starting a match forces the next message to be a
        # keyframe. A number in the hundreds would mean that is not happening
        # and the client is waiting for the next scheduled one instead. There is
        # no threshold here because I have not measured enough machines to know
        # where a fair one sits.
        report.note(f"the opening state arrived {waited_ms}ms after the start")

        report.check(
            "the joining instance sees the same arena",
            mirrored.get("arena") == counting.get("arena"),
            f"{mirrored.get('arena')} against {counting.get('arena')}",
        )
        report.check(
            "the joining instance sees both snakes",
            len(mirrored.get("snakes") or []) == 2,
            str(len(mirrored.get("snakes") or [])),
        )

    running = wait_for(
        lambda: host.get("/api/match/state").get("phase") == "running",
        timeout=COUNTDOWN + 4,
    )
    report.check("the match starts running", bool(running))

    heading("The two sides agree, sample by sample")

    behind_by = []
    on_the_path = True
    lengths_agree = True

    for _ in range(8):
        # The client is read first, deliberately. Reading the host first lets it
        # tick between the two calls, which makes the client look a tick ahead
        # when it is simply a newer reading. This way any client-ahead result is
        # a real one.
        client_state = client.get("/api/match/state")
        host_state = host.get("/api/match/state")
        behind_by.append(host_state["t"] - client_state["t"])

        for entry in client_state.get("snakes") or []:
            theirs = snake_by_id(host_state, entry["id"])
            if not theirs or not entry["b"] or not theirs["b"]:
                continue
            if len(entry["b"]) != len(theirs["b"]):
                lengths_agree = False
            # The client is normally a snapshot behind, so its head is not the
            # host's head. It must be a cell the host's snake has been in: a
            # client on the host's own path is behind, and a client anywhere
            # else has diverged, which is the failure worth catching.
            if entry["b"][0] not in theirs["b"]:
                on_the_path = False
        time.sleep(0.2)

    worst = max(behind_by) if behind_by else 0
    report.check(
        "the joining instance is never ahead of the host",
        min(behind_by) >= 0,
        f"lowest gap {min(behind_by)} ticks",
    )
    report.check(
        "it is at most a few ticks behind",
        worst <= 12,
        f"worst {worst} ticks behind",
    )
    report.check("every snake is the same length on both sides", lengths_agree)
    report.check(
        "the joining instance is on the host's own path, not somewhere else",
        on_the_path,
    )

    folded = client.get("/api/match/state")
    report.check(
        "no message was discarded by the fold",
        folded.get("dropped") == 0,
        f"{folded.get('dropped')} discarded of "
        f"{folded.get('applied')} applied",
    )
    report.note(
        f"applied {folded.get('applied')} messages, newest is "
        f"{folded.get('age_ms')}ms old"
    )

    heading("Input, and what a client is allowed to say")

    mine = snake_by_id(host.get("/api/match/state"), 0)
    across = "up" if mine["h"][0] != 0 else "left"
    answered = host.post("/api/match/input", {
        "heading": across, "seq": 1, "move": mine["move"],
    })
    report.check(
        "the host answers its own input at once",
        answered.get("ok") is True,
        str(answered),
    )
    report.check(
        "the answer carries the cell the head is moving into",
        isinstance(answered.get("next"), list),
        str(answered.get("next")),
    )
    report.check(
        "the answer acknowledges the sequence number",
        answered.get("ack") == 1,
        str(answered.get("ack")),
    )

    # Perpendicular to where the snake is now pointing. The lateness check used
    # to run before the reversal check and hid it; now a reversal is refused on
    # its own merits, so asking for one here would test the wrong thing.
    mine = snake_by_id(host.get("/api/match/state"), 0)
    sideways = "up" if mine["h"][0] != 0 else "left"
    late = host.post("/api/match/input", {
        "heading": sideways, "seq": 2, "move": 0,
    })
    report.check(
        "an input tied to a move the snake has left is refused",
        late.get("ok") is False and late.get("reason") == "too_late",
        str(late),
    )
    report.check(
        "the refusal says which move to aim at, so the press can be resent",
        isinstance(late.get("move"), int) and late["move"] > 0,
        str(late.get("move")),
    )

    resent = host.post("/api/match/input", {
        "heading": sideways, "seq": 3, "move": late.get("move"),
    })
    report.check(
        "and aiming at it lands",
        resent.get("ok") is True,
        str(resent),
    )

    mine = snake_by_id(host.get("/api/match/state"), 0)
    backwards = {
        (1, 0): "left", (-1, 0): "right", (0, 1): "up", (0, -1): "down",
    }[tuple(mine["h"])]
    reversal = host.post("/api/match/input", {
        "heading": backwards, "seq": 3, "move": mine["move"],
    })
    report.check(
        "a reversal into its own neck is refused",
        reversal.get("ok") is False and reversal.get("reason") == "reversal",
        str(reversal),
    )

    before = snake_by_id(host.get("/api/match/state"), 1)
    client.post("/api/match/input", {
        "heading": "up",
        "seq": 1,
        "move": before["move"],
        "score": 9999,
        "length": 80,
        "kills": 50,
    })
    time.sleep(0.5)
    after = snake_by_id(host.get("/api/match/state"), 1)
    report.check(
        "a client cannot report a score",
        after["score"] == before["score"],
        f"{before['score']} became {after['score']}",
    )
    report.check(
        "a client cannot report a length",
        after["len"] == before["len"],
        f"{before['len']} became {after['len']}",
    )
    report.check(
        "a client cannot report a kill",
        after["kills"] == before["kills"],
        f"{before['kills']} became {after['kills']}",
    )
    report.check(
        "but its input did arrive",
        after["ack"] >= 1,
        f"acknowledged {after['ack']}",
    )

    heading("Leaving and rejoining while the match runs")

    client.post("/api/room/leave")
    gone = wait_for(
        lambda: len(host.get("/api/match/state").get("snakes") or []) == 1
    )
    report.check("a snake leaves with its player", bool(gone))

    client.post("/api/room/join", {"address": f"127.0.0.1:{ws_port}"})
    rejoined = wait_for(
        lambda: (
            client.get("/api/match/state")
            if len(client.get("/api/match/state").get("snakes") or []) == 2
            else None
        ),
        timeout=5,
    )
    report.check("the same player can rejoin mid-match", bool(rejoined))

    if rejoined:
        bodies = all(entry["b"] for entry in rejoined["snakes"])
        report.check("both snakes arrive with a body", bodies)
        report.check(
            "rejoining costs no discarded messages",
            rejoined.get("dropped") == 0,
            f"{rejoined.get('dropped')} discarded",
        )

    heading("Back to the lobby")

    host.post("/api/room/rematch")
    time.sleep(0.6)

    report.check(
        "the host has no match left",
        host.get("/api/match/state").get("phase") == "none",
    )
    report.check(
        "the joining instance has none either",
        client.get("/api/match/state").get("phase") == "none",
    )

    room = host.get("/api/room")["room"]
    report.check(
        "the room is waiting again",
        room["phase"] == "waiting",
        room["phase"],
    )

    ready_flags = {
        player["username"]: player["ready"] for player in room["players"]
    }
    report.check(
        "another match has to be agreed to, not assumed",
        ready_flags.get("bob") is False,
        str(ready_flags),
    )

    heading("A grid of arenas")

    # One cell space read as several arenas, so the things worth checking from
    # outside are the reading and the crossing: that the layout reaches the
    # wire, that both sides agree on it, that a boundary is not a wall, and
    # that a body is allowed to be in two arenas at once.
    grid_rules = dict(ROOM_RULES)
    grid_rules.update({
        "layout": "2x1",
        "arena_width": 20,
        "arena_height": 20,
        "base_speed": 10,
    })

    laid_out = host.post("/api/room/rules", {"rules": grid_rules})
    report.check(
        "a layout of two arenas is accepted",
        (laid_out.get("rules") or {}).get("layout") == "2x1",
        str(laid_out)[:200],
    )

    # Walls and a grid together. The editor locks this combination, but the
    # editor is not the only way rules reach a room: an old saved setup, a
    # different build, or anything posting to the API directly can ask for it.
    # What arrives has to be settled rather than played as sent, or the screen
    # and the game would be describing different matches.
    walled = host.post("/api/room/rules", {"rules": dict(
        grid_rules, edge_behaviour="walls"
    )})
    report.check(
        "a walled grid is settled rather than refused",
        (walled.get("rules") or {}).get("edge_behaviour") == "wrap",
        str(walled)[:200],
    )
    report.check(
        "and the layout it asked for survives being settled",
        (walled.get("rules") or {}).get("layout") == "2x1",
        str(walled)[:200],
    )

    client.post("/api/room/ready", {"ready": True})
    restarted = wait_for(
        lambda: host.post("/api/room/start") if host.get(
            "/api/room"
        )["room"]["can_start"] else None
    )
    report.check(
        "a match on the grid starts",
        bool(restarted) and restarted.get("ok") is True,
        str(restarted)[:200],
    )

    if restarted and restarted.get("ok"):
        wait_for(
            lambda: host.get("/api/match/state").get("phase") == "running",
            timeout=6,
        )

        state = host.get("/api/match/state")
        arena = state.get("arena") or {}
        report.check(
            "the cell space is the layout times one arena",
            (arena.get("w"), arena.get("h")) == (40, 20),
            str(arena),
        )
        report.check(
            "and the layout travels with it",
            (arena.get("aw"), arena.get("ah"), arena.get("cols"),
             arena.get("rows")) == (20, 20, 2, 1),
            str(arena),
        )
        report.check(
            "every snake says which arena it is in",
            all("a" in snake for snake in state.get("snakes") or []),
            str([snake.get("a") for snake in state.get("snakes") or []]),
        )
        rolled = state.get("map") or []
        report.check(
            "the arenas are rolled up for the minimap",
            len(rolled) == 2,
            str(rolled),
        )
        report.check(
            "and the roll-up accounts for everybody who is alive",
            sum(rolled) == len([
                snake for snake in state.get("snakes") or []
                if snake.get("alive")
            ]),
            str(rolled),
        )
        report.check(
            "the leaderboard says which arena each player is in",
            all("arena" in row for row in state.get("standings") or []),
            str(state.get("standings"))[:160],
        )
        report.check(
            "the joining instance is told the same layout",
            (client.get("/api/match/state").get("arena") or {}) == arena,
            str(client.get("/api/match/state").get("arena")),
        )

        # Steered, not waited for. Snakes spawn pointing whichever way fits and
        # a 2x1 layout wraps top to bottom inside one arena, so a snake left
        # alone can run for a minute without ever meeting the boundary this is
        # here to check. Both are pushed sideways instead, and each press is
        # aimed at the move the host says the snake is on.
        # Player ids are not guessed. This is the second match of the run and
        # the joining instance has left and come back since the first one, so
        # whichever id it had then is not necessarily the id it has now.
        room_now = host.get("/api/room")["room"]
        host_player = room_now["host_id"]
        other_player = next(
            player["id"] for player in room_now["players"]
            if player["id"] != host_player
        )

        crossed = set()
        spanning = False
        leaked = False
        blind = False
        apart = False
        complete_board = False
        pairs = set()
        opening = {}
        sequence = 100
        deadline = time.time() + 8.0

        while time.time() < deadline:
            frame = client.get("/api/match/state")

            # Opposite ways on purpose. Steered the same way they cross the
            # boundary together and are never in different arenas at the same
            # moment, which is the only moment scoping can be observed in.
            for instance, player_id, way in (
                (host, host_player, "right"), (client, other_player, "left")
            ):
                snake = snake_by_id(frame, player_id)
                if not snake or not snake.get("alive"):
                    continue
                if snake["h"][0] == 0:
                    sequence += 1
                    instance.post("/api/match/input", {
                        "heading": way,
                        "seq": sequence,
                        "move": snake.get("move", 0),
                    })

            for snake in frame.get("snakes") or []:
                where = snake.get("a")
                opening.setdefault(snake["id"], where)
                if where != opening[snake["id"]]:
                    crossed.add(snake["id"])
                sides = {cell[0] // 20 for cell in snake.get("b") or []}
                if len(sides) > 1:
                    spanning = True

            # Scoping, checked from both ends at once. Each instance is asked
            # what it can see, and every body it was sent has to have a cell in
            # the arena that instance's own player is standing in.
            for instance, player_id in (
                (host, host_player), (client, other_player)
            ):
                seen = instance.get("/api/match/state")
                mine = snake_by_id(seen, player_id)
                if not mine:
                    continue

                if not mine.get("b"):
                    blind = True

                for snake in seen.get("snakes") or []:
                    body = snake.get("b") or []
                    if body and not any(
                        cell[0] // 20 == mine.get("a") for cell in body
                    ):
                        leaked = True

                board = {row["id"] for row in seen.get("standings") or []}
                if board == {host_player, other_player}:
                    complete_board = True

            here = snake_by_id(frame, host_player)
            there = snake_by_id(frame, other_player)
            if here and there:
                pairs.add((here.get("a"), there.get("a")))
                if here.get("a") != there.get("a"):
                    apart = True

            if crossed and spanning and apart and complete_board:
                break
            time.sleep(0.05)

        report.check(
            "the joining instance has a roll-up of its own",
            len(client.get("/api/match/state").get("map") or []) == 2,
            str(client.get("/api/match/state").get("map")),
        )
        report.check(
            "a snake crosses a boundary rather than stopping at one",
            bool(crossed),
            "no snake changed arena in six seconds",
        )
        report.check(
            "and nobody was ever sent a body from an arena they were not in",
            not leaked,
            str(leaked),
        )
        report.check(
            "while still being told who else is playing",
            apart and complete_board,
            ("arenas seen: " + str(sorted(pairs))) if not apart
            else "somebody dropped off a leaderboard",
        )
        report.check(
            "and never losing sight of their own snake",
            not blind,
            "an instance was sent no body for its own player",
        )
        report.check(
            "and its body is in both arenas while it does",
            spanning,
            "no body was ever seen spanning the boundary",
        )

        host.post("/api/room/rematch")
        time.sleep(0.6)

    heading("Poisons and pickups")

    # Driven rather than asserted. Both instances are put in a room with a
    # crowded floor and left to run into things, and what is checked is that the
    # items reach both ends, that they are consumed rather than accumulating,
    # and that whatever a snake picks up is reported with a countdown that
    # actually counts down.
    item_rules = dict(ROOM_RULES)
    item_rules.update({
        "layout": "1x1",
        "arena_width": 20,
        "arena_height": 20,
        "base_speed": 10,
        "poisons": "high",
        "pickups": "high",
        "effect_duration": 6,
    })

    stocked = host.post("/api/room/rules", {"rules": item_rules})
    report.check(
        "a room with the items turned up is accepted",
        (stocked.get("rules") or {}).get("poisons") == "high",
        str(stocked)[:200],
    )

    client.post("/api/room/ready", {"ready": True})
    third = wait_for(
        lambda: host.post("/api/room/start") if host.get(
            "/api/room"
        )["room"]["can_start"] else None
    )
    report.check(
        "a match with items starts",
        bool(third) and third.get("ok") is True,
        str(third)[:200],
    )

    if third and third.get("ok"):
        wait_for(
            lambda: host.get("/api/match/state").get("phase") == "running",
            timeout=6,
        )

        opening = host.get("/api/match/state")
        floor = opening.get("items") or []
        report.check(
            "there are items on the floor",
            bool(floor),
            str(floor)[:160],
        )
        report.check(
            "each one says which kind it is",
            all(len(entry) == 3 and entry[2] for entry in floor),
            str(floor)[:160],
        )
        # One arena, so both ends are looking at the same floor. On a layout
        # they would not be, and the two lists differing would be scoping
        # working rather than anything being wrong.
        report.check(
            "the joining instance sees the same floor",
            (client.get("/api/match/state").get("items") or []) == floor,
            str(client.get("/api/match/state").get("items"))[:160],
        )

        # Watch a whole run of it. Both are steered in circles so they cover
        # ground and meet things, and every distinct effect seen is recorded
        # along with whether its countdown was ever observed falling.
        # Read again rather than carried over from the previous match. The
        # names above are set inside a block that only runs if that match
        # started, and an id that was right one match ago is exactly the
        # assumption that made the steering silently do nothing before.
        room_here = host.get("/api/room")["room"]
        first_player = room_here["host_id"]
        second_player = next(
            player["id"] for player in room_here["players"]
            if player["id"] != first_player
        )

        seen_effects = {}
        falling = set()
        stock = []
        eaten = False
        way = ["right", "down", "left", "up"]
        sequence = 500
        deadline = time.time() + 10.0

        while time.time() < deadline:
            frame = host.get("/api/match/state")
            stock.append(len(frame.get("items") or []))

            for instance, player_id in (
                (host, first_player), (client, second_player)
            ):
                snake = snake_by_id(frame, player_id)
                if not snake or not snake.get("alive"):
                    continue

                # Steered at the nearest item rather than driven in circles.
                # Whether a snake wanders into something in ten seconds is
                # chance, and this check failed one run and passed the next
                # before it was aimed. A check that depends on luck is worse
                # than no check.
                # Not named "heading": that is the section title function,
                # and a local of the same name shadows it for the whole of
                # this one, which broke every heading in the run.
                aim = toward_item(snake, frame.get("items") or [])
                if aim is None:
                    aim = way[(sequence // 3) % 4]

                sequence += 1
                instance.post("/api/match/input", {
                    "heading": aim,
                    "seq": sequence,
                    "move": snake.get("move", 0),
                })

                for kind, left in (snake.get("fx") or {}).items():
                    eaten = True
                    if kind in seen_effects and left < seen_effects[kind]:
                        falling.add(kind)
                    seen_effects[kind] = left

            time.sleep(0.05)

        report.check(
            "somebody picked something up",
            eaten,
            "nothing was eaten in ten seconds of driving",
        )
        report.check(
            "and what they picked up counts down",
            bool(falling),
            "an effect was reported but its countdown never fell: "
            + str(seen_effects),
        )
        report.check(
            "the floor is restocked rather than emptying",
            bool(stock) and min(stock) > 0,
            "item counts seen: " + str(sorted(set(stock))),
        )
        report.check(
            "and does not accumulate either",
            bool(stock) and max(stock) <= len(floor) * 2,
            "item counts seen: " + str(sorted(set(stock)))
            + ", opened with " + str(len(floor)),
        )

        host.post("/api/room/rematch")
        time.sleep(0.6)

    heading("Computer players")

    # Bots are the first thing in this game that plays without anybody holding
    # the keys, so the thing worth checking from outside is that they behave
    # like players rather than like a special case: they appear in the room's
    # match, they show up on both leaderboards, they move, and they never do
    # anything a human could not.
    bot_rules = dict(ROOM_RULES)
    bot_rules.update({
        "layout": "1x1",
        "arena_width": 24,
        "arena_height": 24,
        "base_speed": 10,
        "bot_count": 3,
        "bot_difficulty": "medium",
    })

    stocked = host.post("/api/room/rules", {"rules": bot_rules})
    report.check(
        "a room can ask for bots",
        (stocked.get("rules") or {}).get("bot_count") == 3,
        str(stocked)[:200],
    )

    client.post("/api/room/ready", {"ready": True})
    fourth = wait_for(
        lambda: host.post("/api/room/start") if host.get(
            "/api/room"
        )["room"]["can_start"] else None
    )
    report.check(
        "a match with bots starts",
        bool(fourth) and fourth.get("ok") is True,
        str(fourth)[:200],
    )

    if fourth and fourth.get("ok"):
        wait_for(
            lambda: host.get("/api/match/state").get("phase") == "running",
            timeout=6,
        )

        opening = host.get("/api/match/state")
        bots_seen = [
            snake for snake in opening.get("snakes") or [] if snake.get("bot")
        ]
        report.check(
            "three of them are in the match",
            len(bots_seen) == 3,
            str([snake.get("name") for snake in opening.get("snakes") or []]),
        )
        report.check(
            "each has a name and a colour of its own",
            len({snake["name"] for snake in bots_seen}) == 3
            and len({snake["c"] for snake in bots_seen}) == 3,
            str([(snake["name"], snake["c"]) for snake in bots_seen]),
        )
        report.check(
            "and a body on the board",
            all(snake.get("b") for snake in bots_seen),
            str([len(snake.get("b") or []) for snake in bots_seen]),
        )
        report.check(
            "the joining instance sees them too",
            len([
                snake for snake in
                client.get("/api/match/state").get("snakes") or []
                if snake.get("bot")
            ]) == 3,
            "",
        )
        report.check(
            "and they are on the leaderboard",
            len([
                row for row in
                client.get("/api/match/state").get("standings") or []
                if row.get("bot")
            ]) == 3,
            str(client.get("/api/match/state").get("standings"))[:200],
        )

        # Watched rather than asserted about. Whether a bot plays well is not
        # something to pin from out here; whether it is playing at all is.
        opening_heads = {
            snake["id"]: tuple(snake["b"][0]) for snake in bots_seen
        }
        turned = set()
        moved = set()
        reversed_by = set()
        facing = {snake["id"]: tuple(snake["h"]) for snake in bots_seen}
        deadline = time.time() + 6.0

        while time.time() < deadline:
            frame = host.get("/api/match/state")
            for snake in frame.get("snakes") or []:
                if not snake.get("bot") or not snake.get("b"):
                    continue

                here = tuple(snake["b"][0])
                if here != opening_heads.get(snake["id"]):
                    moved.add(snake["id"])

                now_facing = tuple(snake["h"])
                was = facing.get(snake["id"])
                if was and now_facing != was:
                    turned.add(snake["id"])
                    if (now_facing[0] == -was[0]
                            and now_facing[1] == -was[1]
                            and len(snake["b"]) > 1):
                        reversed_by.add(snake["id"])
                facing[snake["id"]] = now_facing

            if len(moved) == 3 and turned:
                break
            time.sleep(0.05)

        report.check(
            "they move without anybody holding the keys",
            len(moved) == 3,
            "moved: " + str(sorted(moved)),
        )
        report.check(
            "and steer rather than going straight until they hit something",
            bool(turned),
            "none of the three ever turned",
        )
        report.check(
            "never turning back on themselves, which a player cannot do either",
            not reversed_by,
            "reversed: " + str(sorted(reversed_by)),
        )

        host.post("/api/room/rematch")
        time.sleep(0.6)

    heading("Themes")

    # The theme is the host's, not each player's. Checked from out here because
    # that is the only place the distinction is visible: it has to be in the
    # rules both instances are given, or two people in one room would be looking
    # at different arenas.
    themed = host.post("/api/room/rules", {"rules": dict(
        ROOM_RULES, theme_set="mono"
    )})
    report.check(
        "a room can choose a theme",
        (themed.get("rules") or {}).get("theme_set") == "mono",
        str(themed)[:200],
    )
    # The rules ride beside the room in the payload rather than inside it.
    report.check(
        "and the joining instance is told which one",
        (client.get("/api/room").get("rules") or {}).get("theme_set") == "mono",
        str(client.get("/api/room").get("rules"))[:200],
    )

    refused = host.post("/api/room/rules", {"rules": dict(
        ROOM_RULES, theme_set="chartreuse"
    )})
    report.check(
        "an unknown theme is refused rather than sent on",
        refused.get("ok") is not True,
        str(refused)[:200],
    )
    report.check(
        "and the room keeps the theme it had",
        (host.get("/api/room").get("rules") or {}).get("theme_set") == "mono",
        str(host.get("/api/room").get("rules"))[:200],
    )

    host.post("/api/room/rules", {"rules": dict(ROOM_RULES)})

    heading("A chosen room code")

    # A generated code carries the host's address. A chosen one carries nothing
    # and is resolved by finding the room broadcasting it, so the only place to
    # check it is a running room with discovery on.
    coded = host.post("/api/room/rules", {"rules": dict(
        ROOM_RULES, visibility="public", room_code="tiger"
    )})
    report.check(
        "a room can choose its own code",
        (coded.get("rules") or {}).get("room_code") == "tiger",
        str(coded)[:200],
    )

    lobby = host.get("/api/room")
    report.check(
        "the host is shown the chosen code as well as the generated one",
        (lobby.get("chosen_code") == "TIGER") and bool(lobby.get("code")),
        f"chosen={lobby.get('chosen_code')} "
        f"generated={lobby.get('code')}",
    )

    private = host.post("/api/room/rules", {"rules": dict(
        ROOM_RULES, visibility="private", room_code="tiger"
    )})
    report.check(
        "a private room is accepted but advertises no chosen code",
        private.get("ok") is not False
        and host.get("/api/room").get("chosen_code") == "",
        str(host.get("/api/room").get("chosen_code")),
    )

    host.post("/api/room/rules", {"rules": dict(ROOM_RULES)})
    report.check(
        "clearing it leaves the generated code working",
        bool(host.get("/api/room").get("code"))
        and host.get("/api/room").get("chosen_code") == "",
        str(host.get("/api/room").get("code")),
    )

    spectator_checks(host, client, watcher, ws_port, report)
    hardening_checks(host, watcher, ws_port, report)

    heading("Nothing is left running")

    host.post("/api/room/leave")
    freed = wait_for(lambda: port_is_free(ws_port), timeout=6)
    report.check(
        "the room's listener port is released",
        bool(freed),
        f"tcp {ws_port} is still bound",
    )


# -- entry point ------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Play a whole match between two local instances and check it."
    )
    parser.add_argument(
        "--keep-logs",
        action="store_true",
        help="print where each instance's log went, instead of only on failure",
    )
    options = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # Neutral prefix on purpose. The name-scan rule covers this file too,
    # and a temporary directory is not worth importing branding for.
    root = tempfile.mkdtemp(prefix="match-check-")
    host = Instance("the host", HTTP_A, os.path.join(root, "a"))
    client = Instance("the joining instance", HTTP_B, os.path.join(root, "b"))
    watcher = Instance("the watching instance", HTTP_C, os.path.join(root, "c"))
    instances = (host, client, watcher)
    report = Report()

    print("Starting three instances. This takes a few seconds.", flush=True)

    try:
        for instance in instances:
            instance.start()
        for instance in instances:
            instance.wait_until_ready()
    except RuntimeError as error:
        print(f"\n{error}")
        for instance in instances:
            instance.stop()
        return 1

    try:
        run_checks(host, client, watcher, report)
    except Exception as error:  # noqa: BLE001
        print(f"\nThe run stopped early: {error!r}")
        report.check("the run finished", False, repr(error))
    finally:
        for instance in instances:
            instance.stop()

    failures = report.failures()

    print()
    if failures:
        print(f"{len(failures)} of {len(report.results)} checks failed:")
        for description, _, detail in failures:
            print(f"  - {description}")
            if detail:
                print(f"      {detail}")
        print()
        print("Logs from this run:")
        for instance in instances:
            print(f"  {instance.log_path()}")
        return 1

    print(f"All {len(report.results)} checks passed.")
    if options.keep_logs:
        for instance in instances:
            print(f"  {instance.log_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
