"""Repository checks that CI and the pre-commit hook both run.

Two checks:

  --ascii      Every shipped text file must be pure 7-bit ASCII.
               Em dashes, curly quotes, ellipsis characters, arrows, emoji and
               accented characters all fail.

  --name-scan  The game's display name must appear only in
               utils/branding.py, Markdown documentation and binary assets. A
               hit in any .py, .js, .css or .html file other than that one
               means the name has leaked back into the code and a rename would
               miss it.

This directory is not imported by the application. It can be deleted before a
public release without touching the source; the only other things that
reference it are the pre-commit hook and two tests that skip when it is absent.

Both run by default. Exit status is 0 on success and 1 on any finding, so this
is usable directly as a hook and as a CI step.

No third-party dependencies on purpose: a contributor must be able to run this
with a bare Python install.
"""

import argparse
import os
import re
import sys

TEXT_EXTENSIONS = (
    ".py", ".js", ".css", ".html", ".md", ".json",
    ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini", ".iss",
)

CODE_EXTENSIONS = (".py", ".js", ".css", ".html")

SKIP_DIRECTORIES = {
    ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
    "build", "dist", ".pytest_cache", ".ruff_cache", "static/fonts",
}


def is_virtualenv(path) -> bool:
    """True when this directory is a Python virtual environment.

    Detected by the marker file rather than by name. A virtual environment can
    be called anything, and every dependency inside one is somebody else's code
    with somebody else's punctuation in it, so scanning one produces thousands
    of findings about files nobody here can fix. Naming the environments we
    happen to know about would leave the next one to be discovered the same
    way, which is why this asks the directory what it is.
    """
    return os.path.isfile(os.path.join(path, "pyvenv.cfg"))

# The font licence files are third-party text shipped verbatim. They are not
# ours to reformat, so they are excluded from the ASCII check by path.
EXCLUDED_PATHS = {
    os.path.join("static", "fonts", "SpaceGrotesk-OFL.txt"),
    os.path.join("static", "fonts", "JetBrainsMono-OFL.txt"),
}

BRANDING_FILE = os.path.join("utils", "branding.py")


def repository_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def walk(root, extensions):
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [
            name for name in subdirectories
            if name not in SKIP_DIRECTORIES
            and not is_virtualenv(os.path.join(directory, name))
        ]
        for filename in filenames:
            if not filename.endswith(extensions):
                continue
            full = os.path.join(directory, filename)
            relative = os.path.relpath(full, root)
            if relative in EXCLUDED_PATHS:
                continue
            yield full, relative


def check_ascii(root):
    findings = []

    for full, relative in walk(root, TEXT_EXTENSIONS):
        with open(full, "rb") as handle:
            for number, line in enumerate(handle, start=1):
                for column, byte in enumerate(line, start=1):
                    if byte > 127:
                        findings.append(
                            f"{relative}:{number}:{column}: non-ASCII byte 0x{byte:02x}"
                        )
                        break

    return findings


def read_branding_names(root):
    path = os.path.join(root, BRANDING_FILE)
    if not os.path.exists(path):
        return []

    with open(path, encoding="utf-8") as handle:
        source = handle.read()

    names = []
    for key in ("APP_NAME", "APP_SLUG", "DATA_DIR_NAME", "DATA_DIR_NAME_POSIX",
                "BINARY_NAME"):
        match = re.search(key + r'\s*=\s*"([^"]+)"', source)
        if match:
            names.append(match.group(1))

    return sorted(set(names), key=len, reverse=True)


def check_name_leak(root):
    names = read_branding_names(root)
    if not names:
        return ["branding.py not found or contains no name constants"]

    patterns = [
        (name, re.compile(re.escape(name), re.IGNORECASE)) for name in names
    ]
    findings = []

    for full, relative in walk(root, CODE_EXTENSIONS):
        if relative == BRANDING_FILE:
            continue

        with open(full, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                for name, pattern in patterns:
                    if pattern.search(line):
                        findings.append(
                            f"{relative}:{number}: the game name '{name}' is "
                            "hardcoded here; import it from branding.py or "
                            "read it from /api/branding"
                        )
                        break

    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ascii", action="store_true", help="run the ASCII check only")
    parser.add_argument(
        "--name-scan", action="store_true", help="run the branding leak check only"
    )
    options = parser.parse_args(argv if argv is not None else sys.argv[1:])

    run_ascii = options.ascii or not options.name_scan
    run_names = options.name_scan or not options.ascii

    root = repository_root()
    failed = False

    if run_ascii:
        findings = check_ascii(root)
        if findings:
            failed = True
            print("FAIL: non-ASCII characters found")
            for finding in findings:
                print("  " + finding)
        else:
            print("PASS: every scanned text file is pure ASCII")

    if run_names:
        findings = check_name_leak(root)
        if findings:
            failed = True
            print("FAIL: the game name is hardcoded outside branding.py")
            for finding in findings:
                print("  " + finding)
        else:
            print("PASS: the game name appears only in branding.py")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
