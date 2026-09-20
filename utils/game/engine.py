"""The engine.

One engine, one tick loop. Single player is a local room with one snake and no
network listeners, and it runs this code. There is deliberately no second
implementation of movement or collision in JavaScript; the browser renders what
the engine reports and sends headings back.

Three things here are load bearing and should not be simplified later:

  1. Snake bodies are deques. Moving is appendleft plus pop, not rebuilding a
     list every tick.
  2. The occupancy grid is updated incrementally on every move, so collision is
     a lookup rather than a scan.
  3. The loop computes each deadline from a monotonic start time and sleeps the
     remainder. Sleeping a fixed 1/60 of a second accumulates error.
"""

import logging
import random
import threading
import time
from collections import deque

from utils.game.arena import EMPTY, FOOD, ITEM, SNAKE, Arena
from utils.game.effects import (
    MAGNET_INTERVAL,
    MAGNET_RANGE,
    SHRINK_SEGMENTS,
    Effects,
    heading_for,
    move_interval_for,
)
from utils.game.items import (
    PICKUPS,
    POISONS,
    enabled_kinds,
    item_target,
    spawn_food,
    spawn_items,
)
from utils.game.rules import RuleSet

log = logging.getLogger("engine")

TICK_HZ = 60
TICK_SECONDS = 1.0 / TICK_HZ

PHASE_IDLE = "idle"
PHASE_PLAYING = "playing"
PHASE_PAUSED = "paused"
PHASE_OVER = "over"

# What a match reports when the engine itself failed rather than somebody
# winning. Both engines use the same string, and both frontends have a line for
# it, because the one thing worse than a crash is a crash nobody is told about.
FAULT = "engine_error"

HEADINGS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

DEATH_WALL = "wall"
DEATH_SELF = "self"


class Snake:
    def __init__(self, snake_id: int, colour: str, segments, heading):
        self.id = snake_id
        self.colour = colour
        self.body = deque(segments)
        self.heading = heading
        self.pending_heading = heading
        self.alive = True
        self.score = 0
        self.accumulator = 0
        self.move_interval = 1
        self.death_cause = None

        # What is currently acting on this snake. The same class the shared
        # match uses, so a poison behaves the same alone as it does in a room.
        self.effects = Effects()

    @property
    def length(self) -> int:
        return len(self.body)

    @property
    def head(self):
        return self.body[0]


class SoloEngine:
    """A single arena with one snake."""

    # The colour a solo snake is drawn in when nobody says otherwise. Every
    # caller in the application passes the player's own, so this only stands in
    # for a test that does not care.
    DEFAULT_COLOUR = "#3ecf8e"

    def __init__(self, rules: RuleSet, seed: int = None, colour: str = None):
        self.rules = rules
        self.colour = colour or self.DEFAULT_COLOUR
        self.random = random.Random(seed)
        self.arena = Arena(rules.arena_width, rules.arena_height, rules.edge_behaviour)
        self.snake = None
        self.phase = PHASE_IDLE
        self.tick = 0
        self.started_at = None
        self.ended_at = None
        self.lock = threading.Lock()

        # Tick health, reported to the client so that the timing is something
        # you can read rather than something you have to believe.
        self.max_drift_ms = 0.0
        self.late_ticks = 0

        # Set only when the tick loop raised. None for every ordinary run,
        # including one that ended in a death.
        self.fault = None

        # When the magnet next drags food a cell. See _pull_food.
        self._next_pull = 0.0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self.lock:
            self.arena.reset()
            self.tick = 0
            self.max_drift_ms = 0.0
            self.late_ticks = 0
            self.ended_at = None
            self.fault = None

            middle_y = self.arena.height // 2
            start_x = max(self.rules.starting_length, self.arena.width // 4)

            segments = [
                (start_x - offset, middle_y)
                for offset in range(self.rules.starting_length)
            ]
            for x, y in segments:
                self.arena.set(x, y, SNAKE)

            self.snake = Snake(0, self.colour, segments, HEADINGS["right"])
            self.snake.move_interval = self.rules.move_interval_ticks(
                self.snake.length
            )

            spawn_food(self.arena, self.rules.food_target(), self.random)
            self._top_up_items()

            self.phase = PHASE_PLAYING
            self.started_at = time.time()

    def _top_up_items(self) -> None:
        """Keep the poisons and the pickups stocked. One arena, so no layout."""
        for density, family in (
            (self.rules.poisons, POISONS),
            (self.rules.pickups, PICKUPS),
        ):
            kinds = enabled_kinds(self.rules, family)
            target = item_target(self.rules, density, kinds)
            if target:
                spawn_items(self.arena, kinds, target, self.random)

    def pause(self) -> None:
        with self.lock:
            if self.phase == PHASE_PLAYING:
                self.phase = PHASE_PAUSED

    def resume(self) -> None:
        with self.lock:
            if self.phase == PHASE_PAUSED:
                self.phase = PHASE_PLAYING

    def toggle_pause(self) -> str:
        with self.lock:
            if self.phase == PHASE_PLAYING:
                self.phase = PHASE_PAUSED
            elif self.phase == PHASE_PAUSED:
                self.phase = PHASE_PLAYING
            return self.phase

    def stop(self) -> None:
        with self.lock:
            self.phase = PHASE_OVER
            self.ended_at = time.time()

    def fail(self, error: BaseException) -> None:
        """The tick loop raised. End the run and say so on the screen.

        Logged with the traceback, because a game that stops updating with
        nothing anywhere is the failure this exists to prevent, and a log line
        without the traceback is only a slightly better version of nothing.
        """
        log.exception("the tick loop failed, ending the run", exc_info=error)
        with self.lock:
            self.fault = FAULT
            self.phase = PHASE_OVER
            self.ended_at = time.time()

    # -- input ------------------------------------------------------------

    def set_heading(self, name: str) -> bool:
        """Queue a heading. A reversal into the neck is refused.

        The check is against the last applied heading, not the pending one, so
        that two fast keypresses in one move interval cannot turn the snake
        back into itself.
        """
        heading = HEADINGS.get(name)
        if heading is None or self.snake is None:
            return False

        with self.lock:
            # Confusion first, so the legality check is against the heading the
            # press actually produces. Same order as the shared match.
            heading = heading_for(self.snake.effects, heading)

            if self.snake.length > 1:
                current = self.snake.heading
                if heading[0] == -current[0] and heading[1] == -current[1]:
                    return False
            self.snake.pending_heading = heading

        return True

    # -- simulation -------------------------------------------------------

    def step(self) -> None:
        """Advance exactly one logic tick."""
        with self.lock:
            if self.phase != PHASE_PLAYING or self.snake is None:
                return

            self.tick += 1
            snake = self.snake
            snake.accumulator += 1

            if snake.accumulator < snake.move_interval:
                return

            snake.accumulator = 0
            snake.heading = snake.pending_heading

            target = self.arena.step_from(
                snake.head[0], snake.head[1], snake.heading
            )

            if target is None:
                self._kill(DEATH_WALL)
                return

            contents = self.arena.at(target[0], target[1])
            ate = contents == FOOD
            took = contents == ITEM

            # The tail cell is about to be vacated, so moving into it is legal.
            # Not special-casing this is the classic off-by-one that makes a
            # snake die by following its own tail.
            tail = snake.body[-1]
            passing = snake.effects.active("phase")
            if contents == SNAKE and not (target == tail and not ate) \
                    and not passing:
                self._kill(DEATH_SELF)
                return

            snake.body.appendleft(target)
            self.arena.set(target[0], target[1], SNAKE)

            if ate:
                self.arena.remove_food(target[0], target[1])
                self.arena.set(target[0], target[1], SNAKE)
                snake.score += 1
                spawn_food(self.arena, self.rules.food_target(), self.random)
            else:
                removed = snake.body.pop()
                if self.arena.at(removed[0], removed[1]) == SNAKE:
                    self.arena.set(removed[0], removed[1], EMPTY)

            if took:
                kind = self.arena.item_at(target[0], target[1])
                self.arena.remove_item(target[0], target[1])
                self.arena.set(target[0], target[1], SNAKE)
                self._take_item(kind)
                self._top_up_items()

            snake.effects.expire()
            snake.move_interval = move_interval_for(
                self.rules, snake.length, snake.effects
            )

            self._pull_food()
            snake.effects.expire()

            if self.arena.empty_cells() == 0:
                self._kill(None)

    def _pull_food(self) -> None:
        """Drag loose food a cell toward the head while a magnet is held.

        The same rule and the same rhythm as the shared match: its own clock so
        the food is seen to drift, the longer axis first so it takes the
        diagonal in steps, and never into a cell the snake is standing in.
        """
        snake = self.snake
        now = time.monotonic()

        if not snake.effects.active("magnet", now) or now < self._next_pull:
            return
        self._next_pull = now + MAGNET_INTERVAL

        head = snake.head
        for cell in sorted(self.arena.food):
            gap = abs(head[0] - cell[0]) + abs(head[1] - cell[1])
            if gap > MAGNET_RANGE or gap == 0:
                continue

            dx = head[0] - cell[0]
            dy = head[1] - cell[1]
            if abs(dx) >= abs(dy):
                step = (cell[0] + (1 if dx > 0 else -1), cell[1])
            else:
                step = (cell[0], cell[1] + (1 if dy > 0 else -1))

            if self.arena.at(step[0], step[1]) != EMPTY:
                continue

            self.arena.remove_food(cell[0], cell[1])
            self.arena.add_food(step[0], step[1])

    def _take_item(self, kind) -> None:
        """Eat an item. The effects are the shared ones; the shrink is here.

        The score is not charged for a shrink in single player, which is the one
        place this differs from a shared match. The percentage a cut costs is a
        multiplayer rule and the single player editor never shows it, so
        applying it here would be a rule nobody was told about, taking points
        away for a reason that is not on screen anywhere.
        """
        if kind is None:
            return

        snake = self.snake

        if kind == "shrink":
            floor = max(1, self.rules.min_length_before_death)
            losing = min(SHRINK_SEGMENTS, snake.length - floor)
            for _ in range(max(0, losing)):
                removed = snake.body.pop()
                if self.arena.at(removed[0], removed[1]) == SNAKE:
                    self.arena.set(removed[0], removed[1], EMPTY)
            return

        snake.effects.apply(kind, self.rules.effect_duration)

    def _kill(self, cause) -> None:
        self.snake.alive = False
        self.snake.death_cause = cause
        self.phase = PHASE_OVER
        self.ended_at = time.time()
        log.info(
            "solo match over, score %s, length %s, cause %s",
            self.snake.score,
            self.snake.length,
            cause,
        )

    # -- reporting --------------------------------------------------------

    def keyframe(self) -> dict:
        """Full state. Sent once at start, and after a restart."""
        with self.lock:
            return {
                "type": "keyframe",
                "rules": self.rules.to_dict(),
                "arena": {
                    "w": self.arena.width,
                    "h": self.arena.height,
                    "edge": self.arena.edge_behaviour,
                },
                "state": self._snapshot_locked(),
            }

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        # Positional arrays rather than objects: a segment is [x, y]. Roughly
        # half the payload for no encoding work, and the same shape the
        # networked game sends, so changing transport does not mean changing
        # this.
        if self.snake is None:
            return {
                "type": "snapshot",
                "t": self.tick,
                "phase": self.phase,
                "snakes": [],
                "food": [],
                "items": [],
                "fault": self.fault,
            }

        progress = 0.0
        if self.snake.move_interval > 0 and self.phase == PHASE_PLAYING:
            progress = self.snake.accumulator / self.snake.move_interval

        # Where the head is going, computed here rather than guessed by the
        # renderer. The pending heading is what the next move will use, and
        # step_from already knows about wrapping and walls. Without this the
        # frontend has to predict, and it predicts wrongly exactly when the
        # player turns on the tick that would have wrapped.
        target = self.arena.step_from(
            self.snake.head[0], self.snake.head[1], self.snake.pending_heading
        )

        return {
            "type": "snapshot",
            "t": self.tick,
            "phase": self.phase,
            "snakes": [
                {
                    "id": self.snake.id,
                    "c": self.snake.colour,
                    "h": list(self.snake.heading),
                    "ph": list(self.snake.pending_heading),
                    "n": list(target) if target else None,
                    "p": round(progress, 3),
                    "alive": self.snake.alive,
                    "score": self.snake.score,
                    "len": self.snake.length,
                    "interval": self.snake.move_interval,
                    "interval_ms": round(
                        1000.0 * self.snake.move_interval / TICK_HZ, 1
                    ),
                    "mps": round(TICK_HZ / max(1, self.snake.move_interval), 2),
                    "fx": self.snake.effects.to_wire(),
                    "b": [[x, y] for x, y in self.snake.body],
                }
            ],
            "food": [[x, y] for x, y in sorted(self.arena.food)],
            "items": [
                [cell[0], cell[1], kind]
                for cell, kind in sorted(self.arena.items.items())
            ],
            "death": self.snake.death_cause,
            "fault": self.fault,
            "timing": {
                "max_drift_ms": round(self.max_drift_ms, 2),
                "late_ticks": self.late_ticks,
            },
        }

    def result(self) -> dict:
        with self.lock:
            if self.snake is None:
                return {}
            duration = 0.0
            if self.started_at:
                duration = (self.ended_at or time.time()) - self.started_at
            return {
                "score": self.snake.score,
                "length": self.snake.length,
                "ticks": self.tick,
                "duration_seconds": round(duration, 1),
                "cause": self.fault or self.snake.death_cause,
                "final_interval": self.snake.move_interval,
                "rules_fingerprint": self.rules.fingerprint(),
            }


class TickLoop:
    """A 60 Hz loop that does not drift.

    Every thread in this application is stoppable: a stop event checked each
    iteration, and a join with a timeout. A loop that will not stop is what turns
    closing the window into a process that lingers.
    """

    def __init__(self, engine: SoloEngine, hz: int = TICK_HZ):
        self.engine = engine
        self.interval = 1.0 / hz
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tick", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        start = time.monotonic()
        ticks = 0

        while not self._stop.is_set():
            ticks += 1
            deadline = start + ticks * self.interval

            # Per tick, not around the whole loop.
            #
            # Without this the thread dies where it stands. In single player
            # that is a board that stops moving; in a room it is worse, because
            # the host's stream loop carries on broadcasting to its own
            # schedule, so every player sees a frozen arena, a healthy link
            # indicator and no message at all. The exception is logged with its
            # traceback and the match is ended with a reason both frontends can
            # name, which is the difference between a bug and a mystery.
            try:
                self.engine.step()
            except Exception as error:  # noqa: BLE001
                self.engine.fail(error)
                return

            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._stop.wait(remaining)
            else:
                drift_ms = -remaining * 1000.0
                self.engine.late_ticks += 1
                if drift_ms > self.engine.max_drift_ms:
                    self.engine.max_drift_ms = drift_ms

                # A long stall (a laptop resuming from sleep, a GC pause on a
                # loaded machine) must not produce a burst of catch-up ticks.
                if drift_ms > 250:
                    start = time.monotonic()
                    ticks = 0
