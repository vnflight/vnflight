## ============================================================================
## vnflight shim — lets an AI agent (or any bridge client) play this game
## ============================================================================
##
## `vnflight.py install-shim` copies this file into a Ren'Py game's `game/`
## folder. It stays inert until the game is started by `vnflight.py launch`
## (or was installed with --always-on), then talks to the vnflight bridge.
##
## The shim:
##   1. Captures all narrative text (dialogue, narration, scene/show/hide).
##   2. Intercepts menu choices and text input.
##   3. Communicates with a Bridge Server over HTTP.
##   4. Supports hybrid mode: both user and external agent can provide input.
##   5. Auto-advances dialogue with configurable delays.
##   6. Optionally captures screenshots for vision-capable models.
##   7. Supports game commands: start, save, load, rollback, quit.
##   8. Detects main menu and game menu contexts.
##   9. Captures and reports inventory and character stats to the bridge.
##   10. Allows external clients to modify inventory via the `/inventory` endpoint.
##
## Configuration is done via the `vnf_player` object defined below.
## The bridge server should be started before launching the game.
##
## Inventory/Stats:
##   - The `_vnf_get_inventory_stats()` function captures current inventory and stats.
##   - Games can override this function to customize how they expose data.
##   - Inventory/stats are included in choice_request events and can be pushed
##     as periodic updates via `inventory_update` and `stats_update` events.
##   - External clients can modify inventory via `POST /inventory` on the bridge.
##   - Inventory versioning (optimistic locking) prevents race conditions.
## ============================================================================

## We use init offset -990 so we hook early but after Ren'Py's own init.

python early:
    pass

init -990 python:

    import os
    import threading
    import json
    import time

    def _vnf_stringify(value):
        """Return a Py2/Py3-safe text value, or None on conversion failure."""
        if value is None:
            return None
        try:
            if isinstance(value, bytes) and not isinstance(value, type(u"")):
                return value.decode("utf-8", "replace")
        except Exception:
            pass
        if isinstance(value, basestring):
            return value
        try:
            return u"{}".format(value)
        except Exception:
            try:
                # On Python 2, ``str`` may succeed by returning non-ASCII
                # bytes. Route that result through the byte-decoding branch
                # above instead of leaking it into event or UI text.
                return _vnf_stringify(str(value))
            except Exception:
                return None

    def _vnf_text(value, default=u""):
        """Return text suitable for event, UI, and diagnostic paths."""
        result = _vnf_stringify(value)
        return result if result is not None else default

    # Ren'Py rebinds ``dict`` and ``list`` in store code to its revertable
    # container classes, while json.loads and bridge internals still return
    # the native Python types.  Capture the native bases once and use these
    # predicates everywhere; checking against the rebound names rejects valid
    # bridge JSON in-engine even though the same code passes ordinary tests.
    _VNF_NATIVE_DICT_TYPE = type(json.loads("{}"))
    _VNF_NATIVE_LIST_TYPE = type(json.loads("[]"))

    def _vnf_is_mapping(value):
        return isinstance(value, _VNF_NATIVE_DICT_TYPE)

    def _vnf_is_list(value):
        return isinstance(value, _VNF_NATIVE_LIST_TYPE)

    def _vnf_is_sequence(value):
        return _vnf_is_list(value) or isinstance(value, tuple)

    # ------------------------------------------------------------------
    # Shift+R reload guard: store monkey-patch originals on the sys
    # module (guaranteed to survive any reload — sys is never reimported).
    # On first init, saves the real original.  On reload, returns the
    # already-saved original instead of the currently-patched function.
    # ------------------------------------------------------------------
    import sys as _sys_mod
    if not hasattr(_sys_mod, "_vnf_patch_originals"):
        _sys_mod._vnf_patch_originals = {}

    def _vnf_save_original(key, target_obj, attr_name):
        _reg = _sys_mod._vnf_patch_originals
        if key not in _reg:
            _reg[key] = getattr(target_obj, attr_name)
        return _reg[key]
    try:
        import uuid
    except ImportError:
        # Ren'Py 6.x ships a stripped Python without uuid.
        # Provide a minimal fallback using random + time.
        import random as _uuid_random
        class uuid:
            @staticmethod
            def uuid4():
                class _FakeUUID:
                    def __init__(self):
                        self._hex = "%032x" % _uuid_random.getrandbits(128)
                    def __str__(self):
                        return self._hex[:8]
                return _FakeUUID()
    import atexit
    import base64
    import traceback as _tb_module

    # Version detection
    # Extract version number from string like "Ren'Py 7.5.2" or "Ren'Py 8.5.2"
    version_str = renpy.version()
    # Split by space and take the last part (the actual version number)
    version_part = version_str.split()[-1]
    def _vnf_parse_renpy_version(value):
        """Parse Ren'Py versions with build suffixes (e.g. 8.5.0+custom)."""
        parts = []
        for raw in _vnf_text(value).split()[-1].split('.'):
            digits = ""
            for ch in raw:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                parts.append(int(digits))
        return tuple(parts) if parts else (0,)
    renpy_version = _vnf_parse_renpy_version(version_part)

    def _vnf_labelize_action_identifier(value):
        """Convert safe action identifiers into readable button labels."""
        if value is None:
            return ""
        raw = _vnf_text(value).strip()
        if not raw:
            return ""
        lowered = raw.lower()
        known = {
            "start": "Start Game",
            "load": "Load Game",
            "save": "Save Game",
            "preferences": "Preferences",
            "prefs": "Preferences",
            "checklist": "Checklist",
            "bonus": "Bonus",
            "quit": "Quit",
        }
        if lowered in known:
            return known[lowered]
        raw = raw.replace("_", " ").replace("-", " ")
        return " ".join(w[:1].upper() + w[1:] for w in raw.split() if w)

    def _vnf_action_label_hint(action):
        """Best-effort label for image-only buttons with descriptive actions."""
        if action is None:
            return ""
        actions = action if _vnf_is_sequence(action) else [action]
        for a in actions:
            cls_name = a.__class__.__name__
            func = getattr(a, "func", None)
            args = getattr(a, "args", None)
            if getattr(func, "__name__", "") == "_returns" and args:
                return _vnf_labelize_action_identifier(args[0])
            if cls_name == "Start":
                label = getattr(a, "label", None)
                return _vnf_labelize_action_identifier(label or "start")
            if cls_name == "ShowMenu":
                screen = getattr(a, "screen", None)
                return _vnf_labelize_action_identifier(screen)
            if cls_name == "Jump":
                label = getattr(a, "label", None)
                if label:
                    for suffix in ("_nofade_page", "_weekend_page", "_page"):
                        if _vnf_text(label).endswith(suffix):
                            label = _vnf_text(label)[:-len(suffix)]
                            break
                    return _vnf_labelize_action_identifier(label)
            if cls_name in ("Quit", "Save", "Load", "QuickSave", "QuickLoad"):
                return _vnf_labelize_action_identifier(cls_name)
        return ""
    _is_legacy = renpy_version < (8, 0)   # 6.x + 7.x (Python 2, urllib2)
    _is_renpy6 = renpy_version < (7, 0)   # 6.x only (DDLC era: no overlay_screens, interact_callbacks polling)

    # Ren'Py 6.x compat: filter_text_tags doesn't exist before ~7.0.
    # Provide a basic regex fallback that strips {tag} and {tag=val}.
    if not hasattr(renpy.text.extras, "filter_text_tags"):
        import re as _compat_re
        def _vnf_filter_text_tags(s, allow=None, deny=None):
            """Strip Ren'Py text tags (basic fallback for Ren'Py 6.x)."""
            try:
                _value = (s if isinstance(s, basestring)
                          else u"{}".format(s))
            except Exception:
                _value = _vnf_text(s)
            return _compat_re.sub(r'\{[^}]*\}', '', _value)
        renpy.text.extras.filter_text_tags = _vnf_filter_text_tags

    # Ren'Py 6.x compat: get_displayable doesn't exist before ~7.0.
    if not hasattr(renpy.display.screen, "get_displayable"):
        renpy.display.screen.get_displayable = lambda *a, **kw: None

    # urllib compatibility shim
    if _is_legacy:
        import urllib2 as _urllib_request
        import urllib as _urllib_parse
    else:
        import urllib.request as _urllib_request
        import urllib.parse as _urllib_parse
    _HAS_URLLIB = True

    # Ren'Py function shims
    if not hasattr(renpy, "exports"):
        renpy.exports = renpy

    try:
        _EndInteraction = renpy.display.core.EndInteraction
    except AttributeError:  # legacy
        _EndInteraction = getattr(renpy, "EndInteraction", Exception)

    # Ren'Py 8.x provides a convenience tuple; 7.x does not.
    # Build a fallback from the individual exception classes so that
    # `except _CONTROL_EXCEPTIONS` actually catches them on 7.5.2.
    _CONTROL_EXCEPTIONS = getattr(renpy.game, "CONTROL_EXCEPTIONS", None)
    if not _CONTROL_EXCEPTIONS:
        _ctrl = []
        for _exc_name in (
            "JumpException", "JumpOutException", "CallException",
            "FullRestartException", "UtterRestartException",
            "QuitException", "RestartTopContext",
        ):
            _exc_cls = getattr(renpy.game, _exc_name, None)
            if _exc_cls is not None:
                _ctrl.append(_exc_cls)
        # Also include EndInteraction so it is never swallowed.
        if _EndInteraction is not Exception:
            _ctrl.append(_EndInteraction)
        _CONTROL_EXCEPTIONS = tuple(_ctrl) if _ctrl else (Exception,)

    # A successful renpy.load() never returns.  Ren'Py 8.x raises
    # rollback.UnfreezeException, a BaseException that is NOT in
    # CONTROL_EXCEPTIONS; 7.x raises RestartTopContext.  The load handler
    # must treat both as "loaded" (reset, confirm, re-raise).  Catching only
    # _CONTROL_EXCEPTIONS left every Ren'Py 8 load unconfirmed ("submitted
    # but not confirmed") and skipped the after-load reset (Sep 8 2026).
    _VNF_LOAD_SUCCESS_EXCEPTIONS = tuple(
        _c for _c in (
            getattr(getattr(renpy, "rollback", None), "UnfreezeException", None),
            getattr(renpy.game, "UnfreezeException", None),
            getattr(renpy.game, "RestartTopContext", None),
        ) if _c is not None
    ) + tuple(_CONTROL_EXCEPTIONS)

    def _vnf_is_load_success_exception(exc):
        """True for the exception a successful renpy.load() raises."""
        if isinstance(exc, _VNF_LOAD_SUCCESS_EXCEPTIONS):
            return True
        # By name as well: the class lives in renpy.rollback on 8.x and the
        # module attribute may not be reachable through `renpy` at init.
        return type(exc).__name__ in ("UnfreezeException", "RestartTopContext")

    # Periodic callback helper
    def _safe_periodic(callback):
        """Wrap a periodic callback in try/except.

        No supported Ren'Py version runs periodic_callbacks with
        per-callback error handling: on 6.x one unhandled exception
        kills all subsequent callbacks in the same tick, and on
        7.x/8.x it propagates out of interact_core to the error
        screen.  Control-flow exceptions still pass through.
        """
        def _wrapped():
            try:
                callback()
            except (_EndInteraction,) + _CONTROL_EXCEPTIONS:
                raise
            except Exception:
                if vnf_player.debug:
                    _tb_module.print_exc()
        _wrapped.__name__ = getattr(callback, "__name__", "?")
        return _wrapped

    def _register_periodic(callback, interval):
        callback = _safe_periodic(callback)
        if hasattr(renpy, "add_periodic_callback"):
            renpy.add_periodic_callback(callback, interval)
            return
        # periodic_callbacks tick at the display framerate clamp
        # (~20/s); no supported version honors an interval natively,
        # so throttle here.  interval <= 0 means every tick.
        if interval and interval > 0:
            _inner = callback
            _last_run = [0.0]
            def _throttled():
                _now = _time.time()
                if _now - _last_run[0] < interval:
                    return
                _last_run[0] = _now
                _inner()
            _throttled.__name__ = getattr(callback, "__name__", "?")
            callback = _throttled
        renpy.config.periodic_callbacks.append(callback)

    # Last-say accessor (covers both versions)
    def _get_last_say():
        store = renpy.store
        who = getattr(store, "_last_say_who", None)
        what = getattr(store, "_last_say_what", None)

        if who is None and hasattr(store, "_last_say_name"):
            who = getattr(store, "_last_say_name")
        if what is None and hasattr(store, "_last_say_text"):
            what = getattr(store, "_last_say_text")
        return who, what

    # =========================================================================
    # Configuration
    # =========================================================================

    # ------------------------------------------------------------------
    # Launch-file handshake
    #
    # steam://rungameid/... (and other launcher-mediated) games are
    # started by the PLATFORM launcher, not by the vnflight launcher, so
    # the env vars the launcher sets on its spawned process
    # (VNFLIGHT_BRIDGE_URL / VNFLIGHT_SLOT_TOKEN / VNFLIGHT_SAVE_SLOT)
    # never reach this shim.  The launcher therefore also writes
    # game/vnflight_launch.json with the same values at launch time.
    # A FRESH file (written_at within the validity window) is
    # this-launch intent and WINS over env — a warm Steam process tree
    # can carry stale env from an earlier launch.  Absent or stale
    # file -> env -> defaults.
    # ------------------------------------------------------------------

    _VNF_LAUNCH_FILE_NAME = "vnflight_launch.json"
    _VNF_LAUNCH_RECEIPT_PREFIX = "vnflight_registration_"
    _VNF_LAUNCH_FILE_TTL = 900.0  # seconds; an older file is stale
    _vnf_launch_file_note = None  # deferred log line (_vnf_log not defined yet)

    def _vnf_read_launch_file(gamedir=None, now=None):
        """Return the parsed launch-file dict when present and fresh.

        Returns None for absent, stale, or malformed files.  This runs
        during config init and must never crash the shim — any problem
        falls through to the env-var path.
        """
        global _vnf_launch_file_note
        try:
            if gamedir is None:
                gamedir = getattr(renpy.config, "gamedir", None)
            if not gamedir:
                return None
            path = os.path.join(gamedir, _VNF_LAUNCH_FILE_NAME)
            if not os.path.isfile(path):
                return None
            f = open(path, "rb")
            try:
                raw = f.read()
            finally:
                f.close()
            if not isinstance(raw, str):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            # No isinstance(data, dict) here: inside Ren'Py store code
            # the name `dict` is rebound to RevertableDict, while
            # json.loads returns a PLAIN dict — the isinstance check
            # would always fail.  Duck-type on .get instead.
            if not callable(getattr(data, "get", None)):
                _vnf_launch_file_note = (
                    "Launch file ignored (not a JSON object): " + path)
                return None
            try:
                written_at = float(data.get("written_at") or 0)
            except (TypeError, ValueError):
                written_at = 0.0
            try:
                ttl = float(data.get("ttl", _VNF_LAUNCH_FILE_TTL))
            except (TypeError, ValueError):
                ttl = _VNF_LAUNCH_FILE_TTL
            if now is None:
                now = time.time()
            age = now - written_at
            # Reject missing/zero timestamps and clocks far in the
            # future as stale — only a provably-fresh file wins.
            if written_at <= 0 or age > ttl or age < -ttl:
                _vnf_launch_file_note = (
                    "Launch file stale (age {0:.0f}s, ttl {1:.0f}s), "
                    "falling back to env: {2}".format(age, ttl, path))
                return None
            # Claim protocol: there is one launch file per game, so two
            # near-simultaneous launches of the SAME game race — the second
            # write can land inside our boot window and point both copies
            # at one bridge.  Whoever adopts the file stamps its pid into
            # it (see _vnf_claim_launch_file); a file claimed by ANOTHER
            # live process is not ours to use.  Our OWN pid is fine:
            # utter_restart re-runs init and re-reads the same file.
            claimed_by = data.get("claimed_by")
            if claimed_by:
                try:
                    claimed_pid = int(claimed_by)
                except (TypeError, ValueError):
                    claimed_pid = -1
                if claimed_pid != os.getpid():
                    _vnf_launch_file_note = (
                        "Launch file already claimed by pid {0}, falling "
                        "back to env: {1}".format(claimed_by, path))
                    return None
            return data
        except Exception:
            _vnf_launch_file_note = (
                "Launch file unreadable, falling back to env: "
                + _vnf_text(_tb_module.format_exc()))
            return None

    def _vnf_claim_launch_file(data, gamedir=None):
        """Stamp our pid into the launch file so parallel launches serialize.

        Rewrites the file with the SAME payload plus claimed_by/claimed_at.
        written_at is preserved untouched — it is the launcher's freshness
        stamp, not ours.  The launcher waits for this claim before
        overwriting a fresh file, so the sooner it lands the shorter the
        window in which a second launch of this game could steal us.

        Best effort only: this runs during config init and must never
        crash the shim, so every failure just leaves a note and returns
        False.  Losing the claim costs a serialization guarantee, not the
        launch — we already have the values we need in memory.
        """
        global _vnf_launch_file_note
        tmp = None
        try:
            if gamedir is None:
                gamedir = getattr(renpy.config, "gamedir", None)
            if not gamedir:
                return False
            path = os.path.join(gamedir, _VNF_LAUNCH_FILE_NAME)
            # Duck-typed copy (data is a PLAIN dict from json.loads, while
            # `dict` here is RevertableDict -- see the reader's note).
            payload = {}
            _items = getattr(data, "items", None)
            if _items is None:
                return False
            for _k, _v in _items():
                payload[_k] = _v
            payload["claimed_by"] = os.getpid()
            payload["claimed_at"] = time.time()
            tmp = path + ".claim-{0}".format(os.getpid())
            raw = json.dumps(payload)
            if not isinstance(raw, bytes):
                raw = raw.encode("utf-8")
            f = open(tmp, "wb")
            try:
                f.write(raw)
            finally:
                f.close()
            # os.replace is Py3-only; on Py2 (Ren'Py 6/7) os.rename cannot
            # clobber an existing file on Windows, so unlink first.  Both
            # steps are guarded: a lost claim must not abort the launch.
            _replace = getattr(os, "replace", None)
            if _replace is not None:
                _replace(tmp, path)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
                os.rename(tmp, path)
            return True
        except Exception:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            _vnf_launch_file_note = (
                "Launch file claim failed (parallel launches of this game "
                "may collide): " + _vnf_text(_tb_module.format_exc()))
            return False

    _VNFLIGHT_SHIM_PROTOCOL_VERSION = 1

    def _vnf_record_launch_registration(data, status, reason=None, slot_id=None,
                                        gamedir=None):
        """Best-effort registration receipt for the process-owned launch file."""
        if not data or not data.get("launch_id"):
            return False
        tmp = None
        try:
            if gamedir is None:
                gamedir = getattr(renpy.config, "gamedir", None)
            if not gamedir:
                return False
            launch_id = _vnf_text(data.get("launch_id"))
            path = os.path.join(
                gamedir,
                _VNF_LAUNCH_RECEIPT_PREFIX + launch_id + ".json",
            )
            payload = {
                "launch_id": launch_id,
                "status": status,
                "shim_protocol_version": _VNFLIGHT_SHIM_PROTOCOL_VERSION,
                "game_pid": os.getpid(),
                "recorded_at": time.time(),
            }
            if reason:
                payload["reason"] = reason
            if slot_id is not None:
                payload["slot_id"] = slot_id
            tmp = path + ".registration-{0}".format(os.getpid())
            raw = json.dumps(payload)
            if not isinstance(raw, bytes):
                raw = raw.encode("utf-8")
            f = open(tmp, "wb")
            try:
                f.write(raw)
            finally:
                f.close()
            _replace = getattr(os, "replace", None)
            if _replace is not None:
                _replace(tmp, path)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
                os.rename(tmp, path)
            return True
        except Exception:
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            return False

    class VNFPlayerConfig(object):
        """
        Runtime configuration for the LLM Player mod.

        Set these in a script or from the console:
            vnf_player.enabled = True
            vnf_player.bridge_url = "http://localhost:8385"
        """

        def __init__(self):
            # Master switch -- nothing happens if False.
            # Enabled via environment variable to avoid slowing down normal play.
            self.enabled = os.environ.get("VNFLIGHT_ENABLED") == "1"

            # Bridge server URL (no trailing slash).
            # Override with VNFLIGHT_BRIDGE_URL env var for custom port.
            self.bridge_url = os.environ.get("VNFLIGHT_BRIDGE_URL", "http://127.0.0.1:8385")

            # Slot access token.  The launcher passes it via the
            # VNFLIGHT_SLOT_TOKEN env var (alongside VNFLIGHT_ENABLED);
            # the shim sends it as the X-Slot-Token header on every
            # bridge request.  On /slots/assign the bridge reserves the
            # slot with this token, so only holders of the token (or the
            # bridge admin token) can read state or consume actions.
            # Empty/missing = tokenless legacy mode (open local bridges).
            self.slot_token = os.environ.get("VNFLIGHT_SLOT_TOKEN") or None

            # Launch-file handshake: a fresh game/vnflight_launch.json
            # describes THIS launch and wins over (possibly stale) env
            # for bridge_url, slot_token, and save_slot.  See
            # _vnf_read_launch_file above.
            self._launch_file = _vnf_read_launch_file()
            self._launch_id = os.environ.get("VNFLIGHT_LAUNCH_ID") or None
            if self._launch_file is not None:
                if self._launch_file.get("launch_id"):
                    self._launch_id = str(self._launch_file["launch_id"])
                if self._launch_file.get("bridge_url"):
                    self.bridge_url = str(self._launch_file["bridge_url"])
                _lf_token = self._launch_file.get("slot_token")
                self.slot_token = str(_lf_token) if _lf_token else None
                # Take the claim immediately: the launcher waits for it
                # before overwriting a fresh file, so claiming is what
                # serializes two near-simultaneous launches of the SAME
                # game (otherwise the later write lands mid-boot and both
                # copies dial the same bridge).  A claim already present
                # here is our own pid (the reader rejects other pids), so
                # skip the rewrite -- e.g. utter_restart re-running init.
                if self.enabled and not self._launch_file.get("claimed_by"):
                    _vnf_claim_launch_file(self._launch_file)

            # --- Auto-advance ---
            # If True, dialogue (say) interactions are auto-advanced.
            self.auto_advance = False

            # Seconds to wait before auto-advancing a say interaction.
            # Set to 0 for instant advance (useful for headless runs).
            self.auto_advance_delay = 0.3

            # If True, auto-advance is automatically enabled when the
            # game transitions from the main menu to in-game (i.e. the
            # external client does not need to send "auto_advance_on").
            # Useful for hybrid mode where a user is watching but the
            # LLM is driving dialogue progression.
            self.auto_advance_on_start = True

            # --- Infinite pause handling ---
            # Maximum duration (seconds) for bare/infinite pause()
            # statements (delay=None).  These normally block until
            # the player clicks.  Set to a positive number to cap
            # them, or None to leave them infinite (requires manual
            # auto_advance_on or user click to dismiss).
            # When visible text is on screen, the timeout is extended
            # to max(pause_timeout, text_length / reading_cps).
            self.pause_timeout = 5.0

            # --- Single-option choice auto-skip ---
            # If True, menus with exactly one selectable choice are
            # resolved automatically without pushing a choice_request.
            # This eliminates mechanical "act 1" round-trips for
            # what are effectively "continue" prompts.
            self.auto_skip_single_choice = True

            # --- Auto-skip predicate ---
            # Optional callback: fn(label) -> bool.  When set, called
            # BEFORE auto-skipping a single-choice menu.  Return True
            # to allow auto-skip, False to block it (the menu will be
            # pushed as a normal choice_request instead).
            # Use this for menus that act as containers for screen
            # buttons (e.g. Roadwarden's question hubs where the
            # player clicks a button, not the menu choice).
            self.auto_skip_predicate = None

            # --- Auto-skip event filter ---
            # Optional callback: fn(label) -> bool.  When set, called
            # before pushing an auto_skipped event.  Return True to
            # emit the event (narrative action the LLM should see),
            # False to suppress it (internal bookkeeping).
            # Game-specific mods can register a filter at init -989.
            self.auto_skip_event_filter = None

            # --- Auto-skip callback ---
            # Optional callback: fn(label).  Called during auto-skip
            # BEFORE the choice is resolved.  Mods can use this for
            # game-specific cleanup (e.g. clearing NVL buffers for
            # bookkeeping menus that the game expects the player to
            # click through with a natural delay).
            self.auto_skip_callback = None

            # --- Pre-menu callback ---
            # Optional callback: fn(choice_labels, is_nvl).  Called
            # before a multi-choice menu is pushed to the bridge.
            # Mods can use this for game-specific cleanup like
            # deduplicating NVL buffers when a hub re-enters.
            self.pre_menu_callback = None

            # --- Input prompt transform ---
            # Optional callback: fn(prompt, default, screen) -> prompt.
            # Mods can use this to replace game-internal input labels
            # before they are exposed through pending input_request data.
            self.input_prompt_transform = None

            # --- Pre-choice pacing ---
            # Delay before a choice becomes actionable, giving a
            # user observer time to read preceding text.  Applies
            # to ALL choices (single and multi-option) in hybrid
            # mode.  In external-only mode, only applies to
            # auto-skipped single choices.
            #   "off"    — no delay
            #   "text"   — delay based on reading_cps and visible
            #              text length (good for text-heavy games)
            #   "audio"  — wait for the voice channel to finish
            #              (good for voiced games)
            self.pacing = "text"

            # --- Reading speed ---
            # Characters per second for estimating how long a user
            # needs to read on-screen text.  Used by pacing="text".
            # Set to 0 to fall back to auto_advance_delay.
            # Average comfortable reading speed is ~15-20 CPS.
            self.reading_cps = 40

            # --- Dialogue advancement ---
            # "auto" preserves the old split: AFM for user-overridable
            # sessions, instant custom auto-advance for external sessions.
            # "afm" lets Ren'Py advance say lines (best for voiced games).
            # "text" uses vnflight's own text-length timer, including a
            # minimum hold after the final character is visible.
            self.dialogue_advance_mode = "auto"

            # Ren'Py text reveal speed.  0 means all at once.  Profiles can
            # set this to a nonzero value for viewer-facing typewriter text.
            self.text_cps = 0

            # Minimum seconds to keep a line visible after it has fully
            # revealed in dialogue_advance_mode="text".
            self.post_reveal_hold = 0.0

            # --- Fast-forward (external-only) ---
            # If True, removes all text display delays so dialogue
            # appears instantly and auto-advance fires immediately.
            # Intended for external-only mode where no user is watching.
            # Sets: text_cps=0, auto_advance_delay=0, post_action_delay=0,
            #        afm_time=1, and dismisses pause() immediately.
            # (Scene transitions are turbo's job, not this one.)
            self.fast_forward = False

            # --- Turbo (validation playthroughs) ---
            # If True, strip RENDERING delay so an agent can walk a route as
            # fast as the engine allows:
            #   preferences.text_cps  = 0  (instant text reveal)
            #   preferences.transitions = 0  (no scene transitions)
            #   renpy.pause(delay) is clamped to _VNF_TURBO_MAX_PAUSE
            # The pre-turbo values are remembered, so setting turbo back to
            # False restores them exactly (see _vnf_apply_turbo /
            # _vnf_restore_turbo).  Unlike fast_forward this does NOT touch
            # auto-advance or the vnf_player pacing delays, so the two are
            # independent and compose in any order.
            #
            # Normally set through the "turbo" PROFILE (vnflight.json),
            # which bundles this key with the profile's pacing values;
            # set_profile("default") clears it again.
            self.turbo = False

            # --- Delays for observation ---
            # Seconds to pause after an external action is applied.
            self.post_action_delay = 0.2
            # Seconds to highlight a choice before selecting it (hybrid mode).
            # The mouse cursor is moved to the target button for this duration
            # so it gets native hover styling before the choice is confirmed.
            # (Deprecated in favor of dynamic delays below)
            self.choice_highlight_delay = 1.5

            # Dynamic choice delays for hybrid mode.
            # Delay = speed * len(choice_text) + offset
            self.choice_delay_speed_no_scroll = 0.02
            self.choice_delay_offset_no_scroll = 1.2
            self.choice_delay_speed_scroll = 0.05
            self.choice_delay_offset_scroll = 2.5

            # Assumed typing speed (chars per second) for input delay calculation.
            # Used to compute a natural delay: len(text) / typing_speed.
            self.typing_speed = 10.0
            # Extra seconds to wait after the computed typing delay before
            # submitting input (simulates a pause before hitting Enter).
            self.post_typing_delay = 2.0

            # --- Hybrid mode ---
            # If True, the normal GUI is shown alongside external polling.
            # User clicks and external actions race -- first one wins.
            # If False, only external input is accepted (menu is not shown).
            self.allow_user_override = True

            # If True, block mouse clicks during the observation delay so
            # the user cannot accidentally override the LLM's highlighted
            # choice.  The cursor is also parked in a dead zone to prevent
            # hover tooltips.  Set to False to allow click-through.
            self.lock_input_during_observation = True

            # If True, visual observation parks the operating-system pointer on
            # highlighted controls. Disable for unattended parallel runs: SDL
            # cursor warps are host-global when a fleet window briefly gains
            # focus, and can make another game misclassify shim output as user.
            # Internal focus and action execution do not depend on the warp.
            _pointer_setting = os.environ.get("VNFLIGHT_MOVE_HOST_POINTER")
            if (
                self._launch_file is not None
                and "move_host_pointer" in self._launch_file
            ):
                _pointer_setting = self._launch_file.get("move_host_pointer")
            if _pointer_setting is None:
                self.move_host_pointer = True
            elif isinstance(_pointer_setting, (str, bytes)):
                self.move_host_pointer = _pointer_setting.strip().lower() not in (
                    "0", "false", "no", "off", "")
            else:
                self.move_host_pointer = bool(_pointer_setting)

            # If True, suppress default focus on choice buttons during
            # active requests (prevents the first choice from appearing
            # highlighted before the LLM picks one).  Disable for games
            # that rely on hover-driven tooltips, since suppression
            # interferes with the focus/unfocus lifecycle.
            self.suppress_default_focus = True

            # If True, screen-button actions highlight the target button and
            # wait before firing, mirroring the choice observation pipeline.
            self.observation_delay_clicks = True
            # Delay = click_delay_speed * len(button_label) + click_delay_offset
            self.click_delay_speed = 0.02
            self.click_delay_offset = 0.3

            # If True, transforms can define pre_resolve_steps that
            # execute visually (highlight + click) before the menu resolves.
            self.pre_resolve_enabled = True
            # How long each pre_resolve step highlights before firing.
            self.pre_resolve_step_delay = 0.5

            # If True, cross-reference menu choices against rendered
            # ChoiceReturn buttons after the menu renders.  Choices
            # not visible on screen (e.g. class-gated) are filtered
            # out and a replacement request is pushed.
            self.filter_hidden_choices = False
            # Optional callable to override vis-check filtering.
            # Signature: fn(pending_data, rendered_labels, hidden_labels,
            #               scraped_buttons)
            # Return False to skip filtering (keep all choices).
            # Return True (or None) to proceed with normal filtering.
            # Set by game mods that need to disable filtering when
            # state-toggling buttons (e.g. class actions) are present.
            self.vis_check_filter = None

            # Seconds to wait for an external action before giving up.
            # Only used when allow_user_override is False.
            self.action_timeout = 120.0

            # --- Screenshots ---
            # If True, capture a screenshot and push it on each interaction.
            self.screenshot_enabled = True

            # Screenshot size (width, height) or None for native resolution.
            self.screenshot_size = (640, 360)

            # When to capture: "interaction", "scene_change", or "both".
            self.screenshot_on = "both"

            # Minimum seconds between screenshot PUSHES.  Under auto-advance
            # and restart_interaction churn the interaction/scene-change
            # callbacks can fire ~20x/sec; this gate keeps the bridge from
            # being saturated.  A vision-heavy profile can lower it; the
            # manual `screenshot` command (force=True) always bypasses it.
            self.screenshot_min_interval = 1.0

            # --- Polling ---
            # How often (seconds) to poll the bridge for external actions
            # during a menu/input interaction.
            self.poll_interval = 0.15

            # How often (seconds) to poll for commands.
            self.command_poll_interval = 0.25

            # --- Logging ---
            # If True, _vnf_log prints to stdout / Ren'Py log AND appends to
            # a per-session file under debug_logs (default: the game's own
            # directory). Off on the golden path: VNFLIGHT_DEBUG=1 or the
            # launch file's "debug" turn it on (per-game "debug" in
            # vnflight.json, overridable with launch --debug/--no-debug).
            self.debug = os.environ.get("VNFLIGHT_DEBUG") == "1"
            self.debug_logs = os.environ.get("VNFLIGHT_DEBUG_LOGS") or None
            if self._launch_file is not None:
                if "debug" in self._launch_file:
                    self.debug = bool(self._launch_file.get("debug"))
                if self._launch_file.get("debug_logs"):
                    self.debug_logs = _vnf_text(self._launch_file["debug_logs"])

            # --- Screen Scraping ---
            # If True, `call screen` is intercepted and scraped for text/choices.
            # This allows the LLM to drive custom GUIs (like frame-based menus).
            # Off by default — enable in game-specific mods that need it.
            self.scrape_screens = False  # Emit screen_content events (texts/buttons)
            # Delay before scraping to allow screen animations/init to complete.
            self.scrape_screens_delay = 0.5
            # Adaptive scrape backoff: when True, scrape frequency reduces
            # when idle (no actions, unchanged content).  Disabled by default.
            self.adaptive_scrape = False

            # --- Dialogue Dedup ---
            # Suppress consecutive identical dialogue pushes (same speaker
            # + text within a short window).  Catches Ren'Py re-displaying
            # the last say as menu context.  Default True.
            self.dialogue_dedup = True

            # --- NVL Auto-Scroll ---
            # If True, automatically scroll NVL viewports when text
            # overflows the visible area.  Smooth scroll starts after
            # a short initial delay so the reader can absorb the first
            # lines.
            self.nvl_auto_scroll = True
            # Seconds to wait before auto-scroll begins.
            self.nvl_auto_scroll_delay = 4.0
            # Pixels per second to scroll.  0 = derive from reading_cps.
            self.nvl_auto_scroll_speed = 0

            # --- Visible Screen Scraping ---
            # If True, an interact callback periodically scrapes currently
            # visible screens for text content and pushes `screen_content`
            # events to the bridge.  This captures embedded screens (e.g.
            # a random-quote frame inside `navigation` / `main_menu`) that
            # are never invoked via `call screen`.
            self.scrape_visible_screens = False
            # Which screens to scrape.  None means auto-detect based on
            # context (main_menu, navigation, game_menu).  Set to a list
            # of screen names to override, e.g. ["main_menu", "navigation"].
            self.scrape_visible_list = None
            # Extra screens to ALWAYS include alongside whatever the
            # scraper discovers (e.g. overlay screens like "selling",
            # "map_display" that appear alongside call_screen menus).
            self.scrape_extra_screens = []

            # --- Menu index guard ---
            # If True, emit an anomaly event when the bridge returns a
            # choice index that does not exist in value_map.  Without
            # this, an out-of-range index silently fails (hybrid mode)
            # or loops until timeout (external mode).  When enabled,
            # the guard falls back to the nearest valid index.
            # Intended to be enabled by the watchdog mod.
            self.menu_indexerror_guard = False

            # --- Stat Exposure Mode ---
            # Controls how inventory/stats are exposed to the LLM.
            # "normal" - Only player-visible stats (what UI shows)
            # "debug"  - All internal variables including hidden counters, quest flags, etc.
            # Games can override this via their shim files if they support dual-mode exposure.
            self.stat_mode = "normal"  # options: "normal", "debug"

            # --- Save Slot ---
            # Isolate saves per agent/slot. When set, Ren'Py's save
            # directory is redirected to a subdirectory.
            # Resolution: launch file (fresh) → env var VNFLIGHT_SAVE_SLOT
            # → bridge command.
            # Empty string = use default save directory (no isolation).
            self.save_slot = os.environ.get("VNFLIGHT_SAVE_SLOT", "")
            if self._launch_file is not None:
                _lf_slot = self._launch_file.get("save_slot")
                self.save_slot = str(_lf_slot) if _lf_slot else ""

        def __repr__(self):
            attrs = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            lines = ["VNFPlayerConfig:"]
            for k, v in sorted(attrs.items()):
                lines.append("  {} = {!r}".format(k, v))
            return "\n".join(lines)

    # =========================================================================
    # Inventory/Stats Capture
    # =========================================================================

    def _vnf_get_inventory_stats():
        """
        Returns (inventory, stats) for the current game state.
        This function can be overridden by games to customize how they
        expose their inventory and stats data.

        Returns:
            inventory: list of dicts with keys like 'name', 'quantity', 'description'
            stats: dict of stat_name -> value
        """
        try:
            inventory = []

            # Check for inventory list
            if hasattr(renpy.store, "inventory"):
                inv = renpy.store.inventory
                if _vnf_is_sequence(inv):
                    for item in inv:
                        if hasattr(item, "name"):
                            inventory.append({
                                "name": _vnf_text(item.name),
                                "quantity": getattr(item, "quantity", 1),
                                "description": getattr(item, "description", "")
                            })
                        else:
                            inventory.append({"name": _vnf_text(item)})
                elif _vnf_is_mapping(inv):
                    for name, qty in inv.items():
                        inventory.append({
                            "name": _vnf_text(name),
                            "quantity": qty
                        })

            # Check for individual stats
            stats = {}
            stat_vars = ["strength", "dexterity", "intelligence", "charisma",
                         "hp", "max_hp", "energy", "money", "gold", "faith", "sanity"]
            for var in stat_vars:
                if hasattr(renpy.store, var):
                    stats[var] = getattr(renpy.store, var)

            # Check for stats object
            if hasattr(renpy.store, "stats") and hasattr(renpy.store.stats, "__dict__"):
                for k, v in renpy.store.stats.__dict__.items():
                    if not k.startswith("_"):
                        stats[k] = v

            return inventory, stats
        except Exception:
            if vnf_player.debug:
                _vnf_log("Error getting inventory/stats: " + _vnf_text(_tb_module.format_exc()))
            return [], {}

    def _vnf_apply_inventory_changes(changes):
        """
        Apply inventory modifications from an external client.

        This function can be overridden by games to customize how
        inventory changes are applied to their data structures.

        Args:
            changes: list of dicts, each with:
                - "action": "add", "remove", or "clear"
                - "item": str or dict describing the item (for add/remove)

        Returns:
            dict with "success" (bool) and "message" (str)
        """
        try:
            inv = getattr(renpy.store, "inventory", None)
            if inv is None:
                return {"success": False, "message": "No inventory variable found on renpy.store"}

            for change in changes:
                if not _vnf_is_mapping(change):
                    continue
                action = change.get("action", "")
                item = change.get("item", "")

                if action == "add":
                    if _vnf_is_list(inv):
                        if _vnf_is_mapping(item):
                            inv.append(item)
                        else:
                            inv.append(_vnf_text(item))
                    elif _vnf_is_mapping(inv):
                        name = (item if isinstance(item, basestring)
                                else item.get("name", _vnf_text(item))
                                if _vnf_is_mapping(item) else _vnf_text(item))
                        qty = item.get("quantity", 1) if _vnf_is_mapping(item) else 1
                        inv[name] = inv.get(name, 0) + qty

                elif action == "remove":
                    name = (item if isinstance(item, basestring)
                            else item.get("name", _vnf_text(item))
                            if _vnf_is_mapping(item) else _vnf_text(item))
                    if _vnf_is_list(inv):
                        inv[:] = [i for i in inv if (
                            (getattr(i, "name", None) != name) if hasattr(i, "name")
                            else (_vnf_text(i) != name and i.get("name", None) != name if _vnf_is_mapping(i) else _vnf_text(i) != name)
                        )]
                    elif _vnf_is_mapping(inv):
                        inv.pop(name, None)

                elif action == "clear":
                    if _vnf_is_list(inv):
                        inv[:] = []
                    elif _vnf_is_mapping(inv):
                        inv.clear()

            return {"success": True, "message": "Applied {} change(s)".format(len(changes))}
        except Exception as e:
            return {"success": False, "message": _vnf_text(e)}

    def _vnf_apply_stats_changes(changes):
        """
        Apply stat modifications from an external client.

        This function can be overridden by games to customize how
        stat changes are applied to their data structures.

        Args:
            changes: dict of stat_name -> new_value

        Returns:
            dict with "success" (bool) and "message" (str)
        """
        try:
            applied = []
            for name, value in changes.items():
                if hasattr(renpy.store, name):
                    setattr(renpy.store, name, value)
                    applied.append(name)

            if applied:
                return {"success": True, "message": "Set {} stat(s): {}".format(len(applied), ", ".join(applied))}
            else:
                return {"success": False, "message": "No matching stat variables found"}
        except Exception as e:
            return {"success": False, "message": _vnf_text(e)}

    # =========================================================================
    # Game Progress Tracking
    # =========================================================================
    #
    # Two data sources, one interpreter:
    #   1. Passive checkers — read game variables to detect state.
    #   2. Active tracker — hooks label callbacks to log visited labels.
    #   3. Interpreter — game mod combines both into a progress report.
    #
    # Mods register via:
    #   _vnf_add_progress_checker(fn)       # fn() -> dict
    #   _vnf_set_progress_interpreter(fn)   # fn(passive, active) -> dict
    # The active label tracker is built-in (no mod needed).

    _vnf_progress_checkers = []    # list of fn() -> dict
    _vnf_progress_interpreter = None  # fn(passive_results, label_history, graph_state) -> dict
    _vnf_label_history = []        # list of (label, timestamp)
    _vnf_current_progress_node = None  # last known graph node (for change detection)
    _VNF_MAX_LABEL_HISTORY = 500   # cap to prevent unbounded growth

    # --- Progress Graph ---
    # Nodes: {name: {"check": fn, "next": [names], "terminal": bool,
    #                 "game_terminal": bool, "thread": str, "phase": str,
    #                 "label": str, "label_trigger": bool}}
    # "check" is optional — if omitted, node is reached by label tracking alone.
    # "next" defines edges to possible successor nodes.
    # "terminal" marks graph-thread endpoints; "game_terminal" marks endings.
    # "label" is a human-readable description.
    _vnf_progress_graph = {}       # node_name -> node_dict

    def _vnf_set_progress_graph(graph):
        """Set the progress graph. Each node:
            {"check": fn() -> bool, "next": [node_names], "terminal": bool, "label": str}
        check and terminal are optional. label defaults to node name."""
        global _vnf_progress_graph
        _vnf_progress_graph = graph

    def _vnf_add_progress_node(name, check=None, next_nodes=None,
                               terminal=False, label=None, thread="main",
                               phase=None, game_terminal=False,
                               label_trigger=True):
        """Add or update a single node in the progress graph."""
        _vnf_progress_graph[name] = {
            "check": check,
            "next": next_nodes or [],
            "terminal": terminal,
            "game_terminal": game_terminal,
            "thread": thread or "main",
            "phase": phase,
            "label": label or name,
            "label_trigger": label_trigger,
        }

    def _vnf_resolve_graph_state():
        """Walk the progress graph using label history and passive checks."""
        if not _vnf_progress_graph:
            return {
                "nodes": {
                    "visited": [],
                    "active": [],
                    "completed": [],
                    "terminal": [],
                },
                "threads": {},
                "available_next": [],
                "completion": 0.0,
                "game_terminal": False,
            }

        # Set of labels the game has visited.
        visited_labels = set(l for l, t in _vnf_label_history)

        # Walk graph: a node is "visited" if its name is in label history
        # OR its check function returns True.
        visited_nodes = set()
        for name, node in _vnf_progress_graph.items():
            check_fn = node.get("check")
            if node.get("label_trigger", True) and name in visited_labels:
                visited_nodes.add(name)
            elif check_fn:
                try:
                    if check_fn():
                        visited_nodes.add(name)
                except Exception:
                    pass

        active_nodes = set()
        completed_nodes = set()
        terminal_nodes = set()
        game_terminal = False
        threads = {}
        available_next = []

        for name in visited_nodes:
            node = _vnf_progress_graph.get(name, {})
            thread = node.get("thread") or "main"
            thread_state = threads.setdefault(thread, {
                "active": [],
                "completed": [],
                "terminal": False,
            })
            if node.get("terminal"):
                terminal_nodes.add(name)
                completed_nodes.add(name)
                thread_state["terminal"] = True
                if node.get("game_terminal"):
                    game_terminal = True
                continue

            next_nodes = node.get("next", []) or []
            next_visited = [
                n for n in next_nodes
                if n in visited_nodes
            ]
            if next_visited:
                completed_nodes.add(name)
            else:
                active_nodes.add(name)
                for n in next_nodes:
                    next_node = _vnf_progress_graph.get(n, {})
                    available_next.append({
                        "from": name,
                        "thread": thread,
                        "node": n,
                        "visited": n in visited_nodes,
                        "label": next_node.get("label", n),
                        "phase": next_node.get("phase"),
                        "terminal": bool(next_node.get("terminal", False)),
                        "game_terminal": bool(next_node.get("game_terminal", False)),
                    })

        for name in active_nodes:
            node = _vnf_progress_graph.get(name, {})
            thread = node.get("thread") or "main"
            threads.setdefault(thread, {
                "active": [],
                "completed": [],
                "terminal": False,
            })["active"].append(name)
        for name in completed_nodes:
            node = _vnf_progress_graph.get(name, {})
            thread = node.get("thread") or "main"
            threads.setdefault(thread, {
                "active": [],
                "completed": [],
                "terminal": False,
            })["completed"].append(name)

        total = len(_vnf_progress_graph)
        completion = float(len(visited_nodes)) / float(total) if total > 0 else 0.0

        return {
            "nodes": {
                "visited": sorted(list(visited_nodes)),
                "active": sorted(list(active_nodes)),
                "completed": sorted(list(completed_nodes)),
                "terminal": sorted(list(terminal_nodes)),
            },
            "threads": threads,
            "available_next": available_next,
            "completion": round(completion, 3),
            "game_terminal": game_terminal,
        }

    def _vnf_add_progress_checker(fn):
        """Register a passive progress checker. Called with no args, returns a dict
        describing current game state (e.g. {"chapter": 3, "quest": "find_tribe"})."""
        _vnf_progress_checkers.append(fn)

    def _vnf_set_progress_interpreter(fn):
        """Register the progress interpreter. Takes (passive_results, label_history, graph_state)
        and returns a unified progress dict. Only one interpreter active at a time."""
        global _vnf_progress_interpreter
        _vnf_progress_interpreter = fn

    def _vnf_on_label(label_name, abnormal=False):
        """Label callback — records visited labels and emits progress_change events."""
        global _vnf_current_progress_node
        if not vnf_player.enabled:
            return
        # Filter internal/system labels.
        if label_name and not label_name.startswith("_"):
            now = _time.time()
            _vnf_label_history.append((label_name, now))
            # Cap history length.
            if len(_vnf_label_history) > _VNF_MAX_LABEL_HISTORY:
                _vnf_label_history[:] = _vnf_label_history[-_VNF_MAX_LABEL_HISTORY:]

            # Emit progress_change event on graph node transitions.
            if (
                    label_name in _vnf_progress_graph
                    and _vnf_progress_graph[label_name].get(
                        "label_trigger", True)
                    and label_name != _vnf_current_progress_node):
                old_node = _vnf_current_progress_node
                _vnf_current_progress_node = label_name
                node = _vnf_progress_graph[label_name]
                _ev = {
                    "type": "progress_change",
                    "from": old_node,
                    "to": label_name,
                    "label": node.get("label", label_name),
                    "terminal": node.get("terminal", False),
                    "game_terminal": node.get("game_terminal", False),
                    "thread": node.get("thread", "main"),
                    "phase": node.get("phase"),
                    "timestamp": now,
                }
                _vnf_client.push_event(_ev)

    # Register the label callback with Ren'Py.
    if hasattr(renpy.config, "label_callbacks"):
        if _vnf_on_label not in renpy.config.label_callbacks:
            renpy.config.label_callbacks.append(_vnf_on_label)

    def _vnf_get_progress():
        """Get current game progress from all registered sources.

        Returns a dict with:
          - passive: list of dicts from passive checkers
          - active: list of (label, timestamp) from label tracker
          - graph: dict with current_node, visited, available_next, completion
          - interpreted: dict from the interpreter (if registered)
          - terminal: bool — True if the game has reached an ending
        """
        # Collect passive checker results.
        passive_results = []
        for fn in _vnf_progress_checkers:
            try:
                result = fn()
                if result:
                    passive_results.append(result)
            except Exception:
                pass

        # Active: label history.
        active = list(_vnf_label_history)

        # Graph: walk the progress graph.
        graph_state = _vnf_resolve_graph_state()

        # Interpret if a game mod provided an interpreter.
        interpreted = {}
        terminal = False
        if _vnf_progress_interpreter:
            try:
                interpreted = _vnf_progress_interpreter(passive_results, active, graph_state) or {}
                terminal = bool(
                    interpreted.get("game_terminal", interpreted.get("terminal", False))
                )
            except Exception:
                pass
        else:
            # Default: merge passive results + graph terminal.
            for r in passive_results:
                interpreted.update(r)
                if r.get("game_terminal", r.get("terminal")):
                    terminal = True
            if graph_state.get("game_terminal"):
                terminal = True
            interpreted["nodes"] = graph_state.get("nodes", {})
            interpreted["threads"] = graph_state.get("threads", {})
            interpreted["available_next"] = graph_state.get("available_next", [])
            interpreted["completion"] = graph_state.get("completion", 0.0)
            interpreted["game_terminal"] = terminal

        return {
            "passive": passive_results,
            "active": active,
            "graph": graph_state,
            "interpreted": interpreted,
            "terminal": terminal,
        }

    # =========================================================================
    # Bridge Client
    # =========================================================================

    class VNFBridgeClient(object):
        """
        Handles HTTP communication with the Bridge Server.
        All network calls are wrapped so they never crash the game.
        """

        POST_ACCEPTANCE_UNKNOWN = object()

        def __init__(self, config):
            self._cfg = config
            self._lock = threading.Lock()
            self.slot_id = None
            # Every bridge mutation shares one ordered outbox.  The old
            # fire-and-forget path created a daemon thread per event, so a
            # later request/state sample could arrive before the narration
            # that caused it.  One worker preserves Ren'Py emission order
            # without blocking the interaction thread for ordinary events.
            self._outbox_condition = threading.Condition()
            self._outbox = _VNF_NATIVE_LIST_TYPE()
            # Bound retained mutations when a bridge accepts TCP but stops
            # answering. Screenshots use their own latest-frame lane below,
            # so ordinary play never fills this with large image payloads.
            self._outbox_limit = 64
            self._outbox_failure_streak = 0
            self._request_retry_cancelled = set()
            self._request_retry_threads = _VNF_NATIVE_DICT_TYPE()
            # Request closures and nonce-bearing command receipts are ordered
            # barriers. One retained item per stable identity retries in place,
            # so a failed receipt cannot move behind later story output.
            self._critical_event_items = _VNF_NATIVE_DICT_TYPE()
            self._critical_event_limit = 64
            self._source_id = str(uuid.uuid4())
            self._source_event_serial = 0
            self._outbox_worker_thread = threading.Thread(
                target=self._outbox_worker)
            self._outbox_worker_thread.daemon = True
            self._outbox_worker_thread.start()
            self._screenshot_outbox_condition = threading.Condition()
            self._screenshot_outbox = _VNF_NATIVE_LIST_TYPE()
            self._screenshot_source_id = str(uuid.uuid4())
            self._screenshot_source_serial = 0
            self._screenshot_worker_thread = threading.Thread(
                target=self._screenshot_outbox_worker)
            self._screenshot_worker_thread.daemon = True
            self._screenshot_worker_thread.start()
            # Derive game_id from the parent of the game directory.
            # renpy.config.gamedir is e.g. ".../echoes_of_tomorrow/game",
            # so we want the parent directory name as game_id.
            try:
                import renpy.config as _rc
                raw_id = os.path.basename(os.path.dirname(_rc.gamedir))
                self.game_id = raw_id.lower().replace(" ", "_")
            except Exception:
                self.game_id = "unknown"

        # -- low level --

        def _url(self, path):
            base = self._cfg.bridge_url.rstrip("/")
            if self.slot_id is not None:
                return base + "/" + str(self.slot_id) + path
            return base + path

        def _admin_url(self, path):
            """URL for admin endpoints (no slot prefix)."""
            return self._cfg.bridge_url.rstrip("/") + path

        def _auth_headers(self, with_content_type=True):
            """Request headers, including the slot token when configured.

            The bridge gates reserved slots on the X-Slot-Token header;
            the launcher hands the token to the game process via the
            VNFLIGHT_SLOT_TOKEN env var. Py2/Py3 compatible (plain dict
            passed to urllib2.Request / urllib.request.Request).
            """
            headers = {}
            if with_content_type:
                headers["Content-Type"] = "application/json"
            headers["X-VNFlight-Shim-Protocol"] = str(
                _VNFLIGHT_SHIM_PROTOCOL_VERSION)
            token = getattr(self._cfg, "slot_token", None)
            if token:
                headers["X-Slot-Token"] = token
            return headers

        def assign_slot(self, disable_on_failure=False, retry_window=0.0,
                        registration_context=None):
            """Request a slot, retrying only transient startup failures."""
            if not _HAS_URLLIB or not self._cfg.enabled:
                return False
            _time = __import__("time")
            _record_registration = globals().get(
                "_vnf_record_launch_registration", lambda *args, **kwargs: False)
            _launch_file = (registration_context if registration_context is not None
                            else getattr(self._cfg, "_launch_file", None))
            deadline = _time.time() + max(float(retry_window or 0.0), 0.0)
            _assignment = {
                "game_id": self.game_id,
                "game_pid": os.getpid(),
                "shim_protocol_version": _VNFLIGHT_SHIM_PROTOCOL_VERSION,
            }
            if registration_context is not None:
                _assignment["registration_retry_mode"] = "recovery"
            elif retry_window:
                _assignment["registration_retry_mode"] = "finite"
                _assignment["registration_retry_until"] = deadline
            else:
                _assignment["registration_retry_mode"] = "single"
            _assignment_launch_id = (
                _launch_file.get("launch_id") if _launch_file else None)
            if _assignment_launch_id is None and registration_context is None:
                _assignment_launch_id = getattr(self._cfg, "_launch_id", None)
            if _assignment_launch_id:
                _assignment["launch_id"] = str(_assignment_launch_id)
            body = json.dumps(_assignment).encode("utf-8")
            last_error = None
            while True:
                remaining = deadline - _time.time()
                if retry_window and last_error is not None and remaining <= 0:
                    break
                timeout = 3.0
                if retry_window:
                    timeout = max(0.001, min(timeout, remaining))
                try:
                    req = _urllib_request.Request(
                        self._admin_url("/slots/assign"),
                        data=body,
                        headers=self._auth_headers(),
                    )
                    if _is_legacy:
                        req.get_method = lambda: "POST"
                    else:
                        req.method = "POST"
                    resp = _urllib_request.urlopen(req, timeout=timeout)
                    try:
                        data = json.loads(resp.read().decode("utf-8"))
                        # Ren'Py replaces the store-level ``dict`` name with
                        # RevertableDict, while json.loads returns a native
                        # Python dict.  Validate the mapping contract instead
                        # of rejecting a successful assignment by type.
                        if not hasattr(data, "get"):
                            raise ValueError("assignment response is not an object")
                    except Exception as parse_error:
                        reason = "Malformed slot assignment response: %s" % (
                            _vnf_text(parse_error),)
                        _vnf_log(reason)
                        _record_registration(
                            _launch_file, "failed", reason=reason)
                        if disable_on_failure:
                            self._cfg.enabled = False
                        return False
                    _bridge_protocol = data.get("shim_protocol_version")
                    if _bridge_protocol != _VNFLIGHT_SHIM_PROTOCOL_VERSION:
                        reason = (
                            "Bridge protocol mismatch (shim %s, bridge %r). "
                            "Restart the bridge from the current vnflight checkout."
                            % (_VNFLIGHT_SHIM_PROTOCOL_VERSION, _bridge_protocol)
                        )
                        _vnf_log(reason)
                        _record_registration(
                            _launch_file, "failed", reason=reason)
                        if disable_on_failure:
                            self._cfg.enabled = False
                        return False
                    self.slot_id = data.get("slot_id")
                    if self.slot_id is None:
                        reason = "Bridge returned no slot id during assignment."
                        _vnf_log(reason)
                        _record_registration(
                            _launch_file, "failed", reason=reason)
                        if disable_on_failure:
                            self._cfg.enabled = False
                        return False
                    _record_registration(
                        _launch_file, "assigned", slot_id=self.slot_id)
                    _vnf_log("Assigned slot %s for game '%s'" % (
                        self.slot_id, self.game_id))
                    return True
                except Exception as e:
                    last_error = e
                    code = int(getattr(e, "code", 0) or 0)
                    error_data = {}
                    if code:
                        try:
                            error_data = json.loads(e.read().decode("utf-8"))
                        except Exception:
                            pass
                    # A reservation can briefly remain owned by the slot that
                    # a warm game is recovering from. The bridge labels that
                    # conflict explicitly; retry it just like capacity rather
                    # than publishing a failed launch while the shim remains
                    # able to recover on its next poll.
                    reservation_pending = (
                        code == 409 and error_data.get("status") == "reserved")
                    terminal = (
                        code in (400, 401, 403, 404, 409, 410, 422)
                        and not reservation_pending)
                    if terminal:
                        reason = error_data.get("error") or _vnf_text(
                            e, "slot assignment refused")
                        _vnf_log(
                            "Slot assignment refused (shim protocol %s): %s"
                            % (_VNFLIGHT_SHIM_PROTOCOL_VERSION, reason)
                        )
                        _record_registration(
                            _launch_file, "failed", reason=reason)
                        if disable_on_failure:
                            self._cfg.enabled = False
                        return False
                    if not retry_window or _time.time() >= deadline:
                        break
                    _time.sleep(min(0.25, max(0.0, deadline - _time.time())))

            reason = "Slot assignment failed after bounded retries: %s" % (
                _vnf_text(last_error, "unknown transport failure"),)
            _vnf_log(reason)
            if disable_on_failure:
                _record_registration(_launch_file, "failed", reason=reason)
                self._cfg.enabled = False
            return False

        def _is_stale_slot_rejection(self, error):
            """Whether an HTTP rejection proves this slot identity is stale."""
            code = int(getattr(error, "code", 0) or 0)
            if code not in (403, 409):
                return False
            raw = getattr(error, "_vnf_error_body", None)
            if raw is None:
                try:
                    raw = error.read()
                    if not isinstance(raw, str):
                        raw = raw.decode("utf-8", "replace")
                    error._vnf_error_body = raw
                except Exception:
                    return False
            return bool(
                "Unknown slot identity" in raw
                or (code == 409 and "Slot is closed" in raw))

        def _recover_stale_slot(self, error, stale_slot):
            """Adopt fresh launch intent after a bridge/slot was replaced.

            Protocol launchers such as GOG may focus an already-running game
            instead of creating a new process. If the hub restarted and reused
            the same bridge port, that warm shim keeps its obsolete slot id and
            token until the bridge explicitly rejects them. A fresh launch file
            is the authenticated handoff for this exact case.
            """
            explicit_unknown = False
            code = int(getattr(error, "code", 0) or 0)
            if code:
                explicit_unknown = self._is_stale_slot_rejection(error)
                if not explicit_unknown:
                    return False
            with self._lock:
                # Another poller may already have completed recovery.
                if self.slot_id != stale_slot:
                    return self.slot_id is not None
                launch = _vnf_read_launch_file()
                if launch is None:
                    return False
                bridge_url = launch.get("bridge_url")
                token = launch.get("slot_token")
                save_slot = launch.get("save_slot")
                save_slot = str(save_slot) if save_slot else ""
                prior = getattr(self._cfg, "_launch_file", None) or {}
                if not explicit_unknown:
                    # A transport error alone is not evidence that this slot
                    # is obsolete. Adopt only a demonstrably newer/different
                    # launch handoff, never the same file after a brief outage.
                    try:
                        newer = float(launch.get("written_at") or 0) > float(
                            prior.get("written_at") or 0)
                    except (TypeError, ValueError):
                        newer = False
                    different = (
                        (bridge_url and str(bridge_url) != self._cfg.bridge_url)
                        or ((str(token) if token else None)
                            != self._cfg.slot_token)
                    )
                    if not newer and not different:
                        return False
                if bridge_url:
                    bridge_url = str(bridge_url)
                else:
                    bridge_url = self._cfg.bridge_url
                old_bridge_url = self._cfg.bridge_url
                old_token = self._cfg.slot_token
                old_launch = getattr(self._cfg, "_launch_file", None)
                old_enabled = self._cfg.enabled
                self._cfg.bridge_url = bridge_url
                self._cfg.slot_token = str(token) if token else None
                self.slot_id = None
                self.assign_slot(registration_context=launch)
                if self.slot_id is None:
                    # The new bridge may still be starting. Roll the client
                    # identity back so the next poll sees the same stale-slot
                    # failure and retries the fresh handoff instead of polling
                    # an unscoped /command URL forever.
                    self._cfg.bridge_url = old_bridge_url
                    self._cfg.slot_token = old_token
                    self._cfg._launch_file = old_launch
                    self._cfg.enabled = old_enabled
                    self.slot_id = stale_slot
                    return False
                try:
                    if save_slot != getattr(self._cfg, "save_slot", ""):
                        _vnf_apply_save_slot(save_slot)
                except Exception as e:
                    # Save isolation is part of the handoff, not an optional
                    # follow-up. Keep the old identity recoverable and retry
                    # the whole adoption on the next poll.
                    self._cfg.bridge_url = old_bridge_url
                    self._cfg.slot_token = old_token
                    self._cfg._launch_file = old_launch
                    self._cfg.enabled = old_enabled
                    self.slot_id = stale_slot
                    _record_registration = globals().get(
                        "_vnf_record_launch_registration",
                        lambda *args, **kwargs: False)
                    _record_registration(
                        launch, "failed", reason="Save-slot recovery failed: %s" % (
                            _vnf_text(e, "unknown save failure"),))
                    _vnf_log("Save-slot recovery failed: %s" % (
                        _vnf_text(e, "unknown save failure"),))
                    return False
                self._cfg.save_slot = save_slot
                self._cfg._launch_file = launch
                if launch.get("launch_id"):
                    self._cfg._launch_id = str(launch.get("launch_id"))
                if not launch.get("claimed_by"):
                    _vnf_claim_launch_file(launch)
                _vnf_log(
                    "Recovered stale bridge slot {0} as {1}.".format(
                        stale_slot, self.slot_id))
                return True

        def _post(self, path, data, timeout=2.0, _allow_recovery=True):
            if not _HAS_URLLIB or not self._cfg.enabled:
                return None
            try:
                body = json.dumps(
                    data, ensure_ascii=False, default=_vnf_text).encode("utf-8")
                req = _urllib_request.Request(
                    self._url(path),
                    data=body,
                    headers=self._auth_headers(),
                )
                # Set method for POST request - compatible with both Python 2.7 and 3
                if _is_legacy:
                    # Python 2.7 workaround: override get_method
                    req.get_method = lambda: "POST"
                else:
                    # Python 3: use method attribute
                    req.method = "POST"
                resp = _urllib_request.urlopen(req, timeout=timeout)
                return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                if self._cfg.debug:
                    _tb_module.print_exc()
                code = int(getattr(e, "code", 0) or 0)
                stale_rejection = self._is_stale_slot_rejection(e)
                if stale_rejection:
                    if _allow_recovery:
                        stale_slot = self.slot_id
                        if self._recover_stale_slot(e, stale_slot):
                            return self._post(
                                path, data, timeout=timeout,
                                _allow_recovery=False)
                    # Critical events interpret None as a retained ordering
                    # barrier and retry after the launch handoff becomes
                    # readable. Ordinary events remain best-effort.
                    return None
                if code in (400, 401, 403, 404, 409, 410, 422):
                    _vnf_log(
                        "Bridge rejected {0} with HTTP {1}; retiring the "
                        "source occurrence.".format(path, code))
                    return _VNF_NATIVE_DICT_TYPE({
                        "_vnf_delivery_rejected": True,
                        "status": code,
                    })
                return None

        def _post_async(self, path, data, timeout=2.0):
            """Queue a POST behind earlier bridge mutations."""
            self._queue_post(path, data, timeout=timeout, wait=False)

        def _outbox_worker(self):
            """Send queued bridge mutations in their source order."""
            while True:
                with self._outbox_condition:
                    while not self._outbox:
                        self._outbox_condition.wait()
                    item = self._outbox.pop(0)
                    _notify_all = getattr(
                        self._outbox_condition, "notify_all", None)
                    if _notify_all is None:
                        _notify_all = self._outbox_condition.notifyAll
                    _notify_all()
                    if item.get("cancelled"):
                        item["done"].set()
                        continue
                    critical_key = item.get("critical_key")
                    if item.get("wait"):
                        remaining = item["deadline"] - time.time()
                        if remaining <= 0 and critical_key is None:
                            item["done"].set()
                            continue
                        item["started"] = True
                first_attempt = True
                while True:
                    effective_timeout = item["timeout"]
                    if (first_attempt and item.get("wait")
                            and remaining > 0):
                        effective_timeout = min(effective_timeout, remaining)
                    if (self._outbox_failure_streak
                            and not item.get("wait")
                            and critical_key is None):
                        effective_timeout = min(effective_timeout, 0.25)
                    result = self._post(
                        item["path"], item["data"], effective_timeout)
                    if result is not None or critical_key is None:
                        break
                    self._outbox_failure_streak += 1
                    # Release a synchronous caller at its deadline while this
                    # worker retains the original source occurrence as the
                    # ordering barrier. Later mutations cannot overtake it.
                    item["done"].set()
                    first_attempt = False
                    time.sleep(0.25)
                item["result"][0] = result
                item["done"].set()
                if result is None:
                    self._outbox_failure_streak += 1
                else:
                    self._outbox_failure_streak = 0
                if critical_key is not None:
                    resume_pending_command = False
                    with self._outbox_condition:
                        if self._critical_event_items.get(critical_key) is item:
                            self._critical_event_items.pop(critical_key, None)
                        resume_pending_command = not self._critical_event_items
                        _notify_all = getattr(
                            self._outbox_condition, "notify_all", None)
                        if _notify_all is None:
                            _notify_all = self._outbox_condition.notifyAll
                        _notify_all()
                    if resume_pending_command:
                        try:
                            _has_pending_command = (
                                _vnf_pending_command_box[0] is not None)
                        except Exception:
                            _has_pending_command = False
                        if _has_pending_command:
                            try:
                                renpy.exports.restart_interaction()
                            except Exception:
                                pass
                if self._outbox_failure_streak >= 3:
                    with self._outbox_condition:
                        keep = _VNF_NATIVE_LIST_TYPE()
                        dropped = _VNF_NATIVE_LIST_TYPE()
                        for queued in self._outbox:
                            if (queued.get("wait")
                                    or queued.get("critical_key") is not None):
                                keep.append(queued)
                            else:
                                dropped.append(queued)
                        self._outbox = keep
                        for queued in dropped:
                            queued["done"].set()
                        if dropped:
                            _vnf_log(
                                "Bridge unavailable; discarded {0} stale "
                                "asynchronous mutation(s).".format(
                                    len(dropped)))

        def _screenshot_outbox_worker(self):
            """Send screenshots independently; only the latest queued frame matters."""
            while True:
                with self._screenshot_outbox_condition:
                    while not self._screenshot_outbox:
                        self._screenshot_outbox_condition.wait()
                    item = self._screenshot_outbox.pop(0)
                result = self._post(
                    item["path"], item["data"], item["timeout"])
                item["result"][0] = result
                item["done"].set()

        def _queue_screenshot(self, path, data, timeout=2.0, wait=False):
            """Queue a latest-frame screenshot outside the story barrier lane."""
            payload = _VNF_NATIVE_DICT_TYPE(data)
            with self._screenshot_outbox_condition:
                self._screenshot_source_serial += 1
                payload.setdefault("_source_id", self._screenshot_source_id)
                payload.setdefault("_source_seq", self._screenshot_source_serial)
                payload.setdefault("_source_ts", time.time())
                done = threading.Event()
                result = _VNF_NATIVE_LIST_TYPE((None,))
                item = _VNF_NATIVE_DICT_TYPE({
                    "path": path,
                    "data": payload,
                    "timeout": timeout,
                    "done": done,
                    "result": result,
                })
                if not wait and self._screenshot_outbox:
                    replaced = self._screenshot_outbox.pop()
                    replaced["done"].set()
                self._screenshot_outbox.append(item)
                self._screenshot_outbox_condition.notify()
            if not wait:
                return None
            done.wait()
            return result[0]

        def _queue_post(self, path, data, timeout=2.0, wait=False,
                        cancel_request_id=None):
            """Enqueue one source-stamped mutation, optionally awaiting it.

            For ordinary synchronous barriers, ``timeout`` covers both
            queueing and the network attempt. Critical lifecycle events may
            release their caller at that deadline, but remain in the ordered
            lane until the bridge acknowledges their original occurrence.
            """
            payload = _VNF_NATIVE_DICT_TYPE(data)
            if path == "/event" and payload.get("type") == "screenshot":
                return self._queue_screenshot(
                    path, payload, timeout=timeout, wait=wait)
            critical_key = None
            if path == "/event":
                critical_key = self._critical_event_key(payload)
            with self._outbox_condition:
                deadline = time.time() + max(0.0, timeout)
                item = None
                if critical_key is not None:
                    item = self._critical_event_items.get(critical_key)
                while (item is None
                       and critical_key is not None
                       and len(self._critical_event_items)
                       >= self._critical_event_limit):
                    # Backpressure is preferable to dropping a unique
                    # lifecycle event. Command polling cannot outrun this
                    # indefinitely because every result passes this gate.
                    self._outbox_condition.wait(0.25)
                    item = self._critical_event_items.get(critical_key)
                while (item is None
                       and critical_key is None
                       and len(self._outbox) >= self._outbox_limit):
                    if (
                        cancel_request_id is not None
                        and cancel_request_id in self._request_retry_cancelled
                    ):
                        return None
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        _vnf_log(
                            "Bridge outbox full; dropping {0}.".format(path))
                        return None
                    self._outbox_condition.wait(remaining)
                if (
                    cancel_request_id is not None
                    and cancel_request_id in self._request_retry_cancelled
                ):
                    return None
                if item is None:
                    self._source_event_serial += 1
                    payload.setdefault("_source_id", self._source_id)
                    payload.setdefault("_source_seq", self._source_event_serial)
                    payload.setdefault("_source_ts", time.time())
                    done = threading.Event()
                    result = _VNF_NATIVE_LIST_TYPE((None,))
                    item = _VNF_NATIVE_DICT_TYPE({
                        "path": path,
                        "data": payload,
                        "timeout": timeout,
                        "done": done,
                        "result": result,
                        "wait": wait,
                        "deadline": deadline,
                        "started": False,
                        "cancelled": False,
                        "critical_key": critical_key,
                    })
                    self._outbox.append(item)
                    if critical_key is not None:
                        self._critical_event_items[critical_key] = item
                    self._outbox_condition.notify()
                else:
                    done = item["done"]
                    result = item["result"]
            if not wait:
                return None
            remaining = max(0.0, deadline - time.time())
            if remaining:
                done.wait(remaining)
            if not done.is_set():
                with self._outbox_condition:
                    if not item["started"] and critical_key is None:
                        item["cancelled"] = True
                        try:
                            self._outbox.remove(item)
                        except ValueError:
                            pass
                        done.set()
                        _notify_all = getattr(
                            self._outbox_condition, "notify_all", None)
                        if _notify_all is None:
                            _notify_all = self._outbox_condition.notifyAll
                        _notify_all()
                # A started urllib operation was given only the remaining
                # deadline as its timeout. Allow it a short scheduling grace,
                # but never let a failed worker wedge the Ren'Py interaction.
                done.wait(0.25)
            if (not done.is_set()
                    and (item["started"] or critical_key is not None)):
                return (self.POST_ACCEPTANCE_UNKNOWN, item)
            return result[0]

        def cancel_request_retry(self, request_id):
            """Stop retrying publication once its local interaction closes."""
            if request_id is None:
                return
            with self._outbox_condition:
                if request_id in self._request_retry_threads:
                    self._request_retry_cancelled.add(request_id)
                    _notify_all = getattr(
                        self._outbox_condition, "notify_all", None)
                    if _notify_all is None:
                        _notify_all = self._outbox_condition.notifyAll
                    _notify_all()

        def has_pending_critical_events(self):
            """Whether command execution must pause behind a delivery barrier."""
            with self._outbox_condition:
                return bool(self._critical_event_items)

        def _schedule_request_retry(self, request_id, event, item=None):
            """Reconcile an unacknowledged request using its stable ID."""
            with self._outbox_condition:
                self._request_retry_cancelled.discard(request_id)
                existing = self._request_retry_threads.get(request_id)
                if existing is not None and existing.is_alive():
                    return

            def _retry():
                pending_item = item
                while True:
                    if pending_item is not None:
                        while not pending_item["done"].is_set():
                            pending_item["done"].wait(0.25)
                            with self._outbox_condition:
                                if request_id in self._request_retry_cancelled:
                                    break
                        with self._outbox_condition:
                            if request_id in self._request_retry_cancelled:
                                break
                        if pending_item["result"][0] is not None:
                            break
                        pending_item = None
                    with self._outbox_condition:
                        if request_id in self._request_retry_cancelled:
                            break
                    time.sleep(0.25)
                    with self._outbox_condition:
                        if request_id in self._request_retry_cancelled:
                            break
                    outcome = self._queue_post(
                        "/request", event, timeout=2.0, wait=True,
                        cancel_request_id=request_id)
                    if (
                        isinstance(outcome, tuple)
                        and len(outcome) == 2
                        and outcome[0] is self.POST_ACCEPTANCE_UNKNOWN
                    ):
                        pending_item = outcome[1]
                        continue
                    if outcome is not None:
                        break
                with self._outbox_condition:
                    self._request_retry_threads.pop(request_id, None)
                    self._request_retry_cancelled.discard(request_id)

            thread = threading.Thread(target=_retry)
            thread.daemon = True
            with self._outbox_condition:
                self._request_retry_threads[request_id] = thread
            thread.start()

        def _critical_event_key(self, event):
            """Return the stable identity for an event that must be retried."""
            event_type = event.get("type")
            if event_type in ("choice_resolved", "input_resolved"):
                identity = event.get("request_id")
            elif event_type == "command_result":
                identity = event.get("nonce")
            else:
                return None
            if identity is None:
                return None
            return (event_type, identity)

        def _get(self, path, params=None, timeout=1.0,
                 _retry_unknown_slot=True):
            if not _HAS_URLLIB or not self._cfg.enabled:
                return None
            stale_slot = self.slot_id
            try:
                url = self._url(path)
                if params:
                    qs = _urllib_parse.urlencode(params)
                    url = url + "?" + qs
                req = _urllib_request.Request(
                    url, None, self._auth_headers(with_content_type=False))
                resp = _urllib_request.urlopen(req, timeout=timeout)
                return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                if (_retry_unknown_slot
                        and self._recover_stale_slot(e, stale_slot)):
                    return self._get(
                        path, params=params, timeout=timeout,
                        _retry_unknown_slot=False)
                if self._cfg.debug:
                    _tb_module.print_exc()
                return None

        # -- high level API --

        # Context-scoped dedup: tracks dialogue/narration/choice text
        # emitted since the last context change.  Screen_content events
        # have matching texts stripped (buttons always pass through).
        # Unlike TTL-based dedup, this survives long pauses and NVL
        # accumulation — text stays tracked until the scene changes.
        _scene_texts = set()  # all text emitted in current scene
        _request_caption_texts = set()

        def _dedup_event(self, event):
            """Dedup layer: strips duplicate text from screen_content events.

            Dialogue, narration, and choice events record their text.
            Screen_content events have matching texts stripped (buttons kept).
            Context changes clear the tracking set.
            All other events pass through unchanged.
            """
            etype = event.get("type", "")

            # Context change — clear tracked text for fresh scene.
            if etype == "context":
                self._scene_texts.clear()
                self._request_caption_texts.clear()
                return event

            if etype in ("game_started", "game_resumed", "mod_loaded"):
                self._scene_texts.clear()
                self._request_caption_texts.clear()
            elif etype in ("choice_resolved", "input_resolved"):
                self._request_caption_texts.clear()

            if etype == "dialogue":
                text = (event.get("text") or event.get("what", "")).strip()
                if text:
                    self._scene_texts.add(text)
                    who = event.get("character") or event.get("who")
                    if who:
                        self._scene_texts.add("%s: %s" % (who, text))
                return event

            if etype == "narration":
                text = event.get("text", "").strip()
                if text:
                    self._scene_texts.add(text)
                return event

            if etype in ("choice_request", "input_request"):
                for c in event.get("choices", []):
                    label = (c if isinstance(c, basestring)
                             else c.get("label", ""))
                    if label:
                        self._scene_texts.add(
                            (_vnf_stringify(label) or "").strip())
                for item in event.get("full_items", []):
                    label = item.get("label", "")
                    if label:
                        self._scene_texts.add(
                            (_vnf_stringify(label) or "").strip())
                return event

            if etype == "screen_content":
                texts = event.get("texts", [])
                if texts:
                    filtered = []
                    for t in texts:
                        ts = t.strip()
                        if not ts:
                            continue
                        # Captions travel inside choice_request so they cannot
                        # race the menu. Suppress only that exact prompt;
                        # short choices such as "No" must not hide prose.
                        if ts in self._request_caption_texts:
                            continue
                        # Exact match.
                        if ts in self._scene_texts:
                            continue
                        # Containment check — covers partial matches
                        # (e.g. "Speaker: text" vs "text").
                        skip = False
                        for seen in self._scene_texts:
                            if ts in seen or seen in ts:
                                skip = True
                                break
                        if not skip:
                            filtered.append(t)
                    event = dict(event, texts=filtered)
                return event

            return event

        def push_event(self, event):
            """Push a game event to the bridge (async, non-blocking)."""
            if not self._cfg.enabled:
                return
            event = self._dedup_event(event)
            self._post_async("/event", event)
            if _vnf_event_hooks:
                _vnf_fire_event_hooks(event)

        def push_event_sync(self, event):
            """Push a game event to the bridge (blocking)."""
            if not self._cfg.enabled:
                return
            event = self._dedup_event(event)
            self._queue_post("/event", event, wait=True)
            if _vnf_event_hooks:
                _vnf_fire_event_hooks(event)

        def push_request(self, req_type, req_id=None, **kwargs):
            """Push an interaction request. Returns the request ID.
            If req_id is given, reuses it (for enrichment updates)."""
            if req_id is None:
                req_id = str(uuid.uuid4())[:8]
            event = dict(type=req_type, id=req_id, **kwargs)
            event["timestamp"] = time.time()
            if req_type == "choice_request":
                captions = set()
                for item in event.get("full_items", []):
                    if (_vnf_is_mapping(item)
                            and item.get("is_caption")):
                        label = _vnf_stringify(item.get("label")) or ""
                        if label.strip():
                            captions.add(label.strip())
                self._request_caption_texts = captions
            else:
                self._request_caption_texts = set()
            # Requests share the event outbox so every preceding narration or
            # state sample reaches the bridge before the decision boundary.
            result = self._queue_post("/request", event, wait=True)
            if (
                isinstance(result, tuple)
                and len(result) == 2
                and result[0] is self.POST_ACCEPTANCE_UNKNOWN
            ):
                _vnf_log(
                    "Request acknowledgement timed out; reconciling stable "
                    "request ID: %s" % req_id)
                self._schedule_request_retry(req_id, event, result[1])
                return req_id
            if result is None:
                _vnf_log(
                    "Request publication failed; retrying stable request ID: "
                    "%s" % req_id)
                self._schedule_request_retry(req_id, event)
            return req_id

        def poll_action(self, request_id):
            """
            Poll the bridge for an action matching request_id.
            Returns the action dict if available, else None.
            """
            data = self._get("/action", params={"request_id": request_id})
            if data and data.get("action") is not None:
                return data["action"]
            return None

        def poll_command(self):
            """
            Poll the bridge for a pending command.
            Returns the command dict if available, else None.
            """
            data = self._get("/command")
            if data and data.get("command") is not None:
                return data["command"]
            return None

        def notify_game_ended(self, reason="return"):
            event = dict(type="game_ended", reason=reason, timestamp=time.time())
            # Synchronous so it arrives before the process exits.
            self._queue_post("/event", event, timeout=3.0, wait=True)

        def reset_bridge(self):
            """Reset all bridge state (transcript, pending requests, commands).
            Called on mod load to clear stale state from previous game cycles."""
            return self._queue_post("/reset", {}, timeout=2.0, wait=True)

        def submit_inventory(self, changes, request_id=None, inventory_version=None):
            """
            Submit inventory modifications to the bridge.

            Args:
                changes: List of dicts with keys 'type' (add/remove/modify), 'item', and quantity info
                request_id: Optional request_id for hybrid mode coordination
                inventory_version: Optional version for optimistic locking

            Returns:
                dict with 'status', 'message', and 'version' if successful
            """
            if not self._cfg.enabled:
                return None
            data = {"changes": changes}
            if request_id:
                data["request_id"] = request_id
            if inventory_version is not None:
                data["inventory_version"] = inventory_version
            result = self._queue_post(
                "/inventory", data, timeout=2.0, wait=True)
            return result

        def get_inventory_version(self):
            """
            Get the current inventory version from the bridge.
            Returns version dict or None on error.
            """
            if not self._cfg.enabled:
                return None
            return self._get("/inventory", timeout=1.0)

    # =========================================================================
    # Periodic-callback based action polling
    # =========================================================================
    #
    # Instead of adding a custom displayable via renpy.ui.add() (which has
    # timing issues — the displayable may not be part of the interaction's
    # tree), we poll the bridge from config.periodic_callbacks.  This fires
    # ~20 times/sec during ANY interaction, reliably.
    #
    # When a pending action is found we call renpy.end_interaction(value)
    # which immediately resolves the current interaction with that value.
    #
    # State is managed via these module-level variables:

    # =========================================================================
    # Screen Scraper Helper
    # =========================================================================

    # Unicode smart-quote → ASCII mappings for fuzzy label matching.
    _VNF_QUOTE_MAP = {
        0x2018: u"'",   # '
        0x2019: u"'",   # '
        0x201A: u"'",   # ‚
        0x201C: u'"',   # "
        0x201D: u'"',   # "
        0x201E: u'"',   # „
        0x00AB: u'"',   # «
        0x00BB: u'"',   # »
        0x2013: u"-",   # –
        0x2014: u"-",   # —
    }

    def _vnf_normalize_quotes(s):
        """Replace Unicode smart quotes/dashes with ASCII equivalents
        and collapse whitespace (newlines, tabs, multiple spaces) to
        single spaces so multi-line button labels match."""
        for cp, repl in _VNF_QUOTE_MAP.items():
            c = unichr(cp) if str is bytes else chr(cp)
            if c in s:
                s = s.replace(c, repl)
        # Collapse any whitespace run (including \n, \t) to single space.
        return " ".join(s.split())

    def _vnf_substitute(s):
        """Resolve Ren'Py [variable] references in *s* using renpy.store.

        Returns the resolved string.  On error (missing variable, etc.)
        returns the original string unchanged so scraping never breaks."""
        if "[" not in s:
            return s
        try:
            resolved, _ = renpy.substitutions.substitute(s, force=True,
                                                         translate=False)
            return resolved
        except Exception:
            return s

    def _vnf_resolve_display_name_expr(s):
        """Resolve simple dynamic speaker-name expressions.

        Some games store a Character name as a bare expression such as
        ``lieke_name()`` rather than as ``[lieke_name()]``.  Ren'Py renders
        that expression for users, so the agent-facing event should use the
        same display value.  Keep this deliberately narrow so ordinary names
        such as ``Gwenelle, Duchess of Sudbury`` are never evaluated.
        """
        if s is None:
            return s
        raw = _vnf_stringify(s)
        if raw is None:
            return s
        raw = raw.strip()
        if not raw:
            return s
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_()."
        for c in raw:
            if c not in allowed:
                return s
        if not (raw.endswith(")") or "." in raw or "_" in raw):
            return s
        try:
            resolved = renpy.python.py_eval(raw)
            if isinstance(resolved, basestring):
                return resolved
        except Exception:
            pass
        return s

    def _vnf_button_subtree(d, depth):
        """Descend a button to collect its rendered text + inline tags.

        Checks both ``children`` and ``child``: on Ren'Py 6.x a Button's
        ``children`` is often empty while ``child`` holds the Text widget.
        Returns the walk-accumulator dict (texts + any _inline_image_tags
        gathered during text capture).
        """
        _bd = {"texts": [], "choices": [], "value_map": {}, "buttons": []}
        if hasattr(d, "children"):
            for c in d.children:
                _vnf_walk_screen(c, _bd, depth + 1)
        if not _bd["texts"] and hasattr(d, "child") and d.child is not None:
            _vnf_walk_screen(d.child, _bd, depth + 1)
        return _bd

    def _vnf_button_label(d, action, cls_name, depth=0, subtree=None):
        """Canonical button-label extraction.

        Shared by the scraper walker (_vnf_walk_screen, agent-facing
        labels) and the act executor's lookup (_vnf_collect_button_actions,
        which matches BY label).  They must agree or numeric/label acts
        silently fail to find the button.  Order: rendered text → alt →
        synthesized from the last meaningful action value (substituted,
        newline-flattened, single tokens bracketed) → action hint →
        ImageButton idle image.  Pass ``subtree`` to reuse an already-
        walked _vnf_button_subtree result (avoids a second descent).
        """
        texts = (subtree if subtree is not None
                 else _vnf_button_subtree(d, depth))["texts"]
        # u"".join with duck-typed conversion — str() on a unicode label
        # raises UnicodeEncodeError on plain-Py2 (Ren'Py 6.x) builds.
        _parts = []
        for t in texts:
            try:
                _parts.append(t if isinstance(t, type(u"")) else u"{}".format(t))
            except Exception:
                pass
        label = u" ".join(_parts).strip()
        if not label:
            label = getattr(d, "alt", None)
        if not label and action is not None:
            _acts = action if _vnf_is_sequence(action) else [action]
            for _a in reversed(_acts):
                try:
                    _v = getattr(_a, "value", None)
                    if _v is None or isinstance(_v, bool):
                        continue
                    # Safe unicode conversion: on Py2 a non-ASCII byte
                    # string raises in u"".format and str() raises on
                    # non-ASCII unicode — decode bytes explicitly.
                    if isinstance(_v, bytes):
                        _vu = _v.decode("utf-8", "replace")
                    else:
                        _vu = u"{}".format(_v)
                    if _vu in (u"True", u"False", u"None"):
                        continue
                    _sub = _vnf_substitute(_vu).strip()
                    _flat = u" / ".join(
                        _p.strip() for _p in _sub.split("\n") if _p.strip())
                    if _flat and (" " in _flat or "/" in _flat):
                        label = _flat
                    else:
                        label = u"[{}]".format(_flat)
                    break
                except Exception:
                    continue
        if not label:
            label = _vnf_action_label_hint(action)
        if not label and "ImageButton" in cls_name:
            idle = getattr(d, "idle_image", None)
            if idle:
                label = u"[Image: {}]".format(idle)
        return label

    def _vnf_collect_button_actions(d, result, screen_name, depth=0):
        """
        Walk a displayable tree and collect (displayable, action, label, screen)
        tuples for every button that has an action.  Used by the shim act
        command to find the actual action object to invoke.
        """
        if d is None or depth > 32:
            return
        cls_name = d.__class__.__name__

        if "Button" in cls_name or "Hotspot" in cls_name:
            action = getattr(d, "action", None)
            # Ren'Py 6.x: Button may have clicked= without action=.
            if action is None:
                action = getattr(d, "clicked", None)
            # Canonical label — must match _vnf_walk_screen so the
            # executor finds the button the agent was shown.
            label = _vnf_button_label(d, action, cls_name, depth)
            if not label:
                label = "[unlabelled]"

            if action:
                # Store the raw action (list or single) for renpy.run().
                result.append((d, action, label, screen_name))

        # Recurse into containers (but not into buttons — we already
        # handled their children above for label extraction).
        if "Button" not in cls_name and "Hotspot" not in cls_name:
            if hasattr(d, "children"):
                for c in d.children:
                    _vnf_collect_button_actions(c, result, screen_name, depth + 1)
            elif hasattr(d, "child"):
                _vnf_collect_button_actions(d.child, result, screen_name, depth + 1)

    def _vnf_walk_screen(d, data, depth=0, _section_depth=None, _current_section=0, _path=()):
        """
        Recursively walk a displayable tree to extract text and return-actions.
        Useful for custom dialogue boxes used in games like Roadwarden.

        `data` dict keys:
            texts            — all scraped text strings
            _text_sections   — parallel int list: section index per text
            _text_paths      — parallel tuple list: child-index path per text
            choices          — labels of buttons whose action is Return (resolve call_screen)
            value_map        — {1-based index: return value} for Return buttons
            buttons          — ALL buttons: [{"label": str, "actions": [str, ...]}]
                               Populated regardless of action type so the LLM knows
                               every clickable element on screen (SetField, Jump, etc.)
                               Each button gets _section: int and _path: tuple.

        Section tracking (optional):
            _section_depth   — tree depth at which children become section boundaries
            _current_section — inherited section index for all descendants
            _path            — tuple of child indices from root to current node
        """
        if d is None or depth > 32:
            return
        # Always use __class__.__name__; __name__ is for functions/modules,
        # not displayable instances.
        cls_name = d.__class__.__name__

        # Ensure the "buttons" and section/path buckets exist
        # (callers created before this change may omit them).
        if "buttons" not in data:
            data["buttons"] = []
        if "_text_sections" not in data:
            data["_text_sections"] = []
        if "_text_paths" not in data:
            data["_text_paths"] = []

        if vnf_player.debug and depth < 4:
            _dbg = "[LLM Scraper] {}{}".format("  " * depth, cls_name)
            print(_dbg)

        # 1. Text Capture: Collect displayable strings for context
        if "Text" in cls_name:
            try:
                txt_list = getattr(d, "text", None)
                if not txt_list:
                    txt_list = getattr(d, "text_parameter", None)
                if not txt_list:
                    txt_list = []
                # Ren'Py's .text is a list of strings and Displayables.
                # Use duck-typing: convert each to unicode, skip items
                # that look like displayable reprs (start with "<").
                _parts = []
                for t in txt_list:
                    try:
                        _s = unicode(t) if hasattr(t, '__unicode__') else _vnf_text(t)
                        if not _s.startswith("<"):
                            _parts.append(_s)
                    except Exception:
                        pass
                s = u"".join(_parts)
                # Extract inline image tags before stripping (e.g.
                # {image=cointest} on choice buttons).
                import re as _re_text
                _inline_imgs = _re_text.findall(r'\{image=([^}]+)\}', s)
                if _inline_imgs:
                    data.setdefault("_inline_image_tags", []).extend(_inline_imgs)
                clean_s = _vnf_substitute(
                    renpy.text.extras.filter_text_tags(s, allow=set())).strip()
                if clean_s:
                    data["texts"].append(clean_s)
                    data["_text_sections"].append(_current_section)
                    data["_text_paths"].append(_path)
                    if vnf_player.debug:
                        print("[LLM Scraper]   text: {!r}  sec={} path={}".format(
                            clean_s[:80], _current_section, _path))
            except Exception:
                pass

        # 2. Button / Hotspot Capture: Identify interaction points
        elif "Button" in cls_name or "Hotspot" in cls_name:
            action = getattr(d, "action", None)
            # Ren'Py 6.x: Button may have clicked= without action=.
            if action is None:
                action = getattr(d, "clicked", None)

            # Canonical label extraction (shared with the act executor's
            # _vnf_collect_button_actions so matching agrees): rendered
            # text → alt → synthesized action value → hint → image.
            # Reuse the subtree walk so inline image tags stay available.
            btn_data = _vnf_button_subtree(d, depth)
            label = _vnf_button_label(d, action, cls_name, depth,
                                      subtree=btn_data)

            # Skip disabled / insensitive buttons — they are visible
            # but not interactive, so the agent should not see them as
            # clickable.  Use the widget's own is_sensitive() first
            # (checks explicit .sensitive attr), fall back to action check.
            try:
                _is_sens = getattr(d, "is_sensitive", None)
                if _is_sens is not None:
                    _sensitive = _is_sens()
                elif action is not None:
                    _sensitive = renpy.exports.is_sensitive(action)
                else:
                    _sensitive = True
            except Exception:
                _sensitive = True
            _btn_disabled = not _sensitive
            if _btn_disabled and vnf_player.debug:
                print("[LLM Scraper]   insensitive button: {!r}".format(
                    label if label else cls_name))

            action_names = []
            is_return = False

            if action:
                actions = action if _vnf_is_sequence(action) else [action]
                for a in actions:
                    a_name = a.__class__.__name__
                    action_names.append(a_name)
                    a_str = _vnf_text(a).lower()
                    # Detect Return actions that resolve a call_screen.
                    # Skip for disabled buttons — disabled choices are
                    # tracked separately via menu context.
                    if ("Return" in a_name or "returns" in a_str or "return(" in a_str):
                        if _btn_disabled:
                            # DISABLED-INTERACTION CONTRACT (2026-08-18):
                            # never actable, STILL LISTED. The old early
                            # `return` dropped the widget from the scrape
                            # entirely, so the augmenter's disabled entry
                            # looked un-rendered and the stale-drop ate
                            # it — Echoes' greyed one-shots were ABSENT
                            # for agents. Skip only the actable choice /
                            # value_map registration; fall through so the
                            # button record below keeps the widget
                            # visible with is_disabled True.
                            break
                        is_return = True
                        idx = len(data["choices"]) + 1
                        data["choices"].append(label or "[Option {}]".format(idx))
                        data["value_map"][idx] = getattr(a, "value", None)
                        if vnf_player.debug:
                            print("[LLM Scraper]   choice #{}: {!r}".format(idx, data["choices"][-1]))
                        break  # Return is terminal; stop scanning actions.

            # Always record every labelled button so the LLM knows what
            # is on screen — even non-Return buttons like SetField,
            # SetVariable, Jump, Start, ShowMenu, etc.
            if label or action_names:
                # Build action detail strings so downstream transforms
                # can distinguish buttons with the same class name but
                # different arguments (e.g. five SetField buttons
                # setting different attitude values).  Extract
                # well-known attributes (value, property/field,
                # variable, screen, label) so game-specific transforms
                # can match on them.  The "label" attr captures Jump
                # targets, tooltip values, etc.
                action_strs = []
                if action:
                    _acts = action if _vnf_is_sequence(action) else [action]
                    for _a in _acts:
                        _a_cls = _a.__class__.__name__
                        _parts = [_a_cls]
                        for _attr in ("property", "field", "variable",
                                      "screen", "value", "label"):
                            _v = getattr(_a, _attr, None)
                            if _v is not None:
                                _parts.append("{}={}".format(
                                    _attr, _v))
                        action_strs.append(" ".join(_parts))

                # Detect selected state.  Mirror Ren'Py's own
                # is_selected() algorithm: if a SelectedIf action
                # is present, its result is authoritative; otherwise
                # fall back to the first action with get_selected().
                _is_selected = False
                if action:
                    _sel_acts = (action if _vnf_is_sequence(action)
                                 else [action])
                    # Priority: SelectedIf first (authoritative).
                    _found_sel_if = False
                    for _sa in _sel_acts:
                        if _sa.__class__.__name__ == "SelectedIf":
                            _found_sel_if = True
                            _gs = getattr(_sa, "get_selected", None)
                            if _gs is not None:
                                try:
                                    _is_selected = bool(_gs())
                                except Exception:
                                    pass
                            break
                    # Fallback: first action with get_selected().
                    if not _found_sel_if:
                        for _sa in _sel_acts:
                            _gs = getattr(_sa, "get_selected", None)
                            if _gs is not None:
                                try:
                                    _rv = _gs()
                                    if _rv is not None:
                                        _is_selected = bool(_rv)
                                        break
                                except Exception:
                                    pass

                # Add to buttons list unconditionally
                btn_info = {
                    "label": label or "[unlabelled]",
                    "actions": action_names or ["none"],
                    "is_return": is_return,
                    "is_disabled": _btn_disabled,
                    "action_strs": action_strs,
                    "is_selected": _is_selected,
                    "_displayable": d,
                    "_action_obj": action,
                    "_section": _current_section,
                    "_path": _path,
                }
                # Check for inline images: both Image displayable
                # children and {image=...} text tags (e.g. dice icon
                # next to choice text in Roadwarden).
                _found_images = []
                def _check_images(w, _d=0):
                    if w is None or _d > 8:
                        return
                    _cn = w.__class__.__name__
                    if "Image" in _cn and "Button" not in _cn:
                        _fn = getattr(w, "filename", None)
                        _found_images.append(_fn or "unknown")
                        return
                    # Check for {image=...} text tags in Text displayables.
                    if "Text" in _cn:
                        _raw = getattr(w, "text", None)
                        if _raw:
                            try:
                                _joined = "".join(_vnf_text(x) for x in _raw)
                                import re as _re_mod
                                for _m in _re_mod.finditer(r'\{image=([^}]+)\}', _joined):
                                    _found_images.append(_m.group(1))
                                for _m in _re_mod.finditer(r'\{img=([^}]+)\}', _joined):
                                    _found_images.append(_m.group(1))
                            except Exception:
                                pass
                    for _ch in getattr(w, "children", []):
                        _check_images(_ch, _d + 1)
                    _ch2 = getattr(w, "child", None)
                    if _ch2 is not None:
                        _check_images(_ch2, _d + 1)
                _check_images(d)
                # Also check inline image tags found during Text
                # processing of button children.
                _inline = btn_data.get("_inline_image_tags", [])
                if _inline:
                    _found_images.extend(_inline)
                if _found_images:
                    btn_info["has_image"] = True
                    btn_info["_image_tags"] = _found_images
                # Attach image path for ImageButtons so transforms
                # can use filenames (e.g. arrow direction).
                if "ImageButton" in cls_name:
                    _idle_img = getattr(d, "idle_image", None)
                    if _idle_img is not None:
                        _img_fn = getattr(_idle_img, "filename", None)
                        if _img_fn is None:
                            _img_inner = getattr(_idle_img, "child", None)
                            if _img_inner is not None:
                                _img_fn = getattr(
                                    _img_inner, "filename", None)
                        if _img_fn is not None:
                            btn_info["_image"] = _img_fn
                data["buttons"].append(btn_info)
                if vnf_player.debug:
                    print("[LLM Scraper]   button: {!r}  actions={}".format(
                        btn_info["label"][:60], action_names))

        # 2b. Input Widget Detection
        # Custom screens may use an Input widget directly (via `call screen`)
        # instead of `renpy.input()`.  Flag it so the scraper can create a
        # synthetic input_request.
        elif cls_name == "Input":
            data["_has_input_widget"] = True

        # 3. Recursive container walking
        if "Button" not in cls_name and "Hotspot" not in cls_name:
            # Ren'Py 6.x: MultiBox.children may contain plain lists.
            if _vnf_is_sequence(d):
                for _ci, c in enumerate(d):
                    if c is not None and hasattr(c, "__class__"):
                        _vnf_walk_screen(c, data, depth + 1,
                                         _section_depth, _current_section,
                                         _path + (_ci,))
            elif hasattr(d, "children"):
                if _section_depth is not None and depth == _section_depth:
                    # Section boundary: each child starts a new section.
                    for _sec_i, c in enumerate(d.children):
                        _vnf_walk_screen(c, data, depth + 1,
                                         _section_depth, _sec_i,
                                         _path + (_sec_i,))
                else:
                    for _ci, c in enumerate(d.children):
                        _vnf_walk_screen(c, data, depth + 1,
                                         _section_depth, _current_section,
                                         _path + (_ci,))
                # Fallback: if children was empty, also check child.
                if not data.get("texts") and not data.get("buttons"):
                    if hasattr(d, "child") and d.child is not None:
                        _vnf_walk_screen(d.child, data, depth + 1,
                                         _section_depth, _current_section,
                                         _path + (0,))
            elif hasattr(d, "child"):
                _vnf_walk_screen(d.child, data, depth + 1,
                                 _section_depth, _current_section,
                                 _path + (0,))

    # =========================================================================
    # Shared Pipeline State
    # =========================================================================
    #
    # A dict shared across menu augmenters, screen transforms, and action
    # transforms within a single menu cycle.  Reset at the start of each
    # menu presentation.  Augmenters write discoveries (e.g. which values
    # are attitudes vs class actions), and downstream transforms read them.
    #
    # Accessible as ctx["shared"] in augmenters and context["shared"] in
    # action transforms.

    _vnf_pipeline_shared = {}

    # =========================================================================
    # Screen Data Transform Pipeline
    # =========================================================================
    #
    # After _vnf_walk_screen extracts raw data from the displayable tree,
    # a series of transform passes process the data before it is sent to
    # the bridge.  Each pass is a function(data) -> data that receives
    # and returns the same dict structure:
    #   {"texts": [...], "choices": [...], "value_map": {...}, "buttons": [...]}
    #
    # Passes run in priority order (lower first).  Game-specific mods can
    # register additional passes via _vnf_add_screen_transform().

    _vnf_screen_transforms = []

    def _vnf_add_screen_transform(fn, priority=50):
        """Register a screen-data transform pass.

        Args:
            fn: callable(data_dict) -> data_dict
            priority: int, lower runs first (default 50)
        """
        _vnf_screen_transforms.append((priority, fn))
        _vnf_screen_transforms.sort(key=lambda x: x[0])

    def _vnf_apply_screen_transforms(per_screen):
        """Run all registered transforms on per-screen scraped data.

        Each transform receives and returns a list of per-screen dicts:
            [{"_tag": "nvl", "texts": [...], "choices": [...],
              "buttons": [...], "value_map": {...}}, ...]

        Transforms can drop entire screens, modify per-screen data,
        or perform cross-screen analysis.
        """
        for _prio, fn in _vnf_screen_transforms:
            try:
                per_screen = fn(per_screen)
            except _CONTROL_EXCEPTIONS:
                raise
            except Exception:
                if vnf_player.debug:
                    _vnf_log(
                        "Screen transform error in {}: {}".format(
                            getattr(fn, "__name__", repr(fn)),
                            _tb_module.format_exc()))
        return per_screen

    # --- Built-in transforms ---

    def _vnf_transform_drop_internal_screens(per_screen):
        """Drop screens belonging to the LLM player shim itself."""
        return [s for s in per_screen if not s["_tag"].startswith("vnf_")]

    def _vnf_transform_drop_null_unlabelled(per_screen):
        """Drop buttons whose only actions are NullAction/None and
        that have no meaningful label.  These are decorative widgets
        (e.g. status icons, empty hotspots) that an LLM cannot act on."""
        null_actions = {"NullAction", "None", "none"}
        for scr in per_screen:
            scr["buttons"] = [
                b for b in scr["buttons"]
                if not (
                    all(a in null_actions for a in b.get("actions", []))
                    and b.get("label", "") in ("", "[unlabelled]")
                )
            ]
        return per_screen

    def _vnf_transform_drop_duplicate_texts(per_screen):
        """Collapse consecutive duplicate text entries within each screen."""
        for scr in per_screen:
            if not scr.get("texts"):
                continue
            texts = scr["texts"]
            text_sections = scr.get("_text_sections", [])
            text_paths = scr.get("_text_paths", [])
            keep_indices = [0]
            deduped = [texts[0]]
            for i, t in enumerate(texts[1:], start=1):
                if t != deduped[-1]:
                    deduped.append(t)
                    keep_indices.append(i)
            scr["texts"] = deduped
            if len(text_sections) == len(texts):
                scr["_text_sections"] = [
                    text_sections[i] for i in keep_indices]
            if len(text_paths) == len(texts):
                scr["_text_paths"] = [text_paths[i] for i in keep_indices]
        return per_screen

    def _vnf_transform_drop_duplicate_buttons(per_screen):
        """Deduplicate buttons by (label, actions, action_strs) signature
        within each screen.

        The displayable tree walker can find the same button through multiple
        paths (e.g. viewport wrapper AND inner vbox), producing duplicates.
        """
        for scr in per_screen:
            if not scr.get("buttons"):
                continue
            seen = set()
            deduped = []
            for btn in scr["buttons"]:
                sig = (btn.get("label", ""),
                       tuple(btn.get("actions", [])),
                       tuple(btn.get("action_strs", [])))
                if sig not in seen:
                    seen.add(sig)
                    deduped.append(btn)
            scr["buttons"] = deduped
        return per_screen

    _vnf_add_screen_transform(_vnf_transform_drop_internal_screens, priority=5)
    _vnf_add_screen_transform(_vnf_transform_drop_null_unlabelled, priority=10)
    _vnf_add_screen_transform(_vnf_transform_drop_duplicate_texts, priority=20)
    _vnf_add_screen_transform(_vnf_transform_drop_duplicate_buttons, priority=25)

    # =========================================================================
    # Menu Item Augmenter Pipeline
    # =========================================================================
    #
    # After the menu wrapper builds the initial choices/value_map from
    # Ren'Py's menu items, the augmenter pipeline can modify them before
    # the action transform pipeline runs.  Augmenters receive a context
    # dict with: raw_ast_items, items, choices, value_map, next_idx,
    # pre_sets, pre_resolve_steps.  Return the modified ctx or None to
    # skip.
    #
    # Registered via _vnf_add_menu_augmenter(fn, priority).

    _vnf_menu_augmenters = []

    def _vnf_add_menu_augmenter(fn, priority=50):
        """Register a menu item augmenter at the given priority.

        Args:
            fn: callable(ctx_dict) -> ctx_dict or None
            priority: int, lower runs first (default 50)
        """
        _vnf_menu_augmenters.append((priority, fn))
        _vnf_menu_augmenters.sort(key=lambda x: x[0])

    def _vnf_apply_menu_augmenters(ctx):
        """Run all registered menu augmenters in priority order."""
        for _prio, _fn in _vnf_menu_augmenters:
            try:
                result = _fn(ctx)
                if result is not None:
                    ctx = result
            except _CONTROL_EXCEPTIONS:
                raise
            except Exception:
                if vnf_player.debug:
                    _vnf_log("Menu augmenter error: "
                             + _vnf_text(_tb_module.format_exc()))
        return ctx

    # =========================================================================
    # Action Transform Pipeline
    # =========================================================================
    #
    # After choices (from _vnf_patched_menu) and screen buttons (from
    # _vnf_scrape_visible_screens) are collected, the action transform
    # pipeline processes the combined list.  Each transform can filter,
    # annotate, promote buttons to choices, or reorder actions.
    #
    # Transforms are registered via _vnf_add_action_transform(fn, priority).

    _vnf_action_transforms = []

    def _vnf_add_action_transform(fn, priority=50):
        """Register an action transform.

        Args:
            fn: callable(actions, context) -> actions
                actions: list of action dicts with keys:
                    label, source ("choice"|"button"), screen,
                    actions (list of action type names),
                    action_strs (list of action repr strings),
                    index, disabled, caption, modal
                context: dict with keys:
                    has_modal, modal_screens, has_choices,
                    choice_labels
            priority: int, lower runs first (default 50)
        """
        _vnf_action_transforms.append((priority, fn))
        _vnf_action_transforms.sort(key=lambda x: x[0])

    def _vnf_apply_action_transforms(actions, context):
        """Run all registered action transforms."""
        for _prio, fn in _vnf_action_transforms:
            try:
                actions = fn(actions, context)
            except _CONTROL_EXCEPTIONS:
                raise
            except Exception:
                if vnf_player.debug:
                    _vnf_log(
                        "Action transform error in {}: {}".format(
                            getattr(fn, "__name__", repr(fn)),
                            _tb_module.format_exc()))
        return actions

    # --- Screen display names ---
    # Mods register friendly names for screen tags.
    # Used in modal headers: [restscreen] -> [Shelter]
    _vnf_screen_display_names = {}

    def _vnf_set_screen_display_name(screen_tag, display_name):
        """Register a friendly display name for a screen tag."""
        _vnf_screen_display_names[screen_tag] = display_name

    def _vnf_get_screen_display_name(screen_tag):
        """Get the friendly display name for a screen tag, or the tag itself."""
        return _vnf_screen_display_names.get(screen_tag, screen_tag)

    # --- Overlay screen registry ---
    # Mods register screens whose text/buttons need overlay scraping. Blocking
    # overlays cover the choice screen; passive overlays (for example a live
    # terminal behind ADV dialogue) are scraped without suppressing input.
    _vnf_overlay_screens = set()
    _vnf_passive_overlay_screens = set()
    _vnf_retained_overlay_screens = set()
    # Presentation contract, separate from the input-blocking flag: a modal
    # overlay is a FULL-SCREEN panel the player sees INSTEAD of the scene, so
    # the consumer must present the panel as the primary surface rather than
    # layering its rows over the scene's story and menu. Blocking alone does
    # not imply this (Roadwarden's journal sits beside its dialogue).
    _vnf_modal_overlay_screens = set()
    _vnf_overlay_instance_serial = 0
    _vnf_overlay_instance_state = {}
    _vnf_overlay_active_instance_tags = set()

    def _vnf_overlay_instance_generation(screen_tag, screen_displayable):
        """Return a monotonic generation for an ordinary screen instance.

        A continuously visible screen may be rebuilt with a replacement
        ScreenDisplayable, so that alone does not start a generation. After a
        sampled absence, however, a different object is a real reopen. Keeping
        the previous object referenced also prevents Python from reusing its
        identity before the comparison.
        """
        global _vnf_overlay_instance_serial
        previous = _vnf_overlay_instance_state.get(screen_tag)
        if previous is None:
            _vnf_overlay_instance_serial += 1
            generation = _vnf_overlay_instance_serial
        elif previous[0] is screen_displayable:
            generation = previous[1]
        elif screen_tag in _vnf_overlay_active_instance_tags:
            generation = previous[1]
        else:
            _vnf_overlay_instance_serial += 1
            generation = _vnf_overlay_instance_serial
        _vnf_overlay_instance_state[screen_tag] = (
            screen_displayable, generation)
        return _vnf_text(generation)

    def _vnf_reset_overlay_instance_generations():
        """Retire screen identities from an abandoned story timeline."""
        global _vnf_overlay_active_instance_tags
        _vnf_overlay_instance_state.clear()
        _vnf_overlay_active_instance_tags = set()

    def _vnf_register_overlay_screen(
            screen_tag, blocking=True, retain_generation=False, modal=False):
        """Register a screen for overlay scraping and delivery semantics.

        ``retain_generation`` keeps the consumer's delivered-row ledger when
        the screen disappears. A mod-provided ``_overlay_generation`` value
        then starts a new ledger explicitly.

        ``modal`` declares the PRESENTATION contract: the panel replaces the
        scene for the player, so the consumer presents its rows and its own
        buttons as the whole surface and reports the covered menu as hidden
        instead of numbering it. A modal overlay is always blocking; passing
        ``modal=True, blocking=False`` is a contradiction and blocking wins.
        """
        _vnf_overlay_screens.add(screen_tag)
        if blocking or modal:
            _vnf_passive_overlay_screens.discard(screen_tag)
        else:
            _vnf_passive_overlay_screens.add(screen_tag)
        if retain_generation:
            _vnf_retained_overlay_screens.add(screen_tag)
        else:
            _vnf_retained_overlay_screens.discard(screen_tag)
        if modal:
            _vnf_modal_overlay_screens.add(screen_tag)
        else:
            _vnf_modal_overlay_screens.discard(screen_tag)

    def _vnf_set_overlay_presentation(screen_tag, mode):
        """Set an overlay screen's presentation mode without re-registering.

        ``"modal"`` makes the panel the primary surface (and blocking);
        ``"layered"`` restores the default delta presentation, where the
        panel's rows are layered over the scene underneath. Unknown modes
        raise rather than silently keeping the old contract.
        """
        if mode not in ("modal", "layered"):
            raise ValueError(
                "unknown overlay presentation mode: " + _vnf_text(mode))
        _vnf_overlay_screens.add(screen_tag)
        if mode == "modal":
            _vnf_passive_overlay_screens.discard(screen_tag)
            _vnf_modal_overlay_screens.add(screen_tag)
        else:
            _vnf_modal_overlay_screens.discard(screen_tag)

    def _vnf_visible_modal_overlay_tags(per_screen, modal_tags):
        """Registered modal-presentation overlays currently on screen.

        Screen order, not registration order: the consumer renders the panel
        bodies in the order the layer stacked them, and a tag that is
        registered but not currently shown must not appear at all.
        """
        tags = []
        for scr_data in (per_screen or []):
            tag = scr_data.get("_tag")
            if not tag or tag not in modal_tags:
                continue
            if tag in tags:
                continue
            tags.append(tag)
        return tags

    # --- Menu screen registry ---
    # Screens whose buttons should be suppressed during gameplay
    # (context == "in_game") but shown at main_menu / game_menu.
    # Common defaults: "navigation", "menu" (StP-style unified menu).
    _vnf_menu_screens = {"navigation", "menu"}

    def _vnf_register_menu_screen(screen_tag):
        """Register a screen tag as a menu/navigation screen."""
        _vnf_menu_screens.add(screen_tag)

    # --- Button category registry ---
    # Mods register display categories for button grouping.
    # Each entry: name -> (header, compact).
    # The formatter reads this from screen_content events to render
    # category headers and choose compact vs numbered display.
    # Default categories (choices, navigation, info, other) are
    # built into the formatter; game mods add their own here.
    _vnf_button_categories = {}

    def _vnf_register_button_category(name, header, compact=False):
        """Register a button display category.

        Args:
            name: Category key (set on buttons via _category field).
            header: Display header (e.g. "WAIT FOR...").
            compact: If True, render as pipe-separated inline list
                     (not numbered, not clickable by index).
        """
        _vnf_button_categories[name] = {
            "header": header,
            "compact": compact,
        }

    # --- Screen section depth registry ---
    # Mods register a section_depth per screen tag.  When the walker
    # reaches that depth, each child container starts a new section.
    # Texts and buttons get a `_section` index so transforms can group
    # by tree position instead of pattern-matching content.

    _vnf_screen_section_depths = {}   # tag -> int

    def _vnf_set_screen_section_depth(screen_tag, depth):
        """Register section-splitting depth for a screen.

        At that depth in the widget tree, each child starts a new
        section (0-indexed).  All descendant texts/buttons inherit
        the section index.
        """
        _vnf_screen_section_depths[screen_tag] = depth

    def _vnf_group_by_section(scr, depth=None):
        """Group a per-screen dict's texts and buttons by section.

        If depth is None, uses pre-computed _section indices.
        If depth is an int, groups by path[depth] child index instead
        (allows grouping at any tree depth without pre-registration).

        Returns {section_int: {"texts": [str, ...], "buttons": [dict, ...]}}.
        Handles missing data gracefully (all items -> section 0).
        """
        groups = {}
        texts = scr.get("texts", [])
        if depth is not None:
            text_paths = scr.get("_text_paths", [])
            for i, t in enumerate(texts):
                _p = text_paths[i] if i < len(text_paths) else ()
                sec = _p[depth] if len(_p) > depth else 0
                groups.setdefault(sec, {"texts": [], "buttons": []})["texts"].append(t)
            for btn in scr.get("buttons", []):
                _p = btn.get("_path", ())
                sec = _p[depth] if len(_p) > depth else 0
                groups.setdefault(sec, {"texts": [], "buttons": []})["buttons"].append(btn)
        else:
            text_sections = scr.get("_text_sections", [])
            for i, t in enumerate(texts):
                sec = text_sections[i] if i < len(text_sections) else 0
                groups.setdefault(sec, {"texts": [], "buttons": []})["texts"].append(t)
            for btn in scr.get("buttons", []):
                sec = btn.get("_section", 0)
                groups.setdefault(sec, {"texts": [], "buttons": []})["buttons"].append(btn)
        return groups

    # --- Interaction list (canonical) ---
    # A unified list of everything the player can do right now.
    # Both display (screen_content / choice_request events) and
    # resolution via the shim act command consumes this list.
    # Built by _vnf_build_interactions() from action-transform output.

    _vnf_current_interactions = []   # latest canonical list
    _vnf_interaction_aliases = {}    # alias_lower -> interaction id
    _vnf_interaction_raw_refs = {}   # interaction id -> (displayable, action_obj, label, screen)

    # Alias providers: mods register fn(interactions) -> interactions
    # to add game-specific aliases (e.g. Travel <-> Map).
    _vnf_alias_providers = []

    def _vnf_add_alias_provider(fn, priority=50):
        """Register an alias provider.

        fn(interactions) -> interactions.
        May append to interaction["aliases"], set ["id"], etc.
        priority: int, lower runs first.
        """
        _vnf_alias_providers.append((priority, fn))
        _vnf_alias_providers.sort(key=lambda x: x[0])

    # Screen/action -> type mapping for categorization.
    _VNF_SCREEN_CATEGORIES = {
        "doubleimage": None,
        "doubleimage2": None,
        "tutorialtooltips": "info",
        "characterstatus": "info",
        "achievements": "info",
    }

    _VNF_NAV_ACTIONS = frozenset({
        "ShowMenu", "QuickSave", "QuickLoad", "Save", "Load",
        "LoadMostRecent", "Quit", "OpenURL",
    })
    # Ren'Py's file actions on a quick menu ("Q.Load" = FileLoad) are
    # navigation chrome too.  The same actions on the game's save/load
    # screens are the slot buttons themselves and keep their category.
    _VNF_QUICK_FILE_ACTIONS = frozenset({
        "FileLoad", "FileSave", "FileTakeScreenshot",
    })
    _VNF_FILE_SLOT_SCREENS = frozenset({"save", "load", "file_slots"})

    def _vnf_categorize_action(a):
        """Assign a type to a unified action dict.

        Returns: "choice", "topic", "nav", "shop", "info", "other",
                 or None to hide.
        """
        if a.get("_interaction_type") is not None:
            return a["_interaction_type"]
        if a["source"] == "choice":
            return "choice"
        screen = a.get("screen", "")
        action_names = set(a.get("actions", []))

        # Screen-based overrides.
        if screen in _VNF_SCREEN_CATEGORIES:
            return _VNF_SCREEN_CATEGORIES[screen]

        # Action-based heuristics.
        # Stock game-menu Return() resumes navigation; Return(value) remains
        # a real decision even if a game puts it on the same screen tag.
        if (screen == "menu" and action_names == set(["Return"])
                and a.get("action_strs") == ["Return"]):
            return "nav"
        if set(action_names).intersection({"ChoiceReturn", "Return"}):
            return "choice"
        if "shopscreen" in screen:
            return "shop"
        if set(action_names).intersection(_VNF_NAV_ACTIONS):
            return "nav"
        if (set(action_names).intersection(_VNF_QUICK_FILE_ACTIONS)
                and screen not in _VNF_FILE_SLOT_SCREENS):
            return "nav"
        if screen in ("menu", "quick_menu"):
            return "nav"
        if "Jump" in action_names and screen == "nvl":
            return "topic"

        # NullAction = informational.
        if action_names and all(
            n in ("NullAction", "None", "none") for n in action_names
        ):
            return "info"

        return "other"

    def _vnf_build_interactions(actions, set_global=True):
        """Build canonical interaction list from action-transform output.

        Args:
            actions: list of action dicts (output of
                     _vnf_apply_action_transforms)
            set_global: if True (default), update _vnf_current_interactions.
                Pass False for auxiliary builds (e.g. screen_content event)
                that should not overwrite the canonical interaction list.

        Returns:
            list of interaction dicts.
        """
        global _vnf_current_interactions, _vnf_interaction_aliases
        global _vnf_interaction_raw_refs

        interactions = []
        seen_ids = {}
        idx = 1
        for a in actions:
            if a.get("hidden"):
                continue
            itype = _vnf_categorize_action(a)
            if itype is None:
                continue

            screen = a.get("screen", "")
            label = a.get("label", "")
            _is_disabled = bool(a.get("disabled") or a.get("caption"))
            # Disabled/caption items don't get a user-facing index so they
            # can't be accidentally selected by number.
            _display_idx = None if _is_disabled else idx
            # Default ID: screen:label or just label for choices.
            if a["source"] == "choice":
                iid = a.get("id") or _vnf_text(_display_idx or label)
            else:
                iid = a.get("id") or "{}:{}".format(
                    screen, label) if screen else label
            if iid in seen_ids:
                seen_ids[iid] += 1
                iid = "{}#{}".format(iid, seen_ids[iid])
            else:
                seen_ids[iid] = 1

            interaction = {
                "id": iid,
                "display_label": label,
                "type": itype,
                "disabled": _is_disabled,
                # Preserve why an unnumbered interaction is unavailable.
                # A game_state-only client fallback must render menu prompts
                # as captions, not as disabled choices.
                "caption": bool(a.get("caption")),
                "aliases": [],
                "source": a["source"],
                "screen": screen,
                "index": _display_idx,
            }
            # Carry through annotation if present.
            if a.get("annotation"):
                interaction["annotation"] = a["annotation"]
            if "is_selected" in a:
                interaction["is_selected"] = bool(a["is_selected"])
            # For choices, keep the value_map index for resolution.
            if a["source"] == "choice":
                interaction["choice_index"] = a.get("choice_value_index") or a.get("index", idx)
            # Action strings for display and matching.
            if a.get("action_strs"):
                interaction["action_strs"] = list(a["action_strs"])
            if a.get("actions"):
                interaction["action_names"] = list(a["actions"])
            # Promoted flag.
            if a.get("promoted"):
                interaction["promoted"] = True
            if a.get("_suppress_pending_action"):
                interaction["_suppress_pending_action"] = True
            # Carry original label when a transform renamed the button.
            if a.get("original_label") is not None:
                interaction["original_label"] = a["original_label"]
            # Carry category for inventory grouping.
            if a.get("_category") is not None:
                interaction["category"] = a["_category"]
            if a.get("_wait_after_action") is not None:
                interaction["wait_after_action"] = bool(a["_wait_after_action"])
            # Distinct from _wait_after_action: "this click runs script and
            # owns the whole opening", not "the UI rebuilds after this frame".
            if a.get("_story_entry") is not None:
                interaction["story_entry"] = bool(a["_story_entry"])

            # Stash inline raw ref directly on the interaction dict
            # so we can pick it up after alias providers run.
            if a["source"] == "button" and a.get("_displayable") is not None:
                interaction["_raw_ref"] = (
                    a["_displayable"], a.get("_action_obj"),
                    a.get("label", ""), a.get("screen", ""))

            interactions.append(interaction)
            if not _is_disabled:
                idx += 1

        # Run alias providers.
        for _prio, fn in _vnf_alias_providers:
            try:
                interactions = fn(interactions)
            except Exception:
                pass

        # Build lookup tables.
        alias_map = {}
        raw_refs = {}
        for itr in interactions:
            iid = itr["id"]
            # Map display_label and aliases.  First-write-wins keeps alias
            # lookup aligned with exact display-label matching when labels
            # collide (for example two visible "Return" buttons).
            dl = _vnf_normalize_quotes(
                itr["display_label"]).lower().strip()
            alias_map.setdefault(dl, iid)
            for al in itr.get("aliases", []):
                alias_map.setdefault(
                    _vnf_normalize_quotes(al).lower().strip(), iid)
            # Pick up inline raw refs stashed during action iteration.
            if itr.get("_raw_ref") is not None:
                raw_refs[iid] = itr.pop("_raw_ref")

        if set_global:
            _vnf_current_interactions = interactions
            _vnf_interaction_aliases = alias_map
            _vnf_interaction_raw_refs = raw_refs

        return interactions

    # --- Anomaly callbacks ---
    # Mods (e.g. watchdog) register callbacks via _vnf_add_anomaly_callback.
    # Called by shim-level guards (menu_indexerror_guard) with an anomaly dict.
    _vnf_anomaly_callbacks = []

    def _vnf_add_anomaly_callback(fn):
        """Register fn(anomaly_dict) to be called on shim-detected anomalies."""
        _vnf_anomaly_callbacks.append(fn)

    def _vnf_fire_anomaly(anomaly):
        """Emit anomaly event and notify registered callbacks."""
        try:
            _vnf_dump_log_ring(anomaly.get("kind", anomaly.get("type", "anomaly")))
        except Exception:
            pass
        _ev = dict(
            type="anomaly",
            kind=anomaly.get("kind", anomaly.get("type", "unknown")),
            details=anomaly)
        # Use sync push for anomalies to ensure delivery during crashes.
        try:
            _vnf_client.push_event_sync(_ev)
        except Exception as _push_err:
            _vnf_log("Anomaly sync push failed: %s" %
                     _vnf_text(_push_err))
            try:
                _vnf_client.push_event(_ev)
            except Exception as _push_err2:
                _vnf_log("Anomaly async push also failed: %s" %
                         _vnf_text(_push_err2))
        for _cb in _vnf_anomaly_callbacks:
            try:
                _cb(anomaly)
            except Exception:
                pass

    # --- Event push hooks ---
    # Mods register callbacks via _vnf_add_event_hook.
    # Called on every push_event/push_event_sync with the event dict.
    _vnf_event_hooks = []

    def _vnf_add_event_hook(fn):
        """Register fn(event_dict) called on every pushed event."""
        _vnf_event_hooks.append(fn)

    def _vnf_fire_event_hooks(event):
        """Notify all registered event hooks. Swallows exceptions."""
        for _cb in _vnf_event_hooks:
            try:
                _cb(event)
            except Exception:
                pass

    # --- Menu lifecycle hooks ---
    # Called at menu entry, exit, and auto-skip with internal state
    # not visible in pushed events.
    _vnf_menu_hooks = []

    def _vnf_add_menu_hook(fn):
        """Register fn(phase, data_dict). phase: 'entry', 'exit', 'auto_skip'."""
        _vnf_menu_hooks.append(fn)

    def _vnf_fire_menu_hooks(phase, data):
        """Notify all registered menu hooks. Swallows exceptions."""
        for _cb in _vnf_menu_hooks:
            try:
                _cb(phase, data)
            except Exception:
                pass

    # --- Custom command handlers ---
    # Mods register handlers via _vnf_add_command_handler(name, fn).
    # Handler signature: fn(cmd_name, cmd_args) — must push its own
    # command_result event via _vnf_client.push_event().
    _vnf_command_handlers = {}
    _vnf_command_causal_boundaries = {}

    def _vnf_add_command_handler(name, fn, causal_boundary=False):
        """Register a command and whether its effects supersede prior acts."""
        _vnf_command_handlers[name] = fn
        _vnf_command_causal_boundaries[name] = bool(causal_boundary)

    _STANDARD_CHOICE_KEYS = frozenset({"label", "disabled", "caption", "index"})

    def _vnf_build_action_list(choices, buttons, modal_screens):
        """Merge choices and buttons into a unified action list.

        Args:
            choices: list of dicts from _vnf_patched_menu (or empty).
                     Standard keys: label, disabled, caption.
                     Extra keys from augmenters are passed through.
            buttons: list of dicts from screen scraping
                     Each has: label, actions, action_strs, screen, modal
            modal_screens: list of screen tags that are modal

        Returns:
            (actions, context) — ready for _vnf_apply_action_transforms
        """
        actions = []
        idx = 1
        has_modal = bool(modal_screens)
        has_choices = bool(choices)
        choice_labels = []

        for c in choices:
            choice_labels.append(c.get("label", ""))
            _a = {
                "label": c.get("label", ""),
                "source": "choice",
                "screen": "",
                "actions": [],
                "action_strs": [],
                "index": idx,
                "choice_value_index": c.get("index"),  # value_map key (None for disabled/caption)
                "disabled": c.get("disabled", False),
                "caption": c.get("caption", False),
                "modal": False,
            }
            # Pass through extra fields from augmenters.
            for _k in c:
                if _k not in _STANDARD_CHOICE_KEYS and _k not in _a:
                    _a[_k] = c[_k]
            actions.append(_a)
            idx += 1

        for b in buttons:
            _bact = {
                "label": b.get("label", ""),
                "source": "button",
                "screen": b.get("screen", ""),
                "actions": list(b.get("actions", [])),
                "action_strs": list(b.get("action_strs", [])),
                "index": b.get("index", idx),
                # A selected pure ShowMenu cannot produce a new boundary.
                # Expose the no-op before admission; selected toggles stay live.
                "disabled": bool(b.get("is_disabled") or (
                    b.get("is_selected") and b.get("actions") == ["ShowMenu"])),
                "is_selected": bool(b.get("is_selected")),
                "caption": False,
                "modal": b.get("screen", "") in modal_screens,
            }
            # Carry raw refs for direct button resolution.
            if b.get("_displayable") is not None:
                _bact["_displayable"] = b["_displayable"]
                _bact["_action_obj"] = b.get("_action_obj")
            # Carry original label when screen transforms renamed the button.
            if b.get("original_label") is not None:
                _bact["original_label"] = b["original_label"]
            # Carry category annotation for inventory grouping.
            _cat = b.get("_category") or b.get("category")
            if _cat is not None:
                _bact["_category"] = _cat
            # Game mods can mark screen buttons that look like ordinary UI
            # actions but actually enter story/script flow.
            if b.get("_wait_after_action") is not None:
                _bact["_wait_after_action"] = bool(b["_wait_after_action"])
            if b.get("_story_entry") is not None:
                _bact["_story_entry"] = bool(b["_story_entry"])
            actions.append(_bact)
            idx += 1

        context = {
            "has_modal": has_modal,
            "modal_screens": list(modal_screens),
            "has_choices": has_choices,
            "choice_labels": choice_labels,
            "shared": _vnf_pipeline_shared,
        }
        return actions, context

    # --- Built-in action transforms ---

    def _vnf_action_transform_modal_filter(actions, context):
        """When a modal screen is active, hide buttons from non-modal
        screens.  This prevents interacting with elements behind
        overlays (e.g. shop modal over NVL hub)."""
        if not context.get("has_modal"):
            return actions
        modal_set = set(context.get("modal_screens", []))
        return [
            a for a in actions
            if a["source"] == "choice"  # choices always visible
            or a.get("screen", "") in modal_set  # button on modal screen
        ]

    def _vnf_action_transform_dedup_choice_button(actions, context):
        """When both a choice and a button have the same label, keep
        only the choice to avoid double-listing."""
        if not context.get("has_choices"):
            return actions
        choice_labels = set(context.get("choice_labels", []))
        choice_keys = set(
            _vnf_normalize_focus_label(label) for label in choice_labels)
        deduped = []
        for action in actions:
            if action["source"] == "choice":
                deduped.append(action)
                continue
            if action["label"] in choice_labels:
                continue
            action_names = set(action.get("actions", []))
            is_focus_choice = (
                action.get("screen") == "_focus_list"
                and bool(action_names.intersection({"ChoiceReturn", "Return"}))
            )
            if (is_focus_choice
                    and _vnf_normalize_focus_label(action.get("label", ""))
                    in choice_keys):
                continue
            deduped.append(action)
        return deduped

    def _vnf_action_transform_unavailable_roll_forward(actions, context):
        """Hide transient RollForward chrome with no replayable history."""
        try:
            if renpy.exports.roll_forward_info() is not None:
                return actions
        except Exception:
            # Unknown engine shapes retain the visible control; execution
            # remains fail-closed in the replay command itself.
            return actions
        return [
            action for action in actions
            if "RollForward" not in action.get("actions", [])
        ]

    _vnf_add_action_transform(_vnf_action_transform_modal_filter, priority=10)
    _vnf_add_action_transform(_vnf_action_transform_dedup_choice_button, priority=20)
    _vnf_add_action_transform(
        _vnf_action_transform_unavailable_roll_forward, priority=30)

    # =========================================================================
    # State Machine Classes
    # =========================================================================
    # Plain classes with string constants — Python 2 compatible (no Enum/dataclass).
    # Each groups related flags into a single object with a reset() method.

    class _VNFRequestState(object):
        """State machine for the request lifecycle.

        Replaces: _vnf_active_request_id, _vnf_active_value_map,
        _vnf_active_is_input, _vnf_received_action, _vnf_external_mode,
        _vnf_observation_start, _vnf_observation_target,
        _vnf_observation_focus_applied, _vnf_observation_choice_scrolled,
        _vnf_observation_choice_len, _vnf_scroll_correction_done.
        """
        IDLE = "idle"
        POLLING = "polling"
        RECEIVED = "received"
        OBSERVING = "observing"
        RESOLVING = "resolving"

        def __init__(self):
            self.state = self.IDLE
            self.request_id = None          # was _vnf_active_request_id
            self.value_map = None           # was _vnf_active_value_map
            self.is_input = False           # was _vnf_active_is_input
            self.received_action = None     # was _vnf_received_action
            self.external_mode = False      # was _vnf_external_mode
            self.obs_start = 0.0            # was _vnf_observation_start
            self.obs_target = None          # was _vnf_observation_target
            self.focus_applied = False      # was _vnf_observation_focus_applied
            self.choice_scrolled = False    # was _vnf_observation_choice_scrolled
            self.choice_len = 0             # was _vnf_observation_choice_len
            self.scroll_correction = False  # was _vnf_scroll_correction_done
            self.obs_deadline = 0.0         # bounded visual-observation lease
            self.obs_last_progress = 0.0    # last bridge lease renewal

        def reset(self):
            """Return to IDLE state, clear all context."""
            self.__init__()

    class _VNFAutoSkipState(object):
        """State machine for auto-skip / auto-resolve.

        Replaces: _vnf_auto_resolve_next[0], _vnf_auto_resolve_after[0],
        _vnf_auto_resolve_highlighted[0], _vnf_auto_resolve_choices[0],
        _vnf_auto_resolved_flag[0], _vnf_auto_skip_last_label[0],
        _vnf_auto_skip_just_skipped[0], _vnf_auto_skip_pause_reasons.
        """
        def __init__(self):
            self.resolve_value = None       # was _vnf_auto_resolve_next[0]
            self.resolve_after = 0          # was _vnf_auto_resolve_after[0]
            self.highlighted = False         # was _vnf_auto_resolve_highlighted[0]
            self.resolve_choices = None     # was _vnf_auto_resolve_choices[0]
            self.resolved_flag = False      # was _vnf_auto_resolved_flag[0]
            self.last_label = None          # was _vnf_auto_skip_last_label[0]
            self.last_text = None
            self.just_skipped = False       # was _vnf_auto_skip_just_skipped[0]
            self.pause_reasons = []         # was _vnf_auto_skip_pause_reasons

        def reset(self):
            """Clear all auto-skip/resolve state."""
            self.__init__()

        def clear_resolve(self):
            """Clear just the auto-resolve portion (keep skip tracking)."""
            self.resolve_value = None
            self.resolve_after = 0
            self.highlighted = False
            self.resolve_choices = None

    class _VNFPreResolveState(object):
        """State machine for pre-resolve step sequences.

        Replaces: _vnf_pre_resolve_steps, _vnf_pre_resolve_step_idx,
        _vnf_pre_resolve_step_start, _vnf_pre_resolve_step_focus,
        _vnf_pre_resolve_final_target, _vnf_pre_resolve_final_value_map.
        """
        def __init__(self):
            self.steps = []                 # was _vnf_pre_resolve_steps
            self.step_idx = 0               # was _vnf_pre_resolve_step_idx
            self.step_start = 0.0           # was _vnf_pre_resolve_step_start
            self.step_focus = False         # was _vnf_pre_resolve_step_focus
            self.final_target = None        # was _vnf_pre_resolve_final_target
            self.final_value_map = None     # was _vnf_pre_resolve_final_value_map
            self.button_only = False         # True = no menu resolution after steps

        def reset(self):
            """Clear all pre-resolve state."""
            self.__init__()

    class _VNFScrollState(object):
        """State machine for NVL auto-scroll.

        Replaces: _vnf_nvl_scroll_start_time, _vnf_nvl_scroll_range,
        _vnf_nvl_scroll_adj, _vnf_nvl_scroll_content_hash,
        _vnf_nvl_scroll_done_time.
        """
        def __init__(self):
            self.start_time = 0.0           # was _vnf_nvl_scroll_start_time
            self.scroll_range = 0.0         # was _vnf_nvl_scroll_range
            self.adj = None                 # was _vnf_nvl_scroll_adj
            self.content_hash = 0           # was _vnf_nvl_scroll_content_hash
            self.done_time = 0.0            # was _vnf_nvl_scroll_done_time

        def reset(self):
            """Clear all scroll state."""
            self.__init__()

    class _VNFExceptionState(object):
        """State for exception/error screen detection.

        Replaces: _vnf_scrape_visible_screens._exception_flag,
        _vnf_scrape_visible_screens._error_notified.
        """
        def __init__(self):
            self.exception_flag = False     # was _vnf_scrape_visible_screens._exception_flag
            self.error_notified = False     # was _vnf_scrape_visible_screens._error_notified

        def reset(self):
            """Clear exception state."""
            self.__init__()

        def consume_detected(self, visible_error=False):
            """Return current error state while consuming the handler edge."""
            detected = bool(visible_error or self.exception_flag)
            self.exception_flag = False
            if not detected:
                self.error_notified = False
            return detected

    # --- Global state machine instances ---
    _vnf_request = _VNFRequestState()
    _vnf_autoskip = _VNFAutoSkipState()
    _vnf_preresolve = _VNFPreResolveState()
    _vnf_scroll = _VNFScrollState()
    _vnf_exception = _VNFExceptionState()

    def _vnf_current_autoskip_text():
        try:
            return (
                _vnf_last_what
                or getattr(renpy.store, "_last_say_what", None)
                or _vnf_auto_advance_last_what
            )
        except Exception:
            return None

    def _vnf_is_autoskip_loop(label):
        if not (_vnf_autoskip.just_skipped and label == _vnf_autoskip.last_label):
            return False
        current_text = _vnf_current_autoskip_text()
        previous_text = getattr(_vnf_autoskip, "last_text", None)
        # Compare without str(): on plain-Py2 builds str() of a
        # non-ASCII unicode label raises UnicodeEncodeError.
        return (current_text or u"") == (previous_text or u"")

    def _vnf_auto_skip_predicate_allows(label):
        # Mod predicates run raw inside the menu wrapper's unguarded
        # zone — a buggy predicate must not crash the host menu.
        # On error, don't auto-skip: skipping wrongly loses story
        # content irreversibly; not skipping is recoverable.
        pred = vnf_player.auto_skip_predicate
        if not pred:
            return True
        try:
            return pred(label)
        except Exception:
            if vnf_player.debug:
                _tb_module.print_exc()
            return False

    def _vnf_refresh_transform_pause_reasons():
        """Let newly-opened overlays pause automation before it resolves."""
        try:
            _pause_showing = _vnf_get_showing_screens(
                vnf_player.scrape_visible_list)
            if not _pause_showing:
                return bool(_vnf_autoskip.pause_reasons)
            _vnf_autoskip.pause_reasons[:] = []
            _pause_per = []
            for _pause_name, _pause_screen in _pause_showing:
                try:
                    if (_pause_screen.child is None
                            and hasattr(_pause_screen, "update")):
                        try:
                            _pause_screen.update()
                        except Exception:
                            pass
                    _pause_data = {
                        "_tag": _pause_name, "modal": False,
                        "texts": [], "choices": [], "value_map": {},
                        "buttons": [],
                    }
                    _vnf_walk_screen(_pause_screen, _pause_data)
                    _pause_per.append(_pause_data)
                except Exception:
                    pass
            if _pause_per:
                _vnf_apply_screen_transforms(_pause_per)
        except Exception:
            pass
        return bool(_vnf_autoskip.pause_reasons)

    # Pre-sets to apply before resolving a choice.
    # Dict mapping choice index (1-based) to list of (field, value) tuples.
    # Set by action transforms (e.g. attitude buttons mapped to choices).
    _vnf_choice_pre_sets = {}

    # --- User mouse activity tracker ---
    # Detects whether the user is actively moving the mouse by
    # comparing the current cursor position against the last
    # shim-parked position.  Shim moves are teleports via
    # set_mouse_pos; user moves are gradual MOUSEMOTION events
    # that drift from the parked position.
    class _VNFMouseTracker(object):
        DRIFT_THRESHOLD = 15  # pixels — ignore sub-pixel jitter

        def __init__(self):
            self.park_x = 0
            self.park_y = 0
            self.park_time = 0.0

        def parked(self, x, y):
            """Record a shim-initiated cursor park."""
            self.park_x = int(x)
            self.park_y = int(y)
            self.park_time = _time.time()

        def sync(self):
            """Reset the drift baseline without moving the host pointer."""
            try:
                import pygame_sdl2 as pygame
                self.parked(*pygame.mouse.get_pos())
            except Exception:
                pass

        def is_user_active(self):
            """True if the cursor has drifted from the park position."""
            if not vnf_player.move_host_pointer:
                return False
            try:
                recovering = (
                    _vnf_exception.exception_flag
                    or bool(getattr(renpy.game, "after_rollback", False))
                )
            except Exception:
                recovering = False
            if recovering:
                self.sync()
                return False
            try:
                import pygame_sdl2 as pygame
                mx, my = pygame.mouse.get_pos()
                dx = abs(mx - self.park_x)
                dy = abs(my - self.park_y)
                return (dx > self.DRIFT_THRESHOLD
                        or dy > self.DRIFT_THRESHOLD)
            except Exception:
                return False

    _vnf_mouse = _VNFMouseTracker()

    # Wrap set_mouse_pos so all shim cursor moves are recorded, and force
    # a tiny NONZERO duration. Ren'Py's set_mouse_pos defaults duration=0,
    # which builds a MouseMove whose perform() does
    # `done = elapsed / self.duration`. That divides only when
    # `elapsed < duration`, which with duration 0 needs a momentarily
    # negative elapsed (clock skew / non-monotonic timer) — rare, but the
    # shim parks the cursor thousands of times in a long run, so the race
    # eventually hits and ZeroDivisionError crashes the game at the next
    # interact (observed at Roadwarden's Old Bridge menu after ~8h). A
    # sub-millisecond duration is an imperceptible teleport and makes the
    # division by zero impossible.
    _vnf_orig_set_mouse_pos = _vnf_save_original("set_mouse_pos", renpy.exports, "set_mouse_pos")
    def _vnf_set_mouse_pos(x, y, *args, **kwargs):
        _vnf_mouse.parked(x, y)
        if not args and "duration" not in kwargs:
            kwargs["duration"] = 0.001
        return _vnf_orig_set_mouse_pos(x, y, *args, **kwargs)
    if not _is_renpy6:
        renpy.exports.set_mouse_pos = _vnf_set_mouse_pos

    def _vnf_move_mouse(x, y, *args, **kwargs):
        """Apply a vnflight-owned visual cursor move when presentation allows."""
        if not vnf_player.move_host_pointer:
            return None
        if _is_renpy6:
            _vnf_mouse.parked(x, y)
            return _vnf_orig_set_mouse_pos(x, y, *args, **kwargs)
        return _vnf_set_mouse_pos(x, y, *args, **kwargs)

    _vnf_native_action_queue = None
    # True while the scrape sees Ren'Py's own exception screen. Read by
    # _vnf_error_screen_action_pump.
    _vnf_error_screen_visible = [False]
    # Pending click: set by the shim act command when the normal command
    # queue can't execute (e.g. Show overlay blocking interaction).
    # The scraper picks it up on the next cycle and executes directly.
    # Format: {"label": str, "screen": str} or None.
    _vnf_pending_click = None

    # Timestamp of the last shim-initiated action (button action, choice,
    # auto-skip, auto-advance).  Used to detect user interactions:
    # if significant game state changes without a recent shim action,
    # we infer the user clicked something.
    _vnf_last_shim_action_time = 0.0

    def _vnf_execute_native_action_once():
        if not vnf_player.enabled:
            return
        global _vnf_native_action_queue, _vnf_last_shim_action_time, _vnf_pending_click
        act = _vnf_native_action_queue
        # CLEAR IMMEIDATELY BEFORE RUNNING to prevent endless loops on control-flow exceptions.
        _vnf_native_action_queue = None
        if act is not None:
            # Clear pending_click too — native path succeeded, no need for fallback.
            _vnf_pending_click = None
            _vnf_last_shim_action_time = _time.time()
            # Mark as shim-resolved so the menu wrapper (if triggered by
            # this action) doesn't tag it as a user click.
            _vnf_shim_resolved_flag[0] = True
            try:
                rv = renpy.run(act)
            except renpy.game.JumpOutException as _joe:
                # Start()/ShowMenu() etc. raise JumpOutException to exit the
                # current context.  This only works from the main menu
                # context; during gameplay the exception escapes the
                # top-level run_context and crashes the game.
                if getattr(renpy.store, "main_menu", False):
                    raise  # Safe — main menu context handles it.
                _label = _joe.args[0] if _joe.args else "?"
                _vnf_log("Suppressed JumpOutException({}) — not safe during gameplay".format(_label))
                _vnf_client.push_event(dict(
                    type="command_result", command="act",
                    success=False,
                    error="Button requires context jump ({}); not safe during gameplay".format(_label)))
                return
            # Actions like Return resolve interactions by returning a
            # value.  When fired from a screen timer the value would be
            # silently discarded, so propagate it explicitly.
            if rv is not None:
                renpy.end_interaction(rv)
    # State for observation delay → _vnf_request (instance of _VNFRequestState)
    # Kept as module-level aliases for backward compatibility with mods.
    _vnf_active_call_screen_name = None  # Track the screen name currently in a call_screen interaction

    # State for button observation delay (Feature 2: click highlighting).
    _vnf_button_observation_start = 0.0
    _vnf_button_observation_target = None   # (displayable, action_obj, label, screen_name)
    _vnf_button_observation_focus_applied = False

    # State for pre_resolve step sequences (Feature 3).
    # Dict: value_map_idx -> list of step dicts.
    _vnf_choice_pre_resolve_steps = {}
    # Pre-resolve execution state → _vnf_preresolve (instance of _VNFPreResolveState)

    # Monkey-patch Ren'Py's infinite-loop detector.
    # In developer mode, check_infinite_loop() clamps il_time to now+60
    # after every 1000 statements, and not_infinite_loop() can only reset
    # during interact().  After a Jump from a shim action, the game can
    # execute a long chain of non-interactive statements (conditionals,
    # scene transitions) where periodic callbacks don't fire, and the
    # 60s deadline can expire.  This patch resets il_time on every check
    # but logs when the original would have crashed (real infinite loop).
    _vnf_cil_orig = _vnf_save_original("check_infinite_loop", renpy.execution, "check_infinite_loop")
    _vnf_loop_check_count = [0]       # batches of 1000 stmts since last interact
    _vnf_loop_last_interact = [0.0]   # timestamp of last interact callback
    _vnf_loop_warned = [False]        # already warned for this spin

    def _vnf_patched_check_infinite_loop():
        # When the shim is disabled, stay inert: delegate to Ren'Py's
        # own infinite-loop protection instead of suppressing it.
        try:
            _vnf_cil_enabled = bool(vnf_player.enabled)
        except Exception:
            _vnf_cil_enabled = False
        if not _vnf_cil_enabled:
            # Resolve the original from the sys registry at CALL time using
            # ONLY builtins. During Shift+R, Ren'Py wipes the store before
            # re-running init, but renpy.execution.check_infinite_loop still
            # points at this function — so every global lookup here (including
            # _vnf_cil_orig and _sys_mod) raises NameError. That NameError
            # surfaced during script load, left init_system_styles unset and
            # killed the process, which is why Shift+R "did nothing" with the
            # shim installed: the game died instead of reloading.
            try:
                _cil_reg = getattr(__import__("sys"), "_vnf_patch_originals", None)
                _cil_orig = _cil_reg.get("check_infinite_loop") if _cil_reg else None
            except Exception:
                _cil_orig = None
            if _cil_orig is not None:
                _cil_orig()
            return
        import time as _time
        now = _time.time()
        renpy.execution.il_time = now + 60
        # Guard: during Shift+R reload the patched function persists
        # but module-level state variables are wiped before re-init.
        try:
            _vnf_loop_check_count[0] += 1
        except (NameError, IndexError):
            return

        # If several batches (1000 statements each) have run since the
        # last interact callback, we're likely in a real non-interactive loop.
        if _vnf_loop_check_count[0] >= 5 and not _vnf_loop_warned[0]:
            _vnf_loop_warned[0] = True
            elapsed = now - _vnf_loop_last_interact[0]
            # Capture current execution position.
            try:
                ctx_current = renpy.game.context().current
            except Exception:
                ctx_current = "?"
            # Get the game script location from the call stack.
            import traceback as _tb
            frames = _tb.format_stack()
            # Find the game script frame (game/*.rpy).
            game_frame = ""
            for f in frames:
                if "game/" in f and ".rpy" in f:
                    game_frame = f.strip()
            renpy.write_log("[LLM Player] WARNING: possible infinite loop detected "
                            "({} batches, {:.1f}s since last interact). "
                            "Current node: {}. Location: {}".format(
                                _vnf_loop_check_count[0], elapsed, ctx_current, game_frame))

        try:
            _cil_reg = getattr(__import__("sys"), "_vnf_patch_originals", None)
            _cil_orig = _cil_reg.get("check_infinite_loop") if _cil_reg else None
            if _cil_orig is not None:
                _cil_orig()
        except Exception:
            pass

    def _vnf_reset_loop_counter():
        """Called from interact callbacks to reset the loop detector."""
        import time as _time
        _vnf_loop_check_count[0] = 0
        _vnf_loop_last_interact[0] = _time.time()
        _vnf_loop_warned[0] = False

    if not _is_renpy6:
        renpy.execution.check_infinite_loop = _vnf_patched_check_infinite_loop
    # Register the reset as an interact callback so it fires on every interact().
    if not _is_renpy6:
        renpy.config.interact_callbacks.append(_vnf_reset_loop_counter)


    # Diagnostic: log when full_restart is called so we can trace unexpected
    # returns to main menu.
    _vnf_orig_full_restart = _vnf_save_original("full_restart", renpy.exports, "full_restart")

    def _vnf_diag_full_restart(transition=False, label="_invoke_main_menu", target="_main_menu", **kwargs):
        import traceback as _tb_mod
        frames = "".join(_tb_mod.format_stack())
        renpy.write_log("[LLM Player] DIAG: full_restart called. label={}, target={}. Stack:\n{}".format(
            label, target, frames))
        import inspect as _insp
        _all_kw = dict(transition=transition, label=label, target=target)
        _all_kw.update(kwargs)
        _valid = None
        _accepts_kwargs = True
        try:
            _spec_func = getattr(_insp, "getfullargspec", None)
            if _spec_func is None:
                _spec_func = getattr(_insp, "getargspec", None)
            if _spec_func is not None:
                _args = _spec_func(_vnf_orig_full_restart)
                _valid = set(_args.args) if _args.args else set()
                _accepts_kwargs = bool(
                    getattr(_args, "varkw", None)
                    or getattr(_args, "keywords", None)
                )
        except Exception:
            _valid = None
        if _valid is None or _accepts_kwargs:
            _fwd = _all_kw
        else:
            _fwd = {k: v for k, v in _all_kw.items() if k in _valid}
        _vnf_orig_full_restart(**_fwd)

    renpy.exports.full_restart = _vnf_diag_full_restart
    renpy.full_restart = _vnf_diag_full_restart

    # Game-state variable names to include in fallthrough diagnostics.
    # Mods register their game's interesting variables via
    # _vnf_set_diag_state_vars; the universal shim logs none by default.
    _vnf_diag_state_vars = []

    def _vnf_set_diag_state_vars(names):
        global _vnf_diag_state_vars
        _vnf_diag_state_vars = list(names or [])

    # Diagnostic: log when the execution context returns normally
    # (= script fell through / label exhausted with no return destination).
    _vnf_orig_run_context = _vnf_save_original("run_context", renpy.execution, "run_context")

    def _vnf_diag_run_context(top):
        # Enable line logging so we can see the last statements before
        # fallthrough.  Debug-only: line_log accumulates an entry per
        # executed statement for the whole session, which is real
        # memory on multi-day runs — and a disabled shim must not
        # change engine behavior at all.
        _want_line_log = False
        try:
            _want_line_log = bool(vnf_player.enabled and vnf_player.debug)
        except Exception:
            pass
        _old_line_log = getattr(renpy.config, "line_log", False)
        if _want_line_log and hasattr(renpy.config, "line_log"):
            renpy.config.line_log = True
        try:
            rv = _vnf_orig_run_context(top)
            # If we get here, the script fell through without an exception.
            try:
                ctx = renpy.game.context()
                last_node = getattr(ctx, "current", "?")
                # Capture the last 2000 entries from the line log.
                # Capture the last 200 entries from the line log.
                line_log = getattr(ctx, "line_log", [])
                tail = line_log[-200:] if line_log else []
                log_entries = []
                for entry in tail:
                    fn = getattr(entry, "filename", "?")
                    ln = getattr(entry, "line", getattr(entry, "linenumber", "?"))
                    node_type = type(getattr(entry, "node", None)).__name__
                    abnormal = getattr(entry, "abnormal", False)
                    log_entries.append("  {}:{} ({}{})".format(fn, ln, node_type, " abnormal" if abnormal else ""))
            except Exception as _diag_e:
                last_node = "?"
                log_entries = ["(error capturing line log: {})".format(
                    _vnf_text(_diag_e))]
            # Also capture key game state variables.  The list is
            # mod-provided (game-specific names don't belong in the
            # universal shim) — empty by default.
            _diag_vars = {}
            for _dv in _vnf_diag_state_vars:
                try:
                    _diag_vars[_dv] = getattr(renpy.store, _dv, "<undef>")
                except Exception:
                    _diag_vars[_dv] = "<error>"
            renpy.write_log("[LLM Player] DIAG: run_context returned normally "
                            "(top={}, rv={}, last_node={}).\n"
                            "Game state: {}\n"
                            "Last {} of {} statements:\n{}".format(
                                top, rv, last_node,
                                _diag_vars,
                                len(log_entries),
                                len(line_log) if line_log else 0,
                                "\n".join(log_entries)))
            return rv
        except Exception:
            raise
        finally:
            if hasattr(renpy.config, "line_log"):
                renpy.config.line_log = _old_line_log

    if not _is_renpy6:
        renpy.execution.run_context = _vnf_diag_run_context

    def _vnf_poll_worker(req_id):
        """Background thread to poll for actions without blocking the UI thread."""
        _deadline = (_time_monotonic() + vnf_player.action_timeout) if _vnf_request.external_mode else 0
        _timed_out = False
        _vnf_request.state = _VNFRequestState.POLLING
        while _vnf_request.request_id == req_id:
            action = _vnf_client.poll_action(req_id)
            # Re-check after the blocking HTTP poll: the request may
            # have been cleared or replaced while the call was in
            # flight — attaching this action to the replacement would
            # resolve the wrong menu.
            if _vnf_request.request_id != req_id:
                break
            if action:
                _vnf_request.received_action = action
                _vnf_request.state = _VNFRequestState.RECEIVED
                break
            # In external mode, enforce timeout — unblock mouse so the
            # user can take over via the rendered GUI menu.
            if _deadline > 0 and _time_monotonic() > _deadline:
                _vnf_log("Timeout waiting for external action ({}s). "
                         "Unblocking mouse for user fallback.".format(
                         vnf_player.action_timeout))
                _vnf_request.external_mode = False
                _vnf_unblock_mouse()
                _vnf_client.push_event(dict(
                    type="external_timeout",
                    timeout=vnf_player.action_timeout,
                    message="Timed out waiting for bridge action. GUI menu is now interactive."))
                _deadline = 0   # Don't fire timeout again.
                _timed_out = True
                # Keep polling — the agent may submit an action later.
            _time.sleep(1.0 if _timed_out else vnf_player.poll_interval)

    def _vnf_begin_observation(target):
        """Arm one bounded visual observation and return its maximum duration."""
        _started = _time.time()
        try:
            _max_duration = min(
                90.0, max(30.0, float(vnf_player.action_timeout)))
        except Exception:
            _max_duration = 90.0
        _vnf_request.obs_target = target
        _vnf_request.obs_start = _started
        _vnf_request.obs_deadline = _started + _max_duration
        _vnf_request.obs_last_progress = _started
        _vnf_request.focus_applied = False
        _vnf_request.scroll_correction = False
        _vnf_request.state = _VNFRequestState.OBSERVING
        return _max_duration

    def _vnf_periodic_action_poll():
        """
        Called ~20 times/sec from config.periodic_callbacks.
        If an action was received by the poll thread, start the
        observation phase (highlight delay for choices, proportional
        delay for input) instead of resolving immediately.

        On 6.x, also polls inline (no background thread) to avoid
        GIL contention causing urllib2 timeouts.
        """
        if not vnf_player.enabled:
            return
        if _vnf_request.request_id is None:
            return

        # Already in observation phase — nothing to do here.
        if _vnf_request.obs_start > 0.0:
            return

        action = _vnf_request.received_action
        if action is None:
            return

        # Consume action and start observation phase.
        _vnf_request.received_action = None
        if _vnf_request.external_mode:
            # External-only: skip observation delay entirely.
            # Set target and let _vnf_execute_observation resolve
            # on its next tick (delay will be 0).
            if _vnf_request.is_input:
                _target = action.get("text", "")
                _vnf_begin_observation(_target)
                _vnf_show_input_text(_vnf_request.obs_target)
            else:
                _vnf_begin_observation(action.get("index", 1))
            _vnf_client.push_event(dict(
                type="observation_started",
                delay=0,
                max_duration=0,
            ))
            return
        if _vnf_request.is_input:
            _max_obs_duration = _vnf_begin_observation(
                action.get("text", ""))
            # Don't show text yet — the typing animation in
            # _vnf_execute_observation will reveal it char by char.
            _obs_delay = len(_vnf_request.obs_target) / max(vnf_player.typing_speed, 1.0) + vnf_player.post_typing_delay
        else:
            _max_obs_duration = _vnf_begin_observation(
                action.get("index", 1))
            # Move the mouse cursor to the target choice button and
            # set focus so it gets native hover styling.
            _vnf_highlight_choice(_vnf_request.obs_target)
            # Compute delay from scrolled state and choice text length
            # (set by _vnf_highlight_choice above).
            if _vnf_request.choice_scrolled:
                _obs_delay = _vnf_request.choice_len * vnf_player.choice_delay_speed_scroll + vnf_player.choice_delay_offset_scroll
            else:
                _obs_delay = _vnf_request.choice_len * vnf_player.choice_delay_speed_no_scroll + vnf_player.choice_delay_offset_no_scroll
        # Tell clients how long the observation will take so they
        # can extend their wait timeout accordingly.
        _vnf_client.push_event(dict(
            type="observation_started",
            delay=_obs_delay,
            max_duration=_max_obs_duration,
        ))

    def _vnf_show_input_text(text):
        """
        Find the active Input widget and set its displayed text.

        Tries the standard 'input' screen first (Ren'Py 7.x/8.x), then
        falls back to scanning the focus_list for any Input widget (6.x
        games like DDLC that use custom screens with Input widgets).

        Returns True if the text was applied (or already matched).
        """
        try:
            widget = renpy.display.screen.get_displayable("input", "input")
            if widget is None:
                # Fallback: find Input widget in focus_list (6.x compat).
                try:
                    for _fi in renpy.display.focus.focus_list:
                        _fw = getattr(_fi, "widget", None)
                        if _fw is not None and type(_fw).__name__ == "Input":
                            widget = _fw
                            break
                except Exception:
                    pass
            if widget is None:
                return False
            if getattr(widget, "content", None) == text:
                return True  # already showing the right text
            widget.content = text
            widget.caret_pos = len(text)
            if hasattr(widget, "update_text"):
                widget.update_text(text, widget.editable)
            renpy.exports.restart_interaction()
            return True
        except Exception:
            return False  # best-effort — input still resolves after the delay

    def _vnf_find_choice_scrollbar():
        """
        Find the scrollbar Bar widget in the choice screen's focus list.
        Returns its adjustment object, or None.
        """
        try:
            for f in renpy.display.focus.focus_list:
                scr = getattr(f, "screen", None)
                if scr is None:
                    continue
                name = getattr(scr, "screen_name", None)
                if isinstance(name, tuple) and name:
                    name = name[0]
                if name not in ("choice", "nvl"):
                    continue
                if type(f.widget).__name__ == "Bar":
                    adj = getattr(f.widget, "adjustment", None)
                    if adj is not None:
                        return adj
        except Exception:
            pass
        return None

    def _vnf_prescroll_to_choice(target_idx, total_choices, candidates):
        """
        If the target button is not completely visible on screen, mathematically snap
        the viewport seamlessly to its proportional depth without any bouncy iterative loops.
        """
        try:
            adj = _vnf_find_choice_scrollbar()
            if adj is None:
                return False

            adj_range = getattr(adj, "range", 0)
            adj_page = getattr(adj, "page", 0)
            if adj_range <= 0:
                return False

            # Calculate precise geometric proportion of the list
            # e.g. target 6 out of 11 -> (6 - 1) / (11 - 1) = 0.50
            if total_choices <= 1:
                proportion = 0.0
            else:
                proportion = float(target_idx - 1) / float(total_choices - 1)

            # Snap scroll directly to the mathematically ideal center
            scroll_to = adj_range * proportion

            # Prevent microscopic floating point jitters
            cur_val = getattr(adj, "value", 0)
            if abs(scroll_to - cur_val) < 1.0:
                pass
            else:
                adj.change(scroll_to)

            _vnf_request.choice_scrolled = True
            if vnf_player.debug:
                _vnf_log("Highlight: Pre-scrolled proportionally to {:.0f} for index {} of {} (range={})".format(
                    scroll_to, target_idx, total_choices, adj_range))
            return True
        except Exception:
            return False  # still waiting — tell caller to retry later

    def _vnf_scroll_to_widget(focus_entry):
        """
        If the focused widget is inside a scrollable viewport, adjust
        the viewport's yadjustment so the widget is visible.

        Returns True if scrolling was performed.
        """
        adj = _vnf_find_choice_scrollbar()
        if adj is None:
            return False
        try:
            fy = focus_entry.y
            fh = focus_entry.h
            vp_y = getattr(adj, "value", 0)
            vp_page = getattr(adj, "page", 0)
            if vp_page <= 0:
                return False
            visible_top = vp_y
            visible_bottom = vp_y + vp_page
            if fy < visible_top:
                adj.change(fy)
                return True
            elif fy + fh > visible_bottom:
                adj.change(fy + fh - vp_page)
                return True
        except Exception:
            pass
        return False

    # -------------------------------------------------------------------------
    # NVL auto-scroll state  → _vnf_scroll (instance of _VNFScrollState)
    # -------------------------------------------------------------------------

    def _vnf_find_nvl_viewport_adj():
        """Find the yadjustment of the NVL Viewport widget in the focus list."""
        try:
            for f in renpy.display.focus.focus_list:
                scr = getattr(f, "screen", None)
                if scr is None:
                    continue
                name = getattr(scr, "screen_name", None)
                if isinstance(name, tuple) and name:
                    name = name[0]
                if name != "nvl":
                    continue
                if type(f.widget).__name__ == "Viewport":
                    adj = getattr(f.widget, "yadjustment", None)
                    if adj is not None and getattr(adj, "range", 0) > 0:
                        return adj
        except Exception:
            pass
        return None

    def _vnf_nvl_auto_scroll_tick():
        """Periodic tick for smooth NVL viewport scrolling.

        Called from the scrape timer.  Manages its own state via _vnf_scroll:
        - Detects when NVL viewport has scrollable content
        - Waits nvl_auto_scroll_delay before starting
        - Smoothly increments scroll position
        - Stops when bottom is reached or content changes
        """
        if not vnf_player.nvl_auto_scroll:
            return

        adj = _vnf_find_nvl_viewport_adj()
        if adj is None:
            # No scrollable NVL viewport — reset state.
            if _vnf_scroll.start_time > 0:
                _vnf_scroll.start_time = 0.0
                _vnf_scroll.adj = None
            return

        adj_range = getattr(adj, "range", 0)
        if adj_range <= 0:
            return

        _now = _time.time()

        # Detect new scroll session (content changed or first time).
        _cur_hash = hash((adj_range, getattr(adj, "page", 0)))
        if _cur_hash != _vnf_scroll.content_hash:
            _vnf_scroll.content_hash = _cur_hash
            _vnf_scroll.start_time = _now
            _vnf_scroll.scroll_range = adj_range
            _vnf_scroll.adj = adj
            _vnf_scroll.done_time = 0.0
            return  # start delay from now

        # restart_interaction() may rebuild the viewport and its Adjustment
        # without changing the content geometry. Keep the scroll session, but
        # follow the live object; otherwise the choice-resolution gate below
        # can watch an abandoned Adjustment forever.
        if adj is not _vnf_scroll.adj:
            _vnf_scroll.adj = adj
            if getattr(adj, "value", 0) < adj_range:
                _vnf_scroll.done_time = 0.0

        # Already at bottom — record completion time.
        cur_val = getattr(adj, "value", 0)
        if cur_val >= adj_range:
            if _vnf_scroll.done_time <= 0:
                _vnf_scroll.done_time = _now
            return

        # Wait for initial delay.
        elapsed = _now - _vnf_scroll.start_time
        if elapsed < vnf_player.nvl_auto_scroll_delay:
            return

        # Calculate scroll speed (pixels per second).
        if vnf_player.nvl_auto_scroll_speed > 0:
            px_per_sec = vnf_player.nvl_auto_scroll_speed
        elif vnf_player.reading_cps > 0:
            # Estimate: viewport page holds ~N characters worth of text.
            # reading_cps chars/sec => page_height / reading_time_per_page
            # Rough heuristic: ~2 chars per vertical pixel of text.
            _page = getattr(adj, "page", 500)
            _chars_per_page = _page * 2.0
            _secs_per_page = _chars_per_page / vnf_player.reading_cps
            px_per_sec = _page / _secs_per_page if _secs_per_page > 0 else 50
        else:
            px_per_sec = 50  # fallback

        # How far we should be by now.
        scroll_elapsed = elapsed - vnf_player.nvl_auto_scroll_delay
        target = min(scroll_elapsed * px_per_sec, adj_range)

        if target > cur_val:
            adj.change(target)
            renpy.exports.restart_interaction()

    def _vnf_nvl_scroll_in_progress():
        """Return True if NVL auto-scroll is active and hasn't settled.

        Includes a 2s grace period after reaching the bottom so the
        viewer can read the last lines before a choice fires.
        """
        if not vnf_player.nvl_auto_scroll:
            return False
        if _vnf_scroll.start_time <= 0:
            return False
        adj = _vnf_scroll.adj
        live_adj = _vnf_find_nvl_viewport_adj()
        if live_adj is not None and live_adj is not adj:
            _vnf_scroll.adj = live_adj
            adj = live_adj
            if getattr(adj, "value", 0) < getattr(adj, "range", 0):
                _vnf_scroll.done_time = 0.0
        if adj is None:
            return False
        adj_range = getattr(adj, "range", 0)
        if adj_range <= 0:
            return False
        cur_val = getattr(adj, "value", 0)
        if cur_val < adj_range:
            return True
        # Scroll reached bottom — grace period.
        _now = _time.time()
        if _vnf_scroll.done_time <= 0:
            _vnf_scroll.done_time = _now
        return (_now - _vnf_scroll.done_time) < 2.0

    def _vnf_block_mouse():
        """Block mouse click events (for external mode)."""
        if not vnf_player.enabled:
            return
        try:
            import pygame_sdl2 as pygame
            pygame.event.set_blocked(pygame.MOUSEBUTTONDOWN)
            pygame.event.set_blocked(pygame.MOUSEBUTTONUP)
        except Exception:
            pass

    def _vnf_unblock_mouse():
        """Restore mouse events blocked during observation."""
        try:
            import pygame_sdl2 as pygame
            pygame.event.set_allowed(pygame.MOUSEMOTION)
            pygame.event.set_allowed(pygame.MOUSEBUTTONDOWN)
            pygame.event.set_allowed(pygame.MOUSEBUTTONUP)
        except Exception:
            pass

    def _vnf_ensure_choice_visible(focus_entry):
        """
        Post-highlight correction: if the focused widget is clipped at the
        viewport edge, nudge the scroll to make it fully visible.

        Converts the widget's screen-space coordinates to content-space
        using the viewport's screen origin (from the scrollbar Bar) and
        the current scroll offset, then checks against the adjustment's
        visible window [value, value+page].
        """
        try:
            fl = renpy.display.focus.focus_list
            if not fl:
                return

            # Find the scrollbar Bar on the choice/nvl screen to get
            # both the adjustment and the viewport's screen-space origin.
            adj = None
            vp_screen_top = 0
            for f in fl:
                if f.x is None:
                    continue
                scr = getattr(f, "screen", None)
                if scr is None:
                    continue
                name = getattr(scr, "screen_name", None)
                if isinstance(name, tuple) and name:
                    name = name[0]
                if name not in ("choice", "nvl"):
                    continue
                if type(f.widget).__name__ == "Bar":
                    adj = getattr(f.widget, "adjustment", None)
                    vp_screen_top = f.y
                    vp_screen_h = f.h
                    break

            if adj is None:
                return
            adj_range = getattr(adj, "range", 0)
            adj_page = getattr(adj, "page", 0)
            _vp_padding = vp_screen_h - adj_page if vp_screen_h > adj_page else 0
            if adj_range <= 0 or adj_page <= 0:
                return

            # The focus entry's h covers the clickable area but not bottom
            # spacing/padding that's visually part of the choice.  Use the
            # viewport padding plus a fraction of the widget height as margin
            # to account for inter-item spacing and visual overflow.
            margin = _vp_padding + max(40, int(focus_entry.h * 0.5))
            cur_val = getattr(adj, "value", 0)

            # Convert widget screen-space y to content-space y.
            widget_content_y = focus_entry.y - vp_screen_top + cur_val
            widget_content_bottom = widget_content_y + focus_entry.h

            # Visible content window: [cur_val, cur_val + adj_page]
            if widget_content_bottom > cur_val + adj_page - margin:
                # Widget bottom is clipped — scroll so it's fully visible.
                new_val = min(adj_range, widget_content_bottom - adj_page + margin)
                if new_val - cur_val > 1.0:
                    adj.change(new_val)
                    _vnf_request.choice_scrolled = True
                    if vnf_player.debug:
                        _vnf_log("Highlight: Post-scroll down to {:.0f} (content_bottom={:.0f}, page={:.0f})".format(
                            new_val, widget_content_bottom, adj_page))
            elif widget_content_y < cur_val + margin:
                # Widget top is clipped — scroll up.
                new_val = max(0, widget_content_y - margin)
                if cur_val - new_val > 1.0:
                    adj.change(new_val)
                    _vnf_request.choice_scrolled = True
                    if vnf_player.debug:
                        _vnf_log("Highlight: Post-scroll up to {:.0f} (content_y={:.0f})".format(
                            new_val, widget_content_y))
        except Exception:
            pass

    def _vnf_apply_visual_highlight(focus_entry, label_text=None):
        """
        Apply visual highlight to a focus list entry: move focus, park
        the cursor on the widget center, and block mouse events.

        Sets ``_vnf_request.focus_applied`` to True on success.
        If *label_text* is provided, updates ``_vnf_request.choice_len``
        with its stripped length (for delay calculation).

        Returns True if highlight was applied.
        """
        try:
            if label_text is not None:
                try:
                    _vnf_request.choice_len = len(renpy.text.extras.filter_text_tags(label_text, allow=set()))
                except Exception:
                    _vnf_request.choice_len = len(label_text)

            try:
                renpy.display.focus.change_focus(focus_entry, default=False)
            except Exception:
                pass

            try:
                _vnf_move_mouse(
                    int(focus_entry.x + focus_entry.w / 2),
                    int(focus_entry.y + focus_entry.h / 2),
                )
            except Exception:
                pass

            try:
                import pygame_sdl2 as pygame
                pygame.event.set_blocked(pygame.MOUSEMOTION)
                if vnf_player.lock_input_during_observation:
                    pygame.event.set_blocked(pygame.MOUSEBUTTONDOWN)
                    pygame.event.set_blocked(pygame.MOUSEBUTTONUP)
            except Exception:
                pass

            _vnf_request.focus_applied = True
            return True
        except Exception:
            pass
        return False

    def _vnf_highlight_button(displayable, label, screen_name=None):
        """
        Find a button's displayable in the focus list and apply visual
        highlight.  Used by the click observation pipeline to show cursor
        movement and hover styling before firing the button action.

        Strategy 1: identity match (``f.widget is displayable``).
        Strategy 2: label text + screen name match (fallback).

        Returns True if highlight was applied.
        """
        try:
            fl = renpy.display.focus.focus_list
            if not fl:
                if vnf_player.debug:
                    _vnf_log("highlight_button: focus list empty")
                return False

            # Strategy 1: identity match
            for f in fl:
                if f.x is None:
                    continue
                if f.widget is displayable:
                    if vnf_player.debug:
                        _vnf_log("highlight_button: identity match for {!r}".format(label[:40] if label else "?"))
                    return _vnf_apply_visual_highlight(f, label)

            # Strategy 2: label + screen match
            if label:
                try:
                    clean_target = _vnf_normalize_quotes(
                        renpy.text.extras.filter_text_tags(label, allow=set())).strip()
                except Exception:
                    clean_target = label

                for f in fl:
                    if f.x is None:
                        continue
                    # Check screen matches if specified.
                    if screen_name:
                        scr = getattr(f, "screen", None)
                        if scr is not None:
                            sn = getattr(scr, "screen_name", None)
                            if isinstance(sn, tuple) and sn:
                                sn = sn[0]
                            if sn != screen_name:
                                continue

                    # Extract text from widget.
                    widget_label = None
                    try:
                        w = f.widget
                        ch_list = getattr(w, "children", [])
                        ch_single = getattr(w, "child", None)
                        for src in [ch_list, [ch_single] if ch_single else []]:
                            for ch in src:
                                if ch is None:
                                    continue
                                t = getattr(ch, "text", None)
                                if t is None:
                                    continue
                                try:
                                    widget_label = "".join(_vnf_text(x) for x in t)
                                except TypeError:
                                    widget_label = _vnf_text(t) if t else None
                                if widget_label:
                                    break
                            if widget_label:
                                break
                    except Exception:
                        pass

                    if widget_label:
                        try:
                            clean_widget = _vnf_normalize_quotes(
                                renpy.text.extras.filter_text_tags(widget_label, allow=set())).strip()
                        except Exception:
                            clean_widget = widget_label
                        if clean_target and clean_target in clean_widget:
                            if vnf_player.debug:
                                _vnf_log("highlight_button: label match {!r} in {!r}".format(
                                    clean_target[:30], clean_widget[:30]))
                            return _vnf_apply_visual_highlight(f, label)

                if vnf_player.debug:
                    _vnf_log("highlight_button: no match for {!r} (screen={})".format(
                        clean_target[:40], screen_name))

        except Exception:
            if vnf_player.debug:
                _tb_module.print_exc()
        return False

    def _vnf_highlight_choice(target_idx):
        """
        Find the choice button for *target_idx* (1-based) in the current
        focus list, move the OS mouse cursor to its centre, and set
        internal focus so hover styles apply immediately.

        First tries value-based matching (reliable on Ren'Py 8.x where
        ChoiceReturn objects are used).  Falls back to label-based and
        positional matching — sorting interactive buttons top-to-bottom
        and picking the *target_idx*-th one — for games with non-standard
        choice actions or scrollable viewports.

        Returns True if focus was successfully applied.
        """
        global _vnf_old_max_default, _vnf_active_choices
        try:
            choice_label = None
            if _vnf_active_choices and 1 <= target_idx <= len(_vnf_active_choices):
                choice_label = _vnf_active_choices[target_idx - 1].get("label", "")

            fl = renpy.display.focus.focus_list
            if not fl:
                if vnf_player.debug: _vnf_log("Highlight: Focus list empty")
                return False



            # Collect candidate focusable buttons (have position + action).
            candidates = []
            if vnf_player.debug:
                _vnf_log("Highlight: Inspecting focus list ({} items)".format(len(fl)))

            # Phase 1: Identify "choice" widgets.
            exclusive_screens = ("choice", "nvl")

            def get_scr_name(f):
                scr = getattr(f, "screen", None)
                if scr is None: return None
                # ScreenDisplayable.screen_name is a tuple like ('choice',)
                name = getattr(scr, "screen_name", None)
                if isinstance(name, tuple) and name: return name[0]
                if isinstance(name, str): return name
                return _vnf_text(name)

            has_exclusive = any(get_scr_name(f) in exclusive_screens for f in fl)

            for i, f in enumerate(fl):
                if f.x is None:
                    continue

                scr_name = get_scr_name(f)
                action = getattr(f.widget, "action", None)
                if action is None:
                    action = getattr(f.widget, "clicked", None)

                if vnf_player.debug and i < 30:
                    _vnf_log("  [{}] screen={}, widget={}, action={}, x={}, y={}, w={}, h={}".format(
                        i, scr_name, type(f.widget).__name__, type(action).__name__, f.x, f.y, f.w, f.h))

                if has_exclusive and scr_name not in exclusive_screens:
                    continue
                if scr_name in ("quick_menu", "navigation"):
                    continue

                if action is not None:
                    is_full_screen = (f.w >= renpy.config.screen_width - 10 and f.h >= renpy.config.screen_height - 10)
                    is_tiny = (f.w < 20 or f.h < 20)
                    if not is_full_screen and not is_tiny:
                        candidates.append(f)

            if not candidates:
                if vnf_player.debug:
                    _vnf_log("Highlight: No suitable candidates found in {} items".format(len(fl)))
                return False

            matched = None
            total_choices = len(_vnf_active_choices) if _vnf_active_choices else 0



            # Strategy 1: value-based matching
            # Safely unpacks single actions or lists of actions (e.g. Play + ChoiceReturn).
            if _vnf_request.value_map:
                target_value = _vnf_request.value_map.get(target_idx)
                if target_value is not None:
                    for f in candidates:
                        raw_action = getattr(f.widget, "action", None)
                        actions = raw_action if _vnf_is_sequence(raw_action) else [raw_action]

                        for act in actions:
                            v = act
                            while v is not None and hasattr(v, "value"):
                                v = v.value
                            if v == target_value:
                                matched = f
                                break
                        if matched:
                            break

                    if matched and vnf_player.debug:
                        _vnf_log("Highlight: Value-based match for index {}".format(target_idx))

            # Strategy 2: Label-based matching (robust for custom screens or scrolled areas)
            if matched is None and _vnf_active_choices:
                try:
                    target_label = _vnf_active_choices[target_idx - 1]["label"]
                    # Strip Ren'Py text tags for comparison.
                    try:
                        clean_target = renpy.text.extras.filter_text_tags(target_label, allow=set())
                    except Exception:
                        clean_target = target_label

                    for f in candidates:
                        # Extract text from button widget.
                        label = None
                        try:
                            w = f.widget
                            ch_list = getattr(w, "children", [])
                            ch_single = getattr(w, "child", None)
                            # Direct access: Button.children[0].text or Button.child.text
                            for src in [ch_list, [ch_single] if ch_single else []]:
                                for ch in src:
                                    if ch is None:
                                        continue
                                    t = getattr(ch, "text", None)
                                    if t is None:
                                        continue
                                    try:
                                        label = "".join(_vnf_text(x) for x in t)
                                    except TypeError:
                                        label = _vnf_text(t) if t else None
                                    if label:
                                        break
                                if label:
                                    break
                        except Exception:
                            pass
                        if label:
                            try:
                                clean_label = renpy.text.extras.filter_text_tags(label, allow=set())
                            except Exception:
                                clean_label = label
                            if clean_target and clean_target in clean_label:
                                matched = f
                                break
                    if matched and vnf_player.debug:
                        _vnf_log("Highlight: Label-based match for index {}".format(target_idx))
                except Exception:
                    pass



            # Strategy 4: if no match found and there's a scrollbar,
            # scroll the viewport toward the target and retry next tick.
            # If the target is physically sitting at the absolute top or bottom
            # of the cached displayable list (i.e. partially cut off by window borders),
            # trigger our mathematical proportional snapper to securely center it!
            if matched is not None and total_choices > len(candidates) and len(candidates) > 2:
                try:
                    local_idx = candidates.index(matched)
                    # If it's one of the extreme edge items, it's dangerously close to culling.
                    if local_idx <= 0 or local_idx >= len(candidates) - 1:
                        # Prevent snapping if it's literally the absolute first/last item of the entire menu hierarchy
                        # (since they are meant to be at the physical limits).
                        if target_idx > 1 and target_idx < total_choices:
                            if _vnf_prescroll_to_choice(target_idx, total_choices, candidates):
                                return False
                except Exception:
                    pass

            if matched is None and total_choices > 0:
                if _vnf_prescroll_to_choice(target_idx, total_choices, candidates):
                    return False

            if matched is None:
                if vnf_player.debug:
                    _vnf_log("Highlight: FAILED to match index {} ({} candidates)".format(target_idx, len(candidates)))
                return False

            return _vnf_apply_visual_highlight(matched, choice_label)
        except Exception:
            pass  # best-effort — if it fails the choice still resolves after the delay
        return False

    def _vnf_execute_observation():
        """
        Periodic callback that checks whether the observation delay has
        elapsed and, if so, finalises the external action.

        For choices the delay is dynamic, based on the choice text length
        and whether scrolling was involved.
        For input the delay is proportional to text length:
        ``len(text) / typing_speed + post_typing_delay``.
        """
        if not vnf_player.enabled:
            return
        if _vnf_request.obs_start <= 0.0:
            return

        _obs_now = _time.time()
        elapsed = _obs_now - _vnf_request.obs_start
        _deadline_reached = bool(
            _vnf_request.obs_deadline > 0.0
            and _obs_now >= _vnf_request.obs_deadline)

        try:
            # External mode: resolve immediately, no visual delays.
            if _vnf_request.external_mode:
                _vnf_finish_observation()
                return

            if _vnf_request.is_input:
                target_text = _vnf_request.obs_target or ""
                typing_speed = max(vnf_player.typing_speed, 1.0)
                typing_duration = len(target_text) / typing_speed

                # Reveal characters one-by-one based on elapsed time.
                chars_to_show = min(int(elapsed * typing_speed), len(target_text))
                _vnf_show_input_text(target_text[:chars_to_show])

                if (_deadline_reached
                        or elapsed >= typing_duration + vnf_player.post_typing_delay):
                    # Ensure the full text is shown before submitting.
                    _vnf_show_input_text(target_text)
                    _vnf_finish_observation()
            else:
                # If focus wasn't applied on the first attempt (e.g. the
                # focus_list wasn't ready yet), retry once.  After that
                # just wait — no continuous restart_interaction spam.
                if not _vnf_request.focus_applied:
                    _vnf_highlight_choice(_vnf_request.obs_target)

                # Post-focus scroll correction: runs on the tick AFTER
                # focus was applied.  By now Ren'Py has re-rendered with
                # the new scroll position and focus, so the focused
                # widget's screen coordinates are accurate.
                if _vnf_request.focus_applied and not _vnf_request.scroll_correction:
                    _vnf_request.scroll_correction = True
                    _focused = renpy.display.focus.get_focused()
                    if _focused is not None:
                        # Find the focus entry for the currently focused widget.
                        for _fe in renpy.display.focus.focus_list:
                            if _fe.widget is _focused and _fe.x is not None:
                                _vnf_ensure_choice_visible(_fe)
                                # Re-park cursor on the (possibly moved) widget.
                                try:
                                    _vnf_move_mouse(
                                        int(_fe.x + _fe.w / 2),
                                        int(_fe.y + _fe.h / 2),
                                    )
                                except Exception:
                                    pass
                                # Re-block mouse so user movement can't
                                # unfocus the highlight before commit.
                                try:
                                    import pygame_sdl2 as pygame
                                    pygame.event.set_blocked(pygame.MOUSEMOTION)
                                except Exception:
                                    pass
                                break

                # Calculate dynamic delay based on scrolling and character count
                if _vnf_request.choice_scrolled:
                    speed = vnf_player.choice_delay_speed_scroll
                    offset = vnf_player.choice_delay_offset_scroll
                else:
                    speed = vnf_player.choice_delay_speed_no_scroll
                    offset = vnf_player.choice_delay_offset_no_scroll

                choice_delay = _vnf_request.choice_len * speed + offset

                _nvl_scrolling = _vnf_nvl_scroll_in_progress()
                if _deadline_reached or (
                        elapsed >= choice_delay and not _nvl_scrolling):
                    if _deadline_reached and _nvl_scrolling:
                        _vnf_log(
                            "Observation deadline reached while NVL scroll "
                            "was still active; resolving the queued choice.")
                    _vnf_finish_observation()
                elif (_obs_now - _vnf_request.obs_last_progress >= 10.0):
                    _vnf_request.obs_last_progress = _obs_now
                    _vnf_client.push_event(dict(
                        type="observation_progress",
                        command="act",
                        elapsed=elapsed,
                        phase=("nvl_scroll" if _nvl_scrolling
                               else "choice_delay"),
                    ))
        except _EndInteraction:
            raise  # MUST propagate — this is how the interaction is resolved
        except _CONTROL_EXCEPTIONS:
            raise  # Re-raise all Ren'Py control-flow exceptions
        except Exception:
            _vnf_finish_observation()

    def _vnf_execute_button_observation():
        """
        Periodic callback for button action observation delay.
        Mirrors ``_vnf_execute_observation`` for screen-button actions:
        highlights the button, waits for a delay, then fires the action.
        """
        global _vnf_button_observation_start, _vnf_button_observation_target
        global _vnf_button_observation_focus_applied
        global _vnf_native_action_queue, _vnf_pending_click

        if not vnf_player.enabled:
            return
        if _vnf_button_observation_start <= 0.0:
            return

        elapsed = time.time() - _vnf_button_observation_start

        try:
            _disp, action_obj, btn_label, btn_screen = _vnf_button_observation_target

            # Retry highlight if not applied on first attempt.
            if not _vnf_button_observation_focus_applied:
                if _vnf_highlight_button(_disp, btn_label, btn_screen):
                    _vnf_button_observation_focus_applied = True

            # Calculate delay from label length.
            try:
                clean_len = len(renpy.text.extras.filter_text_tags(btn_label, allow=set()))
            except Exception:
                clean_len = len(btn_label)
            delay = clean_len * vnf_player.click_delay_speed + vnf_player.click_delay_offset

            if elapsed >= delay:
                # Park cursor at center before unblocking.
                try:
                    _vnf_move_mouse(
                        renpy.config.screen_width // 2,
                        renpy.config.screen_height // 2,
                    )
                except Exception:
                    pass

                _vnf_unblock_mouse()

                # Clear observation state.
                _vnf_button_observation_start = 0.0
                _vnf_button_observation_target = None
                _vnf_button_observation_focus_applied = False

                _vnf_native_action_queue = action_obj
                _vnf_pending_click = {
                    "label": btn_label,
                    "screen": btn_screen,
                }

                _obs_click_acts = action_obj if _vnf_is_sequence(action_obj) else [action_obj]
                _obs_click_null = all(
                    a.__class__.__name__ == "NullAction" or a is None
                    for a in _obs_click_acts
                )
                _obs_result_ev = dict(
                    type="command_result", command="act",
                    success=True,
                    label=btn_label,
                    screen=btn_screen,
                    note="Action queued natively (after highlight).")
                if _obs_click_null:
                    _obs_result_ev["no_effect"] = True
                _vnf_client.push_event(_obs_result_ev)

                # Trigger screen re-evaluation so the vnf_command_poller
                # screen sees the new queue value and fires the timer.
                renpy.restart_interaction()

        except _CONTROL_EXCEPTIONS:
            raise
        except Exception:
            # On error, clean up and fire immediately.
            _fallback_target = _vnf_button_observation_target
            _vnf_unblock_mouse()
            _vnf_button_observation_start = 0.0
            _vnf_button_observation_target = None
            _vnf_button_observation_focus_applied = False
            try:
                if _fallback_target is not None:
                    _disp, action_obj, btn_label, btn_screen = _fallback_target
                    _vnf_native_action_queue = action_obj
                    _vnf_pending_click = {
                        "label": btn_label,
                        "screen": btn_screen,
                    }
                    _vnf_client.push_event(dict(
                        type="command_result", command="act",
                        success=True,
                        label=btn_label,
                        screen=btn_screen,
                        note="Action queued natively (button observation fallback)."))
                    renpy.restart_interaction()
            except _CONTROL_EXCEPTIONS:
                raise
            except Exception:
                pass

    def _vnf_complete_pre_resolve_sequence():
        """
        Finish the pre_resolve sequence by feeding back into the
        normal choice observation pipeline.  This highlights the
        target dialogue choice and waits for the standard delay
        before resolving — giving the viewer time to read the text.

        For button-only sequences (no menu choice to resolve), the
        step already fired the action via renpy.run() — just reset.
        """
        if _vnf_preresolve.button_only:
            _vnf_preresolve.reset()
            return

        target = _vnf_preresolve.final_target
        value_map = _vnf_preresolve.final_value_map

        # Clear pre_resolve state.
        _vnf_preresolve.reset()

        # Re-enter the choice observation pipeline so the target
        # choice gets highlighted with the standard delay.
        _vnf_request.value_map = value_map
        _vnf_begin_observation(target)
        _vnf_highlight_choice(target)
        _vnf_log("Pre-resolve done, entering choice observation for target {}".format(target))

    def _vnf_match_step_button(step, found_buttons):
        """
        Match a pre_resolve click_button step against collected buttons.

        Supports two match strategies that can be combined:
        - **label**: substring match on the button's extracted text
        - **action field/value**: match a SetField action that sets
          ``match_action_field`` to ``match_action_value``

        When both are specified, action match takes priority (labels
        are unreliable for ImageButtons).  Returns the matched
        ``(displayable, action, label, screen)`` tuple or None.
        """
        match_field = step.get("match_action_field")
        match_value = step.get("match_action_value")
        btn_label = step.get("label", "")

        # Strategy 1: action field/value match (most reliable for ImageButtons).
        if match_field is not None:
            for fb in found_buttons:
                _fb_actions = fb[1]
                if not _vnf_is_sequence(_fb_actions):
                    _fb_actions = [_fb_actions]
                for act in _fb_actions:
                    # SetField(object, field, value) — check field and value.
                    act_field = getattr(act, "field", None)
                    act_value = getattr(act, "value", None)
                    if act_field == match_field:
                        if match_value is None or act_value == match_value:
                            return fb
            # Also check nested action lists (e.g. [SetField, Function]).
            for fb in found_buttons:
                _fb_actions = fb[1]
                if not _vnf_is_sequence(_fb_actions):
                    _fb_actions = [_fb_actions]
                for act in _fb_actions:
                    # Some actions wrap others; check .action attribute.
                    inner = getattr(act, "action", None)
                    if inner is not None:
                        inner_list = inner if _vnf_is_sequence(inner) else [inner]
                        for ia in inner_list:
                            if getattr(ia, "field", None) == match_field:
                                if match_value is None or getattr(ia, "value", None) == match_value:
                                    return fb

        # Strategy 2: label substring match (works for text buttons).
        if btn_label:
            btn_label_lower = btn_label.lower().strip()
            for fb in found_buttons:
                if btn_label_lower in fb[2].lower():
                    return fb

        return None

    def _vnf_execute_pre_resolve_steps():
        """
        Periodic callback that executes pre_resolve steps one at a time.
        Each step highlights a button, waits, then fires it.
        After all steps, resolves the menu via ``_vnf_complete_pre_resolve_sequence``.
        """
        if not vnf_player.enabled:
            return
        if not _vnf_preresolve.steps:
            return

        steps = _vnf_preresolve.steps
        idx = _vnf_preresolve.step_idx

        # All steps done — resolve.
        if idx >= len(steps):
            _vnf_complete_pre_resolve_sequence()
            return

        step = steps[idx]
        step_type = step.get("type", "")

        try:
            if step_type == "click_button":
                # First tick for this step: find and highlight button.
                if _vnf_preresolve.step_start <= 0.0:
                    _vnf_preresolve.step_start = time.time()
                    _vnf_preresolve.step_focus = False
                    btn_label = step.get("label", "")
                    _vnf_log("Pre-resolve step {}: click_button {!r}".format(idx, btn_label))

                    # Find button on visible screens.
                    found_buttons = []
                    for sname, scr in _vnf_get_showing_screens():
                        try:
                            btns = []
                            _vnf_collect_button_actions(scr, btns, sname)
                            found_buttons.extend(btns)
                        except Exception:
                            pass

                    target_btn = _vnf_match_step_button(step, found_buttons)

                    if target_btn:
                        _disp, _act, _lbl, _scrn = target_btn
                        _vnf_preresolve.step_focus = _vnf_highlight_button(
                            _disp, _lbl, _scrn)
                        # Store action for firing after delay.
                        step["_action"] = _act
                        step["_displayable"] = _disp
                    else:
                        _vnf_log("Pre-resolve step {}: button {!r} not found, skipping".format(
                            idx, btn_label))
                        # Skip this step.
                        _vnf_preresolve.step_idx = idx + 1
                        _vnf_preresolve.step_start = 0.0
                        return

                # Wait for delay.
                elapsed = time.time() - _vnf_preresolve.step_start
                delay = step.get("delay", vnf_player.pre_resolve_step_delay)

                # Retry highlight if not applied.
                if not _vnf_preresolve.step_focus:
                    found_buttons = []
                    for sname, scr in _vnf_get_showing_screens():
                        try:
                            btns = []
                            _vnf_collect_button_actions(scr, btns, sname)
                            found_buttons.extend(btns)
                        except Exception:
                            pass
                    retry_btn = _vnf_match_step_button(step, found_buttons)
                    if retry_btn:
                        _vnf_preresolve.step_focus = _vnf_highlight_button(
                            retry_btn[0], retry_btn[2], retry_btn[3])
                        step["_action"] = retry_btn[1]

                if elapsed >= delay:
                    # Fire the button action.
                    action = step.get("_action")
                    if action is not None:
                        try:
                            _vnf_unblock_mouse()
                            _vnf_request.focus_applied = False
                            _vnf_request.scroll_correction = False
                            renpy.run(action)
                        except _CONTROL_EXCEPTIONS:
                            raise
                        except Exception:
                            pass

                    # Advance to next step.
                    _vnf_preresolve.step_idx = idx + 1
                    _vnf_preresolve.step_start = 0.0
                    _vnf_preresolve.step_focus = False

            elif step_type == "wait":
                if _vnf_preresolve.step_start <= 0.0:
                    _vnf_preresolve.step_start = time.time()
                    _vnf_log("Pre-resolve step {}: wait {}s".format(
                        idx, step.get("duration", vnf_player.pre_resolve_step_delay)))

                elapsed = time.time() - _vnf_preresolve.step_start
                duration = step.get("duration", vnf_player.pre_resolve_step_delay)
                if elapsed >= duration:
                    _vnf_preresolve.step_idx = idx + 1
                    _vnf_preresolve.step_start = 0.0
            else:
                # Unknown step type, skip.
                _vnf_log("Pre-resolve step {}: unknown type {!r}, skipping".format(idx, step_type))
                _vnf_preresolve.step_idx = idx + 1
                _vnf_preresolve.step_start = 0.0

        except _CONTROL_EXCEPTIONS:
            raise
        except Exception as e:
            _vnf_log("Pre-resolve step error: {}".format(_vnf_text(e)))
            # On error, skip to completion.
            _vnf_complete_pre_resolve_sequence()

    def _vnf_is_generic_game_menu_showing(allow_main_menu=False):
        """Whether Ren'Py's generic game-menu context owns input."""
        try:
            if getattr(renpy.store, "main_menu", False):
                if not allow_main_menu or renpy.exports.get_screen("main_menu") is not None:
                    return False
            if not getattr(renpy.context(), "_menu", False):
                return False
            # A call_screen can run from a label inside the game-menu
            # context. All bundled Ren'Py generations mark that interaction
            # as "screen"; the wrapper's own ui.interact() does not. Preserve
            # the nested caller's return contract instead of treating its
            # transient tag as generic Escape ownership.
            if renpy.exports.current_interact_type() == "screen":
                return False
            return any(
                _tag == "menu"
                for _tag, _scr in _vnf_get_showing_screens()
            )
        except Exception:
            return False

    def _vnf_has_modal_overlay():
        """Return the name of a modal overlay screen if one is showing, else None.

        Uses renpy.exports.get_screen() per tag to get the actual
        ScreenDisplayable (whose .modal attribute is reliable).
        The layer-level displayable returned by _vnf_get_showing_screens
        may be a wrapper without .modal on Ren'Py 7.
        """
        try:
            for _tag, _scr in _vnf_get_showing_screens():
                if (_tag == "menu"
                        and _vnf_is_generic_game_menu_showing()):
                    return _tag
                # First try the displayable from the layer.
                if getattr(_scr, "modal", False):
                    return _tag
                # Fallback: ask Ren'Py for the canonical ScreenDisplayable.
                try:
                    _sd = renpy.exports.get_screen(_tag)
                    if _sd is not None and getattr(_sd, "modal", False):
                        return _tag
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _vnf_has_transient_custom_screen():
        """Return a custom screen owned by ``call screen``, if one is live.

        Every bundled Ren'Py generation shows a called screen with
        ``_transient=True``. Unlike ``modal``, that is an ownership marker:
        non-modal called screens still define the return contract of their
        caller, so a generic Return(None) is not safe for them.
        """
        try:
            _generic_game_menu = _vnf_is_generic_game_menu_showing()
            _called_interaction = (
                renpy.exports.current_interact_type() == "screen")
            for _tag, _scr in _vnf_get_showing_screens():
                if (_tag in ("choice", "nvl")
                        and not _called_interaction):
                    continue
                if _tag == "menu" and _generic_game_menu:
                    continue
                if getattr(_scr, "transient", False):
                    return _tag
                try:
                    _sd = renpy.exports.get_screen(_tag)
                    if _sd is not None and getattr(_sd, "transient", False):
                        return _tag
                except Exception:
                    pass
        except Exception:
            pass
        return None

    def _vnf_finish_observation():
        """Resolve the interaction after the observation delay."""
        global _vnf_old_max_default

        _vnf_request.state = _VNFRequestState.RESOLVING
        # Refuse to resolve if a modal overlay screen is blocking.
        # The overlay must be dismissed before choices
        # can be submitted — otherwise the game enters a bad state.
        modal = _vnf_has_modal_overlay()
        if modal:
            _vnf_log("Blocked choice: modal screen '{}' is active".format(modal))
            _vnf_request.obs_start = 0.0
            _vnf_request.obs_target = None
            _vnf_request.obs_deadline = 0.0
            _vnf_request.obs_last_progress = 0.0
            _vnf_unblock_mouse()
            _vnf_client.push_event(dict(
                type="command_result", command="act",
                success=False,
                error="Modal screen '{}' is blocking. Dismiss it first.".format(modal)))
            return

        target = _vnf_request.obs_target
        is_input = _vnf_request.is_input
        value_map = _vnf_request.value_map

        # Check for pre_resolve steps before resolving.
        if (not is_input and vnf_player.pre_resolve_enabled
                and _vnf_choice_pre_resolve_steps
                and target in _vnf_choice_pre_resolve_steps):
            _vnf_preresolve.steps = list(_vnf_choice_pre_resolve_steps.pop(target))
            _vnf_preresolve.step_idx = 0
            _vnf_preresolve.step_start = 0.0
            _vnf_preresolve.step_focus = False
            _vnf_preresolve.final_target = target
            _vnf_preresolve.final_value_map = value_map

            # Clear observation state but keep mouse blocked.
            _vnf_request.obs_start = 0.0
            _vnf_request.obs_target = None
            _vnf_log("Starting pre_resolve sequence ({} steps) for target {}".format(
                len(_vnf_preresolve.steps), target))
            return

        # Park cursor at screen centre BEFORE unblocking mouse events.
        # After resolution the screen redraws and the old button
        # position may land on hover-sensitive elements (e.g.
        # Roadwarden's characterstatus tooltip).  Centre is the
        # safest neutral spot and causes minimal parallax shift.
        try:
            _vnf_move_mouse(
                renpy.config.screen_width // 2,
                renpy.config.screen_height // 2,
            )
        except Exception:
            pass

        _vnf_unblock_mouse()

        # Clear observation state.
        _vnf_request.obs_start = 0.0
        _vnf_request.obs_target = None
        _vnf_request.obs_deadline = 0.0
        _vnf_request.obs_last_progress = 0.0

        global _vnf_last_shim_action_time, _vnf_synthetic_input_req
        _vnf_last_shim_action_time = _time.time()
        _vnf_shim_resolved_flag[0] = True
        # Clear synthetic input tracking so the scraper doesn't
        # try to cancel an already-resolved request.
        if is_input and _vnf_synthetic_input_req is not None:
            _vnf_synthetic_input_req = None
        try:
            if is_input:
                renpy.exports.end_interaction(target)
            else:
                # Apply pre-sets (e.g. attitude) before resolving.
                if _vnf_choice_pre_sets and target in _vnf_choice_pre_sets:
                    for _ps_field, _ps_value in _vnf_choice_pre_sets[target]:
                        try:
                            setattr(renpy.store, _ps_field, _ps_value)
                            _vnf_log("Pre-set: {0}={1}".format(
                                _vnf_text(_ps_field), _vnf_text(_ps_value)))
                        except Exception:
                            pass
                if value_map and target in value_map:
                    raw_value = value_map[target]
                    renpy.exports.end_interaction(raw_value)
                elif value_map and target not in value_map and vnf_player.menu_indexerror_guard:
                    # Guard: index out of range — fail loudly and leave
                    # the menu open.  Resolving the nearest valid index
                    # (the old behavior) silently committed a choice
                    # the agent never made, and the story can't be
                    # un-advanced.  The request stays active, so the
                    # agent sees the error + anomaly and can act again;
                    # if the menu changed underneath, the scraper's
                    # orphan resync re-issues a fresh choice_request.
                    _valid_keys = sorted(value_map.keys())
                    _vnf_log("INDEX GUARD: target {} not in value_map {}".format(target, _valid_keys))
                    _vnf_fire_anomaly(dict(
                        kind="menu_index_out_of_range",
                        requested_index=target,
                        valid_indices=_valid_keys,
                        mode="hybrid"))
                    _vnf_client.push_event(dict(
                        type="command_result", command="act",
                        success=False,
                        error="Choice index {} is out of range "
                              "(valid: {}). Menu left open — check "
                              "state() and act again.".format(
                                  target, _valid_keys)))
        except _EndInteraction:
            raise
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception:
            pass

    _vnf_old_max_default = None
    _vnf_active_choices = None

    # Monkey-patch before_interact to suppress default focus while a
    # choice request is active.  This runs AFTER before_interact applies
    # default focus, then undoes it — unless we've already applied our
    # own highlight via change_focus.
    _vnf_original_before_interact = _vnf_save_original("before_interact", renpy.display.focus, "before_interact")

    def _vnf_patched_before_interact(roots):
        if vnf_player.enabled and vnf_player.suppress_default_focus:
            _iface = renpy.display.interface

            # On a fresh interaction, clear lingering focus so the
            # focus-persistence logic (match by full_focus_name) does
            # not carry the previously-clicked button's highlight into
            # the new screen.  This runs before the screen is drawn, so
            # mouse-hover focus will re-apply naturally on the next
            # event-loop tick based on actual cursor position.
            if getattr(_iface, "start_interact", False):
                try:
                    renpy.display.focus.set_focused(None, None, None)
                except Exception:
                    pass

            # Prevent default-focus by making should_max_default False.
            #
            # Ren'Py 8.5+: input_event_time > mouse_event_time + 0.1
            # Older / Ren'Py 7: last_event is None or type not in MOUSE_TYPES
            #
            # Detect version by checking for input_event_time.
            _has_iet = hasattr(_iface, "input_event_time")
            if _has_iet:
                _saved_met = _iface.mouse_event_time
                _iface.mouse_event_time = _iface.input_event_time
            else:
                _saved_le = getattr(_iface, "last_event", None)
                try:
                    import pygame_sdl2 as pygame
                    class _FakeMouse(object):
                        type = pygame.MOUSEMOTION
                    _iface.last_event = _FakeMouse()
                except Exception:
                    pass
            try:
                return _vnf_original_before_interact(roots)
            finally:
                if _has_iet:
                    _iface.mouse_event_time = _saved_met
                else:
                    _iface.last_event = _saved_le
        return _vnf_original_before_interact(roots)

    if not _is_renpy6:
        renpy.display.focus.before_interact = _vnf_patched_before_interact

    # Pending command found by the background poll thread, awaiting
    # execution in the screen event path.  A one-element list mutated in
    # place, NOT a store variable that gets rebound: Ren'Py cleans the
    # store between init and _start and on every full restart, reverting
    # every store variable changed since init.  A command the thread had
    # fetched during the game-load gap silently vanished that way (the
    # launch-time default profile on Roadwarden, Sep 7 2026).
    _vnf_pending_command_box = [None]
    # Transactional command-result cache. The bridge leases an act until its
    # nonce-matched result arrives, so a lost GET/POST or hub restart may
    # deliver the same nonce again. Replaying the result is safe; executing the
    # Ren'Py action twice is not.
    _vnf_command_result_cache = {}
    _vnf_command_result_order = []

    def _vnf_remember_command_result(nonce, event):
        if nonce is None or not _vnf_is_mapping(event):
            return
        if event.get("type") != "command_result":
            return
        try:
            cached = dict(event)
            _vnf_command_result_cache[nonce] = cached
            if nonce in _vnf_command_result_order:
                _vnf_command_result_order.remove(nonce)
            _vnf_command_result_order.append(nonce)
            while len(_vnf_command_result_order) > 64:
                old_nonce = _vnf_command_result_order.pop(0)
                _vnf_command_result_cache.pop(old_nonce, None)
        except Exception:
            pass
    # Flag to signal the background command poll thread to stop.
    _vnf_command_poll_stop = False
    # Worker generation token + thread handle. Rotating the token
    # retires any previous worker (bridge reset, Shift+R reload), so
    # exactly one poller consumes bridge commands at a time — two
    # concurrent pollers can each pop a different queued command and
    # silently overwrite one with the other.
    _vnf_command_poll_gen = [None]
    _vnf_command_poll_thread = None

    def _vnf_command_poll_worker():
        """
        Background thread that polls the bridge for commands.
        Uses raw socket instead of urllib2 to avoid GIL contention
        that blocks urllib2.urlopen on Ren'Py 6.x.
        """
        import socket as _cmd_socket

        _my_token = _vnf_command_poll_gen[0]
        while (not _vnf_command_poll_stop
                and _vnf_command_poll_gen[0] is _my_token):
            if (vnf_player.enabled
                    and _vnf_pending_command_box[0] is None
                    and _vnf_client
                    and _vnf_client.slot_id
                    and not _vnf_client.has_pending_critical_events()):
                try:
                    # Raw HTTP GET via socket (avoids urllib2 GIL issues).
                    _path = "/{0}/command".format(_vnf_client.slot_id)
                    _host = _vnf_client._cfg.bridge_url.replace("http://", "")
                    _parts = _host.split(":")
                    _addr = _parts[0]
                    _port = int(_parts[1]) if len(_parts) > 1 else 80
                    s = _cmd_socket.socket(_cmd_socket.AF_INET, _cmd_socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect((_addr, _port))
                    _tok = getattr(vnf_player, "slot_token", None)
                    _tok_line = ("X-Slot-Token: {0}\r\n".format(_tok)
                                 if _tok else "")
                    _protocol_line = (
                        "X-VNFlight-Shim-Protocol: {0}\r\n".format(
                            _VNFLIGHT_SHIM_PROTOCOL_VERSION))
                    s.sendall(
                        "GET {0} HTTP/1.0\r\nHost: {1}\r\n{2}{3}\r\n".format(
                            _path, _host, _tok_line, _protocol_line
                        ).encode("ascii"))
                    _resp = b""
                    while True:
                        _chunk = s.recv(4096)
                        if not _chunk:
                            break
                        _resp += _chunk
                    s.close()
                    # Parse response body (after blank line).
                    _body = _resp.split(b"\r\n\r\n", 1)[-1]
                    if _body:
                        _data = json.loads(_body.decode("utf-8"))
                        if _data and _data.get("command") is not None:
                            _vnf_pending_command_box[0] = _data["command"]
                            try:
                                _vnf_log("Fetched command: {0}".format(
                                    _data["command"].get("name")))
                            except Exception:
                                pass
                            try:
                                renpy.exports.restart_interaction()
                            except Exception:
                                pass
                except Exception:
                    pass
            time.sleep(vnf_player.command_poll_interval)

    _vnf_command_signal_time = 0.0
    # Module-level (NOT store.) so it isn't pickled into the host game's
    # save files or reverted by rollback.
    _vnf_command_signaled = [False]

    def _vnf_periodic_check_pending_command():
        """
        Periodic callback (~20 times/sec) that checks whether the
        background thread found a command.  If so, it calls
        restart_interaction() to trigger the screen timer that
        executes the command.  Retries every 0.5s if the command
        is still stuck (e.g. during overlay screens).
        """
        if not vnf_player.enabled:
            return
        global _vnf_command_signal_time
        if (_vnf_pending_command_box[0] is not None
                and _vnf_client is not None
                and not _vnf_client.has_pending_critical_events()):
            _now = _time.time()
            if not _vnf_command_signaled[0] or _now - _vnf_command_signal_time > 0.5:
                _vnf_command_signaled[0] = True
                _vnf_command_signal_time = _now
                try:
                    renpy.exports.restart_interaction()
                except Exception:
                    pass



    def _vnf_clear_active_request():
        """Clear the active polling request."""
        _vnf_unblock_mouse()
        global _vnf_old_max_default, _vnf_active_choices, _vnf_active_call_screen_name

        _vnf_client.cancel_request_retry(_vnf_request.request_id)

        if _vnf_old_max_default is not None:
            try:
                renpy.display.focus.old_max_default = _vnf_old_max_default
            except Exception:
                pass
            _vnf_old_max_default = None
            # Re-apply default focus now that the choice is done.
            try:
                renpy.exports.restart_interaction()
            except Exception:
                pass

        _vnf_request.reset()
        _vnf_old_max_default = None
        _vnf_active_choices = None
        _vnf_active_call_screen_name = None

        # Clear pre_resolve state.
        _vnf_preresolve.reset()

    def _vnf_set_active_choice_request(req_id, value_map, choices=None, external_mode=False):
        """Activate polling for a choice request."""
        global _vnf_old_max_default, _vnf_active_choices

        if _vnf_request.request_id != req_id:
            _vnf_client.cancel_request_retry(_vnf_request.request_id)

        if _vnf_old_max_default is None:
            _vnf_old_max_default = getattr(renpy.display.focus, "old_max_default", 0)
            # The monkey-patched before_interact handles suppression;
            # just record that we need to restore later.

        _vnf_request.external_mode = external_mode
        _vnf_request.request_id = req_id
        _vnf_request.value_map = value_map
        _vnf_active_choices = choices
        _vnf_request.is_input = False
        _vnf_request.received_action = None
        _vnf_request.obs_start = 0.0
        _vnf_request.obs_target = None
        _vnf_request.focus_applied = False
        _vnf_request.scroll_correction = False
        _vnf_request.choice_scrolled = False
        _vnf_request.choice_len = 0

        # In external mode, block mouse clicks so the user can't
        # race the bridge to resolve the choice.
        if external_mode:
            _vnf_block_mouse()

        # Start background polling thread.
        # On 6.x, skip — the background thread's urllib2 times out
        # due to GIL contention, consuming the bridge action without
        # delivering it.  The 6.x inline poll handles it instead.
        if not _is_renpy6:
            t = _threading.Thread(target=_vnf_poll_worker, args=(req_id,))
            t.daemon = True
            t.start()

    def _vnf_set_active_input_request(req_id):
        """Activate polling for an input request."""
        if _vnf_request.request_id != req_id:
            _vnf_client.cancel_request_retry(_vnf_request.request_id)
        _vnf_request.external_mode = not vnf_player.allow_user_override
        _vnf_request.request_id = req_id
        _vnf_request.value_map = None
        _vnf_request.is_input = True
        _vnf_request.received_action = None
        _vnf_request.obs_start = 0.0
        _vnf_request.obs_target = None
        _vnf_request.focus_applied = False
        _vnf_request.scroll_correction = False
        _vnf_request.choice_scrolled = False
        _vnf_request.choice_len = 0

        # Start background polling thread (skip on 6.x — see above).
        if not _is_renpy6:
            t = _threading.Thread(target=_vnf_poll_worker, args=(req_id,))
            t.daemon = True
            t.start()

    # =========================================================================
    # Resync Command
    # =========================================================================

    def _vnf_cmd_resync(cmd_name, cmd_args):
        """Re-push the current menu's choice_request to the bridge.

        Useful when the bridge's pending_request is stale or missing
        but the game still has an active choice screen.
        Only re-pushes if the bridge has no pending request (or a
        mismatched one).
        """
        _nonce = (cmd_args or {}).get("nonce") if cmd_args else None
        # Settle the one-shot rendered-choice filter first. It can replace the
        # request and schedule its own delayed activation; letting it run
        # after resync would replay the canonical setter during observation.
        if _vnf_pending_vis_check[0] is not None:
            _vnf_periodic_vis_check()
        ctx = _vnf_current_menu_context[0]
        if ctx is None:
            _event = dict(
                type="command_result", command="resync",
                success=False, error="No active menu")
            if _nonce is not None:
                _event["nonce"] = _nonce
            _vnf_client.push_event(_event)
            return
        # Resync makes this menu actionable immediately. Retire the original
        # pacing activation before restoring local state: if that delayed
        # callback fires after an act has begun observing the same menu, the
        # canonical setter clears obs_start/obs_target and silently strands
        # the accepted choice until it is submitted again.
        _vnf_deferred_choice[0] = None
        # Check if the bridge already has a valid pending request.
        pending = _vnf_client._get("/pending")
        if pending:
            bridge_req = pending.get("pending")
            if bridge_req and bridge_req.get("id") == ctx["req_id"]:
                if _vnf_request.request_id == ctx["req_id"] and _vnf_request.value_map:
                    _event = dict(
                        type="command_result", command="resync",
                        success=True, message="Already in sync")
                    if _nonce is not None:
                        _event["nonce"] = _nonce
                    _vnf_client.push_event(_event)
                    return
                _vnf_set_active_choice_request(
                    ctx["req_id"], ctx["value_map"], choices=ctx["choices"],
                    external_mode=not vnf_player.allow_user_override)
                _event = dict(
                    type="command_result", command="resync",
                    success=True, message="Restored local choice state")
                if _nonce is not None:
                    _event["nonce"] = _nonce
                _vnf_client.push_event(_event)
                return
        _resync_kwargs = dict(ctx["req_kwargs"])
        # A fresh request id normally denotes a different interaction, even
        # when the visible labels repeat. Mark this exceptional reissue so the
        # bridge can retain the original logical identity without weakening
        # stale-target cancellation for ordinary consecutive menus.
        _resync_kwargs["reissued_from_request_id"] = ctx["req_id"]
        _resync_kwargs["reissue_root_request_id"] = (
            ctx.get("reissue_root_request_id") or ctx["req_id"])
        _vnf_client.cancel_request_retry(ctx["req_id"])
        new_rid = _vnf_client.push_request(
            "choice_request", **_resync_kwargs)
        _vnf_set_active_choice_request(
            new_rid, ctx["value_map"], choices=ctx["choices"],
            external_mode=not vnf_player.allow_user_override)
        _vnf_log("Resync: re-pushed choice_request as %s (was %s)" % (new_rid, ctx["req_id"]))
        ctx["reissue_root_request_id"] = _resync_kwargs[
            "reissue_root_request_id"]
        ctx["req_id"] = new_rid
        _event = dict(
            type="command_result", command="resync",
            success=True, new_request_id=new_rid)
        if _nonce is not None:
            _event["nonce"] = _nonce
        _vnf_client.push_event(_event)

    _vnf_add_command_handler("resync", _vnf_cmd_resync)

    def _vnf_cmd_progress(cmd_name, cmd_args):
        """Bridge command: progress — query game progress."""
        return _vnf_get_progress()

    _vnf_add_command_handler("progress", _vnf_cmd_progress)

    # =========================================================================
    # Monkey-Patching Helpers
    # =========================================================================

    # We store originals so we can call through.
    _vnf_original_display_menu = None
    _vnf_original_input = None
    _vnf_original_show = None
    _vnf_original_hide = None
    _vnf_original_scene = None

    # Track what the mod has captured (used for dedup / context).
    _vnf_last_who = None
    _vnf_last_what = None

    # NVL lines can surface through both Ren'Py's character callback and the
    # nvl_list fallback.  Track callback-owned occurrences so an immediate
    # ``nvl hide`` / ``nvl clear`` can flush genuinely missed rows without
    # publishing callback-captured rows a second time.
    _vnf_nvl_callback_occurrences = []
    # A forced lifecycle-boundary drain can run before the character callback.
    # Keep the inverse ownership briefly so that callback does not republish a
    # row which the fallback path already delivered.
    _vnf_nvl_prepublished_callbacks = []

    # Context tracking
    _vnf_last_context = None
    _vnf_last_context_time = 0.0
    _vnf_gameplay_seen = False

    # Visible-screen scrape dedup — stores hash of last pushed content.
    _vnf_last_visible_scrape_hash = None
    _vnf_last_scraped_screens = set()  # previous set of screen tags
    # Previous scrape's (screen tag, text) pairs, for delta computation.
    # The tag rides along so a repeat can be judged PER SOURCE SCREEN: the
    # ordinary delta filter still compares text globally, but the modal
    # transcript needs to know whether THIS screen showed the line before.
    _vnf_last_scrape_text_pairs = []

    # Synthetic input request — tracks a request created for a screen-based
    # Input widget (as opposed to one created by renpy.input()).
    _vnf_synthetic_input_req = None  # request ID, or None

    # =========================================================================
    # Instantiate globals
    # =========================================================================

    vnf_player = VNFPlayerConfig()

    # Always-on ring of the last shim log lines (cheap; no I/O). Dumped into
    # vnflight_anomalies.log when an exception/anomaly fires, so the golden
    # path stays silent but the evidence exists when something went wrong.
    import collections as _collections
    _vnf_log_ring = _collections.deque(maxlen=200)
    _vnf_debug_log_path = [None]

    def _vnf_debug_log_file():
        """Per-session debug log path, created lazily; None when disabled."""
        if _vnf_debug_log_path[0] is not None:
            return _vnf_debug_log_path[0] or None
        try:
            _dir = getattr(vnf_player, "debug_logs", None) or renpy.config.basedir
            if not os.path.isdir(_dir):
                os.makedirs(_dir)
            _stamp = time.strftime("%Y%m%d_%H%M%S")
            _launch = getattr(vnf_player, "_launch_id", None) or ""
            _name = "vnflight_debug_%s%s.log" % (
                _stamp, ("_" + _launch[:8]) if _launch else "")
            _vnf_debug_log_path[0] = os.path.join(_dir, _name)
        except Exception:
            _vnf_debug_log_path[0] = ""
        return _vnf_debug_log_path[0] or None

    def _vnf_log(msg):
        try:
            _line = "[%s] %s" % (time.strftime("%H:%M:%S"), _vnf_text(msg))
        except Exception:
            _line = "<unprintable>"
        _vnf_log_ring.append(_line)
        if getattr(vnf_player, "debug", False):
            try:
                print("[LLM Player] " + _vnf_text(msg))
            except Exception:
                pass
            try:
                _path = _vnf_debug_log_file()
                if _path:
                    # Explicit UTF-8 text writer: plain open() on Python 2 or
                    # a non-UTF-8 Windows runtime raises on the first smart
                    # quote and the line is silently lost.
                    import io as _io
                    with _io.open(_path, "a", encoding="utf-8") as _fh:
                        _fh.write(_vnf_text(_line) + u"\n")
            except Exception:
                pass

    def _vnf_dump_log_ring(reason):
        """Write the recent shim log lines next to the anomaly record."""
        try:
            import io as _io
            with _io.open(os.path.join(renpy.config.basedir, "vnflight_anomalies.log"), "a", encoding="utf-8") as _af:
                _af.write(u"--- last %d shim log lines (%s) ---\n" % (len(_vnf_log_ring), _vnf_text(reason)))
                for _l in list(_vnf_log_ring):
                    _af.write(_vnf_text(_l) + u"\n")
                _af.write(u"--- end ---\n")
                _af.flush()
        except Exception:
            pass

    # Launch-file handshake: report deferred notes now that the logger
    # exists (the file is read during VNFPlayerConfig.__init__ above).
    if _vnf_launch_file_note:
        _vnf_log(_vnf_launch_file_note)
    elif getattr(vnf_player, "_launch_file", None) is not None:
        _vnf_log("Launch file applied: bridge_url=%s slot_token=%s save_slot=%s"
                 % (vnf_player.bridge_url,
                    "set" if vnf_player.slot_token else "none",
                    vnf_player.save_slot or "none"))

    # --- Save slot redirect ---
    # Redirect Ren'Py's save directory when a save_slot is configured.
    # This isolates agent saves from the player's real saves.
    _vnf_original_savedir = None

    def _vnf_apply_save_slot(slot_name):
        """Redirect config.savedir to a slot-specific subdirectory."""
        global _vnf_original_savedir
        if not slot_name:
            # Restore original if previously redirected.
            if _vnf_original_savedir is not None:
                renpy.config.savedir = _vnf_original_savedir
                _vnf_original_savedir = None
            return
        if _vnf_original_savedir is None:
            _vnf_original_savedir = renpy.config.savedir
        # Sanitize slot name (alphanumeric, underscore, hyphen only).
        import re as _re
        safe_name = _re.sub(r'[^\w\-]', '_', _vnf_text(slot_name))
        new_dir = os.path.join(_vnf_original_savedir, "vnflight", safe_name)
        # No exist_ok: Py2 (Ren'Py 6/7) doesn't have it.
        if not os.path.isdir(new_dir):
            os.makedirs(new_dir)
        renpy.config.savedir = new_dir
        _vnf_log("Save slot: '%s' -> %s" % (slot_name, new_dir))

    if vnf_player.save_slot:
        _vnf_apply_save_slot(vnf_player.save_slot)

    def _vnf_cmd_set_save_slot(cmd_name, cmd_args):
        """Bridge command: set_save_slot — change save directory at runtime."""
        slot_name = ((cmd_args or {}).get("slot", "")
                     if _vnf_is_mapping(cmd_args) else _vnf_text(cmd_args or ""))
        _vnf_apply_save_slot(slot_name)
        vnf_player.save_slot = slot_name
        return {"success": True, "save_slot": slot_name,
                "savedir": renpy.config.savedir}

    _vnf_add_command_handler("set_save_slot", _vnf_cmd_set_save_slot)

    def _vnf_cmd_dump_tree(cmd_name, cmd_args):
        """Bridge command: dump_tree — print widget tree structure for a screen.

        Usage: command("dump_tree <screen_tag>")
        Prints indented tree with class names, depths, and text/button info
        to help pick the right section_depth value.
        """
        try:
            tag = cmd_args.get("screen", "")
        except AttributeError:
            tag = _vnf_text(cmd_args or "")
        if not tag:
            return {"success": False, "error": "Usage: dump_tree {screen: tag}"}
        scr = renpy.get_screen(tag)
        if scr is None:
            return {"success": False, "error": "Screen '{}' not showing".format(tag)}
        lines = []
        def _walk(d, depth=0):
            if d is None or depth > 20:
                return
            cn = d.__class__.__name__
            indent = "  " * depth
            extra = ""
            if "Text" in cn:
                try:
                    txt = "".join(_vnf_text(t) for t in getattr(d, "text", []))
                    txt = _vnf_substitute(
                        renpy.text.extras.filter_text_tags(
                            txt, allow=set())).strip()
                    if txt:
                        extra = '  text={!r}'.format(txt[:60])
                except Exception:
                    pass
            elif "Button" in cn or "Hotspot" in cn:
                _act = getattr(d, "action", None)
                if _act:
                    _acts = _act if _vnf_is_sequence(_act) else [_act]
                    extra = "  actions=[{}]".format(
                        ", ".join(a.__class__.__name__ for a in _acts))
            n_children = 0
            if hasattr(d, "children"):
                n_children = len(d.children)
            elif hasattr(d, "child") and d.child is not None:
                n_children = 1
            lines.append("{}{}: {}  children={}{}".format(
                indent, depth, cn, n_children, extra))
            if "Button" not in cn and "Hotspot" not in cn:
                if hasattr(d, "children"):
                    for c in d.children:
                        _walk(c, depth + 1)
                elif hasattr(d, "child"):
                    _walk(d.child, depth + 1)
        _walk(scr)
        tree_text = "\n".join(lines)
        _vnf_log("dump_tree {}:\n{}".format(tag, tree_text))
        return {"success": True, "screen": tag, "tree": tree_text}

    _vnf_add_command_handler("dump_tree", _vnf_cmd_dump_tree)

    # Debug-only: raise a deliberate exception inside the game's own
    # interaction so the real exception screen appears, for testing the
    # anomaly path end to end (shim -> bridge latch -> agent render ->
    # Ignore/Rollback -> latch resolves on the next story line).  Queued as
    # a native action: the screen timer runs it through renpy.run() with
    # only control-flow exceptions caught, exactly like a crashing button.
    # Registered only when the game directory carries a marker file, so no
    # shipped game ever exposes it.
    _VNF_DEBUG_MARKER = "vnflight_debug.enabled"

    def _vnf_cmd_debug_crash(cmd_name, cmd_args):
        global _vnf_native_action_queue
        _message = "vnflight debug_crash: deliberate exception for anomaly-path testing"
        try:
            _message = (cmd_args or {}).get("message", _message) or _message
        except Exception:
            pass
        def _vnf_debug_raise():
            raise RuntimeError(_message)
        _vnf_native_action_queue = _vnf_debug_raise
        try:
            renpy.restart_interaction()
        except Exception:
            pass
        return {"success": True, "message": "deliberate exception queued: " + _message}

    try:
        _vnf_debug_enabled = os.path.exists(
            os.path.join(renpy.config.gamedir, _VNF_DEBUG_MARKER))
    except Exception:
        _vnf_debug_enabled = False
    if _vnf_debug_enabled:
        _vnf_add_command_handler("debug_crash", _vnf_cmd_debug_crash)
        _vnf_log("debug commands enabled ({} present)".format(_VNF_DEBUG_MARKER))

    # Snapshot init-time defaults before any profile overrides.
    _vnf_config_defaults = {
        k: v for k, v in vnf_player.__dict__.items()
        if not k.startswith("_") and not callable(v)
    }
    _vnf_client = VNFBridgeClient(vnf_player)


## ============================================================================
## Hook Installation -- runs after all other init blocks
## ============================================================================

init 999 python:

    import os as _os
    import time as _time
    import threading as _threading
    import hashlib as _hashlib

    # Python 2 (Ren'Py 7) doesn't have time.monotonic.
    try:
        _time_monotonic = _time.monotonic
    except AttributeError:
        _time_monotonic = _time.time

    # NOTE: _vnf_log is defined once, in the init -990 block (guarded print +
    # always-on ring + per-session debug file). A second definition here
    # used to shadow it at runtime and silently drop the ring and the file.

    def _vnf_coerce_config_value(old_val, new_val):
        """Coerce *new_val* to match the type of *old_val*."""
        if isinstance(old_val, bool):
            if isinstance(new_val, bool):
                return new_val
            return _vnf_text(new_val).lower() in ("true", "1", "yes")
        elif isinstance(old_val, int):
            return int(new_val)
        elif isinstance(old_val, float):
            return float(new_val)
        return new_val

    def _vnf_apply_text_cps_preference():
        """Apply vnf_player.text_cps to Ren'Py's visible text speed."""
        try:
            renpy.game.preferences.text_cps = int(vnf_player.text_cps)
        except Exception:
            pass

    def _vnf_effective_dialogue_advance_mode():
        """Resolve auto/afm/text/external dialogue advancement mode."""
        mode = _vnf_text(getattr(vnf_player, "dialogue_advance_mode", "auto") or "auto").lower()
        if mode == "auto":
            return "afm" if vnf_player.allow_user_override else "external"
        if mode in ("afm", "audio", "voice"):
            return "afm"
        if mode == "text":
            return "text"
        if mode in ("external", "instant", "turbo"):
            return "external"
        return "afm" if vnf_player.allow_user_override else "external"

    def _vnf_refresh_dialogue_advance_preferences():
        """Keep Ren'Py preferences aligned with the active dialogue mode."""
        if not vnf_player.enabled:
            return
        _vnf_apply_text_cps_preference()
        mode = _vnf_effective_dialogue_advance_mode()
        _manage_afm = bool(getattr(vnf_player, "auto_advance", False))
        try:
            if _manage_afm and mode == "afm":
                renpy.game.preferences.afm_enable = True
                renpy.game.preferences.afm_time = _vnf_compute_afm_time()
                _vnf_afm_intentionally_off[0] = False
            elif _manage_afm:
                renpy.game.preferences.afm_enable = False
                if mode == "external":
                    renpy.config.auto_forward_time = None
        except Exception:
            pass

    def _vnf_set_config_key(key, value):
        """Set one vnf_player config key and run preference side effects."""
        if not hasattr(vnf_player, key):
            raise KeyError(key)
        old = getattr(vnf_player, key)
        coerced = _vnf_coerce_config_value(old, value)
        setattr(vnf_player, key, coerced)
        if key == "fast_forward":
            if coerced and not old:
                _vnf_enable_fast_forward()
            elif old and not coerced:
                _vnf_disable_fast_forward()
                _vnf_refresh_dialogue_advance_preferences()
        elif key == "turbo":
            # Apply/restore only on a real transition: re-applying while
            # already on would memorize turbo's own values as the restore
            # target (guarded again inside _vnf_apply_turbo).
            try:
                if coerced and not old:
                    _vnf_apply_turbo()
                elif old and not coerced:
                    _vnf_restore_turbo()
            except Exception as _turbo_exc:
                _vnf_log("Turbo: set failed: {}".format(
                    _vnf_text(_turbo_exc)))
        elif key == "text_cps":
            _vnf_apply_text_cps_preference()
        elif key == "auto_advance":
            # Reconcile even an unchanged setting: load/start may have
            # restored the store's runtime flag independently of the config.
            if coerced:
                _vnf_enable_auto_advance()
            else:
                _vnf_disable_auto_advance()
        elif key in (
            "dialogue_advance_mode",
            "reading_cps",
            "allow_user_override",
        ):
            _vnf_refresh_dialogue_advance_preferences()
        elif key == "move_host_pointer" and coerced:
            _vnf_mouse.sync()
        return old, coerced

    # -------------------------------------------------------------------------
    # 1. Character Callback -- captures dialogue / narration
    # -------------------------------------------------------------------------

    # Track last pushed dialogue for dedup (speaker, text, timestamp).
    _vnf_last_dialogue_push = [None, None, 0.0]

    # Ren'Py narrates a bare menu caption immediately before display_menu(),
    # while the choice request is published separately. Keep that prompt in a
    # one-shot handoff so both cross the bridge as one mutation.
    if (not hasattr(_sys_mod, "_vnf_pending_menu_caption")
            or type(_sys_mod._vnf_pending_menu_caption)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_pending_menu_caption = _VNF_NATIVE_LIST_TYPE((None,))
    else:
        _sys_mod._vnf_pending_menu_caption[0] = None
    _vnf_pending_menu_caption = _sys_mod._vnf_pending_menu_caption

    def _vnf_stage_menu_caption(event):
        _vnf_pending_menu_caption[0] = dict(event)

    def _vnf_attach_pending_menu_caption(choices):
        """Attach the staged prompt without releasing it before publication."""
        event = _vnf_pending_menu_caption[0]
        if not event:
            return
        label = event.get("text") or ""
        if not label:
            return
        # Ren'Py 6/7 carry narration as value=None rows because their
        # callbacks do not provide ``what``. Newer engines may carry both
        # forms. Any inline caption set is authoritative, including menus
        # assembled from several caption rows.
        for choice in choices:
            if choice.get("caption"):
                return
        choices.insert(0, {
            "index": None,
            "label": label,
            "caption": True,
            "disabled": False,
        })

    def _vnf_commit_pending_menu_caption():
        """Release prompt ownership after its request has been queued."""
        _vnf_pending_menu_caption[0] = None

    def _vnf_restore_raw_menu_captions(choices, raw_items, clean_label):
        """Restore narrator-menu rows omitted from display_menu items."""
        if not getattr(renpy.config, "narrator_menu", False):
            return choices
        captions = []
        for item in (raw_items or []):
            if not item.get("is_caption"):
                continue
            label = clean_label(item.get("label", ""))
            if label:
                captions.append(label)
        if captions and not any(choice.get("caption") for choice in choices):
            # Menu.execute speaks every nonempty caption without evaluating
            # its condition, joined into one narrator payload. Preserve that
            # exact unit so screen-text dedup sees the string Ren'Py drew.
            choices.insert(0, {
                "index": None,
                "label": "\n".join(captions),
                "caption": True,
                "disabled": False,
            })
        return choices

    def _vnf_flush_pending_menu_caption():
        """Publish a staged prompt when no choice request will own it."""
        event = _vnf_pending_menu_caption[0]
        _vnf_pending_menu_caption[0] = None
        if event:
            _vnf_client.push_event(event)

    def _vnf_executing_menu_statement():
        """True when the statement Ren'Py is executing is a `menu:` block.

        Ren'Py renders a menu's bare-string caption lines through the
        NARRATOR, not through the character who spoke last:

            renpy/ast.py  Menu.execute()
                if renpy.config.narrator_menu and label:
                    narration.append(label)
                ...
                renpy.exports.say(None, "\\n".join(narration),
                                  interact=False)

        That say never runs through ast.Say.execute(), so it never
        updates store._last_say_who -- the stale value still names the
        previous speaker.  Detecting the menu statement lets the
        callback drop that stale speaker instead of attributing the
        caption to them.  Works for `menu:` and `nvl menu:` alike (both
        are ast.Menu nodes).
        """
        try:
            _ctx = renpy.game.context()
            _name = getattr(_ctx, "current", None)
            if _name is not None:
                _node = renpy.game.script.lookup(_name)
                if _node is not None:
                    return isinstance(_node, renpy.ast.Menu)
        except Exception:
            pass
        # Fallback for builds where the node lookup is unavailable:
        # ast.statement_name() records "menu" / "menu-with-caption" /
        # "menu-nvl-with-caption" before the narration say fires.
        try:
            _stmt = getattr(renpy.ast, "current_statement_name", None)
            if isinstance(_stmt, basestring):
                return _stmt.startswith("menu")
        except Exception:
            pass
        return False

    def _vnf_clean_event_text(value):
        """Resolve substitutions and tags before path-specific trimming."""
        _raw = _vnf_stringify(value)
        if _raw is None:
            return ""
        return _vnf_substitute(
            renpy.text.extras.filter_text_tags(
                _raw, allow=set()))

    def _vnf_character_display_name_state(who):
        """Return ``(display_name, resolved)`` for a speaker value."""
        if who is None:
            return None, True
        try:
            char = (renpy.python.py_eval(who)
                    if isinstance(who, basestring) else who)
            if isinstance(char, basestring):
                return _vnf_stringify(
                    _vnf_resolve_display_name_expr(char)), True
            name = getattr(char, "name", None)
            if callable(name):
                name = name()
            if name is None:
                return None, False
            raw = _vnf_stringify(name)
            if raw is None:
                return None, False
            resolved = getattr(renpy.store, raw, None)
            if resolved and isinstance(resolved, basestring):
                name = resolved
            else:
                name = renpy.substitutions.substitute(raw)[0]
            return _vnf_stringify(
                _vnf_resolve_display_name_expr(name)), True
        except Exception:
            fallback = _vnf_stringify(who)
            if fallback is None:
                return None, False
            return _vnf_stringify(
                _vnf_resolve_display_name_expr(fallback)), False

    def _vnf_character_display_name(who):
        """Resolve a Character, DynamicCharacter, or store symbol."""
        return _vnf_character_display_name_state(who)[0]

    def _vnf_character_callback(event, interact, **kwargs):
        """
        Called by Ren'Py for every character say statement.
        We capture on "begin" to push dialogue to the bridge.
        """
        if not vnf_player.enabled:
            return

        if event != "begin":
            return

        # ADV says do not pass through NVLCharacter.do_add. Observe the
        # rollback edge here as the universal story-entry fallback; for NVL
        # the earlier do_add observation makes this idempotent.
        try:
            _vnf_observe_rollback_resume()
        except Exception:
            pass

        # Retrieve who / what from the store (covers both 6.x and 7.x+).
        who, _what_fallback = _get_last_say()
        what = kwargs.get("what") or _what_fallback

        if what is None:
            return

        # Menu-caption attribution guard.
        #
        # `store._last_say_who` is written only by ast.Say.execute().  A
        # menu caption is spoken by the NARRATOR from ast.Menu.execute()
        # (see _vnf_executing_menu_statement), which leaves the previous
        # speaker in place -- so without this the caption renders as
        # "[Dr. Chen] Should I tell him?" when Dr. Chen merely spoke
        # last.  A caption is narration / an interaction prompt: it gets
        # no speaker.
        #
        # THE DISCRIMINATOR is menu context + interact=False + "this
        # callback carried its OWN text" -- deliberately NOT text
        # equality with the previous say.  An earlier revision escaped on
        # equality, which mis-attributed a genuine caption that merely
        # repeated the previous line's words.  Engine evidence, identical
        # on Ren'Py 8.5.2 and 7.5.2:
        #
        #  * ast.Menu.execute() says only with interact=False, so a say
        #    arriving with interact=True while a Menu node is current did
        #    not come from the menu (e.g. screen-driven dialogue) and
        #    keeps its speaker.
        #  * A say-CAPTIONED menu (a `chen "..."` line inside `menu:`) is
        #    parsed into a SEPARATE ast.Say node placed BEFORE the Menu
        #    node -- parser.parse_menu() -> finish_say(..., interact=False)
        #    -- so that character line executes under a Say node and never
        #    reaches this branch.  Nothing re-says it under the Menu node
        #    either: Menu.execute()'s `narration` list holds only caption
        #    items, and config.choice_empty_window is skipped once a
        #    window is shown.  Exactly one correctly attributed line, no
        #    duplicate, no loss.
        #  * The only other say under a Menu node is
        #    config.choice_empty_window("", interact=False), whose `what`
        #    is the EMPTY string -- so `what` above falls back to
        #    _last_say_what, the PREVIOUS line's text.
        #  * Ren'Py 7.x / 6.x never pass `what` to character callbacks at
        #    all (7.5.2 character.py: c("begin", interact=interact,
        #    type=type, **cb_args)), so on those engines every say under a
        #    menu arrives text-less and falls back the same way.
        #
        # In both text-less cases the only text in hand belongs to the
        # PREVIOUS say, which its own Say node already published with the
        # right speaker.  Keeping that speaker is what lets the dedup
        # below recognise the duplicate and drop it; narrating it instead
        # would change the dedup key and emit a phantom narration line.
        # Hence: drop the speaker only when this callback brought its own
        # caption text.
        _menu_caption = False
        if (not interact) and _vnf_executing_menu_statement():
            try:
                _own_what = kwargs.get("what")
                _has_own_what = (
                    _own_what is not None and _vnf_text(_own_what) != "")
            except Exception:
                _has_own_what = False
            if _has_own_what:
                _menu_caption = True
                who = None

        # Resolve who to a display name.
        who_name = _vnf_character_display_name(who)

        # Resolve [variable] references and strip Ren'Py text tags.
        clean_what = _vnf_clean_event_text(what)

        # Detect NVL vs ADV mode for the event.
        is_nvl = False
        try:
            # Ren'Py passes the Character's display ``type`` to every callback.
            # Character(kind=nvl) also copies type="nvl" into display_args; it
            # does not retain a ``kind`` attribute.  Do not infer ownership from
            # mode alone: an ADV Character may use mode="nvl" without producing
            # an nvl_list row for the fallback watcher to claim.
            char_type = kwargs.get("type")
            type_known = char_type is not None
            if _vnf_text(char_type).lower() == "nvl":
                is_nvl = True
            if not type_known and who is not None:
                char = renpy.python.py_eval(who) if isinstance(who, basestring) else who
                display_args = getattr(char, "display_args", None)
                if display_args is not None:
                    try:
                        char_type = display_args.get("type")
                    except Exception:
                        char_type = None
                    type_known = char_type is not None
                if _vnf_text(char_type).lower() == "nvl":
                    is_nvl = True
                char_kind = getattr(char, "kind", None)
                if not type_known and not is_nvl and char_kind is not None:
                    kind_name = getattr(char_kind, "name", None) or _vnf_text(char_kind)
                    if "nvl" in _vnf_text(kind_name).lower():
                        is_nvl = True
            # Last-resort compatibility fallback for engines which provide no
            # callback/display type.  Known 6.x, 7.x, and 8.x engines do.
            if not type_known and not is_nvl:
                current_mode = getattr(renpy.store, "_mode", None)
                if current_mode == "nvl":
                    is_nvl = True
        except Exception:
            pass

        if is_nvl:
            clean_what = clean_what.strip()
            if not clean_what:
                return
        # ADV deliberately preserves Ren'Py's exact callback payload,
        # including outer whitespace and whitespace-only says. Such says can
        # be presentation/window-control beats; changing this contract would
        # make upgraded transcripts silently differ from the played script.

        # Track last narration for pacing delay calculations.
        global _vnf_last_what
        _vnf_last_what = clean_what

        if who_name:
            ev = dict(type="dialogue",
                      character=_vnf_stringify(who_name),
                      text=clean_what,
                      mode="nvl" if is_nvl else "adv")
        else:
            ev = dict(type="narration", text=clean_what, mode="nvl" if is_nvl else "adv")
            if _menu_caption:
                # Marks the line as the menu's own prompt text rather
                # than ordinary narration (diagnostics / formatters).
                ev["menu_caption"] = True

        # Detect user-initiated text advance: if no shim action
        # recently caused this say event, the user clicked to advance.
        # Use both a time heuristic and mouse drift detection.
        # Suppress if: shim auto-advance dismissed the previous say,
        # OR Ren'Py's native AFM is on (it auto-forwards without clicks).
        _was_auto_advanced = _vnf_auto_advanced_flag[0]
        _vnf_auto_advanced_flag[0] = False
        _afm_on = False
        try:
            _afm_on = bool(renpy.game.preferences.afm_enable)
        except Exception:
            pass
        _shim_age = _time.time() - _vnf_last_shim_action_time
        if (_shim_age > 1.0 and _vnf_last_shim_action_time > 0
                and _vnf_mouse.is_user_active()
                and not _was_auto_advanced
                and not _afm_on):
            ev["user_initiated"] = True

        # Dedup: when Ren'Py re-displays the last say as menu context,
        # the callback fires again with interact=False and identical text.
        # Suppress the duplicate — the menu will follow immediately.
        _dedup_key = (_vnf_stringify(who_name) if who_name else None,
                      clean_what)
        if (is_nvl
                and _vnf_claim_nvl_prepublished_callback_event(ev, who)):
            _vnf_last_dialogue_push[0] = _dedup_key[0]
            _vnf_last_dialogue_push[1] = _dedup_key[1]
            _vnf_last_dialogue_push[2] = _time.time()
            # Since 6.99.13, do_done persists the row after this callback; let
            # the forward ledger consume that page occurrence too. Older 6.x
            # already persisted it in do_add, so noting here would leave stale
            # ownership capable of claiming a later identical fallback.
            _persists_on_done = True
            try:
                _parsed_version = renpy_version
                _persists_on_done = (
                    not _parsed_version
                    or _parsed_version == (0,)
                    or _parsed_version >= (6, 99, 13))
            except Exception:
                pass
            if _persists_on_done:
                _vnf_note_nvl_callback_event(ev, who)
            _vnf_log("[NVL] suppressed boundary-owned callback: {}: {}".format(
                who_name or "Narrator", clean_what[:60]))
            return
        if vnf_player.dialogue_dedup and not interact:
            if (_vnf_last_dialogue_push[0] == _dedup_key[0]
                    and _vnf_last_dialogue_push[1] == _dedup_key[1]):
                _vnf_log("[dedup] suppressed menu-context re-display: {}: {}".format(
                    who_name or "Narrator", clean_what[:60]))
                # NVLCharacter.do_add runs before this callback and records
                # the re-display in nvl_list even though it is not a new
                # story occurrence. Give that fallback row an owner so its
                # delivery cannot depend on watcher timing.
                if is_nvl:
                    _vnf_note_nvl_callback_event(ev, who)
                return
        # Always track the last push for dedup comparison.
        _vnf_last_dialogue_push[0] = _dedup_key[0]
        _vnf_last_dialogue_push[1] = _dedup_key[1]
        _vnf_last_dialogue_push[2] = _time.time()

        _vnf_log("{}{}: {}".format("[NVL] " if is_nvl else "", who_name or "Narrator", clean_what[:80]))
        if _menu_caption:
            _vnf_stage_menu_caption(ev)
        else:
            _vnf_client.push_event(ev)
        if is_nvl:
            _vnf_note_nvl_callback_event(ev, who)

        # When dialogue auto-advances (interact=False), reset scrape
        # hash so subsequent scrapes see fresh state.  Don't push a
        # separate screen_content — the dialogue event already covers it.
        if not interact:
            global _vnf_last_visible_scrape_hash
            _vnf_last_visible_scrape_hash = None
            _vnf_last_scrape_text_pairs[:] = []

    # Register the callback.
    if _vnf_character_callback not in renpy.config.all_character_callbacks:
        renpy.config.all_character_callbacks.append(_vnf_character_callback)

    # -------------------------------------------------------------------------
    # 1a-2. Exception Handler Hook
    # -------------------------------------------------------------------------
    # Ren'Py's exception screen runs in a separate context, invisible to the
    # scraper.  Hook config.exception_handler to set a flag that the scraper
    # checks on each cycle.

    _vnf_original_exception_handler = _sys_mod._vnf_patch_originals.get("exception_handler", getattr(renpy.config, "exception_handler", None))
    if "exception_handler" not in _sys_mod._vnf_patch_originals:
        _sys_mod._vnf_patch_originals["exception_handler"] = _vnf_original_exception_handler

    def _vnf_exception_handler(*args):
        """Hook that fires when Ren'Py catches an exception.

        Ren'Py 7: called as (short, full, traceback_fn) — 3 args.
        Ren'Py 8.4+: called as (traceback_exception,) — 1 arg.
        """
        _vnf_exception.exception_flag = True  # was _vnf_scrape_visible_screens._exception_flag
        _vnf_mouse.sync()
        # Extract a description for logging.
        if len(args) == 1:
            _desc = _vnf_text(args[0])[:200]
        else:
            _desc = _vnf_text(args[0])[:200] if args else "unknown"
        _vnf_log("Exception handler fired: %s" % _desc)
        # Write to a dedicated file for reliable detection.
        try:
            import datetime as _dt
            with open(_os.path.join(renpy.config.basedir, "vnflight_anomalies.log"), "a") as _af:
                _af.write("[%s] EXCEPTION: %s\n" % (_dt.datetime.now().isoformat(), _desc))
                _af.flush()
        except Exception:
            pass
        # Fire anomaly — use _vnf_fire_anomaly which tries sync push.
        # If HTTP push fails (bridge thread blocked during exception),
        # also write directly to the bridge's event buffer via the
        # internal API (bypassing HTTP).
        _anomaly_ev = {
            "type": "renpy_exception",
            "message": "Ren'Py exception: %s" % _desc,
        }
        try:
            _vnf_fire_anomaly(_anomaly_ev)
        except Exception:
            pass
        # Also write anomaly to the anomaly log with push result.
        try:
            import datetime as _dt2
            with open(_os.path.join(renpy.config.basedir, "vnflight_anomalies.log"), "a") as _af2:
                _af2.write("[%s] PUSH ATTEMPTED\n" % _dt2.datetime.now().isoformat())
                _af2.flush()
        except Exception:
            pass
        # Call original handler if present.
        if _vnf_original_exception_handler:
            return _vnf_original_exception_handler(*args)
        return False  # False = show default exception screen.

    if not _is_renpy6:
        renpy.config.exception_handler = _vnf_exception_handler

    # -------------------------------------------------------------------------
    # 1b. NVL Callback -- captures NVL mode text (Ren'Py 7.5.2+)
    # -------------------------------------------------------------------------

    # In Ren'Py 7.5.2+, NVL mode uses all_nvl_callbacks instead of all_character_callbacks
    if not _is_renpy6 and hasattr(renpy.config, "all_nvl_callbacks"):
        if _vnf_character_callback not in renpy.config.all_nvl_callbacks:
            renpy.config.all_nvl_callbacks.append(_vnf_character_callback)

    # -------------------------------------------------------------------------
    # 1c. NVL List Watcher -- fallback for Ren'Py builds where callbacks
    #     don't fire for NVL characters (e.g. Ren'Py 7.5.2 without
    #     all_nvl_callbacks).  Polls nvl_list for new entries and pushes
    #     dialogue/narration events for any that the callback missed.
    # -------------------------------------------------------------------------

    _vnf_nvl_watch_last_len = [0]
    _vnf_nvl_watch_first_fp = [None]  # semantic page snapshot for in-place changes
    def _vnf_prepare_nvl_entry_queue():
        """Return the native tracker queue and whether this is a re-init."""
        _existing = getattr(_sys_mod, "_vnf_nvl_entries_since_watch", None)
        _queue_exists = type(_existing) is _VNF_NATIVE_LIST_TYPE
        _reloaded = bool(
            _queue_exists
            or hasattr(_sys_mod, "_vnf_nvl_added_since_watch")
            or hasattr(_sys_mod, "_vnf_nvl_tracker_wrapper")
            or hasattr(_sys_mod, "_vnf_nvl_tracker_class")
            or hasattr(_sys_mod, "_vnf_nvl_done_tracker_wrapper")
            or hasattr(_sys_mod, "_vnf_nvl_done_tracker_class"))
        if not _queue_exists:
            _existing = _VNF_NATIVE_LIST_TYPE()
            _sys_mod._vnf_nvl_entries_since_watch = _existing
        else:
            # Callback ownership is interaction-local and does not survive a
            # Shift+R re-init. Drop its paired pending entries; the watcher is
            # baselined below so the visible page is not replayed either.
            _existing[:] = []
        return _existing, _reloaded

    (_vnf_nvl_entries_since_watch,
     _vnf_nvl_tracker_reloaded) = _vnf_prepare_nvl_entry_queue()
    if (not hasattr(_sys_mod, "_vnf_nvl_added_since_watch")
            or type(_sys_mod._vnf_nvl_added_since_watch)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_added_since_watch = _VNF_NATIVE_LIST_TYPE((0,))
    _vnf_nvl_added_since_watch = _sys_mod._vnf_nvl_added_since_watch
    if (not hasattr(_sys_mod, "_vnf_nvl_tracker_page_fp")
            or type(_sys_mod._vnf_nvl_tracker_page_fp)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_tracker_page_fp = _VNF_NATIVE_LIST_TYPE((None,))
    else:
        _sys_mod._vnf_nvl_tracker_page_fp[0] = None
    _vnf_nvl_tracker_page_fp = _sys_mod._vnf_nvl_tracker_page_fp
    if (not hasattr(_sys_mod, "_vnf_nvl_done_tracker_depth")
            or type(_sys_mod._vnf_nvl_done_tracker_depth)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_done_tracker_depth = _VNF_NATIVE_LIST_TYPE((0,))
    else:
        _sys_mod._vnf_nvl_done_tracker_depth[0] = 0
    _vnf_nvl_done_tracker_depth = _sys_mod._vnf_nvl_done_tracker_depth
    if (not hasattr(_sys_mod, "_vnf_nvl_add_merge_suppressed")
            or type(_sys_mod._vnf_nvl_add_merge_suppressed)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_add_merge_suppressed = (
            _VNF_NATIVE_LIST_TYPE((0,)))
    else:
        _sys_mod._vnf_nvl_add_merge_suppressed[0] = 0
    _vnf_nvl_add_merge_suppressed = (
        _sys_mod._vnf_nvl_add_merge_suppressed)
    if (not hasattr(_sys_mod, "_vnf_nvl_active_legacy_adds")
            or type(_sys_mod._vnf_nvl_active_legacy_adds)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_active_legacy_adds = _VNF_NATIVE_LIST_TYPE()
    else:
        _sys_mod._vnf_nvl_active_legacy_adds[:] = []
    _vnf_nvl_active_legacy_adds = (
        _sys_mod._vnf_nvl_active_legacy_adds)
    if (not hasattr(_sys_mod, "_vnf_nvl_current_adds_during_legacy")
            or type(_sys_mod._vnf_nvl_current_adds_during_legacy)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_current_adds_during_legacy = (
            _VNF_NATIVE_LIST_TYPE())
    else:
        _sys_mod._vnf_nvl_current_adds_during_legacy[:] = []
    _vnf_nvl_current_adds_during_legacy = (
        _sys_mod._vnf_nvl_current_adds_during_legacy)
    if (not hasattr(_sys_mod, "_vnf_nvl_capture_reset_epoch")
            or type(_sys_mod._vnf_nvl_capture_reset_epoch)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_capture_reset_epoch = (
            _VNF_NATIVE_LIST_TYPE((0,)))
    _vnf_nvl_capture_reset_epoch = (
        _sys_mod._vnf_nvl_capture_reset_epoch)
    if (not hasattr(_sys_mod, "_vnf_nvl_recorded_adds_pending_done")
            or type(_sys_mod._vnf_nvl_recorded_adds_pending_done)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_recorded_adds_pending_done = (
            _VNF_NATIVE_LIST_TYPE())
    else:
        _sys_mod._vnf_nvl_recorded_adds_pending_done[:] = []
    _vnf_nvl_recorded_adds_pending_done = (
        _sys_mod._vnf_nvl_recorded_adds_pending_done)
    if (not hasattr(_sys_mod, "_vnf_nvl_done_in_progress_entries")
            or type(_sys_mod._vnf_nvl_done_in_progress_entries)
            is not _VNF_NATIVE_LIST_TYPE):
        _sys_mod._vnf_nvl_done_in_progress_entries = (
            _VNF_NATIVE_LIST_TYPE())
    else:
        _sys_mod._vnf_nvl_done_in_progress_entries[:] = []
    _vnf_nvl_done_in_progress_entries = (
        _sys_mod._vnf_nvl_done_in_progress_entries)

    def _vnf_install_nvl_add_tracker():
        """Track NVL occurrences before display, independent of callbacks."""
        # v5 existed in live review worktrees before the era-aware wrapper was
        # committed. Keep the number monotonic so Shift+R upgrades those too.
        _tracker_version = 8
        _state_list_type = type(_sys_mod._vnf_nvl_added_since_watch)
        if not hasattr(_sys_mod, "_vnf_nvl_recorded_adds_pending_done"):
            _sys_mod._vnf_nvl_recorded_adds_pending_done = _state_list_type()
        if not hasattr(_sys_mod, "_vnf_nvl_add_merge_suppressed"):
            _sys_mod._vnf_nvl_add_merge_suppressed = _state_list_type((0,))
        if not hasattr(_sys_mod, "_vnf_nvl_active_legacy_adds"):
            _sys_mod._vnf_nvl_active_legacy_adds = _state_list_type()
        if not hasattr(_sys_mod, "_vnf_nvl_current_adds_during_legacy"):
            _sys_mod._vnf_nvl_current_adds_during_legacy = _state_list_type()
        if not hasattr(_sys_mod, "_vnf_nvl_capture_reset_epoch"):
            _sys_mod._vnf_nvl_capture_reset_epoch = _state_list_type((0,))
        try:
            _nvl_class = renpy.store.NVLCharacter
            _current = _nvl_class.do_add
        except Exception:
            return
        if getattr(_current, "_vnf_owned_nvl_add_tracker", False):
            if (getattr(_current, "_vnf_nvl_tracker_version", None)
                    == _tracker_version):
                _sys_mod._vnf_nvl_tracker_class = _nvl_class
                _sys_mod._vnf_nvl_tracker_wrapper = _current
                return
            # Upgrade an older owned wrapper without stacking its counter.
            _current = getattr(
                _current, "_vnf_original_nvl_add", _current)
        # A game/mod may cooperatively wrap our tracker after init. On reload,
        # do not wrap that outer function again: it still delegates through the
        # recorded tracker, while stacking ours would double-count. If a mod
        # replaces it without delegation, tracking degrades to the conservative
        # capped-page replay rather than risking a silent drop.
        _tracked_class = getattr(_sys_mod, "_vnf_nvl_tracker_class", None)
        _tracked_wrapper = getattr(
            _sys_mod, "_vnf_nvl_tracker_wrapper", None)
        if (_tracked_class is not None
                and getattr(_tracked_wrapper,
                            "_vnf_nvl_tracker_version", None)
                == _tracker_version):
            try:
                if issubclass(_nvl_class, _tracked_class):
                    return
            except Exception:
                if _tracked_class is _nvl_class:
                    return
        _legacy_counter_wrapper = bool(
            _tracked_wrapper is not None
            and getattr(_tracked_wrapper,
                        "_vnf_owned_nvl_add_tracker", False)
            and getattr(_tracked_wrapper,
                        "_vnf_nvl_tracker_version", None)
            != _tracker_version)
        _legacy_exact_wrapper = bool(
            _legacy_counter_wrapper
            and (getattr(_tracked_wrapper,
                         "_vnf_nvl_tracker_version", 0) or 0) >= 2)
        def _tracked_nvl_add(self, *args, **kwargs):
            # Rollback rebuilds the restored NVL row before the ordinary
            # interact/periodic observers necessarily run. Baseline that
            # restored page before pre-add capture can publish it as fresh;
            # ownership for this reconstructed occurrence is recorded below.
            try:
                _vnf_observe_rollback_resume()
            except Exception:
                pass
            try:
                # Each do_add begins a new story occurrence. A callback which
                # did not arrive before this point must not let the prior
                # boundary claim this occurrence, including in a reentrant
                # cooperative wrapper while legacy suppression remains active.
                _vnf_nvl_prepublished_callbacks[:] = []
            except Exception:
                pass
            if vnf_player.enabled:
                try:
                    # A custom screen may append directly to nvl_list between
                    # ordinary do_add calls. Publish those rows before the
                    # next callback so their story order is retained.
                    _vnf_capture_untracked_nvl_delta("pre-add", True)
                except Exception:
                    pass
            try:
                _before_page = _vnf_nvl_tracker_page_fp[0]
            except Exception:
                _before_page = None
            _target = ((args[0], args[1]) if len(args) >= 2 else None)
            _before_counter = _sys_mod._vnf_nvl_added_since_watch[0]
            _before_reset_epoch = (
                _sys_mod._vnf_nvl_capture_reset_epoch[0])
            _before_entry_matches = 0
            if _legacy_exact_wrapper and _target is not None:
                _before_entry_matches = sum(
                    1 for _entry in
                    _sys_mod._vnf_nvl_entries_since_watch
                    if _entry == _target)
            _nested_record_start = len(
                _sys_mod._vnf_nvl_current_adds_during_legacy)
            _active_legacy_add = None
            _legacy_completed = False
            _legacy_abandoned = False
            if _legacy_counter_wrapper:
                _active_record_type = type(
                    _sys_mod._vnf_nvl_active_legacy_adds)
                _active_legacy_add = _active_record_type((
                    id(self),
                    args[0] if len(args) >= 1 else None,
                    args[1] if len(args) >= 2 else None,
                    False,
                    _legacy_exact_wrapper,
                    _before_entry_matches,
                    _nested_record_start,
                    _before_reset_epoch,
                    False,
                    _before_counter))
                _sys_mod._vnf_nvl_active_legacy_adds.append(
                    _active_legacy_add)
                _sys_mod._vnf_nvl_add_merge_suppressed[0] += 1
            try:
                _result = _current(self, *args, **kwargs)
            finally:
                if _legacy_counter_wrapper:
                    try:
                        # Preserve nested calls through this tracker while
                        # removing exactly one bookkeeping record made by the
                        # legacy wrapper around this call. This cleanup belongs
                        # in finally because a cooperative outer wrapper may
                        # raise after the legacy tracker has already returned.
                        _nested_records = (
                            _sys_mod._vnf_nvl_current_adds_during_legacy[
                                _nested_record_start:])
                        _current_reset_epoch = (
                            _sys_mod._vnf_nvl_capture_reset_epoch[0])
                        _current_nested_records = [
                            _record for _record in _nested_records
                            if (len(_record) >= 4
                                and _record[3] == _current_reset_epoch)
                        ]
                        _reset_during_call = (
                            _current_reset_epoch != _before_reset_epoch)
                        if (_legacy_exact_wrapper
                                and _target is not None):
                            _nested_target_records = sum(
                                1 for _record in _current_nested_records
                                if len(_record) >= 3
                                and _record[1] == args[0]
                                and _record[2] == args[1])
                            _after_entry_matches = sum(
                                1 for _entry in
                                _sys_mod._vnf_nvl_entries_since_watch
                                if _entry == _target)
                            _legacy_recorded = (
                                _after_entry_matches
                                - (0 if _reset_during_call
                                   else _before_entry_matches)
                                > _nested_target_records)
                        else:
                            _legacy_recorded = (
                                _sys_mod._vnf_nvl_added_since_watch[0]
                                - (0 if _reset_during_call
                                   else _before_counter)
                                > len(_current_nested_records))
                        _boundary_published = bool(
                            _active_legacy_add is not None
                            and len(_active_legacy_add) >= 9
                            and _active_legacy_add[8])
                        _compensate_legacy = bool(
                            _legacy_recorded and not _boundary_published)
                        _legacy_completed = bool(
                            _compensate_legacy
                            and _active_legacy_add is not None
                            and _active_legacy_add[3])
                        _legacy_abandoned = bool(
                            _boundary_published
                            or (_reset_during_call and not _legacy_recorded))
                        if (_compensate_legacy
                                and _sys_mod._vnf_nvl_added_since_watch[0]
                                > 0):
                            _sys_mod._vnf_nvl_added_since_watch[0] -= 1
                        if (_compensate_legacy and _legacy_exact_wrapper
                                and _target is not None):
                            _entries = _sys_mod._vnf_nvl_entries_since_watch
                            for _index in range(
                                    len(_entries) - 1, -1, -1):
                                if _entries[_index] == _target:
                                    del _entries[_index]
                                    break
                            _pending_entries = (
                                _sys_mod._vnf_nvl_recorded_adds_pending_done)
                            _character_id = id(self)
                            for _index in range(
                                    len(_pending_entries) - 1, -1, -1):
                                _pending = _pending_entries[_index]
                                if (len(_pending) >= 3
                                        and _pending[0] == _character_id
                                        and _pending[1] == args[0]
                                        and _pending[2] == args[1]):
                                    del _pending_entries[_index]
                                    break
                    finally:
                        if _active_legacy_add is not None:
                            for _index in range(
                                    len(_sys_mod._vnf_nvl_active_legacy_adds)
                                    - 1, -1, -1):
                                if (_sys_mod._vnf_nvl_active_legacy_adds[
                                        _index] is _active_legacy_add):
                                    del _sys_mod._vnf_nvl_active_legacy_adds[
                                        _index]
                                    break
                        _sys_mod._vnf_nvl_add_merge_suppressed[0] = max(
                            0,
                            _sys_mod._vnf_nvl_add_merge_suppressed[0] - 1)
                        if (_sys_mod._vnf_nvl_add_merge_suppressed[0]
                                == 0):
                            _sys_mod._vnf_nvl_current_adds_during_legacy[
                                :] = []
            if _legacy_abandoned:
                return _result
            try:
                # Before 6.99.13, Ren'Py persists the say here. Newer engines
                # only prepare/evict, so an identical append there can only be
                # a custom row and must publish.
                try:
                    _persists_in_add = renpy_version < (6, 99, 13)
                except Exception:
                    _persists_in_add = False
                _tracked_entry = (
                    (args[0], args[1])
                    if ((_persists_in_add or _legacy_completed)
                        and len(args) >= 2) else None)
                _merge_previous = (
                    _vnf_nvl_tracker_page_fp[0]
                    if _legacy_completed else _before_page)
                _vnf_merge_nvl_method_delta(
                    _merge_previous,
                    _vnf_nvl_fingerprint(
                        getattr(renpy.store, "nvl_list", None)),
                    _tracked_entry, "post-add", vnf_player.enabled)
            except Exception:
                pass
            if vnf_player.enabled:
                _sys_mod._vnf_nvl_added_since_watch[0] += 1
                if len(args) >= 2:
                    _sys_mod._vnf_nvl_entries_since_watch.append(
                        (args[0], args[1]))
                    if not _legacy_completed:
                        try:
                            # A character instance has at most one active NVL
                            # say. If a custom flow skipped do_done, its receipt
                            # must not claim a later interaction from the same
                            # character.
                            _character_id = id(self)
                            _sys_mod._vnf_nvl_recorded_adds_pending_done[:] = [
                                _pending for _pending in
                                _sys_mod._vnf_nvl_recorded_adds_pending_done
                                if _pending[0] != _character_id
                            ]
                            _sys_mod._vnf_nvl_recorded_adds_pending_done.append(
                                (_character_id, args[0], args[1], False))
                        except Exception:
                            pass
                    try:
                        if (_sys_mod._vnf_nvl_add_merge_suppressed[0]
                                > 0):
                            _sys_mod._vnf_nvl_current_adds_during_legacy.append(
                                (id(self), args[0], args[1],
                                 _sys_mod._vnf_nvl_capture_reset_epoch[0],
                                 bool(_persists_in_add or _legacy_completed)))
                    except Exception:
                        pass
            return _result

        _tracked_nvl_add._vnf_owned_nvl_add_tracker = True
        _tracked_nvl_add._vnf_nvl_tracker_version = _tracker_version
        _tracked_nvl_add._vnf_original_nvl_add = _current
        _nvl_class.do_add = _tracked_nvl_add
        _sys_mod._vnf_nvl_tracker_class = _nvl_class
        _sys_mod._vnf_nvl_tracker_wrapper = _tracked_nvl_add

    _vnf_install_nvl_add_tracker()

    def _vnf_install_nvl_done_tracker():
        """Advance the semantic page shadow after Ren'Py persists a row."""
        _tracker_version = 1
        try:
            _nvl_class = renpy.store.NVLCharacter
            _current = _nvl_class.do_done
        except Exception:
            return
        if getattr(_current, "_vnf_owned_nvl_done_tracker", False):
            if (getattr(_current, "_vnf_nvl_done_tracker_version", None)
                    == _tracker_version):
                _sys_mod._vnf_nvl_done_tracker_class = _nvl_class
                _sys_mod._vnf_nvl_done_tracker_wrapper = _current
                return
            _current = getattr(
                _current, "_vnf_original_nvl_done", _current)
        def _tracked_nvl_done(self, *args, **kwargs):
            _outermost = (_vnf_nvl_done_tracker_depth[0] == 0)
            _recorded_add = False
            _in_progress_entry = None
            _vnf_nvl_done_tracker_depth[0] += 1
            try:
                if _outermost and vnf_player.enabled:
                    try:
                        # Stock do_display pops its temporary row before
                        # entering do_done. Keep the receipt claimable, but do
                        # not let an older identical persistent row stand in
                        # for that vanished temporary occurrence.
                        _vnf_deactivate_recorded_nvl_display(self, args)
                        # Catch custom rows added between do_add and do_done
                        # before the ordinary row changes the persistent page.
                        _vnf_capture_untracked_nvl_delta("pre-done", True)
                    except Exception:
                        pass
                    try:
                        _recorded_add = _vnf_claim_recorded_nvl_add(
                            self, args)
                        if (_recorded_add
                                and _sys_mod._vnf_nvl_add_merge_suppressed[0]
                                > 0
                                and len(args) >= 2):
                            for _active_add in reversed(
                                    _sys_mod._vnf_nvl_active_legacy_adds):
                                if (len(_active_add) >= 4
                                        and _active_add[0] == id(self)
                                        and _active_add[1] == args[0]
                                        and _active_add[2] == args[1]):
                                    _active_add[3] = True
                                    break
                        if _recorded_add and len(args) >= 2:
                            _in_progress_entry = (
                                id(self), args[0], args[1], False)
                            _vnf_nvl_done_in_progress_entries.append(
                                _in_progress_entry)
                    except Exception:
                        _recorded_add = False
                try:
                    _before_page = _vnf_nvl_tracker_page_fp[0]
                except Exception:
                    _before_page = None
                _result = _current(self, *args, **kwargs)
                if _outermost:
                    try:
                        _in_progress_consumed = False
                        if _in_progress_entry is not None:
                            for _pending in (
                                    _vnf_nvl_done_in_progress_entries):
                                if (_pending[:3]
                                        == _in_progress_entry[:3]):
                                    _in_progress_consumed = bool(
                                        len(_pending) >= 4 and _pending[3])
                                    break
                        _tracked_entry = (
                            (args[0], args[1])
                            if (_recorded_add
                                and not _in_progress_consumed) else None)
                        _merge_previous = (
                            _vnf_nvl_tracker_page_fp[0]
                            if _in_progress_consumed else _before_page)
                        _vnf_merge_nvl_method_delta(
                            _merge_previous,
                            _vnf_nvl_fingerprint(
                                getattr(renpy.store, "nvl_list", None)),
                            _tracked_entry, "post-done",
                            vnf_player.enabled)
                        if _recorded_add:
                            # The exact stream already owns this occurrence.
                            # Baseline its now-persistent page so a watcher
                            # that ran inside do_display cannot replay it.
                            _persistent_fp = _vnf_nvl_fingerprint(
                                getattr(renpy.store, "nvl_list", None))
                            _vnf_nvl_watch_first_fp[0] = _persistent_fp
                            _vnf_nvl_watch_last_len[0] = len(
                                _persistent_fp or ())
                    except Exception:
                        pass
                return _result
            finally:
                if _in_progress_entry is not None:
                    try:
                        for _index, _pending in enumerate(
                                _vnf_nvl_done_in_progress_entries):
                            if _pending[:3] == _in_progress_entry[:3]:
                                del _vnf_nvl_done_in_progress_entries[_index]
                                break
                    except Exception:
                        pass
                _vnf_nvl_done_tracker_depth[0] = max(
                    0, _vnf_nvl_done_tracker_depth[0] - 1)

        _tracked_nvl_done._vnf_owned_nvl_done_tracker = True
        _tracked_nvl_done._vnf_nvl_done_tracker_version = _tracker_version
        _tracked_nvl_done._vnf_original_nvl_done = _current
        _nvl_class.do_done = _tracked_nvl_done
        _sys_mod._vnf_nvl_done_tracker_class = _nvl_class
        _sys_mod._vnf_nvl_done_tracker_wrapper = _tracked_nvl_done

    _vnf_install_nvl_done_tracker()

    def _vnf_nvl_speaker_key(value):
        """Canonicalize display-only speaker decoration for matching."""
        _name = _vnf_stringify(value)
        if not _name:
            return None
        _name = renpy.text.extras.filter_text_tags(
            _name, allow=set()).strip()
        # Older NVL screens commonly apply who_prefix="[" and
        # who_suffix="]". Do not strip other punctuation: ``ARIA`` and
        # ``(ARIA)`` can be intentional, distinct display names.
        while _name.startswith("[") and _name.endswith("]"):
            _name = _name[1:-1].strip()
        return _name or None

    def _vnf_nvl_event_key(ev):
        """Return the semantic occurrence key shared by both NVL paths."""
        _text = _vnf_stringify(ev.get("text") or "") or ""
        return (_vnf_nvl_speaker_key(ev.get("character")), _text.strip())

    def _vnf_note_nvl_callback_event(ev, speaker_source=None):
        """Remember one NVL occurrence already emitted by the callback."""
        _name, _text = _vnf_nvl_event_key(ev)
        _source = (_vnf_stringify(speaker_source)
                   if isinstance(speaker_source, basestring) else None)
        _vnf_nvl_callback_occurrences.append((_name, _text, _source))

    def _vnf_note_nvl_prepublished_callback(entry, origin, epoch):
        """Remember a fallback row delivered before its NVL callback."""
        _ev = _vnf_nvl_entry_event(entry)
        if _ev is None:
            return
        _name, _text = _vnf_nvl_event_key(_ev)
        _source = (_ev.get("_vnf_speaker_source")
                   if _ev.get("_vnf_speaker_unresolved") else None)
        for _marker in _vnf_nvl_prepublished_callbacks:
            if _marker[0] == origin and _marker[1] == epoch:
                return
        _vnf_nvl_prepublished_callbacks.append(
            (origin, epoch, _name, _text, _source))

    def _vnf_claim_nvl_prepublished_callback_event(
            ev, speaker_source=None):
        """Consume one callback whose occurrence was boundary-published."""
        if not _vnf_nvl_prepublished_callbacks:
            return False
        _callback_name, _callback_text = _vnf_nvl_event_key(ev)
        _callback_source = (
            _vnf_stringify(speaker_source)
            if isinstance(speaker_source, basestring) else None)
        for _index, _marker in enumerate(
                _vnf_nvl_prepublished_callbacks):
            if _marker[3] != _callback_text:
                continue
            _same_speaker = (_marker[2] == _callback_name)
            _same_unresolved_source = (
                _callback_source is not None
                and _marker[4] == _callback_source)
            if not (_same_speaker or _same_unresolved_source):
                continue
            del _vnf_nvl_prepublished_callbacks[:_index + 1]
            return True
        return False

    def _vnf_claim_nvl_callback_event(ev):
        """Consume the matching callback occurrence, preserving repeats."""
        if not _vnf_nvl_callback_occurrences:
            return False
        _fallback_name, _fallback_text = _vnf_nvl_event_key(ev)
        _fallback_speaker_unresolved = bool(
            ev.get("_vnf_speaker_unresolved"))
        _fallback_source = ev.get("_vnf_speaker_source")
        # The queue is capped, and engine-specific row shapes can still leave
        # a stale marker. Scan the bounded ledger rather than allowing one
        # mismatched head to poison every later occurrence on the page.
        for _index, _occurrence in enumerate(
                _vnf_nvl_callback_occurrences):
            _callback_name = _occurrence[0]
            _callback_text = _occurrence[1]
            _callback_source = _occurrence[2]
            if _callback_text != _fallback_text:
                continue
            _same_speaker = (_callback_name == _fallback_name)
            _same_unresolved_source = (
                _fallback_speaker_unresolved
                and _fallback_source is not None
                and _callback_source == _fallback_source)
            if not (_same_speaker or _same_unresolved_source):
                continue
            # Both sources preserve story order. Any unmatched markers before
            # this occurrence belong to abandoned or already-observed rows;
            # retaining them could falsely claim a later repeated line.
            del _vnf_nvl_callback_occurrences[:_index + 1]
            return True
        return False

    def _vnf_nvl_entry_event(_nvl_entry):
        """Convert one Ren'Py nvl_list row to a bridge event."""
        if _nvl_entry is None:
            return None
        try:
            _nvl_who = _nvl_entry[0]
            _nvl_what = _nvl_entry[1] if len(_nvl_entry) > 1 else None
            if _nvl_what is None:
                return None
            _nvl_clean = _vnf_clean_event_text(_nvl_what).strip()
            if not _nvl_clean:
                return None
            _nvl_name, _nvl_name_resolved = (
                _vnf_character_display_name_state(_nvl_who))
            if _nvl_name:
                _ev = dict(type="dialogue", character=_nvl_name,
                           text=_nvl_clean, mode="nvl")
            else:
                _ev = dict(type="narration", text=_nvl_clean, mode="nvl")
            if _nvl_who is not None and not _nvl_name_resolved:
                _ev["_vnf_speaker_unresolved"] = True
                if isinstance(_nvl_who, basestring):
                    _ev["_vnf_speaker_source"] = _vnf_stringify(_nvl_who)
            return _ev
        except Exception:
            return None

    def _vnf_publish_nvl_entry(_nvl_entry, source):
        """Publish one fallback row unless the callback owns its occurrence."""
        _ev = _vnf_nvl_entry_event(_nvl_entry)
        if _ev is None:
            return False
        if _vnf_claim_nvl_callback_event(_ev):
            _vnf_log("[NVL {}] callback-owned row: {}".format(
                source, _ev.get("text", "")[:80]))
            return False
        _ev.pop("_vnf_speaker_unresolved", None)
        _ev.pop("_vnf_speaker_source", None)
        _vnf_log("[NVL {}] {}: {}".format(
            source, _ev.get("character") or "Narrator",
            _ev.get("text", "")[:80]))
        _vnf_client.push_event(_ev)
        return True

    def _vnf_nvl_fingerprint(nvl):
        """Return a semantic fingerprint of the current page, or None.

        Ren'Py may reconstruct RevertableList row objects while preserving the
        same on-screen NVL page. Object identity would treat that reconstruction
        as an in-place clear and replay the full cumulative page. The complete
        row sequence is needed because a replacement page may retain its first
        line while changing later rows.
        """
        if not nvl:
            return None
        try:
            _rows = []
            for _e in nvl:
                if _e is None:
                    _rows.append(("", ""))
                    continue
                _who = _vnf_text(_e[0]) if len(_e) > 0 else ""
                _what = _vnf_text(_e[1]) if len(_e) > 1 else ""
                _rows.append((_who, _what))
            return tuple(_rows)
        except Exception:
            return None

    def _vnf_nvl_delta_start(previous, current, delivered,
                             window_limit=None, added=0):
        """Return the first row that has not retained its prior occurrence."""
        if current is None:
            return 0
        if previous is None:
            return 0
        if added:
            # do_add runs once per NVL occurrence before display. It is the
            # authoritative answer for append, extend, capped head eviction,
            # and multiple occurrences between watcher ticks.
            return min(delivered, max(0, len(current) - added))
        if current == previous:
            return min(delivered, len(current))

        _limit = min(len(previous), len(current))
        _common = 0
        while _common < _limit and previous[_common] == current[_common]:
            _common += 1

        _at_window_limit = bool(
            window_limit and len(previous) >= window_limit
            and len(current) >= window_limit
            and len(previous) == len(current))
        if delivered >= len(previous) and _at_window_limit:
            # At a hard cap, semantic overlap cannot distinguish retained rows
            # from complete turnover with repeated prose. Without do_add
            # provenance, replay rather than risk skipping a new occurrence.
            _retained = 0
        else:
            _retained = _common
        # Never skip a row that the prior cursor had not delivered.
        return min(delivered, _retained)

    def _vnf_filter_pending_nvl_display_entries(entries):
        """Remove temporary display rows already owned by the exact stream.

        Ren'Py temporarily appends an NVL row while ``do_display`` runs. Its
        periodic callback can observe that page between do_add and do_done.
        Suppress only occurrences backed by both an outstanding add receipt
        and the current exact batch; do_done remains the sole receipt consumer.
        """
        _exact_counts = {}
        for _tracked in _vnf_nvl_entries_since_watch:
            _event = _vnf_nvl_entry_event(_tracked)
            if _event is None:
                continue
            _key = _vnf_nvl_event_key(_event)
            _exact_counts[_key] = _exact_counts.get(_key, 0) + 1
        _candidates = {}
        for _index, _pending in enumerate(
                _vnf_nvl_recorded_adds_pending_done):
            _event = _vnf_nvl_entry_event((_pending[1], _pending[2]))
            if _event is None:
                continue
            _key = _vnf_nvl_event_key(_event)
            _active = bool(len(_pending) >= 4 and _pending[3])
            if not _active and not _exact_counts.get(_key, 0):
                continue
            if not _active:
                _exact_counts[_key] -= 1
            _candidates.setdefault(_key, []).append(_index)
        if not _candidates:
            return _VNF_NATIVE_LIST_TYPE(entries)
        _result = _VNF_NATIVE_LIST_TYPE()
        for _entry in entries:
            _event = _vnf_nvl_entry_event(_entry)
            _key = (_vnf_nvl_event_key(_event)
                    if _event is not None else None)
            _matches = _candidates.get(_key, [])
            if _matches:
                _pending_index = _matches.pop(0)
                _pending = _vnf_nvl_recorded_adds_pending_done[
                    _pending_index]
                if len(_pending) < 4 or not _pending[3]:
                    _vnf_nvl_recorded_adds_pending_done[_pending_index] = (
                        _pending[0], _pending[1], _pending[2], True)
                continue
            _result.append(_entry)
        return _result

    def _vnf_filter_in_progress_nvl_done_entries(entries):
        """Remove rows already owned by an outer do_done exact occurrence."""
        _candidates = {}
        for _index, _pending in enumerate(
                _vnf_nvl_done_in_progress_entries):
            if len(_pending) >= 4 and _pending[3]:
                continue
            _event = _vnf_nvl_entry_event((_pending[1], _pending[2]))
            if _event is None:
                continue
            _key = _vnf_nvl_event_key(_event)
            _candidates.setdefault(_key, []).append(_index)
        if not _candidates:
            return _VNF_NATIVE_LIST_TYPE(entries)
        _result = _VNF_NATIVE_LIST_TYPE()
        for _entry in entries:
            _event = _vnf_nvl_entry_event(_entry)
            _key = (_vnf_nvl_event_key(_event)
                    if _event is not None else None)
            _matches = _candidates.get(_key, [])
            if _matches:
                _pending_index = _matches.pop(0)
                _pending = _vnf_nvl_done_in_progress_entries[
                    _pending_index]
                _vnf_nvl_done_in_progress_entries[_pending_index] = (
                    _pending[0], _pending[1], _pending[2], True)
                continue
            _result.append(_entry)
        return _result

    def _vnf_strip_active_nvl_display_entries(entries):
        """Return the persistent page beneath active do_display rows."""
        _result = _VNF_NATIVE_LIST_TYPE(entries or ())
        for _index, _pending in enumerate(
                _vnf_nvl_recorded_adds_pending_done):
            if len(_pending) < 4 or not _pending[3]:
                continue
            _event = _vnf_nvl_entry_event((_pending[1], _pending[2]))
            _key = (_vnf_nvl_event_key(_event)
                    if _event is not None else None)
            _matched = False
            for _entry_index in range(len(_result) - 1, -1, -1):
                _entry_event = _vnf_nvl_entry_event(_result[_entry_index])
                if (_entry_event is not None
                        and _vnf_nvl_event_key(_entry_event) == _key):
                    del _result[_entry_index]
                    _matched = True
                    break
            if not _matched:
                _vnf_nvl_recorded_adds_pending_done[_index] = (
                    _pending[0], _pending[1], _pending[2], False)
        return tuple(_result)

    def _vnf_capture_untracked_nvl_delta(source, publish_now=False):
        """Merge direct nvl_list mutations with the exact do_add stream.

        Some custom NVL screens append rows without calling
        NVLCharacter.do_add. The tracker remains authoritative for calls it
        observes, while this semantic shadow contributes only page changes
        that occurred between those calls. Rows found before another do_add
        are published immediately to preserve chronology; trailing rows join
        the next watcher batch.
        """
        try:
            if _sys_mod._vnf_nvl_add_merge_suppressed[0] > 0:
                return
        except Exception:
            pass
        _nvl = getattr(renpy.store, "nvl_list", None)
        _current = _vnf_nvl_fingerprint(_nvl)
        _semantic_current = _vnf_strip_active_nvl_display_entries(_current)
        _previous = _vnf_nvl_tracker_page_fp[0]
        if _semantic_current == _previous:
            return
        try:
            _window_limit = renpy.config.nvl_list_length
        except Exception:
            _window_limit = None
        _start = _vnf_nvl_delta_start(
            _previous, _semantic_current, len(_previous or ()),
            _window_limit, 0)
        _new_entries = _vnf_filter_pending_nvl_display_entries(
            (_semantic_current or ())[_start:])
        _publish_entries = _vnf_filter_in_progress_nvl_done_entries(
            _new_entries)
        for _entry in _publish_entries:
            if publish_now:
                _vnf_publish_and_queue_nvl_entry(_entry, source)
            else:
                _vnf_nvl_entries_since_watch.append(_entry)
        # The semantic shadow represents persistent rows. Rows removed above
        # belong to do_display's temporary page and must not become its base.
        _vnf_nvl_tracker_page_fp[0] = tuple(
            (_semantic_current or ())[:_start]) + tuple(_new_entries)

    def _vnf_publish_and_queue_nvl_entry(entry, source):
        """Publish now, then queue an ownership copy for page baselining."""
        _event = _vnf_nvl_entry_event(entry)
        if _event is None:
            return
        _vnf_publish_nvl_entry(entry, source)
        _speaker_source = (_event.get("_vnf_speaker_source")
                           if _event.get("_vnf_speaker_unresolved")
                           else None)
        _vnf_note_nvl_callback_event(_event, _speaker_source)
        _vnf_nvl_entries_since_watch.append(entry)

    def _vnf_merge_nvl_method_delta(previous, current, tracked_entry,
                                    source, publish=True):
        """Baseline stock NVL mutations while preserving override extras."""
        if source == "post-add":
            try:
                if _sys_mod._vnf_nvl_add_merge_suppressed[0] > 0:
                    return
            except Exception:
                pass
        _previous = previous or ()
        _current = current or ()
        _limit = min(len(_previous), len(_current))
        _retained = _limit
        while (_retained > 0
                and tuple(_previous[-_retained:])
                != tuple(_current[:_retained])):
            _retained -= 1
        _new_entries = _VNF_NATIVE_LIST_TYPE(_current[_retained:])
        if tracked_entry is not None:
            _tracked_event = _vnf_nvl_entry_event(tracked_entry)
            _tracked_key = (_vnf_nvl_event_key(_tracked_event)
                            if _tracked_event is not None else None)
            if _tracked_key is not None:
                for _index, _entry in enumerate(_new_entries):
                    _event = _vnf_nvl_entry_event(_entry)
                    if (_event is not None
                            and _vnf_nvl_event_key(_event) == _tracked_key):
                        del _new_entries[_index]
                        break
        if publish:
            for _entry in _new_entries:
                _vnf_publish_and_queue_nvl_entry(_entry, source)
        _vnf_nvl_tracker_page_fp[0] = current

    def _vnf_claim_recorded_nvl_add(character, done_args):
        """Claim the exact do_add receipt corresponding to one do_done."""
        if len(done_args) < 2:
            return False
        _target_event = _vnf_nvl_entry_event(
            (done_args[0], done_args[1]))
        if _target_event is None:
            return False
        _target_key = _vnf_nvl_event_key(_target_event)
        _character_id = id(character)
        for _index, _pending in enumerate(
                _vnf_nvl_recorded_adds_pending_done):
            if _pending[0] != _character_id:
                continue
            _event = _vnf_nvl_entry_event((_pending[1], _pending[2]))
            if (_event is not None
                    and _vnf_nvl_event_key(_event) == _target_key):
                del _vnf_nvl_recorded_adds_pending_done[_index]
                return True
        return False

    def _vnf_deactivate_recorded_nvl_display(character, done_args):
        """Mark a claimed do_display row absent before do_done persistence."""
        if len(done_args) < 2:
            return
        _target_event = _vnf_nvl_entry_event(
            (done_args[0], done_args[1]))
        if _target_event is None:
            return
        _target_key = _vnf_nvl_event_key(_target_event)
        _character_id = id(character)
        for _index, _pending in enumerate(
                _vnf_nvl_recorded_adds_pending_done):
            if _pending[0] != _character_id:
                continue
            _event = _vnf_nvl_entry_event((_pending[1], _pending[2]))
            if (_event is not None
                    and _vnf_nvl_event_key(_event) == _target_key):
                _vnf_nvl_recorded_adds_pending_done[_index] = (
                    _pending[0], _pending[1], _pending[2], False)
                return

    def _vnf_reconcile_equal_nvl_tail(nvl):
        """Consume callback ownership when a bounded roll looks unchanged.

        A full NVL window containing repeated identical rows can roll from one
        occurrence to another without changing its semantic fingerprint. The
        callback already published the new occurrence; claim its final visible
        row here so that marker cannot suppress a later fallback event.
        """
        if not nvl or not _vnf_nvl_callback_occurrences:
            return
        _ev = _vnf_nvl_entry_event(nvl[-1])
        if _ev is None:
            return
        _remaining = len(_vnf_nvl_callback_occurrences)
        _claimed = 0
        while _remaining > 0 and _vnf_claim_nvl_callback_event(_ev):
            _claimed += 1
            _remaining -= 1
        if _claimed:
            _vnf_log(
                "[NVL watch] callback-owned equal-page tail x{}: {}".format(
                    _claimed, _ev.get("text", "")[:80]))

    def _vnf_prepare_legacy_boundary_entries(entries):
        """Include proven legacy rows and identify their exact occurrences."""
        _result = _VNF_NATIVE_LIST_TYPE(entries or ())
        _candidates = _VNF_NATIVE_LIST_TYPE()
        try:
            _current_epoch = _sys_mod._vnf_nvl_capture_reset_epoch[0]
            _nested_records = (
                _sys_mod._vnf_nvl_current_adds_during_legacy)
            _active_records = _sys_mod._vnf_nvl_active_legacy_adds
        except Exception:
            return _result, _candidates
        # Match nested records to their actual exact-stream tuple. Object
        # identity survives the list copies and page reordering below, letting
        # the publisher create reverse callback ownership only if that exact
        # occurrence really emits.
        _reserved_indices = set()
        for _record in reversed(_nested_records):
            if len(_record) < 4 or _record[3] != _current_epoch:
                continue
            _target = (_record[1], _record[2])
            for _index in range(len(_result) - 1, -1, -1):
                if _index in _reserved_indices:
                    continue
                if _result[_index] == _target:
                    _reserved_indices.add(_index)
                    _candidates.append((
                        _result[_index], ("nested", id(_record)),
                        _current_epoch))
                    break
        for _active in reversed(_active_records):
            if len(_active) < 10:
                continue
            if _active[7] != _current_epoch:
                continue
            _target = (_active[1], _active[2])
            _baseline = _active[5]
            _nested_target_count = sum(
                1 for _record in _nested_records[_active[6]:]
                if (len(_record) >= 4
                    and _record[1] == _active[1]
                    and _record[2] == _active[2]
                    and _record[3] == _current_epoch))
            if _active[4]:
                _current_count = sum(
                    1 for _entry in _result if _entry == _target)
                _legacy_recorded = (
                    _current_count - _baseline > _nested_target_count)
            else:
                _counter_baseline = _active[9]
                _current_nested_count = sum(
                    1 for _record in _nested_records[_active[6]:]
                    if (len(_record) >= 4
                        and _record[3] == _current_epoch))
                _legacy_recorded = (
                    _vnf_nvl_added_since_watch[0] - _counter_baseline
                    > _current_nested_count)
            if not _legacy_recorded:
                continue
            _active[8] = True
            if not _active[4] and len(_active) >= 3:
                _result.append(_target)
                _candidates.append((
                    _result[-1], ("active", id(_active)), _current_epoch))
            else:
                for _index in range(len(_result) - 1, -1, -1):
                    if _index in _reserved_indices:
                        continue
                    if _result[_index] == _target:
                        _reserved_indices.add(_index)
                        _candidates.append((
                            _result[_index], ("active", id(_active)),
                            _current_epoch))
                        break
        return _result, _candidates

    def _vnf_merge_forced_boundary_page_entries(entries):
        """Merge direct page rows with exact ownership during a forced drain."""
        _result = _VNF_NATIVE_LIST_TYPE(entries or ())
        _nvl = getattr(renpy.store, "nvl_list", None)
        if not _nvl:
            return _result
        try:
            _current_epoch = _sys_mod._vnf_nvl_capture_reset_epoch[0]
            _active_records = _sys_mod._vnf_nvl_active_legacy_adds
            _nested_records = (
                _sys_mod._vnf_nvl_current_adds_during_legacy)
        except Exception:
            return _result

        try:
            _persists_in_add = renpy_version < (6, 99, 13)
        except Exception:
            _persists_in_add = False
        _page_owned = _VNF_NATIVE_LIST_TYPE()
        for _active in _active_records:
            if (len(_active) >= 9 and _active[8]
                    and len(_active) >= 8
                    and _active[7] == _current_epoch):
                if _persists_in_add or bool(_active[3]):
                    _page_owned.append((_active[1], _active[2]))
        for _record in _nested_records:
            if len(_record) >= 4 and _record[3] == _current_epoch:
                _nested_persisted = (
                    bool(_record[4]) if len(_record) >= 5
                    else _persists_in_add)
                if _nested_persisted:
                    _page_owned.append((_record[1], _record[2]))

        # Remove the current exact occurrences from the accumulated queue.
        # Reinsert them while walking the live page delta so direct rows keep
        # their real position relative to the legacy say. Search from the end
        # because an identical older occurrence may still be queued.
        _removed_by_key = {}
        for _owned_entry in reversed(_page_owned):
            _owned_fp = _vnf_nvl_fingerprint((_owned_entry,))
            _owned_key = (_owned_fp[0] if _owned_fp else None)
            if _owned_key is None:
                continue
            for _index in range(len(_result) - 1, -1, -1):
                _entry_fp = _vnf_nvl_fingerprint((_result[_index],))
                if _entry_fp and _entry_fp[0] == _owned_key:
                    _removed_by_key.setdefault(_owned_key, []).insert(
                        0, _result[_index])
                    del _result[_index]
                    break

        try:
            _previous = _vnf_nvl_tracker_page_fp[0] or ()
        except Exception:
            _previous = ()
        # Forced boundaries can run while do_display has temporarily appended
        # the current say. Reconcile against the persistent page beneath it;
        # the exact stream already owns that occurrence.
        _semantic_nvl = _vnf_strip_active_nvl_display_entries(_nvl)
        _current = _vnf_nvl_fingerprint(_semantic_nvl) or ()
        try:
            _window_limit = renpy.config.nvl_list_length
        except Exception:
            _window_limit = None
        _start = _vnf_nvl_delta_start(
            _previous, _current, len(_previous), _window_limit, 0)
        for _entry in _semantic_nvl[_start:]:
            _entry_fp = _vnf_nvl_fingerprint((_entry,))
            _entry_key = (_entry_fp[0] if _entry_fp else None)
            _owned_entries = _removed_by_key.get(_entry_key, [])
            if _owned_entries:
                _result.append(_owned_entries.pop(0))
            else:
                _result.append(_entry)
        # A modern do_add may own an occurrence without persisting it until
        # do_done. Preserve such exact rows even though the live page cannot
        # place them.
        for _owned_entries in _removed_by_key.values():
            _result.extend(_owned_entries)
        try:
            _vnf_nvl_tracker_page_fp[0] = tuple(_current)
        except Exception:
            pass
        return _result

    def _vnf_publish_forced_boundary_entry(entry, source, candidates):
        """Publish one forced row and transfer its callback ownership."""
        _published = _vnf_publish_nvl_entry(entry, source)
        if not _published:
            return False
        for _index, _candidate in enumerate(candidates):
            if _candidate[0] is not entry:
                continue
            _vnf_note_nvl_prepublished_callback(
                entry, _candidate[1], _candidate[2])
            del candidates[_index]
            break
        return True

    def _vnf_flush_nvl_entries(source, force_boundary=False):
        """Publish unseen rows and advance the NVL-list observation cursor."""
        _suppressed = False
        try:
            # An upgraded cooperative wrapper may still be executing an old
            # tracker. Let the current wrapper normalize its ownership before
            # a reentrant watcher can publish or drain the legacy record.
            _suppressed = bool(
                _sys_mod._vnf_nvl_add_merge_suppressed[0] > 0)
            if _suppressed and not force_boundary:
                return
        except Exception:
            pass
        nvl = getattr(renpy.store, "nvl_list", None)
        try:
            # Capture direct mutations after the final do_add in this batch.
            _vnf_capture_untracked_nvl_delta(source)
        except Exception:
            pass
        try:
            _tracked_entries = _VNF_NATIVE_LIST_TYPE(
                _vnf_nvl_entries_since_watch)
        except Exception:
            _tracked_entries = []
        _forced_exact_stream = bool(
            _suppressed and force_boundary and _tracked_entries)
        _forced_callback_candidates = _VNF_NATIVE_LIST_TYPE()
        if _suppressed and force_boundary:
            (_tracked_entries, _forced_callback_candidates) = (
                _vnf_prepare_legacy_boundary_entries(_tracked_entries))
            _tracked_entries = _vnf_merge_forced_boundary_page_entries(
                _tracked_entries)
            _forced_exact_stream = bool(
                _forced_exact_stream or _tracked_entries)
        try:
            _done_in_progress = bool(
                _vnf_nvl_done_in_progress_entries)
        except Exception:
            _done_in_progress = False
        if nvl is None:
            for _nvl_entry in _tracked_entries:
                if _suppressed and force_boundary:
                    _vnf_publish_forced_boundary_entry(
                        _nvl_entry, source, _forced_callback_candidates)
                else:
                    _vnf_publish_nvl_entry(_nvl_entry, source)
            _vnf_nvl_callback_occurrences[:] = []
            _vnf_nvl_watch_last_len[0] = 0
            _vnf_nvl_watch_first_fp[0] = None
            try:
                _vnf_nvl_tracker_page_fp[0] = None
            except Exception:
                pass
            try:
                _vnf_nvl_entries_since_watch[:] = []
            except Exception:
                pass
            try:
                _vnf_nvl_added_since_watch[0] = 0
            except Exception:
                pass
            return
        try:
            _observed_nvl = _vnf_nvl_tracker_page_fp[0]
        except Exception:
            _observed_nvl = None
        if _observed_nvl is None:
            _observed_nvl = _vnf_nvl_fingerprint(nvl) or ()
        _fp = tuple(_observed_nvl)
        _raw_fp = _vnf_nvl_fingerprint(nvl) or ()
        if tuple(_raw_fp) == _fp:
            _observed_rows = nvl
        else:
            # Preserve the engine's raw row objects for publication while the
            # semantic fingerprint omits temporary do_display occurrences.
            _observed_rows = _VNF_NATIVE_LIST_TYPE()
            _target_index = 0
            for _raw_row, _raw_key in zip(nvl, _raw_fp):
                if (_target_index < len(_fp)
                        and _raw_key == _fp[_target_index]):
                    _observed_rows.append(_raw_row)
                    _target_index += 1
            if _target_index != len(_fp):
                _observed_rows = _fp
        _cur_len = len(_fp)
        try:
            _added = _vnf_nvl_added_since_watch[0]
        except Exception:
            _added = 0
        if _tracked_entries or _done_in_progress or _forced_exact_stream:
            # The do_add wrapper is an exact occurrence stream. Prefer it to
            # reconstructing chronology from the cumulative page: repeated
            # extends and capped eviction can collapse several additions into
            # one visible row, but every occurrence remains present here.
            for _nvl_entry in _tracked_entries:
                if _suppressed and force_boundary:
                    _vnf_publish_forced_boundary_entry(
                        _nvl_entry, source, _forced_callback_candidates)
                else:
                    _vnf_publish_nvl_entry(_nvl_entry, source)
            _vnf_nvl_entries_since_watch[:] = []
            _vnf_nvl_callback_occurrences[:] = []
            try:
                _semantic_fp = _vnf_nvl_tracker_page_fp[0]
            except Exception:
                _semantic_fp = _fp
            if _semantic_fp is None:
                _semantic_fp = _fp
            _vnf_nvl_watch_last_len[0] = len(_semantic_fp or ())
            _vnf_nvl_watch_first_fp[0] = _semantic_fp
            _vnf_nvl_added_since_watch[0] = 0
            return
        if _fp == _vnf_nvl_watch_first_fp[0] and not _added:
            _vnf_reconcile_equal_nvl_tail(_observed_rows)
        try:
            _window_limit = renpy.config.nvl_list_length
        except Exception:
            _window_limit = None
        _start = _vnf_nvl_delta_start(
            _vnf_nvl_watch_first_fp[0], _fp,
            _vnf_nvl_watch_last_len[0], _window_limit, _added)
        for _nvl_entry in _observed_rows[_start:]:
            _vnf_publish_nvl_entry(_nvl_entry, source)
        # With no exact tracker stream, a completed semantic replay is still
        # the ownership boundary. Any unmatched marker describes a row that
        # was replaced, evicted, or abandoned and must not claim a later line.
        _vnf_nvl_callback_occurrences[:] = []
        _vnf_nvl_watch_last_len[0] = _cur_len
        _vnf_nvl_watch_first_fp[0] = _fp
        try:
            _vnf_nvl_tracker_page_fp[0] = _fp
        except Exception:
            pass
        try:
            _vnf_nvl_added_since_watch[0] = 0
        except Exception:
            pass
        try:
            _vnf_nvl_entries_since_watch[:] = []
        except Exception:
            pass

    def _vnf_reset_nvl_capture_state():
        """Drop occurrence ownership from an abandoned Ren'Py timeline."""
        try:
            _sys_mod._vnf_nvl_capture_reset_epoch[0] += 1
        except Exception:
            pass
        _vnf_nvl_callback_occurrences[:] = []
        _vnf_nvl_watch_last_len[0] = 0
        _vnf_nvl_watch_first_fp[0] = None
        try:
            _vnf_nvl_tracker_page_fp[0] = None
        except Exception:
            pass
        try:
            _vnf_nvl_added_since_watch[0] = 0
        except Exception:
            pass
        try:
            _vnf_nvl_entries_since_watch[:] = []
        except Exception:
            pass
        try:
            _vnf_nvl_recorded_adds_pending_done[:] = []
        except Exception:
            pass

    def _vnf_baseline_nvl_capture_state():
        """Treat a restored NVL page as existing state, not new dialogue."""
        try:
            _sys_mod._vnf_nvl_capture_reset_epoch[0] += 1
        except Exception:
            pass
        _vnf_nvl_callback_occurrences[:] = []
        # A load/rollback replaces the pending interaction. No callback from a
        # forced drain in the abandoned timeline may claim restored dialogue.
        _vnf_nvl_prepublished_callbacks[:] = []
        try:
            _vnf_nvl_added_since_watch[0] = 0
        except Exception:
            pass
        try:
            _vnf_nvl_entries_since_watch[:] = []
        except Exception:
            pass
        try:
            _vnf_nvl_recorded_adds_pending_done[:] = []
        except Exception:
            pass
        _nvl = getattr(renpy.store, "nvl_list", None) or []
        _vnf_nvl_watch_last_len[0] = len(_nvl)
        _vnf_nvl_watch_first_fp[0] = _vnf_nvl_fingerprint(_nvl)
        try:
            _vnf_nvl_tracker_page_fp[0] = _vnf_nvl_watch_first_fp[0]
        except Exception:
            pass

    if _vnf_nvl_tracker_reloaded:
        _vnf_baseline_nvl_capture_state()

    def _vnf_periodic_nvl_watch():
        """Check nvl_list for new entries and push dialogue events."""
        if not vnf_player.enabled:
            _vnf_baseline_nvl_capture_state()
            return
        if (_is_renpy6
                and bool(getattr(
                    _sys_mod, "_vnf_rollback_pending", (False,))[0])):
            _vnf_rollback_resume_seen[0] = True
            _vnf_finish_rollback_resume()
            _sys_mod._vnf_rollback_pending[0] = False
        elif not _is_renpy6:
            # Periodic callbacks can run before interact callbacks on the
            # first modern Ren'Py tick after rollback. Retire the rollback
            # edge before the restored cumulative NVL page can be scraped.
            _vnf_observe_rollback_resume()
            try:
                if bool(getattr(renpy.game, "after_rollback", False)):
                    # The restored interaction can rebuild its current NVL
                    # row before that row's character callback runs. Keep the
                    # restored page baselined until callback ownership is
                    # available; the first post-rollback tick then publishes
                    # only a genuinely missed row.
                    return
            except Exception:
                pass
        _vnf_flush_nvl_entries("watch")

    _vnf_periodic_nvl_watch_callback = _safe_periodic(
        _vnf_periodic_nvl_watch)
    renpy.config.periodic_callbacks.append(
        _vnf_periodic_nvl_watch_callback)

    # -------------------------------------------------------------------------
    # 2. Scene / Show / Hide -- track visual context
    # -------------------------------------------------------------------------

    _vnf_original_show = _vnf_save_original("show", renpy.exports, "show")
    _vnf_original_hide = _vnf_save_original("hide", renpy.exports, "hide")
    _vnf_original_scene = _vnf_save_original("scene", renpy.exports, "scene")

    def _vnf_show_wrapper(name, at_list=[], layer=None, what=None, zorder=None,
                          tag=None, behind=[], atl=None, transient=False,
                          munge_name=True, **kwargs):
        if vnf_player.enabled:
            img_name = name if isinstance(name, str) else " ".join(name) if isinstance(name, tuple) else _vnf_text(name)
            _vnf_client.push_event(dict(type="show", name=img_name))
            _vnf_log("show: " + img_name)

            # Capture text content from 'show text' statements for LLM context
            if name == "text" and what:
                try:
                    # 'what' is a Text displayable. 'what.text' is a list of strings in Ren'Py 7/8.
                    txt_list = getattr(what, "text", [])
                    display_text = "".join(txt_list) if _vnf_is_list(txt_list) else _vnf_text(txt_list)
                    if display_text:
                        # Filter tags for the bridge
                        clean_text = renpy.text.extras.filter_text_tags(display_text, allow=set())
                        _vnf_client.push_event(dict(type="text_overlay", text=clean_text))
                        _vnf_log("text_overlay: " + clean_text[:40])
                except Exception:
                    pass

            if vnf_player.screenshot_enabled and vnf_player.screenshot_on in ("scene_change", "both"):
                _vnf_capture_screenshot()

        return _vnf_original_show(
            name, at_list=at_list, layer=layer, what=what, zorder=zorder,
            tag=tag, behind=behind, atl=atl, transient=transient,
            munge_name=munge_name, **kwargs
        )

    def _vnf_hide_wrapper(name, layer=None):
        if vnf_player.enabled:
            img_name = name if isinstance(name, str) else " ".join(name) if isinstance(name, tuple) else _vnf_text(name)
            _vnf_client.push_event(dict(type="hide", name=img_name))
            _vnf_log("hide: " + img_name)

        return _vnf_original_hide(name, layer=layer)

    def _vnf_scene_wrapper(layer="master"):
        if vnf_player.enabled:
            _vnf_client.push_event(dict(type="scene", layer=layer))
            _vnf_log("scene cleared: " + _vnf_text(layer))

            if vnf_player.screenshot_enabled and vnf_player.screenshot_on in ("scene_change", "both"):
                _vnf_capture_screenshot()

        return _vnf_original_scene(layer)

    renpy.exports.show = _vnf_show_wrapper
    renpy.exports.hide = _vnf_hide_wrapper
    renpy.exports.scene = _vnf_scene_wrapper

    # Hook Pause statement to handle fast-forward and reporting
    _vnf_original_pause = _vnf_save_original("pause", renpy.exports, "pause")
    def _vnf_pause_wrapper(delay=None, **kwargs):
        global _vnf_auto_advance_last_what
        if not vnf_player.enabled:
            return _vnf_original_pause(delay=delay, **kwargs)

        _scripted_timer = delay is not None
        _vnf_log("pause: delay={}".format(delay))
        _vnf_client.push_event(dict(type="pause", delay=delay))

        if vnf_player.fast_forward:
            delay = 0.0
        elif delay is None and not vnf_player.allow_user_override:
            # External mode: dismiss bare pauses instantly (content
            # warnings, splash screens, etc.).  No user is watching.
            delay = 0.0
            _vnf_log("External mode: auto-dismissing bare pause")
        elif delay is None and vnf_player.pause_timeout is not None:
            # Cap infinite pause.  Base on reading time if text visible.
            effective = vnf_player.pause_timeout
            if vnf_player.reading_cps > 0:
                try:
                    text_len = 0
                    for _tag, _scr in _vnf_get_showing_screens():
                        _sd = {"texts": [], "choices": [], "value_map": {}, "buttons": []}
                        _vnf_walk_screen(_scr, _sd)
                        for _t in _sd["texts"]:
                            text_len += len(_t)
                    if text_len > 0:
                        effective = max(effective, text_len / vnf_player.reading_cps)
                except Exception:
                    pass
            delay = effective
            _vnf_log("Capping infinite pause to {:.1f}s".format(delay))

        renpy.store._vnf_in_pause = True
        renpy.store._vnf_in_timed_pause = _scripted_timer
        _vnf_auto_advance_last_what = None
        try:
            return _vnf_original_pause(delay=delay, **kwargs)
        finally:
            renpy.store._vnf_in_pause = False
            renpy.store._vnf_in_timed_pause = False
            _vnf_auto_advance_last_what = None

    renpy.exports.pause = _vnf_pause_wrapper
    renpy.pause = _vnf_pause_wrapper

    # Also patch the top-level renpy.show / renpy.hide / renpy.scene aliases.
    renpy.show = _vnf_show_wrapper
    renpy.hide = _vnf_hide_wrapper
    renpy.scene = _vnf_scene_wrapper

    # CRITICAL: Also patch renpy.config.show / .scene / .hide which are the
    # actual references used by the AST (show_imspec / Scene.execute / Hide.execute).
    # These are set in renpy.config.init() to point at renpy.exports.show etc.
    # at import time, so our later monkey-patch of renpy.exports.* doesn't
    # affect them unless we patch here too.
    if hasattr(renpy.config, "show"):
        renpy.config.show = _vnf_show_wrapper
        renpy.config.hide = _vnf_hide_wrapper
        renpy.config.scene = _vnf_scene_wrapper

    # -------------------------------------------------------------------------
    # 3. Menu Interception -- hybrid mode with bridge polling
    # -------------------------------------------------------------------------

    _vnf_original_display_menu = _vnf_save_original("display_menu", renpy.exports, "display_menu")
    # Only access nvl_menu if it exists ( Ren'Py 7.5.2+)
    if hasattr(renpy.exports, "nvl_menu"):
        _vnf_original_nvl_menu = _vnf_save_original("nvl_menu", renpy.exports, "nvl_menu")
    else:
        _vnf_original_nvl_menu = None

    # Re-entrancy guard: prevents duplicate choice_request events when
    # the NVL wrapper calls nvl_menu which internally calls display_menu,
    # hitting the ADV wrapper a second time.
    _vnf_menu_wrapper_active = False

    # Tracks the current active menu context so that the resync command
    # can re-push the choice_request if the bridge loses it.
    _vnf_current_menu_context = [None]  # [dict or None]

    def _make_vnf_menu_wrapper(original_fn, is_nvl_menu=False):
        """
        Factory that creates a menu wrapper bound to a specific original
        menu function.  This is critical for NVL compatibility: the NVL
        menu wrapper must call _vnf_original_nvl_menu (which renders
        choices inside the NVL screen), NOT _vnf_original_display_menu
        (which uses the ADV choice screen).
        """
        def _wrapper(items, interact=True, **kwargs):
            """
            Intercepts menu display.

            Both hybrid and external-only modes call original_fn() so
            the full Ren'Py interaction cycle runs (periodic callbacks,
            screen scraping, pre-resolve steps, etc.).

            In hybrid mode:
              - User clicks can resolve the choice
              - Observation delays give the viewer time to see the choice

            In external-only mode (allow_user_override=False):
              - Mouse clicks are blocked; only the bridge can resolve
              - Observation delays are skipped (instant resolution)
              - Timeout falls back to unblocking mouse for user
            """
            global _vnf_menu_wrapper_active

            if not vnf_player.enabled or not interact:
                try:
                    return original_fn(items, interact=interact, **kwargs)
                except TypeError:
                    return original_fn(items, **kwargs)

            # Re-entrancy guard: if another wrapper is already handling
            # this menu call (e.g. NVL wrapper → nvl_menu → display_menu),
            # delegate directly to avoid a duplicate choice_request.
            if _vnf_menu_wrapper_active:
                try:
                    return original_fn(items, interact=interact, **kwargs)
                except TypeError:
                    return original_fn(items, **kwargs)

            _vnf_menu_wrapper_active = True
            try:
                return _wrapper_inner(items, interact=interact, **kwargs)
            finally:
                # Normally consumed by _wrapper_inner. If interception exits
                # early, publish the prompt instead of leaking it to a later
                # unrelated menu.
                _vnf_flush_pending_menu_caption()
                _vnf_menu_wrapper_active = False
                _vnf_current_menu_context[0] = None

        def _wrapper_inner(items, interact=True, **kwargs):
            def _clean_label(lbl):
                """Strip Ren'Py markup, resolve [variable] refs, and
                unescape literal brackets from a choice label."""
                s = renpy.text.extras.filter_text_tags(
                    _vnf_stringify(lbl) or "", allow=set())
                s = _vnf_substitute(s)
                s = s.replace("[[", "[")  # Ren'Py escape for literal bracket
                return s

            # Extract inline image tags from raw choice labels before
            # Ren'Py strips them.  Used to distinguish dice from cost icons.
            import re as _re_label
            _image_tag_re = _re_label.compile(r'\{image=([^}]+)\}')
            _raw_condition_by_label = {}
            try:
                for _ri in (_vnf_raw_menu_items[0] or []):
                    _raw_label = _clean_label(_ri.get("label", "")).strip()
                    _cond = _ri.get("condition")
                    _ik = _ri.get("item_kwargs", {})
                    if _vnf_is_mapping(_ik) and _ik.get("condition"):
                        _cond = _ik.get("condition")
                    if _raw_label and isinstance(_cond, basestring):
                        _raw_condition_by_label[_raw_label] = _cond
            except Exception:
                _raw_condition_by_label = {}

            # Build choice list and value map.
            choices = []
            value_map = {}
            idx = 1  # Start at 1 for 1-based indexing (consistent with user-facing choice indices)
            for label, value in items:
                # Extract image tags from raw label before stripping.
                _img_tags = _image_tag_re.findall(
                    _vnf_stringify(label) or "")

                if value is None:
                    # Caption -- not a selectable choice.
                    _c = {"index": None, "label": _clean_label(label), "caption": True, "disabled": False}
                    if _img_tags:
                        _c["_image_tags"] = _img_tags
                    choices.append(_c)
                else:
                    # Check if the choice is disabled.
                    # We use renpy.exports.is_sensitive() for the most accurate check,
                    # plus a label heuristic: some games mark choices as disabled
                    # via the label text (e.g. "(disabled)") while keeping them
                    # technically sensitive/clickable.
                    _cleaned = _clean_label(label).strip()
                    is_disabled = (
                        not renpy.exports.is_sensitive(value)
                        or _cleaned == "(disabled)"
                        or _cleaned.endswith("(disabled)")
                    )

                    # Extract the underlying value if wrapped in ChoiceReturn.
                    raw_value = value
                    while hasattr(raw_value, "value"):
                        raw_value = raw_value.value

                    if is_disabled:
                        # Disabled choice -- include for context but not selectable.
                        _c = {"index": None, "label": _clean_label(label), "caption": False, "disabled": True}
                        # Keep the underlying return identity in the local
                        # menu context. It is never serialized or made
                        # selectable; the post-render scrape uses it only to
                        # recognize the same disabled row when a screen
                        # transform changes the text it draws.
                        _c["_choice_value"] = raw_value
                        # Label-based disabled sentinels (ending in
                        # "(disabled)") are game-authored hints.  They
                        # may not surface as scraped ChoiceReturn
                        # buttons, so preserve them across scrape ticks.
                        if _cleaned.endswith("(disabled)"):
                            _c["_keep_stale"] = True
                            _stale_cond = _raw_condition_by_label.get(_cleaned)
                            if _stale_cond:
                                _c["_drop_when_condition_false"] = _stale_cond
                    else:
                        _c = {"index": idx, "label": _clean_label(label), "caption": False, "disabled": False}
                        value_map[idx] = raw_value  # Map 1-based index -> the raw return value
                        idx += 1
                    if _img_tags:
                        _c["_image_tags"] = _img_tags
                    choices.append(_c)

            # With narrator_menu enabled, Ren'Py 6/7 narrate bare caption rows
            # before display_menu and omit them from ``items``; their character
            # callback also carries no ``what``. Recover those rows from the
            # Menu AST captured by our execute hook. Modern callback staging
            # remains the fallback for dynamically assembled menu prompts.
            _vnf_restore_raw_menu_captions(
                choices, _vnf_raw_menu_items[0], _clean_label)

            # Reset shared pipeline state for this menu cycle.
            global _vnf_pipeline_shared
            _vnf_pipeline_shared = {}

            # Run menu augmenter pipeline.  Augmenters can filter,
            # recover, or annotate choices using AST data.
            _aug_pre_sets = {}
            _aug_pre_resolve = {}
            if _vnf_raw_menu_items[0] is not None and _vnf_menu_augmenters:
                _aug_ctx = {
                    "raw_ast_items": _vnf_raw_menu_items[0],
                    "items": items,
                    "choices": choices,
                    "value_map": value_map,
                    "next_idx": idx,
                    "pre_sets": {},
                    "pre_resolve_steps": {},
                    "shared": _vnf_pipeline_shared,
                }
                _aug_ctx = _vnf_apply_menu_augmenters(_aug_ctx)
                choices = _aug_ctx["choices"]
                value_map = _aug_ctx["value_map"]
                idx = _aug_ctx["next_idx"]
                _aug_pre_sets = _aug_ctx.get("pre_sets", {})
                _aug_pre_resolve = _aug_ctx.get("pre_resolve_steps", {})
            _vnf_raw_menu_items[0] = None

            # Only include enabled, non-caption choices in the selectable list.
            choice_labels = [c["label"] for c in choices if not c.get("caption") and not c.get("disabled")]

            def _compute_pacing_delay(choices):
                """Compute a non-blocking pacing delay (seconds) so a
                user observer has time to read on-screen text before
                the LLM can act.  Returns 0 when no delay is needed."""
                if not vnf_player.allow_user_override:
                    return 0
                pacing = vnf_player.pacing
                if pacing == "off":
                    return 0
                if pacing == "audio":
                    # Audio pacing can't be computed as a fixed delay
                    # (depends on playback state).  Fall back to
                    # post_action_delay.
                    return vnf_player.post_action_delay
                if pacing == "text":
                    if vnf_player.reading_cps > 0:
                        text_len = sum(len(c["label"]) for c in choices if c.get("caption"))
                        # Include the current on-screen narration text
                        # so the viewer has time to read it before the
                        # choices become actionable.
                        _say_text = _vnf_last_what or getattr(renpy.store, "_last_say_what", None) or _vnf_auto_advance_last_what
                        if _say_text:
                            text_len += len(_vnf_text(_say_text))
                        return max(vnf_player.post_action_delay, text_len / vnf_player.reading_cps)
                    else:
                        return vnf_player.auto_advance_delay
                return 0

            # Auto-skip single-option choices: if there's exactly one
            # selectable choice, resolve it immediately without pushing
            # a choice_request to the bridge.
            # Loop guard: if this exact label was auto-skipped on the
            # immediately preceding menu call (no other menu in between),
            # it's a runaway loop.  Legitimate hub returns always have
            # other menus (dialogue choices) between repetitions.
            if vnf_player.auto_skip_single_choice and len(choice_labels) == 1 and len(value_map) == 1:
                only_idx = list(value_map.keys())[0]
                only_value = value_map[only_idx]
                only_label = choice_labels[0]
                _is_loop = _vnf_is_autoskip_loop(only_label)
                # Check the predicate — mods can block auto-skip for
                # menus that act as containers (e.g. question hubs).
                _allow_skip = _vnf_auto_skip_predicate_allows(only_label)
                # Transform-driven pause: screen transforms can append
                # reasons to _vnf_autoskip.pause_reasons to block
                # auto-skip for the current interaction.
                # Also run a fresh scrape+transform so pause reasons
                # from screens that just appeared (same interaction
                # cycle) are captured before the auto-skip decision.
                if _allow_skip and not _vnf_autoskip.pause_reasons:
                    _vnf_refresh_transform_pause_reasons()
                if _allow_skip and _vnf_autoskip.pause_reasons:
                    _vnf_log("Auto-skip paused by transforms: {}".format(
                        ", ".join(_vnf_autoskip.pause_reasons)))
                    _allow_skip = False

                if not _is_loop and _allow_skip:
                    _vnf_log("Auto-skipping single choice: {}".format(only_label))
                    # No choice_request will carry the staged prompt here.
                    _vnf_flush_pending_menu_caption()
                    # Compute pacing delay up front so we can include it
                    # in the auto_skipped event for clients.
                    if vnf_player.allow_user_override:
                        _skip_delay = max(vnf_player.post_action_delay, 2.0)
                        if vnf_player.pacing == "text" and vnf_player.reading_cps > 0:
                            _narr_len = len(_vnf_last_what) if _vnf_last_what else 0
                            if _narr_len == 0:
                                _nvl = getattr(renpy.store, "nvl_list", None) or []
                                if _nvl and _nvl[-1] and len(_nvl[-1]) >= 2 and _nvl[-1][1]:
                                    _narr_len = len(_vnf_text(_nvl[-1][1]))
                            _skip_delay = max(_skip_delay, _narr_len / vnf_player.reading_cps)
                    else:
                        _skip_delay = vnf_player.post_action_delay
                    # Check the event filter — mods can suppress
                    # bookkeeping labels while keeping narrative ones.
                    _skip_filter = vnf_player.auto_skip_event_filter
                    try:
                        # Mod-provided; must not crash the menu path.
                        _emit = _skip_filter(only_label) if _skip_filter else True
                    except Exception:
                        _emit = True
                    if _emit:
                        _as_ev = dict(
                            type="auto_skipped",
                            label=only_label,
                            delay=_skip_delay,
                        )
                        # Include current narrative text so clients can
                        # display it inline without needing history -v.
                        if _vnf_last_what:
                            _as_ev["text"] = _vnf_last_what
                        _vnf_client.push_event(_as_ev)
                        if _vnf_menu_hooks:
                            _vnf_fire_menu_hooks("auto_skip", {
                                "label": only_label,
                                "delay": _skip_delay,
                                "pause_reasons": list(_vnf_autoskip.pause_reasons),
                            })
                        # Scrape visible screens so the LLM sees the
                        # current screen state alongside the skip.
                        try:
                            _vnf_scrape_visible_screens()
                        except Exception:
                            pass
                    # Invoke mod cleanup callback (e.g. nvl_clear).
                    _skip_cb = vnf_player.auto_skip_callback
                    if _skip_cb:
                        try:
                            _skip_cb(only_label)
                        except Exception:
                            pass
                    _vnf_autoskip.last_label = only_label
                    _vnf_autoskip.last_text = _vnf_current_autoskip_text()
                    _vnf_autoskip.just_skipped = True
                    # Run the full menu interaction cycle so the game
                    # engine maintains its state (NVL buffers,
                    # checkpoints, etc.).  The periodic callback
                    # auto-resolves after the pacing delay.
                    # In external mode, delay is 0 (resolve on first tick).
                    if not vnf_player.allow_user_override:
                        _skip_delay = 0
                    _vnf_autoskip.resolve_after = _time_monotonic() + _skip_delay
                    _vnf_autoskip.resolve_value = only_value
                    _vnf_autoskip.highlighted = (not vnf_player.allow_user_override)
                    _vnf_autoskip.resolve_choices = choices
                    try:
                        try:
                            _rv = original_fn(items, interact=interact, **kwargs)
                        except TypeError:
                            _rv = original_fn(items, **kwargs)
                        # Clear auto-resolve state.  In the happy
                        # path the periodic callback already cleared
                        # it before calling end_interaction.  But if
                        # the user clicked the choice before the
                        # timer expired, original_fn returns with the
                        # state still pending — without this cleanup
                        # the stale timer would fire on the NEXT
                        # interaction, resolving it with the wrong
                        # value.
                        _vnf_autoskip.clear_resolve()
                        return _rv
                    except Exception:
                        _vnf_autoskip.clear_resolve()
                        raise
                else:
                    if _is_loop:
                        _vnf_log("Auto-skip loop detected (consecutive repeat of '{}'), "
                                 "pushing as choice_request".format(only_label))
                    elif not _allow_skip:
                        # Distinguish predicate-blocked from pause-reason-blocked.
                        # Predicate-blocked: pass through to native (the menu is a
                        # container / hub that should stay open for clicks).
                        # Pause-reason-blocked: push as choice_request so the
                        # agent can still resolve it via act.
                        _blocked_by_pause = bool(_vnf_autoskip.pause_reasons)
                        _pred_result = _vnf_auto_skip_predicate_allows(only_label)
                        if not _pred_result and vnf_player.allow_user_override:
                            _vnf_log("Auto-skip blocked by predicate for '{}', "
                                     "passing through to native menu".format(only_label))
                            _vnf_autoskip.just_skipped = False
                            _vnf_flush_pending_menu_caption()
                            try:
                                try:
                                    return original_fn(items, interact=interact, **kwargs)
                                except TypeError:
                                    return original_fn(items, **kwargs)
                            except Exception:
                                raise
                        elif _blocked_by_pause:
                            _vnf_log("Auto-skip paused by transforms for '{}', "
                                     "pushing as choice_request".format(only_label))
                        else:
                            _vnf_log("Auto-skip blocked for '{}', "
                                     "pushing as choice_request".format(only_label))
                    _vnf_autoskip.just_skipped = False

            # Any menu that reaches this point (multi-choice, or loop
            # guard fallthrough) is NOT an auto-skip — clear the flag
            # so the next single-choice menu doesn't falsely detect a loop.
            _vnf_autoskip.just_skipped = False

            # The callback-captured narrator prompt now travels inside this
            # choice request. It is consumed once; an inline value=None
            # caption already supplied by Ren'Py wins unchanged.
            _vnf_attach_pending_menu_caption(choices)

            # Invoke pre-menu callback for game-specific cleanup.
            _pre_menu_cb = vnf_player.pre_menu_callback
            if _pre_menu_cb:
                try:
                    _pre_menu_cb(choice_labels, is_nvl_menu)
                except Exception as _pmcb_e:
                    _vnf_log("pre_menu_callback error: %s" % (
                        _vnf_text(_pmcb_e),))
                    import traceback as _pmcb_tb
                    _pmcb_tb.print_exc()

            # Run action transforms: merge choices with screen buttons,
            # let mods filter/annotate/promote, then push the result.
            _menu_screen_buttons = []
            _menu_modal_screens = []
            if _vnf_action_transforms:
                try:
                    _menu_showing = _vnf_get_showing_screens(vnf_player.scrape_visible_list)
                    _menu_per_screen = []
                    for _ms_tag, _ms_scr in _menu_showing:
                        try:
                            if _ms_scr.child is None and hasattr(_ms_scr, "update"):
                                try:
                                    _ms_scr.update()
                                except Exception:
                                    pass
                            _ms_modal = getattr(_ms_scr, "modal", False)
                            _ms_data = {"_tag": _ms_tag, "modal": bool(_ms_modal), "texts": [], "choices": [], "value_map": {}, "buttons": []}
                            _vnf_walk_screen(_ms_scr, _ms_data)
                            for _ms_btn in _ms_data["buttons"]:
                                _ms_btn["screen"] = _ms_tag
                            _menu_per_screen.append(_ms_data)
                        except Exception:
                            pass
                    _menu_per_screen = _vnf_apply_screen_transforms(_menu_per_screen)
                    for _ms_data in _menu_per_screen:
                        if _ms_data.get("modal"):
                            _menu_modal_screens.append(_ms_data["_tag"])
                        _menu_screen_buttons.extend(_ms_data.get("buttons", []))
                except Exception:
                    pass

            _menu_choice_dicts = list(choices)  # preserve augmenter fields
            _promoted_buttons = []
            global _vnf_choice_pre_sets, _vnf_choice_pre_resolve_steps
            _vnf_choice_pre_sets = {}
            _vnf_choice_pre_resolve_steps = {}
            _choice_annotations = {}  # index -> annotation string
            _choice_ids = {}  # vm_idx -> string id (from transforms)
            _menu_interactions = []
            _filtered_buttons = _menu_screen_buttons
            if _vnf_action_transforms:
                # Guarded: this is the menu wrapper's hot path and the
                # outer _wrapper has no except — malformed mod transform
                # output must degrade to the no-transform path, never
                # crash the host menu.
                try:
                    _at_actions, _at_ctx = _vnf_build_action_list(
                        _menu_choice_dicts, _menu_screen_buttons, _menu_modal_screens)
                    _at_actions = _vnf_apply_action_transforms(_at_actions, _at_ctx)
                    # Build canonical interaction list.
                    _menu_interactions = _vnf_build_interactions(_at_actions)
                    # Separate back into choices, promoted buttons, and
                    # regular buttons.  Promoted buttons (marked by mods)
                    # get their own section so the client can display them
                    # prominently alongside choices.
                    _filtered_buttons = []
                    _visible_choices = []
                    _visible_value_map = {}
                    _visible_choice_labels = []
                    _remapped_aug_pre_sets = {}
                    _remapped_aug_pre_resolve = {}
                    _vm_idx = 1  # value_map index for visible enabled choices
                    for a in _at_actions:
                        if a.get("hidden"):
                            # The value_map was built before transforms
                            # ran, so a hidden enabled choice still
                            # occupies its index — keep alignment.
                            if (a["source"] == "choice"
                                    and not a.get("disabled")
                                    and not a.get("caption")):
                                _vm_idx += 1
                            continue
                        if a["source"] == "choice":
                            is_enabled = not a.get("disabled") and not a.get("caption")
                            _choice_pos = len(_visible_choices) + 1
                            _old_vm_idx = a.get("choice_value_index")
                            _vm_idx = len(_visible_value_map) + 1
                            _choice_copy = {
                                "index": None,
                                "label": a.get("label", ""),
                                "caption": bool(a.get("caption", False)),
                                "disabled": bool(a.get("disabled", False)),
                            }
                            for _ck in a:
                                if _ck not in (
                                        "label", "source", "screen",
                                        "actions", "action_strs", "index",
                                        "choice_value_index", "disabled",
                                        "caption", "modal", "hidden",
                                        "pre_set", "pre_resolve_steps"):
                                    _choice_copy[_ck] = a[_ck]
                            # Extract pre_set, annotation, id, and pre_resolve_steps from transforms.
                            if is_enabled:
                                _choice_copy["index"] = _vm_idx
                                a["choice_value_index"] = _vm_idx
                                _visible_value_map[_vm_idx] = value_map.get(_old_vm_idx)
                                if _old_vm_idx in _aug_pre_sets:
                                    _remapped_aug_pre_sets[_vm_idx] = list(
                                        _aug_pre_sets[_old_vm_idx])
                                if _old_vm_idx in _aug_pre_resolve:
                                    _remapped_aug_pre_resolve[_vm_idx] = list(
                                        _aug_pre_resolve[_old_vm_idx])
                                if a.get("pre_set"):
                                    _vnf_choice_pre_sets[_vm_idx] = list(a["pre_set"])
                                if a.get("id"):
                                    _choice_ids[_vm_idx] = a["id"]
                                if a.get("pre_resolve_steps"):
                                    _vnf_choice_pre_resolve_steps[_vm_idx] = list(a["pre_resolve_steps"])
                                _visible_choice_labels.append(a.get("label", ""))
                                _vm_idx += 1
                            if a.get("annotation"):
                                _choice_annotations[_choice_pos] = a["annotation"]
                            _visible_choices.append(_choice_copy)
                            continue
                        _btn_dict = {
                            "label": a["label"],
                            "actions": a.get("actions", []),
                            "action_strs": a.get("action_strs", []),
                            "screen": a.get("screen", ""),
                        }
                        if a.get("annotation"):
                            _btn_dict["annotation"] = a["annotation"]
                        if a.get("_suppress_pending_action"):
                            _btn_dict["_suppress_pending_action"] = True
                        if a.get("promoted"):
                            _promoted_buttons.append(_btn_dict)
                        else:
                            _filtered_buttons.append(_btn_dict)
                    # Action transforms can hide source choices after the
                    # original menu value_map was built.  Rebuild the
                    # canonical pending choices from the transformed visible
                    # choice actions so act(number) follows the display.
                    choices = _visible_choices
                    value_map = _visible_value_map
                    choice_labels = _visible_choice_labels
                    _aug_pre_sets = _remapped_aug_pre_sets
                    _aug_pre_resolve = _remapped_aug_pre_resolve
                    _menu_interactions = _vnf_build_interactions(_at_actions)
                except (_EndInteraction,) + _CONTROL_EXCEPTIONS:
                    raise
                except Exception:
                    _menu_interactions = []
                    _filtered_buttons = _menu_screen_buttons
                    _promoted_buttons = []
                    _vnf_choice_pre_sets = {}
                    _vnf_choice_pre_resolve_steps = {}
                    _choice_annotations = {}
                    _choice_ids = {}
                    _vnf_log("Action transform pipeline failed; menu "
                             "continues without transforms: " +
                             _vnf_text(_tb_module.format_exc()))

            # Merge pre_sets and pre_resolve_steps from augmenter pipeline.
            if _aug_pre_sets:
                _vnf_choice_pre_sets.update(_aug_pre_sets)
            if _aug_pre_resolve:
                _vnf_choice_pre_resolve_steps.update(_aug_pre_resolve)

            # Push request to bridge.
            if vnf_player.debug:
                _vnf_log("Sending %s choice request: choices=%s, full_items=%s" % (
                    "NVL" if is_nvl_menu else "ADV",
                    _vnf_text(choice_labels),
                    _vnf_text([{"label": c["label"], "is_caption": c.get("caption", False), "is_disabled": c.get("disabled", False)} for c in choices])
                ))
            # Include inventory and stats for context
            inventory, stats = _vnf_get_inventory_stats()

            _full_items = []
            for _fi_pos, c in enumerate(choices):
                _fi = {"label": c["label"], "is_caption": c.get("caption", False), "is_disabled": c.get("disabled", False)}
                # Action list uses 1-based sequential index across
                # all items (captions, disabled, enabled).
                _fi_ann = _choice_annotations.get(_fi_pos + 1)
                if _fi_ann:
                    _fi["annotation"] = _fi_ann
                _full_items.append(_fi)

            # Build enriched choices list with IDs.
            # When transforms assign IDs, choices become {id, label} dicts.
            # Non-ID choices get auto-numbered starting from 1.
            if _choice_ids:
                _enriched_choices = []
                _auto_idx = 1
                for _ec_vm_idx, _ec_label in enumerate(choice_labels, 1):
                    _ec_id = _choice_ids.get(_ec_vm_idx)
                    if _ec_id is not None:
                        _enriched_choices.append({"id": _ec_id, "label": _ec_label})
                    else:
                        _enriched_choices.append({"id": _auto_idx, "label": _ec_label})
                        _auto_idx += 1
                _choices_for_event = _enriched_choices
            else:
                _choices_for_event = choice_labels

            _req_kwargs = dict(
                choices=_choices_for_event,
                # Also include captions and disabled items for context.
                full_items=_full_items,
                # Indicate whether this is an NVL or ADV menu.
                is_nvl=is_nvl_menu,
                # Include current inventory and stats for LLM decision-making
                inventory=inventory,
                stats=stats,
            )
            # Include filtered screen buttons so the client sees
            # relevant clickable elements alongside choices.
            if _filtered_buttons:
                _req_kwargs["screen_buttons"] = _filtered_buttons
            if _promoted_buttons:
                _req_kwargs["promoted_buttons"] = _promoted_buttons
            if _menu_interactions:
                _req_kwargs["interactions"] = _menu_interactions

            # NVL games can reach the next menu before the periodic NVL watcher
            # has emitted narration from the just-resolved choice. Flush now so
            # clients see story text before the next choice_request.
            if is_nvl_menu:
                try:
                    _vnf_periodic_nvl_watch()
                except Exception:
                    pass

            # This is the authoritative successor-decision sample. Publish
            # its delta after any late NVL narration and before the request so
            # the mutation belongs to this action, never the next one.
            _vnf_publish_inventory_stats(inventory, stats)

            # Push immediately with pre-render data.  The scraper will
            # push a game_state event with post-transform enriched data
            # on the next tick (interactions, stats, screen_buttons).
            req_id = _vnf_client.push_request("choice_request", **_req_kwargs)
            _vnf_commit_pending_menu_caption()
            _wrapper_req = [req_id]  # mutable; vis check may update
            if vnf_player.debug:
                _vnf_log("Choice request sent with ID: %s" % req_id)

            _vnf_log("Choice request {}{}: {}".format(req_id, " [NVL]" if is_nvl_menu else "", choice_labels))

            if _vnf_menu_hooks:
                _vnf_fire_menu_hooks("entry", {
                    "req_id": req_id,
                    "choice_count": len(choice_labels),
                    "choices": list(choice_labels),
                    "is_nvl": is_nvl_menu,
                    "mode": "hybrid" if vnf_player.allow_user_override else "external",
                })

            # Schedule post-render visibility check.
            # The periodic callback will scrape rendered ChoiceReturn
            # buttons and filter out choices not visible on screen.
            if vnf_player.filter_hidden_choices:
                _vnf_pending_vis_check[0] = {
                    "wrapper_req": _wrapper_req,
                    "req_id": req_id,
                    "choices": choices,
                    "value_map": dict(value_map),
                    "choice_labels": list(choice_labels),
                    "req_kwargs": dict(_req_kwargs),
                }

            # Save menu context for resync recovery.
            _vnf_current_menu_context[0] = {
                "req_kwargs": dict(_req_kwargs),
                "value_map": dict(value_map),
                "choices": list(choices),
                "req_id": req_id,
            }

            # Unified path: both hybrid and external-only go through
            # original_fn() so the interaction cycle runs (periodic
            # callbacks, screen scraping, pre-resolve steps, etc.).
            # External mode: no pacing delay, mouse blocked, observation
            # resolves immediately.
            _is_external = not vnf_player.allow_user_override
            _pacing_delay = _compute_pacing_delay(choices)

            if _pacing_delay > 0:
                def _deferred_activate():
                    _vnf_set_active_choice_request(req_id, value_map, choices=choices)
                _vnf_deferred_choice[0] = (_time_monotonic() + _pacing_delay, _deferred_activate)
            else:
                _vnf_set_active_choice_request(req_id, value_map, choices=choices,
                                               external_mode=_is_external)

            try:
                try:
                    rv = original_fn(items, interact=interact, **kwargs)
                except TypeError:
                    rv = original_fn(items, **kwargs)
            except _CONTROL_EXCEPTIONS as _ce:
                _vnf_deferred_choice[0] = None
                _vnf_pending_vis_check[0] = None
                _vnf_current_menu_context[0] = None
                _vnf_clear_active_request()
                raise _ce
            finally:
                _vnf_deferred_choice[0] = None
                _vnf_pending_vis_check[0] = None
                _vnf_current_menu_context[0] = None
                _vnf_client.cancel_request_retry(_wrapper_req[0])
                _vnf_clear_active_request()

            # Figure out who resolved it.
            _was_auto_resolved = _vnf_autoskip.resolved_flag
            _vnf_autoskip.resolved_flag = False
            _was_shim_resolved = _vnf_shim_resolved_flag[0]
            _vnf_shim_resolved_flag[0] = False
            if _is_external:
                resolved_by = "external"
            elif _was_auto_resolved:
                resolved_by = "auto_skip"
            elif _was_shim_resolved:
                resolved_by = "shim"
            else:
                resolved_by = "user"

            chosen_label = _vnf_text(rv)
            for _ri, _rv in value_map.items():
                if _rv == rv:
                    for c in choices:
                        if c.get("index") == _ri:
                            chosen_label = c["label"]
                            break
                    break

            # Bridge auto-clears pending when narration events arrive.
            _vnf_log("Choice resolved by {}: {}".format(resolved_by, chosen_label))
            _vnf_client.push_event_sync(dict(
                type="choice_resolved",
                request_id=_wrapper_req[0],
                label=chosen_label,
                resolved_by=resolved_by,
                timestamp=_time.time(),
            ))

            # Push a visible event when the user clicks a choice
            # in the game window, so the LLM/client can see it.
            if resolved_by == "user":
                _vnf_client.push_event(dict(
                    type="user_choice",
                    label=chosen_label,
                    req_id=_wrapper_req[0],
                    timestamp=_time.time(),
                ))

            if _vnf_menu_hooks:
                _vnf_fire_menu_hooks("exit", {
                    "req_id": _wrapper_req[0],
                    "resolved_by": resolved_by,
                    "chosen_label": chosen_label,
                    "is_nvl": is_nvl_menu,
                })

            if vnf_player.post_action_delay > 0:
                _time.sleep(vnf_player.post_action_delay)

            return rv

        return _wrapper

    # Create the ADV menu wrapper bound to _vnf_original_display_menu.
    _vnf_display_menu_wrapper = _make_vnf_menu_wrapper(_vnf_original_display_menu, is_nvl_menu=False)

    # Patch display_menu everywhere it might be referenced.
    renpy.exports.display_menu = _vnf_display_menu_wrapper
    renpy.display_menu = _vnf_display_menu_wrapper

    # Safely patch renpy.store.menu
    # If the game mapped menu to nvl_menu or a custom function, we must
    # wrap THAT function instead of blindly forcing the ADV display_menu.
    _vnf_store_menu_orig = getattr(renpy.store, "menu", None)
    if _vnf_store_menu_orig is not None:
        if _vnf_store_menu_orig == _vnf_original_nvl_menu:
            renpy.store.menu = _make_vnf_menu_wrapper(_vnf_original_nvl_menu, is_nvl_menu=True)
        elif _vnf_store_menu_orig == _vnf_original_display_menu:
            renpy.store.menu = _vnf_display_menu_wrapper
        else:
            # Custom function mapping
            renpy.store.menu = _make_vnf_menu_wrapper(_vnf_store_menu_orig)
    else:
        renpy.store.menu = _vnf_display_menu_wrapper

    # Also patch nvl_menu for NVL mode games (Ren'Py 7.5.2+)
    # CRITICAL: The NVL wrapper must be bound to _vnf_original_nvl_menu,
    # NOT _vnf_original_display_menu.  Otherwise NVL menus would render
    # using the ADV choice screen instead of appearing inside the NVL
    # window, breaking the visual flow and potentially the game logic.
    if not _is_renpy6 and _vnf_original_nvl_menu is not None:
        _vnf_nvl_menu_wrapper = _make_vnf_menu_wrapper(_vnf_original_nvl_menu, is_nvl_menu=True)

        renpy.exports.nvl_menu = _vnf_nvl_menu_wrapper
        renpy.nvl_menu = _vnf_nvl_menu_wrapper
        renpy.store.nvl_menu = _vnf_nvl_menu_wrapper

    # -------------------------------------------------------------------------
    # 3b. NVL Event Hooks -- capture nvl clear / nvl show / nvl hide
    # -------------------------------------------------------------------------
    #
    # These NVL lifecycle events are important for external clients to
    # understand page breaks and mode transitions.  Without them, an LLM
    # reading the transcript has no idea when the NVL page was cleared or
    # when the game switched between NVL and ADV presentation.

    # Hook nvl_clear (called by "nvl clear" statement).
    _vnf_original_nvl_clear = getattr(renpy.store, "nvl_clear", None)
    if _vnf_original_nvl_clear is not None:
        def _vnf_nvl_clear_wrapper():
            if vnf_player.enabled:
                # Rollback restores the previous NVL page before the ordinary
                # interact observer runs. Baseline it before this boundary can
                # mistake restored rows for newly played output.
                _vnf_observe_rollback_resume()
                # Capture any unseen nvl_list entries BEFORE the clear
                # wipes them.  The periodic nvl_watch may not have fired
                # yet (hub re-entry clears within the same interaction).
                _vnf_flush_nvl_entries("pre-clear", True)
                _vnf_reset_nvl_capture_state()
                _vnf_client.push_event(dict(type="nvl_clear"))
                _vnf_log("nvl clear")
            return _vnf_original_nvl_clear()
        renpy.store.nvl_clear = _vnf_nvl_clear_wrapper

    # Hook nvl_show (called by "nvl show" statement).
    _vnf_original_nvl_show = getattr(renpy.store, "nvl_show", None)
    if _vnf_original_nvl_show is not None:
        def _vnf_nvl_show_wrapper(*args, **kwargs):
            if vnf_player.enabled:
                _vnf_client.push_event(dict(type="nvl_show"))
                _vnf_log("nvl show")
            return _vnf_original_nvl_show(*args, **kwargs)
        renpy.store.nvl_show = _vnf_nvl_show_wrapper

    # Hook nvl_hide (called by "nvl hide" statement).
    _vnf_original_nvl_hide = getattr(renpy.store, "nvl_hide", None)
    if _vnf_original_nvl_hide is not None:
        def _vnf_nvl_hide_wrapper(*args, **kwargs):
            if vnf_player.enabled:
                _vnf_observe_rollback_resume()
                _vnf_flush_nvl_entries("pre-hide", True)
                _vnf_nvl_callback_occurrences[:] = []
                _vnf_client.push_event(dict(type="nvl_hide"))
                _vnf_log("nvl hide")
            return _vnf_original_nvl_hide(*args, **kwargs)
        renpy.store.nvl_hide = _vnf_nvl_hide_wrapper

    # -------------------------------------------------------------------------
    # 4. Input Interception
    # -------------------------------------------------------------------------

    _vnf_original_input = _vnf_save_original("input", renpy.exports, "input")

    def _vnf_transform_input_prompt(prompt, default="", screen="input"):
        transform = getattr(vnf_player, "input_prompt_transform", None)
        if not transform:
            return prompt
        try:
            result = transform(prompt, default, screen)
        except TypeError:
            try:
                result = transform(prompt)
            except Exception as exc:
                _vnf_log("input_prompt_transform error: %s" % _vnf_text(
                    exc, "unknown prompt transform failure"))
                return prompt
        except Exception as exc:
            _vnf_log("input_prompt_transform error: %s" % _vnf_text(
                exc, "unknown prompt transform failure"))
            return prompt
        if result is None:
            return prompt
        return _vnf_stringify(result)

    def _vnf_input_wrapper(prompt, default="", allow=None, exclude="{}",
                           length=None, with_none=None, pixel_width=None,
                           screen="input", mask=None, copypaste=True,
                           multiline=False, **kwargs):
        """
        Intercepts renpy.input() calls.

        In hybrid mode: shows the input screen AND polls bridge.
        In external-only mode: blocks until the bridge provides text.
        """
        if not vnf_player.enabled:
            input_kwargs = dict(
                default=default, allow=allow, exclude=exclude,
                length=length, with_none=with_none, pixel_width=pixel_width,
                screen=screen, mask=mask, copypaste=copypaste,
                **kwargs
            )
            if not _is_legacy:
                input_kwargs["multiline"] = multiline
            return _vnf_original_input(prompt, **input_kwargs)

        # Clean the prompt for the bridge.
        clean_prompt = renpy.text.extras.filter_text_tags(
            _vnf_stringify(prompt) or "", allow=set())
        clean_prompt = _vnf_transform_input_prompt(clean_prompt, default, screen)

        if vnf_player.debug:
            _vnf_log("Sending input request: prompt='%s', default='%s'" % (clean_prompt, default))
        req_id = _vnf_client.push_request(
            "input_request",
            prompt=clean_prompt,
            default=default,
        )
        if vnf_player.debug:
            _vnf_log("Input request sent with ID: %s" % req_id)

        _vnf_log("Input request {}: '{}'".format(req_id, clean_prompt))

        # Unified path: both hybrid and external-only go through
        # original_input() so the interaction cycle runs.
        _vnf_set_active_input_request(req_id)

        try:
            try:
                input_kwargs = dict(
                    default=default, allow=allow, exclude=exclude,
                    length=length, with_none=with_none, pixel_width=pixel_width,
                    screen=screen, mask=mask, copypaste=copypaste,
                    **kwargs
                )
                if not _is_legacy:
                    input_kwargs["multiline"] = multiline
                rv = _vnf_original_input(prompt, **input_kwargs)
            except _CONTROL_EXCEPTIONS as _ce:
                raise _ce
        finally:
            _vnf_client.cancel_request_retry(req_id)
            _vnf_client.push_event_sync(dict(
                type="input_resolved",
                request_id=req_id,
                timestamp=_time.time(),
            ))
            _vnf_clear_active_request()

        # Coerce to string — some race conditions (input action processed
        # before Input widget exists) can cause original_input to return
        # the game's pre-input default value (e.g. False).  Game scripts
        # then crash on .strip()/.lower() etc.  Always return a string.
        # Use basestring for Py2/Py3 compat (Ren'Py provides it via future).
        try:
            _str_type = basestring  # Py2 + Py3-with-future
        except NameError:
            _str_type = str  # Py3 native fallback
        if not isinstance(rv, _str_type):
            _vnf_log("Input wrapper coerced non-string return %r to str" % (rv,))
            rv = _vnf_text(default) if default else ""

        _vnf_log("Input resolved: '{}'".format(rv))
        return rv

    renpy.input = _vnf_input_wrapper
    renpy.exports.input = _vnf_input_wrapper

    # -------------------------------------------------------------------------
    # 4b. Custom Screen Interception (call screen)
    # -------------------------------------------------------------------------



    # -------------------------------------------------------------------------
    # 5. Auto-Advance for Say Interactions
    # -------------------------------------------------------------------------
    #
    # Auto-advance uses periodic_callbacks to detect say interactions
    # and dismiss them after a delay.  This is safe because:
    #   - We only dismiss when main_menu is False (not at the main menu)
    #   - We only dismiss when there is no active choice/input request
    #   - We track the "last say what" to know a say is on screen
    # -------------------------------------------------------------------------

    _vnf_auto_advance_active = False
    _vnf_auto_advance_say_time = 0.0   # when the current say line appeared
    _vnf_auto_advance_last_what = None  # to detect new say lines
    _vnf_auto_advanced_flag = [False]   # set True when auto-advance dismisses a say

    # Auto-skip/resolve state → _vnf_autoskip (instance of _VNFAutoSkipState)
    # _vnf_shim_resolved_flag is separate — not part of auto-skip lifecycle.
    _vnf_shim_resolved_flag = [False]  # set True when shim command handler resolves a choice

    def _vnf_get_auto_resolve_snapshot():
        """Return current auto-resolve state as a dict (for diagnostic hooks)."""
        return {
            "pending": _vnf_autoskip.resolve_value is not None,
            "deadline": _vnf_autoskip.resolve_after,
            "highlighted": _vnf_autoskip.highlighted,
            "has_choices": _vnf_autoskip.resolve_choices is not None,
            "deferred_choice_pending": _vnf_deferred_choice[0] is not None,
            "pause_reasons": list(_vnf_autoskip.pause_reasons),
        }

    def _vnf_periodic_auto_resolve():
        """If an auto-resolve value is pending, end the current
        interaction after any pacing delay has elapsed so the menu
        is visible on screen before being dismissed.  Highlights the
        choice shortly before resolving for visual feedback."""
        if not vnf_player.enabled:
            return
        if _vnf_autoskip.resolve_value is not None:
            _deadline = _vnf_autoskip.resolve_after
            if _deadline > 0:
                _now = _time_monotonic()
                # Highlight the choice shortly before resolving.
                if not _vnf_autoskip.highlighted:
                    _hl_at = _deadline - vnf_player.choice_highlight_delay
                    if _now >= _hl_at:
                        _vnf_autoskip.highlighted = True
                        global _vnf_active_choices
                        _vnf_active_choices = _vnf_autoskip.resolve_choices
                        _vnf_highlight_choice(1)
                if _now < _deadline:
                    return  # wait for pacing deadline
            _val = _vnf_autoskip.resolve_value
            _vnf_autoskip.clear_resolve()
            # Reset observation state so the next menu's before_interact
            # correctly defocuses the default-focused first button.
            _vnf_request.focus_applied = False
            _vnf_request.scroll_correction = False
            # Unblock mouse events that _vnf_highlight_choice blocked.
            _vnf_unblock_mouse()
            # Park cursor at screen centre so it doesn't land on
            # hover-sensitive elements on the next screen.
            try:
                _vnf_move_mouse(
                    renpy.config.screen_width // 2,
                    renpy.config.screen_height // 2,
                )
            except Exception:
                pass
            global _vnf_last_shim_action_time
            _vnf_last_shim_action_time = _time.time()
            _vnf_autoskip.resolved_flag = True
            renpy.exports.end_interaction(_val)

    renpy.config.periodic_callbacks.append(_safe_periodic(_vnf_periodic_auto_resolve))

    # Deferred choice request: delays pushing the choice_request
    # to the bridge so the user viewer can read on-screen text
    # before the LLM can act.  The menu renders immediately via
    # original_fn; the periodic callback pushes the request once
    # the pacing deadline has elapsed.
    _vnf_deferred_choice = [None]  # None or (deadline, push_fn)

    def _vnf_periodic_deferred_push():
        """Push a deferred choice request once pacing has elapsed."""
        if not vnf_player.enabled:
            return
        _dc = _vnf_deferred_choice[0]
        if _dc is not None:
            _deadline, _push_fn = _dc
            if _time_monotonic() >= _deadline:
                _vnf_deferred_choice[0] = None
                _push_fn()

    renpy.config.periodic_callbacks.append(_safe_periodic(_vnf_periodic_deferred_push))

    # Raw AST menu items captured before condition evaluation.
    # Set by the Menu.execute hook; consumed by the menu augmenter
    # pipeline so mods can filter/recover toggle-dependent items.
    _vnf_raw_menu_items = [None]

    try:
        _vnf_orig_ast_menu_execute = _vnf_save_original("Menu.execute", renpy.ast.Menu, "execute")

        def _vnf_ast_menu_execute_hook(self):
            try:
                raw = []
                for i in range(len(self.items)):
                    item = self.items[i]
                    label = item[0]
                    condition = item[1]
                    has_block = (len(item) > 2
                                 and item[2] is not None
                                 and len(item[2]) > 0)
                    is_caption = (len(item) <= 2 or item[2] is None)
                    # Apply say_menu_text_filter so labels match
                    # what the wrapper receives.
                    if renpy.config.say_menu_text_filter:
                        label = renpy.config.say_menu_text_filter(label)
                    ri = {
                        "label": label,
                        "condition": condition,  # string or True
                        "ast_index": i,
                        "has_block": has_block,
                        "is_caption": is_caption,
                    }
                    # Capture item_arguments kwargs (may contain
                    # the real condition for (condition="...") syntax).
                    if (not is_caption
                            and self.item_arguments
                            and i < len(self.item_arguments)
                            and self.item_arguments[i] is not None):
                        try:
                            _ia_args, _ia_kwargs = (
                                self.item_arguments[i].evaluate())
                            if _ia_kwargs:
                                ri["item_kwargs"] = dict(_ia_kwargs)
                        except Exception:
                            pass
                    raw.append(ri)
                _vnf_raw_menu_items[0] = raw
            except Exception:
                _vnf_raw_menu_items[0] = None
            return _vnf_orig_ast_menu_execute(self)

        renpy.ast.Menu.execute = _vnf_ast_menu_execute_hook
    except Exception:
        pass

    # Post-render choice visibility check: after the menu renders,
    # cross-reference the pushed choices against actually-rendered
    # ChoiceReturn buttons.  If some choices are hidden by the screen
    # (e.g. class-gated), resolve the original request and push a
    # filtered replacement.
    _vnf_pending_vis_check = [None]

    def _vnf_periodic_vis_check():
        if not vnf_player.enabled:
            return
        pending = _vnf_pending_vis_check[0]
        if pending is None:
            return
        _vnf_pending_vis_check[0] = None  # one-shot

        try:
            # 1. Scrape rendered buttons (ChoiceReturn + all others).
            rendered_labels = set()
            all_scraped_btns = []
            showing = _vnf_get_showing_screens()
            for sname, scr in showing:
                btns = []
                try:
                    _vnf_collect_button_actions(scr, btns, sname)
                except Exception:
                    continue
                all_scraped_btns.extend(btns)
                for _vd, _va, _vl, _vs in btns:
                    _va_list = _va if _vnf_is_sequence(_va) else [_va]
                    for _va_item in _va_list:
                        if _va_item.__class__.__name__ == "ChoiceReturn":
                            rendered_labels.add(
                                _vnf_normalize_quotes(_vl).strip())
                            break

            if not rendered_labels:
                return  # can't verify (no ChoiceReturn buttons)

            # 2. Cross-reference choices against rendered buttons.
            original_choices = pending["choices"]
            hidden = []
            for c in original_choices:
                if c.get("caption") or c.get("disabled"):
                    continue
                _cn = _vnf_normalize_quotes(c["label"]).strip()
                _found = False
                for _rl in rendered_labels:
                    if _cn == _rl or _cn in _rl or _rl in _cn:
                        _found = True
                        break
                if not _found:
                    hidden.append(c["label"])

            if not hidden:
                return  # all visible, no filtering needed

            # 2b. Consult vis_check_filter hook before filtering.
            #     Mods can return False to skip (e.g. when a state
            #     toggle like a class action is on screen).
            if vnf_player.vis_check_filter is not None:
                try:
                    if not vnf_player.vis_check_filter(
                            pending, rendered_labels, hidden,
                            all_scraped_btns):
                        _vnf_log(
                            "Vis check: filter skipped by hook"
                            " (%d hidden choices kept)" % len(hidden))
                        return
                except _CONTROL_EXCEPTIONS:
                    raise
                except Exception:
                    if vnf_player.debug:
                        _vnf_log("Vis check filter hook error: "
                                 + _vnf_text(_tb_module.format_exc()))

            # 3. Build filtered data (re-indexed from 1).
            new_choices = []
            new_value_map = {}
            new_labels = []
            _ni = 1
            _ovm = pending["value_map"]
            for c in original_choices:
                if c.get("caption") or c.get("disabled"):
                    new_choices.append(c)
                    continue
                if c["label"] in hidden:
                    continue
                _cc = dict(c)
                _old_idx = c["index"]
                _cc["index"] = _ni
                new_choices.append(_cc)
                new_value_map[_ni] = _ovm.get(_old_idx)
                new_labels.append(c["label"])
                _ni += 1

            # 4. Push filtered replacement.
            # Don't call resolve_request (it's async and can race
            # with push_request, clearing the NEW request).
            # set_pending_request in the bridge replaces the old one.
            _old_rid = pending["req_id"]

            _new_kw = dict(pending["req_kwargs"])
            _new_kw["choices"] = new_labels
            _new_kw["full_items"] = [
                fi for fi in _new_kw.get("full_items", [])
                if fi["label"] not in hidden
            ]
            _vnf_client.cancel_request_retry(_old_rid)
            _new_rid = _vnf_client.push_request("choice_request", **_new_kw)
            # 5. Update wrapper's req_id reference.
            pending["wrapper_req"][0] = _new_rid
            _menu_ctx = _vnf_current_menu_context[0]
            if (_menu_ctx is not None
                    and _menu_ctx.get("req_id") == _old_rid):
                _menu_ctx["req_id"] = _new_rid
                _menu_ctx["req_kwargs"] = dict(_new_kw)
                _menu_ctx["value_map"] = dict(new_value_map)
                _menu_ctx["choices"] = list(new_choices)

            # 6. Update deferred activation or active request.
            _dc = _vnf_deferred_choice[0]
            if _dc is not None:
                _vc_deadline = _dc[0]
                def _vc_deferred():
                    _vnf_set_active_choice_request(
                        _new_rid, new_value_map, choices=new_choices)
                _vnf_deferred_choice[0] = (_vc_deadline, _vc_deferred)
            elif _vnf_request.request_id == _old_rid:
                # Re-arm through the canonical setter: mutating
                # request_id in place makes the old poll worker exit
                # (its loop guard) with nobody polling the new request
                # on 7.x/8.x — the agent's act would sit on the bridge
                # undelivered. The setter also starts a fresh worker.
                _vnf_set_active_choice_request(
                    _new_rid, new_value_map, choices=new_choices,
                    external_mode=_vnf_request.external_mode)

            _vnf_log("Vis check: filtered %d -> %d choices (hidden: %s)" % (
                len(_ovm), len(new_value_map), hidden))
            _vnf_client.push_event(dict(
                type="choices_filtered",
                old_req_id=_old_rid,
                new_req_id=_new_rid,
                hidden=hidden,
                visible=new_labels,
            ))
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception:
            if vnf_player.debug:
                _vnf_log("Vis check error: " + _vnf_text(_tb_module.format_exc()))

    renpy.config.periodic_callbacks.append(_safe_periodic(_vnf_periodic_vis_check))

    def _vnf_compute_afm_time():
        """Derive Ren'Py's afm_time from vnf_player.reading_cps so the
        native AFM rate matches the configured reading speed. Ren'Py's
        AFM delay formula is roughly `afm_time * (afm_bonus + nchars
        / afm_characters)` with `afm_characters = 250`, so the
        steady-state chars/sec works out to 250 / afm_time. Inverting:
        afm_time = 250 / reading_cps.

        Returns 1 as the floor (the fastest AFM setting). Earlier
        commits hardcoded afm_time=1 in hybrid mode which made native
        AFM ~15x faster than its sane default and defeated the
        "wait at reading speed" intent.
        """
        cps = getattr(vnf_player, "reading_cps", 40) or 40
        if cps <= 0:
            return 1
        return max(1, int(round(250.0 / cps)))

    def _vnf_enable_auto_advance():
        """Call this at runtime to turn on auto-advance for say lines."""
        if not vnf_player.enabled:
            return
        global _vnf_auto_advance_active
        _vnf_auto_advance_active = True
        vnf_player.auto_advance = True
        _vnf_afm_intentionally_off[0] = False
        # In AFM/audio mode, let Ren'Py handle say-line advancement.
        # afm_time is derived from reading_cps so the AFM rate tracks
        # the operator-chosen reading speed.
        if _vnf_effective_dialogue_advance_mode() == "afm":
            try:
                renpy.game.preferences.afm_enable = True   # type: ignore
                renpy.game.preferences.afm_time = _vnf_compute_afm_time()  # type: ignore
            except Exception:
                pass
        else:
            try:
                renpy.game.preferences.afm_enable = False  # type: ignore
            except Exception:
                pass
        _vnf_log("Auto-advance enabled (delay={}s, afm_time={})".format(
            vnf_player.auto_advance_delay, _vnf_compute_afm_time()))

    def _vnf_disable_auto_advance():
        """Call this at runtime to turn off auto-advance."""
        if not vnf_player.enabled:
            return
        global _vnf_auto_advance_active
        _vnf_auto_advance_active = False
        vnf_player.auto_advance = False
        _vnf_afm_intentionally_off[0] = True
        # Also disable native AFM.
        try:
            renpy.game.preferences.afm_enable = False  # type: ignore
        except Exception:
            pass
        _vnf_log("Auto-advance disabled")

    # --- Preference stashing (2026-08-18, live-reported fade loss) ---
    # Turbo and fast-forward mutate renpy.game.preferences, and Ren'Py
    # PERSISTS preferences: a session killed mid-turbo leaves
    # transitions=0 / text_cps=0 in the player's persistent data, and
    # every later USER session plays fade-less with instant text ("did I
    # lose the fade ins/outs due to some settings?" — yes). The in-memory
    # save/restore pairs cannot survive a kill, so the pre-mutation value
    # is ALSO stashed in persistent data and reconciled at init. First
    # writer wins per key (turbo and fast-forward compose); a clean
    # restore unstashes exactly the keys it wrote back.
    def _vnf_stash_pref(name, value):
        try:
            _p = renpy.game.persistent
            _stash = getattr(_p, "_vnf_stashed_prefs", None) or {}
            if name not in _stash:
                _stash = dict(_stash)
                _stash[name] = value
                _p._vnf_stashed_prefs = _stash
        except Exception:
            pass

    def _vnf_unstash_pref(name):
        try:
            _p = renpy.game.persistent
            _stash = getattr(_p, "_vnf_stashed_prefs", None)
            if _stash and name in _stash:
                _stash = dict(_stash)
                _stash.pop(name)
                _p._vnf_stashed_prefs = _stash or None
        except Exception:
            pass

    def _vnf_reconcile_stashed_prefs():
        """Init-time: a surviving stash means a previous session died with
        mutated preferences — put the player's values back. Runs whether or
        not the shim is enabled this session: the damage belongs to a PAST
        session and this one may be a user's.

        Recovery requires an actual stash. There is no safe value-based
        migration: transitions=0 and text_cps=0 are both legitimate player
        settings, so their combination is not proof that turbo wrote them."""
        try:
            _p = renpy.game.persistent
            _prefs = renpy.game.preferences
            _stash = getattr(_p, "_vnf_stashed_prefs", None)
            if _stash:
                for _name, _value in dict(_stash).items():
                    try:
                        setattr(_prefs, _name, _value)
                    except Exception:
                        pass
                _vnf_log("Restored {} preference(s) orphaned by a killed "
                         "session: {}".format(len(_stash), sorted(_stash)))
                _p._vnf_stashed_prefs = None
        except Exception:
            pass

    # Reconcile at define time (init 999): preferences and persistent are
    # both loaded here, and any of THIS session's turbo/fast-forward
    # applications happen later (profile-on-life), so the ordering is
    # right — orphaned mutations from a killed session are undone before
    # anything new is applied, for user and agent sessions alike.
    _vnf_reconcile_stashed_prefs()

    # --- Fast-forward state ---
    # Boxed (mutated in place), like _vnf_pending_command_box: Ren'Py
    # cleans the store on every full restart (ending -> main menu,
    # MainMenu), rebinding bare names to their init values.  These are
    # the in-session restore targets for preferences the profile
    # overwrote; a bare rebind reverted to None after a full restart and
    # turning the profile off then restored nothing (Sep 7 2026).
    _vnf_fast_forward_saved_box = [None]  # saved settings before fast-forward

    def _vnf_enable_fast_forward():
        """
        Enable fast-forward mode: instant text, zero delays, and pause()
        statements dismissed immediately (see _vnf_pause_wrapper).
        Saves previous settings so they can be restored.

        Scene transitions are NOT touched here despite the historical
        comment — that is turbo's job (vnf_player.turbo), and the two
        compose.
        """

        _vnf_fast_forward_saved_box[0] = {
            "auto_advance_delay": vnf_player.auto_advance_delay,
            "post_action_delay": vnf_player.post_action_delay,
            "text_cps": getattr(renpy.game.preferences, "text_cps", 0),
            "afm_time": getattr(renpy.game.preferences, "afm_time", 15),
            "afm_enable": getattr(renpy.game.preferences, "afm_enable", False),
            "allow_skipping": renpy.config.allow_skipping,
        }
        _vnf_stash_pref("text_cps", _vnf_fast_forward_saved_box[0]["text_cps"])
        _vnf_stash_pref("afm_time", _vnf_fast_forward_saved_box[0]["afm_time"])
        _vnf_stash_pref("afm_enable", _vnf_fast_forward_saved_box[0]["afm_enable"])

        # Zero all delays
        vnf_player.auto_advance_delay = 0
        vnf_player.post_action_delay = 0

        # Instant text display (0 = all at once)
        try:
            renpy.game.preferences.text_cps = 0
            renpy.game.preferences.afm_time = 1
        except Exception:
            pass

        # Enable auto-advance if not already on
        if not _vnf_auto_advance_active:
            _vnf_enable_auto_advance()

        vnf_player.fast_forward = True
        _vnf_log("Fast-forward enabled — zero delays, instant text")

    def _vnf_disable_fast_forward():
        """Restore settings saved before fast-forward was enabled."""

        if _vnf_fast_forward_saved_box[0] is not None:
            vnf_player.auto_advance_delay = _vnf_fast_forward_saved_box[0]["auto_advance_delay"]
            vnf_player.post_action_delay = _vnf_fast_forward_saved_box[0]["post_action_delay"]
            try:
                if (getattr(vnf_player, "turbo", False)
                        and _vnf_turbo_saved_box[0] is not None):
                    # Turbo is still the active owner. It may have saved the
                    # zero written by fast-forward, so hand it our older
                    # baseline and leave both the live value and durable
                    # stash alone. The last owner out performs the restore.
                    _vnf_turbo_saved_box[0]["text_cps"] = _vnf_fast_forward_saved_box[0]["text_cps"]
                    _vnf_log("Fast-forward: turbo active, handed off the "
                             "text_cps restore target")
                else:
                    renpy.game.preferences.text_cps = _vnf_fast_forward_saved_box[0]["text_cps"]
                    _vnf_unstash_pref("text_cps")
                renpy.game.preferences.afm_time = _vnf_fast_forward_saved_box[0]["afm_time"]
                renpy.game.preferences.afm_enable = _vnf_fast_forward_saved_box[0]["afm_enable"]
                _vnf_unstash_pref("afm_time")
                _vnf_unstash_pref("afm_enable")
            except Exception:
                pass
            renpy.config.allow_skipping = _vnf_fast_forward_saved_box[0]["allow_skipping"]
            _vnf_fast_forward_saved_box[0] = None

        vnf_player.fast_forward = False
        _vnf_log("Fast-forward disabled — delays restored")

    # --- Turbo mode ---
    # Presentation-only "go as fast as the engine allows" switch for
    # agent-driven validation runs.  Everything it touches is saved on the
    # way in and put back on the way out, so a run can flip turbo on for a
    # boring stretch and off again for a scene a user is watching.
    #
    # Relationship to fast_forward (the "external" profile): the two are
    # independent and compose in any order.  fast_forward owns PACING
    # (vnf_player delays, AFM, and delay=0 for every pause via
    # _vnf_pause_wrapper); turbo owns RENDERING (text reveal, transitions,
    # and a cap — not a zero — on scripted pause delays).  The one value
    # they share is preferences.text_cps: each saves what it found, so
    # nested on/off restores correctly, and _vnf_restore_turbo additionally
    # declines to write text_cps back while fast_forward is still active so
    # turning turbo off can't undo fast-forward's instant text.
    _VNF_TURBO_MAX_PAUSE = 0.2      # seconds; cap for scripted pause(delay)
    _vnf_turbo_saved_box = [None]   # pre-turbo preference values, or None (boxed, see fast-forward)
    _vnf_turbo_pause_patch = [None] # installed pause patch record, or None

    def _vnf_install_turbo_pause_patch():
        """Wrap renpy.pause so positive delays are capped.

        Idempotent: the record in _vnf_turbo_pause_patch is the "already
        installed" flag, so turning turbo on twice does NOT stack wrappers
        (which would otherwise make the original unrecoverable).  The
        wrapper also re-checks vnf_player.turbo on every call, so even a
        wrapper that somehow outlives a restore is inert.
        """
        if _vnf_turbo_pause_patch[0] is not None:
            return False
        _orig_exports = getattr(renpy.exports, "pause", None)
        _orig_top = getattr(renpy, "pause", None)
        if _orig_exports is None:
            return False

        def _vnf_turbo_pause_wrapper(delay=None, **kwargs):
            try:
                if (getattr(vnf_player, "turbo", False)
                        and delay is not None
                        and delay > _VNF_TURBO_MAX_PAUSE):
                    _vnf_log("Turbo: capping pause {} -> {}".format(
                        delay, _VNF_TURBO_MAX_PAUSE))
                    delay = _VNF_TURBO_MAX_PAUSE
            except Exception:
                pass
            return _orig_exports(delay=delay, **kwargs)

        _vnf_turbo_pause_wrapper._vnf_turbo = True
        _vnf_turbo_pause_patch[0] = {
            "wrapper": _vnf_turbo_pause_wrapper,
            "exports": _orig_exports,
            "top": _orig_top,
        }
        renpy.exports.pause = _vnf_turbo_pause_wrapper
        renpy.pause = _vnf_turbo_pause_wrapper
        return True

    def _vnf_remove_turbo_pause_patch():
        """Restore the pause function saved when the patch was installed.

        Only the names still pointing at OUR wrapper are restored: if some
        other layer patched on top of us, unwinding blindly would drop that
        layer.  In that case the wrapper stays in the chain, harmless
        because it no-ops while vnf_player.turbo is False.
        """
        _patch = _vnf_turbo_pause_patch[0]
        if _patch is None:
            return False
        if getattr(renpy.exports, "pause", None) is _patch["wrapper"]:
            renpy.exports.pause = _patch["exports"]
        if getattr(renpy, "pause", None) is _patch["wrapper"]:
            renpy.pause = _patch["top"]
        _vnf_turbo_pause_patch[0] = None
        return True

    def _vnf_apply_turbo():
        """Turn turbo ON, remembering what it overwrote."""
        if _vnf_turbo_saved_box[0] is not None:
            # Already on: keep the ORIGINAL saved values (re-saving now
            # would memorize turbo's own values as the restore target).
            _vnf_install_turbo_pause_patch()
            return
        _saved = {"prefs": False, "text_cps": None, "transitions": None}
        try:
            _prefs = renpy.game.preferences
            _saved["text_cps"] = getattr(_prefs, "text_cps", None)
            _saved["transitions"] = getattr(_prefs, "transitions", None)
            _saved["prefs"] = True
            if _saved["text_cps"] is not None:
                _vnf_stash_pref("text_cps", _saved["text_cps"])
            if _saved["transitions"] is not None:
                _vnf_stash_pref("transitions", _saved["transitions"])
            _prefs.text_cps = 0
            _prefs.transitions = 0
        except Exception as _turbo_e:
            _vnf_log("Turbo: preference apply failed: {}".format(
                _vnf_text(_turbo_e)))
        try:
            _vnf_install_turbo_pause_patch()
        except Exception as _turbo_e:
            _vnf_log("Turbo: pause patch failed: {}".format(
                _vnf_text(_turbo_e)))
        _vnf_turbo_saved_box[0] = _saved
        _vnf_log(
            "Turbo enabled — instant text, no transitions, "
            "pauses capped at {}s".format(_VNF_TURBO_MAX_PAUSE))

    def _vnf_restore_turbo():
        """Turn turbo OFF, putting back exactly what _vnf_apply_turbo saw."""
        _saved = _vnf_turbo_saved_box[0]
        _vnf_turbo_saved_box[0] = None
        try:
            _vnf_remove_turbo_pause_patch()
        except Exception as _turbo_e:
            _vnf_log("Turbo: pause unpatch failed: {}".format(
                _vnf_text(_turbo_e)))
        if _saved is None:
            return
        try:
            if _saved.get("prefs"):
                _prefs = renpy.game.preferences
                if _saved.get("text_cps") is not None:
                    if getattr(vnf_player, "fast_forward", False):
                        # fast_forward is still on and also wants instant
                        # text. If it started under turbo it saved turbo's
                        # zero, so hand it turbo's older baseline. The last
                        # owner out restores and clears the durable stash.
                        if _vnf_fast_forward_saved_box[0] is not None:
                            _vnf_fast_forward_saved_box[0]["text_cps"] = _saved["text_cps"]
                        _vnf_log("Turbo: fast_forward active, "
                                 "handed off the text_cps restore target")
                    else:
                        _prefs.text_cps = _saved["text_cps"]
                        _vnf_unstash_pref("text_cps")
                if _saved.get("transitions") is not None:
                    # fast_forward never touches transitions, so turbo owns
                    # this one outright.
                    _prefs.transitions = _saved["transitions"]
                    _vnf_unstash_pref("transitions")
        except Exception as _turbo_e:
            _vnf_log("Turbo: preference restore failed: {}".format(
                _vnf_text(_turbo_e)))
        _vnf_log("Turbo disabled — presentation settings restored")

    def _vnf_sync_turbo():
        """Reconcile the applied turbo state with vnf_player.turbo.

        `set turbo` applies the change directly (_vnf_set_config_key), but
        turbo can also arrive without going through that path — a launch
        profile, a mod at init -989, or the console.  This runs from an
        interact callback so those land on the first interaction.
        """
        try:
            _want = bool(getattr(vnf_player, "turbo", False))
        except Exception:
            return
        if _want and _vnf_turbo_saved_box[0] is None:
            _vnf_apply_turbo()
        elif not _want and _vnf_turbo_saved_box[0] is not None:
            _vnf_restore_turbo()
        elif _want:
            # Re-assert: loading a save or a game's own options screen can
            # put the preferences back while turbo is still on.  The saved
            # restore target is deliberately NOT touched here.
            try:
                _prefs = renpy.game.preferences
                if getattr(_prefs, "text_cps", 0):
                    _prefs.text_cps = 0
                if getattr(_prefs, "transitions", 0):
                    _prefs.transitions = 0
            except Exception:
                pass

    def _vnf_periodic_auto_advance():
        """
        Called ~20 times/sec from config.periodic_callbacks.
        If auto-advance is on and a say line is showing, dismiss it
        after the configured delay.

        In external mode (allow_user_override=False), auto-advance
        fires unconditionally with zero delay and suppresses AFM so
        dialogue advances ASAP without waiting for voice audio.
        """
        global _vnf_auto_advance_say_time, _vnf_auto_advance_last_what

        if not vnf_player.enabled:
            return

        _dialogue_mode = _vnf_effective_dialogue_advance_mode()

        if not (vnf_player.auto_advance and _vnf_auto_advance_active):
            return

        # In hybrid mode, native AFM handles text advancement — skip.
        if _dialogue_mode == "afm":
            return

        # A scripted delay can be a custom screen's typewriter interval.
        # Let its own timer finish, independently of the previous say line.
        # Turbo still caps the delay in its pause wrapper.
        if (_dialogue_mode != "external"
                and getattr(renpy.store, "_vnf_in_timed_pause", False)):
            return

        # In external mode, suppress AFM so voice audio doesn't block.
        try:
            if renpy.game.preferences.afm_enable:
                # afm_enable persists too — stash so a killed session's
                # suppression is undone at the next init.
                _vnf_stash_pref("afm_enable", True)
                renpy.game.preferences.afm_enable = False
        except Exception:
            pass

        # Don't auto-advance at the main menu or game menus.
        try:
            if getattr(renpy.store, "main_menu", False):
                return
        except Exception:
            pass
        if _vnf_has_modal_overlay():
            return

        # Don't auto-advance if a choice/input request is pending.
        if _vnf_request.request_id is not None:
            return

        # Don't auto-advance if auto-skip is paused — a single-choice
        # menu is active but held so sidebar/overlay buttons can be
        # clicked.  Firing end_interaction(True) here would resolve the
        # menu with index 1 instead of the correct choice value.
        if _vnf_autoskip.pause_reasons:
            return

        # Don't auto-advance if a choice screen is showing — the menu
        # wrapper may not have set the request ID yet (pacing delay,
        # or between finally-clear and re-entry on game-script loops).
        try:
            if renpy.get_screen("choice") is not None:
                return
        except Exception:
            pass

        # Don't auto-advance while ANY wrapped menu interaction is live
        # (2026-08-18, Echoes wait-door 5/5 repro). The "choice" screen
        # check above misses NVL menus (they render via nvl/nvl_choice)
        # and any custom menu screen, and during the pacing window the
        # request id above is still None — so a menu that opens directly
        # from another menu's arm, with no say line in between to reset
        # the stale-say timer below, was dismissed instantly with
        # end_interaction(True): True == 1 picked the FIRST item, and
        # the resolution carried no shim flag, so it logged as a user
        # choice. The wrapper's context marker is set before the
        # interaction starts and cleared in its finally, so it covers
        # every menu shape for the whole window.
        if _vnf_current_menu_context[0] is not None:
            return

        # Check if there's a say line on screen.  We use _last_say_what
        # which Ren'Py sets for every say statement.  Ignore empty/None.
        try:
            current_what = ("__pause__" if getattr(renpy.store, "_vnf_in_pause", False)
                            else renpy.store._last_say_what)
        except Exception:
            current_what = None

        # Fallback: if we are in a splashscreen or explicit pause, allow auto-advancing
        # even if there is no "say" line text.
        if not current_what:
            if getattr(renpy.store, "_vnf_in_pause", False):
                current_what = "__pause__"
            # Defensive check for context label (handles different Ren'Py versions)
            elif (getattr(renpy.game.context(), "label_name", None) == "splashscreen" or
                  getattr(renpy.game.context(), "current", None) == "splashscreen"):
                current_what = "__splashscreen__"
            else:
                return

        now = time.time()

        # Detect new say line.
        if current_what != _vnf_auto_advance_last_what:
            _vnf_auto_advance_last_what = current_what
            _vnf_auto_advance_say_time = now
            return

        # Check if enough time has passed. External mode skips the
        # delay entirely for maximum speed. Hybrid scales by text
        # length using reading_cps so long narration gets reading
        # time, with a constant auto_advance_delay tail tacked on so
        # even after the viewer finishes reading there is a small
        # beat before the next line — keeps the cadence from feeling
        # twitchy and short lines from snapping past in 0.3s. Short
        # lines still float up to the auto_advance_delay floor when
        # the reading-time component is near zero.
        if _dialogue_mode == "external":
            _adv_delay = 0
        else:
            _text_len = len(current_what) if isinstance(current_what, str) else 0
            _cps = vnf_player.reading_cps or 0
            _read_time = (_text_len / float(_cps)) if _cps > 0 else 0
            _text_cps = getattr(vnf_player, "text_cps", 0) or 0
            _reveal_time = (_text_len / float(_text_cps)) if _text_cps > 0 else 0
            _adv_delay = max(
                _read_time + vnf_player.auto_advance_delay,
                _reveal_time + vnf_player.post_reveal_hold,
            )
        if now - _vnf_auto_advance_say_time < _adv_delay:
            return


        # A custom modal can appear after the periodic screen scrape. Give
        # its transforms one final chance to pause automation before a stale
        # say-line timer dismisses the new interaction with True == 1.
        if _vnf_refresh_transform_pause_reasons():
            return

        # Dismiss the say interaction.
        global _vnf_last_shim_action_time
        _vnf_last_shim_action_time = _time.time()
        _vnf_auto_advanced_flag[0] = True
        try:
            renpy.exports.end_interaction(True)
        except _EndInteraction:
            raise  # MUST propagate — this is how the interaction is resolved
        except _CONTROL_EXCEPTIONS:
            raise  # Re-raise all Ren'Py control-flow exceptions
        except Exception:
            pass

    # Do NOT enable auto-advance during init — it would dismiss the
    # splash screen and main menu.  The external client should send
    # "auto_advance_on" when ready.
    #
    # We CANNOT disable AFM here in init because Ren'Py loads persisted
    # preferences from disk AFTER all init blocks run.  A previous run
    # that enabled AFM would overwrite our setting.  Instead, we use
    # periodic_callbacks with a one-shot flag to clear AFM the first
    # time a periodic tick fires (which is safely inside an interaction,
    # after preferences are loaded, and does NOT trigger
    # restart_interaction or otherwise disturb the current interaction).

    _vnf_afm_cleared = [False]
    # Track whether AFM was intentionally disabled (e.g. by agent command).
    _vnf_afm_intentionally_off = [False]

    def _vnf_periodic_ensure_afm():
        """
        Periodic callback that manages AFM based on mode.

        Hybrid mode (allow_user_override=True):
            Uses Ren'Py's native AFM for text advancement.  On first tick
            after preferences load, enables AFM with a fast time.
            Periodically re-enables it if something turned it off
            (unless intentionally disabled via command).

        External mode (allow_user_override=False):
            Clears AFM on first tick — external mode uses our custom
            _vnf_periodic_auto_advance with zero delay instead.
        """
        if not vnf_player.enabled:
            return

        _manage_afm = bool(vnf_player.auto_advance and _vnf_auto_advance_active)
        _use_afm = _manage_afm and _vnf_effective_dialogue_advance_mode() == "afm"

        if not _vnf_afm_cleared[0]:
            # First tick — preferences are now loaded from disk.
            _vnf_afm_cleared[0] = True
            if _use_afm:
                try:
                    _afm = _vnf_compute_afm_time()
                    renpy.game.preferences.afm_enable = True   # type: ignore
                    renpy.game.preferences.afm_time = _afm     # type: ignore
                    _vnf_log("AFM enabled for hybrid mode (afm_time={})".format(_afm))
                except Exception:
                    pass
            elif _manage_afm:
                try:
                    renpy.game.preferences.afm_enable = False  # type: ignore
                    renpy.game.preferences.afm_time = 15       # type: ignore
                    renpy.config.auto_forward_time = None
                    _vnf_log("Cleared AFM for external mode")
                except Exception:
                    pass
            return

        # Periodic re-check: in hybrid mode, ensure AFM stays on.
        if _use_afm and not _vnf_afm_intentionally_off[0]:
            try:
                if not renpy.game.preferences.afm_enable:
                    renpy.game.preferences.afm_enable = True   # type: ignore
                    renpy.game.preferences.afm_time = _vnf_compute_afm_time()  # type: ignore
                    _vnf_log("Re-enabled AFM (was turned off)")
            except Exception:
                pass

    _register_periodic(_vnf_periodic_ensure_afm, 2.0)

    # If fast_forward is set in config at startup, apply it now.
    if vnf_player.fast_forward:
        _vnf_enable_fast_forward()

    # -------------------------------------------------------------------------
    # 5b. Register periodic callbacks
    # -------------------------------------------------------------------------

    # action_poll and auto_advance do their own time bookkeeping and
    # need every-tick wakeups for snappy action resolution — register
    # unthrottled (interval 0).
    _register_periodic(_vnf_periodic_action_poll, 0)
    _register_periodic(_vnf_execute_observation, 0.05)
    _register_periodic(_vnf_execute_button_observation, 0.05)

    # The command poller's screen timers are the primary post-render
    # observation path, but a screen being present does not mean its timers
    # run.  Ren'Py 7 drops every modal TIMEEVENT for a timer that sits under
    # a modal screen (Timer.event; 8.x does so only with
    # config.modal_blocks_timer), and the engine's own `_exception` screen is
    # modal at zorder 1090, above the poller's 999.  With the poller frozen
    # under it nothing scraped the error screen on 7.5.2: the agent saw
    # GAME ERROR with no Ignore/Rollback and act could not run
    # (live_smoke echoes_of_tomorrow_r7, Sep 7).  The poller's own scrape
    # tick stamps this; the periodic pump takes over once the stamp is stale.
    _vnf_poller_timer_tick = [0.0]
    _VNF_POLLER_TIMER_STALE_S = 0.6

    def _vnf_poller_scrape_tick():
        _vnf_poller_timer_tick[0] = _time.time()
        _vnf_scrape_visible_screens()

    def _vnf_poller_timers_live():
        """True while the poller screen is present and its timers fire."""
        try:
            if renpy.exports.get_screen("vnf_command_poller") is None:
                return False
        except Exception:
            return True
        return (_time.time() - _vnf_poller_timer_tick[0]
                < _VNF_POLLER_TIMER_STALE_S)

    def _vnf_missing_poller_observation_pump():
        """Capture/dispatch after render when the poller's timers are not running.

        Covers a context that suppresses the overlay (no poller screen) and a
        present poller whose timers a modal screen above it has silenced.
        """
        if (not vnf_player.enabled or _is_renpy6
                or not _vnf_focus_snapshot_is_current()):
            return
        if _vnf_poller_timers_live():
            return
        # act's preparation owns capture before dispatch, retaining its original
        # surface for validation. Do not overwrite that surface with a scrape
        # before the pending command has claimed it.
        if (_vnf_is_mapping(_vnf_pending_command_box[0])
                and _vnf_pending_command_box[0].get("name") == "act"):
            _vnf_execute_pending_command()
        else:
            _vnf_scrape_visible_screens()
        if _vnf_native_action_queue is not None:
            # We are in the periodic event-loop path, where Return can safely
            # end the interaction. The executor clears the queue before running.
            _vnf_execute_native_action_once()

    _register_periodic(_vnf_missing_poller_observation_pump, 0.2)

    def _vnf_error_screen_action_pump():
        """Run a queued native action while the exception screen is up.

        Ren'Py shows its exception screen with suppress_overlay=True
        (renpy/display/error.py), so the command-poller overlay timer that
        normally runs queued native actions never fires there: an agent's
        Ignore/Rollback click was queued, reported as applied, and sat
        unexecuted while the game stayed on the error screen (fleet R68,
        ten agents; Sep 5 live repro).  Periodic callbacks run inside the
        interaction's event loop, where EndInteraction is caught, so the
        action's Return ends the error interaction as a real click would.
        (Interact callbacks run before that loop; raising there is
        swallowed -- the first attempt.)
        """
        if _vnf_error_screen_visible[0] and _vnf_native_action_queue is not None:
            _vnf_log("error screen: running queued native action from periodic pump")
            _vnf_execute_native_action_once()

    _register_periodic(_vnf_error_screen_action_pump, 0.05)
    _register_periodic(_vnf_execute_pre_resolve_steps, 0.05)
    _register_periodic(_vnf_periodic_auto_advance, 0)

    if not _is_renpy6:
        # Background command poll thread wakeup (not needed on 6.x —
        # periodic polling handles commands inline).
        _register_periodic(_vnf_periodic_check_pending_command, 0.1)

    # DEBUG: tooltip state logger — REMOVE after debugging.
    # Also patch change_focus to log calls.
    # -------------------------------------------------------------------------
    # 5c. Inventory/Stats Periodic Update
    # -------------------------------------------------------------------------

    # Track last known inventory/stats to avoid spamming updates
    _vnf_last_inventory = None
    _vnf_last_stats = None


    def _vnf_stats_delta(previous, current):
        """Return changed values and explicit removals between stat maps."""
        changed = {}
        removed = []
        if previous is not None:
            for k, v in current.items():
                if k not in previous or previous.get(k) != v:
                    changed[k] = v
            for k in previous:
                if k not in current:
                    changed[k] = None
                    removed.append(k)
        else:
            changed = dict(current)
        return changed, removed


    def _vnf_inventory_delta(previous, current):
        """Return changed and removed occurrences while preserving duplicates."""
        if previous is None:
            return list(current), []
        unmatched = list(previous)
        changed = []
        for item in current:
            try:
                index = unmatched.index(item)
            except ValueError:
                changed.append(item)
            else:
                del unmatched[index]
        return changed, unmatched


    def _vnf_publish_inventory_stats(inventory, stats):
        """Advance the shared state baseline and emit each delta once."""
        global _vnf_last_inventory, _vnf_last_stats
        global _vnf_consecutive_unchanged_scrapes
        global _vnf_last_visible_scrape_hash

        inventory_changed = inventory != _vnf_last_inventory
        stats_changed = stats != _vnf_last_stats

        if inventory_changed:
            changed, removed = _vnf_inventory_delta(
                _vnf_last_inventory, inventory)
            _vnf_last_inventory = inventory
            _vnf_client.push_event(dict(
                type="inventory_update",
                inventory=inventory,
                changed=changed,
                removed=removed,
            ))
            if vnf_player.debug:
                _vnf_log("Inventory updated: {}".format(
                    [i.get("name", "?") for i in inventory]))

        if stats_changed:
            _vnf_consecutive_unchanged_scrapes = 0
            _vnf_last_visible_scrape_hash = None
            changed, removed = _vnf_stats_delta(_vnf_last_stats, stats)
            _vnf_last_stats = stats
            _vnf_client.push_event(dict(
                type="stats_update",
                stats=stats,
                changed=changed,
                removed=removed,
                _ts=_time.time(),
            ))
            if vnf_player.debug:
                _vnf_log("Stats changed: {}".format(changed))


    def _vnf_periodic_inventory_update():
        """
        Push inventory/stats updates to bridge when they change.
        Runs from periodic_callbacks during interactions.
        """
        # Don't spam during active menu/input interactions
        if _vnf_request.request_id is not None:
            return

        try:
            inventory, stats = _vnf_get_inventory_stats()
            _vnf_publish_inventory_stats(inventory, stats)
        except Exception:
            if vnf_player.debug:
                _vnf_log("Error in periodic inventory update: " + _vnf_text(_tb_module.format_exc()))

    _register_periodic(_vnf_periodic_inventory_update, 2.0)  # Check every 2 seconds

    # -------------------------------------------------------------------------
    # 5b. Progress Node Monitoring
    # -------------------------------------------------------------------------
    # Periodically evaluate check-based progress nodes and emit
    # progress_change events when new nodes become True.  Label-based
    # nodes already emit via _vnf_on_label; this covers check lambdas.

    _vnf_progress_emitted = set()

    def _vnf_periodic_progress_check():
        if not _vnf_progress_graph:
            return
        for name, node in _vnf_progress_graph.items():
            check_fn = node.get("check")
            if not check_fn:
                continue
            try:
                reached = bool(check_fn())
            except Exception:
                continue
            if not reached:
                # The set models the current true edge, not process-lifetime
                # history. A new game or pre-ending load rearms the node.
                _vnf_progress_emitted.discard(name)
                continue
            if name in _vnf_progress_emitted:
                continue
            _vnf_progress_emitted.add(name)
            _ev = {
                "type": "progress_change",
                "from": _vnf_current_progress_node,
                "to": name,
                "label": node.get("label", name),
                "terminal": node.get("terminal", False),
                "game_terminal": node.get("game_terminal", False),
                "timestamp": _time.time(),
            }
            _vnf_client.push_event(_ev)
            _vnf_log("Progress: {} reached".format(name))

    _register_periodic(_vnf_periodic_progress_check, 5.0)

    # -------------------------------------------------------------------------
    # 6. Screenshot Capture
    # -------------------------------------------------------------------------

    # Throttle state for screenshot pushes (shared across ALL callers so
    # the interaction/scene-change/6.x-tick paths are gated uniformly).
    #   [0] = timestamp of the last PUSH (or dedup-skip that went quiet)
    _vnf_screenshot_last_push = [0.0]
    #   [0] = md5 hexdigest of the last pushed frame's bytes
    _vnf_screenshot_last_hash = [None]
    _vnf_screenshot_capture_id = [None]

    def _vnf_capture_screenshot(size=None, hide_gui=False, force=False, capture_id=None):
        """Capture a screenshot and push it to the bridge.

        `hide_gui` is accepted for API compatibility but is currently a
        best-effort feature — Ren'Py does not expose a clean public API
        to toggle layer visibility outside of an interaction, so we fall
        back to a normal (with-GUI) screenshot when the hide fails.

        Two throttle gates keep the bridge from being saturated by the
        ~20 captures/sec that auto-advance + restart_interaction churn can
        trigger.  Both are bypassed when *force* is True (the manual
        `screenshot` command):
          1. Min-interval: skip if the last push was < screenshot_min_interval
             seconds ago.
          2. Content dedup: skip if the captured bytes match the last pushed
             frame (but still refresh the timestamp so a static screen goes
             quiet instead of re-capturing every interval).
        """
        if not vnf_player.enabled:
            return
        if not force and not vnf_player.screenshot_enabled:
            return

        # Gate 1 -- min-interval.  Applied before the (expensive) capture.
        if not force:
            try:
                _min_interval = float(getattr(
                    vnf_player, "screenshot_min_interval", 1.0))
            except (TypeError, ValueError):
                _min_interval = 1.0
            if _min_interval > 0:
                if _time.time() - _vnf_screenshot_last_push[0] < _min_interval:
                    return

        try:
            if size is None:
                size = vnf_player.screenshot_size

            if hide_gui:
                # NOTE: Ren'Py does not provide a public API to toggle
                # layer visibility or force a re-render outside of an
                # interaction.  screenshot_to_bytes() captures the
                # back-buffer from the last completed frame, so even if
                # we hid screens the old frame (with GUI) would still be
                # captured.  We therefore take a normal screenshot and
                # log a warning so callers know the limitation.
                if vnf_player.debug:
                    print("[LLM Player] hide_gui screenshot requested but "
                          "not supported — capturing with GUI visible.")

            if hasattr(renpy.exports, "screenshot_to_bytes"):
                # Before the first frame is drawn, screenshot_to_bytes reads
                # interface.surftree, which does not exist yet (seen on
                # Linux at boot): no frame, no capture, no traceback.
                _iface = getattr(getattr(renpy, "game", None), "interface", None)
                if _iface is not None and getattr(_iface, "surftree", None) is None:
                    return
                raw_bytes = renpy.exports.screenshot_to_bytes(size)
            else:
                # Ren'Py 6.x fallback: save to temp file, read back.
                import tempfile as _ss_tmp
                _ss_fd, _ss_path = _ss_tmp.mkstemp(suffix=".png")
                _os.close(_ss_fd)
                try:
                    renpy.exports.screenshot(_ss_path)
                    with open(_ss_path, "rb") as _ss_f:
                        raw_bytes = _ss_f.read()
                finally:
                    try:
                        _os.unlink(_ss_path)
                    except Exception:
                        pass

            if raw_bytes:
                # Gate 2 -- content dedup.  Skip an identical frame but keep
                # the timestamp fresh so a static screen stops re-capturing.
                _digest = None
                try:
                    _digest = _hashlib.md5(raw_bytes).hexdigest()
                except Exception:
                    _digest = None
                if (not force and _digest is not None
                        and _digest == _vnf_screenshot_last_hash[0]):
                    _vnf_screenshot_last_push[0] = _time.time()
                    return
                b64 = base64.b64encode(raw_bytes).decode("ascii")
                if capture_id is not None:
                    _vnf_screenshot_capture_id[0] = capture_id
                # Newer automatic frames may replace this frame in the dedicated
                # screenshot lane, but still satisfy the explicit capture request.
                _vnf_client.push_event(dict(type="screenshot", image=b64,
                    capture_id=_vnf_screenshot_capture_id[0]))
                _vnf_screenshot_last_push[0] = _time.time()
                if _digest is not None:
                    _vnf_screenshot_last_hash[0] = _digest
                return True
        except Exception:
            if vnf_player.debug:
                _tb_module.print_exc()

    def _vnf_screenshot_interact_callback():
        if (
            vnf_player.enabled
            and vnf_player.screenshot_enabled
            and vnf_player.screenshot_on in ("interaction", "both")
        ):
            _vnf_capture_screenshot()

    if not _is_renpy6:
        if _vnf_screenshot_interact_callback not in renpy.config.interact_callbacks:
            renpy.config.interact_callbacks.append(_vnf_screenshot_interact_callback)

    # -------------------------------------------------------------------------
    # 7. Context Detection -- main menu / game menu / in-game
    # -------------------------------------------------------------------------


    def _vnf_story_advance_block_reason():
        """
        Return a human-readable reason if the raw dialogue advance command
        should not synthesize Return(True).

        This is deliberately conservative: `advance` is for plain story
        continuation only. Choices, input widgets, menus, and overlays must be
        handled by their explicit commands so the shim does not accidentally
        pick a menu value.
        """
        if not vnf_player.enabled:
            return "VNFlight is disabled."

        try:
            if getattr(renpy.store, "main_menu", False):
                return "Cannot advance while the main menu is active."
        except Exception:
            pass

        try:
            for _screen in ("game_menu", "save", "load", "preferences"):
                if renpy.exports.get_screen(_screen) is not None:
                    return "Cannot advance while a game menu is active."
        except Exception:
            pass
        if _vnf_has_modal_overlay():
            return "Cannot advance while a game menu is active."

        if _vnf_request.request_id is not None:
            if _vnf_request.is_input:
                return "An input request is active; use input_text()."
            return "A choice request is active; use act()."

        # The menu wrapper installs this context before its request id or
        # choice screen necessarily becomes visible. Return(True) is unsafe
        # in that window: bool is an int in Python, so Ren'Py can interpret it
        # as AST menu item 1 and enter even an unavailable branch.
        if _vnf_current_menu_context[0] is not None:
            return "A choice/menu interaction is active; use act()."

        if _vnf_autoskip.pause_reasons:
            return "A held choice/menu request is active; use act()."

        try:
            if renpy.exports.get_screen("choice") is not None:
                return "A choice screen is active; use act()."
        except Exception:
            pass

        try:
            if (renpy.exports.get_screen("input") is not None
                    or renpy.exports.get_screen("text_input") is not None):
                return "An input screen is active; use input_text()."
        except Exception:
            pass

        if _vnf_active_call_screen_name:
            return "A call_screen interaction is active; use act() or back()."

        try:
            for _tag, _scr in _vnf_get_showing_screens():
                if (_tag in _vnf_overlay_screens
                        and _tag not in _vnf_passive_overlay_screens):
                    return "An overlay screen is active; use act() or back()."
        except Exception:
            pass

        try:
            _current_what = renpy.store._last_say_what
        except Exception:
            _current_what = None
        if not _current_what:
            _in_pause = bool(getattr(renpy.store, "_vnf_in_pause", False))
            try:
                _ctx = renpy.game.context()
                _in_splash = (
                    getattr(_ctx, "label_name", None) == "splashscreen"
                    or getattr(_ctx, "current", None) == "splashscreen")
            except Exception:
                _in_splash = False
            if not (_in_pause or _in_splash):
                return "No dialogue or pause is ready to advance."

        return None



    def _vnf_detect_context():
        """
        Detect the current game context and push a context event
        to the bridge when it changes.
        """
        global _vnf_last_context, _vnf_last_context_time, _vnf_gameplay_seen

        if not vnf_player.enabled:
            return

        # Don't spam -- limit to once per 0.5s (but always run the
        # first detection so _vnf_last_context gets seeded).
        now = _time.time()
        if _vnf_last_context is not None and now - _vnf_last_context_time < 0.5:
            return
        _vnf_last_context_time = now

        context = "unknown"
        context_info = {}

        try:
            # Check if the main menu is showing.
            # Require both the screen AND the store flag — some games
            # keep the main_menu screen in the display list during gameplay.
            main_menu_screen = renpy.exports.get_screen("main_menu")
            _at_main_menu = getattr(renpy.store, "main_menu", False)
            if main_menu_screen is not None and _at_main_menu:
                context = "main_menu"
                context_info["available_commands"] = ["start"]
                # Check if save slots exist for loading
                try:
                    slots = renpy.loadsave.list_slots()
                    if slots:
                        context_info["available_commands"].append("load")
                        context_info["save_slots"] = sorted(slots)[:20]  # Limit to 20
                except Exception:
                    pass
                context_info["available_commands"].extend(["quit"])
            else:
                # Check if we're in a game menu context
                game_menu_screen = renpy.exports.get_screen("game_menu")
                save_screen = renpy.exports.get_screen("save")
                load_screen = renpy.exports.get_screen("load")
                prefs_screen = renpy.exports.get_screen("preferences")
                generic_menu = _vnf_is_generic_game_menu_showing()

                if (game_menu_screen or save_screen or load_screen
                        or prefs_screen or generic_menu):
                    context = "game_menu"
                    current_screen = "game_menu"
                    if save_screen:
                        current_screen = "save"
                    elif load_screen:
                        current_screen = "load"
                    elif prefs_screen:
                        current_screen = "preferences"
                    elif generic_menu:
                        current_screen = "menu"
                    context_info["current_screen"] = current_screen
                    context_info["available_commands"] = ["return", "save", "load", "quit"]
                    try:
                        if renpy.exports.can_rollback():
                            context_info["available_commands"].append("rollback")
                    except Exception:
                        pass
                else:
                    context = "in_game"
                    context_info["available_commands"] = ["save", "load", "quit"]
                    try:
                        if renpy.exports.can_rollback():
                            context_info["available_commands"].append("rollback")
                        context_info["can_rollback"] = renpy.exports.can_rollback()
                    except Exception:
                        context_info["can_rollback"] = False

                    # Include save slot info (COMMENTED OUT FOR STP COMPATIBILITY)
                    # try:
                    #     slots = renpy.loadsave.list_slots()
                    #     if slots:
                    #         context_info["save_slots"] = sorted(slots)[:20]
                    # except Exception:
                    #     pass

                    # Include inventory and stats for in-game context
                    try:
                        inventory, stats = _vnf_get_inventory_stats()
                        if inventory:
                            context_info["inventory"] = inventory
                        if stats:
                            context_info["stats"] = stats
                    except Exception:
                        pass

        except Exception:
            if vnf_player.debug:
                _tb_module.print_exc()

        if context != _vnf_last_context:
            prev_context = _vnf_last_context
            _vnf_last_context = context
            ev = dict(type="context", context=context)
            ev.update(context_info)
            _vnf_client.push_event(ev)
            _vnf_log("Context changed: {} {}".format(context, context_info.get("available_commands", [])))

            if context == "in_game":
                _vnf_gameplay_seen = True

            # Auto-enable auto-advance when entering gameplay for the
            # first time (main_menu -> in_game).
            if (
                context == "in_game"
                and prev_context == "main_menu"
                and vnf_player.auto_advance_on_start
                and not _vnf_auto_advance_active
            ):
                _vnf_enable_auto_advance()
                _vnf_log("Auto-advance auto-enabled on game start (auto_advance_on_start=True)")

            # Detect return to main menu after gameplay. Some games pass
            # through an intermediate game-menu/custom-menu context before
            # the final main-menu screen, so track whether gameplay was ever
            # seen instead of trusting only the immediate previous context.
            if context == "main_menu" and prev_context != "main_menu" and _vnf_gameplay_seen:
                _vnf_log("Game returned to main menu after gameplay — signalling game_ended")
                _vnf_client.notify_game_ended(reason="return_to_menu")
                _vnf_gameplay_seen = False

    # -------------------------------------------------------------------------
    # 8. Command Processing -- start/save/load/rollback/quit
    # -------------------------------------------------------------------------

    def _vnf_cmd_start(cmd_name, cmd_args):
        label = cmd_args.get("label", "start")
        at_main_menu = False
        try:
            at_main_menu = bool(getattr(renpy.store, "main_menu", False))
        except Exception:
            pass
        if not at_main_menu:
            _vnf_log("Ignoring 'start' command — not at main menu (race condition?)")
            _vnf_client.push_event(dict(
                type="command_result", command="start", success=False,
                error="Not at main menu (already started?)",
            ))
            return
        _vnf_log("Starting game via Start('{}')".format(label))
        # Reset bridge state before the context jump — the jump
        # may interrupt periodic callbacks, so push synchronously
        # while the connection is still alive.
        _vnf_client.reset_bridge()
        _vnf_client.push_event_sync(dict(type="command_result", command="start", success=True, label=label))
        renpy.run(renpy.store.Start(label))

    def _vnf_cmd_save(cmd_name, cmd_args):
        # Match native FileSave admission before entering the serializer.
        # A main-menu context is not a playable checkpoint.
        if getattr(renpy.store, "main_menu", False):
            _vnf_client.push_event(dict(
                type="command_result", command=cmd_name, success=False,
                error="Cannot save at the main menu. Start or load a game first."))
            return
        slot = _vnf_text(cmd_args.get("slot", "1-1"))
        _is_debug_save = slot.startswith("_vnf_dbg_")
        if not _is_debug_save:
            if "-" not in slot and not slot.startswith("auto") and not slot.startswith("quick"):
                slot = "1-" + slot
        name = cmd_args.get("name", "LLM Player Save")
        _vnf_log("Saving to slot '{}'{}".format(slot, " (debug)" if _is_debug_save else ""))
        renpy.loadsave.save(slot, extra_info=name)
        # Move debug saves to a separate directory.
        if _is_debug_save:
            try:
                import shutil as _shutil
                _savedir = renpy.config.savedir
                _src = _os.path.join(_savedir, slot + "-LT1.save")
                if not _os.path.exists(_src):
                    # Ren'Py save file naming varies — try without suffix.
                    _src = _os.path.join(_savedir, slot + ".save")
                if _os.path.exists(_src):
                    _dbg_dir = _os.path.join(_savedir, "vnflight", "_debug")
                    # No exist_ok: Py2 (Ren'Py 6/7) doesn't have it.
                    if not _os.path.isdir(_dbg_dir):
                        _os.makedirs(_dbg_dir)
                    _shutil.move(_src, _os.path.join(_dbg_dir, _os.path.basename(_src)))
                    _vnf_log("Moved debug save to {}".format(_dbg_dir))
            except Exception as _e:
                _vnf_log("Failed to move debug save: {}".format(
                    _vnf_text(_e)))
        _vnf_client.push_event(dict(type="command_result", command=cmd_name, success=True, slot=slot))

    def _vnf_cmd_load(cmd_name, cmd_args):
        slot = _vnf_text(cmd_args.get("slot", ""))
        _is_debug_load = slot.startswith("_vnf_dbg_")
        if not slot:
            try:
                slot = renpy.loadsave.newest_slot() or "1-1"
            except Exception:
                slot = "1-1"
        elif not _is_debug_load:
            if "-" not in slot and not slot.startswith("auto") and not slot.startswith("quick"):
                slot = "1-" + slot
        # For debug loads, copy the save from the debug directory
        # to the main savedir so renpy.loadsave.load() can find it.
        if _is_debug_load:
            try:
                import shutil as _shutil
                _savedir = renpy.config.savedir
                _dbg_dir = _os.path.join(_savedir, "vnflight", "_debug")
                # Find the debug save file.
                _found = False
                for _suffix in ["-LT1.save", ".save"]:
                    _src = _os.path.join(_dbg_dir, slot + _suffix)
                    if _os.path.exists(_src):
                        _dst = _os.path.join(_savedir, slot + _suffix)
                        _shutil.copy2(_src, _dst)
                        _vnf_log("Copied debug save to main savedir for loading")
                        _found = True
                        break
                if not _found:
                    _vnf_log("Debug save '{}' not found in {}".format(slot, _dbg_dir))
            except Exception as _e:
                _vnf_log("Failed to prepare debug load: {}".format(
                    _vnf_text(_e)))
        _vnf_log("Loading from slot '{}'{}".format(slot, " (debug)" if _is_debug_load else ""))
        try:
            _available_slots = renpy.loadsave.list_slots()
        except Exception:
            _available_slots = None
        if _available_slots is not None and slot not in _available_slots:
            _vnf_client.push_event_sync(dict(
                type="command_result",
                command=cmd_name,
                success=False,
                slot=slot,
                error="Save slot not found",
            ))
            return
        _load_fn = (
            getattr(renpy.exports, "load", None)
            or getattr(renpy, "load", None)
            or getattr(getattr(renpy, "loadsave", None), "load", None)
        )
        if not callable(_load_fn):
            raise Exception("No Ren'Py load function is available")

        def _vnf_reset_after_successful_load():
            # A successful public Ren'Py load normally raises a control-flow
            # exception.  Reset only after that success signal: clearing the
            # request/bridge before the call made corrupt-save failures leave
            # the still-visible current scene disconnected from vnflight.
            _vnf_clear_active_request()
            _vnf_autoskip.reset()
            _vnf_deferred_choice[0] = None
            _vnf_pending_vis_check[0] = None
            _vnf_client.reset_bridge()

        try:
            _load_fn(slot)
        except BaseException as _load_e:
            if _vnf_is_load_success_exception(_load_e):
                # The load applied; the exception is how Ren'Py unwinds
                # to the restored state.  Confirm, then let it propagate.
                _vnf_log("Load applied from slot '{}' ({})".format(
                    slot, _vnf_text(type(_load_e).__name__)))
                _vnf_reset_after_successful_load()
                _vnf_client.push_event_sync(dict(type="command_result", command=cmd_name, success=True, slot=slot))
                raise
            if not isinstance(_load_e, Exception):
                _vnf_log("Load raised {} (propagating)".format(
                    _vnf_text(type(_load_e).__name__)))
                raise  # KeyboardInterrupt, SystemExit, other engine control flow
            _vnf_client.push_event_sync(dict(
                type="command_result",
                command=cmd_name,
                success=False,
                slot=slot,
                error=_vnf_text(_load_e),
            ))
        else:
            # Defensive compatibility for a load implementation that returns
            # normally instead of using Ren'Py's control-flow exception.
            _vnf_reset_after_successful_load()
            _vnf_client.push_event_sync(dict(
                type="command_result", command=cmd_name,
                success=True, slot=slot))

    def _vnf_cmd_rollback(cmd_name, cmd_args):
        _vnf_log("Rolling back")
        _vnf_client.push_event(dict(type="command_result", command="rollback", success=True))
        renpy.exports.rollback(force=True)

    def _vnf_cmd_advance(cmd_name, cmd_args):
        global _vnf_native_action_queue
        try:
            _block_reason = _vnf_story_advance_block_reason()
            if _block_reason:
                _vnf_client.push_event(dict(
                    type="command_result",
                    command=cmd_name,
                    success=False,
                    error=_block_reason,
                ))
            else:
                _vnf_native_action_queue = Return(True)
                _vnf_client.push_event(dict(
                    type="command_result",
                    command=cmd_name,
                    success=True,
                    note="Advance action queued.",
                ))
        except Exception as e:
            _vnf_client.push_event(dict(
                type="command_result",
                command=cmd_name,
                success=False,
                error="Failed: {}".format(_vnf_text(e)),
            ))

    def _vnf_rewind_refusal_reason():
        """
        Explain why renpy.exports.can_rollback() just returned False, so a
        rewind refusal can say why instead of one generic message.

        Mirrors the exact signal order can_rollback() itself checks for the
        running Ren'Py era: 6.x/7.x (renpy/exports.py) only gate on
        config.rollback_enabled and the rollback log; 8.x
        (renpy/exports/rollbackexports.py) additionally gates on
        store._rollback and the current context's rollback flag before
        ever consulting the log. Checking a signal the running build
        doesn't actually consult would misdiagnose a coincidental False
        on that signal as the cause, so _is_legacy narrows which extra
        checks apply.
        """
        if not renpy.config.rollback_enabled:
            return (
                "rollback_disabled_by_game",
                "Rollback is disabled by this game (config.rollback_enabled "
                "is False) -- rewind is never available in this "
                "playthrough. Use save/load to revisit an earlier point "
                "instead.",
            )

        if not _is_legacy:
            if not bool(getattr(renpy.store, "_rollback", True)):
                return (
                    "menu_blocks_rollback",
                    "Rollback is blocked by the current menu/screen "
                    "interaction -- try rewind again once it resolves.",
                )
            try:
                _ctx_rollback = bool(getattr(
                    renpy.game.context(), "rollback", True))
            except Exception:
                _ctx_rollback = True
            if not _ctx_rollback:
                return (
                    "context_blocks_rollback",
                    "Rollback is blocked in the current context (a "
                    "called screen or nested interaction) -- return to "
                    "normal story flow and try rewind again.",
                )

        return (
            "no_checkpoint_yet",
            "No rollback checkpoint is available yet at this point in "
            "the story.",
        )

    def _vnf_cmd_rewind(cmd_name, cmd_args):
        global _vnf_native_action_queue
        try:
            if not renpy.exports.can_rollback():
                _reason, _detail = _vnf_rewind_refusal_reason()
                _vnf_client.push_event(dict(
                    type="command_result",
                    command=cmd_name,
                    success=False,
                    error=_detail,
                    reason=_reason,
                ))
            else:
                _vnf_native_action_queue = Rollback()
                _vnf_client.push_event(dict(
                    type="command_result",
                    command=cmd_name,
                    success=True,
                    note="Rollback action queued.",
                ))
        except Exception as e:
            _vnf_client.push_event(dict(
                type="command_result",
                command=cmd_name,
                success=False,
                error="Failed: {}".format(_vnf_text(e)),
            ))

    def _vnf_cmd_replay(cmd_name, cmd_args):
        global _vnf_native_action_queue
        try:
            if renpy.exports.roll_forward_info() is None:
                _vnf_client.push_event(dict(
                    type="command_result",
                    command=cmd_name,
                    success=False,
                    error="No roll-forward history is available.",
                ))
            else:
                _vnf_native_action_queue = RollForward()
                _vnf_client.push_event(dict(
                    type="command_result",
                    command=cmd_name,
                    success=True,
                    note="Roll-forward action queued.",
                ))
        except Exception as e:
            _vnf_client.push_event(dict(
                type="command_result",
                command=cmd_name,
                success=False,
                error="Failed: {}".format(_vnf_text(e)),
            ))

    def _vnf_cmd_quit(cmd_name, cmd_args):
        _vnf_log("Quitting game")
        _vnf_client.push_event_sync(dict(type="command_result", command="quit", success=True))
        renpy.quit()

    def _vnf_cmd_skip_toggle(cmd_name, cmd_args):
        current = renpy.config.skipping
        if current:
            renpy.config.skipping = None
        else:
            renpy.config.skipping = "fast"
        _vnf_client.push_event(dict(
            type="command_result", command="skip_toggle",
            success=True, skipping=renpy.config.skipping is not None,
            causal_boundary=renpy.config.skipping is not None,
        ))

    def _vnf_cmd_auto_advance_on(cmd_name, cmd_args):
        _vnf_enable_auto_advance()
        _vnf_client.push_event(dict(type="command_result", command="auto_advance_on", success=True))

    def _vnf_cmd_auto_advance_off(cmd_name, cmd_args):
        _vnf_disable_auto_advance()
        _vnf_client.push_event(dict(type="command_result", command="auto_advance_off", success=True))

    def _vnf_cmd_fast_forward_on(cmd_name, cmd_args):
        _vnf_enable_fast_forward()
        _vnf_client.push_event(dict(type="command_result", command="fast_forward_on", success=True))

    def _vnf_cmd_fast_forward_off(cmd_name, cmd_args):
        _vnf_disable_fast_forward()
        _vnf_client.push_event(dict(type="command_result", command="fast_forward_off", success=True))

    def _vnf_act_candidate_desc(itr):
        """One human-readable candidate for an ambiguous-match refusal."""
        _label = _vnf_text(itr.get("display_label", "") or "")
        if len(_label) > 60:
            _label = _label[:57].rstrip() + "..."
        if itr.get("source") == "choice":
            return "choice {!r}".format(_label)
        return "button {}".format(_label)

    def _vnf_resolve_act_interaction(cmd_args):
        """Resolve an act() target to one interaction, or flag it ambiguous.

        Mirrors the handler-side precedence (vnflight/format.py's
        ``_resolve_label_interaction``) so the two layers never disagree:
        an id is exact and unambiguous by construction -- ids are internal
        identifiers, never player-typed labels, so they cannot collide
        across categories.  Failing that, an exact normalized label/alias
        match wins outright when it names interactions in exactly one
        CATEGORY -- a story choice vs. any other screen control -- even
        when a short label is also a SUBSTRING of an unrelated, much
        longer story choice: substring containment is fuzzy, not exact,
        so it never competes with a real exact hit.  A tie of exact hits
        ACROSS categories refuses instead of guessing which was meant.

        Only once nothing matches exactly does a fuzzy pass run (prefix
        first, substring only when no prefix candidate exists at all),
        held to a stricter rule than the exact tier: it must land on
        exactly one interaction, in any category -- two fuzzy hits in the
        same category are just as ambiguous as one in each.  Fleet R66:
        ``act(target="KIT")`` fuzzy-matched the unrelated story choice
        "The signal analysis toolkit..." because the old substring tier
        accepted any lone match with no regard for what else was on the
        surface; a KIT button on the same surface now wins outright via
        the exact tier before fuzzy ever runs.

        Returns ``(matched, ambiguous)``.  Exactly one is truthy when the
        target reached anything at all; both are falsy when it reached
        nothing.
        """
        _candidates = []
        if cmd_args.get("id"):
            _candidates.append(cmd_args["id"])
        if cmd_args.get("label") and cmd_args.get("label") not in _candidates:
            _candidates.append(cmd_args["label"])
        _index = cmd_args.get("index")

        if _index is not None:
            try:
                _idx = int(_index)
            except (ValueError, TypeError):
                return None, None
            for _itr in _vnf_current_interactions:
                if _itr["index"] == _idx:
                    return _itr, None
            return None, None

        # Decorative brackets (e.g. "[close map]", "[dismiss tutorial]")
        # are part of the rendered label but agents sometimes pass the
        # inner text without brackets.  Strip a single enclosing pair for
        # the normalized form used in exact-match attempts.
        _strip_brackets = lambda s: (
            s[1:-1].strip() if len(s) >= 2 and s[0] == "[" and s[-1] == "]" else s)

        for _target in _candidates:
            _target_lower = _vnf_normalize_quotes(
                _vnf_text(_target)).lower().strip()
            _target_stripped = _strip_brackets(_target_lower)

            # id: exact and unambiguous by construction.
            for _itr in _vnf_current_interactions:
                if _itr["id"] == _target:
                    return _itr, None

            # Exact label/alias match, bucketed by category (choice vs.
            # everything else).  One category wins outright; a tie across
            # categories refuses instead of guessing.
            _exact_by_category = {}
            for _itr in _vnf_current_interactions:
                _disp_lower = _vnf_normalize_quotes(
                    _itr["display_label"]).lower().strip()
                _disp_stripped = _strip_brackets(_disp_lower)
                _hit = (_disp_lower == _target_lower
                        or _disp_stripped == _target_stripped)
                if not _hit:
                    _alias_id = _vnf_interaction_aliases.get(_target_lower)
                    _hit = _alias_id is not None and _itr["id"] == _alias_id
                if _hit:
                    _cat = "choice" if _itr["source"] == "choice" else "control"
                    _exact_by_category.setdefault(_cat, []).append(_itr)
            if _exact_by_category:
                if len(_exact_by_category) == 1:
                    return list(_exact_by_category.values())[0][0], None
                _tied = []
                for _items in _exact_by_category.values():
                    _tied.extend(_items)
                return None, _tied

            # Short control labels require an exact hit, even when absent.
            if len(_target_stripped) <= 3:
                continue

            # Fuzzy: prefix first, substring only when no prefix candidate
            # exists at all.  Exactly one candidate resolves it; more than
            # one -- any category mix -- is ambiguous.
            _prefix_hits = []
            _seen_ids = {}
            for _itr in _vnf_current_interactions:
                if _itr["id"] in _seen_ids:
                    continue
                if _vnf_normalize_quotes(_itr["display_label"]).lower().strip().startswith(
                        _target_lower):
                    _prefix_hits.append(_itr)
                    _seen_ids[_itr["id"]] = True
            _fuzzy_hits = _prefix_hits
            if not _fuzzy_hits:
                _seen_ids = {}
                for _itr in _vnf_current_interactions:
                    if _itr["id"] in _seen_ids:
                        continue
                    if _target_lower and _target_lower in _vnf_normalize_quotes(
                            _itr["display_label"]).lower():
                        _fuzzy_hits.append(_itr)
                        _seen_ids[_itr["id"]] = True
            if len(_fuzzy_hits) == 1:
                return _fuzzy_hits[0], None
            if len(_fuzzy_hits) > 1:
                return None, _fuzzy_hits

        return None, None

    def _vnf_already_selected_menu(action):
        """Only reject a pure ShowMenu no-op, never a selected toggle."""
        if _vnf_is_sequence(action):
            if len(action) != 1:
                return False
            action = action[0]
        try:
            return (action.__class__.__name__ == "ShowMenu"
                and bool(action.get_selected()))
        except Exception:
            return False

    def _vnf_cmd_act(cmd_name, cmd_args):
        global _vnf_native_action_queue, _vnf_pending_click
        global _vnf_button_observation_start, _vnf_button_observation_target
        global _vnf_button_observation_focus_applied
        # ---- Resolve an interaction by ID, label, or index ----
        # Uses the cached canonical interaction list.
        _itr_matched, _itr_ambiguous = _vnf_resolve_act_interaction(cmd_args)

        if _itr_ambiguous:
            # Fail closed: never click when the target reached more than
            # one interaction.  Name every candidate so the caller can
            # retry unambiguously by number or exact label.
            _described = ", ".join(
                _vnf_act_candidate_desc(_itr) for _itr in _itr_ambiguous)
            _searched = cmd_args.get("label", cmd_args.get("id", ""))
            _vnf_client.push_event(dict(
                type="command_result", command="act",
                success=False,
                error="{!r} matches more than one thing: {} - act by "
                      "number or exact label.".format(_searched, _described)))
        elif _itr_matched is None:
            _avail = [i["display_label"] for i in _vnf_current_interactions]
            _itr_candidates = []
            if cmd_args.get("id"):
                _itr_candidates.append(cmd_args["id"])
            if cmd_args.get("label") and cmd_args.get("label") not in _itr_candidates:
                _itr_candidates.append(cmd_args["label"])
            _itr_index = cmd_args.get("index")
            _searched = _itr_candidates if _itr_candidates else [_itr_index]
            _vnf_client.push_event(dict(
                type="command_result", command="act",
                success=False,
                error="No interaction matching {!r}. Available: {}".format(
                    _searched[0] if len(_searched) == 1 else _searched, _avail)))
        elif _itr_matched.get("disabled"):
            # Matched but greyed out.  Surface this explicitly
            # instead of attempting the action — prevents the
            # silent "(no new events)" that used to happen
            # when clicking disabled nav (Sleep in daytime,
            # Wait from wrong scene, etc.).
            _vnf_client.push_event(dict(
                type="command_result", command="act",
                success=False,
                error="'{}' is disabled and cannot be clicked right now.".format(
                    _itr_matched.get("display_label", ""))))
        elif _itr_matched["source"] == "choice":
            # Resolve as a menu choice via the existing mechanism.
            _ci = _itr_matched.get("choice_index")
            if _ci is not None and not (
                    _vnf_request.request_id and _vnf_request.value_map):
                # The choice_request for this exact menu can already be
                # on the wire (the client/agent sees it) while local
                # activation is still deferred for user-pacing (see
                # _compute_pacing_delay) -- the menu renders and pushes
                # its choice_request immediately, but _vnf_request only
                # gets armed once the pacing deadline elapses via the
                # periodic callback. An act() landing in that window
                # used to fall straight to "No active choice request"
                # below and rely on the caller's resync+retry recovery
                # to fix it -- two extra bridge round trips for a menu
                # that never actually changed (resync's own "Restored
                # local choice state" outcome, the only path it ever
                # takes here, proves it's always this exact case, not a
                # genuine new/changed menu). Since an act() has already
                # arrived, there is no more user-pacing benefit to
                # withholding activation, so settle any one-shot
                # visibility filter and fire the deferred activation now
                # instead of bouncing through a separate resync command.
                if _vnf_pending_vis_check[0] is not None:
                    _vnf_periodic_vis_check()
                _dc = _vnf_deferred_choice[0]
                if _dc is not None:
                    _vnf_deferred_choice[0] = None
                    _dc[1]()
                    # Re-resolve against the now-current interaction
                    # cache: the vis-check settle above (or the
                    # activation itself) may have re-numbered choices,
                    # so re-derive _itr_matched/_ci exactly as a fresh
                    # retry would rather than trust the pre-activation
                    # snapshot.
                    _itr_matched, _itr_ambiguous = (
                        _vnf_resolve_act_interaction(cmd_args))
                    _ci = (
                        _itr_matched.get("choice_index")
                        if _itr_matched is not None and not _itr_ambiguous
                        else None)
            if _ci is not None and _vnf_request.request_id and _vnf_request.value_map:
                if _ci in _vnf_request.value_map:
                    _max_obs_duration = _vnf_begin_observation(_ci)
                    _vnf_client.push_event(dict(
                        type="observation_started",
                        command="act",
                        delay=0.0,
                        max_duration=_max_obs_duration,
                    ))
                    _vnf_client.push_event(dict(
                        type="command_result", command="act",
                        success=True,
                        resolved_as="choice",
                        label=_itr_matched["display_label"],
                        index=_ci))
                    # Mark as shim-resolved so the menu wrapper
                    # doesn't tag it as a user click.
                    _vnf_shim_resolved_flag[0] = True
                else:
                    _vnf_client.push_event(dict(
                        type="command_result", command="act",
                        success=False,
                        error="Choice index {} not in value_map".format(_ci)))
            else:
                _vnf_client.push_event(dict(
                    type="command_result", command="act",
                    success=False,
                    error="No active choice request for choice resolution"))
        else:
            # Resolve as a screen-button action using cached raw ref.
            _itr_id = _itr_matched["id"]
            _raw = _vnf_interaction_raw_refs.get(_itr_id)
            if _raw:
                _disp, action_obj, matched_label, matched_screen = _raw
                if _vnf_already_selected_menu(action_obj):
                    return dict(success=False, reason="already_selected",
                        error="That menu is already open. Choose another button.")
                _vnf_log("Interact: clicking button {!r} on {!r}".format(
                    matched_label, matched_screen))
                _vnf_client.push_event(dict(
                    type="command_result", command="act",
                    success=True,
                    resolved_as="button",
                    interaction_type=_itr_matched.get("type", "other"),
                    wait_after_action=bool(_itr_matched.get("wait_after_action")),
                    story_entry=bool(_itr_matched.get("story_entry")),
                    label=_itr_matched["display_label"],
                    screen=matched_screen))
                _use_button_observation = (
                    vnf_player.observation_delay_clicks
                    and vnf_player.allow_user_override
                    and not getattr(renpy.store, "main_menu", False)
                    and matched_screen not in ("menu", "main_menu")
                )
                if _use_button_observation:
                    # Start button observation: highlight, delay, then fire.
                    _vnf_button_observation_target = (_disp, action_obj, matched_label, matched_screen)
                    _vnf_button_observation_focus_applied = _vnf_highlight_button(
                        _disp, matched_label, matched_screen)
                    _vnf_button_observation_start = time.time()
                    try:
                        _btn_clean_len = len(renpy.text.extras.filter_text_tags(matched_label, allow=set()))
                    except Exception:
                        _btn_clean_len = len(matched_label)
                    _btn_obs_delay = _btn_clean_len * vnf_player.click_delay_speed + vnf_player.click_delay_offset
                    _vnf_client.push_event(dict(
                        type="observation_started",
                        delay=_btn_obs_delay))
                    _vnf_log("Button observation started for {!r}".format(matched_label))
                else:
                    _vnf_native_action_queue = action_obj
                    _vnf_pending_click = {
                        "label": matched_label,
                        "screen": matched_screen,
                    }
            else:
                # No cached ref — walk screens to find the button.
                _vnf_log("Interact: no cached ref for {!r}, walking screens".format(_itr_id))
                _itr_label_lower = _vnf_normalize_quotes(
                    _itr_matched["display_label"]).lower().strip()
                # Also try matching by original (pre-transform) label.
                _itr_orig_lower = None
                if _itr_matched.get("original_label") is not None:
                    _itr_orig_lower = _vnf_normalize_quotes(
                        _itr_matched["original_label"]).lower().strip()
                _itr_found = None
                try:
                    _itr_showing = _vnf_get_showing_screens(
                        vnf_player.scrape_visible_list)
                    for _is_tag, _is_scr in _itr_showing:
                        try:
                            if _is_scr.child is None and hasattr(_is_scr, "update"):
                                try:
                                    _is_scr.update()
                                except Exception:
                                    pass
                            _is_btns = []
                            _vnf_collect_button_actions(_is_scr, _is_btns, _is_tag)
                            for fb in _is_btns:
                                _fb_lbl = _vnf_normalize_quotes(
                                    fb[2]).lower().strip()
                                if (_fb_lbl == _itr_label_lower or
                                        (_itr_orig_lower is not None
                                         and _fb_lbl == _itr_orig_lower)):
                                    _itr_found = fb
                                    break
                        except Exception:
                            pass
                        if _itr_found:
                            break
                except Exception:
                    pass
                if _itr_found:
                    _disp, action_obj, matched_label, matched_screen = _itr_found
                    if _vnf_already_selected_menu(action_obj):
                        return dict(success=False, reason="already_selected",
                            error="That menu is already open. Choose another button.")
                    _vnf_client.push_event(dict(
                        type="command_result", command="act",
                        success=True,
                        resolved_as="button",
                        interaction_type=_itr_matched.get("type", "other"),
                        wait_after_action=bool(_itr_matched.get("wait_after_action")),
                        story_entry=bool(_itr_matched.get("story_entry")),
                        label=_itr_matched["display_label"],
                        screen=matched_screen))
                    _use_button_observation = (
                        vnf_player.observation_delay_clicks
                        and vnf_player.allow_user_override
                        and not getattr(renpy.store, "main_menu", False)
                        and matched_screen not in ("menu", "main_menu")
                    )
                    if _use_button_observation:
                        _vnf_button_observation_target = (_disp, action_obj, matched_label, matched_screen)
                        _vnf_button_observation_focus_applied = _vnf_highlight_button(
                            _disp, matched_label, matched_screen)
                        _vnf_button_observation_start = time.time()
                        try:
                            _btn_clean_len = len(renpy.text.extras.filter_text_tags(matched_label, allow=set()))
                        except Exception:
                            _btn_clean_len = len(matched_label)
                        _btn_obs_delay = _btn_clean_len * vnf_player.click_delay_speed + vnf_player.click_delay_offset
                        _vnf_client.push_event(dict(
                            type="observation_started",
                            delay=_btn_obs_delay))
                    else:
                        _vnf_native_action_queue = action_obj
                        _vnf_pending_click = {
                            "label": matched_label,
                            "screen": matched_screen,
                        }
                else:
                    _vnf_client.push_event(dict(
                        type="command_result", command="act",
                        success=False,
                        error="Cached ref missing and screen walk failed for {!r}".format(
                            _itr_matched["display_label"])))

    def _vnf_cmd_screenshot(cmd_name, cmd_args):
        hide_gui = cmd_args.get("hide_gui", True)
        size = cmd_args.get("size", None)
        _vnf_log("Manual screenshot requested (hide_gui={})".format(hide_gui))
        captured = _vnf_capture_screenshot(size=size, hide_gui=hide_gui,
            force=True, capture_id=cmd_args.get("capture_id"))
        return dict(success=bool(captured),
            error=None if captured else "Screenshot capture unavailable")

    def _vnf_cmd_inventory_modify(cmd_name, cmd_args):
        changes = cmd_args.get("changes", [])
        if not changes:
            _vnf_client.push_event(dict(
                type="command_result", command="inventory_modify",
                success=False, error="No changes provided",
            ))
        else:
            result = _vnf_apply_inventory_changes(changes)
            _vnf_log("Inventory modify: {}".format(result.get("message", "")))
            _vnf_client.push_event(dict(
                type="command_result", command="inventory_modify",
                success=result.get("success", False),
                message=result.get("message", ""),
            ))

    def _vnf_cmd_stats_modify(cmd_name, cmd_args):
        changes = cmd_args.get("changes", {})
        if not changes:
            _vnf_client.push_event(dict(
                type="command_result", command="stats_modify",
                success=False, error="No changes provided",
            ))
        else:
            result = _vnf_apply_stats_changes(changes)
            _vnf_log("Stats modify: {}".format(result.get("message", "")))
            _vnf_client.push_event(dict(
                type="command_result", command="stats_modify",
                success=result.get("success", False),
                message=result.get("message", ""),
            ))

    def _vnf_cmd_back(cmd_name, cmd_args):
        global _vnf_native_action_queue
        # overlays_only: refuse instead of falling through to a bare
        # Return() when nothing dismissable is showing.  back_all loops on
        # this until the refusal; a bare Return() at the world screen would
        # END the current say interaction, i.e. advance the story.
        try:
            _overlays_only = bool((cmd_args or {}).get("overlays_only"))
        except Exception:
            _overlays_only = False
        # Close the topmost overlay or game-menu screen.
        # For Show()-based overlays (shop, rest, wait), use Hide().
        # For ShowMenu()-based screens, use Return() (Escape).
        try:
            # Check for visible registered overlay screens.
            _back_target = None
            for _tag, _scr in _vnf_get_showing_screens():
                if (_tag in _vnf_overlay_screens
                        and _tag not in _vnf_passive_overlay_screens):
                    _back_target = _tag
            _modal = _vnf_has_modal_overlay()
            _called_screen = _vnf_has_transient_custom_screen()
            # Return from title-menu subpages uses Ren'Py's own ShowMenu
            # behavior. Do not classify the title screen itself as closable.
            _generic_game_menu = _vnf_is_generic_game_menu_showing(allow_main_menu=True)
            # A modal Show-based panel can cover a non-modal call screen.
            # Hide only that registered panel, never its waiting caller.
            _covered_overlay = False
            if (_called_screen and _modal and _modal != _called_screen
                    and _modal in _vnf_overlay_screens
                    and _modal not in _vnf_passive_overlay_screens):
                try:
                    _panel = renpy.exports.get_screen(_modal)
                    _covered_overlay = (_panel is not None
                                        and not getattr(_panel, "transient", True))
                except Exception:
                    pass
                if _covered_overlay:
                    _back_target = _modal
            if _generic_game_menu:
                # Ren'Py's game-menu wrapper owns Return(), so Escape-style
                # dismissal cannot fall through into story code.
                _vnf_native_action_queue = Return()
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=True,
                    note="Game-menu Return action queued."))
            elif _called_screen and not _covered_overlay:
                # `call screen` always owns the interaction even when its
                # screen is non-modal. Its caller may handle only named return
                # values, so Return(None) can fall through and replay work.
                # rewind() is not a substitute here: it stays a deliberate
                # rollback (moves story history) rather than a same-place
                # "close this UI" action, so the refusal points at the
                # visible control first and rewind only as the fallback
                # that DOES leave the console cleanly, at the cost of
                # moving the story backward.
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=False,
                    error="Screen '{}' is a custom called screen; generic "
                          "'back' cannot safely choose its return value. "
                          "Use act() on its visible Close or Release "
                          "control, or call rewind() to roll back through "
                          "it (rewind moves the story backward through "
                          "the console; back never does).".format(
                              _called_screen)))
            elif _back_target:
                # A registered Show()-based overlay has no caller waiting on
                # a return value. The caller underneath, if any, stays live.
                _vnf_native_action_queue = Hide(_back_target)
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=True,
                    note="Hide '{}' queued.".format(_back_target)))
            elif _modal not in (None, "choice", "nvl"):
                # An arbitrary custom `call screen` defines its own return
                # contract. Return(None) can fall through a caller that only
                # handles named values and replay cost-bearing work. Require
                # the screen's visible Close/Release action instead.
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=False,
                    error="A custom modal is active ('{}'); generic 'back' "
                          "cannot safely choose its return value. Use "
                          "act() on the visible Close or Release control, "
                          "or call rewind() to roll back through it "
                          "(rewind moves the story backward; back never "
                          "does).".format(_modal)))
            elif _vnf_current_menu_context[0]:
                # A choice menu is live. A bare Return() here does not
                # "go back" — Ren'Py resolves the MENU with it, i.e. it
                # PICKS an option (resolved_by=shim, whatever value the
                # Return lands on) and runs that arm's side effects.
                # Run f2-moralist (2026-08-19) tripled its inventory
                # this way: back at the storage menu re-executed the
                # collection arm on every call, 10 minutes each, and
                # the duplicated items persisted to the finale. Same
                # family as the wait-door auto-advance guard: never
                # send a naked Return into a live menu.
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=False,
                    error="A choice is active — 'back' would resolve "
                          "it as a pick, not undo anything. Use act() "
                          "to answer it (or rewind for history)."))
            elif _overlays_only:
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=False,
                    nothing_to_close=True,
                    error="Nothing to close: no overlay or menu screen "
                          "is showing."))
            else:
                _vnf_native_action_queue = Return()
                _vnf_client.push_event(dict(
                    type="command_result", command="back",
                    success=True,
                    note="Return action queued."))
        except Exception as e:
            _vnf_client.push_event(dict(
                type="command_result", command="back",
                success=False,
                error="Failed: {}".format(_vnf_text(e))))

    def _vnf_cmd_inspect(cmd_name, cmd_args):
        _inspect = _vnf_collect_inspect_data()
        _inspect["type"] = "command_result"
        _inspect["command"] = "inspect"
        _inspect["success"] = True
        _vnf_client.push_event(_inspect)

    def _vnf_cmd_set(cmd_name, cmd_args):
        # Runtime config: set vnf_player attributes.
        # Batch mode: {"changes": {"key": val, ...}}
        _nonce = (cmd_args or {}).get("nonce") if cmd_args else None
        def _push_set_result(_event):
            if _nonce is not None:
                _event["nonce"] = _nonce
            _vnf_client.push_event(_event)
        _set_changes = cmd_args.get("changes", None)
        if _set_changes is not None and hasattr(
                _set_changes, "items"):
            _applied = []
            _errors = []
            for _sk, _sv in _set_changes.items():
                if not hasattr(vnf_player, _sk):
                    _errors.append(
                        "Unknown key: {!r}".format(_sk))
                    continue
                try:
                    _old, _coerced = _vnf_set_config_key(_sk, _sv)
                    _vnf_log(
                        "Config set: {} = {!r} (was {!r})"
                        .format(_sk, _coerced, _old))
                    _applied.append(dict(
                        key=_sk, value=_coerced,
                        old_value=_old))
                except Exception as _se:
                    _errors.append("{}: {}".format(
                        _sk, _vnf_text(_se)))
            _push_set_result(dict(
                type="command_result", command="set",
                success=len(_errors) == 0,
                applied=_applied,
                errors=_errors if _errors else None))
        else:
            # Single-key mode.
            _set_key = cmd_args.get("key", "")
            _set_val = cmd_args.get("value", None)
            if not _set_key or not hasattr(
                    vnf_player, _set_key):
                _push_set_result(dict(
                    type="command_result", command="set",
                    success=False,
                    error="Unknown config key: {!r}".format(
                        _set_key)))
            elif _set_val is None:
                # Query mode.
                _cur_val = getattr(vnf_player, _set_key)
                _push_set_result(dict(
                    type="command_result", command="set",
                    success=True,
                    key=_set_key, value=_cur_val))
            else:
                try:
                    _old_val, _coerced = _vnf_set_config_key(
                        _set_key, _set_val)
                    _vnf_log(
                        "Config set: {} = {!r} (was {!r})"
                        .format(_set_key, _coerced,
                                _old_val))
                    _push_set_result(dict(
                        type="command_result", command="set",
                        success=True,
                        key=_set_key, value=_coerced,
                        old_value=_old_val))
                except Exception as _set_exc:
                    _push_set_result(dict(
                        type="command_result", command="set",
                        success=False,
                        error="Failed to set {}: {}".format(
                            _set_key, _vnf_text(_set_exc))))

    def _vnf_cmd_get_defaults(cmd_name, cmd_args):
        # Return the init-time default values for settable config keys.
        # Only include simple types (numbers, bools, strings) —
        # skip callbacks, lists, and objects set by game mods.
        _nonce = (cmd_args or {}).get("nonce") if cmd_args else None
        _gd_simple = {}
        for _gdk, _gdv in _vnf_config_defaults.items():
            if isinstance(_gdv, (int, float, bool)):
                _gd_simple[_gdk] = _gdv
            elif isinstance(_gdv, str):
                _gd_simple[_gdk] = _gdv
        _event = dict(
            type="command_result", command="get_defaults",
            success=True,
            defaults=_gd_simple,
        )
        if _nonce is not None:
            _event["nonce"] = _nonce
        _vnf_client.push_event(_event)

    def _vnf_cmd_get_stats(cmd_name, cmd_args):
        try:
            _gs_inv, _gs_stats = _vnf_get_inventory_stats()
            _gs_ev = dict(
                type="command_result", command="get_stats",
                success=True, _ts=_time.time())
            if _gs_inv is not None:
                _gs_ev["inventory"] = _gs_inv
            if _gs_stats is not None:
                _gs_ev["stats"] = _gs_stats
            _vnf_client.push_event(_gs_ev)
        except Exception as _gs_e:
            _vnf_client.push_event(dict(
                type="command_result", command="get_stats",
                success=False, error=_vnf_text(_gs_e)))

    # Register the built-in command handlers (including aliases).
    # Built-ins register at init 999 — after mods at init -989 — so on
    # a name clash the built-in wins, matching the old elif-chain order.
    _vnf_add_command_handler("start", _vnf_cmd_start, causal_boundary=True)
    _vnf_add_command_handler("save", _vnf_cmd_save)
    _vnf_add_command_handler("load", _vnf_cmd_load, causal_boundary=True)
    _vnf_add_command_handler("rollback", _vnf_cmd_rollback, causal_boundary=True)
    _vnf_add_command_handler("advance", _vnf_cmd_advance, causal_boundary=True)
    _vnf_add_command_handler("step", _vnf_cmd_advance, causal_boundary=True)
    _vnf_add_command_handler("next", _vnf_cmd_advance, causal_boundary=True)
    _vnf_add_command_handler("rewind", _vnf_cmd_rewind, causal_boundary=True)
    _vnf_add_command_handler("backward", _vnf_cmd_rewind, causal_boundary=True)
    _vnf_add_command_handler("story_back", _vnf_cmd_rewind, causal_boundary=True)
    _vnf_add_command_handler("replay", _vnf_cmd_replay, causal_boundary=True)
    _vnf_add_command_handler("forward", _vnf_cmd_replay, causal_boundary=True)
    _vnf_add_command_handler("story_forward", _vnf_cmd_replay, causal_boundary=True)
    _vnf_add_command_handler("quit", _vnf_cmd_quit, causal_boundary=True)
    _vnf_add_command_handler("skip_toggle", _vnf_cmd_skip_toggle)
    _vnf_add_command_handler("auto_advance_on", _vnf_cmd_auto_advance_on, causal_boundary=True)
    _vnf_add_command_handler("auto_advance_off", _vnf_cmd_auto_advance_off)
    _vnf_add_command_handler("fast_forward_on", _vnf_cmd_fast_forward_on, causal_boundary=True)
    _vnf_add_command_handler("fast_forward_off", _vnf_cmd_fast_forward_off)
    _vnf_add_command_handler("act", _vnf_cmd_act)
    _vnf_add_command_handler("screenshot", _vnf_cmd_screenshot)
    _vnf_add_command_handler("inventory_modify", _vnf_cmd_inventory_modify, causal_boundary=True)
    _vnf_add_command_handler("stats_modify", _vnf_cmd_stats_modify, causal_boundary=True)
    _vnf_add_command_handler("back", _vnf_cmd_back, causal_boundary=True)
    _vnf_add_command_handler("inspect", _vnf_cmd_inspect)
    _vnf_add_command_handler("set", _vnf_cmd_set)
    _vnf_add_command_handler("get_defaults", _vnf_cmd_get_defaults)
    _vnf_add_command_handler("get_stats", _vnf_cmd_get_stats)

    # Built-in command names — registered above into the shared handler
    # registry to dissolve the old elif chain. The `custom_commands` field
    # advertised in state/game_state means "commands beyond the shim's
    # built-ins" (i.e. mod-registered), so exclude these from it to keep
    # that field byte-identical to before the registry migration.
    _VNF_BUILTIN_COMMAND_NAMES = frozenset([
        "start", "save", "load", "rollback",
        "advance", "step", "next",
        "rewind", "backward", "story_back",
        "replay", "forward", "story_forward",
        "quit", "skip_toggle",
        "auto_advance_on", "auto_advance_off",
        "fast_forward_on", "fast_forward_off",
        "act", "screenshot", "inventory_modify", "stats_modify",
        "back", "inspect", "set", "get_defaults", "get_stats",
    ])

    def _vnf_mod_command_names():
        """Registry command names that are NOT shim built-ins (for the
        agent-facing custom_commands advertisement)."""
        return [k for k in _vnf_command_handlers
                if k not in _VNF_BUILTIN_COMMAND_NAMES]

    def _vnf_prepare_ui_command(cmd_data):
        """Keep an incoming UI act queued until its current frame is captured."""
        if not _vnf_is_mapping(cmd_data) or cmd_data.get("name") != "act":
            return True
        if "_vnf_expected_surface" not in cmd_data:
            cmd_data["_vnf_expected_surface"] = _vnf_signature_value(
                _vnf_current_interactions)
        if not _vnf_focus_snapshot_is_current():
            return False
        try:
            _vnf_scrape_visible_screens(force=True)
        except Exception:
            return False
        cmd_data["_vnf_surface_changed"] = (
            cmd_data["_vnf_expected_surface"] != _vnf_signature_value(
                _vnf_current_interactions))
        return True

    def _vnf_execute_pending_command():
        """
        Execute a command that was found by the background poll thread.

        IMPORTANT: This is called from a screen timer action (vnf_command_poller
        screen) so that exceptions like JumpOutException propagate correctly
        through Ren'Py's interaction / event handling path.
        """
        if not vnf_player.enabled:
            _vnf_pending_command_box[0] = None
            return

        cmd_data = _vnf_pending_command_box[0]
        if cmd_data is None:
            return
        if (_vnf_client is not None
                and _vnf_client.has_pending_critical_events()):
            # Keep the prefetched command intact. The retained receipt ahead
            # of it is an ordering barrier; executing now could mutate the
            # game repeatedly while its acknowledgements remain unavailable.
            return

        if (_vnf_is_mapping(cmd_data) and cmd_data.get("name") == "act"
                and not _vnf_prepare_ui_command(cmd_data)):
            return

        # Clear the pending command BEFORE executing so the background
        # thread can start looking for the next one, and so the screen
        # timer doesn't re-fire for the same command.
        _vnf_pending_command_box[0] = None

        # cmd_data comes from json.loads via poll_command — it should be a dict.
        try:
            cmd_name = cmd_data["name"]
        except (KeyError, TypeError):
            try:
                cmd_name = cmd_data.get("name", "")
            except AttributeError:
                cmd_name = _vnf_text(cmd_data)
        try:
            cmd_args = cmd_data.get("args", {})
        except AttributeError:
            cmd_args = {}
        try:
            _cmd_nonce = cmd_data.get("nonce", None)
        except AttributeError:
            _cmd_nonce = None
        if _cmd_nonce is not None:
            try:
                _cmd_args_copy = dict(cmd_args or {})
                _cmd_args_copy["nonce"] = _cmd_nonce
                cmd_args = _cmd_args_copy
            except Exception:
                pass
            _cached_result = _vnf_command_result_cache.get(_cmd_nonce)
            if _cached_result is not None:
                _vnf_log("Replaying cached command result for nonce {}".format(
                    _cmd_nonce))
                _vnf_client.push_event_sync(dict(_cached_result))
                return

        _vnf_log("Received command: {} {}".format(cmd_name, cmd_args))

        try:
            _handler = _vnf_command_handlers.get(cmd_name)
            if _handler is not None:
                _old_push_event = getattr(_vnf_client, "push_event", None)
                _old_push_event_sync = getattr(_vnf_client, "push_event_sync", None)
                def _push_event_with_command_nonce(_event):
                    _is_nonce_result = False
                    try:
                        if (_vnf_is_mapping(_event)
                                and _event.get("type") == "command_result"
                                and _event.get("command") == cmd_name):
                            _event.setdefault(
                                "causal_boundary",
                                _vnf_command_causal_boundaries.get(
                                    cmd_name, False))
                            if _cmd_nonce is not None:
                                _event.setdefault("nonce", _cmd_nonce)
                                _is_nonce_result = True
                                _vnf_remember_command_result(
                                    _cmd_nonce, _event)
                    except Exception:
                        pass
                    if _is_nonce_result and callable(_old_push_event_sync):
                        return _old_push_event_sync(_event)
                    return _old_push_event(_event)
                def _push_event_sync_with_command_nonce(_event):
                    try:
                        if (_vnf_is_mapping(_event)
                                and _event.get("type") == "command_result"
                                and _event.get("command") == cmd_name):
                            _event.setdefault(
                                "causal_boundary",
                                _vnf_command_causal_boundaries.get(
                                    cmd_name, False))
                            if _cmd_nonce is not None:
                                _event.setdefault("nonce", _cmd_nonce)
                                _vnf_remember_command_result(
                                    _cmd_nonce, _event)
                    except Exception:
                        pass
                    return _old_push_event_sync(_event)
                if callable(_old_push_event):
                    _vnf_client.push_event = _push_event_with_command_nonce
                if callable(_old_push_event_sync):
                    _vnf_client.push_event_sync = _push_event_sync_with_command_nonce
                try:
                    if cmd_name == "act" and cmd_data.get("_vnf_surface_changed"):
                        _handler_result = dict(
                            success=False,
                            error="The UI changed before this action could run; observe the current controls and retry.",
                            error_code="stale_surface")
                    else:
                        _handler_result = _handler(cmd_name, cmd_args)
                finally:
                    if callable(_old_push_event):
                        _vnf_client.push_event = _old_push_event
                    if callable(_old_push_event_sync):
                        _vnf_client.push_event_sync = _old_push_event_sync
                if _vnf_is_mapping(_handler_result):
                    _handler_result.setdefault("type", "command_result")
                    _handler_result.setdefault("command", cmd_name)
                    _handler_result.setdefault(
                        "causal_boundary",
                        _vnf_command_causal_boundaries.get(cmd_name, False))
                    if _cmd_nonce is not None:
                        _handler_result.setdefault("nonce", _cmd_nonce)
                        _vnf_remember_command_result(_cmd_nonce, _handler_result)
                    if _cmd_nonce is not None:
                        _vnf_client.push_event_sync(_handler_result)
                    else:
                        _vnf_client.push_event(_handler_result)
            else:
                _vnf_log("Unknown command: " + cmd_name)
                _event = dict(type="command_result", command=cmd_name,
                              success=False, error="Unknown command")
                if _cmd_nonce is not None:
                    _event["nonce"] = _cmd_nonce
                    _vnf_remember_command_result(_cmd_nonce, _event)
                    _vnf_client.push_event_sync(_event)
                else:
                    _vnf_client.push_event(_event)

        except _CONTROL_EXCEPTIONS:
            # Re-raise ALL Ren'Py control-flow exceptions:
            # JumpException, JumpOutException, CallException,
            # QuitException, FullRestartException, etc.
            # These MUST propagate or the game hangs / crashes.
            raise
        except Exception as e:
            _vnf_log("Command failed: {} - {}".format(
                cmd_name, _vnf_text(e, "unknown command failure")))
            _event = dict(
                type="command_result", command=cmd_name, success=False,
                error=_vnf_text(e, "unknown command failure"),
            )
            if _cmd_nonce is not None:
                _event["nonce"] = _cmd_nonce
                _vnf_remember_command_result(_cmd_nonce, _event)
                _vnf_client.push_event_sync(_event)
            else:
                _vnf_client.push_event(_event)

    # 7.x/8.x command execution via interact_callbacks (6.x uses the
    # inline _vnf_6x_poll_and_execute instead).  interact_callbacks
    # runs at the START of each interaction — including the one that
    # the poll worker's restart_interaction() triggers — which makes
    # it a reliable execution point across contexts (main menu etc.).
    _vnf_6x_ic_poll = [0.0]
    def _vnf_interact_execute_command():
        """Execute pending commands from interact_callbacks context.

        Polling itself is the background worker's job; the fallback
        main-thread poll below only engages when that worker is dead
        (it blocks the UI for up to the HTTP timeout, and a second
        live poller could pop a queued command the worker then
        overwrites).
        """
        if not vnf_player.enabled:
            return
        # Degraded-mode fallback poll (rate-limited, worker dead only).
        _now = _time.time()
        if (_vnf_pending_command_box[0] is None
                and _now - _vnf_6x_ic_poll[0] > 0.2
                and _vnf_client and _vnf_client.slot_id
                and not _vnf_client.has_pending_critical_events()
                and (_vnf_command_poll_thread is None
                     or not _vnf_command_poll_thread.is_alive())):
            _vnf_6x_ic_poll[0] = _now
            try:
                cmd = _vnf_client.poll_command()
                if cmd is not None:
                    _vnf_pending_command_box[0] = cmd
            except Exception:
                pass
        if (_vnf_pending_command_box[0] is not None
                and not _vnf_client.has_pending_critical_events()):
            _vnf_execute_pending_command()
        # Native action queue is NOT executed here — interact_callbacks
        # runs before event processing, so EndInteraction from
        # end_interaction(rv) would propagate uncaught on Ren'Py 7.x.
        # The vnf_command_poller screen timer handles it instead
        # (screen timers run during event processing where
        # EndInteraction IS caught).
    if not _is_renpy6:
        renpy.config.interact_callbacks.append(_vnf_interact_execute_command)

    # Context detection goes in interact_callbacks (safe, no exceptions).
    def _vnf_context_interact_callback():
        """Detect context changes on each interaction."""
        _vnf_detect_context()

    if not _is_renpy6:
        if _vnf_context_interact_callback not in renpy.config.interact_callbacks:
            renpy.config.interact_callbacks.append(_vnf_context_interact_callback)

    # Turbo reconciliation: picks up vnf_player.turbo when it was set
    # outside the `set` command (launch profile / mod / console) on the
    # first interaction.  Wrapped like the other callbacks so a turbo
    # failure can never propagate into the game's interact loop.
    def _vnf_turbo_interact_callback():
        if vnf_player.enabled:
            _vnf_sync_turbo()

    renpy.config.interact_callbacks.append(
        _safe_periodic(_vnf_turbo_interact_callback))

    # -------------------------------------------------------------------------
    # 7b. UI Inspect
    # -------------------------------------------------------------------------

    def _vnf_extract_widget_label(widget):
        """Extract text label from a button/hotspot widget's children."""
        try:
            ch_list = getattr(widget, "children", [])
            ch_single = getattr(widget, "child", None)
            for src in [ch_list, [ch_single] if ch_single else []]:
                for ch in src:
                    if ch is None:
                        continue
                    t = getattr(ch, "text", None)
                    if t is None:
                        continue
                    try:
                        raw = "".join(_vnf_text(x) for x in t)
                    except TypeError:
                        raw = _vnf_text(t) if t else None
                    if raw:
                        try:
                            return renpy.text.extras.filter_text_tags(raw, allow=set())
                        except Exception:
                            return raw
        except Exception:
            pass
        return None

    def _vnf_mark_focus_snapshot_stale():
        # interact_callbacks run before the new frame is rendered. The engine
        # replaces focus_list in take_focuses after drawing, on 6.x through 8.x.
        # Keep the actual engine object on sys, outside rollback-managed store.
        _sys_mod._vnf_focus_snapshot_before_interact = getattr(
            renpy.display.focus, "focus_list", None)

    def _vnf_focus_snapshot_is_current():
        current = getattr(renpy.display.focus, "focus_list", None)
        return (current is not None and current is not getattr(
            _sys_mod, "_vnf_focus_snapshot_before_interact", None))

    # Invalidate before any scraper/command callback, including on interaction
    # restarts. Replace our own callback on Shift+R rather than stacking copies.
    renpy.config.interact_callbacks[:] = [
        cb for cb in renpy.config.interact_callbacks
        if getattr(cb, "__name__", "") != "_vnf_mark_focus_snapshot_stale"
    ]
    renpy.config.interact_callbacks.insert(0, _vnf_mark_focus_snapshot_stale)
    _vnf_mark_focus_snapshot_stale()

    def _vnf_get_focus_screen_name(f):
        """Extract screen tag name from a focus list entry."""
        scr = getattr(f, "screen", None)
        if scr is None:
            return None
        name = getattr(scr, "screen_name", None)
        if isinstance(name, tuple) and name:
            return name[0]
        if isinstance(name, str):
            return name
        return _vnf_text(name) if name else None

    def _vnf_collect_inspect_data():
        """
        Collect a detailed snapshot of the current UI state:
        active screens, focus list with bounding boxes, viewport
        scroll state, mouse position, and scraped buttons.
        """
        result = {}

        # Screen geometry.
        sw = getattr(renpy.config, "screen_width", 0)
        sh = getattr(renpy.config, "screen_height", 0)
        result["screen_width"] = sw
        result["screen_height"] = sh

        # Active screens.
        screens_info = []
        try:
            showing = _vnf_get_showing_screens(None)
            for tag, scr in showing:
                if tag.startswith("vnf_"):
                    continue
                info = {
                    "tag": tag,
                    "modal": bool(getattr(scr, "modal", False)),
                    "transient": bool(getattr(scr, "transient", False)),
                }
                # zorder: Ren'Py 8 stores it on the SceneList entry,
                # try the displayable first, fall back to 0.
                zo = getattr(scr, "zorder", None)
                if zo is None:
                    zo = 0
                info["zorder"] = zo
                screens_info.append(info)
        except Exception:
            pass
        result["screens"] = screens_info

        # Focus list.
        focus_items = []
        viewports = []
        seen_viewports = set()
        try:
            fl = renpy.display.focus.focus_list
            current_focused = renpy.display.focus.get_focused()
            for f in fl:
                if f.x is None:
                    continue
                scr_name = _vnf_get_focus_screen_name(f)
                widget_type = type(f.widget).__name__
                action = getattr(f.widget, "action", None)
                if action is None:
                    action = getattr(f.widget, "clicked", None)

                # Scrollbar → viewport info.
                if widget_type == "Bar":
                    adj = getattr(f.widget, "adjustment", None)
                    if adj is not None and scr_name and scr_name not in seen_viewports:
                        seen_viewports.add(scr_name)
                        v_range = getattr(adj, "range", 0)
                        v_page = getattr(adj, "page", 0)
                        v_value = getattr(adj, "value", 0)
                        pct = 0.0
                        if v_range > 0:
                            pct = round(v_value / v_range * 100.0, 1)
                        viewports.append({
                            "screen": scr_name,
                            "scroll_value": round(v_value, 1),
                            "scroll_range": round(v_range, 1),
                            "scroll_page": round(v_page, 1),
                            "scroll_percent": pct,
                        })
                    continue  # Don't list scrollbars as clickable.

                # Determine action type name.
                if action is not None:
                    if _vnf_is_sequence(action):
                        action_names = [type(a).__name__ for a in action]
                    else:
                        action_names = [type(action).__name__]
                else:
                    action_names = []
                action_type = ", ".join(action_names) if action_names else "None"

                label = _vnf_extract_widget_label(f.widget) or ""
                is_focused = (f.widget is current_focused)

                item = {
                    "screen": scr_name or "",
                    "widget_type": widget_type,
                    "action_type": action_type,
                    "label": label,
                    "x": int(f.x), "y": int(f.y),
                    "w": int(f.w), "h": int(f.h),
                }
                if is_focused:
                    item["is_focused"] = True

                # Action detail strings (field, value, etc.)
                if action is not None:
                    _acts = action if _vnf_is_sequence(action) else [action]
                    _detail = []
                    for _a in _acts:
                        _a_cls = type(_a).__name__
                        _parts = [_a_cls]
                        for _attr in ("property", "field", "variable",
                                      "screen", "value", "label"):
                            _v = getattr(_a, _attr, None)
                            if _v is not None:
                                _parts.append("{}={}".format(_attr, _v))
                        _detail.append(" ".join(_parts))
                    item["action_strs"] = _detail

                # Image path for ImageButtons.
                if "ImageButton" in widget_type:
                    for _img_attr in ("idle_image", "idle",
                                      "idle_child", "child"):
                        _img = getattr(f.widget, _img_attr, None)
                        if _img is not None:
                            # Walk through wrappers to find a filename.
                            _fname = getattr(_img, "filename", None)
                            if _fname is None:
                                _inner = getattr(_img, "child", None)
                                if _inner is not None:
                                    _fname = getattr(_inner, "filename", None)
                            if _fname is None:
                                _fname = _vnf_text(_img)
                            item["image"] = _fname
                            break

                focus_items.append(item)
        except Exception:
            pass
        result["focus_list"] = focus_items
        result["viewports"] = viewports

        # Mouse state.
        mouse = {}
        try:
            mx, my = renpy.display.draw.get_mouse_pos()
            mouse["x"] = int(mx)
            mouse["y"] = int(my)
        except Exception:
            pass
        try:
            focused = renpy.display.focus.get_focused()
            if focused is not None:
                mouse["focused_label"] = _vnf_extract_widget_label(focused) or ""
                # Find screen of focused widget.
                for f in renpy.display.focus.focus_list:
                    if f.widget is focused:
                        mouse["focused_screen"] = _vnf_get_focus_screen_name(f) or ""
                        break
        except Exception:
            pass
        result["mouse"] = mouse

        # Scraped buttons (for cross-reference).
        scraped = {"buttons": [], "screens": []}
        try:
            showing = _vnf_get_showing_screens(vnf_player.scrape_visible_list)
            per_screen = []
            for tag, scr in showing:
                try:
                    if scr.child is None and hasattr(scr, "update"):
                        try:
                            scr.update()
                        except Exception:
                            pass
                    _is_modal = getattr(scr, "modal", False)
                    scr_data = {"_tag": tag, "modal": bool(_is_modal), "texts": [], "_text_sections": [], "_text_paths": [], "choices": [], "value_map": {}, "buttons": []}
                    _sec_depth = _vnf_screen_section_depths.get(tag)
                    _vnf_walk_screen(scr, scr_data, _section_depth=_sec_depth)
                    for btn in scr_data["buttons"]:
                        btn["screen"] = tag
                    per_screen.append(scr_data)
                except Exception:
                    pass
            per_screen = _vnf_apply_screen_transforms(per_screen)
            _insp_modal_screens = [
                sd["_tag"] for sd in per_screen if sd.get("modal")]
            for scr_data in per_screen:
                scraped["screens"].append(scr_data["_tag"])
                scraped["buttons"].extend(scr_data["buttons"])
            # Apply action transforms (includes modal filter) so the
            # scraped list matches what the normal pipeline produces.
            if _vnf_action_transforms and scraped["buttons"]:
                _iat_actions, _iat_ctx = _vnf_build_action_list(
                    [], scraped["buttons"], _insp_modal_screens)
                _iat_actions = _vnf_apply_action_transforms(
                    _iat_actions, _iat_ctx)
                scraped["buttons"] = [
                    dict(label=a["label"], actions=a["actions"],
                         action_strs=a.get("action_strs", []),
                         screen=a.get("screen", ""),
                         is_selected=a.get("is_selected", False))
                    for a in _iat_actions if a["source"] == "button"
                ]
        except Exception:
            pass
        result["scraped"] = scraped

        _mod_cmds = _vnf_mod_command_names()
        if _mod_cmds:
            result["custom_commands"] = _mod_cmds

        return result

    # -------------------------------------------------------------------------
    # 7c. Visible Screen Scraping
    # -------------------------------------------------------------------------
    #
    # Screens shown via `use` inside another screen (e.g. a random-quote
    # frame inside `navigation` included by `main_menu`) are never called
    # through `call_screen`, so the wrapper in section 4b never fires.
    #
    # This interact callback scrapes the displayable trees of currently
    # visible screens and pushes a `screen_content` event whenever the
    # content changes.  The LLM gets full awareness of what is on screen
    # even for custom embedded UI elements.
    # -------------------------------------------------------------------------

    # Screens whose content is usually noise (overlays, internal) and
    # should be skipped during dynamic discovery.
    _VISIBLE_SCRAPE_SKIP = {
        "say", "choice", "ctc", "notify", "skip_indicator", "_",
        "quick_menu", "text_input",
    }

    def _vnf_get_showing_screens(explicit_list=None):
        """
        Return a list of (tag, ScreenDisplayable) for all screens
        currently showing on the 'screens' layer.

        If `explicit_list` is given (a list of screen-name strings),
        only those screens are returned (via get_screen).  Otherwise
        ALL screens on the layer are discovered dynamically, minus
        those in _VISIBLE_SCRAPE_SKIP.
        """
        global _vnf_active_call_screen_name
        results = []  # [(tag_string, displayable)]

        # If a call_screen is active, start with it but also include
        # scrape_extra_screens (mods may need overlay screens like
        # selling/map that live alongside the call_screen).
        if _vnf_active_call_screen_name:
            try:
                scr = renpy.exports.get_screen(_vnf_active_call_screen_name)
                if scr is not None:
                    results.append((_vnf_active_call_screen_name, scr))
            except Exception:
                pass
            _extras = vnf_player.scrape_extra_screens
            # Also include registered overlay screens so overlay_active
            # can be detected when a call_screen is active.
            _all_extras = set(_extras) if _extras else set()
            _all_extras.update(_vnf_overlay_screens)
            if _all_extras:
                _cs_seen = {_vnf_active_call_screen_name}
                for name in _all_extras:
                    if name in _cs_seen:
                        continue
                    try:
                        scr = renpy.exports.get_screen(name)
                        if scr is not None:
                            results.append((name, scr))
                    except Exception:
                        pass
            return results

        if explicit_list is not None:
            for name in explicit_list:
                try:
                    scr = renpy.exports.get_screen(name)
                    if scr is not None:
                        results.append((name, scr))
                except Exception:
                    pass
        else:
            try:
                sl = renpy.exports.scene_lists()

                if hasattr(sl, "get_all_layer_tag_displayable"):
                    # Ren'Py 8.x
                    for layer, tag, disp in sl.get_all_layer_tag_displayable():
                        if layer != "screens":
                            continue
                        if not tag or "$" in tag:
                            continue
                        if tag in _VISIBLE_SCRAPE_SKIP:
                            continue
                        results.append((tag, disp))
                elif hasattr(sl, "layers"):
                    # Ren'Py 7.x fallback
                    for sle in list(sl.layers.get("screens", [])):
                        tag = getattr(sle, "tag", None)
                        disp = getattr(sle, "displayable", None)
                        if not tag or "$" in tag or not disp:
                            continue
                        if tag in _VISIBLE_SCRAPE_SKIP:
                            continue
                        results.append((tag, disp))
            except Exception:
                if vnf_player.debug:
                    _tb_module.print_exc()

        # Centralized Sorting: transient screens first, then tag alphabetically.
        # This ensures consistent button indexing between scraping and clicking.
        def _screen_priority_key(pair):
            _tag, sd = pair
            is_transient = getattr(sd, "transient", False)
            return (0 if is_transient else 1, _tag)

        results.sort(key=_screen_priority_key)
        return results

    _vnf_last_scrape_time = 0.0
    _vnf_consecutive_unchanged_scrapes = 0
    _vnf_orphan_resync_last = 0.0
    # Set after resync fires; makes the next scraper tick skip the
    # orphan check so Menu.execute for the incoming menu has a cycle
    # to finalize before we re-evaluate. Cleared at the top of each
    # scrape run.
    _vnf_orphan_skip_next = False

    def _vnf_signature_value(value):
        """Return a deterministic, JSON-ish value for change detection."""
        try:
            if _vnf_is_mapping(value):
                return tuple(sorted(
                    (_vnf_text(k), _vnf_signature_value(v))
                    for k, v in value.items()
                ))
            if _vnf_is_sequence(value):
                return tuple(_vnf_signature_value(v) for v in value)
        except Exception:
            pass
        return repr(value)

    def _vnf_normalize_focus_label(label):
        """Return a stable key for focus-list/menu label equivalence."""
        label = label or u""
        try:
            if isinstance(label, bytes) and not isinstance(label, type(u"")):
                label = label.decode("utf-8", "replace")
            elif not isinstance(label, type(u"")):
                label = u"{}".format(label)
        except Exception:
            label = u"{}".format(label)
        label = label.strip()
        try:
            label = renpy.text.extras.filter_text_tags(label, allow=set())
        except Exception:
            # Ren'Py 6 builds may only have the compatibility implementation.
            # Keep normalization deterministic even if its tag filter rejects
            # the input type.
            clean = []
            in_tag = False
            for ch in label:
                if ch == u"{":
                    in_tag = True
                    continue
                if in_tag:
                    if ch == u"}":
                        in_tag = False
                    continue
                clean.append(ch)
            label = u"".join(clean)
        # Ren'Py escapes a literal opening bracket as ``[[``. The rendered
        # button child already contains ``[``, while ChoiceReturn metadata can
        # retain the escaped spelling.
        label = label.replace(u"[[", u"[")
        label = u" ".join(label.split())
        while label and label[0] in u"\u2022\u2023\u25e6\u2043":
            label = label[1:].strip()
        return label

    def _vnf_actable_scraped_choice_labels(buttons, fallback_choices=None):
        """Return rendered labels that may enter the enabled-choice merge.

        Disabled ChoiceReturn widgets are presence evidence for canonical
        disabled rows, never candidates for a new enabled row.
        """
        labels = [
            b.get("label", "") for b in (buttons or [])
            if ("ChoiceReturn" in b.get("actions", [])
                and not b.get("is_disabled"))
        ]
        return labels or fallback_choices or []

    def _vnf_unwrap_choice_value(value):
        """Unwrap nested ChoiceReturn-like values with a defensive bound."""
        for _unused in range(8):
            if not hasattr(value, "value"):
                break
            next_value = value.value
            if next_value is value:
                break
            value = next_value
        return value

    def _vnf_disabled_choice_is_rendered(choice, disabled_buttons):
        """Match a canonical disabled choice to its rendered widget.

        Labels cover ordinary and whitespace-only transforms. The private
        return value covers transforms that restyle or remove part of the
        caption, without letting that rendered label enter the enabled merge.
        """
        choice_key = _vnf_normalize_focus_label(choice.get("label", ""))
        has_value = ("_choice_value" in choice
                     and choice.get("_choice_value") is not None)
        choice_value = choice.get("_choice_value")
        for button in disabled_buttons or []:
            button_key = _vnf_normalize_focus_label(button.get("label", ""))
            if choice_key and choice_key == button_key:
                return True
            if not has_value:
                continue
            action_obj = button.get("_action_obj")
            actions = (action_obj if _vnf_is_sequence(action_obj)
                       else [action_obj])
            for action in actions:
                if (action is None
                        or type(action).__name__ != "ChoiceReturn"
                        or not hasattr(action, "value")):
                    continue
                try:
                    if choice_value == _vnf_unwrap_choice_value(action):
                        return True
                except Exception:
                    pass
        return False

    def _vnf_merge_scraped_choice_labels(choice_dicts, scraped_labels,
                                          choice_index_by_label,
                                          pipeline_owned_labels=None):
        """Append genuinely new focus-list choices using normalized keys.

        ``pipeline_owned_labels``: drawn labels whose ChoiceReturn value is
        already a value_map entry (see the ACTION IDENTITY note at the
        caller). Label equality is a heuristic that breaks whenever the
        RENDERED text diverges from the menu caption (screen transforms:
        cost styling, splits, prefixes); the action value is ground truth,
        so an owned label is never appended, however it reads. The
        augmenter's list stays the single source of truth for these — its
        caption is what act-by-label matches, and the ChoiceReturn value
        resolves the same widget regardless of the drawn text.
        """
        used = set(
            _vnf_normalize_focus_label(c.get("label", ""))
            for c in choice_dicts
            if _vnf_is_mapping(c)
        )
        for label in scraped_labels:
            key = _vnf_normalize_focus_label(label)
            if not label or not key or key in used:
                continue
            if pipeline_owned_labels and label in pipeline_owned_labels:
                # Visible trace on purpose: if a row ever goes missing
                # from an agent's list, the log must say identity
                # suppressed it (and for which label) rather than leave
                # a silent absence to bisect.
                _vnf_log("identity-dedup: scraped label owned by "
                         "pipeline, not appended: {!r}".format(label))
                used.add(key)
                continue
            value_index = choice_index_by_label.get(label)
            choice_dicts.append({
                "label": label,
                "index": value_index if value_index is not None
                         else len(choice_dicts) + 1,
                "caption": False,
                "disabled": False,
            })
            used.add(key)
        return choice_dicts

    def _vnf_push_game_state(all_data, per_screen, modal_screens,
                              overlay_active=False, inventory_stats=None):
        """Push a game_state event with post-transform interactions, stats, inventory.

        Called from the scraper when screen content changes or stabilizes.
        Combines active menu choices with screen buttons through the
        transform pipeline to produce the canonical interaction list.
        The bridge caches game_state (like screen_content) — clients read
        it for enriched data instead of the pending_request.

        When overlay_active is True, choices are excluded from the
        interaction list so that act() cannot resolve stale choices
        hidden behind an overlay screen (inventory, map, etc.).
        """
        if not _vnf_client or not _vnf_client.slot_id:
            return

        # Build choice list from active menu context (if any).
        # Skip when an overlay is active — choices are hidden behind it
        # and should not be resolvable via the shim act command.
        _ctx = _vnf_current_menu_context[0]
        _choice_dicts = []
        # Generic Ren'Py game menus are modal even when they were not
        # registered as vnflight overlays. Their controls own the actionable
        # surface; exposing the obscured request turns Return() into a phantom
        # story choice.
        _story_choices_masked = bool(overlay_active or modal_screens)
        _had_choice_context = bool(
            _ctx and not _vnf_request.is_input and not _story_choices_masked)
        if _ctx and not _vnf_request.is_input and not _story_choices_masked:
            # Canonical pipeline: trust the augmenter's output list
            # (_ctx["choices"]) as the source of truth for labels, order,
            # and synthesized items (e.g. class-gated choices).  Overlay
            # live is_sensitive on value_map entries to catch dynamic
            # disables (inventory consumption, vitality drain, etc.).
            _vmap = _ctx.get("value_map", {})
            _aug_choices = _ctx.get("choices", []) or []

            # Map scraped ChoiceReturn buttons to value_map keys.
            # For widgets already in value_map, record their existing
            # key.  For new widgets (live menu re-filter), extend
            # value_map.  Key by the button's post-transform label so
            # lookups from _scraped_labels (also post-transform) match —
            # walking focus_list for the raw label would miss
            # screen-transform prefixes like "[cost]" added by the mod.
            _new_cr_by_label = {}
            # ACTION IDENTITY (2026-08-18): drawn labels whose ChoiceReturn
            # value is already a value_map entry — they ARE pipeline
            # choices, however the screen renders them. The merge below
            # must never append these as new items: a screen transform
            # that restyles or splits a caption (live-reproduced with the
            # gutter-cost split — phantom cost-less duplicates) makes the
            # drawn label diverge from the menu caption, and label
            # equality alone then reads the same choice as a new one.
            _cr_pipeline_labels = set()
            try:
                _vmap_values = set(_vmap.values())
                _next_idx = max(_vmap.keys()) + 1 if _vmap else 1
                # Reverse lookup for existing entries.
                _val_to_key = {v: k for k, v in _vmap.items()}
                for _btn in all_data.get("buttons", []):
                    if "ChoiceReturn" not in _btn.get("actions", []):
                        continue
                    _act_obj = _btn.get("_action_obj")
                    if _act_obj is None:
                        continue
                    _acts = _act_obj if _vnf_is_sequence(_act_obj) else [_act_obj]
                    for _a in _acts:
                        if (type(_a).__name__ != "ChoiceReturn"
                                or not hasattr(_a, "value")):
                            continue
                        _lbl = _btn.get("label", "")
                        if _a.value is not None and _a.value in _vmap_values:
                            # Already mapped — record the label and mark
                            # it pipeline-owned for the merge. The None
                            # guard keeps a ChoiceReturn whose value
                            # extraction failed from claiming ownership
                            # of anything (None could sit in a map as a
                            # caption artifact; a failed read must
                            # degrade to label behavior, not hide rows).
                            _existing_key = _val_to_key.get(_a.value)
                            if _existing_key is not None and _lbl:
                                _new_cr_by_label[_lbl] = _existing_key
                            if _lbl:
                                _cr_pipeline_labels.add(_lbl)
                        else:
                            _gs = _a.get_sensitive() if hasattr(_a, "get_sensitive") else True
                            if not _gs:
                                break
                            _vmap[_next_idx] = _a.value
                            _vmap_values.add(_a.value)
                            _val_to_key[_a.value] = _next_idx
                            if _vnf_request.value_map is not None:
                                _vnf_request.value_map[_next_idx] = _a.value
                            if _lbl:
                                _new_cr_by_label[_lbl] = _next_idx
                            _next_idx += 1
                        break
            except Exception:
                pass

            # Live scraped ChoiceReturn labels — used to detect whether
            # each augmenter choice is currently rendered on screen.
            _scraped_labels = _vnf_actable_scraped_choice_labels(
                all_data.get("buttons", []), all_data.get("choices", []))
            _disabled_buttons = [
                b for b in all_data.get("buttons", [])
                if b.get("is_disabled") and b.get("label")
            ]

            for _c in _aug_choices:
                if not _vnf_is_mapping(_c):
                    continue
                _c_copy = dict(_c)
                _drop_cond = _c_copy.get("_drop_when_condition_false")
                if _drop_cond and _c_copy.get("disabled"):
                    try:
                        if not bool(renpy.python.py_eval(_drop_cond)):
                            continue
                    except Exception:
                        pass
                _lbl = _c_copy.get("label", "")
                _c_idx = _c_copy.get("index")
                # Live sensitivity check on real value_map entries.
                # Synthesized items (augmenter-added) have ast_idx ints
                # in value_map; is_sensitive on those always returns
                # True, so we trust the augmenter's disabled flag.
                if _c_idx is not None and _c_idx in _vmap:
                    try:
                        _live_sens = renpy.exports.is_sensitive(_vmap[_c_idx])
                    except Exception:
                        _live_sens = True
                    if not _live_sens and not _c_copy.get("disabled"):
                        _c_copy["disabled"] = True
                        _c_copy["index"] = None
                _choice_dicts.append(_c_copy)

            # Append any newly-detected ChoiceReturn values whose labels
            # aren't already in the augmenter's choice list.  Use
            # the value_map index from dynamic update so act(N) works.
            _choice_dicts = _vnf_merge_scraped_choice_labels(
                _choice_dicts, _scraped_labels, _new_cr_by_label,
                _cr_pipeline_labels)

            # Drop augmenter-disabled items Ren'Py is no longer
            # rendering.  When Ren'Py re-filters a menu (e.g. vitality
            # restored after drinking a potion removes the "too
            # exhausted" hint item), the ChoiceReturn widget disappears
            # from _scraped_labels.  We drop those stale augmenter
            # entries unless explicitly tagged _keep_stale (used for
            # class-gated hints the mod wants preserved).
            # DISABLED-INTERACTION CONTRACT (2026-08-18): a disabled item
            # that IS rendered must stay listed. Disabled widgets never
            # appear in _scraped_labels (that list is actable
            # ChoiceReturns), and a value-disabled menu item renders with
            # action None — so "rendered" for a disabled entry means a
            # scraped disabled button whose normalized label or private
            # ChoiceReturn identity matches.
            # Without this, Echoes' greyed one-shots (listed disabled by
            # the augmenter, drawn greyed by Ren'Py) were stale-dropped
            # from every game_state.
            if _scraped_labels or _disabled_buttons:
                _kept = []
                for _c in _choice_dicts:
                    if (_c.get("disabled")
                            and not _c.get("caption")
                            and not _vnf_disabled_choice_is_rendered(
                                _c, _disabled_buttons)
                            and not _c.get("_keep_stale")):
                        continue
                    _kept.append(_c)
                _choice_dicts = _kept

        _screen_buttons = all_data.get("buttons", [])

        # Run combined transform pipeline (choices + buttons together).
        _interactions = []
        _gs_buttons = []
        _gs_promoted = []
        _choice_ann_by_label = {}
        if _vnf_action_transforms:
            _at_actions, _at_ctx = _vnf_build_action_list(
                _choice_dicts, _screen_buttons, modal_screens)
            _at_actions = _vnf_apply_action_transforms(_at_actions, _at_ctx)
            _visible_choice_actions = [
                _ea for _ea in _at_actions
                if _ea.get("source") == "choice" and not _ea.get("hidden")
            ]
            _interactions = _vnf_build_interactions(_at_actions)
            # Collect annotations from choice-source actions so we can
            # propagate them to full_items below (attitudes, etc.).
            for _ea in _at_actions:
                if (_ea.get("source") == "choice"
                        and not _ea.get("hidden")
                        and _ea.get("annotation")):
                    _choice_ann_by_label[_ea.get("label", "")] = _ea["annotation"]
            for _ea in _at_actions:
                if _ea.get("hidden") or _ea.get("source") == "choice":
                    continue
                _eb = {
                    "label": _ea.get("label", ""),
                    "screen": _ea.get("screen", ""),
                    "actions": _ea.get("actions", []),
                    "is_selected": bool(_ea.get("is_selected")),
                    "is_disabled": bool(_ea.get("disabled") or _ea.get("is_disabled")),
                }
                if _ea.get("index") is not None:
                    _eb["index"] = _ea["index"]
                if _ea.get("action_strs"):
                    _eb["action_strs"] = _ea["action_strs"]
                if _ea.get("annotation"):
                    _eb["annotation"] = _ea["annotation"]
                if _ea.get("_suppress_pending_action"):
                    _eb["_suppress_pending_action"] = True
                if _ea.get("_category") is not None:
                    _eb["_category"] = _ea["_category"]
                    _eb["category"] = _ea["_category"]
                if _ea.get("promoted"):
                    _gs_promoted.append(_eb)
                else:
                    _gs_buttons.append(_eb)
            _choice_dicts = [
                {
                    "label": _ea.get("label", ""),
                    "index": _ea.get("choice_value_index"),
                    "disabled": bool(_ea.get("disabled")),
                    "caption": bool(_ea.get("caption")),
                    "annotation": _ea.get("annotation"),
                }
                for _ea in _visible_choice_actions
            ]
        else:
            if _choice_dicts or _screen_buttons:
                _at_actions, _at_ctx = _vnf_build_action_list(
                    _choice_dicts, _screen_buttons, modal_screens)
                _interactions = _vnf_build_interactions(_at_actions)
            _gs_buttons = [
                {"label": b.get("label", ""), "screen": b.get("screen", ""),
                 "actions": b.get("actions", []),
                 "is_selected": bool(b.get("is_selected")),
                 "is_disabled": b.get("is_disabled", False)}
                for b in _screen_buttons
            ]
            for _gb, _sb in zip(_gs_buttons, _screen_buttons):
                if _sb.get("index") is not None:
                    _gb["index"] = _sb["index"]
                if _sb.get("action_strs"):
                    _gb["action_strs"] = _sb["action_strs"]
                _cat = _sb.get("_category") or _sb.get("category")
                if _cat is not None:
                    _gb["_category"] = _cat
                    _gb["category"] = _cat

        # Build the game_state event.
        _gs = {"type": "game_state"}
        _gs_mod_cmds = _vnf_mod_command_names()
        if _gs_mod_cmds:
            _gs["custom_commands"] = _gs_mod_cmds
        if _vnf_button_categories:
            _gs["button_categories"] = dict(_vnf_button_categories)
        if _interactions:
            _gs["interactions"] = _interactions
        if _choice_dicts or _had_choice_context:
            _gs["choices"] = [c["label"] for c in _choice_dicts
                              if not c.get("caption") and not c.get("disabled")]
            _gs_full = []
            for c in _choice_dicts:
                _fi = {
                    "label": c["label"],
                    "is_disabled": c.get("disabled", False),
                    "is_caption": c.get("caption", False),
                }
                _ann = c.get("annotation") or _choice_ann_by_label.get(c.get("label", ""))
                if _ann:
                    _fi["annotation"] = _ann
                _gs_full.append(_fi)
            _gs["full_items"] = _gs_full
        _gs["screen_buttons"] = _gs_buttons
        if _gs_promoted:
            _gs["promoted_buttons"] = _gs_promoted
        # Same additive presentation hint as screen_content. game_state is the
        # canonical actionable surface, so the act/settle projections see the
        # modal open and close as a real surface change.
        _gs_modal_overlays = _vnf_visible_modal_overlay_tags(
            per_screen, _vnf_modal_overlay_screens)
        if _gs_modal_overlays:
            _gs["modal_overlay_screens"] = _gs_modal_overlays
        # Tag whether the single choice is currently scheduled for
        # auto-resolve.  Do not infer this only from the choice shape:
        # loop-guard fallbacks and load-time stale requests can expose a
        # single "(continue)" even though no resolver is pending.
        if _choice_dicts:
            _enabled = [c for c in _choice_dicts
                        if not c.get("disabled") and not c.get("caption")]
            if (len(_enabled) == 1
                    and vnf_player.auto_skip_single_choice
                    and _vnf_autoskip.resolve_value is not None
                    and not _vnf_autoskip.pause_reasons
                    and not _vnf_is_autoskip_loop(_enabled[0].get("label", ""))
                    and _vnf_auto_skip_predicate_allows(_enabled[0].get("label", ""))):
                _gs["_auto_advancing"] = True
        try:
            if inventory_stats is not None:
                _inv, _stats, _stats_ts = inventory_stats
            else:
                _inv, _stats = _vnf_get_inventory_stats()
                _stats_ts = _time.time()
            # Call-screen and overlay interactions can mutate state without
            # publishing a story menu. Share the same delta baseline used by
            # choice requests so their first stable game_state owns the
            # change instead of leaking it into a later action.
            _vnf_publish_inventory_stats(_inv, _stats)
            _gs["stats"] = _stats
            _gs["inventory"] = _inv
            # Source sampling time, not bridge receipt order: event POSTs use
            # independent threads and may arrive out of order under load.
            _gs["_stats_ts"] = _stats_ts
        except Exception:
            pass

        try:
            _gs["playback_config"] = dict(
                auto_advance=bool(getattr(vnf_player, "auto_advance", False)),
                auto_advance_delay=getattr(vnf_player, "auto_advance_delay", 0.3))
            _vnf_client.push_event(_gs)
            if vnf_player.debug:
                _vnf_log("Pushed game_state with %d interactions" % len(_interactions))
        except Exception:
            pass

    def _vnf_panel_text_tags(per_screen, modal_screens, overlay_tags,
                             passive_tags):
        """Screen tags whose text is a live snapshot, not a running log.

        A "panel" is a modal screen or a registered blocking overlay:
        an inventory/equipment/journal window whose body is redrawn from
        current state every time it is shown (item counts, conditional
        rows).  Passive overlays are excluded — those are the cumulative
        renderers (terminal scrollback, NVL-style logs) the delta filter
        exists for.
        """
        tags = set()
        for tag in (modal_screens or []):
            if tag:
                tags.add(tag)
        for scr_data in (per_screen or []):
            tag = scr_data.get("_tag")
            if not tag:
                continue
            if tag in overlay_tags and tag not in passive_tags:
                tags.add(tag)
        return tags

    def _vnf_scrape_emit_texts(texts, sources, prev_texts, panel_tags):
        """Choose the texts a screen_content event carries.

        Ordinary screen text is delta filtered against the previous
        scrape so cumulative renderers (NVL history, terminal scrollback)
        don't snowball.  Text belonging to a panel screen is kept even
        when it repeats: a panel re-opened with the same tile labels must
        still report its CURRENT body, otherwise the agent reads whatever
        happens to be new on the screens underneath and treats the panel
        as empty.

        `sources` is the screen tag per text, parallel to `texts`; it may
        be shorter (older callers), in which case the missing entries are
        treated as non-panel text.
        """
        keep = []
        for i, text in enumerate(texts):
            source = sources[i] if i < len(sources) else None
            if text not in prev_texts:
                keep.append(text)
            elif source is not None and source in panel_tags:
                keep.append(text)
        if prev_texts and keep:
            return keep
        return list(texts)

    def _vnf_scrape_text_pairs(texts, sources):
        """(screen tag, text) pairs for one scrape, kept for the next one.

        `sources` is the screen tag per text, parallel to `texts`; it may
        be shorter (older callers), and those entries pair with None.
        """
        pairs = []
        for i, text in enumerate(texts):
            source = sources[i] if i < len(sources) else None
            pairs.append((source, text))
        return pairs

    def _vnf_modal_delta_texts(texts, sources, prev_pairs, modal_tags):
        """New modal-screen text, for the (transcript) screen_text event.

        Only genuinely new lines are surfaced — screen_text lands in the
        transcript, so repeating the panel body every scrape would
        duplicate it — and only lines the modal itself drew: attributing
        the map/HUD text underneath a popup to the popup made a panel
        read as if it had said something it never showed.

        "New" is judged PER SOURCE SCREEN, against the previous scrape's
        (tag, text) pairs.  A global text comparison silently dropped a
        line the modal is showing for the first time whenever the same
        string had appeared ANYWHERE before — a popup that repeats a HUD
        field ("STATION STATUS") lost that line entirely.  An unchanged
        modal re-scrape still dedups, because its own pair repeats.

        Lines with no attribution (older callers, shorter `sources`) keep
        the previous behaviour: treated as the modal's and compared
        against every text seen last scrape.
        """
        out = []
        prev_texts = set(p[1] for p in prev_pairs)
        for i, text in enumerate(texts):
            source = sources[i] if i < len(sources) else None
            if source is None:
                if text not in prev_texts:
                    out.append(text)
                continue
            if source not in modal_tags:
                continue
            if (source, text) in prev_pairs:
                continue
            out.append(text)
        return out

    def _vnf_scrape_visible_screens(force=False):
        """Walk visible screens and push a screen_content event if new.

        Uses adaptive rate: scrapes every tick after recent actions,
        backs off when idle and content hasn't changed.
        """
        global _vnf_last_visible_scrape_hash, _vnf_last_shim_action_time
        global _vnf_last_scrape_time, _vnf_consecutive_unchanged_scrapes
        global _vnf_orphan_resync_last
        global _vnf_native_action_queue, _vnf_synthetic_input_req
        global _vnf_overlay_active_instance_tags

        if not vnf_player.enabled:
            return

        # A pre-render tree and the previous frame's focus list describe two
        # different moments. Publish neither an incomplete button surface nor
        # stale focus controls; the post-render timer captures them together.
        if not _vnf_focus_snapshot_is_current():
            return

        # Adaptive rate: skip scrapes when idle (opt-in).
        _now = _time.time()
        if vnf_player.adaptive_scrape and not force:
            _since_action = _now - _vnf_last_shim_action_time if _vnf_last_shim_action_time > 0 else 0
            _since_scrape = _now - _vnf_last_scrape_time if _vnf_last_scrape_time > 0 else 999

            # If user mouse is active, don't back off — they might be
            # hovering over UI elements that change screen state.
            _user_active = _vnf_mouse.is_user_active() if hasattr(_vnf_mouse, "is_user_active") else False
            if _since_action > 2.0 and _vnf_consecutive_unchanged_scrapes > 3 and not _user_active:
                # Idle: back off.  Allow one scrape every scrape_screens_delay
                # (default 0.5s, turbo 0.1s) scaled up by consecutive misses.
                _backoff = min(
                    vnf_player.scrape_screens_delay * (1 + _vnf_consecutive_unchanged_scrapes * 0.5),
                    3.0)
                if _since_scrape < _backoff:
                    return

        _vnf_last_scrape_time = _now

        showing = _vnf_get_showing_screens(vnf_player.scrape_visible_list)

        # Detect Ren'Py error/exception screen.
        # The exception screen runs in a separate context, so check both
        # visible screens AND the _vnf_exception_detected flag set by
        # the exception handler hook.
        _error_screens = {"_error_handling", "_exception"}
        _showing_tags = set(t for t, s in showing)
        _has_error = _vnf_exception.consume_detected(
            bool(_error_screens.intersection(_showing_tags)))
        if _has_error:
            if not _vnf_exception.error_notified:
                _vnf_exception.error_notified = True
                _vnf_fire_anomaly({
                    "type": "renpy_exception",
                    "message": "Ren'Py exception detected. The game may need Ignore/Reload.",
                })
                _vnf_log("ANOMALY: Ren'Py exception detected")
        # Remember for the error-screen action pump (periodic), which runs
        # queued native actions while the overlay timer is suppressed.
        _vnf_error_screen_visible[0] = bool(_has_error)
        # Clear transform-driven auto-skip pause for this scrape cycle.
        _vnf_autoskip.pause_reasons[:] = []

        per_screen = []
        _current_overlay_instance_tags = set(
            tag for tag, _scr in showing if tag in _vnf_overlay_screens)

        for tag, scr in showing:
            try:
                # Only force-build the tree if it hasn't been built yet.
                # Calling update() on an already-built screen during an
                # interact callback can disrupt layout state.
                if scr.child is None and hasattr(scr, "update"):
                    try:
                        scr.update()
                    except Exception:
                        pass
                _is_modal = getattr(scr, "modal", False)
                _screen_instance = None
                if tag in _vnf_overlay_screens:
                    _screen_instance = _vnf_overlay_instance_generation(tag, scr)
                scr_data = {"_tag": tag, "_screen_instance": _screen_instance, "modal": bool(_is_modal), "texts": [], "_text_sections": [], "_text_paths": [], "choices": [], "value_map": {}, "buttons": []}
                _sec_depth = _vnf_screen_section_depths.get(tag)
                _vnf_walk_screen(scr, scr_data, _section_depth=_sec_depth)
                # Tag each button with its source screen.
                for btn in scr_data["buttons"]:
                    btn["screen"] = tag
                per_screen.append(scr_data)
            except Exception:
                pass

        _vnf_overlay_active_instance_tags = _current_overlay_instance_tags

        # Run the transform pipeline on per-screen data (before merging).
        per_screen = _vnf_apply_screen_transforms(per_screen)

        # -----------------------------------------------------------
        # Synthetic input_request for screen-based Input widgets.
        # Games may use `call screen` with an Input widget instead of
        # renpy.input().  Detect this and push an input_request so
        # the agent can use input_text().
        # -----------------------------------------------------------
        _has_screen_input = False
        for _si_scr in per_screen:
            if _si_scr.get("_has_input_widget"):
                _has_screen_input = True
                # Only create the request once.
                if (_vnf_synthetic_input_req is None
                        and _vnf_request.request_id is None):
                    # Use the first text on the screen as the prompt.
                    _si_prompt = _si_scr["texts"][0] if _si_scr["texts"] else "Enter text"
                    _si_prompt = _vnf_transform_input_prompt(_si_prompt)
                    _si_req_id = _vnf_client.push_request(
                        "input_request",
                        prompt=_si_prompt,
                        default="",
                    )
                    _vnf_synthetic_input_req = _si_req_id
                    _vnf_set_active_input_request(_si_req_id)
                    _vnf_log("Synthetic input request {}: '{}'".format(
                        _si_req_id, _si_prompt))
                break
        # If the Input screen went away but we still have a pending
        # synthetic request, cancel it (e.g. user dismissed the screen).
        # On 6.x, the tree walker may miss Input widgets that aren't on the
        # screens layer — check focus_list as fallback before cancelling.
        if (not _has_screen_input and _is_renpy6
                and _vnf_synthetic_input_req is not None):
            try:
                for _fi in renpy.display.focus.focus_list:
                    _fw = getattr(_fi, "widget", None)
                    if _fw is not None and type(_fw).__name__ == "Input":
                        _has_screen_input = True
                        break
            except Exception:
                pass
        if (not _has_screen_input
                and _vnf_synthetic_input_req is not None
                and _vnf_request.request_id == _vnf_synthetic_input_req):
            _vnf_clear_active_request()
            _vnf_synthetic_input_req = None
            _vnf_log("Synthetic input request cancelled (screen dismissed)")

        # Merge surviving screens into flat structure for the event.
        all_data = {"texts": [], "choices": [], "value_map": {}, "buttons": []}
        _text_sources = []  # parallel to all_data["texts"]: screen tag per text
        categorized_texts = {}  # category -> [text, ...]
        scraped_screens = []
        modal_screens = []
        _overlay_active = False
        _active_overlays = []

        # Determine if we should suppress menu/navigation screen buttons.
        # Show them at main menu and game menu; hide during gameplay.
        _suppress_menu_buttons = False
        _at_mm = getattr(renpy.store, "main_menu", False)
        _showing_tags = set(t for t, s in showing)
        _generic_game_menu_active = bool(
            _vnf_is_generic_game_menu_showing())
        if _vnf_menu_screens:
            if not _at_mm:
                _in_game_menu = bool(_showing_tags.intersection(
                    {"game_menu", "save", "load", "preferences"}))
                # Ren'Py 7 games such as Roadwarden expose Preferences and
                # History through a generic ``menu`` wrapper. It is still the
                # active game menu: suppressing its buttons leaves a visible
                # modal that agents can only escape with Back.
                if _generic_game_menu_active:
                    _in_game_menu = True
                _suppress_menu_buttons = not _in_game_menu

        for scr_data in per_screen:
            _tag = scr_data["_tag"]
            scraped_screens.append(_tag)
            # Standard Ren'Py game menus are exposed through the generic
            # ``menu`` tag. Its wrapper can report modal=False even though it
            # owns input (live Preferences/Return reproduction, Aug 2026).
            _implicit_game_menu = (
                _tag == "menu"
                and not getattr(renpy.store, "main_menu", False)
            )
            if scr_data.get("modal") or _implicit_game_menu:
                modal_screens.append(_tag)
            if (_tag in _vnf_overlay_screens
                    and _tag not in _vnf_passive_overlay_screens):
                # Only treat as active overlay when the screen actually
                # has visible content — buttons, choices, or texts.
                # Ren'Py sometimes leaves stale screens in the layer
                # after the scene moves on (e.g. Roadwarden's 'selling'
                # persists after a shop closes).  A stale shell
                # shouldn't mask a live menu underneath.
                # Modal screens count even without content: they block
                # input by design, so the menu underneath shouldn't be
                # resolvable.
                _has_content = bool(
                    scr_data.get("buttons")
                    or scr_data.get("choices")
                    or scr_data.get("texts"))
                if _has_content or scr_data.get("modal"):
                    _overlay_active = True
                    _active_overlays.append(_tag)
            _tc = scr_data.get("_text_category")
            if _tc and scr_data["texts"]:
                categorized_texts.setdefault(_tc, []).extend(scr_data["texts"])
            else:
                for _t in scr_data["texts"]:
                    _text_sources.append(scr_data["_tag"])
                all_data["texts"].extend(scr_data["texts"])
            all_data["choices"].extend(scr_data["choices"])
            offset = len(all_data["value_map"])
            for k, v in scr_data["value_map"].items():
                all_data["value_map"][k + offset] = v
            if _suppress_menu_buttons and _tag in _vnf_menu_screens:
                pass  # skip buttons from menu screens during gameplay
            else:
                all_data["buttons"].extend(
                    b for b in scr_data["buttons"]
                    if not b.get("_hidden"))

        # Supplement with focus_list buttons when the screen layer has none.
        # This catches renpy.ui-built screens and image-map style widgets
        # (e.g. LLtQ's weekend map) that do not appear as active screens.
        def _vnf_get_button_label(w):
            """Extract text label from a Button widget's child tree."""
            btn_data = {"texts": [], "choices": [], "value_map": {}, "buttons": []}
            for attr in ("children", "child"):
                ch = getattr(w, attr, None)
                if ch is None:
                    continue
                if not _vnf_is_sequence(ch):
                    ch = [ch]
                for c in ch:
                    if c is not None:
                        _vnf_walk_screen(c, btn_data, depth=1)
                if btn_data["texts"]:
                    break
            return u" ".join(btn_data["texts"]).strip() if btn_data["texts"] else u""

        def _vnf_needs_focus_button_fallback(buttons, generic_menu_active=False):
            """Use focused controls when an opaque generic menu owns input."""
            if generic_menu_active:
                # A partly walkable menu can still have focus-only controls.
                # Merge them below with label dedup instead of treating one
                # scraped menu button as proof that the tree is complete.
                return True
            if not buttons:
                return True
            return False

        def _vnf_is_generic_menu_focus_screen(screen_name):
            """Return whether focus provenance belongs to a game menu."""
            # Missing provenance is deliberately rejected. Generic Ren'Py 7
            # wrappers may report modal=False, so the focus list can still
            # contain underlying quick-menu/HUD widgets.
            return screen_name in {
                "menu", "game_menu", "navigation", "preferences",
                "save", "load", "history", "archive",
            }

        def _vnf_focus_fallback_existing_labels(buttons, generic_menu_active=False):
            """Only menu-owned labels can suppress a generic-menu fallback."""
            if generic_menu_active:
                buttons = [
                    b for b in buttons
                    if _vnf_is_mapping(b) and b.get("screen") == "menu"
                ]
            return set(
                b.get("label", "") for b in buttons
                if _vnf_is_mapping(b)
            )

        if _vnf_needs_focus_button_fallback(
                all_data["buttons"], _generic_game_menu_active):
            _existing_labels = _vnf_focus_fallback_existing_labels(
                all_data["buttons"], _generic_game_menu_active)
            try:
                for _f in (renpy.display.focus.focus_list
                           if _vnf_focus_snapshot_is_current() else ()):
                    _w = getattr(_f, "widget", None)
                    if _w is None:
                        continue
                    _focus_screen = _vnf_get_focus_screen_name(_f)
                    if (_generic_game_menu_active
                            and not _vnf_is_generic_menu_focus_screen(
                                _focus_screen)):
                        continue
                    _act = getattr(_w, "action", None)
                    if _act is None:
                        _act = getattr(_w, "clicked", None)
                    try:
                        _focus_selected = bool(
                            renpy.display.behavior.is_selected(_act))
                    except Exception:
                        _focus_selected = False
                    _label = _vnf_get_button_label(_w)
                    if not _label:
                        _label = _vnf_action_label_hint(_act)
                    _label = _vnf_normalize_focus_label(_label)
                    if not _label or _label in _existing_labels:
                        continue
                    _existing_labels.add(_label)
                    _act_strs = []
                    _act_names = []
                    if _act is not None:
                        _acts = _act if _vnf_is_sequence(_act) else [_act]
                        for _a in _acts:
                            _a_cls = _a.__class__.__name__
                            _act_names.append(_a_cls)
                            _parts = [_a_cls]
                            _func = getattr(_a, "func", None)
                            _args = getattr(_a, "args", None)
                            if getattr(_func, "__name__", "") == "_returns" and _args:
                                _parts.append("value={}".format(_args[0]))
                            for _attr in ("property", "field", "variable",
                                          "screen", "value", "label"):
                                _v = getattr(_a, _attr, None)
                                if _v is not None:
                                    _parts.append("{}={}".format(_attr, _v))
                            _act_strs.append(" ".join(_parts))
                    _focus_button = {
                        "label": _label,
                        "is_selected": _focus_selected,
                        "actions": _act_names or ["none"],
                        "action_strs": _act_strs,
                        # A Ren'Py 7 generic game-menu wrapper can be opaque
                        # to the tree walker while its focused controls remain
                        # usable. Attribute that fallback to the modal owner so
                        # the modal action filter keeps it.
                        "screen": (
                            "menu" if _generic_game_menu_active
                            else "_focus_list"
                        ),
                        "index": len(all_data["buttons"]) + 1,
                        "_displayable": _w,
                        "_action_obj": _act,
                    }
                    if (set(_act_names).intersection({"ChoiceReturn", "Return"})
                            or (_label.startswith("[") and _label.endswith("]"))):
                        _focus_button["_category"] = "choices"
                        _focus_button["category"] = "choices"
                    all_data["buttons"].append(_focus_button)
            except Exception:
                pass

        _playback_sig = (bool(vnf_player.auto_advance), vnf_player.auto_advance_delay)
        _all_empty = not all_data["texts"] and not all_data["choices"] and not all_data["buttons"]
        if _all_empty:
            # Dedup like the normal path: without this, transitions /
            # movies / screenless pauses re-push a full game_state on
            # every poller tick (~5/s) and the unchanged-counter backoff
            # never engages.
            _empty_hash = ("__empty__", bool(_overlay_active),
                           tuple(sorted(modal_screens or [])), _playback_sig)
            if _vnf_last_visible_scrape_hash == _empty_hash and not force:
                _vnf_consecutive_unchanged_scrapes += 1
                return
            # Still push game_state once to clear stale screen_buttons
            # (e.g. main menu buttons lingering after game start), and
            # clear the canonical interaction cache so a stale act
            # can't click a button that's no longer on screen.
            _vnf_build_interactions([], set_global=True)
            _vnf_push_game_state(all_data, per_screen, modal_screens,
                                 overlay_active=_overlay_active)
            _vnf_last_visible_scrape_hash = _empty_hash
            _vnf_consecutive_unchanged_scrapes = 0
            return

        # Run action transforms on buttons (no menu choices here —
        # those are handled in _vnf_patched_menu).
        _scrape_interactions = []
        if _vnf_action_transforms and all_data["buttons"]:
            _at_actions, _at_ctx = _vnf_build_action_list(
                [],  # no choices in the scrape path
                all_data["buttons"],
                modal_screens,
            )
            _at_actions = _vnf_apply_action_transforms(_at_actions, _at_ctx)
            # Build interaction list for screen_content event only —
            # don't overwrite the canonical list (game_state sets that).
            _scrape_interactions = _vnf_build_interactions(_at_actions, set_global=False)
            # Rebuild button list from surviving actions.
            _rebuilt = []
            for a in _at_actions:
                if a["source"] != "button" or a.get("hidden"):
                    continue
                _rb = {
                    "label": a["label"],
                    "actions": a["actions"],
                    "action_strs": a.get("action_strs", []),
                    "screen": a.get("screen", ""),
                    "is_selected": bool(a.get("is_selected")),
                }
                if a.get("index") is not None:
                    _rb["index"] = a["index"]
                if a.get("annotation"):
                    _rb["annotation"] = a["annotation"]
                if a.get("_category") is not None:
                    _rb["_category"] = a["_category"]
                    _rb["category"] = a["_category"]
                if a.get("_wait_after_action") is not None:
                    _rb["_wait_after_action"] = bool(a["_wait_after_action"])
                if a.get("_story_entry") is not None:
                    _rb["_story_entry"] = bool(a["_story_entry"])
                if a.get("disabled") or a.get("is_disabled"):
                    _rb["is_disabled"] = True
                # Carry raw refs for screen-button action resolution.
                if a.get("_displayable") is not None:
                    _rb["_displayable"] = a["_displayable"]
                    _rb["_action_obj"] = a.get("_action_obj")
                _rebuilt.append(_rb)
            all_data["buttons"] = _rebuilt
        elif all_data["buttons"]:
            # No action transforms — build interactions directly.
            _at_actions, _at_ctx = _vnf_build_action_list(
                [], all_data["buttons"], modal_screens)
            _scrape_interactions = _vnf_build_interactions(_at_actions, set_global=False)

        # Deduplicate: only push when content actually changes.
        # Include action names in the hash so that button-state changes
        # (e.g. NullAction → real action when a save slot appears) are
        # detected even when labels stay the same. Selection-only changes
        # (Preferences toggles) must also reach the bridge's settle witness.
        btn_sigs = tuple(
            (b.get("label", ""), tuple(b.get("actions", [])),
             bool(b.get("is_selected")))
            for b in all_data["buttons"]
        )
        _scrape_inventory_stats = None
        _inventory_sig = ()
        _stats_sig = ()
        try:
            _scrape_inv, _scrape_stats = _vnf_get_inventory_stats()
            _scrape_inventory_stats = (
                _scrape_inv, _scrape_stats, _time.time())
            _inventory_sig = _vnf_signature_value(_scrape_inv)
            _stats_sig = _vnf_signature_value(_scrape_stats)
        except Exception:
            pass
        content_key = (
            tuple(all_data["texts"]),
            tuple(all_data["choices"]),
            btn_sigs,
            _inventory_sig,
            _stats_sig,
            _playback_sig,
            _vnf_autoskip.resolve_value is not None,
            bool(_at_mm),
        )
        content_hash = hash(content_key)
        if content_hash == _vnf_last_visible_scrape_hash:
            _vnf_consecutive_unchanged_scrapes += 1
            # Push game_state on first stable scrape (post-render refinement).
            if _vnf_consecutive_unchanged_scrapes == 1 or force:
                _vnf_push_game_state(all_data, per_screen, modal_screens,
                                     overlay_active=_overlay_active,
                                     inventory_stats=_scrape_inventory_stats)
            return
        _vnf_consecutive_unchanged_scrapes = 0
        _vnf_last_visible_scrape_hash = content_hash

        # Delta text: emit only texts not already in the previous scrape.
        # This prevents NVL-style cumulative text from snowballing — each
        # event contains only the NEW lines, not the full history.
        # Panel screens (modals + registered blocking overlays) are exempt:
        # their body is a snapshot of live state, so a panel re-opened with
        # the same tile labels must still report its CURRENT body instead of
        # collapsing to whatever is new on the screens underneath.
        _panel_tags = _vnf_panel_text_tags(
            per_screen, modal_screens,
            _vnf_overlay_screens, _vnf_passive_overlay_screens)
        _prev_pairs = set(_vnf_last_scrape_text_pairs)
        _prev_texts_set = set(p[1] for p in _vnf_last_scrape_text_pairs)
        # Use delta if we have previous data and the delta is non-empty;
        # otherwise fall back to full texts (e.g. first scrape or scene change).
        _emit_texts = _vnf_scrape_emit_texts(
            all_data["texts"], _text_sources, _prev_texts_set, _panel_tags)
        _vnf_last_scrape_text_pairs[:] = _vnf_scrape_text_pairs(
            all_data["texts"], _text_sources)

        # Detect user-initiated screen changes.
        global _vnf_last_scraped_screens
        _cur_screens = set(scraped_screens)
        _prev_screens = _vnf_last_scraped_screens
        _screens_changed = (_cur_screens != _prev_screens
                            and _prev_screens)
        _vnf_last_scraped_screens = _cur_screens

        _shim_age = _time.time() - _vnf_last_shim_action_time
        _user_screen_change = (
            _screens_changed
            and _shim_age > 1.0
            and _vnf_last_shim_action_time > 0
            and _vnf_mouse.is_user_active())

        ev = dict(
            type="screen_content",
            texts=_emit_texts,
            screens=scraped_screens,
            main_menu=bool(_at_mm),
        )
        if all_data["choices"]:
            ev["choices"] = all_data["choices"]
        if all_data["buttons"]:
            # Strip private keys: _displayable/_action_obj are live
            # Ren'Py objects — json falls back to str() on them from
            # the async push thread, shipping repr junk to the bridge.
            ev["buttons"] = [
                {k: v for k, v in b.items() if not k.startswith("_")}
                for b in all_data["buttons"]
            ]
        if _scrape_interactions:
            ev["interactions"] = [
                {k: v for k, v in itr.items()
                 if k not in ("choice_index",)}
                for itr in _scrape_interactions
            ]
        if modal_screens:
            ev["modal_screens"] = modal_screens
            ev["text_sources"] = _text_sources
            # Include friendly names for modal screens if registered.
            if _vnf_screen_display_names:
                _sdn = {}
                for _ms in modal_screens:
                    _dn = _vnf_screen_display_names.get(_ms)
                    if _dn:
                        _sdn[_ms] = _dn
                if _sdn:
                    ev["screen_names"] = _sdn
        # Include full registered-overlay text even for passive overlays. The
        # text channel and the input-blocking flag are deliberately independent.
        _ov_texts = []
        _ov_texts_by_screen = {}
        _ov_screens = []
        _ov_generations = {}
        # Retention is a registration contract, not a visibility property.
        # Keep hidden registered contributors explicit so a rollback can
        # preserve their delivered-row ownership until they reopen.
        _ov_retained = sorted(_vnf_retained_overlay_screens)
        for _osd in per_screen:
            if _osd["_tag"] in _vnf_overlay_screens:
                _ov_tag = _osd["_tag"]
                _osd_texts = _osd.get("texts", [])
                _ov_screens.append(_ov_tag)
                _ov_texts_by_screen[_ov_tag] = list(_osd_texts)
                if _osd_texts:
                    _ov_texts.extend(_osd_texts)
                if _osd.get("_overlay_generation") is not None:
                    _ov_generations[_ov_tag] = _vnf_text(
                        _osd["_overlay_generation"])
                elif (_ov_tag not in _vnf_retained_overlay_screens
                        and _osd.get("_screen_instance") is not None):
                    _ov_generations[_ov_tag] = "instance:" + _vnf_text(
                        _osd["_screen_instance"])
        if _ov_screens:
            ev["overlay_texts"] = _ov_texts
            ev["overlay_texts_by_screen"] = _ov_texts_by_screen
            # Consumers use this stable contributor list to distinguish panel
            # replacement from unrelated screen churn (notify, choice, HUD).
            ev["overlay_screens"] = _ov_screens
            if _ov_generations:
                ev["overlay_generations"] = _ov_generations
            # Additive presentation hint: which of the visible contributors
            # own the whole surface. Absent means "layer the rows over the
            # scene", which is what every unmodified mod still gets.
            _ov_modal = _vnf_visible_modal_overlay_tags(
                per_screen, _vnf_modal_overlay_screens)
            if _ov_modal:
                ev["modal_overlay_screens"] = _ov_modal
        if _ov_retained:
            ev["overlay_retained_screens"] = _ov_retained
        if _overlay_active:
            ev["overlay_active"] = True
            ev["active_overlays"] = _active_overlays
        if categorized_texts:
            ev["categorized_texts"] = categorized_texts
        if _user_screen_change:
            _added = _cur_screens - _prev_screens
            _removed = _prev_screens - _cur_screens
            ev["user_initiated"] = True
            if _added:
                ev["screens_added"] = sorted(_added)
            if _removed:
                ev["screens_removed"] = sorted(_removed)
            _vnf_log("User screen change: +{} -{}".format(
                list(_added) if _added else "[]",
                list(_removed) if _removed else "[]"))

        # Include button category registry so the formatter knows how
        # to render game-specific categories.
        if _vnf_button_categories:
            ev["button_categories"] = dict(_vnf_button_categories)

        # When scrape_screens is enabled, push full screen_content (texts + buttons).
        # Main-menu pages have no live story callback to supply their text.
        # Keep their text visible even when gameplay screen scraping is disabled.
        if vnf_player.scrape_screens or ev.get("main_menu", False):
            _vnf_client.push_event(ev)
        elif ev.get("buttons"):
            _vnf_client.push_event(dict(
                type="screen_content",
                texts=[],
                buttons=ev["buttons"],
                screens=ev.get("screens", []),
                main_menu=ev.get("main_menu", False),
                _buttons_only=True,
            ))

        # Modal screen text: push new text from modal screens (dialog,
        # confirm, etc.) as a transcript event so wait() can surface it.
        # screen_content is stateful (overwrite, not in transcript), so
        # without this the message text of call_screen popups is invisible.
        if modal_screens:
            _modal_delta = _vnf_modal_delta_texts(
                all_data["texts"], _text_sources, _prev_pairs,
                set(modal_screens))
            if _modal_delta:
                _vnf_client.push_event(dict(
                    type="screen_text",
                    texts=_modal_delta,
                    screens=list(modal_screens),
                ))

        # Push game_state with post-transform interactions, stats, inventory.
        _vnf_push_game_state(all_data, per_screen, modal_screens,
                             overlay_active=_overlay_active,
                             inventory_stats=_scrape_inventory_stats)

        # --- Orphan choice detection ---
        # If the scraper sees ChoiceReturn buttons but the shim has no
        # active request, the bridge lost the pending_request (desync).
        # Auto-resync by re-pushing the menu context.  The anomaly is
        # only surfaced when resync raises — transient orphans at
        # screen transitions (old choice resolved, new one about to be
        # pushed) self-heal and shouldn't alarm the dashboard.
        global _vnf_orphan_skip_next
        if _vnf_orphan_skip_next:
            # Previous tick fired resync; skip this one so Menu.execute
            # for the incoming menu has a chance to finalize the new
            # request_id before we re-evaluate.
            _vnf_orphan_skip_next = False
        else:
            _has_choice_btns = any(
                "ChoiceReturn" in b.get("actions", [])
                for b in all_data["buttons"])
            if (_has_choice_btns
                    and _vnf_request.request_id is None
                    and _vnf_deferred_choice[0] is None
                    and _vnf_current_menu_context[0] is not None):
                _orphan_now = _time.time()
                if _orphan_now - _vnf_orphan_resync_last > 2.0:
                    _vnf_orphan_resync_last = _orphan_now
                    _vnf_orphan_skip_next = True
                    _vnf_log("Orphan choice detected — auto-resync")
                    try:
                        _vnf_cmd_resync("resync", None)
                    except Exception as _oe:
                        _vnf_log("Auto-resync failed: %s" % (
                            _vnf_text(_oe),))
                        _vnf_fire_anomaly({
                            "type": "orphaned_choice_screen",
                            "message": "auto-resync failed: {}".format(
                                _vnf_text(_oe))})

        if vnf_player.debug:
            _vnf_log("Visible screen scrape: {} texts, {} choices, {} buttons from {}".format(
                len(all_data["texts"]), len(all_data["choices"]),
                len(all_data["buttons"]), scraped_screens))

    def _vnf_visible_scrape_interact_callback():
        """Interact callback — scrape visible screens for text content."""
        try:
            # This callback is registered before the rollback observer below.
            # Observe here too so game_resumed reaches the bridge before the
            # first restored screen snapshot can look like fresh output.
            _vnf_observe_rollback_resume()
            _vnf_scrape_visible_screens()
        except Exception:
            if vnf_player.debug:
                _tb_module.print_exc()

    if not _is_renpy6:
        if _vnf_visible_scrape_interact_callback not in renpy.config.interact_callbacks:
            renpy.config.interact_callbacks.append(_vnf_visible_scrape_interact_callback)

    # -------------------------------------------------------------------------
    # 9. Game Lifecycle -- start / end detection
    # -------------------------------------------------------------------------

    def _vnf_quit_callback():
        """Called when the game quits."""
        if vnf_player.enabled:
            _vnf_log("Game quit detected.")
            _vnf_client.notify_game_ended(reason="quit")

    if hasattr(renpy.config, "quit_callbacks"):
        if _vnf_quit_callback not in renpy.config.quit_callbacks:
            renpy.config.quit_callbacks.append(_vnf_quit_callback)

    # After-load state sync: when a save is loaded, the bridge is reset
    # but the game resumes mid-interaction.  Push a screen scrape so the
    # client sees the current state (buttons, text) immediately.
    def _vnf_after_load_callback():
        if not vnf_player.enabled:
            return
        global _vnf_last_visible_scrape_hash
        if hasattr(vnf_player, "auto_advance"):
            if vnf_player.auto_advance:
                _vnf_enable_auto_advance()
            else:
                _vnf_disable_auto_advance()
        _vnf_mouse.sync()
        _vnf_commit_pending_menu_caption()
        _vnf_baseline_nvl_capture_state()
        _vnf_reset_overlay_instance_generations()
        _vnf_log("After-load sync: scraping visible screens")
        # Native UI loads do not pass through _vnf_cmd_load and therefore do
        # not reset the bridge. Explicitly retire any terminal latch and stale
        # state from the abandoned timeline while preserving its transcript.
        _vnf_client.push_event_sync(dict(
            type="game_resumed", reason="load", timestamp=_time.time()))
        # The loaded statement re-executes with renpy.game.after_rollback set
        # (unfreeze rolls back onto the restored checkpoint), so the story
        # entry observers would otherwise report a second, spurious rollback
        # resume on top of this load resume. The first post-load interaction
        # clears the flag and re-arms the edge for genuine rollbacks.
        _vnf_rollback_resume_seen[0] = True
        try:
            if _is_renpy6 and bool(_sys_mod._vnf_rollback_pending[0]):
                _sys_mod._vnf_rollback_pending[0] = False
        except Exception:
            pass
        # Clear scrape hash so the scrape isn't deduped against
        # whatever was on screen before the load.
        _vnf_last_visible_scrape_hash = None
        _vnf_last_scrape_text_pairs[:] = []
        # Push inventory/stats so the client has full context.
        try:
            inventory, stats = _vnf_get_inventory_stats()
            if inventory:
                _vnf_client.push_event(dict(type="inventory_update", inventory=inventory))
            if stats:
                _vnf_client.push_event(dict(type="stats_update", stats=stats, _ts=_time.time()))
        except Exception:
            pass
        # Scrape screens on the next interact (can't scrape immediately
        # because the display tree may not be built yet after load).
        # We rely on the interact callback already registered.

    if hasattr(renpy.config, "after_load_callbacks"):
        if _vnf_after_load_callback not in renpy.config.after_load_callbacks:
            renpy.config.after_load_callbacks.append(_vnf_after_load_callback)
    elif not _is_renpy6:
        renpy.config.after_load_callbacks = [_vnf_after_load_callback]

    # Ren'Py 6 clears game.after_rollback before periodic callbacks begin.
    # Capture the successful rollback's restart exception at its public entry
    # point, then let the first watcher tick baseline the restored NVL page.
    if (not hasattr(_sys_mod, "_vnf_rollback_pending")
            or type(_sys_mod._vnf_rollback_pending)
            is not _VNF_NATIVE_LIST_TYPE):
        # A list literal in store code is a RevertableList. Keeping that on
        # sys does not help: rollback still reverts its mutation before the
        # periodic watcher can observe it. Copy from a tuple into the native
        # JSON-list type captured before Ren'Py shadows ``list``.
        _sys_mod._vnf_rollback_pending = _VNF_NATIVE_LIST_TYPE((False,))
    _vnf_rollback_pending = _sys_mod._vnf_rollback_pending
    _vnf_original_rollback = _vnf_save_original(
        "rollback", renpy.exports, "rollback")
    _vnf_previous_rollback_wrapper = getattr(
        _sys_mod, "_vnf_rollback_wrapper", None)

    def _vnf_rollback_wrapper(*args, **kwargs):
        try:
            return _vnf_original_rollback(*args, **kwargs)
        except BaseException:
            if (_is_renpy6
                    and bool(getattr(renpy.game, "after_rollback", False))):
                _sys_mod._vnf_rollback_pending[0] = True
            raise

    _vnf_rollback_wrapper._vnf_owned_rollback_wrapper = True
    _sys_mod._vnf_rollback_wrapper = _vnf_rollback_wrapper

    def _vnf_patch_renpy6_rollback_keymaps():
        if not _is_renpy6:
            return
        try:
            _underlays = getattr(renpy.config, "underlay", ())
        except Exception:
            return
        for _underlay in _underlays:
            try:
                _keymap = getattr(_underlay, "keymap", None)
                if not _vnf_is_mapping(_keymap):
                    continue
                _action = _keymap.get("rollback")
                if (_action is _vnf_original_rollback
                        or _action is _vnf_previous_rollback_wrapper
                        or bool(getattr(
                            _action, "_vnf_owned_rollback_wrapper", False))):
                    _keymap["rollback"] = _vnf_rollback_wrapper
            except Exception:
                continue

    renpy.exports.rollback = _vnf_rollback_wrapper
    renpy.rollback = _vnf_rollback_wrapper
    _vnf_patch_renpy6_rollback_keymaps()

    def _vnf_finish_rollback_resume():
        _vnf_mouse.sync()
        _vnf_commit_pending_menu_caption()
        _vnf_baseline_nvl_capture_state()
        _vnf_reset_overlay_instance_generations()
        _vnf_client.push_event_sync(dict(
            type="game_resumed", reason="rollback", timestamp=_time.time()))

    # Ren'Py has no public after-rollback callback, but exposes this flag until
    # the first post-rollback interaction is assembled. Observe it there so
    # both the native Back action and vnflight's rollback command retire a
    # terminal latch. The edge guard prevents duplicate events if Ren'Py
    # restarts that interaction before clearing the flag.
    _vnf_rollback_resume_seen = [False]
    def _vnf_observe_rollback_resume():
        if not vnf_player.enabled:
            return
        if _is_renpy6:
            try:
                if bool(_sys_mod._vnf_rollback_pending[0]):
                    _vnf_rollback_resume_seen[0] = True
                    _vnf_finish_rollback_resume()
                    _sys_mod._vnf_rollback_pending[0] = False
                    return
            except Exception:
                pass
        try:
            after_rollback = bool(getattr(renpy.game, "after_rollback", False))
        except Exception:
            after_rollback = False
        if after_rollback and not _vnf_rollback_resume_seen[0]:
            _vnf_rollback_resume_seen[0] = True
            _vnf_finish_rollback_resume()
        elif not after_rollback:
            _vnf_rollback_resume_seen[0] = False

    def _vnf_rollback_resume_interact_callback():
        _vnf_observe_rollback_resume()

    if not _is_renpy6:
        if (_vnf_rollback_resume_interact_callback
                not in renpy.config.interact_callbacks):
            renpy.config.interact_callbacks.append(
                _vnf_rollback_resume_interact_callback)

    # Also register an atexit handler as a safety net.
    def _vnf_atexit():
        try:
            if vnf_player.enabled:
                _vnf_client.notify_game_ended(reason="process_exit")
        except Exception:
            pass

    atexit.register(_vnf_atexit)

    # -------------------------------------------------------------------------
    # 10. Notify bridge that the mod is loaded
    # -------------------------------------------------------------------------

    if vnf_player.enabled:
        # Request a slot from the bridge for this game instance.
        # Must happen before reset_bridge() so the reset targets our slot.
        _vnf_slot_assigned = _vnf_client.assign_slot(
            disable_on_failure=True,
            retry_window=(8.0 if vnf_player._launch_id else 0.0))

        if _vnf_slot_assigned:
            # Reset the bridge to clear any stale state (pending commands,
            # actions, transcript) from a previous game cycle.  Without this
            # the game can pick up old "start" or "auto_advance_on" commands
            # left over from a prior run and enter a restart loop.
            _vnf_client.reset_bridge()
            _vnf_clear_active_request()
            _vnf_pending_vis_check[0] = None
            _vnf_afm_cleared[0] = False
            _vnf_afm_intentionally_off[0] = False
            _vnf_last_scrape_text_pairs[:] = []
            _vnf_pending_command_box[0] = None

            _vnf_client.push_event_sync(dict(
                type="mod_loaded",
                pid=_os.getpid(),
                config=dict(
                    auto_advance=vnf_player.auto_advance,
                    auto_advance_delay=vnf_player.auto_advance_delay,
                    allow_user_override=vnf_player.allow_user_override,
                    screenshot_enabled=vnf_player.screenshot_enabled,
                ),
            ))
            _vnf_log("LLM Player mod loaded (bridge reset). Bridge: " + vnf_player.bridge_url)

        # Start the background command poll thread.
        # On Ren'Py 6.x, skip the background thread — the init 999
        # periodic callback handles polling and execution inline,
        # avoiding race conditions with command consumption.
            if not _is_renpy6:
                _vnf_command_poll_stop = False
            # Rotate the generation token so a worker from a previous
            # init pass (bridge reset / Shift+R) exits its loop.
                _vnf_command_poll_gen[0] = object()
                _vnf_command_poll_thread = _threading.Thread(
                    target=_vnf_command_poll_worker)
                _vnf_command_poll_thread.daemon = True
                _vnf_command_poll_thread.start()
                _vnf_log("Command poll background thread started.")
            else:
                _vnf_log("Ren'Py 6.x: using periodic polling (no background thread).")

    # -------------------------------------------------------------------------
    # Done with hook installation!
    # -------------------------------------------------------------------------

## ============================================================================
## Game Start Hook -- notify bridge when the game actually starts
## ============================================================================

init 999 python:
    def _vnf_start_callback():
        if vnf_player.enabled:
            _vnf_commit_pending_menu_caption()
            _vnf_reset_nvl_capture_state()
            # Unlike nvl_clear, a game start cannot still owe the callback for
            # a just-drained say. Retire inverse ownership from the prior run.
            _vnf_nvl_prepublished_callbacks[:] = []
            _vnf_reset_overlay_instance_generations()
            # A new run has visited no labels.  Left alone, the previous
            # run's ending label kept its game_terminal progress node
            # "visited", every scrape of the new game reported terminal, and
            # the bridge re-latched its end-of-run freeze: the footer showed
            # the old run's final stats for the whole next playthrough
            # (rw70-sonnet, Sep 6).  Ren'Py resets the store on a new game;
            # this history is ours to reset.
            _vnf_label_history[:] = []
            # The store clean before _start reset the engaged flag while
            # the agent's own setting (an attribute on vnf_player)
            # survived, so auto-advance was silently off after ending ->
            # New Game while state still reported it on.  The setting is
            # the agent's explicit choice: re-engage it.
            if getattr(vnf_player, "auto_advance", False) and not _vnf_auto_advance_active:
                _vnf_enable_auto_advance()
            game_name = getattr(renpy.config, "name", None)
            if game_name:
                try:
                    game_name = renpy.substitutions.substitute(_vnf_text(game_name))[0]
                except Exception:
                    game_name = _vnf_text(game_name)
            game_name = game_name or "Unknown"
            _vnf_client.push_event_sync(dict(
                type="game_started",
                game_name=game_name,
                version=getattr(renpy.config, "version", ""),
            ))
            _vnf_log("Game started: " + _vnf_text(game_name))

            # NOTE: We do NOT auto-enable auto-advance here.  The splash
            # screen and early game setup may still be running, and
            # auto-advancing would dismiss Pause() interactions and
            # cause the game to cycle.  Instead, the external client
            # should send the "auto_advance_on" command when it is
            # ready to start advancing dialogue.

    if not _is_renpy6:
        if hasattr(renpy.config, "start_callbacks"):
            if _vnf_start_callback not in renpy.config.start_callbacks:
                renpy.config.start_callbacks.append(_vnf_start_callback)
        else:
            # Fallback: just push on first interact.
            _vnf_game_started_sent = [False]
            _orig_interact_cb = _vnf_interact_callback
            def _vnf_interact_with_start():
                if not _vnf_game_started_sent[0] and vnf_player.enabled:
                    _vnf_game_started_sent[0] = True
                    _vnf_start_callback()
                _orig_interact_cb()
            try:
                idx = renpy.config.interact_callbacks.index(_vnf_interact_callback)
                renpy.config.interact_callbacks[idx] = _vnf_interact_with_start
            except ValueError:
                renpy.config.interact_callbacks.append(_vnf_interact_with_start)

## ============================================================================
## Command Poller Screen -- runs as an overlay on every screen
## ============================================================================
##
## Commands (start, save, load, rollback, quit) are executed from a screen
## timer action.  This is essential because actions like Start() raise
## JumpOutException, which only propagates correctly when raised inside
## the screen / interaction event handling path -- NOT from
## interact_callbacks.
##
## The timer is CONDITIONAL: it only exists when a background thread has
## found a pending command.  This avoids the repeated restart_interaction()
## calls that a repeating timer would cause every poll cycle, which was
## the root cause of focus flickering in games like Slay the Princess.

init 999 python:
    def _vnf_observation_timer_action(callback):
        action = Function(callback)
        # 6.99.4 Function does not restart and forwards all kwargs to callback.
        # Later engines expose this flag; disable only their implicit restart.
        if hasattr(action, "update_screens"):
            action.update_screens = False
        return action

screen vnf_command_poller():
    zorder 999
    if (_vnf_pending_command_box[0] is not None
            and not _vnf_client.has_pending_critical_events()):
        timer 0.01 action Function(_vnf_execute_pending_command)
    if _vnf_native_action_queue is not None:
        timer 0.01 action Function(_vnf_execute_native_action_once)
    if vnf_player.enabled:
        timer 0.2 action _vnf_observation_timer_action(_vnf_poller_scrape_tick) repeat True
    if vnf_player.enabled and vnf_player.nvl_auto_scroll:
        # The scroll tick explicitly restarts only when it changes position.
        timer 0.1 action _vnf_observation_timer_action(_vnf_nvl_auto_scroll_tick) repeat True

## ============================================================================
## Screen overlay -- optional debug display showing bridge status
## ============================================================================

screen vnf_player_debug():
    zorder 1000
    if vnf_player.enabled and vnf_player.debug:
        frame:
            xalign 1.0
            yalign 0.0
            xpadding 8
            ypadding 4
            background "#00000088"
            has vbox spacing 2
            text "LLM Player" size 12 color "#88ff88"
            text "Bridge: [vnf_player.bridge_url]" size 10 color "#aaaaaa"
            text "Auto-advance: [vnf_player.auto_advance]" size 10 color "#aaaaaa"
            text "Hybrid: [vnf_player.allow_user_override]" size 10 color "#aaaaaa"

init 999 python:
    # Use always_shown_screens for the command poller so it is active
    # at the main menu, game menu, and during gameplay — not just when
    # overlays are enabled.
    if not vnf_player.enabled:
        pass
    elif _is_renpy6:
        # Ren'Py 6.x: overlay_screens exists but the vnf_command_poller
        # screen doesn't work reliably as an overlay at the main menu.
        # Fall through to the periodic callback path below.
        pass
    elif hasattr(renpy.config, "always_shown_screens"):
        if "vnf_command_poller" not in renpy.config.always_shown_screens:
            renpy.config.always_shown_screens.append("vnf_command_poller")
    elif hasattr(renpy.config, "overlay_screens"):
        # Ren'Py 7.x without always_shown_screens.
        if "vnf_command_poller" not in renpy.config.overlay_screens:
            renpy.config.overlay_screens.append("vnf_command_poller")

    if _is_renpy6:
        # Ren'Py 6.x: overlay_functions can't show_screen reliably,
        # and the background poll thread's urllib2 calls block the GIL.
        # Poll commands inline from a periodic callback instead.
        _vnf_6x_last_poll = [0.0]
        def _vnf_6x_poll_and_execute():
            # _vnf_synthetic_input_req must be global: it's assigned
            # below, and without the declaration the read raises
            # UnboundLocalError — leaving the synthetic request id
            # stale so 6.x games get only one input request per session.
            global _vnf_synthetic_input_req
            _now = _time.time()
            if not (_vnf_client and _vnf_client.slot_id):
                return
            # Poll bridge for commands (rate-limited).
            # Use very short timeout to avoid blocking the
            # main thread — urllib2 on Ren'Py 6.x blocks the GIL.
            if (_vnf_pending_command_box[0] is None
                    and _now - _vnf_6x_last_poll[0] > 0.2
                    and not _vnf_client.has_pending_critical_events()):
                _vnf_6x_last_poll[0] = _now
                try:
                    url = _vnf_client._url("/command")
                    req = _urllib_request.Request(
                        url, None,
                        _vnf_client._auth_headers(with_content_type=False))
                    resp = _urllib_request.urlopen(req, timeout=0.5)
                    data = json.loads(resp.read().decode("utf-8"))
                    if data and data.get("command") is not None:
                        _vnf_pending_command_box[0] = data["command"]
                except Exception:
                    pass
                # Also poll for actions (input/choice) inline.
                # The background poll_worker thread is disabled on 6.x
                # (GIL contention causes urllib2 to consume bridge actions
                # without delivering them to the shim).
                if (_vnf_request.request_id is not None
                        and _vnf_request.received_action is None):
                    try:
                        _act_url = _vnf_client._url("/action")
                        _act_url += "?" + _urllib_parse.urlencode(
                            {"request_id": _vnf_request.request_id})
                        _act_req = _urllib_request.Request(
                            _act_url, None,
                            _vnf_client._auth_headers(with_content_type=False))
                        _act_resp = _urllib_request.urlopen(_act_req, timeout=0.5)
                        _act_data = json.loads(_act_resp.read().decode("utf-8"))
                        if _act_data and _act_data.get("action") is not None:
                            _act = _act_data["action"]
                            _vnf_log("6.x inline action: got %s" % _act.get("type", "?"))
                            # For inputs on 6.x: set the Input widget text
                            # and inject a Return keypress via pygame.
                            # end_interaction doesn't propagate from periodic
                            # callbacks on 6.x, but pygame events are processed
                            # by the main event loop naturally.
                            if _vnf_request.is_input:
                                _in_text = _act.get("text", "")
                                import pygame
                                # Type each character via pygame KEYDOWN
                                # events — the Input widget processes them
                                # naturally through the event loop.
                                for _ch in _in_text:
                                    pygame.event.post(pygame.event.Event(
                                        pygame.KEYDOWN, key=0,
                                        mod=0, unicode=_ch, scancode=0,
                                        repeat=0))
                                # Submit with Return.
                                pygame.event.post(pygame.event.Event(
                                    pygame.KEYDOWN, key=pygame.K_RETURN,
                                    mod=0, unicode=u"\r", scancode=0,
                                    repeat=0))
                                _vnf_request.received_action = None
                                _vnf_request.request_id = None
                                _vnf_request.state = _VNFRequestState.IDLE
                                if _vnf_synthetic_input_req is not None:
                                    _vnf_synthetic_input_req = None
                                _vnf_log("6.x input: typed '%s' + Return via pygame" % _in_text)
                            else:
                                _vnf_request.received_action = _act
                                _vnf_request.state = _VNFRequestState.RECEIVED
                    except Exception:
                        pass
            # Execute pending command.
            if not _vnf_client.has_pending_critical_events():
                _vnf_execute_pending_command()
        renpy.config.periodic_callbacks.append(_vnf_6x_poll_and_execute)
        renpy.config.periodic_callbacks.append(_vnf_execute_native_action_once)

        # Screen scraping + context detection as periodic callbacks.
        # On 7.x/8.x these run via interact_callbacks or the overlay screen,
        # but on 6.x we use periodic callbacks instead.
        _vnf_6x_scrape_last = [0.0]
        def _vnf_6x_scrape_tick():
            _now = _time.time()
            if _now - _vnf_6x_scrape_last[0] < 0.3:
                return
            _vnf_6x_scrape_last[0] = _now
            try:
                _vnf_scrape_visible_screens()
            except Exception:
                pass

        _vnf_6x_ctx_last = [0.0]
        def _vnf_6x_context_tick():
            _now = _time.time()
            if _now - _vnf_6x_ctx_last[0] < 0.5:
                return
            _vnf_6x_ctx_last[0] = _now
            try:
                _vnf_detect_context()
            except Exception:
                pass

        _vnf_6x_screenshot_last = [0.0]
        def _vnf_6x_screenshot_tick():
            if not (vnf_player.enabled and vnf_player.screenshot_enabled):
                return
            _now = _time.time()
            if _now - _vnf_6x_screenshot_last[0] < 2.0:
                return
            _vnf_6x_screenshot_last[0] = _now
            try:
                _vnf_capture_screenshot()
            except Exception:
                pass

        renpy.config.periodic_callbacks.append(_vnf_6x_scrape_tick)
        renpy.config.periodic_callbacks.append(_vnf_6x_context_tick)
        renpy.config.periodic_callbacks.append(_vnf_6x_screenshot_tick)
        _vnf_log("Ren'Py 6.x: registered periodic polling, scraping, context, screenshots.")

    def _vnf_overlay():
        if not vnf_player.enabled:
            return
        # ``always_shown_screens`` is the primary registration path, but a
        # modern Ren'Py main menu can finish its first interaction without
        # instantiating a screen appended during init.  The command worker
        # then holds its first command while the missing screen cannot drain
        # its native-action queue, run timer dispatch, or scrape visible screens.
        # Overlay functions run while that interaction is assembled, so make
        # the registration self-healing and idempotent here as well.
        if renpy.get_screen("vnf_command_poller") is None:
            renpy.exports.show_screen("vnf_command_poller")
        if vnf_player.debug:
            renpy.exports.show_screen("vnf_player_debug")

    if not _is_renpy6:
        if _vnf_overlay not in renpy.config.overlay_functions:
            renpy.config.overlay_functions.append(_vnf_overlay)
