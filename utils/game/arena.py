"""The arena grid and its occupancy index.

collision checking must not be a per-tick
comparison of every segment against every other. The occupancy grid is
maintained incrementally as snakes move, so a collision test is one lookup.
This is built in from the start because retrofitting it later means rewriting
severing.

Several arenas joined edge to edge are one cell space, not several. The grid is
a way of reading a coordinate rather than a collection of objects: with a 2x2
layout of 40x40 arenas the space is 80x80 cells, and which arena a cell is in is
`(y // 40) * 2 + (x // 40)`. Topology below is that reading, and nothing else.

That choice is the reason wrapping between arenas needed no new code. Wrap
across the whole grid is the wrap this file already had at the outer edge, a
body spanning a boundary is not a case at all, and severing, ownership, remains,
head-on staging and food spawning all work on cells that never stopped being in
one space. The cost is paid on the other side: which arena a player can see is
then a filter that has to be written on purpose rather than something that falls
out of the structure.
"""

EMPTY = 0
SNAKE = 1
FOOD = 2
WALL = 3

# Anything on the floor that is not food and is not a body: a poison or a
# beneficial pickup. One cell value for all of them, with what it actually is
# held beside the arena in `items`, because the collision test only ever needs
# to know that the cell is occupied by something edible and the effect is
# looked up once, at the moment it is eaten.
ITEM = 4


class Topology:
    """How one cell space is divided into arenas, and where they sit.

    Holds no cells and no state. Everything here is arithmetic on a coordinate,
    so it can be asked anything about the layout at any time without being kept
    in step with the arena it describes.

    Arena ids run left to right and then top to bottom, so in a 2x2 layout the
    right edge of arena 0 leads to arena 1 and the bottom edge of arena 0 leads
    to arena 2. That is the order the layout is described in everywhere else.
    """

    def __init__(self, arena_width: int, arena_height: int,
                 columns: int = 1, rows: int = 1):
        self.arena_width = arena_width
        self.arena_height = arena_height
        self.columns = max(1, columns)
        self.rows = max(1, rows)

        # The whole cell space. This is what an Arena is built with.
        self.width = arena_width * self.columns
        self.height = arena_height * self.rows

    @property
    def count(self) -> int:
        return self.columns * self.rows

    @property
    def single(self) -> bool:
        return self.count == 1

    def arena_at(self, x: int, y: int) -> int:
        """Which arena a cell is in."""
        column = min(self.columns - 1, max(0, x // self.arena_width))
        row = min(self.rows - 1, max(0, y // self.arena_height))
        return row * self.columns + column

    def column_row(self, arena_id: int) -> tuple:
        arena_id = max(0, min(self.count - 1, arena_id))
        return (arena_id % self.columns, arena_id // self.columns)

    def origin(self, arena_id: int) -> tuple:
        """The top-left cell of an arena, in whole-space coordinates."""
        column, row = self.column_row(arena_id)
        return (column * self.arena_width, row * self.arena_height)

    def neighbour(self, arena_id: int, heading) -> int:
        """The arena reached by leaving this one in a direction.

        The grid is toroidal, so leaving the rightmost column arrives in the
        leftmost one. With walls on there is no crossing at the outer edge at
        all, and the caller is the one that knows that; this answers the
        topology question and nothing about the rules.
        """
        column, row = self.column_row(arena_id)
        column = (column + heading[0]) % self.columns
        row = (row + heading[1]) % self.rows
        return row * self.columns + column

    def to_dict(self) -> dict:
        return {
            "aw": self.arena_width,
            "ah": self.arena_height,
            "cols": self.columns,
            "rows": self.rows,
        }


class Arena:
    def __init__(self, width: int, height: int, edge_behaviour: str, arena_id: int = 0):
        self.id = arena_id
        self.width = width
        self.height = height
        self.edge_behaviour = edge_behaviour
        self.cells = bytearray(width * height)
        self.food: set = set()

        # cell -> kind, for the cells marked ITEM. A dict rather than a second
        # byte array: there are a handful of these and the kind is a name.
        self.items: dict = {}

    # -- indexing ---------------------------------------------------------

    def index(self, x: int, y: int) -> int:
        return y * self.width + x

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def at(self, x: int, y: int) -> int:
        if not self.inside(x, y):
            return WALL
        return self.cells[self.index(x, y)]

    def set(self, x: int, y: int, value: int) -> None:
        if self.inside(x, y):
            self.cells[self.index(x, y)] = value

    def clear(self, x: int, y: int) -> None:
        self.set(x, y, EMPTY)

    # -- movement ---------------------------------------------------------

    def step_from(self, x: int, y: int, heading) -> tuple:
        """Return the next cell, or None when a wall ends the move.

        With wrap on, leaving the top re-enters the bottom and leaving the left
        re-enters the right. With walls on, the caller treats None as a
        collision.
        """
        next_x = x + heading[0]
        next_y = y + heading[1]

        if self.inside(next_x, next_y):
            return (next_x, next_y)

        if self.edge_behaviour == "wrap":
            return (next_x % self.width, next_y % self.height)

        return None

    # -- food -------------------------------------------------------------

    def add_food(self, x: int, y: int) -> None:
        self.food.add((x, y))
        self.set(x, y, FOOD)

    def remove_food(self, x: int, y: int) -> None:
        self.food.discard((x, y))
        if self.at(x, y) == FOOD:
            self.clear(x, y)

    # -- items ------------------------------------------------------------

    def add_item(self, x: int, y: int, kind: str) -> None:
        self.items[(x, y)] = kind
        self.set(x, y, ITEM)

    def remove_item(self, x: int, y: int) -> None:
        self.items.pop((x, y), None)
        if self.at(x, y) == ITEM:
            self.clear(x, y)

    def item_at(self, x: int, y: int):
        return self.items.get((x, y))

    def empty_cells(self) -> int:
        return self.cells.count(EMPTY)

    def reset(self) -> None:
        self.cells = bytearray(self.width * self.height)
        self.food.clear()
        self.items.clear()
