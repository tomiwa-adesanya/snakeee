"""Timed effects on a snake, and what they change.

Both engines use this. The item rules are in scope for single player and for
multiplayer alike, so how long a slow lasts and how much it slows by cannot live
in either engine or they will drift apart, and a poison that behaves differently
depending on how many people are playing is a bug that would take a long time to
be reported as one.

Two things are deliberately not here: what an effect looks like, which is the
client's business, and when one is applied, which is the moment a snake eats an
item and belongs to whichever engine owns that snake.

**Every effect is expressed as a change to an existing number.** The slow adds
to `move_interval`, which is the same number the length penalty writes to and is
clamped by the same ceiling. Nothing here introduces a second speed system, and
`move_interval_for` below is the one place any of it is resolved. A snake with
a slow poison and a long body should be slow once, not twice.
"""

import math
import time

from utils.game.rules import MIN_INTERVAL_TICKS

# How much slower a slow poison makes you, as a multiple of the interval you
# would have had. Held against the same ceiling the length penalty is held
# against, so the worst case is the worst case either can produce and not the
# two of them stacked, and a long snake that eats a slow poison is never frozen.
SLOW_FACTOR = 1.6

# How many segments a shrink poison takes.
SHRINK_SEGMENTS = 3

# How much faster a burst pickup makes you, as a share of the interval you
# would have had. Applied after the slow, and floored at the same minimum the
# rules use, so a burst can cancel a slow but nothing can make a snake move
# faster than the game is willing to simulate.
BURST_FACTOR = 0.6

# How far a magnet reaches, in cells, measured the same way spawn clearance is.
MAGNET_RANGE = 6

# How often it pulls, in seconds. Slower than a move so that the food drifts
# visibly toward you rather than teleporting into your mouth, which is the
# difference between a mechanic that reads as generous and one that reads as
# broken.
MAGNET_INTERVAL = 0.25

# What the controls do while confused: both axes reversed.
#
# Reversed rather than randomly remapped, for two reasons. It is learnable,
# so a good player can keep going through it rather than being made to stop
# playing for six seconds. And it is a fixed rule rather than a rolled one, so
# a client can apply it to its own prediction and arrive at the same heading the
# host does, instead of predicting one turn and being corrected into another.
def confuse(heading) -> tuple:
    return (-heading[0], -heading[1])


class Effects:
    """What is currently acting on one snake, and until when.

    A dict of kind to the moment it ends, and nothing else. Effects do not
    stack: eating a second slow poison while slowed sets a new end time rather
    than making you slower, because stacking makes the worst case unbounded and
    the mechanic is meant to be a setback, not a spiral.
    """

    def __init__(self):
        self.until = {}

    def apply(self, kind: str, seconds: float, now: float = None) -> None:
        moment = time.monotonic() if now is None else now
        self.until[kind] = moment + seconds

    def active(self, kind: str, now: float = None) -> bool:
        moment = time.monotonic() if now is None else now
        return self.until.get(kind, 0.0) > moment

    def remaining(self, kind: str, now: float = None) -> float:
        moment = time.monotonic() if now is None else now
        return max(0.0, self.until.get(kind, 0.0) - moment)

    def clear(self) -> None:
        self.until.clear()

    def expire(self, now: float = None) -> list:
        """Drop what has ended, and say what ended. For the event feed."""
        moment = time.monotonic() if now is None else now
        done = [
            kind for kind, ends in self.until.items() if ends <= moment
        ]
        for kind in done:
            del self.until[kind]
        return done

    def to_wire(self, now: float = None) -> dict:
        """Kind to milliseconds left, for the HUD countdown.

        Time remaining rather than the moment it ends, because the two ends of a
        match have no clock in common and a countdown is the one thing a player
        reads as a promise.

        Rounded up to a quarter of a second, which is what makes it deltable. An
        exact figure differs in every single message by definition, so it would
        be sent in every single message; a quarter-second bucket changes four
        times a second while an effect is running and not at all the rest of the
        time. Rounded up rather than down so it never reads zero while the
        effect is still on.
        """
        moment = time.monotonic() if now is None else now
        return {
            kind: int(math.ceil((ends - moment) * 4.0) * 250)
            for kind, ends in self.until.items()
            if ends > moment
        }


def move_interval_for(rules, length: int, effects: Effects,
                      now: float = None) -> int:
    """The move interval a snake should have, length and effects together.

    The single place speed is resolved. Anything that wants to know how fast a
    snake is moving asks this, and anything that wants to change it adds a term
    here.
    """
    interval = rules.move_interval_ticks(length)

    if effects is None:
        return int(interval)

    if effects.active("slow", now):
        interval = min(
            round(interval * SLOW_FACTOR), rules.max_interval_ticks()
        )

    # After the slow, deliberately. A burst is meant to be an escape from one,
    # so it has to act on the slowed number rather than on the clean one, and
    # the two together land somewhere near normal speed rather than cancelling
    # to something faster than either.
    if effects.active("burst", now):
        interval = max(MIN_INTERVAL_TICKS, round(interval * BURST_FACTOR))

    return int(interval)


def heading_for(effects: Effects, heading, now: float = None):
    """The heading a press actually produces, given what is acting on a snake."""
    if effects is not None and effects.active("confusion", now):
        return confuse(heading)
    return heading
