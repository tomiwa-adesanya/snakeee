"""Room state: the player list, and the rules the host is editing.

Pure logic. No sockets, no threads, no Flask. Everything a join can be refused
for is decided here and returned as a reason code, so the same decisions are
testable without opening a port.

The host occupies a seat like everyone else. Treating the host as a player
rather than as a special case is what keeps the player cap, colour uniqueness
and the ready check from each needing an exception.

A spectator is the opposite: membership without a seat. They are held in their
own dictionary, so every existing question about the room, the cap, the colours,
the ready check, can_start and the win conditions, reads self.players and is
answered without knowing spectators exist. The one thing they share with players
is the id counter, so no spectator can ever be handed the id of a player.
"""

import threading
from collections import deque

from utils.game.rules import RuleSet
from utils.net import protocol
from utils.store.profile import PLAYER_COLOURS

LOBBY_WAITING = "waiting"
MATCH_COUNTDOWN = "countdown"
MATCH_RUNNING = "running"
MATCH_OVER = "over"

# The phases in which a room is playing rather than waiting. Membership is
# asked about in three places, so it is written once.
MATCH_PHASES = (MATCH_COUNTDOWN, MATCH_RUNNING, MATCH_OVER)

# Round trips kept per player. At one ping a second this is an eight second
# window: long enough that a single spike does not carry the median, short
# enough that the number still follows a connection that has actually changed.
LATENCY_SAMPLES = 8

# A ceiling on watchers, not a rule the host sets. It is not a design choice
# worth a row in the editor: it is here so that a room cannot be made to hold an
# unbounded number of connections, each of which costs a copy of every broadcast.
MAX_SPECTATORS = 8


class JoinRefused(Exception):
    """A join that cannot be granted. Carries a reason code and extras."""

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def printable(text: str) -> str:
    """Drop anything that is not printable, then collapse the whitespace.

    Not the repository ASCII rule, which is about source files. A player may
    call themselves whatever they like as long as it draws as itself.

    What this removes is the category that does not: control characters, and the
    format characters that include the bidirectional overrides. None of them is
    an injection risk here, because every name is rendered with textContent and
    there is no innerHTML anywhere in the frontend, but a right-to-left override
    in a lobby row rearranges every other name on the line, on everybody else's
    screen, and there is no reason to carry one.
    """
    kept = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return " ".join(kept.split())


def clean_username(name) -> str:
    if not isinstance(name, str):
        raise JoinRefused(protocol.USERNAME_INVALID)

    cleaned = printable(name)[: protocol.MAX_USERNAME]
    if not cleaned:
        raise JoinRefused(protocol.USERNAME_INVALID)

    return cleaned


class Player:
    def __init__(self, player_id: int, username: str, colour: str, is_host: bool):
        self.id = player_id
        self.username = username
        self.colour = colour
        self.is_host = is_host
        self.ready = is_host

        # A window of round trips rather than the last one. A single sample is
        # dominated by whatever the link was doing at that instant: the first
        # packet after an idle stretch pays for a sleeping WiFi radio, and
        # reporting that as "the latency" makes a healthy network look broken.
        self.latency_samples = deque(maxlen=LATENCY_SAMPLES)

    @property
    def latency_ms(self):
        """The median round trip, or None before anything has been measured.

        Median rather than mean, so one spike does not move the number that
        players read.
        """
        if not self.latency_samples:
            return None
        ordered = sorted(self.latency_samples)
        return ordered[len(ordered) // 2]

    @property
    def latency_best_ms(self):
        """The lowest round trip seen.

        The closest thing available to the floor of the link, because it is the
        one sample that cannot have been inflated by anything. Worth showing
        next to the median: a median far above the best means the connection is
        idling between packets rather than being slow.
        """
        if not self.latency_samples:
            return None
        return min(self.latency_samples)

    def record_latency(self, latency_ms) -> None:
        if isinstance(latency_ms, (int, float)) and latency_ms >= 0:
            self.latency_samples.append(round(latency_ms))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "colour": self.colour,
            "host": self.is_host,
            "ready": self.ready,
            "latency_ms": self.latency_ms,
            "latency_best_ms": self.latency_best_ms,
        }


class Spectator:
    """Somebody watching. No colour, no seat, no ready flag.

    watching is the id of the player whose stream they are on. The host keeps it
    pointing at somebody who exists: a spectator watching nobody would have to be
    sent something, and the only somethings available are an arena picked at
    random or the whole grid, and the second of those is the thing the arena
    scoping exists to refuse.
    """

    def __init__(self, spectator_id: int, username: str):
        self.id = spectator_id
        self.username = username
        self.watching = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "watching": self.watching,
        }


class Room:
    def __init__(self, ruleset: RuleSet, host_username: str, host_colour: str):
        self.rules = ruleset
        self.phase = LOBBY_WAITING
        self.players = {}
        self.spectators = {}
        self.lock = threading.RLock()

        self._next_id = 0
        self.host_id = self._add(host_username, host_colour, is_host=True).id

    # -- membership -------------------------------------------------------

    def _add(self, username: str, colour: str, is_host: bool) -> Player:
        player = Player(self._next_id, username, colour, is_host)
        self.players[player.id] = player
        self._next_id += 1
        return player

    def _add_spectator(self, username: str) -> Spectator:
        spectator = Spectator(self._next_id, username)
        self.spectators[spectator.id] = spectator
        self._next_id += 1
        return spectator

    def _names_in_use(self) -> set:
        names = {player.username.lower() for player in self.players.values()}
        names.update(
            spectator.username.lower() for spectator in self.spectators.values()
        )
        return names

    def taken_colours(self) -> set:
        return {player.colour for player in self.players.values()}

    def available_colours(self) -> list:
        taken = self.taken_colours()
        return [colour for colour in PLAYER_COLOURS if colour not in taken]

    def join(self, username, colour, protocol_version: int) -> Player:
        """Admit a player, or raise JoinRefused with a specific reason.

        The order of these checks is deliberate. Version first, because a
        mismatched client cannot be reasoned with about anything else. Capacity
        next, because it is not the player's fault and not fixable by them.
        Then the two things they can fix, each carrying what they need to fix
        it in one action.
        """
        with self.lock:
            if protocol_version != protocol.PROTOCOL_VERSION:
                raise JoinRefused(
                    protocol.PROTOCOL_MISMATCH,
                    host_protocol=protocol.PROTOCOL_VERSION,
                    client_protocol=protocol_version,
                )

            if len(self.players) >= self.rules.player_cap:
                raise JoinRefused(
                    protocol.ROOM_FULL,
                    player_cap=self.rules.player_cap,
                )

            if self.phase in MATCH_PHASES and not self.rules.allow_join_in_progress:
                raise JoinRefused(protocol.MATCH_IN_PROGRESS)

            name = clean_username(username)
            # Against everybody in the room, watchers included. A player taking
            # the name a spectator is already using would put two rows reading
            # the same thing in front of the host.
            if name.lower() in self._names_in_use():
                raise JoinRefused(protocol.USERNAME_TAKEN, username=name)

            if colour not in PLAYER_COLOURS:
                raise JoinRefused(
                    protocol.COLOUR_INVALID,
                    available_colours=self.available_colours(),
                )

            if colour in self.taken_colours():
                # The list travels with the refusal so the client can offer a
                # free colour to click rather than making the player guess.
                raise JoinRefused(
                    protocol.COLOUR_TAKEN,
                    colour=colour,
                    available_colours=self.available_colours(),
                )

            return self._add(name, colour, is_host=False)

    def join_spectator(self, username, protocol_version: int) -> Spectator:
        """Admit somebody to watch, or raise JoinRefused with a reason.

        Deliberately not the same checks as join(). A spectator takes no seat,
        so the player cap does not apply to them and a full room can still be
        watched. They arrive with no snake, so allow_join_in_progress does not
        apply either: a match already under way is the thing they came for.

        The name is still unique across the whole room. Two rows reading the
        same name, one playing and one watching, is a lobby nobody can read.
        """
        with self.lock:
            if protocol_version != protocol.PROTOCOL_VERSION:
                raise JoinRefused(
                    protocol.PROTOCOL_MISMATCH,
                    host_protocol=protocol.PROTOCOL_VERSION,
                    client_protocol=protocol_version,
                )

            if not self.rules.allow_spectators:
                raise JoinRefused(protocol.SPECTATORS_NOT_ALLOWED)

            if len(self.spectators) >= MAX_SPECTATORS:
                raise JoinRefused(
                    protocol.SPECTATORS_FULL,
                    spectator_cap=MAX_SPECTATORS,
                )

            name = clean_username(username)
            if name.lower() in self._names_in_use():
                raise JoinRefused(protocol.USERNAME_TAKEN, username=name)

            spectator = self._add_spectator(name)
            spectator.watching = self.default_watch()
            return spectator

    def default_watch(self):
        """Who a spectator watches when they have not chosen.

        The host, because the host is the one player who cannot leave, so this
        is the one answer that cannot be stale a moment after it is given.
        """
        with self.lock:
            if self.host_id in self.players:
                return self.host_id
            return min(self.players) if self.players else None

    def watch(self, spectator_id: int, player_id) -> bool:
        """Point a spectator at a player. False when either does not exist.

        Refused rather than tolerated for an unknown player: a spectator pointed
        at nobody is a connection the broadcast has no stream for, and picking
        one for them would be picking an arena they have no business seeing.
        """
        with self.lock:
            spectator = self.spectators.get(spectator_id)
            if spectator is None:
                return False
            if player_id is None:
                spectator.watching = self.default_watch()
                return True
            if player_id not in self.players:
                return False
            spectator.watching = player_id
            return True

    def _repoint_spectators(self, gone_id: int) -> None:
        """Move anybody watching a player who has left onto somebody who has not."""
        replacement = self.default_watch()
        for spectator in self.spectators.values():
            if spectator.watching == gone_id:
                spectator.watching = replacement

    def leave(self, player_id: int) -> bool:
        with self.lock:
            if player_id == self.host_id:
                return False
            if self.players.pop(player_id, None) is None:
                return False
            self._repoint_spectators(player_id)
            return True

    def leave_spectator(self, spectator_id: int) -> bool:
        with self.lock:
            return self.spectators.pop(spectator_id, None) is not None

    def kick(self, requester_id: int, player_id: int) -> bool:
        """Remove a player or a spectator. One door, because the host sees one list.

        A spectator is looked at first and separately rather than by falling
        through, so removing one can never be confused with removing the player
        who happens to hold a nearby id.
        """
        with self.lock:
            if requester_id != self.host_id:
                raise JoinRefused(protocol.NOT_HOST)
            if player_id == self.host_id:
                return False
            if self.spectators.pop(player_id, None) is not None:
                return True
            if self.players.pop(player_id, None) is None:
                return False
            self._repoint_spectators(player_id)
            return True

    # -- state ------------------------------------------------------------

    def set_ready(self, player_id: int, is_ready: bool) -> bool:
        with self.lock:
            player = self.players.get(player_id)
            if player is None:
                return False
            player.ready = bool(is_ready)
            return True

    def record_latency(self, player_id: int, latency_ms) -> None:
        with self.lock:
            player = self.players.get(player_id)
            if player is not None:
                player.record_latency(latency_ms)

    def set_rules(self, requester_id: int, ruleset: RuleSet) -> None:
        with self.lock:
            if requester_id != self.host_id:
                raise JoinRefused(protocol.NOT_HOST)
            self.rules = ruleset

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase

    def return_to_lobby(self) -> None:
        """Back to waiting, with the rules the match was played under intact.

        Ready flags are cleared for everyone but the host, so a rematch is
        agreed to rather than assumed. A player who has left the room is not
        held in it by a rematch they never saw.
        """
        with self.lock:
            self.phase = LOBBY_WAITING
            for player in self.players.values():
                player.ready = player.is_host

    def everyone_ready(self) -> bool:
        with self.lock:
            if len(self.players) < self.rules.min_players:
                return False
            return all(player.ready for player in self.players.values())

    def can_start(self) -> tuple:
        """Returns (allowed, human reason when not allowed)."""
        with self.lock:
            if self.phase in MATCH_PHASES:
                return False, "A match is already under way."
            if len(self.players) < self.rules.min_players:
                return False, f"Waiting for at least {self.rules.min_players} players."
            waiting = [
                player.username
                for player in self.players.values()
                if not player.ready
            ]
            if waiting:
                return False, "Waiting for " + ", ".join(sorted(waiting)) + "."
            return True, None

    def to_dict(self) -> dict:
        with self.lock:
            allowed, reason = self.can_start()
            return {
                "phase": self.phase,
                "host_id": self.host_id,
                "player_cap": self.rules.player_cap,
                "min_players": self.rules.min_players,
                "players": [
                    player.to_dict()
                    for player in sorted(self.players.values(), key=lambda p: p.id)
                ],
                "available_colours": self.available_colours(),
                "can_start": allowed,
                "start_blocked_by": reason,
                # Spectators sit beside the players rather than among them.
                # Everything that counts the room counts the list above, and
                # nothing that counts the room should ever count these.
                "spectators": [
                    spectator.to_dict()
                    for spectator in sorted(
                        self.spectators.values(), key=lambda s: s.id
                    )
                ],
                "spectator_cap": MAX_SPECTATORS,
                "spectators_allowed": bool(self.rules.allow_spectators),
            }
