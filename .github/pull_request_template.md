## What this changes

<!-- One or two sentences. If it fixes an issue, write "Fixes #123". -->

## Before you open this

Run these from the project root. They are the Development section of the
README, and CI runs the last three of them on every pull request.

    pip install -e ".[dev]"
    git config core.hooksPath tools/hooks
    pytest -q
    ruff check .
    python tools/check_ascii.py

The second line installs the pre-commit hook. It is not installed by cloning,
so if you have never run it, nothing has been checking your commits locally and
the first thing you will hear about a problem is CI failing.

- [ ] `pytest -q` passes
- [ ] `ruff check .` reports nothing
- [ ] `python tools/check_ascii.py` passes both rules
- [ ] I ran `git config core.hooksPath tools/hooks` in this clone

## The two repository rules

Both are unusual, both are enforced, and both are easy to break without
noticing. They are stated here rather than linked because a contributor who
skipped the hook is exactly the one who has not read CONTRIBUTING.md.

1. **Every text file is pure 7-bit ASCII.** No em dashes, no curly quotes, no
   ellipsis characters, no arrows, no emoji, no accented characters, anywhere,
   including comments and commit messages. Editors that "smarten" quotes will
   break this silently.
2. **The game name appears only in `utils/branding.py`.** Not in any other
   `.py`, `.js`, `.css` or `.html` file. The frontend reads it from
   `/api/branding`.

- [ ] I have not added a non-ASCII character anywhere
- [ ] I have not hardcoded the game name outside `utils/branding.py`

## If you touched the network or the match engine

`python tools/check_match.py` starts three copies of the game and plays real
matches through them. Almost every bug that has mattered in this project passed
the unit tests and was caught there.

- [ ] Not applicable
- [ ] `python tools/check_match.py` reports all checks passing

## Anything you could not test

<!-- Say so plainly. "I could not test this on Windows" is useful; silence is
     not. Nobody here has every platform. -->
