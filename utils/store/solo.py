"""Solo state persistence.

the last used single-player RuleSet, personal bests
keyed by a hash of the ruleset, and the last 20 solo results. One JSON file, no
database.

Bests are keyed by ruleset fingerprint on purpose. A score of 40 on a 20x20
walled arena and a score of 40 on 80x80 with wrap are not the same achievement,
and a single global best would quietly reward whichever configuration is
easiest.
"""

import json
import logging
import time

from utils.store import paths

FILENAME = "solo.json"
MAX_RESULTS = 20

log = logging.getLogger("store.solo")


def _path():
    return paths.app_data_dir() / FILENAME


def _empty() -> dict:
    return {"last_rules": None, "bests": {}, "results": []}


def load() -> dict:
    path = _path()
    if not path.exists():
        return _empty()

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        backup = path.with_suffix(".json.corrupt")
        try:
            path.replace(backup)
            log.warning("solo state was unreadable, moved to %s", backup)
        except OSError:
            log.exception("solo state was unreadable and could not be moved aside")
        return _empty()

    state = _empty()
    if isinstance(data, dict):
        if isinstance(data.get("bests"), dict):
            state["bests"] = {
                str(key): int(value)
                for key, value in data["bests"].items()
                if isinstance(value, (int, float))
            }
        if isinstance(data.get("results"), list):
            state["results"] = data["results"][:MAX_RESULTS]
        if isinstance(data.get("last_rules"), dict):
            state["last_rules"] = data["last_rules"]

    return state


def _save(state: dict) -> None:
    paths.atomic_write_text(_path(), json.dumps(state, indent=2) + "\n")


def best_for(fingerprint: str) -> int:
    return int(load()["bests"].get(fingerprint, 0))


def remember_rules(rules: dict) -> None:
    state = load()
    state["last_rules"] = rules
    _save(state)


def last_rules():
    return load()["last_rules"]


def record_result(result: dict, rules: dict) -> dict:
    """Store a finished match. Returns the best score for this ruleset.

    Called by the server when the engine ends a match, not by the browser. The
    score therefore comes from the engine's own state and cannot be edited by
    anything the frontend sends.
    """
    fingerprint = result.get("rules_fingerprint", "")
    score = int(result.get("score", 0))

    state = load()
    previous = int(state["bests"].get(fingerprint, 0))
    best = max(previous, score)

    state["bests"][fingerprint] = best
    state["last_rules"] = rules
    state["results"].insert(
        0,
        {
            "at": int(time.time()),
            "score": score,
            "length": result.get("length"),
            "duration_seconds": result.get("duration_seconds"),
            "cause": result.get("cause"),
            "rules_fingerprint": fingerprint,
        },
    )
    state["results"] = state["results"][:MAX_RESULTS]

    _save(state)

    log.info("recorded solo result, score %s, best %s", score, best)
    return {"best": best, "is_new_best": score > previous and score > 0}


def history(fingerprint: str = None) -> dict:
    state = load()
    results = state["results"]
    if fingerprint:
        results = [
            row for row in results
            if row.get("rules_fingerprint") == fingerprint
        ]
    return {
        "results": results,
        "best": int(state["bests"].get(fingerprint, 0)) if fingerprint else 0,
    }
