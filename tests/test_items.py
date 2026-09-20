"""Poisons and pickups.

The rule this whole group is built on is that everything an item does is a
change to a number that already exists. There is no second speed system, no
second collision system and no second economy: a slow adds to the same
`move_interval` the length penalty writes to, a shrink charges the same
percentage a cut charges, and a phase is a clause in the collision the game
already resolves. Most of what is checked here is that, rather than the effects
themselves.
"""

import time
from collections import deque

import pytest

from utils.game.arena import EMPTY, ITEM, SNAKE, Arena
from utils.game.effects import (
    BURST_FACTOR,
    MAGNET_RANGE,
    SHRINK_SEGMENTS,
    SLOW_FACTOR,
    Effects,
    confuse,
    heading_for,
    move_interval_for,
)
from utils.game.engine import SoloEngine
from utils.game.items import (
    PICKUPS,
    POISONS,
    enabled_kinds,
    item_target,
    spawn_items,
)
from utils.game.match import PHASE_RUNNING, MatchEngine
from utils.game.rules import RuleSet

ROSTER = [(0, "ann", "#ef6461"), (1, "bob", "#3ecf8e")]


# -- the effects themselves --------------------------------------------------


def test_an_effect_ends_when_its_time_is_up():
    effects = Effects()
    now = time.monotonic()
    effects.apply("slow", 6, now)

    assert effects.active("slow", now)
    assert effects.active("slow", now + 5.9)
    assert not effects.active("slow", now + 6.1)


def test_effects_do_not_stack():
    """A second one of the same kind resets the clock rather than doubling.

    Stacking makes the worst case unbounded, and the mechanic is meant to be a
    setback rather than a spiral.
    """
    effects = Effects()
    now = time.monotonic()

    effects.apply("slow", 6, now)
    effects.apply("slow", 6, now + 3)

    assert effects.remaining("slow", now + 3) == pytest.approx(6, abs=0.01)


def test_the_countdown_is_sent_in_quarter_seconds():
    """Rounded so it can delta. An exact figure differs in every message by

    definition, so it would be in every message; a bucket changes four times a
    second while an effect runs and not at all the rest of the time.
    """
    effects = Effects()
    now = time.monotonic()
    effects.apply("slow", 6, now)

    assert effects.to_wire(now)["slow"] == 6000
    assert effects.to_wire(now + 0.1)["slow"] == 6000
    assert effects.to_wire(now + 0.3)["slow"] == 5750
    assert "slow" not in effects.to_wire(now + 6.1)


def test_the_countdown_never_reads_zero_while_it_is_still_running():
    effects = Effects()
    now = time.monotonic()
    effects.apply("slow", 6, now)

    assert effects.to_wire(now + 5.99)["slow"] > 0


def test_confusion_is_a_reversal_and_not_a_scramble():
    """Fixed rather than rolled, so both ends arrive at it independently.

    A random remap would mean the client predicting one turn and being
    corrected into another, every turn, for the whole duration.
    """
    assert confuse((1, 0)) == (-1, 0)
    assert confuse((0, -1)) == (0, 1)

    effects = Effects()
    now = time.monotonic()
    assert heading_for(effects, (1, 0), now) == (1, 0)

    effects.apply("confusion", 6, now)
    assert heading_for(effects, (1, 0), now) == (-1, 0)


# -- speed, which is one number and not several ------------------------------


def test_a_slow_is_held_by_the_same_ceiling_the_length_penalty_is():
    rules = RuleSet(base_speed=5, starting_length=8, length_affects_speed=True)
    effects = Effects()
    now = time.monotonic()
    effects.apply("slow", 6, now)

    slowed = move_interval_for(rules, 200, effects, now)

    assert slowed == rules.max_interval_ticks(), (
        "a long snake that eats a slow poison is never frozen"
    )


def test_a_slow_actually_slows_a_snake_that_is_not_at_the_ceiling():
    rules = RuleSet(base_speed=5, starting_length=8, length_affects_speed=False)
    effects = Effects()
    now = time.monotonic()

    plain = move_interval_for(rules, 10, effects, now)
    effects.apply("slow", 6, now)
    slowed = move_interval_for(rules, 10, effects, now)

    assert slowed == round(plain * SLOW_FACTOR)


def test_a_burst_acts_on_the_slowed_number_not_the_clean_one():
    """A burst is meant to be an escape from a slow, so the two compose.

    Applying it to the clean interval instead would make the pair faster than a
    burst alone, which is the wrong shape entirely.
    """
    rules = RuleSet(base_speed=5, starting_length=8, length_affects_speed=False)
    now = time.monotonic()

    burst = Effects()
    burst.apply("burst", 6, now)

    both = Effects()
    both.apply("slow", 6, now)
    both.apply("burst", 6, now)

    plain = move_interval_for(rules, 10, Effects(), now)

    assert move_interval_for(rules, 10, burst, now) < plain
    assert move_interval_for(rules, 10, both, now) > move_interval_for(
        rules, 10, burst, now
    )


def test_a_burst_cannot_go_faster_than_the_game_will_simulate():
    rules = RuleSet(base_speed=10, starting_length=8, length_affects_speed=False)
    effects = Effects()
    now = time.monotonic()
    effects.apply("burst", 6, now)

    assert move_interval_for(rules, 8, effects, now) >= 2
    assert BURST_FACTOR < 1.0


# -- what is on the floor ----------------------------------------------------


def test_a_family_with_every_kind_switched_off_puts_nothing_out():
    rules = RuleSet(poisons="high", slow_poison=False, shrink_poison=False,
                    confusion_poison=False)
    kinds = enabled_kinds(rules, POISONS)

    assert kinds == ()
    assert item_target(rules, rules.poisons, kinds) == 0


def test_a_density_of_off_puts_nothing_out_either():
    rules = RuleSet(poisons="off")
    kinds = enabled_kinds(rules, POISONS)

    assert kinds == POISONS, "the kinds are still on; the density is not"
    assert item_target(rules, rules.poisons, kinds) == 0


def test_the_target_counts_a_family_rather_than_each_kind():
    arena = Arena(40, 40, "wrap")
    spawn_items(arena, POISONS, 3)

    assert len(arena.items) == 3, "three poisons, not three of each"


def test_items_only_land_on_empty_cells():
    arena = Arena(10, 10, "wrap")
    arena.set(0, 0, SNAKE)
    arena.add_food(1, 0)

    spawn_items(arena, ("slow",), 90)

    assert arena.at(0, 0) == SNAKE
    assert (1, 0) not in arena.items
    assert all(arena.at(x, y) == ITEM for x, y in arena.items)


def test_an_item_cell_is_cleared_when_it_is_taken():
    arena = Arena(10, 10, "wrap")
    arena.add_item(4, 4, "slow")

    assert arena.item_at(4, 4) == "slow"

    arena.remove_item(4, 4)

    assert arena.at(4, 4) == EMPTY
    assert arena.item_at(4, 4) is None


# -- the shared match --------------------------------------------------------


def build(**overrides):
    settings = {
        "arena_width": 24,
        "arena_height": 24,
        "base_speed": 10,
        "start_countdown": 0,
        "win_condition": "endless",
        "lives": 3,
        "starting_length": 8,
        "spawn_protection": 0,
        "poisons": "off",
        "pickups": "off",
    }
    settings.update(overrides)

    engine = MatchEngine(RuleSet(**settings), ROSTER, seed=3)
    engine.start()
    engine.starts_at = time.monotonic() - 0.001
    engine.phase = PHASE_RUNNING

    for cell in list(engine.arena.food):
        engine.arena.remove_food(cell[0], cell[1])
    engine.arena.items.clear()

    return engine


def place(engine, player_id, cells, heading):
    snake = engine.snakes[player_id]
    for cell in list(snake.body):
        engine._clear(cell, player_id)

    snake.body = deque(cells)
    snake.heading = snake.pending_heading = heading
    snake.alive = True
    snake.protected_until = 0.0
    snake.arena_id = engine.topology.arena_at(cells[0][0], cells[0][1])

    for cell in snake.body:
        engine._occupy(cell, player_id)


def feed(engine, player_id, kind):
    """Put an item directly in front of a head and take one move."""
    snake = engine.snakes[player_id]
    ahead = (snake.head[0] + snake.heading[0], snake.head[1] + snake.heading[1])
    engine.arena.add_item(ahead[0], ahead[1], kind)
    engine._advance([snake], time.monotonic())
    return ahead


def test_eating_an_item_consumes_it_and_does_not_feed_you():
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))

    before = engine.snakes[0].length
    cell = feed(engine, 0, "burst")

    assert cell not in engine.arena.items
    assert engine.snakes[0].length == before, (
        "growing on a pickup would make it food with extras"
    )


def test_a_slow_poison_slows_the_snake_that_ate_it():
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))

    before = engine.snakes[0].move_interval
    feed(engine, 0, "slow")

    assert engine.snakes[0].effects.active("slow")
    assert engine.snakes[0].move_interval > before


def test_a_shrink_poison_charges_exactly_what_a_cut_would():
    """Not merely "something". The same segments lost to a cut and to a poison

    have to cost the same, or the cheaper of the two becomes the one everybody
    arranges to happen.

    At the default rate three segments round to nothing, which is why this asks
    for a rate where the charge is visible rather than asserting a decrease and
    passing for the wrong reason.
    """
    engine = build(sever_score_cost=50)
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))
    engine.snakes[0].score = 100

    feed(engine, 0, "shrink")
    snake = engine.snakes[0]

    charged = round(SHRINK_SEGMENTS * 50 / 100.0)

    assert snake.length == 8 - SHRINK_SEGMENTS
    assert snake.score == 100 - charged
    assert charged > 0, "otherwise this test proves nothing"


def test_a_shrink_poison_is_never_fatal():
    """A death with nobody to blame for it reads as the game breaking."""
    engine = build(min_length_before_death=2)
    place(engine, 0, [(10, 10), (9, 10)], (1, 0))

    feed(engine, 0, "shrink")
    snake = engine.snakes[0]

    assert snake.alive
    assert snake.length >= 2


def test_shrunk_segments_do_not_land_on_the_floor():
    """Unlike a cut. Re-eating your own poison would simply undo it."""
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))

    feed(engine, 0, "shrink")

    assert not engine.remains
    assert not engine.arena.food


def test_confusion_reverses_the_press_and_not_the_snake():
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))
    snake = engine.snakes[0]
    snake.effects.apply("confusion", 6)

    engine.set_heading(0, "up", seq=1, move=snake.move_count)

    assert snake.pending_heading == (0, 1), "up produced down"
    assert snake.heading == (1, 0), "and the snake has not turned yet"


def test_a_confused_press_forward_is_refused_as_the_reversal_it_becomes():
    """Which is the mechanic working rather than a bug: while confused, the

    key that used to mean straight on now means backwards, and backwards has
    always been refused.
    """
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))
    snake = engine.snakes[0]
    snake.effects.apply("confusion", 6)

    reply = engine.set_heading(0, "right", seq=1, move=snake.move_count)

    assert reply["ok"] is False
    assert reply["reason"] == "reversal"


def test_effects_do_not_survive_a_respawn():
    engine = build(respawn_delay=0)
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))
    engine.snakes[0].effects.apply("slow", 6)

    engine._spawn(engine.snakes[0], time.monotonic())

    assert not engine.snakes[0].effects.to_wire(), (
        "carrying a poison through a death punishes one mistake twice"
    )


# -- phase, which is a clause in a collision that already existed ------------


def test_phasing_passes_through_a_body_without_cutting_it():
    engine = build(severing=True)
    place(engine, 1, [(12, 8 + n) for n in range(6)], (0, 1))
    place(engine, 0, [(11, 10), (10, 10), (9, 10)], (1, 0))
    engine.snakes[0].effects.apply("phase", 6)

    engine._advance([engine.snakes[0]], time.monotonic())

    assert engine.snakes[0].alive
    assert tuple(engine.snakes[0].head) == (12, 10)
    assert engine.snakes[1].length == 6, "phasing is not an attack"
    assert engine.snakes[1].alive


def test_phasing_leaves_the_other_snake_where_it_was():
    """The bug this is here for: the passer took ownership of the cell, so when

    its tail left it cleared a cell the other snake was still standing in, and
    that snake was intangible there for the rest of the match.
    """
    engine = build(severing=True)
    place(engine, 1, [(12, 8 + n) for n in range(6)], (0, 1))
    place(engine, 0, [(11, 10), (10, 10), (9, 10)], (1, 0))
    engine.snakes[0].effects.apply("phase", 6)

    for _ in range(8):
        engine._advance([engine.snakes[0]], time.monotonic())

    victim = engine.snakes[1]

    assert tuple(engine.snakes[0].head)[0] > 12, "it really did pass through"
    assert all(
        engine.arena.at(cell[0], cell[1]) == SNAKE for cell in victim.body
    ), "a hole here means the victim is intangible"
    assert all(engine._owner_at(cell) == 1 for cell in victim.body)


def test_spawn_protection_no_longer_steals_a_cell_either():
    """The same bug, in the case that predates the pickups entirely."""
    engine = build(severing=True)
    place(engine, 1, [(12, 8 + n) for n in range(6)], (0, 1))
    place(engine, 0, [(11, 10), (10, 10), (9, 10)], (1, 0))
    engine.snakes[0].protected_until = time.monotonic() + 60

    for _ in range(8):
        engine._advance([engine.snakes[0]], time.monotonic())

    victim = engine.snakes[1]

    assert all(
        engine.arena.at(cell[0], cell[1]) == SNAKE for cell in victim.body
    )


def test_phasing_is_no_defence_against_being_cut():
    """One way only. A pickup that made you both untouchable would be a way to

    rescue whoever you ran into.
    """
    engine = build(severing=True)
    place(engine, 0, [(12, 8 + n) for n in range(6)], (0, 1))
    place(engine, 1, [(11, 10), (10, 10), (9, 10)], (1, 0))
    engine.snakes[0].effects.apply("phase", 6)

    engine._advance([engine.snakes[1]], time.monotonic())

    assert engine.snakes[0].length < 6, "the phasing snake was still cut"


# -- the magnet, the only one that acts on the arena -------------------------


def magnet_engine():
    engine = build()
    place(engine, 0, [(12, 12), (11, 12), (10, 12), (9, 12)], (1, 0))

    other = engine.snakes[1]
    for cell in list(other.body):
        engine._clear(cell, 1)
    other.body = deque([(2, 2)])
    other.alive = False

    engine.snakes[0].effects.apply("magnet", 6)
    return engine


def pull(engine, times=1):
    for _ in range(times):
        engine._next_pull = 0.0
        engine._pull_food(time.monotonic())


def test_food_in_range_drifts_toward_the_head():
    engine = magnet_engine()
    engine.arena.add_food(18, 12)

    pull(engine, 2)

    assert (16, 12) in engine.arena.food, "two cells closer"


def test_food_out_of_range_stays_where_it_is():
    engine = magnet_engine()
    far = (12 + MAGNET_RANGE + 3, 12)
    engine.arena.add_food(far[0], far[1])

    pull(engine, 4)

    assert far in engine.arena.food


def test_dropped_remains_are_not_dragged_across_the_arena():
    """They are the prize from a fight, and moving them would decide that fight

    after it had ended.
    """
    engine = magnet_engine()
    engine.arena.add_food(15, 12)
    engine.remains[(15, 12)] = (time.monotonic() + 60, 1)

    pull(engine, 3)

    assert (15, 12) in engine.arena.food


def test_nothing_is_pulled_into_a_body():
    engine = magnet_engine()
    engine.arena.add_food(14, 12)
    engine._occupy((13, 12), 0)

    pull(engine, 2)

    assert (14, 12) in engine.arena.food


def test_the_magnet_does_nothing_when_nobody_is_holding_one():
    engine = magnet_engine()
    engine.snakes[0].effects.clear()
    engine.arena.add_food(16, 12)

    pull(engine, 4)

    assert (16, 12) in engine.arena.food


# -- the wire ----------------------------------------------------------------


def test_the_floor_goes_out_with_the_keyframe():
    engine = build(poisons="high")
    engine.arena.add_item(5, 5, "slow")

    frame = engine.keyframe()

    assert [5, 5, "slow"] in frame["items"]


def test_a_snake_carries_what_is_acting_on_it():
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))
    engine.snakes[0].effects.apply("burst", 6)

    entry = next(
        snake for snake in engine.keyframe()["snakes"] if snake["id"] == 0
    )

    assert entry["fx"]["burst"] > 0


def test_a_delta_says_nothing_about_items_when_nothing_happened_to_them():
    engine = build(poisons="off", pickups="off")
    engine.keyframe()

    message = engine.delta()

    assert "item_add" not in message
    assert "item_del" not in message


def test_taking_an_item_is_sent_as_a_removal():
    engine = build()
    place(engine, 0, [(10 - n, 10) for n in range(8)], (1, 0))
    engine.keyframe()
    engine.delta()

    cell = feed(engine, 0, "slow")
    message = engine.delta()

    assert [cell[0], cell[1]] in message["item_del"]


# -- single player, which plays by the same rules ----------------------------


def solo(**overrides):
    settings = {
        "arena_width": 20,
        "arena_height": 20,
        "base_speed": 10,
        "starting_length": 8,
        "poisons": "off",
        "pickups": "off",
    }
    settings.update(overrides)

    engine = SoloEngine(RuleSet(**settings), seed=7)
    engine.start()
    for cell in list(engine.arena.food):
        engine.arena.remove_food(cell[0], cell[1])
    engine.arena.items.clear()
    return engine


def solo_feed(engine, kind):
    snake = engine.snake
    ahead = (snake.head[0] + snake.heading[0], snake.head[1] + snake.heading[1])
    engine.arena.add_item(ahead[0], ahead[1], kind)
    for _ in range(snake.move_interval):
        engine.step()
    return ahead


def test_a_poison_behaves_the_same_alone_as_it_does_in_a_room():
    engine = solo()

    before = engine.snake.move_interval
    solo_feed(engine, "slow")

    assert engine.snake.effects.active("slow")
    assert engine.snake.move_interval > before


def test_single_player_does_not_charge_a_score_it_never_showed():
    """The percentage a cut costs is a multiplayer rule, and the single player

    editor never shows it. Applying it here would take points away for a reason
    that is not on screen anywhere.
    """
    engine = solo()
    engine.snake.score = 50

    solo_feed(engine, "shrink")

    assert engine.snake.length == 8 - SHRINK_SEGMENTS
    assert engine.snake.score == 50


def test_phasing_lets_a_solo_snake_through_its_own_body():
    engine = solo()
    engine.snake.effects.apply("phase", 6)
    engine.snake.body = deque(
        [(10, 10), (10, 11), (11, 11), (11, 10), (12, 10), (13, 10)]
    )
    for cell in engine.snake.body:
        engine.arena.set(cell[0], cell[1], SNAKE)
    engine.snake.heading = engine.snake.pending_heading = (1, 0)

    for _ in range(engine.snake.move_interval):
        engine.step()

    assert engine.snake.alive, "it should have passed through, not died"


def test_the_solo_snapshot_carries_the_floor_and_the_effects():
    engine = solo()
    engine.arena.add_item(3, 3, "magnet")
    engine.snake.effects.apply("magnet", 6)

    frame = engine.snapshot()

    assert [3, 3, "magnet"] in frame["items"]
    assert frame["snakes"][0]["fx"]["magnet"] > 0


def test_every_kind_has_a_rule_that_switches_it_off():
    rules = RuleSet()

    for kind in POISONS + PICKUPS:
        off = RuleSet(**{
            key: False for key in (
                "slow_poison", "shrink_poison", "confusion_poison",
                "burst_pickup", "phase_pickup", "magnet_pickup",
            )
        })
        assert kind not in enabled_kinds(off, POISONS + PICKUPS)

    assert set(enabled_kinds(rules, POISONS + PICKUPS)) == set(POISONS + PICKUPS)
