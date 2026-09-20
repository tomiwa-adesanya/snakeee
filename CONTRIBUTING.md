# Contributing

Contributions are welcome. Read this file first; two of the rules here are
unusual, and the pre-commit hook will reject a commit that breaks either of
them.

[ARCHITECTURE.md](ARCHITECTURE.md) is the other half: what every directory
holds, what each script in `tools/` checks, and the decisions that are load
bearing. Worth reading before your first change, and required before changing
anything under `utils/net/` or `utils/game/match.py`.

## Setup

    pip install -e ".[dev]"
    git config core.hooksPath tools/hooks
    python main.py

The second command installs the pre-commit hook, which is where the two
repository rules below are enforced.

## Rule 1: pure ASCII, everywhere

Every text file in this repository must be 7-bit ASCII. Banned characters
include:

- em dash and en dash: write a plain hyphen, or restructure the sentence
- curly quotes and curly apostrophes: write `'` and `"`
- the single-character ellipsis: write three periods
- arrows, bullet characters, box-drawing characters, non-breaking spaces,
  zero-width characters
- emoji, anywhere, including commit messages
- accented characters, including in names

This covers source, comments, docstrings, HTML, CSS, JS, JSON, Markdown, commit
messages and pull request descriptions. Binary assets are not scanned.

Editors insert these characters silently. Turn off smart quotes and smart
dashes rather than trusting yourself to notice.

Check locally:

    python tools/check_ascii.py --ascii

## Rule 2: the game name lives in one file

`utils/branding.py` is the only `.py`, `.js`, `.css` or `.html` file allowed to
contain the game's name, so that renaming it stays a one-file change.

- Python: `from utils import branding`, then `branding.APP_NAME`.
- Frontend: fetch `/api/branding`, or put the value into an element that
  carries `data-app-name` and let `main.js` fill it in.
- Never build a window title, a file path or a log line by concatenating a
  literal name.

Check locally:

    python tools/check_ascii.py --name-scan

## Comments

Write comments for someone who has never seen the project before. That means:

- The reason a piece of code is the way it is, when the reason is not obvious
  from reading it, and especially where the obvious approach is wrong.
- Constraints that are not visible locally: what another module assumes, what a
  library does that surprised you, what breaks if this changes.
- Units, ranges and invariants that the code itself does not carry.

Do not write:

- Restatements of the code. `# increment the counter` above `counter += 1`.
- Project history. What changed, when, and what it used to do. The commit log and
  the changelog are for that.
- Plans. A comment saying a feature is coming is a promise nobody is holding, and
  it outlives the plan. Where an extension point genuinely shapes the code, say
  what the code allows, not what anyone intends to do.
- References to documents that are not in this repository.

## Style

- Python is formatted to what `ruff` accepts with the settings in
  `pyproject.toml`. Run `ruff check .` before pushing.
- JavaScript is plain ES modules. There is no bundler, no TypeScript and no
  build step for the frontend, and adding one is a blueprint change rather
  than a pull request.
- CSS uses the tokens in `static/css/tokens.css`. Do not introduce a raw hex
  colour in a component stylesheet; add a token if one is genuinely missing.
- Never use `alert()`, `confirm()` or `prompt()`. Every dialog is a styled
  modal.
- Render all player-supplied text with `textContent`, never `innerHTML`.

## Dependencies

The runtime dependency list is fixed at four packages and adding a fifth needs
a strong argument. Every dependency is one a contributor has to install and one
Nuitka has to package correctly on three operating systems.

## Before opening a pull request

    python tools/check_ascii.py
    ruff check .
    pytest -q

Describe what you changed and why. If the change touches the game rules, say
what it does to an existing match in progress; the answer is often the reason a
change is harder than it looks.
