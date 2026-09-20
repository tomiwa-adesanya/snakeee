"""Computer-controlled snakes.

A bot decides one thing: which of the four headings it would like next. It is
handed a read-only view of the match and returns a heading, and the engine then
applies it through exactly the same path a human press goes through -- the same
reversal check, the same simultaneous resolution, the same collisions. A bot has
no privileges and no shortcuts, and nothing here writes to the arena.

That is the whole reason this is a module rather than a method on the engine. A
bot that could reach into the simulation would eventually be given a nudge to
make it play better, and the nudge would be indistinguishable from cheating to
anybody playing against it.

**The tiers change what a bot understands, not how fast it reacts.** A bot that
was merely quicker would feel unfair rather than skilled, and there is nothing
to learn from losing to one. So every tier moves on exactly the same clock as a
human snake of the same length:

    easy    goes for the nearest food, and does not walk into things
    medium  also avoids poisons, and will not corner itself
    hard    also hunts shorter snakes and contests dropped remains

Each tier is the one above it plus a term, so a hard bot that has nobody to hunt
plays like a medium one rather than like something else entirely.
"""

import random
import time

from utils.game.arena import EMPTY, FOOD, ITEM
from utils.game.items import POISONS

HEADINGS = ((1, 0), (-1, 0), (0, 1), (0, -1))

# How willing a bot is to cut somebody, by the room's aggression setting.
#
# **Each bot draws its own temperament from this band when it spawns, and keeps
# it for that life.** That is the whole design decision here, and the
# alternatives are worth naming because they were tried:
#
#   Rolling per decision makes a bot dither. It commits to an attack on one
#   move and thinks better of it on the next, which looks like indecision
#   rather than like a choice, and a player cannot read it or plan against it.
#
#   One fixed value for every bot makes a room of identical animals. Four
#   snakes all deciding the same thing at the same threshold is what made them
#   converge and die together in the first version.
#
#   Drawing once per life gives each bot a personality that lasts long enough
#   to be noticed -- this one hunts, that one farms -- and re-rolls on respawn
#   so a match does not settle into a fixed shape. A player can learn which
#   snake to keep away from, and the lesson expires when it dies, which is
#   what keeps it interesting rather than solvable.
#
# The band, not the value, is what the setting controls. Overlapping bands mean
# a "rare" room can still produce one bot with a taste for it, which reads as
# character rather than as the setting being ignored.
APPETITE = {
    "off": (0.0, 0.0),
    "rare": (0.0, 0.35),
    "normal": (0.25, 0.75),
    "relentless": (0.6, 1.0),
}

# Below this, a bot does not consider a body worth entering at all. It is the
# line between attacking something and blundering into it.
MINIMUM_APPETITE = 8.0

EASY = "easy"
MEDIUM = "medium"
HARD = "hard"

# Bots are named rather than numbered, so a results screen reads as a game
# rather than as a test harness. Short, because they sit in a leaderboard column
# beside human names.
NAMES = (
    "Nibbler", "Coil", "Rattle", "Slink", "Mamba", "Boa",
    "Viper", "Adder", "Python", "Krait", "Racer", "Sidewinder",
)

# Distinct from each other and from the shipped player colours.
COLOURS = (
    "#7f9cf5", "#f59e7f", "#7ff5c1", "#f57fd4",
    "#c1f57f", "#f5d47f", "#9c7ff5", "#7fd4f5",
)


def name_for(index: int) -> str:
    if index < len(NAMES):
        return NAMES[index]
    return f"{NAMES[index % len(NAMES)]} {index // len(NAMES) + 1}"


def colour_for(index: int) -> str:
    return COLOURS[index % len(COLOURS)]


class Bot:
    """One computer player's judgement. Holds no game state of its own."""

    def __init__(self, snake_id: int, difficulty: str = MEDIUM, seed=None,
                 aggression: str = "normal"):
        self.snake_id = snake_id
        self.difficulty = difficulty if difficulty in (
            EASY, MEDIUM, HARD
        ) else MEDIUM
        self.aggression = aggression if aggression in APPETITE else "normal"
        self.random = random.Random(seed)

        # Drawn now and again on every respawn. See APPETITE above.
        self.appetite = 0.0
        self.reroll()

    def reroll(self) -> float:
        """Draw a fresh temperament. Called on spawn and on every respawn."""
        low, high = APPETITE[self.aggression]
        self.appetite = self.random.uniform(low, high) if high else 0.0
        return self.appetite

    # -- the one thing a bot does -------------------------------------------

    def choose(self, view) -> tuple:
        """The heading this bot would like next, or None to carry straight on.

        Never returns a reversal, because the engine would refuse it and the
        bot would then be carrying straight on without having decided to.
        """
        me = view.snake(self.snake_id)
        if me is None or not me["alive"] or not me["body"]:
            return None

        head = me["body"][0]
        options = []

        for heading in HEADINGS:
            if self._is_reversal(me, heading):
                continue

            cell = view.step_from(head, heading)
            if cell is None:
                continue

            score = self._score(view, me, cell, heading)
            if score is None:
                continue

            options.append((score, heading))

        if not options:
            # Every direction is fatal. Carry on rather than picking at random:
            # a snake that thrashes in its last moment looks broken, and one
            # that keeps going looks like it was beaten.
            return None

        best = max(score for score, _ in options)
        # Ties broken at random rather than by the order the headings are listed
        # in, or every bot in an open arena drifts the same way and they look
        # like one animal.
        return self.random.choice(
            [heading for score, heading in options if score == best]
        )

    # -- scoring one candidate cell -----------------------------------------

    def _score(self, view, me, cell, heading):
        """How much this bot wants to move there. None means never."""
        contents = view.at(cell)

        if contents is None:
            return None

        # Occupied by a body. Three different things depending on the rules,
        # and the difference is the whole of whether bots are dangerous:
        #
        #   phasing        pass through, no effect either way
        #   severing on    the body is cut and the snake that moved survives,
        #                  so this is an attack rather than a death
        #   severing off   the snake that moved dies
        #
        # The first version treated all three as a wall, which is why bots
        # never hurt anybody: they were refusing the one move that damages an
        # opponent without costing anything.
        attack = 0.0
        if view.blocked(cell) and not view.phasing(me):
            prize = view.cut_value(cell, self.snake_id)
            if prize is None:
                return None

            attack = prize * self.appetite

            # A cell that is worth nothing to enter is still a wall.
            #
            # Without this line the whole setting did nothing: a cut it did not
            # want scored zero rather than being refused, so a bot with no
            # appetite walked into bodies as readily as into empty ground and
            # an aggression of "off" produced as many cuts as "relentless".
            # Wanting to is what makes it a move rather than an accident.
            if attack < MINIMUM_APPETITE:
                return None

        score = attack

        if contents == FOOD:
            score += 40.0

        if contents == ITEM:
            kind = view.item_at(cell)
            if self.difficulty == EASY:
                # Eats whatever is in front of it, which is how an easy bot
                # ends up slowed and confused and beatable.
                score += 10.0
            elif kind in POISONS:
                score -= 60.0
            else:
                score += 30.0

        # Where other heads could be next move.
        #
        # Without this every bot walks at the nearest food, several of them
        # reach for the same piece, and they meet head on. Four easy bots on an
        # open board wiped each other out in three seconds before this existed,
        # which reads as the game being broken rather than as the bots being
        # bad at it.
        #
        # A head-on with somebody longer or the same length kills you, so it is
        # avoided at every tier. What the tiers change is how firmly: an easy
        # bot weighs it against the food and sometimes goes anyway, which is
        # what makes it easy.
        contested = view.contested_by(cell, self.snake_id)
        if contested is not None and contested >= len(me["body"]):
            score -= 25.0 if self.difficulty == EASY else 400.0

        # How much room this move leads into. The cheap version at easy, which
        # is why an easy bot corners itself.
        if self.difficulty != EASY:
            room = view.room_from(cell, heading, limit=self._reach())
            if room < len(me["body"]):
                score -= (len(me["body"]) - room) * 6.0

        nearest = view.nearest_food(cell, avoid=self._avoids())
        if nearest is not None:
            score -= nearest * 0.9

        # Closing the distance, which is what makes the temperament visible.
        #
        # Willingness alone was not enough: a bot that would cut you if it
        # happened to be touching you almost never was, because everything else
        # pulling on it pointed at food. Measured, an aggressive room was barely
        # different from a pacifist one. This is the term that makes a hunting
        # bot come at you, and its weight is the appetite, so a contented bot
        # ignores it entirely.
        if self.appetite > 0:
            quarry = view.nearest_target(cell, self.snake_id)
            if quarry is not None:
                score += self.appetite * max(0.0, 18.0 - quarry * 1.4)

        if self.difficulty == HARD:
            score += self._hunting(view, me, cell)

        return score

    def _avoids(self) -> tuple:
        return () if self.difficulty == EASY else POISONS

    def _reach(self) -> int:
        return 40 if self.difficulty == MEDIUM else 90

    def _hunting(self, view, me, cell) -> float:
        """Extra pull toward things worth taking, for a hard bot.

        Two of them. A shorter snake's head is worth being near, because being
        near it is how a cut happens under the head-on rule. And dropped remains
        are worth more than food, because they are somebody's loss as well as
        this bot's gain.

        Both are pulls rather than plans. A bot that committed to an ambush
        several moves ahead would spend most of its life walking into walls
        while a human simply turned.
        """
        # Deliberately small. The first version of this was worth up to 55,
        # which swamped both the food term and the room check, and a hard bot
        # spent its life walking into danger after prey it rarely caught. Two
        # bots of each tier in one match, five seeds: medium outscored hard four
        # to one. Hunting has to refine a decision that is already sound, not
        # decide it.
        pull = 0.0

        prize = view.nearest_remains(cell)
        if prize is not None and prize <= 8:
            pull += max(0.0, 15.0 - prize * 1.5)

        prey = view.nearest_shorter_head(cell, len(me["body"]))
        if prey is not None and prey <= 6:
            pull += max(0.0, 12.0 - prey * 1.5)

        return pull

    @staticmethod
    def _is_reversal(me, heading) -> bool:
        if len(me["body"]) < 2:
            return False
        facing = me["heading"]
        return heading[0] == -facing[0] and heading[1] == -facing[1]


class MatchView:
    """A read-only window onto a match, for the bots to think against.

    Everything a bot is allowed to know, and nothing it can change. Built once
    per tick and handed to every bot, so a room of eight bots costs one walk of
    the arena rather than eight.

    A bot sees the whole arena, including arenas it is not standing in. That is
    a deliberate asymmetry and worth saying out loud: human players cannot, and
    scoping a bot's view would mean writing a second, weaker pathfinder rather
    than making the game fairer. It only matters on a layout, and there it makes
    bots better at leaving an arena than at hiding in one.
    """

    def __init__(self, engine):
        self._engine = engine
        self._arena = engine.arena

        self._rules = engine.rules
        now = time.monotonic()

        self._snakes = {}
        for snake in engine.snakes.values():
            self._snakes[snake.id] = {
                "id": snake.id,
                "alive": snake.alive and bool(snake.body),
                "body": list(snake.body),
                "heading": snake.heading,
                "effects": snake.effects,
                "protected": snake.protected_until > now,
            }

        self._food = set(engine.arena.food)
        self._remains = set(engine.remains)

    # -- what a bot may ask -------------------------------------------------

    def snake(self, snake_id):
        return self._snakes.get(snake_id)

    def step_from(self, cell, heading):
        return self._arena.step_from(cell[0], cell[1], heading)

    def at(self, cell):
        if cell is None:
            return None
        return self._arena.at(cell[0], cell[1])

    def blocked(self, cell) -> bool:
        return self._arena.at(cell[0], cell[1]) not in (EMPTY, FOOD, ITEM)

    def item_at(self, cell):
        return self._arena.item_at(cell[0], cell[1])

    def phasing(self, me) -> bool:
        return me["effects"].active("phase")

    def distance(self, one, two) -> int:
        """The short way round when the edges wrap.

        Straight subtraction would call two cells either side of the seam the
        width of the arena apart, and a bot would walk away from food it was
        standing next to.
        """
        gap_x = abs(one[0] - two[0])
        gap_y = abs(one[1] - two[1])

        if self._arena.edge_behaviour == "wrap":
            gap_x = min(gap_x, self._arena.width - gap_x)
            gap_y = min(gap_y, self._arena.height - gap_y)

        return gap_x + gap_y

    def nearest_food(self, cell, avoid=()):
        best = None
        for food in self._food:
            gap = self.distance(cell, food)
            if best is None or gap < best:
                best = gap

        # Items are worth walking toward too, unless this bot knows better.
        for spot, kind in self._arena.items.items():
            if kind in avoid:
                continue
            gap = self.distance(cell, spot)
            if best is None or gap < best:
                best = gap

        return best

    def nearest_remains(self, cell):
        best = None
        for spot in self._remains:
            gap = self.distance(cell, spot)
            if best is None or gap < best:
                best = gap
        return best

    def nearest_shorter_head(self, cell, length):
        best = None
        for snake in self._snakes.values():
            if not snake["alive"] or len(snake["body"]) >= length:
                continue
            gap = self.distance(cell, snake["body"][0])
            if best is None or gap < best:
                best = gap
        return best

    def cut_value(self, cell, mine):
        """What moving into this occupied cell is worth, or None if it is fatal.

        None covers everything a snake should not walk into: its own body when
        self collision kills, anybody at all when severing is off, and a snake
        that spawn protection puts out of reach. In those cases the cell is a
        wall and always was.

        Otherwise the answer is how much damage the cut does. Cutting near a
        head takes most of a snake with it; cutting near a tail takes a few
        segments and mostly annoys them. Taking somebody below the length they
        can survive is worth more again, because that is a kill and a bounty
        rather than a setback.
        """
        if not self._rules.severing:
            return None

        owner = self._owner_at(cell)
        if owner is None:
            return None

        if owner["id"] == mine and self._rules.self_collision != "sever":
            return None

        if owner["protected"] or self._protected(mine):
            return None

        # Cutting your own tail is legal under a sever rule and is never worth
        # choosing, so it is a wall as far as wanting to go there is concerned.
        if owner["id"] == mine:
            return None

        losing = owner["length"] - owner["index"]
        value = min(60.0, losing * 5.0)

        if owner["length"] - losing < self._rules.min_length_before_death:
            value += 40.0

        return value

    def nearest_target(self, cell, mine):
        """How far to the nearest body this snake could profitably cut.

        Heads are left out. A head is where a head-on happens, which is decided
        by length rather than by who arrived, and contested_by already has an
        opinion about those. This is about bodies, where the snake that moves
        wins outright.
        """
        if not self._rules.severing:
            return None

        best = None
        for snake in self._snakes.values():
            if snake["id"] == mine or not snake["alive"] or snake["protected"]:
                continue
            for segment in list(snake["body"])[1:]:
                gap = self.distance(cell, segment)
                if best is None or gap < best:
                    best = gap
        return best

    def _owner_at(self, cell):
        for snake in self._snakes.values():
            if not snake["alive"]:
                continue
            body = snake["body"]
            for index, segment in enumerate(body):
                if segment == cell:
                    return {
                        "id": snake["id"],
                        "index": index,
                        "length": len(body),
                        "protected": snake["protected"],
                    }
        return None

    def _protected(self, snake_id) -> bool:
        snake = self._snakes.get(snake_id)
        return bool(snake and snake["protected"])

    def contested_by(self, cell, mine):
        """The length of the longest other snake that could step here next.

        Could, not will. Guessing which way somebody is about to turn is
        exactly the kind of prediction that makes a bot look silly when it is
        wrong, so this asks only whether a head is one cell away and treats
        every direction it might take as possible.
        """
        longest = None

        for snake in self._snakes.values():
            if snake["id"] == mine or not snake["alive"]:
                continue

            head = snake["body"][0]
            for step in HEADINGS:
                if self.step_from(head, step) == cell:
                    length = len(snake["body"])
                    if longest is None or length > longest:
                        longest = length
                    break

        return longest

    def room_from(self, cell, heading, limit: int = 60) -> int:
        """How many cells are reachable from here, up to a limit.

        A flood fill, capped, and the cap is the point: the answer only has to
        be big enough to tell a dead end from open ground. Filling an 80x80
        arena for every candidate move of every bot on every tick would be the
        most expensive thing in the game by a wide margin.
        """
        seen = {cell}
        queue = [cell]
        counted = 0

        while queue and counted < limit:
            here = queue.pop()
            counted += 1

            for step in HEADINGS:
                nxt = self.step_from(here, step)
                if nxt is None or nxt in seen:
                    continue
                if self.blocked(nxt):
                    continue
                seen.add(nxt)
                queue.append(nxt)

        return counted
