"""Executed smoke test for the built single-file vnflight.py artifact.

The rest of the suite tests the src/vnflight package; test_build.py only
ast-parses and diff-checks the artifact.  Nothing else ever RUNS the built
file, which is how it forked behaviorally from the package (duplicate
top-level names, broken flat-deploy path math) without any test noticing.

This module rebuilds the artifact to a temp dir (guaranteeing it reflects
current sources), starts its bridge on an ephemeral port as a subprocess,
simulates a connected game over plain HTTP (slot assign + events, the same
wire protocol the Ren'Py shim uses), and drives the bridge with the
artifact's own CLI: slots listing, a POST /event -> GET /state round-trip,
and a command mailbox round-trip with a fake game acking the command.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from vnflight.shim_schema import SHIM_PROTOCOL_VERSION

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STARTUP_TIMEOUT = 15.0
_CLI_TIMEOUT = 30


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body: dict | None = None,
          token: str | None = None, shim_protocol: bool = True) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["X-Slot-Token"] = token
    if shim_protocol:
        headers["X-VNFlight-Shim-Protocol"] = str(SHIM_PROTOCOL_VERSION)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(scope="module")
def artifact(tmp_path_factory):
    """Build a fresh artifact from current sources into a temp dir."""
    out_dir = tmp_path_factory.mktemp("artifact")
    out = out_dir / "vnflight.py"
    result = subprocess.run(
        [sys.executable, os.path.join(_root, "build_vnflight.py"),
         "--output", str(out)],
        capture_output=True, text=True, cwd=_root, timeout=60,
    )
    assert result.returncode == 0, f"build failed: {result.stderr}"
    assert out.exists()
    return out


@pytest.fixture(scope="module")
def artifact_env(artifact, tmp_path_factory):
    """Environment for artifact subprocesses: UTF-8 + isolated state dir."""
    state_dir = tmp_path_factory.mktemp("state")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["VNFLIGHT_DATA_DIR"] = str(state_dir)
    env.pop("VNFLIGHT_TOKEN", None)  # The smoke bridge is deliberately open.
    return env


@pytest.fixture(scope="module")
def bridge(artifact, artifact_env, tmp_path_factory):
    """The artifact's bridge server on an ephemeral port, with one slot.

    Yields (base_url, slot_id, proc).  The fake game slot is registered
    over HTTP exactly like the Ren'Py shim would (POST /slots/assign).
    """
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("logs") / "bridge.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(artifact), "bridge",
         "--host", "127.0.0.1", "--port", str(port)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        cwd=str(artifact.parent), env=artifact_env,
    )
    try:
        deadline = time.time() + _STARTUP_TIMEOUT
        last_err = None
        while time.time() < deadline:
            if proc.poll() is not None:
                log.flush()
                raise AssertionError(
                    "artifact bridge exited rc=%s during startup:\n%s"
                    % (proc.returncode, log_path.read_text(encoding="utf-8"))
                )
            try:
                info = _http("GET", url + "/")
                assert "slots" in info
                break
            except Exception as exc:  # noqa: BLE001 - retry until deadline
                last_err = exc
                time.sleep(0.2)
        else:
            raise AssertionError(
                f"artifact bridge did not come up in {_STARTUP_TIMEOUT}s: "
                f"{last_err}"
            )

        assigned = _http("POST", url + "/slots/assign", {
            "game_id": "smoke_test_game",
            "shim_protocol_version": SHIM_PROTOCOL_VERSION,
        })
        assert assigned.get("status") == "assigned", assigned
        slot_id = assigned["slot_id"]

        yield url, slot_id, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()


def _run_cli(artifact, artifact_env, bridge_url, *args):
    return subprocess.run(
        [sys.executable, str(artifact), "--bridge", bridge_url,
         "--json", "--yes", *args],
        capture_output=True, text=True, timeout=_CLI_TIMEOUT,
        stdin=subprocess.DEVNULL, cwd=str(artifact.parent), env=artifact_env,
    )


class TestArtifactSmoke:
    """Run the built artifact for real: bridge subprocess + its own CLI."""

    def test_cli_slots_sees_the_registered_game(self, artifact, artifact_env,
                                                bridge):
        url, slot_id, proc = bridge
        result = _run_cli(artifact, artifact_env, url, "slots")

        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        payload = json.loads(result.stdout)
        games = {s.get("game_id") for s in payload["slots"]}
        assert "smoke_test_game" in games
        assert any(s.get("slot_id") == slot_id for s in payload["slots"])

    def test_event_roundtrip_reaches_cli_state(self, artifact, artifact_env,
                                               bridge):
        url, slot_id, proc = bridge

        # Push events the way the shim does, then read them back through
        # the artifact CLI's `state` command.
        pushed = _http("POST", f"{url}/{slot_id}/event", {
            "type": "dialogue",
            "who": "Narrator",
            "what": "The smoke test begins.",
        })
        assert pushed.get("seq", 0) >= 1

        state_result = _run_cli(artifact, artifact_env, url, "state")
        assert state_result.returncode == 0, (
            f"stdout={state_result.stdout!r} stderr={state_result.stderr!r}"
        )
        state_payload = json.loads(state_result.stdout)
        assert isinstance(state_payload, dict)
        assert state_payload.get("status")

        # The pushed dialogue must come back through the CLI transcript.
        history = _run_cli(artifact, artifact_env, url, "history", "--all")
        assert history.returncode == 0, (
            f"stdout={history.stdout!r} stderr={history.stderr!r}"
        )
        events = json.loads(history.stdout)["events"]
        assert any(
            e.get("type") == "dialogue"
            and e.get("what") == "The smoke test begins."
            for e in events
        ), events

    def test_command_mailbox_roundtrip(self, artifact, artifact_env, bridge):
        url, slot_id, proc = bridge

        # Fake game: poll the slot's command mailbox, ack the first command
        # with a command_result event (what the shim does after executing).
        seen: dict = {}

        def fake_game():
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    got = _http("GET", f"{url}/{slot_id}/command")
                except Exception:  # noqa: BLE001 - keep polling
                    time.sleep(0.1)
                    continue
                cmd = got.get("command")
                if cmd:
                    seen["command"] = cmd
                    _http("POST", f"{url}/{slot_id}/event", {
                        "type": "command_result",
                        "command": cmd.get("name"),
                        "nonce": cmd.get("nonce"),
                        "success": True,
                        "message": "smoke ack",
                    })
                    return
                time.sleep(0.1)

        consumer = threading.Thread(target=fake_game, daemon=True)
        consumer.start()

        result = _run_cli(artifact, artifact_env, url, "cmd", "smoke_noop")
        consumer.join(timeout=20)

        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert seen.get("command", {}).get("name") == "smoke_noop"
        assert '"success": true' in result.stdout.lower()

    def test_bridge_survived_the_session(self, bridge):
        # A crash in any earlier interaction would surface here.
        url, slot_id, proc = bridge
        assert proc.poll() is None
        info = _http("GET", url + "/")
        assert any(
            s.get("game_id") == "smoke_test_game" for s in info["slots"]
        )


@pytest.fixture(scope="module")
def reserved_bridge(artifact, tmp_path_factory):
    """Artifact bridge with a known admin token and a RESERVED game slot.

    Mirrors the real launch flow: launch stores the bridge admin token in
    the CLI state file and starts the game with a minted VNFLIGHT_SLOT_TOKEN,
    which the shim presents at /slots/assign — reserving the slot.  After
    the ca4ba72 lockdown every slot-scoped route on a reserved slot is
    token-gated, so each fresh CLI process must send the stored admin token.

    Yields (base_url, slot_id, env_with_stored_token, env_without_token).
    """
    admin_token = "artifact-admin-token-0123456789abcdef"
    slot_token = "artifact-slot-token-fedcba9876543210"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("logs") / "reserved_bridge.log"
    log = open(log_path, "w", encoding="utf-8")

    def _env(state_dir) -> dict:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["VNFLIGHT_DATA_DIR"] = str(state_dir)
        env.pop("VNFLIGHT_TOKEN", None)
        return env

    # State dir the way `launch` leaves it: admin token stored per bridge URL.
    tokened_state = tmp_path_factory.mktemp("state_tokened")
    (tokened_state / ".vnflight_state.json").write_text(
        json.dumps({url: {"admin_token": admin_token}}), encoding="utf-8"
    )
    tokenless_state = tmp_path_factory.mktemp("state_tokenless")

    proc = subprocess.Popen(
        [sys.executable, str(artifact), "bridge",
         "--host", "127.0.0.1", "--port", str(port),
         "--token", admin_token],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        cwd=str(artifact.parent), env=_env(tokenless_state),
    )
    try:
        deadline = time.time() + _STARTUP_TIMEOUT
        last_err = None
        while time.time() < deadline:
            if proc.poll() is not None:
                log.flush()
                raise AssertionError(
                    "artifact bridge exited rc=%s during startup:\n%s"
                    % (proc.returncode, log_path.read_text(encoding="utf-8"))
                )
            try:
                _http("GET", url + "/")
                break
            except Exception as exc:  # noqa: BLE001 - retry until deadline
                last_err = exc
                time.sleep(0.2)
        else:
            raise AssertionError(
                f"artifact bridge did not come up in {_STARTUP_TIMEOUT}s: "
                f"{last_err}"
            )

        # Shim-style registration WITH a slot token — reserves the slot.
        assigned = _http("POST", url + "/slots/assign",
                         {"game_id": "reserved_game", "token": slot_token,
                          "shim_protocol_version": SHIM_PROTOCOL_VERSION})
        assert assigned.get("status") == "assigned", assigned
        assert assigned.get("token") == slot_token, assigned
        slot_id = assigned["slot_id"]

        # Some game activity so /state has content.  The shim sends its
        # slot token on every bridge call — event pushes are gated too.
        _http("POST", f"{url}/{slot_id}/event", {
            "type": "dialogue",
            "who": "Narrator",
            "what": "Reserved-slot smoke.",
        }, token=slot_token)

        yield url, slot_id, _env(tokened_state), _env(tokenless_state)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()


class TestArtifactReservedSlot:
    """Fresh CLI processes against a reserved slot: the post-launch reality."""

    def test_reserved_slot_rejects_tokenless_reads(self, reserved_bridge):
        # Guard: the reservation must actually gate the slot, otherwise the
        # stored-token test below could pass vacuously.
        url, slot_id, _tokened, _tokenless = reserved_bridge
        req = urllib.request.Request(f"{url}/{slot_id}/state")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as err:
            assert err.code == 403
        else:
            raise AssertionError("tokenless /state on a reserved slot "
                                 "should have returned 403")

    def test_cli_state_uses_stored_admin_token(self, artifact,
                                               reserved_bridge):
        url, slot_id, tokened_env, _tokenless = reserved_bridge
        result = subprocess.run(
            [sys.executable, str(artifact), "--bridge", url, "--json",
             "--slot", str(slot_id), "state"],
            capture_output=True, text=True, timeout=_CLI_TIMEOUT,
            stdin=subprocess.DEVNULL, cwd=str(artifact.parent),
            env=tokened_env,
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        payload = json.loads(result.stdout)
        assert payload.get("status")

    def test_cli_history_uses_stored_admin_token(self, artifact,
                                                 reserved_bridge):
        url, slot_id, tokened_env, _tokenless = reserved_bridge
        result = subprocess.run(
            [sys.executable, str(artifact), "--bridge", url, "--json",
             "--slot", str(slot_id), "history", "--all"],
            capture_output=True, text=True, timeout=_CLI_TIMEOUT,
            stdin=subprocess.DEVNULL, cwd=str(artifact.parent),
            env=tokened_env,
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        events = json.loads(result.stdout)["events"]
        assert any(e.get("what") == "Reserved-slot smoke." for e in events)
