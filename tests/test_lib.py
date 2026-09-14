"""Tests for vnflight.lib helpers."""

import json
import os
import stat
import sys
import time

import pytest


def test_auto_advance_does_not_mirror_unconfirmed_game_change(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://bridge")
    monkeypatch.setattr(client, "command", lambda *a, **kw: {
        "success": False, "acceptance_unknown": True, "nonce": "retry-me"})
    def forbidden(*args, **kwargs):
        raise AssertionError("unconfirmed game change must not update metadata")
    monkeypatch.setattr(client, "set_config", forbidden)
    result = client.set_auto_advance(True)
    assert result["ok"] is False
    assert result["acceptance_unknown"] is True
    assert result["nonce"] == "retry-me"


@pytest.mark.parametrize("older_type", ["dialogue", "progress_change"])
@pytest.mark.parametrize("handback", [False, True])
def test_auto_receipt_returns_held_predecessor_before_its_menu(monkeypatch, older_type, handback):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://bridge", slot_prefix="/1")
    older = {"type": older_type, "text": "Older", "action_id": 75, "_seq": 10}
    current = {"type": "dialogue", "text": "Reply", "action_id": 76, "_seq": 20}
    client._track_action_nonce("current", 76)

    def get(path, params=None, timeout=3):
        if path == "/state":
            return 200, {"transcript": [older, current], "event_counter": 20}
        return 200, {"transaction": {
            "action_nonce": "current", "action_id": 76,
            "transaction_state": "settled", "events": [current], "delivery_end": 1,
            "settled_pending": {"id": "next", "type": "choice_request", "choices": ["Continue"]},
        }}

    monkeypatch.setattr(client, "_get", get)
    kwargs = {"action_nonce": "current", "include_unowned_prefetch": True} if handback else {}
    result = client.wait(timeout=1, **kwargs)
    assert result.events == [older, current]
    assert result.pending["id"] == "next"
    assert client._prefetched_events == []
    assert client.wait(timeout=1, action_nonce="current").events == []


def test_auto_wait_prefix_failure_keeps_receipt_retryable(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://bridge")
    client._track_action_nonce("current", 2)
    opening = {"type": "narration", "text": "Opening", "_seq": 10}
    current = {"type": "narration", "text": "Current", "_seq": 20, "action_id": 2}
    available = [False]
    acks = []

    def get(path, params=None, timeout=3):
        assert 0 < timeout <= 3
        if path == "/state":
            return (200, {"transcript": [opening, current], "event_counter": 20}) if available[0] else (0, None)
        assert path == "/transaction"
        if (params or {}).get("ack"):
            acks.append(params["ack"])
        return 200, {"transaction": {
            "action_nonce": "current", "action_id": 2,
            "transaction_state": "settled", "events": [current],
            "delivery_end": 1,
        }}

    monkeypatch.setattr(client, "_get", get)
    failed = client.wait(timeout=1)
    assert failed.events == []
    assert failed.transaction["reason"] == "story_prefix_unavailable"
    assert not acks
    assert "current" in client._active_action_nonces
    available[0] = True
    recovered = client.wait(timeout=1)
    assert recovered.events == [opening, current]
    assert acks == ["1"]


def test_explicit_nonce_wait_does_not_fetch_ordinary_prefix(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://bridge")
    current = {"type": "narration", "text": "Current", "_seq": 20, "action_id": 2}

    def get(path, **kwargs):
        assert path == "/transaction"
        return 200, {"transaction": {
            "action_nonce": "current", "action_id": 2,
            "transaction_state": "settled", "events": [current],
        }}

    monkeypatch.setattr(client, "_get", get)
    assert client.wait(timeout=1, action_nonce="current").events == [current]


def test_prefix_read_reset_does_not_publish_old_transaction(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://bridge")
    client._track_action_nonce("old", 2)
    old = {"type": "narration", "text": "Abandoned timeline", "_seq": 20, "action_id": 2}

    def get(path, **kwargs):
        if path == "/state":
            return 200, {"transcript": [], "event_counter": 30, "reset_generation": 2}
        return 200, {"transaction": {
            "action_nonce": "old", "action_id": 2, "reset_generation": 1,
            "transaction_state": "settled", "events": [old],
        }}

    monkeypatch.setattr(client, "_get", get)
    result = client._wait_action_transaction("old", timeout=1, include_unowned_prefetch=True)
    assert result.events == []


def test_one_shot_poll_does_not_wait_for_empty_or_delivered_rows(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://bridge")
    client._record_delivered_action_events([(2, 20)])
    calls = []

    def get(path, **kwargs):
        calls.append(path)
        return 200, {"transcript": [{"type": "narration", "_seq": 20, "action_id": 2}],
                     "event_counter": 20}

    monkeypatch.setattr(client, "_get", get)
    assert client.poll(timeout=1, one_shot=True) == []
    assert calls == ["/state"]


def test_screenshot_waits_for_requested_capture(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://127.0.0.1:9999")
    requested = {}
    def command(name, **kwargs):
        assert name == "screenshot"
        requested.update(kwargs)
        return {"success": True}
    reads = []
    def get(path, **kwargs):
        reads.append(path)
        return 200, {"screenshot": "fresh" if len(reads) > 1 else "stale",
                     "capture_id": requested["capture_id"] if len(reads) > 1 else "old"}
    monkeypatch.setattr(client, "command", command)
    monkeypatch.setattr(client, "_get", get)
    assert client.screenshot() == "fresh"
    assert len(reads) == 2


def test_screenshot_does_not_return_cache_after_failed_capture(monkeypatch):
    from vnflight.client import BridgeClient
    client = BridgeClient("http://127.0.0.1:9999")
    monkeypatch.setattr(client, "command", lambda *a, **k: {"success": False})
    monkeypatch.setattr(client, "_get", lambda *a, **k: pytest.fail("read stale cache"))
    assert client.screenshot() is None
    assert client.screenshot(timeout=0) is None


def test_screenshot_missing_matching_frame_expires(monkeypatch):
    import types
    import vnflight.client as module
    now = [100.0]
    monkeypatch.setattr(module, "time", types.SimpleNamespace(
        time=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay)))
    client = module.BridgeClient("http://127.0.0.1:9999")
    monkeypatch.setattr(client, "command", lambda *a, **k: {"success": True})
    monkeypatch.setattr(client, "_get", lambda *a, **k: (200, {
        "screenshot": "stale", "capture_id": "unrelated"}))
    assert client.screenshot(timeout=0.1) is None
    assert now[0] == pytest.approx(100.1)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
# Remove vnflight.py single-file module so the package is found even when this
# file runs after build tests that put the repo root first.
for _path in (_src, _root):
    if _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, _src)
sys.path.insert(1, _root)


def _loaded_vnflight_is_the_src_package() -> bool:
    mod = sys.modules.get("vnflight")
    if mod is None:
        return True  # nothing to purge; the next import resolves via sys.path
    path = getattr(mod, "__path__", None)
    if not path:
        return False  # the root-level single-file artifact
    try:
        first = os.path.abspath(list(path)[0])
    except Exception:
        return False
    return first.startswith(os.path.abspath(_src))


def _purge_vnflight_modules() -> None:
    for name in list(sys.modules):
        if name == "vnflight" or name.startswith("vnflight."):
            del sys.modules[name]


if not _loaded_vnflight_is_the_src_package():
    _purge_vnflight_modules()


@pytest.fixture(autouse=True)
def _force_package_vnflight():
    """Keep these tests isolated from root-level vnflight.py imports.

    Purge the loaded modules ONLY when they are not the src package.  An
    unconditional purge made every later `from vnflight.client import X`
    a different class object from the one modules imported earlier in the
    session still hold (tests/test_cli.py's `cli`), so monkeypatches on
    the fresh class never reached the sessions `cli` builds.
    """
    for _path in (_src, _root):
        if _path in sys.path:
            sys.path.remove(_path)
    sys.path.insert(0, _src)
    sys.path.insert(1, _root)
    if not _loaded_vnflight_is_the_src_package():
        _purge_vnflight_modules()


def test_find_game_install_path_config_wins_over_a_same_named_dir_in_cwd(tmp_path, monkeypatch):
    """The hub's cwd is the main repo, which has a mystic_cafe/ directory;
    the configured launch path (another checkout) must win over it."""
    from vnflight import lib

    cwd = tmp_path / "cwd"
    (cwd / "my_game" / "game").mkdir(parents=True)
    other = tmp_path / "elsewhere" / "my_game"
    (other / "game").mkdir(parents=True)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "renpy.exe").write_text("", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(lib, "discover_games", lambda games_dir=None: [
        {"id": "my_game", "launch_cmd": f"{(sdk / 'renpy.exe').as_posix()} {other.as_posix()}",
         "game_dir": None}])

    assert lib._find_game_install_path("my_game", None) == other
    # Unconfigured id that is a directory: still usable, and absolute.
    assert lib._find_game_install_path("my_game_dir_only", None) is None
    (cwd / "my_game_dir_only").mkdir()
    assert lib._find_game_install_path("my_game_dir_only", None) == (cwd / "my_game_dir_only").resolve()


def test_find_game_install_path_prefers_the_project_dir_over_the_sdk_exe(tmp_path, monkeypatch):
    """Absolute SDK launch: "<sdk>/renpy.exe <project>" must resolve to the
    project, not the SDK folder (which has a game/ dir of its own; the shim
    used to be installed there)."""
    from vnflight import lib

    sdk = tmp_path / "renpy-8.5.2-sdk"
    (sdk / "game").mkdir(parents=True)
    exe = sdk / "renpy.exe"
    exe.write_text("", encoding="utf-8")
    project = tmp_path / "my_game"
    (project / "game").mkdir(parents=True)
    launch = f"{exe.as_posix()} {project.as_posix()}"
    monkeypatch.setattr(lib, "discover_games", lambda games_dir=None: [
        {"id": "my_game", "launch_cmd": launch, "game_dir": None}])

    assert lib._find_game_install_path("my_game", None) == project


def test_find_game_install_path_packaged_exe_resolves_to_its_folder(tmp_path, monkeypatch):
    from vnflight import lib

    folder = tmp_path / "Roadwarden"
    (folder / "game").mkdir(parents=True)
    exe = folder / "Roadwarden.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(lib, "discover_games", lambda games_dir=None: [
        {"id": "rw", "launch_cmd": exe.as_posix(), "game_dir": None}])

    assert lib._find_game_install_path("rw", None) == folder


def test_parse_launch_cmd_keeps_windows_backslash_paths(monkeypatch):
    from vnflight import lib

    monkeypatch.setattr(lib, "IS_WINDOWS", True)
    parts = lib._parse_launch_cmd(r"C:\Games\sdk\renpy.exe C:\Games\my_game")
    assert parts == [r"C:\Games\sdk\renpy.exe", r"C:\Games\my_game"]
    quoted = lib._parse_launch_cmd(r'"C:\Program Files\sdk\renpy.exe" "C:\My Games\vn"')
    assert quoted == [r"C:\Program Files\sdk\renpy.exe", r"C:\My Games\vn"]


def test_find_game_install_path_prefers_configured_game_dir(tmp_path, monkeypatch):
    from vnflight import lib

    game_dir = tmp_path / "local_game"
    game_dir.mkdir()
    sdk_dir = tmp_path / "renpy-sdk"
    sdk_dir.mkdir()

    monkeypatch.setattr(lib, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {
                "id": "configured_game",
                "launch_cmd": "renpy-sdk/renpy.exe local_game",
                "game_dir": "local_game",
            }
        ],
    )

    assert lib._find_game_install_path("configured_game", None) == game_dir


def test_find_project_root_prefers_parent_config_over_source_bridge(
    tmp_path, monkeypatch,
):
    from vnflight import lib

    source_package = tmp_path / "src" / "vnflight"
    source_package.mkdir(parents=True)
    (tmp_path / "src" / "bridge").mkdir()
    (tmp_path / "vnflight.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(lib, "__file__", str(source_package / "lib.py"))

    assert lib._find_project_root() == tmp_path


def test_free_existing_game_slot_reports_failure():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return [
                {"slot_id": 3, "game_id": "echoes_of_tomorrow"},
                {"slot_id": 7, "game_id": "roadwarden"},
            ]

        def free_slot(self, slot_id):
            assert slot_id == 7
            return False, {"error": "Admin token required."}

    ok, error = lib._free_existing_game_slot(FakeSession(), "roadwarden")

    assert ok is False
    assert "Existing slot 7 for 'roadwarden' could not be freed" in error
    assert "Admin token required." in error


def test_free_existing_game_slot_frees_all_same_game_slots():
    from vnflight import lib

    freed = []

    class FakeSession:
        def list_slots(self):
            return [
                {"slot_id": 3, "game_id": "roadwarden"},
                {"slot_id": 4, "game_id": "echoes_of_tomorrow"},
                {"slot_id": 5, "game_id": "roadwarden"},
            ]

        def free_slot(self, slot_id):
            freed.append(slot_id)
            return True, {"message": "freed"}

    assert lib._free_existing_game_slot(FakeSession(), "roadwarden") == (True, "")
    assert freed == [3, 5]


def test_free_existing_game_slot_matches_normalized_game_ids():
    from vnflight import lib

    freed = []

    class FakeSession:
        def list_slots(self):
            return [
                {"slot_id": 54, "game_id": "longlivethequeen"},
                {"slot_id": 55, "game_id": "other_game"},
            ]

        def free_slot(self, slot_id):
            freed.append(slot_id)
            return True, {"message": "freed"}

    assert lib._free_existing_game_slot(FakeSession(), "long_live_the_queen") == (
        True,
        "",
    )
    assert freed == [54]


def test_free_existing_game_slot_fails_on_normalized_collision():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return [
                {"slot_id": 1, "game_id": "a_b"},
                {"slot_id": 2, "game_id": "ab"},
            ]

        def free_slot(self, slot_id):  # pragma: no cover - must fail closed
            raise AssertionError("unexpected free")

    ok, error = lib._free_existing_game_slot(FakeSession(), "a-b")

    assert ok is False
    assert "Ambiguous normalized game id" in error
    assert "a_b" in error
    assert "ab" in error


def test_free_existing_game_slot_fails_on_exact_plus_normalized_alias():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return [
                {"slot_id": 1, "game_id": "long_live_the_queen"},
                {"slot_id": 2, "game_id": "longlivethequeen"},
            ]

        def free_slot(self, slot_id):  # pragma: no cover - must fail closed
            raise AssertionError("unexpected free")

    ok, error = lib._free_existing_game_slot(FakeSession(), "long_live_the_queen")

    assert ok is False
    assert "Ambiguous normalized game id" in error
    assert "long_live_the_queen" in error
    assert "longlivethequeen" in error


def test_free_existing_game_slot_fails_when_slot_list_unavailable():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return None

        def free_slot(self, slot_id):  # pragma: no cover - should not be called
            raise AssertionError("unexpected free")

    ok, error = lib._free_existing_game_slot(FakeSession(), "roadwarden")

    assert ok is False
    assert "Could not list bridge slots" in error


def test_single_file_artifact_is_none_in_package_mode():
    from vnflight import lib

    # These tests run against the src/vnflight package, whose modules have
    # __package__ == "vnflight" — never the flat single-file build.
    assert lib._single_file_artifact() is None


def test_single_file_artifact_detects_flat_deployment(monkeypatch):
    from pathlib import Path

    from vnflight import lib

    # In the built vnflight.py all module bodies share the artifact's
    # globals: __package__ is empty and __file__ is the artifact itself.
    monkeypatch.setattr(lib, "__package__", "")
    result = lib._single_file_artifact()

    assert result == Path(lib.__file__).resolve()


def test_free_existing_game_slot_ignores_other_games():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return [{"slot_id": 3, "game_id": "echoes_of_tomorrow"}]

        def free_slot(self, slot_id):  # pragma: no cover - should not be called
            raise AssertionError("unexpected free")

    assert lib._free_existing_game_slot(FakeSession(), "roadwarden") == (True, "")


def test_free_existing_game_slot_treats_already_gone_as_success():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return [{"slot_id": 7, "game_id": "roadwarden"}]

        def free_slot(self, slot_id):
            return False, {"error": "No slot '7' to free."}

    assert lib._free_existing_game_slot(FakeSession(), "roadwarden") == (True, "")


def test_free_existing_game_slot_treats_structured_not_found_as_success():
    from vnflight import lib

    class FakeSession:
        def list_slots(self):
            return [{"slot_id": 7, "game_id": "roadwarden"}]

        def free_slot(self, slot_id):
            return False, {"error_code": "slot_not_found"}

    assert lib._free_existing_game_slot(FakeSession(), "roadwarden") == (True, "")


def test_launch_game_ignores_preexisting_same_game_slots_in_replace_mode(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def __init__(self):
            self.cursors = []

        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def set_cursor(self, state_key, cursor):
            self.cursors.append((state_key, cursor))

        def set_last_request_id(self, state_key, request_id):
            self.last_request_id = (state_key, request_id)

        def save(self):
            pass

    class FakeBridgeClient:
        instances = []

        def __init__(self, bridge_url, token=None):
            self.bridge_url = bridge_url
            self.token = token
            self.slot_prefix = ""
            self.list_calls = 0
            self.freed = []
            FakeBridgeClient.instances.append(self)

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return [{"slot_id": 1, "game_id": "roadwarden"}]
            if self.list_calls == 2:
                return [
                    {"slot_id": 2, "game_id": "roadwarden", "event_counter": 80},
                    {"slot_id": 9, "game_id": "echoes_of_tomorrow", "event_counter": 4},
                ]
            return [
                {"slot_id": 2, "game_id": "roadwarden", "event_counter": 80},
                    {
                        "slot_id": 3,
                        "game_id": "roadwarden",
                        "event_counter": 1,
                        "game_pid": 777,
                        "reservation_id": "ff3b60bdc614af4a",
                    },
            ]

        def free_slot(self, slot_id):
            self.freed.append(slot_id)
            return True, {"message": "freed"}

        def _send_command(self, command):
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["roadwarden.exe"])
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *args, **kwargs: (1234, None))
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    state = FakeClientState()
    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
            client_state=state,
            connect_timeout=10,
            replace=True,
            reservation_token="test-reservation-token",
        )

    session = FakeBridgeClient.instances[0]
    assert ok is True
    assert slot_id == 3
    assert "slot 3" in message
    assert session.freed == [1]
    assert state.cursors == [("http://127.0.0.1:8385/3", 0)]


def test_launch_game_stops_old_pid_after_freeing_replacement_slot(monkeypatch):
    from vnflight import client
    from vnflight import lib

    killed = []

    class FakeClientState:
        def __init__(self):
            self.cursors = []

        def get_pids(self, bridge_url):
            return {"game": 444}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def set_cursor(self, state_key, cursor):
            self.cursors.append((state_key, cursor))

        def set_last_request_id(self, state_key, request_id):
            self.last_request_id = (state_key, request_id)

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.slot_prefix = ""
            self.list_calls = 0
            self.freed = []

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return [
                    {
                        "slot_id": 1,
                        "game_id": "roadwarden",
                        "game_pid": 444,
                        "event_counter": 9,
                    }
                ]
            if self.list_calls == 2:
                return []
            return [
                    {
                        "slot_id": 2,
                        "game_id": "roadwarden",
                        "game_pid": 777,
                        "event_counter": 1,
                        "reservation_id": "ff3b60bdc614af4a",
                    }
            ]

        def free_slot(self, slot_id):
            self.freed.append(slot_id)
            return True, {"message": "freed"}

        def _send_command(self, command):
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["roadwarden.exe"])
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *args, **kwargs: (1234, None))
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: pid == 444)
    monkeypatch.setattr(lib, "kill_process", lambda pid, *a, **k: killed.append(pid) or True)
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
            client_state=FakeClientState(),
            connect_timeout=10,
            replace=True,
            reservation_token="test-reservation-token",
        )

    assert ok is True
    assert slot_id == 2
    assert "slot 2" in message
    assert killed == [444]


def test_launch_game_accepts_normalized_bridge_game_id(monkeypatch, tmp_path):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def __init__(self):
            self.cursors = []

        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def set_cursor(self, state_key, cursor):
            self.cursors.append((state_key, cursor))

        def set_last_request_id(self, state_key, request_id):
            self.last_request_id = (state_key, request_id)

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.bridge_url = bridge_url
            self.token = token
            self.slot_prefix = ""
            self.list_calls = 0

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls <= 2:
                return []
            return [
                {
                    "slot_id": 54,
                    "game_id": "longlivethequeen",
                        "event_counter": 12,
                        "game_pid": 777,
                        "reservation_id": "ff3b60bdc614af4a",
                },
            ]

        def _send_command(self, command):
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {"id": "long_live_the_queen", "launch_cmd": "steam://rungameid/251990"}
        ],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: [launch_cmd])
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *args, **kwargs: (1234, None))
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    # Hermetic: the steam:// id would otherwise resolve the REAL install
    # via the registry and write a launch file into the actual game dir.
    install_root = _make_install_root(tmp_path)
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )
    monkeypatch.setattr(lib, "shim_report", lambda game_id, games_dir: (None, None))

    state = FakeClientState()
    ok, message, slot_id = lib.launch_game(
        "long_live_the_queen",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=state,
            connect_timeout=10,
            replace=True,
            reservation_token="test-reservation-token",
        )

    assert ok is True, message
    assert slot_id == 54
    assert "slot 54" in message
    assert state.cursors == [("http://127.0.0.1:8385/54", 0)]


def test_launch_game_fails_on_normalized_new_slot_collision(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.list_calls = 0
            self.slot_prefix = ""

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls <= 2:
                return []
            return [
                {"slot_id": 1, "game_id": "a_b", "event_counter": 1},
                {"slot_id": 2, "game_id": "ab", "event_counter": 1},
            ]

        def _send_command(self, command):  # pragma: no cover - no success
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "a-b", "launch_cmd": "game.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: [launch_cmd])
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *args, **kwargs: (1234, None))
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "a-b",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=10,
        replace=True,
    )

    assert ok is False
    assert slot_id is None
    assert "Ambiguous normalized game id" in message


def test_launch_game_fails_on_exact_plus_normalized_new_slot_collision(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.list_calls = 0
            self.slot_prefix = ""

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls <= 2:
                return []
            return [
                {"slot_id": 1, "game_id": "long_live_the_queen", "event_counter": 1},
                {"slot_id": 2, "game_id": "longlivethequeen", "event_counter": 1},
            ]

        def _send_command(self, command):  # pragma: no cover - no success
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {"id": "long_live_the_queen", "launch_cmd": "game.exe"}
        ],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: [launch_cmd])
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *args, **kwargs: (1234, None))
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "long_live_the_queen",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=10,
        replace=True,
    )

    assert ok is False
    assert slot_id is None
    assert "Ambiguous normalized game id" in message
    assert "long_live_the_queen" in message
    assert "longlivethequeen" in message


def test_launch_game_generates_and_persists_owned_bridge_admin_token(monkeypatch):
    from vnflight import client
    from vnflight import lib

    launched = []

    class FakeClientState:
        def __init__(self):
            self.admin_token = None

        def get_admin_token(self, bridge_url):
            return None

        def set_admin_token(self, bridge_url, token):
            self.admin_token = token

        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def set_cursor(self, state_key, cursor):
            self.cursor = (state_key, cursor)

        def set_last_request_id(self, state_key, request_id):
            self.last_request_id = (state_key, request_id)

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.bridge_url = bridge_url
            self.token = token
            self.slot_prefix = ""
            self.is_up_calls = 0
            self.list_calls = 0

        def is_up(self, timeout=2.0):
            self.is_up_calls += 1
            return self.is_up_calls > 1

        def reset_bridge(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return [
                {
                    "slot_id": 4,
                    "game_id": "roadwarden",
                        "event_counter": 1,
                        "game_pid": 777,
                        "reservation_id": "ff3b60bdc614af4a",
                },
            ]

        def _send_command(self, command):
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["roadwarden.exe"])
    monkeypatch.setattr(
        lib,
        "_launch_subprocess",
        lambda cmd, **kwargs: launched.append(cmd) or (1000 + len(launched), None),
    )
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(lib.secrets, "token_urlsafe", lambda _size: "-admin-token")
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    state = FakeClientState()
    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=state,
            connect_timeout=10,
            replace=True,
            reservation_token="test-reservation-token",
        )

    assert ok is True
    assert slot_id == 4
    assert state.admin_token == "-admin-token"
    assert launched[0][-1] == "--token=-admin-token"
    assert "slot 4" in message


def test_launch_game_uses_repo_vnflight_script_when_invoked_as_library(
    monkeypatch,
    tmp_path,
):
    from vnflight import client
    from vnflight import lib

    (tmp_path / "vnflight.py").write_text("# built launcher\n", encoding="utf-8")
    launched = []

    class FakeClientState:
        def __init__(self):
            self.admin_token = None

        def get_admin_token(self, bridge_url):
            return None

        def set_admin_token(self, bridge_url, token):
            self.admin_token = token

        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def set_cursor(self, state_key, cursor):
            self.cursor = (state_key, cursor)

        def set_last_request_id(self, state_key, request_id):
            self.last_request_id = (state_key, request_id)

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.slot_prefix = ""
            self.up_calls = 0
            self.list_calls = 0

        def is_up(self, timeout=2.0):
            self.up_calls += 1
            return self.up_calls > 1

        def reset_bridge(self):
            pass

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return [
                {
                    "slot_id": 1,
                    "game_id": "roadwarden",
                        "game_pid": 777,
                        "event_counter": 1,
                        "reservation_id": "ff3b60bdc614af4a",
                }
            ]

        def _send_command(self, command):
            pass

    def fake_launch(cmd, **kwargs):
        launched.append(cmd)
        return (9876 if cmd[-1] != "roadwarden.exe" else 1234), None

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(
        lib,
        "_find_bridge_script",
        lambda games_dir=None: lib._BRIDGE_MODULE_SENTINEL,
    )
    diagnostic_entrypoint = tmp_path / "run_launch_lifecycle_smoke.py"
    diagnostic_entrypoint.write_text("# diagnostic caller\n", encoding="utf-8")
    monkeypatch.setattr(lib.sys, "argv", [str(diagnostic_entrypoint)])
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["roadwarden.exe"])
    monkeypatch.setattr(lib, "_launch_subprocess", fake_launch)
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        str(tmp_path),
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
            connect_timeout=10,
            replace=True,
            reservation_token="test-reservation-token",
        )

    assert ok is True
    assert slot_id == 1
    assert "slot 1" in message
    assert launched[0][:3] == [
        sys.executable,
        str(tmp_path / "vnflight.py"),
        "bridge",
    ]


def test_launch_game_preserves_existing_bridge_pid_for_mixed_launch(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def __init__(self):
            self.pids = {"bridge": 9876}

        def get_pids(self, bridge_url):
            return dict(self.pids)

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def set_cursor(self, state_key, cursor):
            self.cursor = (state_key, cursor)

        def set_last_request_id(self, state_key, request_id):
            self.last_request_id = (state_key, request_id)

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.slot_prefix = ""
            self.list_calls = 0

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return [
                {
                    "slot_id": 2,
                    "game_id": "echoes_of_tomorrow",
                        "game_pid": 777,
                        "event_counter": 1,
                        "reservation_id": "ff3b60bdc614af4a",
                }
            ]

        def _send_command(self, command):
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {"id": "echoes_of_tomorrow", "launch_cmd": "echoes.exe"}
        ],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["echoes.exe"])
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *args, **kwargs: (1234, None))
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    # Hermetic: "echoes_of_tomorrow" is a real directory in this repo —
    # without the stub a launch file would be written into it.
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: None
    )

    state = FakeClientState()
    ok, message, slot_id = lib.launch_game(
        "echoes_of_tomorrow",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=state,
            connect_timeout=10,
            replace=False,
            reservation_token="test-reservation-token",
        )

    assert ok is True
    assert slot_id == 2
    assert "slot 2" in message
    assert state.pids["bridge"] == 9876 and state.pids["game"] == 777
    # identity recorded next to each pid for a later identity-checked stop
    assert state.pids["bridge_expect"] == lib.BRIDGE_IDENTITY
    assert state.pids["game_expect"][0][0] == "renpy"


def test_stop_game_uses_stored_admin_token(monkeypatch):
    from vnflight import client
    from vnflight import lib

    clients = []

    class FakeClientState:
        def get_admin_token(self, bridge_url):
            return "admin-secret"

        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            self.pids = pids

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.bridge_url = bridge_url
            self.token = token
            clients.append(self)

        def is_up(self):
            return True

        def _send_command(self, command):
            self.command = command

    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)

    ok, message = lib.stop_game("http://bridge", FakeClientState())

    assert ok is True
    assert message == "No processes to stop."
    assert [(c.bridge_url, c.token, c.command) for c in clients] == [
        ("http://bridge", "admin-secret", "quit")
    ]


def test_stop_game_reports_failed_kill_and_preserves_pid(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def __init__(self):
            self.saved_pids = None

        def get_admin_token(self, bridge_url):
            return None

        def get_pids(self, bridge_url):
            return {"game": 444}

        def set_pids(self, bridge_url, pids):
            self.saved_pids = pids

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return False

    state = FakeClientState()
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(lib, "kill_process", lambda pid, *a, **k: False)

    ok, message = lib.stop_game("http://bridge", state)

    assert ok is False
    assert "Failed to stop game (PID 444): process still alive" in message
    assert state.saved_pids == {"game": 444}


def test_launch_game_fails_when_prelaunch_slot_snapshot_unavailable(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {}

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.list_calls = 0

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return None

        def free_slot(self, slot_id):  # pragma: no cover - no slots to free
            raise AssertionError("unexpected free")

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["roadwarden.exe"])
    monkeypatch.setattr(
        lib,
        "_launch_subprocess",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not launch")
        ),
    )
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=10,
        replace=True,
    )

    assert ok is False
    assert slot_id is None
    assert "Could not list bridge slots" in message


def test_launch_game_does_not_kill_old_pid_before_slot_snapshot(monkeypatch):
    from vnflight import client
    from vnflight import lib

    killed = []

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {"game": 444}

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.list_calls = 0

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls == 1:
                return []
            return None

        def free_slot(self, slot_id):  # pragma: no cover - no slots to free
            raise AssertionError("unexpected free")

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(lib.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=10,
        replace=True,
    )

    assert ok is False
    assert slot_id is None
    assert "Could not list bridge slots" in message
    assert killed == []


def test_launch_game_fails_when_old_same_game_process_survives_stop(monkeypatch):
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {"game": 444}

    class FakeBridgeClient:
        list_calls = 0

        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return True

        def list_slots(self):
            FakeBridgeClient.list_calls += 1
            if FakeBridgeClient.list_calls == 1:
                return []
            return [
                {
                    "slot_id": 7,
                    "game_id": "roadwarden",
                    "game_pid": 444,
                }
            ]

        def free_slot(self, slot_id):  # pragma: no cover - no same-game slot
            raise AssertionError("unexpected free")

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(lib, "kill_process", lambda pid, *a, **k: False)
    monkeypatch.setattr(
        lib,
        "_launch_subprocess",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("should not launch")
        ),
    )
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)

    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=10,
        replace=True,
    )

    assert ok is False
    assert slot_id is None
    assert "process 444 is still alive" in message


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def test_is_process_alive_exact_csv_match(monkeypatch):
    from vnflight import lib
    monkeypatch.setattr(lib, "IS_WINDOWS", True)
    # tasklist CSV row for the queried PID.
    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: _FakeProc(
        '"renpy.exe","4321","Console","1","123,456 K"\n'))
    assert lib._is_process_alive(4321) is True
    # A different PID whose digits are a substring (432 in 4321) must NOT match.
    assert lib._is_process_alive(432) is False


def test_is_process_alive_none_stdout_is_safe(monkeypatch):
    # The OEM-decode crash left stdout=None; must not raise "NoneType is
    # not iterable", just report not-alive.
    from vnflight import lib
    monkeypatch.setattr(lib, "IS_WINDOWS", True)
    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: _FakeProc(None))
    assert lib._is_process_alive(4321) is False


def test_is_process_alive_no_tasks_message(monkeypatch):
    from vnflight import lib
    monkeypatch.setattr(lib, "IS_WINDOWS", True)
    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: _FakeProc(
        "INFO: No tasks are running which match the specified criteria.\n"))
    assert lib._is_process_alive(4321) is False


def _make_launch_env(monkeypatch, admin_token):
    """Drive launch_game against a fake already-up bridge; capture the
    game subprocess env.  Returns (launch_calls, run_launch)."""
    from vnflight import client
    from vnflight import lib

    # These fixtures build a synthetic game whose shim can never hash-match the
    # repo, so the freshness gate would refuse every launch. It has its own
    # coverage (test_launch_game_refuses_a_stale_shim); isolate it here so these
    # tests keep testing launch-file behaviour.
    monkeypatch.setattr(lib, "shim_report", lambda *a, **k: (None, None))

    launch_calls = []
    slot_tokens = {}
    reservation_token = "test-reservation-token"
    reservation_id = "ff3b60bdc614af4a"

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            pass

        def set_cursor(self, state_key, cursor):
            pass

        def set_last_request_id(self, state_key, request_id):
            pass

        def get_admin_token(self, bridge_url):
            return admin_token

        def set_admin_token(self, bridge_url, token):
            pass

        def set_slot_token(self, bridge_url, game_id, token, slot_id=None):
            slot_tokens[game_id] = token
            if slot_id is not None:
                slot_tokens[f"slot:{slot_id}"] = token

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.token = token
            self.slot_prefix = ""
            self.list_calls = 0

        def is_up(self):
            return True

        def list_slots(self):
            self.list_calls += 1
            if self.list_calls <= 2:
                return []
            return [
                {"slot_id": 5, "game_id": "roadwarden",
                 "event_counter": 3, "game_pid": 778,
                 "reservation_id": "another-launch"},
                {"slot_id": 4, "game_id": "roadwarden",
                 "event_counter": 3, "game_pid": 777,
                 "reservation_id": reservation_id},
            ]

        def free_slot(self, slot_id):
            return True, {}

        def _send_command(self, command):
            pass

    def fake_launch(cmd, cwd=None, log_file=None, extra_env=None):
        launch_calls.append({"cmd": cmd, "extra_env": extra_env})
        return 1234, ""

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{"id": "roadwarden", "launch_cmd": "roadwarden.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda launch_cmd: ["roadwarden.exe"])
    monkeypatch.setattr(lib, "_launch_subprocess", fake_launch)
    monkeypatch.setattr(lib.time, "sleep", lambda _s: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.delenv("VNFLIGHT_TOKEN", raising=False)
    # Hermetic by default: never resolve a REAL install (registry/repo
    # dirs) — that would write vnflight_launch.json into an actual game.
    # Launch-file tests re-patch this after calling _make_launch_env.
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: None
    )

    def run_launch(
        connect_timeout=10, *, fast_forward=False, auto_advance=False,
    ):
        return lib.launch_game(
            "roadwarden",
            "http://127.0.0.1:8385",
            None,
            fast_forward=fast_forward,
            auto_advance=auto_advance,
            client_state=FakeClientState(),
            connect_timeout=connect_timeout,
            replace=True,
            reservation_token=reservation_token,
        )

    run_launch.slot_tokens = slot_tokens
    return launch_calls, run_launch


def test_launch_game_passes_slot_token_to_game_env(monkeypatch):
    """The shim gets its bridge credential (VNFLIGHT_SLOT_TOKEN) from the
    launcher's env so the bridge can reserve the slot for it."""
    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token="admin-tok")

    ok, message, slot_id = run_launch()

    assert ok is True, message
    game_env = launch_calls[-1]["extra_env"] or {}
    token = game_env.get("VNFLIGHT_SLOT_TOKEN")
    assert token, "game env must carry VNFLIGHT_SLOT_TOKEN"
    assert len(token) >= 16
    # The launcher must keep its own key to the slot it just reserved.
    assert run_launch.slot_tokens.get("roadwarden") == token
    assert token == "test-reservation-token"
    assert game_env.get("VNFLIGHT_LAUNCH_ID")


def test_launch_game_refuses_bridge_without_protocol_stamp(monkeypatch):
    from vnflight import client, lib

    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "status",
        lambda self, *, timeout=3.0: {"status": "idle"},
        raising=False,
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "list_slots",
        lambda self: pytest.fail(
            "protocol mismatch must be checked before reading or mutating slots"
        ),
    )

    ok, message, slot_id = run_launch()

    assert ok is False
    assert slot_id is None
    assert "protocol does not match" in message
    assert "Restart the bridge/hub" in message
    assert launch_calls == []


def test_launch_game_distinguishes_status_timeout_from_protocol_mismatch(
    monkeypatch,
):
    from vnflight import client

    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "status",
        lambda self, *, timeout=3.0: {},
        raising=False,
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "list_slots",
        lambda self: pytest.fail(
            "an unreadable protocol status must fail before bridge mutation"
        ),
    )

    ok, message, slot_id = run_launch()

    assert ok is False
    assert slot_id is None
    assert "protocol status could not be read" in message
    assert "does not match" not in message
    assert launch_calls == []


def test_launch_game_accepts_matching_bridge_protocol_stamp(monkeypatch):
    from vnflight import client
    from vnflight.shim_schema import SHIM_PROTOCOL_VERSION

    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "status",
        lambda self, *, timeout=3.0: {
            "status": "idle",
            "shim_protocol_version": SHIM_PROTOCOL_VERSION,
        },
        raising=False,
    )

    ok, message, slot_id = run_launch()

    assert ok is True, message
    assert slot_id == 4
    assert launch_calls


def test_launch_payload_carries_debug_settings(tmp_path):
    """Per-game debug / debug_logs reach the shim through the launch file
    (launcher-mediated games never see our environment)."""
    from vnflight import lib

    (tmp_path / "game").mkdir()
    path, warning = lib._write_launch_file(
        tmp_path, "http://127.0.0.1:8385", slot_token="t",
        launch_id="abc", debug=True, debug_logs=str(tmp_path / "dbg"),
    )
    import json as _json
    payload = _json.loads(open(path, encoding="utf-8").read())
    assert payload["debug"] is True
    assert payload["debug_logs"] == str(tmp_path / "dbg")

    path, _ = lib._write_launch_file(
        tmp_path, "http://127.0.0.1:8385", slot_token="t", launch_id="abd",
        claim_wait=0,
    )
    payload = _json.loads(open(path, encoding="utf-8").read())
    assert "debug" not in payload and "debug_logs" not in payload


def test_launch_game_kills_bridge_when_readiness_deadline_expires(monkeypatch):
    from vnflight import client, lib

    clock = {"now": 0.0}
    killed = []

    class State:
        def get_admin_token(self, bridge_url):
            return None

    class Bridge:
        def __init__(self, bridge_url, token=None):
            self.token = token
            self.calls = 0

        def is_up(self, timeout=2.0):
            self.calls += 1
            if self.calls == 1:
                return False
            clock["now"] += timeout
            return False

    monkeypatch.setattr(lib, "shim_report", lambda *a, **k: (None, None))
    monkeypatch.setattr(
        lib, "discover_games",
        lambda games_dir=None: [{"id": "echoes", "launch_cmd": "game.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *a, **k: (4321, ""))
    monkeypatch.setattr(lib, "kill_process", lambda pid, *a, **k: killed.append(pid) or True)
    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(client, "BridgeClient", Bridge)

    ok, message, slot_id = lib.launch_game(
        "echoes", "http://127.0.0.1:8385", None,
        False, False, State(),
    )

    assert ok is False
    assert slot_id is None
    assert "did not become ready" in message
    assert killed == [4321]
    assert clock["now"] == 25.0


def test_launch_game_kills_new_bridge_on_protocol_mismatch_before_reset(monkeypatch):
    from vnflight import client, lib

    killed = []

    class State:
        def get_admin_token(self, bridge_url):
            return None

        def set_admin_token(self, bridge_url, token):
            pytest.fail("mismatched bridge credentials must not be persisted")

    class Bridge:
        def __init__(self, bridge_url, token=None):
            self.token = token
            self.calls = 0

        def is_up(self, timeout=2.0):
            self.calls += 1
            return self.calls > 1

        def status(self, *, timeout=3.0):
            return {"status": "idle"}

        def reset_bridge(self):
            pytest.fail("mismatched bridge must not be reset")

        def list_slots(self):
            pytest.fail("mismatched bridge slots must not be read")

    monkeypatch.setattr(lib, "shim_report", lambda *a, **k: (None, None))
    monkeypatch.setattr(
        lib, "discover_games",
        lambda games_dir=None: [{"id": "echoes", "launch_cmd": "game.exe"}],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *a, **k: (4321, ""))
    monkeypatch.setattr(lib, "kill_process", lambda pid, *a, **k: killed.append(pid) or True)
    monkeypatch.setattr(lib.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(client, "BridgeClient", Bridge)

    ok, message, slot_id = lib.launch_game(
        "echoes", "http://127.0.0.1:8385", None,
        False, False, State(),
    )

    assert ok is False
    assert slot_id is None
    assert "protocol does not match" in message
    assert killed == [4321]


def test_launch_game_mints_and_persists_slot_token_without_admin_token(monkeypatch):
    """No stored admin token must NOT skip the slot reservation. The old
    policy skipped minting to avoid locking the launcher out of a foreign
    bridge's slot — but an unreserved slot is exactly the harness-sharing
    hole, and a STALE stored admin token still minted a token that locked
    everyone out anyway. The lock-out is now solved by persisting the slot
    token in ClientState (clients re-resolve it on 403)."""
    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token=None)

    ok, message, slot_id = run_launch()

    assert ok is True, message
    game_env = launch_calls[-1]["extra_env"] or {}
    token = game_env.get("VNFLIGHT_SLOT_TOKEN")
    assert token, "slot token must be minted even without an admin token"
    assert run_launch.slot_tokens.get("roadwarden") == token


# ---------------------------------------------------------------------------
# Per-game bridge config (end_on_menu_return) — lookup + launch push
# ---------------------------------------------------------------------------


def test_extract_bridge_config_only_known_keys():
    from vnflight import lib

    game_cfg = {
        "name": "Slay the Princess",
        "launch": "steam://rungameid/1989270",
        "end_on_menu_return": False,
    }
    assert lib.extract_bridge_config(game_cfg) == {"end_on_menu_return": False}
    assert lib.extract_bridge_config({"name": "Roadwarden"}) == {}


def test_discover_games_carries_bridge_config(monkeypatch, tmp_path):
    from vnflight import lib

    monkeypatch.setattr(lib, "_find_project_root", lambda: tmp_path)
    monkeypatch.setattr(lib, "_load_config", lambda games_dir=None: {
        "games": {
            "slay_the_princess": {
                "name": "Slay the Princess",
                "launch": "steam://rungameid/1989270",
                "end_on_menu_return": False,
            },
            "roadwarden": {"name": "Roadwarden", "launch": "rw.exe"},
        }
    })

    games = {g["id"]: g for g in lib.discover_games()}

    assert games["slay_the_princess"]["bridge_config"] == {
        "end_on_menu_return": False}
    assert games["roadwarden"]["bridge_config"] == {}


def test_launch_game_pushes_per_game_bridge_config(monkeypatch):
    """A game entry with end_on_menu_return must reach the bridge's
    POST /config after the game connects (Slay the Princess opt-out)."""
    from vnflight import client, lib

    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{
            "id": "roadwarden",
            "launch_cmd": "roadwarden.exe",
            "bridge_config": {"end_on_menu_return": False},
        }],
    )
    config_pushes = []
    config_deadlines = []

    def _set_config(self, config, *, deadline=None):
        config_pushes.append(dict(config))
        config_deadlines.append(deadline)
        return {"ok": True}

    # client.BridgeClient is the FakeBridgeClient installed by
    # _make_launch_env; give it a set_config recorder.
    monkeypatch.setattr(client.BridgeClient, "set_config", _set_config,
                        raising=False)

    ok, message, slot_id = run_launch()

    assert ok is True, message
    assert config_pushes == [{"end_on_menu_return": False}]
    assert config_deadlines[0] > __import__("time").time()


def test_launch_game_connect_timeout_is_a_wall_clock_budget(monkeypatch):
    """Slow slot reads consume the advertised timeout instead of multiplying it."""
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    clock = {"now": 0.0}
    request_timeouts = []

    def _sleep(seconds):
        clock["now"] += seconds

    def _list_slots(self, *, timeout=None):
        # The two pre-launch snapshots are intentionally outside the connect
        # budget. Only the connection poll supplies a bounded timeout.
        if timeout is None:
            return []
        request_timeouts.append(timeout)
        clock["now"] += timeout
        return []

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(lib.time, "sleep", _sleep)
    monkeypatch.setattr(client.BridgeClient, "list_slots", _list_slots)

    ok, message, slot_id = run_launch(connect_timeout=4)

    assert ok is False
    assert slot_id is None
    assert "did not connect" in message
    assert clock["now"] == 3.5
    assert request_timeouts == [2.0, 0.5]


def test_launch_game_matches_slot_by_shared_game_dir_not_config_id(
    monkeypatch, tmp_path,
):
    """A config entry that aliases another entry's physical game_dir (e.g.
    testing the same game under a different Ren'Py SDK: config id
    'echoes_of_tomorrow_r7' with "game_dir": "echoes_of_tomorrow") must be
    matched by the id the SHIM itself reports -- the install directory's
    basename (see vnflight.rpy) -- not by the vnflight.json config key used
    to launch it.  The shim has no idea it was launched under an aliased id.

    Regression for the false "Game did not connect to the bridge in time."
    failure: a slow first boot (cache freshly cleared by install-shim)
    registers exactly this way -- late, but within the caller's timeout --
    and the old game_id-equality check never matched, so the connect wait
    always ran out the clock even though the game was running fine.
    """
    from vnflight import client, lib

    install_root = tmp_path / "echoes_of_tomorrow"
    (install_root / "game").mkdir(parents=True)
    monkeypatch.setattr(lib, "shim_report", lambda *a, **k: (None, None))

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            pass

        def set_cursor(self, state_key, cursor):
            pass

        def set_last_request_id(self, state_key, request_id):
            pass

        def get_admin_token(self, bridge_url):
            return "admin-tok"

        def set_admin_token(self, bridge_url, token):
            pass

        def set_slot_token(self, bridge_url, game_id, token, slot_id=None):
            pass

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.token = token
            self.slot_prefix = ""
            self.list_calls = 0

        def is_up(self):
            return True

        def list_slots(self, *, timeout=None):
            self.list_calls += 1
            if self.list_calls <= 2:
                # Slow first boot recompiling .rpyc: no slot yet.
                return []
            return [{
                "slot_id": 9,
                # The shim self-reports the game_dir's basename, NOT the
                # vnflight.json config key ("echoes_of_tomorrow_r7") that
                # was used to launch it.
                "game_id": "echoes_of_tomorrow",
                "event_counter": 1,
                "game_pid": 4242,
                "reservation_id": "ff3b60bdc614af4a",
            }]

        def free_slot(self, slot_id):
            return True, {}

        def _send_command(self, command):
            pass

    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{
            "id": "echoes_of_tomorrow_r7",
            "launch_cmd": "renpy-7.5.2-sdk/renpy.exe echoes_of_tomorrow",
            "game_dir": "echoes_of_tomorrow",
        }],
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(
        lib, "_parse_launch_cmd",
        lambda launch_cmd: ["renpy-7.5.2-sdk/renpy.exe", "echoes_of_tomorrow"],
    )
    monkeypatch.setattr(lib, "_launch_subprocess", lambda *a, **k: (1234, ""))
    monkeypatch.setattr(
        lib, "_find_game_install_path",
        lambda game_id, games_dir: install_root,
    )
    monkeypatch.setattr(lib.time, "sleep", lambda _s: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.delenv("VNFLIGHT_TOKEN", raising=False)

    diagnostics: dict = {}
    ok, message, slot_id = lib.launch_game(
        "echoes_of_tomorrow_r7",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=10,
        replace=True,
        reservation_token="test-reservation-token",
        diagnostics=diagnostics,
    )

    assert ok is True, message
    assert slot_id == 9
    assert "connected_after_s" in diagnostics
    assert "connect_failed_after_s" not in diagnostics


def test_launch_game_reports_registered_but_unready_slot(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    clock = {"now": 0.0}

    def _list_slots(self, *, timeout=None):
        self.list_calls += 1
        if self.list_calls <= 2:
            return []
        return [{
            "slot_id": 4,
            "game_id": "roadwarden",
            "event_counter": 0,
            "game_pid": 777,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(client.BridgeClient, "list_slots", _list_slots)

    ok, message, slot_id = run_launch(connect_timeout=2.5)

    assert ok is False
    assert slot_id == 4
    assert "registered as slot 4" in message
    assert "did not become ready" in message
    assert "stop that slot before retrying" in message.lower()
    assert run_launch.slot_tokens["slot:4"] == "test-reservation-token"


def test_launch_game_reserves_setup_after_last_connection_poll(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{
            "id": "roadwarden",
            "launch_cmd": "roadwarden.exe",
            "bridge_config": {"end_on_menu_return": False},
        }],
    )
    clock = {"now": 0.0}
    config_deadlines = []
    timed_slot_reads = []

    def _sleep(seconds):
        clock["now"] += seconds

    def _list_slots(self, *, timeout=None):
        if timeout is None:
            return []
        timed_slot_reads.append(timeout)
        clock["now"] += timeout
        if len(timed_slot_reads) == 1:
            return []
        return [{
            "slot_id": 4,
            "game_id": "roadwarden",
            "event_counter": 3,
            "game_pid": 777,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    def _set_config(self, config, *, deadline=None):
        config_deadlines.append(deadline)
        return {"ok": True}

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(lib.time, "sleep", _sleep)
    monkeypatch.setattr(client.BridgeClient, "list_slots", _list_slots)
    monkeypatch.setattr(client.BridgeClient, "set_config", _set_config,
                        raising=False)

    ok, message, slot_id = run_launch(connect_timeout=4)

    assert ok is True, message
    assert slot_id == 4
    assert clock["now"] == 3.5
    assert timed_slot_reads == [2.0, 0.5]
    assert config_deadlines[0] > __import__("time").time()


def test_launch_game_fails_if_required_bridge_config_is_rejected(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{
            "id": "roadwarden",
            "launch_cmd": "roadwarden.exe",
            "bridge_config": {"end_on_menu_return": False},
        }],
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "set_config",
        lambda self, config, *, deadline=None: {"ok": False},
        raising=False,
    )

    ok, message, slot_id = run_launch()

    assert ok is False
    assert slot_id == 4
    assert "required bridge config" in message
    assert "Slot 4 is connected but not ready" in message
    assert "stop it before retrying" in message


def test_launch_game_does_not_probe_or_kill_partial_launch(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [{
            "id": "roadwarden",
            "launch_cmd": "roadwarden.exe",
            "bridge_config": {"end_on_menu_return": False},
        }],
    )
    monkeypatch.setattr(
        client.BridgeClient,
        "set_config",
        lambda self, config, *, deadline=None: {"ok": False},
        raising=False,
    )
    monkeypatch.setattr(
        lib, "_is_process_alive",
        lambda pid: pytest.fail("partial launch probed process liveness"),
    )
    monkeypatch.setattr(
        lib, "kill_process",
        lambda pid, *a, **k: pytest.fail("partial launch attempted process cleanup"),
    )

    ok, message, slot_id = run_launch()

    assert ok is False
    assert slot_id == 4
    assert "Slot 4 is connected but not ready" in message


def test_launch_game_retains_partial_slot_if_requested_mode_is_rejected(monkeypatch):
    from vnflight import client

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        client.BridgeClient,
        "_send_command",
        lambda self, command, *, timeout=15.0: (False, "rejected"),
    )

    ok, message, slot_id = run_launch(auto_advance=True)

    assert ok is False
    assert slot_id == 4
    assert "auto-advance setup failed" in message
    assert "stop it before retrying" in message


def test_launch_game_rejects_zero_timeout_before_spawning(monkeypatch):
    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")

    ok, message, slot_id = run_launch(connect_timeout=0)

    assert ok is False
    assert slot_id is None
    assert message == "Launch timeout must be greater than zero."
    assert launch_calls == []


def test_launch_game_skips_config_push_without_bridge_config(monkeypatch):
    from vnflight import client, lib

    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok")
    config_pushes = []

    def _set_config(self, config):
        config_pushes.append(dict(config))
        return {"ok": True}

    monkeypatch.setattr(client.BridgeClient, "set_config", _set_config,
                        raising=False)

    ok, message, slot_id = run_launch()

    assert ok is True, message
    assert config_pushes == []


# ---------------------------------------------------------------------------
# Runtime state location (per-user data dir + legacy migration)
# ---------------------------------------------------------------------------


def test_user_data_dir_env_override(monkeypatch, tmp_path):
    from vnflight import lib

    monkeypatch.setenv("VNFLIGHT_DATA_DIR", str(tmp_path / "custom"))
    assert lib.user_data_dir() == tmp_path / "custom"


def test_user_data_dir_windows_uses_localappdata(monkeypatch, tmp_path):
    from vnflight import lib

    monkeypatch.delenv("VNFLIGHT_DATA_DIR", raising=False)
    monkeypatch.setattr(lib.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert lib.user_data_dir() == tmp_path / "AppData" / "Local" / "vnflight"


def test_user_data_dir_linux_uses_xdg(monkeypatch, tmp_path):
    from vnflight import lib

    monkeypatch.delenv("VNFLIGHT_DATA_DIR", raising=False)
    monkeypatch.setattr(lib.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert lib.user_data_dir() == tmp_path / "xdg" / "vnflight"


def test_default_state_dir_not_inside_package(monkeypatch, tmp_path):
    """Runtime state must not be written into the installed package
    directory — that breaks pip installs into site-packages."""
    from vnflight import lib

    monkeypatch.setenv("VNFLIGHT_DATA_DIR", str(tmp_path / "data"))
    state_dir = lib.default_state_dir()
    assert state_dir == str(tmp_path / "data")
    assert os.path.isdir(state_dir)
    pkg_dir = os.path.dirname(os.path.abspath(lib.__file__))
    assert os.path.abspath(state_dir) != pkg_dir






def test_runtime_log_dir_routes_package_root_to_data_dir(monkeypatch, tmp_path):
    from pathlib import Path

    from vnflight import lib

    monkeypatch.setenv("VNFLIGHT_DATA_DIR", str(tmp_path / "data"))
    pkg_dir = Path(lib.__file__).resolve().parent

    # Fallback root == package dir (pip install, no project around):
    # logs must NOT be written next to the package.
    assert lib._runtime_log_dir(pkg_dir) == tmp_path / "data" / "logs"

    # A real project root keeps the historical bridge/logs location.
    project_root = tmp_path / "project"
    project_root.mkdir()
    assert lib._runtime_log_dir(project_root) == project_root / "bridge" / "logs"


def test_stop_game_reports_clean_exit_after_quit(monkeypatch):
    """The shim quit command working is a success, not 'already stopped'.

    stop sends quit through the bridge, sleeps 1s, and then finds the game
    PID dead — because its own quit worked.  That used to print
    "Game (PID N) already stopped", making a clean stop read like a no-op."""
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def __init__(self):
            self.saved_pids = None

        def get_admin_token(self, bridge_url):
            return None

        def get_pids(self, bridge_url):
            return {"game": 444}

        def set_pids(self, bridge_url, pids):
            self.saved_pids = pids

        def save(self):
            pass

    alive = {"game": True}

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return True

        def _send_command(self, command):
            # The quit command reaches the shim; the game exits during
            # the post-quit grace sleep.
            alive["game"] = False

    state = FakeClientState()
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: alive["game"])
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)

    ok, message = lib.stop_game("http://bridge", state)

    assert ok is True
    assert "exited cleanly" in message
    assert "already stopped" not in message
    assert state.saved_pids == {}


def test_stop_game_reports_already_stopped_for_pre_dead_process(monkeypatch):
    """A PID that was dead before stop ran keeps the 'already stopped' text."""
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def get_admin_token(self, bridge_url):
            return None

        def get_pids(self, bridge_url):
            return {"game": 444}

        def set_pids(self, bridge_url, pids):
            self.saved_pids = pids

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return True

        def _send_command(self, command):
            pass

    state = FakeClientState()
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: False)
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)

    ok, message = lib.stop_game("http://bridge", state)

    assert ok is True
    assert "Game (PID 444) already stopped" in message
    assert "exited cleanly" not in message


def test_stop_game_dead_bridge_dead_pid_stays_already_stopped(monkeypatch):
    """No quit was ever sent (bridge down): a dead PID is 'already stopped'."""
    from vnflight import client
    from vnflight import lib

    class FakeClientState:
        def get_admin_token(self, bridge_url):
            return None

        def get_pids(self, bridge_url):
            return {"game": 444}

        def set_pids(self, bridge_url, pids):
            self.saved_pids = pids

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return False

    state = FakeClientState()
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: False)
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)

    ok, message = lib.stop_game("http://bridge", state)

    assert ok is True
    assert "already stopped" in message


# ---------------------------------------------------------------------------
# Launch-file handshake (steam:// / goggalaxy:// launches don't inherit env)
# ---------------------------------------------------------------------------


def _make_install_root(tmp_path):
    install_root = tmp_path / "SlayThePrincess"
    (install_root / "game").mkdir(parents=True)
    return install_root


def test_write_launch_file_atomic_with_contents(tmp_path, monkeypatch):
    """The launch file is written tmp-then-os.replace with all values."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    replaces = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaces.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(lib.os, "replace", spy_replace)

    before = time.time()
    path, warning = lib._write_launch_file(
        install_root,
        "http://127.0.0.1:9611",
        slot_token="tok-123",
        save_slot="slot-a",
        move_host_pointer=False,
    )

    assert warning is None
    target = install_root / "game" / "vnflight_launch.json"
    assert path == str(target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bridge_url"] == "http://127.0.0.1:9611"
    assert data["slot_token"] == "tok-123"
    assert data["save_slot"] == "slot-a"
    assert data["move_host_pointer"] is False
    assert before <= data["written_at"] <= time.time()
    # Atomicity: content lands via a tmp file + os.replace, no leftovers.
    assert len(replaces) == 1
    assert replaces[0][1] == str(target)
    assert replaces[0][0] != str(target)
    leftovers = [
        p.name
        for p in (install_root / "game").iterdir()
        if p.name not in {
            lib.LAUNCH_FILE_NAME,
            lib.LAUNCH_FILE_NAME + lib.LAUNCH_LOCK_SUFFIX,
        }
    ]
    assert leftovers == []


def test_write_launch_file_always_includes_bridge_url(tmp_path):
    """Even a default-port launch records bridge_url -- a fresh file must
    fully describe this launch (env may be stale or absent)."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    path, warning = lib._write_launch_file(
        install_root, lib.DEFAULT_BRIDGE_URL, slot_token=None, save_slot=None
    )

    assert warning is None
    data = json.loads((install_root / "game" / lib.LAUNCH_FILE_NAME).read_text(
        encoding="utf-8"))
    assert data["bridge_url"] == lib.DEFAULT_BRIDGE_URL
    assert "slot_token" not in data
    assert "save_slot" not in data
    assert data["written_at"] > 0


def test_write_launch_file_waits_out_unclaimed_fresh_file(tmp_path):
    """A fresh, UNCLAIMED file means some game is still booting toward it.
    With waiting disabled we overwrite anyway -- but say so, and the warning
    has to read sensibly even though the file WAS written."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    target.write_text(json.dumps({
        "bridge_url": "http://127.0.0.1:9611",
        "written_at": time.time(),
    }), encoding="utf-8")

    path, warning = lib._write_launch_file(
        install_root, "http://127.0.0.1:9612", claim_wait=0
    )

    assert path == str(target)
    assert warning
    assert "WAS written" in warning
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bridge_url"] == "http://127.0.0.1:9612"


def test_write_launch_file_claimed_file_does_not_block(tmp_path):
    """Once a shim stamps claimed_by, that launch is settled -- write now."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    target.write_text(json.dumps({
        "bridge_url": "http://127.0.0.1:9611",
        "written_at": time.time(),
        "claimed_by": 4321,
        "claimed_at": time.time(),
    }), encoding="utf-8")

    started = time.monotonic()
    path, warning = lib._write_launch_file(install_root, "http://127.0.0.1:9612")

    assert warning is None
    assert path == str(target)
    assert time.monotonic() - started < 1.0, "a claimed file must not be waited on"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bridge_url"] == "http://127.0.0.1:9612"
    assert "claimed_by" not in data  # fresh launch, fresh (unclaimed) file


def test_write_launch_file_stale_file_does_not_block(tmp_path):
    """An old file is a launch that never arrived -- never wait on it."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    target.write_text(json.dumps({
        "bridge_url": "http://127.0.0.1:9611",
        "written_at": time.time() - (lib.LAUNCH_CLAIM_FRESH_HORIZON + 60),
    }), encoding="utf-8")

    started = time.monotonic()
    path, warning = lib._write_launch_file(install_root, "http://127.0.0.1:9612")

    assert warning is None
    assert path == str(target)
    assert time.monotonic() - started < 1.0
    assert json.loads(target.read_text(encoding="utf-8"))["bridge_url"] == (
        "http://127.0.0.1:9612")


def test_write_launch_file_proceeds_once_claim_lands(tmp_path):
    """The real path: the other game boots, claims, and we write straight
    after -- well inside the deadline and with nothing to warn about."""
    import threading

    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    target.write_text(json.dumps({
        "bridge_url": "http://127.0.0.1:9611",
        "written_at": time.time(),
    }), encoding="utf-8")

    def claim_later():
        time.sleep(0.3)
        data = json.loads(target.read_text(encoding="utf-8"))
        data["claimed_by"] = 4321
        data["claimed_at"] = time.time()
        target.write_text(json.dumps(data), encoding="utf-8")

    claimer = threading.Thread(target=claim_later)
    claimer.start()
    try:
        started = time.monotonic()
        path, warning = lib._write_launch_file(
            install_root, "http://127.0.0.1:9612", claim_wait=2.0
        )
        elapsed = time.monotonic() - started
    finally:
        claimer.join()

    assert warning is None
    assert path == str(target)
    assert 0.2 < elapsed < 1.8, f"waited {elapsed:.2f}s"
    assert json.loads(target.read_text(encoding="utf-8"))["bridge_url"] == (
        "http://127.0.0.1:9612")


def test_write_launch_file_serializes_concurrent_writers(tmp_path):
    """The reviewer's repro, made deterministic.

    Two hub-style threads write the SAME game's launch file at once while a
    third thread plays the booting shim, stamping claimed_by shortly after
    each file appears.  Unserialized, both writers see a safe state, both
    write, only one bridge_url survives and the shim claims once.  Serialized,
    the second writer waits behind the first, sees the first's file, waits for
    ITS claim, and only then overwrites -- so the shim claims BOTH urls in
    completion order and the surviving file is the second writer's, intact.
    """
    import threading

    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    urls = {
        "a": "http://127.0.0.1:9701",
        "b": "http://127.0.0.1:9702",
    }
    results = {}
    completions = []
    completions_lock = threading.Lock()
    barrier = threading.Barrier(2)
    stop = threading.Event()
    claims = []

    def writer(name):
        barrier.wait()
        path, warning = lib._write_launch_file(
            install_root, urls[name], claim_wait=1.5
        )
        with completions_lock:
            completions.append((name, time.monotonic()))
            results[name] = (path, warning)

    def shim():
        """Claim any fresh unclaimed file, 0.25s after spotting it."""
        seen = set()
        while not stop.is_set():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            url = data.get("bridge_url")
            if "claimed_by" in data or url in seen:
                time.sleep(0.02)
                continue
            time.sleep(0.25)  # a real game needs time to boot before claiming
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if "claimed_by" in data or data.get("bridge_url") != url:
                continue
            data["claimed_by"] = 4321
            data["claimed_at"] = time.time()
            target.write_text(json.dumps(data), encoding="utf-8")
            seen.add(url)
            claims.append(url)

    claimer = threading.Thread(target=shim, daemon=True)
    claimer.start()
    threads = [threading.Thread(target=writer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join(timeout=20)
            assert not t.is_alive(), "a writer never returned -- deadlock?"
        # Let the shim finish claiming the second file before shutting it down.
        waited = time.monotonic()
        while len(claims) < 2 and time.monotonic() - waited < 3.0:
            time.sleep(0.02)
    finally:
        stop.set()
        claimer.join(timeout=5)

    # Both launches got their file written, neither timed out on the claim.
    assert set(results) == {"a", "b"}
    for name in ("a", "b"):
        path, warning = results[name]
        assert path == str(target), f"{name}: {warning}"
        assert warning is None, f"{name} warned: {warning}"

    order = [name for name, _ in sorted(completions, key=lambda x: x[1])]
    # Serialization evidence: the shim claimed BOTH urls, in completion order
    # (a racing pair would clobber one another and only ever be claimed once),
    # and the second writer finished a claim-delay after the first.
    assert claims == [urls[order[0]], urls[order[1]]], claims
    stamps = sorted(t for _, t in completions)
    assert stamps[1] - stamps[0] >= 0.2, (
        f"writers did not serialize: {stamps[1] - stamps[0]:.3f}s apart")

    # No corruption, and the last writer owns the file.
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["bridge_url"] == urls[order[1]]
    leftovers = [
        p.name
        for p in (install_root / "game").iterdir()
        if p.name not in {
            lib.LAUNCH_FILE_NAME,
            lib.LAUNCH_FILE_NAME + lib.LAUNCH_LOCK_SUFFIX,
        }
    ]
    assert leftovers == [], leftovers


def test_launch_tmp_paths_are_unique_per_thread_and_call(tmp_path):
    """Tmp names must not collide across threads in ONE process.

    The in-process lock makes two writers genuinely concurrent inside the
    write impossible, so this pins the name generator directly (the simpler,
    sufficient option offered in the brief): repeat calls on one thread and
    calls from different threads all differ.
    """
    import threading

    from vnflight import lib

    game_dir = _make_install_root(tmp_path) / "game"
    names = []
    names_lock = threading.Lock()

    def generate():
        got = [str(lib._launch_tmp_path(game_dir)) for _ in range(5)]
        with names_lock:
            names.extend(got)

    threads = [threading.Thread(target=generate) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(names) == 20
    assert len(set(names)) == 20, "tmp names collided"
    assert all(str(os.getpid()) in n for n in names)


def _lock_path(install_root):
    from vnflight import lib

    return install_root / "game" / (
        lib.LAUNCH_FILE_NAME + lib.LAUNCH_LOCK_SUFFIX)


def test_write_launch_file_waits_for_advisory_lock_holder(tmp_path):
    """A contender waits for the OS lock even with claim waiting disabled."""
    import threading

    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    lock_path = _lock_path(install_root)
    holder, warning = lib._acquire_launch_lockfile(lock_path, 45.0)
    assert holder is not None and warning is None
    result = {}

    def contend():
        started = time.monotonic()
        result["path"], result["warning"] = lib._write_launch_file(
            install_root, "http://127.0.0.1:9612", claim_wait=0
        )
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=contend)
    thread.start()
    try:
        time.sleep(0.35)
        assert thread.is_alive(), "contender bypassed a held advisory lock"
    finally:
        lib._release_launch_lockfile(holder, lock_path)
        thread.join(timeout=20)
    assert not thread.is_alive(), "the contender never returned"

    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    assert result["path"] == str(target), result["warning"]
    assert result["warning"] is None
    assert result["elapsed"] >= 0.3, (
        f"did not wait out the holder's expiry: {result['elapsed']:.2f}s")
    assert lock_path.exists(), "the persistent coordination file disappeared"


def test_advisory_lock_is_released_when_holder_descriptor_closes(tmp_path):
    """Process death closes descriptors, so no stale-lock stealing is needed."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    lock_path = _lock_path(install_root)
    holder, warning = lib._acquire_launch_lockfile(lock_path, 45.0)
    assert holder is not None and warning is None
    os.close(holder)  # simulate abrupt process exit without explicit unlock

    replacement, warning = lib._acquire_launch_lockfile(lock_path, 0.0)
    assert replacement is not None and warning is None
    lib._release_launch_lockfile(replacement, lock_path)


def test_two_advisory_lock_contenders_serialize_after_holder_exits(tmp_path):
    """Two waiters cannot both acquire when a crashed holder releases."""
    import threading

    from vnflight import lib

    lock_path = _lock_path(_make_install_root(tmp_path))
    holder, warning = lib._acquire_launch_lockfile(lock_path, 45.0)
    assert holder is not None and warning is None
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    active = 0
    max_active = 0
    results = []

    def contend():
        nonlocal active, max_active
        barrier.wait()
        acquired, got_warning = lib._acquire_launch_lockfile(lock_path, 0.0)
        if acquired is None or got_warning is not None:
            with guard:
                results.append((False, got_warning))
            return
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.15)
        with guard:
            active -= 1
            results.append((True, None))
        lib._release_launch_lockfile(acquired, lock_path)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    lib._release_launch_lockfile(holder, lock_path)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert results == [(True, None), (True, None)]
    assert max_active == 1


def test_advisory_lock_serializes_a_separate_process(tmp_path):
    """The coordination primitive is process-wide, not merely thread-local."""
    import subprocess

    from vnflight import lib

    lock_path = _lock_path(_make_install_root(tmp_path))
    holder, warning = lib._acquire_launch_lockfile(lock_path, 45.0)
    assert holder is not None and warning is None
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(_src)!r}); "
        "from vnflight import lib; "
        "fd, warning = lib._acquire_launch_lockfile(Path(sys.argv[1]), 0.0); "
        "print('acquired' if fd is not None else warning, flush=True); "
        "lib._release_launch_lockfile(fd, Path(sys.argv[1]))"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.35)
        assert child.poll() is None, "child bypassed a lock held by this process"
    finally:
        lib._release_launch_lockfile(holder, lock_path)
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_remove_launch_file_only_when_current_payload_is_owned(tmp_path):
    from vnflight import lib

    game_dir = _make_install_root(tmp_path) / "game"
    target = game_dir / lib.LAUNCH_FILE_NAME
    target.write_text(json.dumps({
        "bridge_url": "http://bridge-b",
        "slot_token": "token-b",
        "written_at": time.time(),
    }), encoding="utf-8")

    removed, warning = lib._remove_launch_file_if_owned(
        str(target), "http://bridge-a", slot_token="token-a"
    )
    assert removed is False and warning is None
    assert target.exists(), "an older slot removed the newer slot's handshake"

    removed, warning = lib._remove_launch_file_if_owned(
        str(target), "http://bridge-b", slot_token="wrong-token"
    )
    assert removed is False and warning is None
    assert target.exists()

    removed, warning = lib._remove_launch_file_if_owned(
        str(target), "http://bridge-b", slot_token="token-b"
    )
    assert removed is True and warning is None
    assert not target.exists()


def test_remove_launch_file_rechecks_owner_after_waiting_for_writer(tmp_path):
    """Cleanup queued behind a writer judges the generation left by it."""
    import threading

    from vnflight import lib

    game_dir = _make_install_root(tmp_path) / "game"
    target = game_dir / lib.LAUNCH_FILE_NAME
    target.write_text(json.dumps({
        "bridge_url": "http://bridge-a",
        "slot_token": "token-a",
        "written_at": time.time(),
    }), encoding="utf-8")
    lock_path = game_dir / (lib.LAUNCH_FILE_NAME + lib.LAUNCH_LOCK_SUFFIX)
    holder, warning = lib._acquire_launch_lockfile(lock_path, 0.0)
    assert holder is not None and warning is None
    result = {}

    def clean_old_slot():
        result["value"] = lib._remove_launch_file_if_owned(
            str(target), "http://bridge-a", slot_token="token-a"
        )

    thread = threading.Thread(target=clean_old_slot)
    thread.start()
    time.sleep(0.25)
    assert thread.is_alive()
    target.write_text(json.dumps({
        "bridge_url": "http://bridge-b",
        "slot_token": "token-b",
        "written_at": time.time(),
    }), encoding="utf-8")
    lib._release_launch_lockfile(holder, lock_path)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result["value"] == (False, None)
    assert json.loads(target.read_text(encoding="utf-8"))["bridge_url"] == (
        "http://bridge-b")


def test_write_launch_file_releases_lock_on_failure(tmp_path, monkeypatch):
    """A failed write must not leave the lock (or a tmp file) behind, and it
    must not swallow the failure either."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    game_dir = install_root / "game"
    lock_path = game_dir / (lib.LAUNCH_FILE_NAME + lib.LAUNCH_LOCK_SUFFIX)
    real_dump = json.dump

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(lib.json, "dump", boom)
    path, warning = lib._write_launch_file(install_root, "http://127.0.0.1:9611")
    assert path is None
    assert "could not write" in warning
    assert lock_path.exists(), "persistent lockfile should remain"

    # An UNEXPECTED exception must propagate untouched and still release.
    def worse(*args, **kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(lib.json, "dump", worse)
    with pytest.raises(RuntimeError):
        lib._write_launch_file(install_root, "http://127.0.0.1:9611")
    assert lock_path.exists(), "persistent lockfile should remain"

    monkeypatch.setattr(lib.json, "dump", real_dump)
    path, warning = lib._write_launch_file(install_root, "http://127.0.0.1:9612")
    assert path == str(game_dir / lib.LAUNCH_FILE_NAME)
    assert warning is None
    leftovers = [
        p.name for p in game_dir.iterdir()
        if p.name not in {
            lib.LAUNCH_FILE_NAME,
            lib.LAUNCH_FILE_NAME + lib.LAUNCH_LOCK_SUFFIX,
        }
    ]
    assert leftovers == [], leftovers


def test_write_launch_file_missing_game_dir_warns(tmp_path):
    from vnflight import lib

    path, warning = lib._write_launch_file(
        tmp_path / "NoSuchGame", "http://127.0.0.1:9611"
    )
    assert path is None
    assert "game/ directory" in warning

    path, warning = lib._write_launch_file(None, "http://127.0.0.1:9611")
    assert path is None
    assert warning


def test_launch_game_writes_launch_file_for_every_launch(
    monkeypatch, tmp_path
):
    """launch_game drops game/vnflight_launch.json with the minted slot
    token so launcher-mediated (env-less) games still reach the bridge."""
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )

    ok, message, slot_id = run_launch()

    assert ok is True, message
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    assert target.exists(), "launch_game must write the launch file"
    data = json.loads(target.read_text(encoding="utf-8"))
    game_env = launch_calls[-1]["extra_env"] or {}
    # File and env carry the SAME slot token (identical values by design).
    assert data["slot_token"] == game_env["VNFLIGHT_SLOT_TOKEN"]
    # bridge_url is present even though the port is the default (no
    # VNFLIGHT_BRIDGE_URL in env on default port).
    assert data["bridge_url"] == "http://127.0.0.1:8385"
    assert "VNFLIGHT_BRIDGE_URL" not in game_env


def test_launch_game_proceeds_when_launch_file_unwritable(
    monkeypatch, capsys
):
    """No game/ dir (or unwritable) -> warn and launch anyway; env-capable
    direct-exe games must not regress."""
    from vnflight import lib

    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token="admin-tok")
    # _make_launch_env already stubs _find_game_install_path to None —
    # exactly the unwritable/unlocatable case.

    ok, message, slot_id = run_launch()

    assert ok is True, message
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "launch file not written" in captured.err
    # Env path still intact.
    game_env = launch_calls[-1]["extra_env"] or {}
    assert "VNFLIGHT_SLOT_TOKEN" in game_env


def test_mediated_launch_refuses_before_teardown_for_readonly_launch_file(
    monkeypatch, tmp_path,
):
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    game_dir = install_root / "game"
    target = game_dir / lib.LAUNCH_FILE_NAME
    target.write_text("{}", encoding="utf-8")
    # What makes the handoff unwritable differs by platform, and so does the
    # probe: on Windows a read-only target cannot be replaced, on POSIX it
    # can (rename only needs the directory), so there the directory itself
    # is made unwritable.
    if os.name == "nt":
        target.chmod(stat.S_IREAD)

        def restore():
            target.chmod(stat.S_IWRITE | stat.S_IREAD)
    else:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("directory permissions do not bind root")
        game_dir.chmod(0o500)

        def restore():
            game_dir.chmod(0o700)
    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {"id": "roadwarden", "launch_cmd": "steam://rungameid/123"},
        ],
    )
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda value: [value])
    monkeypatch.setattr(
        lib, "_free_existing_game_slot",
        lambda *a, **k: pytest.fail("preflight refusal freed an existing slot"),
    )
    monkeypatch.setattr(
        lib, "kill_process",
        lambda *a, **k: pytest.fail("preflight refusal killed a process"),
    )

    try:
        ok, message, slot_id = run_launch()
    finally:
        restore()

    assert ok is False
    assert slot_id is None
    assert "Cannot launch through Steam/GOG" in message
    assert launch_calls == []


def test_launch_write_prunes_stale_registration_receipts(tmp_path):
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    receipt = install_root / "game" / "vnflight_registration_abandoned.json"
    receipt.write_text("{}", encoding="utf-8")
    old = time.time() - lib.LAUNCH_CLAIM_FRESH_HORIZON - 10
    os.utime(receipt, (old, old))

    path, warning = lib._write_launch_file(
        install_root, "http://127.0.0.1:8385", launch_id="fresh",
    )
    assert path is not None
    assert warning is None
    assert not receipt.exists()


def test_launch_preflight_does_not_wait_for_handoff_writer(monkeypatch, tmp_path):
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    monkeypatch.setattr(
        lib, "_acquire_launch_lockfile",
        lambda *a, **k: pytest.fail("advisory probe waited on launch lock"),
    )
    monkeypatch.setattr(
        lib, "_launch_write_lock",
        lambda *a, **k: pytest.fail("advisory probe waited on process lock"),
    )

    assert lib._probe_launch_file_writable(install_root) is None


def test_launch_preflight_does_not_apply_windows_readonly_rule_on_posix(
    monkeypatch, tmp_path,
):
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    target = install_root / "game" / lib.LAUNCH_FILE_NAME
    target.write_text("{}", encoding="utf-8")
    target.chmod(stat.S_IRGRP | stat.S_IWGRP)
    monkeypatch.setattr(lib.os, "name", "posix")
    monkeypatch.setattr(
        lib, "_acquire_launch_lockfile", lambda path, wait: (None, None)
    )
    monkeypatch.setattr(lib, "_release_launch_lockfile", lambda lock, path: None)

    try:
        assert lib._probe_launch_file_writable(install_root) is None
    finally:
        target.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_launch_reports_shim_registration_receipt_without_waiting(
    monkeypatch, tmp_path,
):
    from vnflight import client, lib

    install_root = _make_install_root(tmp_path)
    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )

    class ReceiptBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.slot_prefix = ""
            self.calls = 0
            self.stamped = False

        def is_up(self):
            return True

        def list_slots(self):
            self.calls += 1
            target = install_root / "game" / lib.LAUNCH_FILE_NAME
            if target.exists() and not self.stamped:
                data = json.loads(target.read_text(encoding="utf-8"))
                receipt_path = lib._launch_receipt_path(
                    target, data["launch_id"],
                )
                receipt_path.write_text(json.dumps({
                    "launch_id": data["launch_id"],
                    "status": "failed",
                    "reason": "Installed vnflight shim protocol mismatch.",
                }), encoding="utf-8")
                self.stamped = True
            return []

    monkeypatch.setattr(client, "BridgeClient", ReceiptBridgeClient)

    ok, message, slot_id = run_launch(connect_timeout=30)

    assert ok is False
    assert slot_id is None
    assert "protocol mismatch" in message
    assert len(launch_calls) == 1
    assert not list(
        (install_root / "game").glob("vnflight_registration_*.json")
    )


def test_launch_reports_bridge_owned_registration_rejection_without_receipt(
    monkeypatch,
):
    from vnflight import client

    launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )

    class RejectionBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.token = token
            self.slot_prefix = ""

        def is_up(self):
            return True

        def list_slots(self, timeout=None):
            return []

        def registration_rejection(self, timeout=None):
            if self.token != "test-reservation-token":
                return None
            return {
                "reason": "shim_protocol_mismatch",
                "game_id": "roadwarden",
                "rejected_at": time.time(),
                "message": (
                    "Installed vnflight shim protocol mismatch. Run "
                    "install-shim for this game."
                ),
            }

    monkeypatch.setattr(client, "BridgeClient", RejectionBridgeClient)

    ok, message, slot_id = run_launch(connect_timeout=30)

    assert ok is False
    assert slot_id is None
    assert "protocol mismatch" in message
    assert "install-shim" in message
    assert len(launch_calls) == 1


@pytest.mark.parametrize("failure_channel", ["bridge", "receipt"])
def test_launch_final_diagnostic_catches_boundary_registration_failure(
    monkeypatch, tmp_path, failure_channel,
):
    from vnflight import client, lib

    install_root = _make_install_root(tmp_path)
    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root,
    )
    clock = {"now": 0.0}
    published = {"value": False}
    rejection_reads = []
    timed_slot_reads = []

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def registration_rejection(self, timeout=None):
        if self.token != "test-reservation-token":
            return None
        rejection_reads.append(timeout)
        if len(rejection_reads) == 1:
            return None
        assert published["value"], (
            "the final slot read must be able to publish the late failure"
        )
        if failure_channel == "receipt":
            launch_file = install_root / "game" / lib.LAUNCH_FILE_NAME
            launch = json.loads(launch_file.read_text(encoding="utf-8"))
            lib._launch_receipt_path(
                launch_file, launch["launch_id"],
            ).write_text(json.dumps({
                "launch_id": launch["launch_id"],
                "status": "failed",
                "reason": "late receipt rejection",
            }), encoding="utf-8")
            return None
        return {
            "game_id": "roadwarden",
            "rejected_at": time.time(),
            "reason": "shim_protocol_mismatch",
            "message": "late bridge rejection",
        }

    def list_slots(self, timeout=None):
        if timeout is None:
            return []
        timed_slot_reads.append(timeout)
        clock["now"] += timeout
        if len(timed_slot_reads) == 1:
            return []
        published["value"] = True
        return [{
            "slot_id": 12,
            "game_id": "roadwarden",
            "game_pid": 777,
            "event_counter": 0,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )
    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is False
    assert slot_id == 12
    assert "late" in message
    assert len(rejection_reads) == 2
    assert len(timed_slot_reads) == 2
    assert clock["now"] <= 2.0


def test_launch_exact_ready_slot_wins_over_stale_registration_rejection(
    monkeypatch,
):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    clock = {"now": 0.0}
    failure_seen = {"value": False}

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def registration_rejection(self, timeout=None):
        if self.token != "test-reservation-token":
            return None
        failure_seen["value"] = True
        return {
            "game_id": "roadwarden",
            "rejected_at": time.time(),
            "reason": "reservation_conflict",
            "message": "stale registration rejection",
        }

    def list_slots(self, timeout=None):
        if timeout is None or not failure_seen["value"]:
            return []
        return [{
            "slot_id": 12,
            "game_id": "roadwarden",
            "game_pid": 777,
            "event_counter": 1,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )
    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is True
    assert "Launched 'roadwarden'" in message
    assert slot_id == 12


def test_launch_exact_ready_slot_overrides_terminal_bridge_rejection(
    monkeypatch, tmp_path,
):
    from vnflight import client, lib

    install_root = _make_install_root(tmp_path)
    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root,
    )
    clock = {"now": 0.0}
    failure_seen = {"value": False}

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def registration_rejection(self, timeout=None):
        if self.token != "test-reservation-token":
            return None
        failure_seen["value"] = True
        return {
            "game_id": "roadwarden",
            "rejected_at": time.time(),
            "reason": "shim_protocol_mismatch",
            "message": "terminal protocol failure",
        }

    def list_slots(self, timeout=None):
        if timeout is None or not failure_seen["value"]:
            return []
        return [{
            "slot_id": 12,
            "game_id": "roadwarden",
            "game_pid": 777,
            "event_counter": 1,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )
    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is True
    assert "Launched 'roadwarden'" in message
    assert slot_id == 12


def test_launch_exact_ready_slot_does_not_hide_failed_shim_receipt(
    monkeypatch, tmp_path,
):
    from vnflight import client, lib

    install_root = _make_install_root(tmp_path)
    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root,
    )
    clock = {"now": 0.0}
    failure_seen = {"value": False}
    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def registration_rejection(self, timeout=None):
        if self.token != "test-reservation-token":
            return None
        failure_seen["value"] = True
        launch_file = install_root / "game" / lib.LAUNCH_FILE_NAME
        launch = json.loads(launch_file.read_text(encoding="utf-8"))
        lib._launch_receipt_path(
            launch_file, launch["launch_id"],
        ).write_text(json.dumps({
            "launch_id": launch["launch_id"],
            "status": "failed",
            "reason": "terminal receipt failure",
        }), encoding="utf-8")
        return None

    def list_slots(self, timeout=None):
        if timeout is None or not failure_seen["value"]:
            return []
        return [{
            "slot_id": 12,
            "game_id": "roadwarden",
            "game_pid": 777,
            "event_counter": 1,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )
    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is False
    assert "terminal receipt failure" in message
    assert slot_id == 12


def test_launch_ready_override_rechecks_receipt_after_slot_read(
    monkeypatch, tmp_path,
):
    from vnflight import client, lib

    install_root = _make_install_root(tmp_path)
    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root,
    )
    clock = {"now": 0.0}
    failure_seen = {"value": False}
    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def registration_rejection(self, timeout=None):
        if self.token != "test-reservation-token":
            return None
        failure_seen["value"] = True
        return {
            "game_id": "roadwarden",
            "rejected_at": time.time(),
            "reason": "shim_protocol_mismatch",
            "message": "stale bridge rejection",
        }

    def list_slots(self, timeout=None):
        if timeout is None or not failure_seen["value"]:
            return []
        launch_file = install_root / "game" / lib.LAUNCH_FILE_NAME
        launch = json.loads(launch_file.read_text(encoding="utf-8"))
        lib._launch_receipt_path(
            launch_file, launch["launch_id"],
        ).write_text(json.dumps({
            "launch_id": launch["launch_id"],
            "status": "failed",
            "reason": "receipt landed during slot confirmation",
        }), encoding="utf-8")
        return [{
            "slot_id": 12,
            "game_id": "roadwarden",
            "game_pid": 777,
            "event_counter": 1,
            "reservation_id": "ff3b60bdc614af4a",
        }]

    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )
    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is False
    assert "receipt landed during slot confirmation" in message
    assert slot_id == 12


def test_registration_rejection_matching_rejects_reused_token_history():
    from vnflight import lib

    current = {
        "game_id": "roadwarden",
        "launch_id": "launch-new",
        "rejected_at": 100.0,
    }
    assert lib._registration_rejection_matches(
        current, game_id="RoadWarden", launch_id="launch-new",
        launch_started_at=100.0,
    )
    assert not lib._registration_rejection_matches(
        dict(current, game_id="echoes"), game_id="roadwarden",
        launch_id="launch-new", launch_started_at=100.0,
    )
    assert not lib._registration_rejection_matches(
        dict(current, launch_id="launch-old"), game_id="roadwarden",
        launch_id="launch-new", launch_started_at=100.0,
    )
    assert not lib._registration_rejection_matches(
        {"game_id": "roadwarden", "rejected_at": 90.0},
        game_id="roadwarden", launch_id="launch-new",
        launch_started_at=100.0,
    )
    assert not lib._registration_rejection_matches(
        {"game_id": "roadwarden", "rejected_at": 99.5},
        game_id="roadwarden", launch_id="launch-new",
        launch_started_at=100.0,
    )
    assert lib._registration_rejection_matches(
        {"game_id": "roadwarden", "rejected_at": 100.0},
        game_id="roadwarden", launch_id="launch-new",
        launch_started_at=100.0,
    )
    assert not lib._registration_rejection_matches(
        {
            "game_id": "roadwarden",
            "launch_id": "launch-new",
            "rejected_at": 101.0,
            "transient": True,
        },
        game_id="roadwarden", launch_id="launch-new",
        launch_started_at=100.0,
    )


@pytest.mark.parametrize(
    ("reported_pid", "expected"),
    [
        (1234, "connected without this launch reservation"),
        (9999, "different launch reservation"),
    ],
)
def test_launch_diagnoses_only_owned_unreserved_slot(
    monkeypatch, reported_pid, expected,
):
    from vnflight import client

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )

    class MismatchBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.slot_prefix = ""
            self.calls = 0

        def is_up(self):
            return True

        def list_slots(self, timeout=None):
            self.calls += 1
            if self.calls <= 2:
                return []
            return [{
                "slot_id": 8,
                "game_id": "roadwarden",
                "game_pid": reported_pid,
                "event_counter": 1,
                "reservation_id": "another-launch",
            }]

    monkeypatch.setattr(client, "BridgeClient", MismatchBridgeClient)

    ok, message, _slot_id = run_launch(connect_timeout=0.05)

    assert ok is False
    assert expected in message


def test_launch_discards_mismatch_absent_from_final_slot_snapshot(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    clock = {"now": 0.0}
    timed_reads = {"count": 0}

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def list_slots(self, timeout=None):
        if timeout is None:
            return []
        timed_reads["count"] += 1
        clock["now"] += timeout
        if timed_reads["count"] == 1:
            return [{
                "slot_id": 8,
                "game_id": "roadwarden",
                "game_pid": 9999,
                "event_counter": 1,
                "reservation_id": "another-launch",
            }]
        return []

    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)
    monkeypatch.setattr(
        client.BridgeClient,
        "registration_rejection",
        lambda self, **kwargs: None,
        raising=False,
    )

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is False
    assert slot_id is None
    assert "did not connect" in message
    assert "different launch reservation" not in message


def test_launch_retains_mismatch_across_failed_slot_reads(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    clock = {"now": 0.0}
    timed_reads = {"count": 0}
    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def list_slots(self, timeout=None):
        if timeout is None:
            return []
        timed_reads["count"] += 1
        if timed_reads["count"] == 1:
            return [{
                "slot_id": 8,
                "game_id": "roadwarden",
                "game_pid": 9999,
                "event_counter": 1,
                "reservation_id": "another-launch",
            }]
        return None

    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)
    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        lambda self, **kwargs: None, raising=False,
    )

    ok, message, slot_id = run_launch(connect_timeout=3.0)

    assert ok is False
    assert slot_id is None
    assert "different launch reservation" in message


@pytest.mark.parametrize(
    ("later_result", "expected"),
    [
        ({}, "did not connect"),
        (None, "still retrying a reservation handoff"),
    ],
)
def test_launch_transient_diagnostic_clears_only_on_authoritative_empty_read(
    monkeypatch, later_result, expected,
):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    clock = {"now": 0.0}
    wall_clock = time.time()
    reads = {"count": 0}
    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time, "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        client.BridgeClient, "list_slots", lambda self, timeout=None: [],
    )

    def registration_rejection(self, **kwargs):
        reads["count"] += 1
        if reads["count"] == 1:
            return {
                "game_id": "roadwarden",
                "reason": "reservation_conflict",
                "message": "prior slot is releasing",
                "transient": True,
                "retry_mode": "recovery",
                "first_rejected_at": wall_clock - 5.0,
                "rejected_at": wall_clock + 1.0,
            }
        return later_result

    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )

    ok, message, _slot_id = run_launch(connect_timeout=3.0)

    assert ok is False
    assert reads["count"] >= 2
    assert expected in message


def test_launch_timeout_surfaces_retryable_registration_handoff(monkeypatch):
    from vnflight import client, lib

    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    clock = {"now": 0.0}
    wall_clock = time.time()

    monkeypatch.setattr(lib.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        lib.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def list_slots(self, timeout=None):
        if timeout is not None:
            clock["now"] += timeout
        return []

    def registration_rejection(self, **kwargs):
        return {
            "game_id": "roadwarden",
            "reason": "reservation_conflict",
            "message": "Slot 1 is still owned by the prior bridge session.",
            "transient": True,
            "retry_mode": "recovery",
            "first_rejected_at": wall_clock - 10.0,
            "rejected_at": wall_clock + 1.0,
        }

    monkeypatch.setattr(client.BridgeClient, "list_slots", list_slots)
    monkeypatch.setattr(
        client.BridgeClient, "registration_rejection",
        registration_rejection, raising=False,
    )

    ok, message, slot_id = run_launch(connect_timeout=2.0)

    assert ok is False
    assert slot_id is None
    assert "still retrying a reservation handoff" in message
    assert "prior bridge session" in message
    assert "install-shim" not in message


def test_mediated_launch_uses_receipt_pid_for_reservation_diagnostic(
    monkeypatch, tmp_path,
):
    from vnflight import client, lib

    install_root = _make_install_root(tmp_path)
    _launch_calls, run_launch = _make_launch_env(
        monkeypatch, admin_token="admin-tok",
    )
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {"id": "roadwarden", "launch_cmd": "steam://rungameid/123"},
        ],
    )
    monkeypatch.setattr(lib, "_parse_launch_cmd", lambda value: [value])
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )

    class MediatedMismatchClient:
        def __init__(self, bridge_url, token=None):
            self.slot_prefix = ""
            self.calls = 0

        def is_up(self):
            return True

        def list_slots(self, timeout=None):
            self.calls += 1
            target = install_root / "game" / lib.LAUNCH_FILE_NAME
            if target.exists():
                launch = json.loads(target.read_text(encoding="utf-8"))
                receipt = lib._launch_receipt_path(target, launch["launch_id"])
                if not receipt.exists():
                    receipt.write_text(json.dumps({
                        "launch_id": launch["launch_id"],
                        "status": "assigned",
                        "game_pid": 777,
                    }), encoding="utf-8")
            if self.calls <= 2:
                return []
            return [{
                "slot_id": 9,
                "game_id": "roadwarden",
                "game_pid": 777,
                "event_counter": 1,
                "reservation_id": "another-launch",
            }]

    monkeypatch.setattr(client, "BridgeClient", MediatedMismatchClient)

    ok, message, slot_id = run_launch(connect_timeout=0.05)

    assert ok is False
    assert slot_id == 9
    assert "connected without this launch reservation" in message
    assert not list(
        (install_root / "game").glob("vnflight_registration_*.json")
    )


def test_launch_game_records_launch_file_in_client_state(
    monkeypatch, tmp_path
):
    from vnflight import lib

    install_root = _make_install_root(tmp_path)
    launch_calls, run_launch = _make_launch_env(monkeypatch, admin_token="admin-tok")
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )
    # _make_launch_env's FakeClientState has no add_launch_file; use the
    # real ClientState (disabled=in-memory) to capture the recorded path.
    state = lib.ClientState(str(tmp_path), disabled=True)
    state.data["http://127.0.0.1:8385"] = {"admin_token": "admin-tok"}

    ok, message, slot_id = lib.launch_game(
        "roadwarden",
        "http://127.0.0.1:8385",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=state,
            connect_timeout=10,
            replace=True,
            reservation_token="test-reservation-token",
        )

    assert ok is True, message
    target = str(install_root / "game" / lib.LAUNCH_FILE_NAME)
    assert state.get_launch_files("http://127.0.0.1:8385") == [target]


def test_stop_game_removes_launch_file(monkeypatch, tmp_path):
    """stop_game deletes the handshake files recorded for the bridge."""
    from vnflight import client
    from vnflight import lib

    launch_file = tmp_path / "game" / "vnflight_launch.json"
    launch_file.parent.mkdir(parents=True)
    launch_file.write_text(json.dumps({
        "bridge_url": "http://bridge",
        "written_at": time.time(),
    }), encoding="utf-8")

    state = lib.ClientState(str(tmp_path), disabled=True)
    state.add_launch_file("http://bridge", str(launch_file))

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return False

    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.setattr(lib.time, "sleep", lambda _seconds: None)

    ok, message = lib.stop_game("http://bridge", state)

    assert ok is True
    assert not launch_file.exists(), "stop_game must delete the launch file"
    assert state.get_launch_files("http://bridge") == []


def test_stop_game_keeps_launch_file_now_owned_by_another_bridge(
    monkeypatch, tmp_path
):
    from vnflight import client
    from vnflight import lib

    launch_file = tmp_path / "game" / lib.LAUNCH_FILE_NAME
    launch_file.parent.mkdir(parents=True)
    launch_file.write_text(json.dumps({
        "bridge_url": "http://newer-bridge",
        "written_at": time.time(),
    }), encoding="utf-8")
    state = lib.ClientState(str(tmp_path), disabled=True)
    state.add_launch_file("http://older-bridge", str(launch_file))

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            pass

        def is_up(self):
            return False

    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    ok, _message = lib.stop_game("http://older-bridge", state)

    assert ok is True
    assert launch_file.exists(), "older bridge removed the newer handshake"
    assert state.get_launch_files("http://older-bridge") == []


def test_launch_timeout_hint_mentions_install_shim_for_launcher_games(
    monkeypatch, tmp_path
):
    """A launcher-mediated (steam://) connect timeout must point at the
    launch-file handshake: an OLD installed shim ignores the file and
    dials the default port, so install-shim must be re-run."""
    from vnflight import client
    from vnflight import lib

    # This test drives the CONNECT-timeout hint; the freshness gate would refuse
    # before we ever get there (its own coverage is below).
    monkeypatch.setattr(lib, "shim_report", lambda *a, **k: (None, None))

    class FakeClientState:
        def get_pids(self, bridge_url):
            return {}

        def set_pids(self, bridge_url, pids):
            pass

        def get_admin_token(self, bridge_url):
            return "admin-tok"

        def set_admin_token(self, bridge_url, token):
            pass

        def save(self):
            pass

    class FakeBridgeClient:
        def __init__(self, bridge_url, token=None):
            self.token = token
            self.slot_prefix = ""

        def is_up(self):
            return True

        def list_slots(self):
            return []  # the game never appears

        def free_slot(self, slot_id):
            return True, {}

        def _send_command(self, command):
            pass

    install_root = _make_install_root(tmp_path)
    monkeypatch.setattr(
        lib,
        "discover_games",
        lambda games_dir=None: [
            {"id": "slay_the_princess",
             "launch_cmd": "steam://rungameid/1989270"}
        ],
    )
    monkeypatch.setattr(
        lib, "_find_game_install_path", lambda game_id, games_dir: install_root
    )
    monkeypatch.setattr(lib, "_find_bridge_script", lambda games_dir=None: "bridge.py")
    monkeypatch.setattr(
        lib, "_launch_subprocess",
        lambda cmd, cwd=None, log_file=None, extra_env=None: (1234, ""),
    )
    monkeypatch.setattr(lib.time, "sleep", lambda _s: None)
    monkeypatch.setattr(client, "BridgeClient", FakeBridgeClient)
    monkeypatch.delenv("VNFLIGHT_TOKEN", raising=False)

    ok, message, slot_id = lib.launch_game(
        "slay_the_princess",
        "http://127.0.0.1:9611",
        None,
        fast_forward=False,
        auto_advance=False,
        client_state=FakeClientState(),
        connect_timeout=1,
        replace=True,
    )

    assert ok is False
    assert "did not connect" in message
    assert "vnflight_launch.json" in message
    assert "install-shim slay_the_princess --always-on" in message


# ---------------------------------------------------------------------------
# Slot-token persistence (harness/local-MCP bridge sharing fix)
# ---------------------------------------------------------------------------

def test_client_state_deferred_events_roundtrip(tmp_path):
    from vnflight.lib import ClientState

    rows = [{"type": "narration", "text": "x", "_seq": 41}]
    state = ClientState(str(tmp_path))
    state.set_cursor("http://b/slot-1", 45)
    state.set_deferred_events("http://b/slot-1", rows + ["not a row"])
    state.save()

    reloaded = ClientState(str(tmp_path))
    assert reloaded.get_cursor("http://b/slot-1") == 45
    assert reloaded.get_deferred_events("http://b/slot-1") == rows
    assert reloaded.get_deferred_events("http://b/other") == []

    reloaded.set_deferred_events("http://b/slot-1", [])
    reloaded.save()
    assert ClientState(str(tmp_path)).get_deferred_events("http://b/slot-1") == []


def test_client_state_delivered_action_events_roundtrip(tmp_path):
    from vnflight.lib import ClientState

    state = ClientState(str(tmp_path))
    assert state.get_delivered_action_events("http://b/slot-1") == {
        "reset_generation": None, "ownership": []}
    state.set_delivered_action_events("http://b/slot-1", {
        "reset_generation": 3,
        "ownership": [(3, 7, 41), [3, 7, 42], "junk", [1, 2]],
    })
    state.save()

    reloaded = ClientState(str(tmp_path))
    assert reloaded.get_delivered_action_events("http://b/slot-1") == {
        "reset_generation": 3, "ownership": [[3, 7, 41], [3, 7, 42]]}
    assert reloaded.get_delivered_action_events("http://b/other") == {
        "reset_generation": None, "ownership": []}

    reloaded.set_delivered_action_events("http://b/slot-1", {})
    reloaded.save()
    assert ClientState(str(tmp_path)).get_delivered_action_events("http://b/slot-1") == {
        "reset_generation": None, "ownership": []}


def test_client_state_slot_token_roundtrip(tmp_path):
    from vnflight.lib import ClientState

    state = ClientState(str(tmp_path))
    url = "http://127.0.0.1:8385"
    assert state.get_slot_token(url) is None
    state.set_slot_token(url, "echoes_of_tomorrow", "tok-a")
    state.set_slot_token(url, "roadwarden", "tok-b")
    # _last always tracks the most recent launch.
    assert state.get_slot_token(url) == "tok-b"
    assert state.get_slot_token(url, "echoes_of_tomorrow") == "tok-a"
    tokens = state.get_slot_tokens(url)
    assert tokens["_last"] == "tok-b"
    # Survives a save/load cycle.
    state.save()
    reloaded = ClientState(str(tmp_path))
    assert reloaded.get_slot_token(url, "roadwarden") == "tok-b"


def test_client_state_parallel_saves_merge_distinct_slot_tokens(tmp_path):
    from vnflight.lib import ClientState

    url = "http://127.0.0.1:8385"
    first = ClientState(str(tmp_path))
    second = ClientState(str(tmp_path))
    first.set_slot_token(url, "echoes", "tok-echoes")
    second.set_slot_token(url, "roadwarden", "tok-roadwarden")

    first.save()
    second.save()

    reloaded = ClientState(str(tmp_path))
    assert reloaded.get_slot_token(url, "echoes") == "tok-echoes"
    assert reloaded.get_slot_token(url, "roadwarden") == "tok-roadwarden"


def test_client_state_mutations_preserve_additions_and_apply_deletions(tmp_path):
    from vnflight.lib import ClientState

    url = "http://127.0.0.1:8385"
    seed = ClientState(str(tmp_path))
    seed.set_pids(url, {"game": 123})
    seed.add_launch_file(url, "first.json")
    seed.set_admin_token(url, "admin")
    seed.save()

    first = ClientState(str(tmp_path))
    second = ClientState(str(tmp_path))
    first.add_launch_file(url, "second.json")
    second.set_pids(url, {})
    second.clear_launch_files(url)
    second.set_admin_token(url, None)
    first.save()
    second.save()

    reloaded = ClientState(str(tmp_path))
    assert reloaded.get_pids(url) == {}
    assert reloaded.get_launch_files(url) == []
    assert reloaded.get_admin_token(url) is None


def test_client_state_keeps_same_game_tokens_by_assigned_slot(tmp_path):
    from vnflight.lib import ClientState

    url = "http://127.0.0.1:8385"
    first = ClientState(str(tmp_path))
    second = ClientState(str(tmp_path))
    first.set_slot_token(url, "echoes", "tok-a", slot_id=3)
    second.set_slot_token(url, "echoes", "tok-b", slot_id=8)
    first.save()
    second.save()

    reloaded = ClientState(str(tmp_path))
    tokens = reloaded.get_slot_tokens(url)
    assert tokens["slot:3"] == "tok-a"
    assert tokens["slot:8"] == "tok-b"

    reloaded.remove_slot_token(url, 3)
    reloaded.save()
    tokens = ClientState(str(tmp_path)).get_slot_tokens(url)
    assert "slot:3" not in tokens
    assert tokens["slot:8"] == "tok-b"


def test_client_state_clear_slot_tokens_persists(tmp_path):
    from vnflight.lib import ClientState

    url = "http://127.0.0.1:8385"
    state = ClientState(str(tmp_path))
    state.set_slot_token(url, "echoes", "tok-a", slot_id=3)
    state.save()
    state.clear_slot_tokens(url)
    state.save()

    assert ClientState(str(tmp_path)).get_slot_tokens(url) == {}


def test_client_state_stale_remover_cleans_persisted_slot_aliases(tmp_path):
    from vnflight.lib import ClientState

    url = "http://127.0.0.1:8385"
    stale_remover = ClientState(str(tmp_path))
    writer = ClientState(str(tmp_path))
    writer.set_slot_token(url, "echoes", "tok-a", slot_id=3)
    writer.save()

    stale_remover.remove_slot_token(url, 3)
    stale_remover.save()

    assert ClientState(str(tmp_path)).get_slot_tokens(url) == {}


def test_client_state_slot_removal_does_not_delete_reused_slot(tmp_path):
    import hashlib
    from vnflight.lib import ClientState

    url = "http://127.0.0.1:8385"
    old = ClientState(str(tmp_path))
    old.set_slot_token(url, "echoes", "old-token", slot_id=3)
    old.save()
    stale_remover = ClientState(str(tmp_path))

    replacement = ClientState(str(tmp_path))
    replacement.set_slot_token(url, "echoes", "new-token", slot_id=3)
    replacement.save()
    stale_remover.remove_slot_token(
        url,
        3,
        reservation_id=hashlib.sha256(b"old-token").hexdigest()[:16],
    )
    stale_remover.save()

    tokens = ClientState(str(tmp_path)).get_slot_tokens(url)
    assert tokens["slot:3"] == "new-token"
    assert tokens["echoes"] == "new-token"


def test_client_state_clear_all_persists(tmp_path):
    from vnflight.lib import ClientState

    state = ClientState(str(tmp_path))
    state.set_pids("http://bridge", {"game": 123})
    state.save()
    state.clear()
    state.save()

    assert ClientState(str(tmp_path)).data == {}


def test_resolve_stored_slot_tokens_orders_last_first(tmp_path, monkeypatch):
    from vnflight import lib

    monkeypatch.setattr(lib, "default_state_dir", lambda: str(tmp_path))
    url = "http://127.0.0.1:8385"
    state = lib.ClientState(str(tmp_path))
    state.set_slot_token(url, "game_a", "tok-a")
    state.set_slot_token(url, "game_b", "tok-b")
    state.save()

    tokens = lib.resolve_stored_slot_tokens(url)
    assert tokens[0] == "tok-b"          # most recent launch first
    assert set(tokens) == {"tok-a", "tok-b"}
    assert lib.resolve_stored_slot_tokens(None) == []


def test_launch_game_refuses_a_stale_shim(monkeypatch):
    """A game whose installed shim has drifted from the repo must not launch.

    Regression guard for 2026-08-01: Mystic Café ran a four-day-old shim, every
    `act(..., wait:true)` timed out while the click still landed, and two
    investigations were spent on the symptom because nothing compared the files.
    """
    from vnflight import lib

    monkeypatch.setattr(
        lib, "shim_report",
        lambda *a, **k: ("REFUSED: stale shim" + chr(10) + "  fix: install-shim", None))
    ok, message, slot_id = lib.launch_game(
        "roadwarden", "http://127.0.0.1:8777", None, False, False,
        client_state=object(),
    )
    assert ok is False
    assert "REFUSED" in message and "install-shim" in message
    assert slot_id is None


def test_shim_status_fails_open_when_the_source_shim_is_absent(tmp_path):
    """No repo/release shim to compare against => report, never block.

    This is the two-file release case: if vnflight.rpy isn't found next to
    vnflight.py we cannot know whether the game is current, and refusing to
    launch on "I don't know" would be worse than proceeding.
    """
    from vnflight import lib

    st = lib.shim_status("roadwarden", games_dir=str(tmp_path))
    assert st["checked"] is False
    assert st["reason"]
    assert lib.stale_shim_error("roadwarden", games_dir=str(tmp_path)) is None


def test_stale_shim_gate_honours_the_override(monkeypatch, tmp_path):
    """VNFLIGHT_ALLOW_STALE_SHIM=1 lets a deliberately-modified shim run."""
    from vnflight import lib

    monkeypatch.setattr(lib, "shim_status",
                        lambda *a, **k: {"checked": True, "ok": False,
                                         "reason": None, "stale": ["vnflight.rpy"],
                                         "missing": [], "files": [
                                             {"name": "vnflight.rpy", "state": "stale",
                                              "installed": "aaaa", "expected": "bbbb"}]})
    assert lib.stale_shim_error("roadwarden") is not None
    monkeypatch.setenv(lib.ALLOW_STALE_SHIM_ENV, "1")
    assert lib.stale_shim_error("roadwarden") is None


def test_shim_status_reports_a_mod_whose_source_is_missing(tmp_path, monkeypatch):
    """A configured mod with no source must be REPORTED, not silently passed.

    The installer treats a missing mod source as a config error. Dropping it
    here would let a release or partial checkout approve an arbitrary installed
    copy while never having compared it.
    """
    from vnflight import lib

    root = tmp_path / "repo"
    (root).mkdir()
    (root / "vnflight.rpy").write_text("shim source\n", encoding="utf-8")
    game = tmp_path / "game_install"
    (game / "game").mkdir(parents=True)
    (game / "game" / "vnflight.rpy").write_text("shim source\n", encoding="utf-8")
    (game / "game" / "vnf_ghost.rpy").write_text("whatever\n", encoding="utf-8")

    monkeypatch.setattr(lib, "_find_game_install_path", lambda *a, **k: game)
    monkeypatch.setattr(lib, "_load_config_with_error", lambda *a, **k: (
        {"games": {"g": {"mods": [{"source": "mods/ghost.rpy", "target": "vnf_ghost.rpy"}]}}},
        None,
    ))

    st = lib.shim_status("g", games_dir=str(root))
    assert st["checked"] is True
    assert "vnf_ghost.rpy" in st["unchecked"]
    assert "could not verify" in (st["reason"] or "")
    # fail-open: an unverifiable mod does not block the launch...
    assert lib.stale_shim_error("g", games_dir=str(root)) is None
    # ...but it MUST still reach the operator. Recording it in the status dict
    # was not enough: production callers only consult the gate, so the note has
    # its own accessor that the launch paths print.
    warn = lib.shim_warning("g", games_dir=str(root))
    assert warn and "vnf_ghost.rpy" in warn and "not compared" in warn


def test_shim_warning_is_silent_when_everything_was_compared(tmp_path, monkeypatch):
    """No warning noise when the check actually verified every configured file."""
    from vnflight import lib

    root = tmp_path / "repo"
    root.mkdir()
    (root / "vnflight.rpy").write_text("shim source" + chr(10), encoding="utf-8")
    game = tmp_path / "game_install"
    (game / "game").mkdir(parents=True)
    (game / "game" / "vnflight.rpy").write_text("shim source" + chr(10), encoding="utf-8")

    monkeypatch.setattr(lib, "_find_game_install_path", lambda *a, **k: game)
    monkeypatch.setattr(lib, "_load_config_with_error", lambda *a, **k: ({"games": {"g": {}}}, None))

    assert lib.shim_status("g", games_dir=str(root))["ok"] is True
    assert lib.shim_warning("g", games_dir=str(root)) is None


def test_shim_warning_covers_every_unverifiable_state(tmp_path, monkeypatch):
    """Fail-open must never mean silent-open.

    Review finding: shim_warning only spoke up for `unchecked` mods, so the
    three states that most clearly mean "we did not confirm this game is
    current" — no source shim, unresolvable game directory, unreadable config —
    all proceeded without a word, which is the exact failure this gate exists
    to end.
    """
    from vnflight import lib

    # 1. no source shim to compare against (the two-file release, moved apart)
    refusal, warning = lib.shim_report("g", games_dir=str(tmp_path))
    assert refusal is None, "must still fail open"
    assert warning and "NOT verified" in warning

    # 2. game directory cannot be resolved
    root = tmp_path / "repo"
    root.mkdir()
    (root / "vnflight.rpy").write_text("shim" + chr(10), encoding="utf-8")
    monkeypatch.setattr(lib, "_find_game_install_path", lambda *a, **k: None)
    refusal, warning = lib.shim_report("g", games_dir=str(root))
    assert refusal is None
    assert warning and "game/ directory" in warning

    # 3. config unreadable -> the shim is still compared, but mods were not
    game = tmp_path / "inst"
    (game / "game").mkdir(parents=True)
    (game / "game" / "vnflight.rpy").write_text("shim" + chr(10), encoding="utf-8")
    monkeypatch.setattr(lib, "_find_game_install_path", lambda *a, **k: game)
    monkeypatch.setattr(lib, "_load_config_with_error", lambda *a, **k: (None, "boom"))
    refusal, warning = lib.shim_report("g", games_dir=str(root))
    assert refusal is None
    assert warning and "incomplete" in warning


def test_shim_report_scans_once(monkeypatch):
    """Both answers must come from a single filesystem scan.

    Two scans duplicate the work and leave a window where the refusal and the
    warning could describe different states of the same launch.
    """
    from vnflight import lib

    calls = []
    monkeypatch.setattr(lib, "shim_status",
                        lambda *a, **k: (calls.append(1) or
                                         {"checked": True, "ok": True, "reason": None,
                                          "stale": [], "missing": [], "unchecked": [],
                                          "files": []}))
    lib.shim_report("g")
    assert len(calls) == 1


def test_malformed_mod_metadata_does_not_crash_a_launch(tmp_path, monkeypatch):
    """Hand-edited config must not turn a fail-open check into a crash.

    Review finding: a non-string `target` was stored raw in `unchecked`, then
    joined by the FORMATTER, which runs outside shim_status's exception guard —
    so shim_report() raised TypeError straight through launch_game().
    """
    from vnflight import lib

    root = tmp_path / "repo"
    root.mkdir()
    (root / "vnflight.rpy").write_text("shim" + chr(10), encoding="utf-8")
    game = tmp_path / "inst"
    (game / "game").mkdir(parents=True)
    (game / "game" / "vnflight.rpy").write_text("shim" + chr(10), encoding="utf-8")

    monkeypatch.setattr(lib, "_find_game_install_path", lambda *a, **k: game)
    monkeypatch.setattr(lib, "_load_config_with_error", lambda *a, **k: (
        {"games": {"g": {"mods": [
            {"source": "mods/x.rpy", "target": 123},   # non-string target
            {"source": 456, "target": "vnf_ok.rpy"},   # non-string source
            "not-a-dict",                              # not a mapping at all
        ]}}},
        None,
    ))

    st = lib.shim_status("g", games_dir=str(root))
    assert st["checked"] is True
    assert all(isinstance(x, str) for x in st["unchecked"])

    refusal, warning = lib.shim_report("g", games_dir=str(root))  # must not raise
    assert refusal is None, "malformed metadata must fail open, not block"
    assert warning and "not compared" in warning


def test_the_stale_shim_override_needs_an_affirmative_value(monkeypatch):
    """VNFLIGHT_ALLOW_STALE_SHIM=0 must NOT disable the safety gate."""
    from vnflight import lib

    monkeypatch.setattr(lib, "shim_status",
                        lambda *a, **k: {"checked": True, "ok": False, "reason": None,
                                         "stale": ["vnflight.rpy"], "missing": [],
                                         "unchecked": [], "files": [
                                             {"name": "vnflight.rpy", "state": "stale",
                                              "installed": "aaaa", "expected": "bbbb"}]})
    for value, bypassed in (("1", True), ("true", True), ("YES", True),
                            ("0", False), ("false", False), ("", False)):
        monkeypatch.setenv(lib.ALLOW_STALE_SHIM_ENV, value)
        refusal = lib.shim_report("g")[0]
        assert (refusal is None) is bypassed, f"{value!r} bypassed={refusal is None}"


# ---------------------------------------------------------------------------
# resolve_game_mods: explicit list, or the mods-repo manifest
# ---------------------------------------------------------------------------

def _mods_repo(tmp_path, files=None, games=None, name="mods"):
    """A mods repo dir with adapter files and a manifest.json."""
    import hashlib
    import json

    repo = tmp_path / name
    repo.mkdir()
    files = files or {"a.rpy": "# a\n", "b.rpy": "# b\n"}
    for fn, text in files.items():
        (repo / fn).write_text(text, encoding="utf-8")
    entries = {}
    for gid, mods in (games or {"g": [("a.rpy", "vnf_a.rpy"), ("b.rpy", "vnf_b.rpy")]}).items():
        entries[gid] = {"name": gid, "mods": [
            {"file": fn, "target": tgt,
             "sha256": hashlib.sha256((repo / fn).read_bytes()).hexdigest()
             if (repo / fn).exists() else "0" * 64}
            for fn, tgt in mods
        ]}
    (repo / "manifest.json").write_text(
        json.dumps({"manifest_version": 1, "games": entries}), encoding="utf-8")
    return repo


def test_resolve_game_mods_explicit_list_wins_over_the_manifest(tmp_path):
    from vnflight.lib import resolve_game_mods

    repo = _mods_repo(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    config = {"mods_manifest": str(repo / "manifest.json"),
              "games": {"g": {"mods": [{"source": "own/x.rpy", "target": "vnf_x.rpy"}]}}}

    res = resolve_game_mods(config, "g", config["games"]["g"], root)

    assert res.origin == "explicit"
    assert [e["target"] for e in res.entries] == ["vnf_x.rpy"]
    assert res.entries[0]["source"] == root / "own" / "x.rpy"
    assert res.problems == []


def test_resolve_game_mods_empty_explicit_list_means_none_not_manifest(tmp_path):
    from vnflight.lib import resolve_game_mods

    repo = _mods_repo(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    config = {"mods_manifest": str(repo / "manifest.json"),
              "games": {"g": {"mods": []}}}

    res = resolve_game_mods(config, "g", config["games"]["g"], root)

    assert res.origin == "explicit"
    assert res.entries == [] and res.problems == []


def test_resolve_game_mods_from_manifest_absolute_and_relative(tmp_path):
    from vnflight.lib import resolve_game_mods

    root = tmp_path / "proj"
    root.mkdir()
    repo = _mods_repo(root, name="mods")           # sibling of vnflight.json
    for manifest_ref in (str(repo / "manifest.json"), "mods/manifest.json"):
        config = {"mods_manifest": manifest_ref, "games": {"g": {}}}
        res = resolve_game_mods(config, "g", config["games"]["g"], root)
        assert res.origin == "manifest", manifest_ref
        assert res.problems == []
        assert [(e["source"], e["target"]) for e in res.entries] == [
            (repo / "a.rpy", "vnf_a.rpy"), (repo / "b.rpy", "vnf_b.rpy")]
        assert res.entries[0]["sha256"]
        assert res.manifest_path == repo / "manifest.json"


def test_resolve_game_mods_manifest_unknown_game_is_none(tmp_path):
    from vnflight.lib import resolve_game_mods

    repo = _mods_repo(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    config = {"mods_manifest": str(repo / "manifest.json"), "games": {"other": {}}}

    res = resolve_game_mods(config, "other", {}, root)

    assert res.origin == "none" and res.entries == [] and res.problems == []


def test_resolve_game_mods_adapters_alias_reuses_another_entry(tmp_path):
    from vnflight.lib import resolve_game_mods

    repo = _mods_repo(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    config = {"mods_manifest": str(repo / "manifest.json"),
              "games": {"g_r7": {"adapters": "g"}}}

    res = resolve_game_mods(config, "g_r7", config["games"]["g_r7"], root)

    assert res.origin == "manifest"
    assert [e["target"] for e in res.entries] == ["vnf_a.rpy", "vnf_b.rpy"]


def test_resolve_game_mods_refuses_a_sha256_mismatch(tmp_path):
    from vnflight.lib import resolve_game_mods

    repo = _mods_repo(tmp_path)
    (repo / "a.rpy").write_text("# tampered\n", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    config = {"mods_manifest": str(repo / "manifest.json"), "games": {"g": {}}}

    res = resolve_game_mods(config, "g", {}, root)

    assert [e["target"] for e in res.entries] == ["vnf_b.rpy"]
    assert len(res.problems) == 1
    assert "a.rpy" in res.problems[0] and "does not match the manifest" in res.problems[0]


def test_resolve_game_mods_reports_a_missing_manifest_file(tmp_path):
    from vnflight.lib import resolve_game_mods

    repo = _mods_repo(tmp_path)
    (repo / "b.rpy").unlink()
    root = tmp_path / "proj"
    root.mkdir()
    config = {"mods_manifest": str(repo / "manifest.json"), "games": {"g": {}}}

    res = resolve_game_mods(config, "g", {}, root)

    assert [e["target"] for e in res.entries] == ["vnf_a.rpy"]
    assert len(res.problems) == 1 and "b.rpy" in res.problems[0] and "missing" in res.problems[0]


def test_resolve_game_mods_reports_a_missing_or_broken_manifest(tmp_path):
    from vnflight.lib import resolve_game_mods

    root = tmp_path / "proj"
    root.mkdir()
    res = resolve_game_mods({"mods_manifest": "nowhere/manifest.json"}, "g", {}, root)
    assert res.origin == "manifest" and res.entries == []
    assert res.problems and "not found" in res.problems[0]

    (root / "bad.json").write_text("{not json", encoding="utf-8")
    res = resolve_game_mods({"mods_manifest": "bad.json"}, "g", {}, root)
    assert res.problems and "unreadable" in res.problems[0]


def test_shim_status_compares_manifest_adapters(tmp_path, monkeypatch):
    """The stale-shim check resolves adapters the same way install-shim does."""
    from vnflight import lib

    root = tmp_path / "proj"
    root.mkdir()
    (root / "vnflight.rpy").write_text("shim source\n", encoding="utf-8")
    repo = _mods_repo(root, files={"a.rpy": "# a\n"}, games={"g": [("a.rpy", "vnf_a.rpy")]})
    game = tmp_path / "game_install"
    (game / "game").mkdir(parents=True)
    (game / "game" / "vnflight.rpy").write_text("shim source\n", encoding="utf-8")
    (game / "game" / "vnf_a.rpy").write_text("# a\n", encoding="utf-8")

    monkeypatch.setattr(lib, "_find_game_install_path", lambda *a, **k: game)
    monkeypatch.setattr(lib, "_load_config_with_error", lambda *a, **k: (
        {"mods_manifest": str(repo / "manifest.json"), "games": {"g": {}}}, None))

    st = lib.shim_status("g", games_dir=str(root))
    assert st["checked"] is True
    assert [f["name"] for f in st["files"]] == ["vnflight.rpy", "vnf_a.rpy"]
    assert all(f["state"] == "ok" for f in st["files"])
    assert st["unchecked"] == []


def test_discover_games_skips_template_comments_and_example_entries(tmp_path):
    """The shipped template has a string "_comment" under games and
    "_example_*" entries; `games` crashed on the string and listed the
    examples as games."""
    import json

    from vnflight.lib import discover_games

    (tmp_path / "vnflight.json").write_text(json.dumps({
        "games": {
            "_comment": "Add your games below.",
            "_example_local_renpy_game": {"name": "Example", "launch": "x y"},
            "real": {"name": "Real Game", "launch": "renpy.exe real"},
        }
    }), encoding="utf-8")

    games = discover_games(str(tmp_path))

    assert [g["id"] for g in games] == ["real"]


# --- process-identity check on every kill path -----------------------------

def test_process_matches_groups_are_all_of_any_of():
    from vnflight import lib
    exp = [["renpy", "Roadwarden"], ["roadwarden"]]
    assert lib.process_matches("Roadwarden.exe C:/Games/Roadwarden/Roadwarden.exe", exp)
    assert lib.process_matches("renpy.exe C:/sdk/renpy.exe C:/games/road_warden", exp)
    # a different Ren'Py game: engine group passes, this-game group fails
    assert not lib.process_matches("renpy.exe C:/sdk/renpy.exe C:/games/mystic_cafe", exp)
    assert not lib.process_matches(None, exp)
    assert not lib.process_matches("renpy.exe", [])


def test_game_identity_expectation_requires_this_game_not_just_an_engine():
    from vnflight import lib
    exp = lib.game_identity_expectation(
        "echoes_of_tomorrow", ["C:/x/renpy-8.5.2-sdk/renpy.exe", "C:/x/echoes_of_tomorrow"],
        "C:/x/echoes_of_tomorrow", "Echoes of Tomorrow")
    assert lib.process_matches("renpy.exe C:/x/renpy-8.5.2-sdk/renpy.exe C:/x/echoes_of_tomorrow", exp)
    assert not lib.process_matches("renpy.exe C:/x/renpy-8.5.2-sdk/renpy.exe C:/x/mystic_cafe", exp)
    assert not lib.process_matches("python.exe -c import time", exp)
    # launcher game: the real process is the game exe under the install dir
    slay = lib.game_identity_expectation(
        "slay_the_princess", ["steam://rungameid/1989270"],
        "D:/vn/Slay the Princess", "Slay the Princess")
    assert lib.process_matches(
        "SlayThePrincess.exe D:/vn/Slay the Princess/SlayThePrincess.exe", slay)
    assert not lib.process_matches("renpy.exe C:/x/renpy.exe C:/x/echoes_of_tomorrow", slay)


@pytest.mark.parametrize("launcher", ["renpy.py", "renpy.exe"])
@pytest.mark.parametrize("python", ["python.exe", "pythonw.exe", "python2.exe", "python3.11.exe", "python3.13t.exe"])
def test_game_identity_shared_python_launcher_is_not_game_identity(launcher, python):
    from vnflight import lib
    expectation = lib.game_identity_expectation(
        "echoes_of_tomorrow",
        ["C:/Python/" + python, "C:/sdk/" + launcher, "C:/games/echoes_of_tomorrow"],
    )
    assert lib.process_matches(
        "C:/Python/" + python + " C:/sdk/" + launcher + " C:/games/echoes_of_tomorrow", expectation)
    assert not lib.process_matches(
        "C:/Python/" + python + " C:/sdk/" + launcher + " C:/games/mystic_cafe", expectation)
    assert not lib.process_matches("C:/Python/python.exe unrelated.py", expectation)


def test_kill_process_refuses_without_expectation_or_identity(monkeypatch, capsys):
    from vnflight import lib
    calls = []
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(lib.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(lib, "process_command_line", lambda pid: None)
    assert lib.kill_process(4242) is False
    assert lib.kill_process(4242, [["renpy"]]) is False
    err = capsys.readouterr().err
    assert "no identity expectation" in err and "could not be read" in err
    assert calls == []


def test_kill_process_refuses_a_mismatch_and_kills_a_match(monkeypatch, capsys):
    from vnflight import lib
    calls = []
    alive = {"v": True}
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: alive["v"])
    monkeypatch.setattr(lib, "IS_WINDOWS", True)

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        alive["v"] = False
    monkeypatch.setattr(lib.subprocess, "run", fake_run)
    monkeypatch.setattr(lib.time, "sleep", lambda s: None)
    monkeypatch.setattr(lib, "process_command_line",
                        lambda pid: "python.exe C:/py/python.exe -c import time")
    assert lib.kill_process(4242, [["renpy"], ["echoes_of_tomorrow"]]) is False
    assert "does not look like" in capsys.readouterr().err
    assert calls == []
    monkeypatch.setattr(lib, "process_command_line",
                        lambda pid: "renpy.exe C:/sdk/renpy.exe C:/g/echoes_of_tomorrow")
    assert lib.kill_process(4242, [["renpy"], ["echoes_of_tomorrow"]]) is True
    assert calls and calls[0][:4] == ["taskkill", "/F", "/T", "/PID"]


def test_stop_game_passes_the_recorded_identity_and_legacy_default(monkeypatch):
    from vnflight import lib
    seen = []
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(lib, "kill_process", lambda pid, expect=None: seen.append((pid, expect)) or True)
    monkeypatch.setattr(lib, "BridgeClient", None, raising=False)

    class FakeSession:
        def __init__(self, *a, **k): pass
        def is_up(self): return False

    import vnflight.client as client_mod
    monkeypatch.setattr(client_mod, "BridgeClient", FakeSession)

    class State:
        def __init__(self, pids):
            self._pids = pids
            self.saved = None
        def get_admin_token(self, url): return None
        def get_pids(self, url): return dict(self._pids)
        def set_pids(self, url, pids): self.saved = pids
        def save(self): pass
        def get_launch_files(self, url): return []
        def clear_launch_files(self, url): pass

    st = State({"game": 11, "game_expect": [["renpy"], ["mystic_cafe"]],
                "bridge": 12, "bridge_expect": [["vnflight"], ["bridge"]]})
    ok, msg = lib.stop_game("http://bridge", st)
    assert ok, msg
    assert seen == [(11, [["renpy"], ["mystic_cafe"]]), (12, [["vnflight"], ["bridge"]])]
    seen.clear()
    st = State({"game": 21, "bridge": 22})
    ok, msg = lib.stop_game("http://bridge", st)
    assert seen == [(21, lib.LEGACY_GAME_IDENTITY), (22, lib.LEGACY_BRIDGE_IDENTITY)]
    assert "predates identity tracking" in msg


def test_stop_game_keeps_a_refused_pid_and_its_identity(monkeypatch):
    from vnflight import lib
    monkeypatch.setattr(lib, "_is_process_alive", lambda pid: True)
    monkeypatch.setattr(lib, "kill_process", lambda pid, expect=None: False)
    import vnflight.client as client_mod

    class FakeSession:
        def __init__(self, *a, **k): pass
        def is_up(self): return False
    monkeypatch.setattr(client_mod, "BridgeClient", FakeSession)

    class State:
        saved = None
        def get_admin_token(self, url): return None
        def get_pids(self, url): return {"game": 31, "game_expect": [["renpy"], ["x_game"]]}
        def set_pids(self, url, pids): self.saved = pids
        def save(self): pass
        def get_launch_files(self, url): return []
        def clear_launch_files(self, url): pass

    st = State()
    ok, msg = lib.stop_game("http://bridge", st)
    assert not ok and "refused" in msg
    assert st.saved == {"game": 31, "game_expect": [["renpy"], ["x_game"]]}


# -- From-zero install findings (2026-09-14) --------------------------------

def test_find_game_install_path_resolves_relative_launch_against_the_config_dir(tmp_path, monkeypatch):
    """A relative project dir in `launch` means relative to vnflight.json
    (the template says so, and launch_game runs the command from that
    directory).  The install-path finder tried the shell's cwd FIRST, so
    `install-shim` run from another checkout that also had a
    "../my_game" installed the shim into that other checkout's game."""
    from vnflight import lib

    config_dir = tmp_path / "a" / "vnflight"
    config_dir.mkdir(parents=True)
    theirs = tmp_path / "a" / "my_game"
    (theirs / "game").mkdir(parents=True)
    cwd = tmp_path / "b" / "somewhere"
    cwd.mkdir(parents=True)
    decoy = tmp_path / "b" / "my_game"
    (decoy / "game").mkdir(parents=True)
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "renpy.exe").write_text("", encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(lib, "discover_games", lambda games_dir=None: [
        {"id": "my_game", "launch_cmd": f"{(sdk / 'renpy.exe').as_posix()} ../my_game",
         "game_dir": None}])

    found = lib._find_game_install_path("my_game", str(config_dir))

    assert found is not None
    assert found.resolve() == theirs.resolve()
    assert found.resolve() != decoy.resolve()


def test_find_game_install_path_ignores_a_relative_launch_that_only_exists_in_cwd(tmp_path, monkeypatch):
    from vnflight import lib

    config_dir = tmp_path / "a"
    config_dir.mkdir()
    cwd = tmp_path / "b" / "somewhere"
    cwd.mkdir(parents=True)
    ((tmp_path / "b" / "my_game") / "game").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(lib, "discover_games", lambda games_dir=None: [
        {"id": "my_game", "launch_cmd": "renpy.exe ../my_game", "game_dir": None}])

    assert lib._find_game_install_path("my_game", str(config_dir)) is None


def test_launch_subprocess_names_the_missing_executable(tmp_path):
    """A bad `launch` used to surface as a localized "[WinError 2] ..." with
    no path in it."""
    from vnflight import lib

    exe = tmp_path / "definitely_not_here" / "renpy.exe"
    pid, err = lib._launch_subprocess([str(exe), "some_game"], cwd=str(tmp_path))

    assert pid is None
    assert "executable not found" in err
    assert str(exe) in err


def test_shim_source_path_prefers_the_explicit_root_then_the_code(tmp_path):
    from vnflight.lib import shim_source_path

    own = tmp_path / "vnflight.rpy"
    own.write_text("# a project root with its own shim\n", encoding="utf-8")
    assert shim_source_path(str(tmp_path)) == own

    bare = tmp_path / "config-only"
    bare.mkdir()
    code_adjacent = shim_source_path(str(bare))
    assert code_adjacent.exists()
    assert code_adjacent.name == "vnflight.rpy"
    assert code_adjacent.parent != bare
    assert shim_source_path(None) == code_adjacent


def test_always_on_is_the_primary_spelling_and_the_flag_list_still_works():
    from vnflight.lib import game_wants_always_on

    assert game_wants_always_on({"always_on": True}) is True
    assert game_wants_always_on({"install_shim_flags": ["--always-on"]}) is True
    assert game_wants_always_on({"always_on": True, "install_shim_flags": []}) is True
    assert game_wants_always_on({"always_on": False}) is False
    assert game_wants_always_on({"always_on": "yes"}) is False   # booleans only
    assert game_wants_always_on({"install_shim_flags": ["--no-mods"]}) is False
    assert game_wants_always_on({"install_shim_flags": "--always-on"}) is False
    assert game_wants_always_on({}) is False
    assert game_wants_always_on(None) is False


def test_project_root_resolves_from_dist_before_a_config_exists(tmp_path, monkeypatch):
    """A fresh clone has dist/vnflight.py, vnflight.rpy and the template but
    no vnflight.json yet; the root is still the folder with the shim."""
    from vnflight import lib

    root = tmp_path / "clone"
    (root / "dist").mkdir(parents=True)
    (root / "vnflight.rpy").write_text("# shim\n", encoding="utf-8")
    (root / "vnflight.default.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(lib, "__file__", str(root / "dist" / "vnflight.py"))

    assert lib._find_project_root() == root
    assert lib.shim_source_path(None) == root / "vnflight.rpy"

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "vnflight.rpy").write_text("# shim\n", encoding="utf-8")
    monkeypatch.setattr(lib, "__file__", str(flat / "vnflight.py"))
    assert lib._find_project_root() == flat
    assert lib.shim_source_path(None) == flat / "vnflight.rpy"
