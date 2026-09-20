"""Named rule presets.

every RuleSet is saveable as a named preset, and three
ship with the game.

The shipped presets are read only. A player who wants a variation on Classic
saves their own copy under a new name, which means an update that improves a
shipped preset is not silently overwritten by an old local copy, and a player
cannot end up with a "Classic" that is not classic.
"""

import json
import logging

from utils.game.rules import SHIPPED_PRESETS, RuleError, RuleSet
from utils.store import paths

FILENAME = "presets.json"
# Long enough for a real description of a setup. It was 24, which silently
# truncated "A rather long setup name for a room" to "A rather long setup name"
# and left the player looking for a name that was never stored.
MAX_NAME = 48
MAX_PRESETS = 50

log = logging.getLogger("store.presets")


class PresetError(ValueError):
    """A preset name or payload that cannot be stored."""


def _path():
    return paths.app_data_dir() / FILENAME


def _load_user() -> dict:
    path = _path()
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        backup = path.with_suffix(".json.corrupt")
        try:
            path.replace(backup)
            log.warning("presets were unreadable, moved to %s", backup)
        except OSError:
            log.exception("presets were unreadable and could not be moved aside")
        return {}

    if not isinstance(data, dict):
        return {}

    return {
        str(name): rules
        for name, rules in data.items()
        if isinstance(rules, dict)
    }


def _save_user(presets: dict) -> None:
    paths.atomic_write_text(_path(), json.dumps(presets, indent=2) + "\n")


def clean_name(name) -> str:
    """The name a setup will actually be stored under.

    Two things happen here that the player did not ask for: runs of whitespace
    are collapsed, and anything past the limit is cut off. Both are reasonable;
    doing them silently was not. A name that is quietly changed is a setup the
    player then cannot find, because they go looking for what they typed.

    Over-long names are now refused outright rather than trimmed. Truncating is
    the worse of the two: it succeeds, so there is nothing to notice, and it is
    exactly the case that produced a saved setup nobody could find. Collapsing
    whitespace stays silent because the result still reads as the name that was
    typed, and save() reports the stored name back either way.
    """
    if not isinstance(name, str):
        raise PresetError("a preset needs a name")

    cleaned = " ".join(name.split())
    if not cleaned:
        raise PresetError("a preset needs a name")
    if len(cleaned) > MAX_NAME:
        raise PresetError(
            f"that name is {len(cleaned)} characters; "
            f"keep it to {MAX_NAME} or fewer"
        )
    if cleaned in SHIPPED_PRESETS:
        raise PresetError(
            f"{cleaned} is a shipped preset and cannot be overwritten; "
            "save it under another name"
        )

    return cleaned


def listing() -> list:
    """Shipped presets first, then the player's own, alphabetically."""
    rows = [
        {"name": name, "builtin": True, "rules": ruleset.to_dict()}
        for name, ruleset in SHIPPED_PRESETS.items()
    ]

    for name in sorted(_load_user()):
        rows.append(
            {"name": name, "builtin": False, "rules": _load_user()[name]}
        )

    return rows


def get(name: str):
    if name in SHIPPED_PRESETS:
        return SHIPPED_PRESETS[name]

    stored = _load_user().get(name)
    if stored is None:
        return None

    # Stored presets are re-validated on read. A file edited by hand, or
    # written by an older version with different bounds, must not be able to
    # start a match the engine cannot run.
    try:
        return RuleSet.from_dict(stored)
    except RuleError:
        log.warning("stored preset %s is no longer valid and was ignored", name)
        return None


def save(name: str, ruleset: RuleSet) -> dict:
    cleaned = clean_name(name)
    presets = _load_user()

    if cleaned not in presets and len(presets) >= MAX_PRESETS:
        raise PresetError(
            f"you already have {MAX_PRESETS} presets; delete one first"
        )

    presets[cleaned] = ruleset.validate().to_dict()
    _save_user(presets)

    log.info("saved preset %s", cleaned)
    return {"name": cleaned, "builtin": False, "rules": presets[cleaned]}


def delete(name: str) -> bool:
    if name in SHIPPED_PRESETS:
        raise PresetError("shipped presets cannot be deleted")

    presets = _load_user()
    if name not in presets:
        return False

    del presets[name]
    _save_user(presets)

    log.info("deleted preset %s", name)
    return True
