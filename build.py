"""
build.py - Compile the game into a standalone distributable binary.

Run on the target platform (each OS produces its own binary):

    python build.py

On Linux it offers a .deb as well as the plain binary. The .deb puts the game
in the applications menu with its icon and declares the webview packages pip
cannot install, so it is the friendlier of the two for anyone who is not going
to read a README.

On macOS it builds an application bundle, dist/<name>.app, because Nuitka can
only compile the Cocoa webview backend into a bundle. There is no single file,
so it also writes a zip of the app and a setup .pkg beside it, which install it
into Applications.

Prerequisites:
    pip install nuitka
    - Windows : Visual Studio Build Tools (C++ workload) or MinGW64
    - macOS   : Xcode Command Line Tools  (xcode-select --install)
                pip install imageio       (Nuitka converts the PNG icon to .icns)
    - Linux   : gcc + patchelf            (apt install gcc patchelf)
    - .deb    : dpkg-deb                  (apt install dpkg-dev)
"""

import os
import platform
import plistlib
import shutil
import subprocess
import sys
import textwrap

from app import APP_VERSION
from utils.branding import APP_NAME, APP_SLUG, BINARY_NAME

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
# The Dock draws the macOS icon large, so it is converted from the biggest PNG.
ICON_PNG_MACOS = "static/icons/app/icon_1024.png"
ENTRY_POINT = "main.py"

# Where the macOS app bundle is written. dist/ is already ignored by git.
MACOS_OUTPUT_DIR = "dist"

# What macOS keys the app's permissions to, and what lets a new setup upgrade an
# installed copy. Like the Windows installer id it must never change, so it does
# not follow the display name.
MACOS_BUNDLE_ID = f"io.github.tomiwa-adesanya.{APP_SLUG}"

# The webview packages pip cannot install. Listed here so that apt pulls them in
# and the window opens on a machine that has never run the game, rather than
# failing with a message about a missing backend.
DEPENDS = (
    "python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, "
    "gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0"
)


def macos_arch():
    """The architecture word used in file names: arm64 or x64, as on Windows."""
    return "arm64" if platform.machine() == "arm64" else "x64"


def sign_macos_bundle(bundle):
    """Apple silicon refuses unsigned code. Nuitka signs ad hoc already, so
    this normally only verifies, and signs only if that check fails."""
    check = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", bundle]
    )
    if check.returncode != 0:
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", bundle],
            check=True,
        )


def build_pkg(bundle, installer):
    """Wrap the app in a package that installs it into Applications, which is
    what puts it in Launchpad, as the Windows setup does for the Start menu."""
    root = f"{MACOS_OUTPUT_DIR}/pkgroot"
    components = f"{MACOS_OUTPUT_DIR}/pkg-components.plist"
    try:
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root)
        # ditto keeps the symlinks and attributes a signed bundle depends on.
        subprocess.run(
            ["ditto", bundle, f"{root}/{os.path.basename(bundle)}"],
            check=True,
        )

        # Installer treats an app as relocatable by default, so with another
        # copy on the disk, such as the unzipped download, it would update that
        # one and leave Applications empty.
        subprocess.run(
            ["pkgbuild", "--analyze", "--root", root, components], check=True
        )
        with open(components, "rb") as handle:
            entries = plistlib.load(handle)
        for entry in entries:
            entry["BundleIsRelocatable"] = False
        with open(components, "wb") as handle:
            plistlib.dump(entries, handle)

        subprocess.run(
            [
                "pkgbuild",
                "--root", root,
                "--component-plist", components,
                "--install-location", "/Applications",
                "--identifier", MACOS_BUNDLE_ID,
                "--version", APP_VERSION,
                installer,
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"\npkgbuild failed ({error}). The zip is still usable.",
              file=sys.stderr)
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)
        if os.path.exists(components):
            os.remove(components)

    return installer


def package_macos(bundle):
    """Write the no-install zip and the setup package beside the bundle."""
    stem = f"{MACOS_OUTPUT_DIR}/{BINARY_NAME}-v{APP_VERSION}-macos-{macos_arch()}"
    archive = f"{stem}.zip"
    installer = f"{stem}-setup.pkg"

    print("\nPackaging for macOS ...")
    sign_macos_bundle(bundle)

    subprocess.run(
        ["ditto", "-c", "-k", "--keepParent", bundle, archive], check=True
    )
    print(f"  zip      -> {archive}")

    if build_pkg(bundle, installer):
        print(f"  setup    -> {installer}")
        print("\nSetup package ready. Double click it to install into Applications.")


def run_build():
    system = platform.system()
    print(f"Building {APP_NAME} v{APP_VERSION} for {system} ...")

    if system == "Darwin":
        # pywebview reaches Cocoa through PyObjC, which Nuitka will only compile
        # into an application bundle ("package 'Foundation' requires
        # '--mode=app'"). A bundle cannot be one file, and the folder name below
        # names it after the game rather than after the entry script.
        output_mode = [
            "--mode=app",
            f"--output-dir={MACOS_OUTPUT_DIR}",
            f"--output-folder-name={APP_NAME}",
        ]
        naming = [
            f"--macos-app-name={APP_NAME}",
            f"--macos-app-version={APP_VERSION}",
            f"--macos-signed-app-name={MACOS_BUNDLE_ID}",
        ]
    else:
        output_mode = [
            "--standalone",                 # Bundle interpreter + deps.
            "--onefile",                    # Single self-extracting binary.
        ]
        naming = [f"--output-filename={BINARY_NAME}"]

    cmd = [
        sys.executable, "-m", "nuitka",

        # -- Output mode ------------------------------------------
        *output_mode,

        # -- Data files -------------------------------------------
        "--include-data-dir=./static=static",

        # -- Imports reached late or by name ----------------------
        "--include-module=waitress",
        "--include-module=websockets.asyncio.server",
        "--include-module=websockets.asyncio.client",

        "--nofollow-import-to=tkinter",

        # -- Binary name + metadata -------------------------------
        *naming,
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
        cmd.append(f"--macos-app-icon={ICON_PNG_MACOS}")

    elif system == "Linux":
        # No console flag needed on Linux. Set the icon for desktop entries.
        cmd.append(f"--linux-icon={ICON_PNG}")

    # -- Entry point ----------------------------------------------
    cmd.append(ENTRY_POINT)

    print("Running:")
    print("  " + " \\\n    ".join(cmd))
    print()

    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nBuild failed (exit code {result.returncode}).", file=sys.stderr)
        sys.exit(result.returncode)

    if system == "Darwin":
        bundle = f"{MACOS_OUTPUT_DIR}/{APP_NAME}.app"
        if not os.path.isdir(bundle):
            print(f"\nBuild reported success but {bundle} is not here.",
                  file=sys.stderr)
            sys.exit(1)
        print(f"\nBuild succeeded.  App bundle: {bundle}  (v{APP_VERSION})")
        return bundle

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
    elif platform.system() == "Darwin":
        package_macos(built)
