# How Snakeee is built

A tour of the repository for anyone who wants to change something. It covers
what each directory holds, what every script does, and the handful of decisions
that are load bearing: the ones where the obvious change breaks something
subtle.

`CONTRIBUTING.md` covers the mechanics of sending a patch. This file covers what
you are patching.

---

## The shape of it

The game is a local web application in a native window. A Flask server runs on
loopback, `pywebview` opens a window pointed at it, and the whole interface is
plain HTML, CSS and JavaScript with no build step and no framework. Editing a
file under `static/` and reloading is the entire development loop.

    main.py          starts the server, then opens the window
    app.py           the Flask app: every HTTP route, and the session object
                     that holds whichever mode you are in
    utils/           all of the Python
    static/          all of the interface
    tests/           pytest
    tools/           development checks, none of it imported by the game
    build.py         Nuitka build, and the Debian packaging on Linux
    installer/       the Inno Setup script for the Windows setup

Multiplayer does not go through Flask. The host opens a WebSocket server on its
own port and players connect to that directly; the Flask server is only ever
talking to the window on the same machine.

---

## utils/game: the simulation

Nothing in here knows about the network, and that is deliberate. It is what
lets the same code run a single player game and a twelve player match.

| file | lines | what it is |
|---|---|---|
| `arena.py` | 188 | the grid, and `Topology`, which answers which arena a cell belongs to |
| `rules.py` | 773 | all 47 options: types, defaults, dependencies, validation, and the fingerprint that keys a personal best |
| `engine.py` | 544 | `SoloEngine`, and `TickLoop`, the thread that drives both engines |
| `match.py` | 2663 | `MatchEngine`: the host authoritative multiplayer simulation, snapshots and deltas |
| `items.py` | 126 | food, poisons and pickups: what spawns and where |
| `effects.py` | 151 | what an item does to a snake, and the one place speed is resolved |
| `bots.py` | 543 | computer controlled snakes: three tiers and an aggression setting |

`match.py` is the largest file in the project and the one where mistakes are
most expensive. Read the section on invariants below before changing it.

## utils/net: rooms and the wire

| file | lines | what it is |
|---|---|---|
| `protocol.py` | 321 | every message type, the version number, and the reasons a request can be refused |
| `room.py` | 440 | the lobby: players, spectators, colours, ready flags, whether a match can start |
| `server.py` | 1012 | the host: connections, dispatch, the broadcast loop, rate limiting |
| `client.py` | 454 | the joining side: the socket, and the local view of the match |
| `discovery.py` | 503 | the UDP beacon that puts a public room in everyone's room list |
| `joincode.py` | 297 | the five character code, which carries an address rather than being looked up |

## utils/store: what persists

Profile, saved rule presets and personal bests, as JSON under the platform's
own application data directory. `paths.py` is the only file that knows where
that is.

## static: the interface

No framework, no bundler, no transpiler. ES modules loaded directly by the
browser engine.

| file | lines | what it is |
|---|---|---|
| `js/match.js` | 1394 | the multiplayer board: input, prediction, camera, HUD, results |
| `js/multiplayer.js` | 1010 | room list, joining, the lobby |
| `js/render.js` | 788 | everything drawn to the canvas, for both modes |
| `js/solo.js` | 759 | the single player board |
| `js/rules.js` | 602 | the rule editor, built from the schema the server sends |
| `js/splash.js` | 420 | the launch animation |
| `js/presets.js`, `colours.js`, `main.js`, `view.js`, `nav.js`, `input.js`, `screens.js` | | shell, navigation and shared pieces |

`css/tokens.css` holds the colours and spacing as custom properties; the other
five stylesheets use them and define nothing of their own.

If you are adding a rule option, the editor is generated from the schema in
`rules.py`. You add it there, and `tools/check_rules_ui.py` will tell you if the
editor and the validator disagree about it.

---

## tools: the checks

None of this is imported by the game, and all of it can be deleted before a
release without affecting anything.

| script | needs | what it checks |
|---|---|---|
| `verify.ps1` | PowerShell | runs everything below, in the order that fails fastest |
| `check_ascii.py` | | the two repository rules, and what the pre-commit hook calls |
| `check_match.py` | | starts three copies of the game and plays real matches through them |
| `check_view.mjs` | node | the board window arithmetic: which cells are on screen and where |
| `check_palettes.mjs` | node | contrast, arena patterns, and that no two themes are the same theme |
| `check_rules_ui.py` + `.mjs` | node | the editor and the validator agree, over 1786 answers |
| `check_editor.mjs` | node + jsdom | the real editor in a real DOM |
| `hooks/pre-commit` | | installed with `git config core.hooksPath tools/hooks` |

`check_match.py` is the important one. It starts three separate processes,
hosts a room from the first, joins from the second, watches from the third, and
plays five matches. Almost every bug that has mattered in this project passed
the unit tests and was caught here: a snapshot builder that marked state as
already sent, an owner grid that overflowed a byte, bots that never attacked,
aggression that ran backwards, and a refusal that disconnected the client it was
refusing.

If you change anything under `utils/net/`, run it.

---

## Decisions that are load bearing

Each of these cost something to arrive at, and each has a plausible looking
change that breaks it.

**The host is authoritative for everything.** A client sends a desired heading
with a sequence number and the move it was meant for, and nothing else. It
never sends a position.

**Progress is measured in time, not ticks.** Seconds since the last move,
rather than ticks accumulated against ticks needed. This is why the game stays
smooth without depending on the operating system's timer resolution.

**Other players are interpolated, not extrapolated,** at a moment slightly in
the past. Your own snake is the exception and is predicted locally.

**Never replay a backlog.** A stalled link delivers a clump of messages at
once. Fold them and draw only the fold; replaying them makes every other snake
sprint.

**Several arenas are one cell space, not several.** `Topology` is a reading of a
coordinate, not a collection of objects. This is why wrapping between arenas
needed no new code, and why severing, ownership and food spawning were never
reopened when the grid arrived.

**Which arena somebody is in is public. Where in that arena they are is not.**
Every message carries every snake's name, colour, score, lives and arena; bodies
and food only for the arena the reader is in. Take any later question about the
layout back to this sentence. A spectator is routed the stream of the player it
is watching for exactly this reason: one implementation of the rule, not two.

**Building a message is not the same as sending one.** The snapshot builder
records nothing. When it did, anything that changed between two of the host's
frames was marked as already told and never sent.

**A passing snake does not take a cell it entered.** Whoever was there first
keeps it. Otherwise the passer clears a cell the other snake is still standing
in, and that snake is intangible there for the rest of the match.

**Everything an item does is a change to a number that already exists.** No
second speed system, no second economy. `effects.py` resolves speed and is the
only place that does.

**A bot has no privileges.** It returns a heading, and the engine applies it
through the same path a key press takes.

**A refusal is not a disconnection.** Only the reasons in
`protocol.CLOSING_REASONS` end a connection. Everything else refuses one request
and leaves the socket alone.

---

## The two repository rules

Both are enforced by `tools/check_ascii.py`, which the pre-commit hook runs.

**Every text file is pure 7-bit ASCII.** No em dashes, no curly quotes, no
ellipsis characters, no arrows, no emoji, no accented characters, anywhere,
including comments and commit messages.

**The game name appears only in `utils/branding.py`.** The frontend reads it
from `/api/branding`. This is what makes the project renameable in one file.

Five files are CRLF and must stay CRLF: `static/js/screens.js`,
`static/js/solo.js`, `utils/net/protocol.py`, `utils/net/room.py` and
`static/fonts/SpaceGrotesk-OFL.txt`. After any patch that touches them, check
with `git diff --stat --ignore-all-space`: a file that flipped shows every line
as changed while showing no real change.

---

## Testing multiplayer by hand

Two terminals on one machine works for most things:

    python main.py
    python main.py

The second instance finds the first port taken and moves to the next one. Host
from one, join from the other.

What one machine cannot show you is a real link. The things that only appear
over WiFi are input feel when the connection stalls, whether the staleness
readout above the board is honest, and whether a player is ever disconnected for
sending too much. If you are changing anything about timing or the transport,
test across two machines.

---

## Where to start

- **A rule option.** `utils/game/rules.py`, then run
  `python tools/check_rules_ui.py`.
- **Anything visual.** `static/css/tokens.css` and `static/js/render.js`. The
  palettes have a check: `node tools/check_palettes.mjs`.
- **Bots.** `utils/game/bots.py` is self contained and reads a view of the board
  it cannot modify.
- **The lobby or the room list.** `static/js/multiplayer.js` and
  `utils/net/room.py`.
- **The protocol.** Read the invariants above first, then run
  `python tools/check_match.py` before and after.
