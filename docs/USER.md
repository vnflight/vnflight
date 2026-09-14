# User guide

Everything runs from the single-file `vnflight.py`. It reads `vnflight.json` next to it to find games and timing profiles, and it starts the bridge for you when you launch a game. This guide covers the config file, installing the shim, the CLI essentials (the full verb list is in [CLI.md](CLI.md)), the MCP server, profiles, adapters, save safety, and troubleshooting.

## The config file

Copy `vnflight.default.json` to `vnflight.json`. The file has two top-level sections, `profiles` and `games`, plus an optional `mods_manifest` (see Adapters). A game entry:

```json
"my_game": {
  "name": "My Visual Novel",
  "launch": "path/to/renpy-sdk/renpy.exe path/to/my_game",
  "mods": [],
  "default_profile": "turbo",
  "briefing": "my_game/briefing.md",
  "always_on": true,
  "debug": false,
  "debug_logs": "logs/my_game"
}
```

- `launch` is the command that starts the game: a Ren'Py SDK plus a project path, a game executable, or a Steam URL (`steam://rungameid/<appid>`). `steam_appid` / `gog_gameid` help vnflight find the running process for launcher games.
- `default_profile` is applied right after launch (see Timing profiles).
- `briefing` (a file path) or `briefing_text` (inline) is an optional spoiler-free text shown by `vnflight.py info <game>` and included in `vnflight.py prompt`.
- `mods` lists the game's adapters to install alongside the shim; leave the key out to take them from the adapter manifest instead (see Adapters).
- `always_on` records that the shim should be installed with `--always-on` (see below).
- `debug` turns on the shim's command/action log for that game; `debug_logs` is the directory the logs go to. Both are off by default and can be overridden per launch with `launch --debug` / `--no-debug`.

## Installing the shim

```bash
python vnflight.py install-shim my_game              # shim + the game's adapters
python vnflight.py install-shim my_game --always-on  # for Steam/GOG/launcher games
python vnflight.py install-shim my_game --no-mods    # core shim only
```

This copies `vnflight.rpy` (and each configured adapter, under a `vnf_*.rpy` name) into the game's `game/` directory. It asks for confirmation; the global `--yes` (before the verb: `python vnflight.py --yes install-shim my_game`) skips the prompt. A target without a `game/` directory is refused, since that almost always means the path in `vnflight.json` is wrong; `--create-game-dir` overrides that for a game laid out differently. Relative paths in a game's `launch` are resolved against `vnflight.json`, never against the directory you run the command from. `install-shim` always prints text, even with `--json`.

The installed shim is inert during normal play. It activates only when the game is started with `VNFLIGHT_ENABLED=1` in its environment, which `vnflight.py launch` sets for games it starts directly, or when it was installed with `--always-on`, which patches the copy to be active unconditionally. Use `--always-on` for games that Steam, GOG Galaxy or another launcher starts, since those never see the launcher's environment.

Separately from activation, every `launch` writes a small handshake file, `game/vnflight_launch.json`, telling that one game which bridge to dial, its slot token and save slot, and whether debug logging is on. The shim reads it at start, claims it, and removes nothing: a fresh file wins over the environment, a stale one (older than 15 minutes) is ignored. You can leave it in place.

Reinstall after every update of `vnflight.rpy` or an adapter, then restart the game. `launch` refuses a game whose installed shim no longer matches the source, and says so. On Ren'Py 7 the engine may keep using a compiled copy: delete `game/vnflight.rpyc` and `game/cache/*.rpyb` if the game still behaves like the old shim.

## CLI

`python vnflight.py <verb> [options]`. Global options go before the verb: `--json` for structured output, `--slot <slot-or-game_id>` when more than one game is running, `--bridge <url>` (default `http://127.0.0.1:8385`), `--quiet`, `--yes`.

The verbs you need to play a game:

| Verb | What it does |
|---|---|
| `launch <game>` | Start the bridge and the game; `--wait` waits for the first menu |
| `wait` | Read the story until a choice or text input is needed |
| `act <target>` | Pick a choice or a screen button by number, label or id; waits for the next interaction |
| `input <text>` | Answer a text prompt |
| `state` | Footer, stats, inventory and visible buttons |
| `screenshot` | Capture the game window |
| `save [slot]` / `load [slot]` | Save and load; the save slot defaults to `1-1` for `save` and to the newest save for `load` |
| `stop [game]` | Stop one game, or everything |

The play loop is `wait`, read, `act`, repeat; when the game asks for text, `input` answers it. `act` accepts what `wait` and `state` showed you: a choice number, a choice label, or a screen button label. When a panel covers the story (a kit, a log, a map), `state` lists the panel's own buttons; `back` closes one panel, `back_all` closes them all.

Every verb reports what the shim actually did, not just that the request was sent. A refused or unconfirmed command prints a `✗` line and exits 1: a rewind on a game that disables rollback, `back_all` with nothing to close, an `advance` while a menu is active, an `act` the game rejected. With `--json` the same result is a JSON object carrying `success`, `error` or `reason`.

When the game crashes into Ren'Py's exception screen, `state` and `wait` start with a `GAME ERROR` note and the screen's buttons (Ignore, Reload, Quit…) become actionable. `act Ignore` usually resumes play; the note clears once the story moves again.

Every other verb (navigation such as `back`, `advance`, `rewind`; setup such as `games`, `install-shim`, `set`; diagnostics such as `inspect`, `poll`, `cmd`) is listed in [CLI.md](CLI.md).

## MCP server

```bash
python vnflight.py mcp --game my_game
```

The server speaks MCP over stdio and binds the session to a running game: launch the game first with `python vnflight.py launch my_game`, then start the server with `--game my_game`; with several games running, bind explicitly with `--slot <slot-or-game_id>`. Until a game is connected, `state` answers `status: unknown` and `act` is rejected with `state_unavailable`. `--capabilities` selects the tool set: `play` (default), `lifecycle`, `diagnostic`, `admin`, or `all`; `--tools` narrows it to an exact allowlist. The default `play` set has no `launch` or `stop`: stop the game from the CLI (`python vnflight.py stop`), or start the server with `--capabilities play,lifecycle` so the client can call `launch` (its argument is `game_id`) and `stop` itself.

Point an MCP client at it with a stdio server entry, for example:

```json
{ "mcpServers": { "vnflight": {
    "command": "python",
    "args": ["C:/path/to/vnflight.py", "mcp", "--game", "my_game"] } } }
```

Tools by capability:

- **play**: `wait` (read until a decision), `act`, `input_text`, `state`, `transcript`, `screenshot`, `back`, `back_all`, `advance`, `rewind`, `replay`, `save`, `load`, `set_profile`, `auto_skip` (skip single-choice menus, on by default), `set_format` (output format for `wait`/`state`, and whether crash anomalies are reported).
- **lifecycle**: `launch` (with a `debug` option like the CLI flag), `stop`, `games`, `bridge_connect`.
- **diagnostic**: `inspect`, `progress`, `save_scan`.
- **admin**: `command` (raw game commands).

The MCP tools and the CLI verbs share one implementation, so their results and refusals match.

## Timing profiles

Profiles in `vnflight.json` bundle the shim's delays: auto-advance and post-action delays, click timing, reading speed, and whether the user can still click (`allow_user_override`). The defaults are `turbo` (fast, the user can intervene), `external` (fastest, user clicks blocked) and `hybrid` (slower, watchable). Apply one with `set --profile turbo` or the `set_profile` tool; `default_profile` in the game entry applies one automatically at launch.

## Adapters (mods)

To download a snapshot instead of cloning the adapter repository ([vnflight/mods](https://github.com/vnflight/mods)):

```text
python vnflight.py fetch-mods --output path/to/mods-snapshot
```

With no URL and no digest, the command uses the snapshot pinned under
`mods_snapshot` in `vnflight.json`: the template carries the manifest URL
and SHA-256 of the adapter snapshot this vnflight release was tested with,
and both are refreshed with each release. It prints the URL and digest it
is about to use and asks before downloading (the global `--yes` skips the
question). Nothing ever fetches that snapshot implicitly. To take a
snapshot from somewhere else, give both explicitly, and they win as
written:

```text
python vnflight.py fetch-mods https://HOST/REPO/RAW/COMMIT/manifest.json --sha256 TRUSTED_MANIFEST_SHA256 --output path/to/mods-snapshot
```

Use an immutable commit URL and obtain the manifest digest from a trusted
release channel. SHA-256 is an integrity check, not a publisher signature.
The command verifies the manifest and every adapter, refuses HTTP redirects
away from HTTPS, and publishes only a complete snapshot. The output directory
must not exist. Downloads are size-limited and use a shared time budget.
It neither changes your configuration nor installs or executes code.
Remote manifests must include a `license` entry naming `LICENSE` with its
`sha256`; the license is downloaded and verified alongside the adapters.
Set `mods_manifest` to the returned local manifest path, then run
`install-shim`. Fetch updates into a new directory and switch configuration
explicitly after stopping the affected games. Observation tools never fetch.

Some games cannot be read from the generic scrape: buttons drawn as images, custom inventory or stat screens, modal panels that replace the scene, progress that only exists in game variables. An adapter is a small Ren'Py file that teaches the shim about one game: it names buttons, filters noise, exposes stats and inventory, registers overlay screens and adds game-specific commands. The config keys and CLI flags call them mods (`mods`, `mods_manifest`, `--no-mods`, `fetch-mods`); the two words mean the same file. Adapters are unofficial compatibility layers: not affiliated with or endorsed by the game's creators, they contain no game assets, and they only make a locally owned copy easier to read for an agent.

Adapters are distributed separately, in the adapter repository, which carries a `manifest.json` listing, per game id, its adapter files, their install names and a sha256 hash of each. Point the config at it once, outside the games list:

```json
"mods_manifest": "C:/path/to/mods/manifest.json"
```

The path is absolute or relative to `vnflight.json`; it is never a URL. A game entry with no `mods` key then takes its adapters from the manifest entry for its id (`"adapters": "<other id>"` reuses another entry, for a second config entry of the same game). Each file is checked against its hash before anything is installed; a mismatch or a missing file refuses the whole install (shim included) and says why, so a game is never left with half its adapters. `--no-mods` installs the core shim alone if you need to play on regardless. `python vnflight.py games` shows where each game's adapters come from.

A game entry can also map its adapters itself, and that list wins as written:

```json
"mods": [ { "source": "C:/path/to/mods/roadwarden.rpy", "target": "vnf_inventory_stats.rpy" } ]
```

`source` is absolute or relative to `vnflight.py`; `target` is the file name inside the game's `game/` directory and must start with `vnf_` so the installer never overwrites a game file. An empty `mods` list means no adapters, whatever the manifest says. Writing a mod is covered in the developer guide.

## Save safety

Ren'Py saves can pickle references to the shim's objects. Before sharing a save, or before uninstalling the shim, scan it:

```bash
python vnflight.py save-scan path/to/save-or-directory --recursive
```

The scan is read-only and reports likely `vnflight`/`vnf_` references. A clean result is a good sign, not a proof.

## Troubleshooting

- **`launch` refuses: installed shim is stale.** Run `install-shim` again (with `--always-on` if the game needs it) and restart the game. On Ren'Py 7 also remove `game/vnflight.rpyc` and `game/cache/*.rpyb`.
- **"Profile apply failed: No confirmation from shim" at launch.** The game took longer than expected to reach its first screen. Apply it by hand with `set --profile <name>`; if it keeps happening on one game, raise nothing, just report it.
- **"Multiple games connected."** Add `--slot <game_id>` (or `--slot latest:<game_id>`) before the verb, or stop the extra game.
- **"Admin token required" on `stop` or `slots`.** The bridge was started by another process that owns it. Pass `--token` (or set `VNFLIGHT_TOKEN`), or stop it from the process that started it. Tokens are a local convenience, not a security boundary against other users on the machine.
- **`GAME ERROR` in `state`.** The game raised an exception. `act Ignore` continues past it in most cases; `act Reload` restarts the script; save first if the game allows it.
- **Story text seems to repeat or skip.** CLI invocations persist their reading cursors in `.vnflight_state.json` in the per-user vnflight data directory (`%LOCALAPPDATA%\vnflight` on Windows, `~/Library/Application Support/vnflight` on macOS, or `$XDG_DATA_HOME/vnflight`, defaulting to `~/.local/share/vnflight`, on Linux). `VNFLIGHT_DATA_DIR` overrides this directory; `reset` clears the selected bridge's client state. If the directory cannot be created, the CLI falls back to storing state beside the package. MCP sessions keep their own cursor in memory.
- **MCP tools answer `status: unknown` or `state_unavailable`.** No game is connected to the session: launch one from the CLI first, or give the server `--capabilities play,lifecycle` and call `launch`.
- **Nothing happens after `act` on a button.** Some buttons open a panel instead of advancing the story; run `state` to see it, then `back` or `back_all`.
