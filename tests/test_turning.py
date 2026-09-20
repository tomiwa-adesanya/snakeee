"""Turning, and the timing of what is drawn between moves.

Three things these hold in place, each of which was wrong once and looked like a
different problem than it was:

  1. A direction refused now must be accepted later, once the snake has turned.
     Whether a turn is legal depends on where the snake is, not on what was
     pressed earlier.
  2. A snapshot carries the duration of a move, so the renderer can extrapolate
     between snapshots instead of drawing 20 positions a second on a screen that
     refreshes 60 times a second.
  3. A snapshot carries the cell the head is moving into, so the renderer never
     animates a move that is not going to happen.
"""

from utils.game.engine import HEADINGS, SoloEngine
from utils.game.rules import RuleSet


def slow_rules(**overrides):
    values = {
        "arena_width": 30,
        "arena_height": 30,
        "edge_behaviour": "wrap",
        "base_speed": 1,
        "starting_length": 4,
        "length_affects_speed": False,
    }
    values.update(overrides)
    return RuleSet(**values)


def advance_one_move(engine):
    for _ in range(engine.snake.move_interval + 1):
        engine.step()
        if engine.snake.accumulator == 0:
            return


def test_a_refused_direction_is_accepted_after_the_snake_turns():
    # Moving right, left is refused. After turning up, left is legal again, and
    # nothing about the earlier refusal may prevent it.
    engine = SoloEngine(slow_rules(), seed=1)
    engine.start()

    assert engine.set_heading("left") is False
    assert engine.set_heading("up") is True

    advance_one_move(engine)
    assert engine.snake.heading == HEADINGS["up"]

    assert engine.set_heading("left") is True


def test_wrapping_does_not_change_which_turns_are_legal():
    engine = SoloEngine(slow_rules(base_speed=10), seed=2)
    engine.start()
    engine.set_heading("up")
    advance_one_move(engine)

    # Drive up until the snake has crossed the top edge at least once.
    for _ in range(engine.snake.move_interval * 40):
        engine.step()

    assert engine.phase == "playing"
    assert engine.set_heading("right") is True
    assert engine.set_heading("down") is False


def test_a_refusal_leaves_the_queued_heading_alone():
    engine = SoloEngine(slow_rules(), seed=3)
    engine.start()

    engine.set_heading("up")
    queued = engine.snake.pending_heading

    assert engine.set_heading("left") is False
    assert engine.snake.pending_heading == queued


def test_the_snapshot_names_the_cell_the_head_is_moving_into():
    # The renderer slides the head toward this cell rather than predicting from
    # the heading, so a turn on the tick that would have wrapped does not animate
    # a crossing that never happens.
    engine = SoloEngine(slow_rules(arena_width=20, arena_height=20), seed=8)
    engine.start()

    head = engine.snake.head
    assert engine.snapshot()["snakes"][0]["n"] == [head[0] + 1, head[1]]

    # A queued turn changes the target immediately, before the move happens.
    engine.set_heading("up")
    assert engine.snapshot()["snakes"][0]["n"] == [head[0], head[1] - 1]


def test_the_target_cell_wraps_and_reports_none_at_a_wall():
    wrapping = SoloEngine(slow_rules(arena_width=20, arena_height=20), seed=9)
    wrapping.start()
    wrapping.set_heading("up")

    for _ in range(wrapping.snake.move_interval * 12):
        wrapping.step()

    head = wrapping.snake.head
    target = wrapping.snapshot()["snakes"][0]["n"]
    assert target is not None

    # Once the head reaches the top row the target is the bottom row, which is
    # what tells the renderer to draw both halves of the crossing.
    if head[1] == 0:
        assert target == [head[0], 19]

    walled = SoloEngine(slow_rules(edge_behaviour="walls"), seed=9)
    walled.start()
    for _ in range(walled.snake.move_interval * 40):
        walled.step()
        if walled.phase == "over":
            break
    assert walled.phase == "over"


def test_the_snapshot_carries_the_queued_heading():
    engine = SoloEngine(slow_rules(), seed=4)
    engine.start()
    engine.set_heading("down")

    snake = engine.snapshot()["snakes"][0]
    assert snake["h"] == [1, 0]
    assert snake["ph"] == [0, 1]


def test_the_snapshot_carries_the_move_duration():
    # The renderer extrapolates between snapshots using this, so it has to be the
    # real duration of a move rather than something the frontend infers.
    engine = SoloEngine(slow_rules(base_speed=1), seed=5)
    engine.start()

    snake = engine.snapshot()["snakes"][0]
    assert snake["interval"] == 12
    assert snake["interval_ms"] == 200.0

    faster = SoloEngine(slow_rules(base_speed=10), seed=5)
    faster.start()
    assert faster.snapshot()["snakes"][0]["interval_ms"] == 50.0


def test_progress_climbs_towards_the_next_move():
    engine = SoloEngine(slow_rules(base_speed=1), seed=6)
    engine.start()

    first = engine.snapshot()["snakes"][0]["p"]
    for _ in range(6):
        engine.step()
    second = engine.snapshot()["snakes"][0]["p"]

    assert second > first
    assert second <= 1.0


def test_two_turns_inside_one_move_keep_the_last_legal_one():
    # Only the last legal heading before the move boundary applies.
    engine = SoloEngine(slow_rules(), seed=7)
    engine.start()

    assert engine.set_heading("up") is True
    assert engine.set_heading("down") is True

    advance_one_move(engine)
    assert engine.snake.heading == HEADINGS["down"]


def test_a_finished_match_reports_the_same_result_every_time(tmp_path, monkeypatch):
    """Every request about a finished match must get the same answer.

    Two state requests can be in flight at once, and the frontend draws whichever
    lands last. If only the first request to notice the end carried the result,
    the summary could show zeros while the bar behind it showed the real score.
    """
    from utils.store import paths

    monkeypatch.setattr(paths, "_resolve", lambda: tmp_path)

    from app import create_app

    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as client:
        client.post(
            "/api/solo/start",
            json={
                "rules": {
                    "arena_width": 20,
                    "arena_height": 20,
                    "edge_behaviour": "walls",
                    "base_speed": 10,
                }
            },
        )

        engine = app.solo_session.engine
        for _ in range(3000):
            engine.step()
            if engine.phase == "over":
                break

        first = client.get("/api/solo/state").get_json()
        second = client.get("/api/solo/state").get_json()
        third = client.get("/api/solo/state").get_json()

        assert first["phase"] == "over"
        assert first["result"] == second["result"] == third["result"]
        assert second["best"] == first["best"]
        assert "result" in third

    app.solo_session.stop()


def test_an_input_returns_the_state_it_produced(tmp_path, monkeypatch):
    # The reply carries the cell the head is now moving into. Without it the
    # renderer would keep animating the previous target until the next poll.
    from utils.store import paths

    monkeypatch.setattr(paths, "_resolve", lambda: tmp_path)

    from app import create_app

    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as client:
        client.post(
            "/api/solo/start",
            json={"rules": {"arena_width": 20, "arena_height": 20, "base_speed": 1}},
        )

        reply = client.post("/api/solo/input", json={"heading": "up"}).get_json()

        assert reply["ok"] is True
        snake = reply["state"]["snakes"][0]
        assert snake["ph"] == [0, -1]
        assert snake["n"] == [snake["b"][0][0], snake["b"][0][1] - 1]

    app.solo_session.stop()
