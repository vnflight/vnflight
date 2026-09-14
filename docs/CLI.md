# CLI reference

`python vnflight.py <verb> [options]`. Global options go before the verb: `--json` for structured output, `--slot <slot-or-game_id>` when more than one game is running, `--bridge <url>` (default `http://127.0.0.1:8385`), `--quiet`, `--yes`. `python vnflight.py <verb> --help` shows a verb's own options.

Every verb reports the shim's actual result: a refused or unconfirmed command prints a `✗` line and exits 1, and `--json` carries `success`, `error` or `reason`.

## Playing

| Verb | What it does |
|---|---|
| `launch <game>` | Start the bridge and the game; `--wait` waits for the first menu, `--debug`/`--no-debug` override the shim log, `--fast-forward`, `--auto` |
| `wait` | Read the story until a choice or text input is needed (`--timeout`, `--verbose`) |
| `act <target>` | Pick a choice or a screen button by number, label or id; waits for the next interaction unless `--no-wait` |
| `input <text>` | Answer a text prompt |
| `state` | Footer, stats, inventory and visible buttons (`--verbose` for screens and actions) |
| `screenshot` | Capture the game window |
| `save [slot]` / `load [slot]` | Save and load; the save slot defaults to `1-1` for `save` and to the newest save for `load` |
| `stop [game]` | Stop one game, or everything |

## Navigation and extras

| Verb | What it does |
|---|---|
| `choices` | Show the current choices again |
| `back` | Close the current menu or overlay (Escape / Return) |
| `back_all` | Close overlays one after another until the story is back; refuses when nothing is open |
| `advance` | One dialogue step without auto-forward; refused while a menu is active |
| `rewind` / `replay` | One step back through Ren'Py rollback, and forward again; refused on games that disable rollback |
| `history` | Recent transcript |
| `autoplay` | Turn auto-advance on and watch the story |
| `progress` | Story progress from a game's progress adapter |

## Setup

| Verb | What it does |
|---|---|
| `games` | List the games in `vnflight.json` |
| `info <game>` | Show the game's spoiler-free briefing |
| `prompt <game>` | Print a system prompt for an agent playing this game |
| `install-shim <game>` | Install the shim and the game's adapters (`--always-on`, `--no-mods`, `--create-game-dir` for a target without `game/`); always prints text |
| `fetch-mods [<https-url> --sha256 <manifest-digest>]` | Download a verified adapter snapshot into `--output <new-directory>`; with no URL and digest it uses the snapshot pinned under `mods_snapshot` in `vnflight.json`, shows it, and asks first (`--yes` skips the question) |
| `slots` | List the running games on the bridge |
| `set <key> [value]` | Read or set a runtime config key (for example `auto_skip_single_choice`); `--profile <name>` applies a timing profile |
| `save-scan <path>` | Read-only scan of `.save` files for shim references (`--recursive`) |
| `mcp` | Start the MCP server (`--game`, `--slot`, `--capabilities`, `--tools`) |
| `bridge` | Start a standalone bridge and block |

## Diagnostics

| Verb | What it does |
|---|---|
| `inspect` | Raw screens, focus and widgets |
| `poll` | Raw bridge events since the last cursor (`--all` re-reads the history) |
| `cmd <name>` | A raw game command (`next`, `rollback`, `auto_advance_on/off`, `skip_toggle`, `save`, `load`, `start`, `quit`) |
| `resync` | Ask the shim to re-push the active menu |
| `reset` | Reset the CLI's saved cursor and slot state |
