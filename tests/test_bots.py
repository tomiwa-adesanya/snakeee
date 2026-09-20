"""Computer players.

A bot decides one thing: which heading it would like next. Everything else --
the reversal check, simultaneous resolution, collisions, scoring -- is the same
code a human press goes through. Most of what is checked here is that, rather
than whether the bots play well.

Whether they play well is not something a test can assert without freezing a
number that will move the first time anything is tuned. What is asserted is the
shape of it: that the tiers differ, that the difference is in what they
understand rather than how fast they react, and that a bot never gets a
privilege a player does not have.
"""

import time

from utils.game import bots as bots_module
from utils.game.match import PHASE_RUNNING, MatchEngine
from utils.game.rules import RuleSet


def build(**overrides):
    settings = {
        "arena_width": 30,
        "arena_height": 30,
        "base_speed": 10,
        "start_countdown": 0,
        "win_condition": "endless",
        "lives": 3,
        "respawn_delay": 1,
        "min_players": 1,
        "starting_length": 5,
        "bot_count": 3,
        "poisons": "off",
        "pickups": "off",
    }
    settings.update(overrides)

    engine = MatchEngine(RuleSet(**settings), [(0, "ann", "#ef6461")], seed=5)
    engine.start()
    engine.starts_at = time.monotonic() - 0.001
    engine.phase = PHASE_RUNNING
    return engine


def bot_snakes(engine):
    return [snake for snake in engine.snakes.values() if snake.is_bot]


# -- who is in the room ------------------------------------------------------


def test_bots_are_added_to_the_match():
    engine = build(bot_count=3)

    assert len(bot_snakes(engine)) == 3
    assert len(engine.bots) == 3


def test_a_bot_is_a_snake_like_any_other():
    engine = build(bot_count=1)
    bot = bot_snakes(engine)[0]

    assert bot.body, "it spawned"
    assert bot.lives_left == engine.rules.lives
    assert bot.move_interval == engine.snakes[0].move_interval, (
        "a tier changes what a bot understands, never how fast it moves"
    )


def test_no_bots_by_default():
    assert build(bot_count=0).bots == {}


def test_bots_never_push_a_room_past_its_cap():
    engine = build(bot_count=11, player_cap=4)

    assert len(engine.snakes) == 4, "one human and three bots"


def test_filling_the_slots_tops_the_room_up():
    engine = build(bot_count=0, bots_fill_slots=True, player_cap=6)

    assert len(bot_snakes(engine)) == 5


def test_asking_both_ways_takes_the_larger_rather_than_the_sum():
    """A host who asks for four bots and a full room means a full room."""
    engine = build(bot_count=4, bots_fill_slots=True, player_cap=8)

    assert len(bot_snakes(engine)) == 7


def test_a_bot_says_it_is_one():
    engine = build(bot_count=1)
    entries = {snake["name"]: snake["bot"] for snake in engine.keyframe()["snakes"]}

    assert entries["ann"] is False
    assert any(entries[name] for name in entries if name != "ann")


def test_bots_have_names_and_colours_of_their_own():
    engine = build(bot_count=4)
    names = {snake.username for snake in bot_snakes(engine)}
    colours = {snake.colour for snake in bot_snakes(engine)}

    assert len(names) == 4, "no two bots share a name"
    assert len(colours) == 4
    assert "ann" not in names


def test_more_bots_than_there_are_names():
    """The list runs out at twelve; the numbering has to keep going."""
    first = bots_module.name_for(0)
    wrapped = bots_module.name_for(len(bots_module.NAMES))

    assert wrapped != first
    assert wrapped == first + " 2", "the thirteenth is the first one again"


# -- a bot has no privileges -------------------------------------------------


def test_a_bot_never_turns_back_on_itself():
    """The same refusal a human press gets. A bot that could reverse would be

    playing a different game from the person it is playing against.
    """
    engine = build(bot_count=3)

    for _ in range(400):
        engine.step()
        for snake in bot_snakes(engine):
            if snake.length < 2 or not snake.alive:
                continue
            facing = snake.heading
            wanted = snake.pending_heading
            assert not (
                wanted[0] == -facing[0] and wanted[1] == -facing[1]
            ), "a bot queued a reversal"


def test_a_bot_can_die_like_anybody_else():
    engine = build(bot_count=5, arena_width=20, arena_height=20, lives=1,
                   severing=True)

    for _ in range(3000):
        engine.step()

    assert any(snake.deaths for snake in bot_snakes(engine)), (
        "five bots in a small arena and none of them ever came to any harm"
    )


def test_bots_move_on_the_same_clock_as_a_person():
    engine = build(bot_count=2, length_affects_speed=True)

    for _ in range(600):
        engine.step()

    for snake in bot_snakes(engine):
        if not snake.alive:
            continue
        assert snake.move_interval == engine.rules.move_interval_ticks(
            snake.length
        ), "a bot is subject to the length penalty like everybody else"


# -- the tiers differ, and in the right direction ----------------------------


def survivors(difficulty, ticks=2500, seed=5):
    engine = MatchEngine(
        RuleSet(
            arena_width=24, arena_height=24, base_speed=10, start_countdown=0,
            win_condition="endless", lives=0, respawn_delay=1, min_players=1,
            starting_length=5, bot_count=4, bot_difficulty=difficulty,
            poisons="normal", pickups="low", severing=True,
        ),
        [(0, "ann", "#ef6461")],
        seed=seed,
    )
    engine.start()
    engine.starts_at = time.monotonic() - 0.001
    engine.phase = PHASE_RUNNING
    engine.snakes[0].accumulator = -10 ** 9

    for _ in range(ticks):
        engine.step()

    return [snake for snake in bot_snakes(engine) if snake.alive]


def test_easy_bots_do_not_wipe_each_other_out_in_seconds():
    """The first version had no idea where other heads were, so four of them

    converged on the same food and met head on. All four were gone inside sixty
    ticks, which reads as the game being broken rather than as the bots being
    bad at it.
    """
    engine = MatchEngine(
        RuleSet(
            arena_width=30, arena_height=30, base_speed=10, start_countdown=0,
            win_condition="endless", lives=0, min_players=1, bot_count=4,
            bot_difficulty="easy", starting_length=5, severing=True,
        ),
        [(0, "ann", "#ef6461")],
        seed=5,
    )
    engine.start()
    engine.starts_at = time.monotonic() - 0.001
    engine.phase = PHASE_RUNNING
    engine.snakes[0].accumulator = -10 ** 9

    for _ in range(120):
        engine.step()

    assert any(snake.alive for snake in bot_snakes(engine))


def test_a_better_tier_survives_longer():
    """Loose on purpose. The exact numbers move whenever anything is tuned, and

    a test that pinned them would fail on every improvement. What has to hold is
    the direction: understanding more keeps you alive longer.
    """
    easy = sum(len(survivors("easy", seed=seed)) for seed in (1, 2, 3))
    medium = sum(len(survivors("medium", seed=seed)) for seed in (1, 2, 3))

    assert medium > easy


def test_an_unknown_difficulty_is_not_a_crash():
    bot = bots_module.Bot(1, "impossible")

    assert bot.difficulty == "medium"


# -- the arena is left alone -------------------------------------------------


def test_thinking_changes_nothing_but_a_heading():
    """The view handed to the bots is read-only by construction, and this is

    what says so out loud: a whole round of thinking, and nothing in the arena
    moved.
    """
    engine = build(bot_count=4, poisons="normal", pickups="normal")

    before_cells = bytes(engine.arena.cells)
    before_food = set(engine.arena.food)
    before_items = dict(engine.arena.items)
    before_bodies = {
        snake.id: list(snake.body) for snake in engine.snakes.values()
    }

    engine._think(list(engine.snakes.values()), time.monotonic())

    assert bytes(engine.arena.cells) == before_cells
    assert set(engine.arena.food) == before_food
    assert dict(engine.arena.items) == before_items
    assert {
        snake.id: list(snake.body) for snake in engine.snakes.values()
    } == before_bodies


def test_a_dead_bot_is_asked_for_nothing():
    engine = build(bot_count=1)
    bot = bot_snakes(engine)[0]
    bot.alive = False

    view = bots_module.MatchView(engine)

    assert engine.bots[bot.id].choose(view) is None


# -- the match is still a match ----------------------------------------------


def test_bots_alone_do_not_keep_a_match_running():
    """A room where every human is out and four bots are still circling is

    over. Nobody is playing it, and leaving it running means everybody watching
    a demonstration with no way to agree on another game.
    """
    engine = build(bot_count=4, lives=1)
    engine.snakes[0].eliminated = True

    engine._check_end(time.monotonic())

    assert engine.outcome is not None


def test_a_match_with_no_bots_ends_the_way_it_always_did():
    engine = build(bot_count=0)
    engine.snakes[0].eliminated = True

    engine._check_end(time.monotonic())

    assert engine.outcome is None, "one player alone is not a match ending"


# -- the owner grid, which bots broke ---------------------------------------


def test_a_player_id_above_a_byte_does_not_break_the_arena():
    """The owner grid is a bytearray and used to hold player id plus one. A room

    hands out a new id on every join and never reuses them, so its 255th player
    would have crashed every write to that grid. Bots met it immediately,
    because their ids start above every human's.
    """
    engine = MatchEngine(
        RuleSet(arena_width=24, arena_height=24, start_countdown=0,
                min_players=1),
        [(300, "late", "#ef6461"), (9001, "later", "#3ecf8e")],
        seed=1,
    )
    engine.start()

    assert all(snake.body for snake in engine.snakes.values())
    assert max(engine.owner) <= len(engine.snakes)


def test_the_grid_still_says_who_holds_a_cell():
    engine = build(bot_count=2)

    for snake in engine.snakes.values():
        for cell in snake.body:
            assert engine._owner_at(cell) == snake.id


# -- aggression --------------------------------------------------------------
#
# The setting decides whether bots cut anybody. Before it existed they never
# did: the scoring refused every occupied cell, so a bot would not enter a body
# even when severing meant the snake that moved survived and the one it hit was
# cut. "Hard hunts shorter snakes" was only ever a pull toward being near one.


def test_a_body_is_a_wall_when_severing_is_off():
    """With severing off, a head that meets a body kills the snake that moved.

    There is nothing to be willing about, and an appetite must not talk a bot
    into suicide.
    """
    engine = build(bot_count=1, severing=False, bot_aggression="relentless")
    view = bots_module.MatchView(engine)
    victim = engine.snakes[0]

    assert view.cut_value(victim.body[1], 1000) is None


def test_a_body_is_worth_entering_when_severing_is_on():
    engine = build(bot_count=1, severing=True, spawn_protection=0)
    for snake in engine.snakes.values():
        snake.protected_until = 0.0

    view = bots_module.MatchView(engine)
    victim = engine.snakes[0]

    assert view.cut_value(victim.body[1], 1000) > 0


def test_cutting_nearer_a_head_is_worth_more():
    engine = build(bot_count=1, severing=True, spawn_protection=0,
                   starting_length=8)
    for snake in engine.snakes.values():
        snake.protected_until = 0.0

    view = bots_module.MatchView(engine)
    victim = engine.snakes[0]

    near_head = view.cut_value(victim.body[1], 1000)
    near_tail = view.cut_value(victim.body[-1], 1000)

    assert near_head > near_tail


def test_a_protected_snake_is_not_a_target():
    engine = build(bot_count=1, severing=True)
    engine.snakes[0].protected_until = time.monotonic() + 60

    view = bots_module.MatchView(engine)

    assert view.cut_value(engine.snakes[0].body[1], 1000) is None


def test_an_unwanted_cut_is_still_a_wall():
    """The defect this is named for: an attack a bot did not want scored zero

    rather than being refused, so a bot with no appetite walked into bodies as
    readily as into empty ground. Measured, an aggression of "off" produced as
    many cuts as "relentless".
    """
    engine = build(bot_count=1, severing=True, bot_aggression="off",
                   spawn_protection=0)
    for snake in engine.snakes.values():
        snake.protected_until = 0.0

    bot = engine.bots[1000]
    view = bots_module.MatchView(engine)
    me = view.snake(1000)

    assert bot.appetite == 0.0
    assert bot._score(view, me, tuple(engine.snakes[0].body[1]), (1, 0)) is None


def test_aggression_off_gives_every_bot_no_appetite():
    engine = build(bot_count=4, bot_aggression="off")

    assert all(bot.appetite == 0.0 for bot in engine.bots.values())


def test_a_hungrier_setting_produces_hungrier_bots():
    """Bands rather than values, so a quiet room can still turn out one bot

    with a taste for it. What has to hold is the average.
    """
    def average(setting):
        engine = build(bot_count=11, player_cap=12, bot_aggression=setting)
        return sum(bot.appetite for bot in engine.bots.values()) / 11

    assert average("rare") < average("normal") < average("relentless")


def test_a_temperament_lasts_a_life_and_not_a_decision():
    """Rolling per decision makes a bot dither: it commits to an attack on one

    move and thinks better of it on the next. Drawn once per life it is a
    personality a player can read and plan against.
    """
    engine = build(bot_count=1, bot_aggression="normal")
    bot = engine.bots[1000]

    first = bot.appetite
    view = bots_module.MatchView(engine)
    for _ in range(20):
        bot.choose(view)

    assert bot.appetite == first


def test_a_respawn_draws_a_fresh_temperament():
    """So the snake that has been hunting you may come back content, and the

    one you learned to ignore may not be.
    """
    engine = build(bot_count=1, bot_aggression="normal", respawn_delay=0)
    bot = engine.bots[1000]
    snake = engine.snakes[1000]

    seen = set()
    for _ in range(30):
        engine._spawn(snake, time.monotonic())
        seen.add(round(bot.appetite, 6))

    assert len(seen) > 1


def test_bots_that_want_to_cut_actually_do():
    """The whole feature, end to end. Not a threshold on how many, because that

    moves with any tuning; what has to hold is that switching it on changes
    what happens.
    """
    def cuts(setting):
        made = 0
        for seed in (1, 2, 3, 4):
            engine = MatchEngine(
                RuleSet(
                    arena_width=26, arena_height=26, base_speed=10,
                    start_countdown=0, win_condition="endless", lives=0,
                    respawn_delay=1, min_players=1, starting_length=6,
                    bot_count=4, bot_difficulty="medium",
                    bot_aggression=setting, severing=True,
                    spawn_protection=1,
                ),
                [(0, "ann", "#ef6461")],
                seed=seed,
            )
            engine.bots[0] = bots_module.Bot(0, "medium", seed=seed,
                                             aggression="off")
            engine.start()
            engine.starts_at = time.monotonic() - 0.001
            engine.phase = PHASE_RUNNING

            names = {
                snake.username for snake in engine.snakes.values()
                if snake.is_bot
            }
            for _ in range(2500):
                engine.step()
                if not engine.snakes[0].alive:
                    break

            for entry in engine.events:
                event = entry[1] if isinstance(entry, tuple) else entry
                if event.get("k") == "sever" and event.get("a") in names:
                    made += 1
        return made

    assert cuts("relentless") > cuts("off")
