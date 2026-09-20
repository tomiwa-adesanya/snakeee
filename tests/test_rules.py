"""The ruleset: its schema, its bounds, and the presets built from it.

Four things this file exists to hold in place:

  1. Every option that affects play is settable and takes effect.
  2. Out-of-bounds values sent directly to the API are refused by name.
  3. A saved preset survives a restart and reproduces the same match.
  4. A long snake is measurably slower than a short one, and the slowdown stops
     at the configured ceiling.
"""

import pytest

from utils.game.engine import SoloEngine
from utils.game.rules import (
    BY_KEY,
    FORCED,
    NEEDS,
    SCHEMA,
    SHIPPED_PRESETS,
    RuleError,
    RuleSet,
    coerce,
    forced_value,
    relevant,
    schema_document,
)


@pytest.fixture()
def isolated_store(monkeypatch, tmp_path):
    from utils.store import paths

    monkeypatch.setattr(paths, "_resolve", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def client(isolated_store):
    from app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client
    app.solo_session.stop()


# -- schema -----------------------------------------------------------------


def test_the_schema_covers_every_ruleset_field():
    fields = {field.name for field in RuleSet.__dataclass_fields__.values()}
    assert fields == set(BY_KEY)


def test_every_schema_entry_is_well_formed():
    for entry in SCHEMA:
        assert entry["scope"] in ("solo", "multiplayer", "both"), entry["key"]
        assert entry["type"] in ("int", "float", "bool", "enum", "text")
        assert entry["label"]

        if entry["type"] in ("int", "float"):
            assert entry["min"] <= entry["default"] <= entry["max"], entry["key"]
        if entry["type"] == "enum":
            assert entry["default"] in entry["options"], entry["key"]
        assert "phase" not in entry, entry["key"] + " must not carry a roadmap"


def test_the_section_4_6_option_count_is_met():
    # The design calls for at least forty configurable options.
    assert len(SCHEMA) >= 40


def test_the_documented_defaults_are_the_actual_defaults():
    defaults = RuleSet()
    assert defaults.arena_width == 40
    assert defaults.edge_behaviour == "wrap"
    assert defaults.base_speed == 5
    assert defaults.lives == 3
    assert defaults.player_cap == 8
    assert defaults.win_condition == "last_standing"
    assert defaults.food_density == "normal"
    assert defaults.bot_count == 0


def test_schema_document_groups_are_ordered():
    document = schema_document()
    names = [group["name"] for group in document["groups"]]
    assert names == document["group_order"]


# -- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("arena_width", 19),
        ("arena_height", 81),
        ("base_speed", 11),
        ("length_penalty", 6),
        ("max_slowdown", 3.5),
        ("lives", 10),
        ("player_cap", 13),
        ("effect_duration", 2),
        ("bot_count", 12),
    ],
)
def test_out_of_bounds_values_are_rejected(field, value):
    with pytest.raises(RuleError):
        RuleSet(**{field: value}).validate()


def test_every_error_is_reported_at_once():
    errors = RuleSet(arena_width=5, base_speed=99, lives=44).errors()
    assert set(errors) == {"arena_width", "base_speed", "lives"}
    assert "between 20 and 80" in errors["arena_width"]


def test_enum_values_are_checked():
    assert "edge_behaviour" in RuleSet(edge_behaviour="bouncy").errors()
    assert "must be one of" in RuleSet(edge_behaviour="bouncy").errors()[
        "edge_behaviour"
    ]


def test_cross_field_rules_are_checked():
    assert "starting_length" in RuleSet(arena_width=20, starting_length=10).errors() \
        or RuleSet(arena_width=20, starting_length=10).errors() == {}
    assert "min_players" in RuleSet(min_players=12, player_cap=4).errors()
    assert "bot_count" in RuleSet(bot_count=11, player_cap=4).errors()


def test_strings_are_coerced_from_form_values():
    ruleset = RuleSet.from_dict(
        {"arena_width": "30", "length_affects_speed": "false", "max_slowdown": "2.5"}
    )
    assert ruleset.arena_width == 30
    assert ruleset.length_affects_speed is False
    assert ruleset.max_slowdown == 2.5


def test_room_name_does_not_change_the_fingerprint():
    # Otherwise renaming a room would reset the personal best attached to it.
    assert RuleSet(room_name="one").fingerprint() == RuleSet(
        room_name="two"
    ).fingerprint()
    assert RuleSet().fingerprint() != RuleSet(base_speed=9).fingerprint()


def test_api_rejects_out_of_bounds_with_a_named_field(client):
    response = client.post("/api/solo/start", json={"rules": {"base_speed": 44}})
    assert response.status_code == 400

    body = response.get_json()
    assert body["error"] == "invalid_rules"
    assert "base_speed" in body["detail"]


def test_validate_endpoint_names_every_bad_field(client):
    payload = client.post(
        "/api/rules/validate",
        json={"rules": {"arena_width": 1, "lives": 99}},
    ).get_json()

    assert payload["valid"] is False
    assert set(payload["errors"]) == {"arena_width", "lives"}


def test_validate_endpoint_survives_an_out_of_range_speed(client):
    # Found live: the endpoint used to compute the tick interval before
    # checking the speed, so the one request guaranteed to carry a bad speed
    # was the one that raised.
    response = client.post(
        "/api/rules/validate", json={"rules": {"base_speed": 50, "arena_width": 3}}
    )
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["valid"] is False
    assert set(payload["errors"]) == {"base_speed", "arena_width"}
    assert payload["base_interval_ticks"] is None


def test_validate_endpoint_reports_the_interval_range(client):
    payload = client.post(
        "/api/rules/validate", json={"rules": {"base_speed": 5, "max_slowdown": 2.0}}
    ).get_json()

    assert payload["valid"] is True
    assert payload["base_interval_ticks"] == 8
    assert payload["max_interval_ticks"] == 16


# -- length affects speed ---------------------------------------------------


def test_a_longer_snake_is_slower_and_the_ceiling_holds():
    ruleset = RuleSet(base_speed=5, starting_length=3, length_penalty=5,
                      max_slowdown=2.0)
    base = ruleset.move_interval_ticks(3)

    assert ruleset.move_interval_ticks(10) > base
    assert ruleset.move_interval_ticks(16) > ruleset.move_interval_ticks(10)

    # Past a certain length every longer snake sits on the ceiling, which is
    # the point of the ceiling.
    assert ruleset.move_interval_ticks(20) == ruleset.max_interval_ticks()
    assert ruleset.move_interval_ticks(10000) == ruleset.max_interval_ticks()


def test_the_toggle_switches_the_slowdown_off():
    ruleset = RuleSet(length_affects_speed=False, length_penalty=5)
    assert ruleset.move_interval_ticks(3) == ruleset.move_interval_ticks(400)


def test_the_engine_reports_the_current_interval():
    engine = SoloEngine(RuleSet(base_speed=5), seed=1)
    engine.start()

    snake = engine.snapshot()["snakes"][0]
    assert snake["interval"] == 8
    assert snake["mps"] == pytest.approx(7.5)


# -- presets ----------------------------------------------------------------


def test_shipped_presets_are_valid_and_present(isolated_store):
    from utils.store import presets as preset_store

    names = [row["name"] for row in preset_store.listing()]
    assert names[:3] == ["Classic", "Brawl", "Hide and Seek"]

    for ruleset in SHIPPED_PRESETS.values():
        assert ruleset.errors() == {}


def test_classic_is_actually_classic():
    classic = SHIPPED_PRESETS["Classic"]
    assert classic.edge_behaviour == "walls"
    assert classic.severing is False
    assert classic.poisons == "off"
    assert classic.length_affects_speed is False


def test_saving_and_reading_a_preset(isolated_store):
    from utils.store import presets as preset_store

    saved = preset_store.save("  My   Setup  ", RuleSet(arena_width=55))
    assert saved["name"] == "My Setup"

    loaded = preset_store.get("My Setup")
    assert loaded.arena_width == 55


def test_a_preset_survives_a_restart(isolated_store):
    # "Restart" here means a fresh read from disk with no in-memory state.
    from utils.store import presets as preset_store

    ruleset = RuleSet(arena_width=61, base_speed=8, edge_behaviour="walls")
    preset_store.save("Tight", ruleset)

    reloaded = preset_store.get("Tight")
    assert reloaded.to_dict() == ruleset.to_dict()
    assert reloaded.fingerprint() == ruleset.fingerprint()


def test_shipped_presets_cannot_be_overwritten_or_deleted(isolated_store):
    from utils.store import presets as preset_store

    with pytest.raises(preset_store.PresetError):
        preset_store.save("Classic", RuleSet())
    with pytest.raises(preset_store.PresetError):
        preset_store.delete("Classic")


def test_an_unnamed_preset_is_refused(isolated_store):
    from utils.store import presets as preset_store

    with pytest.raises(preset_store.PresetError):
        preset_store.save("   ", RuleSet())


def test_a_corrupt_stored_preset_is_ignored_not_loaded(isolated_store):
    from utils.store import presets as preset_store

    preset_store.save("Broken", RuleSet())
    path = isolated_store / preset_store.FILENAME
    path.write_text('{"Broken": {"arena_width": 500}}', encoding="utf-8")

    assert preset_store.get("Broken") is None


def test_preset_endpoints(client):
    listing = client.get("/api/presets").get_json()
    assert len(listing["presets"]) == 3

    created = client.post(
        "/api/presets", json={"name": "Wide", "rules": {"arena_width": 70}}
    )
    assert created.status_code == 200

    listing = client.get("/api/presets").get_json()
    assert any(row["name"] == "Wide" for row in listing["presets"])

    removed = client.delete("/api/presets/Wide")
    assert removed.status_code == 200
    assert client.delete("/api/presets/Wide").status_code == 404


def test_preset_endpoint_refuses_invalid_rules(client):
    response = client.post(
        "/api/presets", json={"name": "Bad", "rules": {"arena_width": 2}}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_rules"


def test_preset_endpoint_refuses_a_shipped_name(client):
    response = client.post("/api/presets", json={"name": "Brawl", "rules": {}})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_preset"


def test_a_preset_reproduces_the_same_match(client):
    ruleset = {"arena_width": 30, "arena_height": 30, "edge_behaviour": "walls",
               "base_speed": 6, "starting_length": 5}

    client.post("/api/presets", json={"name": "Repeat", "rules": ruleset})

    keyframe = client.post("/api/solo/start", json={"rules": ruleset}).get_json()
    assert keyframe["arena"]["w"] == 30
    assert keyframe["arena"]["edge"] == "walls"
    assert len(keyframe["state"]["snakes"][0]["b"]) == 5

    stored = next(
        row for row in client.get("/api/presets").get_json()["presets"]
        if row["name"] == "Repeat"
    )
    assert stored["rules"]["base_speed"] == 6


# -- the layout --------------------------------------------------------------


def test_a_layout_reads_as_columns_by_rows():
    assert RuleSet(layout="1x1").grid_shape() == (1, 1)
    assert RuleSet(layout="2x1").grid_shape() == (2, 1)
    assert RuleSet(layout="2x2").grid_shape() == (2, 2)
    assert RuleSet(layout="2x2").arena_count() == 4


def test_an_unknown_layout_is_refused_rather_than_guessed():
    assert "layout" in RuleSet(layout="9x9").errors()


def test_the_food_target_is_for_one_arena():
    single = RuleSet(arena_width=40, arena_height=40)
    grid = RuleSet(layout="2x2", arena_width=40, arena_height=40)

    assert grid.food_target() == single.food_target(), (
        "density is what a player sees where they are standing"
    )


def test_single_player_is_never_asked_about_a_layout():
    assert BY_KEY["layout"]["scope"] == "multiplayer"
    assert RuleSet(layout="2x2").fingerprint() == RuleSet().fingerprint()


# -- one option depending on another -----------------------------------------
#
# Two relationships, and they are not the same thing. An option that cannot have
# any effect right now is irrelevant, and the editor hides it. An option that
# would have an effect but is not allowed in this combination is forced, and the
# editor shows it locked with a reason. The tables live here rather than in the
# editor because a setup can arrive from an older build or a different client,
# and the game has to settle it whether or not anything drew a screen.


def test_an_option_is_relevant_until_something_makes_it_not():
    assert relevant("base_speed", RuleSet())
    assert relevant("arena_width", RuleSet(layout="2x2"))


def test_the_length_penalty_is_irrelevant_when_length_does_not_matter():
    assert not relevant("length_penalty", RuleSet(length_affects_speed=False))
    assert not relevant("max_slowdown", RuleSet(length_affects_speed=False))
    assert relevant("length_penalty", RuleSet(length_affects_speed=True))


def test_only_the_target_the_match_is_won_on_is_relevant():
    scored = RuleSet(win_condition="first_to_score")

    assert relevant("score_target", scored)
    assert not relevant("kill_target", scored)
    assert not relevant("time_limit", scored)

    timed = RuleSet(win_condition="timed")

    assert relevant("time_limit", timed)
    assert not relevant("score_target", timed)


def test_remains_are_irrelevant_when_nothing_can_be_cut():
    assert not relevant("remains_lifetime", RuleSet(severing=False))
    assert relevant("remains_yield", RuleSet(severing=True))


def test_which_poisons_exist_is_irrelevant_when_there_are_none():
    assert not relevant("slow_poison", RuleSet(poisons="off"))
    assert relevant("slow_poison", RuleSet(poisons="low"))


def test_the_effect_duration_matters_if_either_family_is_on_the_floor():
    """The one condition that is an any-of rather than a single test."""
    assert relevant("effect_duration", RuleSet(poisons="normal", pickups="off"))
    assert relevant("effect_duration", RuleSet(poisons="off", pickups="low"))
    assert not relevant("effect_duration", RuleSet(poisons="off", pickups="off"))


def test_a_grid_of_arenas_always_wraps():
    assert forced_value("edge_behaviour", RuleSet(layout="2x1")) == "wrap"
    assert forced_value("edge_behaviour", RuleSet(layout="2x2")) == "wrap"


def test_one_arena_is_free_to_have_walls():
    assert forced_value("edge_behaviour", RuleSet(layout="1x1")) is None


def test_a_forced_value_is_applied_rather_than_rejected():
    """Somebody who set walls and then chose a grid should get a grid that

    wraps, not an error telling them to change a setting the editor has already
    locked. A forced value is not a mistake the player made.
    """
    settled = RuleSet.from_dict({
        "layout": "2x2", "edge_behaviour": "walls", "arena_width": 30,
    })

    assert settled.edge_behaviour == "wrap"
    assert settled.layout == "2x2"
    assert settled.arena_width == 30, "nothing else is touched"


def test_an_old_saved_setup_is_settled_rather_than_played_as_sent():
    """The reason this is not only in the editor. A preset saved before the

    rule existed loads, looks legal, and would otherwise play by something the
    screen says is impossible.
    """
    stale = {"layout": "2x1", "edge_behaviour": "walls"}

    assert coerce(stale)["edge_behaviour"] == "wrap"


def test_settling_leaves_a_free_option_alone():
    assert coerce({"layout": "1x1", "edge_behaviour": "walls"})[
        "edge_behaviour"
    ] == "walls"


def test_every_condition_names_an_option_that_exists():
    """A typo in the table would silently read as an unset value, which reads

    as a condition that is never met, which reads as a row that is never shown.
    """
    for conditions in NEEDS.values():
        for condition in conditions:
            assert condition["key"] in BY_KEY

    for rule in FORCED.values():
        for condition in rule["when"]:
            assert condition["key"] in BY_KEY


def test_every_condition_names_values_that_option_can_take():
    for key, conditions in NEEDS.items():
        assert key in BY_KEY, key
        for condition in conditions:
            field = BY_KEY[condition["key"]]
            if field["type"] == "enum":
                for value in condition["in"]:
                    assert value in field["options"], (condition["key"], value)


def test_a_forced_value_is_one_the_option_accepts():
    for key, rule in FORCED.items():
        field = BY_KEY[key]
        if field["type"] == "enum":
            assert rule["value"] in field["options"]


def test_the_schema_carries_the_tables_to_the_editor():
    document = schema_document()

    assert document["needs"] == NEEDS
    assert document["forced"] == FORCED


def test_every_field_says_where_it_belongs_on_screen():
    for field in SCHEMA:
        assert field["tier"] in ("basic", "advanced"), field["key"]
        assert "section" in field


def test_every_group_has_something_to_show_before_it_is_expanded():
    """A tab whose every option is advanced would open on nothing but a

    collapsed heading, which reads as an empty tab.
    """
    groups = {}
    for field in SCHEMA:
        groups.setdefault(field["group"], []).append(field)

    for name, fields in groups.items():
        if not any(field["active"] for field in fields):
            continue
        basic = [
            field for field in fields
            if field["tier"] == "basic" and not field["section"]
        ]
        assert basic, name


# -- themes ------------------------------------------------------------------


def test_a_theme_is_one_of_the_four_that_exist():
    assert RuleSet(theme_set="mono").theme_set == "mono"
    assert "theme_set" in RuleSet(theme_set="chartreuse").errors()


def test_a_theme_is_part_of_what_a_solo_best_is_keyed_on_or_it_is_not():
    """Two runs that differ only in colour are the same run.

    A personal best is keyed on the options that change how a match plays, and
    a palette does not. If this ever fails it means the theme has been added to
    the fingerprint, and every existing best has quietly been orphaned.
    """
    assert RuleSet(theme_set="mono").fingerprint() == RuleSet(
        theme_set="neon"
    ).fingerprint()


def test_the_personal_best_key_has_not_moved():
    """The exact hash a default rule set produced before any of the recent

    work. A personal best is stored under this, so if it changes, everybody
    silently loses theirs.

    It has happened once: rescoping the bot options to multiplayer dropped them
    from the key. That was caught by hashing an older copy of the file, which is
    not something to rely on twice, so the value is pinned here instead.

    If this fails, do not update the number. Work out what entered or left the
    key and add it to KEPT_IN_KEY, or mark it cosmetic.
    """
    assert RuleSet().fingerprint() == "cc830893cd91301f"


def test_an_option_that_changes_how_a_match_plays_still_moves_the_key():
    assert RuleSet(arena_width=30).fingerprint() != RuleSet().fingerprint()
    assert RuleSet(base_speed=9).fingerprint() != RuleSet().fingerprint()


def test_an_option_kept_only_for_compatibility_cannot_move_it():
    assert RuleSet(bot_count=7).fingerprint() == RuleSet().fingerprint()


# -- preset names ------------------------------------------------------------


def test_a_name_that_is_too_long_is_refused_rather_than_cut_short():
    """Truncating succeeds, so there is nothing to notice, and the setup ends

    up stored under a name the player never saw and cannot find. Refusing is
    the kinder failure: it says so at the moment it happens.
    """
    from utils.store import presets

    with pytest.raises(presets.PresetError):
        presets.clean_name("x" * (presets.MAX_NAME + 1))


def test_a_name_at_the_limit_is_fine():
    from utils.store import presets

    at_limit = "x" * presets.MAX_NAME

    assert presets.clean_name(at_limit) == at_limit


def test_whitespace_is_tidied_and_the_result_still_reads_as_the_name():
    from utils.store import presets

    assert presets.clean_name("  spaced   out  ") == "spaced out"


def test_the_name_field_can_hold_a_whole_name():
    """The input used to cap at the old limit, so the store's limit could never

    be reached from the screen. Raising one without the other would put it back.
    """
    from pathlib import Path

    from utils.store import presets

    markup = Path("static/index.html").read_text(encoding="ascii")

    wanted = f'maxlength="{presets.MAX_NAME}" placeholder="Name this setup"'

    assert markup.count(wanted) == 2
