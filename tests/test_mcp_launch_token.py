"""Regression: the MCP server must authenticate to a require-token bridge
with the admin token its own launch resolved/stored.

Bug (2026-07-06, Echoes round-3 smoke): the vnflight MCP `launch` tool
succeeds on a require-token bridge — the CLI subprocess it spawns reads the
bridge admin token from ClientState (or VNFLIGHT_TOKEN) and reserves the
game's slot — but the SAME MCP session's next `act`/`state`/`screenshot`
calls 403 with "Invalid or missing token for this slot".

Root cause: the long-lived MCP `BridgeClient` (built once in
`run_server`, and rebuilt-with-reset after `launch` in `handle_launch`)
only took a token from `--token`.  `BridgeClient.__init__` adds the
`VNFLIGHT_TOKEN` env fallback but NOT the stored-admin-token fallback that
the CLI's `_stored_admin_token` uses.  So on a shared/harness require-token
bridge whose admin token lives in ClientState (persisted at game launch,
not exported into the MCP process env), the client stays tokenless while
its own launch subprocess authenticates fine.

Fix: `lib.resolve_stored_admin_token(bridge_url)` — shared by `run_server`
(client construction) and `handle_launch` (post-launch adoption).
"""

import http.client
import json
import os

from vnflight.shim_schema import SHIM_PROTOCOL_VERSION
import sys
import threading

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

import vnflight.bridge as bridge
import vnflight.lib as lib
from vnflight.bridge import BridgeHandler, SlotManager, ThreadedHTTPServer
from vnflight.client import BridgeClient
from vnflight.handlers import HandlerContext, Hooks, handle_tool

ADMIN_TOKEN = "admin-secret-token"
SLOT_TOKEN = "slot-secret-token"


class _BridgeFixture:
    def __init__(self, server, port, manager):
        self.server = server
        self.port = port
        self.manager = manager
        self.url = f"http://127.0.0.1:{port}"

    def post(self, path, body, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Slot-Token"] = token
            conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                         headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = None
            return resp.status, data
        finally:
            conn.close()

    def assign_reserved_slot(self, game_id="echoes", token=SLOT_TOKEN):
        """Assign a slot born-reserved with the supplied token."""
        status, data = self.post("/slots/assign", {
            "game_id": game_id,
            "shim_protocol_version": SHIM_PROTOCOL_VERSION,
        }, token=token)
        assert status == 200, data
        return data["slot_id"]


@pytest.fixture
def strict_bridge(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # keep JSONL logs out of the repo
    manager = SlotManager(admin_token=ADMIN_TOKEN, require_token=True)
    monkeypatch.setattr(bridge, "slots", manager)
    server = ThreadedHTTPServer(("127.0.0.1", 0), BridgeHandler)
    server.verbose = False
    server.allowed_hosts = set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fx = _BridgeFixture(server, server.server_address[1], manager)
    yield fx
    fx.server.shutdown()


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    """A private ClientState dir, with VNFLIGHT_TOKEN cleared so the env
    fallback cannot mask the stored-token path under test.

    Uses the ``VNFLIGHT_DATA_DIR`` env var (read by ``user_data_dir`` at call
    time) rather than monkeypatching ``lib.default_state_dir`` directly: the
    test suite juggles ``sys.modules['vnflight']`` (conftest + test_lib), so a
    module-attribute patch can miss the ``vnflight.lib`` object that
    ``handle_launch``'s runtime ``from .lib import`` resolves.  The env var is
    process-global and immune to that.
    """
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("VNFLIGHT_DATA_DIR", str(d))
    monkeypatch.delenv("VNFLIGHT_TOKEN", raising=False)
    return str(d)


def _store_admin_token(bridge_url):
    """Simulate what a game launch does: persist the bridge admin token."""
    cs = lib.ClientState(lib.default_state_dir())
    cs.set_admin_token(bridge_url, ADMIN_TOKEN)
    cs.save()


def test_resolve_stored_admin_token_roundtrip(state_dir):
    url = "http://127.0.0.1:12345"
    assert lib.resolve_stored_admin_token(url) is None
    _store_admin_token(url)
    assert lib.resolve_stored_admin_token(url) == ADMIN_TOKEN
    # Unknown bridge / falsy url must not blow up.
    assert lib.resolve_stored_admin_token("http://other") is None
    assert lib.resolve_stored_admin_token(None) is None


def test_run_server_client_seam_adopts_stored_token(strict_bridge, state_dir):
    """The construction run_server now performs (token=None -> stored admin)
    must authenticate; the pre-fix tokenless construction 403s."""
    slot_id = strict_bridge.assign_reserved_slot()

    # Pre-fix behaviour: token stays None, no VNFLIGHT_TOKEN in env -> 403.
    # Construct the tokenless client BEFORE any admin token is stored, so
    # the 04abbeb 403 self-heal has no stored credential to rescue it with
    # and the genuine pre-fix denial stands.
    tokenless = BridgeClient(strict_bridge.url, slot=slot_id, token=None)
    assert tokenless.state() == {}
    assert tokenless.last_http_status == 403  # documents the reported bug

    # Now simulate the game launch persisting the bridge admin token.
    _store_admin_token(strict_bridge.url)

    # Post-fix: run_server resolves the stored admin token before building
    # the client.
    resolved = lib.resolve_stored_admin_token(strict_bridge.url)
    assert resolved == ADMIN_TOKEN
    fixed = BridgeClient(strict_bridge.url, slot=slot_id, token=resolved)
    data = fixed.state()
    assert fixed.last_http_status == 200
    assert "transcript" in data


def test_handle_launch_adopts_stored_token_then_state_authenticates(
    strict_bridge, state_dir, monkeypatch
):
    """End-to-end MCP path: a tokenless client whose own `launch` reserves a
    slot must adopt the stored admin token and then `state` (its next tool
    call) must authenticate rather than 403."""
    # Don't wait the real second for the slot to register.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    # The MCP client is built tokenless (shared bridge, no --token, no env).
    client = BridgeClient(strict_bridge.url, token=None)
    assert client.token is None

    def _fake_run_cli(*args, timeout=90):
        # Simulate the CLI launch subprocess using the handler's exact
        # reservation credential and returning the machine-readable receipt.
        _store_admin_token(strict_bridge.url)
        token_index = args.index("--reservation-token") + 1
        reservation_token = args[token_index]
        slot_id = strict_bridge.assign_reserved_slot(
            "echoes", token=reservation_token,
        )
        return {"ok": True, "output": json.dumps({
            "success": True,
            "message": "Launched 'echoes'",
            "slot_id": slot_id,
        })}

    ctx = HandlerContext(client=client, hooks=Hooks(run_cli=_fake_run_cli))

    result = handle_tool(ctx, "launch", {"game_id": "echoes"})
    assert result.get("ok"), result

    # The client must have adopted the exact per-slot launch credential...
    assert client.token not in (None, ADMIN_TOKEN, SLOT_TOKEN)
    # ...and bound directly to the receipt's slot.
    assert client.slot_prefix, "auto_select_slot did not bind the slot"

    # ...and its next tool call (state) must authenticate, not 403.
    handle_tool(ctx, "state", {})
    assert client.last_http_status == 200
