"""The RuleSet and its schema.

the host's design surface, 40+ options, validated
server-side against a schema with hard bounds, sent to clients in `welcome`,
and saveable as a named preset.

SCHEMA is the single source of truth. The dataclass fields mirror it, the
validator reads bounds from it, the coercion in from_dict reads types from it,
and /api/rules/schema serves it so the rule editor is generated rather than
hand-written. A bound written in two places eventually disagrees with itself;
here it is written once.

Two attributes on each field matter beyond the bounds:

  scope   solo, multiplayer, or both. A player configuring a solo match should
          not be offered a player cap.
  active  whether the option takes effect today. An option that is settable but
          inert is worse than one that is absent, so the editor shows an
          inactive option disabled and marked planned rather than leaving a
          dead control on screen.
"""

import hashlib
import json
from dataclasses import asdict, dataclass


class RuleError(ValueError):
    """A rule value outside its documented bounds."""


# Ticks between moves at each base speed. Speed 1 is five moves a second and
# speed 10 is twenty, which is the range that stays playable on a 60 Hz tick.
# snakes do not move every tick.
SPEED_TO_INTERVAL = {
    1: 12, 2: 11, 3: 10, 4: 9, 5: 8, 6: 7, 7: 6, 8: 5, 9: 4, 10: 3,
}

MIN_INTERVAL_TICKS = 2

UNLIMITED_LIVES = 0

# A layout name read as columns by rows. The arena sizes above are per arena,
# not for the whole grid, so 2x2 at 40x40 is an 80x80 cell space divided into
# four. Kept here rather than parsed from the string wherever it is needed: a
# name is a name, and there is one place that knows what each one means.
# Options that are no longer part of what a solo match is keyed on, but that
# have to stay in the key so that bests set before they moved still match.
# Read at their default, so being in the list costs nothing but the bytes.
#
# Never remove an entry. Adding one is how an option leaves the key safely;
# taking one out is how everybody loses their bests.
KEPT_IN_KEY = ("bot_count", "bot_difficulty")

LAYOUTS = {
    "1x1": (1, 1),
    "2x1": (2, 1),
    "2x2": (2, 2),
}


def _int(key, group, label, default, low, high, **extra):
    entry = {
        "key": key, "group": group, "label": label, "type": "int",
        "default": default, "min": low, "max": high, "step": 1,
        "scope": "both", "active": False, "unit": None,
        "help": "",
        "tier": "advanced", "section": None,
        # True for anything that changes only how the game looks. See
        # fingerprint(): a personal best must not depend on a palette.
        "cosmetic": False,
    }
    entry.update(extra)
    return entry


def _enum(key, group, label, default, options, **extra):
    entry = {
        "key": key, "group": group, "label": label, "type": "enum",
        "default": default, "options": options,
        "scope": "both", "active": False, "unit": None,
        "help": "",
        "tier": "advanced", "section": None,
        # True for anything that changes only how the game looks. See
        # fingerprint(): a personal best must not depend on a palette.
        "cosmetic": False,
    }
    entry.update(extra)
    return entry


def _bool(key, group, label, default, **extra):
    entry = {
        "key": key, "group": group, "label": label, "type": "bool",
        "default": default,
        "scope": "both", "active": False, "unit": None,
        "help": "",
        "tier": "advanced", "section": None,
        # True for anything that changes only how the game looks. See
        # fingerprint(): a personal best must not depend on a palette.
        "cosmetic": False,
    }
    entry.update(extra)
    return entry


def _float(key, group, label, default, low, high, step, **extra):
    entry = {
        "key": key, "group": group, "label": label, "type": "float",
        "default": default, "min": low, "max": high, "step": step,
        "scope": "both", "active": False, "unit": None,
        "help": "",
        "tier": "advanced", "section": None,
        # True for anything that changes only how the game looks. See
        # fingerprint(): a personal best must not depend on a palette.
        "cosmetic": False,
    }
    entry.update(extra)
    return entry


def _printable(text: str) -> str:
    """Drop control and format characters, then collapse the whitespace.

    Deliberately a copy of the rule in room.py rather than an import of it:
    rules.py is the game and knows nothing about the network, and one shared
    four line helper is not worth a dependency in that direction.
    """
    kept = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return " ".join(kept.split())


def _text(key, group, label, default, max_length, **extra):
    entry = {
        "key": key, "group": group, "label": label, "type": "text",
        "default": default, "max_length": max_length,
        "scope": "both", "active": False, "unit": None,
        "help": "",
        "tier": "advanced", "section": None,
        # True for anything that changes only how the game looks. See
        # fingerprint(): a personal best must not depend on a palette.
        "cosmetic": False,
    }
    entry.update(extra)
    return entry


# -- how one option depends on another ---------------------------------------
#
# Two relationships, and the difference between them is what the editor shows.
#
# **needs** is irrelevance. The option has no effect at all while the condition
# is unmet, so the editor hides it rather than greying it out: a row that cannot
# matter is noise, and a screen of greyed-out rows reads as something being
# broken. Its stored value is left alone, so turning the parent back on brings
# back what was set rather than a default.
#
# **forced** is a rule of the game. The option would have an effect, but this
# combination is not allowed, so the editor shows it, locks it, sets it, and
# says why. Hiding it here would leave a player wondering what happened to a
# setting they had chosen.
#
# A condition is {"key": ..., "in": [...]}, and a list of them is satisfied when
# ANY one is, which is what effect_duration needs: it matters if either family
# of items is on the floor.
#
# Kept here rather than in the editor because both ends have to agree. The
# editor cannot be the only thing that knows a combination is disallowed, or a
# saved setup from before the rule existed would load, look legal, and play by
# something else entirely. validate() below reads the same table.
NEEDS = {
    # Either way of asking for bots. Filling empty slots produces them with a
    # count of zero, so a single condition on the count would hide the
    # difficulty of the bots a host had just asked for.
    "bot_difficulty": [
        {"key": "bot_count", "in": list(range(1, 12))},
        {"key": "bots_fill_slots", "in": [True]},
    ],
    # A chosen code carries no address. It is resolved by finding the room
    # broadcasting it, and a private room broadcasts nothing, so on a private
    # room the field would be a setting that silently does nothing.
    "room_code": [{"key": "visibility", "in": ["public"]}],

    # Aggression is willingness to cut, and cutting is what severing is. With
    # severing off, a head that meets a body kills the snake that moved, so
    # there is nothing here for a bot to be willing about.
    "bot_aggression": [{"key": "severing", "in": [True]}],

    "length_penalty": [{"key": "length_affects_speed", "in": [True]}],
    "max_slowdown": [{"key": "length_affects_speed", "in": [True]}],

    "score_target": [{"key": "win_condition", "in": ["first_to_score"]}],
    "kill_target": [{"key": "win_condition", "in": ["first_to_kills"]}],
    "time_limit": [{"key": "win_condition", "in": ["timed"]}],

    "remains_lifetime": [{"key": "severing", "in": [True]}],
    "remains_yield": [{"key": "severing", "in": [True]}],

    "slow_poison": [{"key": "poisons", "in": ["low", "normal", "high"]}],
    "shrink_poison": [{"key": "poisons", "in": ["low", "normal", "high"]}],
    "confusion_poison": [{"key": "poisons", "in": ["low", "normal", "high"]}],

    "burst_pickup": [{"key": "pickups", "in": ["low", "normal", "high"]}],
    "phase_pickup": [{"key": "pickups", "in": ["low", "normal", "high"]}],
    "magnet_pickup": [{"key": "pickups", "in": ["low", "normal", "high"]}],

    # Either family. This is the one that needs the any-of reading.
    "effect_duration": [
        {"key": "poisons", "in": ["low", "normal", "high"]},
        {"key": "pickups", "in": ["low", "normal", "high"]},
    ],
}

# key -> {"when": [conditions], "value": ..., "reason": "..."}
FORCED = {
    "edge_behaviour": {
        "when": [{"key": "layout", "in": ["2x1", "2x2"]}],
        "value": "wrap",
        "reason": "A grid of arenas always wraps.",
    },
}


def _met(conditions, rules) -> bool:
    """True when any one condition holds. rules is a dict or a RuleSet."""
    read = rules.get if isinstance(rules, dict) else (
        lambda key, default=None: getattr(rules, key, default)
    )
    for condition in conditions:
        if read(condition["key"]) in condition["in"]:
            return True
    return False


def relevant(key: str, rules) -> bool:
    """False when this option cannot have any effect on that rule set."""
    conditions = NEEDS.get(key)
    return True if conditions is None else _met(conditions, rules)


def forced_value(key: str, rules):
    """What this option is pinned to, or None when it is free."""
    rule = FORCED.get(key)
    if rule is None or not _met(rule["when"], rules):
        return None
    return rule["value"]


def coerce(values: dict) -> dict:
    """Apply every forced value to a rule set, and say nothing about the rest.

    Called before validation rather than as part of it, because a forced value
    is not a mistake the player made. Somebody who set walls and then chose a
    grid should get a grid that wraps, not an error telling them to go and
    change a setting the editor has already locked.
    """
    settled = dict(values)
    for key in FORCED:
        pinned = forced_value(key, settled)
        if pinned is not None:
            settled[key] = pinned
    return settled


SCHEMA = [
    # -- Arena ------------------------------------------------------------
    _enum("layout", "Arena", "Layout", "1x1", ["1x1", "2x1", "2x2"],
          scope="multiplayer", active=True,
          help="Multiple arenas joined edge to edge. You see only the one you "
               "are in.", tier="basic"),
    _int("arena_width", "Arena", "Arena width", 40, 20, 80,
         active=True, unit="cells", tier="basic"),
    _int("arena_height", "Arena", "Arena height", 40, 20, 80,
         active=True, unit="cells", tier="basic"),
    _enum("edge_behaviour", "Arena", "Edges", "wrap", ["wrap", "walls"],
          active=True,
          help="Wrap re-enters the opposite edge. Walls end the run.", tier="basic"),
    _enum("theme_set", "Arena", "Theme", "classic",
          ["classic", "neon", "earth", "mono"], active=True, tier="basic",
          cosmetic=True,
          help="Arena colours. Everybody in a room sees the host's choice. "
               "Mono tells the arenas apart by brightness and pattern rather "
               "than by colour."),

    # -- Movement ---------------------------------------------------------
    _int("base_speed", "Movement", "Base speed", 5, 1, 10,
         active=True, tier="basic"),
    _bool("length_affects_speed", "Movement", "Length affects speed", True,
          active=True,
          help="A longer snake moves more slowly, down to the ceiling below.",
          tier="basic"),
    _int("length_penalty", "Movement", "Length penalty strength", 3, 1, 5,
         active=True),
    _int("starting_length", "Movement", "Starting length", 3, 3, 10,
         active=True, unit="segments", tier="basic"),
    _float("max_slowdown", "Movement", "Maximum slowdown", 2.0, 1.5, 3.0, 0.1,
           active=True, unit="x",
           help="Total slowdown never exceeds this multiple of base speed."),

    # -- Combat -----------------------------------------------------------
    _bool("severing", "Combat", "Severing", True,
          scope="multiplayer", active=True,
          help="Running into a snake cuts it there instead of killing you.",
          tier="basic"),
    _enum("self_collision", "Combat", "Self collision", "sever",
          ["sever", "kill"], scope="multiplayer", active=True,
          help="What running into your own body does. Needs severing on.",
          tier="basic"),
    _enum("head_on", "Combat", "Head-on rule", "longer_wins",
          ["longer_wins", "both_die"], scope="multiplayer", active=True,
          help="Longer wins, or both die. Equal lengths always both die.",
          tier="basic"),
    _int("min_length_before_death", "Combat", "Minimum length before death",
         2, 1, 5, scope="multiplayer", unit="segments", active=True,
         help="Cut shorter than this and you lose a life."),
    _int("remains_lifetime", "Combat", "Dropped remains lifetime", 10, 3, 30,
         scope="multiplayer", unit="s", active=True, section="Remains"),
    _int("remains_yield", "Combat", "Dropped remains yield", 100, 50, 100,
         scope="multiplayer", unit="%", active=True,
         help="How much of a cut snake ends up on the floor.", section="Remains"),
    _int("kill_bounty", "Combat", "Kill bounty", 25, 0, 100,
         scope="multiplayer", unit="%", active=True,
         help="Share of a killed snake's length paid to whoever killed it."),
    _int("sever_score_cost", "Combat", "Score cost of being cut", 10, 0, 100,
         scope="multiplayer", unit="%", active=True,
         help="Share of the segments lost that also comes off the score. "
              "Zero means a cut costs length but no points."),

    # -- Match ------------------------------------------------------------
    _enum("win_condition", "Match", "Win condition", "last_standing",
          ["last_standing", "first_to_score", "first_to_kills", "timed",
           "endless"], scope="multiplayer", active=True, tier="basic"),
    _int("score_target", "Match", "Score target", 200, 50, 1000,
         scope="multiplayer", active=True, tier="basic"),
    _int("kill_target", "Match", "Kill target", 5, 3, 25,
         scope="multiplayer", active=True, tier="basic"),
    _int("time_limit", "Match", "Time limit", 10, 2, 30,
         scope="multiplayer", unit="min", active=True, tier="basic"),
    _int("lives", "Match", "Lives", 3, 0, 9,
         scope="multiplayer", active=True,
         help="Zero means unlimited, which last standing cannot end on.", tier="basic"),
    _int("respawn_delay", "Match", "Respawn delay", 2, 0, 5,
         scope="multiplayer", unit="s", active=True, tier="basic"),
    _int("spawn_protection", "Match", "Spawn protection", 2, 0, 5,
         scope="multiplayer", unit="s", active=True,
         help="Nothing can kill you, and you can kill nothing, for this long."),
    _int("player_cap", "Match", "Player cap", 8, 2, 12,
         scope="multiplayer", active=True),
    _int("min_players", "Match", "Minimum players to start", 2, 2, 12,
         scope="multiplayer", active=True),
    _int("start_countdown", "Match", "Start countdown", 5, 3, 10,
         scope="multiplayer", unit="s", active=True),

    # -- Items ------------------------------------------------------------
    _enum("food_density", "Items", "Food density", "normal",
          ["sparse", "normal", "dense"], active=True, tier="basic"),
    _enum("poisons", "Items", "Poisons", "normal",
          ["off", "low", "normal", "high"], active=True,
          help="How much of the floor is poison. Each kind can be switched "
               "off separately below.", tier="basic"),
    _bool("slow_poison", "Items", "Slow poison", True, active=True,
          help="Temporarily slower, never frozen.", section="Poisons"),
    _bool("shrink_poison", "Items", "Shrink poison", True, active=True,
          help="Costs length, and the points that length was worth.",
          section="Poisons"),
    _bool("confusion_poison", "Items", "Confusion poison", True, active=True,
          help="Reverses your controls for the duration.", section="Poisons"),
    _int("effect_duration", "Items", "Effect duration", 6, 3, 15, unit="s",
         active=True,
         help="How long a poison or a pickup lasts."),
    _enum("pickups", "Items", "Beneficial pickups", "low",
          ["off", "low", "normal", "high"], active=True,
          help="How much of the floor is a reward rather than a punishment.",
          tier="basic"),
    _bool("burst_pickup", "Items", "Burst pickup", True, active=True,
          help="A short turn of speed, whatever your length.", section="Pickups"),
    _bool("phase_pickup", "Items", "Phase pickup", True, active=True,
          help="Pass through bodies, briefly. You cannot cut while phasing.",
          section="Pickups"),
    _bool("magnet_pickup", "Items", "Magnet pickup", True, active=True,
          help="Loose food drifts toward you. Dropped remains do not.",
          section="Pickups"),

    # -- Bots -------------------------------------------------------------
    # Multiplayer only for now. Single player is one snake all the way down --
    # it has no cell ownership, no severing and no second body -- so bots there
    # are not a switch to flip but a rewrite of that engine, and doing it badly
    # would mean two simulations that disagree about the same rules.
    _int("bot_count", "Bots", "Bot count", 0, 0, 11,
         scope="multiplayer", active=True, tier="basic",
         help="Computer players in the room, up to the player cap."),
    _enum("bot_difficulty", "Bots", "Bot difficulty", "medium",
          ["easy", "medium", "hard"],
          scope="multiplayer", active=True, tier="basic",
          help="What they understand, not how fast they react. Easy chases "
               "food; medium avoids poisons and dead ends; hard hunts shorter "
               "snakes and contests remains."),
    _enum("bot_aggression", "Bots", "Bot aggression", "normal",
          ["off", "rare", "normal", "relentless"],
          scope="multiplayer", active=True, tier="basic",
          help="How willing bots are to cut other snakes. Each bot draws its "
               "own temperament from this when it spawns, so some hunt and "
               "some feed. Needs severing on to mean anything."),
    _bool("bots_fill_slots", "Bots", "Bots fill empty slots", False,
          scope="multiplayer", active=True,
          help="Top the room up to the player cap with bots."),

    # -- Room -------------------------------------------------------------
    _text("room_name", "Room", "Room name", "", 24,
          scope="multiplayer", active=True,
          help="Left empty, a name is generated.", tier="basic"),
    _enum("visibility", "Room", "Visibility", "public", ["public", "private"],
          scope="multiplayer", active=True,
          help="A private room broadcasts nothing. The code is the only way in.",
          tier="basic"),
    _text("room_code", "Room", "Room code", "", 12,
          scope="multiplayer", active=True, tier="basic",
          help="A code of your own that people can type instead of the "
               "generated one. Only works for public rooms, because it is "
               "matched against rooms on the network rather than carrying an "
               "address the way the generated code does."),
    _bool("allow_join_in_progress", "Room", "Allow join in progress", True,
          scope="multiplayer", active=True),
    _bool("allow_spectators", "Room", "Allow spectators", True,
          scope="multiplayer", active=True,
          help="Let people watch without taking a seat. A spectator follows "
               "one player and sees exactly what that player sees, so on a "
               "grid of arenas watching reveals no more than playing does. "
               "Turning this off refuses new spectators; anyone already "
               "watching stays until they leave or you remove them."),
]

BY_KEY = {entry["key"]: entry for entry in SCHEMA}

GROUP_ORDER = ["Arena", "Movement", "Combat", "Match", "Items", "Bots", "Room"]


@dataclass
class RuleSet:
    # Arena
    layout: str = "1x1"
    arena_width: int = 40
    arena_height: int = 40
    edge_behaviour: str = "wrap"
    theme_set: str = "classic"

    # Movement
    base_speed: int = 5
    length_affects_speed: bool = True
    length_penalty: int = 3
    starting_length: int = 3
    max_slowdown: float = 2.0

    # Combat
    severing: bool = True
    self_collision: str = "sever"
    head_on: str = "longer_wins"
    min_length_before_death: int = 2
    remains_lifetime: int = 10
    remains_yield: int = 100
    sever_score_cost: int = 10
    kill_bounty: int = 25

    # Match
    win_condition: str = "last_standing"
    score_target: int = 200
    kill_target: int = 5
    time_limit: int = 10
    lives: int = 3
    respawn_delay: int = 2
    spawn_protection: int = 2
    player_cap: int = 8
    min_players: int = 2
    start_countdown: int = 5

    # Items
    food_density: str = "normal"
    poisons: str = "normal"
    slow_poison: bool = True
    shrink_poison: bool = True
    confusion_poison: bool = True
    effect_duration: int = 6
    pickups: str = "low"
    burst_pickup: bool = True
    phase_pickup: bool = True
    magnet_pickup: bool = True

    # Bots
    bot_count: int = 0
    bot_difficulty: str = "medium"
    bot_aggression: str = "normal"
    bots_fill_slots: bool = False

    # Room
    room_name: str = ""
    visibility: str = "public"
    room_code: str = ""
    allow_join_in_progress: bool = True
    allow_spectators: bool = True

    # -- validation -------------------------------------------------------

    def errors(self) -> dict:
        """Return {field: message} for everything out of bounds.

        Returning all of them at once matters for the editor: fixing one
        problem, resubmitting, and being told about the next one is a miserable
        way to configure forty options.
        """
        found = {}

        for entry in SCHEMA:
            key = entry["key"]
            value = getattr(self, key)
            kind = entry["type"]

            if kind == "bool":
                if not isinstance(value, bool):
                    found[key] = "must be true or false"
                continue

            if kind == "enum":
                if value not in entry["options"]:
                    found[key] = "must be one of " + ", ".join(entry["options"])
                continue

            if kind == "text":
                if not isinstance(value, str):
                    found[key] = "must be text"
                elif len(value) > entry["max_length"]:
                    found[key] = "must be at most {} characters".format(
                        entry["max_length"]
                    )
                continue

            if isinstance(value, bool) or not isinstance(value, (int, float)):
                found[key] = "must be a number"
                continue
            if kind == "int" and not float(value).is_integer():
                found[key] = "must be a whole number"
                continue
            if value < entry["min"] or value > entry["max"]:
                found[key] = "must be between {} and {}".format(
                    entry["min"], entry["max"]
                )

        # Cross-field rules. These cannot live in the per-field schema because
        # they depend on more than one value.
        if "starting_length" not in found and "arena_width" not in found:
            if self.starting_length >= min(self.arena_width, self.arena_height):
                found["starting_length"] = "does not fit inside the arena"

        if "min_players" not in found and "player_cap" not in found:
            if self.min_players > self.player_cap:
                found["min_players"] = "cannot exceed the player cap"

        if "bot_count" not in found and "player_cap" not in found:
            if self.bot_count > self.player_cap - 1:
                found["bot_count"] = "leaves no room for a human player"

        # Last standing ends when everyone but one player is out of lives, so
        # with unlimited lives it never ends at all. Refusing the pair here is
        # better than shipping a room that cannot finish a match.
        if "lives" not in found and "win_condition" not in found:
            if self.win_condition == "last_standing" and self.lives == UNLIMITED_LIVES:
                found["lives"] = "cannot be unlimited when last standing wins"

        return found

    def validate(self) -> "RuleSet":
        found = self.errors()
        if found:
            key = sorted(found)[0]
            raise RuleError(f"{key} {found[key]}")
        return self

    # -- construction -----------------------------------------------------

    @classmethod
    def from_dict(cls, payload: dict, validate: bool = True) -> "RuleSet":
        """Build from untrusted input, ignoring unknown keys.

        Coercion is driven by the schema type, so a form posting "40" as a
        string produces the same ruleset as an API client posting 40.
        """
        cleaned = {}

        for key, value in (payload or {}).items():
            entry = BY_KEY.get(key)
            if entry is None:
                continue

            kind = entry["type"]
            try:
                if kind == "int":
                    cleaned[key] = int(value)
                elif kind == "float":
                    cleaned[key] = round(float(value), 3)
                elif kind == "bool":
                    if isinstance(value, str):
                        cleaned[key] = value.lower() in ("1", "true", "yes", "on")
                    else:
                        cleaned[key] = bool(value)
                elif kind == "text":
                    # Same treatment a username gets, and for the same reason:
                    # the room name is drawn on every other machine on the
                    # subnet, in the room list, before anybody has joined.
                    cleaned[key] = _printable(
                        str(value)
                    )[: entry["max_length"]]
                else:
                    cleaned[key] = value
            except (TypeError, ValueError) as error:
                raise RuleError(f"{key} is not a valid value") from error

        # Settled before validation, so a combination the editor locks is
        # applied here too. Anything arriving from an older saved setup, from a
        # host on a different build, or from a client that never saw the editor
        # is brought into line rather than played as sent.
        merged = coerce({**DEFAULTS.to_dict(), **cleaned})
        cleaned = {key: merged[key] for key in cleaned} if cleaned else cleaned
        for key in FORCED:
            if key in merged:
                cleaned[key] = merged[key]

        ruleset = cls(**cleaned)
        return ruleset.validate() if validate else ruleset

    def to_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        """Stable id for this exact configuration.

        personal bests are keyed by a hash of the
        ruleset, so a best set on a 20x20 walled arena is not compared against
        one set on 80x80 with wrap.

        Only the options that actually change how a solo match plays are
        included. Without that, renaming a room would reset your personal best.

        The key is a fixed set of options, not "whatever is scoped to solo
        today". Scope is an editor concern and it moves: rescoping bot_count
        and bot_difficulty to multiplayer dropped them from the key and
        orphaned every personal best anybody held, which was noticed only by
        hashing an older copy of this file and comparing. KEPT_IN_KEY below is
        the list of options that have left the key's natural definition but
        must stay in it, read at their default so their value cannot matter.

        Cosmetic options are read at their default rather than left out. Left
        out, the hash would change shape and every best anybody already holds
        would be orphaned the moment this shipped; pinned to the default, the
        hash is byte for byte what it was before themes existed, and picking a
        different one changes nothing. That is worth the small oddity of a
        fingerprint that reports a value the player is not using.
        """
        significant = {}
        for key in sorted(BY_KEY):
            entry = BY_KEY[key]
            counts = entry["scope"] in ("solo", "both") or key in KEPT_IN_KEY
            if not counts:
                continue
            pinned = entry.get("cosmetic") or key in KEPT_IN_KEY
            significant[key] = entry["default"] if pinned else getattr(self, key)
        canonical = json.dumps(significant, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]

    # -- derived values ---------------------------------------------------

    def base_interval_ticks(self) -> int:
        return SPEED_TO_INTERVAL[int(self.base_speed)]

    def move_interval_ticks(self, length: int) -> int:
        base = self.base_interval_ticks()

        if not self.length_affects_speed:
            return base

        growth = max(0, length - self.starting_length)
        penalty = growth * self.length_penalty / 10.0
        ceiling = base * self.max_slowdown

        return int(max(MIN_INTERVAL_TICKS, min(base + penalty, ceiling)))

    def max_interval_ticks(self) -> int:
        return int(self.base_interval_ticks() * self.max_slowdown)

    def grid_shape(self) -> tuple:
        """The layout as (columns, rows). Unknown names read as a single arena.

        Single player never asks: it builds one arena from the width and the
        height and ignores the layout entirely, which is why an unknown name
        falls back to 1x1 rather than raising. The validator is what refuses a
        bad name; this is only ever asked about a ruleset that passed it.
        """
        return LAYOUTS.get(self.layout, (1, 1))

    def arena_count(self) -> int:
        columns, rows = self.grid_shape()
        return columns * rows

    def food_target(self) -> int:
        """Food for one arena. A grid multiplies this by how many it has.

        Per arena rather than per grid, so the density a host sets is the
        density they see wherever they are standing. A grid target divided four
        ways would make the same setting mean a quarter as much food the moment
        the layout changed.
        """
        area = self.arena_width * self.arena_height
        per_density = {"sparse": 2200, "normal": 800, "dense": 320}
        return max(1, round(area / per_density[self.food_density]))


DEFAULTS = RuleSet()


def schema_document() -> dict:
    """The payload served by /api/rules/schema."""
    groups = []

    for name in GROUP_ORDER:
        fields = [dict(entry) for entry in SCHEMA if entry["group"] == name]
        if fields:
            groups.append({"name": name, "fields": fields})

    return {
        "groups": groups,
        "defaults": DEFAULTS.to_dict(),
        "group_order": GROUP_ORDER,
        # Sent rather than duplicated in the editor. One table, read by the
        # screen that hides a row and by the validator that settles it.
        "needs": NEEDS,
        "forced": FORCED,
    }


# The presets that ship with the game. Read only: a player who wants a variation
# saves their own copy under a new name.
SHIPPED_PRESETS = {
    "Classic": RuleSet(
        edge_behaviour="walls",
        base_speed=5,
        length_affects_speed=False,
        severing=False,
        poisons="off",
        slow_poison=False,
        shrink_poison=False,
        confusion_poison=False,
        pickups="off",
        burst_pickup=False,
        phase_pickup=False,
        magnet_pickup=False,
        food_density="normal"),
    "Brawl": RuleSet(
        base_speed=7,
        severing=True,
        poisons="high",
        pickups="normal",
        food_density="dense",
        lives=UNLIMITED_LIVES,
        win_condition="timed",
        time_limit=10),
    "Hide and Seek": RuleSet(
        layout="2x2",
        arena_width=70,
        arena_height=70,
        base_speed=4,
        severing=True,
        food_density="sparse",
        poisons="low",
        lives=3),
}
