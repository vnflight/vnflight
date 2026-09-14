# Developer guide

Code-wise, the project is split into two pieces: `vnflight.rpy`, the shim that runs inside the Ren'Py game, and `vnflight.py`, the Python side that runs the bridge and one of the clients that talk to it, a CLI or an MCP server.

## Architecture

```text
Game (Ren'Py)            Bridge (HTTP, localhost:8385)        Clients
  vnflight.rpy   <--->   vnflight.py bridge            <--->   vnflight.py <verb>   (CLI)
  + vnf_*.rpy mods        one slot per running game            vnflight.py mcp      (MCP server)
                                                               your own client
```

- **Shim.** Installed into `game/`, it hooks Ren'Py's say, menu, input and screen machinery and pushes events to the bridge: `dialogue`, `narration`, `nvl`, `choice_request`, `input_request`, `screen_content`, `stats_update`, `inventory_update`, `progress_change`, `game_started` / `game_resumed` / `game_ended`, `context` (main menu vs in game), `renpy_exception`, and `command_result` for every command it executes. It polls the bridge for commands on a background thread and runs them on the main thread from a screen timer.
- **Bridge.** A stateful HTTP server. It keeps the transcript, the current pending request (the menu or input the game is waiting on), the latest stats/inventory/progress, and per-slot command queues. Clients read events from a cursor and submit commands with a nonce; the bridge matches the shim's `command_result` back to the nonce. A bridge can host several games at once, one **slot** each; slots are addressed by id or game_id and can be reserved with a token.
- **Clients.** The CLI and the MCP server are thin: both call the same handlers, which turn "act on choice 2" into a command, wait for the result, settle on the next story state, and format it. A refusal from the shim (`rollback_disabled_by_game`, `nothing_to_close`, a stale surface) travels back unchanged.

Protocol version is `SHIM_PROTOCOL_VERSION = 1` (`src/vnflight/shim_schema.py`, mirrored as `_VNFLIGHT_SHIM_PROTOCOL_VERSION` in the shim). Every shim request carries it in the `X-VNFlight-Shim-Protocol` header, and the bridge rejects a mismatch outright: HTTP 409 with `reason: shim_protocol_mismatch`, the expected and received numbers, and the remediation (`install-shim`, then restart the game and the bridge). Nothing is merely warned about; a stale shim never gets to talk.

## The single-file build

`dist/vnflight.py` is a **build artifact**: `src/vnflight/` is bundled into it by

```bash
python build_vnflight.py
```

Never edit `dist/vnflight.py` by hand; edit `src/vnflight/` and rebuild. `vnflight.rpy` is hand-written and is the single source of the shim.

The client-side rules for which story rows an action owns (observed, held, claimed, recorded, acknowledged) and their invariants are documented in the module docstring of `src/vnflight/delivery_ownership.py`, next to the code that enforces them.

Modules under `src/vnflight/`:

- `bridge.py`: the HTTP server, game state, slots, transcript and command journal.
- `handlers.py`: the tool handlers shared by the CLI and MCP (`act`, `wait`, `state`, `back_all`, `launch`, …).
- `cli.py`: argument parsing and text/JSON rendering for every verb.
- `mcp.py`: the MCP server; a tool table with capabilities that maps onto `handlers.py`.
- `client.py`: the Python client for the bridge (cursor, polling, command submission, slot attachment).
- `lib.py`: config loading, game discovery, launching, shim installation, persistent CLI state.
- `format.py`: turns bridge events and pending requests into the text an agent reads.
- `presentation_lane.py`: serialises presentation-changing commands and applies their deadlines.
- `act_settle.py`: the pure policy that decides when an act has "settled" and what to report.
- `overlay_presentation.py`, `overlay_ledger.py`: what an overlay screen has shown and what is new.
- `action_surface.py`: the projection of choices and buttons into one numbered action list.
- `settle.py`, `lifecycle.py`, `presentation.py`, `overlay.py`, `save_scan.py`, `shim_schema.py`: settle helpers, launch lifecycle, presentation state, the save scanner, and the shim event schema.

### Building your own client

`handlers.py` is the API. A handler takes a `HandlerContext` (wrapping a `BridgeClient`) and a parameter dict, and returns a plain dict. `handle_tool(ctx, name, params)` dispatches by tool name. The stock MCP server is a loop that exposes `_TOOLS` and calls `handle_tool`; a custom server, or a harness that wants to delay or annotate an agent's turns, wraps the same calls and renders the dicts its own way.

## Writing a mod

A mod is a Ren'Py file that runs at `init -989`, one step after the shim's `init -990`, so every registration function exists. It must be valid on Ren'Py 7 (Python 2): no f-strings, no keyword-only arguments, `.format()` for strings. It is installed by `install-shim` under a `vnf_*.rpy` target name configured in `vnflight.json`.

```renpy
init -989 python:
    # Expose stats/inventory the generic scrape cannot see.
    def _vnf_get_inventory_stats():
        inventory = [{"name": it.name, "quantity": it.count} for it in renpy.store.inventory]
        stats = {"hp": renpy.store.hp, "coins": renpy.store.coins}
        return inventory, stats

    # Rewrite scraped screen data: rename buttons, drop noise.  Each entry
    # is a per-screen dict with "_tag", "texts", "choices" and "buttons".
    # A button dict carries "label", "actions" (action class names),
    # "action_strs" (their repr), "is_disabled", "is_selected" and
    # "is_return"; there is no image-name key, so an image-only button
    # shows up as label "[unlabelled]" and is told apart by its action.
    def _my_transform(per_screen):
        for scr in per_screen:
            if scr.get("_tag") != "map_screen":
                continue
            for btn in scr.get("buttons", []):
                if (btn.get("label") == "[unlabelled]"
                        and any("map" in s for s in btn.get("action_strs", []))):
                    btn["label"] = "Map"
        return per_screen
    _vnf_add_screen_transform(_my_transform, priority=10)   # lower runs first

    # A panel that replaces the scene: present it as the whole surface.
    _vnf_register_overlay_screen("inventory_screen", modal=True)
    _vnf_set_screen_section_depth("inventory_screen", 2)

    # A game-specific command: `cmd my_travel dest=Creeks`.
    def _my_travel(cmd_name, cmd_args):
        dest = (cmd_args or {}).get("dest")
        renpy.store.travel_to(dest)
        return {"success": True, "message": "Travelling to {}".format(dest)}
    _vnf_add_command_handler("my_travel", _my_travel, causal_boundary=True)

    # Progress: nodes the shim marks visited by label or by check().
    _vnf_add_progress_node("ending_good", label="ending_good", game_terminal=True,
                           check=lambda: getattr(renpy.store, "ending", None) == "good")
```

Registration API (all defined in `vnflight.rpy`):

- `_vnf_get_inventory_stats()` → `(inventory, stats)`; override it to expose the game's own data.
- `_vnf_add_screen_transform(fn, priority=50)`: `fn(per_screen_list) -> per_screen_list`, runs on every scrape.
- `_vnf_register_overlay_screen(tag, blocking=True, retain_generation=False, modal=False)`: treat a screen as an overlay; `modal=True` presents it instead of the covered menu.
- `_vnf_set_screen_section_depth(tag, depth)`: split a screen's texts and buttons into sections at that widget depth.
- `_vnf_add_command_handler(name, fn, causal_boundary=False)`: `fn(cmd_name, cmd_args) -> dict`; the dict becomes the `command_result` (add `success`, `error`, `message`). `causal_boundary=True` says the command supersedes earlier acts.
- `_vnf_add_progress_node(name, check=None, next_nodes=None, terminal=False, label=None, thread="main", phase=None, game_terminal=False, label_trigger=True)`, `_vnf_add_progress_checker(fn)`, `_vnf_set_progress_interpreter(fn)`: the progress graph behind `progress` and the bridge's end-of-game detection.


## Protocol notes

- Launch handshake: `launch` writes `game/vnflight_launch.json` (bridge URL, slot token, save slot, launch id, debug flag, `written_at`) before starting the game. A fresh file (15 min) wins over the `VNFLIGHT_*` environment, because launcher-started games never see the launcher's environment and a warm Steam process tree can carry stale values. The shim claims the file with its pid and writes a `vnflight_registration_*` receipt; the launcher waits for the claim before it will overwrite the file for a second launch of the same game. Whether the shim is active at all is still decided by `VNFLIGHT_ENABLED=1` or the `--always-on` patch, not by the file.
- The shim pushes events as JSON to the bridge, each stamped with a sequence number; clients read with `since=<cursor>`.
- A command is queued on the bridge with a nonce; the shim fetches it, executes it on the main thread, and pushes `{"type": "command_result", "command": ..., "nonce": ..., "success": ..., ...}`. Handlers wait for the nonce-matched result rather than for submission.
- A `choice_request` carries the menu's items (`full_items` with captions and disabled entries) and an id; acting on a stale id is refused.
- A UI button that enters story flow is reported with `story_entry: true`, which retires the menu it interrupted.
- `renpy_exception` marks the exception screen; it stays "current" until story progress resolves it, and `state` reports it as an anomaly meanwhile.

## Testing

```bash
pip install -r requirements-dev.txt          # pytest, plus mcp for the MCP tests
python -m pytest tests -q                    # unit tests, about ten minutes
```

The shim's unit tests only string-match and stub-execute `vnflight.rpy`; nothing runs it. After any shim change, drive the built CLI against a real game on **both** Ren'Py 7 and Ren'Py 8 (the defects that reach users differ between them): `launch --wait`, `act Start`, `wait`, `back` at the story screen (must refuse), `cmd debug_crash` then `act Ignore` (the exception screen must render and clear), a panel or overlay if the game has one, and `stop`. The sample games run on both engines when you point two config entries at the same project directory with different SDKs.

## Engine gotchas

- **Python 2 syntax in the shim.** Ren'Py 7 runs Python 2; the shim and mods must avoid f-strings and Python-3-only syntax. Lint passes on 7 but some constructs only fail at runtime.
- **Ren'Py 7 cleans the store before `_start` and on every full restart** (main menu, New Game). Any store variable rebound after init reverts to its init value. State written from the shim's poll thread or from callbacks must live in a container created at init and mutated in place, never a bare store variable.
- **Ren'Py 7 modal screens silence timers under them** unconditionally (8.x only when `config.modal_blocks_timer` is set). The engine's exception screen is modal, so a mechanism that relies on a screen timer stops during an error; the shim's periodic pump takes over when its poller stops ticking.
- **`.rpyc` caching.** Ren'Py 7 may keep running a compiled copy of an old shim; clear `game/vnflight.rpyc` and `game/cache/*.rpyb` when a reinstall seems to have no effect.
- **Launcher games.** Steam and GOG start the game themselves, so `VNFLIGHT_ENABLED` is not inherited; install with `--always-on`. Games that share a save folder through the launcher are not isolated per run.
