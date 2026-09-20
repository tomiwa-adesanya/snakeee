"""The wire protocol.

JSON over WebSocket with compact positional arrays: a segment is [x, y], not
{"x": 1, "y": 2}. No binary encoding, no schema compiler, no extra dependency.

Every hello and welcome carries PROTOCOL_VERSION. A mismatch is a clean reject
carrying both numbers, never a crash and never a silent desync, because players
will be running mixed versions the day after any release.

This module is pure: it builds and validates messages and knows nothing about
sockets or threads.
"""

import json

# Raised for the shared match. Version 1 had no message that could carry an
# input, a snapshot or a match phase, so a version 1 client in a version 2
# room would sit in a lobby that could never start. The mismatch is refused
# at the handshake, with both numbers, rather than discovered at kickoff.
#
# Raised again for spectators. A version 3 client asking to watch carries a
# flag on its hello, and a version 2 host has no idea what that flag means: it
# would ignore it and hand the person a snake, a colour and a seat they did
# not ask for. Silently getting the opposite of what was requested is exactly
# what the version number exists to prevent, so the pairing is refused.
PROTOCOL_VERSION = 3

MAX_MESSAGE_BYTES = 64 * 1024

# What a client is allowed to send, which is a different question from what a
# host is allowed to build. Both numbers were measured rather than guessed.
#
# The largest real client message is a full rules payload, 47 options with a
# 24 character room name: 1006 bytes. Four kilobytes is four times that, which
# leaves room for options added later without leaving room for a client to make
# the host allocate anything worth allocating.
#
# The ceiling above stays where it is, because it also bounds what a client will
# accept from a host, and a host legitimately sends more: the largest keyframe
# measured, a 2x2 grid of 120x120 arenas with twelve snakes and dense food, is
# 5715 bytes. Lowering the single cap to four kilobytes would have made the host
# unable to encode its own snapshot on a large layout. This is why the number
# was measured before it was changed.
MAX_CLIENT_BYTES = 4 * 1024
MAX_USERNAME = 16

# Client to host.
HELLO = "hello"
INPUT = "input"
READY = "ready"
RULES = "rules"
START = "start"
KICK = "kick"
PONG = "pong"
REMATCH = "rematch"
SPECTATE = "spectate"

# Host to client.
WELCOME = "welcome"
REJECT = "reject"
LOBBY = "lobby"
SNAPSHOT = "snapshot"
KEYFRAME = "keyframe"
EVENT = "event"
STANDINGS = "standings"
MATCH = "match"
PING = "ping"

CLIENT_TYPES = {HELLO, INPUT, READY, RULES, START, KICK, PONG, REMATCH,
                SPECTATE}
HOST_ONLY_TYPES = {RULES, START, KICK, REMATCH}

# What a connection with no snake may send. Written as the allowed set rather
# than as a list of things to refuse, so a message type added later is refused
# from a spectator until somebody decides otherwise. The decision for every
# member of CLIENT_TYPES, so none of them is answered by a default:
#
#   hello     handshake only, and a second one is ignored for everybody.
#   input     no. A spectator has no snake to steer.
#   ready     no. A spectator is not counted by can_start, so a ready flag
#             would be a control that changes nothing.
#   rules     no, and host-only besides.
#   start     no, and host-only besides.
#   kick      no, and host-only besides.
#   rematch   no, and host-only besides.
#   pong      yes. It is the answer to the host's own ping and nothing else.
#   spectate  yes, and only from a spectator. It changes who they are watching.
SPECTATOR_TYPES = {PONG, SPECTATE}

# Reject reasons. Every one of these is specific: a player who cannot join is
# told which thing stopped them, and where the fix is.
PROTOCOL_MISMATCH = "protocol_mismatch"
ROOM_FULL = "room_full"
USERNAME_TAKEN = "username_taken"
USERNAME_INVALID = "username_invalid"
COLOUR_TAKEN = "colour_taken"
COLOUR_INVALID = "colour_invalid"
MATCH_IN_PROGRESS = "match_in_progress"
CANNOT_START = "cannot_start"
NOT_HOST = "not_host"
BAD_MESSAGE = "bad_message"
KICKED = "kicked"
ROOM_CLOSED = "room_closed"
FLOODING = "flooding"
TOO_MANY_ATTEMPTS = "too_many_attempts"
TOO_MANY_BAD_MESSAGES = "too_many_bad_messages"
SPECTATORS_NOT_ALLOWED = "spectators_not_allowed"
SPECTATORS_FULL = "spectators_full"
NOT_A_PLAYER = "not_a_player"

REJECT_TEXT = {
    PROTOCOL_MISMATCH: "This copy of the game speaks a different version of the "
                       "protocol. Both players need the same release.",
    ROOM_FULL: "The room is full.",
    USERNAME_TAKEN: "Someone in the room is already using that name.",
    USERNAME_INVALID: "That name cannot be used.",
    COLOUR_TAKEN: "Someone in the room already has that colour.",
    COLOUR_INVALID: "That colour is not one of the twelve player colours.",
    MATCH_IN_PROGRESS: "The match has already started and this room does not "
                       "allow joining in progress.",
    CANNOT_START: "The match cannot start yet.",
    NOT_HOST: "Only the host can do that.",
    BAD_MESSAGE: "The host could not understand that message.",
    KICKED: "The host removed you from the room.",
    ROOM_CLOSED: "The host closed the room.",
    FLOODING: "Too many messages, too quickly.",
    TOO_MANY_ATTEMPTS: "Too many connection attempts. Wait a moment and "
                       "try again.",
    TOO_MANY_BAD_MESSAGES: "Too many messages the host could not read.",
    SPECTATORS_NOT_ALLOWED: "This room does not allow spectators.",
    SPECTATORS_FULL: "This room already has as many spectators as it takes.",
    NOT_A_PLAYER: "You are watching this room, not playing in it.",
}


# A refusal means one of two different things, and a client that cannot tell
# them apart hangs up on the first one it meets.
#
# These reasons end the connection: the room is gone, or this client is no
# longer in it, and there is nothing further to say. Every other reason refuses
# one request on a connection that is still perfectly good, and the client goes
# on using it. Before this existed, a spectator asking to follow a player who
# had just left was answered "nobody has that id" and then disconnected itself
# over it, which cost it its seat in the room for a mistyped id.
CLOSING_REASONS = {
    KICKED,
    ROOM_CLOSED,
    PROTOCOL_MISMATCH,
    FLOODING,
    TOO_MANY_ATTEMPTS,
    TOO_MANY_BAD_MESSAGES,
}


class ProtocolError(ValueError):
    """A message that cannot be understood or must not be trusted."""


def encode(message: dict) -> str:
    return json.dumps(message, separators=(",", ":"))


def decode(raw, limit: int = MAX_MESSAGE_BYTES) -> dict:
    """Parse an inbound frame. Never trusts the sender.

    Size is checked before parsing: a client should not be able to make the
    host allocate an arbitrary amount of memory by sending one frame.

    The limit is a parameter because the two directions are not the same. A
    host reading a client passes MAX_CLIENT_BYTES; a client reading a host
    takes the default, which is the wider one.
    """
    if isinstance(raw, bytes):
        if len(raw) > limit:
            raise ProtocolError("message too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("message was not valid UTF-8") from error
    elif not isinstance(raw, str):
        raise ProtocolError("message was neither text nor bytes")

    if len(raw) > limit:
        raise ProtocolError("message too large")

    try:
        message = json.loads(raw)
    except ValueError as error:
        raise ProtocolError("message was not valid JSON") from error

    if not isinstance(message, dict):
        raise ProtocolError("message was not an object")

    kind = message.get("type")
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("message had no type")

    return message


# -- client to host ---------------------------------------------------------


def hello(username: str, colour: str, spectate: bool = False) -> dict:
    """Asking to join. The flag is what separates playing from watching.

    A colour still travels with a spectator's hello and is ignored by the host.
    Leaving it out would mean two shapes of hello to validate for the sake of
    one field nobody reads.
    """
    return {
        "type": HELLO,
        "protocol": PROTOCOL_VERSION,
        "username": username,
        "colour": colour,
        "spectate": bool(spectate),
    }


def spectate(player_id) -> dict:
    """Watch this player instead. None means whoever the host picks."""
    return {
        "type": SPECTATE,
        "player": None if player_id is None else int(player_id),
    }


def input_intent(seq: int, heading: str, move: int) -> dict:
    """A desired heading, and nothing else.

    seq  monotonically increasing per client, so the host can tell the client
         which of its inputs it has seen and the client knows when to stop
         drawing a turn it predicted.
    move the move the player was looking at when they pressed the key. On a link
         that stalls this can be several moves behind by the time it lands, and
         a turn applied several cells further along is a different turn.
    """
    return {
        "type": INPUT,
        "seq": int(seq),
        "heading": heading,
        "move": int(move),
    }


def rematch() -> dict:
    return {"type": REMATCH}


def ready(is_ready: bool) -> dict:
    return {"type": READY, "ready": bool(is_ready)}


def rules(ruleset: dict) -> dict:
    return {"type": RULES, "rules": ruleset}


def start() -> dict:
    return {"type": START}


def kick(player_id: int) -> dict:
    return {"type": KICK, "player": int(player_id)}


def pong(stamp) -> dict:
    return {"type": PONG, "stamp": stamp}


# -- host to client ---------------------------------------------------------


def welcome(player_id: int, room: dict, ruleset: dict,
            spectator: bool = False, watching=None) -> dict:
    """Admitted. The id is a seat for a player and a handle for a spectator.

    Both come from the room's one counter, so no spectator ever carries the id
    of a player. Everything keyed by id on the host, the connection registries
    included, can therefore hold both without a second namespace.
    """
    return {
        "type": WELCOME,
        "protocol": PROTOCOL_VERSION,
        "player": player_id,
        "room": room,
        "rules": ruleset,
        "spectator": bool(spectator),
        "watching": watching,
    }


def reject(reason: str, detail: str = None, **extra) -> dict:
    message = {
        "type": REJECT,
        "reason": reason,
        "text": detail or REJECT_TEXT.get(reason, "The host refused the request."),
    }
    message.update(extra)
    return message


def lobby(room: dict, rules: dict) -> dict:
    """The room as it stands, and the rules it will be played under.

    The rules travel with every lobby update rather than only with the welcome.
    A host can change them while people are sitting in the room, and a client
    that heard them once would show the rules it joined under and then play a
    match under different ones.
    """
    return {"type": LOBBY, "room": room, "rules": rules}


def ping(stamp) -> dict:
    return {"type": PING, "stamp": stamp}


def match(payload: dict) -> dict:
    """A change of match phase: a countdown started, a match ended."""
    message = {"type": MATCH}
    message.update(payload)
    return message
