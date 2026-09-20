"""The shared match: many snakes, one simulation, one authority.

The host runs this. A client runs nothing: it sends a desired heading and draws
what comes back. No position, length, score, kill or death is ever accepted from
a client, so a modified client can lie about what it wants and about nothing
else.

Four things here exist because of what a real link does, and none of them should
be simplified away.

  1. A move is resolved for every snake at once, not one snake at a time. Two
     heads entering the same cell is a head-on collision, not a race between two
     loops. The order snakes happen to be stored in must never decide who dies.

  2. The occupancy grid carries an owner alongside it, so a collision knows
     whose body was hit and can credit the kill without scanning anybody. A cell
     is only cleared by the snake that still owns it, which is what keeps the
     grid consistent when two snakes overlap during spawn protection.

  3. Snapshots are deltas against the previous snapshot, and a snake's delta is
     the cells its head gained and the number its tail lost. A body is only sent
     whole in a keyframe, or for a snake that has just spawned. This is small
     because a snake moves at most twice between snapshots, not because anything
     is compressed.

  4. An input carries the move it was meant for. On a link that stalls, an input
     can arrive several moves after the one the player was looking at when they
     pressed the key, and a turn applied several cells further along is a
     different turn from the one that was asked for. Past the tolerance it is
     dropped rather than applied somewhere else.
"""

import logging
import random
import threading
import time
from collections import deque

from utils.game import bots as bots_module
from utils.game.arena import EMPTY, FOOD, ITEM, SNAKE, Arena, Topology
from utils.game.effects import (
    MAGNET_INTERVAL,
    MAGNET_RANGE,
    SHRINK_SEGMENTS,
    Effects,
    heading_for,
    move_interval_for,
)
from utils.game.engine import FAULT, HEADINGS, TICK_HZ
from utils.game.items import (
    PICKUPS,
    POISONS,
    enabled_kinds,
    item_target,
    spawn_food,
    spawn_items,
)
from utils.game.rules import RuleSet

log = logging.getLogger("match")

# Snapshots go out at this rate regardless of the tick rate. The simulation
# ticks at 60 Hz; sending every tick would be three times the traffic for a
# difference no eye resolves, and the renderer interpolates between snapshots
# either way.
SNAPSHOT_HZ = 20
SNAPSHOT_SECONDS = 1.0 / SNAPSHOT_HZ

# A keyframe once a second. Deltas are only applicable in order, so a client
# that joins mid-match, or that somehow loses its place, is never more than a
# second from a state it can trust without asking for anything.
KEYFRAME_EVERY = SNAPSHOT_HZ

# How long an event stays on the feed. The host prunes to this window and sends
# what survives with every message, so a client holds no event state of its own
# and a client that joins late sees what just happened rather than nothing.
EVENT_WINDOW = 5.0

# How far back the jump measurement looks. Long enough to catch a stall that
# happened a moment ago, short enough that the reading goes quiet again once the
# link settles.
BURST_WINDOW = 5.0

# How far behind the newest state a client draws everybody else.
#
# This is the whole idea, so it is worth stating rather than tuning silently.
# Extrapolation guesses where a snake will be and corrects when the guess is
# wrong, and every correction is a visible snap. Interpolation never guesses: it
# draws between two states that actually arrived, at a moment slightly in the
# past. The cost is a fixed, small delay on other players. The gain is that
# there is nothing left to be wrong about, so there is nothing to snap.
#
# The delay has to cover the gap between arrivals or there is nothing to
# interpolate between, so it follows the link rather than being guessed: the
# recent worst arrival gap with a margin, floored so a perfect link is still
# smooth across one missed message and capped so a bad one stays playable.
#
# Your own snake is not delayed. It is drawn from the newest state and from your
# own prediction, because a hundred milliseconds on somebody else is invisible
# and a hundred milliseconds on yourself is the game feeling broken.
RENDER_DELAY_MIN = 0.06
RENDER_DELAY_MAX = 0.25
RENDER_DELAY_MARGIN = 1.4

# How fast the delay follows the link. Small, so it answers a link that has
# genuinely changed rather than one that looked bad for a moment.
DELAY_EASE = 0.05

# States kept to interpolate between. A second at twenty a second, which is more
# than the largest delay can ask for.
FRAME_BUFFER = 40

# Fields that never change while a snake exists. Sent with a keyframe and not
# again, because a name and a colour do not need saying twenty times a second.
IDENTITY_FIELDS = ("name", "c")

# Fields a delta always carries, whatever they were last time. The id is what
# the entry is about, and progress is what the interpolation runs on, so it is
# genuinely different in every message.
ALWAYS_SENT = ("id", "p")


def standings_from(snakes, rank) -> list:
    """The leaderboard, built from the snakes and the order they are ranked in.

    It used to travel on the wire and was a third of every snapshot, all of it
    a second copy of numbers already in the message. The order is the only part
    a client cannot work out for itself, because the rule for it depends on the
    win condition and lives on the host, so the order is what gets sent.
    """
    by_id = {snake["id"]: snake for snake in snakes}

    rows = []
    for snake_id in rank:
        snake = by_id.get(snake_id)
        if snake is None:
            continue
        rows.append({
            "id": snake["id"],
            "username": snake.get("name"),
            "colour": snake.get("c"),
            "score": snake.get("score", 0),
            "kills": snake.get("kills", 0),
            "deaths": snake.get("deaths", 0),
            "lives": snake.get("lives", 0),
            "alive": snake.get("alive", False),
            "eliminated": snake.get("eliminated", False),
            # From the stat rather than from the body, because a snapshot
            # scoped to one arena carries no body for a snake in another one and
            # a leaderboard that reported those as length zero would be worse
            # than useless.
            "length": snake.get("len", len(snake.get("b") or [])),
            "arena": snake.get("a", 0),
            "bot": snake.get("bot", False),
        })
    return rows


PHASE_COUNTDOWN = "countdown"
PHASE_RUNNING = "running"
PHASE_OVER = "over"

# How many moves late an input may be and still be applied.
#
# Past this it is refused, because a turn tied to a position the snake left
# several cells ago is not the turn that was asked for: applied where the snake
# is now it steers into a wall the player never saw, and being killed by your
# own stale input is worse than being ignored.
#
# The refusal is not the end of the press. The answer carries the move the snake
# is on now, and the client resends against that rather than dropping it, so a
# turn that missed its moment happens at the next one instead of vanishing. The
# host still never applies a heading tied to a stale position, which is the
# property this constant exists to protect.
INPUT_MOVE_TOLERANCE = 2

DEATH_WALL = "wall"
DEATH_SELF = "self"
DEATH_SNAKE = "snake"
DEATH_HEAD_ON = "head_on"
DEATH_SEVERED = "severed"

UNLIMITED_LIVES = 0

# A sentinel, because None is a real value for several of these fields.
_MISSING = object()


class MatchSnake:
    """One player's snake, plus everything the leaderboard reads."""

    def __init__(self, player_id: int, username: str, colour: str):
        self.id = player_id
        self.username = username
        self.colour = colour

        self.body = deque()
        self.heading = HEADINGS["right"]
        self.pending_heading = self.heading

        self.alive = False
        self.eliminated = False
        self.score = 0
        self.kills = 0
        self.deaths = 0
        self.lives_left = 0

        self.accumulator = 0
        self.move_interval = 1

        # When this snake last moved, by the clock rather than by the tick
        # counter. What the renderer draws is derived from this, so how far
        # through a move a snake appears to be does not depend on the tick
        # arriving on time. See _stats_for.
        self.last_move_at = 0.0

        # What an input names. Incremented once per move, never per tick, so a
        # client and the host are counting the same thing.
        self.move_count = 0
        self.last_seq = 0

        # How often this player's input arrived so late that it landed on a
        # different move from the one they were looking at, and the worst case.
        # Reported rather than acted on: it is the honest measure of whether the
        # link is keeping up with the player.
        self.late_inputs = 0
        self.worst_input_lag = 0

        self.death_cause = None
        self.respawn_at = None

        # What is currently acting on this snake, and until when.
        self.effects = Effects()

        # This snake's position in the match, which is what the owner grid
        # records. See the note above _occupy for why it is not the id.
        self.slot = 0

        # True when nobody is holding the keys. It changes nothing about how
        # the snake is simulated -- a bot is a player that happens to be
        # decided by code -- and is here so the leaderboard can say so and so
        # that an empty room does not end a match the moment the last human
        # leaves the bots to it.
        self.is_bot = False

        # Which arena the head is in. Held rather than derived on demand so a
        # snake that is dead and waiting to respawn still reports somewhere: a
        # client with nothing to draw still has a camera pointed at something,
        # and the last arena its player was in is the only sensible answer.
        self.arena_id = 0

        # How long this snake was when it last died, which is what a bounty is
        # a share of. Held on the snake rather than passed around because the
        # body is already gone by the time the killer is paid.
        self.deaths_length = 0
        self.protected_until = 0.0

    @property
    def length(self) -> int:
        return len(self.body)

    @property
    def head(self):
        return self.body[0]


class MatchEngine:
    """The authoritative simulation for one room."""

    def __init__(self, rules: RuleSet, roster, seed: int = None):
        self.rules = rules
        self.random = random.Random(seed)
        # One cell space, divided into arenas by arithmetic rather than by
        # being several objects. With a 1x1 layout the topology describes
        # exactly the arena that was here before it existed.
        self.topology = Topology(
            rules.arena_width, rules.arena_height, *rules.grid_shape()
        )
        self.arena = Arena(
            self.topology.width, self.topology.height, rules.edge_behaviour
        )

        # Who owns each cell, as player id plus one so that zero means nobody.
        # Kept beside the arena rather than inside it because single player has
        # no use for it and Arena is shared with single player.
        self.owner = bytearray(self.arena.width * self.arena.height)

        self.snakes = {}
        self.phase = PHASE_COUNTDOWN
        self.tick = 0
        self.lock = threading.RLock()

        self.starts_at = None
        self.started_at = None
        self.ended_at = None
        self.outcome = None

        # Read and written by the tick loop, which is shared with single player.
        self.max_drift_ms = 0.0
        self.late_ticks = 0

        # How evenly snapshots are actually leaving this machine. The rate is
        # nominally twenty a second; whether it is depends on how fine a timer
        # the operating system will give a sleeping thread, and on Windows the
        # default is coarse enough to matter. Measured rather than assumed,
        # because it is invisible from inside the game and looks like the
        # network from outside it.
        self.send_gaps = deque(maxlen=200)

        # Severed segments lying on the floor, and when each expires. They are
        # kept in the arena's food set as well, so eating one needs no special
        # case anywhere: a remain is food that happens to have been somebody.
        # This dict is what makes them decay and what keeps them from counting
        # against the food target.
        self.remains = {}

        # What just happened, so a player can tell why they are suddenly short.
        # Names and colours travel with each entry rather than being looked up
        # by id, so an event about somebody who has since left the room still
        # reads properly.
        self.events = deque()
        self._event_id = 0

        self._pending = {}
        self._food_add = []
        self._food_del = []
        self._item_add = []
        self._item_del = []
        self._resync = set()

        # Everything below is per stream.
        #
        # A stream is one arena's worth of the match, and there is one for each
        # arena in the layout. A single arena has exactly one, keyed None, which
        # is the whole cell space and is what this was before layouts existed.
        #
        # It is kept per stream because what has already been said is a property
        # of the conversation and not of the match: a player in arena 0 and a
        # player in arena 3 are told different things, so what they can be
        # assumed to know is different too. One shared record of that produced
        # deltas that said nothing to a client that had not been told the thing
        # they were leaving out.
        #
        # _sent_fields  what each snake's fields were the last time this stream
        #               sent them. A delta carries a field only when it differs.
        # _events_sent  the last event id this stream has carried.
        # _map_sent     the last arena roll-up this stream has carried.
        # _shown        which snakes this stream last sent a body for, so a
        #               snake arriving in the arena gets a whole one and a snake
        #               leaving gets an empty one exactly once.
        self._sent_fields = {}
        self._events_sent = {}
        self._map_sent = {}
        self._shown = {}

        self._base_tick = 0
        self._snapshots_sent = 0
        self._force_keyframe = True

        # Streams that owe a keyframe to somebody who has just arrived in that
        # arena, and has no state of that stream to apply a delta to.
        self._keyframe_for = set()

        # Where each snake is, for the length of one broadcast. See _arenas_of.
        self._arena_cache = None

        # When the magnet next drags loose food a cell. See _pull_food.
        self._next_pull = 0.0

        # slot -> player id, the reverse of MatchSnake.slot.
        self._by_slot = {}

        for player_id, username, colour in roster:
            self._add(player_id, username, colour)

        # Bots are added last and take ids above every human, so a player
        # joining in progress cannot be handed an id a bot already holds.
        self.bots = {}
        self._add_bots(len(roster))

    # -- bots ---------------------------------------------------------------

    BOT_ID_BASE = 1000

    def _wanted_bots(self, humans: int) -> int:
        """How many computer players this rule set asks for.

        Two ways to ask, and they are not added together. `bot_count` is a
        number of bots; `bots_fill_slots` is a wish for a full room, which means
        however many it takes to reach the cap. A host who sets both means the
        larger of the two -- asking for four bots and a full room and getting
        four in a room of ten would be the setting not working.
        """
        wanted = max(0, int(self.rules.bot_count))

        if self.rules.bots_fill_slots:
            wanted = max(wanted, self.rules.player_cap - humans)

        # Never more than the room could hold. The cap is a cap.
        return max(0, min(wanted, self.rules.player_cap - humans))

    def _add_bots(self, humans: int) -> None:
        wanted = self._wanted_bots(humans)

        for index in range(wanted):
            snake_id = self.BOT_ID_BASE + index
            self._add(
                snake_id,
                bots_module.name_for(index),
                bots_module.colour_for(index),
                is_bot=True,
            )
            self.bots[snake_id] = bots_module.Bot(
                snake_id,
                self.rules.bot_difficulty,
                aggression=self.rules.bot_aggression,
                # Seeded from the match seed, so a match replayed with the same
                # seed plays out the same way. Bots that rolled their own
                # randomness would make every run different for no reason.
                seed=self.random.random(),
            )

        if wanted:
            log.info(
                "%s bots added at %s difficulty",
                wanted,
                self.rules.bot_difficulty,
            )

    def _think(self, movers, now: float) -> None:
        """Let every bot choose a heading, through the ordinary input path.

        Built once per tick and shared, so a room of eight bots costs one walk
        of the arena rather than eight.

        A bot's choice goes through set_heading like a key press: it is checked
        for legality, refused if it is a reversal, and resolved simultaneously
        with everybody else's. Nothing here touches the arena, and a bot that
        wanted to cheat would have to be given a new method to do it with.
        """
        if not self.bots:
            return

        thinking = [
            snake for snake in movers
            if snake.is_bot and snake.alive and snake.body
        ]
        if not thinking:
            return

        view = bots_module.MatchView(self)

        for snake in thinking:
            bot = self.bots.get(snake.id)
            if bot is None:
                continue

            heading = bot.choose(view)
            if heading is None:
                continue

            # The same refusal a human press gets. A bot that could turn back
            # on itself would be playing a different game.
            if snake.length > 1:
                facing = snake.heading
                if heading[0] == -facing[0] and heading[1] == -facing[1]:
                    continue

            snake.pending_heading = heading_for(snake.effects, heading, now)

    # -- roster -----------------------------------------------------------

    def _add(self, player_id: int, username: str, colour: str,
             is_bot: bool = False) -> MatchSnake:
        snake = MatchSnake(player_id, username, colour)
        snake.lives_left = self.rules.lives
        snake.is_bot = is_bot
        snake.slot = len(self.snakes)
        self.snakes[player_id] = snake
        self._by_slot[snake.slot] = player_id
        return snake

    def add_player(self, player_id: int, username: str, colour: str) -> bool:
        """Admit somebody to a match already under way.

        They spawn where there is room, with a full set of lives. Joining late
        is not a disadvantage worth engineering around: the score they missed is
        already on the board for everyone to see.
        """
        with self.lock:
            if player_id in self.snakes:
                return False

            snake = self._add(player_id, username, colour)

            if self.phase == PHASE_RUNNING:
                self._spawn(snake, time.monotonic())
            elif self.phase == PHASE_COUNTDOWN:
                self._spawn(snake, time.monotonic(), protect=False)

            # Everybody gets a keyframe rather than the new arrival getting a
            # private one. One stream with one base means a delta is applicable
            # to every client or to none, instead of being applicable to
            # everyone except the person who joined half a beat ago.
            self._force_keyframe = True

            log.info("player %s joined the match in progress", username)
            return True

    def remove_player(self, player_id: int) -> bool:
        with self.lock:
            snake = self.snakes.pop(player_id, None)
            if snake is None:
                return False

            for cell in snake.body:
                self._clear(cell, snake.id)
            self._pending.pop(player_id, None)
            return True

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            self.arena.reset()
            self.owner = bytearray(self.arena.width * self.arena.height)
            self.remains = {}
            self.events.clear()
            self._events_sent = {}
            self._map_sent = {}
            self._shown = {}
            self._sent_fields = {}
            self._keyframe_for = set()
            self.tick = 0
            self._base_tick = 0
            self._snapshots_sent = 0
            self._pending = {}
            self._food_add = []
            self._food_del = []
            self._item_add = []
            self._item_del = []
            self._sent_fields = {}
            self._force_keyframe = True
            self.outcome = None
            self.ended_at = None
            self.max_drift_ms = 0.0
            self.late_ticks = 0

            now = time.monotonic()
            for snake in self.snakes.values():
                snake.body.clear()
                snake.alive = False
                snake.eliminated = False
                snake.score = 0
                snake.kills = 0
                snake.deaths = 0
                snake.lives_left = self.rules.lives
                snake.move_count = 0
                snake.last_seq = 0
                snake.late_inputs = 0
                snake.worst_input_lag = 0
                snake.death_cause = None
                snake.respawn_at = None
                snake.protected_until = 0.0
                self._spawn(snake, now, protect=False)

            self._top_up_food()
            self._top_up_items()

            self.phase = PHASE_COUNTDOWN
            self.starts_at = now + self.rules.start_countdown

            log.info(
                "match starting, %s players, %s arenas of %sx%s, "
                "win condition %s",
                len(self.snakes),
                self.topology.count,
                self.topology.arena_width,
                self.topology.arena_height,
                self.rules.win_condition,
            )

    def fail(self, error: BaseException) -> None:
        """The tick loop raised. End the match with a reason rather than stall.

        The same contract SoloEngine.fail has, because one TickLoop drives both
        and it must not have to know which engine it is holding.
        """
        log.exception("the match tick failed, ending the match", exc_info=error)
        self.stop(FAULT)

    def stop(self, reason: str = "closed") -> None:
        with self.lock:
            if self.phase != PHASE_OVER:
                self._finish(reason)

    # -- spawning ---------------------------------------------------------

    def _anchors(self) -> list:
        """Candidate head cells, spread evenly and inset from the edges.

        Laid out per arena rather than across the whole cell space. Spreading a
        fixed number of points over a grid would put most of them near the
        middle of it, which on a 2x2 layout is four inside corners: everybody
        would start within sight of everybody else and the layout would be
        pointless. Each arena gets its own set, and which one a player lands in
        is then decided by clearance like any other spawn.
        """
        width = self.topology.arena_width
        height = self.topology.arena_height
        margin = max(1, min(4, width // 8, height // 8))
        steps = 4 if self.topology.single else 3

        span_x = max(1, width - 1 - 2 * margin)
        span_y = max(1, height - 1 - 2 * margin)

        points = []
        for arena_id in range(self.topology.count):
            origin_x, origin_y = self.topology.origin(arena_id)
            for row in range(steps):
                for column in range(steps):
                    points.append((
                        origin_x + margin + round(span_x * column / (steps - 1)),
                        origin_y + margin + round(span_y * row / (steps - 1)),
                    ))
        return points

    def _body_at(self, x: int, y: int, heading, length: int):
        """The cells a snake would occupy, or None if it does not fit.

        The body extends backwards from the head, and the cells ahead of it are
        checked too: spawning nose against a wall is a death in the first move,
        which reads as the game killing the player for nothing.
        """
        cells = []
        for step in range(length):
            cell_x = x - heading[0] * step
            cell_y = y - heading[1] * step

            if not self.arena.inside(cell_x, cell_y):
                if self.arena.edge_behaviour != "wrap":
                    return None
                cell_x %= self.arena.width
                cell_y %= self.arena.height

            if self.arena.at(cell_x, cell_y) != EMPTY:
                return None
            cells.append((cell_x, cell_y))

        ahead_x, ahead_y = x, y
        for _ in range(max(2, length)):
            step = self.arena.step_from(ahead_x, ahead_y, heading)
            if step is None or self.arena.at(step[0], step[1]) != EMPTY:
                return None
            ahead_x, ahead_y = step

        return cells

    def _clearance(self, x: int, y: int) -> int:
        """Distance from the nearest live head, so spawns do not crowd.

        Measured the short way round when the edges wrap. Straight subtraction
        calls two snakes either side of the seam the width of the space apart,
        which is the one place it would most like to keep them from spawning.
        """
        wrap = self.arena.edge_behaviour == "wrap"
        best = self.arena.width + self.arena.height

        for other in self.snakes.values():
            if not other.alive or not other.body:
                continue
            head = other.head

            gap_x = abs(head[0] - x)
            gap_y = abs(head[1] - y)
            if wrap:
                gap_x = min(gap_x, self.arena.width - gap_x)
                gap_y = min(gap_y, self.arena.height - gap_y)

            best = min(best, gap_x + gap_y)
        return best

    def _spawn(self, snake: MatchSnake, now: float, protect: bool = True) -> bool:
        length = self.rules.starting_length

        best = None
        best_score = -1

        for x, y in self._anchors():
            for heading in HEADINGS.values():
                cells = self._body_at(x, y, heading, length)
                if cells is None:
                    continue
                score = self._clearance(x, y)
                if score > best_score:
                    best_score = score
                    best = (cells, heading)

        if best is None:
            # A full arena, or an arena so small that nothing fits. Better to
            # leave the player out of this life than to place them inside
            # somebody else.
            snake.alive = False
            snake.respawn_at = now + max(1, self.rules.respawn_delay)
            return False

        cells, heading = best

        snake.body = deque(cells)
        self._enters(snake, self.topology.arena_at(cells[0][0], cells[0][1]))
        snake.heading = heading
        snake.pending_heading = heading
        snake.alive = True
        snake.death_cause = None
        snake.respawn_at = None
        snake.accumulator = 0
        snake.last_move_at = now

        # A new body is a clean one. Carrying a slow poison through a death
        # would be punishing the same mistake twice.
        snake.effects.clear()

        # A bot draws a fresh temperament each life, so the snake that has been
        # hunting you may come back content to feed, and the one you learned to
        # ignore may not be. See APPETITE in bots.py.
        bot = self.bots.get(snake.id)
        if bot is not None:
            bot.reroll()
        snake.move_interval = move_interval_for(
            self.rules, len(cells), snake.effects, now
        )
        snake.protected_until = (
            now + self.rules.spawn_protection if protect else 0.0
        )

        for cell in cells:
            self._occupy(cell, snake.id)

        return True

    # -- the grid ---------------------------------------------------------

    # -- who holds a cell ---------------------------------------------------
    #
    # The owner grid records a **slot**, not a player id.
    #
    # It is a bytearray, one entry per cell, and a player id is not bounded: a
    # room hands out a new one on every join and never reuses them, so a
    # long-lived room reaches 255 and every write to this grid then raises. That
    # was true before bots existed and would have taken a very long session to
    # meet; bots hit it immediately, because their ids start above every human.
    #
    # A slot is this snake's position in the match, so it is bounded by the
    # player cap and fits in a byte with room to spare. Zero means nobody.

    def _slot_of(self, snake_id: int) -> int:
        snake = self.snakes.get(snake_id)
        return 0 if snake is None else snake.slot + 1

    def _occupy(self, cell, snake_id: int) -> None:
        self.arena.set(cell[0], cell[1], SNAKE)
        self.owner[self.arena.index(cell[0], cell[1])] = self._slot_of(snake_id)

    def _occupy_shared(self, cell, snake_id: int) -> None:
        """Enter a cell without taking it from whoever already holds it.

        Two snakes are in the same cell whenever one passes through the other,
        and only one of them can be recorded as its owner. The one that was
        there first keeps it.

        Taking it instead is a real bug and it was found by driving one snake
        through another and printing the grid: the passer becomes the owner, so
        when its tail leaves it clears a cell the other snake is still standing
        in, and the other snake is intangible there for the rest of the match.
        Anything crossing that cell then cuts nobody.

        The mirror of that is accepted: if the original owner leaves first, the
        passer is the one left standing in a cell that has been freed. Somebody
        has to be, only one of them is recorded, and this way the cost falls on
        the snake that chose to be there rather than on the one that was minding
        its own business.
        """
        index = self.arena.index(cell[0], cell[1])
        self.arena.set(cell[0], cell[1], SNAKE)
        if self.owner[index] == 0:
            self.owner[index] = self._slot_of(snake_id)

    def _clear(self, cell, snake_id: int) -> None:
        """Free a cell, but only if this snake is still the one holding it.

        Two snakes can overlap while one of them is spawn protected. Without
        this check the one that leaves first erases a cell the other is standing
        on, and the survivor becomes partly intangible for the rest of the
        match.
        """
        index = self.arena.index(cell[0], cell[1])
        if self.owner[index] != self._slot_of(snake_id):
            return
        self.owner[index] = 0
        if self.arena.at(cell[0], cell[1]) == SNAKE:
            self.arena.set(cell[0], cell[1], EMPTY)

    def _owner_at(self, cell):
        stored = self.owner[self.arena.index(cell[0], cell[1])]
        if stored == 0:
            return None
        return self._by_slot.get(stored - 1)

    # -- input ------------------------------------------------------------

    def set_heading(self, player_id: int, name, seq=None, move=None) -> dict:
        """Queue a heading for one player. Returns why, when it is refused.

        A refused heading is never remembered anywhere. Whether a turn is legal
        depends on where the snake is now, not on what was pressed earlier.
        """
        with self.lock:
            snake = self.snakes.get(player_id)
            if snake is None:
                return {"ok": False, "reason": "not_playing"}

            if isinstance(seq, int) and seq > snake.last_seq:
                snake.last_seq = seq

            if self.phase != PHASE_RUNNING or not snake.alive:
                return {"ok": False, "reason": "not_playing", "ack": snake.last_seq}

            heading = HEADINGS.get(name)
            if heading is None:
                return {"ok": False, "reason": "invalid_heading",
                        "ack": snake.last_seq}

            # Confusion is applied here, before the legality check, so that what
            # is refused is the heading the press actually produces rather than
            # the one that was pressed. The host does this rather than trusting
            # a client to scramble its own controls, and the client applies the
            # same fixed rule to its own prediction so the two agree.
            heading = heading_for(snake.effects, heading, time.monotonic())

            behind = 0
            if isinstance(move, int) and move >= 0:
                behind = max(0, snake.move_count - move)
                if behind > INPUT_MOVE_TOLERANCE:
                    snake.late_inputs += 1
                    snake.worst_input_lag = max(snake.worst_input_lag, behind)
                    return {
                        "ok": False,
                        "reason": "too_late",
                        "behind": behind,
                        "ack": snake.last_seq,
                        # The move to aim at, so a resend is not late again for
                        # the same reason.
                        "move": snake.move_count,
                    }

            if snake.length > 1:
                current = snake.heading
                if heading[0] == -current[0] and heading[1] == -current[1]:
                    return {"ok": False, "reason": "reversal",
                            "ack": snake.last_seq}

            snake.pending_heading = heading
            return {
                "ok": True,
                "ack": snake.last_seq,
                "move": snake.move_count,
                "behind": behind,
                "next": self._next_cell(snake),
            }

    def _next_cell(self, snake: MatchSnake):
        if not snake.body:
            return None
        step = self.arena.step_from(
            snake.head[0], snake.head[1], snake.pending_heading
        )
        return list(step) if step else None

    # -- simulation -------------------------------------------------------

    def step(self) -> None:
        """Advance exactly one logic tick."""
        with self.lock:
            now = time.monotonic()

            if self.phase == PHASE_COUNTDOWN:
                if self.starts_at is not None and now >= self.starts_at:
                    self.phase = PHASE_RUNNING
                    self.started_at = time.time()
                    log.info("match under way")
                return

            if self.phase != PHASE_RUNNING:
                return

            self.tick += 1
            self._expire_remains(now)
            self._respawn_due(now)

            movers = []
            for snake in self.snakes.values():
                if not snake.alive or not snake.body:
                    continue
                snake.accumulator += 1
                if snake.accumulator >= snake.move_interval:
                    snake.accumulator = 0
                    movers.append(snake)

            if movers:
                # Decided at the moment of the move, which is both cheaper and
                # closer to what a human does: the press that counts is the
                # last one before the snake moves. Thinking every tick meant
                # every bot recomputing a decision nine times out of ten for a
                # move it was not making.
                self._think(movers, now)
                self._advance(movers, now)

            self._pull_food(now)
            self._expire_effects(now)
            self._check_end(now)

    def _pull_food(self, now: float) -> None:
        """Drag loose food one cell toward anybody holding a magnet.

        Its own rhythm rather than once per move, so the food is seen to drift
        rather than to arrive. A pull that kept pace with the snake would be a
        pickup that eats for you.

        Only free food moves. Remains stay where they fell, because they are the
        prize from a fight and dragging somebody else's cut across the arena
        would decide that fight after it ended. And a cell somebody is standing
        in is not a destination, so nothing is pulled into a body.
        """
        if now < self._next_pull:
            return
        self._next_pull = now + MAGNET_INTERVAL

        pullers = [
            snake for snake in self.snakes.values()
            if snake.alive and snake.body and snake.effects.active("magnet", now)
        ]
        if not pullers:
            return

        for cell in sorted(self.arena.food):
            if cell in self.remains:
                continue

            nearest = None
            best = MAGNET_RANGE + 1
            for snake in pullers:
                head = snake.head
                gap = abs(head[0] - cell[0]) + abs(head[1] - cell[1])
                if gap < best:
                    best = gap
                    nearest = head

            if nearest is None:
                continue

            step = self._toward(cell, nearest)
            if step is None or self.arena.at(step[0], step[1]) != EMPTY:
                continue

            self.arena.remove_food(cell[0], cell[1])
            self.arena.add_food(step[0], step[1])
            self._food_del.append([cell[0], cell[1]])
            self._food_add.append([step[0], step[1]])

    def _toward(self, cell, target):
        """One cell along whichever axis is further from the target.

        The longer axis first, so the food takes the diagonal in steps rather
        than running to one edge of it and then along.
        """
        dx = target[0] - cell[0]
        dy = target[1] - cell[1]

        if dx == 0 and dy == 0:
            return None

        if abs(dx) >= abs(dy):
            return (cell[0] + (1 if dx > 0 else -1), cell[1])
        return (cell[0], cell[1] + (1 if dy > 0 else -1))

    def _expire_effects(self, now: float) -> None:
        """Retire what has run out, and put speed back where it belongs."""
        for snake in self.snakes.values():
            for kind in snake.effects.expire(now):
                self._record("effect_end", None, snake, i=kind)
                if kind in ("slow", "burst") and snake.alive:
                    snake.move_interval = move_interval_for(
                        self.rules, snake.length, snake.effects, now
                    )

    def _respawn_due(self, now: float) -> None:
        for snake in self.snakes.values():
            if snake.alive or snake.eliminated or snake.respawn_at is None:
                continue
            if now < snake.respawn_at:
                continue
            if self._spawn(snake, now):
                self._pending_for(snake)["body"] = self._body_of(snake)

    def _advance(self, movers, now: float) -> None:
        # Every stage below runs across all movers before the next one starts.
        # Resolving one snake completely and then the next would make the
        # iteration order decide a head-on collision.
        targets = {}
        for snake in movers:
            snake.heading = snake.pending_heading
            targets[snake.id] = self.arena.step_from(
                snake.head[0], snake.head[1], snake.heading
            )

        dead = {}

        for snake in movers:
            if targets[snake.id] is None:
                dead[snake.id] = (DEATH_WALL, None)

        eating = {}
        taking = {}
        for snake in movers:
            target = targets[snake.id]
            reachable = target is not None and snake.id not in dead
            contents = (
                self.arena.at(target[0], target[1]) if reachable else None
            )
            eating[snake.id] = contents == FOOD

            # An item is walked onto and consumed, and does not feed you.
            # Growing on a poison would be the wrong signal entirely, and
            # growing on a pickup would make the pickups food with extras.
            taking[snake.id] = contents == ITEM

        self._resolve_head_on(movers, targets, dead, now)

        # A tail only frees its cell if that snake is actually moving and is not
        # growing. Not accounting for it is the classic off-by-one that kills a
        # snake for following its own tail.
        vacating = set()
        for snake in movers:
            if snake.id in dead or eating[snake.id]:
                continue
            vacating.add(snake.body[-1])

        # Who cuts whom, and where. Collected rather than applied in place,
        # because a cut frees cells that another mover in this same tick may be
        # entering, and resolving them one at a time would let the iteration
        # order decide whether that mover lives.
        cuts = {}

        for snake in movers:
            if snake.id in dead:
                continue

            target = targets[snake.id]
            if self.arena.at(target[0], target[1]) != SNAKE:
                continue
            if target in vacating:
                continue

            owner_id = self._owner_at(target)
            if owner_id is None:
                continue

            other = self.snakes.get(owner_id)
            if self._shielded(snake, other, now):
                continue

            # Phase passes through a body without touching it. One way only:
            # phasing through somebody does not cut them and does not kill you,
            # but it does not protect them from anybody else either, and it is
            # no defence against being cut by a snake that is not phasing.
            #
            # It is checked here rather than in _shielded because spawn
            # protection is mutual by design and this is deliberately not: a
            # pickup that made you both untouchable would be a way to rescue
            # whoever you ran into.
            if snake.effects.active("phase", now):
                continue

            if owner_id == snake.id:
                if self.rules.severing and self.rules.self_collision == "sever":
                    self._plan_cut(cuts, snake, snake, target)
                else:
                    dead[snake.id] = (DEATH_SELF, None)
            elif self.rules.severing:
                self._plan_cut(cuts, snake, other, target)
            else:
                dead[snake.id] = (DEATH_SNAKE, owner_id)

        # Cuts are made before anybody moves, so the cell a cut freed is empty
        # by the time the snake that made the cut arrives in it. What comes off
        # is not dropped yet: see below.
        dropped = {}
        for victim_id, (attacker, index) in cuts.items():
            self._sever(
                self.snakes[victim_id], attacker, index, dead, now, dropped
            )

        for snake in movers:
            if snake.id in dead:
                continue
            self._apply_move(
                snake, targets[snake.id], eating[snake.id],
                taking[snake.id], now,
            )

        for snake_id, (cause, killer_id) in dead.items():
            self._kill(self.snakes[snake_id], cause, killer_id, now, dropped)

        # Only now, once every snake has moved. Dropping at the moment of the
        # cut put a remain on the cell the snake that made the cut was about to
        # enter, and that snake had already been told there was nothing there to
        # eat. It moved on top of the remain instead of eating it, and from then
        # on the arena's food set and its grid disagreed about that cell: it was
        # drawn as food, it could never be eaten by anybody, and it sat there
        # until it expired.
        for owner_id, cells in dropped.items():
            self._drop_remains(cells, now, owner_id)

        # After every move, so a snake that was cut and then moved in the same
        # tick is sent the body it ended the tick with rather than the one it
        # had at the moment of the cut.
        for snake_id in self._resync:
            snake = self.snakes.get(snake_id)
            if snake is None or not snake.alive:
                continue
            record = self._pending_for(snake)
            record["body"] = self._body_of(snake)
            record["heads"] = []
            record["pop"] = 0
        self._resync.clear()

        if any(eating.values()):
            self._top_up_food()
        if any(taking.values()):
            self._top_up_items()

    def _shielded(self, snake: MatchSnake, other, now: float) -> bool:
        """True when spawn protection cancels a death between these two.

        Protection works both ways: a protected snake cannot be killed, and it
        cannot kill. One-way protection would turn a spawn into a weapon.
        """
        if snake.protected_until > now:
            return True
        if other is not None and other is not snake and other.protected_until > now:
            return True
        return False

    def _resolve_head_on(self, movers, targets, dead, now: float) -> None:
        alive_movers = [snake for snake in movers if snake.id not in dead]

        groups = {}
        for snake in alive_movers:
            groups.setdefault(targets[snake.id], []).append(snake)

        # Two snakes swapping cells pass through each other without ever sharing
        # a target, so it is checked separately rather than being missed.
        for index, snake in enumerate(alive_movers):
            for other in alive_movers[index + 1:]:
                if not snake.body or not other.body:
                    continue
                if (
                    tuple(targets[snake.id]) == tuple(other.head)
                    and tuple(targets[other.id]) == tuple(snake.head)
                ):
                    groups.setdefault(("swap", snake.id, other.id), [snake, other])

        for group in groups.values():
            if len(group) < 2:
                continue
            if any(member.protected_until > now for member in group):
                continue
            self._settle_head_on(group, dead)

    def _settle_head_on(self, group, dead) -> None:
        if self.rules.head_on == "both_die":
            for member in group:
                dead[member.id] = (DEATH_HEAD_ON, None)
            return

        longest = max(member.length for member in group)
        winners = [member for member in group if member.length == longest]

        if len(winners) != 1:
            # Equal length is a mutual loss. Picking one by any other measure
            # would be arbitrary and would read as the game choosing a side.
            for member in group:
                dead[member.id] = (DEATH_HEAD_ON, None)
            return

        winner = winners[0]
        for member in group:
            if member is not winner:
                dead[member.id] = (DEATH_HEAD_ON, winner.id)

    def _apply_move(self, snake: MatchSnake, target, ate: bool, took: bool,
                    now: float) -> None:
        cell = (target[0], target[1])

        # Read before the cell is occupied. Occupying overwrites the cell value
        # and the kind would then have to be recovered from a dict keyed by a
        # cell that no longer says it holds an item.
        kind = self.arena.item_at(cell[0], cell[1]) if took else None
        snake.last_move_at = now
        self._enters(snake, self.topology.arena_at(cell[0], cell[1]))

        snake.body.appendleft(cell)
        self._occupy_shared(cell, snake.id)
        snake.move_count += 1

        record = self._pending_for(snake)
        record["heads"].append([cell[0], cell[1]])

        if ate:
            remain = self.remains.pop(cell, None)
            self.arena.remove_food(cell[0], cell[1])
            self._occupy(cell, snake.id)
            self._food_del.append([cell[0], cell[1]])

            # Your own segment is length you already paid for. Everything else
            # on the floor, food or somebody else's remains, is a point.
            if remain is None or remain[1] != snake.id:
                snake.score += 1
        else:
            removed = snake.body.pop()
            self._clear(removed, snake.id)
            record["pop"] += 1

        if kind is not None:
            self.arena.remove_item(cell[0], cell[1])
            self._item_del.append([cell[0], cell[1]])
            self._take_item(snake, kind, now)

        snake.move_interval = move_interval_for(
            self.rules, snake.length, snake.effects, now
        )

    def _enters(self, snake, arena_id: int) -> None:
        """Move a snake into an arena, and owe that arena's stream a keyframe.

        A player who has just crossed has no state of the stream they are now
        being sent, so the first thing it can usefully say to them is
        everything. Everyone already in that arena gets the keyframe too, which
        is a message they did not need at most twice a second on a busy grid and
        is far cheaper than the alternative of a per-player stream.
        """
        if snake.arena_id != arena_id and not self.topology.single:
            self._keyframe_for.add(arena_id)
        snake.arena_id = arena_id

    def _record(self, kind: str, actor, target, **extra) -> None:
        self._event_id += 1

        event = {"id": self._event_id, "k": kind}
        if actor is not None:
            event["a"] = actor.username
            event["ac"] = actor.colour
        if target is not None:
            event["t"] = target.username
            event["tc"] = target.colour

        # Where it happened, on a grid. A feed that says somebody was cut down
        # is reporting something you may not have been able to see, and on four
        # arenas the first question is which one. Left out entirely on a single
        # arena, where the answer is never in doubt and the field would be two
        # bytes on every event for nothing.
        if not self.topology.single:
            where = target if target is not None else actor
            if where is not None:
                event["w"] = where.arena_id

        event.update(extra)

        self.events.append((time.monotonic(), event))

    def _recent_events(self, now: float) -> list:
        """Everything still inside the window. Sent whole in a keyframe only."""
        while self.events and now - self.events[0][0] > EVENT_WINDOW:
            self.events.popleft()
        return [event for _, event in self.events]

    def _new_events(self, now: float, stream=None, record: bool = True) -> list:
        """Only what has happened since the last message.

        Sending the whole window every time was costing a sixth of every
        snapshot on a busy match, because an event that lasts five seconds was
        being retransmitted a hundred times at twenty messages a second. A
        keyframe still carries the whole window, which is what a client that has
        just arrived needs, and it arrives every second anyway.
        """
        marker = self._events_sent.get(stream, 0)
        fresh = [
            event
            for _, event in self._recent_events_pairs(now)
            if event["id"] > marker
        ]
        if fresh and record:
            self._events_sent[stream] = fresh[-1]["id"]
        return fresh

    def _recent_events_pairs(self, now: float):
        while self.events and now - self.events[0][0] > EVENT_WINDOW:
            self.events.popleft()
        return list(self.events)

    def _plan_cut(self, cuts: dict, attacker, victim, cell) -> None:
        """Record where a victim is to be cut, keeping the deepest cut.

        Two snakes can hit the same victim on the same tick. Taking the cut
        nearest the head takes the more severe of the two, so the victim is not
        cut twice and does not keep a piece that the other collision would
        already have taken.
        """
        try:
            index = list(victim.body).index((cell[0], cell[1]))
        except ValueError:
            return

        existing = cuts.get(victim.id)
        if existing is None or index < existing[1]:
            cuts[victim.id] = (attacker, index)

    def _sever(
        self, victim, attacker, index: int, dead: dict, now: float, dropped: list
    ) -> None:
        """Cut a victim at index, dropping what is lost onto the floor.

        The attacker gains nothing directly. That is the point of the rule: what
        comes off is on the ground for anybody to eat, including the snake it
        came from, so a cut creates a contested prize rather than a transfer.
        """
        lost = list(victim.body)[index:]
        if not lost:
            return

        for cell in lost:
            self._clear(cell, victim.id)

        for _ in range(len(lost)):
            victim.body.pop()

        # A cut costs a fraction of what those segments earned, not all of it.
        #
        # Charging the full amount made severing the strongest move in the game
        # and made a score race unwinnable: cutting a snake of a hundred near
        # its head took it back to almost nothing, and since cutting is easy,
        # nobody could hold a lead. Charging nothing at all is the other cliff,
        # because snakes get faster as they shorten, so a cut would be a reward.
        #
        # A small percentage keeps a cut a real loss without undoing an hour of
        # eating, and the segments themselves are still the main currency: they
        # are on the floor, and whoever collects them scores them.
        victim.score = max(
            0,
            victim.score - round(len(lost) * self.rules.sever_score_cost / 100.0),
        )
        victim.move_interval = move_interval_for(
            self.rules, victim.length, victim.effects, now
        )

        keep = round(len(lost) * self.rules.remains_yield / 100.0)
        dropped.setdefault(victim.id, []).extend(lost[:keep])

        # A cut is not expressible as heads gained and tail lost, which is all a
        # delta can say, so this snake's body goes out whole in the next one.
        self._resync.add(victim.id)

        if victim.length < self.rules.min_length_before_death:
            killer = None if attacker is victim else attacker.id
            dead[victim.id] = (DEATH_SEVERED, killer)
            return

        self._record(
            "sever",
            None if attacker is victim else attacker,
            victim,
            n=len(lost),
        )

    def _drop_remains(self, cells, now: float, owner_id: int) -> None:
        """Leave segments on the floor as food, for a while, remembering whose.

        Whose matters. A snake eating its own remains is picking up something it
        already earned, so it gets the length back and no points: otherwise the
        way to farm a score is to be cut and then eat yourself, over and over.
        Anybody else eating them is taking something off the floor that they did
        not earn until now, which is what makes a cut worth making.

        A segment only lands where the arena is actually empty. Anywhere else it
        is simply gone, which is the only answer that keeps the food set and the
        grid saying the same thing about every cell.
        """
        expires = now + self.rules.remains_lifetime

        for cell in cells:
            if self.arena.at(cell[0], cell[1]) != EMPTY:
                continue
            self.arena.add_food(cell[0], cell[1])
            self.remains[cell] = (expires, owner_id)
            self._food_add.append([cell[0], cell[1]])

    def _expire_remains(self, now: float) -> None:
        for cell, (expires, _owner) in list(self.remains.items()):
            if now < expires:
                continue
            del self.remains[cell]
            if cell in self.arena.food:
                self.arena.remove_food(cell[0], cell[1])
                self._food_del.append([cell[0], cell[1]])

    def _kill(
        self, snake: MatchSnake, cause: str, killer_id, now: float,
        dropped: dict = None,
    ) -> None:
        snake.alive = False
        snake.death_cause = cause
        snake.deaths += 1
        snake.deaths_length = snake.length

        # A killed snake leaves its body on the floor, exactly as a cut one
        # does. It used to vanish, which made killing somebody worth strictly
        # less than cutting them: a cut dropped a pile worth taking and a kill
        # dropped nothing. That is the wrong way round, and it is the whole
        # reason the two never felt like they were part of the same game.
        keep = round(len(snake.body) * self.rules.remains_yield / 100.0)
        if dropped is not None and keep:
            dropped.setdefault(snake.id, []).extend(list(snake.body)[:keep])

        for cell in snake.body:
            self._clear(cell, snake.id)
        snake.body.clear()

        record = self._pending_for(snake)
        record["body"] = []

        killer = None
        if killer_id is not None and killer_id != snake.id:
            killer = self.snakes.get(killer_id)
            if killer is not None:
                killer.kills += 1
                # A bounty, so a kill is worth something even to somebody who
                # cannot reach the body before it decays or before the next
                # snake gets there. Set it to nothing and the only reward for a
                # kill is the pile it leaves, which is a different game and a
                # legitimate one.
                killer.score += round(
                    snake.deaths_length * self.rules.kill_bounty / 100.0
                )

        self._record("death", killer, snake, c=cause)

        if self.rules.lives != UNLIMITED_LIVES:
            snake.lives_left = max(0, snake.lives_left - 1)
            if snake.lives_left == 0:
                snake.eliminated = True
                snake.respawn_at = None
                self._record("out", None, snake)
                log.info("player %s is out", snake.username)
                return

        snake.respawn_at = now + self.rules.respawn_delay

    def _top_up_food(self) -> None:
        # Remains are food to eat but not food to count. Letting them count
        # would mean a big fight suppressed every natural spawn until the pile
        # decayed, and then the arena would be suddenly empty.
        # The rule set states a density for one arena, so a grid wants that
        # many times however many arenas it has.
        target = self.rules.food_target() * self.topology.count

        before = set(self.arena.food)
        spawn_food(self.arena, target + len(self.remains), self.random)
        for cell in self.arena.food - before:
            self._food_add.append([cell[0], cell[1]])

    def _top_up_items(self) -> None:
        """Keep the poisons and the pickups stocked, per family.

        Two families rather than one pool, so a host who wants a hostile arena
        and no rewards gets exactly that. The target is per arena and multiplied
        by the layout, for the same reason food is: a density is what you should
        see wherever you are standing.
        """
        for density, family in (
            (self.rules.poisons, POISONS),
            (self.rules.pickups, PICKUPS),
        ):
            kinds = enabled_kinds(self.rules, family)
            target = item_target(self.rules, density, kinds)
            if not target:
                continue

            added = spawn_items(
                self.arena,
                kinds,
                target * self.topology.count,
                self.random,
            )
            for cell in added:
                self._item_add.append(
                    [cell[0], cell[1], self.arena.items[cell]]
                )

    def _take_item(self, snake: MatchSnake, kind: str, now: float) -> None:
        """Eat an item. What it does is decided here and nowhere else."""
        if kind == "shrink":
            self._shrink(snake, now)
        else:
            snake.effects.apply(kind, self.rules.effect_duration, now)

        self._record("item", None, snake, i=kind)

    def _shrink(self, snake: MatchSnake, now: float) -> None:
        """Take segments off the tail, and charge for them like a cut.

        The same economy as severing, because losing length has to cost the same
        however it happened, or the cheaper of the two ways becomes the one
        everybody uses. What is different is that nothing lands on the floor:
        segments from a cut are a prize somebody fought for, and re-eating your
        own poison would simply undo it.

        It is never fatal. A poison is a setback, and a death with nobody to
        blame for it reads as the game breaking rather than as a mechanic.
        """
        floor = max(1, self.rules.min_length_before_death)
        losing = min(SHRINK_SEGMENTS, snake.length - floor)
        if losing <= 0:
            return

        lost = list(snake.body)[-losing:]
        for cell in lost:
            self._clear(cell, snake.id)
        for _ in range(losing):
            snake.body.pop()

        snake.score = max(
            0,
            snake.score - round(losing * self.rules.sever_score_cost / 100.0),
        )

        # Losing a run of tail is not expressible as heads gained and a tail
        # count, so this body goes out whole in the next message.
        self._resync.add(snake.id)

    # -- ending -----------------------------------------------------------

    def _standing_order(self) -> list:
        """Ranked by what this match is actually being won on.

        Ranking on score whatever the rules were is how a player who is losing
        appears to be first. In a match won by outlasting everybody, somebody
        with fifty points and no lives left is not ahead of somebody with ten
        points who is still alive; they are out, and the board should say so.
        Every ordering below ends in the same tiebreakers, so within the thing
        that decides the match, points still separate people.
        """
        condition = self.rules.win_condition

        def rest(snake):
            return (-snake.score, -snake.kills, snake.deaths, snake.username)

        def key(snake):
            if condition == "first_to_kills":
                return (-snake.kills,) + rest(snake)
            if condition == "last_standing":
                # Out last, then whoever has most left to lose.
                return (snake.eliminated, -snake.lives_left) + rest(snake)
            return rest(snake)

        return sorted(self.snakes.values(), key=key)

    def _still_playing(self) -> list:
        return [snake for snake in self.snakes.values() if not snake.eliminated]

    def _check_end(self, now: float) -> None:
        # Before anything the win condition has to say. A match that nobody can
        # still play is over whatever it was being played for, and leaving it
        # running left everybody watching an empty arena with no way to agree on
        # another game. A score target nobody is left to reach is not a match in
        # progress.
        #
        # Measured against who is in the room now, not who started. Against the
        # starting count, somebody leaving a two player match ended it for the
        # player still going, which is a quit deciding a match rather than a
        # result. Being out of lives ends a match; walking away does not.
        # Bots do not keep a match alive on their own. A room where every
        # human is out and four bots are still circling is over: nobody is
        # playing it, and leaving it running means everybody watching a
        # demonstration with no way to agree on another game.
        if len(self.snakes) > 1:
            playing = self._still_playing()
            humans = [snake for snake in playing if not snake.is_bot]

            if not humans and self.bots:
                self._finish("everyone_out")
                return

            if len(playing) == 1:
                self._finish("last_standing")
                return
            if not playing:
                self._finish("everyone_out")
                return

        condition = self.rules.win_condition

        if condition == "endless":
            return

        if condition == "last_standing":
            # Handled above, for every condition rather than only this one.
            return

        if condition == "first_to_score":
            for snake in self.snakes.values():
                if snake.score >= self.rules.score_target:
                    self._finish("score_target")
                    return
            return

        if condition == "first_to_kills":
            for snake in self.snakes.values():
                if snake.kills >= self.rules.kill_target:
                    self._finish("kill_target")
                    return
            return

        if condition == "timed" and self.started_at is not None:
            if self.elapsed_seconds() >= self.rules.time_limit * 60:
                self._finish("time_limit")

    def _finish(self, reason: str) -> None:
        self.phase = PHASE_OVER
        self.ended_at = time.time()

        order = self._standing_order()

        if reason in ("last_standing", "everyone_out"):
            remaining = self._still_playing()
            winner = remaining[0] if len(remaining) == 1 else None
        else:
            winner = order[0] if order else None

        self.outcome = {
            "reason": reason,
            "winner": winner.id if winner else None,
            "winner_name": winner.username if winner else None,
            "duration_seconds": round(self.elapsed_seconds(), 1),
            "standings": self.standings(),
        }

        log.info(
            "match over, %s, winner %s",
            reason,
            winner.username if winner else "nobody",
        )

    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.ended_at or time.time()) - self.started_at

    def standings(self) -> list:
        return [
            {
                "id": snake.id,
                "username": snake.username,
                "colour": snake.colour,
                "score": snake.score,
                "kills": snake.kills,
                "deaths": snake.deaths,
                "lives": snake.lives_left,
                "alive": snake.alive,
                "eliminated": snake.eliminated,
                "length": snake.length,
            }
            for snake in self._standing_order()
        ]

    # -- reporting --------------------------------------------------------

    def _pending_for(self, snake: MatchSnake) -> dict:
        record = self._pending.get(snake.id)
        if record is None:
            record = {"heads": [], "pop": 0, "body": None}
            self._pending[snake.id] = record
        return record

    def _body_of(self, snake: MatchSnake) -> list:
        return [[x, y] for x, y in snake.body]

    def record_send_gap(self, gap_ms: float) -> None:
        with self.lock:
            self.send_gaps.append(gap_ms)

    def cadence(self) -> dict:
        """What the broadcast rate really is, and how steady the tick is."""
        with self.lock:
            gaps = sorted(self.send_gaps)

        if not gaps:
            return {"samples": 0}

        return {
            "samples": len(gaps),
            "median_ms": round(gaps[len(gaps) // 2], 1),
            "p95_ms": round(gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))], 1),
            "worst_ms": round(gaps[-1], 1),
            "tick_drift_ms": round(self.max_drift_ms, 1),
            "late_ticks": self.late_ticks,
        }

    def _clock(self, now: float) -> dict:
        countdown = 0
        if self.phase == PHASE_COUNTDOWN and self.starts_at is not None:
            countdown = max(0, round((self.starts_at - now) * 1000))

        remaining = None
        if self.rules.win_condition == "timed":
            remaining = max(
                0,
                round(self.rules.time_limit * 60 - self.elapsed_seconds()),
            )

        return {
            "countdown_ms": countdown,
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "remaining_seconds": remaining,
        }

    def _stats_for(self, snake: MatchSnake, now: float) -> dict:
        # How far through its current move this snake is, measured in seconds
        # since it last moved rather than in ticks accumulated since it last
        # moved.
        #
        # This is the difference between a design that needs an accurate clock
        # and one that only needs a monotonic one. The tick counter advances in
        # whole ticks, so the fraction it produces is quantised to whenever the
        # scheduler happened to wake the loop up. Where the operating system
        # rounds a sleep up, ticks arrive in bursts, and every snake on every
        # screen inherits that: a run of frames reporting the same progress,
        # then a jump.
        #
        # Time answers the question directly. Two snapshots taken a millisecond
        # apart report progress a millisecond apart whether a tick happened
        # between them or not, so what the player sees is smooth on any
        # platform, whatever the scheduler is doing.
        progress = 0.0
        if snake.move_interval > 0 and self.phase == PHASE_RUNNING and snake.alive:
            span = snake.move_interval / float(TICK_HZ)
            if span > 0:
                # Capped just under a whole move: a move that is running late
                # must not draw the head inside the cell it has not reached.
                progress = min(0.999, max(0.0, (now - snake.last_move_at) / span))

        respawn_in = None
        if not snake.alive and snake.respawn_at is not None:
            respawn_in = max(0, round((snake.respawn_at - now) * 1000))

        return {
            "id": snake.id,
            "c": snake.colour,
            "name": snake.username,
            "a": snake.arena_id,
            "bot": snake.is_bot,
            "h": list(snake.heading),
            "ph": list(snake.pending_heading),
            "n": self._next_cell(snake),
            "p": round(progress, 3),
            "alive": snake.alive,
            "eliminated": snake.eliminated,
            "score": snake.score,
            "kills": snake.kills,
            "deaths": snake.deaths,
            "lives": snake.lives_left,
            "len": snake.length,
            "move": snake.move_count,
            "ack": snake.last_seq,
            "late": snake.worst_input_lag,
            "interval_ms": round(1000.0 * snake.move_interval / TICK_HZ, 1),
            "mps": round(TICK_HZ / max(1, snake.move_interval), 2),
            "protected": snake.protected_until > now,
            "fx": snake.effects.to_wire(now),
            "respawn_in": respawn_in,
            "death": snake.death_cause,
        }

    # -- who is told what -------------------------------------------------
    #
    # A stream is one arena's worth of the match. On a single arena there is one
    # stream, keyed None, carrying everything, which is exactly what this was
    # before layouts existed.
    #
    # What is scoped is position: bodies and food. What is not scoped is the
    # match: every snake appears in every message with its name, score, lives
    # and which arena it is in, because the leaderboard, the minimap and the
    # event feed are about the match rather than about the arena, and a player
    # who cannot see who is winning is not being made to hunt, only confused.
    #
    # So the line drawn here, and it is worth stating plainly because every
    # later question about the layout comes back to it: **which arena somebody
    # is in is public, and where in that arena they are is not.**
    #
    # A snake with any part of its body in your arena is sent whole, including
    # the part of it that is not in your arena. Trimming a body at the boundary
    # would mean sending it in full every message rather than as the cells it
    # gained and lost, and would leave the drawn tail cut off in mid-slide. What
    # it would buy is hiding the shape of a snake that is already half in the
    # room with you.

    def _streams(self) -> list:
        if self.topology.single:
            return [None]
        return list(range(self.topology.count))

    def streams(self) -> list:
        """Every stream this match broadcasts on, in order.

        Public because the server needs it when next_messages is the thing that
        failed and there is no set of messages to read the streams out of.
        """
        with self.lock:
            return list(self._streams())

    def stream_for(self, player_id: int):
        """Which stream a player should be receiving right now."""
        with self.lock:
            if self.topology.single:
                return None
            snake = self.snakes.get(player_id)
            return None if snake is None else snake.arena_id

    def _arenas_of(self, snake) -> set:
        """Every arena the snake occupies, which is two while it is crossing.

        Answered from a cache while a broadcast is being built. Every stream
        asks about every snake, so on a 2x2 with a full room this was walking
        twelve bodies four times over to reach the same answer each time.
        """
        cached = self._arena_cache
        if cached is not None:
            found = cached.get(snake.id)
            if found is not None:
                return found

        if not snake.body:
            arenas = {snake.arena_id}
        else:
            arenas = {
                self.topology.arena_at(cell[0], cell[1]) for cell in snake.body
            }

        if cached is not None:
            cached[snake.id] = arenas
        return arenas

    def _sees(self, snake, stream) -> bool:
        return stream is None or stream in self._arenas_of(snake)

    def _food_for(self, stream) -> list:
        if stream is None:
            return sorted(self.arena.food)
        return [
            cell for cell in sorted(self.arena.food)
            if self.topology.arena_at(cell[0], cell[1]) == stream
        ]

    def keyframe(self, stream=None, record: bool = True) -> dict:
        """Complete state. Everything a client needs with no prior knowledge.

        record is what separates a message that was sent from a payload that was
        merely built. The host's own screen asks for one of these many times a
        second, and recording those as things a client has been told meant a
        joined player never being sent a score that changed between two of them.
        """
        with self.lock:
            now = time.monotonic()

            snakes = []
            known = {}
            shown = set()

            for snake in self.snakes.values():
                entry = self._stats_for(snake, now)
                if self._sees(snake, stream):
                    entry["b"] = self._body_of(snake)
                    shown.add(snake.id)
                else:
                    # Present, so the leaderboard has them, and empty, so
                    # nothing of them is drawn.
                    entry["b"] = []
                snakes.append(entry)
                # A keyframe says everything, so everything is now known.
                known[snake.id] = {
                    field: value for field, value in entry.items()
                    if field != "b"
                }

            if record:
                self._sent_fields[stream] = known
                self._shown[stream] = shown

            message = {
                "type": "keyframe",
                "t": self.tick,
                "phase": self.phase,
                # w and h are the whole cell space, which is what every
                # coordinate in this message is in. The arena size and the
                # layout are what a client draws a window of it with.
                "arena": dict(
                    self.topology.to_dict(),
                    w=self.arena.width,
                    h=self.arena.height,
                    edge=self.arena.edge_behaviour,
                ),
                "rules": self.rules.to_dict(),
                "snakes": snakes,
                "food": [[x, y] for x, y in self._food_for(stream)],
                "items": self._items_for(stream),
                "rank": [snake["id"] for snake in self.standings()],
                "outcome": self.outcome,
                "cadence": self.cadence(),
            }
            message.update(self._clock(now))

            # The whole window, and the marker moves with it, so the delta that
            # follows a keyframe does not repeat what the keyframe just carried.
            message["events"] = self._recent_events(now)
            self._roll_up(message, stream, force=True, record=record)

            if record:
                self._events_sent[stream] = self._event_id

            return message

    def _arena_roll_up(self) -> list:
        """How many live snakes are in each arena, in arena id order.

        The minimap runs on this and on nothing finer. Where inside an arena
        somebody is standing is exactly what a layout exists to hide, so it is
        not sent: a count says an arena is worth avoiding without saying which
        corner to avoid.

        It is also what keeps the minimap working when snapshots are scoped to
        one arena, because a count is not derivable from a message that no
        longer mentions the snakes it counts.
        """
        counts = [0] * self.topology.count
        for snake in self.snakes.values():
            if snake.alive and snake.body:
                counts[min(len(counts) - 1, snake.arena_id)] += 1
        return counts

    def _roll_up(self, message: dict, stream, force: bool,
                 record: bool = True) -> None:
        """Put the roll-up in a message only when it says something new.

        It changes when somebody crosses, dies or respawns, which is rarely, and
        repeating four numbers twenty times a second to say nothing is the habit
        the snapshot work was done to break. A client keeps the last one it was
        given.
        """
        if self.topology.single:
            return

        counts = self._arena_roll_up()
        if force or counts != self._map_sent.get(stream):
            message["map"] = counts
            if record:
                self._map_sent[stream] = list(counts)

    def _changed_only(self, entry: dict, stream=None) -> dict:
        """Strip a snake entry down to what the other end does not already know.

        A snapshot used to describe every snake completely, twenty times a
        second, including its name and its colour. Almost none of it differed
        from the message before. What is left after this is the id, the
        progress, and whatever actually moved.
        """
        known = self._sent_fields.setdefault(stream, {}).setdefault(
            entry["id"], {}
        )
        trimmed = {}

        for field, value in entry.items():
            if field in ALWAYS_SENT or known.get(field, _MISSING) != value:
                trimmed[field] = value
            known[field] = value

        return trimmed

    def delta(self, stream=None) -> dict:
        """The change since the previous call, for one stream, and clear.

        The single-stream case, which is what a test and a single arena both
        want. next_messages builds several against the same interval and clears
        once at the end instead.
        """
        with self.lock:
            message = self._delta_payload(stream, record=True)
            self._clear_accumulated()
            return message

    def _delta_payload(self, stream=None, record: bool = True) -> dict:
        """The change since the previous call, and the state that cannot delta.

        Bodies delta as the cells a head gained and the number a tail lost.
        Everything else about a snake deltas by simply not being repeated when
        it has not changed. Progress is the exception, because it is different
        in every message by definition, and it is what the drawing runs on.

        Scoped to one arena this is the same message with two additions. A snake
        that has just come into the arena gets its whole body, because the cells
        it gained since the last message are meaningless to a client that has
        none of the rest of it. A snake that has just left gets an empty one,
        once, because a body that simply stopped being mentioned would be left
        on the screen where it was last seen.

        Clearing what has been accumulated is the caller's job, so that every
        stream describes the same interval.
        """
        with self.lock:
            now = time.monotonic()

            was_shown = self._shown.get(stream, set())
            shown = set()

            snakes = []
            for snake in self.snakes.values():
                visible = self._sees(snake, stream)
                arrived = visible and snake.id not in was_shown
                left = not visible and snake.id in was_shown

                if arrived and record:
                    # Nothing this stream said about it before counts, because
                    # it was talking about a snake nobody could see.
                    self._sent_fields.setdefault(stream, {}).pop(snake.id, None)

                entry = self._changed_only(self._stats_for(snake, now), stream)

                if arrived:
                    entry["b"] = self._body_of(snake)
                elif left:
                    entry["b"] = []
                elif visible:
                    pending = self._pending.get(snake.id)
                    if pending is not None:
                        if pending["body"] is not None:
                            entry["b"] = pending["body"] or self._body_of(snake)
                        if pending["heads"]:
                            entry["heads"] = pending["heads"]
                        if pending["pop"]:
                            entry["pop"] = pending["pop"]

                if visible:
                    shown.add(snake.id)
                snakes.append(entry)

            if record:
                self._shown[stream] = shown

            message = {
                "type": "snapshot",
                "t": self.tick,
                "base": self._base_tick,
                "phase": self.phase,
                "snakes": snakes,
                "food_add": self._scoped_cells(self._food_add, stream),
                "food_del": self._scoped_cells(self._food_del, stream),
                "rank": [snake.id for snake in self._standing_order()],
                "outcome": self.outcome,
            }
            # Left out entirely when nothing happened to them, which is nearly
            # every message in a match with the items switched off.
            item_add = self._scoped_cells(self._item_add, stream)
            if item_add:
                message["item_add"] = item_add
            item_del = self._scoped_cells(self._item_del, stream)
            if item_del:
                message["item_del"] = item_del

            message.update(self._clock(now))
            message["events"] = self._new_events(now, stream, record)
            self._roll_up(message, stream, force=False, record=record)

            return message

    def _items_for(self, stream) -> list:
        return [
            [cell[0], cell[1], kind]
            for cell, kind in sorted(self.arena.items.items())
            if stream is None or self.topology.arena_at(*cell) == stream
        ]

    def _scoped_cells(self, cells, stream) -> list:
        if stream is None:
            return list(cells)
        return [
            cell for cell in cells
            if self.topology.arena_at(cell[0], cell[1]) == stream
        ]

    def _clear_accumulated(self) -> None:
        """Forget what every stream has now been told about, once."""
        self._pending = {}
        self._food_add = []
        self._food_del = []
        self._item_add = []
        self._item_del = []
        self._base_tick = self.tick

    def request_keyframe(self) -> None:
        """Make the next broadcast a keyframe.

        Called when somebody arrives, because a delta means nothing to a client
        with no state to apply it against.
        """
        with self.lock:
            self._force_keyframe = True

    def next_messages(self) -> dict:
        """One message per stream, all describing the same interval.

        Built together and cleared once, which is what lets every stream share
        a base tick: a delta is the change since the last broadcast, and there
        is one last broadcast rather than one per arena. A stream that is owed a
        keyframe gets one without disturbing the others.

        The counter is advanced here rather than by the caller so that a
        keyframe cannot be skipped by a caller that forgets to count.
        """
        with self.lock:
            periodic = (
                self._force_keyframe
                or self._snapshots_sent % KEYFRAME_EVERY == 0
            )
            self._snapshots_sent += 1
            self._force_keyframe = False

            owed = self._keyframe_for
            self._keyframe_for = set()

            self._arena_cache = {}
            messages = {}
            for stream in self._streams():
                if periodic or stream in owed:
                    messages[stream] = self.keyframe(stream)
                else:
                    messages[stream] = self._delta_payload(stream)

            self._arena_cache = None
            self._clear_accumulated()
            return messages

    def next_message(self) -> dict:
        """The whole grid as one message, for a caller that wants all of it."""
        with self.lock:
            due = (
                self._force_keyframe
                or self._snapshots_sent % KEYFRAME_EVERY == 0
            )
            self._snapshots_sent += 1
            self._force_keyframe = False

            message = (
                self.keyframe(None) if due else self._delta_payload(None)
            )
            self._clear_accumulated()
            return message

    def state(self, own_id=None) -> dict:
        """Full state for a local browser. The host's own view of the match.

        Scoped like anybody else's, because a host that could see into every
        arena would be playing a different game from the people who joined it.

        It records nothing. This is asked for once per drawn frame, far more
        often than anything is broadcast, and marking those fields as sent left
        joined players never being told about a score that changed between two
        of the host's frames.
        """
        with self.lock:
            stream = self.stream_for(own_id) if own_id is not None else None
            frame = self.keyframe(stream, record=False)
            frame["standings"] = standings_from(frame["snakes"], frame["rank"])
            return frame


class MatchView:
    """A client's copy of the match, folded from keyframes and deltas.

    The rule that makes this worth having a class of its own: **the newest
    state is the truth and a backlog is never replayed.** A stalled link does
    not lose frames, it delivers them in a clump, and applying seven of them as
    seven rendered steps is seven frames of fast-forward. Every message that
    arrives is folded into this state immediately, which costs nothing, and the
    renderer only ever reads where the fold ended up.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.state_dict = None
        self.updated_at = 0.0
        self.applied = 0
        self.dropped = 0

        # Events, held with the moment each arrived. A delta carries only what
        # is new, so unlike everything else in this class the client is the one
        # that decides when an event stops being shown. Timed from arrival
        # rather than from anything in the message, so a clock that disagrees
        # with the host's cannot make an entry stick or vanish early.
        self.events = deque()

        # The gaps between arriving messages. The host measures the gaps between
        # the ones it sends; this measures the ones that land. Comparing the two
        # separates a host that is broadcasting unevenly from a link that is
        # delivering unevenly, which look identical from inside the game.
        self.arrivals = deque(maxlen=200)

        # Completed states, oldest first, each with the moment it landed here.
        # This is what makes drawing an interpolation rather than a guess.
        self.frames = deque(maxlen=FRAME_BUFFER)

        # The moment being drawn, and the delay it is drawn at. Both are held
        # rather than recomputed from scratch, for one reason: the render moment
        # must never move backwards.
        #
        # The delay follows the link, so it changes. Subtracting a changing
        # delay from a rising clock does not give a rising answer: the instant
        # the delay grows, the moment being drawn jumps into the past, and every
        # snake on screen takes a step backwards. That is worse than the
        # stutter this was built to remove, and it is invisible in any test that
        # only looks at one frame.
        self._render_at = None
        self._delay = None

        # How many moves a snake gained in a single message, over the last few
        # seconds. One is the normal case. More than one means the link stalled
        # and released, and it is the number of cells a snake appears to jump,
        # which is the thing a player actually sees. Milliseconds behind is the
        # honest measurement; moves jumped is the one somebody can act on.
        self.bursts = deque()

    def clear(self) -> None:
        with self.lock:
            self.state_dict = None
            self.applied = 0
            self.dropped = 0
            self.bursts.clear()
            self.events.clear()
            self.arrivals.clear()
            self.frames.clear()
            self._render_at = None
            self._delay = None

    def apply(self, message: dict) -> bool:
        kind = message.get("type")
        if kind == "keyframe":
            return self._apply_keyframe(message)
        if kind == "snapshot":
            return self._apply_delta(message)
        return False

    def _apply_keyframe(self, message: dict) -> bool:
        with self.lock:
            snakes = {}
            for entry in message.get("snakes") or []:
                snake = dict(entry)
                snake["b"] = [list(cell) for cell in snake.get("b") or []]
                snakes[snake["id"]] = snake

            self.state_dict = {
                "t": message.get("t", 0),
                "phase": message.get("phase"),
                "arena": message.get("arena"),
                "rules": message.get("rules"),
                "snakes": snakes,
                "food": {
                    (cell[0], cell[1]) for cell in message.get("food") or []
                },
                "items": {
                    (cell[0], cell[1]): cell[2]
                    for cell in message.get("items") or []
                },
                "rank": message.get("rank") or [],
                "map": message.get("map") or [],
                "outcome": message.get("outcome"),
                "countdown_ms": message.get("countdown_ms", 0),
                "elapsed_seconds": message.get("elapsed_seconds", 0),
                "remaining_seconds": message.get("remaining_seconds"),
            }

            # A keyframe carries the whole window, so it replaces rather than
            # adds. That is also what puts a client that has just arrived in
            # step with everybody else.
            now = time.monotonic()
            self.events.clear()
            self._take_events(message, now)
            self._record_arrival(now)

            self.updated_at = now
            self.applied += 1
            self.frames.append((now, self._frame()))
            return True

    def _apply_delta(self, message: dict) -> bool:
        with self.lock:
            current = self.state_dict
            if current is None:
                self.dropped += 1
                return False

            # A delta only means anything applied to the state it was computed
            # against. Anything else is discarded and the next keyframe, which
            # is at most a second away, puts the client back on its feet.
            if message.get("base") != current["t"]:
                self.dropped += 1
                return False

            snakes = current["snakes"]
            seen = set()
            burst = 0

            for entry in message.get("snakes") or []:
                snake_id = entry.get("id")
                seen.add(snake_id)

                existing = snakes.get(snake_id)
                body = existing["b"] if existing else []

                if "b" in entry:
                    body = [list(cell) for cell in entry["b"]]
                else:
                    heads = entry.get("heads") or []
                    burst = max(burst, len(heads))
                    for head in heads:
                        body.insert(0, list(head))
                    for _ in range(entry.get("pop") or 0):
                        if body:
                            body.pop()

                # Merged onto what is already known, not substituted for it. A
                # delta now names only the fields that changed, so replacing
                # would drop a snake's name the first time it did not move.
                merged = dict(existing) if existing else {}
                merged.update(entry)
                merged.pop("heads", None)
                merged.pop("pop", None)
                merged["b"] = body
                snakes[snake_id] = merged

            for snake_id in list(snakes):
                if snake_id not in seen:
                    del snakes[snake_id]

            for cell in message.get("food_del") or []:
                current["food"].discard((cell[0], cell[1]))
            for cell in message.get("food_add") or []:
                current["food"].add((cell[0], cell[1]))

            # Absent means nothing changed, which is why they are left out of a
            # message rather than sent empty.
            for cell in message.get("item_del") or []:
                current["items"].pop((cell[0], cell[1]), None)
            for cell in message.get("item_add") or []:
                current["items"][(cell[0], cell[1])] = cell[2]

            current["t"] = message.get("t", current["t"])
            current["phase"] = message.get("phase")
            current["rank"] = message.get("rank") or []
            # Kept rather than overwritten. The roll-up is sent only when it
            # changes, so a message without one is saying it has not.
            if "map" in message:
                current["map"] = message["map"]
            current["outcome"] = message.get("outcome")
            current["countdown_ms"] = message.get("countdown_ms", 0)
            current["elapsed_seconds"] = message.get("elapsed_seconds", 0)
            current["remaining_seconds"] = message.get("remaining_seconds")
            # Taken whole rather than merged. The host already prunes to the
            # window and sends what survives every time, so the newest message
            # is the whole truth and a client keeps no event state of its own.
            now = time.monotonic()
            self._take_events(message, now)

            self._record_arrival(now)
            self.bursts.append((now, burst))
            while self.bursts and now - self.bursts[0][0] > BURST_WINDOW:
                self.bursts.popleft()

            self.updated_at = now
            self.applied += 1
            self.frames.append((now, self._frame()))
            return True

    def _record_arrival(self, now: float) -> None:
        if self.updated_at:
            self.arrivals.append((now - self.updated_at) * 1000.0)

    def cadence(self) -> dict:
        """How evenly messages are landing here. Same shape as the host's."""
        gaps = sorted(self.arrivals)
        if not gaps:
            return {"samples": 0}

        return {
            "samples": len(gaps),
            "median_ms": round(gaps[len(gaps) // 2], 1),
            "p95_ms": round(gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))], 1),
            "worst_ms": round(gaps[-1], 1),
        }

    def _take_events(self, message: dict, now: float) -> None:
        for event in message.get("events") or []:
            self.events.append((now, event))
        while self.events and now - self.events[0][0] > EVENT_WINDOW:
            self.events.popleft()

    def _frame(self) -> dict:
        """The folded state as a plain snapshot, ready to be interpolated.

        Every snake is copied, and so is its body.

        The fold advances a snake by mutating its body list in place, which is
        the cheap and right thing for a fold to do. A buffer of frames that held
        references to those lists would not be a buffer of past states at all:
        every frame would show the newest body, attached to the older frame's
        heading and progress. That produced a snake drawn at a cell with its own
        cell as the one it was moving into, and a drawn position that walked
        backwards. A buffer of the past has to actually hold the past.
        """
        current = self.state_dict

        def frozen(snake):
            copy = dict(snake)
            copy["b"] = [list(cell) for cell in snake.get("b") or []]
            return copy

        snakes = {key: frozen(current["snakes"][key]) for key in current["snakes"]}

        return {
                "type": "keyframe",
                "t": current["t"],
                "phase": current["phase"],
                "arena": current["arena"],
                "rules": current["rules"],
                "snakes": [snakes[key] for key in sorted(snakes)],
                "food": [list(cell) for cell in sorted(current["food"])],
                "items": [
                    [cell[0], cell[1], kind]
                    for cell, kind in sorted(current["items"].items())
                ],
                "rank": current["rank"],
                "map": current.get("map") or [],
                "outcome": current["outcome"],
                "countdown_ms": current["countdown_ms"],
                "elapsed_seconds": current["elapsed_seconds"],
                "remaining_seconds": current["remaining_seconds"],
                "snake_by_id": snakes,
            }

    # -- drawing between two states that arrived --------------------------

    def render_delay(self) -> float:
        """How far behind the newest state to draw, from what the link is doing.

        Fixed at a small number this would stutter on a link whose messages
        arrive further apart than the delay, because there would be nothing
        ahead of the render moment to interpolate towards. Taken from the recent
        worst gap with a margin, it covers the link it is actually on.

        Moved towards slowly rather than jumped to. A delay that tracks every
        sample exactly is a delay that changes on every frame, and since the
        moment being drawn is the clock minus this, a jittering delay is a
        jittering picture. Easing it means the link has to genuinely get worse,
        not momentarily look worse, before the smoothing widens.
        """
        gaps = sorted(self.arrivals)

        if not gaps:
            target = RENDER_DELAY_MIN
        else:
            p95 = gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))] / 1000.0
            target = max(
                RENDER_DELAY_MIN, min(RENDER_DELAY_MAX, p95 * RENDER_DELAY_MARGIN)
            )

        if self._delay is None:
            self._delay = target
        else:
            self._delay += (target - self._delay) * DELAY_EASE

        return self._delay

    def _render_moment(self, now: float, delay: float) -> float:
        """The moment to draw, which only ever moves forward.

        Held rather than computed, because the delay changes underneath it. If
        it were simply the clock minus the delay, then widening the delay would
        move it into the past and every snake would step backwards.
        """
        target = now - delay

        if self._render_at is None or target > self._render_at:
            self._render_at = target

        # But never so far behind that it stops being this match. If the delay
        # widened a lot, this walks forward to the new delay instead of holding
        # a moment that is drifting further into the past every frame.
        self._render_at = max(self._render_at, now - RENDER_DELAY_MAX)

        return self._render_at

    def _bracket(self, moment: float):
        """The two buffered frames either side of a moment, oldest first."""
        frames = list(self.frames)
        if not frames:
            return None, None
        if moment <= frames[0][0]:
            return frames[0], None
        for index in range(len(frames) - 1):
            if frames[index][0] <= moment < frames[index + 1][0]:
                return frames[index], frames[index + 1]
        return frames[-1], None

    def _blend(self, older: dict, newer: dict, fraction: float) -> list:
        """One snake's drawn position, somewhere between two arrived states.

        Neither state is guessed, so nothing here can turn out to be wrong. The
        only judgement is when, between the two, the move happened, and that is
        recoverable: the snake covered the rest of one move and the start of the
        next, so the split falls where those two shares meet.
        """
        blended = []

        for snake in older["snakes"]:
            after = newer["snake_by_id"].get(snake["id"]) if newer else None
            drawn = dict(snake)

            if after is None or not snake["b"] or not after["b"]:
                blended.append(drawn)
                continue

            if list(after["b"][0]) == list(snake["b"][0]):
                # Same cell in both, so it is still on the same move.
                drawn["p"] = snake["p"] + (after["p"] - snake["p"]) * fraction
                blended.append(drawn)
                continue

            if len(after["b"]) != len(snake["b"]) and abs(
                len(after["b"]) - len(snake["b"])
            ) > 1:
                # Cut, or several moves at once. Nothing sensible lies between
                # these two, so the later one is drawn rather than a blend of
                # states that never existed.
                blended.append(dict(after))
                continue

            remaining = max(0.0, 1.0 - snake["p"])
            started = max(0.0, after["p"])
            span = remaining + started

            if span <= 0:
                blended.append(dict(after))
                continue

            switch = remaining / span

            if fraction < switch:
                drawn["p"] = snake["p"] + remaining * (fraction / switch)
            else:
                drawn = dict(after)
                drawn["p"] = started * ((fraction - switch) / (1.0 - switch)) \
                    if switch < 1.0 else started

            blended.append(drawn)

        return blended

    def latest(self) -> dict:
        """The newest folded state, with no delay applied.

        What this client believes is true right now, which is a different
        question from what it should be drawing right now. Folding correctness
        is checked against this; the drawing is checked against state().
        """
        with self.lock:
            if self.state_dict is None:
                return None
            frame = self._frame()
            frame.pop("snake_by_id", None)
            frame["standings"] = standings_from(
                frame["snakes"], frame.get("rank") or []
            )
            return frame

    def state(self, own_id=None) -> dict:
        """What to draw now: everybody else slightly in the past, you now.

        The delay applies to other players only. A tenth of a second on somebody
        else's snake is invisible; the same on your own is the game feeling
        broken, so your snake comes from the newest state and from your own
        prediction on top of that.
        """
        with self.lock:
            if self.state_dict is None:
                return None

            now = time.monotonic()
            newest = self.frames[-1][1] if self.frames else self._frame()
            delay = self.render_delay()
            moment = self._render_moment(now, delay)

            older, newer = self._bracket(moment)

            if older is None:
                snakes = newest["snakes"]
                drawn_at = now
            elif newer is None:
                # Nothing has arrived beyond the render moment. Holding the last
                # state we have is the honest answer; guessing past it is the
                # thing this whole approach exists to stop doing.
                snakes = older[1]["snakes"]
                drawn_at = older[0]
            else:
                fraction = (moment - older[0]) / max(1e-6, newer[0] - older[0])
                snakes = self._blend(
                    older[1], newer[1], max(0.0, min(1.0, fraction))
                )
                drawn_at = moment

            if own_id is not None:
                mine = newest["snake_by_id"].get(own_id)
                if mine is not None:
                    snakes = [
                        mine if snake["id"] == own_id else snake
                        for snake in snakes
                    ]

            frame = dict(newest)
            frame.pop("snake_by_id", None)
            frame["snakes"] = snakes
            frame["events"] = [
                event
                for stamp, event in self.events
                if now - stamp <= EVENT_WINDOW
            ]
            frame["standings"] = standings_from(
                newest["snakes"], frame.get("rank") or []
            )
            frame["cadence"] = self.cadence()
            frame["age_ms"] = round((now - self.updated_at) * 1000)
            frame["applied"] = self.applied
            frame["dropped"] = self.dropped
            frame["jump"] = max([entry[1] for entry in self.bursts], default=0)
            frame["delay_ms"] = round(delay * 1000)
            frame["behind_ms"] = round((now - drawn_at) * 1000)

            return frame
