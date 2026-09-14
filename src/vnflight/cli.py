"""vnflight CLI -- command-line interface for playing Ren'Py visual novels.

This module contains all CLI commands, argument parsing, game lifecycle
management, and session helpers.  It uses BridgeClient from client.py for
bridge communication, formatters from format.py for output rendering, and
reusable helpers from lib.py for configuration, discovery, and process
management.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import shutil
import sys
import time
import uuid
from . import __version__
from .mod_fetch import fetch_mods_manifest
from .delivery_ownership import ActionDeliveryOwnership
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Set

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(errors="replace")
        except Exception:
            pass

from .client import (
    BridgeClient,
    WaitResult,
    _SINGLE_CONTINUE_DEADLINE_MARGIN,
    _SINGLE_CONTINUE_PENDING_GRACE,
    _PREFETCH_BOOKKEEPING_EVENT_TYPES,
    _is_single_continue_pending,
    _screen_is_nvl_continue_only,
    drain_stale_pending_request,
    preserve_prefetched_events,
)
from .lifecycle import (
    classify_lifecycle,
    item_is_default_focus_chrome,
    item_is_disabled,
    screen_names,
)
from .settle import screen_signature, wait_for_stable_change
from .format import (
    _bold,
    _cyan,
    _dim,
    _format_inspect_result,
    _get_live_screen,
    _get_latest_interactions,
    _get_latest_screen_buttons,
    _green,
    _has_choice_overlay,
    _is_pending_input_prompt_echo,
    _normalize_interaction_disabled,
    _normalize_quotes_client,
    _reclassify_nav_items,
    _red,
    _yellow,
    build_wait_data,
    build_state_data,
    format_event,
    format_events,
    format_interactions,
    format_main_menu,
    format_pending_request,
    format_screen_buttons,
    format_state_text,
)
from .handlers import (
    HandlerContext,
    _run_after_input_text_hook,
    handle_tool,
    render_tool_result_text,
    strip_internal_result_fields,
)
from .lib import (
    CONFIG_FILENAME,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_BRIDGE_URL,
    IS_WINDOWS,
    STATE_FILENAME,
    ClientState,
    _build_auto_launch_cmd,
    _discover_sdks,
    _find_bridge_script,
    _find_game_install_path,
    _find_gog_game_path,
    _find_project_root,
    _find_steam_game_path,
    shim_source_path,
    _get_file_hash,
    _http_request,
    _validated_setting_application_receipt,
    _is_process_alive,
    _launch_log_handles,
    _launch_subprocess,
    _load_config,
    _load_config_with_error,
    _resolve_manifest_path,
    MODS_MANIFEST_KEY,
    cli_command_hint,
    game_wants_always_on,
    pinned_mods_snapshot,
    resolve_game_mods,
    _parse_launch_cmd,
    _read_briefing,
    _slot_free_already_gone,
    _slot_free_detail,
    default_state_dir,
    discover_games,
    generate_prompt,
    game_identity_expectation,
    kill_process,
    pid_identity_from_record,
    launch_game,
    stop_game,
)
from .save_scan import (
    format_save_scan_results,
    iter_save_files,
    scan_save_file,
    summarize_save_scan_results,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = __version__   # one constant, src/vnflight/__init__.py
POLL_FAST_MS = 100  # first 2 seconds
POLL_MEDIUM_MS = 250  # 2-10 seconds
POLL_SLOW_MS = 500  # 10+ seconds

# How long _perform_wait tolerates consecutive bridge connection failures
# before failing out with an explicit "bridge unreachable" error.  Repeated
# connection failures are not a quiet game; without this, `wait` (default
# timeout None) hangs forever when the bridge process dies mid-wait.
_WAIT_BRIDGE_DOWN_GRACE = 10.0


def _confirm(prompt: str) -> bool:
    """Ask a yes/no question on stdin; no answer (closed stdin, EOF,
    Ctrl-C) means No, never a traceback."""
    try:
        ans = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ("y", "yes")


def cmd_fetch_mods(args: argparse.Namespace, client_state: ClientState) -> int:
    """Download a hash-verified adapter snapshot.

    Explicit ``url`` + ``--sha256`` win as written.  With neither, the
    snapshot pinned under ``mods_snapshot`` in vnflight.json (the one the
    release was tested with) is used, shown, and confirmed unless the
    global ``--yes`` is set.  Every download check stays in mod_fetch.
    """
    url, digest = args.url, args.sha256
    if url is None and digest is None:
        config, config_error = _load_config_with_error(
            getattr(args, "games_dir", None))
        if config_error:
            print(f"Adapter download refused: {config_error}", file=sys.stderr)
            return 1
        pinned = pinned_mods_snapshot(config)
        if pinned is None:
            print(
                "Adapter download refused: no manifest URL given and no "
                f"'mods_snapshot' entry (url + sha256) in {CONFIG_FILENAME}. "
                "Pass the HTTPS manifest URL and --sha256 explicitly, or copy "
                "the pinned 'mods_snapshot' block from vnflight.default.json.",
                file=sys.stderr,
            )
            return 1
        url, digest = pinned
        print(f"Adapter snapshot pinned in {CONFIG_FILENAME} (mods_snapshot):")
        print(f"  url:    {url}")
        print(f"  sha256: {digest}")
        print(f"  output: {args.output}")
        if not getattr(args, "yes", False):
            if not _confirm("Download and verify this snapshot? [y/N]: "):
                print("Aborted.")
                return 1
    elif url is None or digest is None:
        print(
            "Adapter download refused: give both the manifest URL and "
            "--sha256, or neither to use the snapshot pinned in "
            f"{CONFIG_FILENAME}.",
            file=sys.stderr,
        )
        return 1
    try:
        path = fetch_mods_manifest(url, digest, args.output)
    except (OSError, ValueError) as exc:
        print(f"Adapter download failed: {exc}", file=sys.stderr)
        return 1
    # stdout stays one JSON object (scripts parse it); the wiring hint
    # goes to stderr.
    print(f'Snapshot verified. Set "mods_manifest": "{path}" in {CONFIG_FILENAME}, '
          "then run install-shim.", file=sys.stderr)
    print(json.dumps({"mods_manifest": str(path)}))
    return 0


def cmd_install_shim(args: argparse.Namespace, client_state: ClientState) -> int:
    """Subcommand: install the vnflight.rpy shim (and game-specific mods) into a game."""
    game_id = args.game
    games_dir = args.games_dir
    yes = getattr(args, "yes", False)
    always_on = getattr(args, "always_on", False)
    no_mods = getattr(args, "no_mods", False)

    # 1. Locate source shim: the explicit root's own copy, else the one
    # that ships next to this code (a --games-dir holding only a config).
    root = Path(games_dir) if games_dir else _find_project_root()
    source_shim = shim_source_path(games_dir)
    if not source_shim.exists():
        print(_red(f"Error: Source shim not found at {source_shim}"))
        return 1

    # 2. Locate target game
    game_path = _find_game_install_path(game_id, games_dir)
    if not game_path:
        print(_red(f"Error: Could not locate installation path for game '{game_id}'."))
        return 1

    # 2b. Resolve the config entry (mods + per-game flags).  This must never
    # fail silently: a missed lookup used to skip the game's mods without a
    # word, leaving a half-installed setup.
    config, config_error = _load_config_with_error(games_dir)
    if config_error:
        print(_yellow(f"Warning: game config could not be read: {config_error}"))
    games_cfg = (config or {}).get("games", {})
    config_game_id: Optional[str] = game_id if game_id in games_cfg else None
    if config and config_game_id is None:
        # GOG/Steam-style invocations often pass the install directory
        # instead of the config id — match config entries by resolved
        # install path so their mods still install.
        for cfg_id in games_cfg:
            try:
                cfg_path = _find_game_install_path(cfg_id, games_dir)
            except Exception:
                cfg_path = None
            if not cfg_path:
                continue
            try:
                if cfg_path.resolve() == game_path.resolve():
                    config_game_id = cfg_id
                    print(_dim(
                        f"  Matched config entry '{cfg_id}' by install path."
                    ))
                    break
            except OSError:
                continue
    game_cfg = games_cfg.get(config_game_id, {}) if config_game_id else {}
    display_id = config_game_id or game_id

    # Apply per-game defaults from the config entry: "always_on": true (or
    # the older install_shim_flags spelling) and --no-mods.
    if game_wants_always_on(game_cfg):
        always_on = True
    for flag in game_cfg.get("install_shim_flags", []) or []:
        if flag == "--no-mods":
            no_mods = True

    # Handle macOS .app bundles or games containing bundles
    target_game_dir = game_path / "game"
    if platform.system() == "Darwin" and not target_game_dir.is_dir():
        # Look for a .app bundle (the game itself or inside the folder)
        bundles = (
            [game_path] if game_path.suffix == ".app" else list(game_path.glob("*.app"))
        )
        for app in bundles:
            for sub in ["Contents/Resources/autorun/game", "Contents/Resources/game"]:
                cand = app / sub
                if cand.is_dir():
                    target_game_dir = cand
                    break
            if target_game_dir.is_dir():
                break

    # Check if game/ folder exists.  A missing game/ almost always means
    # the path is wrong (a from-zero run once created mods/game/ this way
    # because --yes answered the old "create it?" prompt), so creating it
    # takes an explicit flag rather than a confirmation.
    if not target_game_dir.is_dir():
        if not getattr(args, "create_game_dir", False):
            print(_red(
                f"Error: '{game_path}' has no 'game/' subdirectory, so it does "
                "not look like a Ren'Py game. Check the game's path in "
                f"{CONFIG_FILENAME}; if this really is the game, re-run with "
                "--create-game-dir."
            ))
            return 1
        print(_yellow(
            f"Warning: '{game_path}' has no 'game/' subdirectory; creating it "
            "(--create-game-dir)."
        ))
        os.makedirs(target_game_dir, exist_ok=True)

    target_shim = target_game_dir / "vnflight.rpy"

    # 3. Gather mod sources from the resolved config entry.  Every skip
    # reason is reported explicitly — install-shim is the first command a
    # new user runs and must never half-succeed silently.
    # Each mod_files entry: (source_path, target_filename, config_label)
    mod_files: list = []
    mods_note: Optional[str] = None
    mod_config_errors = 0
    if no_mods:
        mods_note = "mods skipped (--no-mods)"
    elif config is None:
        if config_error:
            mods_note = "mods skipped: game config could not be read (see above)"
        else:
            mods_note = (
                f"mods skipped: no {CONFIG_FILENAME} found near {root}"
            )
    elif config_game_id is None:
        mods_note = (
            f"mods skipped: no config entry matches '{game_id}' "
            f"in {CONFIG_FILENAME}"
        )
    else:
        # One truth for "which adapters belong to this game" (explicit
        # list, or the mods-repo manifest named by the top-level
        # mods_manifest key): the same resolver the stale-shim check uses.
        resolution = resolve_game_mods(config, config_game_id, game_cfg, root)
        for problem in resolution.problems:
            print(_red(f"Error: {problem} (see {CONFIG_FILENAME})."))
            mod_config_errors += 1
        if resolution.origin == "manifest" and resolution.manifest_path:
            print(_dim(f"  Adapters from manifest: {resolution.manifest_path}"))
        if not resolution.entries and not resolution.problems:
            mods_note = f"no mods configured for '{config_game_id}'"
        for entry in resolution.entries:
            mod_source = entry["source"]
            mod_target_name = entry["target"]
            if not mod_target_name.startswith("vnf_"):
                print(
                    _red(
                        f"Error: Mod target '{mod_target_name}' does not start "
                        f"with 'vnf_' — refusing to overwrite game files. "
                        f"Fix the 'target' in {CONFIG_FILENAME} or the manifest."
                    )
                )
                mod_config_errors += 1
                continue
            if not mod_source.exists():
                print(
                    _red(
                        f"Error: Mod source not found: {mod_source} "
                        f"(mapped for '{config_game_id}' in {CONFIG_FILENAME})."
                    )
                )
                mod_config_errors += 1
                continue
            mod_files.append((mod_source, mod_target_name, entry["label"]))
    if mods_note:
        style = _dim if (no_mods or "no mods configured" in mods_note) else _yellow
        print(style(f"  Note: {mods_note}"))

    # 3b. Any adapter problem refuses the whole install.  Installing the
    # shim plus "the adapters that verified" leaves a game half-adapted,
    # and the docs promise that a mismatch refuses the install.
    if mod_config_errors:
        print()
        print(_red(
            f"✗ Nothing installed for '{display_id}': {mod_config_errors} "
            "adapter problem(s) above. Fix them, or use --no-mods to install "
            "the core shim only."
        ))
        return 1

    # 4. Per-file SHA-256 idempotency check
    shim_up_to_date = (
        not always_on
        and target_shim.exists()
        and _get_file_hash(source_shim) == _get_file_hash(target_shim)
    )
    mods_up_to_date = []
    for mod_source, mod_target_name, _label in mod_files:
        mod_target = target_game_dir / mod_target_name
        up = mod_target.exists() and _get_file_hash(mod_source) == _get_file_hash(
            mod_target
        )
        mods_up_to_date.append(up)

    if shim_up_to_date and all(mods_up_to_date) and not mod_config_errors:
        n_mods = len(mod_files)
        what = (
            f"shim + {n_mods} mod{'s' if n_mods != 1 else ''}"
            if mod_files
            else "shim"
        )
        print(_green(
            f"Everything is already up to date for '{display_id}' ({what})."
        ))
        return 0

    # 5. Confirmation
    if not yes:
        what = "shim + mods" if mod_files else "shim"
        print(f"Action: Install vnflight {what} for '{display_id}'")
        print(f"Target: {target_game_dir}")
        print()
        if shim_up_to_date:
            print(f"  [shim]  vnflight.rpy  (up to date)")
        else:
            status = "update" if target_shim.exists() else "install"
            print(f"  [shim]  vnflight.rpy  ({status})")
        for i, (mod_source, mod_target_name, mod_label) in enumerate(mod_files):
            if mods_up_to_date[i]:
                print(f"  [mod]   {mod_target_name}  (up to date)")
            else:
                status = (
                    "update"
                    if (target_game_dir / mod_target_name).exists()
                    else "install"
                )
                print(f"  [mod]   {mod_target_name}  ({status})  <-- {mod_label}")

        if always_on:
            print(_dim("\n  The shim will be patched to always-on mode."))

        if not _confirm(_bold("\nConfirm installation? [y/N]: ")):
            print("Aborted.")
            return 1

    # 6. Install shim
    errors = 0
    if not shim_up_to_date:
        try:
            shutil.copy2(source_shim, target_shim)
        except Exception as exc:
            print(_red(f"Error copying shim: {exc}"))
            return 1

        # Optionally patch the shim so it's always enabled (for GOG etc.)
        if always_on:
            try:
                content = target_shim.read_text(encoding="utf-8")
                old_line = 'self.enabled = os.environ.get("VNFLIGHT_ENABLED") == "1"'
                new_line = "self.enabled = True  # patched by install-shim --always-on"
                if old_line in content:
                    content = content.replace(old_line, new_line, 1)
                    target_shim.write_text(content, encoding="utf-8")
                    print(_green(f"  + Installed vnflight.rpy"))
                    print(_dim("    Patched: shim is always enabled (--always-on)."))
                else:
                    print(_green(f"  + Installed vnflight.rpy"))
                    print(
                        _yellow(
                            "    Warning: could not find env-var line to patch. "
                            "You may need to edit vnflight.rpy manually."
                        )
                    )
            except Exception as exc:
                print(_yellow(f"  Warning: shim copied but patching failed: {exc}"))
                print(_dim("    You may need to set self.enabled = True manually."))
        else:
            print(_green(f"  + Installed vnflight.rpy"))

    # 7. Install mods
    for i, (mod_source, mod_target_name, _mod_label) in enumerate(mod_files):
        if mods_up_to_date[i]:
            continue
        mod_target = target_game_dir / mod_target_name
        try:
            shutil.copy2(mod_source, mod_target)
            print(_green(f"  + Installed {mod_target_name}"))
        except Exception as exc:
            print(_red(f"  Error copying {mod_target_name}: {exc}"))
            errors += 1

    # 7b. Invalidate compiled cache so Ren'Py recompiles the new .rpy
    # files on next launch.  Without this, Ren'Py can keep loading a
    # stale .rpyc / bytecode.rpyb that was compiled before this install,
    # making shim changes silently no-op.  Covers both the Ren'Py 7
    # layout (game/cache/bytecode.rpyb) and the Ren'Py 8 layout
    # (game/cache/bytecode-312.rpyb + py3analysis.rpyb + pyanalysis.rpyb
    # + screens.rpyb).
    any_updated = (not shim_up_to_date) or not all(mods_up_to_date)
    if any_updated:
        invalidated = []
        # Delete .rpyc for the installed .rpy files.
        for rpy_path in (
            [target_shim]
            + [target_game_dir / name for _, name, _ in mod_files]
        ):
            rpyc_path = rpy_path.with_suffix(".rpyc")
            if rpyc_path.exists():
                try:
                    rpyc_path.unlink()
                    invalidated.append(rpyc_path.name)
                except Exception:
                    pass
        # Delete whole-project compiled caches; Ren'Py regenerates them
        # on launch.  Keep shaders.txt (non-python cache).
        cache_dir = target_game_dir / "cache"
        if cache_dir.is_dir():
            cache_names = {
                "pyanalysis.rpyb",
                "py3analysis.rpyb",
                "screens.rpyb",
            }
            cache_paths = [
                path for path in cache_dir.glob("bytecode*.rpyb")
                if path.is_file()
            ]
            cache_paths.extend(cache_dir / name for name in cache_names)
            for cache_path in cache_paths:
                if cache_path.exists():
                    try:
                        cache_path.unlink()
                        invalidated.append("cache/" + cache_path.name)
                    except Exception:
                        pass
        if invalidated:
            print(_dim(f"    Cleared compiled cache: {', '.join(invalidated)}"))

    # 8. Summary notes
    if not always_on and not shim_up_to_date:
        print(_dim("\nNote: The shim is only active when VNFLIGHT_ENABLED=1 is set."))
        print(
            _dim(
                "  For GOG or platforms without env-var support, re-run with --always-on."
            )
        )

    # 9. Explicit final verdict — never end without saying what happened.
    total_errors = errors + mod_config_errors
    n_mods = len(mod_files)
    print()
    if total_errors:
        expected_mods = n_mods + mod_config_errors
        print(_red(
            f"✗ Install for '{display_id}' finished with {total_errors} "
            f"error(s): shim + {n_mods - errors}/{expected_mods} mods installed."
        ))
        return 1
    if mods_note and not (
        no_mods or mods_note.startswith("no mods configured")
    ):
        print(_yellow(
            f"⚠ Installed shim for '{display_id}' WITHOUT mods — {mods_note}."
        ))
        return 0
    if mods_note:
        print(_green(f"✓ Installed shim for '{display_id}' ({mods_note})."))
        return 0
    print(_green(
        f"✓ Installed shim + {n_mods} mod{'s' if n_mods != 1 else ''} "
        f"for '{display_id}'."
    ))
    return 0


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------


def _make_session(args: argparse.Namespace, client_state: ClientState) -> BridgeClient:
    """Create a BridgeClient with cursor restored from persistent state.

    The stored bridge admin token must ride along: launch reserves the
    game's slot with a minted token, and the bridge token-gates every
    slot-scoped route on reserved slots — a tokenless session gets 403
    on state/wait/act right after a successful launch.
    """
    slot_hint = getattr(args, "target_slot", None)
    session = BridgeClient(
        args.bridge,
        token=_cli_token(args, client_state, args.bridge),
    )
    if not session.auto_select_slot(slot_hint):
        ambiguous = getattr(session, "_ambiguous_slots", None)
        if ambiguous:
            names = ", ".join(
                f"--slot {s.get('game_id') or s['slot_id']}" for s in ambiguous
            )
            raise ValueError(f"Multiple games connected. Use {names}")
        elif slot_hint:
            raise ValueError(f"Could not resolve --slot '{slot_hint}'; check the bridge and available slots.")
    state_key = args.bridge + session.slot_prefix
    session.cursor = client_state.get_cursor(state_key)
    has_cursor = getattr(client_state, "has_cursor", None)
    session._cli_has_saved_cursor = (
        bool(has_cursor(state_key)) if callable(has_cursor) else session.cursor > 0
    )
    session.last_request_id = client_state.get_last_request_id(state_key)
    # Rows a previous invocation polled past while waiting for a command
    # result but never showed: back into the client's stash, which the
    # next poll drains regardless of the cursor (the MCP server keeps the
    # same stash in memory; the CLI keeps it in the session file).
    deferred = _client_state_deferred_events(client_state, state_key)
    if deferred:
        preserve_prefetched_events(session, deferred)
    _restore_delivered_ledger(session, client_state, state_key)
    return session


def _restore_delivered_ledger(session: BridgeClient, client_state: Any, state_key: str) -> None:
    """Rows an earlier invocation delivered through an act's scoped drain.

    The client filters the ordinary poll by this ledger instead of moving
    the cursor past those rows (a transaction's rows stay drainable by
    nonce).  The MCP server keeps the ledger in memory for its lifetime;
    the CLI keeps it in the session file, or every `wait` after an act
    re-reads the story the act already printed.
    """
    getter = getattr(client_state, "get_delivered_action_events", None)
    if getter is None:  # a minimal test double
        return
    try:
        payload = getter(state_key)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    ownership: set[tuple[Optional[int], int, int]] = set()
    for item in payload.get("ownership") or []:
        try:
            gen, action_id, seq = item
            key = (
                None if gen is None else int(gen),
                int(action_id),
                int(seq),
            )
        except (TypeError, ValueError):
            continue
        if key[1] > 0 and key[2] > 0:
            ownership.add(key)
    if not ownership:
        return
    generation = payload.get("reset_generation")
    if generation is not None:
        try:
            session._action_delivery_reset_generation = int(generation)
        except (TypeError, ValueError):
            pass
    session._delivered_action_event_ownership = ownership
    session._delivered_action_events = {
        (action_id, seq) for _gen, action_id, seq in ownership
    }


def _persist_delivered_ledger(session: BridgeClient, client_state: Any, state_key: str) -> None:
    ownership = getattr(session, "_delivered_action_event_ownership", None) or set()
    generation = getattr(session, "_action_delivery_reset_generation", None)
    payload = {
        "reset_generation": generation,
        "ownership": sorted(
            [
                [None if g is None else int(g), int(a), int(s)]
                for g, a, s in ownership
            ],
            key=lambda item: (-1 if item[0] is None else item[0], item[1], item[2]),
        ),
    }
    setter = getattr(client_state, "set_delivered_action_events", None)
    if setter is not None:  # absent only on a minimal test double
        setter(state_key, payload)


def _client_state_deferred_events(client_state: Any, state_key: str) -> list:
    getter = getattr(client_state, "get_deferred_events", None)
    if getter is None:  # a minimal test double
        return []
    try:
        return list(getter(state_key) or [])
    except Exception:
        return []


def _save_session(
    session: BridgeClient, args: argparse.Namespace, client_state: ClientState
) -> None:
    """Persist cursor, request ID and the unconsumed event stash.

    A verb that waited for a nonce-matched ``command_result`` may have
    polled past story rows; the client stashes those in
    ``_prefetched_events`` for its next poll and advances the cursor.  The
    MCP server is long-lived and drains that stash on its next wait; this
    process is about to exit.  Persist the stash itself so the next
    invocation restores it (``_make_session``) and its first poll shows
    those rows.  Rewinding the cursor instead is not enough: an explicit
    ``--slot`` wait attaches at the active decision and fast-forwards the
    cursor past them again, while the stash survives attachment.
    """
    state_key = session.bridge_url + session.slot_prefix
    client_state.set_cursor(state_key, session.cursor)
    setter = getattr(client_state, "set_deferred_events", None)
    if setter is not None:  # absent only on a minimal test double
        setter(state_key, list(getattr(session, "_prefetched_events", None) or []))
    _persist_delivered_ledger(session, client_state, state_key)
    client_state.set_last_request_id(state_key, session.last_request_id)
    client_state.save()


def _mark_current_events_seen_for_explicit_wait(
    session: BridgeClient,
    args: argparse.Namespace,
) -> None:
    """Attach once; subsequent CLI processes must drain their saved continuation."""
    if not getattr(args, "target_slot", None):
        return
    if getattr(session, "_cli_has_saved_cursor", False):
        return
    def _warn(error: str) -> None:
        _wait_trace(
            args,
            "mark_current_events_seen_failed",
            error=error,
        )

    session.attach_to_running_slot(warn=_warn)


def _output(args: argparse.Namespace, text: str = "", data: Any = None):
    """Print human-readable text or JSON depending on --json flag."""
    if args.json:
        if data is not None:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        else:
            print(json.dumps({"message": text}, ensure_ascii=False))
    else:
        if text:
            # In quiet mode, only print non-confirmation messages
            if getattr(args, "quiet", False):
                # Suppress success confirmations ("✓ Choice submitted:"
                # etc.) only. Failures (✗) must stay visible — quiet
                # mode hiding a failed act leaves just an exit code.
                if "✓" in text and "✗" not in text:
                    return
            print(text)


def _bridge_is_up(session: Any) -> bool:
    """Probe bridge reachability (duck-typed for test fakes).

    Sessions without an ``is_up`` probe are treated as up so read-only
    commands keep their current behavior with minimal fakes.
    """
    probe = getattr(session, "is_up", None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:
        return False


def _bridge_unreachable_error(args: argparse.Namespace, session: Any) -> int:
    """Report a dead bridge loudly and return a non-zero exit code.

    A dead bridge previously looked like quiet success ("no events",
    "no state", exit 0) because the HTTP client swallows connection
    errors.  A new user's first debugging experience must not be a
    command that silently pretends the game is idle.
    """
    msg = (
        f"Bridge unreachable at {session.bridge_url}. "
        "No bridge is listening there — the game or bridge process may "
        "have exited. Launch a game first ('vnflight.py launch <game>') "
        "or pass the right --bridge URL."
    )
    _output(
        args,
        _red(f"✗ {msg}"),
        {
            "success": False,
            "error": "bridge_unreachable",
            "message": msg,
            "bridge": session.bridge_url,
        },
    )
    return 1


def _slot_access_denied(session: Any) -> bool:
    """True when the session's last data request was rejected with 403."""
    return getattr(session, "last_http_status", None) == 403


def _access_denied_error(args: argparse.Namespace, session: Any) -> int:
    """Report a 403 (reserved slot / require-token bridge) distinctly.

    A 403 used to collapse into {}/None in BridgeClient and render as
    "no game is connected. Check 'vnflight.py slots'" — misleading: the
    game is right there, the caller just lacks the slot's token.
    """
    slot = str(getattr(session, "slot_prefix", "") or "").lstrip("/")
    slot_part = f"slot '{slot}'" if slot else "the target slot"
    detail = getattr(session, "last_http_error", None)
    detail_part = f" Bridge said: {detail}" if detail else ""
    msg = (
        f"Access denied — {slot_part} is reserved (or the bridge requires "
        "a token). Provide the slot's token or the bridge admin token via "
        f"--token or VNFLIGHT_TOKEN.{detail_part}"
    )
    _output(
        args,
        _red(f"✗ {msg}"),
        {
            "success": False,
            "error": "access_denied",
            "message": msg,
            "bridge": session.bridge_url,
            "slot": slot or None,
        },
    )
    return 1


def _wait_trace(args: argparse.Namespace, event: str, **details: Any) -> None:
    """Emit compact wait-loop diagnostics without changing normal output."""
    if not (
        getattr(args, "trace_wait", False)
        or os.environ.get("VNFLIGHT_TRACE_WAIT")
    ):
        return
    payload = {"event": event}
    payload.update(details)
    print(
        "[wait-trace] " + json.dumps(payload, ensure_ascii=False, default=str),
        file=sys.stderr,
        flush=True,
    )


def _config_warnings(config: Optional[dict], games_dir: Optional[str]) -> list:
    """One-line warnings about a config that loads but cannot work as is."""
    warnings: list = []
    raw = (config or {}).get(MODS_MANIFEST_KEY)
    if isinstance(raw, str) and raw.strip():
        root = Path(games_dir) if games_dir else _find_project_root()
        path = _resolve_manifest_path(config or {}, root)
        if path is None or not path.exists():
            warnings.append(
                f"{MODS_MANIFEST_KEY}: '{raw}' does not exist; point it at an "
                "adapter manifest.json (fetch-mods downloads one) or remove "
                "the key. See docs/USER.md, Adapters."
            )
    return warnings


def cmd_games(args: argparse.Namespace, client_state: ClientState) -> int:
    config, _config_error = _load_config_with_error(args.games_dir)
    for warning in _config_warnings(config, args.games_dir):
        # stderr, so --json stdout stays one object.
        print(_yellow(f"Warning: {warning}"), file=sys.stderr)
    games = discover_games(args.games_dir)
    if not games:
        if config is not None:
            # A config that exists but lists no game (the untouched
            # template) is a normal first step, not an error.
            hint = (
                f'No games configured yet; add one under "games" in '
                f"{CONFIG_FILENAME} (see README, Quick Install)."
            )
            _output(args, _yellow(hint), {"games": [], "hint": hint})
            return 0
        _output(
            args,
            _red("No games found.") if not args.json else "No games found.",
            {"games": []},
        )
        return 1

    if args.json:
        _output(
            args,
            data={
                "games": [
                    {
                        "id": g["id"],
                        "short_desc": g["short_desc"],
                        "has_briefing": g["has_briefing"],
                        "mods": g.get("mods"),
                    }
                    for g in games
                ]
            },
        )
    else:
        print(_bold("Available games:"))
        for g in games:
            desc = g["short_desc"] or "(no briefing)"
            mods = g.get("mods")
            suffix = f"  [{mods}]" if mods and mods != "no adapters" else ""
            print(f"  {_green(g['id']):40s} {desc}{suffix}")
        print()
        hint = cli_command_hint()
        print(f"Use '{_cyan(hint + ' info <game>')}' for the full briefing.")
        print(f"Use '{_cyan(hint + ' launch <game>')}' to start playing.")
    return 0


def _stored_admin_token(client_state: ClientState, bridge_url: str) -> str | None:
    getter = getattr(client_state, "get_admin_token", None)
    stored = getter(bridge_url) if callable(getter) else None
    # Env fallback: an MCP server (or user) that owns a token-gated
    # bridge exports VNFLIGHT_TOKEN so CLI subprocesses inherit access.
    return stored or os.environ.get("VNFLIGHT_TOKEN") or None


def _cli_token(
    args: argparse.Namespace,
    client_state: ClientState,
    bridge_url: str,
) -> str | None:
    """Resolve the token for CLI-built sessions.

    Explicit --token wins; otherwise the stored admin token for this
    bridge, then the VNFLIGHT_TOKEN environment fallback.
    """
    explicit = getattr(args, "token", None)
    if explicit:
        return explicit
    return _stored_admin_token(client_state, bridge_url)


def cmd_slots(args: argparse.Namespace, client_state: ClientState) -> int:
    """List active game slots on the bridge."""
    admin_token = _cli_token(args, client_state, args.bridge)
    client = (
        BridgeClient(args.bridge, token=admin_token)
        if admin_token
        else BridgeClient(args.bridge)
    )
    slot_list = client.list_slots()
    if slot_list is None:
        if _slot_access_denied(client):
            return _access_denied_error(args, client)
        _output(
            args,
            _red("Could not reach bridge or bridge does not support slots."),
            {"success": False, "error": "unreachable"},
        )
        return 1
    if getattr(args, "reap_stale", False):
        results = _reap_stale_slots(client, slot_list)
        if args.json:
            _output(args, data={"slots": slot_list, "reap": results})
        else:
            if not results:
                print("No stale game slots to reap.")
            else:
                for result in results:
                    status = result["status"]
                    prefix = _green("freed") if result["freed"] else _yellow("kept")
                    reason = result.get("reason") or status
                    detail = result.get("detail")
                    line = (
                        f"{prefix}: slot {result['slot_id']} "
                        f"{result.get('game_id') or '?'} ({reason})"
                    )
                    if detail:
                        line += f": {detail}"
                    print(line)
        return 0 if all(result["ok"] for result in results) else 1
    if not slot_list:
        _output(args, "No active game slots.", {"slots": []})
        return 0
    if args.json:
        _output(args, data={"slots": slot_list})
    else:
        print(_bold("Active game slots:"))
        for s in slot_list:
            status = s.get("status", "?")
            pending = " [PENDING]" if s.get("has_pending_request") else ""
            print(
                f"  Slot {s['slot_id']:2d}  {_green(s.get('game_id', '?')):30s}  "
                f"{status}{pending}  (events: {s.get('event_counter', 0)})"
            )
    return 0


def _slot_stale_reason(slot: dict) -> str | None:
    status = slot.get("status")
    if status == "ended":
        return "ended"
    pid = slot.get("game_pid")
    if pid:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return None
        if not _is_process_alive(pid_int):
            return "dead_pid"
    return None


def _reap_stale_slots(client: BridgeClient, slot_list: list[dict]) -> list[dict]:
    results: list[dict] = []
    for slot in slot_list:
        reason = _slot_stale_reason(slot)
        if reason is None:
            continue
        slot_id = slot.get("slot_id")
        ok, data = client.free_slot(slot_id)
        if not ok and _slot_free_already_gone(data):
            ok = True
        results.append({
            "slot_id": slot_id,
            "game_id": slot.get("game_id"),
            "status": slot.get("status"),
            "game_pid": slot.get("game_pid"),
            "reason": reason,
            "freed": ok,
            "ok": ok,
            "detail": data.get("message") or data.get("error") or "",
        })
    return results


def cmd_info(args: argparse.Namespace, client_state: ClientState) -> int:
    games = discover_games(args.games_dir)
    game = next((g for g in games if g["id"] == args.game), None)
    if not game:
        _output(
            args,
            _red(f"Game '{args.game}' not found."),
            {"error": f"Game '{args.game}' not found."},
        )
        return 1
    if not game["has_briefing"]:
        _output(
            args,
            _yellow(f"No briefing found for '{args.game}'."),
            {"error": f"No briefing for '{args.game}'"},
        )
        return 1

    if args.json:
        _output(args, data={"game": args.game, "briefing": game["briefing"]})
    else:
        print(game["briefing"])
    return 0


def _explicit_launch_flag_profile_overrides(
    args: argparse.Namespace,
) -> Dict[str, str]:
    """Profile keys that explicit launch flags must win over.

    ``launch <game> --fast-forward`` sends ``fast_forward_on`` right after
    the game connects; auto-applying a default profile whose
    ``fast_forward: false`` afterwards silently reverted the explicit flag
    (self-clobber).  Same interplay for ``--auto`` vs the profile's
    ``auto_advance`` keys.  Returns {profile_key: flag} for every conflict.
    """
    skip_keys: Dict[str, str] = {}
    if getattr(args, "fast_forward", False):
        skip_keys["fast_forward"] = "--fast-forward"
    if getattr(args, "auto", False):
        skip_keys["auto_advance"] = "--auto"
        skip_keys["auto_advance_on_start"] = "--auto"
    return skip_keys


def _auto_apply_default_profile(
    args: argparse.Namespace,
    session: BridgeClient,
    client_state: ClientState,
    *,
    emit: bool = True,
) -> Dict[str, Any]:
    """If the game has a default_profile in config, apply it silently.

    Profile keys that conflict with explicit launch flags are skipped so
    the flag the user just typed survives the auto-applied profile.
    """
    if getattr(args, "defer_default_profile", False):
        return {}
    config = _load_config(getattr(args, "games_dir", None))
    if not config:
        return {}
    game_cfg = (config.get("games") or {}).get(args.game, {})
    if not isinstance(game_cfg, dict):
        game_cfg = {}
    profile_name = game_cfg.get("default_profile")
    if not profile_name:
        return {}
    skip_keys = _explicit_launch_flag_profile_overrides(args)
    ok, msg, _data = _apply_profile(
        profile_name,
        session,
        args,
        client_state,
        quiet=True,
        skip_keys=skip_keys,
    )
    if ok:
        skipped = _data.get("skipped") or []
        kept_note = ""
        if skipped:
            kept_flags = sorted({entry["reason"] for entry in skipped})
            kept_note = f" (kept {', '.join(kept_flags)})"
        result = {
            "profile_applied": profile_name,
            "profile_skipped_keys": skipped,
        }
        if emit:
            _output(
                args,
                _green(f"✓ Auto-applied profile '{profile_name}'{kept_note}"),
                result,
            )
        return result
    else:
        result = {"profile_error": msg, "profile_name": profile_name}
        if emit:
            _output(
                args,
                _red(f"⚠ Default profile '{profile_name}': {msg}"),
                result,
            )
    return result


def _has_default_profile(args: argparse.Namespace) -> bool:
    """Return whether this launch has child-owned default-profile work."""
    if getattr(args, "defer_default_profile", False):
        return False
    config = _load_config(getattr(args, "games_dir", None))
    if not config:
        return False
    game_cfg = (config.get("games") or {}).get(args.game, {})
    return isinstance(game_cfg, dict) and bool(game_cfg.get("default_profile"))


def cmd_launch(args: argparse.Namespace, client_state: ClientState) -> int:
    fast_forward = getattr(args, "fast_forward", False)
    auto_advance = getattr(args, "auto", False)
    timeout = getattr(args, "timeout", None)
    save_slot = getattr(args, "save_slot", None)
    launch_diagnostics: Dict[str, Any] = {}
    success, msg, _slot_id = launch_game(
        args.game,
        args.bridge,
        args.games_dir,
        fast_forward,
        auto_advance,
        client_state,
        connect_timeout=timeout,
        save_slot=save_slot,
        token=getattr(args, "token", None),
        reservation_token=getattr(args, "reservation_token", None),
        diagnostics=launch_diagnostics,
        debug=getattr(args, "shim_debug", None),
    )
    if success:
        if _slot_id is not None and not getattr(args, "target_slot", None):
            # Bind post-launch display/wait to the slot we just created.
            # Otherwise a multi-game bridge can make the follow-up state
            # lookup ambiguous even though launch itself succeeded.
            setattr(args, "target_slot", str(_slot_id))
        quiet = getattr(args, "quiet", False)
        quiet_profile_result: Dict[str, Any] = {}
        if quiet and _has_default_profile(args):
            # Quiet is an output policy, not a launch-policy switch. Apply the
            # configured profile before emitting the one JSON launch receipt.
            session = _make_session(args, client_state)
            quiet_profile_result = _auto_apply_default_profile(
                args, session, client_state, emit=False,
            )
        # Suppress launch confirmation in quiet mode.
        if not quiet:
            _output(
                args,
                _green(f"✓ {msg}"),
                {"success": True, "message": msg, **launch_diagnostics},
            )
        else:
            receipt = {
                "success": True,
                "message": msg,
                "slot_id": _slot_id,
            }
            receipt.update(launch_diagnostics)
            receipt.update(quiet_profile_result)
            _output(args, data=receipt)
        if getattr(args, "wait", False):
            session = _make_session(args, client_state)
            if not quiet:
                _auto_apply_default_profile(args, session, client_state)
            wait_started_at = time.monotonic()
            rc = _perform_wait(
                session, args, client_state, getattr(args, "timeout", None)
            )
            if args.json:
                # A separate, later JSON object rather than threading this
                # through _perform_wait's many internal exit points -- it
                # already emits the wait outcome itself.  first_choice_after_s
                # is elapsed time regardless of outcome (success or timeout);
                # callers that want "did it actually reach a choice" still
                # read the preceding status/event JSON.
                _output(
                    args,
                    data={
                        "first_choice_after_s": round(
                            time.monotonic() - wait_started_at, 3
                        ),
                    },
                )
            return rc
        elif not getattr(args, "quiet", False):
            # Show main menu options after launch (poll briefly).
            session = _make_session(args, client_state)
            for _ in range(10):
                time.sleep(0.5)
                state = session.state()
                ctx = (state.get("context") if state else None) or {}
                if ctx.get("context") == "main_menu":
                    _auto_apply_default_profile(args, session, client_state)
                    _save_session(session, args, client_state)
                    if args.json:
                        _output(args, data={"context": ctx})
                    else:
                        print()
                        quiet = getattr(args, "quiet", False)
                        print(format_main_menu(ctx, colour=True, quiet=quiet))
                    break
    else:
        error_data = {"success": False, "error": msg}
        error_data.update(launch_diagnostics)
        if _slot_id is not None:
            error_data.update({
                "partial_launch": True,
                "slot_id": _slot_id,
            })
        _output(args, _red(f"✗ {msg}"), error_data)
    return 0 if success else 1


def cmd_stop(args: argparse.Namespace, client_state: ClientState) -> int:
    # Positional 'game' arg takes priority over global --slot.
    target_slot = getattr(args, "game", None) or getattr(args, "target_slot", None)
    if target_slot:
        # Stop a specific game by slot, not the whole bridge.
        admin_token = _cli_token(args, client_state, args.bridge)
        session = (
            BridgeClient(args.bridge, token=admin_token)
            if admin_token
            else BridgeClient(args.bridge)
        )
        our_slot = session.resolve_slot_info(target_slot)
        if not our_slot:
            _output(
                args,
                _red(f"No slot matching '{target_slot}'"),
                {"success": False, "error": f"No slot '{target_slot}'"},
            )
            return 1
        # Send quit command to that slot.
        slot_session = (
            BridgeClient(args.bridge, slot=our_slot["slot_id"], token=admin_token)
            if admin_token
            else BridgeClient(args.bridge, slot=our_slot["slot_id"])
        )
        slot_session._send_command("quit")
        time.sleep(1.0)
        # Kill the game PID if still alive -- only a process that still looks
        # like this game (the bridge's slot record names the game id; the
        # launch record, when this checkout made it, names the executable and
        # directories too).
        game_pid = our_slot.get("game_pid")
        if game_pid and _is_process_alive(game_pid):
            get_pids = getattr(client_state, "get_pids", None)
            record = (get_pids(args.bridge) if callable(get_pids) else None) or {}
            expect = None
            if record.get("game") == game_pid:
                expect, _note = pid_identity_from_record(record, "game")
            if not expect:
                expect = game_identity_expectation(
                    our_slot.get("game_id") or "", None, None, our_slot.get("name"))
            if not kill_process(game_pid, expect):
                game_id = our_slot.get("game_id", "?")
                _output(
                    args,
                    _red(
                        f"✗ Could not stop '{game_id}' process "
                        f"(PID {game_pid}); slot {our_slot['slot_id']} left intact"
                    ),
                    {
                        "success": False,
                        "game_id": game_id,
                        "slot_id": our_slot["slot_id"],
                        "game_pid": game_pid,
                        "error": "process still alive",
                    },
                )
                return 1
        # Free the slot.  Treat failure as a real stop failure because a stale
        # bridge slot can make the next launch/load operate on old game state.
        ok, data = session.free_slot(our_slot["slot_id"])
        game_id = our_slot.get("game_id", "?")
        if not ok and _slot_free_already_gone(data):
            ok = True
        if not ok:
            detail = _slot_free_detail(data)
            _output(
                args,
                _red(
                    f"✗ Stopped process for '{game_id}' but could not free "
                    f"slot {our_slot['slot_id']}: {detail}"
                ),
                {
                    "success": False,
                    "game_id": game_id,
                    "slot_id": our_slot["slot_id"],
                    "error": detail,
                },
            )
            return 1
        remove_token = getattr(client_state, "remove_slot_token", None)
        if callable(remove_token):
            try:
                remove_token(
                    args.bridge,
                    our_slot["slot_id"],
                    reservation_id=our_slot.get("reservation_id"),
                )
            except TypeError:
                remove_token(args.bridge, our_slot["slot_id"])
            client_state.save()
        _output(
            args,
            _green(f"✓ Stopped '{game_id}' (slot {our_slot['slot_id']})"),
            {"success": True, "game_id": game_id, "slot_id": our_slot["slot_id"]},
        )
        return 0
    # No --slot: stop everything.
    success, msg = stop_game(args.bridge, client_state)
    _output(
        args,
        _green(f"✓ {msg}") if success else _red(f"✗ {msg}"),
        {"success": success, "message": msg},
    )
    return 0 if success else 1


def cmd_prompt(args: argparse.Namespace, client_state: ClientState) -> int:
    games = discover_games(args.games_dir)
    game = next((g for g in games if g["id"] == args.game), None)
    if not game:
        _output(
            args,
            _red(f"Game '{args.game}' not found."),
            {"error": f"Game '{args.game}' not found."},
        )
        return 1

    prompt_text = generate_prompt(game)
    if args.json:
        _output(args, data={"game": args.game, "prompt": prompt_text})
    else:
        print(prompt_text)
    return 0


def _reset_cursor_for_full_read(session: BridgeClient) -> None:
    """Re-read from the start of the bridge's history.

    Also drops the client's event stash (rows a previous invocation polled
    past, restored by ``_make_session``): a full re-read serves those rows
    again from the bridge, and a load that replaced the timeline made them
    stale.  Keeping the stash made ``poll --all`` return only the stash,
    save cursor 0, and repeat the rows on the next poll.
    """
    session.cursor = 0
    ActionDeliveryOwnership._clear_held_events(session)


def cmd_poll(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    timeout = getattr(args, "wait_timeout", 0) or 0
    show_all = getattr(args, "all", False)

    if show_all:
        _reset_cursor_for_full_read(session)

    events = session.poll(timeout=timeout)
    _save_session(session, args, client_state)

    if not events:
        if _slot_access_denied(session):
            return _access_denied_error(args, session)
        if not _bridge_is_up(session):
            return _bridge_unreachable_error(args, session)
        _output(args, _dim("(no new events)"), {"events": [], "event_count": 0})
        return 0

    if args.json:
        _output(args, data={"events": events, "event_count": len(events)})
    else:
        quiet = getattr(args, "quiet", False)
        verbose = getattr(args, "verbose", False)
        print(format_events(events, colour=True, quiet=quiet, verbose=verbose))
    return 0


def _freshen_choice_request_events(
    events: list[dict],
    session: BridgeClient,
) -> list[dict]:
    """Replace stale choice_request choices with the current screen choices."""
    freshened: list[dict] = []
    for event in events:
        if event.get("type") != "choice_request":
            freshened.append(event)
            continue

        interactions = None
        has_disabled_choice = any(
            item.get("is_disabled")
            for item in event.get("full_items", [])
            if isinstance(item, dict)
        )
        attempts = 3 if has_disabled_choice else 1
        for attempt in range(attempts):
            interactions = _get_latest_interactions(
                session,
                event,
                overlay_active=False,
            )
            event_interactions = event.get("interactions")
            normalized_event_interactions = (
                _normalize_interaction_disabled(event_interactions)
                if event_interactions
                else event_interactions
            )
            if (
                interactions is not None
                and interactions != normalized_event_interactions
            ):
                break
            if attempt + 1 < attempts:
                time.sleep(0.3)

        event_interactions = event.get("interactions")
        event_has_interactions = "interactions" in event
        normalized_event_interactions = (
            _normalize_interaction_disabled(event_interactions)
            if event_interactions
            else event_interactions
        )
        if interactions is None or interactions == normalized_event_interactions:
            freshened.append(event)
            continue

        choice_items = [
            i for i in interactions
            if i.get("type") == "choice"
        ]
        if (
            not choice_items
            and not event_has_interactions
            and event.get("choices")
        ):
            live_screen = _get_live_screen(session)
            if not (live_screen and live_screen.get("interactions") == []):
                freshened.append(event)
                continue
        event_choice_items = [
            i for i in (normalized_event_interactions or [])
            if i.get("type") == "choice"
        ]
        if (
            event_choice_items
            and _choice_item_signature(choice_items)
            == _choice_item_signature(event_choice_items)
        ):
            freshened.append(event)
            continue

        updated = dict(event)
        updated["interactions"] = interactions
        updated["choices"] = [
            i.get("display_label", "")
            for i in choice_items
            if not i.get("disabled") and i.get("display_label")
        ]
        updated["full_items"] = [
            {
                "label": i.get("display_label", ""),
                "is_disabled": bool(i.get("disabled")),
                "is_caption": False,
            }
            for i in choice_items
            if i.get("display_label")
        ]
        freshened.append(updated)
    return freshened


def _choice_item_signature(items: list[dict]) -> list[tuple[str, bool]]:
    return [
        (
            str(i.get("display_label") or "").strip(),
            bool(i.get("disabled")),
        )
        for i in items
    ]


def _choice_request_label_signature(pending: Optional[dict]) -> tuple[str, ...]:
    if not pending:
        return ()
    source = pending.get("full_items") or pending.get("choices") or []
    labels: list[str] = []
    for item in source:
        if isinstance(item, dict):
            label = item.get("label") or item.get("display_label") or ""
        else:
            label = str(item)
        label = str(label).strip()
        if label:
            labels.append(label)
    return tuple(labels)


def _pending_matches_choice_signature(
    pending: Optional[dict],
    signature: tuple[str, ...],
) -> bool:
    return bool(
        pending
        and pending.get("type") == "choice_request"
        and signature
        and _choice_request_label_signature(pending) == signature
    )


class _ScreenContentFilterResult(NamedTuple):
    event: Optional[dict]
    deferred_screen_prompt: Optional[dict]
    post_action_story_seen: bool
    fresh_story_seen: bool


def _cli_screen_story_parts(event: dict) -> tuple[list, Optional[list]]:
    """Separate ordinary screen text from bridge-owned passive occurrences.

    Drained events may deliver the returned delta. Snapshot-only callers must
    use only ordinary text: the cursor lane owns delivery, not latest-state reads.
    None means the bridge supplied no usable passive ownership metadata.
    """
    texts = list(event.get("texts") or [])
    delta = event.get("passive_overlay_delta")
    passive = event.get("overlay_texts")
    if not isinstance(delta, list) or not isinstance(passive, list):
        return texts, None
    remaining = [str(row).strip() for row in passive]
    ordinary = []
    for row in texts:
        normalized = str(row).strip()
        if normalized in remaining:
            remaining.remove(normalized)
        else:
            ordinary.append(row)
    return ordinary, list(delta)


def _filter_screen_content_event(
    args: argparse.Namespace,
    event: dict,
    *,
    stale_screen_label: Optional[str],
    initial_grace: float,
    post_action_story_seen: bool,
    fresh_story_seen: bool,
    story_wait_until: float,
    initial_screen_texts: set[str],
    narr_texts_cumulative: set[str],
    seen_sc_keys: set[tuple[str, ...]],
) -> _ScreenContentFilterResult:
    """
    Filter a screen_content event before printing it from wait.

    Intentionally impure: reads the clock and traces deferred prompts. Mutates
    ``narr_texts_cumulative`` and ``seen_sc_keys`` to preserve the running
    de-dup behavior across wait loop iterations.
    """
    empty = _ScreenContentFilterResult(
        None,
        None,
        post_action_story_seen,
        fresh_story_seen,
    )
    if _screen_event_contains_label(event, stale_screen_label):
        return empty
    # Skip lightweight say-progress events; the dialogue event already covers
    # the same text.
    if event.get("_lightweight"):
        return empty
    ordinary, durable_rows = _cli_screen_story_parts(event)
    if not event.get("texts") and not durable_rows and not _screen_event_has_actionable_prompt(event):
        return empty
    if (
        initial_grace > 0
        and not post_action_story_seen
        and time.time() < story_wait_until
        and _screen_event_has_actionable_prompt(event)
        and not event.get("texts")
        and not durable_rows
    ):
        # Topic/shop screens can refresh before the narration produced by the
        # action arrives. Do not print or return the refreshed prompt until the
        # story grace window has had a chance to drain.
        _wait_trace(
            args,
            "defer_screen_prompt",
            screens=event.get("screens") or [],
        )
        return _ScreenContentFilterResult(
            None,
            event,
            post_action_story_seen,
            fresh_story_seen,
        )

    # Durable passive rows already have occurrence ownership at the bridge.
    # A cumulative panel may reappear long after our per-call text cache expired.
    # Remove only its snapshot rows, leaving unrelated screen text untouched.
    if durable_rows is not None:
        _wait_trace(args, "passive_event_projection", seq=event.get("_seq"),
                    snapshot_rows=len(event.get("overlay_texts") or []),
                    delta_rows=len(durable_rows))
        event = dict(event, texts=ordinary)

    # Strip texts already covered by dialogue events or previously displayed
    # screen_content. Screen scrape texts may have "Speaker: text" format while
    # dialogue events have just "text".
    if event.get("texts"):

        def _sc_text_is_dup(text: str) -> bool:
            stripped = text.strip()
            if stripped in narr_texts_cumulative:
                return True
            # Strip "Speaker: " prefix for comparison.
            if ": " in stripped:
                _, rest = stripped.split(": ", 1)
                if rest in narr_texts_cumulative:
                    return True
            return False

        filtered = [t for t in event["texts"] if not _sc_text_is_dup(t)]
        if any(
            str(t).strip() and str(t).strip() not in initial_screen_texts
            for t in filtered
        ):
            post_action_story_seen = True
            fresh_story_seen = True
        if filtered != event.get("texts"):
            event = dict(event, texts=filtered)
        # All texts were duplicates; skip this event entirely unless it has
        # buttons/interactions to show.
        if not filtered and not durable_rows and not event.get("buttons") and not event.get("interactions"):
            return _ScreenContentFilterResult(
                None,
                None,
                post_action_story_seen,
                fresh_story_seen,
            )
        # Whole-event dedup: if the exact same set of (filtered) texts was
        # already shown, skip. Catches NVL re-scrapes and stale resends.
        screen_key = tuple(t.strip() for t in filtered)
        if screen_key in seen_sc_keys and not durable_rows:
            return _ScreenContentFilterResult(
                None,
                None,
                post_action_story_seen,
                fresh_story_seen,
            )
        seen_sc_keys.add(screen_key)
        # Track displayed screen_content texts so subsequent resends are caught
        # as duplicates.
        for filtered_text in filtered:
            stripped = filtered_text.strip()
            if stripped:
                narr_texts_cumulative.add(stripped)
                if ": " in stripped:
                    _, without_speaker = stripped.split(": ", 1)
                    narr_texts_cumulative.add(without_speaker)

    if durable_rows is not None:
        # Do not text-deduplicate fresh occurrences, including legitimate repeats.
        event = dict(event, texts=durable_rows + list(event.get("texts") or []))
        if durable_rows:
            post_action_story_seen = True
            fresh_story_seen = True
        if not event["texts"] and not event.get("buttons") and not event.get("interactions"):
            return empty

    return _ScreenContentFilterResult(
        event,
        None,
        post_action_story_seen,
        fresh_story_seen,
    )


def _screen_content_event_for_display(
    event: dict,
    *,
    has_choice_req: bool,
    defer_actionable_prompt: bool = False,
) -> dict:
    """Strip button/action payloads when another phase owns prompt rendering."""
    if (
        (has_choice_req or defer_actionable_prompt)
        and (event.get("buttons") or event.get("interactions"))
    ):
        stripped = dict(event)
        stripped.pop("buttons", None)
        stripped.pop("interactions", None)
        return stripped
    return event


class _StalePendingHardCheckResult(NamedTuple):
    pending: Optional[dict]
    should_continue: bool


def _drain_stale_pending_with_trace(
    session: BridgeClient,
    args: argparse.Namespace,
    pending: dict,
    *,
    deadline: float,
    by: str,
) -> Optional[dict]:
    """Drain stale pending request while emitting consistent trace events."""
    remaining = max(0.0, deadline - time.time())
    stale_id = pending.get("id")
    _wait_trace(
        args,
        "drain_stale_pending_start",
        id=stale_id,
        by=by,
        timeout=round(min(8.0, remaining), 3),
    )
    fresh_pending = drain_stale_pending_request(
        session,
        pending,
        timeout=min(8.0, remaining),
    )
    _wait_trace(
        args,
        "drain_stale_pending_done",
        old_id=stale_id,
        new_id=(fresh_pending or {}).get("id"),
    )
    return fresh_pending


def _stale_pending_hard_check_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    pending: Optional[dict],
    *,
    initial_grace: float,
    skip_request_id: Optional[str],
    skip_choice_signature: tuple[str, ...],
    post_action_story_seen: bool,
    fresh_story_seen: bool,
    transition_since_story: bool,
    story_wait_until: float,
    deadline: float,
) -> _StalePendingHardCheckResult:
    """Drain or suppress stale pending prompts at the hard-check boundary.

    Intentionally impure: may poll/drain the bridge and emits wait trace events.
    It returns `should_continue` when the caller should keep waiting instead of
    exposing a same-signature prompt during story grace.
    """
    if pending is not None and initial_grace > 0:
        pending_matches_answered_request = bool(
            (skip_request_id and pending.get("id") == skip_request_id)
            or _pending_matches_choice_signature(pending, skip_choice_signature)
        )
        if pending_matches_answered_request:
            pending = _drain_stale_pending_with_trace(
                session,
                args,
                pending,
                deadline=deadline,
                by="answered_request",
            )
    if (
        pending is not None
        and skip_request_id
        and pending.get("id") == skip_request_id
        and not post_action_story_seen
        and time.time() < story_wait_until
    ):
        _wait_trace(args, "suppress_same_id_pending", id=pending.get("id"))
        pending = None
    if (
        pending is not None
        and _pending_matches_choice_signature(pending, skip_choice_signature)
        and (not fresh_story_seen or transition_since_story)
        and time.time() < story_wait_until
    ):
        _wait_trace(
            args,
            "suppress_same_signature_pending",
            id=pending.get("id"),
            story_wait_remaining=round(
                max(0.0, story_wait_until - time.time()),
                3,
            ),
        )
        return _StalePendingHardCheckResult(pending, True)
    return _StalePendingHardCheckResult(pending, False)


class _FinalDrainBeforePendingResult(NamedTuple):
    post_action_story_seen: bool
    fresh_story_seen: bool
    transition_since_story: bool
    story_wait_until: float
    should_continue: bool


class _LatestScreenTextBeforePendingResult(NamedTuple):
    post_action_story_seen: bool
    fresh_story_seen: bool


def _final_drain_before_pending_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    pending: dict,
    *,
    initial_grace: float,
    post_action_story_seen: bool,
    fresh_story_seen: bool,
    transition_since_story: bool,
    story_wait_until: float,
    initial_screen_texts: set[str],
    narr_texts_cumulative: set[str],
    seen_sc_keys: set[tuple[str, ...]],
    printed_ids: set[str],
    colour: bool,
    quiet: bool,
    skip_stale_game_ended_event: Any,
) -> _FinalDrainBeforePendingResult:
    """Drain buffered story before printing the live pending prompt.

    Intentionally impure: polls the bridge, mutates de-dup/printed-id sets,
    traces, and may print drained story text. It asks the caller to continue
    when story grace is still waiting for late narration.
    """
    # get_pending() can see the choice_request before get_new_events() delivers
    # the preceding dialogue events.
    drain_timeout = (
        1.5
        if initial_grace > 0 and (not fresh_story_seen or transition_since_story)
        else 0.5
    )
    drain_events = session.poll(timeout=drain_timeout)
    if not drain_events:
        return _FinalDrainBeforePendingResult(
            post_action_story_seen,
            fresh_story_seen,
            transition_since_story,
            story_wait_until,
            False,
        )

    _wait_trace(
        args,
        "final_drain",
        count=len(drain_events),
        timeout=drain_timeout,
    )
    drain_display = []
    for index, event in enumerate(drain_events):
        if skip_stale_game_ended_event(drain_events, index):
            _wait_trace(args, "skip_stale_game_ended_event")
            continue
        event_type = event.get("type", "")
        if event_type in (
            "nvl_clear",
            "scene",
            "show",
            "hide",
            "stats_update",
        ):
            transition_since_story = True
            story_wait_until = max(
                story_wait_until,
                time.time() + min(initial_grace, 5.0),
            )
        if event_type == "screen_content":
            filtered = _filter_screen_content_event(
                args, event, stale_screen_label=None, initial_grace=0,
                post_action_story_seen=post_action_story_seen,
                fresh_story_seen=fresh_story_seen, story_wait_until=story_wait_until,
                initial_screen_texts=initial_screen_texts,
                # Screen text cannot claim a later dialogue occurrence merely
                # because its wording matches. Keep drain bookkeeping one-way.
                narr_texts_cumulative=set(narr_texts_cumulative), seen_sc_keys=seen_sc_keys,
            )
            event = filtered.event
            post_action_story_seen = filtered.post_action_story_seen
            fresh_story_seen = filtered.fresh_story_seen
            if event is None:
                continue
            event = _screen_content_event_for_display(event, has_choice_req=True)
        if event_type in ("narration", "dialogue"):
            text = event.get("text", "").strip()
            if text:
                if text in narr_texts_cumulative:
                    _wait_trace(
                        args,
                        "skip_duplicate_drain_narration",
                        type=event_type,
                        chars=len(text),
                    )
                    continue
                if initial_grace <= 0 or text not in initial_screen_texts:
                    post_action_story_seen = True
                    fresh_story_seen = True
                    transition_since_story = False
                narr_texts_cumulative.add(text)
        if event_type == "screen_content" and event.get("texts"):

            filtered_texts = [
                text for text in event["texts"]
                if not _is_pending_input_prompt_echo(text, pending)
            ]
            event = dict(event, texts=filtered_texts)
            if not filtered_texts:
                continue
        # Skip the choice_request we're about to show as pending.
        if event_type in ("choice_request", "input_request"):
            event_id = event.get("id")
            if event_id:
                printed_ids.add(event_id)
            continue
        drain_display.append(event)

    if drain_display:
        text = format_events(
            drain_display,
            colour=colour,
            quiet=quiet,
            verbose=getattr(args, "verbose", False),
            show_stats=False,
        )
        if text.strip():
            print(text, flush=True)

    should_continue = (
        initial_grace > 0
        and (not fresh_story_seen or transition_since_story)
        and time.time() < story_wait_until
    )
    if should_continue:
        _wait_trace(
            args,
            "continue_after_final_drain",
            reason="awaiting_story",
            story_wait_remaining=round(
                max(0.0, story_wait_until - time.time()),
                3,
            ),
        )
    return _FinalDrainBeforePendingResult(
        post_action_story_seen,
        fresh_story_seen,
        transition_since_story,
        story_wait_until,
        should_continue,
    )


def _print_latest_screen_text_before_pending_phase(
    args: argparse.Namespace,
    pending: dict,
    sc_latest: Optional[dict],
    *,
    modal_active: bool,
    initial_grace: float,
    post_action_story_seen: bool,
    fresh_story_seen: bool,
    initial_screen_texts: set[str],
    narr_texts_cumulative: set[str],
    seen_sc_keys: set[tuple[str, ...]],
    colour: bool,
    quiet: bool,
) -> _LatestScreenTextBeforePendingResult:
    """Print fresh screen text immediately before the pending prompt.

    Intentionally impure: may print text and mutates narrative de-dup sets.
    """
    # Suppress the pending choice when a modal overlay or full-screen menu is
    # active (inventory, character sheet, etc.) because the underlying choices
    # are not actionable.
    if modal_active or not sc_latest or not sc_latest.get("texts"):
        return _LatestScreenTextBeforePendingResult(
            post_action_story_seen,
            fresh_story_seen,
        )

    pending_texts = []

    def pending_sc_dup(text: str) -> bool:
        if _is_pending_input_prompt_echo(text, pending):
            return True
        stripped = text.strip()
        if stripped in narr_texts_cumulative:
            return True
        if ": " in stripped:
            _, rest = stripped.split(": ", 1)
            if rest in narr_texts_cumulative:
                return True
        return False

    ordinary_texts, durable_rows = _cli_screen_story_parts(sc_latest)
    if durable_rows is not None:
        _wait_trace(args, "passive_snapshot_omitted_before_pending",
                    seq=sc_latest.get("_seq"),
                    snapshot_rows=len(sc_latest.get("overlay_texts") or []))
    for screen_text in ordinary_texts:
        stripped = str(screen_text).strip()
        if not stripped or pending_sc_dup(str(screen_text)):
            continue
        if initial_grace > 0 and stripped in initial_screen_texts:
            continue
        pending_texts.append(screen_text)

    if pending_texts:
        post_action_story_seen = True
        fresh_story_seen = True
        pending_sc_key = tuple(str(text).strip() for text in pending_texts)
        if pending_sc_key not in seen_sc_keys:
            seen_sc_keys.add(pending_sc_key)
            for text in pending_texts:
                stripped = str(text).strip()
                if stripped:
                    narr_texts_cumulative.add(stripped)
                    if ": " in stripped:
                        _, rest = stripped.split(": ", 1)
                        narr_texts_cumulative.add(rest)
            screen_text = format_events(
                [{
                    "type": "screen_content",
                    "texts": pending_texts,
                    "screens": sc_latest.get("screens", []),
                }],
                colour=colour,
                quiet=quiet,
                verbose=getattr(args, "verbose", False),
                show_stats=False,
            )
            if screen_text.strip():
                print(screen_text, flush=True)

    return _LatestScreenTextBeforePendingResult(
        post_action_story_seen,
        fresh_story_seen,
    )


def _print_pending_or_modal_prompt_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    pending: dict,
    sc_latest: Optional[dict],
    *,
    eid: Any,
    modal_active: bool,
    printed_ids: set[str],
    colour: bool,
    quiet: bool,
) -> None:
    """Print the final text-mode pending or modal prompt.

    Intentionally impure: may query live interactions while freshening the
    pending request, prints prompt text, and mutates `printed_ids`.
    """
    if modal_active:
        # Show the current screen buttons so the user knows what actions are
        # available while the underlying pending prompt is hidden.
        if sc_latest and (not eid or eid not in printed_ids):
            overlay_interactions = sc_latest.get("interactions")
            if overlay_interactions:
                interaction_text = format_interactions(
                    overlay_interactions,
                    colour=colour,
                    quiet=quiet,
                    verbose=False,
                )
                if interaction_text:
                    print()
                    print(interaction_text)
            else:
                overlay_buttons = sc_latest.get("buttons", [])
                overlay_screens = sc_latest.get("screens", [])
                if overlay_buttons:
                    button_text = format_screen_buttons(
                        overlay_buttons,
                        overlay_screens,
                        colour=colour,
                        quiet=quiet,
                        verbose=False,
                    )
                    if button_text:
                        print()
                        print(button_text)
            if eid:
                printed_ids.add(eid)
        return

    if eid and eid in printed_ids:
        return

    print()
    pending_events = _freshen_choice_request_events([pending], session)
    fresh_pending = pending_events[0] if pending_events else pending
    if fresh_pending != pending:
        print(
            format_events(
                [fresh_pending],
                colour=colour,
                quiet=quiet,
                verbose=getattr(args, "verbose", False),
                show_stats=False,
            )
        )
    else:
        print(
            format_pending_request(
                pending,
                colour=colour,
                quiet=quiet,
                show_stats=False,
            )
        )
    if eid:
        printed_ids.add(eid)


def _screen_status_for_interactions(interactions: Any) -> str:
    return "screen_interactions" if interactions else "screen_buttons"


def _rendered_pending_action(pending: Optional[dict]) -> Optional[dict]:
    """Return the agent-facing pending shape used by wait/state formatters."""
    if not pending:
        return None
    return build_wait_data([], pending).get("pending")


def _public_screen_buttons(buttons: Optional[list[dict]]) -> list[dict]:
    """Strip raw Ren'Py object references from JSON screen button payloads."""
    public = []
    for button in buttons or []:
        if not isinstance(button, dict):
            continue
        if item_is_default_focus_chrome(button):
            continue
        public.append({
            key: value for key, value in button.items()
            if not str(key).startswith("_")
        })
    return public


def _public_interactions(interactions: Optional[list[dict]]) -> list[dict]:
    """Strip private fields and passive focus-list chrome from interactions."""
    public = []
    for interaction in interactions or []:
        if not isinstance(interaction, dict):
            continue
        if item_is_default_focus_chrome(interaction):
            continue
        public.append(strip_internal_result_fields(interaction))
    return public


def _public_wait_event(event: dict) -> dict:
    """Return the stable public shape for a wait JSON event."""
    public = strip_internal_result_fields(event)
    public.pop("timestamp", None)
    if public.get("type") == "choice_request":
        public.pop("interactions", None)
    elif public.get("type") == "screen_content":
        if "buttons" in public:
            public["buttons"] = _public_screen_buttons(public.get("buttons"))
        if "interactions" in public:
            public["interactions"] = _public_interactions(
                public.get("interactions")
            )
    return public


def _public_wait_events(events: list[dict]) -> list[dict]:
    return [
        _public_wait_event(event)
        for event in events
        if isinstance(event, dict)
    ]


def _diag_events_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "diag", False))


def _emit_pending_or_modal_json_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    pending: dict,
    sc_latest: Optional[dict],
    *,
    modal_active: bool,
    current_events: list[dict],
) -> None:
    """Emit the final JSON pending or modal prompt.

    Intentionally impure: polls remaining bridge events, may freshen the
    pending request from live interactions, and emits JSON.
    """
    drained_events = session.poll(timeout=0)
    events: list[dict] = []
    seen_seq: set[Any] = set()
    for event in [*current_events, *drained_events]:
        seq = event.get("_seq") if isinstance(event, dict) else None
        if seq:
            if seq in seen_seq:
                continue
            seen_seq.add(seq)
        events.append(event)
    public_events = _public_wait_events(events)
    data = {
        "events": public_events,
        "event_count": len(public_events),
        "status": "waiting_for_input",
        "pending_action": _rendered_pending_action(pending),
    }
    if _diag_events_enabled(args):
        data["diag_events"] = events
        data["diag_event_count"] = len(events)
    if modal_active:
        interactions = (sc_latest or {}).get("interactions")
        public_interactions = _public_interactions(interactions)
        data.update({
            "status": _screen_status_for_interactions(public_interactions),
            "message": "Screen has clickable buttons.",
            "pending_action": None,
            "buttons": _public_screen_buttons((sc_latest or {}).get("buttons")),
            "screens": (sc_latest or {}).get("screens", []),
        })
        if interactions is not None:
            data["interactions"] = public_interactions
    else:
        pending_events = _freshen_choice_request_events([pending], session)
        if pending_events:
            data["pending_action"] = _rendered_pending_action(pending_events[0])
    _output(args, data=data)


class _SettleScreenForPendingResult(NamedTuple):
    eid: Any
    sc_latest: Optional[dict]
    modal_active: bool
    should_return: bool


def _settle_screen_for_pending_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    client_state: Optional[dict],
    pending: dict,
    *,
    initial_grace: float,
    post_action_story_seen: bool,
    skip_choice_signature: Optional[tuple[str, ...]],
    printed_something: bool,
    colour: bool,
    quiet: bool,
) -> _SettleScreenForPendingResult:
    """Settle live screen state before exposing a pending prompt.

    Intentionally impure: polls/settles screen state, saves session state, may
    print or emit JSON, and tells the caller whether to return.
    """
    # A pending choice can linger while a modal/screen transform is active.
    # Settle the live screen before deciding what is actually actionable for
    # both JSON and text callers.
    eid = pending.get("id")
    sc_latest = _get_latest_screen_buttons(session)
    if sc_latest:
        settle_timeout = max(1.2, min(initial_grace, 3.0))
        sc_latest = _get_settled_screen_buttons(
            session,
            sc_latest,
            timeout=settle_timeout,
            return_initial_when_stable=initial_grace <= 0,
        )
    modal_active = _pending_hidden_by_screen(session, sc_latest, pending)
    if (
        pending is not None
        and post_action_story_seen
        and _pending_matches_choice_signature(pending, skip_choice_signature)
        and sc_latest
        and _screen_event_has_actionable_prompt(sc_latest)
    ):
        _save_session(session, args, client_state)
        if args.json:
            interactions = sc_latest.get("interactions")
            public_interactions = _public_interactions(interactions)
            _output(
                args,
                data={
                    "status": _screen_status_for_interactions(public_interactions),
                    "message": "Screen has clickable buttons.",
                    "pending_action": None,
                    "buttons": _public_screen_buttons(sc_latest.get("buttons")),
                    "screens": sc_latest.get("screens", []),
                    "interactions": public_interactions,
                },
            )
        else:
            if printed_something:
                print()
            screen_text = ""
            interactions = sc_latest.get("interactions")
            if interactions:
                screen_text = format_interactions(
                    interactions,
                    colour=colour,
                    quiet=quiet,
                    verbose=getattr(args, "verbose", False),
                )
            else:
                screen_text = format_screen_buttons(
                    sc_latest.get("buttons", []),
                    sc_latest.get("screens", []),
                    colour=colour,
                    quiet=quiet,
                    verbose=getattr(args, "verbose", False),
                )
            if screen_text:
                print(screen_text, flush=True)
        return _SettleScreenForPendingResult(
            eid,
            sc_latest,
            modal_active,
            True,
        )

    return _SettleScreenForPendingResult(
        eid,
        sc_latest,
        modal_active,
        False,
    )


def _single_continue_defer_remaining(
    seen_at: Dict[Any, float],
    key: Any,
    now: float,
    deadline: float,
) -> Optional[float]:
    """Return remaining grace for a bare continue prompt, or None."""
    first_seen = seen_at.setdefault(key, now)
    elapsed = now - first_seen
    if (
        elapsed < _SINGLE_CONTINUE_PENDING_GRACE
        and deadline - now > _SINGLE_CONTINUE_DEADLINE_MARGIN
    ):
        return _SINGLE_CONTINUE_PENDING_GRACE - elapsed
    return None


def _defer_bare_continue_pending_phase(
    args: argparse.Namespace,
    pending: dict,
    *,
    fresh_story_seen: bool,
    deadline: float,
    single_continue_seen_at: Dict[str, float],
) -> bool:
    """Defer a bare continue pending prompt while late story may arrive.

    Intentionally impure: reads the clock, traces, sleeps, and tells the caller
    whether to continue polling.
    """
    if (
        not _is_single_continue_pending(pending)
        or fresh_story_seen
        or time.time() >= deadline
    ):
        return False

    sid = str(pending.get("id") or "")
    now_pending = time.time()
    remaining_grace = _single_continue_defer_remaining(
        single_continue_seen_at,
        sid,
        now_pending,
        deadline,
    )
    if remaining_grace is None:
        return False

    _wait_trace(
        args,
        "defer_bare_continue_pending",
        id=pending.get("id"),
        remaining=round(remaining_grace, 3),
    )
    time.sleep(min(0.2, max(0.0, deadline - now_pending)))
    return True


def _first_choice_request_event(events: list[dict]) -> Optional[dict]:
    return next(
        (event for event in events if event.get("type") == "choice_request"),
        None,
    )


class _DeferredChoiceStoryGraceResult(NamedTuple):
    pending_story_grace_started: bool
    story_wait_until: float
    should_defer: bool
    deferred_event: Optional[dict]
    deferred_id: Optional[str]
    story_wait_remaining: float


def _defer_choice_request_during_story_grace(
    display_events: list[dict],
    *,
    has_text_in_batch: bool,
    initial_grace: float,
    fresh_story_seen: bool,
    pending_story_grace_started: bool,
    story_wait_until: float,
    has_load_command: bool,
    now: float,
) -> _DeferredChoiceStoryGraceResult:
    """Return updated story-grace state and a deferred choice request, if any."""
    if (
        has_text_in_batch
        or initial_grace <= 0
        or fresh_story_seen
        or has_load_command
    ):
        return _DeferredChoiceStoryGraceResult(
            pending_story_grace_started,
            story_wait_until,
            False,
            None,
            None,
            0.0,
        )
    if not pending_story_grace_started:
        pending_story_grace_started = True
        story_wait_until = max(
            story_wait_until,
            now + min(initial_grace, 5.0),
        )
    if now >= story_wait_until:
        return _DeferredChoiceStoryGraceResult(
            pending_story_grace_started,
            story_wait_until,
            False,
            None,
            None,
            0.0,
        )
    deferred = _first_choice_request_event(display_events)
    deferred_id = deferred.get("id") if deferred else None
    return _DeferredChoiceStoryGraceResult(
        pending_story_grace_started,
        story_wait_until,
        True,
        deferred,
        deferred_id,
        max(0.0, story_wait_until - now),
    )


class _PreDrainPendingStoryGraceResult(NamedTuple):
    post_action_story_seen: bool
    fresh_story_seen: bool
    transition_since_story: bool
    story_wait_until: float
    should_continue: bool


def _pre_drain_pending_story_grace(
    session: BridgeClient,
    args: argparse.Namespace,
    *,
    initial_grace: float,
    story_wait_until: float,
    fresh_story_seen: bool,
    transition_since_story: bool,
    post_action_story_seen: bool,
    narr_texts_cumulative: set[str],
    initial_screen_texts: set[str],
) -> _PreDrainPendingStoryGraceResult:
    """Poll once for late story before exposing a pending choice."""
    if args.json or initial_grace <= 0 or time.time() >= story_wait_until:
        return _PreDrainPendingStoryGraceResult(
            post_action_story_seen,
            fresh_story_seen,
            transition_since_story,
            story_wait_until,
            False,
        )

    pre_drain = session.poll(timeout=0.5)
    if pre_drain:
        _wait_trace(
            args,
            "pre_drain",
            count=len(pre_drain),
            fresh_story=fresh_story_seen,
            transition=transition_since_story,
        )
        for event in pre_drain:
            event_type = event.get("type")
            if event_type in (
                "nvl_clear",
                "scene",
                "show",
                "hide",
                "stats_update",
            ):
                transition_since_story = True
                story_wait_until = max(
                    story_wait_until,
                    time.time() + min(initial_grace, 5.0),
                )
            elif event_type in ("dialogue", "narration"):
                text = str(event.get("text", "")).strip()
                if text and text in narr_texts_cumulative:
                    _wait_trace(
                        args,
                        "skip_duplicate_pre_drain_narration",
                        type=event_type,
                        chars=len(text),
                    )
                if (
                    text
                    and text not in narr_texts_cumulative
                    and text not in initial_screen_texts
                ):
                    post_action_story_seen = True
                    fresh_story_seen = True
                    transition_since_story = False
        preserve_prefetched_events(session, pre_drain)

    should_continue = (
        (not fresh_story_seen or transition_since_story)
        and time.time() < story_wait_until
    )
    if should_continue:
        _wait_trace(
            args,
            "continue_after_pre_drain",
            reason="awaiting_story",
            story_wait_remaining=round(
                max(0.0, story_wait_until - time.time()),
                3,
            ),
        )
    return _PreDrainPendingStoryGraceResult(
        post_action_story_seen,
        fresh_story_seen,
        transition_since_story,
        story_wait_until,
        should_continue,
    )


class _PendingStoryGraceResult(NamedTuple):
    pending_story_grace_started: bool
    story_wait_until: float
    deadline: float


def _start_pending_story_grace_for_live_pending(
    args: argparse.Namespace,
    pending: dict,
    *,
    initial_grace: float,
    fresh_story_seen: bool,
    pending_story_grace_started: bool,
    story_wait_until: float,
    deadline: float,
    now: float,
) -> _PendingStoryGraceResult:
    """Start text-mode story grace when a live pending prompt may be early."""
    if (
        args.json
        or initial_grace <= 0
        or fresh_story_seen
        or pending_story_grace_started
    ):
        return _PendingStoryGraceResult(
            pending_story_grace_started,
            story_wait_until,
            deadline,
        )

    pending_story_grace_started = True
    story_wait_until = max(
        story_wait_until,
        now + min(initial_grace, 5.0),
    )
    deadline = max(deadline, story_wait_until)
    _wait_trace(
        args,
        "pending_story_grace",
        id=pending.get("id"),
        story_wait_remaining=round(
            max(0.0, story_wait_until - now),
            3,
        ),
    )
    return _PendingStoryGraceResult(
        pending_story_grace_started,
        story_wait_until,
        deadline,
    )


class _ChoiceRequestCatchupResult(NamedTuple):
    display_events: list[dict]
    post_action_story_seen: bool
    fresh_story_seen: bool
    has_text_in_batch: bool


def _choice_request_catchup_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    display_events: list[dict],
    *,
    initial_grace: float,
    post_action_story_seen: bool,
    fresh_story_seen: bool,
    initial_screen_texts: set[str],
    narr_texts_cumulative: set[str],
    seen_sc_keys: set[tuple[str, ...]],
) -> _ChoiceRequestCatchupResult:
    """Ensure narrative text precedes a choice request when scraper lags.

    Mutates ``narr_texts_cumulative`` and ``seen_sc_keys`` when late text is
    injected, matching the inline phase's running de-dup cache behavior.
    """
    has_text_in_batch = any(
        e.get("type") in ("narration", "dialogue")
        or (e.get("type") == "screen_content" and e.get("texts"))
        for e in display_events
    )

    cr_idx = next(
        (
            i
            for i, e in enumerate(display_events)
            if e.get("type") == "choice_request"
        ),
        None,
    )
    if cr_idx is None:
        return _ChoiceRequestCatchupResult(
            display_events,
            post_action_story_seen,
            fresh_story_seen,
            has_text_in_batch,
        )

    # Pull text events that appear after the choice_request to before it.
    to_move = []
    remaining = []
    for i, e in enumerate(display_events):
        if i > cr_idx and (
            e.get("type") in ("narration", "dialogue")
            or (e.get("type") == "screen_content" and e.get("texts"))
        ):
            to_move.append(e)
        else:
            remaining.append(e)
    if to_move:
        new_cr_idx = next(
            (
                i
                for i, e in enumerate(remaining)
                if e.get("type") == "choice_request"
            ),
            0,
        )
        for j, e in enumerate(to_move):
            remaining.insert(new_cr_idx + j, e)
        display_events = remaining

    if has_text_in_batch:
        return _ChoiceRequestCatchupResult(
            display_events,
            post_action_story_seen,
            fresh_story_seen,
            has_text_in_batch,
        )

    # No text at all: poll increasingly broader sources for late scraper text.
    inject_texts = None
    inject_events = []
    catchup = session.poll(timeout=1.5)
    if catchup:
        for ce in catchup:
            if ce.get("type") in ("narration", "dialogue"):
                ctext = str(ce.get("text", "")).strip()
                if initial_grace > 0 and ctext in initial_screen_texts:
                    continue
                if ctext and ctext not in narr_texts_cumulative:
                    inject_events.append(ce)
                    narr_texts_cumulative.add(ctext)
                continue
            if ce.get("type") == "screen_content":
                filtered = _filter_screen_content_event(
                    args, ce, stale_screen_label=None, initial_grace=0,
                    post_action_story_seen=post_action_story_seen,
                    fresh_story_seen=fresh_story_seen, story_wait_until=0,
                    initial_screen_texts=initial_screen_texts,
                    narr_texts_cumulative=set(narr_texts_cumulative),
                    seen_sc_keys=seen_sc_keys,
                ).event
                if filtered and filtered.get("texts"):
                    inject_events.append(_screen_content_event_for_display(
                        filtered, has_choice_req=True))

    if inject_events:
        post_action_story_seen = True
        fresh_story_seen = True
        _wait_trace(args, "inject_catchup_events", count=len(inject_events))
        cr_idx2 = next(
            (
                i
                for i, e in enumerate(display_events)
                if e.get("type") == "choice_request"
            ),
            0,
        )
        for offset, inject_ev in enumerate(inject_events):
            display_events.insert(cr_idx2 + offset, inject_ev)
        has_text_in_batch = True

    # Historical screens are not evidence of this choice's missing narration.
    # Only snapshots published after this request may supplement its text.
    request_seq = next(
        (event.get("_seq") for event in display_events
         if event.get("type") == "choice_request"), None)

    def _belongs_to_request(snapshot: dict) -> bool:
        try:
            return int(request_seq) > 0 and int(snapshot.get("_seq", 0)) > int(request_seq)
        except (TypeError, ValueError):
            return False

    if not inject_events and not inject_texts:
        state = session.state()
        if state:
            for te in reversed(state.get("transcript", [])):
                if (te.get("type") == "screen_content" and te.get("texts")
                        and _belongs_to_request(te)):
                    inject_texts = [
                        t
                        for t in _cli_screen_story_parts(te)[0]
                        if t.strip() not in narr_texts_cumulative
                        and (
                            initial_grace <= 0
                            or t.strip() not in initial_screen_texts
                        )
                    ]
                    break

    if not inject_events and not inject_texts:
        settled_screen = _get_settled_screen_buttons(
            session,
            timeout=max(1.2, min(initial_grace, 5.0)),
            return_initial_when_stable=initial_grace <= 0,
        )
        if (settled_screen and settled_screen.get("texts")
                and _belongs_to_request(settled_screen)):
            inject_texts = [
                t
                for t in _cli_screen_story_parts(settled_screen)[0]
                if str(t).strip()
                and str(t).strip() not in narr_texts_cumulative
                and (
                    initial_grace <= 0
                    or str(t).strip() not in initial_screen_texts
                )
            ]

    if inject_texts:
        post_action_story_seen = True
        fresh_story_seen = True
        has_text_in_batch = True
        _wait_trace(args, "inject_catchup_texts", count=len(inject_texts))
        inject_key = tuple(str(t).strip() for t in inject_texts)
        seen_sc_keys.add(inject_key)
        for it in inject_texts:
            its = str(it).strip()
            if its:
                narr_texts_cumulative.add(its)
                if ": " in its:
                    _, itr = its.split(": ", 1)
                    narr_texts_cumulative.add(itr)
        inject_ev = {"type": "screen_content", "texts": inject_texts}
        cr_idx2 = next(
            (
                i
                for i, e in enumerate(display_events)
                if e.get("type") == "choice_request"
            ),
            0,
        )
        display_events.insert(cr_idx2, inject_ev)

    return _ChoiceRequestCatchupResult(
        display_events,
        post_action_story_seen,
        fresh_story_seen,
        has_text_in_batch,
    )


def _emit_deferred_screen_prompt_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    client_state: ClientState,
    deferred_screen_prompt: Optional[dict],
    *,
    post_action_story_seen: bool,
    has_screen_buttons: bool,
    has_choice_req_cumulative: bool,
    colour: bool,
    quiet: bool,
    verbose: bool,
    printed_something: bool,
) -> bool:
    """Emit a deferred screen prompt once late story has arrived.

    This phase is intentionally impure: it prints, saves session state, and
    returns True when _perform_wait should exit.
    """
    if not (
        deferred_screen_prompt
        and post_action_story_seen
        and not has_screen_buttons
        and not has_choice_req_cumulative
    ):
        return False

    deferred_interactions = deferred_screen_prompt.get("interactions")
    if deferred_interactions:
        deferred_text = format_interactions(
            deferred_interactions,
            colour=colour,
            quiet=quiet,
            verbose=verbose,
        )
    else:
        deferred_text = format_screen_buttons(
            deferred_screen_prompt.get("buttons", []),
            deferred_screen_prompt.get("screens", []),
            colour=colour,
            quiet=quiet,
            verbose=verbose,
        )
    if deferred_text:
        if printed_something:
            print()
        print(deferred_text, flush=True)
    _save_session(session, args, client_state)
    return True


class _ScreenPromptResult(NamedTuple):
    should_continue: bool
    should_return: bool


class _ScreenButtonsAfterEventsResult(NamedTuple):
    should_continue: bool
    should_return: bool


class _PromptAllowedGateResult(NamedTuple):
    should_continue: bool
    prompt_allowed: bool
    current_screen_names: set[str]


def _prompt_allowed_gate_phase(
    args: argparse.Namespace,
    *,
    current_screen: Optional[dict],
    allow_unchanged_screen_prompt: bool,
    initial_grace: float,
    allow_screen_prompts_at: float,
    pre_action_screen_sig: Any,
    stale_screen_label: Optional[str],
    stale_screen_suppress_until: float,
    fresh_story_seen: bool,
    story_wait_until: float,
    state: dict,
    lifecycle: dict,
) -> _PromptAllowedGateResult:
    """Classify whether the current screen prompt may be returned.

    This phase is intentionally impure: it traces and sleeps when story-grace
    or stale-menu suppression asks _perform_wait to continue polling.
    """
    can_show_screen_prompt = time.time() >= allow_screen_prompts_at
    if (
        not allow_unchanged_screen_prompt
        and
        can_show_screen_prompt
        and initial_grace > 0
        and _screen_signature(current_screen) == pre_action_screen_sig
    ):
        can_show_screen_prompt = False
    stale_clicked_screen = bool(
        stale_screen_label
        and current_screen
        and time.time() < stale_screen_suppress_until
        and _screen_event_contains_label(current_screen, stale_screen_label)
    )
    if can_show_screen_prompt and stale_clicked_screen:
        can_show_screen_prompt = False
    current_screen_actionable = _screen_event_has_actionable_prompt(
        current_screen or {}
    )
    prompt_allowed = (
        state.get("status") == "idle"
        or (
            lifecycle.get("effective_status") == "screen_actions"
            and current_screen_actionable
        )
        or (
            can_show_screen_prompt
            and current_screen_actionable
        )
    )
    if (
        initial_grace > 0
        and not fresh_story_seen
        and state.get("status") == "idle"
        and time.time() < story_wait_until
    ):
        _wait_trace(
            args,
            "suppress_idle_prompt_during_story_grace",
            story_wait_remaining=round(
                max(0.0, story_wait_until - time.time()),
                3,
            ),
        )
        time.sleep(0.2)
        return _PromptAllowedGateResult(True, prompt_allowed, set())
    current_screen_names = screen_names(current_screen)
    stale_in_game_menu = _screen_is_stale_in_game_menu(
        current_screen,
        lifecycle.get("context"),
    )
    stale_post_action_main_menu = bool(
        initial_grace > 0
        and time.time() < story_wait_until
        and (
            current_screen_names == {"menu"}
            or (
                not fresh_story_seen
                and (
                    (
                        lifecycle.get("suppress_raw_ended")
                        and lifecycle.get("context") == "main_menu"
                    )
                    or lifecycle.get("context") == "main_menu"
                    or "main_menu" in current_screen_names
                )
            )
        )
    )
    if stale_post_action_main_menu:
        _wait_trace(
            args,
            "suppress_stale_main_menu_prompt",
            story_wait_remaining=round(
                max(0.0, story_wait_until - time.time()),
                3,
            ),
        )
        time.sleep(0.2)
        return _PromptAllowedGateResult(True, prompt_allowed, current_screen_names)
    if prompt_allowed and stale_in_game_menu:
        _wait_trace(args, "suppress_in_game_menu_screen_prompt")
        prompt_allowed = False
    if prompt_allowed and stale_clicked_screen:
        prompt_allowed = False
    return _PromptAllowedGateResult(False, prompt_allowed, current_screen_names)


def _screen_prompt_ahead_of_story(session: BridgeClient, screen: Optional[dict]) -> bool:
    """A live screen must not overtake the ordinary lane's unread story.

    The state counter covers state-only screen events too, unlike the transcript
    cursor. Prefetch still needs draining even when that counter is current.
    """
    if getattr(session, "_prefetched_events", None):
        return True
    observed = getattr(session, "_last_event_counter", None)
    if observed is None:
        observed = getattr(session, "cursor", None)
    sequence = (screen or {}).get("_seq")
    if observed is None or sequence is None:
        return False
    try:
        return int(sequence) > int(observed)
    except (TypeError, ValueError):
        return False


def _return_screen_buttons_after_events_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    events: list[dict],
    *,
    has_screen_buttons: bool,
    deferred_choice_request_event: Optional[dict],
    transition_since_story: bool,
    initial_grace: float,
    fresh_story_seen: bool,
    story_wait_until: float,
    latest_sc_event: Optional[dict],
    colour: bool,
    quiet: bool,
    verbose: bool,
    printed_something: bool,
) -> _ScreenButtonsAfterEventsResult:
    """Return screen buttons found in the event batch once they settle.

    This phase is intentionally impure: it polls pending/screen state, may
    sleep via the settle helper, prints/output JSON, traces, and tells
    _perform_wait whether to continue or exit.
    """
    has_auto_skip = any(e.get("type") == "auto_skipped" for e in events)
    if not (
        has_screen_buttons
        and not has_auto_skip
        and deferred_choice_request_event is None
        and not transition_since_story
    ):
        return _ScreenButtonsAfterEventsResult(False, False)

    if (
        initial_grace > 0
        and (
            not fresh_story_seen
            or transition_since_story
        )
        and time.time() < story_wait_until
    ):
        return _ScreenButtonsAfterEventsResult(True, False)

    pending_check = session.pending()
    if pending_check is not None or latest_sc_event is None:
        return _ScreenButtonsAfterEventsResult(False, False)

    story_in_batch = any(
        e.get("type") in ("dialogue", "narration")
        or (e.get("type") == "screen_content" and e.get("texts"))
        for e in events
    )
    settle_timeout = max(
        1.2,
        min(
            3.0,
            initial_grace
            if initial_grace > 0
            else (3.0 if story_in_batch else 1.2),
        ),
    )
    settled_sc_event = _get_settled_screen_buttons(
        session,
        latest_sc_event,
        timeout=settle_timeout,
        return_initial_when_stable=initial_grace <= 0 and not story_in_batch,
    )
    if settled_sc_event:
        latest_sc_event = settled_sc_event

    if _screen_prompt_ahead_of_story(session, latest_sc_event):
        _wait_trace(args, "defer_screen_until_story_caught_up",
                    screen_seq=latest_sc_event.get("_seq"))
        return _ScreenButtonsAfterEventsResult(True, False)

    interactions = latest_sc_event.get("interactions")
    buttons = latest_sc_event.get("buttons", [])
    screens = latest_sc_event.get("screens", [])
    if args.json:
        public_interactions = _public_interactions(interactions)
        data = {
            "status": _screen_status_for_interactions(public_interactions),
            "message": "Screen has actionable interactions.",
            "buttons": _public_screen_buttons(buttons),
            "screens": screens,
        }
        if interactions is not None:
            data["interactions"] = public_interactions
        _output(
            args,
            data=data,
        )
    else:
        # Text-mode story output strips candidate screen actions before this
        # phase. Print the settled prompt exactly once so transient screen
        # refreshes do not leak stale buttons.
        if interactions:
            settled_text = format_interactions(
                interactions,
                colour=colour,
                quiet=quiet,
                verbose=verbose,
            )
        else:
            settled_text = format_screen_buttons(
                buttons,
                screens,
                colour=colour,
                quiet=quiet,
                verbose=verbose,
            )
        if settled_text:
            if printed_something:
                print()
            print(settled_text)
    _wait_trace(
        args,
        "return_screen_buttons",
        screens=screens,
        button_count=len(buttons),
    )
    return _ScreenButtonsAfterEventsResult(False, True)


def _return_current_screen_prompt_phase(
    session: BridgeClient,
    args: argparse.Namespace,
    client_state: ClientState,
    *,
    current_screen: Optional[dict],
    current_screen_names: set[str],
    prompt_allowed: bool,
    printed_something: bool,
    fresh_story_seen: bool,
    initial_grace: float,
    deadline: float,
    single_continue_screen_seen_at: Dict[Any, float],
    state: dict,
    lifecycle: dict,
) -> _ScreenPromptResult:
    """Return or defer an actionable screen prompt at the loop tail.

    This phase is intentionally impure: it traces, sleeps, prints/output JSON,
    saves session state, and tells _perform_wait whether to continue or exit.
    """
    if not prompt_allowed:
        return _ScreenPromptResult(False, False)

    if _screen_prompt_ahead_of_story(session, current_screen):
        return _ScreenPromptResult(True, False)

    if (
        _screen_is_nvl_continue_only(current_screen)
        and not printed_something
        and time.time() < deadline
    ):
        screen_sig = _screen_signature(current_screen)
        now_screen = time.time()
        remaining_grace = _single_continue_defer_remaining(
            single_continue_screen_seen_at,
            screen_sig,
            now_screen,
            deadline,
        )
        if remaining_grace is not None:
            _wait_trace(
                args,
                "defer_bare_continue_screen",
                remaining=round(remaining_grace, 3),
            )
            time.sleep(min(0.2, max(0.0, deadline - now_screen)))
            return _ScreenPromptResult(True, False)

    prompt_text = _format_current_screen_prompt(session, args, screen=current_screen)
    if not prompt_text.strip():
        return _ScreenPromptResult(False, False)

    _wait_trace(
        args,
        "return_screen_prompt",
        status=state.get("status"),
        context=(state.get("context") or {}).get("context"),
        lifecycle=lifecycle.get("effective_status"),
        screens=sorted(current_screen_names),
        fresh_story=fresh_story_seen,
        initial_grace=initial_grace,
    )
    _save_session(session, args, client_state)
    if args.json:
        interactions = (current_screen or {}).get("interactions")
        public_interactions = _public_interactions(interactions)
        buttons = (current_screen or {}).get("buttons", [])
        screens = (current_screen or {}).get("screens", [])
        data = {
            "status": _screen_status_for_interactions(public_interactions),
            "message": "Screen has actionable interactions.",
            "buttons": _public_screen_buttons(buttons),
            "screens": screens,
        }
        if interactions is not None:
            data["interactions"] = public_interactions
        _output(
            args,
            data=data,
        )
    else:
        if printed_something:
            print()
        print(prompt_text, flush=True)
    return _ScreenPromptResult(False, True)


@dataclass
class _WaitInit:
    """Loop-start state for _perform_wait.

    Many fields are mutable sets/dicts shared by reference into the loop body
    and the phase helpers — they accumulate state across iterations. Returned
    as a dataclass rather than a NamedTuple because the field count is high
    enough that positional unpacking would obscure intent.
    """
    deadline: float
    min_deadline: float
    last_event_time: float
    narr_texts_cumulative: Set[str]
    recent_screen_texts: Set[str]
    seen_sc_keys: Set[tuple]
    initial_screen_texts: Set[str]
    pre_action_screen_sig: Any
    allow_screen_prompts_at: float
    post_action_story_seen: bool
    fresh_story_seen: bool
    story_wait_until: float
    pending_story_grace_started: bool
    transition_since_story: bool
    stale_screen_suppress_until: float
    skip_choice_signature: tuple


def _init_wait_state(
    session: BridgeClient,
    *,
    timeout: Optional[float],
    initial_grace: float,
    idle_timeout: float,
    skip_request_choices: Optional[list[str]],
    stale_screen_label: Optional[str],
) -> _WaitInit:
    """Compute the loop-start state for _perform_wait.

    Pure setup work: deadline arithmetic, dedup-cache seeding from the
    transcript, story-grace defaults, skip-signature precomputation.
    Holding it together here keeps _perform_wait's body focused on the
    loop itself.
    """
    deadline = (
        time.time()
        + (timeout if timeout is not None else idle_timeout)
        + initial_grace
    )
    # Floor: the deadline should never drop below this (protects
    # initial_grace from being overwritten by the timeout=None reset).
    min_deadline = deadline

    narr_texts_cumulative: Set[str] = set()  # dedup screen_content vs narration
    recent_screen_texts: Set[str] = set()
    # Seed from recent dialogue/narration so screen_content resends of
    # just-seen text are filtered. Only dialogue/narration — seeding from
    # screen_content is too aggressive (filters legitimate re-appearances
    # in new scenes, e.g. game loops).
    for entry in session.transcript(last=20):
        if entry.get("type") in ("narration", "dialogue"):
            text = entry.get("text", "").strip()
            if text:
                narr_texts_cumulative.add(text)
        elif entry.get("type") == "screen_content":
            for raw in entry.get("texts") or []:
                text = str(raw).strip()
                if text:
                    recent_screen_texts.add(text)

    initial_screen = _get_latest_screen_buttons(session)
    pre_action_screen_sig = _screen_signature(initial_screen)
    initial_screen_texts: Set[str] = {
        str(t).strip()
        for t in (initial_screen or {}).get("texts") or []
        if str(t).strip()
    }
    initial_screen_texts.update(recent_screen_texts)
    if initial_grace > 0:
        try:
            initial_state = session.state()
        except Exception:
            initial_state = {}
        for src in (
            (initial_state or {}).get("screen") or {},
            (initial_state or {}).get("game_state") or {},
        ):
            for raw in src.get("texts") or []:
                text = str(raw).strip()
                if text:
                    initial_screen_texts.add(text)

    skip_choice_signature = tuple(
        str(label).strip()
        for label in (skip_request_choices or [])
        if str(label).strip()
    )

    now = time.time()
    return _WaitInit(
        deadline=deadline,
        min_deadline=min_deadline,
        last_event_time=now,
        narr_texts_cumulative=narr_texts_cumulative,
        recent_screen_texts=recent_screen_texts,
        seen_sc_keys=set(),
        initial_screen_texts=initial_screen_texts,
        pre_action_screen_sig=pre_action_screen_sig,
        allow_screen_prompts_at=now + initial_grace,
        post_action_story_seen=initial_grace <= 0,
        fresh_story_seen=initial_grace <= 0,
        story_wait_until=now + min(initial_grace, 5.0),
        pending_story_grace_started=False,
        transition_since_story=False,
        stale_screen_suppress_until=(
            now + max(initial_grace, 2.0)
            if stale_screen_label
            else 0.0
        ),
        skip_choice_signature=skip_choice_signature,
    )


def _perform_wait(
    session: BridgeClient,
    args: argparse.Namespace,
    client_state: ClientState,
    timeout: Optional[float],
    initial_grace: float = 0.0,
    skip_request_id: Optional[str] = None,
    skip_request_choices: Optional[list[str]] = None,
    drain_stale_map_pending: bool = False,
    stale_screen_label: Optional[str] = None,
    allow_unchanged_screen_prompt: bool = False,
) -> int:
    colour = not args.json
    quiet = getattr(args, "quiet", False)
    # Dynamic deadline that updates based on game activity.
    # Initial estimate for a single line of dialogue.
    line_est = 5.0
    # Idle timeout: how long to wait with no events before giving up.
    # Longer than line_est to survive gaps between choice resolution
    # and first dialogue (scene transitions, animations, etc.).
    _idle_timeout = 15.0
    # Pull the heavy setup out into a dataclass-returning helper. We rebind
    # locals 1:1 so the loop body below doesn't change at all — only the
    # initialization noise moves out.
    _init = _init_wait_state(
        session,
        timeout=timeout,
        initial_grace=initial_grace,
        idle_timeout=_idle_timeout,
        skip_request_choices=skip_request_choices,
        stale_screen_label=stale_screen_label,
    )
    deadline = _init.deadline
    min_deadline = _init.min_deadline
    last_event_time = _init.last_event_time
    _narr_texts_cumulative = _init.narr_texts_cumulative
    _recent_screen_texts = _init.recent_screen_texts
    _seen_sc_keys = _init.seen_sc_keys
    _initial_screen_texts = _init.initial_screen_texts
    _pre_action_screen_sig = _init.pre_action_screen_sig
    _allow_screen_prompts_at = _init.allow_screen_prompts_at
    _post_action_story_seen = _init.post_action_story_seen
    _fresh_story_seen = _init.fresh_story_seen
    _story_wait_until = _init.story_wait_until
    _pending_story_grace_started = _init.pending_story_grace_started
    _transition_since_story = _init.transition_since_story
    _stale_screen_suppress_until = _init.stale_screen_suppress_until
    _skip_choice_signature = _init.skip_choice_signature

    printed_something = False
    printed_ids: set[str] = set()
    json_wait_events: list[dict] = []

    # Track when we last did expensive checks (pending, screen buttons).
    _last_hard_check = 0.0
    _HARD_CHECK_INTERVAL = 0.5  # seconds between expensive HTTP calls
    # Cumulative: once a choice_request is seen anywhere, strip choice
    # interactions from subsequent screen_content events to avoid
    # showing the same choices twice (once as CHOICE REQUIRED, once as CHOICES).
    _has_choice_req_cumulative = False
    _deferred_screen_prompt: Optional[dict] = None

    def _should_skip_request_event(event: dict) -> bool:
        if event.get("type") not in ("choice_request", "input_request"):
            return False
        if (
            skip_request_id
            and event.get("id") == skip_request_id
            and not _post_action_story_seen
        ):
            return True
        if event.get("type") == "choice_request" and _skip_choice_signature:
            return _choice_request_label_signature(event) == _skip_choice_signature
        return False

    def _matches_skip_choice_signature(pending: Optional[dict]) -> bool:
        return _pending_matches_choice_signature(pending, _skip_choice_signature)

    def _is_fresh_gameplay_event(event: dict) -> bool:
        etype = event.get("type")
        if etype in (
            "dialogue",
            "narration",
            "choice_request",
            "input_request",
            "stats_update",
            "inventory_update",
            "game_started",
            "game_resumed",
        ):
            return True
        if etype == "context":
            return event.get("context") == "in_game"
        if etype == "screen_content":
            return bool(event.get("texts"))
        return False

    _return_to_menu_has_actionable_screen: Optional[bool] = None

    def _has_actionable_return_to_menu_screen() -> bool:
        nonlocal _return_to_menu_has_actionable_screen
        if _return_to_menu_has_actionable_screen is not None:
            return _return_to_menu_has_actionable_screen
        screen = _get_latest_screen_buttons(session)
        _return_to_menu_has_actionable_screen = bool(
            screen and _screen_event_has_actionable_prompt(screen)
        )
        return _return_to_menu_has_actionable_screen

    def _should_skip_stale_game_ended_event(events: list[dict], index: int) -> bool:
        event = events[index]
        if event.get("type") != "game_ended":
            return False
        # Ren'Py can briefly emit return_to_menu from the old menu context
        # while a fresh game is already streaming. Do not tell agents the game
        # ended if the same batch proves gameplay resumed.
        if event.get("reason") != "return_to_menu":
            return False
        if initial_grace > 0:
            return True
        if any(_is_fresh_gameplay_event(e) for e in events[index + 1:]):
            return True
        return _has_actionable_return_to_menu_screen()

    _skipped_choice_event_candidate: Optional[dict] = None
    _deferred_choice_request_event: Optional[dict] = None
    _single_continue_seen_at: Dict[str, float] = {}
    _single_continue_screen_seen_at: Dict[tuple, float] = {}
    _bridge_down_since: Optional[float] = None

    def _emit_bridge_unreachable(down_for: float) -> None:
        duration = (
            f" (connection failed for {int(down_for)}s)" if down_for >= 1 else ""
        )
        msg = (
            f"Bridge unreachable at {session.bridge_url}{duration}. "
            "The game or bridge process may have exited; "
            "check 'vnflight.py slots' or relaunch the game."
        )
        if args.json:
            _output(args, data={
                "status": "error",
                "error": "bridge_unreachable",
                "message": msg,
                "bridge": session.bridge_url,
            })
        else:
            print()
            print(_red(f"✗ {msg}"))
    _wait_trace(
        args,
        "start",
        timeout=timeout,
        initial_grace=initial_grace,
        skip_request_id=skip_request_id,
        skip_request_choices=skip_request_choices or [],
        drain_stale_map_pending=drain_stale_map_pending,
        stale_screen_label=stale_screen_label,
    )

    while time.time() < deadline:
        # Fix wait logic.
        poll_timeout = min(deadline - time.time(), max(5.0, line_est))
        # Wait loops forever until a choice or screen is required unless explicitly bounded.
        if timeout is None:
            poll_timeout = max(5.0, _idle_timeout)
            deadline = max(time.time() + poll_timeout, min_deadline)
        events = session.poll(timeout=poll_timeout)
        if args.json and events:
            json_wait_events.extend(events)

        # Access-denied detection: a 403 on a reserved slot is
        # deterministic for this session (poll() bails out early on it) —
        # waiting longer cannot succeed, so fail out immediately instead
        # of spinning until timeout with "(no events)".
        if not events and _slot_access_denied(session):
            _save_session(session, args, client_state)
            _wait_trace(args, "access_denied")
            return _access_denied_error(args, session)

        # Bridge-death detection: session.poll() returns [] both for
        # "no new events" and "bridge unreachable"; the client's
        # connectivity tracker distinguishes the two.  Give the bridge a
        # short grace to come back (transient hiccup, restart), then fail
        # out loudly instead of hanging forever — repeated connection
        # failures are not a quiet game.
        if not getattr(session, "_bridge_up", True):
            _now = time.time()
            if _bridge_down_since is None:
                _bridge_down_since = _now
                _wait_trace(args, "bridge_down")
            if _now - _bridge_down_since >= _WAIT_BRIDGE_DOWN_GRACE:
                _save_session(session, args, client_state)
                _wait_trace(
                    args,
                    "bridge_unreachable",
                    down_for=round(_now - _bridge_down_since, 3),
                )
                _emit_bridge_unreachable(_now - _bridge_down_since)
                return 1
            time.sleep(1.0)
            # Cheap probe; a successful exchange flips the client back
            # to "up" and the loop resumes normal polling.
            try:
                session.is_up()
            except Exception:
                pass
            if getattr(session, "_bridge_up", True):
                _bridge_down_since = None
                _wait_trace(args, "bridge_reconnected")
            continue
        else:
            _bridge_down_since = None

        # Classify batch: does it contain "heavy" events that need
        # expensive follow-up checks (pending request, screen buttons)?
        _need_hard_check = False

        if events:
            # Activity extends only an unbounded wait. An explicit timeout
            # must not renew whenever another line or pacing notice arrives.
            for e in events:
                etype = e.get("type")
                if (
                    initial_grace > 0
                    and etype in (
                        "nvl_clear",
                        "scene",
                        "show",
                        "hide",
                        "stats_update",
                    )
                ):
                    _transition_since_story = True
                    _story_wait_until = max(
                        _story_wait_until,
                        time.time() + min(initial_grace, 5.0),
                    )
                    _wait_trace(
                        args,
                        "transition_event",
                        type=etype,
                        story_wait_remaining=round(
                            max(0.0, _story_wait_until - time.time()),
                            3,
                        ),
                    )
                if etype in ("dialogue", "narration"):
                    text = e.get("text", "")
                    line_est = len(text) * 0.1 + 5.0
                    text_key = str(text).strip()
                    if (
                        text_key
                        and text_key not in _narr_texts_cumulative
                        and (
                            initial_grace <= 0
                            or text_key not in _initial_screen_texts
                        )
                    ):
                        _post_action_story_seen = True
                        _fresh_story_seen = True
                        _transition_since_story = False
                        _wait_trace(
                            args,
                            "fresh_story",
                            type=etype,
                            chars=len(text_key),
                        )

                    # Extend deadline: at least idle_timeout from now
                    # so we survive gaps between lines.
                    new_deadline = time.time() + max(line_est, _idle_timeout)
                    if timeout is None and new_deadline > deadline:
                        deadline = new_deadline

                elif etype == "auto_skipped":
                    # Auto-skip has a pacing delay before the game
                    # resolves the choice.  Use the delay from the shim
                    # (with a small buffer) so we don't time out.
                    skip_delay = e.get("delay", 5.0)
                    new_deadline = time.time() + skip_delay + 2.0
                    if timeout is None and new_deadline > deadline:
                        deadline = new_deadline

                elif etype == "observation_started":
                    # The shim is highlighting a choice/input before
                    # resolving it.  Extend the deadline by the reported
                    # observation delay so we don't time out mid-highlight.
                    obs_delay = e.get("delay", 3.0)
                    new_deadline = time.time() + obs_delay + 2.0
                    if timeout is None and new_deadline > deadline:
                        deadline = new_deadline

                elif etype in (
                    "choice_request",
                    "input_request",
                    "screen_content",
                    "command_result",
                ):
                    _need_hard_check = True

            # Check if any screen_content event has buttons — this is
            # a signal that the game is waiting for the player to act
            # something (e.g. main menu buttons, quote dismiss).
            has_screen_buttons = False
            latest_sc_event = None
            for e in events:
                if (
                    e.get("type") == "screen_content"
                    and _screen_event_has_actionable_prompt(e)
                ):
                    if _screen_event_contains_label(e, stale_screen_label):
                        continue
                    has_screen_buttons = True
                    latest_sc_event = e

            _verbose = getattr(args, "verbose", False)
            if not args.json:
                for e in events:
                    eid = e.get("id")
                    if eid and not _should_skip_request_event(e):
                        printed_ids.add(eid)

                # Determine has_choice_req: check the batch first (cheap),
                # only do an HTTP call if we have a reason to (hard event
                # in batch or enough time has passed).
                has_choice_req = _has_choice_req_cumulative or any(
                    e.get("type") == "choice_request"
                    and not _should_skip_request_event(e)
                    for e in events
                )
                if not has_choice_req and _need_hard_check:
                    pending_peek = session.pending()
                    if (
                        pending_peek is not None
                        and pending_peek.get("type") == "choice_request"
                        and not (
                            _post_action_story_seen
                            and _matches_skip_choice_signature(pending_peek)
                        )
                    ):
                        has_choice_req = True
                # A skipped request is still pending during observation —
                # treat it as a choice_req so screen_content buttons are
                # stripped (they'd just duplicate the old choice list).
                if (
                    skip_request_id
                    and not has_choice_req
                    and not _post_action_story_seen
                ):
                    has_choice_req = True
                if has_choice_req:
                    _has_choice_req_cumulative = True

                display_events = []

                # Collect narration/dialogue texts so we can strip
                # duplicates from screen_content events (NVL watcher
                # and scraper both capture the same text).
                for e in events:
                    if e.get("type") in ("narration", "dialogue"):
                        _nt = e.get("text", "").strip()
                        if _nt:
                            _narr_texts_cumulative.add(_nt)

                for idx, e in enumerate(events):
                    if _should_skip_stale_game_ended_event(events, idx):
                        _wait_trace(args, "skip_stale_game_ended_event")
                        continue
                    # Hide the choice/input request we already responded to.
                    if _should_skip_request_event(e):
                        if (
                            e.get("type") == "choice_request"
                            and _post_action_story_seen
                        ):
                            _skipped_choice_event_candidate = e
                        continue
                    if e.get("type") == "screen_content":
                        _screen_filter_result = _filter_screen_content_event(
                            args,
                            e,
                            stale_screen_label=stale_screen_label,
                            initial_grace=initial_grace,
                            post_action_story_seen=_post_action_story_seen,
                            fresh_story_seen=_fresh_story_seen,
                            story_wait_until=_story_wait_until,
                            initial_screen_texts=_initial_screen_texts,
                            narr_texts_cumulative=_narr_texts_cumulative,
                            seen_sc_keys=_seen_sc_keys,
                        )
                        (
                            e,
                            _new_deferred_screen_prompt,
                            _post_action_story_seen,
                            _fresh_story_seen,
                        ) = _screen_filter_result
                        if _new_deferred_screen_prompt is not None:
                            _deferred_screen_prompt = _new_deferred_screen_prompt
                        if e is None:
                            continue
                        display_events.append(
                            _screen_content_event_for_display(
                                e,
                                has_choice_req=has_choice_req,
                                defer_actionable_prompt=(
                                    has_screen_buttons
                                    and not _screen_event_contains_label(
                                        e,
                                        stale_screen_label,
                                    )
                                ),
                            )
                        )
                    else:
                        display_events.append(e)

                _batch_has_cr = any(
                    e.get("type") == "choice_request" for e in display_events
                )
                if _batch_has_cr:
                    (
                        display_events,
                        _post_action_story_seen,
                        _fresh_story_seen,
                        _has_text_in_batch,
                    ) = _choice_request_catchup_phase(
                        session,
                        args,
                        display_events,
                        initial_grace=initial_grace,
                        post_action_story_seen=_post_action_story_seen,
                        fresh_story_seen=_fresh_story_seen,
                        initial_screen_texts=_initial_screen_texts,
                        narr_texts_cumulative=_narr_texts_cumulative,
                        seen_sc_keys=_seen_sc_keys,
                    )

                    _has_load_command = any(
                        e.get("type") == "command_result"
                        and e.get("command") == "load"
                        for e in display_events
                    )
                    (
                        _pending_story_grace_started,
                        _story_wait_until,
                        _should_defer_choice_request,
                        _deferred_choice_request_event,
                        _deferred_id,
                        _story_wait_remaining,
                    ) = _defer_choice_request_during_story_grace(
                        display_events,
                        has_text_in_batch=_has_text_in_batch,
                        initial_grace=initial_grace,
                        fresh_story_seen=_fresh_story_seen,
                        pending_story_grace_started=(
                            _pending_story_grace_started
                        ),
                        story_wait_until=_story_wait_until,
                        has_load_command=_has_load_command,
                        now=time.time(),
                    )
                    if _should_defer_choice_request:
                        if _deferred_id:
                            printed_ids.discard(_deferred_id)
                        _wait_trace(
                            args,
                            "defer_choice_request",
                            id=_deferred_id,
                            story_wait_remaining=round(_story_wait_remaining, 3),
                        )
                        continue
                    if not _has_load_command:
                        display_events = _freshen_choice_request_events(
                            display_events,
                            session,
                        )

                _display_has_text = any(
                    e.get("type") in ("narration", "dialogue")
                    or (e.get("type") == "screen_content" and e.get("texts"))
                    for e in display_events
                )
                _display_has_request = any(
                    e.get("type") in ("choice_request", "input_request")
                    for e in display_events
                )
                if (
                    _deferred_choice_request_event is not None
                    and _display_has_text
                    and not _display_has_request
                ):
                    display_events.extend(
                        _freshen_choice_request_events(
                            [_deferred_choice_request_event],
                            session,
                        )
                    )
                    _wait_trace(
                        args,
                        "append_deferred_choice",
                        id=_deferred_choice_request_event.get("id"),
                    )
                    _transition_since_story = False
                    _deferred_choice_request_event = None

                text = format_events(
                    display_events,
                    colour=colour,
                    quiet=quiet,
                    verbose=_verbose,
                    show_stats=False,
                )
                if text.strip():
                    print(text, flush=True)
                    printed_something = True
                    for _printed_event in display_events:
                        if _printed_event.get("type") in (
                            "choice_request",
                            "input_request",
                        ):
                            _printed_id = _printed_event.get("id")
                            if _printed_id:
                                printed_ids.add(_printed_id)

                if _emit_deferred_screen_prompt_phase(
                    session,
                    args,
                    client_state,
                    _deferred_screen_prompt,
                    post_action_story_seen=_post_action_story_seen,
                    has_screen_buttons=has_screen_buttons,
                    has_choice_req_cumulative=_has_choice_req_cumulative,
                    colour=colour,
                    quiet=quiet,
                    verbose=_verbose,
                    printed_something=printed_something,
                ):
                    return 0
            last_event_time = time.time()
            _save_session(session, args, client_state)

            # If we saw screen buttons and there's no pending choice/input,
            # stop and show the buttons as an actionable prompt.
            # But don't exit if the batch contains an auto_skipped event —
            # the game is still resolving and will continue to the next scene.
            screen_buttons_result = _return_screen_buttons_after_events_phase(
                session,
                args,
                events,
                has_screen_buttons=has_screen_buttons,
                deferred_choice_request_event=_deferred_choice_request_event,
                transition_since_story=_transition_since_story,
                initial_grace=initial_grace,
                fresh_story_seen=_fresh_story_seen,
                story_wait_until=_story_wait_until,
                latest_sc_event=latest_sc_event,
                colour=colour,
                quiet=quiet,
                verbose=_verbose,
                printed_something=printed_something,
            )
            if screen_buttons_result.should_continue:
                continue
            if screen_buttons_result.should_return:
                return 0

        # Hard checks: pending interaction, etc.  Only run when triggered
        # by a heavy event in the batch or periodically (every 0.5s).
        _now = time.time()
        _do_hard = (
            _need_hard_check
            or _now - _last_hard_check >= _HARD_CHECK_INTERVAL
            or not events
        )
        if not _do_hard:
            continue
        _last_hard_check = _now

        # Check for pending interaction.
        pending = session.pending()
        (
            pending,
            _should_continue_after_stale_pending,
        ) = _stale_pending_hard_check_phase(
            session,
            args,
            pending,
            initial_grace=initial_grace,
            skip_request_id=skip_request_id,
            skip_choice_signature=_skip_choice_signature,
            post_action_story_seen=_post_action_story_seen,
            fresh_story_seen=_fresh_story_seen,
            transition_since_story=_transition_since_story,
            story_wait_until=_story_wait_until,
            deadline=deadline,
        )
        if _should_continue_after_stale_pending:
            continue
        if pending is not None:
            if _defer_bare_continue_pending_phase(
                args,
                pending,
                fresh_story_seen=_fresh_story_seen,
                deadline=deadline,
                single_continue_seen_at=_single_continue_seen_at,
            ):
                continue
            _pending_grace_result = _start_pending_story_grace_for_live_pending(
                args,
                pending,
                initial_grace=initial_grace,
                fresh_story_seen=_fresh_story_seen,
                pending_story_grace_started=_pending_story_grace_started,
                story_wait_until=_story_wait_until,
                deadline=deadline,
                now=time.time(),
            )
            (
                _pending_story_grace_started,
                _story_wait_until,
                deadline,
            ) = _pending_grace_result
            if drain_stale_map_pending:
                pending = _drain_stale_pending_with_trace(
                    session,
                    args,
                    pending,
                    deadline=deadline,
                    by="map_pending",
                )
            _save_session(session, args, client_state)
            (
                eid,
                _sc_latest,
                _modal_active,
                _should_return_after_settle,
            ) = _settle_screen_for_pending_phase(
                session,
                args,
                client_state,
                pending,
                initial_grace=initial_grace,
                post_action_story_seen=_post_action_story_seen,
                skip_choice_signature=_skip_choice_signature,
                printed_something=printed_something,
                colour=colour,
                quiet=quiet,
            )
            if _should_return_after_settle:
                return 0
            if (
                not args.json
                and not _modal_active
                and (
                    not _fresh_story_seen
                    or _transition_since_story
                )
                and time.time() < _story_wait_until
            ):
                continue
            if (
                not args.json
                and not _modal_active
                and initial_grace > 0
                and time.time() < _story_wait_until
            ):
                (
                    _post_action_story_seen,
                    _fresh_story_seen,
                    _transition_since_story,
                    _story_wait_until,
                    _should_continue_after_pre_drain,
                ) = _pre_drain_pending_story_grace(
                    session,
                    args,
                    initial_grace=initial_grace,
                    story_wait_until=_story_wait_until,
                    fresh_story_seen=_fresh_story_seen,
                    transition_since_story=_transition_since_story,
                    post_action_story_seen=_post_action_story_seen,
                    narr_texts_cumulative=_narr_texts_cumulative,
                    initial_screen_texts=_initial_screen_texts,
                )
                if _should_continue_after_pre_drain:
                    continue
            if args.json:
                _emit_pending_or_modal_json_phase(
                    session,
                    args,
                    pending,
                    _sc_latest,
                    modal_active=_modal_active,
                    current_events=json_wait_events,
                )
            else:
                (
                    _post_action_story_seen,
                    _fresh_story_seen,
                    _transition_since_story,
                    _story_wait_until,
                    _should_continue_after_final_drain,
                ) = _final_drain_before_pending_phase(
                    session,
                    args,
                    pending,
                    initial_grace=initial_grace,
                    post_action_story_seen=_post_action_story_seen,
                    fresh_story_seen=_fresh_story_seen,
                    transition_since_story=_transition_since_story,
                    story_wait_until=_story_wait_until,
                    initial_screen_texts=_initial_screen_texts,
                    narr_texts_cumulative=_narr_texts_cumulative,
                    seen_sc_keys=_seen_sc_keys,
                    printed_ids=printed_ids,
                    colour=colour,
                    quiet=quiet,
                    skip_stale_game_ended_event=(
                        _should_skip_stale_game_ended_event
                    ),
                )
                if _should_continue_after_final_drain:
                    continue

                # The choice/input may have already been printed as an event
                # in this iteration's event list or a previous one.
                (
                    _post_action_story_seen,
                    _fresh_story_seen,
                ) = _print_latest_screen_text_before_pending_phase(
                    args,
                    pending,
                    _sc_latest,
                    modal_active=_modal_active,
                    initial_grace=initial_grace,
                    post_action_story_seen=_post_action_story_seen,
                    fresh_story_seen=_fresh_story_seen,
                    initial_screen_texts=_initial_screen_texts,
                    narr_texts_cumulative=_narr_texts_cumulative,
                    seen_sc_keys=_seen_sc_keys,
                    colour=colour,
                    quiet=quiet,
                )

                _print_pending_or_modal_prompt_phase(
                    session,
                    args,
                    pending,
                    _sc_latest,
                    eid=eid,
                    modal_active=_modal_active,
                    printed_ids=printed_ids,
                    colour=colour,
                    quiet=quiet,
                )
            _wait_trace(
                args,
                "return_pending",
                id=eid,
                modal_active=_modal_active,
            )
            return 0

        # Check if we're back at the main menu (treat as session end)
        # We only exit if it's been at least 5 seconds since the last event,
        # to avoid exiting immediately if we're starting a new game.
        state = session.state()
        ctx = (state.get("context") if state else None) or {}
        current_screen = _get_latest_screen_buttons(session)
        state_for_lifecycle = dict(state or {})
        if current_screen:
            state_for_lifecycle["screen"] = current_screen
        lifecycle = classify_lifecycle(state_for_lifecycle)
        if (
            ctx.get("context") == "main_menu"
            and not lifecycle.get("has_pending")
            and not lifecycle.get("has_screen_actions")
        ):
            if time.time() - last_event_time < 5.0:
                # Still in the "quiet period" after last event, keep waiting.
                time.sleep(0.5)
                continue

            _save_session(session, args, client_state)
            # Show the main menu options before exiting.
            # Also check for screen buttons (e.g. quote frame, custom menus).
            sc_ev = current_screen
            if args.json:
                data: dict = {
                    "status": "main_menu",
                    "message": "Returned to main menu.",
                    "context": ctx,
                }
                if sc_ev:
                    data["buttons"] = _public_screen_buttons(sc_ev.get("buttons"))
                    data["screens"] = sc_ev.get("screens", [])
                _output(args, data=data)
            else:
                print()
                print(_dim("■ Returned to main menu."))
                menu_text = format_main_menu(ctx, colour=colour, quiet=quiet)
                if menu_text:
                    print(menu_text)
                if sc_ev:
                    _mm_interactions = sc_ev.get("interactions")
                    if _mm_interactions:
                        btn_text = format_interactions(
                            _mm_interactions,
                            colour=colour,
                            quiet=quiet,
                        )
                    else:
                        btn_text = format_screen_buttons(
                            sc_ev.get("buttons", []),
                            sc_ev.get("screens"),
                            colour=colour,
                            quiet=quiet,
                        )
                    if btn_text:
                        print()
                        print(btn_text)
            return 0

        prompt_gate_result = _prompt_allowed_gate_phase(
            args,
            current_screen=current_screen,
            allow_unchanged_screen_prompt=allow_unchanged_screen_prompt,
            initial_grace=initial_grace,
            allow_screen_prompts_at=_allow_screen_prompts_at,
            pre_action_screen_sig=_pre_action_screen_sig,
            stale_screen_label=stale_screen_label,
            stale_screen_suppress_until=_stale_screen_suppress_until,
            fresh_story_seen=_fresh_story_seen,
            story_wait_until=_story_wait_until,
            state=state,
            lifecycle=lifecycle,
        )
        if prompt_gate_result.should_continue:
            continue
        prompt_allowed = prompt_gate_result.prompt_allowed
        current_screen_names = prompt_gate_result.current_screen_names
        screen_prompt_result = _return_current_screen_prompt_phase(
            session,
            args,
            client_state,
            current_screen=current_screen,
            current_screen_names=current_screen_names,
            prompt_allowed=prompt_allowed,
            printed_something=printed_something,
            fresh_story_seen=_fresh_story_seen,
            initial_grace=initial_grace,
            deadline=deadline,
            single_continue_screen_seen_at=_single_continue_screen_seen_at,
            state=state,
            lifecycle=lifecycle,
        )
        if screen_prompt_result.should_continue:
            continue
        if screen_prompt_result.should_return:
            return 0

        # Check if game ended.
        status = session.status()
        if (
            status
            and status.get("status") == "ended"
            and lifecycle.get("terminal")
        ):
            _wait_trace(
                args,
                "return_ended",
                reason=status.get("end_reason", "unknown"),
            )
            _save_session(session, args, client_state)
            reason = status.get("end_reason", "unknown")
            if args.json:
                _output(args, data={"status": "ended", "end_reason": reason})
            else:
                print()
                print(_red(f"■ Game ended ({reason})"))
            return 0

    _save_session(session, args, client_state)
    _wait_trace(args, "timeout", printed=printed_something)
    if not getattr(session, "_bridge_up", True) and not _bridge_is_up(session):
        # The bounded wait expired while the bridge was unreachable —
        # report the dead bridge, not a quiet timeout.  (On a dead bridge
        # even the wait-loop setup can consume a short timeout, so this
        # cannot rely on the loop having observed the outage.)
        _emit_bridge_unreachable(
            time.time() - _bridge_down_since if _bridge_down_since else 0.0
        )
        return 1
    if (
        _skipped_choice_event_candidate
        and not args.json
    ):
        print()
        print(
            format_events(
                [_skipped_choice_event_candidate],
                colour=colour,
                quiet=quiet,
                verbose=getattr(args, "verbose", False),
                show_stats=False,
            )
        )
    if not printed_something and not args.json and timeout is not None:
        print(_dim("(timeout — no events or choices within the wait period)"))
    if args.json:
        _output(args, data={"status": "timeout", "message": "Wait timed out."})
    return 0


def cmd_wait(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    _mark_current_events_seen_for_explicit_wait(session, args)
    return _perform_wait(session, args, client_state, getattr(args, "timeout", None))


def cmd_autoplay(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    success, msg = session._send_command("auto_advance_on")
    _save_session(session, args, client_state)
    if not success:
        _output(args, _red(f"✗ {msg}"), {"success": False, "error": msg})
        return 1
    _output(args, _green("✓ Auto-advance enabled. Playing..."))
    return _perform_wait(session, args, client_state, getattr(args, "timeout", None))


def _act_result_failed(result: dict) -> bool:
    """True when an act result reports a refusal, rejection or failure."""
    if result.get("error"):
        return True
    if result.get("transaction_state") in {"failed", "rejected"}:
        return True
    if "success" in result or "ok" in result:
        return not bool(result.get("success", result.get("ok")))
    return False


def _cli_result_guidance(result: dict) -> dict:
    """Adapt tool-generated instructions without rewriting story text."""
    result = dict(result)
    warning = result.get("warning")
    if isinstance(warning, str):
        result["warning"] = warning.replace("wait()", "the CLI `wait` command")
    # The pending hint ('Use act <N> or act "<label>" to respond.') is
    # already in CLI form; nothing to rewrite there.
    return result


def cmd_act(args: argparse.Namespace, client_state: ClientState) -> int:
    """Submit an action through the same resolver used by MCP act()."""
    session = _make_session(args, client_state)
    fmt = "json" if getattr(args, "json", False) else (
        "quiet" if getattr(args, "quiet", False) else "text"
    )
    params = {
        "target": args.target,
        "wait": getattr(args, "wait", True),
        "timeout": getattr(args, "timeout", None),
        "format": fmt,
    }
    # CLI commands are separate processes, so provisional latest-screen
    # ownership cannot survive into the next command. Use the public
    # invocation scope but rely only on durable overlay events.
    ctx = HandlerContext(session, allow_live_overlay_lookahead=False)
    result = handle_tool(ctx, "act", params)
    if getattr(args, "wait", True) and isinstance(result, dict) and not _act_result_failed(result):
        result = _cli_withhold_queued_story_decision(session, result)
    _save_session(session, args, client_state)

    if not isinstance(result, dict):
        result = {"error": str(result)}
    result = _cli_result_guidance(result)
    # A rejected act does not always carry an `error` (e.g. transaction_state
    # "rejected" with reason "state_unavailable" when no game is connected);
    # judging by `error` alone rendered it as "✓ Acted" with exit 0.
    if _act_result_failed(result):
        public_result = strip_internal_result_fields(result)
        error = (
            result.get("error")
            or result.get("reason")
            or "act was not applied"
        )
        prefix = "" if result.get("error") else "Did not act: "
        _output(
            args,
            _red(f"✗ {prefix}{error}"),
            {"success": False, **public_result},
        )
        return 1

    if getattr(args, "json", False):
        _output(args, data=strip_internal_result_fields(result))
        return 0

    if getattr(args, "wait", True) and (
        "wait" in result
        or any(result.get(key) for key in (
            "text", "screen_text", "status", "pending", "buttons", "brief", "warning"
        ))
    ):
        _output(args, render_tool_result_text(result))
        return 0

    label = result.get("label") or result.get("matched") or args.target
    resolved_as = result.get("resolved_as") or "action"
    _output(
        args,
        _green(f"✓ Acted ({resolved_as}): {label}"),
        {"success": True, **result},
    )
    return 0


def _cli_withhold_queued_story_decision(session: BridgeClient, result: dict) -> dict:
    """Do not advertise a successor before the saved ordinary lane is read.

    Act's state/admission probes can park events after its scoped wait. A
    long-lived handler may recover them on the next call; this process must
    persist them and explicitly hand back to wait instead of offering a menu.
    No events are claimed here and no new I/O budget is opened.
    """
    queued = getattr(session, "_prefetched_events", None) or []
    if not any(
        not isinstance(event, dict)
        or event.get("type") not in _PREFETCH_BOOKKEEPING_EVENT_TYPES
        for event in queued
    ):
        return result

    def without_decision(value):
        value = dict(value)
        for key in ("pending", "buttons", "interactions", "choices",
                    "_pending_raw", "_actionable_snapshot", "_footer"):
            if key == "pending" and isinstance(value.get(key), bool):
                continue  # Transaction acceptance, not a rendered menu.
            value.pop(key, None)
        for key in ("wait", "_data"):
            if isinstance(value.get(key), dict):
                value[key] = without_decision(value[key])
        return value

    result = without_decision(result)
    result["story_continues"] = True
    hint = "Earlier story is queued; call the CLI `wait` command before choosing again."
    result["warning"] = " ".join(filter(None, (result.get("warning"), hint)))
    return result


def cmd_choices(args: argparse.Namespace, client_state: ClientState) -> int:
    """Show the current choices or overlay items."""
    session = _make_session(args, client_state)
    pending = session.pending()

    # When an overlay covers the choices, show the overlay's items.
    sc_ev = _get_latest_screen_buttons(session)
    ignore_screen = _screen_is_stale_menu_overlay(session, sc_ev, pending)
    if ignore_screen:
        sc_ev = None
    _overlay = _has_choice_overlay(sc_ev, pending)
    interactions = _get_latest_interactions(
        session,
        pending,
        overlay_active=_overlay,
        ignore_screen=ignore_screen,
    )
    live_interactions_present = bool(
        sc_ev is not None
        and not ignore_screen
        and isinstance(sc_ev, dict)
        and "interactions" in sc_ev
    )
    pending_interactions = pending.get("interactions") if pending else None
    normalized_pending_interactions = (
        _normalize_interaction_disabled(pending_interactions)
        if pending_interactions
        else pending_interactions
    )
    live_choice_view = bool(
        interactions
        and normalized_pending_interactions
        and interactions != normalized_pending_interactions
        and any(i.get("type") == "choice" for i in interactions)
    )
    live_empty_choice_view = bool(
        live_interactions_present
        and interactions == []
        and normalized_pending_interactions
    )
    # Called screens can offer decisions without a standard menu request.
    screen_only_view = bool(not pending and live_interactions_present and interactions)
    if _overlay or live_choice_view or live_empty_choice_view or screen_only_view:
        _save_session(session, args, client_state)
        if interactions:
            quiet = getattr(args, "quiet", False)
            msg = format_interactions(interactions, colour=not args.json, quiet=quiet)
            _output(args, msg, {"interactions": interactions})
        else:
            _output(
                args, _dim("No choices or items available."), {"pending_action": None}
            )
        return 0

    _save_session(session, args, client_state)
    if pending:
        quiet = getattr(args, "quiet", False)
        msg = format_pending_request(
            pending, colour=not args.json, quiet=quiet, show_stats=False
        )
        _output(args, msg, {"pending_action": pending})
    else:
        if _slot_access_denied(session):
            return _access_denied_error(args, session)
        if not _bridge_is_up(session):
            return _bridge_unreachable_error(args, session)
        msg = _dim("No choices are currently pending.")
        _output(args, msg, {"pending_action": None})
    return 0


def _screen_is_stale_menu_overlay(
    session: BridgeClient,
    sc_ev: Optional[dict],
    pending: Optional[dict],
) -> bool:
    try:
        state = session.state()
    except Exception:
        return False
    raw = dict(state or {})
    raw["pending_request"] = pending
    raw["screen"] = sc_ev or {}
    return bool(classify_lifecycle(raw).get("stale_menu_overlay"))


def _pending_hidden_by_screen(
    session: BridgeClient,
    sc_ev: Optional[dict],
    pending: Optional[dict],
) -> bool:
    """Return True when an overlay/menu replaces an underlying pending choice."""
    if not sc_ev or not pending or pending.get("type") != "choice_request":
        return False
    if sc_ev.get("overlay_active") or sc_ev.get("modal_screens"):
        return True
    try:
        state = session.state()
    except Exception:
        state = {}
    raw = dict(state or {})
    raw["pending_request"] = pending
    raw["screen"] = sc_ev
    lifecycle = classify_lifecycle(raw)
    screens = set(sc_ev.get("screens") or [])
    screens.difference_update(
        {
            "vnf_command_poller",
            "vnf_player_debug",
            "llm_command_poller",
            "llm_player_debug",
        }
    )
    return bool(screens and screens <= {"menu"} and not lifecycle["stale_menu_overlay"])


def _screen_signature(sc_ev: Optional[dict]) -> tuple:
    return screen_signature(sc_ev)


def _get_settled_screen_buttons(
    session: BridgeClient,
    initial: Optional[dict] = None,
    *,
    timeout: float = 1.2,
    settle_delay: float = 0.4,
    return_initial_when_stable: bool = True,
) -> Optional[dict]:
    """Return the latest screen after transient screen transforms settle."""
    current = initial or _get_latest_screen_buttons(session)
    if not current:
        return current
    return wait_for_stable_change(
        fetch=lambda: _get_latest_screen_buttons(session),
        signature=_screen_signature,
        initial=current,
        timeout=timeout,
        settle_delay=settle_delay,
        poll_interval=0.2,
        return_initial_when_stable=return_initial_when_stable,
    ) or current


def cmd_input(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)

    if not session.last_request_id:
        session.pending()

    # Capture current pending prompt for stale-retry comparison.
    old_pending = session.pending()
    old_prompt = (old_pending or {}).get("prompt")

    text = " ".join(args.text) if isinstance(args.text, list) else args.text
    _result = session.input_text(text)
    success = _result.get("ok", False)
    msg = _result.get("message", _result.get("error", "Unknown error"))

    # Auto-retry on stale request ID if the prompt is the same.
    if not success and ("Stale request" in msg or "No pending request" in msg):
        pending = session.pending()
        if pending and pending.get("type") == "input_request":
            if pending.get("prompt") == old_prompt:
                _result = session.input_text(text)
                success = _result.get("ok", False)
                msg = _result.get("message", _result.get("error", "Unknown error"))
            else:
                _output(
                    args,
                    _dim("(input prompt changed, refreshing)"),
                    {"success": False, "error": "prompt_changed"},
                )
                _output(
                    args,
                    format_pending_request(pending, colour=not args.json),
                    {"pending_action": pending},
                )
                _save_session(session, args, client_state)
                return 1

    _save_session(session, args, client_state)

    if success:
        _run_after_input_text_hook(HandlerContext(session), _result, text)
        # The hook polls the bridge and HOLDS any story that answers the
        # input (a game's opening after the name prompt, an adapter's
        # answer narration).  Held rows live only in this process, so the
        # session must be saved again here: saving only before the hook
        # dropped those rows on exit, and the next `wait` printed nothing.
        _save_session(session, args, client_state)
        # Suppress input confirmation in quiet mode
        if not getattr(args, "quiet", False):
            _output(
                args,
                _green(f'✓ Input submitted: "{text}"'),
                {"success": True, "text": text},
            )
        else:
            _output(args, data={"success": True, "text": text})
        if getattr(args, "wait", False):
            # Small grace for polling gap.  The shim reports the actual
            # typing delay via an observation_started event.
            stale_screen_label = (
                "Confirm"
                if any(k in _result for k in ("_auto_confirmed", "_auto_confirm_skipped"))
                else None
            )
            return _perform_wait(
                session,
                args,
                client_state,
                getattr(args, "timeout", None),
                initial_grace=5.0,
                skip_request_id=session.last_request_id,
                stale_screen_label=stale_screen_label,
            )
    else:
        _output(args, _red(f"✗ {msg}"), {"success": False, "error": msg})
    return 0 if success else 1


def _parse_cmd_args(raw_args: List[str]) -> dict:
    """
    Parse key=value pairs from CLI arguments into a dict.

    Auto-coerces values:  integers, floats, booleans, and JSON arrays/objects.
    """
    result: dict = {}
    for arg in raw_args:
        if "=" not in arg:
            continue
        key, _, val = arg.partition("=")
        key = key.strip()
        val = val.strip()

        # Strip surrounding quotes.
        if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
            val = val[1:-1]

        # Auto-coerce.
        if val.lower() == "true":
            result[key] = True
        elif val.lower() == "false":
            result[key] = False
        elif val.lower() == "null" or val.lower() == "none":
            result[key] = None
        else:
            # Try int.
            try:
                result[key] = int(val)
                continue
            except ValueError:
                pass
            # Try float.
            try:
                result[key] = float(val)
                continue
            except ValueError:
                pass
            # Try JSON (for arrays/objects).
            if val.startswith(("{", "[")):
                try:
                    result[key] = json.loads(val)
                    continue
                except json.JSONDecodeError:
                    pass
            result[key] = val

    return result


def _command_start_seq(session: BridgeClient) -> Optional[int]:
    """Return the bridge event counter immediately before submitting a command."""
    try:
        code, state = session._get("/state", timeout=2.0)
        if code == 200 and isinstance(state, dict):
            return int(state.get("event_counter", 0) or 0)
    except Exception:
        return None
    return None


def _send_and_wait_command_result(
    session: BridgeClient,
    command: str,
    cmd_args: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 5.0,
    reset_boundary: bool = False,
) -> tuple[bool, str, Optional[dict]]:
    """Submit a shim command and wait only for this submit's result."""
    start_seq = _command_start_seq(session)
    nonce = uuid.uuid4().hex
    submitted, msg = session._send_command(command, cmd_args, nonce=nonce)
    if not submitted:
        return False, msg, None
    match = lambda event: event.get("nonce") == nonce
    result = session._wait_command_result(
        command,
        timeout=timeout,
        after_seq=None if reset_boundary else start_seq,
        match=match,
    )
    return True, msg, result


def _apply_profile(
    profile: str,
    session: BridgeClient,
    args: argparse.Namespace,
    client_state: ClientState,
    *,
    quiet: bool = False,
    skip_keys: Optional[Dict[str, str]] = None,
) -> tuple:
    """Apply a named profile from config.  Returns (success, message, data).

    ``skip_keys`` maps profile keys to a human-readable reason (usually an
    explicit launch flag such as ``--fast-forward``).  Matching keys are NOT
    sent to the shim: an explicit launch flag must win over the game's
    default profile, which would otherwise silently flip the mode right
    back (e.g. ``launch --fast-forward`` followed by a default profile with
    ``fast_forward: false``).
    """
    config = _load_config(getattr(args, "games_dir", None))
    if not config:
        return False, "Cannot load vnflight.json", {}
    profiles = config.get("profiles", {})
    if profile not in profiles:
        available = ", ".join(sorted(profiles.keys())) or "(none)"
        return False, f"Unknown profile: {profile!r}. Available: {available}", {}
    changes = profiles[profile]
    skipped: list = []
    if skip_keys and changes:
        skipped = [
            {"key": key, "value": changes[key], "reason": skip_keys[key]}
            for key in changes
            if key in skip_keys
        ]
        changes = {k: v for k, v in changes.items() if k not in skip_keys}
    skip_note = ""
    if skipped:
        kept_flags = sorted({entry["reason"] for entry in skipped})
        skip_note = f" (kept {', '.join(kept_flags)})"
    if not changes:
        return (
            True,
            f"Profile '{profile}' has nothing to apply{skip_note}.",
            {"applied": [], "skipped": skipped},
        )
    cmd_args = {"changes": changes}
    submitted, msg, result = _send_and_wait_command_result(
        session,
        "set",
        cmd_args,
        timeout=15.0,  # Roadwarden: execution follows the first interaction after _start, 3-4 s after the POST
    )
    if not submitted:
        return False, f"Set failed: {msg}", {}
    if result and result.get("success"):
        applied, receipt_error = _validated_setting_application_receipt(
            result.get("applied"), changes)
        if receipt_error:
            return False, (
                "Profile apply was confirmed successful but returned an "
                "invalid application receipt: {}. Inspect current settings "
                "before retrying.".format(receipt_error)
            ), {
                "profile": profile,
                "applied": result.get("applied"),
                "skipped": skipped,
                "reason": "invalid_application_receipt",
                "mutation_may_have_applied": True,
            }
        changed = [
            entry for entry in applied
            if entry.get("old_value") != entry.get("value")
        ]
        if changed:
            change_label = "setting" if len(changed) == 1 else "settings"
            lines = [
                f"Applied profile '{profile}' "
                f"({len(changed)} {change_label} changed){skip_note}"
            ]
        else:
            lines = [f"Profile '{profile}' already applied{skip_note}"]
        if not quiet:
            for entry in changed:
                lines.append(
                    f"  {entry['key']}: {entry['old_value']} → {entry['value']}"
                )
        return True, "\n".join(lines), {
            "profile": profile,
            "applied": applied,
            "skipped": skipped,
        }
    else:
        error = (
            result.get("error", "Unknown") if result else "No confirmation from shim"
        )
        errors = result.get("errors", []) if result else []
        if errors:
            error = "; ".join(errors)
        return False, f"Profile apply failed: {error}", {}


def cmd_set(args: argparse.Namespace, client_state: ClientState) -> int:
    """Set a runtime config value on vnflight."""
    session = _make_session(args, client_state)
    profile = getattr(args, "profile", None)

    # -- Profile mode: batch-apply a named profile from config. --
    if profile is not None:
        # Special "default" profile: query shim for init-time defaults
        # and apply them, restoring all timing values to their originals.
        if profile == "default":
            session.poll(timeout=0.1)  # drain
            submitted, _sm, _def_result = _send_and_wait_command_result(
                session,
                "get_defaults",
                {},
                timeout=3.0,
            )
            if not submitted:
                _output(
                    args,
                    _red(f"✗ Cannot query defaults: {_sm}"),
                    {"success": False, "command": "set", "error": _sm},
                )
                return 1
            if not _def_result or not _def_result.get("success"):
                _output(
                    args,
                    _red("✗ Shim does not support get_defaults (update shim?)"),
                    {
                        "success": False,
                        "command": "set",
                        "error": "get_defaults_unsupported",
                    },
                )
                return 1
            defaults = _def_result.get("defaults", {})
            if not defaults:
                _output(
                    args,
                    _dim("No defaults returned."),
                    {"success": True, "command": "set", "applied": []},
                )
                return 0
            # Only restore keys that appear in at least one defined profile,
            # so we don't clobber game-mod overrides (callbacks, lists, etc.).
            config = _load_config(getattr(args, "games_dir", None))
            _profile_keys: set = set()
            if config:
                for _pv in config.get("profiles", {}).values():
                    if isinstance(_pv, dict):
                        _profile_keys.update(_pv.keys())
            if _profile_keys:
                defaults = {k: v for k, v in defaults.items() if k in _profile_keys}
            if not defaults:
                _output(
                    args,
                    _dim("No profile-relevant defaults to restore."),
                    {"success": True, "command": "set", "applied": []},
                )
                return 0
            submitted, _sm, result = _send_and_wait_command_result(
                session,
                "set",
                {"changes": defaults},
                timeout=5.0,
            )
            if not submitted:
                _output(
                    args,
                    _red(f"✗ Restore failed: {_sm}"),
                    {"success": False, "command": "set", "error": _sm},
                )
                return 1
            _save_session(session, args, client_state)
            if result and result.get("success"):
                applied = result.get("applied", [])
                # Only show keys that actually changed.
                changed = [e for e in applied if e["old_value"] != e["value"]]
                if changed:
                    lines = [f"Restored {len(changed)} settings to defaults"]
                    for entry in changed:
                        lines.append(
                            f"  {entry['key']}: "
                            f"{entry['old_value']} \u2192 {entry['value']}"
                        )
                else:
                    lines = ["All settings already at defaults"]
                _output(
                    args,
                    _green(f"\u2713 {chr(10).join(lines)}"),
                    {
                        "success": True,
                        "command": "set",
                        "profile": "default",
                        "applied": applied,
                    },
                )
            else:
                error = (
                    result.get("error", "Unknown")
                    if result
                    else "No confirmation from shim"
                )
                _output(
                    args,
                    _red(f"\u2717 Restore failed: {error}"),
                    {"success": False, "command": "set", "error": error},
                )
                return 1
            return 0

        ok, msg, data = _apply_profile(profile, session, args, client_state)
        _save_session(session, args, client_state)
        if ok:
            _output(
                args, _green(f"✓ {msg}"), {"success": True, "command": "set", **data}
            )
        else:
            _output(
                args,
                _red(f"✗ {msg}"),
                {"success": False, "command": "set", "error": msg},
            )
        return 0 if ok else 1

    # -- Single-key mode (original behavior). --
    key = args.key
    value = args.value

    if key is None:
        _output(
            args,
            _red("✗ Provide a key name, or use --profile"),
            {"success": False, "command": "set", "error": "No key provided"},
        )
        _save_session(session, args, client_state)
        return 1

    if value is None:
        # Query mode: just get the current value.
        cmd_args = {"key": key}
    else:
        # Try to parse value as JSON-ish (bool, int, float, string).
        if value.lower() in ("true", "yes"):
            parsed = True
        elif value.lower() in ("false", "no"):
            parsed = False
        else:
            try:
                parsed = int(value)
            except ValueError:
                try:
                    parsed = float(value)
                except ValueError:
                    parsed = value
        cmd_args = {"key": key, "value": parsed}

    submitted, msg, result = _send_and_wait_command_result(
        session,
        "set",
        cmd_args,
        timeout=3.0,
    )
    if not submitted:
        _save_session(session, args, client_state)
        _output(
            args,
            _red(f"✗ Set failed: {msg}"),
            {"success": False, "command": "set", "error": msg},
        )
        return 1

    _save_session(session, args, client_state)

    if result and result.get("success"):
        old_val = result.get("old_value")
        new_val = result.get("value")
        if old_val is not None:
            _output(
                args,
                _green(f"✓ {key} = {new_val} (was {old_val})"),
                {"success": True, "key": key, "value": new_val, "old_value": old_val},
            )
        else:
            _output(
                args,
                _green(f"✓ {key} = {new_val}"),
                {"success": True, "key": key, "value": new_val},
            )
        return 0
    else:
        error = (
            result.get("error", "Unknown") if result else "No confirmation from shim"
        )
        _output(
            args,
            _red(f"✗ Set failed: {error}"),
            {"success": False, "command": "set", "error": error},
        )
        return 1


def cmd_cmd(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    name = args.command_name

    cmd_args = _parse_cmd_args(args.command_args) if args.command_args else None

    # Special handling for inventory_modify: turn flat key=value into changes list.
    if name == "inventory_modify" and cmd_args and "action" in cmd_args:
        action = cmd_args.pop("action")
        item = cmd_args.pop("item", "")
        changes = [{"action": action, "item": item}]
        cmd_args = {"changes": changes}

    # Special handling for stats_modify: wrap flat key=value into changes dict.
    if name == "stats_modify" and cmd_args and "changes" not in cmd_args:
        cmd_args = {"changes": cmd_args}

    if getattr(args, "wait", False):
        nonce = uuid.uuid4().hex
        success, msg = session._send_command(name, cmd_args, nonce=nonce)
        result = None
    else:
        success, msg, result = _send_and_wait_command_result(
            session,
            name,
            cmd_args,
            timeout=3.0,
            reset_boundary=name in {"start", "load"},
        )
    _save_session(session, args, client_state)

    if success:
        # Suppress command confirmation in quiet mode
        if not getattr(args, "quiet", False):
            _output(
                args,
                _green(f"✓ Command '{name}' accepted."),
                {"success": True, "command": name, "message": msg},
            )
        else:
            _output(args, data={"success": True, "command": name, "message": msg})
        # Wait for and display the command result unless --wait is used
        # (in which case _perform_wait will show events including the result).
        if getattr(args, "wait", False):
            return _perform_wait(
                session, args, client_state, getattr(args, "timeout", None)
            )
        else:
            _save_session(session, args, client_state)
            if result:
                if args.json:
                    _output(args, data=result)
                else:
                    # Pretty-print the result.
                    _ok = result.get("success", True)
                    _filtered = {
                        k: v
                        for k, v in result.items()
                        if k not in ("type", "command", "_seq")
                    }
                    if _ok:
                        for k, v in _filtered.items():
                            if k == "success":
                                continue
                            print(f"  {k}: {v}")
                    else:
                        error = result.get("error", "Unknown")
                        print(_red(f"  Error: {error}"))
                        return 1
            else:
                error = f"Command '{name}' was submitted but not confirmed"
                _output(
                    args,
                    _red(f"✗ {error}"),
                    {
                        "success": False,
                        "command": name,
                        "confirmed": False,
                        "error": error,
                    },
                )
                return 1
    else:
        _output(
            args, _red(f"✗ {msg}"), {"success": False, "command": name, "error": msg}
        )
    return 0 if success else 1


def cmd_back(args: argparse.Namespace, client_state: ClientState) -> int:
    """Close the current game-menu screen (equivalent to pressing Escape)."""
    return _cmd_story_navigation(
        args,
        client_state,
        command_name="back",
        description="Back (Return)",
        initial_grace=1.0,
    )


def cmd_back_all(args: argparse.Namespace, client_state: ClientState) -> int:
    """Close overlay/menu screens until the story screen is back (bounded).

    Same handler as MCP ``back_all``: a loop over the shim's ``back`` with
    ``overlays_only`` set, so it refuses (``nothing_to_close``) instead of
    sending a bare Return into the story once nothing dismissable shows.
    """
    session = _make_session(args, client_state)
    ctx = HandlerContext(session, allow_live_overlay_lookahead=False)
    result = handle_tool(ctx, "back_all", {})
    _save_session(session, args, client_state)

    if not isinstance(result, dict):
        result = {"error": str(result)}
    public = strip_internal_result_fields(result)
    closed = int(result.get("closed") or 0)
    if not bool(result.get("success", result.get("ok"))):
        error = (
            result.get("error")
            or result.get("reason")
            or "back_all was not applied"
        )
        partial = f" (closed {closed} first)" if closed else ""
        _output(
            args,
            _red(f"✗ back_all failed: {error}{partial}"),
            {"success": False, "command": "back_all", **public},
        )
        return 1

    line = f"✓ Closed {closed} overlay{'' if closed == 1 else 's'}."
    warning = result.get("warning")
    if isinstance(warning, str) and warning:
        line += f" {warning}"
    _output(
        args,
        _green(line),
        {"success": True, "command": "back_all", **public},
    )
    if not getattr(args, "json", False):
        # The handler appends a brief state read so the caller sees where
        # it landed; show it without repeating the closed-count summary.
        text = result.get("text")
        if isinstance(text, str) and text.startswith("back_all: closed"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        if text and text.strip():
            print(text.rstrip())
    if getattr(args, "wait", False):
        return _perform_wait(
            session,
            args,
            client_state,
            getattr(args, "timeout", None),
            initial_grace=1.0,
        )
    return 0


def _cmd_story_navigation(
    args: argparse.Namespace,
    client_state: ClientState,
    *,
    command_name: str,
    description: str,
    initial_grace: float = 0.3,
) -> int:
    """Run one story navigation command and report the shim's actual result.

    Routes through the same handler MCP uses (``handle_tool``), which waits
    for the nonce-matched ``command_result``.  Submission alone used to be
    reported as "queued" with exit 0, so a refusal the shim sent a moment
    later (``rollback_disabled_by_game``, ``nothing_to_close``, ...) was
    invisible to a CLI-driven agent.
    """
    session = _make_session(args, client_state)
    ctx = HandlerContext(session, allow_live_overlay_lookahead=False)
    result = handle_tool(ctx, command_name, {})
    _save_session(session, args, client_state)

    if not isinstance(result, dict):
        result = {"error": str(result)}
    public = strip_internal_result_fields(result)
    applied = bool(result.get("success", result.get("ok")))
    if not applied:
        error = (
            result.get("error")
            or result.get("reason")
            or f"{description} was not applied"
        )
        _output(
            args,
            _red(f"✗ {description} failed: {error}"),
            {"success": False, "command": command_name, **public},
        )
        # An advance refused by a menu that became active meanwhile carries
        # that decision; show it so the next act can be chosen from it.
        if not getattr(args, "json", False) and result.get("pending"):
            print(render_tool_result_text(result))
        return 1

    message = result.get("message")
    suffix = f" {message}" if isinstance(message, str) and message else ""
    _output(
        args,
        _green(f"✓ {description} applied.{suffix}"),
        {"success": True, "command": command_name, **public},
    )
    if getattr(args, "wait", False):
        return _perform_wait(
            session,
            args,
            client_state,
            getattr(args, "timeout", None),
            initial_grace=initial_grace,
        )
    return 0


def cmd_advance(args: argparse.Namespace, client_state: ClientState) -> int:
    """Advance one dialogue interaction without enabling auto-forward."""
    return _cmd_story_navigation(
        args,
        client_state,
        command_name="advance",
        description="Advance",
    )


def cmd_rewind(args: argparse.Namespace, client_state: ClientState) -> int:
    """Move one normal dialogue/checkpoint backward."""
    return _cmd_story_navigation(
        args,
        client_state,
        command_name="rewind",
        description="Rewind",
    )


def cmd_replay(args: argparse.Namespace, client_state: ClientState) -> int:
    """Roll forward after a prior story rollback."""
    return _cmd_story_navigation(
        args,
        client_state,
        command_name="replay",
        description="Replay",
    )


def cmd_state(args: argparse.Namespace, client_state: ClientState) -> int:
    """Show the current game state.

    Delegates to build_state_data + format_state_text (the same pipeline
    MCP uses for the ``state`` tool) so CLI and MCP outputs stay in
    sync.  Adds CLI-only envelope: "On Screen" narration preamble,
    "Recent" post-scrape dialogue delta, and save-slot listing.
    """
    session = _make_session(args, client_state)
    state = session.state()

    # BridgeClient.state() returns {} on connection failure, so a plain
    # None check let a dead bridge print nothing and exit 0.  Distinguish
    # "access denied" (403 — check first: the is_up probe hits an open
    # route and would overwrite the recorded status) from "bridge
    # unreachable" from "bridge up but no game state".
    if not state:
        if _slot_access_denied(session):
            return _access_denied_error(args, session)
        if not _bridge_is_up(session):
            return _bridge_unreachable_error(args, session)
        slot_part = (
            f" for slot '{session.slot_prefix.lstrip('/')}'"
            if getattr(session, "slot_prefix", "")
            else ""
        )
        msg = (
            f"Bridge at {session.bridge_url} is up but returned no game "
            f"state{slot_part} — no game is connected. "
            "Check 'vnflight.py slots'."
        )
        _output(
            args,
            _red(f"✗ {msg}"),
            {"success": False, "error": "no_state", "message": msg},
        )
        return 1

    # Merge screen data for button/overlay visibility.
    code_sc, screen_data = session._get("/screen", timeout=2.0)
    if code_sc == 200 and screen_data and screen_data.get("screen"):
        state["screen"] = screen_data["screen"]

    # Persist the rendered request id: `state` output IS "the last
    # rendered numbered list", so a follow-up `act N` binds to what this
    # command just showed.  Without this, wait → (game advances) →
    # state → act N would be refused as stale even though the caller is
    # replying to the state output it just read.
    _save_session(session, args, client_state)

    data = build_state_data(state)
    at_main_menu = classify_lifecycle(state).get("context") == "main_menu"

    if args.json:
        rendered = format_state_text(data, verbose=True, fmt="json")
        rendered = strip_internal_result_fields(rendered)
        _output(args, data=rendered)
        return 0

    verbose = getattr(args, "verbose", False)
    ctx = state.get("context") or {}
    save_slots = ctx.get("save_slots", [])
    transcript = state.get("transcript", [])

    if verbose:
        print(_bold("Game State"))
        print(f"  Status:  {state.get('status', '?')}")
        print(f"  Context: {ctx.get('context', '?')}")
        print(f"  Events:  {state.get('event_counter', 0)}")
        print()

    # --- CLI preamble: "On Screen" narration from screen_content ---
    # The shared formatter doesn't surface narration/modal texts
    # verbatim, so keep this section as CLI-exclusive context.
    # Skip when an overlay (map/shop/etc.) is active — the shared
    # pipeline already renders overlay texts via _screen_texts, and
    # printing both here would duplicate the header.
    latest_screen_content = state.get("screen")
    if not latest_screen_content:
        for _ti in range(len(transcript) - 1, -1, -1):
            if transcript[_ti].get("type") == "screen_content":
                latest_screen_content = transcript[_ti]
                break
    _overlay_active_for_preamble = bool(
        latest_screen_content
        and (latest_screen_content.get("overlay_active")
             or latest_screen_content.get("modal_screens")))

    if (latest_screen_content and not _overlay_active_for_preamble
            and not data.get("_screen_texts")):
        sc_texts = latest_screen_content.get("texts", [])
        pending_for_echo = data.get("pending")
        if sc_texts and pending_for_echo:
            keep_indices = [
                idx for idx, text in enumerate(sc_texts)
                if not _is_pending_input_prompt_echo(text, pending_for_echo)
            ]
            if len(keep_indices) != len(sc_texts):
                raw_texts = list(sc_texts)
                sc_texts = [raw_texts[idx] for idx in keep_indices]
                raw_sources = latest_screen_content.get("text_sources", [])
                if len(raw_sources) == len(raw_texts):
                    latest_screen_content = dict(latest_screen_content)
                    latest_screen_content["text_sources"] = [
                        raw_sources[idx] for idx in keep_indices
                    ]
        sc_screens = latest_screen_content.get("screens", [])
        if sc_texts:
            _modal_set = set(latest_screen_content.get("modal_screens") or [])
            _text_sources = latest_screen_content.get("text_sources", [])
            _screen_names = latest_screen_content.get("screen_names", {})
            if _modal_set and _text_sources and len(_text_sources) == len(sc_texts):
                _base_texts = [
                    t for t, s in zip(sc_texts, _text_sources) if s not in _modal_set
                ]
                _modal_texts: Dict[str, List[str]] = {}
                for t, s in zip(sc_texts, _text_sources):
                    if s in _modal_set:
                        _modal_texts.setdefault(s, []).append(t)
                if _base_texts:
                    if verbose:
                        _non_modal = [s for s in sc_screens if s not in _modal_set]
                        screen_label = ", ".join(_non_modal) if _non_modal else "visible"
                        print(_bold(f"On Screen ({screen_label}):"))
                    else:
                        print(_bold("On Screen:"))
                    for t in _base_texts:
                        print(f"  {t}")
                for _ms, _mt in _modal_texts.items():
                    _ms_label = _screen_names.get(_ms, _ms)
                    print()
                    print(_bold(f"[{_ms_label}]"))
                    for t in _mt:
                        print(f"  {t}")
            else:
                if verbose:
                    screen_label = ", ".join(sc_screens) if sc_screens else "visible"
                    print(_bold(f"On Screen ({screen_label}):"))
                else:
                    print(_bold("On Screen:"))
                for t in sc_texts:
                    print(f"  {t}")
            print()
        # Categorized texts (credits etc.) kept as CLI envelope.
        cat_texts = latest_screen_content.get("categorized_texts", {})
        for cat_name, cat_items in cat_texts.items():
            if cat_items:
                print(f"--- {cat_name.upper()} ---")
                for ct in cat_items:
                    print(f"  {ct}")

    # --- Shared pipeline: pending + buttons ---
    # build_state_data + format_state_text produce the same section
    # text the MCP ``state`` tool returns — single source of truth for
    # choice / button rendering.
    rendered = format_state_text(data, verbose=True)

    # A crashed game leads, as in the MCP render: every later section
    # looks normal on the exception screen.
    if rendered.get("anomaly_note"):
        print(_red("⚠ " + rendered["anomaly_note"]))
        print()

    # Status line (shown only when no pending + no overlay).
    if rendered.get("status"):
        print(f"status: {rendered['status']}")

    # Overlay screen texts (shop resources, character sheet etc.).
    if rendered.get("text"):
        print(rendered["text"])
        print()

    # Pending choice / input section.
    if rendered.get("pending"):
        print(rendered["pending"])
        print()

    # Categorized buttons (NAVIGATION, TOPICS, ITEMS, etc.).
    if rendered.get("buttons"):
        print(rendered["buttons"])
        print()

    # --- Recent: post-scrape dialogue delta ---
    _latest_sc_idx = -1
    for _ti in range(len(transcript) - 1, -1, -1):
        if transcript[_ti].get("type") == "screen_content":
            _latest_sc_idx = _ti
            break
    if (not at_main_menu and _latest_sc_idx >= 0
            and _latest_sc_idx < len(transcript) - 1):
        _post_sc_lines: list = []
        for _pe in transcript[_latest_sc_idx + 1:]:
            _pet = _pe.get("type", "")
            if _pet == "dialogue":
                _who = _pe.get("character", "")
                _what = _pe.get("text", "")
                _post_sc_lines.append(f"[{_who}] {_what}" if _who else _what)
            elif _pet == "narration":
                _post_sc_lines.append(_pe.get("text", ""))
            elif _pet == "screen_content":
                if _pe.get("_lightweight"):
                    continue
                break
        if _post_sc_lines:
            print(_dim("Recent:"))
            for _psl in _post_sc_lines:
                print(f"  {_psl}")
            print()

    # --- Stats / inventory footer ---
    if rendered.get("_footer"):
        print(rendered["_footer"])
        print()
    elif not at_main_menu and not getattr(args, "no_stats", False):
        # Fallback: query shim for fresh stats (mostly for games
        # without a stats summary string).
        _fresh_stats = {}
        try:
            session.poll(timeout=0.1)
            _submitted, _msg = session._send_command("get_stats", {})
            if _submitted:
                _gs_result = session._wait_command_result("get_stats", timeout=3.0)
                if _gs_result and _gs_result.get("success"):
                    _fresh_stats = _gs_result.get("stats") or {}
        except Exception:
            pass
        if _fresh_stats:
            _summary = _fresh_stats.get("_summary")
            if _summary:
                print(f"  {_summary}")
                print()

    # --- Save slots (CLI-only) ---
    if save_slots:
        print(_bold("Save Slots:"))
        print(f"  {', '.join(map(str, save_slots))}")
        print()

    return 0


def cmd_save(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    slot = args.slot or "1-1"   # the shim's own default
    name = args.name or "LLM Player Save"
    success, msg, result = _send_and_wait_command_result(
        session,
        "save",
        {"slot": slot, "name": name},
        timeout=5.0,
    )
    _save_session(session, args, client_state)

    if success and not result:
        error = "Save command was submitted but not confirmed"
        _output(
            args,
            _red(error),
            {"success": False, "slot": slot, "confirmed": False, "error": error},
        )
        return 1
    if success and not result.get("success", True):
        error = result.get("error", "Save failed")
        _output(
            args,
            _red(f"Save failed for slot '{slot}': {error}"),
            {
                "success": False,
                "slot": slot,
                "error": error,
                "result": result,
            },
        )
        return 1

    if success:
        _output(
            args,
            _green(f"✓ Save command for slot '{slot}' confirmed."),
            {
                "success": True,
                "slot": slot,
                "message": msg,
                "confirmed": True,
                "result": result,
            },
        )
    else:
        _output(
            args,
            _red(f"✗ {msg}"),
            {"success": False, "error": msg},
        )
    return 0 if success else 1


def cmd_save_scan(args: argparse.Namespace, client_state: ClientState) -> int:
    """Read-only scan for saves that may contain vnflight shim references."""
    root = Path(args.path).expanduser()
    if not root.exists():
        _output(
            args,
            _red(f"Path does not exist: {root}"),
            {"error": "path_not_found", "path": str(root)},
        )
        return 1
    results = [scan_save_file(p) for p in iter_save_files(root, args.recursive)]
    summary = summarize_save_scan_results(results)
    data = {
        "path": str(root),
        "recursive": bool(args.recursive),
        **summary,
        "results": results,
    }
    _output(args, format_save_scan_results(root, results), data)
    return 0


def _load_state_marker(session: BridgeClient) -> tuple[int, Optional[str]]:
    state = session.state()
    counter = int(state.get("event_counter") or 0)
    pending = state.get("pending_request") or {}
    pending_id = pending.get("id") if isinstance(pending, dict) else None
    return counter, pending_id


def _wait_for_load_ready(
    session: BridgeClient,
    preload_cursor: int,
    preload_pending_id: Optional[str],
    timeout: float = 5.0,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        counter, pending_id = _load_state_marker(session)
        if counter < preload_cursor:
            _reset_cursor_for_full_read(session)
            return True
        if pending_id and pending_id != preload_pending_id:
            _reset_cursor_for_full_read(session)
            return True
        if counter > preload_cursor and _get_latest_interactions(session, pending=None):
            session.cursor = preload_cursor
            return True
        time.sleep(0.1)
    session.cursor = preload_cursor
    return False


def _format_current_screen_prompt(
    session: BridgeClient,
    args: argparse.Namespace,
    *,
    screen: Optional[dict] = None,
) -> str:
    if screen is not None:
        # The wait loop has already checked this snapshot's delivery boundary.
        # A second live read here could expose a newer, unchecked menu.
        options = dict(colour=not args.json, quiet=getattr(args, "quiet", False),
                       verbose=getattr(args, "verbose", False))
        if screen.get("interactions"):
            return format_interactions(screen["interactions"], **options)
        return format_screen_buttons(screen.get("buttons") or [],
                                     screen.get("screens"), **options)
    pending = session.pending()
    if pending:
        return format_pending_request(
            pending,
            colour=not args.json,
            quiet=getattr(args, "quiet", False),
            show_stats=False,
        )

    sc_ev = _get_live_screen(session)
    # A current legacy button snapshot is authoritative even without the
    # newer interactions field. Do not resurrect transcript choices behind it.
    if sc_ev is not None and "buttons" in sc_ev and "interactions" not in sc_ev:
        return format_screen_buttons(
            sc_ev.get("buttons") or [], sc_ev.get("screens"),
            colour=not args.json, quiet=getattr(args, "quiet", False),
            verbose=getattr(args, "verbose", False),
        )

    interactions = _get_latest_interactions(session, pending=None)
    if interactions:
        return format_interactions(
            interactions,
            colour=not args.json,
            quiet=getattr(args, "quiet", False),
            verbose=getattr(args, "verbose", False),
        )

    sc_ev = _get_latest_screen_buttons(session)
    if sc_ev and sc_ev.get("buttons"):
        return format_screen_buttons(
            sc_ev.get("buttons", []),
            sc_ev.get("screens"),
            colour=not args.json,
            quiet=getattr(args, "quiet", False),
            verbose=getattr(args, "verbose", False),
        )

    return ""


def _current_screen_text_event(
    session: BridgeClient,
    *,
    timeout: float = 2.0,
) -> Optional[dict]:
    """Return current screen text as a synthetic screen_content event."""
    deadline = time.time() + timeout
    sc_ev = None
    while time.time() < deadline:
        sc_ev = _get_settled_screen_buttons(
            session,
            timeout=0.4,
            return_initial_when_stable=True,
        )
        if sc_ev and sc_ev.get("texts"):
            break
        getter = getattr(session, "_get", None)
        if callable(getter):
            try:
                code, data = getter("/screen", timeout=2.0)
            except Exception:
                code, data = 0, {}
            if code == 200 and data:
                sc_ev = data.get("screen")
                if sc_ev and sc_ev.get("texts"):
                    break
        state_getter = getattr(session, "state", None)
        if callable(state_getter):
            try:
                state = state_getter() or {}
            except Exception:
                state = {}
            for source in (
                state.get("screen") or {},
                state.get("game_state") or {},
            ):
                if source.get("texts"):
                    sc_ev = source
                    break
            if sc_ev and sc_ev.get("texts"):
                break
        transcript_getter = getattr(session, "get_transcript", None)
        if callable(transcript_getter):
            try:
                transcript = transcript_getter(last_n=30)
            except Exception:
                transcript = []
            for ev in reversed(transcript):
                if ev.get("type") == "screen_content" and ev.get("texts"):
                    sc_ev = ev
                    break
                if ev.get("type") in ("narration", "dialogue"):
                    text = str(ev.get("text") or "").strip()
                    if text:
                        sc_ev = {"type": "screen_content", "texts": [text]}
                        break
            if sc_ev and sc_ev.get("texts"):
                break
        time.sleep(0.1)
    texts = [
        str(t)
        for t in (sc_ev or {}).get("texts") or []
        if str(t).strip()
    ]
    if not texts:
        return None
    return {
        "type": "screen_content",
        "texts": texts,
        "screens": (sc_ev or {}).get("screens", []),
    }


def _load_events_with_screen_text(
    events: list[dict],
    session: BridgeClient,
) -> list[dict]:
    """Prepend current screen text when load events contain a bare prompt."""
    if not events or not _events_include_request_prompt(events):
        return events
    first_prompt = next(
        (
            i for i, e in enumerate(events)
            if e.get("type") in ("choice_request", "input_request")
        ),
        None,
    )
    if first_prompt is not None:
        text_after_prompt = []
        remaining = []
        for i, event in enumerate(events):
            has_text = (
                event.get("type") in ("narration", "dialogue")
                or (
                    event.get("type") == "screen_content"
                    and event.get("texts")
                )
            )
            if i > first_prompt and has_text:
                text_after_prompt.append(event)
            else:
                remaining.append(event)
        if text_after_prompt:
            insert_at = next(
                (
                    i for i, e in enumerate(remaining)
                    if e.get("type") in ("choice_request", "input_request")
                ),
                0,
            )
            return [
                *remaining[:insert_at],
                *text_after_prompt,
                *remaining[insert_at:],
            ]
    has_text = any(
        e.get("type") in ("narration", "dialogue")
        or (e.get("type") == "screen_content" and e.get("texts"))
        for e in events
    )
    if has_text:
        return events
    screen_text = _current_screen_text_event(session, timeout=4.0)
    if not screen_text:
        return events
    return [screen_text, *events]


def _load_events_with_late_text(
    events: list[dict],
    session: BridgeClient,
    *,
    timeout: float = 8.0,
) -> list[dict]:
    """Drain text that can arrive just after a load-time request prompt."""
    if not events:
        return events
    if _events_include_text(events):
        return events

    collected = list(events)
    seen_prompts = {
        _request_event_key(event)
        for event in events
        if event.get("type") in ("choice_request", "input_request")
    }
    poll = getattr(session, "poll", None)
    if not callable(poll):
        return collected
    if not callable(getattr(session, "transcript", None)):
        timeout = min(timeout, 0.5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        more = poll(timeout=min(0.5, remaining))
        if more:
            appended = False
            for event in more:
                if event.get("type") in ("choice_request", "input_request"):
                    key = _request_event_key(event)
                    if key in seen_prompts:
                        continue
                    seen_prompts.add(key)
                collected.append(event)
                appended = True
            if _events_include_text(more):
                break
            if not appended:
                time.sleep(0.1)
        else:
            time.sleep(0.1)
    return collected


def _request_event_key(event: dict) -> tuple:
    return (
        event.get("type"),
        event.get("id"),
        tuple(str(choice) for choice in event.get("choices") or []),
    )


def _events_include_text(events: list[dict]) -> bool:
    return any(
        event.get("type") in ("narration", "dialogue")
        or (event.get("type") == "screen_content" and event.get("texts"))
        for event in events
    )


def _bare_load_prompt_needs_wait(events: list[dict], session: BridgeClient) -> bool:
    if not _events_include_request_prompt(events) or _events_include_text(events):
        return False
    if not callable(getattr(session, "transcript", None)):
        return False
    return any(
        event.get("type") not in ("choice_request", "input_request")
        for event in events
    )


def _screen_is_stale_in_game_menu(screen: Optional[dict], context: Optional[str]) -> bool:
    """Return True when a stale main-menu scrape is visible during gameplay."""
    if context != "in_game":
        return False
    return screen_names(screen) == {"menu"}


def _current_screen_has_actionable_prompt(session: BridgeClient) -> bool:
    """Return True for real screen prompts while ignoring passive quick-menu UI."""
    pending = session.pending()
    if pending:
        return True

    return _screen_event_has_actionable_prompt({
        "interactions": _get_latest_interactions(session, pending=None),
    })


def _screen_event_has_actionable_prompt(event: dict) -> bool:
    """Return True for real screen prompts while ignoring passive nav chrome."""
    interactions = event.get("interactions") or []
    if interactions:
        for itr in interactions:
            if not isinstance(itr, dict) or item_is_disabled(itr):
                continue
            if item_is_default_focus_chrome(itr):
                continue
            category = str(itr.get("category") or "")
            typ = str(itr.get("type") or "")
            if category == "info" or typ == "info":
                continue
            if category == "navigation" and typ == "nav":
                continue
            return True
        return False

    for itr in event.get("buttons") or []:
        if not isinstance(itr, dict) or item_is_disabled(itr):
            continue
        if item_is_default_focus_chrome(itr):
            continue
        screen = str(itr.get("screen") or "")
        if screen == "quick_menu":
            continue
        return True

    return False


def _events_include_request_prompt(events: list[dict]) -> bool:
    for event in events:
        if event.get("type") in ("choice_request", "input_request"):
            return True
    return False


def _events_include_screen_action_prompt(events: list[dict]) -> bool:
    for event in events:
        if event.get("type") == "screen_content" and (
            event.get("buttons") or event.get("interactions")
        ):
            return True
    return False


def _find_command_result(
    events: list[dict],
    command: str,
    *,
    nonce: Optional[str] = None,
) -> Optional[dict]:
    for event in events:
        if event.get("type") != "command_result" or event.get("command") != command:
            continue
        if nonce is not None and event.get("nonce") != nonce:
            continue
        return event
    return None


def _fast_forward_to_state_counter(
    session: BridgeClient,
    settle_timeout: float = 0.8,
) -> None:
    """Mark the current loaded state transcript as already observed."""
    deadline = time.time() + max(0.0, settle_timeout)
    counter = 0
    stable_seen_at = 0.0
    while True:
        try:
            state = session.state()
        except Exception:
            break
        try:
            next_counter = int((state or {}).get("event_counter") or 0)
        except Exception:
            next_counter = 0
        now = time.time()
        if next_counter > counter:
            counter = next_counter
            stable_seen_at = now
        elif counter and stable_seen_at and now - stable_seen_at >= 0.2:
            break
        if now >= deadline:
            break
        time.sleep(0.05)
    if counter > session.cursor:
        session.cursor = counter


def _screen_event_contains_label(event: dict, label: Optional[str]) -> bool:
    if not label:
        return False
    target = _normalize_quotes_client(label).lower().strip()
    for item in event.get("interactions") or event.get("buttons") or []:
        item_label = _normalize_quotes_client(
            str(item.get("display_label") or item.get("label") or "")
        ).lower().strip()
        if not item_label:
            continue
        if (
            item_label == target
            or (len(target) >= 8 and target in item_label)
            or (len(item_label) >= 8 and item_label in target)
        ):
            return True
    return False


def cmd_load(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    slot = args.slot
    # Loading is asynchronous.  Keep the pre-load cursor instead of resetting
    # immediately; poll() will detect the eventual counter reset and then read
    # the new save's transcript without briefly dumping stale history.
    preload_cursor, preload_pending_id = _load_state_marker(session)
    session.cursor = preload_cursor
    load_nonce = uuid.uuid4().hex
    # Rows stashed before the load (restored from the session file) belong
    # to the timeline a successful load replaces; the MCP client drops them
    # in reconcile_after_load.  Hold them aside until the outcome is known:
    # a failed or unconfirmed load leaves the timeline, and those unread
    # rows, in place.
    held_stash = ActionDeliveryOwnership._take_held_events(session)

    def _keep_unread_rows() -> None:
        if held_stash:
            preserve_prefetched_events(session, held_stash)
            _save_session(session, args, client_state)

    # No slot: send none, and the shim loads its newest save.
    success, msg = session._send_command(
        "load", {"slot": slot} if slot else {}, nonce=load_nonce)
    load_ready = False
    load_events: list[dict] = []

    if success:
        load_ready = _wait_for_load_ready(
            session,
            preload_cursor,
            preload_pending_id,
            timeout=5.0,
        )
        _wait_trace(
            args,
            "load_ready",
            ready=load_ready,
            preload_cursor=preload_cursor,
            preload_pending_id=preload_pending_id,
        )
        session.last_request_id = None
        session.last_request_type = None
        session.last_choices = None
        session.last_actionable_snapshot = None
        poll_fn = getattr(session, "poll", None)
        if callable(poll_fn):
            if load_ready:
                load_events = poll_fn(timeout=0.5)
            else:
                load_events = poll_fn(timeout=0)
        _wait_trace(
            args,
            "load_events_initial",
            count=len(load_events),
            types=[event.get("type") for event in load_events],
            has_text=_events_include_text(load_events),
            has_prompt=_events_include_request_prompt(load_events),
        )
    _save_session(session, args, client_state)

    if success:
        load_result = _find_command_result(load_events, "load", nonce=load_nonce)
        if load_result and not load_result.get("success"):
            error = load_result.get("error", "Load failed")
            _output(
                args,
                _red(f"Load failed for slot '{slot or 'newest'}': {error}"),
                {
                    "success": False,
                    "slot": slot,
                    "error": error,
                    "result": load_result,
                },
            )
            _keep_unread_rows()
            return 1
        if not load_result:
            error = "Load command was submitted but not confirmed"
            _output(
                args,
                _red(error),
                {"success": False, "slot": slot, "confirmed": False, "error": error},
            )
            _keep_unread_rows()
            return 1
        _output(
            args,
            _green(f"Load command for slot '{slot}' confirmed."),
            {
                "success": True,
                "slot": slot,
                "message": msg,
                "confirmed": True,
                "result": load_result,
            },
        )
        if getattr(args, "wait", False):
            if load_ready and not args.json:
                events = load_events
                printed = False
                already_showed_prompt = False
                if events:
                    events = _freshen_choice_request_events(events, session)
                    _wait_trace(
                        args,
                        "load_events_freshened",
                        count=len(events),
                        types=[event.get("type") for event in events],
                        has_text=_events_include_text(events),
                        has_prompt=_events_include_request_prompt(events),
                    )
                    events = _load_events_with_late_text(events, session)
                    _wait_trace(
                        args,
                        "load_events_late_text",
                        count=len(events),
                        types=[event.get("type") for event in events],
                        has_text=_events_include_text(events),
                        has_prompt=_events_include_request_prompt(events),
                    )
                    events = _load_events_with_screen_text(events, session)
                    _wait_trace(
                        args,
                        "load_events_screen_text",
                        count=len(events),
                        types=[event.get("type") for event in events],
                        has_text=_events_include_text(events),
                        has_prompt=_events_include_request_prompt(events),
                    )
                    if _bare_load_prompt_needs_wait(events, session):
                        _wait_trace(args, "load_events_wait_fallback")
                        return _perform_wait(
                            session,
                            args,
                            client_state,
                            min(float(getattr(args, "timeout", None) or 8.0), 8.0),
                        )
                    already_showed_prompt = _events_include_request_prompt(events)
                    text = format_events(
                        events,
                        colour=True,
                        quiet=getattr(args, "quiet", False),
                        verbose=getattr(args, "verbose", False),
                        show_stats=False,
                    )
                    if text.strip():
                        print(text, flush=True)
                        printed = True
                    if (
                        not already_showed_prompt
                        and _events_include_screen_action_prompt(events)
                    ):
                        already_showed_prompt = session.pending() is None
                prompt_text = ""
                if not already_showed_prompt:
                    prompt_deadline = time.time() + 2.0
                    while time.time() < prompt_deadline:
                        prompt_text = _format_current_screen_prompt(session, args)
                        if prompt_text.strip():
                            break
                        time.sleep(0.2)
                if prompt_text.strip():
                    if printed:
                        print()
                    print(prompt_text, flush=True)
                    printed = True
                if printed:
                    _fast_forward_to_state_counter(session)
                    _save_session(session, args, client_state)
                    return 0
            return _perform_wait(
                session, args, client_state, getattr(args, "timeout", None)
            )
    else:
        _output(
            args,
            _red(f"✗ {msg}"),
            {"success": False, "error": msg},
        )
        _keep_unread_rows()
    return 0 if success else 1


def cmd_history(args: argparse.Namespace, client_state: ClientState) -> int:
    session = _make_session(args, client_state)
    show_all = getattr(args, "all", False)
    first_n = getattr(args, "first", None)
    last_n = getattr(args, "last", None)
    verbose = getattr(args, "verbose", False)
    quiet = getattr(args, "quiet", False)

    # Always fetch a generous buffer — slicing is done after filtering.
    events = session.transcript(
        last=9999 if (show_all or first_n) else max(last_n or 5, 5) * 10
    )

    if not events:
        if _slot_access_denied(session):
            return _access_denied_error(args, session)
        if not _bridge_is_up(session):
            return _bridge_unreachable_error(args, session)
        _output(args, _dim("(no transcript events)"), {"events": [], "event_count": 0})
        return 0

    if args.json:
        # JSON mode: apply raw slicing without filtering.
        if first_n and not show_all:
            events = events[:first_n]
        elif not show_all:
            events = events[-(last_n or 5) :]
        public_events = _public_wait_events(events)
        data = {"events": public_events, "event_count": len(public_events)}
        if _diag_events_enabled(args):
            data["diag_events"] = events
            data["diag_event_count"] = len(events)
        _output(args, data=data)
    else:
        # Text mode: filter to displayable events, then slice.
        displayable = []
        for ev in events:
            formatted = format_event(ev, colour=True, quiet=quiet, verbose=verbose)
            if formatted is not None:
                displayable.append((ev, formatted))

        if not displayable:
            print(_dim("(no displayable events)"))
            return 0

        # Collapse consecutive identical formatted lines.
        deduped = []
        _prev_fmt = None
        for ev, fmt in displayable:
            if fmt == _prev_fmt:
                continue
            deduped.append((ev, fmt))
            _prev_fmt = fmt
        displayable = deduped

        if show_all:
            pass  # show everything
        elif first_n:
            displayable = displayable[:first_n]
        else:
            displayable = displayable[-(last_n or 5) :]

        print("\n".join(fmt for _, fmt in displayable))
    return 0


def cmd_screenshot(args: argparse.Namespace, state: ClientState) -> int:
    session = _make_session(args, state)
    b64_data = session.screenshot()
    if not b64_data:
        if _slot_access_denied(session):
            return _access_denied_error(args, session)
        if not _bridge_is_up(session):
            return _bridge_unreachable_error(args, session)
        if args.json:
            print(json.dumps({"error": "No screenshot available"}, ensure_ascii=False))
        else:
            print(_red("No screenshot available."))
        return 1

    output_path = args.output
    try:
        image_data = base64.b64decode(b64_data)
        with open(output_path, "wb") as f:
            f.write(image_data)

        if args.json:
            print(
                json.dumps(
                    {"status": "success", "path": output_path}, ensure_ascii=False
                )
            )
        else:
            print(_green(f"✓ Screenshot saved to {output_path}"))
        return 0
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print(_red(f"Failed to save screenshot: {e}"))
        return 1


def cmd_reset(args: argparse.Namespace, client_state: ClientState) -> int:
    bridge_too = getattr(args, "bridge_reset", False)
    admin_token = _cli_token(args, client_state, args.bridge) if bridge_too else None

    msg_parts = ["Client state cleared"]

    if bridge_too:
        session = (
            BridgeClient(args.bridge, token=admin_token)
            if admin_token
            else BridgeClient(args.bridge)
        )
        if session.reset_bridge():
            msg_parts.append("bridge state reset")
        else:
            msg = "failed to reset bridge (is it running?)"
            _output(args, _red(f"✗ {msg}"), {"success": False, "error": msg})
            return 1

    client_state.clear(args.bridge)
    client_state.save()
    msg = "; ".join(msg_parts) + "."
    _output(args, _green(f"✓ {msg}"), {"success": True, "message": msg})
    return 0


def cmd_resync(args: argparse.Namespace, client_state: ClientState) -> int:
    """Re-push the active choice menu to the bridge.

    Recovers from desync where the bridge lost the pending request
    but the game still has an active choice screen.
    """
    session = _make_session(args, client_state)
    success, msg, result = _send_and_wait_command_result(
        session, "resync", None, timeout=3.0,
    )
    _save_session(session, args, client_state)
    if not success:
        _output(args, _red(f"✗ Resync failed: {msg}"), {"success": False, "error": msg})
        return 1
    if not result:
        error = "Resync was submitted but not confirmed"
        _output(args, _red(f"✗ {error}"),
                {"success": False, "confirmed": False, "error": error})
        return 1
    if not result.get("success", True):
        error = result.get("error", "Resync failed")
        _output(args, _red(f"✗ Resync failed: {error}"),
                {"success": False, "error": error, "result": result})
        return 1
    detail = result.get("message") or msg
    _output(args, _green(f"✓ Resync succeeded. {detail}".rstrip()),
            {"success": True, "message": detail, "result": result})
    return 0


def cmd_inspect(args: argparse.Namespace, client_state: ClientState) -> int:
    """Inspect the game's UI state: screens, clickable regions, viewports."""
    session = _make_session(args, client_state)

    # A fresh CLI process may still observe earlier inspect receipts. Use the
    # shared nonce-matched path rather than accepting any result with this name.
    result_data = session.command("inspect", _deadline=time.time() + 5.0)

    _save_session(session, args, client_state)

    if result_data is None:
        _output(
            args,
            _red("✗ Timed out waiting for inspect result."),
            {"success": False, "error": "timeout"},
        )
        return 1

    if not result_data.get("success"):
        err = result_data.get("error", "unknown")
        _output(args, _red(f"✗ {err}"), {"success": False, "error": err})
        return 1

    if args.json:
        _output(args, data=result_data)
    else:
        colour = True
        verify = getattr(args, "verify", False)
        focus_only = getattr(args, "focus", False)
        text = _format_inspect_result(
            result_data, colour=colour, verify=verify, focus_only=focus_only
        )
        print(text)
    return 0


def cmd_progress(args: argparse.Namespace, client_state: ClientState) -> int:
    """Query game progress — story beats, choices, phase, endings."""
    session = _make_session(args, client_state)

    success, msg = session._send_command("progress")
    if not success:
        _output(args, _red(f"✗ {msg}"), {"success": False, "error": msg})
        return 1

    deadline = time.time() + 5.0
    result_data = None
    while time.time() < deadline:
        events = session.poll(timeout=1.0)
        for e in events:
            if e.get("type") == "command_result" and e.get("command") == "progress":
                result_data = e
                break
        if result_data is not None:
            break

    _save_session(session, args, client_state)

    if result_data is None:
        _output(
            args,
            _red("✗ Timed out. Progress tracking may not be installed for this game."),
            {"success": False, "error": "timeout"},
        )
        return 1

    if args.json:
        _output(args, data=result_data)
    else:
        # Pretty-print the progress report.
        interpreted = result_data.get("interpreted", result_data)
        lines = []
        phase = interpreted.get("phase", "")
        if phase:
            lines.append(f"Phase: {_cyan(phase)}")

        nodes = interpreted.get("nodes") or {}
        active_nodes = nodes.get("active") or []
        completed_nodes = nodes.get("completed") or []
        terminal_nodes = nodes.get("terminal") or []
        if active_nodes:
            lines.append("Active:")
            for node in active_nodes[:12]:
                lines.append(f"  -> {node}")
        if completed_nodes:
            sample = ", ".join(completed_nodes[:12])
            suffix = " ..." if len(completed_nodes) > 12 else ""
            lines.append(f"Completed: {sample}{suffix}")
        if terminal_nodes:
            sample = ", ".join(terminal_nodes[:12])
            suffix = " ..." if len(terminal_nodes) > 12 else ""
            lines.append(f"Terminal nodes: {sample}{suffix}")

        threads = interpreted.get("threads") or {}
        if threads:
            lines.append("Threads:")
            for name in sorted(threads.keys())[:12]:
                info = threads.get(name) or {}
                active = ", ".join(info.get("active") or [])
                completed = ", ".join(info.get("completed") or [])
                parts = []
                if active:
                    parts.append("active: " + active)
                if completed:
                    parts.append("completed: " + completed)
                if info.get("terminal"):
                    parts.append("terminal")
                lines.append(f"  {name}: {'; '.join(parts) if parts else 'idle'}")

        choices = interpreted.get("choices", {})
        if choices:
            lines.append("Choices:")
            for k, v in choices.items():
                lines.append(f"  {k}: {v}")

        stats = interpreted.get("stats", {})
        if stats:
            stat_parts = [f"{k}={v}" for k, v in stats.items() if not k.startswith("_")]
            if stat_parts:
                lines.append(f"Stats: {', '.join(stat_parts)}")

        path = interpreted.get("path", [])
        if path:
            lines.append(f"Path: {' → '.join(path)}")

        available_next = interpreted.get("available_next", [])
        if available_next:
            lines.append("Available next:")
            for n in available_next:
                if isinstance(n, dict):
                    label = n.get("label") or n.get("node") or "?"
                    thread = n.get("thread")
                    prefix = f"[{thread}] " if thread else ""
                    lines.append(f"  -> {prefix}{label}")
                    continue
                lines.append(f"  → {n}")

        completion = interpreted.get("completion", 0)
        if completion > 0:
            pct = int(completion * 100)
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            lines.append(f"Completion: [{bar}] {pct}%")

        ending = interpreted.get("ending")
        if ending:
            lines.append(f"Ending: {_green(ending)}")

        terminal = interpreted.get("game_terminal", interpreted.get("terminal", False))
        if terminal:
            lines.append(_yellow("Game has ended."))

        # Graph visualization (if --graph flag).
        graph = result_data.get("graph", {})
        graph_nodes = graph.get("nodes") or {}
        if getattr(args, "graph", False) and graph_nodes.get("visited"):
            lines.append("")
            lines.append(_bold("Progress Graph:"))
            visited = set(graph_nodes.get("visited", []))
            # Render as simple tree from result data.
            for node_info in graph.get("available_next", []):
                name = node_info.get("node", "?")
                label = node_info.get("label", name)
                marker = _green("✓") if node_info.get("visited") else _dim("○")
                lines.append(f"  {marker} {label}")
            if visited:
                lines.append(f"  Visited: {', '.join(sorted(visited))}")

        print("\n".join(lines) if lines else "(no progress data)")
    return 0


# Subcommands that must not receive the common override copies: they
# define their own --bridge/--token/--slot with distinct semantics.
_NO_COMMON_OVERRIDE_SUBCOMMANDS = {"mcp", "bridge"}

# (sub_dest, global_dest) pairs for post-subcommand copies of common
# global flags.  Distinct dests are load-bearing (the 3c17347 pattern):
# a subparser argument sharing the global dest would apply its default
# and clobber the value the main parser already parsed
# ("vnflight.py --slot 1 state" must keep working).
_COMMON_OVERRIDE_DESTS = (
    ("sub_target_slot", "target_slot"),
    ("sub_bridge", "bridge"),
    ("sub_token", "token"),
    ("sub_quiet", "quiet"),
    ("sub_json", "json"),
)


def _add_common_override_args(
    sub_parser: argparse.ArgumentParser,
    *,
    bridge: bool = True,
) -> None:
    """Accept the common global flags AFTER the subcommand too.

    `vnflight.py state --slot 1` was an argparse error ("unrecognized
    arguments") because --slot & friends were global-only — an
    ergonomics trap since flags usually work in either position.
    """
    sub_parser.add_argument(
        "--slot",
        dest="sub_target_slot",
        default=None,
        help="Target a specific game slot (same as the global --slot)",
    )
    if bridge:
        sub_parser.add_argument(
            "--bridge",
            dest="sub_bridge",
            default=None,
            help="Bridge server URL (same as the global --bridge)",
        )
    sub_parser.add_argument(
        "--token",
        dest="sub_token",
        default=None,
        help="Slot/bridge admin token (same as the global --token)",
    )
    sub_parser.add_argument(
        "--quiet",
        dest="sub_quiet",
        action="store_true",
        default=None,
        help="Suppress confirmation headers (same as the global --quiet)",
    )
    sub_parser.add_argument(
        "--json",
        dest="sub_json",
        action="store_true",
        default=None,
        help="Output structured JSON (same as the global --json)",
    )


def _merge_subcommand_override_flags(args: argparse.Namespace) -> None:
    """Fold post-subcommand flag copies into the global dests."""
    for sub_dest, dest in _COMMON_OVERRIDE_DESTS:
        value = getattr(args, sub_dest, None)
        if value is not None:
            setattr(args, dest, value)


# Top-level help groups, mirroring docs/CLI.md.  Every registered verb must
# appear in exactly one group (tests/test_cli.py pins that); a verb left
# out lands in an "Ungrouped" section so it can never vanish from --help.
COMMAND_GROUPS: list = [
    ("Setup", ["games", "info", "install-shim", "fetch-mods", "launch",
               "stop", "prompt", "mcp", "bridge"]),
    ("Playing", ["wait", "act", "input", "state", "choices", "save", "load"]),
    ("Navigation and extras", ["back", "back_all", "advance", "rewind",
                               "replay", "autoplay", "cmd", "set", "history",
                               "screenshot", "progress"]),
    ("Diagnostics", ["slots", "poll", "reset", "resync", "inspect",
                     "save-scan"]),
]


def _format_command_groups(sub: argparse._SubParsersAction) -> str:
    """Render the registered verbs under COMMAND_GROUPS, argparse-aligned."""
    import textwrap

    helps = {action.dest: (action.help or "") for action in sub._choices_actions}
    grouped = {verb for _title, verbs in COMMAND_GROUPS for verb in verbs}
    groups = list(COMMAND_GROUPS)
    leftovers = [verb for verb in sub.choices if verb not in grouped]
    if leftovers:
        groups.append(("Ungrouped", leftovers))
    lines = ["commands:"]
    for title, verbs in groups:
        lines.append(f"  {title}:")
        for verb in verbs:
            if verb not in sub.choices:
                continue
            text = helps.get(verb, "")
            head = f"    {verb:<18}"
            if len(head) > 24:
                lines.append(head.rstrip())
                head = " " * 24
            lines.append(textwrap.fill(
                text, width=79, initial_indent=head,
                subsequent_indent=" " * 24) if text else head.rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vnflight.py",
        usage="%(prog)s [global options] <command> ...",
        description="CLI tool for LLM agents to play Ren'Py visual novels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    connection = parser.add_argument_group("Connection")
    output = parser.add_argument_group("Output")
    behaviour = parser.add_argument_group("Behaviour")

    connection.add_argument(
        "--bridge",
        default=DEFAULT_BRIDGE_URL,
        help=f"Bridge server URL (default: {DEFAULT_BRIDGE_URL})",
    )
    connection.add_argument(
        "--slot",
        default=None,
        dest="target_slot",
        help=(
            "Target a specific game slot (slot ID or game_id). Use "
            "latest:<game_id> to pick the newest live slot for a game."
        ),
    )
    connection.add_argument(
        "--token",
        default=None,
        help=(
            "Slot or bridge admin token for reserved slots / require-token "
            "bridges (falls back to the stored admin token, then "
            "VNFLIGHT_TOKEN)"
        ),
    )
    connection.add_argument(
        "--games-dir", help="Root directory containing games (default: auto-detect)"
    )

    output.add_argument(
        "--json",
        action="store_true",
        help=(
            "Output structured JSON instead of human-readable text for the "
            "play and query verbs; setup verbs (install-shim, fetch-mods) "
            "always print text"
        ),
    )
    output.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress confirmation headers and titles for a more serene experience",
    )
    output.add_argument(
        "--no-state", action="store_true", help="Don't persist cursor to disk"
    )

    behaviour.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Automatically confirm all prompts (non-interactive mode)",
    )
    behaviour.add_argument(
        "--version", action="version", version=f"vnflight {VERSION}")
    behaviour.add_argument(
        "--trace-wait",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # The verb list is rendered grouped in the epilog (see COMMAND_GROUPS);
    # argparse's own flat listing is suppressed.  An unknown verb still gets
    # the standard "invalid choice ... (choose from ...)" error.
    sub = parser.add_subparsers(
        dest="command", metavar="<command>", help=argparse.SUPPRESS)

    # -- Game management --

    sub.add_parser("games", help="List available games")
    p_slots = sub.add_parser("slots", help="List active game slots on the bridge")
    p_slots.add_argument(
        "--reap-stale",
        action="store_true",
        help="Free ended slots and slots whose recorded game PID is no longer alive",
    )

    p_info = sub.add_parser("info", help="Show the spoiler-free briefing for a game")
    p_info.add_argument("game", help="Game ID (directory name)")

    p_launch = sub.add_parser("launch", help="Start the bridge server + Ren'Py game")
    p_launch.add_argument(
        "game", help="Game ID (from vnflight.json or directory name)"
    )
    p_launch.add_argument(
        "--fast-forward",
        action="store_true",
        help="Enable fast-forward mode (instant text, no delays)",
    )
    p_launch.add_argument(
        "--auto",
        action="store_true",
        help="Enable auto-advance mode (Ren'Py Auto-Forward)",
    )
    p_launch.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the game to reach a choice after launching",
    )
    p_launch.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Wait timeout (default: none)",
    )
    p_launch.add_argument(
        "--save-slot",
        default=None,
        help="Save slot name for isolated saves (e.g. 'agent_alice')",
    )
    p_launch.add_argument(
        "--debug",
        dest="shim_debug",
        action="store_true",
        default=None,
        help="Turn on the shim's command/action log for this launch "
             "(overrides the game's 'debug' config; files go to its "
             "'debug_logs' directory, one per session)",
    )
    p_launch.add_argument(
        "--no-debug",
        dest="shim_debug",
        action="store_false",
        help="Turn the shim's debug log off for this launch",
    )
    p_launch.add_argument(
        "--reservation-token",
        default=None,
        help=argparse.SUPPRESS,
    )
    p_launch.add_argument(
        "--defer-default-profile",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    p_stop = sub.add_parser("stop", help="Stop a game or the whole bridge")
    p_stop.add_argument(
        "game",
        nargs="?",
        default=None,
        help="Game ID or slot to stop (omit to stop everything)",
    )

    p_fetch = sub.add_parser(
        "fetch-mods",
        help=(
            "Download a hash-verified adapter snapshot (with no URL and no "
            "--sha256: the one pinned under mods_snapshot in vnflight.json)"
        ),
    )
    p_fetch.add_argument(
        "url", nargs="?", default=None,
        help="HTTPS manifest URL from a trusted repository (default: the pinned snapshot)",
    )
    p_fetch.add_argument(
        "--sha256", default=None,
        help="Trusted manifest SHA-256 digest (required with a URL)",
    )
    p_fetch.add_argument("--output", required=True, help="New snapshot directory (must not exist)")

    p_install = sub.add_parser(
        "install-shim", help="Install the vnflight.rpy shim into a game"
    )
    p_install.add_argument(
        "game", help="Game ID (from vnflight.json or directory name/path)"
    )
    p_install.add_argument(
        "--always-on",
        action="store_true",
        help="Patch the shim to be always enabled (for GOG/platforms without env-var support)",
    )
    p_install.add_argument(
        "--no-mods",
        action="store_true",
        help="Install only the core shim, skip game-specific mods",
    )
    p_install.add_argument(
        "--create-game-dir",
        action="store_true",
        help=(
            "Create the game/ directory if the target has none. Without "
            "this flag a directory without game/ is refused, since it "
            "usually means the path is wrong."
        ),
    )

    p_prompt = sub.add_parser("prompt", help="Print a system prompt for an LLM agent")
    p_prompt.add_argument("game", help="Game ID (directory name)")

    # -- Gameplay --

    p_wait = sub.add_parser(
        "wait", help="Watch the story; stop when a choice is needed"
    )
    p_wait.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Event-wait budget in seconds; final screen settling may add time (default: none)",
    )
    p_wait.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include scene/show/hide and metadata events",
    )
    p_wait.add_argument(
        "--diag",
        action="store_true",
        help="With --json, also include raw current-window events as diag_events",
    )

    p_autoplay = sub.add_parser(
        "autoplay", help="Enable auto-advance and watch the story unfold"
    )
    p_autoplay.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Event-wait budget in seconds; final screen settling may add time (default: none)",
    )

    p_poll = sub.add_parser(
        "poll",
        help="Diagnostic raw bridge event poll since last cursor",
        description=(
            "Poll raw bridge events since the CLI cursor. This is a "
            "diagnostic surface; use 'wait' for agent-facing play output."
        ),
    )
    p_poll.add_argument(
        "--wait",
        dest="wait_timeout",
        type=float,
        default=0,
        help="Block up to N seconds for new events",
    )
    p_poll.add_argument(
        "--all",
        action="store_true",
        help="Show all raw events (ignore cursor)",
    )
    p_poll.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include scene/show/hide and metadata events",
    )

    p_act = sub.add_parser(
        "act",
        help="Pick a visible choice or screen button by number or label",
    )
    p_act.add_argument("target", help="Choice/button number, label, or choice ID")
    p_act.add_argument(
        "--wait",
        action="store_true",
        default=True,
        help="Wait for next interaction after acting (default: on)",
    )
    p_act.add_argument(
        "--no-wait",
        action="store_false",
        dest="wait",
        help="Don't wait after acting",
    )
    p_act.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    sub.add_parser("choices", help="Show current choices")

    p_input = sub.add_parser("input", help="Provide text input")
    p_input.add_argument("text", nargs="+", help="Text to submit")
    p_input.add_argument(
        "--wait", action="store_true", help="Wait for next interaction"
    )
    p_input.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_back = sub.add_parser("back", help="Close the current menu screen (Escape)")
    p_back.add_argument("--wait", action="store_true", help="Wait for next interaction")
    p_back.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_back_all = sub.add_parser(
        "back_all",
        help=(
            "Close overlay/menu screens until the story screen is back "
            "(bounded; refuses when nothing is open)"
        ),
    )
    p_back_all.add_argument(
        "--wait", action="store_true", help="Wait for next interaction"
    )
    p_back_all.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_advance = sub.add_parser(
        "advance",
        help="Advance one dialogue interaction without auto-forward",
    )
    p_advance.add_argument("--wait", action="store_true", help="Wait for next interaction")
    p_advance.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_rewind = sub.add_parser(
        "rewind",
        help="Move one dialogue/checkpoint backward",
    )
    p_rewind.add_argument("--wait", action="store_true", help="Wait for next interaction")
    p_rewind.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_replay = sub.add_parser(
        "replay",
        help="Roll forward after rewind",
    )
    p_replay.add_argument("--wait", action="store_true", help="Wait for next interaction")
    p_replay.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_cmd = sub.add_parser(
        "cmd", help="Send a game command (next, auto_advance_on, etc.)"
    )
    p_cmd.add_argument(
        "command_name",
        metavar="name",
        help="Command name (next, rollback, auto_advance_on/off, skip_toggle, save, load, start, quit)",
    )
    p_cmd.add_argument(
        "command_args",
        nargs="*",
        metavar="key=value",
        help="Command arguments as key=value pairs",
    )
    p_cmd.add_argument("--wait", action="store_true", help="Wait for next interaction")
    p_cmd.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_set = sub.add_parser(
        "set", help="Get or set a runtime config value (e.g. auto_skip_single_choice)"
    )
    p_set.add_argument("key", nargs="?", default=None, help="Config key name")
    p_set.add_argument(
        "value", nargs="?", default=None, help="New value (omit to query current)"
    )
    p_set.add_argument(
        "--profile",
        "-p",
        default=None,
        help="Apply a named profile from vnflight.json",
    )

    p_state = sub.add_parser("state", help="Show current game state")
    p_state.add_argument(
        "--no-stats", action="store_true", help="Hide inventory and stats from output"
    )
    p_state.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show full detail (actions, screens, game state header)",
    )

    p_save = sub.add_parser("save", help="Save the game to a save slot")
    p_save.add_argument(
        "slot", nargs="?", default=None,
        help="Save slot, e.g. 1-1 (default: 1-1); not the bridge --slot",
    )
    p_save.add_argument("--name", help="Optional name for the save")

    p_save_scan = sub.add_parser(
        "save-scan",
        help="Read-only scan for saves that may reference vnflight shim code",
    )
    p_save_scan.add_argument("path", help="Ren'Py .save file or directory to scan")
    p_save_scan.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="Recurse into subdirectories when scanning a directory",
    )

    p_load = sub.add_parser("load", help="Load the game from a save slot")
    p_load.add_argument(
        "slot", nargs="?", default=None,
        help="Save slot, e.g. 1-1 (default: the newest save); not the bridge --slot",
    )
    p_load.add_argument("--wait", action="store_true", help="Wait for next interaction")
    p_load.add_argument(
        "--timeout", type=float, default=None, help="Wait timeout (default: none)"
    )

    p_history = sub.add_parser("history", help="Show recent transcript")
    p_history.add_argument("--last", type=int, default=None, help="Show last N events")
    p_history.add_argument(
        "--first", type=int, default=None, help="Show first N events"
    )
    p_history.add_argument("--all", action="store_true", help="Show all events")
    p_history.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include scene/show/hide and metadata events",
    )
    p_history.add_argument(
        "--diag",
        action="store_true",
        help="With --json, also include raw transcript events as diag_events",
    )

    p_screenshot = sub.add_parser("screenshot", help="Capture a screenshot of the game")
    p_screenshot.add_argument(
        "--output",
        "-o",
        default="screenshot.png",
        help="Output filename (default: screenshot.png)",
    )

    p_reset = sub.add_parser("reset", help="Reset client state")
    p_reset.add_argument(
        "--bridge",
        dest="bridge_reset",
        action="store_true",
        help="Also reset the bridge server state",
    )

    sub.add_parser(
        "resync", help="Re-push active choice menu to the bridge (desync recovery)"
    )

    p_inspect = sub.add_parser(
        "inspect", help="Inspect game UI state (screens, focus, viewports)"
    )
    p_inspect.add_argument(
        "--focus", action="store_true", help="Show only the focus/clickable list"
    )
    p_inspect.add_argument(
        "--verify",
        action="store_true",
        help="Cross-reference scraped buttons vs focus list",
    )

    p_progress = sub.add_parser(
        "progress", help="Query game progress (story beats, choices, endings)"
    )
    p_progress.add_argument(
        "--graph",
        action="store_true",
        help="Show progress graph with visited/unvisited nodes",
    )

    p_mcp = sub.add_parser(
        "mcp", help="Start an MCP server for AI agents (Claude Desktop, Cursor, etc.)"
    )
    # Distinct dests from the global --bridge/--token: argparse subparser
    # defaults would otherwise clobber a value parsed by the main parser
    # (``vnflight.py --bridge URL mcp`` must keep working).
    p_mcp.add_argument(
        "--bridge",
        dest="mcp_bridge",
        default=None,
        metavar="URL",
        help=(
            "Bridge server URL to connect this MCP session to "
            f"(default: the global --bridge, {DEFAULT_BRIDGE_URL})"
        ),
    )
    p_mcp.add_argument(
        "--token",
        dest="mcp_token",
        default=None,
        metavar="TOKEN",
        help=(
            "Bridge admin/slot token for token-gated bridges "
            "(falls back to the VNFLIGHT_TOKEN environment variable)"
        ),
    )
    p_mcp.add_argument("--game", default=None, help="Game ID or slot to connect to")
    p_mcp.add_argument(
        "--slot",
        default=None,
        help="Specific bridge slot ID or game_id to bind this MCP session to",
    )
    p_mcp.add_argument(
        "--capabilities",
        default=None,
        help=(
            "Comma-separated MCP tool capabilities: play, lifecycle, "
            "diagnostic, admin, or all. Default: play"
        ),
    )
    p_mcp.add_argument(
        "--tools",
        default=None,
        help=(
            "Comma-separated exact MCP tool allowlist. Tools must also be "
            "enabled by --capabilities."
        ),
    )
    p_mcp.add_argument(
        "--debug",
        action="store_true",
        help="Enable diagnostic/admin MCP tools (legacy alias)",
    )

    p_bridge = sub.add_parser(
        "bridge", help="Start the bridge server (standalone, blocks until stopped)"
    )
    p_bridge.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    p_bridge.add_argument("--port", type=int, default=8385, help="Port to bind (default: 8385)")
    p_bridge.add_argument("--token", default=None, help="Admin token (generated if omitted)")
    p_bridge.add_argument(
        "--require-token",
        action="store_true",
        default=False,
        help="Require token for all write operations (default: open)",
    )

    # Accept the common global flags after the subcommand too.  `reset`
    # keeps its historical `--bridge` (= also reset the bridge server),
    # so it only gets the non-conflicting copies.
    for name, sub_parser in sub.choices.items():
        if name in _NO_COMMON_OVERRIDE_SUBCOMMANDS:
            continue
        _add_common_override_args(sub_parser, bridge=(name != "reset"))

    parser.epilog = _format_command_groups(sub)
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMAND_MAP = {
    "fetch-mods": cmd_fetch_mods,
    "games": cmd_games,
    "slots": cmd_slots,
    "info": cmd_info,
    "launch": cmd_launch,
    "install-shim": cmd_install_shim,
    "stop": cmd_stop,
    "prompt": cmd_prompt,
    "wait": cmd_wait,
    "autoplay": cmd_autoplay,
    "poll": cmd_poll,
    "act": cmd_act,
    "choices": cmd_choices,
    "input": cmd_input,
    "back": cmd_back,
    "back_all": cmd_back_all,
    "advance": cmd_advance,
    "rewind": cmd_rewind,
    "replay": cmd_replay,
    "cmd": cmd_cmd,
    "set": cmd_set,
    "state": cmd_state,
    "save": cmd_save,
    "save-scan": cmd_save_scan,
    "load": cmd_load,
    "history": cmd_history,
    "screenshot": cmd_screenshot,
    "reset": cmd_reset,
    "resync": cmd_resync,
    "inspect": cmd_inspect,
    "progress": cmd_progress,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _merge_subcommand_override_flags(args)

    if not args.command:
        parser.print_help()
        return 0

    # MCP server mode — starts a long-running server, bypasses normal flow.
    if args.command == "mcp":
        from .mcp import run_server
        # Subcommand-level --bridge wins over the global flag (which
        # always carries at least the default URL, so a plain
        # ``vnflight.py mcp`` keeps connecting to the shared bridge).
        bridge_url = getattr(args, "mcp_bridge", None) or (
            args.bridge if hasattr(args, "bridge") else DEFAULT_BRIDGE_URL
        )
        run_server(
            bridge_url=bridge_url,
            game=args.game,
            slot=getattr(args, "slot", None) or getattr(args, "target_slot", None),
            debug=getattr(args, "debug", False),
            token=getattr(args, "mcp_token", None) or getattr(args, "token", None),
            capabilities=getattr(args, "capabilities", None),
            tools=getattr(args, "tools", None),
        )
        return 0

    # Bridge server mode — starts the HTTP bridge, blocks until stopped.
    if args.command == "bridge":
        # Try package import first, fall back to __main__ (built single-file).
        _bridge_run = None
        try:
            from .bridge import run_bridge_server as _bridge_run
        except (ImportError, ModuleNotFoundError):
            pass
        if _bridge_run is None:
            import __main__ as _m
            _bridge_run = getattr(_m, "run_bridge_server", None)
        if _bridge_run is None:
            print("Error: bridge server module not found", file=sys.stderr)
            return 1
        _bridge_run(
            host=args.host,
            port=args.port,
            verbose=True,
            admin_token=args.token,
            require_token=args.require_token,
        )
        return 0

    # Runtime state lives in the per-user data dir (site-packages may be
    # read-only for pip installs).
    client_state = ClientState(default_state_dir(), disabled=args.no_state)

    handler = COMMAND_MAP.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args, client_state)
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(_red(f"Error: {exc}"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
