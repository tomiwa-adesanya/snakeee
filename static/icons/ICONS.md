# Icons

PNG only. No inline SVG, no icon fonts, no emoji and no Unicode symbols anywhere
in the interface.

Every file in this directory is currently a **generated placeholder**. They
exist so that layout work is not blocked, and every one of them is meant to be
replaced with real artwork at the same path, the same pixel size, and the same
white-on-transparent treatment.

Replace each one with real artwork at the same path and pixel size.

## The application icon

`app/` is different. It is not a placeholder: it is the snake the game draws,
laid out as an S, generated from the same proportions the renderer uses for a
segment, a head and an eye. `icon.ico` is what the Windows build embeds and
`icon_256.png` is what the Linux and macOS builds use.

| File | Size | Used for | Replaced |
|---|---|---|---|
| `home_white_20.png` | 20x20 | Bottom nav | no |
| `play_white_20.png` | 20x20 | Bottom nav | no |
| `settings_white_20.png` | 20x20 | Bottom nav | no |
| `info_white_20.png` | 20x20 | Bottom nav | no |
| `host_white_18.png` | 18x18 | Create room | no |
| `join_white_18.png` | 18x18 | Join room | no |
| `refresh_white_18.png` | 18x18 | Refresh room list | no |
| `lock_white_16.png` | 16x16 | Private room marker | no |
| `players_white_16.png` | 16x16 | Player count | no |
| `bot_white_16.png` | 16x16 | Bot marker | no |
| `crown_white_16.png` | 16x16 | Host marker | no |
| `ready_white_16.png` | 16x16 | Ready state | no |
| `kick_white_16.png` | 16x16 | Host kick control | no |
| `heart_white_16.png` | 16x16 | Lives | no |
| `skull_white_16.png` | 16x16 | Kills | no |
| `trophy_white_18.png` | 18x18 | Results screen | no |
| `pause_white_18.png` | 18x18 | Pause | no |
| `close_white_16.png` | 16x16 | Modal dismiss | no |
| `warning_white_18.png` | 18x18 | Connection and error states | no |
| `spectate_white_16.png` | 16x16 | Spectator marker | no |
| `arena_white_16.png` | 16x16 | Minimap toggle | no |
| `preset_white_16.png` | 16x16 | Saved rule presets | no |

Change the last column to `yes` as each real icon lands, so the remaining work
is visible at a glance.
