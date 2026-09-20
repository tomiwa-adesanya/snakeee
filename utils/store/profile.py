"""Profile persistence: username and colour, and nothing else.

Multiplayer stores nothing else about a player, which is the whole reason this
can be a few hundred bytes of JSON with no schema migration story.

A corrupt file must never stop the game launching. On a parse failure the file
is backed up and defaults are returned.
"""

import json
import logging
import random

from utils.store import paths

FILENAME = "profile.json"
MAX_USERNAME = 16

# The twelve player colours. Chosen to stay distinguishable against the arena
# background and from each other.
PLAYER_COLOURS = [
    "#ef6461", "#f0b950", "#3ecf8e", "#58a6ff", "#c878f0", "#f08fb4",
    "#5b7cfa", "#6fd8d0", "#d9d36a", "#ff9d5c", "#9be86a", "#e0e4ef",
]

log = logging.getLogger("store.profile")


def _path():
    return paths.app_data_dir() / FILENAME


def default_profile() -> dict:
    return {
        "username": "Player",
        "colour": random.choice(PLAYER_COLOURS),
    }


def load() -> dict:
    path = _path()
    if not path.exists():
        return default_profile()

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        backup = path.with_suffix(".json.corrupt")
        try:
            path.replace(backup)
            log.warning("profile was unreadable, moved to %s", backup)
        except OSError:
            log.exception("profile was unreadable and could not be moved aside")
        return default_profile()

    profile = default_profile()
    if isinstance(data, dict):
        profile.update(sanitise(data))
    return profile


def sanitise(data: dict) -> dict:
    cleaned = {}

    username = data.get("username")
    if isinstance(username, str) and username.strip():
        cleaned["username"] = username.strip()[:MAX_USERNAME]

    colour = data.get("colour")
    if isinstance(colour, str) and colour in PLAYER_COLOURS:
        cleaned["colour"] = colour

    return cleaned


def save(data: dict) -> dict:
    profile = load()
    profile.update(sanitise(data))
    paths.atomic_write_text(_path(), json.dumps(profile, indent=2) + "\n")
    return profile
