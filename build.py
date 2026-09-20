"""
build.py - Compile the game into a standalone distributable binary.

Run on the target platform (each OS produces its own binary):

    python build.py

On Linux it offers a .deb as well as the plain binary. The .deb puts the game
in the applications menu with its icon and declares the webview packages pip
cannot install, so it is the friendlier of the two for anyone who is not going
to read a README.

Prerequisites:
    pip install nuitka
    - Windows : Visual Studio Build Tools (C++ workload) or MinGW64
    - macOS   : Xcode Command Line Tools  (xcode-select --install)
    - Linux   : gcc + patchelf            (apt install gcc patchelf)
    - .deb    : dpkg-deb                  (apt install dpkg-dev)
"""

import os
import platform
import shutil
import subprocess
import sys
import textwrap

from app import APP_VERSION
from utils.branding import APP_NAME, BINARY_NAME

MAINTAINER = "Tomiwa Adesanya <a.tomiwa.tech@gmail.com>"
SUMMARY = "LAN multiplayer snake"
DESCRIPTION = (
    "One person hosts, reads out a short code, and everyone else on the same "
    "network joins. The host writes the rules of the match. No account, no "
    "server, no internet connection."
)
CATEGORIES = "Game;ArcadeGame;"
ICON_PNG = "static/icons/app/icon_256.png"
ICON_ICO = "static/icons/app/icon.ico"

# The webview packages pip cannot install. Listed here so that apt pulls them in
# and the window opens on a machine that has never run the game, rather than
# failing with a message about a missing backend.
DEPENDS = (
    "python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, "
    "gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0"
)


def run_build():
    system = platform.system()
    print(f"Building {APP_NAME} v{APP_VERSION} for {system} ...")

    cmd = [
        sys.executable, "-m", "nuitka",

        # -- Output mode ------------------------------------------
        "--standalone",                     # Bundle interpreter + deps.
        "--onefile",                        # Single self-extracting binary.

        # -- Data files -------------------------------------------
        "--include-data-dir=./static=static",

        # -- Imports reached late or by name ----------------------
        "--include-module=waitress",
        "--include-module=websockets.asyncio.server",
        "--include-module=websockets.asyncio.client",

        "--nofollow-import-to=tkinter",

        # -- Binary name + metadata -------------------------------
        f"--output-filename={BINARY_NAME}",
        f"--product-version={APP_VERSION}",
        f"--product-name={APP_NAME}",
    ]

    # -- Platform-specific flags ----------------------------------

    if system == "Windows":
        # Hide the console window on launch.
        cmd.append("--windows-console-mode=disable")
        # Embed the app icon into the .exe metadata and taskbar.
        cmd.append(f"--windows-icon-from-ico={ICON_ICO}")

    elif system == "Darwin":
        cmd.append(f"--macos-app-icon={ICON_PNG}")

    elif system == "Linux":
        # No console flag needed on Linux. Set the icon for desktop entries.
        cmd.append(f"--linux-icon={ICON_PNG}")

    # -- Entry point ----------------------------------------------
    cmd.append("main.py")

    print("Running:")
    print("  " + " \\\n    ".join(cmd))
    print()

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nBuild failed (exit code {result.returncode}).", file=sys.stderr)
        sys.exit(result.returncode)

    ext = ".exe" if system == "Windows" else ""
    binary = f"{BINARY_NAME}{ext}"
    if not os.path.exists(binary):
        print(f"\nBuild reported success but {binary} is not here.",
              file=sys.stderr)
        sys.exit(1)

    print(f"\nBuild succeeded.  Binary: {binary}  (v{APP_VERSION})")
    return binary


def build_deb(binary):
    """Assemble a .deb from the compiled Linux binary."""
    print("\nBuilding .deb package ...")

    root = f"dist/{BINARY_NAME}_{APP_VERSION}"
    bin_dir = f"{root}/usr/local/bin"
    desktop_dir = f"{root}/usr/share/applications"
    icon_dir = f"{root}/usr/share/icons/hicolor/256x256/apps"
    debian_dir = f"{root}/DEBIAN"

    for directory in (bin_dir, desktop_dir, icon_dir, debian_dir):
        os.makedirs(directory, exist_ok=True)

    shutil.copy2(binary, f"{bin_dir}/{BINARY_NAME}")
    os.chmod(f"{bin_dir}/{BINARY_NAME}", 0o755)
    print(f"  binary   -> {bin_dir}/{BINARY_NAME}")

    shutil.copy2(ICON_PNG, f"{icon_dir}/{BINARY_NAME}.png")
    print(f"  icon     -> {icon_dir}/{BINARY_NAME}.png")

    # StartupWMClass is what lets the running window match this launcher, so
    # the taskbar shows one icon rather than the launcher and a generic entry.
    desktop = textwrap.dedent(f"""\
        [Desktop Entry]
        Version=1.0
        Type=Application
        Name={APP_NAME}
        GenericName={SUMMARY}
        Comment={DESCRIPTION}
        Exec=/usr/local/bin/{BINARY_NAME}
        Icon={BINARY_NAME}
        Categories={CATEGORIES}
        Terminal=false
        StartupNotify=true
        StartupWMClass={BINARY_NAME}
    """)
    with open(f"{desktop_dir}/{BINARY_NAME}.desktop", "w") as handle:
        handle.write(desktop)
    print(f"  desktop  -> {desktop_dir}/{BINARY_NAME}.desktop")

    # The leading space on the continuation line is required: it is how the
    # Debian control format marks the long description.
    control = textwrap.dedent(f"""\
        Package: {BINARY_NAME}
        Version: {APP_VERSION}
        Section: games
        Priority: optional
        Architecture: amd64
        Depends: {DEPENDS}
        Maintainer: {MAINTAINER}
        Description: {SUMMARY}
         {DESCRIPTION}
    """)
    with open(f"{debian_dir}/control", "w") as handle:
        handle.write(control)
    print(f"  control  -> {debian_dir}/control")

    postinst = textwrap.dedent("""\
        #!/bin/bash
        gtk-update-icon-cache /usr/share/icons/hicolor/ 2>/dev/null || true
        update-desktop-database /usr/share/applications/ 2>/dev/null || true
    """)
    with open(f"{debian_dir}/postinst", "w") as handle:
        handle.write(postinst)
    os.chmod(f"{debian_dir}/postinst", 0o755)
    print(f"  postinst -> {debian_dir}/postinst")

    output = f"dist/{BINARY_NAME}_{APP_VERSION}_amd64.deb"
    result = subprocess.run(["dpkg-deb", "--build", root, output])
    if result.returncode != 0:
        print("\ndpkg-deb failed. The plain binary is still usable.",
              file=sys.stderr)
        return None

    shutil.rmtree(root, ignore_errors=True)
    print(f"\nPackage ready: {output}")
    print(f"  install with: sudo apt install ./{output}")
    return output


def ask_deb():
    """On Linux, ask whether to package the binary as well."""
    if platform.system() != "Linux":
        return False

    if shutil.which("dpkg-deb") is None:
        print("dpkg-deb is not installed, so only the plain binary can be")
        print("built. Install it with: sudo apt install dpkg-dev\n")
        return False

    print()
    print("  [1] Plain binary only (default)")
    print("  [2] Plain binary and a .deb package")
    print()
    return input("Choose output [1/2]: ").strip() == "2"


if __name__ == "__main__":
    want_deb = ask_deb()
    built = run_build()
    if want_deb:
        os.makedirs("dist", exist_ok=True)
        build_deb(built)
