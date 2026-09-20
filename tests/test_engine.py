"""The simulation and the single player API.

Whether the game is fun, and whether the arena size feels different, are checked
by hand. Movement, collision, growth, wrapping, scoring and persistence are
checked here.
"""

import pytest

from utils.game.arena import EMPTY, FOOD, ITEM, SNAKE, Arena, Topology
from utils.game.engine import (
    DEATH_SELF,
    DEATH_WALL,
    PHASE_OVER,
    PHASE_PLAYING,
    SoloEngine,
)
from utils.game.items import spawn_food
from utils.game.rules import RuleError, RuleSet


@pytest.fixture()
def small_walls():
    return RuleSet(
        arena_width=20,
        arena_height=20,
        edge_behaviour="walls",
        base_speed=10,
        starting_length=3,
        length_affects_speed=False,
    )


@pytest.fixture()
def small_wrap(small_walls):
    return RuleSet(
        arena_width=20,
        arena_height=20,
        edge_behaviour="wrap",
        base_speed=10,
        starting_length=3,
        length_affects_speed=False,
    )


def run_until_over(engine, limit=5000):
    for _ in range(limit):
        engine.step()
        if engine.phase == PHASE_OVER:
            return True
    return False


# -- rules ------------------------------------------------------------------


def test_defaults_are_valid():
    RuleSet().validate()


@pytest.mark.parametrize(
    "field, value",
    [
        ("arena_width", 19),
        ("arena_width", 81),
        ("arena_height", 19),
        ("base_speed", 0),
        ("base_speed", 11),
        ("starting_length", 2),
        ("starting_length", 11),
    ],
)
def test_out_of_bounds_values_are_rejected(field, value):
    with pytest.raises(RuleError):
        RuleSet(**{field: value}).validate()


def test_unknown_keys_are_ignored():
    ruleset = RuleSet.from_dict({"arena_width": 30, "cheat_mode": True})
    assert ruleset.arena_width == 30
    assert not hasattr(ruleset, "cheat_mode")


def test_fingerprint_separates_configurations():
    assert RuleSet().fingerprint() != RuleSet(arena_width=50).fingerprint()
    assert RuleSet().fingerprint() == RuleSet().fingerprint()


def test_longer_snake_moves_slower_and_the_ceiling_holds():
    # The ceiling is load bearing: without it a long snake stops moving.
    ruleset = RuleSet(base_speed=5, starting_length=3, length_penalty=5,
                      max_slowdown=2.0)
    base = ruleset.move_interval_ticks(3)
    longer = ruleset.move_interval_ticks(20)
    absurd = ruleset.move_interval_ticks(4000)

    assert longer > base
    assert absurd <= base * ruleset.max_slowdown


# -- arena ------------------------------------------------------------------


def test_wrap_reenters_the_opposite_edge():
    arena = Arena(10, 10, "wrap")
    assert arena.step_from(0, 5, (-1, 0)) == (9, 5)
    assert arena.step_from(5, 0, (0, -1)) == (5, 9)
    assert arena.step_from(9, 5, (1, 0)) == (0, 5)


def test_walls_end_the_move():
    arena = Arena(10, 10, "walls")
    assert arena.step_from(0, 5, (-1, 0)) is None
    assert arena.step_from(9, 5, (1, 0)) is None
    assert arena.step_from(5, 5, (1, 0)) == (6, 5)


def test_food_spawns_only_on_empty_cells():
    arena = Arena(6, 6, "wrap")
    for x in range(6):
        for y in range(5):
            arena.set(x, y, SNAKE)

    spawn_food(arena, 10)

    assert len(arena.food) == 6
    for x, y in arena.food:
        assert y == 5
        assert arena.at(x, y) == FOOD


# -- engine -----------------------------------------------------------------


def test_a_walled_arena_kills_on_the_wall(small_walls):
    engine = SoloEngine(small_walls, seed=1)
    engine.start()

    assert run_until_over(engine)
    assert engine.snake.death_cause == DEATH_WALL


def test_wrap_does_not_kill(small_wrap):
    engine = SoloEngine(small_wrap, seed=2)
    engine.start()

    for _ in range(1200):
        engine.step()

    assert engine.phase == PHASE_PLAYING


def test_running_into_itself_ends_the_match():
    ruleset = RuleSet(arena_width=30, arena_height=30, base_speed=10,
                      starting_length=6, length_affects_speed=False)
    engine = SoloEngine(ruleset, seed=3)
    engine.start()

    turns = ["down", "left", "up", "right"]
    index = 0

    for _ in range(600):
        engine.step()
        if engine.snake.accumulator == 0:
            engine.set_heading(turns[index % 4])
            index += 1
        if engine.phase == PHASE_OVER:
            break

    assert engine.phase == PHASE_OVER
    assert engine.snake.death_cause == DEATH_SELF


def test_reversing_into_the_neck_is_refused(small_wrap):
    engine = SoloEngine(small_wrap, seed=4)
    engine.start()

    assert engine.set_heading("left") is False
    assert engine.set_heading("up") is True
    assert engine.set_heading("nowhere") is False


def test_eating_grows_the_snake_and_scores(small_wrap):
    engine = SoloEngine(small_wrap, seed=5)
    engine.start()

    head_x, head_y = engine.snake.head
    engine.arena.food.clear()
    engine.arena.add_food(head_x + 1, head_y)

    before = engine.snake.length
    for _ in range(small_wrap.base_interval_ticks()):
        engine.step()

    assert engine.snake.length == before + 1
    assert engine.snake.score == 1
    assert len(engine.arena.food) >= 1


def test_following_your_own_tail_is_legal(small_wrap):
    # The tail cell is vacated on the same move, so entering it is not a
    # collision. Getting this wrong makes a tight turn lethal.
    engine = SoloEngine(small_wrap, seed=6)
    engine.start()
    engine.arena.food.clear()

    interval = small_wrap.base_interval_ticks()
    for heading in ("down", "left", "up"):
        engine.set_heading(heading)
        for _ in range(interval):
            engine.step()

    assert engine.phase == PHASE_PLAYING


def test_occupancy_grid_matches_the_body(small_wrap):
    engine = SoloEngine(small_wrap, seed=7)
    engine.start()

    for _ in range(200):
        engine.step()

    occupied = sum(1 for value in engine.arena.cells if value == SNAKE)
    assert occupied == engine.snake.length

    for x, y in engine.snake.body:
        assert engine.arena.at(x, y) == SNAKE

    # Items are on the floor too, and are the reason this census names four
    # things rather than the two it started with.
    empty = sum(1 for value in engine.arena.cells if value == EMPTY)
    assert empty == (
        engine.arena.width * engine.arena.height
        - engine.snake.length
        - len(engine.arena.food)
        - len(engine.arena.items)
    )
    assert sum(1 for value in engine.arena.cells if value == ITEM) == len(
        engine.arena.items
    )


def test_snapshot_uses_positional_arrays(small_wrap):
    engine = SoloEngine(small_wrap, seed=8)
    engine.start()

    snapshot = engine.snapshot()
    snake = snapshot["snakes"][0]

    assert snapshot["type"] == "snapshot"
    assert isinstance(snake["b"][0], list) and len(snake["b"][0]) == 2
    assert 0.0 <= snake["p"] <= 1.0


# -- persistence and API ----------------------------------------------------


@pytest.fixture()
def isolated_store(monkeypatch, tmp_path):
    from utils.store import paths

    monkeypatch.setattr(paths, "_resolve", lambda: tmp_path)
    return tmp_path


def test_profile_round_trips(isolated_store):
    from utils.store import profile as profile_store

    saved = profile_store.save({"username": "  Tomiwa  ", "colour": "#58a6ff"})
    assert saved["username"] == "Tomiwa"
    assert saved["colour"] == "#58a6ff"
    assert profile_store.load()["username"] == "Tomiwa"


def test_profile_rejects_an_unknown_colour(isolated_store):
    from utils.store import profile as profile_store

    saved = profile_store.save({"colour": "#123456"})
    assert saved["colour"] != "#123456"


def test_personal_best_is_kept_per_ruleset(isolated_store):
    from utils.store import solo as solo_store

    rules_a = RuleSet()
    rules_b = RuleSet(arena_width=60)

    solo_store.record_result(
        {"score": 12, "rules_fingerprint": rules_a.fingerprint()}, rules_a.to_dict()
    )
    solo_store.record_result(
        {"score": 3, "rules_fingerprint": rules_b.fingerprint()}, rules_b.to_dict()
    )

    assert solo_store.best_for(rules_a.fingerprint()) == 12
    assert solo_store.best_for(rules_b.fingerprint()) == 3

    outcome = solo_store.record_result(
        {"score": 5, "rules_fingerprint": rules_a.fingerprint()}, rules_a.to_dict()
    )
    assert outcome["best"] == 12
    assert outcome["is_new_best"] is False


def test_history_is_capped(isolated_store):
    from utils.store import solo as solo_store

    ruleset = RuleSet()
    for score in range(30):
        solo_store.record_result(
            {"score": score, "rules_fingerprint": ruleset.fingerprint()},
            ruleset.to_dict(),
        )

    assert len(solo_store.load()["results"]) == solo_store.MAX_RESULTS


@pytest.fixture()
def client(isolated_store):
    from app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client
    app.solo_session.stop()


def test_solo_start_returns_a_keyframe(client):
    payload = client.post(
        "/api/solo/start",
        json={"rules": {"arena_width": 24, "arena_height": 24, "base_speed": 3}},
    ).get_json()

    assert payload["type"] == "keyframe"
    assert payload["arena"]["w"] == 24
    assert payload["state"]["phase"] == PHASE_PLAYING


def test_solo_start_rejects_out_of_bounds_rules(client):
    response = client.post("/api/solo/start", json={"rules": {"arena_width": 5}})

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "invalid_rules"
    assert "arena_width" in body["detail"]


def test_input_without_a_match_is_a_conflict(client):
    response = client.post("/api/solo/input", json={"heading": "up"})
    assert response.status_code == 409


def test_input_and_pause_drive_the_match(client):
    client.post("/api/solo/start", json={"rules": {}})

    assert client.post("/api/solo/input", json={"heading": "up"}).status_code == 200
    bad = client.post("/api/solo/input", json={"heading": "sideways"})
    assert bad.status_code == 400

    assert client.post("/api/solo/pause").get_json()["phase"] == "paused"
    assert client.post("/api/solo/pause").get_json()["phase"] == "playing"


def test_state_reports_the_best_score(client):
    client.post("/api/solo/start", json={"rules": {}})
    payload = client.get("/api/solo/state").get_json()

    assert payload["phase"] in (PHASE_PLAYING, "paused")
    assert "best" in payload


def test_only_the_schema_endpoint_serves_the_options(client):
    # /api/rules/defaults was replaced by /api/rules/schema, and the old route
    # is gone rather than kept as an alias: a second way to ask the same
    # question is a second thing to keep in step.
    assert client.get("/api/rules/defaults").status_code == 404
    assert client.get("/api/rules/schema").status_code == 200


# -- the arena grid ----------------------------------------------------------


def test_one_arena_is_the_whole_space():
    topology = Topology(40, 30)

    assert (topology.width, topology.height) == (40, 30)
    assert topology.count == 1
    assert topology.single
    assert topology.arena_at(39, 29) == 0


def test_arena_ids_run_across_and_then_down():
    topology = Topology(20, 20, 2, 2)

    assert (topology.width, topology.height) == (40, 40)
    assert topology.arena_at(0, 0) == 0
    assert topology.arena_at(20, 0) == 1
    assert topology.arena_at(0, 20) == 2
    assert topology.arena_at(39, 39) == 3


def test_an_arena_starts_where_the_last_one_ended():
    topology = Topology(20, 20, 2, 2)

    assert topology.origin(0) == (0, 0)
    assert topology.origin(1) == (20, 0)
    assert topology.origin(2) == (0, 20)
    assert topology.origin(3) == (20, 20)


def test_the_grid_wraps_round_to_itself():
    topology = Topology(20, 20, 2, 2)

    assert topology.neighbour(0, (1, 0)) == 1
    assert topology.neighbour(1, (1, 0)) == 0, "off the right, back on the left"
    assert topology.neighbour(0, (0, 1)) == 2
    assert topology.neighbour(2, (0, 1)) == 0


def test_a_row_of_arenas_has_nothing_above_or_below_it():
    topology = Topology(20, 20, 2, 1)

    assert topology.neighbour(0, (0, 1)) == 0
    assert topology.neighbour(0, (1, 0)) == 1
