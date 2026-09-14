"""vnflight library -- reusable functions for game discovery, launch, and management.

This module contains non-CLI-specific functionality: configuration loading,
game discovery, process management, and the persistent client state class.
It has no dependency on .format (a higher layer).  The shared game-id
matching helpers live in .client (a lower layer in the build order); where
BridgeClient itself is needed it is imported locally inside the function
body to keep module import time down.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import os
import platform
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Single shared definitions — duplicating these here would fork behavior in
# the single-file build, where the later copy silently shadows the earlier
# one (build_vnflight.py now fails on duplicate top-level names).
from .client import (
    _game_ids_match,
    _normalized_game_id_collision,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8385"
DEFAULT_BRIDGE_PORT = 8385
STATE_FILENAME = ".vnflight_state.json"
CONFIG_FILENAME = "vnflight.json"
# How long launch_game waits for a freshly spawned bridge to answer. The
# 1.6 MB single-file artifact imports in 4-5 s idle and slower under load;
# the MCP launch handler reserves this plus its setup phase out of the
# transport budget so a slow start is never killed before its receipt.
BRIDGE_READINESS_TIMEOUT_S = 25.0
# Launch-file handshake: written into the game's game/ directory at launch
# so launcher-mediated games (steam://, goggalaxy://) — which do NOT inherit
# the vnflight launcher's environment — still learn the bridge URL, slot
# token, and save slot for THIS launch.  The shim reads it at init.
LAUNCH_FILE_NAME = "vnflight_launch.json"
LAUNCH_RECEIPT_PREFIX = "vnflight_registration_"
# Claim protocol (see ``_write_launch_file``).  A game needs several seconds
# to boot far enough to read the launch file; two launches of the SAME game
# started close together would otherwise both read whichever file won the
# race.  The shim stamps ``claimed_by`` once it has adopted a file, and the
# writer waits for that stamp before overwriting.
LAUNCH_CLAIM_WAIT = 45.0  # seconds to wait for the previous launch's claim
LAUNCH_CLAIM_POLL = 0.5  # seconds between re-reads while waiting
LAUNCH_CLAIM_FRESH_HORIZON = 120.0  # older/farther-future files never block
# Writer-vs-writer serialization (see ``_write_launch_file``).  The claim
# protocol above serializes a writer against a BOOTING GAME; these serialize
# two WRITERS — two threads of the (threaded) hub, or two processes.
LAUNCH_LOCK_SUFFIX = ".lock"  # sits next to the launch file
LAUNCH_LOCK_POLL = 0.1  # seconds between non-blocking OS-lock retries
# A contender may have arrived behind a writer doing the full claim wait.
# Keep acquisition bounded so an unexpected filesystem-lock failure cannot
# wedge launches forever; timeout degrades to the existing explicit warning.
LAUNCH_LOCK_ACQUIRE_WAIT = LAUNCH_CLAIM_WAIT + 20.0

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

# Keep references to log file handles so they aren't garbage-collected
# while the child process is still writing to them.
_launch_log_handles: List[Any] = []


def _coerce_expected_setting_value(old_value: Any, requested: Any) -> Any:
    """Mirror the shim's receipt-producing setting coercion."""
    if isinstance(old_value, bool):
        if isinstance(requested, bool):
            return requested
        return str(requested).lower() in {"true", "1", "yes"}
    if isinstance(old_value, int) and not isinstance(old_value, bool):
        return int(requested)
    if isinstance(old_value, float):
        return float(requested)
    return requested


def _validated_setting_application_receipt(
    applied: Any,
    expected_changes: Dict[str, Any],
) -> Tuple[Optional[List[dict]], Optional[str]]:
    """Validate a successful batch-set receipt before describing its result.

    The shim returns one complete row per submitted key. Missing, duplicate,
    partial, or malformed rows cannot prove that a profile was already in
    effect, even when the surrounding command result says ``success``.
    """
    expected_keys = set(expected_changes)
    if not isinstance(applied, list):
        return None, "the applied receipt is not a list"
    if len(applied) != len(expected_keys):
        return None, (
            "the applied receipt has {} row(s), expected {}".format(
                len(applied), len(expected_keys))
        )
    seen = set()
    for entry in applied:
        if not isinstance(entry, dict):
            return None, "the applied receipt contains a non-object row"
        if not {"key", "old_value", "value"} <= set(entry):
            return None, "the applied receipt contains an incomplete row"
        key = entry.get("key")
        if key not in expected_keys:
            return None, "the applied receipt contains an unexpected key"
        if key in seen:
            return None, "the applied receipt contains a duplicate key"
        try:
            expected_value = _coerce_expected_setting_value(
                entry.get("old_value"), expected_changes[key])
        except (TypeError, ValueError):
            return None, "the submitted value cannot match the live setting type"
        if entry.get("value") != expected_value:
            return None, "the applied receipt contains an unexpected value"
        seen.add(key)
    if seen != expected_keys:
        return None, "the applied receipt omits a submitted key"
    return applied, None


# ---------------------------------------------------------------------------
# HTTP helper (for admin endpoints not covered by BridgeClient)
# ---------------------------------------------------------------------------


def _http_request(
    method: str,
    url: str,
    data: Optional[dict] = None,
    timeout: float = 5.0,
) -> Tuple[int, Any]:
    """
    Perform an HTTP request.  Returns (status_code, parsed_json_or_None).
    """
    headers = {"Content-Type": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw}
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
            return exc.code, json.loads(err_body)
        except Exception:
            return exc.code, {"error": str(exc)}
    except urllib.error.URLError as exc:
        return 0, {"error": f"Connection failed: {exc.reason}"}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _slot_free_detail(data: Any) -> str:
    if isinstance(data, dict):
        detail = data.get("message") or data.get("error") or data.get("_raw")
        if detail:
            return str(detail)
    return "slot free request failed"


def _slot_free_already_gone(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    code = str(
        data.get("error_code")
        or data.get("code")
        or data.get("reason")
        or ""
    ).lower()
    if code in {"slot_not_found", "no_slot_to_free", "already_freed"}:
        return True
    text = str(data.get("message") or data.get("error") or data.get("_raw") or "")
    lowered = text.lower()
    return "no slot" in lowered and "to free" in lowered


def _expected_shim_game_id(
    game: Optional[dict], fallback_game_id: str,
) -> str:
    """Return the game_id the shim will self-report for this launch.

    The shim derives its own identity from its install directory's
    basename (see vnflight.rpy: ``os.path.basename(os.path.dirname(
    renpy.config.gamedir))``), NOT from the vnflight.json config key that
    was used to launch it.  Ordinarily those coincide -- the game_dir IS
    the config id.  But a config entry can explicitly alias a physical
    game directory shared with another entry -- e.g. testing the same
    game under a different Ren'Py SDK ("echoes_of_tomorrow_r7" with
    ``"game_dir": "echoes_of_tomorrow"``) -- in which case the shim
    reports "echoes_of_tomorrow" while the launch used the suffixed
    config id.  Slot-matching during the connect wait must compare
    against THIS derived id, or a slot that legitimately belongs to the
    launch never matches and the wait times out even though the game is
    running fine.

    Deliberately keyed off the config's own ``game_dir`` field rather
    than the resolved install path: a Steam/GOG install path's directory
    name need not match the game's vnflight.json id at all (registry
    lookups, launcher-owned folder names), and conflating "install path
    basename differs from the id" with "this is a shim-id alias" would
    misfire launch matching for any launcher-mediated game.
    """
    game_dir = (game or {}).get("game_dir")
    if game_dir:
        name = Path(str(game_dir)).name
        if name:
            return name.lower().replace(" ", "_")
    return fallback_game_id


def _same_game_slot_pids(slots: list[dict], game_id: str) -> set[int]:
    pids: set[int] = set()
    for slot in slots:
        if not _game_ids_match(slot.get("game_id"), game_id):
            continue
        pid = slot.get("game_pid")
        try:
            if pid:
                pids.add(int(pid))
        except (TypeError, ValueError):
            continue
    return pids


def _free_existing_game_slot(
    session: Any,
    game_id: str,
    existing_slots: list[dict] | None = None,
    client_state: Any = None,
    bridge_url: Optional[str] = None,
    match_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """Free existing same-game slots before replacement launch.

    *match_id*, when given, is the id actually compared against each slot's
    reported ``game_id`` (see ``_expected_shim_game_id``); *game_id* still
    names the launch in error/log text so messages stay in terms of what the
    caller typed.

    Returns (ok, error).  Failing closed is safer than launching into a stale
    slot and letting later commands operate on old pending state.
    """
    target_id = match_id if match_id is not None else game_id
    if existing_slots is None:
        existing_slots = session.list_slots()
    if existing_slots is None:
        return False, (
            "Could not list bridge slots before launching. "
            "Restart the bridge/MCP session before launching."
        )
    collisions = _normalized_game_id_collision(existing_slots, target_id)
    if collisions:
        return False, (
            f"Ambiguous normalized game id for '{game_id}': "
            f"{', '.join(sorted(collisions))}. "
            "Use a slot id or resolve the bridge slot collision before launching."
        )
    for slot in existing_slots:
        if not _game_ids_match(slot.get("game_id"), target_id):
            continue
        slot_id = slot.get("slot_id")
        ok, data = session.free_slot(slot_id)
        if not ok and _slot_free_already_gone(data):
            ok = True
        if not ok:
            detail = _slot_free_detail(data)
            detail_suffix = "" if detail.endswith((".", "!", "?")) else "."
            return False, (
                f"Existing slot {slot_id} for '{game_id}' could not be freed: "
                f"{detail}{detail_suffix} "
                "Restart the bridge/MCP session before launching."
            )
        if client_state is not None and bridge_url:
            remove_token = getattr(client_state, "remove_slot_token", None)
            if callable(remove_token):
                try:
                    remove_token(
                        bridge_url, slot_id,
                        reservation_id=slot.get("reservation_id"),
                    )
                except TypeError:
                    remove_token(bridge_url, slot_id)
                client_state.save()
    return True, ""


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def user_data_dir() -> Path:
    """Per-user vnflight data directory (state, fallback logs).

    Runtime state must not live inside the package directory: for pip
    installs that is site-packages, which may be read-only and is wiped
    on reinstall.  Override with VNFLIGHT_DATA_DIR.
    """
    env = os.environ.get("VNFLIGHT_DATA_DIR")
    if env:
        return Path(env)
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        base_path = Path(base) if base else Path.home() / "AppData" / "Local"
        return base_path / "vnflight"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "vnflight"
    base = os.environ.get("XDG_DATA_HOME")
    base_path = Path(base) if base else Path.home() / ".local" / "share"
    return base_path / "vnflight"


def _package_dir() -> Path:
    """Directory of this module — the legacy runtime-state location."""
    return Path(__file__).resolve().parent


def _single_file_artifact() -> Optional[Path]:
    """Path of the running single-file vnflight build, or None (package).

    In the package checkout this code runs as ``vnflight.lib``; in the
    built artifact every module body shares the artifact's globals, so
    ``__package__`` is empty and ``__file__`` is the artifact itself —
    a script that hosts the full CLI (subcommands, bridge, MCP).
    Deployment-aware subprocess spawns (CLI re-invocation, owned bridge)
    must use this file directly: there is no importable ``vnflight``
    package to ``-m`` into in a flat single-file deployment.
    """
    if (__package__ or "").startswith("vnflight"):
        return None
    try:
        candidate = Path(__file__).resolve()
    except (OSError, ValueError):
        return None
    if candidate.is_file():
        return candidate
    return None


def default_state_dir() -> str:
    """Directory for the persistent CLI state file.

    Prefers the per-user data dir; falls back to the historical
    package-adjacent location only if the data dir cannot be created.
    """
    data_dir = user_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return str(_package_dir())
    return str(data_dir)


class ClientState:
    """Persists cursor and session info across CLI invocations."""

    def __init__(
        self,
        state_dir: str,
        disabled: bool = False,
    ):
        self.disabled = disabled
        self.path = os.path.join(state_dir, STATE_FILENAME)
        self.data: dict = {}
        self._pending_mutations: List[Tuple[str, Tuple[str, ...], Any]] = []
        if not disabled:
            self._load()

    def _record_mutation(
        self, operation: str, path: Tuple[str, ...], value: Any = None,
    ) -> None:
        self._pending_mutations.append((operation, path, value))

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            return
        except json.JSONDecodeError:
            self.data = {}
            return
        except FileNotFoundError:
            self.data = {}

    def save(self) -> None:
        if self.disabled:
            return
        target = Path(self.path)
        lock_path = Path(self.path + ".lock")
        thread_lock = _launch_write_lock(target)
        lock_fd: Optional[int] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with thread_lock:
                lock_fd, _warning = _acquire_launch_lockfile(
                    lock_path, LAUNCH_LOCK_ACQUIRE_WAIT,
                )
                if lock_fd is None:
                    return
                persisted: dict = {}
                try:
                    with target.open("r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        persisted = loaded
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass

                if not self._pending_mutations:
                    merged = dict(self.data)
                else:
                    merged = persisted
                    for operation, path, value in self._pending_mutations:
                        if operation == "set_root":
                            merged = dict(value)
                            continue
                        parent = merged
                        for key in path[:-1]:
                            child = parent.get(key)
                            if not isinstance(child, dict):
                                child = {}
                                parent[key] = child
                            parent = child
                        key = path[-1]
                        if operation == "set":
                            parent[key] = value
                        elif operation == "delete":
                            parent.pop(key, None)
                        elif operation == "remove_slot_token":
                            tokens = parent.get(key)
                            if isinstance(tokens, dict):
                                slot_key = str(value.get("slot_key"))
                                expected_id = value.get("reservation_id")
                                candidate = tokens.get(slot_key)
                                if (
                                    candidate is not None
                                    and expected_id is not None
                                    and hashlib.sha256(
                                        str(candidate).encode("utf-8")
                                    ).hexdigest()[:16] != expected_id
                                ):
                                    continue
                                removed = tokens.pop(slot_key, None)
                                if removed:
                                    for alias, token in list(tokens.items()):
                                        if (
                                            not alias.startswith("slot:")
                                            and token == removed
                                        ):
                                            tokens.pop(alias, None)
                        elif operation == "append_unique":
                            values = parent.setdefault(key, [])
                            if not isinstance(values, list):
                                values = []
                                parent[key] = values
                            if value not in values:
                                values.append(value)
                tmp = target.with_name(
                    target.name
                    + ".tmp-%d-%d" % (os.getpid(), threading.get_ident())
                )
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(str(tmp), str(target))
                self.data = merged
                self._pending_mutations.clear()
        except Exception:
            pass
        finally:
            _release_launch_lockfile(lock_fd, lock_path)

    def get_cursor(self, bridge_url: str) -> int:
        return self.data.get(bridge_url, {}).get("cursor", 0)

    def has_cursor(self, bridge_url: str) -> bool:
        """Distinguish a saved continuation (including zero) from first attach."""
        return "cursor" in self.data.get(bridge_url, {})

    def set_cursor(self, bridge_url: str, cursor: int) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        self.data[bridge_url]["cursor"] = cursor
        self._record_mutation("set", (bridge_url, "cursor"), cursor)

    def get_last_request_id(self, bridge_url: str) -> Optional[str]:
        return self.data.get(bridge_url, {}).get("last_request_id")

    def get_deferred_events(self, bridge_url: str) -> list:
        """Story rows a previous invocation polled past but never showed."""
        events = self.data.get(bridge_url, {}).get("deferred_events")
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

    def set_deferred_events(self, bridge_url: str, events: list) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        events = [e for e in (events or []) if isinstance(e, dict)]
        self.data[bridge_url]["deferred_events"] = events
        self._record_mutation("set", (bridge_url, "deferred_events"), events)

    def get_delivered_action_events(self, bridge_url: str) -> dict:
        """The client's delivered-action-events ledger from the last
        invocation: ``{"reset_generation": int|None,
        "ownership": [[generation|None, action_id, seq], ...]}``.

        An act's scoped receipt drain delivers the act's story rows and
        records them here without advancing the ordinary cursor; the
        ordinary poll filters by this ledger.  Without it every ``wait``
        after an act re-read the story the act had already printed."""
        payload = self.data.get(bridge_url, {}).get("delivered_action_events")
        if not isinstance(payload, dict):
            return {"reset_generation": None, "ownership": []}
        ownership = payload.get("ownership")
        return {
            "reset_generation": payload.get("reset_generation"),
            "ownership": [
                list(item) for item in (ownership or [])
                if isinstance(item, (list, tuple)) and len(item) == 3
            ],
        }

    def set_delivered_action_events(self, bridge_url: str, payload: dict) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        payload = payload if isinstance(payload, dict) else {}
        clean = {
            "reset_generation": payload.get("reset_generation"),
            "ownership": [
                list(item) for item in (payload.get("ownership") or [])
                if isinstance(item, (list, tuple)) and len(item) == 3
            ],
        }
        self.data[bridge_url]["delivered_action_events"] = clean
        self._record_mutation(
            "set", (bridge_url, "delivered_action_events"), clean,
        )

    def set_last_request_id(self, bridge_url: str, rid: Optional[str]) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        self.data[bridge_url]["last_request_id"] = rid
        self._record_mutation(
            "set", (bridge_url, "last_request_id"), rid,
        )

    def get_pids(self, bridge_url: str) -> dict:
        return self.data.get(bridge_url, {}).get("pids", {})

    def set_pids(self, bridge_url: str, pids: dict) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        self.data[bridge_url]["pids"] = pids
        self._record_mutation("set", (bridge_url, "pids"), dict(pids))

    def get_admin_token(self, bridge_url: str) -> Optional[str]:
        token = self.data.get(bridge_url, {}).get("admin_token")
        return str(token) if token else None

    def set_admin_token(self, bridge_url: str, token: Optional[str]) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        if token:
            self.data[bridge_url]["admin_token"] = token
            self._record_mutation(
                "set", (bridge_url, "admin_token"), token,
            )
        else:
            self.data[bridge_url].pop("admin_token", None)
            self._record_mutation(
                "delete", (bridge_url, "admin_token"),
            )

    def get_slot_token(self, bridge_url: str, game_id: Optional[str] = None) -> Optional[str]:
        """Most useful stored slot token for ``bridge_url``.

        Slot tokens are stored per game_id plus a ``_last`` (most recent
        launch) entry. With no ``game_id``, returns the last-launch token.
        """
        tokens = self.data.get(bridge_url, {}).get("slot_tokens", {})
        token = tokens.get(game_id) if game_id else tokens.get("_last")
        return str(token) if token else None

    def get_slot_tokens(self, bridge_url: str) -> dict:
        return dict(self.data.get(bridge_url, {}).get("slot_tokens", {}))

    def set_slot_token(
        self,
        bridge_url: str,
        game_id: str,
        token: Optional[str],
        slot_id: Optional[int] = None,
    ) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        tokens = self.data[bridge_url].setdefault("slot_tokens", {})
        if token:
            tokens[game_id] = token
            tokens["_last"] = token
            self._record_mutation(
                "set", (bridge_url, "slot_tokens", game_id), token,
            )
            self._record_mutation(
                "set", (bridge_url, "slot_tokens", "_last"), token,
            )
            if slot_id is not None:
                slot_key = "slot:%s" % slot_id
                tokens[slot_key] = token
                self._record_mutation(
                    "set", (bridge_url, "slot_tokens", slot_key), token,
                )
        else:
            tokens.pop(game_id, None)
            self._record_mutation(
                "delete", (bridge_url, "slot_tokens", game_id),
            )

    def remove_slot_token(
        self,
        bridge_url: str,
        slot_id: int | str,
        reservation_id: Optional[str] = None,
    ) -> None:
        tokens = self.data.get(bridge_url, {}).get("slot_tokens", {})
        slot_key = "slot:%s" % slot_id
        removed = tokens.pop(slot_key, None)
        if removed:
            for key, value in list(tokens.items()):
                if not key.startswith("slot:") and value == removed:
                    tokens.pop(key, None)
        self._record_mutation(
            "remove_slot_token",
            (bridge_url, "slot_tokens"),
            {"slot_key": slot_key, "reservation_id": reservation_id},
        )

    def clear_slot_tokens(self, bridge_url: str) -> None:
        if bridge_url in self.data:
            self.data[bridge_url].pop("slot_tokens", None)
        self._record_mutation(
            "delete", (bridge_url, "slot_tokens"),
        )

    def get_launch_files(self, bridge_url: str) -> List[str]:
        return list(self.data.get(bridge_url, {}).get("launch_files", []))

    def add_launch_file(self, bridge_url: str, path: str) -> None:
        if bridge_url not in self.data:
            self.data[bridge_url] = {}
        files = self.data[bridge_url].setdefault("launch_files", [])
        if path not in files:
            files.append(path)
            self._record_mutation(
                "append_unique", (bridge_url, "launch_files"), path,
            )

    def clear_launch_files(self, bridge_url: str) -> None:
        if bridge_url in self.data:
            self.data[bridge_url].pop("launch_files", None)
        self._record_mutation(
            "delete", (bridge_url, "launch_files"),
        )

    def clear(self, bridge_url: Optional[str] = None) -> None:
        if bridge_url:
            self.data.pop(bridge_url, None)
            self._record_mutation("delete", (bridge_url,))
        else:
            self.data.clear()
            self._pending_mutations = [("set_root", tuple(), {})]


def resolve_stored_admin_token(bridge_url: Optional[str]) -> Optional[str]:
    """Admin token this machine persisted for ``bridge_url``, if any.

    Mirrors the CLI's ``_stored_admin_token`` (cli.py): a game launch stores
    the bridge admin token in :class:`ClientState` (keyed by bridge URL) so
    later CLI invocations — and the CLI subprocesses the MCP server spawns —
    can authenticate to a require-token bridge.  The long-lived MCP
    ``BridgeClient`` used for tool calls must resolve it the same way,
    otherwise its own ``launch`` succeeds (the subprocess reads the stored
    token) but its subsequent ``act``/``state``/``screenshot`` calls stay
    tokenless and 403.  The ``VNFLIGHT_TOKEN`` env fallback is left to the
    ``BridgeClient`` constructor.
    """
    if not bridge_url:
        return None
    try:
        state = ClientState(default_state_dir())
        return state.get_admin_token(bridge_url)
    except Exception:
        return None


def resolve_stored_slot_tokens(bridge_url: Optional[str]) -> List[str]:
    """Stored slot tokens for ``bridge_url``, most-recent launch first.

    A launch mints a per-slot token, hands it to the game (env + launch
    file) and persists it here. The bridge accepts it for that slot even
    when the stored ADMIN token is stale (open-mode bridge, or a bridge
    started by the harness with an admin token this machine never knew) —
    which is exactly the case where a client holding only the admin token
    403s on the slot it just launched.
    """
    if not bridge_url:
        return []
    try:
        state = ClientState(default_state_dir())
        tokens = state.get_slot_tokens(bridge_url)
        last = tokens.pop("_last", None)
        ordered = [last] if last else []
        ordered.extend(t for t in tokens.values() if t and t != last)
        return ordered
    except Exception:
        return []


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------


def _get_file_hash(path: Path) -> str:
    """Return SHA-256 hash of a file."""
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Game path resolution
# ---------------------------------------------------------------------------


def _find_steam_game_path(app_id: str) -> Optional[Path]:
    """Find installation path for a Steam game by App ID."""
    if IS_WINDOWS and winreg:
        # Check 64-bit and 32-bit registry locations
        paths = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App {app_id}",
            rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App {app_id}",
        ]
        for p in paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p) as key:
                    val, _ = winreg.QueryValueEx(key, "InstallLocation")
                    if val:
                        return Path(val)
            except Exception:
                continue
    elif platform.system() in ("Linux", "Darwin"):
        # Search for Steam installation
        if platform.system() == "Darwin":
            steam_roots = [Path.home() / "Library" / "Application Support" / "Steam"]
        else:
            steam_roots = [
                Path.home() / ".steam" / "steam",
                Path.home() / ".local" / "share" / "Steam",
                Path.home()
                / ".var"
                / "app"
                / "com.valvesoftware.Steam"
                / ".steam"
                / "steam",  # Flatpak
            ]

        for root in steam_roots:
            if not (root / "steamapps").is_dir():
                continue

            # 1. Check libraries defined in libraryfolders.vdf
            vdf = root / "steamapps" / "libraryfolders.vdf"
            lib_paths = [root]
            if vdf.exists():
                try:
                    content = vdf.read_text(encoding="utf-8")
                    # Extract "path" values using regex
                    lib_paths.extend(
                        [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', content)]
                    )
                except Exception:
                    pass

            # 2. Look for the manifest in each library
            for lib in lib_paths:
                manifest = lib / "steamapps" / f"appmanifest_{app_id}.acf"
                if manifest.exists():
                    try:
                        m_content = manifest.read_text(encoding="utf-8")
                        m = re.search(r'"installdir"\s+"([^"]+)"', m_content)
                        if m:
                            return lib / "steamapps" / "common" / m.group(1)
                    except Exception:
                        pass
    return None


def _find_gog_game_path(game_id: str) -> Optional[Path]:
    """Find installation path for a GOG game by Game ID."""
    if not IS_WINDOWS or not winreg:
        return None
    paths = [
        rf"SOFTWARE\GOG.com\Games\{game_id}",
        rf"SOFTWARE\WOW6432Node\GOG.com\Games\{game_id}",
    ]
    for p in paths:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, p) as key:
                val, _ = winreg.QueryValueEx(key, "path")
                if val:
                    return Path(val)
        except Exception:
            continue
    return None


def _find_game_install_path(game_id: str, games_dir: Optional[str]) -> Optional[Path]:
    """Find installation directory for a game."""
    games = discover_games(games_dir)
    game = next((g for g in games if g["id"] == game_id), None)

    if game:
        launch_cmd = game.get("launch_cmd", "")
        # Check for Steam URL
        steam_match = re.search(r"steam://rungameid/(\d+)", launch_cmd)
        if steam_match:
            return _find_steam_game_path(steam_match.group(1))

        # Check for GOG URL
        gog_match = re.search(r"goggalaxy://launchGame/(\d+)", launch_cmd)
        if gog_match:
            return _find_gog_game_path(gog_match.group(1))

    if not game:
        # Not configured: the id may be a game directory itself.
        if Path(game_id).is_dir():
            return Path(game_id).resolve()
        return None

    launch_cmd = game.get("launch_cmd", "")
    game_dir = game.get("game_dir")
    if game_dir:
        p = Path(game_dir)
        if p.exists():
            return p
        root = Path(games_dir) if games_dir else _find_project_root()
        p = root / game_dir
        if p.exists():
            return p
    if not launch_cmd:
        # Check if it's a local subdirectory in the project root
        root = Path(games_dir) if games_dir else _find_project_root()
        local_path = root / game_id
        if local_path.is_dir():
            return local_path
        return None

    # Fallback: try to parse the launch command.  A Ren'Py SDK launch is
    # "<sdk>/renpy.exe <project dir>": the project DIRECTORY is the install
    # path, never the executable's parent (that is the SDK, which has a
    # game/ dir of its own -- an absolute SDK path used to get the shim
    # installed there).  Only an executable-only launch (a packaged game)
    # resolves to the executable's parent.
    parts = _parse_launch_cmd(launch_cmd)
    if parts:
        root = Path(games_dir) if games_dir else _find_project_root()

        def _existing(part: str) -> Optional[Path]:
            # Config-relative only.  The template promises that launch
            # paths are relative to vnflight.json, and launch_game runs the
            # command with the config directory as its cwd, so that is the
            # one place a relative part can mean anything.  Trying the
            # shell's cwd first (as this once did) made `install-shim` run
            # from another checkout write the shim into THAT checkout's
            # game when both had a "../my_game".  An absolute part joins
            # through unchanged (pathlib discards the root on the left).
            p = root / part
            return p if p.exists() else None

        found = [(part, _existing(part)) for part in parts[:3]]
        for _part, p in found:
            if p is not None and p.is_dir():
                return p
        for _part, p in found:
            if p is not None and p.is_file():
                return p.parent

    return None


# ---------------------------------------------------------------------------
# Launch-file handshake
# ---------------------------------------------------------------------------


def _launch_file_game_dir(install_root: Optional[Path]) -> Optional[Path]:
    """Locate the game's ``game/`` directory for the launch file."""
    if not install_root:
        return None
    game_dir = install_root / "game"
    if game_dir.is_dir():
        return game_dir
    if platform.system() == "Darwin":
        # Mirror install-shim's .app bundle probing.
        bundles = (
            [install_root]
            if install_root.suffix == ".app"
            else list(install_root.glob("*.app"))
        )
        for app in bundles:
            for sub in (
                "Contents/Resources/autorun/game",
                "Contents/Resources/game",
            ):
                cand = app / sub
                if cand.is_dir():
                    return cand
    return None


#: The one line ``install-shim --always-on`` rewrites in the INSTALLED copy.
_ALWAYS_ON_FROM = 'self.enabled = os.environ.get("VNFLIGHT_ENABLED") == "1"'
_ALWAYS_ON_TO = "self.enabled = True  # patched by install-shim --always-on"


def _shim_fingerprint(path: Path) -> str:
    """SHA-256 of a shim's text with line endings normalised.

    Not ``_get_file_hash``: raw bytes are the wrong comparison here. The
    installer rewrites the file through ``Path.write_text`` for ``--always-on``
    (which re-terminates every line), and this repo checks out with CRLF
    translation — so a byte hash reports "drifted" for files that are identical
    code. What we care about is whether the game is running the same SOURCE.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _expected_fingerprints(source: Path, always_on: bool) -> set:
    """Fingerprints an installed file may legitimately have.

    ``--always-on`` games (see ``install_shim_flags`` in the config) get one
    line rewritten at install time, so their correct installed shim does NOT
    match the source byte-for-byte. Accept BOTH forms: the installer only
    patches when the pristine line is present, so an unpatched copy is equally
    valid.
    """
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    out = {hashlib.sha256(norm.encode("utf-8")).hexdigest()}
    if always_on and _ALWAYS_ON_FROM in norm:
        patched = norm.replace(_ALWAYS_ON_FROM, _ALWAYS_ON_TO, 1)
        out.add(hashlib.sha256(patched.encode("utf-8")).hexdigest())
    return out


def shim_status(game_id: str, games_dir: Optional[str] = None) -> dict:
    """Compare the shim + mods INSTALLED in a game against the repo copies.

    A game keeps its own copy of ``vnflight.rpy`` (plus per-game mods), so the
    repo can move on while a game silently keeps running an old one. That is not
    hypothetical: on 2026-08-01 Mystic Café was four days behind and every
    ``act(..., wait:true)`` ran to its timeout ceiling while the click still
    landed — the game was missing the very commit that hardened the interaction
    path the wait settles on. Two separate investigations were spent on the
    symptom because nothing compared these files.

    Resolution deliberately mirrors ``cmd_install_shim`` (same config lookup,
    same ``_get_file_hash``) so the check can never disagree with the installer
    about which files belong to a game.

    Returns a dict — never raises, a broken check must not block a launch::

        {"checked": bool,          # False => could not determine (see "reason")
         "ok": bool,               # True  => everything matches
         "reason": str | None,     # why the check could not run
         "stale": [name, ...],     # installed but different from the repo
         "missing": [name, ...],   # configured but absent from the game
         "unchecked": [name, ...],  # configured but its SOURCE is absent here,
                                    # so nothing was compared (never silent)
         "files": [{"name","state","installed","expected"}, ...]}
    """
    result: dict = {"checked": False, "ok": True, "reason": None,
                    "stale": [], "missing": [], "unchecked": [], "files": []}
    try:
        root = Path(games_dir) if games_dir else _find_project_root()
        source_shim = shim_source_path(games_dir)
        if not source_shim.exists():
            result["reason"] = f"source shim not found at {source_shim}"
            return result
        install_root = _find_game_install_path(game_id, games_dir)
        game_dir = _launch_file_game_dir(install_root)
        if not game_dir:
            result["reason"] = f"could not locate the game/ directory for {game_id!r}"
            return result

        wanted: list = [(source_shim, "vnflight.rpy")]
        always_on = False
        config, config_error = _load_config_with_error(games_dir)
        if config_error:
            # Mods unknown — still worth checking the core shim, but say so.
            result["reason"] = f"game config unreadable ({config_error}); mods not checked"
        else:
            game_cfg = ((config or {}).get("games", {}) or {}).get(game_id, {}) or {}
            always_on = "--always-on" in (game_cfg.get("install_shim_flags") or [])
            # One truth for "which adapters belong to this game": the same
            # resolver install-shim uses (explicit list, or the mods-repo
            # manifest).  Hand-edited config: nothing can be assumed to be a
            # string, and a missing source used to crash the launch OUTSIDE
            # this function's guard.  The installer treats every problem as a
            # CONFIG ERROR; here a problem is REPORTED as unchecked, never
            # silently dropped (that would let a partial checkout approve an
            # arbitrary installed copy), and never blocks — see
            # stale_shim_error's fail-open rule.
            resolution = resolve_game_mods(config, game_id, game_cfg, root)
            for entry in resolution.entries:
                if entry["source"].exists():
                    wanted.append((entry["source"], entry["target"]))
                else:
                    # An explicit mod whose source is missing: the installer
                    # treats it as a config error; here it is visible as an
                    # unchecked target, not implied.
                    result["unchecked"].append(entry["target"])
            for problem in resolution.problems:
                result["unchecked"].append(f"<{problem}>")

        for src, name in wanted:
            installed = game_dir / name
            expected = _expected_fingerprints(
                src, always_on=(always_on and name == "vnflight.rpy")
            )
            if not installed.exists():
                result["missing"].append(name)
                result["files"].append({"name": name, "state": "missing",
                                        "installed": None,
                                        "expected": next(iter(expected), "")})
                continue
            got = _shim_fingerprint(installed)
            state = "ok" if got in expected else "stale"
            if state == "stale":
                result["stale"].append(name)
            result["files"].append({"name": name, "state": state,
                                    "installed": got,
                                    "expected": next(iter(expected), "")})
        result["checked"] = True
        result["ok"] = not result["stale"] and not result["missing"]
        if result["unchecked"]:
            note = ("could not verify "
                    + ", ".join(str(x) for x in result["unchecked"])
                    + " — mod source missing, or its config entry is malformed")
            result["reason"] = f"{result['reason']}; {note}" if result["reason"] else note
    except Exception as exc:  # noqa: BLE001 — a broken check must never block a launch
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


def _env_enabled(name: str) -> bool:
    """True only for an explicitly affirmative value.

    A bare truthiness test made VNFLIGHT_ALLOW_STALE_SHIM=0 (and =false) DISABLE
    the safety gate — the opposite of what the operator wrote and of what the
    message documents.
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


#: Set to 1 to launch anyway with a shim that does not match the repo (e.g. you
#: are deliberately testing a modified shim).
ALLOW_STALE_SHIM_ENV = "VNFLIGHT_ALLOW_STALE_SHIM"


def _shim_refusal(st: dict, game_id: str) -> Optional[str]:
    """Format a refusal from an already-computed status, or None to proceed."""
    if not st.get("checked") or st.get("ok"):
        return None
    lines = [f"REFUSED: {game_id} is running a stale shim — the game's copy has "
             f"drifted from the repo."]
    for f in st["files"]:
        if f["state"] == "ok":
            continue
        got = (f["installed"] or "")[:8] or "-"
        exp = (f["expected"] or "")[:8] or "-"
        lines.append(f"    {f['state']:8} {f['name']:24} installed {got}  repo {exp}")
    lines.append(f"  fix:      python vnflight.py install-shim {game_id}")
    lines.append(f"  override: {ALLOW_STALE_SHIM_ENV}=1")
    return chr(10).join(lines)


def _shim_note(st: dict, game_id: str) -> Optional[str]:
    """Format a warning for anything the check could NOT verify, or None.

    Every fail-open path belongs here, not just unchecked mods: a missing
    source shim, an unresolvable game directory and an unreadable config all
    mean "we did not actually confirm this game is current", and each used to
    proceed in complete silence — which is the failure this whole gate exists
    to end.
    """
    if not st.get("checked"):
        reason = st.get("reason") or "reason unknown"
        return (f"WARNING: {game_id} shim NOT verified — {reason}. "
                f"Proceeding; run `python vnflight.py install-shim {game_id}` "
                f"if the game misbehaves.")
    if st.get("unchecked"):
        # str() every item: this formatter runs OUTSIDE shim_status's exception
        # guard, so a stray non-string here would escape as a crashed launch.
        names = ", ".join(str(x) for x in st["unchecked"])
        return (f"WARNING: {game_id} shim check incomplete — "
                f"{names} not compared "
                f"(mod source missing from this checkout/release, or its "
                f"config entry is malformed).")
    if st.get("reason"):
        return f"WARNING: {game_id} shim check incomplete — {st['reason']}."
    return None


def shim_report(
    game_id: str, games_dir: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(refusal, warning)`` for a game from ONE filesystem scan.

    Launch paths should call this rather than ``stale_shim_error`` +
    ``shim_warning``: those each re-scan, doubling the work and leaving a window
    in which the two answers could disagree about the same launch.

    A refusal means do not start (see ``_shim_refusal``); a warning means
    proceed but say so. Both can be None, and a refusal never comes with a
    warning worth printing on top of it.
    """
    st = shim_status(game_id, games_dir)
    refusal = None if _env_enabled(ALLOW_STALE_SHIM_ENV) else _shim_refusal(st, game_id)
    return refusal, _shim_note(st, game_id)


def stale_shim_error(game_id: str, games_dir: Optional[str] = None) -> Optional[str]:
    """Convenience wrapper: just the refusal. Prefer ``shim_report`` in launch
    paths, which gets the warning from the same scan."""
    return shim_report(game_id, games_dir)[0]


def shim_warning(game_id: str, games_dir: Optional[str] = None) -> Optional[str]:
    """Convenience wrapper: just the warning. See ``shim_report``."""
    return shim_report(game_id, games_dir)[1]


def _launch_file_awaiting_claim(
    target: Path, now: Optional[float] = None
) -> bool:
    """True when *target* holds a FRESH, UNCLAIMED launch file.

    That is the one state in which overwriting is unsafe: some game process
    was handed this file moments ago and has not yet booted far enough to
    read it.  Everything else — absent, unreadable, malformed, already
    claimed, or too old (a launch that never started, or a broken clock) —
    is fair game, because nothing is going to adopt it any more.
    """
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if "claimed_by" in data:
        return False  # a shim already adopted it; that launch is settled
    try:
        written_at = float(data.get("written_at") or 0)
    except (TypeError, ValueError):
        return False
    if written_at <= 0:
        return False
    age = (time.time() if now is None else now) - written_at
    # Symmetric horizon, mirroring the shim's freshness check: a timestamp
    # far in the future is a broken clock, not a live claim window.
    if age > LAUNCH_CLAIM_FRESH_HORIZON or age < -LAUNCH_CLAIM_FRESH_HORIZON:
        return False
    return True


def _launch_receipt_path(launch_file_path: str | Path, launch_id: str) -> Path:
    """Return the process-owned registration receipt path for one launch."""
    target = Path(launch_file_path)
    return target.with_name(f"{LAUNCH_RECEIPT_PREFIX}{launch_id}.json")


def _prune_stale_launch_receipts(game_dir: Path) -> None:
    """Remove abandoned per-launch receipts after their live retry horizon."""
    cutoff = time.time() - LAUNCH_CLAIM_FRESH_HORIZON
    for path in game_dir.glob(f"{LAUNCH_RECEIPT_PREFIX}*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


_launch_tmp_counter = itertools.count()
_launch_write_locks: Dict[str, "threading.Lock"] = {}
_launch_write_locks_guard = threading.Lock()


def _launch_tmp_path(game_dir: Path) -> Path:
    """A tmp path that cannot collide with any other writer, anywhere.

    A pid-only suffix is NOT enough: the hub is threaded, so two writers in
    ONE process would share the path and interleave their writes into it.
    pid + thread ident + a process-wide counter is unique across processes,
    across threads, and across repeat calls on one thread (ident is recycled
    once a thread dies).
    """
    return game_dir / (
        LAUNCH_FILE_NAME
        + ".tmp-%d-%d-%d" % (
            os.getpid(), threading.get_ident(), next(_launch_tmp_counter)
        )
    )


def _launch_write_lock(target: Path) -> "threading.Lock":
    """The in-process lock guarding *target*, created on first use.

    Keyed on the normalized absolute path so two spellings of the same file
    (relative vs absolute, differing case on Windows) share one lock.
    """
    key = os.path.normcase(os.path.abspath(str(target)))
    with _launch_write_locks_guard:
        lock = _launch_write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _launch_write_locks[key] = lock
        return lock


def _try_launch_file_lock(fd: int) -> bool:
    """Try to take the platform's non-blocking exclusive lock on *fd*."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        return False
    return True


def _acquire_launch_lockfile(
    lock_path: Path, _claim_wait: float
) -> Tuple[Optional[int], Optional[str]]:
    """Take the cross-process lock for a launch file.

    Returns ``(handle, warning)``.  ``handle`` is None when we could not take
    the lock; the caller then proceeds ANYWAY (a launch must never deadlock)
    with the returned warning, which — like the claim-timeout warning — is
    written to stand on its own, because callers branch on the returned path,
    not on warning text.

    The file is persistent; ownership is the OS advisory lock, not the
    pathname's existence.  POSIX ``flock`` and Windows byte-range locks are
    released automatically when the descriptor closes, including process
    death, so there is no stale file to identify or unlink.  That matters:
    check-then-unlink stale recovery cannot be made generation-safe when two
    contenders inspect the same abandoned pathname concurrently.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        # msvcrt.locking requires a real byte range.  Concurrent initializers
        # may both write the same sentinel byte; that is harmless.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
    except OSError as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        return None, (
            f"the launch file WAS written, but without a cross-process "
            f"lock ({lock_path.name} could not be opened: {exc}); a second "
            f"launcher writing the same game at this moment could overwrite it"
        )

    deadline = time.monotonic() + LAUNCH_LOCK_ACQUIRE_WAIT
    while not _try_launch_file_lock(fd):
        if time.monotonic() >= deadline:
            try:
                os.close(fd)
            except OSError:
                pass
            return None, (
                f"the launch file WAS written, but only after waiting for "
                f"another launcher process to release {lock_path.name}, which "
                f"it never did; that launch and this one may now disagree "
                f"about which bridge this game talks to"
            )
        time.sleep(LAUNCH_LOCK_POLL)
    return fd, None


def _release_launch_lockfile(
    lock: Optional[int], lock_path: Path
) -> None:
    """Release the OS lock and close its descriptor.  Never raises."""
    if lock is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(lock, 0, os.SEEK_SET)
            msvcrt.locking(lock, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(lock)
    except OSError:
        pass


def _remove_launch_file_if_owned(
    launch_file: str,
    bridge_url: str,
    slot_token: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Remove a handshake only if its current payload still belongs to us.

    ``vnflight_launch.json`` is one shared pathname per game.  An older slot
    therefore must not blindly unlink the path during teardown: a newer slot
    may already have written its own still-unclaimed handshake there.  Read
    and remove under the same writer locks used by ``_write_launch_file`` and
    require the current bridge URL plus, when available, the slot token to
    match the owner being stopped.

    Returns ``(removed, warning)``.  A missing or foreign file is a normal
    no-op; malformed/unreadable state and lock failures are reported but left
    untouched because ownership cannot be proved safely.
    """
    target = Path(launch_file)
    lock_path = target.parent / (LAUNCH_FILE_NAME + LAUNCH_LOCK_SUFFIX)
    with _launch_write_lock(target):
        lock, lock_warning = _acquire_launch_lockfile(lock_path, 0.0)
        if lock is None:
            return False, lock_warning
        try:
            try:
                with open(target, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except FileNotFoundError:
                return False, None
            except (OSError, ValueError) as exc:
                return False, (
                    f"could not verify ownership of {target}: {exc}"
                )
            if not isinstance(payload, dict):
                return False, (
                    f"could not verify ownership of {target}: invalid payload"
                )
            if str(payload.get("bridge_url") or "") != str(bridge_url):
                return False, None
            if (
                slot_token is not None
                and str(payload.get("slot_token") or "") != str(slot_token)
            ):
                return False, None
            try:
                os.remove(str(target))
            except FileNotFoundError:
                return False, None
            except OSError as exc:
                return False, f"could not remove {target}: {exc}"
            return True, None
        finally:
            _release_launch_lockfile(lock, lock_path)


def _await_launch_claim(target: Path, wait: float) -> Optional[str]:
    """Wait for a fresh, unclaimed *target* to be claimed; warn on timeout.

    The deadline starts HERE — i.e. when the caller entered the critical
    section, not when it called ``_write_launch_file`` — so a writer that
    queued behind another writer still gets its full claim wait for the
    file that writer left behind.
    """
    deadline = time.monotonic() + max(wait, 0.0)
    while _launch_file_awaiting_claim(target):
        if time.monotonic() >= deadline:
            return (
                f"the launch file WAS written, but only after waiting "
                f"{max(wait, 0.0):.0f}s for the previous launch of this game "
                f"to claim the existing {LAUNCH_FILE_NAME}; if that game is "
                f"still booting it may now read THIS bridge's URL"
            )
        time.sleep(LAUNCH_CLAIM_POLL)
    return None


def _write_launch_file(
    install_root: Optional[Path],
    bridge_url: str,
    slot_token: Optional[str] = None,
    save_slot: Optional[str] = None,
    claim_wait: Optional[float] = None,
    move_host_pointer: Optional[bool] = None,
    launch_id: Optional[str] = None,
    debug: Optional[bool] = None,
    debug_logs: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Atomically write ``game/vnflight_launch.json`` for the shim.

    Launcher-mediated games (steam://rungameid/..., goggalaxy://...) are
    started by the PLATFORM launcher, not by us — the environment set on
    the process we spawn (VNFLIGHT_BRIDGE_URL / VNFLIGHT_SLOT_TOKEN /
    VNFLIGHT_SAVE_SLOT) never reaches the game, so the shim would dial
    the default port tokenless and miss per-slot bridges entirely.  This
    file carries the same values through the filesystem; the shim treats
    a FRESH file (see its validity window) as this-launch intent that
    wins over possibly-stale env.

    Written for EVERY launch — for env-capable direct-exe games the
    values are identical to the env, so behavior is unchanged.

    CLAIM PROTOCOL.  There is exactly ONE such file per game, so two
    launches of the SAME game race: the second write can land inside the
    first game's multi-second Ren'Py boot window, and BOTH shims then read
    the second slot's bridge_url — one bridge sees no game, the other sees
    two.  The file therefore doubles as the serializer.  A shim that adopts
    a file rewrites it with ``claimed_by`` (its pid); this writer waits up
    to *claim_wait* seconds for a fresh, unclaimed file to pick up its
    claim before overwriting it.  Absent, malformed, already-claimed, and
    stale files (see ``_launch_file_awaiting_claim``) never wait.

    *claim_wait* defaults to ``LAUNCH_CLAIM_WAIT``; 0 disables waiting
    (the state is still inspected, so the caller still gets the warning).

    WRITER VS WRITER.  The claim protocol serializes a writer against a
    booting GAME; it does nothing about two WRITERS, which both observe a
    safe state and both write — and the hub is threaded and starts slots
    near-simultaneously.  So the whole inspect/wait/write section runs
    under two locks: a per-target ``threading.Lock`` (in-process, covering
    hub threads) and an OS advisory lock on a persistent file next to the
    target (cross-process, covering two CLIs or CLI+hub).  After waiting for
    the writer lock, a contender re-runs the claim loop against the previous
    writer's now-present fresh file.  Its full *claim_wait* starts only after
    it owns the writer lock, not when it called this function.

    Returns ``(path, warning)``: the written file path (None on failure)
    and a human-readable warning.  Note both can be set at once — waiting
    out the deadline writes anyway and warns — so the warning text is
    written to stand on its own rather than to imply "not written".
    """
    game_dir = _launch_file_game_dir(install_root)
    if game_dir is None:
        where = f" under {install_root}" if install_root else ""
        return None, f"could not locate the game's game/ directory{where}"
    target = game_dir / LAUNCH_FILE_NAME
    wait = max(
        LAUNCH_CLAIM_WAIT if claim_wait is None else float(claim_wait), 0.0
    )
    lock_path = game_dir / (LAUNCH_FILE_NAME + LAUNCH_LOCK_SUFFIX)
    notes: List[str] = []
    with _launch_write_lock(target):
        lock, lock_warning = _acquire_launch_lockfile(lock_path, wait)
        if lock_warning:
            notes.append(lock_warning)
        try:
            claim_warning = _await_launch_claim(target, wait)
            if claim_warning:
                notes.append(claim_warning)
            _prune_stale_launch_receipts(game_dir)
            path, write_warning = _write_launch_payload(
                game_dir,
                target,
                bridge_url,
                slot_token,
                save_slot,
                move_host_pointer,
                launch_id,
                debug=debug,
                debug_logs=debug_logs,
            )
        finally:
            _release_launch_lockfile(lock, lock_path)
    if write_warning:
        notes.append(write_warning)
    return path, ("; ".join(notes) if notes else None)


def _write_launch_payload(
    game_dir: Path,
    target: Path,
    bridge_url: str,
    slot_token: Optional[str],
    save_slot: Optional[str],
    move_host_pointer: Optional[bool],
    launch_id: Optional[str],
    debug: Optional[bool] = None,
    debug_logs: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """tmp-write + atomic replace of the launch file itself.

    Call only while holding the target's locks (see ``_write_launch_file``).
    """
    payload: Dict[str, Any] = {
        # ALWAYS include the bridge URL (even the default) — a fresh file
        # must fully describe this launch, not depend on leftover env.
        "bridge_url": bridge_url,
        "written_at": time.time(),
    }
    if slot_token:
        payload["slot_token"] = slot_token
    if save_slot:
        payload["save_slot"] = save_slot
    if move_host_pointer is not None:
        payload["move_host_pointer"] = bool(move_host_pointer)
    if launch_id:
        payload["launch_id"] = launch_id
    if debug is not None:
        payload["debug"] = bool(debug)
    if debug_logs:
        payload["debug_logs"] = str(debug_logs)
    tmp = _launch_tmp_path(game_dir)
    replaced = False
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(str(tmp), str(target))
        replaced = True
    except OSError as exc:
        return None, f"could not write {target}: {exc}"
    finally:
        # Clean up after ANY failure, not just OSError — an unexpected
        # exception must not leave a half-written tmp file behind either.
        if not replaced:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
    return str(target), None


def _probe_launch_file_writable(
    install_root: Optional[Path],
) -> Optional[str]:
    """Non-destructively verify that a mediated launch can write its handoff."""
    game_dir = _launch_file_game_dir(install_root)
    if game_dir is None:
        where = f" under {install_root}" if install_root else ""
        return f"could not locate the game's game/ directory{where}"
    target = game_dir / LAUNCH_FILE_NAME
    probe = _launch_tmp_path(game_dir)
    replaced = probe.with_name(probe.name + ".replaced")
    # This is an advisory directory/target permission probe, not a handoff
    # writer. Its temp paths are process-unique, so waiting behind the real
    # launch serializer only turns legitimate same-game contention into a
    # false "not writable" result.
    try:
        if target.exists():
            try:
                mode = target.stat().st_mode
            except OSError as exc:
                return f"could not inspect {target}: {exc}"
            if (
                os.name == "nt"
                and (not (mode & stat.S_IWRITE) or not os.access(target, os.W_OK))
            ):
                return f"existing launch file is not writable: {target}"
        with open(probe, "wb") as f:
            f.write(b"{}")
        os.replace(str(probe), str(replaced))
    except OSError as exc:
        return f"could not write in {game_dir}: {exc}"
    finally:
        for path in (probe, replaced):
            try:
                path.unlink()
            except OSError:
                pass
    return None


# ---------------------------------------------------------------------------
# Configuration & game discovery
# ---------------------------------------------------------------------------
#
# Games are configured in ``vnflight.json`` at the project root.
# The config maps game IDs to opaque launch commands so the LLM never
# sees source paths.
#
# Example config:
#
#   {
#     "bridge_script": "bridge/game_bridge.py",
#     "games": {
#       "echoes_of_tomorrow": {
#         "name": "Echoes of Tomorrow",
#         "launch": "renpy-8.5.2-sdk/renpy.exe echoes_of_tomorrow",
#         "briefing": "echoes_of_tomorrow/game_briefing.md"
#       },
#       "some_steam_game": {
#         "name": "Disco Elysium",
#         "launch": "steam://rungameid/632470",
#         "briefing_text": "An RPG about a detective with amnesia..."
#       }
#     }
#   }
#
# If no config exists, the tool falls back to auto-discovery (scans for
# directories containing ``game/vnflight.rpy`` and Ren'Py SDKs).


def _find_project_root() -> Path:
    """Walk up from this script to find the project root."""
    here = Path(__file__).resolve().parent
    candidates = [here, here.parent, here.parent.parent]
    # A source checkout contains both ``src/bridge`` and the actual project
    # config one level above ``src``.  Prefer an explicit config across the
    # whole search range before accepting the legacy bridge-directory marker,
    # otherwise MCP subprocesses load ``src`` as their root and see no games or
    # timing profiles.
    for candidate in candidates:
        if (candidate / CONFIG_FILENAME).exists():
            return candidate
    for candidate in candidates:
        if (candidate / "bridge").is_dir():
            return candidate
    return here


def shim_source_path(games_dir: Optional[str] = None) -> Path:
    """The ``vnflight.rpy`` that ships next to this code.

    An explicit ``--games-dir`` that carries its own ``vnflight.rpy`` is a
    full project root and wins, as before.  A config directory without one
    (the released layout: ``vnflight.json`` and games somewhere, the code
    elsewhere) falls back to where the code lives: the directory of the
    single-file ``vnflight.py``, or the checkout root above ``src/vnflight``.
    """
    here = Path(__file__).resolve().parent
    candidates = []
    if games_dir:
        candidates.append(Path(games_dir))
    candidates += [here, here.parent, here.parent.parent]
    for candidate in candidates:
        source = candidate / "vnflight.rpy"
        if source.exists():
            return source
    return (Path(games_dir) if games_dir else _find_project_root()) / "vnflight.rpy"


def _load_config_with_error(
    games_dir: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """Load ``vnflight.json``.

    Returns (config, error).  ``error`` is set when the config file exists
    but could not be read/parsed — callers that must not fail silently
    (install-shim) surface it instead of quietly proceeding without mods.
    """
    root = Path(games_dir) if games_dir else _find_project_root()
    config_path = root / CONFIG_FILENAME
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f), None
        except (json.JSONDecodeError, OSError) as exc:
            return None, f"{config_path}: {exc}"
    return None, None


def _load_config(games_dir: Optional[str] = None) -> Optional[dict]:
    """Load ``vnflight.json`` if it exists.  Returns None otherwise."""
    return _load_config_with_error(games_dir)[0]


# ---------------------------------------------------------------------------
# Game adapters ("mods"): explicit list, or a mods-repo manifest
# ---------------------------------------------------------------------------

MODS_MANIFEST_KEY = "mods_manifest"
MODS_SNAPSHOT_KEY = "mods_snapshot"


def pinned_mods_snapshot(config: Optional[dict]) -> Optional[Tuple[str, str]]:
    """The (url, sha256) pinned under the top-level ``mods_snapshot`` key.

    The template ships the snapshot a release was tested with.  Only
    ``fetch-mods`` reads it, and only when run without explicit
    arguments; nothing downloads it implicitly.  ``None`` when the key is
    missing or malformed (callers say which key to fix).
    """
    entry = (config or {}).get(MODS_SNAPSHOT_KEY)
    if not isinstance(entry, dict):
        return None
    url = entry.get("url")
    digest = entry.get("sha256")
    if not isinstance(url, str) or not isinstance(digest, str):
        return None
    url, digest = url.strip(), digest.strip().lower()
    if not url or not digest:
        return None
    return url, digest


class ModResolution:
    """Where a game's adapters come from and what they are.

    ``entries``: ``[{"source": Path, "target": str, "label": str,
    "sha256": Optional[str]}]`` ready to install/compare.
    ``origin``: ``"explicit"`` (the game entry's own ``mods`` list),
    ``"manifest"`` (the mods-repo manifest named by the top-level
    ``mods_manifest`` key) or ``"none"``.
    ``problems``: human-readable reasons for entries that could NOT be
    resolved (malformed entry, missing file, manifest unreadable, sha256
    mismatch).  Callers decide whether a problem blocks (install-shim) or
    is reported and skipped (the fail-open stale-shim check).
    """

    def __init__(self, origin: str, entries: List[dict], problems: List[str],
                 manifest_path: Optional[Path] = None):
        self.origin = origin
        self.entries = entries
        self.problems = problems
        self.manifest_path = manifest_path

    def summary(self) -> str:
        n = len(self.entries)
        noun = "adapter" if n == 1 else "adapters"
        if self.origin == "explicit":
            base = f"{n} {noun} (config)"
        elif self.origin == "manifest":
            base = f"{n} {noun} (manifest)"
        else:
            base = "no adapters"
        if self.problems:
            # Say what the problem is; a bare count sends the user to
            # install-shim just to read the reason.
            base += f", {len(self.problems)} problem(s): " + "; ".join(self.problems)
        return base


def _resolve_manifest_path(config: dict, root: Path) -> Optional[Path]:
    raw = (config or {}).get(MODS_MANIFEST_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        p = root / p
    return p


def resolve_game_mods(config: Optional[dict], game_id: str, game_cfg: dict,
                      root: Path) -> ModResolution:
    """Resolve a game's adapters.

    Order: a ``mods`` key on the game entry wins as written (an empty list
    means "none", the manifest is not consulted).  Without a ``mods`` key,
    the manifest named by the top-level ``mods_manifest`` (absolute, or
    relative to ``vnflight.json``; never a URL) supplies the files for the
    game id, or for the id named by the entry's ``adapters`` key (so a
    second config entry for the same game reuses one manifest entry).
    Manifest files are verified against their sha256 before use; a mismatch
    or a missing file is a problem, not an install.
    """
    game_cfg = game_cfg or {}
    problems: List[str] = []
    entries: List[dict] = []

    if "mods" in game_cfg:
        raw_mods = game_cfg.get("mods") or []
        if not isinstance(raw_mods, list):
            return ModResolution("explicit", [], [f"'mods' for {game_id!r} is not a list"])
        for mod in raw_mods:
            if not isinstance(mod, dict):
                problems.append(f"malformed mod entry for {game_id!r}: {mod!r}")
                continue
            source = mod.get("source")
            target = mod.get("target")
            if not isinstance(target, str) or not target.strip():
                problems.append(f"mod with invalid target for {game_id!r}: {target!r}")
                continue
            if not isinstance(source, str) or not source.strip():
                problems.append(f"mod {target!r} for {game_id!r} has no source")
                continue
            src = Path(source).expanduser()
            if not src.is_absolute():
                src = root / src
            entries.append({"source": src, "target": target, "label": source,
                            "sha256": None})
        return ModResolution("explicit", entries, problems)

    manifest_path = _resolve_manifest_path(config or {}, root)
    if manifest_path is None:
        return ModResolution("none", [], [])

    key = game_cfg.get("adapters") if isinstance(game_cfg.get("adapters"), str) else game_id
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ModResolution("manifest", [], [f"mods manifest not found: {manifest_path}"],
                             manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        return ModResolution("manifest", [], [f"mods manifest unreadable: {manifest_path}: {exc}"],
                             manifest_path)
    games = manifest.get("games") if isinstance(manifest, dict) else None
    entry = (games or {}).get(key) if isinstance(games, dict) else None
    if not isinstance(entry, dict):
        # Not listed: a game the manifest does not know is simply a game
        # without adapters.
        return ModResolution("none", [], [], manifest_path)
    for mod in entry.get("mods") or []:
        if not isinstance(mod, dict):
            problems.append(f"malformed manifest entry for {key!r}: {mod!r}")
            continue
        name = mod.get("file")
        target = mod.get("target")
        expected = mod.get("sha256")
        if not isinstance(name, str) or not name.strip() or "/" in name or "\\" in name:
            problems.append(f"manifest entry for {key!r} has an invalid file name: {name!r}")
            continue
        if not isinstance(target, str) or not target.strip():
            problems.append(f"manifest entry {name!r} for {key!r} has an invalid target: {target!r}")
            continue
        src = manifest_path.parent / name
        if not src.exists():
            problems.append(f"adapter {name!r} listed for {key!r} is missing next to the manifest ({src})")
            continue
        if isinstance(expected, str) and expected.strip():
            actual = hashlib.sha256(src.read_bytes()).hexdigest()
            if actual.lower() != expected.strip().lower():
                problems.append(
                    f"adapter {name!r} for {key!r} does not match the manifest "
                    f"(sha256 {actual[:12]}... vs manifest {expected.strip()[:12]}...); "
                    "refusing to install it"
                )
                continue
        entries.append({"source": src, "target": target,
                        "label": f"{manifest_path.name}:{name}",
                        "sha256": expected if isinstance(expected, str) else None})
    return ModResolution("manifest", entries, problems, manifest_path)


def describe_game_mods(config: Optional[dict], game_id: str, game_cfg: dict,
                       root: Path) -> str:
    """One line for ``games``: where the adapters come from."""
    try:
        return resolve_game_mods(config, game_id, game_cfg, root).summary()
    except Exception as exc:  # noqa: BLE001 - listing must never fail on mods
        return f"adapters: error ({exc})"


def _read_briefing(root: Path, game_cfg: dict) -> Tuple[bool, str, str]:
    """
    Read briefing text for a game entry.

    Returns (has_briefing, full_text, short_description).
    """
    # Inline briefing text takes priority.
    inline = game_cfg.get("briefing_text", "")
    if inline:
        short = ""
        for line in inline.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                short = line[:100]
                break
        return True, inline, short

    # Path-based briefing.
    briefing_rel = game_cfg.get("briefing", "")
    if briefing_rel:
        briefing_path = root / briefing_rel
        if briefing_path.exists():
            text = briefing_path.read_text(encoding="utf-8")
            short = ""
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    short = line[:100]
                    break
            return True, text, short

    return False, "", ""


# Game-entry keys (vnflight.json "games" values) that map to bridge-side
# GameState runtime config, pushed via POST /config after the game connects.
# This is THE lookup for per-game bridge config — the CLI launch path
# (launch_game below) and the harness vn plugin both go through
# extract_bridge_config so the key list lives in exactly one place.
BRIDGE_CONFIG_KEYS: Tuple[str, ...] = ("end_on_menu_return",)


def extract_bridge_config(game_cfg: dict) -> dict:
    """Return the bridge-side config overrides present in a game entry.

    e.g. Slay the Princess sets ``"end_on_menu_return": false`` because its
    live gameplay screens are classified as main_menu, which false-fires
    the return-to-menu playthrough-ended heuristic.
    """
    return {k: game_cfg[k] for k in BRIDGE_CONFIG_KEYS if k in game_cfg}


def discover_games(games_dir: Optional[str] = None) -> List[dict]:
    """
    Discover available games.

    First checks ``vnflight.json`` for explicit entries.  Falls back
    to scanning for directories containing ``game/vnflight.rpy``.
    """
    root = Path(games_dir) if games_dir else _find_project_root()
    config = _load_config(games_dir)

    games: List[dict] = []

    if config and "games" in config:
        # ---- Config-based discovery ----
        for game_id, game_cfg in config["games"].items():
            # The template carries a string-valued "_comment" under games
            # and "_example_*" entries: neither is a game.  A string
            # entry used to crash the listing ('str' has no .get).
            if not isinstance(game_cfg, dict) or str(game_id).startswith("_"):
                continue
            has_briefing, briefing, short_desc = _read_briefing(root, game_cfg)
            games.append(
                {
                    "id": game_id,
                    "name": game_cfg.get("name", game_id),
                    "launch_cmd": game_cfg.get("launch", ""),
                    "game_dir": game_cfg.get("game_dir"),
                    # Optional per-game shim diagnostics: debug (bool) turns
                    # on the shim's command/action log; debug_logs is the
                    # directory it writes per-session files into (default:
                    # the game's own directory). A launch may override debug.
                    "debug": game_cfg.get("debug"),
                    "debug_logs": game_cfg.get("debug_logs"),
                    "has_briefing": has_briefing,
                    "briefing": briefing,
                    "short_desc": short_desc,
                    "bridge_config": extract_bridge_config(game_cfg),
                    # Where the adapters come from: the entry's own mods
                    # list, the mods-repo manifest, or none.
                    "mods": describe_game_mods(config, game_id, game_cfg, root),
                    "_source": "config",
                }
            )
    else:
        # ---- Auto-discovery fallback ----
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            marker = entry / "game" / "vnflight.rpy"
            if not marker.exists():
                continue

            briefing = ""
            short_desc = ""
            has_briefing = False
            briefing_path = entry / "game_briefing.md"
            if briefing_path.exists():
                has_briefing = True
                briefing = briefing_path.read_text(encoding="utf-8")
                for line in briefing.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        short_desc = line[:100]
                        break

            # Build a default launch command from auto-detected SDK.
            launch_cmd = _build_auto_launch_cmd(root, str(entry))

            games.append(
                {
                    "id": entry.name,
                    "name": entry.name,
                    "launch_cmd": launch_cmd,
                    "has_briefing": has_briefing,
                    "briefing": briefing,
                    "short_desc": short_desc,
                    "_source": "auto",
                }
            )

    return games


def _build_auto_launch_cmd(root: Path, game_path: str) -> str:
    """Build a launch command string from auto-detected SDK + game path."""
    sdks = _discover_sdks(root)
    if not sdks:
        return ""
    sdk = sdks[0]
    exe = sdk["exe"]
    # Build a command relative to root if possible.
    try:
        rel_exe = os.path.relpath(exe, root)
        rel_game = os.path.relpath(game_path, root)
    except ValueError:
        rel_exe = exe
        rel_game = game_path
    return f"{rel_exe} {rel_game}"


def _discover_sdks(root: Path) -> List[dict]:
    """Scan for Ren'Py SDKs (directories matching renpy-*-sdk/)."""
    sdks: List[dict] = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not re.match(r"renpy-[\d.]+-sdk", entry.name):
            continue

        exe = entry / ("renpy.exe" if IS_WINDOWS else "renpy.sh")
        if not exe.exists():
            exe = entry / "renpy.py"
            if not exe.exists():
                continue

        m = re.search(r"renpy-([\d.]+)-sdk", entry.name)
        version = m.group(1) if m else "unknown"

        sdks.append(
            {
                "path": str(entry),
                "exe": str(exe),
                "version": version,
                "name": entry.name,
            }
        )

    sdks.sort(key=lambda s: s["version"], reverse=True)
    return sdks


_BRIDGE_MODULE_SENTINEL = "__vnflight_bridge_module__"


def _find_bridge_script(games_dir: Optional[str] = None) -> Optional[str]:
    """Locate the bridge server.

    Returns a path to a standalone bridge script (if configured), or the
    sentinel ``_BRIDGE_MODULE_SENTINEL`` to launch via
    ``python -m vnflight.bridge``.
    """
    root = Path(games_dir) if games_dir else _find_project_root()

    # Honour explicit config (e.g. custom bridge script).
    config = _load_config(games_dir)
    if config:
        script = config.get("bridge_script")
        if script:
            candidate = root / script
            if candidate.exists():
                return str(candidate)

    # Primary: use the package module.
    try:
        import vnflight.bridge  # noqa: F401
        return _BRIDGE_MODULE_SENTINEL
    except (ImportError, ModuleNotFoundError):
        pass
    # File-system fallback: locate bridge.py relative to this module.
    _bridge_py = Path(__file__).parent / "bridge.py"
    if _bridge_py.exists():
        return _BRIDGE_MODULE_SENTINEL
    # Built single-file: bridge symbols are in __main__.
    try:
        import __main__ as _main_mod
        if callable(getattr(_main_mod, "run_bridge_server", None)):
            return _BRIDGE_MODULE_SENTINEL
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Process management (launch / stop)
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        if IS_WINDOWS:
            # tasklist emits OEM-codepage bytes; decoding as UTF-8
            # (text=True's default) crashes the reader thread on bytes
            # like 0xff, leaving stdout=None -> "NoneType is not
            # iterable". errors="replace" keeps it safe (the PID digits
            # we match on are ASCII regardless). CSV output lets us match
            # the PID column exactly instead of a substring that could
            # hit 1234 for 123 or a memory-size column.
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
            out = result.stdout or ""
            for row in csv.reader(out.splitlines()):
                if len(row) >= 2 and row[1].strip() == str(pid):
                    return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def process_command_line(pid: int) -> Optional[str]:
    """Return "<image name> <command line>" for a live process, or None when
    it cannot be read (no such process, access denied, tool failure)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        if IS_WINDOWS:
            script = (
                "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = "
                + str(pid)
                + "\"; if ($p) { $p.Name; $p.CommandLine }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, errors="replace", timeout=15,
            )
            lines = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
            return " ".join(lines) or None
        proc_cmdline = f"/proc/{pid}/cmdline"
        if os.path.exists(proc_cmdline):
            with open(proc_cmdline, "rb") as f:
                value = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if value:
                return value
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
        return (result.stdout or "").strip() or None
    except Exception:
        return None


# Tokens too generic to identify a game process on their own.
_GENERIC_IDENTITY_TOKENS = {
    "python", "python3", "pythonw", "steam", "exe", "game", "games", "app",
    "bin", "lib", "sh", "run", "start", "launch", "x86", "x64", "windows",
    "linux", "mac", "macos",
}


def _identity_norm(text: str) -> str:
    """Case-fold and drop separators so 'Slay the Princess', 'slay_the_princess'
    and 'SlayThePrincess.exe' all compare on the same footing."""
    out = []
    for ch in str(text).lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def _identity_token_ok(token: object) -> bool:
    if not isinstance(token, str):
        return False
    norm = _identity_norm(token)
    return len(norm) >= 4 and norm not in {_identity_norm(t) for t in _GENERIC_IDENTITY_TOKENS}


def normalize_identity_expectation(expect: object) -> list:
    """An expectation is a list of groups; every group must be satisfied by at
    least one of its alternatives (case-insensitive, separator-insensitive
    substring of the process's image name + command line).  A bare string or
    a flat list of strings is one group.  Empty/invalid -> []."""
    if isinstance(expect, str):
        expect = [[expect]]
    if not isinstance(expect, (list, tuple)):
        return []
    groups = []
    if expect and all(isinstance(e, str) for e in expect):
        expect = [list(expect)]
    for group in expect:
        if isinstance(group, str):
            group = [group]
        if not isinstance(group, (list, tuple)):
            continue
        alts = [str(a) for a in group if isinstance(a, str) and a.strip()]
        if alts:
            groups.append(alts)
    return groups


def process_matches(identity: Optional[str], expect: object) -> bool:
    """True when every expectation group has an alternative present in the
    process identity string.  No identity or no expectation -> False."""
    groups = normalize_identity_expectation(expect)
    if not identity or not groups:
        return False
    hay = _identity_norm(identity)
    for group in groups:
        if not any(_identity_norm(alt) and _identity_norm(alt) in hay for alt in group):
            return False
    return True


def _describe_expectation(expect: object) -> str:
    groups = normalize_identity_expectation(expect)
    return " AND ".join("(" + " | ".join(g) + ")" for g in groups) or "<nothing>"


def kill_process(pid: int, expect: object = None) -> bool:
    """Kill a process only when it still looks like the one we launched.

    ``expect`` describes the identity (see normalize_identity_expectation):
    groups of substrings that must appear in the process's image name or
    command line.  Without an expectation, with an unreadable identity, or
    with a mismatch the kill is REFUSED (False, with a stderr line): the
    per-user state file is shared by every checkout and can hold a stale
    pid that the OS has since given to an unrelated process.

    Returns True if the process was terminated or already dead, False if
    it was refused or termination failed.
    """
    if not _is_process_alive(pid):
        return True

    groups = normalize_identity_expectation(expect)
    if not groups:
        print(
            f"[vnflight] refused to kill pid {pid}: no identity expectation given",
            file=sys.stderr,
        )
        return False
    identity = process_command_line(pid)
    if identity is None:
        print(
            f"[vnflight] refused to kill pid {pid}: its command line could not be "
            f"read (expected {_describe_expectation(groups)})",
            file=sys.stderr,
        )
        return False
    if not process_matches(identity, groups):
        head = identity if len(identity) <= 120 else identity[:117] + "..."
        print(
            f"[vnflight] refused to kill pid {pid}: {head} does not look like "
            f"{_describe_expectation(groups)}",
            file=sys.stderr,
        )
        return False

    try:
        if IS_WINDOWS:
            # Use /T to kill child processes as well (needed for Steam games)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        # Give it a moment to terminate
        time.sleep(0.2)
        return not _is_process_alive(pid)
    except Exception:
        return False


BRIDGE_IDENTITY: list = [["vnflight"], ["bridge"]]
# What a legacy pid record (no stored identity) is checked against: only
# that the process is a Ren'Py engine / a vnflight bridge at all.
LEGACY_GAME_IDENTITY: list = [["renpy"]]
LEGACY_BRIDGE_IDENTITY: list = [["vnflight"]]


def _shared_game_launcher(value: str) -> bool:
    name = value.lower()
    for suffix in (".exe", ".py", ".sh"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return bool(re.fullmatch(r"renpy|pythonw?(?:\d+(?:\.\d+)*)?(?:t|_d)?", name))


def game_identity_expectation(
    game_id: str,
    game_cmd_parts: Optional[List[str]] = None,
    install_root: object = None,
    game_name: object = None,
) -> list:
    """Two groups: the process looks like an engine/exe we launched, AND it
    looks like THIS game (its id, install directory, project directory or
    display name), so a recycled pid belonging to another Ren'Py game is
    spared too."""
    engine: list = ["renpy"]
    this_game: list = []
    parts = [str(p) for p in (game_cmd_parts or []) if isinstance(p, str)]
    if parts and "://" not in parts[0]:
        exe = parts[0]
        stem = os.path.splitext(os.path.basename(exe.rstrip("/\\")))[0]
        if _identity_token_ok(stem):
            engine.append(stem)
            if not _shared_game_launcher(stem):
                this_game.append(stem)
        for arg in parts[1:]:
            if arg.startswith("-") or "://" in arg:
                continue
            base = os.path.basename(str(arg).rstrip("/\\"))
            if _shared_game_launcher(base):
                continue
            if _identity_token_ok(base):
                this_game.append(base)
    if install_root:
        base = os.path.basename(str(install_root).rstrip("/\\"))
        if _identity_token_ok(base):
            engine.append(base)
            if not _shared_game_launcher(base):
                this_game.append(base)
    if _identity_token_ok(game_id):
        this_game.append(str(game_id))
    if _identity_token_ok(game_name):
        this_game.append(str(game_name))
    seen: set = set()
    engine = [t for t in engine if not (t.lower() in seen or seen.add(t.lower()))]
    seen = set()
    this_game = [t for t in this_game if not (t.lower() in seen or seen.add(t.lower()))]
    return [engine, this_game] if this_game else [engine]


def pid_identity_from_record(pids: dict, name: str) -> Tuple[list, str]:
    """The expectation stored next to a pid in the state file, or the legacy
    default plus a note saying so."""
    stored = (pids or {}).get(f"{name}_expect")
    groups = normalize_identity_expectation(stored)
    if groups:
        return groups, ""
    if name == "bridge":
        return LEGACY_BRIDGE_IDENTITY, " (record predates identity tracking; checked only that it is a vnflight process)"
    return LEGACY_GAME_IDENTITY, " (record predates identity tracking; checked only that it is a Ren'Py process)"


def _launch_subprocess(
    cmd: List[str],
    cwd: Optional[str] = None,
    log_file: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], str]:
    """
    Launch a subprocess detached from the current terminal.

    If *log_file* is given, stdout and stderr are redirected there.
    Otherwise output goes to ``os.devnull`` opened as a real file
    descriptor (more reliable than ``subprocess.DEVNULL`` on Windows
    when combined with ``CREATE_NEW_PROCESS_GROUP``).

    Returns (pid_or_None, error_message).
    """
    try:
        if log_file:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            fh: Any = open(log_file, "w", encoding="utf-8")
        else:
            fh = open(os.devnull, "w")
        # Prevent the handle from being garbage-collected.
        _launch_log_handles.append(fh)

        # Pass environment variable to enable the shim
        env = os.environ.copy()
        env["VNFLIGHT_ENABLED"] = "1"
        if extra_env:
            env.update(extra_env)

        kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": fh,
            "stderr": fh,
            "close_fds": True,
            "env": env,
        }
        if cwd:
            kwargs["cwd"] = cwd
        if IS_WINDOWS:
            # Both flags are needed for true detachment on Windows:
            # CREATE_NEW_PROCESS_GROUP prevents Ctrl+C propagation,
            # DETACHED_PROCESS prevents the child from dying when the
            # parent's console/shell session ends.
            DETACHED_PROCESS = 0x00000008
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS  # type: ignore[attr-defined]
            )
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **kwargs)
        return proc.pid, ""
    except FileNotFoundError as exc:
        # The OS text is localized and names nothing; say which
        # executable was asked for.
        return None, f"executable not found: '{cmd[0]}' ({exc})"
    except Exception as exc:
        return None, f"could not start '{cmd[0]}': {exc}"


def _runtime_log_dir(root: Path) -> Path:
    """Directory for bridge/game stdout logs.

    Project checkouts keep logs under ``<root>/bridge/logs`` as before.
    When root fell back to the package directory (pip install into
    site-packages, no project around), route logs to the per-user data
    dir instead of writing next to the package.
    """
    try:
        if root.resolve() == _package_dir():
            return user_data_dir() / "logs"
    except OSError:
        pass
    return root / "bridge" / "logs"


def _parse_launch_cmd(launch_cmd: str) -> List[str]:
    """
    Parse a launch command string into a list of arguments.

    Handles quoted arguments and protocol URLs (e.g., steam://, goggalaxy://).
    """
    if not launch_cmd:
        return []

    # Steam URLs or other protocol handlers.
    if "://" in launch_cmd and not launch_cmd.startswith(("/", ".", "\\")):
        if IS_WINDOWS:
            return ["cmd", "/c", "start", "", launch_cmd]
        elif platform.system() == "Darwin":
            return ["open", launch_cmd]
        else:
            return ["xdg-open", launch_cmd]

    # Split respecting quotes.  POSIX-mode shlex treats a backslash as an
    # escape, which silently mangles Windows paths ("C:\\Games\\x" ->
    # "C:Gamesx"); on Windows split non-POSIX and strip the quotes shlex
    # then leaves on quoted tokens.
    import shlex

    try:
        if IS_WINDOWS:
            parts = shlex.split(launch_cmd, posix=False)
            return [
                part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part
                for part in parts
            ]
        return shlex.split(launch_cmd)
    except ValueError:
        return launch_cmd.split()


def _registration_rejection_belongs_to_launch(
    rejection: Any,
    *,
    game_id: str,
    launch_id: str,
    launch_started_at: float,
) -> bool:
    """Return whether a token-owned diagnostic belongs to this launch."""
    if (
        not isinstance(rejection, dict)
        or not _game_ids_match(
            rejection.get("game_id"), game_id,
        )
    ):
        return False
    rejected_launch_id = rejection.get("launch_id")
    if rejected_launch_id is not None:
        return str(rejected_launch_id) == str(launch_id)
    try:
        rejected_at = float(rejection.get("rejected_at") or 0.0)
    except (TypeError, ValueError):
        return False
    # A pre-receipt shim cannot send launch_id. Bridge and launcher share the
    # host clock, and the attempt timestamp precedes writing the handoff, so a
    # current rejection cannot legitimately predate it.
    return rejected_at >= float(launch_started_at)


def _registration_rejection_matches(
    rejection: Any,
    *,
    game_id: str,
    launch_id: str,
    launch_started_at: float,
) -> bool:
    """Return whether a terminal refusal belongs to this launch attempt."""
    return (
        isinstance(rejection, dict)
        and rejection.get("transient") is not True
        and _registration_rejection_belongs_to_launch(
            rejection,
            game_id=game_id,
            launch_id=launch_id,
            launch_started_at=launch_started_at,
        )
    )


def launch_game(
    game_id: str,
    bridge_url: str,
    games_dir: Optional[str],
    fast_forward: bool,
    auto_advance: bool,
    client_state: ClientState,
    connect_timeout: Optional[float] = None,
    replace: bool = True,
    save_slot: Optional[str] = None,
    token: Optional[str] = None,
    reservation_token: Optional[str] = None,
    diagnostics: Optional[dict] = None,
    debug: Optional[bool] = None,
) -> Tuple[bool, str, Optional[int]]:
    """
    Launch the bridge server and a game.

    *debug* overrides the game's ``debug`` config for this launch (None =
    use the config). The effective value and the game's ``debug_logs``
    directory reach the shim through the environment and the launch file.

    *diagnostics*, when given, is populated in place (never via the return
    tuple, which many callers unpack positionally) with timing signals a
    caller can surface: ``connected_after_s`` (seconds from the start of the
    connect wait to the slot becoming ready) on success, or
    ``connect_failed_after_s`` on a connect-phase failure.

    The launch command comes from the config or auto-discovery.
    The LLM sees only the game ID and a success/failure message — never
    the underlying file paths.

    When *replace* is False, existing instances of the same game are kept
    alive and a new slot is allocated on the bridge (multi-instance mode).

    Returns (success, message).
    """
    # Local import to avoid module-level dependency on .client
    from .client import BridgeClient
    from .shim_schema import SHIM_PROTOCOL_VERSION

    # Refuse a game whose installed shim has drifted from the repo — see
    # stale_shim_error(). Cheapest possible moment: nothing is running yet.
    # ONE scan yields both answers — calling the two wrappers would re-scan and
    # could disagree with itself about the same launch.
    _stale, _shim_note = shim_report(game_id, games_dir)
    if _stale:
        return False, _stale, None

    games = discover_games(games_dir)
    game = next((g for g in games if g["id"] == game_id), None)
    if not game:
        available = ", ".join(g["id"] for g in games) or "(none found)"
        return False, f"Game '{game_id}' not found.  Available: {available}", None

    launch_cmd = game.get("launch_cmd", "")
    if not launch_cmd:
        return False, (
            f"No launch command for '{game_id}'.  "
            "Add a 'launch' field in vnflight.json or place a Ren'Py SDK "
            "next to the game directory."
        ), None

    if connect_timeout is not None and float(connect_timeout) <= 0:
        return False, "Launch timeout must be greater than zero.", None

    game_cmd_parts = _parse_launch_cmd(launch_cmd)
    if not game_cmd_parts:
        return False, f"Empty launch command for '{game_id}'.", None
    if game_cmd_parts[0].endswith(".py"):
        game_cmd_parts = [sys.executable] + game_cmd_parts
    is_launcher = any(launch_cmd.startswith(p)
                      for p in ("steam://", "goggalaxy://", "com.epicgames."))
    install_root = _find_game_install_path(game_id, games_dir)
    # See _expected_shim_game_id: a config entry can point at a game_dir it
    # shares with another entry (Ren'Py-version variants), in which case the
    # shim reports the shared directory's name, not this config's own id.
    # Every slot/rejection match below must compare against that id.
    shim_game_id = _expected_shim_game_id(game, game_id)
    if is_launcher:
        launch_probe_error = _probe_launch_file_writable(install_root)
        if launch_probe_error:
            return False, (
                "Cannot launch through Steam/GOG without a writable vnflight "
                f"launch file: {launch_probe_error}. Fix the game install "
                "path or file permissions before retrying."
            ), None

    bridge_script = _find_bridge_script(games_dir)
    if not bridge_script:
        return False, "Cannot find bridge server (vnflight.bridge not importable)", None

    # Parse port from bridge_url.
    parsed = urllib.parse.urlparse(bridge_url)
    port = parsed.port or DEFAULT_BRIDGE_PORT
    host = parsed.hostname or "127.0.0.1"

    # Determine working directory (project root) for resolving relative paths.
    root = Path(games_dir) if games_dir else _find_project_root()

    get_admin_token = getattr(client_state, "get_admin_token", None)
    admin_token = token or (
        get_admin_token(bridge_url) if callable(get_admin_token) else None
    ) or os.environ.get("VNFLIGHT_TOKEN") or None

    # ---- Start bridge ----
    session = BridgeClient(bridge_url, token=admin_token)
    bridge_pid = None
    replacement_game_pids: set[int] = set()
    bridge_was_up = session.is_up()

    def bridge_protocol_error() -> Optional[str]:
        status_reader = getattr(session, "status", None)
        if not callable(status_reader):
            return None
        bridge_status = status_reader(timeout=3.0)
        if not isinstance(bridge_status, dict) or not bridge_status:
            return (
                "The running bridge answered its health check, but its "
                "protocol status could not be read. Retry the launch; if "
                "the bridge remains unavailable, restart the bridge/hub."
            )
        bridge_protocol = bridge_status.get("shim_protocol_version")
        if bridge_protocol == SHIM_PROTOCOL_VERSION:
            return None
        return (
            "Running bridge protocol does not match this vnflight build "
            f"(expected shim protocol {SHIM_PROTOCOL_VERSION}, got "
            f"{bridge_protocol!r}). Restart the bridge/hub from the "
            "current checkout before launching a game."
        )

    if bridge_was_up:
        protocol_error = bridge_protocol_error()
        if protocol_error:
            return False, protocol_error, None
        if replace:
            existing_slot_list = session.list_slots()
            if existing_slot_list is None:
                return False, (
                    "Could not list bridge slots before launching. "
                    "Restart the bridge/MCP session before launching."
                ), None
            replacement_game_pids = _same_game_slot_pids(
                existing_slot_list,
                shim_game_id,
            )
            # Bridge already running — free any stale slot for this game
            # (the shim will re-assign on connect). Don't reset other slots.
            ok, error = _free_existing_game_slot(
                session,
                game_id,
                existing_slots=existing_slot_list,
                client_state=client_state,
                bridge_url=bridge_url,
                match_id=shim_game_id,
            )
            if not ok:
                return False, error, None
    else:
        if admin_token is None:
            admin_token = secrets.token_urlsafe(16)
            session.token = admin_token
        # Start the bridge as a subprocess so it outlives the launcher.
        # Strategy 1: python <this_script> bridge --port N (built single-file)
        # Strategy 2: python -m vnflight.bridge --port N (package)
        # Strategy 3: python <explicit_script> --port N (config override)
        _extra_env: dict[str, str] = {
            # Keep the bridge alive across game restarts by default.
            # Without this, the bridge auto-shuts-down 10s after all
            # games disconnect, forcing each restart to bootstrap a
            # fresh bridge.  Interactive human-in-the-loop workflows
            # need the bridge to survive `stop` → `launch` cycles.
            "VNFLIGHT_KEEP_ALIVE": "1",
        }
        if bridge_script == _BRIDGE_MODULE_SENTINEL:
            # Prefer "self bridge" only for the actual built launcher. Library
            # callers such as diagnostics scripts have their own argv[0] and do
            # not implement the vnflight CLI subcommands.
            _self_script = Path(sys.argv[0]).resolve() if sys.argv else None
            if (
                _self_script
                and _self_script.exists()
                and _self_script.name == "vnflight.py"
            ):
                bridge_cmd = [sys.executable, str(_self_script), "bridge"]
            elif (root / "vnflight.py").exists():
                bridge_cmd = [sys.executable, str(root / "vnflight.py"), "bridge"]
            else:
                bridge_cmd = [sys.executable, "-m", "vnflight.bridge"]
                _src_dir = root / "src" / "vnflight"
                if _src_dir.is_dir():
                    _extra_env["PYTHONPATH"] = str(_src_dir.parent)
        else:
            bridge_cmd = [sys.executable, bridge_script]
        bridge_cmd += [
            "--host", host,
            "--port", str(port),
            "--token=" + admin_token,
        ]
        bridge_log = str(_runtime_log_dir(root) / "bridge_stdout.log")
        bridge_pid, err = _launch_subprocess(
            bridge_cmd, cwd=str(root), log_file=bridge_log,
            extra_env=_extra_env,
        )
        if not bridge_pid:
            return False, f"Failed to start bridge: {err}", None

        # Importing the 1.6 MB single-file artifact takes 4-5 s idle and
        # more on a loaded machine (a full test run alongside pushed it
        # past 10 s on Sep 5): a tight deadline reports a healthy bridge
        # as "did not become ready".
        readiness_deadline = time.monotonic() + BRIDGE_READINESS_TIMEOUT_S
        bridge_ready = False
        while True:
            remaining = readiness_deadline - time.monotonic()
            if remaining <= 0:
                break
            bridge_ready = session.is_up(timeout=min(2.0, remaining))
            if bridge_ready:
                break
            time.sleep(min(0.3, max(0.0, readiness_deadline - time.monotonic())))
        if not bridge_ready:
            kill_process(bridge_pid, BRIDGE_IDENTITY)
            return False, "Bridge server did not become ready in time.", None

        protocol_error = bridge_protocol_error()
        if protocol_error:
            kill_process(bridge_pid, BRIDGE_IDENTITY)
            return False, protocol_error, None

        set_admin_token = getattr(client_state, "set_admin_token", None)
        if callable(set_admin_token):
            set_admin_token(bridge_url, admin_token)
            client_state.save()

        session.reset_bridge()

    # Snapshot existing slot IDs before killing or launching processes.  This
    # lets replacement launch fail closed on slot-list errors without first
    # terminating the previous game.
    pre_launch_slot_list = session.list_slots()
    if pre_launch_slot_list is None:
        return False, (
            "Could not list bridge slots before launching. "
            "Restart the bridge/MCP session before launching."
        ), None
    replacement_game_pids.update(
        _same_game_slot_pids(pre_launch_slot_list, shim_game_id)
    )
    pre_launch_slots = {s.get("slot_id") for s in pre_launch_slot_list}

    # Stop any previous instance of THIS game (not other games).
    # In multi-game mode, other games may be running on the same bridge.
    # Skip when replace=False (multi-instance mode).
    if replace:
        pids = client_state.get_pids(bridge_url)
        if pids:
            # Only kill the old game PID if the bridge attributed it to this
            # game before slot cleanup. Don't touch the bridge — it may be
            # serving other games.
            old_game_pid = pids.get("game")
            try:
                old_game_pid_int = int(old_game_pid) if old_game_pid else None
            except (TypeError, ValueError):
                old_game_pid_int = None
            if (
                old_game_pid_int
                and old_game_pid_int in replacement_game_pids
                and _is_process_alive(old_game_pid_int)
            ):
                old_expect, _legacy_note = pid_identity_from_record(pids, "game")
                if not kill_process(old_game_pid_int, old_expect):
                    return False, (
                        f"Existing '{game_id}' process {old_game_pid_int} "
                        "is still alive after stop request. "
                        "Stop it manually before launching a replacement."
                    ), None

    # ---- Start game ----
    game_log = str(_runtime_log_dir(root) / "game_stdout.log")
    game_extra_env = {}
    if save_slot:
        game_extra_env["VNFLIGHT_SAVE_SLOT"] = save_slot
    # Pass bridge URL to the game so the shim connects to the right port.
    if port != DEFAULT_BRIDGE_PORT:
        game_extra_env["VNFLIGHT_BRIDGE_URL"] = bridge_url
    # Slot access token: the shim sends it as X-Slot-Token, and the bridge
    # reserves the game's slot with it at /slots/assign — after that,
    # reading state or consuming actions on the slot requires this token
    # or the bridge admin token.  ALWAYS minted and ALWAYS persisted in
    # ClientState: the stored admin token can be stale without anything
    # noticing (an open-mode bridge accepts admin ops regardless), and a
    # slot reserved under a stale admin token would otherwise lock out
    # every client on this machine — including the one that launched it.
    # Persisting the slot token is what keeps this launcher (and the
    # long-lived MCP client, via its 403 token refresh) inside its own
    # game on bridges whose admin token it doesn't actually hold, e.g. a
    # bridge started by the harness.
    slot_token: Optional[str] = (
        reservation_token or secrets.token_urlsafe(16)
    )
    launch_id = secrets.token_hex(16)
    # A warm protocol-launched game may consume the handoff before the URL
    # launcher subprocess is spawned, so the correlation window begins before
    # writing the launch file, not at Popen.
    launch_attempt_started_at = time.time()
    game_extra_env["VNFLIGHT_SLOT_TOKEN"] = slot_token
    game_extra_env["VNFLIGHT_LAUNCH_ID"] = launch_id
    # Shim diagnostics: per-game config, overridable per launch.
    effective_debug = (
        bool(debug) if debug is not None else bool((game or {}).get("debug"))
    )
    debug_logs_dir = (game or {}).get("debug_logs") or None
    if effective_debug:
        game_extra_env["VNFLIGHT_DEBUG"] = "1"
    if debug_logs_dir:
        game_extra_env["VNFLIGHT_DEBUG_LOGS"] = str(debug_logs_dir)
    set_slot_token = getattr(client_state, "set_slot_token", None)
    if callable(set_slot_token):
        set_slot_token(bridge_url, game_id, slot_token)
        client_state.save()

    # Launch-file handshake: launcher-mediated games (steam://, goggalaxy://)
    # do NOT inherit game_extra_env — the platform launcher starts the game
    # with ITS environment.  Write the same values into the game's game/
    # directory so the shim can pick them up regardless of launch path.
    launch_file_path, launch_file_warning = _write_launch_file(
        install_root,
        bridge_url,
        slot_token=slot_token,
        save_slot=save_slot,
        launch_id=launch_id,
        debug=effective_debug if (debug is not None or effective_debug) else None,
        debug_logs=debug_logs_dir,
    )
    # A warning no longer implies "not written": the claim-protocol timeout
    # writes the file AND warns, so branch on the path, not the warning.
    if launch_file_warning and not launch_file_path:
        if is_launcher:
            return False, (
                "Cannot launch through Steam/GOG without the vnflight launch "
                f"file: {launch_file_warning}. Fix the game install path or "
                "file permissions before retrying."
            ), None
        print(
            f"  Warning: launch file not written ({launch_file_warning}). "
            "Direct-exe launches still work via env, but Steam/GOG-mediated "
            "launches may not reach this bridge.",
            file=sys.stderr,
        )
    elif launch_file_warning:
        print(f"  Warning: {launch_file_warning}.", file=sys.stderr)
    if launch_file_path:
        add_launch_file = getattr(client_state, "add_launch_file", None)
        if callable(add_launch_file):
            add_launch_file(bridge_url, launch_file_path)
            client_state.save()

    game_pid, err = _launch_subprocess(
        game_cmd_parts, cwd=str(root), log_file=game_log,
        extra_env=game_extra_env if game_extra_env else None,
    )
    if not game_pid:
        return False, (
            f"Failed to start game: {err}. The command comes from the "
            f"'launch' key of '{game_id}' in {CONFIG_FILENAME}; check the "
            "executable path there."
        ), None

    # Save PIDs (cursor reset happens after slot assignment below). Preserve
    # the existing bridge PID when launching another game on an already-running
    # bridge; otherwise stop_game loses the bridge owner after mixed launches.
    pids: Dict[str, Any] = dict(client_state.get_pids(bridge_url) or {})
    pids["game"] = game_pid
    # Identity next to the pid: a later stop (possibly from another checkout
    # sharing this state file) kills only a process that still looks like
    # this game / this bridge.
    pids["game_expect"] = game_identity_expectation(
        game_id, game_cmd_parts, install_root, (game or {}).get("name"))
    if bridge_pid:
        pids["bridge"] = bridge_pid
    if pids.get("bridge"):
        # A preserved bridge pid from an earlier launch is a vnflight bridge
        # too; give it the identity if its record predates tracking.
        pids["bridge_expect"] = BRIDGE_IDENTITY
    client_state.set_pids(bridge_url, pids)
    client_state.save()

    # Wait for the game to connect to the bridge (appear in /slots). Treat
    # connect_timeout as a wall-clock bound: an iteration also contains HTTP,
    # so counting one-second sleeps can multiply the advertised timeout under
    # bridge contention.
    connect_wait_started_at = time.monotonic()
    effective_timeout = (
        90.0 if is_launcher else 30.0
    ) if connect_timeout is None else float(connect_timeout)
    connect_deadline = time.monotonic() + effective_timeout
    final_diagnostic_budget = min(2.0, effective_timeout / 4.0)
    poll_deadline = connect_deadline - final_diagnostic_budget

    def connect_timeout_message() -> str:
        message = "Game did not connect to the bridge in time."
        if is_launcher:
            message += (
                " Steam/GOG-mediated launches do not inherit the launcher's "
                "environment; the installed shim must understand the "
                f"{LAUNCH_FILE_NAME} handshake. If the game's shim predates "
                f"it, re-run `install-shim {game_id} --always-on`; an old "
                "installed shim ignores the launch file and dials the "
                "default bridge port."
            )
        return message

    reservation_id = hashlib.sha256(
        slot_token.encode("utf-8")
    ).hexdigest()[:16]
    receipt_path = (
        _launch_receipt_path(launch_file_path, launch_id)
        if launch_file_path else None
    )
    rejection_session = BridgeClient(bridge_url, token=slot_token)
    last_transient_registration_rejection: Optional[dict] = None

    def read_registration_receipt() -> dict:
        if receipt_path is None:
            return {}
        try:
            data = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if data.get("launch_id") == launch_id else {}

    def remove_registration_receipt() -> None:
        if receipt_path is not None:
            try:
                receipt_path.unlink()
            except OSError:
                pass

    def fail_connection(message: str, slot_id: Any = None):
        remove_registration_receipt()
        if diagnostics is not None:
            diagnostics["connect_failed_after_s"] = round(
                time.monotonic() - connect_wait_started_at, 3
            )
        return False, message, slot_id

    def current_registration_failure(
        rejection_timeout: float,
    ) -> Optional[dict[str, str]]:
        """Return a current launch failure from either diagnostic channel."""
        nonlocal last_transient_registration_rejection
        receipt = read_registration_receipt()
        if receipt.get("status") == "failed":
            reason = str(receipt.get("reason") or "shim registration failed")
            return {"source": "receipt", "reason": reason, "message": reason}
        read_rejection = getattr(
            rejection_session, "registration_rejection", None,
        )
        if callable(read_rejection) and rejection_timeout > 0:
            try:
                rejection_result = read_rejection(
                    timeout=rejection_timeout,
                    game_id=shim_game_id,
                    launch_id=launch_id,
                    launch_started_at=launch_attempt_started_at,
                )
            except TypeError:
                # Test/dummy clients may expose the pre-filter signature.
                rejection_result = read_rejection(timeout=rejection_timeout)
            rejection_read_succeeded = rejection_result is not None
            rejection = rejection_result or {}
            if _registration_rejection_belongs_to_launch(
                rejection,
                game_id=shim_game_id,
                launch_id=launch_id,
                launch_started_at=launch_attempt_started_at,
            ):
                if rejection.get("transient") is True:
                    last_transient_registration_rejection = dict(rejection)
                else:
                    last_transient_registration_rejection = None
            elif rejection_read_succeeded:
                last_transient_registration_rejection = None
            if _registration_rejection_matches(
                rejection,
                game_id=shim_game_id,
                launch_id=launch_id,
                launch_started_at=launch_attempt_started_at,
            ):
                reason = str(
                    rejection.get("reason") or "shim registration failed"
                )
                return {
                    "source": "bridge",
                    "reason": reason,
                    "message": str(rejection.get("message") or reason),
                }
        # The shim can write its process-owned receipt while the bridge read
        # above is in flight. Recheck the local channel before returning.
        receipt = read_registration_receipt()
        if receipt.get("status") == "failed":
            reason = str(receipt.get("reason") or "shim registration failed")
            return {"source": "receipt", "reason": reason, "message": reason}
        return None

    poll_i = 0
    our_slot = None
    registered_slot = None
    mismatched_slots: dict[Any, dict] = {}
    persisted_slot_id = None

    def observe_slot_list(slot_list: list[dict]) -> Optional[str]:
        """Update launch ownership from one authoritative slot snapshot."""
        nonlocal our_slot, registered_slot, persisted_slot_id
        collisions = _normalized_game_id_collision(slot_list, shim_game_id)
        if collisions:
            return (
                f"Ambiguous normalized game id for '{game_id}': "
                f"{', '.join(sorted(collisions))}. Use a slot id or resolve "
                "the bridge slot collision before launching."
            )
        registered_slot = next(
            (s for s in slot_list
             if _game_ids_match(s.get("game_id"), shim_game_id)
             and s.get("slot_id") not in pre_launch_slots
             and s.get("reservation_id") == reservation_id),
            None,
        )
        current_mismatches: dict[Any, dict] = {}
        for candidate in slot_list:
            candidate_id = candidate.get("slot_id")
            if (
                _game_ids_match(candidate.get("game_id"), shim_game_id)
                and candidate_id not in pre_launch_slots
                and candidate.get("reservation_id") != reservation_id
            ):
                current_mismatches[candidate_id] = candidate
        mismatched_slots.clear()
        mismatched_slots.update(current_mismatches)
        if (
            registered_slot
            and registered_slot.get("slot_id") is not None
            and registered_slot.get("slot_id") != persisted_slot_id
            and callable(set_slot_token)
        ):
            persisted_slot_id = registered_slot["slot_id"]
            try:
                set_slot_token(
                    bridge_url, game_id, slot_token,
                    slot_id=persisted_slot_id,
                )
            except TypeError:
                set_slot_token(bridge_url, game_id, slot_token)
            client_state.save()
        if registered_slot and registered_slot.get("event_counter", 0) > 0:
            our_slot = registered_slot
            session.slot_prefix = "/" + str(our_slot["slot_id"])
            game_reported_pid = our_slot.get("game_pid")
            if game_reported_pid:
                pids["game"] = game_reported_pid
                client_state.set_pids(bridge_url, pids)
                client_state.save()
        return None

    while time.monotonic() < poll_deadline:
        remaining = poll_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
        remaining = poll_deadline - time.monotonic()
        if remaining <= 0:
            break
        failure = current_registration_failure(min(1.0, remaining))
        if failure:
            remaining = poll_deadline - time.monotonic()
            if remaining > 0:
                failure_slots = None
                try:
                    failure_slots = session.list_slots(
                        timeout=min(3.0, remaining),
                    )
                except TypeError:
                    if remaining >= 3.0:
                        failure_slots = session.list_slots()
                if failure_slots is not None:
                    failure_slot_error = observe_slot_list(failure_slots or [])
                    if failure_slot_error:
                        return fail_connection(failure_slot_error)
                    if (
                        our_slot
                        and failure["source"] == "bridge"
                    ):
                        late_receipt = read_registration_receipt()
                        if late_receipt.get("status") == "failed":
                            reason = str(
                                late_receipt.get("reason")
                                or "shim registration failed"
                            )
                            return fail_connection(
                                f"Game shim registration failed: {reason}",
                                our_slot.get("slot_id"),
                            )
                        # An exact-reservation ready slot is authoritative. The
                        # successful assignment clears its own diagnostics
                        # atomically, so one read immediately before this
                        # snapshot was stale or belonged to a sibling process.
                        # Failed shim receipts remain authoritative.
                        break
            slot_id = (
                registered_slot.get("slot_id") if registered_slot else None
            )
            return fail_connection(
                f"Game shim registration failed: {failure['message']}", slot_id,
            )
        remaining = poll_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            slot_list = session.list_slots(
                timeout=min(3.0, remaining))
        except TypeError:
            # Test/dummy clients may expose the pre-timeout signature. Never
            # start that fixed 3s request with less than its full budget.
            if remaining < 3.0:
                break
            slot_list = session.list_slots()
        if slot_list is not None:
            slot_error = observe_slot_list(slot_list or [])
            if slot_error:
                return fail_connection(slot_error)
        receipt = read_registration_receipt()
        if receipt.get("status") == "failed":
            reason = receipt.get("reason") or "shim registration failed"
            slot_id = (
                registered_slot.get("slot_id") if registered_slot else None
            )
            return fail_connection(
                f"Game shim registration failed: {reason}", slot_id,
            )
        if our_slot:
            break
        if poll_i == 15 and is_launcher:
            print(
                "  (waiting for launcher to start the game...)",
                file=sys.stderr,
            )
        poll_i += 1

    # Reserve a small piece of the advertised connect budget for one final,
    # gap-free observation. A rejection or receipt can land after the last
    # regular poll, and a slot can become ready during that diagnostic read.
    if not our_slot:
        remaining = max(0.0, connect_deadline - time.monotonic())
        final_slot_error = None
        if remaining > 0:
            final_slot_list = None
            slot_timeout = min(final_diagnostic_budget / 2.0, remaining)
            try:
                final_slot_list = session.list_slots(
                    timeout=slot_timeout,
                )
            except TypeError:
                # Compatibility for test/dummy clients with an unbounded
                # legacy signature. Do not violate the wall-clock contract.
                if slot_timeout >= 3.0:
                    final_slot_list = session.list_slots()
            if final_slot_list is not None:
                final_slot_error = observe_slot_list(final_slot_list or [])
        remaining = max(0.0, connect_deadline - time.monotonic())
        final_failure = current_registration_failure(remaining)
        if final_failure and not (
            our_slot and final_failure["source"] == "bridge"
        ):
            slot_id = (
                registered_slot.get("slot_id") if registered_slot else None
            )
            return fail_connection(
                "Game shim registration failed: "
                f"{final_failure['message']}",
                slot_id,
            )
        if final_slot_error:
            return fail_connection(final_slot_error)

    if our_slot:
        remove_registration_receipt()
    if not our_slot:
        if registered_slot and registered_slot.get("slot_id") is not None:
            slot_id = registered_slot["slot_id"]
            return fail_connection((
                f"Game registered as slot {slot_id}, but did not become "
                "ready before the connection deadline. Stop that slot "
                "before retrying launch."
            ), slot_id)
        receipt = read_registration_receipt()
        launched_pid = pids.get("game")
        receipt_pid = receipt.get("game_pid")
        owned_mismatch = next(
            (s for s in mismatched_slots.values()
             if s.get("game_pid") in (launched_pid, receipt_pid)
             and s.get("game_pid") is not None),
            None,
        )
        if owned_mismatch:
            return fail_connection((
                "Game connected without this launch reservation. Check that "
                "the launch file is writable and reinstall the shim before "
                "retrying."
            ), owned_mismatch.get("slot_id"))
        if mismatched_slots:
            return fail_connection(
                "A new same-game slot connected with a different launch "
                "reservation. It was not adopted or stopped; another launch "
                "may be running concurrently."
            )
        if last_transient_registration_rejection:
            first_seen = float(
                last_transient_registration_rejection.get("first_rejected_at")
                or last_transient_registration_rejection.get("rejected_at")
                or time.time()
            )
            pending_for = max(0, int(time.time() - first_seen))
            reason = (
                last_transient_registration_rejection.get("message")
                or last_transient_registration_rejection.get("reason")
                or "reservation handoff is still pending"
            )
            return fail_connection(
                "Game shim registration is still retrying a reservation "
                f"handoff after {pending_for}s: {reason} Stop the conflicting "
                "slot or restart its bridge before retrying launch."
            )
        return fail_connection(connect_timeout_message())

    # Connection and setup are separate phases. A slot may publish on the
    # final connection poll, but required bridge policy (and the requested
    # launch mode) must still be applied before launch can report success.
    # MCP callers reserve this five-second window in their child deadline.
    setup_deadline = time.monotonic() + 5.0
    assigned_slot = our_slot["slot_id"]
    if diagnostics is not None:
        diagnostics["connected_after_s"] = round(
            time.monotonic() - connect_wait_started_at, 3
        )
    def fail_post_connect_setup(message: str):
        """Report a partial launch without risking deadline-unsafe cleanup.

        Process liveness is not reliably tri-state on Windows: a timed-out
        tasklist probe looks the same as a dead process. Keep the known slot
        and persisted PID intact so the caller has a durable recovery target.
        """
        return False, (
            f"{message} Slot {assigned_slot} is connected but not ready; "
            "stop it before retrying launch."
        ), assigned_slot

    # Reset cursor for this slot's fresh session.
    state_key = bridge_url + session.slot_prefix
    client_state.set_cursor(state_key, 0)
    client_state.set_last_request_id(state_key, None)
    client_state.save()

    # Push per-game bridge config (POST /config) now that the slot exists —
    # e.g. end_on_menu_return=false for games whose gameplay screens are
    # classified as main_menu (Slay the Princess) so the return-to-menu
    # ending heuristic doesn't falsely end the run.
    bridge_config = game.get("bridge_config") or {}
    remaining = setup_deadline - time.monotonic()
    if bridge_config and remaining > 0:
        result = session.set_config(
            bridge_config, deadline=time.time() + remaining)
        if not result.get("ok"):
            return fail_post_connect_setup(
                f"Game connected, but required bridge config "
                f"{bridge_config!r} was not applied."
            )
    elif bridge_config:
        return fail_post_connect_setup(
            "Game connected, but bridge setup timed out.")

    remaining = setup_deadline - time.monotonic()
    if fast_forward and remaining > 0:
        ok, message = session._send_command(
            "fast_forward_on", timeout=min(15.0, remaining))
        if not ok:
            return fail_post_connect_setup(
                f"Game connected, but fast-forward setup failed: {message}")
    elif auto_advance and remaining > 0:
        ok, message = session._send_command(
            "auto_advance_on", timeout=min(15.0, remaining))
        if not ok:
            return fail_post_connect_setup(
                f"Game connected, but auto-advance setup failed: {message}")
    elif fast_forward or auto_advance:
        return fail_post_connect_setup(
            "Game connected, but launch-mode setup timed out.")

    msg = f"Launched '{game_id}'"
    if assigned_slot is not None:
        msg += f" (slot {assigned_slot})"
    if fast_forward:
        msg += " (fast-forward on)"
    if _shim_note:
        msg += f"\n{_shim_note}"
    return True, msg, assigned_slot


def stop_game(bridge_url: str, client_state: ClientState) -> Tuple[bool, str]:
    """Stop the running game and bridge."""
    # Local import to avoid module-level dependency on .client
    from .client import BridgeClient

    get_admin_token = getattr(client_state, "get_admin_token", None)
    admin_token = get_admin_token(bridge_url) if callable(get_admin_token) else None
    session = (
        BridgeClient(bridge_url, token=admin_token)
        if admin_token
        else BridgeClient(bridge_url)
    )
    messages: list[str] = []

    # Snapshot liveness BEFORE the quit command: a process that dies
    # during the post-quit grace sleep exited because our quit worked —
    # that must read as a successful stop, not "already stopped".
    pids = client_state.get_pids(bridge_url) or {}
    alive_before_quit = {
        name: bool(pids.get(name) and _is_process_alive(pids.get(name)))
        for name in ("game", "bridge")
    }

    # Try sending quit command through the bridge.
    quit_sent = False
    if session.is_up():
        session._send_command("quit")
        quit_sent = True
        time.sleep(1.0)

    # Kill processes by PID.
    remaining_pids: dict = {}
    failed = False

    for name in ("game", "bridge"):
        pid = pids.get(name)
        if pid and _is_process_alive(pid):
            expect, legacy_note = pid_identity_from_record(pids, name)
            if kill_process(pid, expect):
                messages.append(f"Stopped {name} (PID {pid}){legacy_note}")
            else:
                failed = True
                remaining_pids[name] = pid
                stored_expect = pids.get(f"{name}_expect")
                if stored_expect:
                    remaining_pids[f"{name}_expect"] = stored_expect
                messages.append(
                    f"Failed to stop {name} (PID {pid}): process still alive "
                    f"or refused (not a matching process){legacy_note}"
                )
        elif pid:
            if quit_sent and alive_before_quit.get(name):
                messages.append(
                    f"Stopped {name} (PID {pid}) — exited cleanly on quit"
                )
            else:
                messages.append(f"{name.title()} (PID {pid}) already stopped")

    client_state.set_pids(bridge_url, remaining_pids if failed else {})
    if not failed:
        clear_slot_tokens = getattr(client_state, "clear_slot_tokens", None)
        if callable(clear_slot_tokens):
            clear_slot_tokens(bridge_url)
    client_state.save()

    # Launch-file hygiene: remove the handshake files written for this
    # bridge's launches.  Best-effort only — the shim's freshness window
    # is the real guard against a stale file steering a later launch.
    get_launch_files = getattr(client_state, "get_launch_files", None)
    if callable(get_launch_files):
        for launch_file in get_launch_files(bridge_url):
            _remove_launch_file_if_owned(launch_file, bridge_url)
        clear_launch_files = getattr(client_state, "clear_launch_files", None)
        if callable(clear_launch_files):
            clear_launch_files(bridge_url)
            client_state.save()

    if not messages:
        return True, "No processes to stop."
    return not failed, "; ".join(messages)


# ---------------------------------------------------------------------------
# Prompt generation
# ---------------------------------------------------------------------------


def generate_prompt(game: dict) -> str:
    """Generate a system prompt for an LLM agent playing a game."""
    briefing = game.get("briefing", "").strip()
    game_id = game.get("id", "unknown")
    game_name = game.get("name", game_id)

    lines = [
        f'You are about to play "{game_name}", a Ren\'Py visual novel.',
        "",
    ]

    if briefing:
        lines.append(briefing)
        lines.append("")

    lines.extend(
        [
            "## Instructions",
            "",
            "You are playing this visual novel using the `vnflight.py` command-line tool.",
            "Your available commands are:",
            "",
            "  python vnflight.py wait          — Watch the story unfold; stops when a choice is needed",
            "  python vnflight.py autoplay      — Enable auto-advance and watch the story unfold",
            "  python vnflight.py act <target> [--wait] — Pick a visible choice or screen button",
            "  python vnflight.py input <text> [--wait] — Provide text input when asked",
            "  python vnflight.py state          — Check current game state (stats, evidence, context)",
            "  python vnflight.py history         — Review recent dialogue",
            "  python vnflight.py cmd <name> [--wait] — Send a game command (start, save, load, etc.)",
            "",
            "## How to Play",
            "",
            "1. Start the game:",
            f"     python vnflight.py launch {game_id} --auto",
            "     python vnflight.py act start --wait",
            "",
            "2. Watch the story and make choices:",
            "     python vnflight.py wait",
            "     (read the output, then when a CHOICE REQUIRED appears:)",
            "     python vnflight.py act 1 --wait",
            "",
            "3. Repeat step 2 until the game ends.",
            "",
            "4. If the game shows buttons on screen (reported in 'state' or as screen_content events),",
            "   select them with the same command:",
            '     python vnflight.py act "Continue" --wait',
            "     python vnflight.py act 2 --wait          (by visible 1-based action index)",
            "",
            "## Important Rules",
            "",
            "- Read dialogue and narration carefully before making choices.",
            "- Consider character motivations and any evidence/stats provided.",
            "- Use 'state' to review your inventory and stats for difficult decisions.",
            "- Do NOT read any game source files (*.rpy, *.rpyc) — this would spoil the story.",
            "- All information you need comes through game events and this briefing.",
            "- Play authentically — make choices based on the narrative, not on trying",
            "  to find a 'best' ending.",
        ]
    )

    return "\n".join(lines)
