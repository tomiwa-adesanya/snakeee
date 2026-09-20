# Third party licenses

## Bundled fonts

Both fonts are redistributed under the SIL Open Font License 1.1. The full
license text ships alongside each font file, which the OFL requires.

| Font | File | License text |
|---|---|---|
| Space Grotesk | `static/fonts/SpaceGrotesk-Variable.ttf` | `static/fonts/SpaceGrotesk-OFL.txt` |
| JetBrains Mono | `static/fonts/JetBrainsMono-Variable.ttf` | `static/fonts/JetBrainsMono-OFL.txt` |

Neither font is loaded from a CDN. Both are served from disk by the local
server, so the game makes no outbound request of any kind.

## Python dependencies

| Package | License |
|---|---|
| Flask | BSD 3-Clause |
| waitress | ZPL 2.1 |
| pywebview | BSD 3-Clause |
| websockets | BSD 3-Clause |

Verify this table against the installed versions before a release:

    pip install pip-licenses
    pip-licenses --from=mixed --order=license
