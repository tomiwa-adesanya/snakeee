# Fonts

Two OFL 1.1 fonts, served from disk, never from a CDN.

| File | Family | Used for |
|---|---|---|
| `SpaceGrotesk-Variable.ttf` | Space Grotesk | UI, headings, the splash wordmark |
| `JetBrainsMono-Variable.ttf` | JetBrains Mono | Scores, timers, room codes, addresses |

Both are variable fonts, so a single file covers every weight the interface
uses. The `@font-face` declarations in `static/css/tokens.css` declare the
supported weight ranges.

Do not replace either of these with a commercially licensed font. Bundling one
in a public repository is a license violation.

`SpaceGrotesk-OFL.txt` and `JetBrainsMono-OFL.txt` must stay next to the font
files. Redistributing an OFL font without its license text is not permitted.
