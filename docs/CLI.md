Here is a list of all available commands and their explanation. Global options go before the command: `python dist/vnflight.py [global options] <command> ...` in a clone, `python vnflight.py ...` in a flat release folder; `<command> --help` shows a command's own options.

## Global options

- `--bridge URL`\
  Bridge server URL (default `http://127.0.0.1:8385`).
- `--slot SLOT`\
  Target one running game when several are connected: a slot id or a game id, or `latest:<game_id>` for the newest live slot of a game.
- `--token TOKEN`\
  Slot or bridge admin token for reserved slots and token-gated bridges; falls back to the stored admin token, then `VNFLIGHT_TOKEN`.
- `--games-dir DIR`\
  Project root holding `vnflight.json` (default: found next to the code).
- `--json`\
  Structured JSON instead of text, for the play and query commands; setup commands (`install-shim`, `fetch-mods`) always print text.
- `--quiet`\
  Suppress confirmation headers and titles.
- `--no-state`\
  Do not persist the read cursor to disk.
- `--yes`, `-y`\
  Answer every confirmation prompt with yes (non-interactive mode).
- `--version`\
  Print `vnflight <version>` and exit.

## Setup

- `games`\
  List the games in `vnflight.json` and where each one's adapters come from. On an untouched template it exits 0 with a hint to add a game.
- `info <game>`\
  Show the game's spoiler-free briefing.
- `install-shim <game> [--always-on] [--no-mods] [--create-game-dir]`\
  Copy `vnflight.rpy` and the game's adapters into `<game>/game/`. `--always-on` patches the shim for launcher-started games (also set by `"always_on": true` in the config), `--no-mods` installs the core shim only, and a target without a `game/` folder is refused unless `--create-game-dir` is given. Any adapter problem refuses the whole install.
- `fetch-mods [url --sha256 DIGEST] --output DIR`\
  Download a hash-verified adapter snapshot into a new directory. With no URL and no digest it uses the snapshot pinned under `mods_snapshot` in `vnflight.json`, prints it and asks first (`--yes` skips the question); a URL must be HTTPS and come with its trusted manifest digest.
- `launch <game> [--wait] [--timeout S] [--auto] [--fast-forward] [--save-slot NAME] [--debug | --no-debug]`\
  Start the bridge and the game. `--wait` returns once the first menu or choice is up, `--save-slot` isolates the game's saves under a name, `--debug`/`--no-debug` override the game's shim-log setting for this launch. A game whose installed shim no longer matches the source is refused with the fix spelled out.
- `stop [game]`\
  Quit one game (or every game and the bridge when no game is named). Only processes this tool started and can still identify are killed.
- `prompt <game>`\
  Print a system prompt for an agent that will play this game through the CLI.
- `mcp [--game GAME] [--slot SLOT] [--bridge URL] [--token TOKEN] [--capabilities LIST] [--tools LIST]`\
  Start the MCP server on stdio. `--capabilities` is `play` (default), `lifecycle`, `diagnostic`, `admin` or `all`; `--tools` is an exact allowlist within those. Without `--bridge` the server starts and owns a bridge of its own.
- `bridge [--host HOST] [--port PORT] [--token TOKEN] [--require-token]`\
  Run a standalone bridge and block until stopped.

## Playing

- `wait [--timeout S] [--verbose]`\
  Read the story until something needs an answer: a choice, a text prompt, a screen with real controls, the end of the game, or the timeout. The quick menu alone never ends a wait. `--verbose` includes scene and metadata events.
- `act <N|label> [--no-wait] [--timeout S]`\
  Pick a numbered choice or a visible button by number or label, then read on to the next decision unless `--no-wait`. A target that is not on screen, or an act while a result is still settling, is refused with exit 1 and the reason; nothing is clicked blindly.
- `input <text> [--wait] [--timeout S]`\
  Answer a text prompt. `--wait` reads on afterwards.
- `state [--verbose] [--no-stats]`\
  The current footer, stats, inventory and visible buttons; `--verbose` adds screens and actions.
- `choices`\
  Show the current choices again.
- `save [slot] [--name NAME]`\
  Save the game; the save slot defaults to `1-1`. Refused at the main menu.
- `load [slot] [--wait] [--timeout S]`\
  Load a save; with no slot the newest save. A load that does not complete keeps the unread story for the next `wait` instead of discarding it.

## Navigation and extras

- `back [--wait]`\
  Close the current menu or overlay (Escape / Return). Refused, with the reason, when nothing is open, when a choice is live, or when the open screen is a custom called screen whose return value the tool cannot pick for you.
- `back_all [--wait]`\
  Close overlays one after another until the story screen is back; bounded, and refused when nothing is open.
- `advance [--wait]`\
  One dialogue step without auto-forward; refused while a menu is active.
- `rewind [--wait]`\
  One step back through Ren'Py rollback; refused on games that disable rollback.
- `replay [--wait]`\
  Roll forward again after a rewind.
- `autoplay [--timeout S]`\
  Turn auto-advance on and watch the story unfold.
- `cmd <name> [key=value ...] [--wait]`\
  Send a raw game command: `next`, `rollback`, `auto_advance_on`, `auto_advance_off`, `skip_toggle`, `save`, `load`, `start`, `quit`, or a command an adapter registers.
- `set [key] [value] [--profile NAME]`\
  Read or set a runtime config key such as `auto_skip_single_choice`; `--profile` applies a timing profile from `vnflight.json`.
- `history [--last N | --first N | --all] [--verbose]`\
  Show the recent transcript.
- `screenshot [--output FILE]`\
  Capture the game window to a PNG (default `screenshot.png`).
- `progress [--graph]`\
  Story progress from the game's progress adapter; `--graph` marks visited and unvisited nodes.

## Diagnostics

- `slots [--reap-stale]`\
  List the running games on the bridge; `--reap-stale` frees ended slots and slots whose game process is gone.
- `poll [--wait S] [--all] [--verbose]`\
  Raw bridge events since the last cursor; `--all` re-reads the whole history and moves the cursor to its end.
- `reset [--bridge]`\
  Reset the CLI's saved cursor and slot state; `--bridge` also resets the bridge.
- `resync`\
  Ask the shim to re-push the active menu (desync recovery).
- `inspect [--focus] [--verify]`\
  Raw screens, focus list and widgets; `--focus` shows only the clickable list, `--verify` cross-checks scraped buttons against it.
- `save-scan <path> [--recursive]`\
  Read-only scan of `.save` files for references to shim code, before sharing a save or uninstalling the shim.
