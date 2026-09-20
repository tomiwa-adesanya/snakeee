"""The game's display name, in one place.

This is the only Python, JS, CSS or HTML file permitted to contain the name as a
literal. Everything else imports from here, and the frontend reads it from
/api/branding. A rename is therefore this file plus the documentation.

Three identifiers deliberately do not follow the name, because they have to
survive one:

  - the discovery magic used to recognise other copies on the network, so a
    renamed build still finds older ones
  - the installer application id, so Windows treats a rename as an upgrade
    rather than as a second program
  - the package directory names

Constants only. Anything needing a runtime decision belongs elsewhere, so that
this module is safe to import from anywhere.
"""

APP_NAME = "Snakeee"
APP_SLUG = "snakeee"

WINDOW_TITLE = APP_NAME

DATA_DIR_NAME = "Snakeee"
DATA_DIR_NAME_POSIX = "snakeee"

BINARY_NAME = "snakeee"

TAGLINE = "LAN multiplayer snake"
