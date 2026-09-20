"""Rooms: the protocol, the player list, and two instances in one lobby.

Six things this file exists to hold in place:

  1. Two instances reach a shared lobby.
  2. A player who picks a taken colour is offered the free ones, so the refusal
     has to carry them.
  3. A duplicate username is refused with a clear message.
  4. Joining a full room answers "room is full" rather than timing out.
  5. A protocol version mismatch is clear on both sides.
  6. Stopping a room terminates every thread and releases the port.

Discovery adds three more:

  7. A datagram from the network is never trusted, whatever it contains.
  8. A room that stops beaconing leaves the list, and one that restarts does
     not appear twice.
  9. A private room sends nothing at all.

The socket tests use real listeners on high ports rather than mocks, because
every failure worth catching here lives in the parts a mock would replace.
"""

import asyncio
import json
import socket
import time

import pytest

from utils import ports
from utils.game.rules import RuleSet
from utils.net import discovery, protocol
from utils.net.client import RoomClient, parse_address
from utils.net.room import LOBBY_WAITING, JoinRefused, Room, clean_username
from utils.net.server import HostServer
from utils.store.profile import PLAYER_COLOURS

BASE_PORT = 46300


def free_port(offset: int) -> int:
    port = BASE_PORT + offset
    while not ports.is_free(port, "127.0.0.1"):
        port += 1
    return port


def rebinds(port: int) -> bool:
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def free_udp_port(offset: int) -> int:
    # ports.is_free probes TCP, and a TCP port being free says nothing about
    # the UDP port with the same number.
    port = BASE_PORT + 400 + offset
    while True:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("", port))
            return port
        except OSError:
            port += 1
        finally:
            probe.close()


def udp_rebinds(port: int) -> bool:
    # Deliberately without SO_REUSEADDR. The listener sets it, and on Linux two
    # UDP sockets share a port only when both do, so this fails while the
    # listener holds the port and succeeds once it has let go.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def a_payload(**overrides) -> dict:
    payload = discovery.build_payload(
        port=45881,
        name="Kitchen",
        host_username="Ada",
        players=2,
        cap=8,
        phase=LOBBY_WAITING,
        joinable=True,
    )
    payload.update(overrides)
    return payload


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# -- protocol ---------------------------------------------------------------


def test_decode_refuses_junk():
    for bad in ("", "not json", "[]", '{"no":"type"}', b"\xff\xfe"):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode(bad)


def test_decode_refuses_an_oversized_frame():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("x" * (protocol.MAX_MESSAGE_BYTES + 1))


def test_every_reject_reason_has_human_text():
    for reason in (protocol.PROTOCOL_MISMATCH, protocol.ROOM_FULL,
                   protocol.USERNAME_TAKEN, protocol.COLOUR_TAKEN,
                   protocol.MATCH_IN_PROGRESS, protocol.NOT_HOST,
                   protocol.KICKED, protocol.ROOM_CLOSED):
        message = protocol.reject(reason)
        assert message["reason"] == reason
        assert len(message["text"]) > 10


def test_host_only_types_are_declared():
    assert protocol.HOST_ONLY_TYPES <= protocol.CLIENT_TYPES
    assert protocol.RULES in protocol.HOST_ONLY_TYPES


def test_addresses_parse():
    assert parse_address("10.0.0.5") == ("10.0.0.5", ports.DEFAULT_WS_PORT)
    assert parse_address("10.0.0.5:1234") == ("10.0.0.5", 1234)
    assert parse_address("ws://10.0.0.5:1234/") == ("10.0.0.5", 1234)

    for bad in ("", "   ", "host:notaport", "host:0", "host:70000"):
        with pytest.raises(ValueError):
            parse_address(bad)


# -- room, without any sockets ---------------------------------------------


def test_the_host_occupies_a_seat():
    room = Room(RuleSet(player_cap=2), "Host", "#3ecf8e")
    assert len(room.to_dict()["players"]) == 1
    assert room.to_dict()["players"][0]["host"] is True


def test_usernames_are_cleaned_and_bounded():
    assert clean_username("  Two   Words  ") == "Two Words"
    assert len(clean_username("x" * 40)) == protocol.MAX_USERNAME
    for bad in ("", "   ", None, 12):
        with pytest.raises(JoinRefused):
            clean_username(bad)


def test_a_duplicate_username_is_refused_case_insensitively():
    room = Room(RuleSet(), "Tomiwa", "#3ecf8e")
    with pytest.raises(JoinRefused) as caught:
        room.join("TOMIWA", "#58a6ff", protocol.PROTOCOL_VERSION)
    assert caught.value.reason == protocol.USERNAME_TAKEN


def test_a_taken_colour_is_refused_with_the_free_ones():
    room = Room(RuleSet(), "Host", "#3ecf8e")
    with pytest.raises(JoinRefused) as caught:
        room.join("Other", "#3ecf8e", protocol.PROTOCOL_VERSION)

    refusal = caught.value
    assert refusal.reason == protocol.COLOUR_TAKEN
    available = refusal.extra["available_colours"]
    assert "#3ecf8e" not in available
    assert len(available) == 11


def test_a_full_room_is_refused_with_its_cap():
    room = Room(RuleSet(player_cap=2), "Host", "#3ecf8e")
    room.join("Second", "#58a6ff", protocol.PROTOCOL_VERSION)

    with pytest.raises(JoinRefused) as caught:
        room.join("Third", "#f0b950", protocol.PROTOCOL_VERSION)

    assert caught.value.reason == protocol.ROOM_FULL
    assert caught.value.extra["player_cap"] == 2


def test_a_version_mismatch_carries_both_numbers():
    room = Room(RuleSet(), "Host", "#3ecf8e")
    with pytest.raises(JoinRefused) as caught:
        room.join("Other", "#58a6ff", protocol.PROTOCOL_VERSION + 5)

    refusal = caught.value
    assert refusal.reason == protocol.PROTOCOL_MISMATCH
    assert refusal.extra["host_protocol"] == protocol.PROTOCOL_VERSION
    assert refusal.extra["client_protocol"] == protocol.PROTOCOL_VERSION + 5


def test_version_is_checked_before_anything_else():
    # A mismatched client must be told about the version even when its name and
    # colour are also unusable, because that is the only fixable thing.
    room = Room(RuleSet(player_cap=1), "Host", "#3ecf8e")
    with pytest.raises(JoinRefused) as caught:
        room.join("Host", "#3ecf8e", 99)
    assert caught.value.reason == protocol.PROTOCOL_MISMATCH


def test_only_the_host_can_change_the_rules_or_kick():
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    guest = room.join("Guest", "#58a6ff", protocol.PROTOCOL_VERSION)

    with pytest.raises(JoinRefused) as caught:
        room.set_rules(guest.id, RuleSet(base_speed=9))
    assert caught.value.reason == protocol.NOT_HOST

    with pytest.raises(JoinRefused):
        room.kick(guest.id, room.host_id)

    assert room.kick(room.host_id, guest.id) is True
    assert len(room.to_dict()["players"]) == 1


def test_the_host_cannot_be_removed_or_leave():
    room = Room(RuleSet(), "Host", "#3ecf8e")
    assert room.leave(room.host_id) is False
    assert room.kick(room.host_id, room.host_id) is False


def test_starting_waits_for_everyone():
    room = Room(RuleSet(player_cap=4, min_players=2), "Host", "#3ecf8e")
    allowed, reason = room.can_start()
    assert allowed is False
    assert "2 players" in reason

    guest = room.join("Guest", "#58a6ff", protocol.PROTOCOL_VERSION)
    allowed, reason = room.can_start()
    assert allowed is False
    assert "Guest" in reason

    room.set_ready(guest.id, True)
    assert room.can_start() == (True, None)


# -- over real sockets -----------------------------------------------------


@pytest.fixture()
def hosted():
    room = Room(RuleSet(player_cap=3, min_players=2), "Host", "#3ecf8e")
    server = HostServer(room, port=free_port(0))
    server.start()
    yield room, server
    server.stop()


def test_two_instances_reach_a_shared_lobby(hosted):
    room, server = hosted
    address = f"127.0.0.1:{server.port}"

    guest = RoomClient()
    try:
        outcome = guest.join(address, "Guest", "#58a6ff")

        assert outcome["ok"] is True
        assert outcome["player_id"] != room.host_id
        names = [player["username"] for player in outcome["room"]["players"]]
        assert names == ["Host", "Guest"]

        # And the guest sees lobby changes the host makes.
        room.set_ready(room.host_id, False)
        server.notify_lobby()
        assert wait_for(lambda: any(
            not player["ready"]
            for player in guest.state()["room"]["players"]
            if player["host"]
        ))
    finally:
        guest.leave()


def test_a_refusal_arrives_as_an_answer_not_a_timeout(hosted):
    room, server = hosted
    address = f"127.0.0.1:{server.port}"

    first = RoomClient()
    second = RoomClient()
    third = RoomClient()

    try:
        assert first.join(address, "Alice", "#58a6ff")["ok"] is True

        # Two seats used of three. The name and colour checks are reachable
        # while a seat is free; capacity is checked before either of them, which
        # is why the full-room case comes last here.
        duplicate = second.join(address, "alice", "#c878f0")
        assert duplicate["reason"] == protocol.USERNAME_TAKEN

        taken = second.join(address, "Bob", "#58a6ff")
        assert taken["ok"] is False
        assert taken["reason"] == protocol.COLOUR_TAKEN
        assert len(taken["available_colours"]) >= 1

        # One click: rejoin with a colour the host offered.
        fixed = second.join(address, "Bob", taken["available_colours"][0])
        assert fixed["ok"] is True

        full = third.join(address, "Carol", "#c878f0")
        assert full["reason"] == protocol.ROOM_FULL
        assert full["player_cap"] == 3
    finally:
        first.leave()
        second.leave()
        third.leave()


def test_a_bad_address_fails_fast_with_a_reason():
    client = RoomClient()
    started = time.monotonic()
    outcome = client.join(f"127.0.0.1:{free_port(50)}", "X", "#ef6461")

    assert outcome["ok"] is False
    assert outcome["reason"] == "connect_failed"
    assert time.monotonic() - started < 8
    client.leave()


def test_a_version_mismatch_is_answered_over_the_wire(hosted):
    _room, server = hosted

    async def lie_about_the_version():
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await connection.send(json.dumps({
                "type": "hello",
                "protocol": protocol.PROTOCOL_VERSION + 7,
                "username": "Old",
                "colour": "#ef6461",
            }))
            return json.loads(await connection.recv())

    reply = asyncio.run(lie_about_the_version())

    assert reply["type"] == protocol.REJECT
    assert reply["reason"] == protocol.PROTOCOL_MISMATCH
    assert reply["host_protocol"] == protocol.PROTOCOL_VERSION
    assert reply["client_protocol"] == protocol.PROTOCOL_VERSION + 7
    assert "same release" in reply["text"]


def test_a_client_that_opens_with_junk_is_refused(hosted):
    _room, server = hosted

    async def send_junk():
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await connection.send("this is not json")
            return json.loads(await connection.recv())

    reply = asyncio.run(send_junk())
    assert reply["reason"] == protocol.BAD_MESSAGE


def test_leaving_frees_the_seat(hosted):
    room, server = hosted
    address = f"127.0.0.1:{server.port}"

    guest = RoomClient()
    guest.join(address, "Guest", "#58a6ff")
    assert wait_for(lambda: len(room.to_dict()["players"]) == 2)

    guest.leave()
    assert wait_for(lambda: len(room.to_dict()["players"]) == 1)


def test_a_kicked_player_is_told_why(hosted):
    room, server = hosted
    address = f"127.0.0.1:{server.port}"

    guest = RoomClient()
    try:
        outcome = guest.join(address, "Guest", "#58a6ff")
        room.kick(room.host_id, outcome["player_id"])
        server.disconnect(outcome["player_id"], protocol.KICKED)

        assert wait_for(lambda: guest.state()["closed_reason"] is not None)
        assert guest.state()["closed_reason"]["reason"] == protocol.KICKED
    finally:
        guest.leave()


def test_stopping_releases_the_port_even_with_players_connected():
    port = free_port(20)
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    server = HostServer(room, port=port)
    server.start()

    clients = []
    for index, colour in enumerate(("#58a6ff", "#f0b950")):
        client = RoomClient()
        client.join(f"127.0.0.1:{port}", f"P{index}", colour)
        clients.append(client)

    started = time.monotonic()
    stopped = server.stop()
    elapsed = time.monotonic() - started

    assert stopped is True
    assert elapsed < 3.0
    assert server.running is False
    assert rebinds(port) is True

    for client in clients:
        assert client.leave() is True


def test_a_closed_room_tells_the_clients():
    port = free_port(30)
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    server = HostServer(room, port=port)
    server.start()

    guest = RoomClient()
    guest.join(f"127.0.0.1:{port}", "Guest", "#58a6ff")

    server.stop()

    assert wait_for(lambda: guest.state()["closed_reason"] is not None)
    guest.leave()


# -- discovery: the payload -------------------------------------------------


def test_a_beacon_survives_a_round_trip():
    entry = discovery.parse(discovery.encode(a_payload()), "192.168.1.24")

    assert entry["name"] == "Kitchen"
    assert entry["host"] == "Ada"
    assert entry["players"] == 2
    assert entry["cap"] == 8
    assert entry["port"] == 45881
    assert entry["joinable"] is True
    assert entry["compatible"] is True
    assert entry["target"] == "192.168.1.24:45881"


def test_the_address_comes_from_the_sender_not_the_payload():
    # A datagram must not be able to advertise a room on somebody else's
    # machine, so anything address-shaped inside it is ignored.
    datagram = discovery.encode(a_payload(address="10.0.0.1", target="10.0.0.1:1"))
    entry = discovery.parse(datagram, "192.168.1.24")

    assert entry["address"] == "192.168.1.24"
    assert entry["target"] == "192.168.1.24:45881"


def test_junk_on_the_discovery_port_is_ignored():
    # The port belongs to nobody, so all of these are ordinary traffic rather
    # than errors, and every one of them has to come back as None.
    assert discovery.parse(b"", "10.0.0.1") is None
    assert discovery.parse(b"\xff\xfe\x00", "10.0.0.1") is None
    assert discovery.parse(b"not json at all", "10.0.0.1") is None
    assert discovery.parse(b"[1,2,3]", "10.0.0.1") is None
    assert discovery.parse(b'"a string"', "10.0.0.1") is None
    assert discovery.parse(b"{}", "10.0.0.1") is None
    assert discovery.parse(b'{"magic":"OTHER1","port":1}', "10.0.0.1") is None
    assert discovery.parse(b"x" * (discovery.MAX_DATAGRAM + 1), "10.0.0.1") is None


def test_a_beacon_without_a_usable_port_is_ignored():
    for bad in [None, 0, 70000, "45881", True, -1]:
        datagram = discovery.encode(a_payload(port=bad))
        assert discovery.parse(datagram, "10.0.0.1") is None, bad


def test_a_hostile_name_cannot_reach_the_interface_intact():
    datagram = discovery.encode(
        a_payload(name="x" * 500, host="  Ada\nRoom  ")
    )
    entry = discovery.parse(datagram, "10.0.0.1")

    assert len(entry["name"]) == discovery.MAX_ROOM_NAME
    # Collapsed, not merely truncated: newlines and tabs would otherwise break
    # the row the name is drawn in.
    assert entry["host"] == "Ada Room"


def test_out_of_range_counts_fall_back_rather_than_refusing_the_room():
    entry = discovery.parse(
        discovery.encode(a_payload(players=-4, cap=9999, phase="nonsense")),
        "10.0.0.1",
    )

    assert entry["players"] == 0
    assert entry["cap"] == 0
    assert entry["phase"] == LOBBY_WAITING


def test_a_different_protocol_is_marked_incompatible():
    entry = discovery.parse(
        discovery.encode(a_payload(protocol=protocol.PROTOCOL_VERSION + 1)),
        "10.0.0.1",
    )

    # Listed but not offered. Being refused after connecting is a worse
    # experience than being told before.
    assert entry["compatible"] is False


# -- discovery: the table ---------------------------------------------------


def test_a_room_leaves_the_list_once_it_stops_beaconing():
    table = discovery.RoomTable(timeout=5.0)
    entry = discovery.parse(discovery.encode(a_payload()), "10.0.0.1")

    table.record(entry, now=100.0)
    assert len(table.listing(now=104.9)) == 1
    assert table.listing(now=105.1) == []


def test_a_restarted_host_does_not_appear_twice():
    table = discovery.RoomTable()
    first = discovery.parse(discovery.encode(a_payload(name="Old")), "10.0.0.1")
    second = discovery.parse(discovery.encode(a_payload(name="New")), "10.0.0.1")

    table.record(first, now=100.0)
    table.record(second, now=100.5)

    rooms = table.listing(now=101.0)
    assert len(rooms) == 1
    assert rooms[0]["name"] == "New"


def test_the_listing_carries_how_long_ago_a_room_was_heard():
    table = discovery.RoomTable()
    table.record(
        discovery.parse(discovery.encode(a_payload()), "10.0.0.1"), now=100.0
    )

    assert table.listing(now=101.5)[0]["age_ms"] == 1500


# -- discovery: the sockets -------------------------------------------------


def test_a_beacon_reaches_a_listener():
    port = free_udp_port(0)
    listener = discovery.Listener(port=port, ignore_origin="another-instance")
    listener.start()

    beacon = discovery.Beacon(
        lambda: a_payload(),
        port=port,
        address="127.0.0.1",
        interval=0.05,
        interfaces=["127.0.0.1"],
    )
    beacon.start()

    try:
        assert wait_for(lambda: listener.rooms())
        room = listener.rooms()[0]
        assert room["name"] == "Kitchen"
        assert room["address"] == "127.0.0.1"
        assert room["target"] == "127.0.0.1:45881"
    finally:
        beacon.stop()
        listener.stop()


def test_a_listener_ignores_its_own_beacon():
    # A broadcast reaches the machine that sent it, so without this a host
    # would find its own room in the list.
    port = free_udp_port(10)
    listener = discovery.Listener(port=port)
    listener.start()

    beacon = discovery.Beacon(
        lambda: a_payload(),
        port=port,
        address="127.0.0.1",
        interval=0.05,
        interfaces=["127.0.0.1"],
    )
    beacon.start()

    try:
        time.sleep(0.4)
        assert listener.rooms() == []
    finally:
        beacon.stop()
        listener.stop()


def test_a_private_room_sends_nothing_at_all():
    port = free_udp_port(20)
    listener = discovery.Listener(port=port, ignore_origin="another-instance")
    listener.start()

    # describe returning None is how a private room is expressed.
    beacon = discovery.Beacon(
        lambda: None,
        port=port,
        address="127.0.0.1",
        interval=0.05,
        interfaces=["127.0.0.1"],
    )
    beacon.start()

    try:
        time.sleep(0.4)
        assert listener.rooms() == []
        # Silent rather than quiet: the claim is that no socket is opened to
        # send from, which is stronger than sending nothing through one.
        assert beacon._sockets == []
    finally:
        beacon.stop()
        listener.stop()


def test_a_broken_describe_does_not_kill_the_beacon():
    def explode():
        raise RuntimeError("no room")

    beacon = discovery.Beacon(
        explode, port=free_udp_port(30), address="127.0.0.1",
        interval=0.05, interfaces=["127.0.0.1"],
    )
    beacon.start()

    try:
        time.sleep(0.2)
        assert beacon.running
    finally:
        beacon.stop()


def test_stopping_the_listener_releases_the_udp_port():
    port = free_udp_port(40)
    listener = discovery.Listener(port=port)
    listener.start()

    assert not udp_rebinds(port)
    assert listener.stop(timeout=3.0)
    assert wait_for(lambda: udp_rebinds(port), timeout=2.0)


def test_a_listener_reports_a_port_it_cannot_bind():
    port = free_udp_port(50)
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("", port))

    try:
        with pytest.raises(OSError):
            discovery.Listener(port=port).start()
    finally:
        holder.close()


# -- latency ----------------------------------------------------------------


def test_latency_is_unknown_until_something_is_measured():
    room = Room(RuleSet(), "Ada", PLAYER_COLOURS[0])
    host = room.players[room.host_id]

    assert host.latency_ms is None
    assert host.latency_best_ms is None
    assert host.to_dict()["latency_ms"] is None


def test_one_spike_does_not_carry_the_reported_latency():
    # The case this exists for: a link that is genuinely fast, with one sample
    # that paid for waking a sleeping radio.
    room = Room(RuleSet(), "Ada", PLAYER_COLOURS[0])
    for sample in [4, 3, 5, 210, 4, 3]:
        room.record_latency(room.host_id, sample)

    host = room.players[room.host_id]
    assert host.latency_ms < 10
    assert host.latency_best_ms == 3


def test_the_window_forgets_a_connection_that_has_changed():
    room = Room(RuleSet(), "Ada", PLAYER_COLOURS[0])
    for _ in range(20):
        room.record_latency(room.host_id, 3)
    for _ in range(20):
        room.record_latency(room.host_id, 180)

    # Not an average over the whole session: the number has to follow a link
    # that actually got worse.
    assert room.players[room.host_id].latency_ms == 180


def test_nonsense_latency_samples_are_dropped():
    room = Room(RuleSet(), "Ada", PLAYER_COLOURS[0])
    for bad in [None, "12", -5, object()]:
        room.record_latency(room.host_id, bad)

    assert room.players[room.host_id].latency_ms is None


# -- the app layer ---------------------------------------------------------


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from utils.store import paths

    monkeypatch.setattr(paths, "_resolve", lambda: tmp_path)

    from app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client
    app.shutdown_threads()


def test_room_state_starts_idle(client):
    payload = client.get("/api/room").get_json()
    assert payload["mode"] == "idle"
    assert payload["protocol"] == protocol.PROTOCOL_VERSION


def test_hosting_reports_an_address_and_a_seat(client):
    response = client.post("/api/room/host", json={"rules": {"player_cap": 4}})
    payload = response.get_json()

    assert payload["mode"] == "hosting"
    assert payload["port"]
    assert len(payload["room"]["players"]) == 1
    assert payload["room"]["players"][0]["host"] is True

    assert client.get("/api/room").get_json()["mode"] == "hosting"

    assert client.post("/api/room/leave").status_code == 200
    assert client.get("/api/room").get_json()["mode"] == "idle"


def test_hosting_refuses_invalid_rules(client):
    response = client.post("/api/room/host", json={"rules": {"player_cap": 99}})
    assert response.status_code == 400
    assert "player_cap" in response.get_json()["detail"]


def test_ready_and_rules_need_a_room(client):
    assert client.post("/api/room/ready", json={"ready": True}).status_code == 409
    assert client.post("/api/room/rules", json={"rules": {}}).status_code == 403
    assert client.post("/api/room/kick", json={"player": 1}).status_code == 403


def test_the_host_can_edit_the_rules_in_the_lobby(client):
    client.post("/api/room/host", json={"rules": {"base_speed": 5}})

    payload = client.post("/api/room/rules", json={"rules": {"base_speed": 9}})
    assert payload.status_code == 200
    assert payload.get_json()["rules"]["base_speed"] == 9

    client.post("/api/room/leave")


def test_joining_a_bad_address_is_a_clear_answer(client):
    response = client.post("/api/room/join", json={"address": "not a host"})
    assert response.status_code == 409
    assert response.get_json()["ok"] is False


# -- join codes ------------------------------------------------------------


def test_a_code_round_trips_for_every_private_range():
    from utils.net import joincode

    cases = [
        ("192.168.1.24", joincode.DEFAULT_PORT),
        ("192.168.0.1", joincode.DEFAULT_PORT),
        ("10.0.0.7", joincode.DEFAULT_PORT),
        ("172.20.10.3", joincode.DEFAULT_PORT),
        ("192.168.1.24", joincode.DEFAULT_PORT + 2),
        ("203.0.113.5", 8080),
    ]

    for address, port in cases:
        assert joincode.decode(joincode.encode(address, port)) == (address, port)


def test_a_home_network_code_is_five_characters():
    from utils.net import joincode

    for last in (0, 7, 24, 199, 255):
        code = joincode.encode(f"192.168.1.{last}")
        assert len(code) == 5, code


def test_codes_avoid_the_ambiguous_letters():
    from utils.net import joincode

    for banned in "ILOU":
        assert banned not in joincode.ALPHABET

    # And a code typed with them is understood anyway.
    code = joincode.encode("192.168.1.24")
    assert joincode.decode(code.replace("0", "O").replace("1", "I")) == (
        "192.168.1.24", joincode.DEFAULT_PORT
    )


def test_case_and_dashes_do_not_matter():
    from utils.net import joincode

    code = joincode.encode("10.0.0.7")
    assert joincode.decode(joincode.pretty(code).lower()) == (
        "10.0.0.7", joincode.DEFAULT_PORT
    )
    assert joincode.decode("  " + code + " ") == ("10.0.0.7", joincode.DEFAULT_PORT)


def test_a_mistyped_code_is_refused_rather_than_resolved():
    from utils.net import joincode

    code = joincode.encode("192.168.1.24")
    refused = 0
    attempts = 0

    for position in range(len(code)):
        for replacement in joincode.ALPHABET:
            if replacement == code[position]:
                continue
            attempts += 1
            candidate = code[:position] + replacement + code[position + 1:]
            try:
                decoded = joincode.decode(candidate)
            except joincode.CodeError:
                refused += 1
                continue
            # Anything accepted must at least be a different address, never a
            # silent match for the original.
            assert decoded != ("192.168.1.24", joincode.DEFAULT_PORT)

    assert refused / attempts > 0.9


def test_junk_is_not_a_code():
    from utils.net import joincode

    for junk in ("", "   ", "!!!", "hello world"):
        assert joincode.looks_like_a_code(junk) is False
        with pytest.raises(joincode.CodeError):
            joincode.decode(junk)


def test_an_address_is_not_mistaken_for_a_code():
    from utils.net import joincode

    assert joincode.looks_like_a_code("192.168.1.24") is False
    assert joincode.looks_like_a_code("192.168.1.24:45881") is False
    assert joincode.looks_like_a_code(joincode.encode("192.168.1.24")) is True


def test_hosting_publishes_a_code(client):
    payload = client.post("/api/room/host", json={"rules": {}}).get_json()

    assert payload["mode"] == "hosting"
    if payload["addresses"]:
        assert payload["code"]
        assert payload["code_pretty"]

    client.post("/api/room/leave")


def test_joining_accepts_a_code_and_reports_a_typo(client):
    from utils.net import joincode

    typo = client.post("/api/room/join", json={"address": "ZZZZZ"})
    assert typo.status_code == 400
    assert typo.get_json()["reason"] == "bad_code"

    # A well-formed code for a machine that is not listening fails as a
    # connection, which proves the code was decoded and used.
    stale = client.post("/api/room/join", json={
        "address": joincode.encode("127.0.0.1", free_port(60))
    })
    assert stale.status_code == 409
    assert stale.get_json()["reason"] == "connect_failed"


# -- the rules a room plays by ----------------------------------------------


def test_a_lobby_update_carries_the_rules():
    """A guest has to be told when the host changes them.

    They used to travel only with the welcome message, so somebody already
    sitting in the room kept the rules they joined under. The lobby showed them
    one arena and the match was played in another.
    """
    ruleset = RuleSet(arena_width=40, base_speed=8)
    message = protocol.lobby({"phase": LOBBY_WAITING}, ruleset.to_dict())

    assert message["type"] == protocol.LOBBY
    assert message["rules"]["arena_width"] == 40
    assert message["rules"]["base_speed"] == 8


def test_a_lobby_without_rules_leaves_the_ones_already_known():
    """An older host sends no rules. That is not a reason to forget them."""
    client = RoomClient()
    known = RuleSet(arena_width=40).to_dict()
    client._rules = known

    client.apply_lobby({"type": protocol.LOBBY, "room": {}, "rules": None})
    assert client.state()["rules"] == known

    fresh = RuleSet(arena_width=60).to_dict()
    client.apply_lobby({"type": protocol.LOBBY, "room": {}, "rules": fresh})
    assert client.state()["rules"]["arena_width"] == 60


def test_changing_the_rules_needs_to_be_the_host(client):
    joined = client.post("/api/room/rules", json={"rules": {"arena_width": 40}})

    assert joined.status_code == 403
    assert joined.get_json()["error"] == protocol.NOT_HOST


def test_a_refused_rule_set_names_every_field_that_is_wrong(client):
    client.post("/api/room/host", json={"rules": {}})

    response = client.post("/api/room/rules", json={"rules": {
        "win_condition": "last_standing",
        "lives": 0,
        "arena_width": 5,
    }})

    assert response.status_code == 400

    errors = response.get_json()["errors"]
    assert "arena_width" in errors
    assert "lives" in errors, "a match that cannot end was accepted"

    client.post("/api/room/leave")


def test_the_host_can_change_the_rules_from_the_lobby(client):
    client.post("/api/room/host", json={"rules": {"arena_width": 26}})

    response = client.post("/api/room/rules", json={"rules": {
        "arena_width": 40,
        "base_speed": 8,
        "win_condition": "first_to_kills",
        "kill_target": 4,
    }})

    assert response.status_code == 200

    rules = response.get_json()["rules"]
    assert rules["arena_width"] == 40
    assert rules["base_speed"] == 8
    assert rules["kill_target"] == 4

    client.post("/api/room/leave")


# -- chosen room codes -------------------------------------------------------
#
# A generated code carries the host's address, so decoding one is enough to
# reach the room. A chosen code carries nothing: it is resolved by finding the
# room broadcasting it. That single difference decides everything about how it
# behaves, including why it cannot reach a private room.


def test_a_chosen_code_rides_in_the_beacon():
    payload = discovery.build_payload(
        port=45881, name="Room", host_username="ann", players=1, cap=8,
        phase="waiting", joinable=True, code="tiger",
    )

    assert payload["code"] == "TIGER", "matched without case getting in the way"


def test_a_room_with_no_chosen_code_says_so_rather_than_omitting_it():
    payload = discovery.build_payload(
        port=45881, name="Room", host_username="ann", players=1, cap=8,
        phase="waiting", joinable=True,
    )

    assert payload["code"] == ""


def test_a_chosen_code_survives_the_wire():
    payload = discovery.build_payload(
        port=45881, name="Room", host_username="ann", players=1, cap=8,
        phase="waiting", joinable=True, code="TIGER",
    )
    entry = discovery.parse(discovery.encode(payload), "192.168.1.9")

    assert entry["code"] == "TIGER"
    assert entry["target"] == "192.168.1.9:45881"


def test_an_over_long_chosen_code_is_cut_to_fit_the_beacon():
    """The beacon is a datagram with a size limit, so this one is a trim rather

    than a refusal. The rule set is what stops a player getting here with a
    name this long.
    """
    payload = discovery.build_payload(
        port=45881, name="Room", host_username="ann", players=1, cap=8,
        phase="waiting", joinable=True, code="X" * 40,
    )

    assert len(payload["code"]) == discovery.MAX_ROOM_CODE


def test_a_beacon_from_an_older_build_has_no_code_and_still_parses():
    payload = discovery.build_payload(
        port=45881, name="Room", host_username="ann", players=1, cap=8,
        phase="waiting", joinable=True,
    )
    del payload["code"]

    entry = discovery.parse(discovery.encode(payload), "192.168.1.9")

    assert entry is not None
    assert entry["code"] == ""


# -- resolving a chosen code -------------------------------------------------


class FakeSession:
    """Just enough of a NetSession to ask it about codes."""

    def __init__(self, rooms):
        self._rooms = rooms

    def discovered(self):
        return {"listening": True, "rooms": self._rooms}


def resolve(rooms, typed):
    from app import NetSession

    return NetSession.address_for_code(FakeSession(rooms), typed)


TWO_ROOMS = [
    {"target": "192.168.1.9:45881", "code": "TIGER"},
    {"target": "192.168.1.4:45881", "code": "OTTER"},
]


def test_a_chosen_code_finds_the_room_broadcasting_it():
    assert resolve(TWO_ROOMS, "TIGER") == "192.168.1.9:45881"


def test_case_and_stray_spaces_do_not_matter():
    """It is a thing said across a room, so it has to survive being written

    down carelessly.
    """
    assert resolve(TWO_ROOMS, "  tiger ") == "192.168.1.9:45881"


def test_a_code_nobody_is_using_resolves_to_nothing():
    """None rather than an error, because this is asked about everything typed

    into that box, and most of it is a generated code or an address.
    """
    assert resolve(TWO_ROOMS, "ZEBRA") is None
    assert resolve(TWO_ROOMS, "") is None


def test_two_rooms_with_the_same_code_is_refused_rather_than_guessed():
    """Nothing stops two hosts on one network both picking TIGER. Joining

    whichever was heard from first would put a player in the wrong room with no
    way to tell.
    """
    clash = [
        {"target": "192.168.1.9:45881", "code": "TIGER"},
        {"target": "192.168.1.4:45881", "code": "TIGER"},
    ]

    assert resolve(clash, "tiger") == "ambiguous"


def test_one_room_heard_twice_is_not_a_clash():
    """Beacons repeat, and the same room can be in the list under two entries.

    Matching on the address rather than counting rows is what makes that safe.
    """
    twice = [
        {"target": "192.168.1.9:45881", "code": "TIGER"},
        {"target": "192.168.1.9:45881", "code": "TIGER"},
    ]

    assert resolve(twice, "TIGER") == "192.168.1.9:45881"


def test_a_generated_code_falls_through_to_the_decoder():
    """Chosen codes are tried first because a match against a room that is

    really there beats a checksum: the generated form carries five bits of it,
    so roughly one arbitrary string in thirty-two decodes to an address that
    belongs to nobody.
    """
    assert resolve(TWO_ROOMS, "4KQ2M") is None


# -- spectators -------------------------------------------------------------
#
# Seven things, and the first four are the ones that would make spectators worse
# than not having them:
#
#   1. A spectator takes no seat, so a full room can still be watched.
#   2. A spectator is not counted by can_start, so one cannot hold up a match
#      or, worse, let one start that has too few players in it.
#   3. Every message a player may send is refused from a spectator, and the
#      refusal is explicit rather than a branch falling through.
#   4. A spectator is routed the stream of the player it is watching and no
#      other, which is what stops watching being a way around arena scoping.
#   5. Turning the rule off refuses them.
#   6. A spectator whose player leaves is moved rather than left pointing at
#      somebody who is gone.
#   7. Ids come from one counter, so no spectator holds a player's id.


async def _answer(connection):
    """The next message that is an answer to what was just sent.

    A room broadcasts a lobby whenever anything about it changes and a ping once
    a second, so both arrive on this connection unasked. Taking the next frame
    off the wire and calling it the reply is how a test starts failing for a
    reason that has nothing to do with what it is testing.
    """
    while True:
        message = json.loads(await connection.recv())
        if message.get("type") not in (protocol.LOBBY, protocol.PING):
            return message


def test_a_spectator_takes_no_seat():
    room = Room(RuleSet(player_cap=2), "Host", "#3ecf8e")
    room.join("Second", "#58a6ff", protocol.PROTOCOL_VERSION)

    # The room is full for players.
    with pytest.raises(JoinRefused) as caught:
        room.join("Third", "#f0b950", protocol.PROTOCOL_VERSION)
    assert caught.value.reason == protocol.ROOM_FULL

    watcher = room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)
    assert watcher.id not in room.players
    assert len(room.to_dict()["players"]) == 2
    assert len(room.to_dict()["spectators"]) == 1


def test_a_spectator_does_not_hold_up_or_let_through_a_start():
    room = Room(RuleSet(player_cap=4, min_players=2), "Host", "#3ecf8e")
    room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)

    # Two members of the room, one of them a spectator, and it still cannot
    # start: the minimum counts players.
    allowed, reason = room.can_start()
    assert allowed is False
    assert "2 players" in reason

    guest = room.join("Guest", "#58a6ff", protocol.PROTOCOL_VERSION)
    room.set_ready(guest.id, True)

    # And the spectator has no ready flag to withhold.
    assert room.can_start() == (True, None)
    assert room.everyone_ready() is True


def test_ids_are_never_shared_between_players_and_spectators():
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    watcher = room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)
    guest = room.join("Guest", "#58a6ff", protocol.PROTOCOL_VERSION)

    assert len({room.host_id, watcher.id, guest.id}) == 3


def test_a_name_is_unique_across_players_and_spectators():
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)

    with pytest.raises(JoinRefused) as caught:
        room.join("watcher", "#58a6ff", protocol.PROTOCOL_VERSION)
    assert caught.value.reason == protocol.USERNAME_TAKEN

    with pytest.raises(JoinRefused) as caught:
        room.join_spectator("HOST", protocol.PROTOCOL_VERSION)
    assert caught.value.reason == protocol.USERNAME_TAKEN


def test_spectators_can_be_refused_by_the_rule():
    room = Room(RuleSet(allow_spectators=False), "Host", "#3ecf8e")
    with pytest.raises(JoinRefused) as caught:
        room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)
    assert caught.value.reason == protocol.SPECTATORS_NOT_ALLOWED


def test_spectators_are_bounded():
    from utils.net.room import MAX_SPECTATORS

    room = Room(RuleSet(), "Host", "#3ecf8e")
    for index in range(MAX_SPECTATORS):
        room.join_spectator(f"W{index}", protocol.PROTOCOL_VERSION)

    with pytest.raises(JoinRefused) as caught:
        room.join_spectator("One more", protocol.PROTOCOL_VERSION)
    assert caught.value.reason == protocol.SPECTATORS_FULL
    assert caught.value.extra["spectator_cap"] == MAX_SPECTATORS


def test_a_spectator_follows_somebody_who_is_still_there():
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    guest = room.join("Guest", "#58a6ff", protocol.PROTOCOL_VERSION)
    watcher = room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)

    # Given somebody by default rather than left pointing at nothing.
    assert watcher.watching == room.host_id

    assert room.watch(watcher.id, guest.id) is True
    assert watcher.watching == guest.id

    # Nobody by that id, so the request is refused rather than silently
    # leaving the spectator on a stream that belongs to nobody.
    assert room.watch(watcher.id, 999) is False
    assert watcher.watching == guest.id

    # The player leaves and the spectator is moved rather than orphaned.
    room.leave(guest.id)
    assert watcher.watching == room.host_id


def test_a_kicked_spectator_is_removed_without_touching_the_players():
    room = Room(RuleSet(player_cap=4), "Host", "#3ecf8e")
    guest = room.join("Guest", "#58a6ff", protocol.PROTOCOL_VERSION)
    watcher = room.join_spectator("Watcher", protocol.PROTOCOL_VERSION)

    assert room.kick(room.host_id, watcher.id) is True
    assert room.spectators == {}
    assert guest.id in room.players


def test_a_spectator_reaches_a_room_over_the_wire(hosted):
    room, server = hosted
    address = f"127.0.0.1:{server.port}"

    watcher = RoomClient()
    try:
        outcome = watcher.join(address, "Watcher", "#58a6ff", spectate=True)

        assert outcome["ok"] is True
        assert outcome["spectator"] is True
        assert outcome["watching"] == room.host_id

        # Present in the room, and not among its players.
        assert wait_for(lambda: len(room.to_dict()["spectators"]) == 1)
        names = [player["username"] for player in room.to_dict()["players"]]
        assert names == ["Host"]

        # And the host's view of the room reaches them like anybody else's.
        room.set_ready(room.host_id, False)
        server.notify_lobby()
        assert wait_for(lambda: any(
            not player["ready"]
            for player in watcher.state()["room"]["players"]
        ))
    finally:
        watcher.leave()


def test_a_spectator_is_refused_every_message_a_player_may_send(hosted):
    """The security surface. Each of these is refused explicitly.

    Sent raw rather than through RoomClient, because the client refuses some of
    them itself and what is being checked here is the host.
    """
    room, server = hosted

    async def try_them_all():
        from websockets.asyncio.client import connect

        answers = {}
        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await connection.send(json.dumps(
                protocol.hello("Watcher", "#58a6ff", spectate=True)
            ))
            welcome = json.loads(await connection.recv())

            for message in (
                protocol.input_intent(1, "left", 0),
                protocol.ready(True),
                protocol.rules(RuleSet().to_dict()),
                protocol.start(),
                protocol.kick(0),
                protocol.rematch(),
            ):
                await connection.send(json.dumps(message))
                answers[message["type"]] = await _answer(connection)

        return welcome, answers

    welcome, answers = asyncio.run(try_them_all())

    assert welcome["type"] == protocol.WELCOME
    assert welcome["spectator"] is True

    for kind, reply in answers.items():
        assert reply["type"] == protocol.REJECT, kind
        assert reply["reason"] == protocol.NOT_A_PLAYER, kind

    # Nothing it sent took effect.
    assert room.phase == LOBBY_WAITING
    assert server.match is None


def test_a_player_cannot_ask_to_be_shown_another_arena(hosted):
    """The reverse guard. Spectating is the only way onto somebody else's
    stream, and it is refused to anybody holding a snake.
    """
    _room, server = hosted

    async def ask():
        from websockets.asyncio.client import connect

        async with connect(f"ws://127.0.0.1:{server.port}") as connection:
            await connection.send(json.dumps(
                protocol.hello("Guest", "#58a6ff")
            ))
            await connection.recv()
            await connection.send(json.dumps(protocol.spectate(0)))
            return await _answer(connection)

    reply = asyncio.run(ask())
    assert reply["type"] == protocol.REJECT
    assert reply["reason"] == protocol.BAD_MESSAGE


def test_a_spectator_is_sent_the_stream_of_the_player_it_watches():
    """The information rule, on a grid.

    Two arenas, one player in each, and a spectator following one of them. What
    arrives carries the body of the player being watched and not the body of the
    other, which is exactly what that player's own stream carries.
    """
    from utils.game.match import MatchEngine

    rules = RuleSet(layout="2x1", arena_width=20, arena_height=20)
    engine = MatchEngine(rules, [
        (0, "Host", "#3ecf8e"),
        (1, "Guest", "#58a6ff"),
    ])
    engine.start()

    messages = engine.next_messages()

    # Two streams, one per arena, and each of the two players is on one of them.
    assert set(messages) == {0, 1}
    host_stream = engine.stream_for(0)
    guest_stream = engine.stream_for(1)

    def bodies(message):
        return {
            snake["id"] for snake in message["snakes"]
            if snake.get("b")
        }

    # A spectator watching the host is routed engine.stream_for(host), which is
    # the same object the host is routed. Nothing about it is spectator-shaped.
    watched = messages[host_stream]
    assert 0 in bodies(watched)

    if host_stream != guest_stream:
        assert 1 not in bodies(watched)
        # And every snake is still named, scored and placed in an arena, which
        # is public whatever arena you are in.
        named = {snake["id"] for snake in watched["snakes"]}
        assert named == {0, 1}

    engine.stop()


def test_a_refusal_does_not_end_a_connection_that_is_still_good(hosted):
    """Found by running the harness, not by reading the code.

    A spectator asking to follow an id nobody holds is refused, and the refusal
    used to be read as the connection being closed: the client hung up, the host
    dropped it, and a mistyped id cost somebody their place in the room. Only a
    reason that really means the connection is over ends it now.
    """
    room, server = hosted
    address = f"127.0.0.1:{server.port}"

    watcher = RoomClient()
    try:
        assert watcher.join(address, "Watcher", "#58a6ff", spectate=True)["ok"]
        assert wait_for(lambda: len(room.to_dict()["spectators"]) == 1)

        watcher.send(protocol.spectate(4242))
        time.sleep(0.5)

        assert watcher.state()["closed_reason"] is None
        assert watcher.connected is True
        assert len(room.to_dict()["spectators"]) == 1

        # Still following the host, which is who it was given.
        assert room.to_dict()["spectators"][0]["watching"] == room.host_id

        # And a reason that does mean the connection is over still ends it.
        server.disconnect(watcher.state()["player_id"], protocol.KICKED)
        assert wait_for(lambda: watcher.state()["closed_reason"] is not None)
        assert watcher.state()["closed_reason"]["reason"] == protocol.KICKED
    finally:
        watcher.leave()
