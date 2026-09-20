"""The shared match.

Two things here are worth more than the rest and are tested hardest.

The fold. A client's copy of the match is built from a keyframe plus a stream of
deltas, and if that reconstruction is ever wrong the game is wrong on one screen
and right on another, which is the worst kind of bug this project can have. The
fold is therefore checked against the engine's own state on every snapshot of a
long run rather than at the end of a short one.

Simultaneity. Two heads entering one cell is a collision and not a race, so the
order snakes happen to be iterated in must not decide who dies. The head-on
tests set the same position up twice with the roster reversed.
"""

import time
from collections import deque

import pytest

from utils.game.arena import FOOD
from utils.game.match import (
    INPUT_MOVE_TOLERANCE,
    KEYFRAME_EVERY,
    PHASE_COUNTDOWN,
    PHASE_OVER,
    PHASE_RUNNING,
    RENDER_DELAY_MAX,
    RENDER_DELAY_MIN,
    MatchEngine,
    MatchView,
    standings_from,
)
from utils.game.rules import RuleSet

ROSTER = [(0, "ann", "#ef6461"), (1, "bob", "#3ecf8e"), (2, "cid", "#58a6ff")]


def build(players=2, **overrides):
    values = {
        "arena_width": 24,
        "arena_height": 24,
        "start_countdown": 3,
        "base_speed": 10,
        "length_affects_speed": False,
        "lives": 3,
        "respawn_delay": 0,
        "spawn_protection": 0,
        "food_density": "sparse",
    }
    values.update(overrides)

    engine = MatchEngine(RuleSet(**values), ROSTER[:players], seed=11)
    engine.start()
    return engine


def kick_off(engine):
    """Skip the countdown without waiting for it in real time."""
    engine.starts_at = time.monotonic() - 0.001
    engine.step()
    return engine


def place(engine, snake_id, cells, heading):
    """Put a snake exactly where a test needs it, grid and all."""
    snake = engine.snakes[snake_id]
    snake.body = deque(tuple(cell) for cell in cells)
    snake.heading = heading
    snake.pending_heading = heading
    snake.alive = True
    snake.accumulator = snake.move_interval - 1
    snake.arena_id = engine.topology.arena_at(cells[0][0], cells[0][1])

    for cell in snake.body:
        engine._occupy(cell, snake.id)


def clear_grid(engine):
    engine.arena.reset()
    engine.owner = bytearray(engine.arena.width * engine.arena.height)
    for snake in engine.snakes.values():
        snake.body.clear()
        snake.move_interval = 999
        snake.accumulator = 0


# -- lifecycle --------------------------------------------------------------


def test_a_match_starts_in_a_countdown_and_then_runs():
    engine = build()
    assert engine.phase == PHASE_COUNTDOWN

    engine.step()
    assert engine.phase == PHASE_COUNTDOWN, "the countdown must not be skipped"

    kick_off(engine)
    assert engine.phase == PHASE_RUNNING


def test_every_snake_starts_somewhere_legal_and_apart():
    engine = build(players=3)

    heads = []
    for snake in engine.snakes.values():
        assert snake.alive
        assert len(snake.body) == engine.rules.starting_length
        heads.append(snake.head)

    assert len(set(heads)) == len(heads)

    occupied = [cell for snake in engine.snakes.values() for cell in snake.body]
    assert len(set(occupied)) == len(occupied), "two snakes share a cell"


def test_the_countdown_leaves_time_to_look_at_the_arena():
    engine = build(start_countdown=5)
    frame = engine.keyframe()

    assert frame["phase"] == PHASE_COUNTDOWN
    assert 3500 < frame["countdown_ms"] <= 5000


# -- input ------------------------------------------------------------------


def test_a_reversal_is_refused_and_not_remembered():
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))

    refused = engine.set_heading(0, "left", seq=1, move=0)
    assert refused["ok"] is False
    assert refused["reason"] == "reversal"
    assert engine.snakes[0].pending_heading == (1, 0)

    accepted = engine.set_heading(0, "up", seq=2, move=0)
    assert accepted["ok"] is True
    assert engine.snakes[0].pending_heading == (0, -1)


def test_an_input_tied_to_a_position_the_snake_has_left_is_refused():
    """A stale turn steers somewhere the player never chose.

    Applied where the snake is now rather than where it was, a turn from four
    moves ago can go into a wall that was not there when the key went down.
    Being killed by your own stale input is worse than being ignored.
    """
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    snake = engine.snakes[0]
    snake.move_count = 20

    inside = engine.set_heading(0, "up", seq=1, move=20 - INPUT_MOVE_TOLERANCE)
    assert inside["ok"] is True

    outside = engine.set_heading(
        0, "down", seq=2, move=20 - INPUT_MOVE_TOLERANCE - 1
    )
    assert outside["ok"] is False
    assert outside["reason"] == "too_late"
    assert outside["behind"] == INPUT_MOVE_TOLERANCE + 1


def test_a_refusal_says_which_move_to_aim_at_next():
    """So the press can be sent again rather than thrown away.

    Without this the client would resend against the same stale move and be
    refused for the same reason, which is a retry that cannot ever succeed.
    """
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    snake = engine.snakes[0]
    snake.move_count = 20

    refused = engine.set_heading(0, "up", seq=1, move=0)
    assert refused["ok"] is False
    assert refused["move"] == 20

    accepted = engine.set_heading(0, "up", seq=2, move=refused["move"])
    assert accepted["ok"] is True
    assert snake.pending_heading == (0, -1)


def test_how_late_input_arrives_is_counted():
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    snake = engine.snakes[0]
    snake.move_count = 20

    engine.set_heading(0, "up", seq=1, move=20)
    assert snake.late_inputs == 0, "on time"

    engine.set_heading(0, "down", seq=2, move=20 - INPUT_MOVE_TOLERANCE - 5)
    assert snake.late_inputs == 1
    assert snake.worst_input_lag == INPUT_MOVE_TOLERANCE + 5

    assert engine.keyframe()["snakes"][0]["late"] == INPUT_MOVE_TOLERANCE + 5


def test_a_refusal_still_acknowledges_the_sequence_number():
    """A client stops predicting when its input is acknowledged.

    A refusal that did not acknowledge would leave the client drawing a turn
    that is never coming, until the prediction timed out.
    """
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))

    answer = engine.set_heading(0, "left", seq=9, move=0)
    assert answer["ok"] is False
    assert answer["ack"] == 9


def test_an_unknown_player_cannot_steer_anything():
    engine = kick_off(build())
    assert engine.set_heading(99, "up", seq=1, move=0)["ok"] is False


def test_the_answer_carries_the_cell_the_head_is_moving_into():
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))

    answer = engine.set_heading(0, "up", seq=1, move=0)
    assert answer["next"] == [10, 9]


# -- collisions -------------------------------------------------------------


def test_a_wall_ends_a_life():
    engine = kick_off(build(edge_behaviour="walls"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[0].death_cause == "wall"


def test_a_wrapping_edge_does_not():
    engine = kick_off(build(edge_behaviour="wrap"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()

    assert engine.snakes[0].alive is True
    assert engine.snakes[0].head == (0, 10)


def test_running_into_another_snake_kills_you_when_severing_is_off():
    engine = kick_off(build(severing=False))
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].accumulator = 0
    engine.snakes[1].move_interval = 999

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[0].death_cause == "snake"
    assert engine.snakes[1].kills == 1


def test_following_your_own_tail_is_legal():
    engine = kick_off(build())
    clear_grid(engine)
    place(engine, 0, [(10, 10), (11, 10), (11, 11), (10, 11)], (0, 1))

    engine.step()

    assert engine.snakes[0].alive is True, "the tail cell was vacated this move"


@pytest.mark.parametrize("reversed_roster", [False, True])
def test_the_longer_snake_survives_a_head_on(reversed_roster):
    engine = kick_off(build(head_on="longer_wins"))
    clear_grid(engine)

    longer = [(10, 10), (9, 10), (8, 10), (7, 10)]
    shorter = [(12, 10), (13, 10), (14, 10)]

    first, second = (1, 0) if reversed_roster else (0, 1)
    place(engine, first, longer, (1, 0))
    place(engine, second, shorter, (-1, 0))

    engine.step()

    assert engine.snakes[first].alive is True
    assert engine.snakes[second].alive is False
    assert engine.snakes[second].death_cause == "head_on"
    assert engine.snakes[first].kills == 1


def test_equal_lengths_both_die_in_a_head_on():
    engine = kick_off(build(head_on="longer_wins"))
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(12, 10), (13, 10), (14, 10)], (-1, 0))

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[1].alive is False
    assert engine.snakes[0].kills == 0
    assert engine.snakes[1].kills == 0


def test_two_snakes_swapping_cells_collide():
    """Adjacent heads never share a target, so a swap is checked separately."""
    engine = kick_off(build(head_on="both_die"))
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 10), (12, 10), (13, 10)], (-1, 0))

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[1].alive is False


def test_spawn_protection_cancels_a_death_both_ways():
    engine = kick_off(build(spawn_protection=5, severing=False))
    clear_grid(engine)
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].accumulator = 0
    engine.snakes[1].move_interval = 999
    engine.snakes[1].protected_until = time.monotonic() + 5

    engine.step()

    assert engine.snakes[0].alive is True
    assert engine.snakes[1].kills == 0


# -- lives and the end of a match -------------------------------------------


def test_a_death_costs_a_life_and_schedules_a_return():
    engine = kick_off(build(lives=3, respawn_delay=2, edge_behaviour="walls"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()

    snake = engine.snakes[0]
    assert snake.lives_left == 2
    assert snake.eliminated is False
    assert snake.respawn_at is not None


def test_the_last_life_eliminates_rather_than_respawning():
    engine = kick_off(build(lives=1, edge_behaviour="walls"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(2, 2), (2, 3), (2, 4)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert engine.snakes[0].eliminated is True
    assert engine.phase == PHASE_OVER
    assert engine.outcome["reason"] == "last_standing"
    assert engine.outcome["winner"] == 1


def test_unlimited_lives_never_eliminates():
    engine = kick_off(build(lives=0, win_condition="timed", edge_behaviour="walls"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()

    assert engine.snakes[0].eliminated is False
    assert engine.snakes[0].respawn_at is not None


def test_a_score_target_ends_the_match():
    engine = kick_off(
        build(win_condition="first_to_score", score_target=50, lives=3)
    )
    engine.snakes[0].score = 50
    engine.step()

    assert engine.phase == PHASE_OVER
    assert engine.outcome["reason"] == "score_target"
    assert engine.outcome["winner"] == 0


def test_a_kill_target_ends_the_match():
    engine = kick_off(build(win_condition="first_to_kills", kill_target=3))
    engine.snakes[1].kills = 3
    engine.step()

    assert engine.phase == PHASE_OVER
    assert engine.outcome["winner"] == 1


def test_an_endless_match_does_not_end_on_its_own():
    engine = kick_off(build(win_condition="endless", lives=1))
    for _ in range(200):
        engine.step()

    assert engine.phase == PHASE_RUNNING


def test_the_standings_are_ordered_by_score_then_kills():
    engine = kick_off(build(players=3, win_condition="endless"))
    engine.snakes[0].score = 5
    engine.snakes[1].score = 9
    engine.snakes[2].score = 9
    engine.snakes[2].kills = 2

    order = [entry["id"] for entry in engine.standings()]
    assert order == [2, 1, 0]


# -- the wire ---------------------------------------------------------------


def test_a_client_cannot_apply_a_delta_it_has_no_state_for():
    engine = kick_off(build())
    view = MatchView()

    assert view.apply(engine.delta()) is False
    assert view.state() is None
    assert view.dropped == 1


def test_a_delta_against_the_wrong_base_is_discarded():
    engine = kick_off(build())
    view = MatchView()
    view.apply(engine.keyframe())

    stray = engine.delta()
    stray["base"] = stray["base"] + 5

    assert view.apply(stray) is False
    assert view.dropped == 1


def test_the_fold_reproduces_the_engine_exactly():
    """The whole reason MatchView exists, checked every snapshot of a long run.

    Bodies, food and the leaderboard are compared against the engine itself, so
    a delta that drops a segment or leaks a food item fails here rather than
    appearing as one player seeing a different arena from another.
    """
    engine = kick_off(build(players=3, win_condition="endless", lives=0))

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    for tick in range(900):
        engine.step()

        if tick % 3:
            continue

        assert view.apply(engine.delta()) is True

        folded = view.latest()
        truth = {snake["id"]: snake for snake in engine.keyframe()["snakes"]}

        for entry in folded["snakes"]:
            expected = [[x, y] for x, y in engine.snakes[entry["id"]].body]
            assert entry["b"] == expected, f"body diverged at tick {tick}"

            # Every field, not only the body. A delta names only what changed,
            # so a field that stops being sent and is not carried forward would
            # silently disappear on one side and not the other, which is exactly
            # the failure that is invisible until somebody's name vanishes.
            for field, value in truth[entry["id"]].items():
                # Fields derived from the clock are excluded, and only those.
                # Progress, a respawn countdown and whether spawn protection is
                # still running are all answers to "what time is it now", so
                # comparing the one the client was sent against one computed a
                # moment later compares two different instants and proves
                # nothing. Everything else must match exactly.
                if field in ("b", "p", "respawn_in", "protected"):
                    continue
                assert entry.get(field) == value, (
                    f"{field} diverged at tick {tick}"
                )

        assert sorted(tuple(cell) for cell in folded["food"]) == sorted(
            engine.arena.food
        )
        assert folded["rank"] == [entry["id"] for entry in engine.standings()]
        assert folded["standings"] == engine.state()["standings"]


def test_a_keyframe_arrives_regularly_without_being_asked_for():
    engine = kick_off(build())

    kinds = [engine.next_message()["type"] for _ in range(KEYFRAME_EVERY * 2)]

    assert kinds[0] == "keyframe"
    assert kinds[KEYFRAME_EVERY] == "keyframe"
    assert kinds.count("keyframe") == 2


def test_a_keyframe_alone_is_enough_to_draw_the_match():
    engine = kick_off(build(players=3))
    view = MatchView()

    assert view.apply(engine.keyframe()) is True

    folded = view.latest()
    assert folded["arena"]["w"] == engine.arena.width
    assert len(folded["snakes"]) == 3
    assert folded["rules"]["base_speed"] == engine.rules.base_speed


def test_a_folded_state_says_how_old_it_is():
    engine = kick_off(build())
    view = MatchView()
    view.apply(engine.keyframe())

    time.sleep(0.05)
    assert view.state()["age_ms"] >= 40


def test_a_player_who_leaves_stops_being_drawn():
    engine = kick_off(build(players=3, win_condition="endless", lives=0))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.remove_player(2)
    engine.step()

    assert view.apply(engine.delta()) is True
    # latest(), not state(): this is about what the fold believes, not about
    # what should be on screen at this instant.
    assert [entry["id"] for entry in view.latest()["snakes"]] == [0, 1]


def test_a_player_who_joins_in_progress_puts_everyone_on_a_keyframe():
    """One stream, one base.

    Handing the new arrival a private keyframe would leave them the only client
    whose base does not match the stream's, and they would discard every delta
    until the next scheduled keyframe. Promoting the next broadcast for
    everybody costs one keyframe and removes the special case.
    """
    engine = kick_off(build(players=2, win_condition="endless", lives=0))

    view = MatchView()
    view.apply(engine.next_message())
    engine.next_message()

    assert engine.add_player(2, "cid", "#58a6ff") is True
    engine.step()

    message = engine.next_message()
    assert message["type"] == "keyframe"
    assert view.apply(message) is True

    joined = [
        entry for entry in view.latest()["snakes"] if entry["id"] == 2
    ][0]
    assert joined["b"], "a snake that joined mid-match arrived with no body"
    assert joined["b"] == [[x, y] for x, y in engine.snakes[2].body]


def test_a_late_arrival_can_ask_for_a_keyframe_directly():
    engine = kick_off(build())

    engine.next_message()
    assert engine.next_message()["type"] == "snapshot"

    engine.request_keyframe()
    assert engine.next_message()["type"] == "keyframe"


def test_the_client_state_and_the_host_state_are_the_same_shape():
    """One renderer draws both sides, so both sides must look identical to it."""
    engine = kick_off(build(players=2))
    view = MatchView()
    view.apply(engine.keyframe())

    host_keys = set(engine.state())
    client_keys = set(view.state())

    assert host_keys - client_keys == set()

    host_snake = engine.state()["snakes"][0]
    client_snake = view.state()["snakes"][0]
    assert set(host_snake) == set(client_snake)


# -- severing ---------------------------------------------------------------


def test_running_into_a_snake_cuts_it_there():
    """The rule that makes a fight worth having.

    Without severing, touching anybody is death, and the winning strategy is to
    avoid everybody. With it, contact costs the snake that was hit and rewards
    nobody directly, so the arena is worth entering.
    """
    engine = kick_off(build(severing=True, min_length_before_death=2))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    attacker = engine.snakes[0]
    victim = engine.snakes[1]

    assert attacker.alive is True, "the snake that made the cut must survive"
    assert attacker.head == (11, 10), "and must take the cell it cut"
    assert attacker.length == 3, "and gains nothing from the cut itself"

    assert victim.alive is True
    assert list(victim.body) == [(11, 7), (11, 8), (11, 9)]


def test_what_is_cut_off_is_left_on_the_floor():
    engine = kick_off(build(severing=True, remains_yield=100))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert (11, 11) in engine.remains
    assert (11, 11) in engine.arena.food

    # The cut cell itself is where the attacker now stands, so nothing is left
    # there. Dropping onto it is what used to produce a cell the food set called
    # food and the grid called something else.
    assert (11, 10) not in engine.remains


def test_remains_decay_rather_than_lasting_the_match():
    engine = kick_off(build(severing=True, remains_lifetime=3))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()
    assert engine.remains

    for cell, (_expires, owner) in list(engine.remains.items()):
        engine.remains[cell] = (time.monotonic() - 1, owner)

    engine.step()

    assert engine.remains == {}
    assert (11, 11) not in engine.arena.food


def test_a_yield_below_a_hundred_leaves_less_than_was_taken():
    engine = kick_off(build(severing=True, remains_yield=50))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 6), (11, 7), (11, 8), (11, 9), (11, 10), (11, 11)],
          (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert len(engine.remains) < 2, "half of two segments is one, at most"


def test_a_cut_costs_a_share_of_the_score_rather_than_all_of_it():
    """A big cut must not undo a big lead.

    Charging the full length made cutting the strongest move in the game and a
    score race unwinnable: a snake of a hundred cut near the head went back to
    almost nothing, and cutting is easy. Charging nothing is the other cliff,
    because snakes get faster as they shorten.
    """
    engine = kick_off(build(severing=True, sever_score_cost=10))
    clear_grid(engine)

    long_body = [(11, y) for y in range(1, 21)]
    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, long_body, (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0
    engine.snakes[1].score = 100

    engine.step()

    victim = engine.snakes[1]
    assert victim.length == 9, "eleven segments came off"
    assert victim.score == 99, "and one point with them, not eleven"
    assert engine.snakes[0].score == 0, "the attacker is given none of it"


def test_the_score_cost_of_a_cut_can_be_turned_off_entirely():
    engine = kick_off(build(severing=True, sever_score_cost=0))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0
    engine.snakes[1].score = 10

    engine.step()

    assert engine.snakes[1].score == 10
    assert engine.snakes[1].length == 3, "the length still went"


def test_being_cut_too_short_costs_a_life_and_credits_the_kill():
    engine = kick_off(build(severing=True, min_length_before_death=3, lives=3))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    victim = engine.snakes[1]
    assert victim.alive is False
    assert victim.death_cause == "severed"
    assert victim.lives_left == 2
    assert engine.snakes[0].kills == 1


def test_hitting_your_own_body_severs_your_own_tail():
    engine = kick_off(build(severing=True, self_collision="sever"))
    clear_grid(engine)

    # Curled so that moving right runs the head into its own body.
    place(engine, 0, [
        (10, 10), (10, 11), (11, 11), (11, 10), (11, 9), (10, 9)
    ], (0, -1))
    engine.snakes[0].pending_heading = (1, 0)

    engine.step()

    snake = engine.snakes[0]
    assert snake.alive is True
    assert snake.length < 6, "the tail behind the contact point came off"
    assert snake.kills == 0, "cutting yourself is not a kill"


def test_hitting_your_own_body_can_still_be_fatal():
    engine = kick_off(build(severing=True, self_collision="kill"))
    clear_grid(engine)

    place(engine, 0, [
        (10, 10), (10, 11), (11, 11), (11, 10), (11, 9), (10, 9)
    ], (0, -1))
    engine.snakes[0].pending_heading = (1, 0)

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[0].death_cause == "self"


def test_severing_off_restores_plain_lethal_contact():
    engine = kick_off(build(severing=False))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[1].length == 5, "and the victim keeps every segment"


def test_a_cut_snake_is_sent_whole_rather_than_as_a_delta():
    """A cut is not heads gained and tail lost, so it cannot be encoded as one.

    This is the check that stops a severed snake looking right on the host and
    wrong on every other screen.
    """
    engine = kick_off(build(severing=True, win_condition="endless", lives=0))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.step()

    assert view.apply(engine.delta()) is True

    for entry in view.latest()["snakes"]:
        expected = [[x, y] for x, y in engine.snakes[entry["id"]].body]
        assert entry["b"] == expected


def test_two_snakes_cutting_one_victim_take_the_deeper_cut():
    engine = kick_off(build(players=3, severing=True, min_length_before_death=1))
    clear_grid(engine)

    place(engine, 0, [(10, 9), (9, 9), (8, 9)], (1, 0))
    place(engine, 1, [(10, 12), (9, 12), (8, 12)], (1, 0))
    place(engine, 2, [
        (11, 6), (11, 7), (11, 8), (11, 9), (11, 10), (11, 11), (11, 12)
    ], (0, -1))
    engine.snakes[2].move_interval = 999
    engine.snakes[2].accumulator = 0

    engine.step()

    # Cut at (11, 9), which is index 3, rather than at (11, 12), which is 6.
    assert list(engine.snakes[2].body) == [(11, 6), (11, 7), (11, 8)]


def food_and_grid_agree(engine):
    """Every cell the food set claims must be food on the grid, and the reverse.

    They disagreeing is not a cosmetic problem. A cell in the food set that the
    grid does not call food is drawn as food, cannot be eaten by anybody, and
    stays there until it expires, which is exactly what a dropped segment used
    to do when it landed under the snake that had just made the cut.
    """
    claimed = {
        (x, y)
        for y in range(engine.arena.height)
        for x in range(engine.arena.width)
        if engine.arena.at(x, y) == FOOD
    }
    return claimed == set(engine.arena.food)


def test_a_dropped_segment_is_never_left_where_it_cannot_be_eaten():
    engine = kick_off(build(severing=True))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert food_and_grid_agree(engine)

    for cell in engine.remains:
        assert engine.arena.at(cell[0], cell[1]) == FOOD


def test_a_dropped_segment_can_actually_be_eaten():
    engine = kick_off(build(severing=True))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    attacker = engine.snakes[0]
    assert (11, 11) in engine.remains

    # Turn down into the remain that was just dropped.
    engine.set_heading(0, "down", seq=1, move=attacker.move_count)
    attacker.accumulator = attacker.move_interval - 1
    length = attacker.length
    score = attacker.score

    engine.step()

    assert attacker.head == (11, 11)
    assert attacker.length == length + 1, "eating a remain must feed you"
    assert attacker.score == score + 1
    assert (11, 11) not in engine.remains
    assert (11, 11) not in engine.arena.food
    assert food_and_grid_agree(engine)


# -- a match nobody is left to play -----------------------------------------


def test_one_player_left_wins_whatever_the_match_was_being_played_for():
    """Reported from a real game.

    Two players, a score target, and both of them died out. The score nobody was
    left to reach had not been reached, so the match kept running and both
    players sat watching an empty arena with no way to agree on another game.
    """
    engine = kick_off(
        build(win_condition="first_to_score", score_target=500, lives=1,
              edge_behaviour="walls")
    )
    clear_grid(engine)

    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(2, 2), (2, 3), (2, 4)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert engine.phase == PHASE_OVER
    assert engine.outcome["reason"] == "last_standing"
    assert engine.outcome["winner"] == 1, "the one still playing wins"


def test_a_match_nobody_is_left_to_play_ends_without_a_winner():
    engine = kick_off(
        build(win_condition="endless", lives=1, edge_behaviour="walls")
    )
    clear_grid(engine)

    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(23, 14), (22, 14), (21, 14)], (1, 0))

    engine.step()

    assert engine.phase == PHASE_OVER
    assert engine.outcome["reason"] == "everyone_out"
    assert engine.outcome["winner"] is None


def test_a_solo_room_is_not_over_the_moment_its_one_player_dies():
    """Somebody trying the arena on their own is not a match with a winner."""
    engine = kick_off(
        build(players=1, win_condition="endless", lives=3,
              edge_behaviour="walls")
    )
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()

    assert engine.phase == PHASE_RUNNING
    assert engine.snakes[0].alive is False


def test_a_client_reports_how_far_a_snake_jumped():
    """The measurement that turns "it lags sometimes" into a number.

    A stalled link does not lose messages, it delivers them together, and when
    it does a snake gains several moves at once. That count is how many cells it
    appears to leap, which is the thing somebody watching can actually describe.
    """
    engine = kick_off(build(win_condition="endless", lives=0))

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    # A quiet link: one move per message.
    for _ in range(20):
        engine.step()
        view.apply(engine.delta())

    assert view.state()["jump"] <= 1

    # A stall, then everything at once. The engine runs on without anybody
    # collecting, and the single message that follows carries the lot.
    for _ in range(60):
        engine.step()

    assert view.apply(engine.delta()) is True
    assert view.state()["jump"] > 1


# -- the event feed ----------------------------------------------------------


def kinds_of(engine):
    return [event["k"] for event in engine.keyframe()["events"]]


def test_a_cut_is_announced_with_who_did_it_and_how_much():
    """Severing is otherwise invisible.

    You are suddenly half as long and nothing says who did it, or whether you
    did it to yourself, which are very different pieces of information.
    """
    engine = kick_off(build(severing=True))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    events = engine.keyframe()["events"]
    cut = [event for event in events if event["k"] == "sever"]

    assert len(cut) == 1
    assert cut[0]["a"] == "ann"
    assert cut[0]["t"] == "bob"
    assert cut[0]["n"] == 2


def test_cutting_yourself_names_nobody_else():
    engine = kick_off(build(severing=True, self_collision="sever"))
    clear_grid(engine)

    place(engine, 0, [
        (10, 10), (10, 11), (11, 11), (11, 10), (11, 9), (10, 9)
    ], (0, -1))
    engine.snakes[0].pending_heading = (1, 0)

    engine.step()

    cut = [e for e in engine.keyframe()["events"] if e["k"] == "sever"]
    assert len(cut) == 1
    assert "a" not in cut[0], "there is no attacker to name"
    assert cut[0]["t"] == "ann"


def test_a_death_is_announced_with_its_cause():
    engine = kick_off(build(edge_behaviour="walls", lives=3))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()

    death = [e for e in engine.keyframe()["events"] if e["k"] == "death"]
    assert len(death) == 1
    assert death[0]["t"] == "ann"
    assert death[0]["c"] == "wall"
    assert "a" not in death[0], "a wall is not a killer"


def test_running_out_of_lives_is_announced():
    engine = kick_off(build(edge_behaviour="walls", lives=1))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(2, 2), (2, 3), (2, 4)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert "out" in kinds_of(engine)


def test_events_age_out_rather_than_piling_up():
    engine = kick_off(build(edge_behaviour="walls", lives=3))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.step()
    assert engine.keyframe()["events"]

    engine.events = deque(
        (stamp - 60, event) for stamp, event in engine.events
    )

    assert engine.keyframe()["events"] == []


def test_a_client_is_told_the_same_events_as_the_host():
    engine = kick_off(build(severing=True, win_condition="endless", lives=0))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.step()

    assert view.apply(engine.delta()) is True
    assert view.state()["events"] == engine.keyframe()["events"]


def test_a_delta_carries_only_events_that_are_new():
    """An event lasts five seconds; a delta goes out twenty times a second.

    Repeating the whole window every time cost a sixth of every snapshot on a
    busy match, for the same handful of lines sent a hundred times over.
    """
    engine = kick_off(build(edge_behaviour="walls", lives=3))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))

    engine.delta()
    engine.step()

    assert len(engine.delta()["events"]) == 1, "the death, once"
    assert engine.delta()["events"] == [], "and not again"
    assert engine.delta()["events"] == []

    # A keyframe still carries the lot, which is what somebody arriving needs.
    assert len(engine.keyframe()["events"]) == 1


def test_a_client_keeps_showing_an_event_the_deltas_have_stopped_sending():
    engine = kick_off(build(edge_behaviour="walls", lives=3,
                            win_condition="endless"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(2, 2), (2, 3), (2, 4)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.step()
    view.apply(engine.delta())
    assert len(view.state()["events"]) == 1

    # Several more deltas, none of them mentioning it again.
    for _ in range(5):
        engine.step()
        assert view.apply(engine.delta()) is True

    assert len(view.state()["events"]) == 1, "it must not disappear on arrival"


def test_a_client_expires_events_on_its_own_clock():
    engine = kick_off(build(edge_behaviour="walls", lives=3,
                            win_condition="endless"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(2, 2), (2, 3), (2, 4)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.step()
    view.apply(engine.delta())
    assert view.state()["events"]

    view.events = deque(
        (stamp - 60, event) for stamp, event in view.events
    )

    assert view.state()["events"] == []


def test_a_keyframe_resets_the_feed_rather_than_doubling_it():
    engine = kick_off(build(edge_behaviour="walls", lives=3,
                            win_condition="endless"))
    clear_grid(engine)
    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    place(engine, 1, [(2, 2), (2, 3), (2, 4)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.step()
    view.apply(engine.delta())

    view.apply(engine.keyframe())

    ids = [event["id"] for event in view.state()["events"]]
    assert ids == sorted(set(ids)), "the same event arrived twice"


# -- who is actually winning --------------------------------------------------


def test_outlasting_beats_scoring_when_outlasting_is_the_point():
    """Reported from a real game.

    A ate fifty and B ate ten, but B cut A down until A was out of lives, so B
    won the match. The board still had A first, because it ranked on points
    whatever the match was being played for. A player who is out is not ahead of
    a player who is still alive.
    """
    engine = kick_off(build(win_condition="last_standing", lives=3))

    ahead_on_points = engine.snakes[0]
    ahead_on_points.score = 50
    ahead_on_points.eliminated = True
    ahead_on_points.lives_left = 0

    still_alive = engine.snakes[1]
    still_alive.score = 10
    still_alive.kills = 3
    still_alive.lives_left = 2

    assert [entry["id"] for entry in engine.standings()] == [1, 0]


def test_lives_separate_two_players_who_are_both_still_in():
    engine = kick_off(build(players=3, win_condition="last_standing", lives=3))

    engine.snakes[0].score = 90
    engine.snakes[0].lives_left = 1
    engine.snakes[1].score = 5
    engine.snakes[1].lives_left = 3
    engine.snakes[2].score = 5
    engine.snakes[2].lives_left = 1

    order = [entry["id"] for entry in engine.standings()]
    assert order[0] == 1, "most lives left leads"
    assert order[1] == 0, "then points separate the two on one life"


def test_kills_lead_the_board_when_kills_win_the_match():
    engine = kick_off(build(win_condition="first_to_kills", kill_target=5))

    engine.snakes[0].score = 80
    engine.snakes[0].kills = 1
    engine.snakes[1].score = 4
    engine.snakes[1].kills = 4

    assert [entry["id"] for entry in engine.standings()] == [1, 0]


def test_points_still_rank_a_match_that_is_won_on_points():
    engine = kick_off(build(win_condition="first_to_score", score_target=100))

    engine.snakes[0].score = 80
    engine.snakes[1].score = 4
    engine.snakes[1].kills = 9

    assert [entry["id"] for entry in engine.standings()] == [0, 1]


def test_somebody_leaving_does_not_end_the_match_for_whoever_is_left():
    """Being out of lives ends a match. Walking away does not.

    Measured against the starting roster this ended the moment one of two
    players quit, which is a disconnection deciding a result. It also broke
    joining in progress, since the match was gone before they could come back.
    """
    engine = kick_off(build(win_condition="endless", lives=3))

    assert engine.remove_player(1) is True
    engine.step()

    assert engine.phase == PHASE_RUNNING
    assert engine.outcome is None

    # And they can come back to the match that is still running.
    assert engine.add_player(1, "bob", "#3ecf8e") is True
    engine.step()
    assert engine.phase == PHASE_RUNNING


# -- smoothness that does not depend on the scheduler ------------------------


def test_progress_is_measured_in_time_not_in_ticks():
    """The property that makes this smooth on every platform.

    Reported progress used to be ticks accumulated over ticks needed. That
    fraction only changes when a tick happens, so it is quantised to whenever
    the operating system chose to wake the loop up. Windows rounds a sleep up to
    15.6 milliseconds by default, so a loop asking for 16.7 gets two short waits
    and then a catch-up step with no wait at all: the ticks arrive in bursts,
    and every snake on every screen inherits the burst.

    Raising the timer resolution fixes that on Windows and does nothing
    anywhere else. Measuring in time fixes it everywhere, which is why the fix
    is here and the Windows call is only a second line of defence.
    """
    engine = kick_off(build(base_speed=5, win_condition="endless", lives=0))

    before = engine.keyframe()["snakes"][0]["p"]

    # No ticks at all, only time. The scheduler has done nothing.
    time.sleep(0.05)
    assert engine.keyframe()["snakes"][0]["p"] > before

    # And the reverse: ticks with no time between them are not progress.
    settled = engine.keyframe()["snakes"][0]["p"]
    for _ in range(3):
        engine.step()

    assert engine.keyframe()["snakes"][0]["p"] == pytest.approx(
        settled, abs=0.02
    )


def test_progress_never_draws_a_head_into_a_cell_it_has_not_reached():
    """A move that runs late must not let the drawing overtake the simulation."""
    engine = kick_off(build(base_speed=10, win_condition="endless", lives=0))

    # A move at speed 10 takes about 50 ms. Wait several of them without
    # stepping, which is what a badly stalled host looks like.
    time.sleep(0.3)

    for snake in engine.keyframe()["snakes"]:
        assert snake["p"] < 1.0


# -- drawing between two states rather than past the newest one --------------


def feed(view, engine, gap, count):
    """Deliver messages at a chosen spacing, as a link would."""
    for _ in range(count):
        for _ in range(3):
            engine.step()
        view.apply(engine.delta())
        time.sleep(gap)


def test_a_client_draws_the_recent_past_not_a_guess_at_the_present():
    """The change this whole approach exists for.

    Extrapolating means guessing where a snake will be and correcting when the
    guess is wrong, and every correction is a snap. Interpolating between two
    states that actually arrived cannot be wrong, so there is nothing to
    correct.
    """
    engine = kick_off(build(win_condition="endless", lives=0, base_speed=5))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    feed(view, engine, 0.02, 8)

    drawn = view.state()

    assert drawn["behind_ms"] > 0, "it is drawing a moment that has passed"
    assert drawn["delay_ms"] >= RENDER_DELAY_MIN * 1000


def test_the_delay_follows_the_link_rather_than_being_fixed():
    """A delay shorter than the gap between messages has nothing to draw."""
    steady = MatchView()
    engine = kick_off(build(win_condition="endless", lives=0))
    steady.apply(engine.keyframe())
    engine.delta()
    feed(steady, engine, 0.01, 10)
    tight = steady.render_delay()

    lumpy = MatchView()
    engine2 = kick_off(build(win_condition="endless", lives=0))
    lumpy.apply(engine2.keyframe())
    engine2.delta()
    feed(lumpy, engine2, 0.12, 10)
    loose = lumpy.render_delay()

    assert loose > tight, "a worse link is smoothed over more"
    assert tight >= RENDER_DELAY_MIN
    assert loose <= RENDER_DELAY_MAX, "but never so much that it is unplayable"


def test_a_stall_holds_the_last_state_rather_than_running_ahead_of_it():
    """What used to happen here is the jump.

    With nothing beyond the render moment there is nothing to interpolate
    towards. Extrapolating carried on regardless and then corrected hard when
    the truth arrived. Holding is honest: the snake waits where it was last
    known to be.
    """
    engine = kick_off(build(win_condition="endless", lives=0, base_speed=10))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()
    feed(view, engine, 0.02, 6)

    time.sleep(0.4)

    held = view.state()
    for snake in held["snakes"]:
        assert snake["p"] < 1.0, "nothing is drawn past the cell it was in"


def test_your_own_snake_is_not_delayed_with_everybody_else():
    """A tenth of a second on somebody else is invisible. On you it is the game
    feeling broken."""
    engine = kick_off(build(players=2, win_condition="endless", lives=0,
                            base_speed=10))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()
    feed(view, engine, 0.03, 8)

    mine = [s for s in view.state(0)["snakes"] if s["id"] == 0][0]
    newest = [s for s in view.latest()["snakes"] if s["id"] == 0][0]

    assert mine["b"] == newest["b"], "own snake comes from the newest state"

    delayed = [s for s in view.state(0)["snakes"] if s["id"] == 1][0]
    assert delayed["id"] == 1


def test_interpolation_never_invents_a_cell_neither_state_had():
    engine = kick_off(build(players=2, win_condition="endless", lives=0))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    # Seeded with the opening state. Drawing happens in the recent past, so the
    # first frames drawn are from before the loop started, and not recording it
    # would fail this for the one reason that is not a bug.
    seen = [{s["id"]: [tuple(c) for c in s["b"]] for s in
             view.latest()["snakes"]}]

    for _ in range(12):
        for _ in range(3):
            engine.step()
        view.apply(engine.delta())
        seen.append({s["id"]: [tuple(c) for c in s["b"]] for s in
                     view.latest()["snakes"]})
        time.sleep(0.02)

        for snake in view.state()["snakes"]:
            body = [tuple(c) for c in snake["b"]]
            assert any(body == frame.get(snake["id"]) for frame in seen), (
                "a body was drawn that no arrived state ever contained"
            )


def test_the_moment_being_drawn_never_moves_backwards():
    """The bug this catches was invisible in every single-frame test.

    The moment being drawn is the clock minus a delay that follows the link. A
    delay that grows moves that moment into the past, and every snake on screen
    takes a step backwards. Drawn positions went 0.62, 0.46, 0.25 in a real
    match before this: not a stutter, a reversal.
    """
    engine = kick_off(build(win_condition="endless", lives=0, base_speed=5))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    # Uneven arrivals, which is what makes the delay move.
    for index in range(14):
        for _ in range(3):
            engine.step()
        view.apply(engine.delta())
        time.sleep(0.01 if index % 3 else 0.09)

    moments = []
    for _ in range(30):
        view.state()
        moments.append(view._render_at)
        time.sleep(0.004)

    for earlier, later in zip(moments, moments[1:], strict=False):
        assert later >= earlier, "the drawn moment went backwards"


def test_a_widening_delay_does_not_strand_the_drawing_in_the_past():
    """Forward only, but not frozen: it walks to the new delay rather than
    holding a moment that drifts further behind on every frame."""
    engine = kick_off(build(win_condition="endless", lives=0))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()
    feed(view, engine, 0.02, 6)

    view.state()
    time.sleep(0.4)
    view.state()

    assert time.monotonic() - view._render_at <= RENDER_DELAY_MAX + 0.05


# -- the economy -------------------------------------------------------------


def test_eating_your_own_remains_feeds_you_but_does_not_pay_you():
    """Otherwise the way to farm a score is to be cut and eat yourself.

    The segments were already paid for when the food that grew them was eaten.
    Picking them back up is recovering length, not earning again.
    """
    engine = kick_off(build(severing=True, win_condition="endless", lives=0))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    victim = engine.snakes[1]
    assert (11, 11) in engine.remains
    assert engine.remains[(11, 11)][1] == victim.id, "it remembers whose it was"

    score = victim.score
    length = victim.length

    # Turn the victim round onto its own dropped segment.
    victim.move_interval = engine.rules.move_interval_ticks(victim.length)
    victim.pending_heading = (0, 1)
    victim.heading = (0, 1)
    victim.body = deque([(11, 10), (11, 9), (11, 8)])
    engine.owner = bytearray(len(engine.owner))
    for cell in victim.body:
        engine._occupy(cell, victim.id)
    victim.accumulator = victim.move_interval - 1

    engine.step()

    assert victim.head == (11, 11), "it reached its own segment"
    assert victim.length == length + 1, "and got the length back"
    assert victim.score == score, "but was not paid for it a second time"


def test_somebody_else_eating_your_remains_is_paid_for_it():
    engine = kick_off(build(severing=True, win_condition="endless", lives=0))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 7), (11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    attacker = engine.snakes[0]
    score = attacker.score

    engine.set_heading(0, "down", seq=1, move=attacker.move_count)
    attacker.accumulator = attacker.move_interval - 1
    engine.step()

    assert attacker.head == (11, 11)
    assert attacker.score == score + 1, "the pile is what a cut is worth"


def test_a_killed_snake_leaves_its_body_on_the_floor():
    """A kill used to be worth less than a cut, which is the wrong way round.

    A cut dropped a pile worth taking and a kill dropped nothing at all, so the
    two never felt like parts of the same game.
    """
    engine = kick_off(build(severing=False, edge_behaviour="walls", lives=3,
                            win_condition="endless", remains_yield=100))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert engine.snakes[0].alive is False, "severing off, so this is a death"
    assert engine.remains, "the body is on the floor"
    assert all(owner == 0 for _, (_, owner) in engine.remains.items())


def test_a_kill_pays_a_bounty():
    engine = kick_off(build(severing=False, kill_bounty=50, lives=3,
                            win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert engine.snakes[0].alive is False
    assert engine.snakes[1].kills == 1
    assert engine.snakes[1].score == 2, "half of a snake three long, rounded"


def test_the_bounty_can_be_turned_off():
    engine = kick_off(build(severing=False, kill_bounty=0, lives=3,
                            win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 8), (11, 9), (11, 10), (11, 11)], (0, -1))
    engine.snakes[1].move_interval = 999
    engine.snakes[1].accumulator = 0

    engine.step()

    assert engine.snakes[1].kills == 1
    assert engine.snakes[1].score == 0, "the pile is the only reward"


# -- the multi-arena grid ----------------------------------------------------
#
# The grid is one cell space read as several arenas rather than several spaces
# joined up, so most of what would otherwise need testing here is the movement
# and severing already tested above, unchanged. What is left is the reading:
# which arena a cell is in, that a boundary is not an edge, and that the outer
# edge still wraps to the far side of the grid rather than to the near one.


def test_the_cell_space_is_the_layout_times_one_arena():
    engine = build(layout="2x2", arena_width=24, arena_height=24)

    assert engine.arena.width == 48
    assert engine.arena.height == 48
    assert engine.topology.count == 4
    assert len(engine.owner) == 48 * 48


def test_a_single_layout_is_the_arena_it_always_was():
    engine = build(arena_width=24, arena_height=24)

    assert engine.arena.width == 24
    assert engine.arena.height == 24
    assert engine.topology.single


def test_crossing_a_boundary_is_a_move_like_any_other():
    engine = kick_off(build(layout="2x2", arena_width=24, arena_height=24,
                            win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    engine.step()

    snake = engine.snakes[0]
    assert snake.alive, "an arena boundary is not a wall"
    assert tuple(snake.head) == (24, 10)
    assert snake.arena_id == 1
    assert engine.topology.arena_at(*snake.body[-1]) == 0, (
        "the tail is still in the arena the head has left"
    )


def test_the_outer_edge_wraps_to_the_far_side_of_the_grid():
    engine = kick_off(build(layout="2x2", arena_width=24, arena_height=24,
                            win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(47, 10), (46, 10), (45, 10)], (1, 0))
    engine.step()

    snake = engine.snakes[0]
    assert tuple(snake.head) == (0, 10)
    assert snake.arena_id == 0, "out of the right of arena 1 and into arena 0"


def test_walls_still_end_the_run_at_the_outside_of_the_grid():
    engine = kick_off(build(layout="2x2", arena_width=24, arena_height=24,
                            edge_behaviour="walls", win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(47, 10), (46, 10), (45, 10)], (1, 0))
    place(engine, 1, [(23, 14), (22, 14), (21, 14)], (1, 0))
    engine.step()

    assert engine.snakes[0].alive is False, "the outside of the grid is a wall"
    assert engine.snakes[1].alive is True, "the inside of it is not"


def test_food_is_stocked_for_every_arena():
    one = build(arena_width=24, arena_height=24, food_density="dense")
    four = build(layout="2x2", arena_width=24, arena_height=24,
                 food_density="dense")

    assert len(four.arena.food) == len(one.arena.food) * 4


def test_players_do_not_all_start_in_the_same_arena():
    engine = build(players=3, layout="2x2", arena_width=24, arena_height=24)

    used = {snake.arena_id for snake in engine.snakes.values()}
    assert len(used) > 1, "three players crowded into one arena of four"


def test_the_keyframe_says_what_the_layout_is():
    engine = build(layout="2x1", arena_width=24, arena_height=26)
    frame = engine.keyframe()

    assert frame["arena"] == {
        "w": 48, "h": 26, "edge": "wrap",
        "aw": 24, "ah": 26, "cols": 2, "rows": 1,
    }


def test_every_snake_reports_which_arena_it_is_in():
    engine = kick_off(build(layout="2x2", arena_width=24, arena_height=24,
                            win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(30, 30), (29, 30), (28, 30)], (1, 0))
    engine.step()

    entry = next(
        snake for snake in engine.keyframe()["snakes"] if snake["id"] == 0
    )
    assert entry["a"] == 3


def test_a_dead_snake_still_reports_the_arena_it_died_in():
    engine = kick_off(build(layout="2x2", arena_width=24, arena_height=24,
                            edge_behaviour="walls", win_condition="endless",
                            respawn_delay=5))
    clear_grid(engine)

    place(engine, 0, [(47, 30), (46, 30), (45, 30)], (1, 0))
    engine.step()

    snake = engine.snakes[0]
    assert snake.alive is False
    assert snake.arena_id == 3, "the camera has to be pointed somewhere"


# -- reading a grid from the outside -----------------------------------------
#
# What a player can work out about a layout without being able to see into it:
# where the leaderboard says everybody is, where an event happened, and how many
# players are in each arena. The last of these is the only thing sent for the
# minimap, and it is a count rather than a position on purpose.


def test_the_leaderboard_says_which_arena_everybody_is_in():
    engine = kick_off(build(players=2, layout="2x2", arena_width=24,
                            arena_height=24, win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(2, 2), (1, 2), (0, 2)], (1, 0))
    place(engine, 1, [(30, 30), (29, 30), (28, 30)], (1, 0))

    rows = {row["id"]: row["arena"] for row in engine.state()["standings"]}
    assert rows == {0: 0, 1: 3}


def test_an_event_says_which_arena_it_happened_in():
    engine = kick_off(build(players=2, layout="2x2", arena_width=24,
                            arena_height=24, severing=True,
                            win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(30, 30), (29, 30), (28, 30)], (1, 0))
    place(engine, 1, [(31, 28), (31, 29), (31, 30), (31, 31)], (0, 1))
    engine.step()

    events = engine.keyframe()["events"]
    assert events, "the collision should have said something"
    assert all(event["w"] == 3 for event in events)


def test_a_single_arena_does_not_pay_for_a_field_it_cannot_use():
    engine = kick_off(build(players=2, severing=True, win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(10, 10), (9, 10), (8, 10)], (1, 0))
    place(engine, 1, [(11, 8), (11, 9), (11, 10), (11, 11)], (0, 1))
    engine.step()

    events = engine.keyframe()["events"]
    assert events
    assert all("w" not in event for event in events)


def test_the_roll_up_counts_live_snakes_by_arena():
    engine = kick_off(build(players=3, layout="2x2", arena_width=24,
                            arena_height=24, win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(2, 2), (1, 2), (0, 2)], (1, 0))
    place(engine, 1, [(26, 2), (25, 2), (24, 2)], (1, 0))
    place(engine, 2, [(30, 2), (29, 2), (28, 2)], (1, 0))

    assert engine.keyframe()["map"] == [1, 2, 0, 0]


def test_the_roll_up_says_nothing_about_where_in_an_arena():
    engine = kick_off(build(players=2, layout="2x1", arena_width=24,
                            arena_height=24, win_condition="endless"))

    roll_up = engine.keyframe()["map"]
    assert all(isinstance(count, int) for count in roll_up)
    assert len(roll_up) == 2


def test_a_single_arena_sends_no_roll_up_at_all():
    engine = kick_off(build())

    assert "map" not in engine.keyframe()
    assert "map" not in engine.delta()


def test_the_roll_up_is_sent_again_only_when_it_changes():
    engine = kick_off(build(players=2, layout="2x1", arena_width=24,
                            arena_height=24, win_condition="endless"))
    clear_grid(engine)

    place(engine, 0, [(2, 10), (1, 10), (0, 10)], (1, 0))
    place(engine, 1, [(2, 14), (1, 14), (0, 14)], (1, 0))

    engine.keyframe()
    engine.delta()

    assert "map" not in engine.delta(), "nobody has moved arena"

    place(engine, 1, [(26, 14), (25, 14), (24, 14)], (1, 0))
    assert engine.delta()["map"] == [1, 1]


def test_a_client_keeps_the_last_roll_up_it_was_given():
    engine = kick_off(build(players=2, layout="2x1", arena_width=24,
                            arena_height=24, win_condition="endless"))
    view = MatchView()

    view.apply(engine.keyframe())
    engine.delta()

    opening = view.state()["map"]
    assert opening

    engine.step()
    quiet = engine.delta()
    assert "map" not in quiet, "the arenas have not changed hands"

    view.apply(quiet)
    assert view.state()["map"] == opening


# -- snapshots scoped to one arena -------------------------------------------
#
# The line these check is the one the whole layout rests on: which arena
# somebody is in is public, and where in that arena they are is not. So a
# message carries every snake, and carries a body only for the snakes its
# reader could actually see.


def two_arenas(**overrides):
    settings = {
        "players": 2,
        "layout": "2x1",
        "arena_width": 24,
        "arena_height": 24,
        "win_condition": "endless",
    }
    settings.update(overrides)
    engine = kick_off(build(**settings))
    clear_grid(engine)
    place(engine, 0, [(4, 10), (3, 10), (2, 10)], (1, 0))
    place(engine, 1, [(28, 10), (27, 10), (26, 10)], (1, 0))
    return engine


def test_a_stream_carries_bodies_only_for_the_arena_it_is_for():
    engine = two_arenas()

    left = {entry["id"]: entry for entry in engine.keyframe(0)["snakes"]}
    assert left[0]["b"], "your own arena, drawn"
    assert left[1]["b"] == [], "the other arena, not drawn"

    right = {entry["id"]: entry for entry in engine.keyframe(1)["snakes"]}
    assert right[1]["b"]
    assert right[0]["b"] == []


def test_a_snake_you_cannot_see_is_still_on_the_leaderboard():
    engine = two_arenas()
    engine.snakes[1].score = 12

    frame = engine.keyframe(0)
    rows = {row["id"]: row for row in standings_from(frame["snakes"],
                                                     frame["rank"])}

    assert rows[1]["score"] == 12
    assert rows[1]["length"] == 3, "length comes from the stat, not the body"
    assert rows[1]["arena"] == 1


def test_food_belongs_to_the_arena_it_is_in():
    engine = two_arenas()
    engine.arena.food = {(5, 5), (30, 5)}

    assert engine.keyframe(0)["food"] == [[5, 5]]
    assert engine.keyframe(1)["food"] == [[30, 5]]


def test_a_snake_crossing_is_in_both_streams_at_once():
    engine = two_arenas()
    place(engine, 0, [(24, 10), (23, 10), (22, 10)], (1, 0))

    left = {entry["id"]: entry for entry in engine.keyframe(0)["snakes"]}
    right = {entry["id"]: entry for entry in engine.keyframe(1)["snakes"]}

    assert left[0]["b"], "the tail is still in the arena it is leaving"
    assert right[0]["b"], "and the head is in the one it is entering"
    assert left[0]["b"] == right[0]["b"], "one body, sent whole to both"


def test_arriving_in_an_arena_is_sent_as_a_whole_body():
    engine = two_arenas()
    engine.keyframe(1)
    engine.delta(1)

    place(engine, 0, [(26, 14), (25, 14), (24, 14)], (1, 0))
    arrival = next(
        entry for entry in engine.delta(1)["snakes"] if entry["id"] == 0
    )

    assert arrival["b"] == [[26, 14], [25, 14], [24, 14]]
    assert "heads" not in arrival, "cells gained mean nothing without the rest"
    assert arrival.get("name"), "and nothing it was told before counts"


def test_leaving_an_arena_empties_the_body_once():
    engine = two_arenas()
    engine.keyframe(1)
    place(engine, 0, [(26, 14), (25, 14), (24, 14)], (1, 0))
    engine.delta(1)

    place(engine, 0, [(4, 14), (3, 14), (2, 14)], (1, 0))
    leaving = next(
        entry for entry in engine.delta(1)["snakes"] if entry["id"] == 0
    )
    assert leaving["b"] == [], "otherwise it stays on the screen where it was"

    after = next(
        entry for entry in engine.delta(1)["snakes"] if entry["id"] == 0
    )
    assert "b" not in after, "said once, not every message from now on"


def test_crossing_owes_the_arena_it_entered_a_keyframe():
    engine = two_arenas()
    engine.next_messages()
    engine.next_messages()

    place(engine, 0, [(23, 10), (22, 10), (21, 10)], (1, 0))
    engine.step()

    messages = engine.next_messages()
    assert messages[1]["type"] == "keyframe", (
        "the player arriving has no state of that stream to apply a delta to"
    )
    assert messages[0]["type"] == "snapshot", "and the others are undisturbed"


def test_every_stream_describes_the_same_interval():
    engine = two_arenas()
    engine.next_messages()

    for _ in range(20):
        engine.step()
        messages = engine.next_messages()
        bases = {
            message["base"] for message in messages.values()
            if message["type"] == "snapshot"
        }
        assert len(bases) <= 1, "streams sharing a tick share a base"


def test_two_clients_on_two_streams_each_see_their_own_arena():
    engine = two_arenas(players=2)
    views = {0: MatchView(), 1: MatchView()}

    for _ in range(60):
        engine.step()
        messages = engine.next_messages()
        for stream, view in views.items():
            view.apply(messages[stream])

    for stream, view in views.items():
        frame = view.latest()
        bodies = {
            snake["id"]: snake["b"] for snake in frame["snakes"]
        }
        mine = engine.snakes[stream]
        assert bodies[stream], "your own snake is always drawn"
        for body in bodies.values():
            if not body:
                continue
            arenas = {
                engine.topology.arena_at(cell[0], cell[1]) for cell in body
            }
            assert stream in arenas, (
                "a body was sent to an arena no part of it is in"
            )
        assert len(frame["standings"]) == 2, "everybody is on the leaderboard"
        assert mine.id in {row["id"] for row in frame["standings"]}


def test_a_single_arena_is_one_stream_and_is_unchanged():
    engine = kick_off(build(players=2))

    messages = engine.next_messages()
    assert list(messages) == [None]
    assert engine.stream_for(0) is None


# -- what the host asks for is not what a client was told ---------------------


def test_the_host_drawing_its_own_screen_tells_nobody_anything():
    """The host polls its own state once per frame, far more often than it

    broadcasts. Recording those as fields a client now knows meant a joined
    player was never sent a score that changed between two of the host's
    frames, and never sent an event that happened between them either.
    """
    engine = kick_off(build(players=2, win_condition="endless"))
    view = MatchView()
    view.apply(engine.keyframe())
    engine.delta()

    engine.snakes[0].score += 7
    engine._record("death", None, engine.snakes[1], c="wall")

    for _ in range(5):
        engine.state(own_id=0)

    message = engine.delta()
    view.apply(message)

    seen = next(snake for snake in view.latest()["snakes"] if snake["id"] == 0)
    assert seen["score"] == 7, "the client was never sent what it now believes"
    assert len(message["events"]) == 1


def test_the_host_sees_its_own_arena_and_no_further():
    engine = two_arenas()

    frame = engine.state(own_id=1)
    bodies = {snake["id"]: snake["b"] for snake in frame["snakes"]}

    assert bodies[1], "the host's own snake"
    assert bodies[0] == [], "hosting is not a vantage point"
    assert len(frame["standings"]) == 2


def test_a_client_routed_by_arena_never_loses_its_own_snake():
    """The transport as the server actually drives it, across a crossing.

    Each broadcast the client is handed whichever stream its player is now on,
    which is what utils/net/server.py does. The crossing is the moment worth
    checking: the state it has is of a stream it is about to stop being sent,
    and a delta from the new one would mean nothing to it.
    """
    engine = kick_off(build(players=2, layout="2x1", arena_width=24,
                            arena_height=24, base_speed=10,
                            win_condition="endless"))
    clear_grid(engine)
    place(engine, 0, [(20, 10), (19, 10), (18, 10)], (1, 0))
    place(engine, 1, [(4, 20), (3, 20), (2, 20)], (1, 0))

    view = MatchView()
    arenas = set()

    for _ in range(120):
        engine.step()
        stream = engine.stream_for(0)
        arenas.add(stream)
        assert view.apply(engine.next_messages()[stream]) is True

        drawn = view.latest()
        mine = next(snake for snake in drawn["snakes"] if snake["id"] == 0)
        assert mine["b"], "your own snake is drawn in whichever arena you are in"
        assert len(drawn["standings"]) == 2

    assert len(arenas) == 2, "the run has to actually cross for this to mean anything"
