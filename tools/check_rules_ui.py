"""Check that the rules editor and the rules validator agree.

Run it from the project root:

    python tools/check_rules_ui.py

Which options are irrelevant given the rest of a setup, and which are pinned by
another setting, is answered in two places: in `static/js/rules.py`'s sibling
`rules.js` so the editor can hide and lock rows, and in `utils/game/rules.py` so
a setup arriving from an older build or a different client is settled rather
than played as sent.

Both read the same table, but they are separate implementations of reading it,
and separate implementations drift. When they do, the editor shows something the
game will not honour, which is the worst kind of wrong because it looks like it
worked.

This generates a spread of rule sets, answers both questions for every option in
each of them using the game's own functions, and hands the lot to node to answer
again with the editor's. Any disagreement is reported with both answers.

Needs node, like `tools/check_view.mjs`. Nothing else in the project does, and
this whole directory goes before a release.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.game import rules as rules_module  # noqa: E402


def cases() -> list:
    """Rule sets worth asking about.

    Chosen rather than generated at random: every condition in the table, on
    both sides, plus the combinations where two of them interact. A random
    sweep would mostly produce setups where nothing is conditional at all.
    """
    base = rules_module.DEFAULTS.to_dict()

    def variant(name, **overrides):
        settings = dict(base)
        settings.update(overrides)
        return (name, settings)

    return [
        variant("the defaults"),

        variant("one arena, walls", layout="1x1", edge_behaviour="walls"),
        variant("a row of two", layout="2x1", edge_behaviour="walls"),
        variant("a grid of four", layout="2x2", edge_behaviour="wrap"),

        variant("length does not affect speed", length_affects_speed=False),
        variant("length affects speed", length_affects_speed=True),

        variant("last one standing", win_condition="last_standing"),
        variant("first to a score", win_condition="first_to_score"),
        variant("first to a number of kills", win_condition="first_to_kills"),
        variant("on the clock", win_condition="timed"),
        variant("endless", win_condition="endless"),

        variant("no severing", severing=False),
        variant("severing", severing=True),

        variant("nothing on the floor", poisons="off", pickups="off"),
        variant("poisons only", poisons="normal", pickups="off"),
        variant("pickups only", poisons="off", pickups="high"),
        variant("both", poisons="high", pickups="high"),

        variant(
            "a walled grid with nothing on the floor",
            layout="2x2", edge_behaviour="walls",
            poisons="off", pickups="off",
        ),
        variant(
            "everything at once",
            layout="2x2", edge_behaviour="walls", win_condition="timed",
            severing=True, length_affects_speed=True,
            poisons="high", pickups="high",
        ),
    ]


def main() -> int:
    schema = rules_module.schema_document()
    keys = [field["key"] for field in rules_module.SCHEMA]

    payload = {"schema": schema, "cases": []}

    for name, settings in cases():
        payload["cases"].append({
            "name": name,
            "rules": settings,
            "relevant": {
                key: rules_module.relevant(key, settings) for key in keys
            },
            "forced": {
                key: rules_module.forced_value(key, settings) for key in keys
            },
        })

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "check_rules_ui.mjs")

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="ascii"
    )
    try:
        json.dump(payload, handle)
        handle.close()

        try:
            finished = subprocess.run(
                ["node", script, handle.name],
                cwd=root,
                check=False,
            )
        except FileNotFoundError:
            print("node is not installed, so this check cannot run.")
            print("It is not needed to play the game, build it, or test it.")
            return 0

        return finished.returncode
    finally:
        os.unlink(handle.name)


if __name__ == "__main__":
    raise SystemExit(main())
