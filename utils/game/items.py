"""Items.

Food, poisons and beneficial pickups: everything that can sit in the arena and
be eaten by walking onto it.

Poisons and pickups are the same thing wearing different signs. Both are a cell
with a kind on it, both are eaten by moving into them, and both start a timed
effect on the snake that ate them. The only asymmetry is in what the effect
does, so there is one spawner, one target calculation and one lookup, and the
difference lives in the table of kinds below and nowhere else.

Why they exist at all: a map with only punishments makes "avoid everything" the
safest strategy, which is the opposite of what severing is for. The poisons give
contested ground a cost and the pickups give it a reason.

Spawning picks from genuinely empty cells rather than guessing and retrying.
On a nearly full arena, rejection sampling degrades badly at exactly the moment
the player is doing well, which is the worst possible time for a frame to take
several milliseconds longer than the others.
"""

import random

from utils.game.arena import EMPTY, Arena

# What each kind is, and which rule switches it on. The rule keys are read
# rather than hardcoded at the call site so that adding a kind is an entry here
# and a field in the schema, and nothing else.
POISONS = ("slow", "shrink", "confusion")
PICKUPS = ("burst", "phase", "magnet")

KIND_RULE = {
    "slow": "slow_poison",
    "shrink": "shrink_poison",
    "confusion": "confusion_poison",
    "burst": "burst_pickup",
    "phase": "phase_pickup",
    "magnet": "magnet_pickup",
}

# Cells per item, by density, against the same arena area food is measured
# against. Sparser than food at every setting: an item is an event, and an
# arena where one is always within reach is an arena where they stop being one.
PER_DENSITY = {"off": 0, "low": 4000, "normal": 1800, "high": 900}


def enabled_kinds(rules, kinds) -> tuple:
    """The kinds from a family that this rule set has switched on."""
    return tuple(
        kind for kind in kinds if getattr(rules, KIND_RULE[kind], False)
    )


def item_target(rules, density: str, kinds) -> int:
    """How many of a family to keep on the floor, for one arena.

    Zero when the density is off or when every kind in the family is switched
    off, which are different ways of saying the same thing and both have to
    work: a host who turns off all three poisons individually has turned off
    poisons, whatever the density says.
    """
    if not kinds:
        return 0

    per = PER_DENSITY.get(density, 0)
    if not per:
        return 0

    area = rules.arena_width * rules.arena_height
    return max(1, round(area / per))


def _free_cells(arena: Arena) -> list:
    return [index for index, value in enumerate(arena.cells) if value == EMPTY]


def spawn_food(arena: Arena, count: int, rng: random.Random = None) -> int:
    """Top the arena up to count food items. Returns how many were added."""
    generator = rng or random
    wanted = count - len(arena.food)
    if wanted <= 0:
        return 0

    free = _free_cells(arena)
    if not free:
        return 0

    added = 0
    for index in generator.sample(free, min(wanted, len(free))):
        arena.add_food(index % arena.width, index // arena.width)
        added += 1

    return added


def spawn_items(arena: Arena, kinds, count: int,
                rng: random.Random = None) -> list:
    """Top the arena up to count items of the given kinds.

    Counted across the whole family rather than per kind. Three poisons at a
    target of two does not mean six on the floor; it means two, and which two
    is chance. Returns the cells added, because the caller has to tell the
    clients about them.
    """
    generator = rng or random
    if not kinds:
        return []

    present = sum(
        1 for kind in arena.items.values() if kind in kinds
    )
    wanted = count - present
    if wanted <= 0:
        return []

    free = _free_cells(arena)
    if not free:
        return []

    added = []
    for index in generator.sample(free, min(wanted, len(free))):
        cell = (index % arena.width, index // arena.width)
        arena.add_item(cell[0], cell[1], generator.choice(list(kinds)))
        added.append(cell)

    return added
