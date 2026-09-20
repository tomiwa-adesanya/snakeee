"""Hardening: the tick loop, the throttles, the caps, the names, the headers.

Every one of these is here because something was measured rather than reasoned
about, and three of them are here because the first attempt was wrong:

  - A strike per message over the bucket closed a connection on one clump from
    a stalled link, which is the thing the design says to fold rather than
    punish. The strike is throttled now, and there is a test for the clump.
  - The input rate was first set from "a player sends one per frame". The real
    client resends a refused press up to three times inside 260ms, so a held
    key produces several times that. There is a test at the rate the client can
    actually reach.
  - The message cap could not simply be lowered to the figure originally
    chosen, because the host's own keyframe on a large layout is bigger than
    it. The two directions have separate limits and there is a test for each.

The parity test at the end is the guard against the two engines drifting apart,
and it is the one test in this file that will still be earning its place in a
year.
"""

import asyncio
import json
import time
from collections import deque

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app import create_app
from utils import ports
from utils.game.arena import FOOD, SNAKE
from utils.game.engine import FAULT, PHASE_OVER, SoloEngine, TickLoop
from utils.game.match import MatchEngine
from utils.game.rules import RuleSet
from utils.net import protocol
from utils.net.client import RoomClient
from utils.net.room import Room, clean_username
from utils.net.server import (
    BAD_MESSAGE_STRIKES,
    CONNECTIONS_PER_WINDOW,
    INPUT_RATE,
    Bucket,
    HostServer,
)

BASE_PORT = 46500


def free_port(offset: int) -> int:
    port = BASE_PORT + offset
    while not ports.is_free(port, "127.0.0.1"):
        port += 1
    return port


# -- the tick loop ----------------------------------------------------------


def test_an_exception_in_the_tick_ends_the_run_rather_than_freezing_it():
    """The worst failure in the game, and it used to be silent.

    The thread died where it stood. In single player that is a board that stops
    moving; in a room the host's stream loop carries on broadcasting to its own
    schedule, so every client sees a frozen arena and a healthy link.
    """
    engine = SoloEngine(RuleSet())
    engine.start()

    real = engine.step
    seen = {"ticks": 0}

    def explodes():
        seen["ticks"] += 1
        if seen["ticks"] > 3:
            raise RuntimeError("deliberate")
        real()

    engine.step = explodes

    loop = TickLoop(engine)
    loop.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and loop.running:
        time.sleep(0.02)

    assert loop.running is False, "the loop kept running after a fault"
    assert engine.phase == PHASE_OVER
    assert engine.fault == FAULT

    # And the frontend is told, rather than being left to guess at a cause.
    assert engine.snapshot()["fault"] == FAULT
    assert engine.result()["cause"] == FAULT

    loop.stop()


def test_a_match_that_faults_ends_with_a_reason_the_results_screen_can_name():
    engine = MatchEngine(RuleSet(), [(0, "a", "#3ecf8e"), (1, "b", "#58a6ff")])
    engine.start()

    engine.fail(RuntimeError("deliberate"))

    assert engine.phase == "over"
    assert engine.outcome["reason"] == FAULT
    assert "standings" in engine.outcome


def test_an_ordinary_run_carries_no_fault():
    """The other half of it. A death is not a fault."""
    engine = SoloEngine(RuleSet())
    engine.start()
    assert engine.fault is None
    assert engine.snapshot()["fault"] is None


# -- the bucket -------------------------------------------------------------


def test_the_bucket_refills_at_its_rate():
    bucket = Bucket(rate=10.0, burst=5.0)
    now = 1000.0

    # The burst, and then nothing.
    assert [bucket.take(now) for _ in range(5)] == [True] * 5
    assert bucket.take(now) is False

    # A tenth of a second buys exactly one more at ten a second.
    assert bucket.take(now + 0.1) is True
    assert bucket.take(now + 0.1) is False

    # And it never fills past the burst, however long it sits idle.
    assert [bucket.take(now + 600) for _ in range(5)] == [True] * 5
    assert bucket.take(now + 600) is False


# -- the throttles, over a real socket --------------------------------------


@pytest.fixture
def hosted(request):
    room = Room(RuleSet(player_cap=8), "Host", "#3ecf8e")
    server = HostServer(room, port=free_port(request.node.name.__len__() % 40))
    server.start()
    time.sleep(0.4)
    yield room, server
    server.stop()


async def _hello(connection, name, spectate=False):
    await connection.send(json.dumps(
        protocol.hello(name, "#58a6ff", spectate)
    ))
    await asyncio.sleep(0.3)
    while True:
        try:
            await asyncio.wait_for(connection.recv(), timeout=0.15)
        except (asyncio.TimeoutError, TimeoutError, ConnectionClosed):
            return


async def _send_until_closed(connection, payloads, pause):
    """Send, and report how many the socket took before it refused.

    Sleeping between sends is not padding. A tight loop never yields, so the
    close frame is never processed and the connection looks open long after the
    host has closed it. The first version of this measurement got that wrong
    and reported that nothing was being closed at all.
    """
    sent = 0
    for payload in payloads:
        try:
            await connection.send(payload)
        except ConnectionClosed:
            break
        sent += 1
        await asyncio.sleep(pause)

    rejects = []
    try:
        while True:
            raw = await asyncio.wait_for(connection.recv(), timeout=0.4)
            message = json.loads(raw)
            if message.get("type") == protocol.REJECT:
                rejects.append(message.get("reason"))
    except (asyncio.TimeoutError, TimeoutError, ConnectionClosed):
        pass

    return sent, rejects, connection.state.name != "OPEN"


def test_malformed_frames_close_the_connection_after_a_few(hosted):
    _room, server = hosted

    async def run():
        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await _hello(connection, "Garbage")
            return await _send_until_closed(
                connection, ["not json at all"] * 25, 0.01
            )

    sent, rejects, closed = asyncio.run(run())

    assert closed is True
    assert sent <= BAD_MESSAGE_STRIKES + 2
    assert rejects[-1] == protocol.TOO_MANY_BAD_MESSAGES


def test_a_client_that_will_not_stop_is_closed(hosted):
    _room, server = hosted

    async def run():
        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await _hello(connection, "Nonstop")
            payloads = [
                json.dumps(protocol.input_intent(i, "left", 0))
                for i in range(4000)
            ]
            return await _send_until_closed(connection, payloads, 0.0002)

    sent, rejects, closed = asyncio.run(run())

    assert closed is True
    assert sent < 4000
    assert protocol.FLOODING in rejects


def test_a_stalled_link_delivering_clumps_is_not_punished(hosted):
    """The case the first version of this got wrong.

    A clump is one event. Charging a strike per message over the bucket turned
    a single clump of sixty into forty strikes and closed the connection, which
    is the opposite of the rule elsewhere that says to fold a clump rather than
    punish it.
    """
    _room, server = hosted

    async def run():
        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await _hello(connection, "Stally")
            for _ in range(4):
                for index in range(60):
                    await connection.send(json.dumps(
                        protocol.input_intent(index, "left", 0)
                    ))
                    await asyncio.sleep(0.0005)
                await asyncio.sleep(1.2)
            return connection.state.name == "OPEN"

    assert asyncio.run(run()) is True


def test_the_rate_the_real_client_can_reach_is_not_refused(hosted):
    """A held key, resent three times inside 260ms, is around 90 a second.

    The limit has to sit above what the client can produce, not above what a
    player seems likely to press. The first number chosen here was below it.
    """
    _room, server = hosted
    assert INPUT_RATE > 90, "the limit is below what the client can emit"

    async def run():
        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await _hello(connection, "Masher")
            start = time.monotonic()
            index = 0
            while time.monotonic() - start < 2.0:
                await connection.send(json.dumps(
                    protocol.input_intent(index, "left", 0)
                ))
                index += 1
                await asyncio.sleep(1 / 90.0)
            return index, connection.state.name == "OPEN"

    sent, open_still = asyncio.run(run())

    assert open_still is True, f"closed after {sent} messages at 90 a second"


def test_connections_from_one_address_are_limited(hosted):
    """And the refusal says why.

    Driven through RoomClient rather than a raw socket, because the first
    version of this limit closed the connection before the client had reached
    its first receive: the reject was written to a socket nobody was reading
    and the person was told "the host closed the connection during the
    handshake". A raw socket test would not have noticed, because a raw socket
    can be made to read at exactly the right moment. The real client cannot.
    """
    room, server = hosted
    room.rules = RuleSet(player_cap=12)

    # A distinct colour each, so a refusal for a taken colour cannot be mistaken
    # for the refusal being tested. The limit fires before the room is touched,
    # so the ones past it are refused whatever colour they asked for.
    palette = [
        "#ef6461", "#f0b950", "#58a6ff", "#c878f0", "#f08fb4",
        "#5b7cfa", "#6fd8d0", "#d9d36a",
    ]

    outcomes = []
    clients = []
    try:
        for index in range(CONNECTIONS_PER_WINDOW + 3):
            client = RoomClient()
            clients.append(client)
            outcomes.append(client.join(
                f"127.0.0.1:{server.port}", f"C{index}",
                palette[index % len(palette)],
            ))
    finally:
        for client in clients:
            client.leave()

    admitted = [one for one in outcomes if one.get("ok")]
    refused = [one for one in outcomes if not one.get("ok")]

    assert len(admitted) <= CONNECTIONS_PER_WINDOW
    assert refused, "nothing was refused"
    for one in refused:
        assert one["reason"] == protocol.TOO_MANY_ATTEMPTS, one
        assert "attempts" in one["text"].lower()


# -- the caps ---------------------------------------------------------------


def test_the_two_directions_have_different_caps_and_both_are_needed():
    """Measured, not assumed.

    The largest real client message is a full rules payload. The largest host
    message is a keyframe on a big grid, and it is larger than four kilobytes,
    so a single shared cap at four kilobytes would have stopped the host
    encoding its own snapshot.
    """
    biggest_client = protocol.encode(protocol.rules(
        RuleSet(layout="2x2", arena_width=120, arena_height=120,
                room_name="x" * 24).to_dict()
    ))
    assert len(biggest_client) < protocol.MAX_CLIENT_BYTES

    # A client frame above the inbound cap is refused before it is parsed.
    oversized = json.dumps({"type": protocol.INPUT, "pad": "x" * 5000})
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(oversized, protocol.MAX_CLIENT_BYTES)

    # The same frame is fine against the wider ceiling, which is what a client
    # reading a host uses.
    assert protocol.decode(oversized)["type"] == protocol.INPUT

    engine = MatchEngine(
        RuleSet(layout="2x2", arena_width=120, arena_height=120,
                player_cap=12, food_density="dense"),
        [(i, f"player{i:02d}", "#3ecf8e") for i in range(12)],
    )
    engine.start()
    for _ in range(300):
        engine.step()

    largest = max(
        len(protocol.encode(engine.keyframe(stream)))
        for stream in engine.streams()
    )
    engine.stop()

    assert largest > protocol.MAX_CLIENT_BYTES, (
        "if this ever fails the two caps could be merged, which would be "
        "simpler: check the measurement before doing it"
    )
    assert largest < protocol.MAX_MESSAGE_BYTES


# -- names ------------------------------------------------------------------


def test_a_name_keeps_its_letters_and_loses_its_control_characters():
    # A bidirectional override rearranges every other name on the line, on
    # everybody else's screen.
    assert clean_username("a\u202eb\x07c   d") == "abc d"

    # And an ordinary name with an accent in it is left alone. The repository
    # ASCII rule is about source files, not about what a player may call
    # themselves.
    assert clean_username("Jos\u00e9  Ana") == "Jos\u00e9 Ana"


def test_a_room_name_is_cleaned_the_same_way():
    """It is drawn on every machine on the subnet, before anybody joins."""
    ruleset = RuleSet.from_dict({"room_name": "bad\u202ename\x00here"})
    assert ruleset.room_name == "badnamehere"


# -- headers ----------------------------------------------------------------


def test_every_response_carries_a_policy():
    app = create_app()
    with app.test_client() as client:
        for path in ("/", "/static/css/base.css", "/api/health"):
            response = client.get(path)
            policy = response.headers.get("Content-Security-Policy")
            assert policy, f"no policy on {path}"
            assert "default-src 'self'" in policy
            # The point of the policy is this clause. Without it an inline
            # handler works, and the next one somebody adds works too.
            assert "unsafe-inline" not in policy
            assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_nothing_in_the_page_depends_on_an_inline_handler():
    """The policy above breaks these quietly: the attribute stops running and
    the form submits, reloading the whole page. Cheaper to assert than to
    discover.
    """
    from app import STATIC_DIR

    with open(f"{STATIC_DIR}/index.html", encoding="utf-8") as handle:
        markup = handle.read()

    for attribute in ("onsubmit=", "onclick=", "onload=", "onchange=",
                      "onerror=", "oninput="):
        assert attribute not in markup, f"{attribute} will not run under the policy"


# -- the two engines agree --------------------------------------------------


PARITY_RULES = dict(
    arena_width=20, arena_height=20, base_speed=10,
    length_affects_speed=False, food_density="sparse", start_countdown=0,
    lives=1, respawn_delay=0, spawn_protection=0, severing=False,
    poisons=False, pickups=False, edge_behaviour="wrap",
)
PARITY_BODY = [(10, 10), (9, 10), (8, 10)]
PARITY_FOOD = [(13, 10), (13, 7)]
PARITY_SCRIPT = {10: "up", 19: "left"}
PARITY_SEED = 1234


def _seed_arena(engine, place_cell):
    engine.arena.reset()
    for cell in PARITY_BODY:
        place_cell(cell)
    for cell in PARITY_FOOD:
        engine.arena.food.add(cell)
        engine.arena.set(cell[0], cell[1], FOOD)


def test_the_two_engines_play_the_same_game():
    """The guard against single player and multiplayer drifting apart.

    Same rules, same seed, same starting body, same food, same headings at the
    same ticks. Any divergence in movement, wrapping, growth or scoring shows
    up as a mismatched body or score on the tick it happens.

    The seed matters and is not decoration. Without it the two engines respawn
    eaten food at different cells and the scores part company around fifty
    ticks in, for a reason that has nothing to do with the simulation.
    """
    solo = SoloEngine(RuleSet(**PARITY_RULES), seed=PARITY_SEED)
    solo.start()
    snake = solo.snake
    snake.body = deque(PARITY_BODY)
    snake.heading = (1, 0)
    snake.pending_heading = (1, 0)
    snake.accumulator = 0
    _seed_arena(solo, lambda cell: solo.arena.set(cell[0], cell[1], SNAKE))

    match = MatchEngine(
        RuleSet(**PARITY_RULES, player_cap=2),
        [(0, "a", "#3ecf8e")],
        seed=PARITY_SEED,
    )
    match.start()
    match.starts_at = 0.0
    match.step()
    other = match.snakes[0]
    other.body = deque(PARITY_BODY)
    other.heading = (1, 0)
    other.pending_heading = (1, 0)
    other.alive = True
    other.accumulator = 0
    other.arena_id = match.topology.arena_at(*PARITY_BODY[0])
    match.owner = bytearray(match.arena.width * match.arena.height)
    _seed_arena(match, lambda cell: match._occupy(cell, other.id))

    for tick in range(1, 70):
        if tick in PARITY_SCRIPT:
            solo.set_heading(PARITY_SCRIPT[tick])
            match.set_heading(0, PARITY_SCRIPT[tick])

        solo.step()
        match.step()

        assert list(snake.body) == list(other.body), f"bodies differ at tick {tick}"
        assert snake.score == other.score, f"scores differ at tick {tick}"
        assert snake.length == other.length, f"lengths differ at tick {tick}"

    # And the scenario has to have been worth running: it must have eaten,
    # turned and wrapped, or it proves the two engines agree about nothing
    # happening.
    assert snake.score >= 2, "the scripted run never ate anything"
    assert snake.head[0] > PARITY_BODY[0][0], "the scripted run never wrapped"

    match.stop()
