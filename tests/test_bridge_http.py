"""HTTP-surface security tests for the vnflight bridge.

These exercise BridgeHandler end-to-end over a real socket: token gating
on consuming/data-bearing routes, CORS removal, the Host-header
allowlist (DNS rebinding), the JSON Content-Type requirement on POSTs
(browser "simple request" blocker), and the /reserve semantics.
"""

import http.client
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import vnflight.bridge as bridge
from vnflight.bridge import BridgeHandler, SlotManager, ThreadedHTTPServer
from vnflight.shim_schema import SHIM_PROTOCOL_VERSION

ADMIN_TOKEN = "admin-secret-token"
SLOT_TOKEN = "slot-secret-token"


class BridgeFixture:
    def __init__(self, server, port, manager):
        self.server = server
        self.port = port
        self.manager = manager

    def request(self, method, path, body=None, token=None, headers=None,
                content_type="application/json", shim_protocol=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            all_headers = dict(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                if content_type:
                    all_headers.setdefault("Content-Type", content_type)
            if token:
                all_headers["X-Slot-Token"] = token
            if shim_protocol:
                all_headers["X-VNFlight-Shim-Protocol"] = str(
                    SHIM_PROTOCOL_VERSION
                )
            conn.request(method, path, body=payload, headers=all_headers)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = dict(resp.getheaders())
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = None
            return resp.status, data, resp_headers
        finally:
            conn.close()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body=body, **kwargs)


def _make_bridge(monkeypatch, tmp_path, require_token=False):
    monkeypatch.chdir(tmp_path)  # keep JSONL logs out of the repo
    manager = SlotManager(admin_token=ADMIN_TOKEN, require_token=require_token)
    monkeypatch.setattr(bridge, "slots", manager)
    server = ThreadedHTTPServer(("127.0.0.1", 0), BridgeHandler)
    server.verbose = False
    server.allowed_hosts = set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return BridgeFixture(server, server.server_address[1], manager)


@pytest.fixture
def open_bridge(monkeypatch, tmp_path):
    fx = _make_bridge(monkeypatch, tmp_path, require_token=False)
    yield fx
    fx.server.shutdown()


@pytest.fixture
def strict_bridge(monkeypatch, tmp_path):
    fx = _make_bridge(monkeypatch, tmp_path, require_token=True)
    yield fx
    fx.server.shutdown()


def _assign_slot(fx, game_id="testgame", token=None):
    status, data, _ = fx.post("/slots/assign", {
        "game_id": game_id,
        "shim_protocol_version": SHIM_PROTOCOL_VERSION,
    }, token=token)
    assert status == 200, data
    return data["slot_id"]


def _apply_act(gs, nonce, index=1):
    """Accept + dispatch + resolve one act directly on a slot's GameState.

    Only the setup runs in-process; every assertion below goes over HTTP,
    which is the layer these regressions are about.
    """
    command = {
        "name": "act", "args": {"index": index}, "nonce": nonce,
        "reset_generation": gs.reset_generation,
    }
    ok, message, ack = gs.submit_command_with_ack(command)
    assert ok is True, message
    assert gs.consume_command() == command
    gs.push_event({
        "type": "command_result", "command": "act", "nonce": nonce,
        "success": True, "resolved_as": "choice", "label": "Go",
    })
    return ack


def _settle_act(gs, nonce, index):
    """Push the act past its settle boundary so it can be spilled."""
    gs.set_pending_request({
        "type": "choice_request", "id": f"req-{index}", "choices": ["On"],
    })
    gs._act_transactions[nonce]["settle_observed_at"] -= (
        bridge.GameState._ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE + 1
    )


class TestSpilledTransactionRecoveryOverHttp:
    """The spill/rehydrate machinery, exercised through BridgeHandler.

    Each of these was verified by hand during the Aug 2026 review and had no
    HTTP-layer regression: the unit tests drive GameState directly, so a
    routing or serialization mistake between the handler and the registry
    would not have been caught.
    """

    def test_scoped_drain_of_a_spilled_record_reloads_from_the_journal(
        self, open_bridge, monkeypatch,
    ):
        monkeypatch.setattr(bridge.GameState, "_MAX_ACT_TRANSACTION_EVENTS", 2)
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        _apply_act(gs, "spilled-events")
        spoken = [f"line {i}" for i in range(5)]
        for text in spoken:
            gs.push_event({"type": "narration", "text": text})
        assert gs._act_transactions["spilled-events"]["events_offloaded"] > 0

        status, data, _ = open_bridge.get(
            f"/{slot_id}/transaction?action_nonce=spilled-events"
        )

        assert status == 200, data
        transaction = data["transaction"]
        assert [
            event["text"] for event in transaction["events"]
            if event.get("type") == "narration"
        ] == spoken
        assert transaction["events_reloaded"] > 0

    def test_offloaded_tombstone_is_read_and_acked_over_http(
        self, open_bridge, monkeypatch,
    ):
        monkeypatch.setattr(bridge.GameState, "_MAX_ACT_TRANSACTIONS", 4)
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        for i in range(20):
            nonce = f"act-{i}"
            _apply_act(gs, nonce)
            gs.push_event({"type": "narration", "text": f"story {i}"})
            _settle_act(gs, nonce, i)
        assert "act-0" not in gs._act_transactions

        status, data, _ = open_bridge.get(
            f"/{slot_id}/transaction?action_nonce=act-0"
        )
        assert status == 200, data
        transaction = data["transaction"]
        assert any(
            event.get("text") == "story 0" for event in transaction["events"]
        )

        status, acked, _ = open_bridge.get(
            "/{}/transaction?action_nonce=act-0&ack={}".format(
                slot_id, transaction["delivery_end"])
        )
        assert status == 200, acked
        # Drained: it compacts to a tombstone, which is still readable.
        assert acked["transaction"]["compacted"] is True

    def test_command_post_deduplicates_a_spilled_nonce(
        self, open_bridge, monkeypatch,
    ):
        monkeypatch.setattr(bridge.GameState, "_MAX_ACT_TRANSACTIONS", 4)
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        for i in range(20):
            nonce = f"act-{i}"
            _apply_act(gs, nonce)
            _settle_act(gs, nonce, i)
        assert "act-0" not in gs._act_transactions
        queued_before = len(gs.pending_commands)
        next_action_id = gs._next_action_id

        status, data, _ = open_bridge.post(f"/{slot_id}/command", {
            "name": "act",
            "args": {"index": 1},
            "nonce": "act-0",
            "reset_generation": gs.reset_generation,
        })

        assert status == 200, data
        assert data["deduplicated"] is True
        assert data["action_id"] == 1
        # Rehydrating a spilled nonce must not mint a second action or requeue.
        assert gs._next_action_id == next_action_id
        assert len(gs.pending_commands) == queued_before


class TestTransactionStorageFailures:
    def test_transaction_read_reports_retryable_storage_failure(
        self, open_bridge, monkeypatch,
    ):
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        gs._remember_offloaded_nonce_locked("stored-nonce")

        def fail_scan(_nonce=None):
            raise bridge.TransactionJournalError("temporarily unreadable")

        monkeypatch.setattr(gs, "_scan_transaction_journal", fail_scan)
        status, data, _ = open_bridge.get(
            f"/{slot_id}/transaction?action_nonce=stored-nonce"
        )

        assert status == 503
        assert "temporarily unavailable" in data["error"]

    def test_duplicate_act_does_not_reapply_when_storage_is_unreadable(
        self, open_bridge, monkeypatch,
    ):
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        gs._remember_offloaded_nonce_locked("stored-nonce")
        original_action_id = gs._next_action_id

        def fail_scan(_nonce=None):
            raise bridge.TransactionJournalError("temporarily unreadable")

        monkeypatch.setattr(gs, "_scan_transaction_journal", fail_scan)
        status, data, _ = open_bridge.post(f"/{slot_id}/command", {
            "name": "act",
            "nonce": "stored-nonce",
            "reset_generation": gs.reset_generation,
            "index": 1,
        })

        assert status == 503
        assert data["transaction_state"] == "acceptance_unknown"
        assert data["reason"] == "storage_error"
        assert gs._next_action_id == original_action_id
        assert gs.pending_commands == []

    def test_a_second_act_while_one_is_in_flight_is_a_conflict(
        self, open_bridge,
    ):
        """409, not 400: the request is well-formed, the state is busy."""
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        _apply_act(gs, "first")

        status, data, _ = open_bridge.post(f"/{slot_id}/command", {
            "name": "act",
            "args": {"index": 2},
            "nonce": "second",
            "reset_generation": gs.reset_generation,
        })

        assert status == 409, data
        assert data["reason"] == "action_in_flight"


# ---------------------------------------------------------------------------
# Token gating on GET routes
# ---------------------------------------------------------------------------


class TestReservedSlotGetGating:
    def test_reserved_slot_state_requires_token(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)

        status, data, _ = open_bridge.get(f"/{slot_id}/state")
        assert status == 403

        status, data, _ = open_bridge.get(f"/{slot_id}/state", token=SLOT_TOKEN)
        assert status == 200
        assert "transcript" in data

        status, data, _ = open_bridge.get(f"/{slot_id}/state", token=ADMIN_TOKEN)
        assert status == 200

    def test_reserved_slot_wrong_token_rejected(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)

        status, _, _ = open_bridge.get(f"/{slot_id}/state", token="wrong-token")
        assert status == 403

    def test_reserved_slot_action_not_consumed_without_token(self, open_bridge):
        """GET /action is consuming — a tokenless GET must not pop the
        pending action (the silent-act-failure attack)."""
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)
        gs = open_bridge.manager.get(slot_id)
        gs.set_pending_request({
            "type": "choice_request", "id": "r1", "choices": ["Go"],
        })
        ok, _ = gs.submit_action({"type": "act", "request_id": "r1", "index": 1})
        assert ok is True

        status, _, _ = open_bridge.get(f"/{slot_id}/action")
        assert status == 403
        assert gs.pending_action is not None  # NOT consumed.

        status, data, _ = open_bridge.get(f"/{slot_id}/action", token=SLOT_TOKEN)
        assert status == 200
        assert data["action"]["index"] == 1

    def test_reserved_slot_command_not_consumed_without_token(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)
        gs = open_bridge.manager.get(slot_id)
        gs.submit_command({"name": "save"})

        status, _, _ = open_bridge.get(f"/{slot_id}/command")
        assert status == 403
        assert gs.pending_command is not None

        status, data, _ = open_bridge.get(f"/{slot_id}/command", token=SLOT_TOKEN)
        assert status == 200
        assert data["command"]["name"] == "save"

    def test_all_data_bearing_gets_gated_when_reserved(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)
        for route in ("/status", "/state", "/screen", "/game_state",
                      "/pending", "/transcript", "/context", "/inventory",
                      "/screenshot"):
            status, _, _ = open_bridge.get(f"/{slot_id}{route}")
            assert status == 403, route

    def test_unreserved_slot_stays_open_on_open_bridge(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.get(f"/{slot_id}/state")
        assert status == 200
        assert "transcript" in data

    def test_require_token_gates_even_unreserved_slots(self, strict_bridge):
        slot_id = _assign_slot(strict_bridge, token=SLOT_TOKEN)
        # Assign with a token auto-reserves; but even a manually-created
        # unreserved slot is gated in require-token mode.
        sid2 = strict_bridge.manager.assign("othergame")
        status, _, _ = strict_bridge.get(f"/{sid2}/state")
        assert status == 403
        status, _, _ = strict_bridge.get(f"/{sid2}/state", token=ADMIN_TOKEN)
        assert status == 200


# ---------------------------------------------------------------------------
# Token gating on POST routes (incl. shim pushes)
# ---------------------------------------------------------------------------


class TestReservedSlotPostGating:
    def test_external_inventory_update_does_not_require_shim_protocol(
        self, open_bridge,
    ):
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(
            f"/{slot_id}/inventory",
            {"add": [{"name": "Keycard"}]},
            shim_protocol=False,
        )
        assert status == 200
        assert data["status"] == "accepted"

    def test_inventory_update_requires_explicit_slot(self, open_bridge):
        _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(
            "/inventory",
            {"add": [{"name": "Keycard"}]},
            shim_protocol=False,
        )
        assert status == 400
        assert "explicit slot prefix" in data["error"]

    def test_forged_game_ended_rejected_without_token(self, open_bridge):
        """POST /event with game_ended frees the slot — must be gated."""
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)

        status, _, _ = open_bridge.post(
            f"/{slot_id}/event", {"type": "game_ended", "reason": "forged"})
        assert status == 403
        assert open_bridge.manager.get(slot_id) is not None  # not freed

        status, _, _ = open_bridge.post(
            f"/{slot_id}/event",
            {"type": "dialogue", "text": "hi"},
            token=SLOT_TOKEN,
        )
        assert status == 200

    def test_request_push_gated_when_reserved(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)
        status, _, _ = open_bridge.post(
            f"/{slot_id}/request",
            {"type": "choice_request", "id": "x", "choices": ["A"]},
        )
        assert status == 403

    def test_act_and_command_gated_when_reserved(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)
        status, _, _ = open_bridge.post(f"/{slot_id}/act", {"type": "act", "index": 1})
        assert status == 403
        status, _, _ = open_bridge.post(f"/{slot_id}/command", {"name": "save"})
        assert status == 403

    def test_command_queue_full_returns_409(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        gs = open_bridge.manager.get(slot_id)
        for i in range(gs._MAX_PENDING_COMMANDS):
            status, _, _ = open_bridge.post(f"/{slot_id}/command", {"name": f"c{i}"})
            assert status == 200
        status, data, _ = open_bridge.post(f"/{slot_id}/command", {"name": "overflow"})
        assert status == 409
        assert "queue is full" in data["error"]


# ---------------------------------------------------------------------------
# Slot assignment + reservation flow (shim path)
# ---------------------------------------------------------------------------


class TestAssignAndReserve:
    def test_assign_with_token_header_auto_reserves(self, open_bridge):
        """The shim sends X-Slot-Token on /slots/assign (from the
        VNFLIGHT_SLOT_TOKEN env var) and the slot is born reserved."""
        status, data, _ = open_bridge.post(
            "/slots/assign", {
                "game_id": "testgame",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            }, token=SLOT_TOKEN)
        assert status == 200
        assert data.get("token") == SLOT_TOKEN
        slot_id = data["slot_id"]

        status, _, _ = open_bridge.get(f"/{slot_id}/state")
        assert status == 403
        status, _, _ = open_bridge.get(f"/{slot_id}/state", token=SLOT_TOKEN)
        assert status == 200

    def test_assign_without_token_rejected_in_require_token_mode(self, strict_bridge):
        status, _, _ = strict_bridge.post("/slots/assign", {"game_id": "g"})
        assert status == 403

    def test_assign_rejects_missing_or_wrong_shim_protocol(self, open_bridge):
        status, data, _ = open_bridge.post(
            "/slots/assign", {"game_id": "old-shim"},
        )
        assert status == 409
        assert data["reason"] == "shim_protocol_mismatch"
        assert data["expected"] == SHIM_PROTOCOL_VERSION
        assert data["received"] is None
        assert "install-shim" in data["remediation"]

        status, data, _ = open_bridge.post(
            "/slots/assign", {
                "game_id": "future-shim",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION + 1,
            },
        )
        assert status == 409
        assert data["received"] == SHIM_PROTOCOL_VERSION + 1

    def test_protocol_rejection_is_visible_only_to_its_launch_token(self, open_bridge):
        token = "stale-launch-secret"
        status, data, _ = open_bridge.post(
            "/slots/assign",
            {"game_id": "old-shim", "game_pid": 4321},
            token=token,
            shim_protocol=False,
        )
        assert status == 409
        assert open_bridge.manager.list_slots() == []

        status, rejection, _ = open_bridge.get(
            "/registration-rejection", token=token, shim_protocol=False,
        )
        assert status == 200
        record = rejection["registration_rejection"]
        assert record["reason"] == "shim_protocol_mismatch"
        assert record["game_id"] == "old-shim"
        assert record["game_pid"] == 4321
        assert record["expected_protocol"] == SHIM_PROTOCOL_VERSION
        assert record["received_protocol"] is None
        assert record["reservation_id"] == bridge.hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()[:16]
        assert token not in json.dumps(rejection)

        status, _, _ = open_bridge.get(
            "/registration-rejection", token="another-launch",
            shim_protocol=False,
        )
        assert status == 404
        status, _, _ = open_bridge.get(
            "/registration-rejection", shim_protocol=False,
        )
        assert status == 403

    def test_protocol_rejection_bounds_non_scalar_diagnostics(self, open_bridge):
        token = "malformed-protocol-launch"
        status, response, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "old-shim",
                "shim_protocol_version": {"nested": ["x" * 10000]},
            },
            token=token,
        )
        assert status == 409
        assert response["received"] == "<dict>"
        assert len(json.dumps(response)) < 2000

        status, data, _ = open_bridge.get(
            "/registration-rejection", token=token, shim_protocol=False,
        )
        assert status == 200
        assert data["registration_rejection"]["received_protocol"] == "<dict>"
        assert len(json.dumps(data)) < 2000

    def test_successful_assignment_preserves_unrelated_terminal_rejection(
        self, open_bridge,
    ):
        token = "retry-launch-secret"
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "retry",
                "game_pid": 101,
                "launch_id": "older-attempt",
            },
            token=token,
            shim_protocol=False,
        )
        assert status == 409

        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "retry",
                "game_pid": 202,
                "launch_id": "current-attempt",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token=token,
        )
        assert status == 200
        status, _, _ = open_bridge.get(
            "/registration-rejection", token=token, shim_protocol=False,
        )
        assert status == 200

    def test_slots_full_is_transient_and_does_not_publish_rejection(self, open_bridge):
        open_bridge.manager.max_slots = 1
        occupied = _assign_slot(open_bridge, game_id="occupied")
        token = "waiting-launch"

        status, data, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "waiting",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token=token,
        )
        assert status == 503
        assert data["status"] == "full"
        status, _, _ = open_bridge.get(
            "/registration-rejection", token=token, shim_protocol=False,
        )
        assert status == 404

        assert open_bridge.manager.free(occupied) is True
        status, data, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "waiting",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token=token,
        )
        assert status == 200
        assert data["game_id"] == "waiting"

    def test_reservation_conflict_promotes_after_declared_retry_deadline(
        self, open_bridge, monkeypatch,
    ):
        clock = {"now": 100.0}
        monkeypatch.setattr(bridge.time, "time", lambda: clock["now"])
        status, assigned, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 4321,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="old-reservation",
        )
        assert status == 200

        status, conflict, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 4321,
                "launch_id": "finite-launch",
                "registration_retry_mode": "finite",
                "registration_retry_until": 108.0,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="new-reservation",
        )
        assert status == 409
        assert conflict["status"] == "reserved"
        assert conflict["slot_id"] == assigned["slot_id"]

        status, pending, _ = open_bridge.get(
            "/registration-rejection",
            token="new-reservation",
            shim_protocol=False,
        )
        assert status == 200
        assert pending["registration_rejection"]["transient"] is True
        assert pending["registration_rejection"]["first_rejected_at"] == 100.0
        assert pending["registration_rejection"]["retry_mode"] == "finite"
        assert pending["registration_rejection"]["retry_until"] == 108.0

        clock["now"] += 3.0
        status, still_pending, _ = open_bridge.get(
            "/registration-rejection",
            token="new-reservation",
            shim_protocol=False,
        )
        assert status == 200
        assert still_pending["registration_rejection"]["transient"] is True

        clock["now"] += 6.0
        status, promoted, _ = open_bridge.get(
            "/registration-rejection",
            token="new-reservation",
            shim_protocol=False,
        )
        assert status == 200
        assert promoted["registration_rejection"]["transient"] is False
        assert promoted["registration_rejection"]["first_rejected_at"] == 100.0

        clock["now"] += 10.0
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 4321,
                "launch_id": "fresh-launch",
                "registration_retry_mode": "finite",
                "registration_retry_until": 127.0,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="new-reservation",
        )
        assert status == 409
        status, fresh, _ = open_bridge.get(
            "/registration-rejection?game_id=warm-game&launch_id=fresh-launch",
            token="new-reservation",
            shim_protocol=False,
        )
        assert status == 200
        assert fresh["registration_rejection"]["transient"] is True
        assert fresh["registration_rejection"]["first_rejected_at"] == 119.0

    def test_warm_recovery_conflict_stays_pending_until_success(
        self, open_bridge, monkeypatch,
    ):
        clock = {"now": 100.0}
        monkeypatch.setattr(bridge.time, "time", lambda: clock["now"])
        status, assigned, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 4321,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="old-reservation",
        )
        assert status == 200

        recovery_body = {
            "game_id": "warm-game",
            "game_pid": 4321,
            "launch_id": "warm-recovery",
            "registration_retry_mode": "recovery",
            "shim_protocol_version": SHIM_PROTOCOL_VERSION,
        }
        status, _, _ = open_bridge.post(
            "/slots/assign", recovery_body, token="new-reservation",
        )
        assert status == 409

        clock["now"] += 60.0
        status, pending, _ = open_bridge.get(
            "/registration-rejection",
            token="new-reservation",
            shim_protocol=False,
        )
        assert status == 200
        assert pending["registration_rejection"]["transient"] is True
        assert pending["registration_rejection"]["retry_mode"] == "recovery"

        assert open_bridge.manager.free(assigned["slot_id"]) is True
        status, _, _ = open_bridge.post(
            "/slots/assign", recovery_body, token="new-reservation",
        )
        assert status == 200
        status, _, _ = open_bridge.get(
            "/registration-rejection",
            token="new-reservation",
            shim_protocol=False,
        )
        assert status == 404

    def test_shared_launch_token_preserves_terminal_failure_across_recovery(
        self, open_bridge,
    ):
        status, assigned, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 222,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="old-reservation",
        )
        assert status == 200

        shared = "shared-launch-token"
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 111,
                "launch_id": "shared-launch",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION - 1,
            },
            token=shared,
            shim_protocol=False,
        )
        assert status == 409
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 222,
                "launch_id": "shared-launch",
                "registration_retry_mode": "recovery",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token=shared,
        )
        assert status == 409
        assert len(open_bridge.manager.registration_rejections(shared)) == 2

        query = "?game_id=warm-game&launch_id=shared-launch"
        status, rejection, _ = open_bridge.get(
            "/registration-rejection" + query,
            token=shared,
            shim_protocol=False,
        )
        assert status == 200
        assert rejection["registration_rejection"]["reason"] == (
            "shim_protocol_mismatch"
        )
        assert rejection["registration_rejection"]["transient"] is False

        assert open_bridge.manager.free(assigned["slot_id"]) is True
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "warm-game",
                "game_pid": 222,
                "launch_id": "shared-launch",
                "registration_retry_mode": "recovery",
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token=shared,
        )
        assert status == 200
        remaining = open_bridge.manager.registration_rejections(shared)
        assert [record["reason"] for record in remaining] == [
            "shim_protocol_mismatch"
        ]

    def test_success_clears_all_rejections_for_exact_registration_attempt(
        self, open_bridge,
    ):
        body = {
            "game_id": "warm-game",
            "game_pid": 222,
            "launch_id": "same-launch",
        }
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {**body, "shim_protocol_version": SHIM_PROTOCOL_VERSION - 1},
            token="same-token",
            shim_protocol=False,
        )
        assert status == 409

        status, _, _ = open_bridge.post(
            "/slots/assign",
            {**body, "shim_protocol_version": SHIM_PROTOCOL_VERSION},
            token="same-token",
        )
        assert status == 200
        assert open_bridge.manager.registration_rejections("same-token") == []

    def test_finite_retry_deadline_is_not_silently_clamped_to_30_seconds(
        self, open_bridge, monkeypatch,
    ):
        clock = {"now": 100.0}
        monkeypatch.setattr(bridge.time, "time", lambda: clock["now"])
        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "slow-start",
                "game_pid": 200,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="old-reservation",
        )
        assert status == 200

        status, _, _ = open_bridge.post(
            "/slots/assign",
            {
                "game_id": "slow-start",
                "game_pid": 200,
                "launch_id": "slow-launch",
                "registration_retry_mode": "finite",
                "registration_retry_until": 140.0,
                "shim_protocol_version": SHIM_PROTOCOL_VERSION,
            },
            token="slow-reservation",
        )
        assert status == 409

        clock["now"] = 135.0
        pending = open_bridge.manager.registration_rejection(
            "slow-reservation", launch_id="slow-launch",
        )
        assert pending["transient"] is True
        assert pending["retry_until"] == 140.0

        clock["now"] = 141.0
        promoted = open_bridge.manager.registration_rejection(
            "slow-reservation", launch_id="slow-launch",
        )
        assert promoted["transient"] is False

    def test_rejected_shim_cannot_use_sole_slot_fallback(self, open_bridge):
        status, _, _ = open_bridge.post(
            "/slots/assign", {"game_id": "stale-shim"},
        )
        assert status == 409

        slot_id = _assign_slot(open_bridge, game_id="current-shim")
        gs = open_bridge.manager.get(slot_id)
        gs.submit_command({"name": "save"})
        before_events = gs.event_counter

        status, data, _ = open_bridge.post(
            "/event", {"type": "dialogue", "text": "stale"},
            shim_protocol=False,
        )
        assert status == 409
        assert data["reason"] == "shim_protocol_mismatch"
        assert gs.event_counter == before_events

        status, data, _ = open_bridge.get(
            "/command", shim_protocol=False,
        )
        assert status == 409
        assert data["reason"] == "shim_protocol_mismatch"
        assert gs.pending_command is not None

        status, data, _ = open_bridge.get(f"/{slot_id}/command")
        assert status == 200
        assert data["command"]["name"] == "save"

        gs.push_event({"type": "dialogue", "text": "keep me"})
        before_reset = gs.event_counter
        status, data, _ = open_bridge.post(
            "/reset", token=ADMIN_TOKEN, shim_protocol=False,
        )
        assert status == 409
        assert data["reason"] == "shim_protocol_mismatch"
        assert gs.event_counter == before_reset

    def test_assign_conflicting_token_does_not_report_success(self, open_bridge):
        status, first, _ = open_bridge.post(
            "/slots/assign",
            {"game_id": "testgame", "game_pid": 1234,
             "shim_protocol_version": SHIM_PROTOCOL_VERSION},
            token="first-token",
        )
        assert status == 200

        status, second, _ = open_bridge.post(
            "/slots/assign",
            {"game_id": "testgame", "game_pid": 1234,
             "shim_protocol_version": SHIM_PROTOCOL_VERSION},
            token="second-token",
        )
        assert status == 409
        assert second["status"] == "reserved"
        assert second["slot_id"] == first["slot_id"]
        assert open_bridge.manager.get(first["slot_id"]) is not None

    def test_reserve_second_caller_conflicts(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(
            "/reserve", {"slot_hint": str(slot_id), "token": "first"})
        assert status == 200
        status, data, _ = open_bridge.post(
            "/reserve", {"slot_hint": str(slot_id), "token": "second"})
        assert status == 409

    def test_admin_can_force_reserve(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.post("/reserve", {"slot_hint": str(slot_id), "token": "first"})
        status, data, _ = open_bridge.post(
            "/reserve", {"slot_hint": str(slot_id), "token": "second"},
            token=ADMIN_TOKEN)
        assert status == 200
        assert data["token"] == "second"
        assert open_bridge.manager.check_token(slot_id, "first") is False


# ---------------------------------------------------------------------------
# Browser-attack guards: CORS removal, Host allowlist, Content-Type
# ---------------------------------------------------------------------------


class TestBrowserGuards:
    def test_no_cors_headers_in_responses(self, open_bridge):
        _assign_slot(open_bridge)
        for path in ("/", "/slots", "/status"):
            status, _, headers = open_bridge.get(path)
            assert status == 200
            lower = {k.lower() for k in headers}
            assert "access-control-allow-origin" not in lower, path

    def test_options_no_cors_approval(self, open_bridge):
        status, _, headers = open_bridge.request("OPTIONS", "/")
        lower = {k.lower() for k in headers}
        assert "access-control-allow-origin" not in lower
        assert "access-control-allow-methods" not in lower

    def test_dns_rebinding_host_rejected(self, open_bridge):
        status, data, _ = open_bridge.get(
            "/slots", headers={"Host": "evil.example.com"})
        assert status == 403

    def test_local_and_ip_literal_hosts_allowed(self, open_bridge):
        for host in ("127.0.0.1:9999", "localhost", "192.168.1.4:8385", "[::1]:8385"):
            status, _, _ = open_bridge.get("/slots", headers={"Host": host})
            assert status == 200, host

    def test_allowed_hosts_extension(self, open_bridge):
        open_bridge.server.allowed_hosts = {"bridge.lan"}
        status, _, _ = open_bridge.get("/slots", headers={"Host": "bridge.lan:8385"})
        assert status == 200

    def test_cross_origin_header_rejected(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        # A browser "simple GET" (fetch no-cors / img tag) carries the
        # page's Origin or Referer — reject non-local web origins even
        # for unreserved slots.
        status, _, _ = open_bridge.get(
            f"/{slot_id}/action", headers={"Origin": "https://evil.example.com"})
        assert status == 403
        status, _, _ = open_bridge.get(
            f"/{slot_id}/action", headers={"Referer": "https://evil.example.com/page"})
        assert status == 403
        status, _, _ = open_bridge.get(
            f"/{slot_id}/action", headers={"Origin": "null"})
        assert status == 403

    def test_local_origin_allowed(self, open_bridge):
        status, _, _ = open_bridge.get(
            "/slots", headers={"Origin": "http://localhost:3000"})
        assert status == 200

    def test_post_requires_json_content_type(self, open_bridge):
        """Browser simple POSTs are limited to text/plain and form
        content-types; requiring application/json forces a preflight
        the bridge never approves."""
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(
            f"/{slot_id}/event", {"type": "game_ended", "reason": "forged"},
            content_type="text/plain")
        assert status == 415

    def test_post_form_content_type_rejected(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        status, _, _ = open_bridge.post(
            f"/{slot_id}/command", {"name": "save"},
            content_type="application/x-www-form-urlencoded")
        assert status == 415


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


class TestAdminRoutes:
    def test_global_reset_requires_admin(self, open_bridge):
        status, _, _ = open_bridge.post("/reset")
        assert status == 403
        status, _, _ = open_bridge.post("/reset", token=ADMIN_TOKEN)
        assert status == 200

    def test_unreserve_requires_admin(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        open_bridge.manager.reserve(str(slot_id), token=SLOT_TOKEN)
        status, _, _ = open_bridge.post("/unreserve", {"slot": slot_id})
        assert status == 403
        status, _, _ = open_bridge.post(
            "/unreserve", {"slot": slot_id}, token=ADMIN_TOKEN)
        assert status == 200


# ---------------------------------------------------------------------------
# GET /slots gating in require-token mode
# ---------------------------------------------------------------------------


class TestSlotsListingGating:
    """GET /slots (and the slots metadata mirrored on /status and /)
    carries game ids, PIDs, and reserved flags.  Open bridges keep it
    open for discovery; --require-token bridges gate it behind the admin
    token or any slot reservation token."""

    def test_slots_open_on_open_bridge(self, open_bridge):
        _assign_slot(open_bridge)
        status, data, _ = open_bridge.get("/slots")
        assert status == 200
        assert isinstance(data["slots"], list)
        assert len(data["slots"]) == 1

    def test_slots_tokenless_denied_in_require_token_mode(self, strict_bridge):
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, data, _ = strict_bridge.get("/slots")
        assert status == 403
        # Marker-compatible with harness _bridge_token_mismatch detection.
        assert "missing token" in data["error"].lower()

    def test_slots_admin_token_allowed_in_require_token_mode(self, strict_bridge):
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, data, _ = strict_bridge.get("/slots", token=ADMIN_TOKEN)
        assert status == 200
        assert len(data["slots"]) == 1

    def test_slots_reservation_token_allowed_in_require_token_mode(self, strict_bridge):
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, data, _ = strict_bridge.get("/slots", token=SLOT_TOKEN)
        assert status == 200
        assert len(data["slots"]) == 1

    def test_slots_wrong_token_denied_in_require_token_mode(self, strict_bridge):
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, _, _ = strict_bridge.get("/slots", token="wrong-token")
        assert status == 403

    def test_status_stays_200_but_redacts_slots_in_require_token_mode(self, strict_bridge):
        """is_up() probes GET /status and only checks the status code —
        the route must stay 200 tokenless, but without slot metadata
        (game ids, PIDs, sole-slot details)."""
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, data, _ = strict_bridge.get("/status")
        assert status == 200
        assert not data.get("slots")
        assert "game_pid" not in json.dumps(data)

    def test_status_full_with_admin_token_in_require_token_mode(self, strict_bridge):
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, data, _ = strict_bridge.get("/status", token=ADMIN_TOKEN)
        assert status == 200
        assert len(data["slots"]) == 1

    def test_root_redacts_slots_in_require_token_mode(self, strict_bridge):
        _assign_slot(strict_bridge, token=SLOT_TOKEN)
        status, data, _ = strict_bridge.get("/")
        assert status == 200
        assert not data.get("slots")

    def test_status_and_root_keep_slots_on_open_bridge(self, open_bridge):
        _assign_slot(open_bridge)
        status, data, _ = open_bridge.get("/status")
        assert status == 200
        assert len(data["slots"]) == 1
        status, data, _ = open_bridge.get("/")
        assert status == 200
        assert len(data["slots"]) == 1


# ---------------------------------------------------------------------------
# POST /config — bridge-side runtime config (end_on_menu_return)
# ---------------------------------------------------------------------------


class TestConfigRoute:
    def test_config_accepts_end_on_menu_return(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(
            f"/{slot_id}/config",
            {"config": {"end_on_menu_return": False}})
        assert status == 200
        assert data["config"]["end_on_menu_return"] is False
        # Round-trips through /state alongside the auto_advance neighbors.
        status, state, _ = open_bridge.get(f"/{slot_id}/state")
        assert status == 200
        assert state["config"]["end_on_menu_return"] is False
        assert "auto_advance" in state["config"]

    def test_config_read_reports_default_true(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(f"/{slot_id}/config", {})
        assert status == 200
        assert data["config"]["end_on_menu_return"] is True

    def test_config_coerces_junk_like_neighbors(self, open_bridge):
        """auto_advance bool()-coerces whatever arrives; the new key
        must behave identically (0 -> False, non-empty string -> True)."""
        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.post(
            f"/{slot_id}/config",
            {"config": {"end_on_menu_return": 0, "auto_advance": 0}})
        assert status == 200
        assert data["config"]["end_on_menu_return"] is False
        assert data["config"]["auto_advance"] is False
        status, data, _ = open_bridge.post(
            f"/{slot_id}/config",
            {"config": {"end_on_menu_return": "junk", "auto_advance": "junk"}})
        assert status == 200
        assert data["config"]["end_on_menu_return"] is True
        assert data["config"]["auto_advance"] is True

    def test_config_gate_suppresses_menu_return_end_over_http(self, open_bridge):
        """End-to-end over the wire: opt-out set via /config, shim pushes
        gameplay then a return_to_menu game_ended — slot must NOT end,
        and the transcript carries the suppressed terminal_evidence."""
        slot_id = _assign_slot(open_bridge)
        status, _, _ = open_bridge.post(
            f"/{slot_id}/config", {"config": {"end_on_menu_return": False}})
        assert status == 200
        for ev in (
            {"type": "game_started"},
            {"type": "context", "context": "in_game"},
            {"type": "narration", "text": "story"},
            {"type": "context", "context": "main_menu"},
            {"type": "game_ended", "reason": "return_to_menu"},
        ):
            status, _, _ = open_bridge.post(f"/{slot_id}/event", ev)
            assert status == 200
        status, state, _ = open_bridge.get(f"/{slot_id}/state")
        assert status == 200
        assert state["status"] != "ended"
        assert state.get("game_terminal") is not True
        evidence = [e for e in state["transcript"]
                    if e.get("type") == "terminal_evidence"]
        assert len(evidence) == 1
        assert evidence[0]["suppressed"] is True
        # The slot survives (return_to_menu never frees; ignored keeps it too).
        status, data, _ = open_bridge.get("/slots")
        assert status == 200
        assert len(data["slots"]) == 1

    def test_quit_still_ends_and_frees_with_gate_off(self, open_bridge):
        slot_id = _assign_slot(open_bridge)
        status, _, _ = open_bridge.post(
            f"/{slot_id}/config", {"config": {"end_on_menu_return": False}})
        assert status == 200
        status, _, _ = open_bridge.post(
            f"/{slot_id}/event", {"type": "game_started"})
        assert status == 200
        status, _, _ = open_bridge.post(
            f"/{slot_id}/event", {"type": "game_ended", "reason": "quit"})
        assert status == 200
        # quit is a real terminal: the slot is auto-freed.
        status, data, _ = open_bridge.get("/slots")
        assert status == 200
        assert data["slots"] == []


class TestVersion:
    def test_root_and_status_carry_the_package_version(self, open_bridge):
        from vnflight import __version__

        status, data, _ = open_bridge.get("/")
        assert status == 200
        assert data["version"] == __version__
        assert data["name"] == "vnflight bridge"

        status, data, _ = open_bridge.get("/status")
        assert status == 200
        assert data["version"] == __version__

        slot_id = _assign_slot(open_bridge)
        status, data, _ = open_bridge.get("/status")
        assert status == 200 and data["version"] == __version__
        status, data, _ = open_bridge.get(f"/{slot_id}/status")
        assert status == 200 and data["version"] == __version__
