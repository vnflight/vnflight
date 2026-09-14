"""Tests for vnflight/handlers.py — shared tool handler implementations."""

import threading
import time

import pytest
from dataclasses import dataclass, field, replace
from typing import Any
from unittest.mock import ANY
from vnflight.delivery_ownership import ActionDeliveryOwnership


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------

@dataclass
class MockWaitResult:
    events: list = field(default_factory=list)
    pending: dict | None = None
    ended: bool = False
    screen: dict | None = None
    transaction: dict | None = None
    foreign_action_boundary: bool = False


class MockClient(ActionDeliveryOwnership):
    """Minimal mock satisfying the GameClient protocol."""

    def __init__(self):
        self.slot_prefix = "/test"
        self.bridge_url = "http://mock:8385"
        self.last_request_id = None
        self.last_request_type = None
        self.last_choices = None
        self.last_actionable_snapshot = None
        self.cursor = 0

        # Configurable responses.
        self._wait_result = MockWaitResult()
        self._pending = None
        self._screenshot = None
        self._screen = None
        self._screen_provider = None
        self._state = {}
        self._game_state = None
        self._transcript = []
        self._command_results = {}
        self._act_result = {"ok": True}
        self._input_result = {"ok": True}
        self._prefetched_events = []
        self._delivered_action_events = set()
        # Transactional act support.  Mirrors BridgeClient.act_transaction:
        # a successful submission returns an ACCEPTED acknowledgment (no
        # resolved_as yet), and the settled record — carrying the shim's
        # resolution — is served by a scoped wait(action_nonce=...).
        self._act_transaction_result: dict | None = None
        self._act_transactions: dict[str, dict] = {}
        self._active_action_nonces: list[str] = []
        self._last_command_nonce: str | None = None
        self._next_mock_action_id = 1

        # Call tracking.
        self.calls: list[tuple[str, Any]] = []

    def reset_action_event_delivery(self):
        self._delivered_action_events.clear()

    def retire_auto_action_nonces(
        self, reason, *, preserve_nonce=None, preserve_nonces=None,
    ):
        preserved = set(preserve_nonces or ())
        if preserve_nonce is not None:
            preserved.add(preserve_nonce)
        self._active_action_nonces = [
            nonce for nonce in self._active_action_nonces
            if nonce in preserved
        ]

    def claim_undelivered_action_events(
        self, events, *, reset_generation=None,
    ):
        claimed = []
        for event in events:
            key = (event.get("action_id", 0), event.get("_seq", 0))
            if key[0] > 0 and key[1] > 0:
                if key in self._delivered_action_events:
                    continue
                self._delivered_action_events.add(key)
            claimed.append(event)
        return claimed

    def _record_delivered_action_events(self, keys, reset_generation=None):
        self._delivered_action_events.update(keys)

    def claim_prefetched_visible_prefix(
        self, before_seq, *, ordinary_action_id=None, action_scoped=False,
    ):
        from vnflight.client import BridgeClient
        return BridgeClient.claim_prefetched_visible_prefix(
            self,
            before_seq,
            ordinary_action_id=ordinary_action_id,
            action_scoped=action_scoped,
        )

    @property
    def action_calls(self) -> list[tuple[str, Any]]:
        """User-facing action calls only (excludes side-effect HTTP probes)."""
        return [
            c for c in self.calls
            if c[0] not in (
                "_get", "_send_command", "act_transaction",
                "action_transaction",
            )
        ]

    def wait(self, timeout=60, **kw):
        self.calls.append(("wait", {"timeout": timeout, **kw}))
        result = self._wait_result
        nonce = kw.get("action_nonce")
        if nonce and getattr(result, "transaction", None) is None:
            record = self._act_transactions.get(nonce)
            if record is not None:
                result = replace(result, transaction=dict(record))
        return result

    def pending(self, *, timeout=3.0):
        self.calls.append(("pending", {"timeout": timeout}))
        return self._pending

    def act(self, target, _nonce=None):
        self.calls.append(("act", target))
        return self._act_result

    def act_transaction(
        self, target, *, action_nonce=None, accept_timeout=15.0,
        deadline=None, invocation=None,
    ):
        """Accept an act and return the durable acknowledgment.

        Submission is delegated to ``self.act`` so a test that stubs the
        submission (transport timeouts, resync retries) exercises the
        transactional path with its stub intact; ``act`` records the
        ``("act", target)`` call as before.
        """
        call = {
            "target": target, "action_nonce": action_nonce,
            "accept_timeout": accept_timeout,
        }
        if invocation is not None:
            call["invocation"] = invocation
        self.calls.append(("act_transaction", call))
        if self._act_transaction_result is not None:
            return dict(self._act_transaction_result)
        base = dict(self.act(target) or {})
        if not base.get("success", base.get("ok")):
            # A rejected submission never becomes a transaction — the real
            # client returns the bridge's 400/409 body unchanged.
            return base
        nonce = action_nonce or f"mock-nonce-{self._next_mock_action_id}"
        action_id = self._next_mock_action_id
        self._next_mock_action_id += 1
        self._last_command_nonce = nonce
        if nonce not in self._active_action_nonces:
            self._active_action_nonces.append(nonce)
        settled = {
            "action_nonce": nonce,
            "action_id": action_id,
            "transaction_state": "settled",
            "pending": False,
            "ok": True,
            "success": True,
            "submitted_target": target,
            "resolved_as": base.get("resolved_as", "choice"),
        }
        # The applied/settled record carries the shim's full resolution
        # payload, matching what the bridge copies off the act command_result.
        for key, value in base.items():
            if key in {"ok", "success", "error"}:
                continue
            settled.setdefault(key, value)
        self._act_transactions[nonce] = settled
        ack = {
            "action_nonce": nonce,
            "action_id": action_id,
            "transaction_state": "accepted",
            "pending": True,
            "ok": True,
            "success": True,
            "submitted_target": target,
            "reset_generation": 0,
        }
        return ack

    def action_transaction(self, action_nonce, *, timeout=3.0):
        """Peek at a transaction (never drains, never acknowledges)."""
        self.calls.append(("action_transaction", action_nonce))
        record = self._act_transactions.get(action_nonce)
        return dict(record) if record is not None else None

    def discard_rendered_action_transaction(
        self, action_nonce, *, action_id=None, timeout=1.5,
    ):
        self.calls.append(("discard_rendered_action_transaction", {
            "action_nonce": action_nonce,
            "action_id": action_id,
            "timeout": timeout,
        }))
        if action_nonce in self._active_action_nonces:
            self._active_action_nonces.remove(action_nonce)
        record = self._act_transactions.get(action_nonce)
        return MockWaitResult(
            transaction=dict(record) if record is not None else None,
        )

    def _retire_auto_action_nonce(self, action_nonce, reason):
        self.calls.append(("_retire_auto_action_nonce", {
            "action_nonce": action_nonce,
            "reason": reason,
        }))
        if action_nonce in self._active_action_nonces:
            self._active_action_nonces.remove(action_nonce)

    def input_text(self, text, *, deadline=None, request_id=None):
        self.calls.append(("input_text", text))
        if deadline is not None or request_id is not None:
            self.calls.append(("input_text_options", {
                "deadline": deadline,
                "request_id": request_id,
            }))
        return self._input_result

    def poll(self, timeout=0, include_prefetched=True):
        self.calls.append(("poll", {"timeout": timeout, "include_prefetched": include_prefetched}))
        return []

    def screenshot(self):
        self.calls.append(("screenshot", {}))
        return self._screenshot

    def state(self, *, timeout=3.0):
        self.calls.append(("state", {"timeout": timeout}))
        from vnflight.client import actionable_state_snapshot
        self.last_actionable_snapshot = actionable_state_snapshot(self._state)
        return dict(self._state)

    def game_state(self, *, timeout=2.0):
        self.calls.append(("game_state", {"timeout": timeout}))
        return self._game_state

    def transcript(self, last=20):
        self.calls.append(("transcript", last))
        return self._transcript[:last]

    def command(self, cmd_name, **args):
        self.calls.append(("command", (cmd_name, args)))
        return self._command_results.get(cmd_name, {"ok": True})

    def _wait_command_result(self, cmd_name, timeout=3.0, after_seq=None, match=None):
        self.calls.append(("_wait_command_result", {
            "command": cmd_name,
            "timeout": timeout,
            "after_seq": after_seq,
            "match": match is not None,
        }))
        result = self._command_results.get(cmd_name)
        if result is not None and match is not None and not match(result):
            return None
        return result

    def set_auto_advance(self, enabled, delay=None, *, deadline=None):
        payload = {"enabled": enabled, "delay": delay}
        if deadline is not None:
            payload["deadline"] = deadline
        self.calls.append(("set_auto_advance", payload))
        return {"ok": True}

    def _get(self, path, timeout=5.0):
        self.calls.append(("_get", path))
        if path == "/state":
            return (200, dict(self._state))
        if path == "/screen":
            screen = (
                self._screen_provider()
                if callable(self._screen_provider) else self._screen
            )
            return (200, {"screen": screen})
        return (404, None)

    def _send_command(self, name, args=None, nonce=None, *, timeout=15.0):
        self.calls.append(("_send_command", (name, args)))
        return (True, f"Command '{name}' sent.")


# ---------------------------------------------------------------------------
# Scripted bridge client — opt-in realistic poll model
# ---------------------------------------------------------------------------

def ScriptedBridgeClient(
    url: str = "http://bridge",
    *,
    slot_prefix: str = "/1",
    transcript: list[dict] | None = None,
    screen: dict | None = None,
    state: dict | None = None,
):
    """Build a real ``BridgeClient`` wired to a scripted HTTP transport.

    Opt-in fidelity helper for SEQUENCE tests.  ``MockClient.poll()`` returns
    ``[]`` and the class carries no ``_state_poll_serial``, so it models none
    of the poll contract; fence tests built on it can only hand-set
    ``cursor``, ``_state_poll_serial`` and ``_prefetched_events``, which makes
    them predicate tables rather than sequences.

    This helper overrides nothing but the transport.  ``poll()``, ``wait()``,
    ``state()``, ``screen()`` and the cross-lane delivery ledger are the
    production implementations, so a test moves the cursor, the prefetch
    stash, the ``ordinary_action_id`` fence + retention, ledger recording
    (``_delivered_action_events``), ``_state_poll_serial`` and
    ``_last_poll_foreign_action_boundary`` exactly the way a live bridge
    moves them.

    The scripted bridge keeps ONE durable transcript and answers ``/state``
    with the rows after ``since`` — the contract the real bridge offers — so a
    cursor that has passed a row cannot see it again, and an exhausted
    transcript yields the empty-but-successful 200 that advances
    ``_state_poll_serial`` without advancing the cursor.

    Usage::

        client = ScriptedBridgeClient()
        client.push_events({"type": "narration", "text": "x", "_seq": 120})
        client.poll(timeout=0)          # -> [row]; cursor 120, serial 1
        client.poll(timeout=0)          # -> [];    cursor 120, serial 2

    The class is built lazily so this module keeps its import-inside-the-test
    convention (the package/single-file ``vnflight`` shadowing dance in
    conftest happens before any test body runs).
    """
    from vnflight.client import BridgeClient

    class _ScriptedBridgeClient(BridgeClient):

        def __init__(self):
            super().__init__(url, slot_prefix=slot_prefix)
            self.script_transcript: list[dict] = [
                dict(event) for event in (transcript or [])
            ]
            self.script_screen: dict | None = screen
            # Non-transcript /state fields (status, pending_request,
            # game_state, reset_generation, ...) merged into every response.
            self.script_state: dict = dict(state or {})
            # Per-nonce receipts, for a sequence that has more than one act
            # in the registry at once.  Falls back to script_state's single
            # "transaction" so existing single-act tests are unaffected.
            self.script_transactions: dict[str, dict] = {}
            # Optional ``(path, payload) -> (code, body) | None`` hook, so a
            # sequence can script POST /command answers (an acceptance, a 409)
            # instead of the blanket 200.
            self.post_handler = None
            self.script_status_code = 200
            self.http_calls: list[tuple[str, dict]] = []
            self.http_posts: list[tuple[str, dict]] = []

        # -- scripting ---------------------------------------------------

        def push_events(self, *events: dict) -> None:
            """Append durable rows a later ``/state`` read will observe."""
            self.script_transcript.extend(dict(event) for event in events)

        def set_screen(self, new_screen: dict | None) -> None:
            self.script_screen = new_screen

        def set_transaction(
            self, transaction: dict | None, *, nonce: str | None = None,
        ) -> None:
            """Script the scoped receipt ``GET /transaction`` answers with.

            Lets a sequence test drive the act settle policy's E1 (the
            bridge's own structural settle) through the production scoped
            drain instead of hand-building a WaitResult.  With *nonce*, the
            receipt answers only that nonce; without it, it is the fallback
            for every nonce.
            """
            if nonce is None:
                self.script_state["transaction"] = transaction
            elif transaction is None:
                self.script_transactions.pop(nonce, None)
            else:
                self.script_transactions[nonce] = transaction

        def state_reads(self) -> int:
            return sum(1 for path, _ in self.http_calls if path == "/state")

        # -- transport ---------------------------------------------------

        def _state_payload(self, params: dict | None) -> dict:
            try:
                since = int((params or {}).get("since") or 0)
            except (TypeError, ValueError):
                since = 0
            seqs = [
                int(event.get("_seq", 0) or 0)
                for event in self.script_transcript
            ]
            payload = dict(self.script_state)
            payload.setdefault("status", "playing")
            payload["transcript"] = [
                dict(event)
                for event, seq in zip(self.script_transcript, seqs)
                if seq > since
            ]
            payload["event_counter"] = max(seqs) if seqs else 0
            return payload

        def _get(self, path, params=None, timeout=5.0):
            self.http_calls.append((path, dict(params or {})))
            if self.script_status_code != 200:
                return self.script_status_code, None
            if path == "/state":
                return 200, self._state_payload(params)
            if path == "/screen":
                return 200, {"screen": self.script_screen}
            if path == "/pending":
                return 200, {
                    "pending": self.script_state.get("pending_request"),
                }
            if path == "/transaction":
                requested = str((params or {}).get("action_nonce") or "")
                transaction = self.script_transactions.get(
                    requested, self.script_state.get("transaction"))
                if transaction is None:
                    return 404, None
                return 200, {"transaction": dict(transaction)}
            if path == "/game_state":
                return 200, {
                    "game_state": self.script_state.get("game_state"),
                }
            if path == "/transcript":
                return 200, {
                    "transcript": [
                        dict(event) for event in self.script_transcript
                    ],
                }
            return 404, None

        def _post(self, path, data, timeout=5.0):
            self.http_posts.append((path, dict(data or {})))
            if self.post_handler is not None:
                answered = self.post_handler(path, dict(data or {}))
                if answered is not None:
                    return answered
            return 200, {"ok": True}

    return _ScriptedBridgeClient()


def _menu_state(*labels, request_id="menu-1"):
    """Minimal rendered-menu state: numeric acts are a reply to a rendered
    numbered list, so tests that act by number need one on screen."""
    return {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": request_id,
            "choices": list(labels),
        },
        "game_state": {
            "interactions": [
                {"source": "choice", "type": "choice", "index": i,
                 "display_label": label, "disabled": False}
                for i, label in enumerate(labels, 1)
            ],
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return MockClient()


@pytest.fixture
def ctx(client):
    from vnflight.handlers import HandlerContext
    return HandlerContext(client=client)


# ---------------------------------------------------------------------------
# handle_act
# ---------------------------------------------------------------------------

def _last_wait_call(client) -> dict:
    """The kwargs of the last client.wait() call."""
    for name, payload in reversed(client.calls):
        if name == "wait":
            return payload
    raise AssertionError("client.wait() was never called")


def _wait_kwargs(client) -> dict:
    """Last wait() kwargs minus the callback for forwarding assertions."""
    return {
        key: value
        for key, value in _last_wait_call(client).items()
        if key != "on_events"
    }


def test_handle_wait_treats_none_timeout_as_default(ctx):
    from vnflight.handlers import handle_wait

    handle_wait(ctx, {"timeout": None})

    assert _wait_kwargs(ctx.client) == {"timeout": 60, "min_wait": 0}
    # A wait with no caller callback still carries the lifecycle-preemption
    # hook, which stays inert until stop/launch asks the lane back.
    hook = _last_wait_call(ctx.client)["on_events"]
    assert hook([]) is None
    ctx._presentation_lane_preempt.set()
    assert hook([]) is True


def test_handle_wait_forwards_nonce_and_surfaces_transaction(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(
        events=[{"type": "dialogue", "text": "Recovered."}],
        transaction={
            "action_nonce": "action-7", "action_id": 7,
            "transaction_state": "applied", "pending": True,
        },
    )

    result = handle_wait(ctx, {"action_nonce": "action-7", "timeout": 2})

    assert _wait_kwargs(ctx.client) == {
        "timeout": 2, "min_wait": 0, "action_nonce": "action-7",
    }
    assert result["transaction_state"] == "applied"
    assert result["action_id"] == 7
    assert result["_data"]["transaction"]["action_nonce"] == "action-7"
    assert 'wait(action_nonce="action-7")' in result["warning"]


def test_handle_wait_omits_recovery_guidance_for_settled_nonce(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(
        events=[{"type": "dialogue", "text": "Recovered."}],
        transaction={
            "action_nonce": "action-8", "action_id": 8,
            "transaction_state": "settled", "pending": False,
        },
    )

    result = handle_wait(ctx, {"action_nonce": "action-8", "timeout": 2})

    assert result["transaction_state"] == "settled"
    assert "warning" not in result


def test_empty_settled_nonce_wait_explains_ordinary_successor(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(transaction={
        "action_nonce": "action-empty",
        "action_id": 9,
        "transaction_state": "settled",
        "pending": False,
    })

    result = handle_wait(ctx, {
        "action_nonce": "action-empty",
        "timeout": 0,
        "_result_deadline": 0,
    })

    rendered = render_tool_result_text(result)
    assert result["transaction_state"] == "settled"
    assert "Transaction settled; no additional transaction output." in rendered
    assert "Call wait() for the current scene or decision." in rendered
    assert "(no new events)" not in rendered


@pytest.mark.parametrize("transaction", [
    {
        "action_nonce": "action-pending",
        "action_id": 11,
        "transaction_state": "accepted",
        "pending": True,
    },
    None,
])
def test_empty_unsettled_nonce_wait_explains_scoped_retry(ctx, transaction):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(transaction=transaction)

    result = handle_wait(ctx, {
        "action_nonce": "action-pending",
        "timeout": 0,
        "_result_deadline": 0,
    })

    rendered = render_tool_result_text(result)
    assert "The transaction is still settling" in rendered
    assert 'wait(action_nonce="action-pending")' in rendered
    assert "(no new events)" not in rendered


def test_empty_admission_open_nonce_wait_points_to_current_scene(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(transaction={
        "action_nonce": "action-open",
        "action_id": 12,
        "transaction_state": "applied",
        "pending": True,
        "admission_open": True,
    })

    rendered = render_tool_result_text(handle_wait(ctx, {
        "action_nonce": "action-open",
        "timeout": 0,
        "_result_deadline": 0,
    }))

    assert "no longer blocks new actions" in rendered
    assert "Call wait()" in rendered
    assert "wait(action_nonce=" not in rendered
    assert "still settling" not in rendered


def test_failed_nonce_wait_surfaces_error_beside_visible_story(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[{"type": "dialogue", "text": "The console rejects it."}],
        transaction={
            "action_nonce": "action-failed",
            "action_id": 13,
            "transaction_state": "failed",
            "pending": False,
            "error": "No matching interaction.",
        },
    )

    result = handle_wait(ctx, {
        "action_nonce": "action-failed",
        "timeout": 0,
        "_result_deadline": 0,
    })
    rendered = render_tool_result_text(result)

    assert "The console rejects it." in rendered
    assert "Transaction failed: No matching interaction." in rendered
    assert result["ok"] is False
    assert result["success"] is False


@pytest.mark.parametrize("surface", ["story", "pending"])
def test_json_settled_nonce_with_visible_output_has_no_empty_receipt(ctx, surface):
    from vnflight.handlers import handle_wait

    kwargs = {
        "transaction": {
            "action_nonce": "json-settled",
            "action_id": 10,
            "transaction_state": "settled",
            "pending": False,
        },
    }
    if surface == "story":
        kwargs["events"] = [{
            "type": "narration", "text": "Visible structured story.",
        }]
    else:
        kwargs["pending"] = {
            "type": "choice_request",
            "id": "json-successor",
            "choices": ["Continue"],
        }
    ctx.client._wait_result = MockWaitResult(**kwargs)

    result = handle_wait(ctx, {
        "action_nonce": "json-settled",
        "timeout": 0,
        "format": "json",
    })

    assert "brief" not in result
    if surface == "story":
        assert result["story"]
    else:
        assert result["pending"]


def test_scoped_wait_drains_terminal_story_to_ordinary_successor(ctx):
    """Fleet r45: Start settled before its first story choice appeared."""
    from vnflight import handlers

    scoped = MockWaitResult(
        events=[{"type": "narration", "text": "ARIA confirms the anomaly."}],
        transaction={
            "action_nonce": "start-1",
            "action_id": 17,
            "transaction_state": "settled",
            "pending": False,
        },
    )
    successor = MockWaitResult(pending={
        "type": "choice_request",
        "id": "specialization",
        "choices": ["Signal processing", "Systems engineering"],
    })
    responses = [scoped, successor]

    def sequenced_wait(timeout=60, **kwargs):
        ctx.client.calls.append(("wait", {"timeout": timeout, **kwargs}))
        return responses.pop(0)

    ctx.client.wait = sequenced_wait
    result = handlers.handle_wait(ctx, {
        "action_nonce": "start-1",
        "timeout": 2,
        "_story_transition_idle_timeout": 1,
    })

    wait_calls = [payload for name, payload in ctx.client.calls if name == "wait"]
    assert wait_calls[0]["action_nonce"] == "start-1"
    assert wait_calls[1]["ordinary_only"] is True
    assert wait_calls[1]["ordinary_action_id"] == 17
    assert "action_nonce" not in wait_calls[1]
    assert "ARIA confirms" in result["text"]
    assert "Signal processing" in result["pending"]
    assert result["transaction_state"] == "settled"
    assert handlers._pending_request_id(result) == "specialization"


def test_empty_scoped_receipt_drains_to_first_successor_story(ctx):
    """Settlement may publish before the action's first visible output."""
    from vnflight import handlers

    scoped = MockWaitResult(transaction={
        "action_nonce": "start-empty",
        "action_id": 18,
        "transaction_state": "settled",
        "pending": False,
    })
    successor = MockWaitResult(pending={
        "type": "choice_request",
        "id": "specialization",
        "choices": ["Signal processing", "Systems engineering"],
    })
    responses = [scoped, successor]

    def sequenced_wait(timeout=60, **kwargs):
        ctx.client.calls.append(("wait", {"timeout": timeout, **kwargs}))
        return responses.pop(0)

    ctx.client.wait = sequenced_wait
    result = handlers.handle_wait(ctx, {
        "action_nonce": "start-empty",
        "timeout": 2,
        "_story_transition_idle_timeout": 1,
    })

    wait_calls = [payload for name, payload in ctx.client.calls if name == "wait"]
    assert len(wait_calls) == 2
    assert wait_calls[1]["ordinary_only"] is True
    assert wait_calls[1]["ordinary_action_id"] == 18
    assert "Signal processing" in result["pending"]
    assert result["transaction_state"] == "settled"


def test_empty_story_tail_stops_after_two_empty_probe_chunks(ctx, monkeypatch):
    from vnflight import handlers

    calls = []

    def empty_wait(_ctx, params):
        calls.append(dict(params))
        return {"text": "(no new events)", "_data": {}}

    monkeypatch.setattr(handlers, "handle_wait", empty_wait)
    result = {"text": "(no new events)", "_data": {}}

    handlers._drain_story_gap_after_choice_action(
        ctx,
        result,
        {
            "_allow_empty_story_tail": True,
            "_ordinary_only": True,
            "_story_transition_idle_timeout": 30,
        },
        result,
    )

    assert len(calls) == 2
    assert all(call["_ordinary_only"] is True for call in calls)


def test_scoped_tail_stops_before_foreign_action_menu(ctx):
    """A newer action's decision cannot precede its retained story rows."""
    from vnflight import handlers

    scoped = MockWaitResult(
        events=[{"type": "narration", "text": "Current action settles."}],
        transaction={
            "action_nonce": "current-action",
            "action_id": 11,
            "transaction_state": "settled",
            "pending": False,
        },
    )
    ownership_boundary = MockWaitResult(
        events=[{"type": "narration", "text": "Current action tail."}],
        foreign_action_boundary=True,
    )
    responses = [scoped, ownership_boundary]

    def sequenced_wait(timeout=60, **kwargs):
        ctx.client.calls.append(("wait", {"timeout": timeout, **kwargs}))
        return responses.pop(0)

    ctx.client.wait = sequenced_wait
    # A globally live menu exists, but belongs after the foreign story that
    # the client retained for its own transaction receipt.
    ctx.client._game_state = {
        "interactions": [{
            "type": "choice",
            "id": "foreign-menu",
            "choices": [{"label": "Foreign choice", "value": 0}],
        }],
    }

    result = handlers.handle_wait(ctx, {
        "action_nonce": "current-action",
        "timeout": 2,
        "_story_transition_idle_timeout": 1,
    })

    assert len(responses) == 0
    assert "Current action settles" in result["text"]
    assert "Current action tail" in result["text"]
    assert "Foreign choice" not in handlers.render_tool_result_text(result)
    assert not result.get("pending")
    assert result["_foreign_action_boundary"] is True


def test_foreign_action_boundary_performs_no_live_state_or_screen_merge(ctx):
    from vnflight import handlers

    ctx.client._wait_result = MockWaitResult(
        events=[{"type": "narration", "text": "Owned prefix."}],
        foreign_action_boundary=True,
    )
    ctx.client._game_state = {
        "interactions": [{
            "type": "choice",
            "id": "foreign-menu",
            "choices": [{"label": "Foreign choice", "value": 0}],
        }],
    }
    ctx.client._state = {
        "status": "waiting_for_input",
        "screen": {
            "buttons": [{"label": "Foreign button", "screen": "menu"}],
            "passive_overlay_text": ["Foreign overlay"],
        },
    }

    result = handlers.handle_wait(ctx, {"timeout": 1})
    rendered = handlers.render_tool_result_text(result)

    assert "Owned prefix" in rendered
    assert "Foreign choice" not in rendered
    assert "Foreign button" not in rendered
    assert "Foreign overlay" not in rendered
    assert not any(name == "game_state" for name, _payload in ctx.client.calls)
    assert not any(name == "_get" for name, _payload in ctx.client.calls)


def test_foreign_boundary_flushes_booked_overlay_without_live_sampling(ctx):
    from vnflight import handlers

    ctx.client._wait_result = MockWaitResult(
        events=[{
            "type": "screen_content",
            "_seq": 74,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["CURRENT ACTION TERMINAL ROW"],
            "overlay_texts": ["CURRENT ACTION TERMINAL ROW"],
            "overlay_texts_by_screen": {
                "terminal": ["CURRENT ACTION TERMINAL ROW"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "overlay_retained_screens": ["terminal"],
        }],
        foreign_action_boundary=True,
    )

    result = handlers.handle_wait(ctx, {"timeout": 1})
    rendered = handlers.render_tool_result_text(result)

    assert "CURRENT ACTION TERMINAL ROW" in rendered
    assert "(no new events)" not in rendered
    assert ctx.overlay.pending_deliveries == []
    assert not any(name == "game_state" for name, _payload in ctx.client.calls)
    assert not any(name == "_get" for name, _payload in ctx.client.calls)


def test_title_return_suppresses_store_reset_against_frozen_final_state(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[
            {"type": "narration", "text": "ENDING: TOGETHER"},
            {
                "type": "stats_update",
                "changed": {
                    "signal_strength": 80,
                    "evidence_count": 0,
                    "aria_integrity": 100,
                },
                "previous": {
                    "signal_strength": 80,
                    "evidence_count": 16,
                    "aria_integrity": 67,
                },
            },
        ],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {
            "signal_strength": 80,
            "evidence_count": 16,
            "aria_integrity": 67,
        },
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }

    result = handle_wait(ctx, {"timeout": 1})
    rendered = render_tool_result_text(result)

    assert "ENDING: TOGETHER" in rendered
    assert "stats updated" not in rendered
    assert "signal_strength: 80" not in rendered
    assert "evidence_count: 0" not in rendered
    assert "--- GAME ENDED ---" in rendered


def test_title_return_preserves_legitimate_incremental_inventory(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[
            {"type": "narration", "text": "ENDING: TOGETHER"},
            {
                "type": "inventory_update",
                "inventory": [{"name": "old"}, {"name": "final proof"}],
                "changed": [{"name": "final proof"}],
                "removed": [],
            },
        ],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {},
        "inventory": [{"name": "old"}, {"name": "final proof"}],
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }

    rendered = render_tool_result_text(handle_wait(ctx, {"timeout": 1}))

    assert "[inventory updated] final proof" in rendered
    assert "[inventory updated] old" not in rendered


def test_title_return_preserves_inventory_chain_ending_at_frozen_state(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[
            {
                "type": "inventory_update",
                "inventory": [{"name": "old"}, {"name": "first"}],
                "changed": [{"name": "first"}],
                "removed": [],
            },
            {
                "type": "inventory_update",
                "inventory": [
                    {"name": "old"},
                    {"name": "first"},
                    {"name": "second"},
                ],
                "changed": [{"name": "second"}],
                "removed": [],
            },
        ],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {},
        "inventory": [
            {"name": "old"},
            {"name": "first"},
            {"name": "second"},
        ],
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }

    rendered = render_tool_result_text(handle_wait(ctx, {"timeout": 1}))

    assert "[inventory updated] first, second" in rendered


def test_title_return_empty_full_snapshot_clears_earlier_inventory(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[
            {
                "type": "inventory_update",
                "inventory": [{"name": "last item"}],
            },
            {
                "type": "inventory_update",
                "inventory": [],
            },
        ],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {},
        "inventory": [],
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }

    rendered = render_tool_result_text(handle_wait(ctx, {"timeout": 1}))

    assert "inventory updated" not in rendered
    assert "last item" not in rendered


def test_title_return_suppresses_proven_post_terminal_inventory_reset(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[
            {"type": "narration", "text": "ENDING: TOGETHER"},
            {
                "type": "inventory_update",
                "inventory": [],
                "changed": [],
                "removed": [{"name": "final proof"}],
                "post_terminal": True,
            },
        ],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {},
        "inventory": [{"name": "final proof"}],
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }

    rendered = render_tool_result_text(handle_wait(ctx, {"timeout": 1}))

    assert "inventory updated" not in rendered
    assert "final proof (removed)" not in rendered


def test_title_return_suppresses_untagged_racing_inventory_reset(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[
            {"type": "narration", "text": "ENDING: TOGETHER"},
            {
                "type": "inventory_update",
                "inventory": [],
                "changed": [],
                "removed": [{"name": "final proof"}],
            },
        ],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {},
        "inventory": [{"name": "final proof"}],
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }

    rendered = render_tool_result_text(handle_wait(ctx, {"timeout": 1}))

    assert "inventory updated" not in rendered
    assert "final proof (removed)" not in rendered


def test_scoped_title_presentation_is_owned_once_per_action_and_run(ctx):
    from vnflight.handlers import (
        _reset_timeline_context,
        handle_wait,
        render_tool_result_text,
    )

    ctx.client._wait_result = MockWaitResult(
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
        transaction={
            "action_nonce": "ending-once",
            "action_id": 44,
            "reset_generation": 3,
            "transaction_state": "applied",
            "pending": True,
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {},
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "ended",
        "context": {"context": "main_menu"},
    }
    params = {"action_nonce": "ending-once", "timeout": 0}

    first = render_tool_result_text(handle_wait(ctx, params))
    second = render_tool_result_text(handle_wait(ctx, params))

    assert "Start" in first
    assert "--- GAME ENDED ---" in first
    assert "Start" not in second
    assert "--- GAME ENDED ---" not in second
    assert "still settling" in second

    _reset_timeline_context(ctx)
    fresh = render_tool_result_text(handle_wait(ctx, params))
    assert "Start" in fresh
    assert "--- GAME ENDED ---" in fresh


def test_wait_caches_custom_screen_only_after_rendering_it(ctx):
    from vnflight.handlers import handle_wait
    from vnflight.settle import screen_signature

    screen = {
        "screens": ["echo_terminal_choice"],
        "_seq": 42,
        "buttons": [{
            "label": "Ask about the signal",
            "screen": "echo_terminal_choice",
            "actions": ["Return"],
        }],
    }
    ctx.client._wait_result = MockWaitResult(screen=screen)

    result = handle_wait(ctx, {"timeout": 0})

    assert "Ask about the signal" in result["buttons"]
    assert ctx.client._last_delivered_actionable_screen == screen
    assert ctx.client._last_delivered_actionable_screen_signature == (
        screen_signature(screen)
    )


def test_wait_does_not_cache_quick_menu_as_actionable_screen(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._last_delivered_actionable_screen = {"_seq": 1}
    ctx.client._last_delivered_actionable_screen_signature = ("old",)
    ctx.client._wait_result = MockWaitResult(
        pending={
            "type": "choice_request",
            "id": "story-choice",
            "choices": ["Continue"],
        },
        screen={
            "screens": ["say"],
            "buttons": [{
                "label": "Q.Save",
                "screen": "quick_menu",
                "actions": ["QuickSave"],
            }],
        },
    )

    result = handle_wait(ctx, {"timeout": 0})

    assert "Continue" in result["pending"]
    assert ctx.client._last_delivered_actionable_screen is None
    assert ctx.client._last_delivered_actionable_screen_signature == ()


def test_wait_does_not_stop_on_a_navigation_only_quick_menu(ctx):
    """MCP side of the quick-menu rule: Q.Load (a FileLoad from QuickLoad())
    on the focus fallback is chrome, not a screen decision."""
    from vnflight.handlers import handle_wait
    from vnflight.lifecycle import has_actionable_screen_buttons

    screen = {
        "screens": ["say", "quick_menu"],
        "buttons": [
            {"label": "Q.Save", "screen": "_focus_list",
             "actions": ["FileTakeScreenshot", "FileSave"]},
            {"label": "Q.Load", "screen": "_focus_list", "actions": ["FileLoad"]},
            {"label": "Skip", "screen": "_focus_list", "actions": ["Skip"]},
        ],
    }
    assert has_actionable_screen_buttons(screen) is False
    ctx.client._wait_result = MockWaitResult(events=[], pending=None, screen=screen)

    result = handle_wait(ctx, {"timeout": 0})

    assert result.get("status") != "screen_actions"
    assert ctx.client._last_delivered_actionable_screen is None


def test_terminal_verdict_defers_banner_while_transaction_is_settling(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[{"type": "narration", "text": "The epilogue begins."}],
        transaction={
            "action_nonce": "ending-1",
            "transaction_state": "applied",
            "pending": True,
        },
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {"evidence_count": 19},
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "playing",
        "context": {"context": "game"},
    }

    result = handle_wait(ctx, {
        "action_nonce": "ending-1",
        "timeout": 1,
    })

    assert result["ended"] is True
    assert result["_defer_ended_banner"] is True
    assert "--- GAME ENDED ---" not in render_tool_result_text(result)
    assert "The epilogue begins" in render_tool_result_text(result)


def test_graph_terminal_plain_wait_defers_banner_until_presentation_boundary(ctx):
    from vnflight.handlers import handle_wait, render_tool_result_text

    ctx.client._wait_result = MockWaitResult(
        events=[{"type": "narration", "text": "The epilogue continues."}],
    )
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {"evidence_count": 19},
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "playing",
        "context": {"context": "game"},
    }

    result = handle_wait(ctx, {"timeout": 0})

    assert result["ended"] is True
    assert result["_defer_ended_banner"] is True
    assert "--- GAME ENDED ---" not in render_tool_result_text(result)


def test_settled_ending_receipt_drains_card_then_real_end(ctx):
    from vnflight import handlers

    scoped = MockWaitResult(
        events=[{"type": "narration", "text": "The epilogue begins."}],
        transaction={
            "action_nonce": "ending-settled",
            "action_id": 44,
            "transaction_state": "settled",
            "pending": False,
        },
    )
    ending_card = MockWaitResult(events=[{
        "type": "narration", "text": "ECHOES OF TOMORROW",
    }])
    title = MockWaitResult(
        events=[{"type": "game_ended", "terminal": True}],
        ended=True,
        screen={
            "main_menu": True,
            "screens": ["main_menu"],
            "buttons": [{"label": "Start", "screen": "main_menu"}],
        },
    )
    responses = [scoped, ending_card, title]

    def sequenced_wait(timeout=60, **kwargs):
        ctx.client.calls.append(("wait", {"timeout": timeout, **kwargs}))
        return responses.pop(0)

    ctx.client.wait = sequenced_wait
    ctx.client._game_state = {
        "game_terminal": True,
        "progress_frozen": True,
        "stats": {"evidence_count": 19},
    }
    ctx.client._state = {
        "game_terminal": True,
        "status": "playing",
        "context": {"context": "game"},
    }

    result = handlers.handle_wait(ctx, {
        "action_nonce": "ending-settled",
        "timeout": 3,
        "_story_transition_idle_timeout": 1,
    })
    rendered = handlers.render_tool_result_text(result)

    assert "The epilogue begins" in rendered
    assert "ECHOES OF TOMORROW" in rendered
    assert "--- GAME ENDED ---" in rendered
    assert result.get("_defer_ended_banner") is not True
    assert result["transaction_state"] == "settled"


@pytest.mark.parametrize(
    "transaction_state", ["accepted", "applied", "acceptance_unknown"])
def test_handle_wait_guides_nonterminal_nonce_without_derived_pending_flag(
    ctx, transaction_state,
):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(transaction={
        "action_nonce": "partial-receipt",
        "transaction_state": transaction_state,
    })

    result = handle_wait(
        ctx, {"action_nonce": "partial-receipt", "timeout": 2})

    assert 'wait(action_nonce="partial-receipt")' in result["warning"]


def test_handle_wait_refetches_hud_controls_after_choice_frame(ctx):
    from vnflight.handlers import handle_wait

    pending = {
        "type": "choice_request", "id": "room-menu",
        "choices": ["Wait", "Leave"],
    }
    base = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Wait", "action_strs": ["Return('wait')"]},
        {"source": "choice", "type": "choice", "index": 2,
         "display_label": "Leave", "action_strs": ["Return('leave')"]},
    ]
    enriched = base + [
        {"source": "button", "type": "other", "index": 3,
         "display_label": "KIT", "action_strs": ["Show('kit')"]},
        {"source": "button", "type": "other", "index": 4,
         "display_label": "LOG", "action_strs": ["Show('log')"]},
    ]
    samples = iter((
        {"interactions": base},
        {
            "interactions": enriched,
            "screen_buttons": [
                {"label": "KIT", "screen": "observatory_hud",
                 "action_strs": ["Show('kit')"]},
                {"label": "LOG", "screen": "observatory_hud",
                 "action_strs": ["Show('log')"]},
            ],
        },
    ))

    def game_state(*, timeout=2.0):
        return next(samples)

    ctx.client.game_state = game_state
    ctx.client._wait_result = MockWaitResult(pending=pending)

    result = handle_wait(ctx, {"timeout": 2})

    assert "KIT" in result["pending"]
    assert "LOG" in result["pending"]
    assert [button["label"] for button in result["_buttons_raw"]] == [
        "KIT", "LOG",
    ]


def test_handle_wait_drops_stale_overlay_when_fresh_menu_outlives_screen_read(ctx):
    from vnflight.handlers import handle_wait

    pending = {
        "type": "choice_request", "id": "reopened-menu",
        "choices": ["Continue", "Leave"],
        "interactions": [
            {"source": "choice", "type": "choice", "index": 1,
             "display_label": "Continue", "action_strs": ["Return('continue')"]},
            {"source": "choice", "type": "choice", "index": 2,
             "display_label": "Leave", "action_strs": ["Return('leave')"]},
        ],
    }
    samples = iter((
        {"_seq": 10, "_source_id": "shim", "_source_seq": 10},
        {"_seq": 12, "_source_id": "shim", "_source_seq": 12,
         "choices": ["Continue", "Leave"], "interactions": [
            {"source": "choice", "type": "choice", "index": 1,
             "display_label": "Continue", "action_strs": ["Return('continue')"]},
            {"source": "choice", "type": "choice", "index": 2,
             "display_label": "Leave", "action_strs": ["Return('leave')"]},
        ]},
    ))
    ctx.client.game_state = lambda **_kwargs: next(samples)
    stale_overlay = {
        "_seq": 11, "_source_id": "shim", "_source_seq": 11,
        "overlay_active": True, "buttons": [{"label": "Close"}],
    }
    ctx.client._wait_result = MockWaitResult(
        pending=pending, screen=stale_overlay)
    # The bridge's stateful endpoint normally returns its last cached screen,
    # not None, when a later noncritical screen_content POST was lost.
    ctx.client._screen = stale_overlay

    result = handle_wait(ctx, {"timeout": 2})

    assert "Continue" in result["pending"]
    assert "Leave" in result["pending"]
    assert "Close" not in result.get("buttons", "")


def test_handle_wait_keeps_overlay_when_fresh_menu_does_not_match_pending(ctx):
    from vnflight.handlers import handle_wait

    pending = {
        "type": "choice_request", "id": "covered-menu",
        "choices": ["Old A", "Old B"],
        "interactions": [
            {"source": "choice", "type": "choice", "index": 1,
             "display_label": "Old A", "action_strs": ["Return('a')"]},
            {"source": "choice", "type": "choice", "index": 2,
             "display_label": "Old B", "action_strs": ["Return('b')"]},
        ],
    }
    samples = iter(({"_seq": 10, "_source_id": "shim", "_source_seq": 10},
                   {"_seq": 12, "_source_id": "shim", "_source_seq": 12,
                    "choices": ["New X", "New Y"], "interactions": [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "New X", "action_strs": ["Return('x')"]},
        {"source": "choice", "type": "choice", "index": 2,
         "display_label": "New Y", "action_strs": ["Return('y')"]},
    ]}))
    ctx.client.game_state = lambda **_kwargs: next(samples)
    ctx.client._wait_result = MockWaitResult(
        pending=pending,
        screen={"_seq": 11, "_source_id": "shim", "_source_seq": 11,
                "overlay_active": True,
                "buttons": [{"label": "Close"}]},
    )
    ctx.client._screen = None

    result = handle_wait(ctx, {"timeout": 2})

    assert "Old A" not in result.get("pending", "")
    assert "New X" not in result.get("pending", "")
    assert "Close" in result.get("buttons", "")


@pytest.mark.parametrize("fresh_interactions", [
    [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Old A", "action_strs": ["Return('changed')"]},
        {"source": "choice", "type": "choice", "index": 2,
         "display_label": "Old B", "action_strs": ["Return('b')"]},
    ],
    [
        {"source": "choice", "type": "choice", "index": 2,
         "display_label": "Old B", "action_strs": ["Return('b')"]},
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Old A", "action_strs": ["Return('a')"]},
    ],
    [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Old A"},
        {"source": "choice", "type": "choice", "index": 2,
         "display_label": "Old B", "action_strs": ["Return('b')"]},
    ],
])
def test_overlay_reopen_rejects_unproven_action_surfaces(
    ctx, fresh_interactions,
):
    from vnflight.handlers import handle_wait

    pending = {
        "type": "choice_request", "id": "covered-menu",
        "choices": ["Old A", "Old B"],
        "interactions": [
            {"source": "choice", "type": "choice", "index": 1,
             "display_label": "Old A", "action_strs": ["Return('a')"]},
            {"source": "choice", "type": "choice", "index": 2,
             "display_label": "Old B", "action_strs": ["Return('b')"]},
        ],
    }
    samples = iter((
        {"_seq": 20, "_source_id": "shim", "_source_seq": 20},
        {"_seq": 22, "_source_id": "shim", "_source_seq": 22,
         "choices": ["Old A", "Old B"],
         "interactions": fresh_interactions},
    ))
    ctx.client.game_state = lambda **_kwargs: next(samples)
    ctx.client._wait_result = MockWaitResult(
        pending=pending,
        screen={"_seq": 21, "_source_id": "shim", "_source_seq": 21,
                "overlay_active": True,
                "buttons": [{"label": "Close"}]},
    )
    ctx.client._screen = None

    result = handle_wait(ctx, {"timeout": 2})

    assert "Old A" not in result.get("pending", "")
    assert "Close" in result.get("buttons", "")


def test_overlay_reopen_rejects_matching_but_pre_overlay_game_state(ctx):
    from vnflight.handlers import handle_wait

    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Continue", "action_strs": ["Return('go')"]},
    ]
    pending = {
        "type": "choice_request", "id": "covered-menu",
        "choices": ["Continue"], "interactions": interactions,
    }
    cached = {"_seq": 30, "_source_id": "shim", "_source_seq": 30,
              "choices": ["Continue"],
              "interactions": interactions}
    ctx.client.game_state = lambda **_kwargs: cached
    ctx.client._wait_result = MockWaitResult(
        pending=pending,
        screen={"_seq": 31, "_source_id": "shim", "_source_seq": 31,
                "overlay_active": True,
                "buttons": [{"label": "Close"}]},
    )
    ctx.client._screen = None

    result = handle_wait(ctx, {"timeout": 2})

    assert "Continue" not in result.get("pending", "")
    assert "Close" in result.get("buttons", "")


def test_overlay_reopen_accepts_first_already_post_close_game_state(ctx):
    from vnflight.handlers import handle_wait

    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Continue", "action_strs": ["Return('go')"]},
    ]
    pending = {
        "type": "choice_request", "id": "reopened-menu",
        "choices": ["Continue"], "interactions": interactions,
    }
    reopened = {"_seq": 32, "_source_id": "shim", "_source_seq": 32,
                "choices": ["Continue"],
                "interactions": interactions}
    ctx.client.game_state = lambda **_kwargs: reopened
    ctx.client._wait_result = MockWaitResult(
        pending=pending,
        screen={"_seq": 31, "_source_id": "shim", "_source_seq": 31,
                "overlay_active": True,
                "buttons": [{"label": "Close"}]},
    )
    ctx.client._screen = None

    result = handle_wait(ctx, {"timeout": 2})

    assert "Continue" in result.get("pending", "")
    assert "Close" not in result.get("buttons", "")


def test_overlay_reopen_does_not_compare_bridge_seq_across_source_epochs():
    from vnflight.handlers import _game_state_proves_overlay_closed

    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Continue", "action_strs": ["Return('go')"]},
    ]
    pending = {
        "type": "choice_request", "choices": ["Continue"],
        "interactions": interactions,
    }

    assert not _game_state_proves_overlay_closed(
        pending,
        {"_seq": 99, "_source_id": "new", "_source_seq": 1,
         "choices": ["Continue"], "interactions": interactions},
        overlay_screen={
            "_seq": 3, "_source_id": "old", "_source_seq": 3,
            "overlay_active": True,
        },
        previous_game_state=None,
    )


def test_overlay_reopen_rejects_present_unsequenced_overlay(ctx):
    from vnflight.handlers import handle_wait

    interactions = [
        {"source": "choice", "type": "choice", "index": 1,
         "display_label": "Continue", "action_strs": ["Return('go')"]},
    ]
    pending = {
        "type": "choice_request", "id": "covered-menu",
        "choices": ["Continue"], "interactions": interactions,
    }
    samples = iter((
        {"_source_id": "shim", "_source_seq": 10},
        {"_source_id": "shim", "_source_seq": 12,
         "choices": ["Continue"], "interactions": interactions},
    ))
    legacy_overlay = {
        "overlay_active": True, "buttons": [{"label": "New Close"}],
    }
    ctx.client.game_state = lambda **_kwargs: next(samples)
    ctx.client._wait_result = MockWaitResult(
        pending=pending,
        screen={"_source_id": "shim", "_source_seq": 11,
                "overlay_active": True,
                "buttons": [{"label": "Old Close"}]},
    )
    ctx.client._screen = legacy_overlay

    result = handle_wait(ctx, {"timeout": 2})

    assert "Continue" not in result.get("pending", "")
    assert "New Close" in result.get("buttons", "")


@pytest.mark.parametrize("refresh", [lambda: {}, lambda: (_ for _ in ()).throw(
    OSError("state unavailable"))])
def test_handle_wait_keeps_overlay_without_positive_reopen_evidence(ctx, refresh):
    from vnflight.handlers import handle_wait

    pending = {
        "type": "choice_request", "id": "covered-menu",
        "choices": ["Continue"],
    }
    samples = iter(({},))

    def game_state(**_kwargs):
        try:
            return next(samples)
        except StopIteration:
            return refresh()

    ctx.client.game_state = game_state
    ctx.client._wait_result = MockWaitResult(
        pending=pending,
        screen={
            "overlay_active": True,
            "buttons": [{"label": "Close", "screen": "modal"}],
        },
    )
    ctx.client._screen = None

    result = handle_wait(ctx, {"timeout": 2})

    assert "Continue" not in result.get("pending", "")
    assert "Close" in result.get("buttons", "")


def test_handle_wait_bounds_all_post_wait_composition_reads(ctx):
    import time
    from vnflight.handlers import handle_wait

    pending = {
        "type": "choice_request", "id": "quick-menu",
        "choices": ["Continue"],
    }
    seen_timeouts = []

    def slow_game_state(*, timeout=2.0):
        seen_timeouts.append(timeout)
        time.sleep(timeout)
        return None

    ctx.client.game_state = slow_game_state
    ctx.client._wait_result = MockWaitResult(pending=pending)
    started = time.monotonic()

    handle_wait(ctx, {"timeout": 0.05})

    assert time.monotonic() - started < 2.3
    assert seen_timeouts and max(seen_timeouts) <= 2.01


def test_screen_reads_use_client_timeout_contract(ctx):
    from vnflight.handlers import _get_screen

    seen = []

    def get(path, timeout=5.0):
        seen.append((path, timeout))
        return 200, {"screen": {"buttons": [{"label": "Ready"}]}}

    ctx.client._get = get

    assert _get_screen(ctx, timeout=0.05) == {
        "buttons": [{"label": "Ready"}],
    }
    assert seen == [("/screen", 0.05)]


def test_hooks_no_longer_accept_unbounded_screen_workers():
    from vnflight.handlers import Hooks

    with pytest.raises(TypeError):
        Hooks(get_screen=lambda _client: None)


def test_state_render_carries_exact_actionable_snapshot(ctx):
    from vnflight.client import actionable_state_snapshot
    from vnflight.handlers import handle_state

    ctx.client._state = _menu_state(
        "Continue", "Wait", request_id="successor-menu")

    rendered = handle_state(ctx, {"brief": True})

    assert rendered["_pending_raw"]["id"] == "successor-menu"
    assert rendered["_actionable_snapshot"] == actionable_state_snapshot(
        ctx.client._state)


def test_fresh_timeline_marker_resets_delivery_ledgers_once(ctx):
    from vnflight.handlers import _observe_timeline_boundaries

    ctx.overlay.pending_deliveries = [{"id": 1, "text": "old"}]
    ctx.overlay.durable_receipts = {("source", "4", 0): None}
    ctx.overlay.transcript_rescued_seqs = {9}
    ctx.client._prefetched_events = [{"type": "narration", "text": "old"}]
    ctx.client._delivered_action_events = {(7, 11)}
    ctx.client.last_request_id = "old-menu"

    marker = {
        "type": "game_started",
        "_source_id": "shim-a",
        "_source_seq": 10,
        "_seq": 1,
    }
    _observe_timeline_boundaries(ctx, [marker])

    assert ctx.overlay.pending_deliveries == []
    assert ctx.overlay.durable_receipts == {}
    assert ctx.overlay.transcript_rescued_seqs == set()
    assert ctx.client._prefetched_events == []
    assert ctx.client._delivered_action_events == {(7, 11)}
    assert ctx.client.last_request_id is None

    # A scoped replay of the same lifecycle marker must not erase rows from
    # the already-running new timeline.
    ctx.overlay.pending_deliveries = [{"id": 2, "text": "new"}]
    _observe_timeline_boundaries(ctx, [marker])
    assert ctx.overlay.pending_deliveries == [{"id": 2, "text": "new"}]


def test_lifecycle_handler_reset_preserves_fresh_client_action_ownership(ctx):
    from vnflight.handlers import _observe_timeline_boundaries

    marker = {
        "type": "game_started", "_source_id": "new-run",
        "_source_seq": 1, "_seq": 10,
    }
    row = {
        "type": "dialogue", "text": "Fresh.",
        "action_id": 7, "_seq": 11,
    }
    ctx.client._delivered_action_events = {(7, 11)}

    _observe_timeline_boundaries(ctx, [marker, row])
    rescued = ctx.client.claim_undelivered_action_events([row])

    assert rescued == []


def test_main_menu_start_discards_old_timeline_prefetch(ctx):
    from vnflight.handlers import handle_act

    ctx.client._state = {
        "status": "ended",
        "context": {"context": "main_menu"},
        "game_state": {
            "interactions": [{
                "type": "navigation",
                "display_label": "Start",
                "index": 1,
            }],
            "screen_buttons": [{
                "label": "Start", "screen": "menu", "index": 1,
            }],
        },
    }
    ctx.client._prefetched_events = [{
        "type": "narration", "text": "old ending",
    }]
    ctx.client._delivered_action_events = {(17, 81)}
    ctx.overlay.pending_deliveries = [{"id": 1, "text": "old terminal"}]

    result = handle_act(ctx, {"target": "Start", "wait": False})

    assert result.get("ok") is True
    assert ctx.client._prefetched_events == []
    assert ctx.client._delivered_action_events == {(17, 81)}
    assert ctx.overlay.pending_deliveries == []


def test_string_act_rejects_target_hidden_from_rendered_surface(ctx):
    from vnflight.handlers import handle_act

    ctx.client._state = {
        "status": "screen_actions",
        "context": {"context": "main_menu"},
        "game_state": {
            "interactions": [{
                "source": "button",
                "type": "navigation",
                "display_label": "Start",
                "index": 1,
                "disabled": False,
            }],
            "screen_buttons": [{
                "label": "Start", "screen": "menu", "index": 1,
            }],
        },
    }

    result = handle_act(ctx, {"target": "Storm Tests", "wait": False})

    assert result["_target_not_visible_at_act"] is True
    assert "not in the rendered choices or controls" in result["error"]
    assert not [call for call in ctx.client.calls if call[0] == "act"]


def test_string_act_reconfirms_caller_visible_control_after_scrape_gap(
    ctx, monkeypatch,
):
    import importlib

    from vnflight.client import actionable_state_snapshot
    from vnflight.handlers import handle_act

    handlers_module = importlib.import_module("vnflight.handlers")

    def state_with(label):
        return {
            "status": "screen_actions",
            "game_state": {
                "interactions": [{
                    "source": "button",
                    "type": "other",
                    "display_label": label,
                    "index": 1,
                    "action_strs": ["Show('kit')"],
                    "disabled": False,
                }],
                "screen_buttons": [{
                    "label": label,
                    "screen": "observatory_hud",
                    "index": 1,
                    "action_strs": ["Show('kit')"],
                }],
            },
        }

    visible = state_with("KIT")
    missing = state_with("LOG")
    # The first live read and the ordinary pre-submission read both miss KIT.
    # Only the bounded reconfirmation loop sees it return.
    samples = iter((missing, missing, visible, visible, visible))
    ctx.client.last_actionable_snapshot = actionable_state_snapshot(visible)

    def read_state(*, timeout=3.0):
        ctx.client.calls.append(("state", {"timeout": timeout}))
        try:
            result = next(samples)
        except StopIteration:
            result = visible
        ctx.client.last_actionable_snapshot = actionable_state_snapshot(result)
        return dict(result)

    ctx.client.state = read_state
    ctx.client._game_state = visible["game_state"]
    monkeypatch.setattr(handlers_module.time, "sleep", lambda _delay: None)

    result = handle_act(ctx, {
        "target": "KIT", "wait": False, "result_timeout": 5,
    })

    assert result.get("ok") is True
    assert ("act", "KIT") in ctx.client.calls
    act_index = ctx.client.calls.index(("act", "KIT"))
    assert sum(call[0] == "state" for call in ctx.client.calls[:act_index]) >= 3


def test_string_act_rejects_hidden_control_while_choice_is_pending(ctx):
    from vnflight.handlers import handle_act

    ctx.client._state = {
        "status": "choice",
        "pending": {
            "type": "choice",
            "choices": [
                {"label": "Ask about the signal", "index": 1},
            ],
        },
        "game_state": {
            "interactions": [{
                "source": "choice",
                "type": "choice",
                "display_label": "Ask about the signal",
                "index": 1,
                "disabled": False,
            }],
        },
    }

    result = handle_act(ctx, {"target": "Q.Load", "wait": False})

    assert result["_target_not_visible_at_act"] is True
    assert not [call for call in ctx.client.calls if call[0] == "act"]


def test_string_act_rejects_sole_control_hidden_by_transform(ctx):
    from vnflight.handlers import handle_act

    ctx.client._state = {
        "status": "screen_actions",
        "game_state": {"interactions": []},
    }

    result = handle_act(ctx, {"target": "Storm Tests", "wait": False})

    assert result["_target_not_visible_at_act"] is True
    assert not [call for call in ctx.client.calls if call[0] == "act"]


def test_plain_wait_does_not_surface_previous_transaction(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(
        events=[],
        transaction={
            "action_nonce": "old-action", "action_id": 7,
            "transaction_state": "settled", "pending": False,
        },
    )

    result = handle_wait(ctx, {"timeout": 0})

    assert "transaction" not in result
    assert "transaction_state" not in result
    assert "transaction" not in result["_data"]


def test_plain_wait_surfaces_transaction_rejection(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(
        events=[],
        transaction={
            "action_nonce": "unknown-action",
            "transaction_state": "rejected",
            "pending": False,
            "reason": "unknown_nonce",
        },
    )

    result = handle_wait(ctx, {"timeout": 0})

    assert result["transaction_state"] == "rejected"
    assert result["transaction"]["reason"] == "unknown_nonce"
    assert result["_data"]["transaction"]["reason"] == "unknown_nonce"


def test_scoped_wait_explains_an_unknown_command_nonce(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(
        events=[],
        transaction={
            "action_nonce": "advance-command",
            "transaction_state": "rejected",
            "pending": False,
            "reason": "unknown_nonce",
        },
    )

    result = handle_wait(ctx, {
        "timeout": 0,
        "action_nonce": "advance-command",
    })

    assert result["ok"] is False
    assert "not known as a current act transaction" in result["error"]
    assert "it is a command nonce" in result["error"]
    assert "call plain wait()" in result["error"]


def test_scoped_wait_does_not_misclassify_an_expired_act_nonce(ctx):
    from vnflight.handlers import handle_wait

    ctx.client._wait_result = MockWaitResult(
        events=[],
        transaction={
            "action_nonce": "expired-act",
            "transaction_state": "rejected",
            "pending": False,
            "reason": "unknown_nonce",
        },
    )

    result = handle_wait(ctx, {
        "timeout": 0,
        "action_nonce": "expired-act",
    })

    assert result["ok"] is False
    assert "restart, expiry, or slot reset" in result["error"]
    assert "is not an act transaction nonce" not in result["error"]


def test_failed_transaction_overrides_optimistic_submission_booleans():
    from vnflight.handlers import _apply_transaction_to_output

    result = {
        "ok": True,
        "success": True,
        "status": "accepted",
        "message": "Command 'act' submitted.",
        "pending": True,
    }
    _apply_transaction_to_output(result, {
        "action_nonce": "action-8",
        "transaction_state": "failed",
        "pending": False,
        "error": "No matching interaction.",
    })

    assert result["ok"] is False
    assert result["success"] is False
    assert result["transaction_state"] == "failed"
    assert result["status"] == "failed"
    assert result["message"] == "Command 'act' failed."
    assert result["pending"] is False
    assert result["transaction_pending"] is False


def test_followup_state_promotion_preserves_delivered_status():
    from vnflight.handlers import _promote_wait_output_preserving_story

    result = {
        "text": "The warning lamp changes.",
        "status": "(stats updated: signal_strength: 60)",
    }

    _promote_wait_output_preserving_story(result, {
        "pending": "--- CHOICE REQUIRED ---\n1: Continue.",
        "_footer": "Signal: 60%",
    })

    assert result["status"] == "(stats updated: signal_strength: 60)"


def test_followup_promotion_drops_no_events_sentinel_before_story_merge():
    from vnflight.handlers import (
        _promote_wait_output_preserving_story,
        render_tool_result_text,
    )

    result = {"text": "(no new events)"}
    _promote_wait_output_preserving_story(result, {
        "screen_text": "ARIA IS WORKING ON SOMETHING.",
        "pending": "--- CHOICE REQUIRED ---\n1: Inspect.",
    })

    rendered = render_tool_result_text(result)
    assert "ARIA IS WORKING ON SOMETHING." in rendered
    assert "Inspect." in rendered
    assert "(no new events)" not in rendered
    assert all(
        section.get("text") != "(no new events)"
        for section in result.get("_story_render_sections", [])
    )


def test_followup_promotion_drops_sentinel_from_existing_section_plan():
    from vnflight.handlers import (
        _promote_wait_output_preserving_story,
        render_tool_result_text,
    )

    result = {
        "text": "(no new events)",
        "_story_render_sections": [{
            "channel": "text", "text": "(no new events)",
        }],
    }
    _promote_wait_output_preserving_story(result, {
        "text": "The successor scene arrives.",
    })

    rendered = render_tool_result_text(result)
    assert rendered == "The successor scene arrives."


def test_followup_promotion_joins_distinct_status_occurrences_in_order():
    from vnflight.handlers import _promote_wait_output_preserving_story

    result = {"status": "(stats updated: signal_strength: 60)"}
    _promote_wait_output_preserving_story(result, {
        "status": "[inventory updated] sealed drive",
    })

    assert result["status"].splitlines() == [
        "(stats updated: signal_strength: 60)",
        "[inventory updated] sealed drive",
    ]


def test_terminal_failure_preserves_formatted_status_occurrence():
    from vnflight.handlers import _apply_transaction_to_output

    result = {"status": "(stats updated: time_remaining: 280)"}
    _apply_transaction_to_output(result, {
        "transaction_state": "failed",
        "pending": False,
        "error": "No matching interaction.",
    })

    assert result["status"] == "(stats updated: time_remaining: 280)"


def test_followup_promotion_preserves_and_merges_structured_status():
    from vnflight.handlers import _promote_wait_output_preserving_story

    result = {"status": {
        "stats": [{"stat": "signal_strength", "value": 80, "delta": -10}],
        "inventory": ["old drive"],
        "resolved": [{"label": "Inspect", "by": "agent"}],
    }}
    _promote_wait_output_preserving_story(result, {"status": {
        "stats": [
            {"stat": "signal_strength", "value": 60, "delta": -20},
            {"stat": "aria_integrity", "value": 90},
        ],
        "inventory": ["sealed drive"],
        "resolved": [{"label": "Leave", "by": "agent"}],
    }})

    assert result["status"] == {
        "stats": [
            {"stat": "signal_strength", "value": 60, "delta": -20},
            {"stat": "aria_integrity", "value": 90},
        ],
        "inventory": ["sealed drive"],
        "resolved": [
            {"label": "Inspect", "by": "agent"},
            {"label": "Leave", "by": "agent"},
        ],
    }


def test_identical_structured_status_replay_is_not_double_counted():
    from vnflight.handlers import _promote_wait_output_preserving_story

    status = {"stats": [
        {"stat": "time_remaining", "value": 280, "delta": -20},
    ]}
    result = {"status": status}
    _promote_wait_output_preserving_story(result, {"status": status})

    assert result["status"] == status


def test_partial_structured_status_replay_does_not_double_count_overlap():
    from vnflight.handlers import _promote_wait_output_preserving_story

    time_entry = {
        "stat": "time_remaining", "value": 280, "delta": -20,
    }
    result = {"status": {"stats": [time_entry]}}
    _promote_wait_output_preserving_story(result, {"status": {"stats": [
        time_entry,
        {"stat": "aria_integrity", "value": 90, "delta": -10},
    ]}})

    assert result["status"]["stats"] == [
        time_entry,
        {"stat": "aria_integrity", "value": 90, "delta": -10},
    ]


def test_overlapping_same_stat_windows_use_latest_aggregate():
    from vnflight.handlers import _promote_wait_output_preserving_story

    result = {"status": {"stats": [
        {"stat": "time_remaining", "value": 280, "delta": -20},
    ]}}
    _promote_wait_output_preserving_story(result, {"status": {"stats": [
        {"stat": "time_remaining", "value": 270, "delta": -20},
    ]}})

    assert result["status"]["stats"] == [
        {"stat": "time_remaining", "value": 270, "delta": -20},
    ]


def test_terminal_failure_preserves_structured_status_without_crashing():
    from vnflight.handlers import _apply_transaction_to_output

    status = {"stats": [
        {"stat": "time_remaining", "value": 280, "delta": -20},
    ]}
    result = {"status": status}
    _apply_transaction_to_output(result, {
        "transaction_state": "failed",
        "pending": False,
        "error": "No matching interaction.",
    })

    assert result["status"] == status
    assert result["ok"] is False


def test_wait_after_action_is_a_rescrape_hint_not_a_story_entry():
    """The flag split.

    `_wait_after_action` means "the UI rebuilds after this click, re-scrape
    the frame".  It still routes the act through the wait path, but it is NOT
    "this is a Start-like story entry" -- reading it that way is what made an
    Echoes panel toggle wait out the whole act budget (fleet R61).  A mod that
    really means story flow says `_story_entry`, which the shim reports as
    `story_entry` on the act's command_result.
    """
    from vnflight import handlers

    result = {
        "resolved_as": "button",
        "label": "Done",
        "screen": "class_chooser",
        "action_names": ["function"],
        "wait_after_action": True,
    }

    assert handlers._button_action_uses_wait_path(result) is True
    assert handlers._button_action_is_story_entry(result) is False

    assert handlers._button_action_is_story_entry(
        dict(result, story_entry=True)) is True
    assert handlers._button_action_is_story_entry(
        {"resolved_as": "button", "label": "Start"}) is True


def test_story_entry_button_act_drains_transition_batches_to_decision():
    from vnflight.handlers import HandlerContext, handle_act

    class SequencedWaitClient(MockClient):
        def __init__(self):
            super().__init__()
            self._act_result = {
                "ok": True,
                "success": True,
                "resolved_as": "button",
                "interaction_type": "other",
                # mods/long_live_the_queen.rpy marks the class-chooser Done
                # button `_story_entry` (the day plays out before the next
                # decision) as well as `_wait_after_action` (the UI rebuilds).
                "wait_after_action": True,
                "story_entry": True,
                "label": "Done",
                "screen": "class_chooser",
            }
            self._wait_results = [
                MockWaitResult(
                    events=[
                        {
                            "type": "narration",
                            "text": "The carriage is attacked by bandits.",
                        }
                    ],
                    screen={
                        "type": "screen_content",
                        "screens": ["class_chooser"],
                        "buttons": [{"label": "Done", "screen": "class_chooser"}],
                    },
                ),
                MockWaitResult(events=[{"type": "show", "name": "death arrow"}]),
                MockWaitResult(
                    events=[
                        {
                            "type": "narration",
                            "text": "The wound turns into a fatal one.",
                        }
                    ],
                ),
                MockWaitResult(
                    screen={
                        "type": "screen_content",
                        "screens": ["end_menu_screen"],
                        "buttons": [
                            {"label": "Load Game", "screen": "end_menu_screen"},
                            {"label": "Quit", "screen": "end_menu_screen"},
                        ],
                    },
                ),
            ]

        def wait(self, timeout=60, **kw):
            self.calls.append(("wait", {"timeout": timeout, **kw}))
            if self._wait_results:
                return self._wait_results.pop(0)
            return MockWaitResult()

    ctx = HandlerContext(client=SequencedWaitClient())

    result = handle_act(
        ctx,
        {
            "target": "Done",
            "wait": True,
            "timeout": 2.0,
            "_story_transition_idle_timeout": 0.01,
        },
    )

    assert "attacked by bandits" in result["text"]
    assert "fatal one" in result["text"]
    assert "Load Game" in result["buttons"]
    assert "Quit" in result["buttons"]
    assert "Done" not in result["buttons"]


def test_story_entry_keeps_early_overlay_rows_before_later_narration():
    """The Start drain must not rewrite text outside the sequence plan."""
    from vnflight.handlers import (
        HandlerContext,
        handle_act,
        render_tool_result_text,
    )

    class OpeningClient(MockClient):
        def __init__(self):
            super().__init__()
            self._act_result = {
                "ok": True,
                "success": True,
                "resolved_as": "button",
                "interaction_type": "other",
                "wait_after_action": True,
                "label": "Start",
                "screen": "menu",
            }
            self._wait_results = [
                MockWaitResult(events=[{
                    "type": "screen_content",
                    "_seq": 74,
                    "passive_overlay_snapshot": True,
                    "passive_overlay_delta": ["SYSTEM BOOT... OK"],
                    "overlay_texts": ["SYSTEM BOOT... OK"],
                    "overlay_texts_by_screen": {
                        "terminal": ["SYSTEM BOOT... OK"],
                    },
                    "overlay_screens": ["terminal"],
                    "overlay_generations": {"terminal": "1"},
                    "overlay_retained_screens": ["terminal"],
                }]),
                MockWaitResult(events=[{
                    "type": "narration",
                    "text": "The cursor blinks.",
                    "_seq": 113,
                }]),
                MockWaitResult(events=[{
                    "type": "narration",
                    "text": "Another night.",
                    "_seq": 125,
                }]),
                MockWaitResult(screen={
                    "type": "screen_content",
                    "screens": ["ending"],
                    "buttons": [{"label": "Continue", "screen": "ending"}],
                }),
            ]

        def wait(self, timeout=60, **kw):
            self.calls.append(("wait", {"timeout": timeout, **kw}))
            if self._wait_results:
                return self._wait_results.pop(0)
            return MockWaitResult()

    result = handle_act(
        HandlerContext(client=OpeningClient()),
        {
            "target": "Start",
            "wait": True,
            "timeout": 2.0,
            "_story_transition_idle_timeout": 0.01,
        },
    )

    rendered = render_tool_result_text(result)
    assert rendered.index("SYSTEM BOOT... OK") < rendered.index(
        "The cursor blinks.")
    assert rendered.index("The cursor blinks.") < rendered.index(
        "Another night.")
    assert rendered.count("SYSTEM BOOT... OK") == 1


# ---------------------------------------------------------------------------
# Fleet R61: overlay/screen transitions burned the whole act budget
# ---------------------------------------------------------------------------
#
# Echoes of Tomorrow, 2026-09-02 21:31:10 (bridge log
# playthrough_20260902_212444.jsonl, agent echo-s03):
#
#   21:31:10.806  act('LOG')
#   21:31:11.126  command_result  act ok  resolved_as=button
#                                 interaction_type=other wait_after_action=True
#   21:31:11.600  screen_text / game_state -> evidence_screen, button CLOSE
#   21:31:11.727  ... and then NOTHING but screenshots
#   21:32:10.829  act returns, 60.02 s, with the correct STATION LOG panel
#
# The mod marks KIT/LOG/CLOSE with `_wait_after_action` so the post-click
# rebuild is observed; `_button_action_is_story_entry` reads that same flag as
# "this is a Start-like story entry", and the story-entry drain in
# `_settle_wait_after_action` has no stopping condition except story or a new
# choice request.  A panel toggle produces neither, so it waited out the whole
# budget.  15 of the fleet's 130 sixty-second acts had 59 s of pure bridge
# silence before returning; the rest were genuinely long scenes still emitting
# narration at the deadline.

def _r61_overlay_act_result(**overrides):
    result = {
        "ok": True,
        "success": True,
        "resolved_as": "button",
        "interaction_type": "other",
        "wait_after_action": True,
        "label": "LOG",
        "screen": "observatory_hud",
    }
    result.update(overrides)
    return result


_R61_HUB_BUTTONS = "--- OTHER BUTTONS ---\n  1: KIT\n  2: LOG"
_R61_PANEL_BUTTONS = "--- OTHER BUTTONS ---\n  1: CLOSE"


class _R61WaitClient(MockClient):
    """One scoped settle wait, then a bridge that never says anything again."""

    def __init__(self, first_wait, act_result=None):
        super().__init__()
        self._act_result = act_result or _r61_overlay_act_result()
        self._first_wait = first_wait
        self.wait_calls = 0

    def wait(self, timeout=60, **kw):
        self.calls.append(("wait", {"timeout": timeout, **kw}))
        self.wait_calls += 1
        if self.wait_calls == 1:
            return self._first_wait
        # Post-transition silence: no events, no pending, nothing to render.
        return MockWaitResult()


def _r61_panel_wait(transaction_state="settled"):
    return MockWaitResult(
        screen={
            "type": "screen_content",
            "screens": ["evidence_screen"],
            "buttons": [{"label": "CLOSE", "screen": "evidence_screen"}],
        },
        transaction={
            "action_nonce": "n1",
            "action_id": 7,
            "transaction_state": transaction_state,
            "pending": transaction_state != "settled",
        },
    )


def _r61_settle(client, *, pre_rendered, timeout=4.0):
    import time

    from vnflight import handlers
    from vnflight.handlers import HandlerContext

    result = dict(client._act_result)
    result["action_nonce"] = "n1"
    ctx = HandlerContext(client=client)
    started = time.time()
    handlers._settle_wait_after_action(
        ctx,
        result,
        {"timeout": timeout, "action_nonce": "n1"},
        button_context=True,
        pre_state_sig=None,
        pre_rendered=pre_rendered,
        pre_visible_sig=handlers._visible_output_signature(pre_rendered),
        pre_pending_id=None,
        pre_was_button_only=True,
    )
    return result, time.time() - started


def test_r61_overlay_act_returns_on_the_new_rendered_surface():
    """act('LOG') must return the panel, not wait out the act budget."""
    client = _R61WaitClient(_r61_panel_wait())
    pre = {"buttons": _R61_HUB_BUTTONS}

    result, elapsed = _r61_settle(client, pre_rendered=pre)

    assert "CLOSE" in result["buttons"]
    # The story-entry drain never ran: one scoped settle wait and nothing more.
    assert client.wait_calls == 1
    assert elapsed < 2.0


def test_r61_overlay_drain_still_runs_without_a_settled_receipt():
    """No settle evidence means the successor may still be rendering."""
    client = _R61WaitClient(_r61_panel_wait(transaction_state="applied"))
    pre = {"buttons": _R61_HUB_BUTTONS}

    _result, elapsed = _r61_settle(client, pre_rendered=pre, timeout=1.0)

    assert client.wait_calls > 1
    assert elapsed >= 1.0


def test_r61_overlay_drain_still_runs_on_the_pre_act_surface():
    """Re-presenting the surface the act was aimed at is not an arrival.

    This is the numeric-act snapshot contract: act N resolves against the
    PRE-act rendered snapshot, so handing that same snapshot back as the
    settled outcome would make the caller's next numeric act stale.
    """
    unchanged = MockWaitResult(
        screen={
            "type": "screen_content",
            "screens": ["observatory_hud"],
            "buttons": [
                {"label": "KIT", "screen": "observatory_hud"},
                {"label": "LOG", "screen": "observatory_hud"},
            ],
        },
        transaction={
            "action_nonce": "n1",
            "action_id": 7,
            "transaction_state": "settled",
            "pending": False,
        },
    )
    client = _R61WaitClient(unchanged)
    pre = {"buttons": _R61_HUB_BUTTONS}

    _result, elapsed = _r61_settle(client, pre_rendered=pre, timeout=1.0)

    assert client.wait_calls > 1
    assert elapsed >= 1.0


def test_r61_story_entry_start_still_drains_its_opening():
    """Start has no post-act controls yet, so the drain must still run."""
    story_only = MockWaitResult(
        events=[{"type": "narration", "text": "The cursor blinks."}],
        transaction={
            "action_nonce": "n1",
            "action_id": 7,
            "transaction_state": "settled",
            "pending": False,
        },
    )
    client = _R61WaitClient(
        story_only,
        act_result=_r61_overlay_act_result(
            label="Start", interaction_type="nav", wait_after_action=False),
    )
    pre = {"buttons": "--- OTHER BUTTONS ---\n  1: Start\n  2: Load"}

    _result, elapsed = _r61_settle(client, pre_rendered=pre, timeout=1.0)

    assert client.wait_calls > 1
    assert elapsed >= 1.0

# ---------------------------------------------------------------------------
# One act-settle policy: evidence -> verdict
# ---------------------------------------------------------------------------
# handle_act used to carry five separate notions of "the act is done".  These
# tests pin the single policy that replaced the post-action timer and the
# story-entry drain's ad-hoc exits, and the subordination of the two remaining
# story drains to it.


def _evidence(**overrides):
    from vnflight import act_settle

    return act_settle._ActSettleEvidence(**overrides)


def test_act_settle_verdict_table():
    """The whole policy, stated once, as a table."""
    from vnflight import act_settle, handlers  # noqa: F401

    done = act_settle._ACT_SETTLE_DONE
    handback = act_settle._ACT_SETTLE_STORY_HANDBACK
    keep_going = act_settle._ACT_SETTLE_CONTINUE
    verdict = act_settle._act_settle_verdict

    # E1 and (E3 or E4 or E6) -> DONE.
    assert verdict(_evidence(bridge_settled=True, decision=True)) == done
    assert verdict(_evidence(bridge_settled=True, surface_changed=True)) == done
    assert verdict(_evidence(bridge_settled=True, terminal=True)) == done

    # Without E1 the same surface is not proof: the successor may still be
    # rendering, so a declared expectation keeps polling.
    assert verdict(_evidence(
        surface_changed=True, rescrape_expected=True)) == keep_going

    # E1 and E2 and not E5 -> DONE (quiet after a post-action sample).
    assert verdict(_evidence(
        bridge_settled=True, post_action_sample=True)) == done
    assert verdict(_evidence(
        bridge_settled=True, post_action_sample=True, story_flowing=True,
        story_entry=True,
    )) == keep_going

    # E1 and E5 continuously for the handback window, with no E3.
    assert verdict(_evidence(
        bridge_settled=True, story_flowing=True, story_entry=True,
        settled_story_seconds=handlers._ACT_STORY_HANDBACK_SECONDS,
    )) == handback
    assert verdict(_evidence(
        bridge_settled=True, story_flowing=True, decision=True,
        settled_story_seconds=handlers._ACT_STORY_HANDBACK_SECONDS,
    )) == done

    # E1 cannot hold during an unbroken burst — the bridge settles only after
    # its own quiet — so the handback also accepts E1' (the APPLIED receipt),
    # and then only while no decision surface is rendered at all.
    assert verdict(_evidence(
        action_applied=True, story_flowing=True, story_entry=True,
        settled_story_seconds=handlers._ACT_STORY_HANDBACK_SECONDS,
    )) == handback
    assert verdict(_evidence(
        action_applied=True, story_flowing=True, story_entry=True,
        pending_surface=True,
        settled_story_seconds=handlers._ACT_STORY_HANDBACK_SECONDS,
    )) == keep_going
    # Applied is not acceptance: nothing hands back before the click ran.
    assert verdict(_evidence(
        story_flowing=True, story_entry=True,
        settled_story_seconds=handlers._ACT_STORY_HANDBACK_SECONDS,
    )) == keep_going

    # The boot lull is not an outcome.  While the bridge itself still says
    # gameplay has not started, "the title screen went away" and "the bridge
    # went quiet after a post-action sample" both describe the START of the
    # opening, not its end -- R62's act('Start') returned the bare token
    # "starting" in 7.6 s and moved the whole opening onto the next wait().
    assert verdict(_evidence(
        bridge_settled=True, surface_changed=True, story_entry=True,
        pre_gameplay=True,
    )) == keep_going
    assert verdict(_evidence(
        bridge_settled=True, post_action_sample=True, story_entry=True,
        pre_gameplay=True,
    )) == keep_going
    # A decision or an ending IS an outcome wherever it appears.
    assert verdict(_evidence(
        bridge_settled=True, decision=True, pre_gameplay=True)) == done
    assert verdict(_evidence(
        bridge_settled=True, terminal=True, pre_gameplay=True)) == done

    # An act with no declared expectation and no evidence is finished: it has
    # already produced everything it is going to produce.  Polling it is the
    # R61 stall.
    assert verdict(_evidence()) == done
    assert verdict(_evidence(story_entry=True)) == keep_going
    assert verdict(_evidence(rescrape_expected=True)) == keep_going
    assert verdict(_evidence(story_flowing=True)) == keep_going
    assert verdict(_evidence(story_flowing=True, story_boundary=True)) == done


def test_post_action_sample_must_be_stamped_after_acceptance():
    """E2 is strict: the pre-act scrape is never the act's outcome."""
    from vnflight import handlers

    is_after = handlers._post_action_sample_is_after

    # Shim queue order: same source, strictly higher source seq.
    assert is_after(
        {"_source_id": "shim-1", "_source_seq": 41},
        source_id="shim-1", source_seq=40, admission_seq=None,
    ) is True
    assert is_after(
        {"_source_id": "shim-1", "_source_seq": 40},
        source_id="shim-1", source_seq=40, admission_seq=None,
    ) is False
    assert is_after(
        {"_source_id": "shim-1", "_source_seq": 39},
        source_id="shim-1", source_seq=40, admission_seq=None,
    ) is False
    # A different source proves nothing about this act's ordering.
    assert is_after(
        {"_source_id": "other", "_source_seq": 999, "_seq": 5},
        source_id="shim-1", source_seq=40, admission_seq=7,
    ) is False

    # Bridge event counter fallback, equally strict.
    assert is_after(
        {"_seq": 8}, source_id=None, source_seq=None, admission_seq=7,
    ) is True
    assert is_after(
        {"_seq": 7}, source_id=None, source_seq=None, admission_seq=7,
    ) is False

    # No anchor, no sample, no proof.
    assert is_after(
        {"_seq": 8}, source_id=None, source_seq=None, admission_seq=None,
    ) is False
    assert is_after(
        None, source_id="shim-1", source_seq=1, admission_seq=1,
    ) is False


def _settle_observer(client, **overrides):
    from vnflight import handlers
    from vnflight.handlers import HandlerContext

    ctx = HandlerContext(client=client)
    options = {
        "pre_visible_sig": None,
        "story_entry": False,
        "rescrape_expected": False,
    }
    options.update(overrides)
    return ctx, handlers._ActSettleObserver(ctx, **options)


def test_settle_loop_returns_on_the_bridge_receipt_and_a_new_surface():
    """(a) An overlay toggle is done the moment E1 and E4 hold.

    Sequence test on a real BridgeClient: the loop must not spend a single
    poll once the bridge has settled the receipt and the rendered surface is
    no longer the one the act was aimed at.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    pre = {"buttons": _R61_HUB_BUTTONS}
    ctx, settle = _settle_observer(
        client,
        pre_visible_sig=handlers._visible_output_signature(pre),
        rescrape_expected=True,
    )
    settle.note_transaction({"transaction_state": "settled"})
    panel = {
        "buttons": _R61_PANEL_BUTTONS,
        "_data": {"buttons": [
            {"label": "CLOSE", "screen": "evidence_screen"},
        ]},
    }

    result = {}
    started = time.time()
    out = handlers._settle_act_until_verdict(
        ctx, result, {"timeout": 10}, panel, settle)

    assert out is panel
    assert client.state_reads() == 0, "it polled despite complete evidence"
    assert time.time() - started < 1.0


def test_settle_loop_keeps_polling_while_the_surface_is_the_pre_act_one():
    """The numeric-act snapshot contract, inside the new loop.

    Re-presenting the surface the act was aimed at is not an arrival, so a
    settled receipt alone must not end the act.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    pre = {"buttons": _R61_HUB_BUTTONS}
    ctx, settle = _settle_observer(
        client,
        pre_visible_sig=handlers._visible_output_signature(pre),
        rescrape_expected=True,
    )
    settle.note_transaction({"transaction_state": "settled"})

    result = {}
    started = time.time()
    handlers._settle_act_until_verdict(
        ctx, result, {"timeout": 1.0}, dict(pre), settle)

    assert client.state_reads() > 0
    assert time.time() - started >= 1.0


def test_quiet_after_a_post_action_sample_returns_without_a_timer():
    """(d) E1 and E2 and not E5 is a complete answer -- no 3/5/8 s wait."""
    from vnflight import handlers

    client = ScriptedBridgeClient(state={"game_state": {
        "_source_id": "shim-1", "_source_seq": 41, "_seq": 41,
    }})
    ctx, settle = _settle_observer(client, rescrape_expected=True)
    settle.note_transaction({
        "transaction_state": "settled",
        "_source_id": "shim-1",
        "_source_seq": 40,
    })

    started = time.time()
    handlers._settle_act_until_verdict(ctx, {}, {"timeout": 10}, {}, settle)
    elapsed = time.time() - started

    assert settle.anchored is True
    assert client.state_reads() == 0
    assert elapsed < 1.0

    # And the same act with only the PRE-act sample available keeps polling:
    # nothing proves the click was observed yet.
    stale = ScriptedBridgeClient(state={"game_state": {
        "_source_id": "shim-1", "_source_seq": 40, "_seq": 40,
    }})
    ctx, settle = _settle_observer(stale, rescrape_expected=True)
    settle.note_transaction({
        "transaction_state": "settled",
        "_source_id": "shim-1",
        "_source_seq": 40,
    })
    started = time.time()
    handlers._settle_act_until_verdict(ctx, {}, {"timeout": 1.0}, {}, settle)

    assert time.time() - started >= 1.0


def test_story_entry_hands_the_story_back_and_the_next_wait_continues(
    monkeypatch,
):
    """(b) A story that will not stop is handed back, not held onto.

    Sequence test on a real BridgeClient: the act returns the story so far
    with ``story_continues`` and the agent-facing hint, and a plain wait then
    continues from the same cursor -- no duplicated line, no gap.
    """
    from vnflight import act_settle, handlers

    # Both bindings: the verdict rule reads act_settle's copy, the observer's
    # handback clock reads the name handlers imported.
    monkeypatch.setattr(handlers, "_ACT_STORY_HANDBACK_SECONDS", 2.0)
    monkeypatch.setattr(act_settle, "_ACT_STORY_HANDBACK_SECONDS", 2.0)

    client = ScriptedBridgeClient()
    ctx, settle = _settle_observer(client, story_entry=True)
    settle.note_transaction({"transaction_state": "settled"})

    stop = threading.Event()
    emitted = []

    def narrate():
        seq = 100
        while not stop.is_set():
            seq += 1
            emitted.append(seq)
            client.push_events({
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
            })
            time.sleep(0.2)

    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        result = {}
        started = time.time()
        handlers._settle_act_until_verdict(
            ctx, result, {"timeout": 45}, {}, settle)
        elapsed = time.time() - started
        assert settle.story_handback is True
        assert elapsed < 30.0, "it held the act open for the whole budget"

        handlers._mark_act_story_continues(result)
        assert result["story_continues"] is True
        assert act_settle._ACT_STORY_HANDBACK_HINT in result["warning"]
        assert "call wait()" in handlers.render_tool_result_text(result)

        act_lines = [
            line for line in str(result.get("text") or "").splitlines()
            if line.strip()
        ]
        assert act_lines, "the story so far must be returned, not withheld"

        continuation = handlers.handle_wait(ctx, {"timeout": 2})
    finally:
        stop.set()
        narrator.join(timeout=2)

    wait_lines = [
        line for line in str(continuation.get("text") or "").splitlines()
        if line.strip()
    ]
    assert wait_lines, "the wait must continue the same story"
    # No duplicate: the handback consumed exactly what it rendered.
    assert not set(act_lines) & set(wait_lines)
    # No gap: the wait resumes on the line after the act's last one.
    last_act = int(act_lines[-1].split()[-1].strip("."))
    first_wait = int(wait_lines[0].split()[-1].strip("."))
    assert first_wait == last_act + 1


def test_unbroken_burst_hands_back_without_waiting_for_the_bridge_settle(
    monkeypatch,
):
    """Fleet R62 defect 1: the handback must fire during the burst itself.

    The bridge calls a transaction settled only after _ACTION_SETTLE_GRACE
    (0.75 s) of quiet, and these blocks emit roughly every two seconds, so E1
    is false for exactly as long as the burst runs.  Gated on E1 the handback
    could never fire in the case it exists for: 54 R62 acts sat to the 60 s
    deadline and took the legacy "still settling" return instead, all 12
    agents, deterministic on Marcus's "48 hours" beat.

    Sequence test on a real BridgeClient: the receipt never settles, and the
    act must still hand back within a couple of seconds of the bound.
    """
    from vnflight import act_settle, handlers

    monkeypatch.setattr(handlers, "_ACT_STORY_HANDBACK_SECONDS", 3.0)
    monkeypatch.setattr(act_settle, "_ACT_STORY_HANDBACK_SECONDS", 3.0)

    client = ScriptedBridgeClient()
    ctx, settle = _settle_observer(client, story_entry=True)
    # APPLIED, never settled — the burst keeps the bridge's quiet window open.
    settle.note_transaction({
        "action_nonce": "n1", "action_id": 7,
        "transaction_state": "applied", "pending": True,
    })
    assert settle.evidence({}).bridge_settled is False

    stop = threading.Event()

    def narrate():
        seq = 100
        while not stop.is_set():
            seq += 1
            client.push_events({
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
            })
            time.sleep(0.4)

    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        result = {}
        # The act's own first wait has already rendered a line by the time the
        # settle loop runs, exactly as _settle_wait_after_action arranges.
        opening = {"text": "Line 100."}
        started = time.time()
        handlers._settle_act_until_verdict(
            ctx, result, {"timeout": 45}, opening, settle)
        elapsed = time.time() - started
    finally:
        stop.set()
        narrator.join(timeout=3)

    assert settle.story_handback is True, (
        "an unbroken burst never reached the handback"
    )
    # The constant must mean what a latency-sizing reader expects: R62's
    # handbacks landed at a median 31 s against a 20 s constant because the
    # loop polled in 5 s chunks.
    assert elapsed < handlers._ACT_STORY_HANDBACK_SECONDS + 2.0, elapsed
    handlers._mark_act_story_continues(result)
    assert result["story_continues"] is True
    # The numeric-act contract: story only, no numbers to answer.
    assert not result.get("pending")
    assert str(result.get("text") or "").strip()


def test_start_does_not_finish_in_the_boot_lull(monkeypatch):
    """Fleet R62 defect 2: act('Start') returned "starting" in 7.6 s.

    Clicking Start changes the surface immediately and the bridge settles in
    the quiet before the opening begins, so the story-entry guard's own
    evidence pair fired at the START of the opening.  The bridge's
    ``gameplay_seen`` is the flag that tells those apart, and every /state
    read already carries it.

    Sequence test on a real BridgeClient: while the bridge says gameplay has
    not started the act keeps polling; once it has, the same evidence ends it.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    pre = {"buttons": "  1: Start"}
    ctx, settle = _settle_observer(
        client,
        pre_visible_sig=handlers._visible_output_signature(pre),
        story_entry=True,
    )
    settle.note_transaction({"transaction_state": "settled"})
    in_game = {
        "buttons": "--- OTHER BUTTONS ---\n  1: Continue",
        "_data": {"buttons": [{"label": "Continue", "screen": "say"}]},
    }

    client._last_gameplay_seen = False
    started = time.time()
    handlers._settle_act_until_verdict(
        ctx, {}, {"timeout": 1.0}, in_game, settle)
    assert time.time() - started >= 1.0, (
        "the act returned inside the boot lull"
    )

    client._last_gameplay_seen = True
    started = time.time()
    handlers._settle_act_until_verdict(
        ctx, {}, {"timeout": 10}, in_game, settle)
    assert time.time() - started < 1.0

    # And the block is bounded: a run that never publishes gameplay must not
    # hold the act open past the pre-gameplay window.
    monkeypatch.setattr(handlers, "_ACT_PRE_GAMEPLAY_WAIT_SECONDS", 0.0)
    client._last_gameplay_seen = False
    settle._pre_gameplay_since = None
    assert settle.evidence(in_game).pre_gameplay is False


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_stats_only_running_wait_explains_how_to_continue(fmt):
    from vnflight import handlers

    client = MockClient()
    client._state = {"status": "running"}
    client._game_state = {"stats": {"time_remaining": 210}, "interactions": []}
    ctx = handlers.HandlerContext(client=client)
    result = handlers.handle_wait(ctx, {"format": fmt})
    assert "call wait() to continue" in result.get("warning", "")
    assert not result.get("story_continues")
    assert len([c for c in client.calls if c[0] == "wait"]) == 1


@pytest.mark.parametrize("status", ["ended", "disconnected", "unknown"])
def test_empty_wait_does_not_claim_progress_without_running_game(status):
    from vnflight import handlers

    client = MockClient()
    client._state = {"status": status}
    result = handlers.handle_wait(handlers.HandlerContext(client=client), {})
    assert "No new dialogue or decision yet" not in result.get("warning", "")


@pytest.mark.parametrize("boundary", ["story", "choice", "ended", "interrupted"])
def test_empty_wait_guidance_does_not_override_other_outcomes(boundary):
    from vnflight import handlers

    client = MockClient()
    client._game_state = {"interactions": []}
    if boundary == "story":
        client._wait_result.events = [{"type": "narration", "text": "The thread resumes."}]
    elif boundary == "choice":
        client._game_state = _choice_game_state(["Read the thread", "Leave"])
    elif boundary == "ended":
        client._wait_result.ended = True
        client._state = {"status": "ended"}
    else:
        client._wait_result.interrupted = True
    result = handlers.handle_wait(handlers.HandlerContext(client=client), {})
    assert "No new dialogue or decision yet" not in result.get("warning", "")


def test_a_long_opening_hands_back_instead_of_blocking_the_wait(monkeypatch):
    """Fleet R62 defect 2, second half: the block moved into wait().

    R61 recorded zero slow waits in 114; R62 had 41 of 150 in a 55-61 s
    cluster, most returning no menu.  wait() had no handback of its own, so a
    long opening simply held it to its full budget.
    """
    from vnflight import act_settle, handlers

    monkeypatch.setattr(handlers, "_ACT_STORY_HANDBACK_SECONDS", 2.0)
    monkeypatch.setattr(act_settle, "_ACT_STORY_HANDBACK_SECONDS", 2.0)

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    stop = threading.Event()

    def narrate():
        seq = 100
        while not stop.is_set():
            seq += 1
            client.push_events({
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
            })
            time.sleep(0.3)

    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        started = time.time()
        first = handlers.handle_wait(ctx, {"timeout": 30})
        elapsed = time.time() - started
        second = handlers.handle_wait(ctx, {"timeout": 5})
    finally:
        stop.set()
        narrator.join(timeout=3)

    assert elapsed < 8.0, elapsed
    assert first.get("story_continues") is True
    assert act_settle._ACT_STORY_HANDBACK_HINT in first["warning"]
    first_lines = [
        line for line in str(first.get("text") or "").splitlines()
        if line.strip().startswith("Line ")
    ]
    second_lines = [
        line for line in str(second.get("text") or "").splitlines()
        if line.strip().startswith("Line ")
    ]
    assert first_lines and second_lines
    # Nothing consumed early: no duplicate across the seam, and no gap.
    assert not set(first_lines) & set(second_lines)
    last_first = int(first_lines[-1].split()[-1].strip("."))
    first_second = int(second_lines[0].split()[-1].strip("."))
    assert first_second == last_first + 1


# ---------------------------------------------------------------------------
# The numeric binding follows the RENDER (fleet R64 defect 2)
# ---------------------------------------------------------------------------

_R64_MENU_A = [
    "I authorized ARIA to search your partition.",
    "ARIA flagged an anomaly during a security sweep.",
    "Marcus, sit down.",
]
_R64_MENU_B = [
    "Even if you're right, you can't just flip the switch.",
    "You sound like a terrorist justifying a bombing.",
    "Maybe you have a point.",
]


def _choice_game_state(labels):
    """A live game_state whose interactions are a numbered choice menu."""
    return {
        "interactions": [
            {"source": "choice", "type": "choice", "index": index,
             "display_label": label, "disabled": False,
             "action_strs": ["ChoiceReturn({})".format(index)]}
            for index, label in enumerate(labels, 1)
        ],
    }


def _publish_pending_on_authoritative_read(client, pending):
    """Publish ``pending_request`` on the next /state read that is not a poll.

    ``poll()`` sends ``since``; the composition's authoritative read does not.
    A menu that registers between the drain's last poll and that read is
    exactly the live window this defect lives in.
    """
    original = client._get

    def patched(path, params=None, timeout=5.0):
        if path == "/state" and not (params or {}).get("since"):
            client.script_state["pending_request"] = dict(pending)
        return original(path, params, timeout)

    client._get = patched


def _render_first_menu(handlers, ctx, client):
    """Play up to the point where the caller has been shown menu A."""
    client.script_state["pending_request"] = {
        "type": "choice_request", "id": "menu-a", "choices": list(_R64_MENU_A),
    }
    client.script_state["game_state"] = _choice_game_state(_R64_MENU_A)
    client.push_events({"type": "choice_request", "id": "menu-a",
                        "choices": list(_R64_MENU_A), "_seq": 1})
    rendered = handlers.handle_wait(ctx, {"timeout": 1})
    assert _R64_MENU_A[0] in rendered["pending"]
    assert client.last_request_id == "menu-a"
    # Menu A is answered; the successor has not registered yet.
    client.script_state["pending_request"] = None
    client.script_state["game_state"] = {"interactions": []}
    client.push_events(
        {"type": "choice_resolved", "request_id": "menu-a",
         "label": _R64_MENU_A[0], "_seq": 2},
        {"type": "narration", "text": "Marcus does not sit down.", "_seq": 3},
    )
    story = handlers.handle_wait(ctx, {"timeout": 1})
    assert not story.get("pending")
    # The binding is still the CONSUMED menu: _remember_pending only moves
    # when a read observes a pending, and no read did.
    assert client.last_request_id == "menu-a"


def test_the_menu_a_handback_wait_rendered_is_what_the_next_act_binds_to():
    """Fleet R64 defect 2 (echo64-s08 call113), reproduced end to end.

    The successor menu registered 0.25 s before the wait composed its answer,
    so the drain never carried a choice_request row and the decision was built
    from the live game_state rows instead.  That synthesis carries ``id: ""``
    (``format._pending_from_choice_interactions``), which short-circuited the
    reconciliation that would have named it -- the agent was shown menu B while
    the numeric binding still named the consumed menu A.  ``act('3')`` was then
    refused for "the state changed since you last looked", quoting the OLD menu
    in ``rendered_choices`` and the new one in ``current_choices``.  The
    identical retry was accepted.

    Sequence test on a real BridgeClient: the menu the wait RENDERED is the one
    the next numeric act replies to.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    _render_first_menu(handlers, ctx, client)

    # Menu B is on screen (live choice rows) and registers with the bridge
    # between the drain's last poll and the composition's authoritative read.
    client.script_state["game_state"] = _choice_game_state(_R64_MENU_B)
    client.push_events({"type": "narration", "text": "He looks up.",
                        "_seq": 4})
    _publish_pending_on_authoritative_read(client, {
        "type": "choice_request", "id": "menu-b", "choices": list(_R64_MENU_B),
    })

    rendered = handlers.handle_wait(ctx, {"timeout": 1})

    assert _R64_MENU_B[2] in rendered["pending"], "menu B was not rendered"
    assert client.last_request_id == "menu-b"
    assert client.last_choices == _R64_MENU_B
    assert isinstance(client.last_actionable_snapshot, dict)

    result = handlers.handle_act(ctx, {"target": "3", "wait": False})

    assert not result.get("_stale_numeric_act"), result
    assert "error" not in result, result


def test_a_menu_drained_as_an_event_binds_even_if_the_state_read_fails():
    """The other lag shape: the successor arrives on the event lane.

    ``_latest_pending_request_event`` renders a choice_request row the drain
    carried, and the composition's /state read is what would otherwise update
    the client's cache.  When that read fails (a saturated bridge answers
    non-200), the caller still receives the menu -- so the render, not the
    read, has to be what the next numeric act binds to.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    _render_first_menu(handlers, ctx, client)

    client.push_events({"type": "choice_request", "id": "menu-b",
                        "choices": list(_R64_MENU_B), "_seq": 4})
    client.script_state["game_state"] = _choice_game_state(_R64_MENU_B)
    original_get = client._get

    def fail_authoritative_reads(path, params=None, timeout=5.0):
        if path == "/state" and not (params or {}).get("since"):
            return 503, None
        return original_get(path, params, timeout)

    client._get = fail_authoritative_reads
    rendered = handlers.handle_wait(ctx, {"timeout": 1})
    client._get = original_get

    assert _R64_MENU_B[2] in rendered["pending"], "menu B was not rendered"
    assert client.last_request_id == "menu-b"
    assert client.last_choices == _R64_MENU_B

    client.script_state["pending_request"] = {
        "type": "choice_request", "id": "menu-b", "choices": list(_R64_MENU_B),
    }
    result = handlers.handle_act(ctx, {"target": "2", "wait": False})

    assert not result.get("_stale_numeric_act"), result
    assert "error" not in result, result


def test_state_binds_the_numbered_menu_it_rendered():
    """state() is a render path too: what it shows is what act N replies to."""
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    _render_first_menu(handlers, ctx, client)

    client.script_state["pending_request"] = {
        "type": "choice_request", "id": "menu-b", "choices": list(_R64_MENU_B),
    }
    client.script_state["game_state"] = _choice_game_state(_R64_MENU_B)

    rendered = handlers.handle_state(ctx, {"brief": False})

    assert _R64_MENU_B[0] in rendered["pending"]
    assert client.last_request_id == "menu-b"
    assert client.last_choices == _R64_MENU_B

    result = handlers.handle_act(ctx, {"target": "1", "wait": False})

    assert not result.get("_stale_numeric_act"), result
    assert "error" not in result, result


def test_a_genuinely_renumbered_menu_is_still_refused():
    """The guard is not loosened: a real decision change still refuses.

    echo64-s05's refusal was correct -- a row had vanished and everything
    below it renumbered.  Recording the render must not turn that into an
    accept.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    _render_first_menu(handlers, ctx, client)

    client.script_state["pending_request"] = {
        "type": "choice_request", "id": "menu-b", "choices": list(_R64_MENU_B),
    }
    client.script_state["game_state"] = _choice_game_state(_R64_MENU_B)
    rendered = handlers.handle_wait(ctx, {"timeout": 1})
    assert _R64_MENU_B[0] in rendered["pending"]
    assert client.last_request_id == "menu-b"

    # The world moves on without the agent looking again: a different menu,
    # with a different first row, is what index 1 now means.
    replaced = ["Call the mainland now.", "Wait for the storm to pass."]
    client.script_state["pending_request"] = {
        "type": "choice_request", "id": "menu-c", "choices": list(replaced),
    }
    client.script_state["game_state"] = _choice_game_state(replaced)

    result = handlers.handle_act(ctx, {"target": "1", "wait": False})

    assert result["_stale_numeric_act"] is True
    assert "state changed since you last looked" in result["error"]
    assert result["stale_details"]["rendered_request_id"] == "menu-b"
    assert result["stale_details"]["current_request_id"] == "menu-c"
    assert result["stale_details"]["rendered_choices"] == _R64_MENU_B


# ---------------------------------------------------------------------------
# Roadwarden R67: the pacing-deferred-activation resync race
#
# The shim renders a menu, pushes its choice_request to the bridge/agent, and
# then (see vnflight.rpy _compute_pacing_delay / the menu wrapper's
# "_pacing_delay > 0" branch) DEFERS arming local choice-resolution
# (_vnf_request.request_id/value_map) until a user-pacing delay elapses.
# An act() landing in that window used to fall straight into "No active
# choice request for choice resolution" even though nothing about the menu
# had changed -- the R67 Roadwarden bridge log shows 157/767 command_results
# (20.5%) hit exactly this, and 156/157 of the shim's own resync replies came
# back "Restored local choice state" (the SAME pending request id restored,
# never a new/different one) -- proof this was always the deferred-pacing
# race, not a genuinely moved menu, a sidebar re-render, or an orphaned
# choice.  These two tests pin that shape end to end on a real BridgeClient:
# the "before" test reproduces the fail+resync+retry recovery exactly as the
# unfixed shim produces it (3 shim-side commands for one agent-visible act);
# the "after" test is what the fixed shim (vnflight.rpy _vnf_cmd_act firing
# the deferred activation inline instead of failing closed) collapses it to
# -- a single command, same outcome.
# ---------------------------------------------------------------------------

_R67_GATE_MENU = ["I approach the gate slowly."]


def _render_r67_gate_menu(handlers, ctx, client):
    """Play up to the point where the single-choice 'continue' menu, the
    dominant shape in the R67 sample (63/157 fails), is on screen."""
    client.script_state["pending_request"] = {
        "type": "choice_request", "id": "menu-gate",
        "choices": list(_R67_GATE_MENU),
    }
    client.script_state["game_state"] = _choice_game_state(_R67_GATE_MENU)
    client.push_events({"type": "choice_request", "id": "menu-gate",
                        "choices": list(_R67_GATE_MENU), "_seq": 1})
    rendered = handlers.handle_wait(ctx, {"timeout": 1})
    assert _R67_GATE_MENU[0] in rendered["pending"]
    assert client.last_request_id == "menu-gate"


def test_pacing_deferred_activation_reproduces_r67_dominant_resync_trigger():
    """Before the fix: act fails on the deferred-pacing window, resync
    restores the SAME request id ("Restored local choice state" -- never a
    new one), the retry lands -- 2 extra shim round trips for a menu that
    never moved, but dup-not-drop and the eventual outcome hold.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    _render_r67_gate_menu(handlers, ctx, client)

    posted = []
    next_seq = [2]

    def post_handler(path, data):
        if path != "/command":
            return None
        posted.append(dict(data))
        name = data.get("name")
        act_attempts = sum(1 for p in posted if p.get("name") == "act")
        if name == "act" and act_attempts == 1:
            # The shim's local activation is still deferred for
            # user-pacing (_compute_pacing_delay); the menu itself has
            # not changed, but _vnf_request isn't armed yet.
            return 200, {
                "action_nonce": data.get("nonce"),
                "transaction_state": "failed",
                "error": "No active choice request for choice resolution",
            }
        if name == "resync":
            nonce = (data.get("args") or {}).get("nonce")
            client.push_events({
                "type": "command_result", "command": "resync",
                "nonce": nonce, "success": True,
                "message": "Restored local choice state",
                "_seq": next_seq[0],
            })
            next_seq[0] += 1
            return 200, {"ok": True}
        if name == "act" and act_attempts == 2:
            # Retry resolves against the exact same request -- no
            # reissued_from_request_id, no new_request_id anywhere.
            return 200, {
                "action_nonce": data.get("nonce"),
                "transaction_state": "settled",
                "resolved_as": "choice",
                "label": _R67_GATE_MENU[0],
                "index": 1,
            }
        return None

    client.post_handler = post_handler

    result = handlers.handle_act(ctx, {"target": "1", "wait": False})

    assert _result_succeeded_helper(result)
    assert result["_resynced_before_act"] is True
    assert result["_original_error"].startswith("No active choice request")
    assert result["resolved_as"] == "choice"
    assert result["label"] == _R67_GATE_MENU[0]

    act_posts = [p for p in posted if p.get("name") == "act"]
    resync_posts = [p for p in posted if p.get("name") == "resync"]
    assert len(act_posts) == 2, "exactly one retry -- no runaway, no drop"
    assert len(resync_posts) == 1
    # dup-not-drop: the two act attempts carried different nonces (the
    # failed one is retired, never double-counted as a committed choice).
    assert act_posts[0]["nonce"] != act_posts[1]["nonce"]


def test_pacing_fix_collapses_resync_round_trip_to_a_single_act():
    """After the fix: the shim arms the deferred pacing activation inline
    when act() lands inside the window instead of failing closed, so the
    first (and only) act succeeds -- one shim command instead of three,
    same resolved choice, same dup-not-drop guarantees untouched.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    _render_r67_gate_menu(handlers, ctx, client)

    posted = []

    def post_handler(path, data):
        if path != "/command":
            return None
        posted.append(dict(data))
        if data.get("name") == "act":
            return 200, {
                "action_nonce": data.get("nonce"),
                "transaction_state": "settled",
                "resolved_as": "choice",
                "label": _R67_GATE_MENU[0],
                "index": 1,
            }
        return None

    client.post_handler = post_handler

    result = handlers.handle_act(ctx, {"target": "1", "wait": False})

    assert _result_succeeded_helper(result)
    assert not result.get("_resynced_before_act")
    assert result["resolved_as"] == "choice"
    assert result["label"] == _R67_GATE_MENU[0]

    assert len(posted) == 1, (
        "the fixed shim needs exactly one command where the race needed "
        "three (act, resync, act) -- this is the latency saving"
    )
    assert posted[0]["name"] == "act"


def _result_succeeded_helper(result):
    transaction_state = result.get("transaction_state")
    if transaction_state:
        return transaction_state not in {"failed", "rejected"}
    return bool(result.get("success", result.get("ok")))


def test_a_rendered_menu_blocks_the_applied_only_handback():
    """The relaxed arm may never re-present a surface as a successor.

    With the receipt applied but not settled, a rendered decision surface --
    stale or fresh -- keeps the act polling.  Only a story-only render, which
    carries no numbers at all, may be handed back on E1' alone.
    """
    from vnflight import act_settle, handlers

    client = ScriptedBridgeClient()
    ctx, settle = _settle_observer(client, story_entry=True)
    settle.note_transaction({"transaction_state": "applied"})
    rendered = {
        "text": "The lights flicker twice.",
        "pending": "--- CHOICE REQUIRED ---\n  1: Stay",
        "_pending_raw": {"id": "menu-1", "choices": ["Stay"]},
    }
    settle.judge(rendered)
    evidence = settle.evidence(rendered)
    evidence.settled_story_seconds = act_settle._ACT_STORY_HANDBACK_SECONDS
    evidence.story_flowing = True
    # A STALE menu: E3 is false (it is the surface the act was aimed at), so
    # only pending_surface stands between the relaxed arm and a re-present.
    evidence.decision = False
    assert evidence.pending_surface is True

    assert act_settle._act_settle_positive_verdict(evidence) is None

    evidence.pending_surface = False
    assert act_settle._act_settle_positive_verdict(evidence) == (
        act_settle._ACT_SETTLE_STORY_HANDBACK
    )


# ---------------------------------------------------------------------------
# Fleet R63 defect 1/2 — the choice act's own scoped receipt drain
# ---------------------------------------------------------------------------
#
# A choice resolution's receipt settles only on a successor request, a game
# end, or fifteen seconds of attributed silence (the bridge's
# _ACTION_CHOICE_ATTRIBUTED_OUTCOME_SETTLE_GRACE; the structural screen
# boundary is deliberately suppressed for choice resolutions).  An unbroken
# narration burst therefore keeps _wait_action_transaction polling for the
# WHOLE act budget, and everything that could hand the story back -- the two
# drains, _settle_act_until_verdict, the _ActSettleObserver -- is constructed
# only AFTER it returns.  Fleet R63: 56 acts to the 60 s deadline with the
# legacy "still settling" banner, all of them numeric story choices, 12/12
# agents on Marcus's "48 hours" beat.

_R63_PRE_ACT_MENU = {
    "pending": (
        "--- CHOICE REQUIRED ---\n"
        "  1: “You’re right. We file the report.”\n"
        "  2: “Not yet. Give me 48 hours.”"
    ),
    "_pending_raw": {
        "id": "menu-1",
        "choices": [
            "“You’re right. We file the report.”",
            "“Not yet. Give me 48 hours.”",
        ],
    },
}


def _r63_scoped_choice_receipt():
    """The receipt shape a choice act gets while its burst is still running."""
    return {
        "action_nonce": "n1",
        "action_id": 7,
        "transaction_state": "applied",
        "pending": True,
        "resolved_as": "choice",
        "resolved_label": "“Not yet. Give me 48 hours.”",
        "delivery_end": 100,
        # The act's OWN resolution leads the scoped stream.  On a plain wait
        # this row is a boundary; here it is a receipt and must not disarm
        # the hand-back.
        "events": [{
            "type": "choice_resolved",
            "text": "“Not yet. Give me 48 hours.”",
            "_seq": 100,
            "action_id": 7,
        }],
    }


def _r63_act_params(issued_at, timeout=60.0):
    return {
        "timeout": timeout,
        "action_nonce": "n1",
        "_result_deadline": issued_at + timeout,
        "_act_issued_at": issued_at,
    }


def _r63_settle(ctx, result, params):
    from vnflight import handlers

    handlers._settle_wait_after_action(
        ctx,
        result,
        params,
        button_context=False,
        pre_state_sig=None,
        pre_rendered=_R63_PRE_ACT_MENU,
        pre_visible_sig=handlers._visible_output_signature(_R63_PRE_ACT_MENU),
        pre_pending_id="menu-1",
        pre_was_button_only=False,
        acted_request_id="menu-1",
    )


def _r63_story_lines(text):
    return [
        line.strip() for line in str(text or "").splitlines()
        if line.strip().startswith("Line ")
    ]


def test_numeric_choice_into_a_long_burst_hands_back_from_the_scoped_drain(
    monkeypatch,
):
    """Fleet R63 defect 1: a choice act must hand back like a story entry.

    Sequence test on a real BridgeClient: the receipt never settles and the
    burst never pauses, so before the fix the scoped drain owned the entire
    act budget and the act returned at 60.0 s with the legacy settling banner
    and no menu.  It must now return at the hand-back bound with the story so
    far, ``story_continues``, the hint, and NO numbered menu -- the pre-act
    menu is consumed and no successor is claimed.
    """
    from vnflight import act_settle, handlers

    monkeypatch.setattr(handlers, "_ACT_STORY_HANDBACK_SECONDS", 4.0)
    monkeypatch.setattr(act_settle, "_ACT_STORY_HANDBACK_SECONDS", 4.0)

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    receipt = _r63_scoped_choice_receipt()
    client.set_transaction(receipt)
    client._active_action_nonces.append("n1")
    client._action_nonce_started["n1"] = time.time()

    stop = threading.Event()
    emitted = []

    def narrate():
        seq = 100
        while not stop.is_set():
            seq += 1
            emitted.append(seq)
            receipt["events"] = receipt["events"] + [{
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
                "action_id": 7,
            }]
            receipt["delivery_end"] = seq
            time.sleep(0.3)

    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        result = {}
        issued_at = time.time()
        _r63_settle(ctx, result, _r63_act_params(issued_at))
        elapsed = time.time() - issued_at
    finally:
        pass

    try:
        # The whole point: it did not run to the caller's 60 s budget, and it
        # landed at the constant rather than a chunk boundary beyond it.
        assert elapsed < handlers._ACT_STORY_HANDBACK_SECONDS + 2.0, elapsed
        assert result.get("story_continues") is True
        assert act_settle._ACT_STORY_HANDBACK_HINT in result["warning"]

        # The numeric-act contract: story only, nothing numbered to answer.
        assert not result.get("pending")
        rendered = handlers.render_tool_result_text(result)
        assert "CHOICE REQUIRED" not in rendered
        assert "48 hours" not in rendered.split("Line ")[0]
        act_lines = _r63_story_lines(result.get("text"))
        assert act_lines, "the story so far must be returned, not withheld"

        # ... and the caller's next wait() continues the same transaction.
        # The successor menu arrives only once the burst ends, exactly as the
        # bridge settles a choice receipt.
        time.sleep(0.6)
        stop.set()
        narrator.join(timeout=2)
        receipt["transaction_state"] = "settled"
        receipt["settled_pending"] = {
            "type": "choice_request",
            "id": "menu-2",
            "choices": ["Open the file", "Leave it"],
        }
        client.script_state["pending_request"] = receipt["settled_pending"]
        continuation = handlers.handle_wait(ctx, {"timeout": 10})
    finally:
        stop.set()
        narrator.join(timeout=2)

    wait_lines = _r63_story_lines(continuation.get("text"))
    assert wait_lines, "the wait must continue the same story"
    # No duplicate and no gap across the hand-back seam.
    assert not set(act_lines) & set(wait_lines)
    last_act = int(act_lines[-1].split()[-1].strip("."))
    first_wait = int(wait_lines[0].split()[-1].strip("."))
    assert first_wait == last_act + 1, (act_lines[-1], wait_lines[0])
    assert continuation.get("pending"), "the successor menu was never presented"


def test_the_scoped_handback_is_measured_from_when_the_act_was_issued(
    monkeypatch,
):
    """Fleet R63 defect 2: the bound is wall time, not qualified time.

    R63 measured act hand-backs at a median 31.9 s against a 20 s constant,
    because the clock only started once the receipt's own scoped drain had
    returned.  Give the act a head start and the hand-back must arrive
    EARLIER, not at the same distance from the settle loop's first look.
    """
    from vnflight import act_settle, handlers

    monkeypatch.setattr(handlers, "_ACT_STORY_HANDBACK_SECONDS", 4.0)
    monkeypatch.setattr(act_settle, "_ACT_STORY_HANDBACK_SECONDS", 4.0)

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    receipt = _r63_scoped_choice_receipt()
    client.set_transaction(receipt)
    client._active_action_nonces.append("n1")
    client._action_nonce_started["n1"] = time.time()

    stop = threading.Event()

    def narrate():
        seq = 200
        while not stop.is_set():
            seq += 1
            receipt["events"] = receipt["events"] + [{
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
                "action_id": 7,
            }]
            receipt["delivery_end"] = seq
            time.sleep(0.3)

    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        # The act was issued two seconds before this settle call -- pre-act
        # reads, submission, acceptance.  Those seconds are inside the bound.
        issued_at = time.time() - 2.0
        result = {}
        started = time.time()
        _r63_settle(ctx, result, _r63_act_params(issued_at))
        elapsed = time.time() - started
    finally:
        stop.set()
        narrator.join(timeout=2)

    assert result.get("story_continues") is True
    # 4 s bound, 2 s already spent: the drain may hold roughly two more.
    assert elapsed < handlers._ACT_STORY_HANDBACK_SECONDS, elapsed
    assert time.time() - issued_at < (
        handlers._ACT_STORY_HANDBACK_SECONDS + 2.0
    )


def test_a_choice_whose_menu_appears_early_still_returns_on_the_decision():
    """The hand-back must not delay an act that simply finished.

    The successor menu lands well inside the hand-back window; the receipt
    settles with it and the act returns on E1+E3, with the menu, and without
    the story-continues marker.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    receipt = _r63_scoped_choice_receipt()
    client.set_transaction(receipt)
    client._active_action_nonces.append("n1")
    client._action_nonce_started["n1"] = time.time()

    def resolve():
        seq = 300
        for _ in range(4):
            seq += 1
            receipt["events"] = receipt["events"] + [{
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
                "action_id": 7,
            }]
            receipt["delivery_end"] = seq
            time.sleep(0.4)
        receipt["transaction_state"] = "settled"
        receipt["settled_pending"] = {
            "type": "choice_request",
            "id": "menu-2",
            "choices": ["Open the file", "Leave it"],
        }
        client.script_state["pending_request"] = receipt["settled_pending"]

    resolver = threading.Thread(target=resolve, daemon=True)
    resolver.start()
    try:
        result = {}
        issued_at = time.time()
        _r63_settle(ctx, result, _r63_act_params(issued_at))
        elapsed = time.time() - issued_at
    finally:
        resolver.join(timeout=5)

    assert elapsed < handlers._ACT_STORY_HANDBACK_SECONDS, elapsed
    assert not result.get("story_continues")
    assert result.get("pending"), "the successor menu must be presented"
    assert "menu-2" == handlers._pending_request_id(result)


# ---------------------------------------------------------------------------
# Fleet R64 defect 1 — acting straight off a hand-back
# ---------------------------------------------------------------------------
#
# The hand-back returns while the prior receipt is still applied and its burst
# is still running.  echo64-s04 and echo64-s07 each answered the successor menu
# the moment it rendered and were told "Another action is still in flight" ten
# seconds later.  The bridge now admits over an applied blocker whose outcome
# grace has run (tests/test_bridge.py), and reports the hold when it has not;
# from the caller's side the act must simply land, and the burst that was still
# arriving must cross the seam exactly once.

_R64_SUCCESSOR_MENU = {
    "type": "choice_request",
    "id": "menu-2",
    "choices": ["What is coming? Tell me everything.", "Who sent you?"],
}


def test_act_during_unread_burst_returns_story_before_accepting_new_choice(
    monkeypatch,
):
    from vnflight import act_settle, handlers

    monkeypatch.setattr(handlers, "_ACT_STORY_HANDBACK_SECONDS", 3.0)
    monkeypatch.setattr(act_settle, "_ACT_STORY_HANDBACK_SECONDS", 3.0)

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    prior = {
        "action_nonce": "n1",
        "action_id": 7,
        "transaction_state": "applied",
        "pending": True,
        "resolved_as": "choice",
        "delivery_end": 100,
        "events": [],
    }
    client.set_transaction(prior, nonce="n1")
    client._active_action_nonces.append("n1")
    client._action_nonce_started["n1"] = time.time()

    # The bridge's own reattribution, scripted: rows belong to the prior act
    # until the successor is DISPATCHED, and to the successor after that.
    owner = {"receipt": prior, "action_id": 7}
    stop = threading.Event()

    def narrate():
        seq = 100
        while not stop.is_set():
            seq += 1
            receipt = owner["receipt"]
            row = {
                "type": "narration",
                "text": "Line {}.".format(seq),
                "_seq": seq,
                "action_id": owner["action_id"],
            }
            receipt["events"] = receipt["events"] + [dict(row)]
            receipt["delivery_end"] = seq
            # push_event puts every row on BOTH lanes: the owning receipt's
            # scoped drain and the ordinary transcript.  Delivering each row
            # exactly once across the two is the client ledger's job.
            client.push_events(dict(row))
            time.sleep(0.3)

    def answer_post(path, payload):
        if path != "/command" or payload.get("name") != "act":
            return None
        nonce = payload.get("nonce")
        if len(
            [1 for call_path, body in client.http_posts
             if call_path == "/command" and body.get("name") == "act"]
        ) == 1:
            # The bridge that has not yet let go: one 409, and it says when
            # the gate reopens rather than leaving the client to guess.
            return 409, {
                "action_nonce": nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "action_in_flight",
                "blocking_action_id": 7,
                "blocking_action_nonce": "n1",
                "blocking_transaction_state": "applied",
                "admission_retry_after": 0.4,
            }
        successor = {
            "action_nonce": nonce,
            "action_id": 8,
            "transaction_state": "settled",
            "pending": False,
            "resolved_as": "choice",
            "delivery_end": owner["receipt"]["delivery_end"],
            "events": [],
            "settled_pending": {
                "type": "choice_request",
                "id": "menu-3",
                "choices": ["Ask about the probe", "Say nothing"],
            },
        }
        client.set_transaction(successor, nonce=str(nonce))
        owner["receipt"] = successor
        owner["action_id"] = 8
        return 200, {
            "action_nonce": nonce,
            "action_id": 8,
            "transaction_state": "accepted",
            "pending": True,
        }

    client.post_handler = answer_post
    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        handback = handlers.handle_wait(ctx, {"timeout": 20})
        # The successor menu is on screen by the time the agent answers it.
        client.script_state["pending_request"] = dict(_R64_SUCCESSOR_MENU)
        started = time.time()
        act_result = handlers.handle_act(ctx, {
            "target": "1",
            "timeout": 15,
            "_result_deadline": started + 15,
        })
        act_elapsed = time.time() - started
        stop.set()
        narrator.join(timeout=3)
        tail = handlers.handle_wait(ctx, {"timeout": 3})
    finally:
        stop.set()
        narrator.join(timeout=3)

    assert handback.get("story_continues") is True
    assert act_settle._ACT_STORY_HANDBACK_HINT in handback["warning"]

    # A visible successor cannot overtake unread predecessor narration.
    # Return the burst without submitting the new choice.
    assert act_result.get("_act_not_submitted") is True
    assert "previous action" in act_result["error"]
    assert act_elapsed < 8.0, act_elapsed

    act_posts = [
        body for path, body in client.http_posts
        if path == "/command" and body.get("name") == "act"
    ]
    assert act_posts == []

    # ... and the burst that was mid-flight crossed the seam intact.
    #
    # Dup-not-drop, stated exactly: every row the bridge emitted reaches the
    # agent exactly once across the three calls.  It is deliberately NOT
    # "the act continues the hand-back line for line": rows attributed to the
    # retired nonce after the client stopped draining it come back on the
    # ORDINARY lane, so the act's own drain sees the successor's rows and the
    # following wait() picks up the tail.  Both orders are the real system;
    # a row served twice, or never, would not be.
    handback_lines = _r63_story_lines(handback.get("text"))
    act_lines = _r63_story_lines(act_result.get("text"))
    tail_lines = _r63_story_lines(tail.get("text"))
    assert handback_lines, "the hand-back must return the story so far"
    assert act_lines, "the act must return the burst it drained"
    delivered = handback_lines + act_lines + tail_lines
    assert len(delivered) == len(set(delivered)), delivered
    seqs = sorted(int(line.split()[-1].strip(".")) for line in delivered)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs
    assert seqs[0] == 101


# ---------------------------------------------------------------------------
# Fleet R64 defect 4 — every story-only act return carries the marker
# ---------------------------------------------------------------------------


def test_a_numeric_act_recovered_mid_scene_carries_the_handback_hint():
    """The ~13.2 s exit path: a numeric act that never dispatched a click.

    ``_bind_numeric_act_target`` refuses a number when nothing is numbered --
    unless ``_recover_numeric_act_during_autoadvance`` can drain the story
    that raced the click, in which case it returns a SUCCESSFUL result from
    inside the phase loop, before ``handle_act`` ever reaches the finalize
    pass.  Fleet R64 counted 7 of those: continuing story, no menu, no marker,
    all at ~13.2 s (a 1.5 s state-change probe plus the recovery drain's cap).
    """
    from vnflight import act_settle, handlers

    client = ScriptedBridgeClient()
    ctx = handlers.HandlerContext(client=client)
    # The agent answered "6: Step away from the console" a turn ago, so the
    # menu this "4" replies to is already consumed and nothing is numbered.
    client.last_request_id = "menu-1"

    def narrate():
        time.sleep(0.5)
        client.push_events(
            {"type": "narration", "text": "Line 501.", "_seq": 501},
            {"type": "dialogue", "text": "Line 502.", "_seq": 502},
        )

    narrator = threading.Thread(target=narrate, daemon=True)
    narrator.start()
    try:
        issued = time.time()
        result = handlers.handle_act(ctx, {
            "target": "4",
            "timeout": 8,
            "_result_deadline": issued + 8,
        })
    finally:
        narrator.join(timeout=3)

    # Nothing was ever clicked -- no act reached the bridge.
    assert not [
        body for path, body in client.http_posts
        if path == "/command" and body.get("name") == "act"
    ]
    assert result.get("_recovered_after_advance") is True
    assert _r63_story_lines(result.get("text")), "the drained story is returned"
    assert not result.get("pending")

    # ...and it says so, where the agent actually reads it.
    assert result.get("story_continues") is True
    rendered = handlers.render_tool_result_text(result)
    assert act_settle._ACT_STORY_HANDBACK_HINT in rendered
    assert rendered.index(act_settle._ACT_STORY_HANDBACK_HINT) < rendered.index(
        "Line 501.")


def test_the_finalize_pass_marks_any_story_only_act_return():
    """One place, one rule: story, nothing to answer, no ending -> the hint."""
    from vnflight import act_settle, handlers

    result = {
        "ok": True, "success": True,
        "transaction_state": "settled",
        "text": "The reroute queue answers.",
    }
    handlers.finalize_act_presentation(result, actionable_decision=False)

    assert result["story_continues"] is True
    assert act_settle._ACT_STORY_HANDBACK_HINT in result["warning"]


@pytest.mark.parametrize("shape", [
    {"pending": "--- CHOICE REQUIRED ---\n  1: Stay"},
    {"buttons": "--- OTHER BUTTONS ---\n  1: CLOSE"},
    {"_data": {"pending": {"type": "choice_request", "id": "m"}}},
    {"ended": True},
    {"result_timeout_reached": True},
    {"_scene_unchanged": True},
    {"error": "Did not act - nothing is numbered right now."},
    {"ok": False, "success": False, "transaction_state": "failed"},
])
def test_the_finalize_pass_stays_silent_where_wording_already_exists(shape):
    """Anything to answer, an ending, or its own wording: no second promise."""
    from vnflight import handlers

    result = {
        "ok": True, "success": True,
        "transaction_state": "settled",
        "text": "The reroute queue answers.",
    }
    result.update(shape)
    handlers.finalize_act_presentation(result, actionable_decision=False)

    assert "story_continues" not in result


def test_a_silent_game_still_stops_at_the_story_drains_idle_cap():
    """The drains' own caps stay the fallback for games that say nothing.

    The hand-back clock only runs while story is arriving, so a genuinely
    silent game reaches no hand-back at all and must still be bounded by the
    story-gap drain's idle window rather than the caller's whole budget.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient()
    ctx, settle = _settle_observer(client)
    settle.note_transaction({"transaction_state": "applied"})
    opening = {"text": "The corridor is empty."}
    assert settle.story_handback_at is None

    params = {
        "timeout": 60,
        "_result_deadline": time.time() + 60,
        "_story_transition_idle_timeout": 1.5,
    }
    started = time.time()
    handlers._drain_story_gap_after_choice_action(
        ctx, {}, params, opening, settle)
    elapsed = time.time() - started

    assert 1.0 < elapsed < 6.0, elapsed


class _SuccessorMenuClient(MockClient):
    """A choice whose successor menu renders only after several polls."""

    def __init__(self, story_waits=3):
        super().__init__()
        self._act_result = {
            "ok": True,
            "success": True,
            "resolved_as": "choice",
            "label": "Open the hatch",
        }
        self._story_waits = story_waits
        self.wait_calls = 0

    def wait(self, timeout=60, **kw):
        self.calls.append(("wait", {"timeout": timeout, **kw}))
        self.wait_calls += 1
        if self.wait_calls == 1:
            return MockWaitResult(
                events=[{"type": "narration", "text": "The hatch groans."}],
                transaction={
                    "action_nonce": "n1",
                    "action_id": 4,
                    "transaction_state": "settled",
                    "pending": False,
                },
            )
        if self.wait_calls <= self._story_waits:
            # The successor has NOT rendered yet.
            return MockWaitResult()
        return MockWaitResult(pending={
            "type": "choice_request",
            "id": "menu-2",
            "choices": ["Climb down", "Stay"],
        })


def test_choice_act_returns_when_the_successor_menu_renders():
    """(c) The decision ends the act -- and nothing earlier does."""
    from vnflight import handlers
    from vnflight.handlers import HandlerContext

    client = _SuccessorMenuClient()
    ctx = HandlerContext(client=client)
    result = dict(client._act_result)
    result["action_nonce"] = "n1"

    handlers._settle_wait_after_action(
        ctx,
        result,
        {"timeout": 20, "action_nonce": "n1",
         "_story_transition_idle_timeout": 20},
        button_context=False,
        pre_state_sig=None,
        pre_rendered=None,
        pre_visible_sig=None,
        pre_pending_id="menu-1",
        pre_was_button_only=False,
        acted_request_id="menu-1",
    )

    assert "Climb down" in result["pending"]
    assert "The hatch groans." in result["text"]
    # It kept polling across the un-rendered successor rather than handing
    # back the empty frames, and stopped as soon as the menu appeared.
    assert client.wait_calls >= 4


def test_screen_action_cleanup_keeps_the_actions_own_script_lines():
    """Fleet R62 defect 0, screen path: a say line is not "rendered state".

    Sequence test on a real ``BridgeClient``.  The action's own NVL line is
    parked by the production ``ordinary_action_id`` fence, the screen action
    then settles through ``state()`` and retires its receipt, and the line
    must reach the agent -- on the act result if the cleanup receipt still
    holds it, otherwise on the next wait -- and exactly once.
    """
    from vnflight import handlers

    client = ScriptedBridgeClient(state={"status": "running"})
    client.set_screen({
        "buttons": [{"label": "CLOSE", "screen": "evidence_screen"}],
    })
    client.push_events({
        "type": "narration", "text": "The lab hums to itself.", "_seq": 100,
    })
    assert [event["_seq"] for event in client.poll(timeout=0)] == [100]

    repair_line = (
        "Dr. Voss, the storm has damaged the antenna array. A local bypass "
        "requires one spare coupling."
    )
    client.push_events({
        "type": "dialogue", "character": "ARIA>", "text": repair_line,
        "mode": "nvl", "action_id": 27, "_seq": 130,
    })
    # An ordinary poll owned by another action fences this row into the
    # prefetch stash instead of delivering it -- production's own path.
    assert client.poll(timeout=0, ordinary_action_id=11) == []
    assert [e["_seq"] for e in client._prefetched_events] == [130]

    client.set_transaction({
        "action_nonce": "n27", "action_id": 27,
        "transaction_state": "settled", "pending": False,
    })
    ctx = handlers.HandlerContext(client=client)
    result = {
        "ok": True, "success": True, "resolved_as": "button",
        "action_nonce": "n27", "action_id": 27,
    }
    handlers._settle_state_after_screen_action(
        ctx,
        result,
        {"_result_deadline": time.time() + 3},
        pre_state_sig=None,
        pre_was_button_only=False,
    )
    act_text = handlers.render_tool_result_text(result)

    follow_up = handlers.render_tool_result_text(
        handlers.handle_wait(ctx, {"timeout": 1}))

    delivered = "\n".join([act_text, follow_up])
    assert repair_line in delivered, (
        "the screen-action cleanup dropped the script line the panel state "
        "cannot represent"
    )
    assert delivered.count(repair_line) == 1
    assert client._prefetched_events == []


def test_act_settle_keeps_story_a_rejected_followup_wait_consumed():
    """Fleet R62 defect 0, wait path: a consumed wait is never droppable.

    Sequence test on a real ``BridgeClient``.  The post-settle follow-up wait
    inside ``_settle_wait_after_action`` polls the ordinary lane, so it moves
    the cursor and books action rows in the delivery ledger.  Rejecting its
    render because no successor menu rendered yet -- the NVL interlude
    between a console screen and its next menu -- must not take the script
    lines it drained with it.
    """
    from vnflight import handlers

    console_pending = {
        "type": "choice_request", "id": "console-1",
        "choices": ["01 ARIA TRUST PROTOCOL", "Step away from the console."],
    }
    client = ScriptedBridgeClient(state={
        "status": "waiting_for_input",
        "context": {"context": "game"},
        "pending_request": dict(console_pending),
    })
    client.set_screen({"buttons": [
        {"label": "01 ARIA TRUST PROTOCOL"},
        {"label": "Step away from the console."},
    ]})
    client.push_events({
        "type": "narration", "text": "The console waits.", "_seq": 100,
    })
    assert [event["_seq"] for event in client.poll(timeout=0)] == [100]

    opening = "Every speaker on the station clears its throat at once."
    client.set_transaction({
        "action_nonce": "n27", "action_id": 27,
        "transaction_state": "settled", "pending": False,
        "admission_open": True, "delivery_end": 120,
        "settled_pending": dict(console_pending),
        "events": [{
            "type": "narration", "text": opening, "mode": "nvl",
            "action_id": 27, "_seq": 120,
        }],
    })
    client.push_events({
        "type": "narration", "text": opening, "mode": "nvl",
        "action_id": 27, "_seq": 120,
    })

    alert = "ALERT: ANTENNA ARRAY DAMAGE DETECTED"
    repair = "A local bypass requires one spare coupling."
    stop = threading.Event()

    def interlude():
        # The click's own frame lands first: the rendered surface changes
        # while the consumed console menu is still the bridge's pending.
        if stop.wait(1.8):
            return
        client.set_screen({"buttons": [
            {"label": "01 ARIA TRUST PROTOCOL"},
            {"label": "Step away from the console. (used)"},
        ]})
        # Then the console screen closes into a scripted NVL page: story keeps
        # arriving with no decision behind it.
        if stop.wait(1.2):
            return
        client.script_state["pending_request"] = None
        client.script_state["status"] = "running"
        client.set_screen({"buttons": []})
        client.push_events(
            {"type": "dialogue", "character": "SYSTEM", "text": alert,
             "mode": "nvl", "_seq": 132},
            {"type": "dialogue", "character": "ARIA>", "text": repair,
             "mode": "nvl", "_seq": 138},
        )

    ctx = handlers.HandlerContext(client=client)
    pre_rendered = handlers.handle_state(
        ctx, {"brief": False, "_suppress_details": True})
    worker = threading.Thread(target=interlude, daemon=True)
    worker.start()
    try:
        result = {
            "ok": True, "success": True, "resolved_as": "button",
            "interaction_type": "choice", "wait_after_action": True,
            "label": "Step away from the console.",
            "action_nonce": "n27", "action_id": 27,
        }
        handlers._settle_wait_after_action(
            ctx,
            result,
            {"timeout": 4, "action_nonce": "n27",
             "_result_deadline": time.time() + 16},
            button_context=True,
            pre_state_sig=handlers._state_signature(pre_rendered),
            pre_rendered=pre_rendered,
            pre_visible_sig=handlers._visible_output_signature(pre_rendered),
            pre_pending_id=None,
            pre_was_button_only=True,
            acted_request_id=None,
            pre_act_seq=100,
        )
        act_text = handlers.render_tool_result_text(result)
        follow_up = handlers.render_tool_result_text(
            handlers.handle_wait(ctx, {"timeout": 2}))
    finally:
        stop.set()
        worker.join(timeout=3)

    delivered = "\n".join([act_text, follow_up])
    assert opening in delivered
    for line in (alert, repair):
        assert line in delivered, (
            "a follow-up wait consumed {!r} and the settle chain threw its "
            "render away".format(line)
        )
        assert delivered.count(line) == 1


def test_absorbed_story_never_repeats_what_the_result_already_shows():
    """The carry is dup-tolerant, but it is not a duplicator."""
    from vnflight import handlers

    result = {"text": "Line one.\nLine two."}
    handlers._absorb_unpromoted_wait_story(result, {
        "text": "Line two.\nLine three.",
        "_data": {"story": [
            {"type": "narration", "text": "Line two."},
            {"type": "narration", "text": "Line three."},
        ]},
    })
    assert result["text"].splitlines() == [
        "Line one.", "Line two.", "Line three.",
    ]

    # A screen scrape is current state, not an occurrence: still droppable.
    screen_only = {"text": "Line one."}
    handlers._absorb_unpromoted_wait_story(screen_only, {
        "text": "FIELD KIT",
        "_data": {"story": [
            {"type": "screen_text", "text": "FIELD KIT",
             "source": "screen_text"},
        ]},
    })
    assert screen_only["text"] == "Line one."


def test_acceptance_unknown_receipt_keeps_its_recovery_guidance():
    """(f) Not-settled receipts are untouched by the settle policy."""
    from vnflight.handlers import HandlerContext, handle_act

    class UnknownAcceptanceClient(MockClient):
        def act_transaction(self, target, *, action_nonce=None,
                            accept_timeout=15.0, deadline=None,
                            invocation=None):
            self.calls.append(("act_transaction", target))
            return {
                "action_nonce": "unknown-1",
                "reset_generation": 0,
                "submitted_target": target,
                "transaction_state": "acceptance_unknown",
                "pending": True,
            }

        def action_transaction(self, action_nonce, *, timeout=3.0):
            return {
                "action_nonce": "unknown-1",
                "transaction_state": "acceptance_unknown",
                "pending": True,
            }

    client = UnknownAcceptanceClient()
    client._state = {"pending_request": {
        "type": "choice_request", "id": "menu-1", "choices": ["Go north"],
    }}
    ctx = HandlerContext(client=client)

    result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 3})

    assert result["transaction_state"] == "acceptance_unknown"
    assert "still settling" in result.get("warning", "")
    assert "story_continues" not in result
    assert result["transaction"]["action_nonce"] == "unknown-1"


def test_story_gap_drain_extends_idle_window_on_new_story(ctx, monkeypatch):
    from vnflight import handlers

    now = 0.0
    wait_results = [
        {
            "text": "[Narrator] The duel continues.",
            "story": ["[Narrator] The duel continues."],
        },
        {
            "text": "[Narrator] The opening appears.",
            "story": ["[Narrator] The opening appears."],
        },
        {
            "pending": "--- CHOICE REQUIRED ---\n  1: Magic sword",
            "_data": {
                "pending": {
                    "type": "choice",
                    "choices": [{"label": "Magic sword", "index": 1}],
                },
            },
        },
    ]
    calls = []

    def fake_time():
        return now

    def fake_handle_wait(wait_ctx, params):
        nonlocal now
        calls.append(params)
        now += 10.0
        return wait_results.pop(0)

    monkeypatch.setattr(handlers.time, "time", fake_time)
    monkeypatch.setattr(handlers, "handle_wait", fake_handle_wait)

    result = {
        "text": "[Narrator] You trade attacks.",
        "story": ["[Narrator] You trade attacks."],
    }
    initial_wait = dict(result)

    final = handlers._drain_story_gap_after_choice_action(
        ctx,
        result,
        {"timeout": 50},
        initial_wait,
    )

    assert len(calls) == 3
    assert "duel continues" in result["text"]
    assert "opening appears" in result["text"]
    assert "Magic sword" in result["pending"]
    assert final["pending"].startswith("--- CHOICE REQUIRED ---")


def test_story_gap_drain_promotes_structured_json_story_chunks(ctx, monkeypatch):
    """A settled nonce wait must not consume JSON story without returning it."""
    from vnflight import handlers

    wait_results = [
        {
            "story": [{"type": "narration", "text": "The cursor blinks."}],
            "_data": {"story": [
                {"type": "narration", "text": "The cursor blinks."},
            ]},
        },
        {
            "story": [{"type": "narration", "text": "Another night."}],
            "_data": {"story": [
                {"type": "narration", "text": "Another night."},
            ]},
        },
        {
            "pending": {
                "type": "choice",
                "choices": [{"label": "Continue", "index": 1}],
            },
            "_data": {"pending": {
                "type": "choice",
                "choices": [{"label": "Continue", "index": 1}],
            }},
        },
    ]

    monkeypatch.setattr(
        handlers, "handle_wait", lambda *_args, **_kwargs: wait_results.pop(0))

    transaction = {
        "action_nonce": "start-json",
        "action_id": 1,
        "transaction_state": "settled",
    }
    result = {"transaction": transaction, "_data": {"transaction": transaction}}
    initial_wait = dict(result)

    final = handlers._drain_story_gap_after_choice_action(
        ctx,
        result,
        {
            "timeout": 5,
            "_allow_empty_story_tail": True,
            "format": "json",
        },
        initial_wait,
    )

    assert [item["text"] for item in result["story"]] == [
        "The cursor blinks.",
        "Another night.",
    ]
    assert result["transaction"] == transaction
    assert final["pending"]["choices"][0]["label"] == "Continue"
    assert wait_results == []


def test_story_transition_drain_deadlines_are_tunable(monkeypatch):
    from vnflight import handlers

    monkeypatch.setattr(handlers.time, "time", lambda: 100.0)

    default_hard = handlers._story_transition_drain_deadline({"timeout": 120})
    assert default_hard == pytest.approx(190.0)

    default_idle = handlers._story_transition_idle_deadline({}, default_hard)
    assert default_idle == pytest.approx(130.0)

    hard = handlers._story_transition_drain_deadline(
        {"timeout": 120, "_story_transition_hard_timeout": 20}
    )
    assert hard == pytest.approx(120.0)

    idle = handlers._story_transition_idle_deadline(
        {"_story_transition_idle_timeout": 5},
        hard,
    )
    assert idle == pytest.approx(105.0)

    disabled_idle = handlers._story_transition_idle_deadline(
        {"_story_transition_idle_timeout": 0},
        hard,
    )
    assert disabled_idle == hard


def test_handle_transcript_formats_non_story_events(ctx):
    from vnflight.handlers import handle_transcript

    ctx.client._transcript = [
        {"type": "command_result", "command": "set", "success": True},
        {"type": "game_ended", "reason": "return_to_menu"},
    ]

    result = handle_transcript(ctx, {"last": 2})

    assert result["text"] == "X Game ended (return_to_menu)"


def test_handle_transcript_formats_choice_request_details(ctx):
    from vnflight.handlers import handle_transcript

    ctx.client._transcript = [
        {
            "type": "choice_request",
            "choices": ["Stay here", "Leave"],
            "interactions": [
                {
                    "type": "topic",
                    "display_label": "Ask about the town",
                    "category": "topics",
                },
            ],
        },
    ]

    result = handle_transcript(ctx, {"last": 1})

    assert "--- CHOICE REQUIRED ---" in result["text"]
    assert "1: Stay here" in result["text"]
    assert "2: Leave" in result["text"]
    assert "--- TOPICS ---" in result["text"]
    assert "3: Ask about the town" in result["text"]


def test_transcript_uses_post_render_disabled_choice_visibility(ctx):
    """Transient augmenter rows must not become phantom transcript choices."""
    from vnflight.handlers import handle_transcript

    ctx.client._transcript = [
        {
            "type": "choice_request",
            "choices": ["Stay close", "Keep distance"],
            "full_items": [
                {"label": "No potion", "is_disabled": True},
                {"label": "Out of quarrels", "is_disabled": True},
                {"label": "Stay close", "is_disabled": False},
                {"label": "Keep distance", "is_disabled": False},
            ],
        },
        {
            "type": "game_state",
            "choices": ["Stay close", "Keep distance"],
            "full_items": [
                {"label": "No potion", "is_disabled": True},
                {"label": "Stay close", "is_disabled": False},
                {"label": "Keep distance", "is_disabled": False},
            ],
        },
    ]

    result = handle_transcript(ctx, {"last": 2})

    assert "No potion" in result["text"]
    assert "Out of quarrels" not in result["text"]


class TestActWaitFollowup:
    """act(wait=...) controls the VN-event-wait follow-up (waiting for the
    game's next surface) -- a separate concern from the stream drain-gate.

    wait:False must NOT follow up with wait()/state(); the default
    (ctx.act_wait=True) does. This is the contract behind "does act wait
    for VN events even with wait:false?" -- it does not."""

    _PENDING = {"pending_request": {"type": "choice_request",
                                    "choices": ["Go north"]}}
    _ACT_OK = {"ok": True, "success": True, "resolved_as": "choice"}

    def test_wait_false_skips_vn_event_followup(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = dict(self._PENDING)
        ctx.client._act_result = dict(self._ACT_OK)
        handle_act(ctx, {"target": "1", "wait": False})
        called = [name for name, _ in ctx.client.action_calls]
        assert "act" in called
        assert "wait" not in called, f"wait:False still waited for VN events: {called}"

    def test_wait_true_does_vn_event_followup(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = dict(self._PENDING)
        ctx.client._act_result = dict(self._ACT_OK)
        handle_act(ctx, {"target": "1", "wait": True})
        called = [name for name, _ in ctx.client.action_calls]
        assert "wait" in called, f"wait:True should follow up with wait(): {called}"

    def test_default_act_wait_follows_up(self, ctx):
        # No explicit wait -> ctx.act_wait default (True) -> it waits.
        from vnflight.handlers import handle_act
        ctx.client._state = dict(self._PENDING)
        ctx.client._act_result = dict(self._ACT_OK)
        assert ctx.act_wait is True
        handle_act(ctx, {"target": "1"})
        called = [name for name, _ in ctx.client.action_calls]
        assert "wait" in called, f"default act should follow up with wait(): {called}"

    @pytest.mark.parametrize("falsy", [False, "false", "False", "0", "no", "off"])
    def test_falsy_wait_values_skip_followup(self, falsy):
        # wait may arrive as a string from the MCP layer; falsy spellings must
        # not silently flip back into waiting (the act(wait:false) bug).
        from vnflight.handlers import HandlerContext, handle_act
        c = MockClient(); c._state = dict(self._PENDING); c._act_result = dict(self._ACT_OK)
        ctx = HandlerContext(client=c)
        handle_act(ctx, {"target": "1", "wait": falsy})
        called = [name for name, _ in c.action_calls]
        assert "wait" not in called, f"wait={falsy!r} wrongly waited: {called}"

    @pytest.mark.parametrize("truthy", [True, "true", "yes", "1"])
    def test_truthy_wait_values_follow_up(self, truthy):
        from vnflight.handlers import HandlerContext, handle_act
        c = MockClient(); c._state = dict(self._PENDING); c._act_result = dict(self._ACT_OK)
        ctx = HandlerContext(client=c)
        handle_act(ctx, {"target": "1", "wait": truthy})
        called = [name for name, _ in c.action_calls]
        assert "wait" in called, f"wait={truthy!r} should follow up: {called}"


class TestHandleAct:
    # -- Round 12: the two surface-change terminal reasons ------------------

    def test_a_stale_surface_rejection_is_not_retried_into_the_new_surface(
        self, ctx,
    ):
        """The bridge refused the act because the interaction moved.

        Pinning the behaviour that already holds: neither the resync retry
        (gated on "No active choice request") nor the scene-advance recovery
        (gated on ``_transient_act_error``) matches these diagnostics, so the
        handler surfaces the failure instead of firing a second act at a
        surface it has not re-read.
        """
        from vnflight.handlers import handle_act

        ctx.client._state = {"pending_request": {
            "type": "choice_request", "id": "request-1",
            "choices": ["Go north", "Go south"],
        }}
        ctx.client._act_result = {
            "ok": False, "success": False,
            "transaction_state": "failed", "reason": "stale_surface",
            "pending": False,
            "action_nonce": "raced", "action_id": 3,
            "error": (
                "The interaction changed while the action was being "
                "accepted; inspect the current state and act again."
            ),
        }

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result.get("success") is False
        assert result.get("reason") == "stale_surface"
        acts = [name for name, _ in ctx.client.calls if name == "act"]
        assert len(acts) == 1, ctx.client.calls

    def test_a_cancelled_successor_is_not_retried_into_the_new_surface(
        self, ctx,
    ):
        """Same discipline for ``admission_revoked_before_dispatch``."""
        from vnflight.handlers import handle_act

        ctx.client._state = {"pending_request": {
            "type": "choice_request", "id": "request-1",
            "choices": ["Go north", "Go south"],
        }}
        ctx.client._act_result = {
            "ok": False, "success": False,
            "transaction_state": "failed",
            "reason": "admission_revoked_before_dispatch",
            "pending": False,
            "action_nonce": "cancelled", "action_id": 4,
            "error": (
                "This action was cancelled before dispatch because the "
                "interaction surface it targeted changed. Retrying with the "
                "same action_nonce returns this cancelled transaction; a NEW "
                "act is admitted once the prior action settles at an observed "
                "story boundary, or once admission reopens after its idle "
                "budget."
            ),
        }

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result.get("success") is False
        assert result.get("reason") == "admission_revoked_before_dispatch"
        acts = [name for name, _ in ctx.client.calls if name == "act"]
        assert len(acts) == 1, ctx.client.calls

    def test_only_the_act_path_asks_to_return_on_admission(self, ctx):
        """Plain-wait drain semantics are unchanged; the act path opts in."""
        from vnflight.handlers import handle_act, handle_wait

        ctx.client._state = {"pending_request": {
            "type": "choice_request", "choices": ["Go north"],
        }}
        ctx.client._act_result = {
            "ok": True, "success": True, "resolved_as": "choice",
        }

        handle_act(ctx, {"target": "1", "wait": True})
        scoped = [
            kwargs for name, kwargs in ctx.client.calls
            if name == "wait" and kwargs.get("action_nonce")
        ]
        assert scoped, ctx.client.calls
        assert all(
            kwargs.get("return_on_admission") is True for kwargs in scoped
        )

        handle_wait(ctx, {"timeout": 1, "action_nonce": scoped[0]["action_nonce"]})
        plain = [
            kwargs for name, kwargs in ctx.client.calls
            if name == "wait" and "return_on_admission" not in kwargs
        ]
        assert plain, "an explicit wait(action_nonce=...) must not opt in"

    # -- Round 16: act(wait=True) returns only once a follow-up is admissible --

    def test_act_wait_returns_only_once_a_follow_up_would_be_admitted(self):
        """The live gap between "settled enough to render" and "admissible".

        Roadwarden, 2026-08-17: _settle_wait_after_action returned on min_wait
        expiry with buttons on screen while the transaction was still applied
        and unreleased, and the agent's next act came straight back as
        409 action_in_flight.
        """
        from vnflight.handlers import HandlerContext, handle_act

        class SlowSettleClient(MockClient):
            def __init__(self):
                super().__init__()
                self.probes = 0

            def act_transaction(self, target, *, action_nonce=None,
                                accept_timeout=15.0, deadline=None,
                                invocation=None):
                self.calls.append(("act_transaction", target))
                return {
                    "ok": True, "success": True,
                    "action_nonce": "slow", "action_id": 3,
                    "transaction_state": "accepted", "pending": True,
                    "submitted_target": target, "resolved_as": "choice",
                }

            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.probes += 1
                state = "applied" if self.probes < 3 else "settled"
                return {
                    "action_nonce": "slow", "action_id": 3,
                    "transaction_state": state,
                    "pending": state != "settled",
                    "resolved_as": "choice", "label": "Go north",
                }

        client = SlowSettleClient()
        client._state = {"pending_request": {
            "type": "choice_request", "choices": ["Go north"],
        }}
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert client.probes >= 2, "it stopped looking too early"
        assert result["transaction_state"] == "settled"
        assert "admission_pending" not in (result.get("transaction") or {})

    def test_act_wait_reports_when_admission_is_still_pending(self, monkeypatch):
        """At the cap it returns as before — but never silently.

        An act with no observable outcome keeps the slot busy for the bridge's
        full idle budget; blocking the caller for that long would be worse
        than the 409 it replaces, so the wait is capped and the pending
        admission is stated on the transaction view.
        """
        import vnflight.handlers as handlers_module
        from vnflight.handlers import HandlerContext, handle_act

        monkeypatch.setattr(
            handlers_module, "_ADMISSION_WAIT_CAP_SECONDS", 0.3)

        class BlockedClient(MockClient):
            def act_transaction(self, target, *, action_nonce=None,
                                accept_timeout=15.0, deadline=None,
                                invocation=None):
                self.calls.append(("act_transaction", target))
                return {
                    "ok": True, "success": True,
                    "action_nonce": "blocked", "action_id": 4,
                    "transaction_state": "accepted", "pending": True,
                    "submitted_target": target, "resolved_as": "choice",
                }

            def action_transaction(self, action_nonce, *, timeout=3.0):
                return {
                    "action_nonce": "blocked", "action_id": 4,
                    "transaction_state": "applied", "pending": True,
                    "resolved_as": "choice",
                }

        client = BlockedClient()
        client._state = {"pending_request": {
            "type": "choice_request", "choices": ["Go north"],
        }}
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result["transaction"]["admission_pending"] is True
        assert 'wait(action_nonce="blocked")' in result["warning"]
        # The admission flags themselves still never reach the act result.
        assert "admission_open" not in result

    def test_the_admission_wait_never_outlives_the_callers_deadline(self):
        """result_timeout is the whole budget; this only ever spends the rest."""
        import time as _time

        from vnflight.handlers import HandlerContext, _await_act_admission

        class BlockedClient(MockClient):
            def __init__(self):
                super().__init__()
                self.probes = 0

            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.probes += 1
                return {
                    "action_nonce": action_nonce,
                    "transaction_state": "applied", "pending": True,
                }

        client = BlockedClient()
        ctx = HandlerContext(client=client)
        result = {
            "ok": True, "action_nonce": "blocked",
            "transaction_state": "applied",
            "text": "The story remains top-level.",
            "story": [{"text": "A formatted story row."}],
            "pending": "--- CHOICE REQUIRED ---",
            "buttons": [{"label": "Continue"}],
        }

        started = _time.time()
        _await_act_admission(ctx, result, deadline=_time.time() - 1)

        assert _time.time() - started < 0.5
        assert client.probes == 0, "an exhausted budget must not perform I/O"
        assert result["transaction"]["admission_pending"] is True
        assert result["transaction"] == {
            "action_nonce": "blocked",
            "transaction_state": "applied",
            "admission_pending": True,
        }
        assert result["pending"] == "--- CHOICE REQUIRED ---"

    def test_timeout_with_verified_successor_does_not_demand_another_wait(self):
        from vnflight.handlers import (
            HandlerContext,
            _mark_act_result_timeout,
            _returned_decision_is_actionable,
        )

        client = MockClient()
        snapshot = {
            "request_type": "choice_request",
            "request": {"choices": ["Continue"]},
            "screen": {"interactions": [{
                "display_label": "Continue",
                "index": 1,
                "action_strs": ["Return value=continue"],
            }]},
        }
        client.last_request_id = "req-next"
        client.last_actionable_snapshot = snapshot
        ctx = HandlerContext(client=client)
        result = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue",
            "transaction": {
                "action_nonce": "slow",
                "transaction_state": "applied",
            },
            "_data": {
                "_pending_raw": {"id": "req-next", "choices": ["Continue"]},
                "_actionable_snapshot": snapshot,
            },
        }

        actionable = _returned_decision_is_actionable(
            ctx, result, acted_request_id="req-old")
        _mark_act_result_timeout(result, actionable_decision=actionable)

        assert result["result_timeout_reached"] is True
        assert result["transaction"]["admission_pending"] is True
        assert result["transaction"]["successor_actionable"] is True
        assert result["warning"].endswith(
            "Call wait() before acting again.")

    def test_timeout_marker_does_not_contradict_failed_transaction(self):
        from vnflight.handlers import _mark_act_result_timeout

        for state in ("failed", "rejected"):
            result = {
                "result_timeout_reached": True,
                "transaction": {
                    "action_nonce": "finished",
                    "transaction_state": state,
                    "pending": False,
                },
            }

            _mark_act_result_timeout(result)

            assert "result_timeout_reached" not in result
            assert "admission_pending" not in result["transaction"]

    def test_settled_transaction_keeps_presentation_timeout_honest(self):
        from vnflight.handlers import _mark_act_result_timeout

        result = {
            "text": "The opening story is available.",
            "transaction": {
                "action_nonce": "finished",
                "transaction_state": "settled",
                "pending": False,
            },
        }

        _mark_act_result_timeout(result)

        assert result["result_timeout_reached"] is True
        assert "admission_pending" not in result["transaction"]
        assert "observation reached the result timeout" in result["warning"]
        assert result["text"] == "The opening story is available."

    def test_handle_act_warns_when_settled_story_reaches_deadline_before_menu(
        self, monkeypatch,
    ):
        import time
        import vnflight.handlers as handlers
        from vnflight.handlers import HandlerContext, handle_act, handle_wait

        class BoundaryClient(MockClient):
            def __init__(self):
                super().__init__()
                self._state = {
                    "game_state": {
                        "interactions": [{
                            "type": "button",
                            "index": 1,
                            "display_label": "Start",
                            "disabled": False,
                        }],
                    },
                }
                self._screen = {
                    "type": "screen_content",
                    "main_menu": True,
                    "screens": ["main_menu"],
                    "buttons": [{"label": "Start", "screen": "main_menu"}],
                }
                self._act_result = {
                    "ok": True,
                    "success": True,
                    "resolved_as": "button",
                    "label": "Start",
                    "screen": "main_menu",
                }
                self.successor_results = [
                    MockWaitResult(pending={
                        "type": "choice_request",
                        "id": "specialization-menu",
                        "choices": ["Signals", "Physics", "Computing"],
                    }),
                    MockWaitResult(),
                ]

            def wait(self, timeout=60, **kw):
                self.calls.append(("wait", {"timeout": timeout, **kw}))
                if self.successor_results:
                    return self.successor_results.pop(0)
                return MockWaitResult()

        def settle_at_deadline(_ctx, result, params, _settle_context):
            # Starting the game consumes the main-menu button before the
            # opening narration settles.  Keep the fake on that same boundary
            # so the consumed Start surface cannot masquerade as a successor.
            _ctx.client._state = {}
            _ctx.client._screen = None
            result.pop("pending", None)
            result.pop("buttons", None)
            data = result.get("_data")
            if isinstance(data, dict):
                data.pop("pending", None)
                data.pop("buttons", None)
            time.sleep(0.02)
            transaction = {
                "action_nonce": result["action_nonce"],
                "action_id": result["action_id"],
                "transaction_state": "settled",
                "resolved_as": "button",
                "pending": False,
            }
            result["wait"] = {
                "text": "The complete opening story.",
                "transaction": transaction,
                "_data": {"transaction": transaction},
            }
            result["text"] = "The complete opening story."
            result["transaction"] = transaction
            result["transaction_state"] = "settled"
            result["resolved_as"] = "button"

        monkeypatch.setattr(handlers, "_act_result_timeout", lambda _params: 0.01)
        monkeypatch.setattr(
            handlers, "_settle_after_successful_act", settle_at_deadline)
        ctx = HandlerContext(client=BoundaryClient())

        acted = handle_act(ctx, {"target": "Start", "wait": True})

        assert acted["transaction_state"] == "settled"
        assert acted.get("result_timeout_reached") is True, acted
        assert "admission_pending" not in acted["transaction"]
        assert "observation reached the result timeout" in acted["warning"]
        assert acted["text"] == "The complete opening story."
        assert "pending" not in acted

        successor = handle_wait(ctx, {"timeout": 1})
        assert "Signals" in successor["pending"]
        assert "complete opening story" not in (successor.get("text") or "")
        assert "pending" not in handle_wait(ctx, {"timeout": 1})

    def test_timeout_keeps_warning_for_unverified_or_consumed_decision(self):
        from vnflight.handlers import (
            HandlerContext,
            _mark_act_result_timeout,
            _returned_decision_is_actionable,
        )

        client = MockClient()
        client.last_request_id = "req-acted"
        client.last_actionable_snapshot = {"request_id": "req-acted"}
        ctx = HandlerContext(client=client)
        result = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Again",
            "buttons": "--- OTHER BUTTONS ---\n  - Journal",
            "transaction": {"transaction_state": "applied"},
            "_data": {
                "buttons": [{"label": "Journal"}],
                "_pending_raw": {"id": "req-acted", "choices": ["Again"]},
                "_actionable_snapshot": {"request_id": "req-acted"},
            },
        }

        actionable = _returned_decision_is_actionable(
            ctx, result, acted_request_id="req-acted")
        _mark_act_result_timeout(result, actionable_decision=actionable)

        assert actionable is False
        assert "still settling" in result["warning"]
        assert result["warning"].endswith("Call wait() to continue this transaction.")
        assert "pending" not in result
        assert "buttons" not in result
        assert "_pending_raw" not in result["_data"]
        assert "buttons" not in result["_data"]
        assert result["_stale_pending_suppressed"] is True

    def test_timeout_warning_uses_plain_wait_across_receipt_retirement(self):
        from vnflight.handlers import _mark_act_result_timeout

        result = {
            "action_nonce": "fleet-action-47",
            "transaction": {
                "action_nonce": "fleet-action-47",
                "transaction_state": "applied",
            },
        }

        _mark_act_result_timeout(result)

        assert "Call wait() to continue this transaction." in result["warning"]
        assert "action_nonce" not in result["warning"]

    def test_story_timeout_warning_describes_confirmation_and_clears(self):
        from vnflight.handlers import (
            _clear_settling_warning_for_actionable_decision,
            _mark_act_result_timeout,
        )

        result = {
            "text": "All currently available story is visible.",
            "transaction": {
                "action_nonce": "fleet-action-story",
                "transaction_state": "applied",
            },
        }

        _mark_act_result_timeout(result)

        assert result["warning"].startswith(
            "Available output is shown; transaction confirmation is still "
            "settling."
        )
        assert "Call wait() to confirm it" in result["warning"]
        assert "action_nonce" not in result["warning"]
        assert "may contain no additional story" in result["warning"]

        result["transaction"]["admission_open"] = True
        _clear_settling_warning_for_actionable_decision(result)
        assert "warning" not in result

    def test_verified_successor_clears_an_earlier_settling_warning(self):
        from vnflight.handlers import (
            _clear_settling_warning_for_actionable_decision,
        )

        result = {
            "warning": (
                "The action was accepted, but its result is still settling. "
                'Call wait(action_nonce="fleet-action-48") to continue this '
                "transaction."
            ),
            "_stale_pending_suppressed": True,
            "transaction": {"transaction_state": "applied"},
        }

        _clear_settling_warning_for_actionable_decision(result)

        assert 'wait(action_nonce="fleet-action-48")' in result["warning"]
        assert "_stale_pending_suppressed" not in result
        assert result["transaction"]["successor_actionable"] is True

    def test_actionable_successor_clears_warning_when_admission_is_open(self):
        from vnflight.handlers import (
            _clear_settling_warning_for_actionable_decision,
        )

        result = {
            "warning": (
                "The action was accepted, but its result is still settling. "
                'Call wait(action_nonce="fleet-action-49") to continue this '
                "transaction."
            ),
            "transaction": {
                "action_nonce": "fleet-action-49",
                "transaction_state": "applied",
                "admission_open": True,
            },
        }

        _clear_settling_warning_for_actionable_decision(result)

        assert "warning" not in result
        assert result["transaction"]["successor_actionable"] is True

    def test_slow_preflight_read_spends_only_the_shared_act_budget(self, ctx):
        import time
        from vnflight.handlers import handle_act

        state_calls = []

        def slow_state(*, timeout=3.0):
            state_calls.append(timeout)
            time.sleep(timeout)
            return {}

        ctx.client.state = slow_state
        started = time.monotonic()
        result = handle_act(ctx, {
            "target": "Continue", "wait": False, "result_timeout": 0.03,
        })
        elapsed = time.monotonic() - started

        assert result["reason"] == "result_timeout_before_submission"
        assert len(state_calls) == 1
        assert state_calls[0] <= 0.03
        assert not any(call[0] == "act" for call in ctx.client.calls)
        assert elapsed < 0.15

    def test_broadcast_presentation_does_not_spend_result_budget(self, ctx):
        """Fleet pacing before mutation is not transaction observation time."""
        import time
        from vnflight.handlers import handle_act

        ctx.client._state = _menu_state("Continue")
        ctx.hooks.before_action = lambda _name, _payload: time.sleep(0.04)

        result = handle_act(ctx, {
            "target": "1", "wait": False, "result_timeout": 0.03,
        })

        assert result.get("success") is True
        assert result.get("reason") != "result_timeout_before_submission"
        assert ctx.client.action_calls[-1] == ("act", 1)

    def test_transport_deadline_still_bounds_broadcast_presentation(self, ctx):
        """A slow bubble drain must return before the outer tools/call dies."""
        import time
        from vnflight.handlers import handle_act

        ctx.client._state = _menu_state("Continue")
        ctx.hooks.before_action = lambda _name, _payload: time.sleep(0.04)

        result = handle_act(ctx, {
            "target": "1",
            "wait": False,
            "result_timeout": 1,
            "_transport_deadline": time.time() + 0.02,
        })

        assert result["reason"] == "transport_timeout_before_submission"
        assert not [
            call for call in ctx.client.action_calls if call[0] == "act"
        ]

    def test_broadcast_presentation_reserves_action_acceptance_window(self, ctx):
        """A saturated bubble drain must leave time to submit the action."""
        import time
        from vnflight.handlers import handle_act

        ctx.client._state = _menu_state("Continue")
        observed = {}

        def consume_presentation_budget(_name, payload):
            observed.update(payload)
            time.sleep(max(
                0.0,
                payload["_transport_deadline"] - time.time() + 0.01,
            ))

        ctx.hooks.before_action = consume_presentation_budget
        outer_deadline = time.time() + 0.35
        result = handle_act(ctx, {
            "target": "1",
            "wait": False,
            "result_timeout": 1,
            "accept_timeout": 0.18,
            "_transport_deadline": outer_deadline,
        })

        assert observed["_transport_deadline"] <= outer_deadline - 0.17
        assert result.get("success") is True
        assert ctx.client.action_calls[-1] == ("act", 1)

    def test_presentation_reserves_only_effective_acceptance_window(self, ctx):
        """A short result budget must not discard unused transport headroom."""
        import time
        from vnflight.handlers import handle_act

        ctx.client._state = _menu_state("Continue")
        observed = {}
        ctx.hooks.before_action = (
            lambda _name, payload: observed.update(payload)
        )
        outer_deadline = time.time() + 1.0

        result = handle_act(ctx, {
            "target": "1",
            "wait": False,
            "result_timeout": 0.08,
            "accept_timeout": 15,
            "_transport_deadline": outer_deadline,
        })

        assert outer_deadline - 0.09 <= observed["_transport_deadline"]
        assert observed["_transport_deadline"] < outer_deadline
        assert result.get("success") is True

    def test_expired_settle_enrichment_does_no_io_and_flushes_booked_rows(
        self, ctx,
    ):
        import time
        from vnflight.handlers import (
            _append_renpy_exception_hint_on_failed_act,
            _mark_scene_unchanged_after_settle,
            _merge_passive_overlay_text,
            _refresh_missing_screen_text_from_state,
        )

        deadline = time.time() - 1
        ctx.overlay.pending_deliveries = [{"id": "row-1", "text": "late"}]
        ctx.client.calls.clear()
        result = {"pending": "1. Continue"}

        _refresh_missing_screen_text_from_state(
            ctx, result, {"format": "text", "_result_deadline": deadline})
        _merge_passive_overlay_text(
            ctx, result, fmt="text", deadline=deadline)
        _mark_scene_unchanged_after_settle(
            ctx, result, pre_act_seq=1, deadline=deadline)
        _append_renpy_exception_hint_on_failed_act(
            ctx, {"success": False, "error": "failed"}, deadline=deadline)

        assert ctx.client.calls == []
        assert ctx.overlay.pending_deliveries == []
        assert result["screen_text"] == "late"

    def test_act_settle_receives_only_budget_left_after_acceptance(self):
        """Acceptance and settlement share one result_timeout deadline."""
        import time as _time

        from vnflight.handlers import HandlerContext, handle_act

        class SlowAcceptanceClient(MockClient):
            def act_transaction(self, target, *, action_nonce=None,
                                accept_timeout=15.0, deadline=None,
                                invocation=None):
                self.calls.append(("accept_timeout", accept_timeout))
                _time.sleep(0.08)
                return super().act_transaction(
                    target,
                    action_nonce=action_nonce,
                    accept_timeout=accept_timeout,
                )

        client = SlowAcceptanceClient()
        client._state = _menu_state("Continue")
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {
            "target": "1", "wait": True, "result_timeout": 0.2,
        })

        accept_timeout = next(
            payload for name, payload in client.calls
            if name == "accept_timeout"
        )
        settle_timeout = next(
            payload["timeout"] for name, payload in client.calls
            if name == "wait"
        )
        assert 0 < accept_timeout <= 0.2
        assert 0 <= settle_timeout < 0.16
        assert result["transaction_state"] == "settled"

    def test_admission_probe_receives_only_the_remaining_budget(self):
        import time as _time

        from vnflight.handlers import HandlerContext, _await_act_admission

        class ReleasingClient(MockClient):
            def __init__(self):
                super().__init__()
                self.timeout = None

            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.timeout = timeout
                return {
                    "action_nonce": action_nonce,
                    "transaction_state": "applied",
                    "pending": True,
                    "admission_open": True,
                }

        client = ReleasingClient()
        result = {
            "ok": True, "action_nonce": "releasing",
            "transaction_state": "applied",
        }
        deadline = _time.time() + 0.5

        _await_act_admission(
            HandlerContext(client=client), result, deadline=deadline,
        )

        assert 0 < client.timeout <= 0.5

    def test_an_open_admission_gate_ends_the_wait_without_a_settle(self):
        """admission_open is the contract; ``settled`` is not required."""
        import time as _time

        from vnflight.handlers import HandlerContext, _await_act_admission

        class ReleasingClient(MockClient):
            def __init__(self):
                super().__init__()
                self.probes = 0

            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.probes += 1
                view = {
                    "action_nonce": action_nonce, "action_id": 8,
                    "transaction_state": "applied", "pending": True,
                }
                if self.probes > 1:
                    view["admission_open"] = True
                    view["released_by"] = "idle_ttl_live_shim"
                return view

        client = ReleasingClient()
        ctx = HandlerContext(client=client)
        result = {
            "ok": True, "action_nonce": "released",
            "transaction_state": "applied",
        }

        _await_act_admission(ctx, result, deadline=_time.time() + 30)

        assert client.probes == 2
        assert "admission_pending" not in result["transaction"]
        # Bookkeeping still stays off the act result itself.
        assert "admission_open" not in result

    def test_act_result_never_carries_the_admission_flags(self):
        """admission_open/released_by are transaction fields, not shim metadata.

        The apply probe copies every NON-bookkeeping key of the transaction
        into the act result as the shim's resolution metadata, so an admission
        signal that is not declared bookkeeping is promoted as if the game had
        reported it.
        """
        from vnflight.handlers import HandlerContext, _probe_act_resolution

        class ReleasedProbeClient(MockClient):
            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.calls.append(("action_transaction", action_nonce))
                return {
                    "action_nonce": action_nonce, "action_id": 7,
                    "transaction_state": "applied", "pending": True,
                    "admission_open": True, "released_by": "abandoned_shim",
                    "gate_released": True, "gate_released_by": "abandoned_shim",
                    "gate_released_at": 123.0,
                    "resolved_as": "choice", "label": "Go north",
                }

        ctx = HandlerContext(client=ReleasedProbeClient())
        result = _probe_act_resolution(ctx, {
            "ok": True, "success": True,
            "action_nonce": "released", "transaction_state": "accepted",
        })

        # The shim's resolution IS promoted...
        assert result["resolved_as"] == "choice"
        assert result["label"] == "Go north"
        # ...and the admission bookkeeping is not.
        for leaked in (
            "admission_open", "released_by",
            "gate_released", "gate_released_by", "gate_released_at",
        ):
            assert leaked not in result, leaked

    def test_transaction_resolution_is_promoted_before_hook_and_bookkeeping(self):
        from vnflight.handlers import HandlerContext, Hooks, handle_act

        class TransactionClient(MockClient):
            def act_transaction(self, target, **kwargs):
                self.calls.append(("act_transaction", target))
                return {
                    "ok": True, "success": True,
                    "action_nonce": "tx-button", "action_id": 4,
                    "transaction_state": "accepted", "pending": True,
                }

        client = TransactionClient()
        client._state = {
            "game_state": {
                "interactions": [{
                    "type": "button", "display_label": "Options",
                    "index": 1, "disabled": False,
                }],
            },
        }
        client._wait_result = MockWaitResult(transaction={
            "action_nonce": "tx-button", "action_id": 4,
            "transaction_state": "settled", "pending": False,
            "resolved_as": "button", "resolved_label": "Options",
        })
        hook_sources = []

        def after_act(source, target, result):
            hook_sources.append(source)
            return result

        ctx = HandlerContext(client=client, hooks=Hooks(after_act=after_act))
        result = handle_act(ctx, {"target": "Options", "wait": True})

        assert hook_sources == ["button"]
        assert result["transaction_state"] == "settled"
        assert result["resolved_as"] == "button"
        assert result["transaction_pending"] is False
        assert getattr(client, "_acted_request_id", None) is None

    def test_numeric_target(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = _menu_state("Go left", "Go right")
        ctx.client._act_result = {"ok": True, "chosen": 1}
        result = handle_act(ctx, {"target": "2"})
        assert ("act", 2) in ctx.client.action_calls

    def test_integer_target(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = _menu_state("Continue")
        result = handle_act(ctx, {"target": 1})
        assert ("act", 1) in ctx.client.action_calls

    def test_failed_act_reports_renpy_exception_anomaly(self, ctx):
        from vnflight.handlers import handle_act

        anomaly = {
            "type": "renpy_exception",
            "message": "Ren'Py exception: bad screen action",
        }
        ctx.client._state = {"anomaly": anomaly}
        ctx.client._act_result = {
            "ok": False,
            "error": "Timed out waiting for act result",
        }

        result = handle_act(ctx, {"target": "Start", "wait": False})

        assert "Timed out waiting for act result" in result["error"]
        assert "Ren'Py exception detected" in result["error"]
        assert result["_renpy_exception_anomaly"] == anomaly

    def test_label_target_goes_to_act(self, ctx):
        # Handler delegates label matching to the shim via act() —
        # no Python-side fuzzy matching anymore.
        from vnflight.handlers import handle_act
        ctx.client._pending = {
            "type": "choice_request",
            "choices": ["Go north", "Go south", "Stay here"],
        }
        result = handle_act(ctx, {"target": "Go south"})
        assert ("act", "Go south") in ctx.client.action_calls

    def test_label_target_strips_rendered_choice_annotation(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "pending_request": {
                "type": "choice_request",
                "choices": ["Ask politely"],
                "full_items": [
                    {
                        "label": "Ask politely",
                        "annotation": "friendly",
                        "is_disabled": False,
                    },
                ],
            },
            "game_state": {
                "interactions": [
                    {
                        "index": 1,
                        "type": "choice",
                        "display_label": "Ask politely",
                        "disabled": False,
                    },
                ],
            },
        }

        handle_act(ctx, {"target": "Ask politely  (friendly)", "wait": False})

        assert ("act", "Ask politely") in ctx.client.action_calls
        assert ("act", "Ask politely  (friendly)") not in ctx.client.action_calls

    def test_label_target_coalesces_pending_and_interaction_annotation(self, ctx):
        """Roadwarden reports one attitude row through two enriched sources."""
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "pending_request": {
                "type": "choice_request",
                "choices": [{"id": "friendly", "label": "Ask politely"}],
                "full_items": [{
                    "label": "Ask politely",
                    "annotation": "friendly",
                    "is_disabled": False,
                }],
            },
            "game_state": {
                "interactions": [{
                    "index": 1,
                    "type": "choice",
                    "display_label": "Ask politely",
                    "annotation": "friendly",
                    "id": "friendly",
                    "disabled": False,
                }],
            },
        }

        handle_act(ctx, {"target": "Ask politely  (friendly)", "wait": False})

        assert ("act", "Ask politely") in ctx.client.action_calls
        assert ("act", "Ask politely  (friendly)") not in ctx.client.action_calls

    @pytest.mark.parametrize("target", [
        "friendly: Ask politely",
        "friendly",
        "Ask poli",
        "friendly: Ask poli",
    ])
    def test_label_target_accepts_choice_annotation_aliases(self, ctx, target):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "pending_request": {
                "type": "choice_request",
                "choices": ["Ask politely", "Keep distance"],
                "full_items": [
                    {
                        "label": "Ask politely",
                        "annotation": "friendly",
                        "is_disabled": False,
                    },
                    {
                        "label": "Keep distance",
                        "annotation": "distanced",
                        "is_disabled": False,
                    },
                ],
            },
            "game_state": {
                "interactions": [
                    {
                        "index": 1,
                        "type": "choice",
                        "display_label": "Ask politely",
                        "disabled": False,
                    },
                    {
                        "index": 2,
                        "type": "choice",
                        "display_label": "Keep distance",
                        "disabled": False,
                    },
                ],
            },
        }

        handle_act(ctx, {"target": target, "wait": False})

        assert ("act", "Ask politely") in ctx.client.action_calls

    def test_label_target_accepts_live_interaction_annotation_aliases(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "pending_request": {
                "type": "choice_request",
                "choices": ["Ask politely", "Keep distance"],
            },
            "game_state": {
                "interactions": [
                    {
                        "index": 1,
                        "type": "choice",
                        "display_label": "Ask politely",
                        "annotation": "friendly",
                        "disabled": False,
                    },
                    {
                        "index": 2,
                        "type": "choice",
                        "display_label": "Keep distance",
                        "annotation": "distanced",
                        "disabled": False,
                    },
                ],
            },
        }

        handle_act(ctx, {"target": "friendly", "wait": False})

        assert ("act", "Ask politely") in ctx.client.action_calls
        assert ("act", "friendly") not in ctx.client.action_calls

    def test_numeric_target_prefers_rendered_overlay_button(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "stale-story",
                "full_items": [
                    {"label": "I approach Foggy.", "is_disabled": False},
                    {"label": "I go outside.", "is_disabled": False},
                ],
            },
            "screen": {
                "overlay_active": True,
                "buttons": [
                    {"label": "Food Rations", "screen": "inventory", "actions": ["Return"]},
                    {"label": "Wild Plants", "screen": "inventory", "actions": ["Return"]},
                    {"label": "Small Healing Potion", "screen": "inventory", "actions": ["Return"]},
                ],
            },
        }

        handle_act(ctx, {"target": "3", "wait": False})

        assert ("act", "Small Healing Potion") in ctx.client.action_calls
        assert ("act", 3) not in ctx.client.action_calls

    def test_numeric_target_out_of_range_choices_only_errors(self, ctx):
        # act(N) past the last choice with no buttons must tell the agent,
        # not fall through to a shim no-op the client reports as ok.
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "oor-choices",
                "full_items": [
                    {"label": "Go north", "is_disabled": False},
                    {"label": "Go south", "is_disabled": False},
                ],
            },
        }

        result = handle_act(ctx, {"target": "9", "wait": False})

        assert "out of range" in result.get("error", "")
        assert "2 option" in result["error"]
        assert ("act", 9) not in ctx.client.action_calls

    def test_numeric_target_out_of_range_with_buttons_errors(self, ctx):
        # Two choices + one numbered button → max visible 3. act(9) errors,
        # but the in-range button act(3) still resolves (no over-eager guard).
        from vnflight.handlers import handle_act
        base_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "oor-buttons",
                "full_items": [
                    {"label": "Take it.", "is_disabled": False},
                    {"label": "Forget it.", "is_disabled": False},
                ],
            },
            "screen": {
                "buttons": [
                    {"label": "Take it.", "screen": "", "actions": ["ChoiceReturn"]},
                    {"label": "Forget it.", "screen": "", "actions": ["ChoiceReturn"]},
                    {"label": "[Sell: Bronze Rod]", "screen": "selling",
                     "actions": ["Return"], "index": 3},
                ],
            },
        }
        ctx.client._state = dict(base_state)
        oor = handle_act(ctx, {"target": "9", "wait": False})
        assert "out of range" in oor.get("error", "")
        assert "3 option" in oor["error"]

        # In-range button still works.
        ctx.client.calls.clear()
        ctx.client._state = dict(base_state)
        handle_act(ctx, {"target": "3", "wait": False})
        assert ("act", "[Sell: Bronze Rod]") in ctx.client.action_calls

    def test_numeric_target_above_choice_count_prefers_rendered_button(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "sell-story",
                "full_items": [
                    {"label": "Take it.", "is_disabled": False},
                    {"label": "Forget it.", "is_disabled": False},
                ],
            },
            "screen": {
                "buttons": [
                    {"label": "Take it.", "screen": "", "actions": ["ChoiceReturn"]},
                    {"label": "Forget it.", "screen": "", "actions": ["ChoiceReturn"]},
                    {"label": "[Sell: Bronze Rod]", "screen": "selling", "actions": ["Return"], "index": 3},
                ],
            },
        }

        handle_act(ctx, {"target": "3", "wait": False})

        assert ("act", "[Sell: Bronze Rod]") in ctx.client.action_calls
        assert ("act", 3) not in ctx.client.action_calls

    def test_numeric_target_prefers_rendered_choice_over_nav_button(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "stale-count",
                "full_items": [
                    {"label": "[Finish your adventure.] Ready?", "is_disabled": False},
                    {"label": "(lie) I found Asterion.", "is_disabled": False},
                    {"label": "I take another look at the main hall.", "is_disabled": False},
                    {"label": "I go outside.", "is_disabled": False},
                ],
            },
            "game_state": {
                "interactions": [
                    {
                        "source": "button",
                        "type": "quick_menu",
                        "index": 5,
                        "display_label": "Character",
                        "action_names": ["ShowMenu"],
                    },
                ],
            },
        }

        def add_rendered_choice(data):
            pending = {
                "type": "choice",
                "id": "stale-count",
                "choices": [
                    {"label": "[Finish your adventure.] Ready?", "index": 1},
                    {"label": "(lie) I found Asterion.", "index": 2},
                    {"label": "I've been to your camp.", "index": 3},
                    {"label": "I take another look at the main hall.", "index": 4},
                    {"label": "I go outside.", "index": 5},
                ],
            }
            data["pending"] = pending
            data["buttons"] = [
                {"label": "Character", "screen": "quick_menu", "index": 5},
            ]
            return data

        ctx.hooks.after_state = add_rendered_choice

        handle_act(ctx, {"target": "5", "wait": False})

        assert ("act", "I go outside.") in ctx.client.action_calls
        assert ("act", "Character") not in ctx.client.action_calls
        assert ("act", 5) not in ctx.client.action_calls

    def test_numeric_target_uses_rendered_choice_id_when_index_is_compacted(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "compacted",
                "choices": ["Ask about herbs."],
                "full_items": [
                    {"label": "A contextual hint.", "is_caption": True, "is_disabled": False},
                    {"label": "Ask about herbs.", "is_disabled": False},
                ],
            },
            "game_state": {
                "interactions": [
                    {
                        "source": "info",
                        "type": "info",
                        "index": 1,
                        "display_label": "A contextual hint.",
                    },
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 2,
                        "id": "choice:ask-herbs",
                        "display_label": "Ask about herbs.",
                        "disabled": False,
                    },
                ],
            },
        }

        def add_rendered_choice(data):
            data["pending"] = {
                "type": "choice",
                "id": "compacted",
                "choices": [
                    {"label": "Ask about herbs.", "index": 1},
                ],
            }
            return data

        ctx.hooks.after_state = add_rendered_choice

        handle_act(ctx, {"target": "1", "wait": False})

        assert ("act", "choice:ask-herbs") in ctx.client.action_calls
        assert ("act", 1) not in ctx.client.action_calls
        assert ("act", "Ask about herbs.") not in ctx.client.action_calls

    def test_numeric_target_keeps_numeric_when_rendered_index_matches_shim(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "slay-style",
                "full_items": [
                    {"label": "Option one.", "is_disabled": False},
                    {"label": "Option two.", "is_disabled": False},
                ],
            },
            "game_state": {
                "interactions": [
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 1,
                        "id": "choice:one",
                        "display_label": "Option one.",
                        "disabled": False,
                    },
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 2,
                        "id": "choice:two",
                        "display_label": "Option two.",
                        "disabled": False,
                    },
                ],
            },
        }

        def add_rendered_choice(data):
            data["pending"] = {
                "type": "choice",
                "id": "slay-style",
                "choices": [
                    {"label": "Option one.", "index": 1},
                    {"label": "Option two.", "index": 2},
                ],
            }
            return data

        ctx.hooks.after_state = add_rendered_choice

        handle_act(ctx, {"target": "2", "wait": False})

        assert ("act", 2) in ctx.client.action_calls
        assert ("act", "choice:two") not in ctx.client.action_calls
        assert ("act", "Option two.") not in ctx.client.action_calls

    def test_numeric_target_resyncs_lost_active_choice_request(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "opening",
                "choices": [f"Option {i}" for i in range(1, 12)],
            },
            "game_state": {
                "interactions": [
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": i,
                        "display_label": f"Option {i}",
                        "disabled": False,
                    }
                    for i in range(1, 12)
                ],
            },
        }
        responses = [
            {
                "ok": False,
                "error": "No active choice request for choice resolution",
                "action_nonce": "failed-opening",
            },
            {"ok": True, "chosen": 10},
        ]
        ctx.client._active_action_nonces = ["failed-opening"]
        ctx.client._act_transactions["failed-opening"] = {
            "action_nonce": "failed-opening",
            "transaction_state": "failed",
        }
        ctx.client._state["event_counter"] = 41
        ctx.client._command_results["resync"] = {
            "type": "command_result",
            "command": "resync",
            "success": True,
        }
        seen_nonce = {}

        def send_command(name, args=None, nonce=None, **kwargs):
            ctx.client.calls.append(("_send_command", (name, args)))
            seen_nonce["value"] = (args or {}).get("nonce")
            ctx.client._command_results["resync"]["nonce"] = seen_nonce["value"]
            return True, f"Command '{name}' sent."

        ctx.client._send_command = send_command

        def act(target):
            ctx.client.calls.append(("act", target))
            return responses.pop(0)

        ctx.client.act = act

        result = handle_act(ctx, {
            "target": "10",
            "wait": False,
            "_mcp_server_instance_id": "server-a",
            "_mcp_call_id": "call-a",
            "_mcp_original_target": "10",
        })

        assert result["ok"] is True
        assert result["_resynced_before_act"] is True
        assert result["_recovered_from_action_nonce"] == "failed-opening"
        assert result["_original_error"].startswith("No active choice request")
        assert "failed-opening" not in ctx.client._active_action_nonces
        assert ctx.client.action_transaction("failed-opening") == {
            "action_nonce": "failed-opening",
            "transaction_state": "failed",
        }
        assert ("_retire_auto_action_nonce", {
            "action_nonce": "failed-opening",
            "reason": "resync_retry_replaced",
        }) in ctx.client.calls
        invocations = [
            call[1]["invocation"]
            for call in ctx.client.calls
            if call[0] == "act_transaction"
        ]
        assert invocations == [
            {
                "server_instance_id": "server-a",
                "call_id": "call-a",
                "original_target": "10",
                "attempt_kind": "initial",
            },
            {
                "server_instance_id": "server-a",
                "call_id": "call-a",
                "original_target": "10",
                "attempt_kind": "resync_retry",
            },
        ]
        assert ctx.client.action_calls.count(("act", 10)) == 2
        assert seen_nonce["value"]
        assert ("_wait_command_result", {
            "command": "resync",
            "timeout": 3.0,
            "after_seq": 41,
            "match": True,
        }) in ctx.client.calls

    def test_failed_resync_retry_keeps_original_nonce_auto_selectable(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        ctx.client._active_action_nonces = ["failed-opening"]
        monkeypatch.setattr(
            handlers,
            "_run_resync_for_act_retry",
            lambda *args, **kwargs: {"ok": True},
        )
        ctx.client.act_transaction = lambda *args, **kwargs: {
            "ok": False,
            "action_nonce": "retry-opening",
            "error": "retry failed",
        }

        result = handlers._retry_failed_act_after_resync(
            ctx,
            {
                "ok": False,
                "action_nonce": "failed-opening",
                "error": "No active choice request for choice resolution",
            },
            target=1,
            act_target=1,
            pre_rendered=None,
        )

        assert result["_resync_retry_failed"] is True
        assert ctx.client._active_action_nonces == ["failed-opening"]
        assert not any(
            call[0] == "_retire_auto_action_nonce"
            for call in ctx.client.calls
        )

    def test_same_nonce_resync_success_does_not_retire_live_retry(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        ctx.client._active_action_nonces = ["same-opening"]
        monkeypatch.setattr(
            handlers,
            "_run_resync_for_act_retry",
            lambda *args, **kwargs: {"ok": True},
        )
        ctx.client.act_transaction = lambda *args, **kwargs: {
            "ok": True,
            "action_nonce": "same-opening",
        }

        result = handlers._retry_failed_act_after_resync(
            ctx,
            {
                "ok": False,
                "action_nonce": "same-opening",
                "error": "No active choice request for choice resolution",
            },
            target=1,
            act_target=1,
            pre_rendered=None,
        )

        assert result["_resynced_before_act"] is True
        assert "_recovered_from_action_nonce" not in result
        assert ctx.client._active_action_nonces == ["same-opening"]
        assert not any(
            call[0] == "_retire_auto_action_nonce"
            for call in ctx.client.calls
        )

    def test_numeric_target_does_not_retry_when_resync_times_out(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "event_counter": 7,
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "opening",
                "choices": ["First", "Second"],
            },
            "game_state": {
                "interactions": [
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 1,
                        "display_label": "First",
                        "disabled": False,
                    },
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 2,
                        "display_label": "Second",
                        "disabled": False,
                    },
                ],
            },
        }

        def act(target):
            ctx.client.calls.append(("act", target))
            return {
                "ok": False,
                "error": "No active choice request for choice resolution",
            }

        ctx.client.act = act

        result = handle_act(ctx, {"target": "2", "wait": False})

        assert result["ok"] is False
        assert result["_resync_failed"] == "Timed out waiting for resync result"
        assert ctx.client.action_calls.count(("act", 2)) == 1
        assert ("_wait_command_result", {
            "command": "resync",
            "timeout": 3.0,
            "after_seq": 7,
            "match": True,
        }) in ctx.client.calls

    def test_numeric_target_does_not_retry_on_stale_resync_result(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "event_counter": 9,
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "opening",
                "choices": ["First", "Second"],
            },
            "game_state": {
                "interactions": [
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 1,
                        "display_label": "First",
                        "disabled": False,
                    },
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 2,
                        "display_label": "Second",
                        "disabled": False,
                    },
                ],
            },
        }
        ctx.client._command_results["resync"] = {
            "type": "command_result",
            "command": "resync",
            "success": True,
            "nonce": "old-resync",
        }

        def act(target):
            ctx.client.calls.append(("act", target))
            return {
                "ok": False,
                "error": "No active choice request for choice resolution",
            }

        ctx.client.act = act

        result = handle_act(ctx, {"target": "2", "wait": False})

        assert result["ok"] is False
        assert result["_resync_failed"] == "Timed out waiting for resync result"
        assert ctx.client.action_calls.count(("act", 2)) == 1


    def test_numeric_target_uses_visible_display_index_not_info_index(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "status": "idle",
            "game_state": {
                "screen_buttons": [
                    {
                        "label": "A contextual hint.",
                        "screen": "nvl",
                        "actions": ["NullAction"],
                        "_category": "info",
                    },
                    {
                        "label": "Ask about herbs.",
                        "screen": "nvl",
                        "actions": ["SetField", "Jump"],
                        "_category": "topics",
                    },
                ],
            },
        }

        handle_act(ctx, {"target": "1", "wait": False})

        assert ("act", "Ask about herbs.") in ctx.client.action_calls
        assert ("act", "A contextual hint.") not in ctx.client.action_calls

    def test_numeric_target_uses_topic_index_after_story_choices(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._pending = {
            "type": "choice_request",
            "id": "hybrid",
            "choices": ["Go north", "Go south"],
            "full_items": [
                {"label": "Go north", "is_disabled": False},
                {"label": "Go south", "is_disabled": False},
            ],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": ctx.client._pending,
            "game_state": {
                "interactions": [
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 1,
                        "display_label": "Go north",
                    },
                    {
                        "source": "choice",
                        "type": "choice",
                        "index": 2,
                        "display_label": "Go south",
                    },
                    {
                        "source": "button",
                        "type": "topic",
                        "category": "topics",
                        "index": 3,
                        "display_label": "Ask about herbs.",
                        "action_names": ["SetField", "Jump"],
                    },
                ],
            },
        }

        handle_act(ctx, {"target": "3", "wait": False})

        assert ("act", "Ask about herbs.") in ctx.client.action_calls
        assert ("act", 3) not in ctx.client.action_calls

    def test_fallback_to_act(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._pending = None
        result = handle_act(ctx, {"target": "Travel"})
        assert ("act", "Travel") in ctx.client.action_calls

    def test_no_match_falls_to_act(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._pending = {
            "type": "choice_request",
            "choices": ["Go north", "Go south"],
        }
        result = handle_act(ctx, {"target": "Travel"})
        assert ("act", "Travel") in ctx.client.action_calls

    def test_string_target_blocks_nullaction_interaction(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "game_state": {
                "interactions": [
                    {
                        "index": 3,
                        "display_label": "Buy Marshbules: ? (too expensive)",
                        "type": "shop",
                        "action_names": ["NullAction"],
                        "disabled": False,
                    },
                ],
            },
        }

        result = handle_act(ctx, {"target": "Buy Marshbules: ?", "wait": False})

        assert "disabled" in result["error"]
        assert ("act", "Buy Marshbules: ?") not in ctx.client.action_calls

    def test_numeric_target_blocks_nullaction_interaction(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = {
            "game_state": {
                "interactions": [
                    {
                        "index": 3,
                        "display_label": "Buy Marshbules: ? (too expensive)",
                        "type": "shop",
                        "action_names": ["NullAction"],
                        "disabled": False,
                    },
                ],
            },
        }

        result = handle_act(ctx, {"target": "3", "wait": False})

        assert "disabled" in result["error"]
        assert ("act", 3) not in ctx.client.action_calls

    def test_missing_target(self, ctx):
        from vnflight.handlers import handle_act
        result = handle_act(ctx, {})
        assert "error" in result

    def test_after_act_hook_invoked(self, ctx):
        from vnflight.handlers import handle_act
        # after_act receives source, target, result and may modify result.
        seen = {}
        def hook(source, target, result):
            seen["source"] = source
            seen["target"] = target
            return {**result, "hook_ran": True}
        ctx.hooks.after_act = hook
        result = handle_act(ctx, {"target": "Travel"})
        assert seen.get("target") == "Travel"
        assert result.get("hook_ran") is True

    def test_after_act_hook(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = _menu_state("Continue")
        ctx.hooks.after_act = lambda action_type, target, result: {**result, "hooked": True}
        result = handle_act(ctx, {"target": "1"})
        assert result.get("hooked") is True

    def test_before_action_hook_runs_before_act(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = _menu_state("Continue")
        calls = []

        def before_action(tool_name, payload):
            calls.append((tool_name, payload))
            calls.append(("client_calls_before", list(ctx.client.action_calls)))

        ctx.hooks.before_action = before_action

        handle_act(ctx, {"target": "1", "wait": False})

        assert calls[0][0] == "act"
        assert calls[0][1]["target"] == 1
        assert not any(call[0] == "act" for call in calls[1][1])
        assert ctx.client.action_calls[-1] == ("act", 1)

    def test_string_target_passes_through_to_act(self, ctx):
        # Quote normalization (smart vs straight) is now the shim's job.
        # Handler just passes the string through to client.act().
        from vnflight.handlers import handle_act
        result = handle_act(ctx, {"target": '"Hello," she said'})
        assert ("act", '"Hello," she said') in ctx.client.action_calls

    def test_act_wait_promotes_pending_from_wait_result(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._state = _menu_state("Onward")
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "A scene finishes."}],
            pending={
                "type": "choice_request",
                "id": "next-1",
                "choices": ["Open the door", "Wait"],
            },
        )

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert "A scene finishes." in result["text"]
        assert "CHOICE REQUIRED" in result["pending"]
        assert "Open the door" in result["pending"]
        assert result["wait"]["pending"] == result["pending"]

    def test_choice_act_marks_pre_state_pending_as_acted(self, ctx):
        from vnflight.handlers import handle_act

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "pre-choice",
                "choices": ["Continue"],
            },
        }
        ctx.client.last_request_id = None
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "A scene finishes."}],
            pending=None,
        )

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result["ok"] is True
        assert ctx.client._acted_request_id == "pre-choice"

    def test_choice_wait_drains_same_stale_pending_after_story(self, ctx):
        from vnflight.handlers import handle_act
        stale_pending = {
            "type": "choice_request",
            "id": "analysis-hub",
            "choices": ["Analyze signal", "Check logs"],
        }
        next_pending = {
            "type": "choice_request",
            "id": "marcus",
            "choices": ["Show him everything", "Hide the message"],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "The waveform unfolds."}],
                pending=stale_pending,
            ),
            MockWaitResult(
                events=[{"type": "narration", "text": "Marcus enters the lab."}],
                pending=next_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            if len(wait_results) == 2:
                ctx.client._state = {"status": "running"}
            else:
                ctx.client._state = {
                    "status": "waiting_for_input",
                    "pending_request": next_pending,
                }
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 5})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) >= 2
        assert "The waveform unfolds" in result["text"]
        assert "Marcus enters the lab" in result["text"]
        assert "Show him everything" in result["pending"]
        assert "Analyze signal" not in result["pending"]

    def test_choice_wait_drains_story_gap_until_followup_prompt(self, ctx):
        from vnflight.handlers import handle_act

        pre_pending = {
            "type": "choice_request",
            "id": "diplomats",
            "choices": ["Accept his terms", "Offer power", "Refuse outright"],
        }
        followup_pending = {
            "type": "choice_request",
            "id": "duel-choice",
            "choices": ["Accept his terms", "Refuse outright"],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": pre_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "He points at your father."}],
                pending=None,
            ),
            MockWaitResult(
                events=[
                    {
                        "type": "dialogue",
                        "who": "Togami",
                        "text": "And that is why we must duel.",
                    }
                ],
                pending=followup_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            if len(wait_results) == 2:
                ctx.client._state = {"status": "running"}
            else:
                ctx.client._state = {
                    "status": "waiting_for_input",
                    "pending_request": followup_pending,
                }
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(
            ctx, {"target": "Offer power", "wait": True, "timeout": 90}
        )

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 2
        assert "He points at your father" in result["text"]
        assert "And that is why we must duel" in result["text"]
        assert "Accept his terms" in result["pending"]
        assert "Refuse outright" in result["pending"]
        assert "Offer power" not in result["pending"]

    def test_button_routed_choice_wait_drains_story_gap_until_prompt(self, ctx):
        from vnflight.handlers import handle_act

        pre_pending = {
            "type": "choice_request",
            "id": "diplomats",
            "choices": ["Accept his terms", "Offer power", "Refuse outright"],
        }
        followup_pending = {
            "type": "choice_request",
            "id": "duel-choice",
            "choices": ["Accept his terms", "Refuse outright"],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": pre_pending,
        }
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "choice",
        }
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "He points at your father."}],
                pending=None,
            ),
            MockWaitResult(
                events=[
                    {
                        "type": "dialogue",
                        "who": "Togami",
                        "text": "And that is why we must duel.",
                    }
                ],
                pending=followup_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            if len(wait_results) == 2:
                ctx.client._state = {"status": "running"}
            else:
                ctx.client._state = {
                    "status": "waiting_for_input",
                    "pending_request": followup_pending,
                }
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(
            ctx, {"target": "Offer power", "wait": True, "timeout": 90}
        )

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 2
        assert "And that is why we must duel" in result["text"]
        assert "Accept his terms" in result["pending"]
        assert "Refuse outright" in result["pending"]
        assert "Offer power" not in result["pending"]

    def test_story_gap_ignores_non_actionable_screen_actions(self, ctx):
        from vnflight.handlers import handle_act

        pre_pending = {
            "type": "choice_request",
            "id": "diplomats",
            "choices": ["Accept his terms", "Offer power", "Refuse outright"],
        }
        followup_pending = {
            "type": "choice_request",
            "id": "duel-choice",
            "choices": ["Accept his terms", "Refuse outright"],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": pre_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "He points at your father."}],
                pending=None,
            ),
            MockWaitResult(events=[], pending=None),
            MockWaitResult(
                events=[
                    {
                        "type": "dialogue",
                        "who": "Togami",
                        "text": "And that is why we must duel.",
                    }
                ],
                pending=followup_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            if len(wait_results) == 3:
                ctx.client._state = {"status": "running"}
            elif len(wait_results) == 2:
                ctx.client._state = {
                    "status": "running",
                    "game_state": {
                        "screen_buttons": [
                            {"label": "", "screen": "quick_menu"},
                            {"label": "", "screen": "menu"},
                        ]
                    },
                }
            else:
                ctx.client._state = {
                    "status": "waiting_for_input",
                    "pending_request": followup_pending,
                }
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(
            ctx, {"target": "Offer power", "wait": True, "timeout": 90}
        )

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 3
        assert "And that is why we must duel" in result["text"]
        assert "Accept his terms" in result["pending"]
        assert "Refuse outright" in result["pending"]
        assert "Offer power" not in result["pending"]

    def test_choice_wait_keeps_story_but_suppresses_consumed_same_id_menu(self, ctx):
        from vnflight.handlers import handle_act
        hub_pending = {
            "type": "choice_request",
            "id": "analysis-hub",
            "choices": ["Analyze signal", "Check logs"],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": hub_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        # A real live menu refuses the explicit advance probe.
        ctx.client._command_results["advance"] = {
            "ok": False,
            "success": False,
            "error": "Cannot advance while a choice is active.",
        }
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "The waveform unfolds."}],
                pending=hub_pending,
            ),
            MockWaitResult(
                events=[],
                pending=hub_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            ctx.client._state = {
                "status": "waiting_for_input",
                "pending_request": hub_pending,
            }
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 5})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) >= 2
        assert "The waveform unfolds" in result["text"]
        assert "pending" not in result
        assert result["_stale_pending_suppressed"] is True

    def test_choice_story_under_stale_menu_advances_before_returning(self, ctx):
        """Do not hand a caller the dead menu covering a post-choice say."""
        from vnflight.handlers import handle_act

        stale_pending = {
            "type": "choice_request",
            "id": "marcus-room-visit",
            "choices": [
                "ARIA started searching your partition.",
                "How are you holding up?",
                "Leave him to it.",
            ],
        }
        room_pending = {
            "type": "choice_request",
            "id": "lab-room",
            "choices": ["Go to the audit console.", "Leave the lab."],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        wait_results = [
            MockWaitResult(
                events=[{
                    "type": "narration",
                    "text": "Telling him has a price.",
                }],
                pending=stale_pending,
            ),
            MockWaitResult(events=[], pending=stale_pending),
            MockWaitResult(
                events=[{
                    "type": "dialogue",
                    "character": "Dr. Voss",
                    "text": "ARIA started an unsanctioned scan.",
                }],
                pending=room_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            result = wait_results.pop(0)
            # The real client clears this transient marker after repeated
            # same-request /pending polls. Recovery must still use the stable
            # request id captured before the act.
            ctx.client._acted_request_id = None
            ctx.client._state = {
                "status": "waiting_for_input",
                "pending_request": result.pending,
            }
            return result

        ctx.client.wait = delayed_wait

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 5})

        assert ("command", ("advance", {})) in ctx.client.calls
        assert "Telling him has a price" in result["text"]
        assert "ARIA started an unsanctioned scan" in result["text"]
        assert "Go to the audit console" in result["pending"]
        assert "How are you holding up" not in result["pending"]

    def test_unsettled_post_choice_menu_is_suppressed_after_advance_cap(
        self, ctx,
    ):
        from vnflight.handlers import handle_act

        stale_pending = {
            "type": "choice_request",
            "id": "old-menu",
            "choices": ["Tell him."],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        waits = [
            MockWaitResult(
                events=[{"type": "narration", "text": "She begins."}],
                pending=stale_pending,
            ),
            MockWaitResult(events=[], pending=stale_pending),
        ] + [
            MockWaitResult(
                events=[{
                    "type": "dialogue",
                    "character": "Dr. Voss",
                    "text": "Still speaking {}.".format(index),
                }],
                pending=stale_pending,
            )
            for index in range(6)
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return waits.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 5})

        assert "pending" not in result
        assert result["_stale_pending_suppressed"] is True
        assert "Call wait()" in result["warning"]
        assert "She begins" in result["text"]
        assert "Still speaking 5" in result["text"]
        assert ctx.client.last_request_id == (
            "__resolved_choice_waiting_for_successor__"
        )

    def test_background_terminal_output_cannot_resurrect_consumed_menu(
        self, ctx,
    ):
        """A passive update may accompany the cached shell, but not revive it."""
        from vnflight.handlers import handle_act

        stale_pending = {
            "type": "choice_request",
            "id": "terminal-question",
            "choices": ["Ask how the signal works."],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        ctx.client._command_results["advance"] = {
            "ok": False, "error": "nothing to advance",
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{
                "type": "screen_content",
                "overlay_texts": ["ECHO-7> The carrier is phase-shifted."],
                "overlay_screens": ["echo_terminal_live"],
                "passive_overlay_snapshot": True,
                "passive_overlay_delta": [
                    "ECHO-7> The carrier is phase-shifted."
                ],
                "_source_id": "shim-a",
                "_source_seq": 41,
                "_seq": 90,
            }],
            pending=stale_pending,
        )

        result = handle_act(ctx, {
            "target": "1", "wait": True, "timeout": 0.2,
        })

        assert "phase-shifted" in result.get("screen_text", "")
        assert "pending" not in result
        assert result["_stale_pending_suppressed"] is True
        assert ctx.client.last_request_id == (
            "__resolved_choice_waiting_for_successor__"
        )

    def test_choice_wait_uses_caller_timeout_for_stale_same_pending_settle(
        self, ctx, monkeypatch
    ):
        from vnflight import handlers

        stale_pending = {
            "type": "choice_request",
            "id": "opening",
            "choices": ["Go to the cabin"],
        }
        fresh_rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Take the blade",
            "_data": {
                "pending": {
                    "type": "choice",
                    "id": "cabin",
                    "choices": [{"label": "Take the blade", "index": 1}],
                },
                "_pending_raw": {"id": "cabin"},
            },
        }
        timeouts = []

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        ctx.client._wait_result = MockWaitResult(events=[], pending=stale_pending)

        def fake_wait_for_rendered_state_change(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return fresh_rendered

        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            fake_wait_for_rendered_state_change,
        )

        result = handlers.handle_act(
            ctx,
            {"target": "1", "wait": True, "timeout": 120},
        )

        assert timeouts == [30.0]
        assert "Take the blade" in result["pending"]
        assert "Go to the cabin" not in result["pending"]

    def test_choice_wait_keeps_small_timeout_for_stale_same_pending_settle(
        self, ctx, monkeypatch
    ):
        from vnflight import handlers

        stale_pending = {
            "type": "choice_request",
            "id": "opening",
            "choices": ["Go to the cabin"],
        }
        fresh_rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Take the blade",
            "_data": {
                "pending": {
                    "type": "choice",
                    "id": "cabin",
                    "choices": [{"label": "Take the blade", "index": 1}],
                },
                "_pending_raw": {"id": "cabin"},
            },
        }
        timeouts = []

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        ctx.client._wait_result = MockWaitResult(events=[], pending=stale_pending)

        def fake_wait_for_rendered_state_change(*args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return fresh_rendered

        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            fake_wait_for_rendered_state_change,
        )

        handlers.handle_act(ctx, {"target": "1", "wait": True, "timeout": 1})

        assert len(timeouts) == 1
        assert 0.0 <= timeouts[0] <= 1.0

    def test_choice_wait_uses_caller_timeout_for_stale_same_pending_followup(
        self, ctx
    ):
        from vnflight.handlers import handle_act

        stale_pending = {
            "type": "choice_request",
            "id": "opening",
            "choices": ["Go to the cabin"],
        }
        fresh_pending = {
            "type": "choice_request",
            "id": "cabin",
            "choices": ["Take the blade", "Enter the basement"],
        }
        wait_results = [
            MockWaitResult(events=[], pending=stale_pending),
            MockWaitResult(
                events=[{"type": "narration", "text": "The cabin is bare."}],
                pending=fresh_pending,
            ),
        ]

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 120})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert wait_calls[1][1]["timeout"] == 30.0
        assert "The cabin is bare" in result["text"]
        assert "Take the blade" in result["pending"]
        assert "Go to the cabin" not in result["pending"]

    def test_deferred_act_marks_consumed_menu_not_rendered_successor(self, ctx):
        """A successor returned by act(wait=True) is immediately actionable.

        Echoes live receipt, 2026-08-22: menu A resolved and the act result
        rendered menu B. Deferred-resolution bookkeeping then marked B as the
        acted request, so the next numeric act rejected the unchanged menu as
        stale. Only A was consumed.
        """
        from vnflight.handlers import handle_act

        old_pending = {
            "type": "choice_request",
            "id": "menu-a",
            "choices": ["Tell him."],
        }
        fresh_pending = {
            "type": "choice_request",
            "id": "menu-b",
            "choices": ["Tell him everything.", "Keep one detail back."],
        }
        ctx.client.last_request_id = "menu-a"
        ctx.client.last_choices = ["Tell him."]
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": old_pending,
        }
        ctx.client._act_result = {"ok": True}

        def successor_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            ctx.client.last_request_id = "menu-b"
            ctx.client.last_choices = list(fresh_pending["choices"])
            ctx.client._state = {
                "status": "waiting_for_input",
                "pending_request": fresh_pending,
            }
            return MockWaitResult(
                events=[{"type": "narration", "text": "What next?"}],
                pending=fresh_pending,
            )

        ctx.client.wait = successor_wait

        first = handle_act(ctx, {"target": "1", "wait": True})

        assert "Tell him everything" in first["pending"]
        assert ctx.client.last_request_id == "menu-b"
        assert ctx.client._acted_request_id == "menu-a"

        second = handle_act(ctx, {"target": "1", "wait": False})
        assert "state changed since you last looked" not in second.get(
            "error", ""
        )
        assert second.get("success", second.get("ok")) is True

    def test_act_binds_successor_menu_even_when_wait_does_not_update_client(
        self, ctx
    ):
        """The returned menu, not a stale client marker, owns the next number."""
        from vnflight.handlers import handle_act

        old_pending = {
            "type": "choice_request",
            "id": "menu-a",
            "choices": ["Tell him."],
        }
        fresh_pending = {
            "type": "choice_request",
            "id": "menu-b",
            "choices": ["Tell him everything.", "Keep one detail back."],
        }
        ctx.client.last_request_id = "menu-a"
        ctx.client.last_choices = list(old_pending["choices"])
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": old_pending,
        }
        ctx.client._act_result = {"ok": True}

        def successor_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            ctx.client._state = {
                "status": "waiting_for_input",
                "pending_request": fresh_pending,
            }
            return MockWaitResult(
                events=[{"type": "narration", "text": "What next?"}],
                pending=fresh_pending,
            )

        ctx.client.wait = successor_wait

        first = handle_act(ctx, {"target": "1", "wait": True})

        assert "Tell him everything" in first["pending"]
        assert ctx.client.last_request_id == "menu-b"
        assert ctx.client.last_choices == fresh_pending["choices"]

        second = handle_act(ctx, {"target": "1", "wait": False})
        assert "state changed since you last looked" not in second.get(
            "error", ""
        )
        assert second.get("success", second.get("ok")) is True

    def test_returned_successor_binding_follows_nested_wait_metadata(
        self, ctx
    ):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending, handle_act

        ctx.client.last_request_id = "menu-a"
        returned_state = {
            "pending_request": {
                "type": "choice_request",
                "id": "menu-b",
                "choices": ["Continue"],
            },
            "game_state": {
                "interactions": [{
                    "source": "choice",
                    "type": "choice",
                    "display_label": "Continue",
                    "action_strs": ["Return('continue')"],
                }],
            },
        }
        rendered = {
            "pending": "--- CHOICE REQUIRED ---\n1: Continue",
            "wait": {
                "_data": {
                    "_pending_raw": returned_state["pending_request"],
                    "_actionable_snapshot": actionable_state_snapshot(
                        returned_state
                    ),
                },
            },
        }

        _bind_returned_pending(ctx, rendered)

        assert ctx.client.last_request_id == "menu-b"
        assert ctx.client.last_choices == ["Continue"]
        assert ctx.client.last_actionable_snapshot == actionable_state_snapshot(
            returned_state
        )

        # Under load Ren'Py can immediately register the same menu again with
        # a new request id. The numeric reply still targets the exact menu the
        # previous act returned, so it must pass the actionable-equivalence
        # gate rather than compare against the consumed predecessor menu.
        ctx.client._state = {
            **returned_state,
            "pending_request": {
                **returned_state["pending_request"],
                "id": "menu-b-reregistered",
            },
        }
        ctx.client._act_result = {"ok": True, "chosen": 1}

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True
        assert not result.get("_stale_numeric_act")

    def test_nested_same_request_keeps_outer_actionable_snapshot(self, ctx):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        state = {
            "pending_request": {"id": "menu-a", "choices": ["Continue"]},
            "game_state": {"interactions": [{
                "type": "choice", "display_label": "Continue",
                "action_strs": ["Return('continue')"],
            }]},
        }
        snapshot = actionable_state_snapshot(state)
        repeated = state["pending_request"]

        _bind_returned_pending(ctx, {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue",
            "_pending_raw": repeated,
            "_actionable_snapshot": snapshot,
            "wait": {"_pending_raw": repeated},
        })

        assert ctx.client.last_request_id == "menu-a"
        assert ctx.client.last_actionable_snapshot == snapshot

    def test_successful_choice_never_returns_its_consumed_menu_actionable(
        self, ctx
    ):
        """Fleet receipt: delayed dialogue arrived after a stale menu echo."""
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        consumed = {
            "type": "choice_request",
            "id": "marcus-menu",
            "choices": [
                "ARIA started searching your partition.",
                "Leave him to it.",
            ],
        }
        state = {
            "pending_request": consumed,
            "game_state": {"interactions": [
                {
                    "type": "choice",
                    "display_label": label,
                    "action_strs": [f"Return({index!r})"],
                }
                for index, label in enumerate(consumed["choices"], 1)
            ]},
        }
        rendered = {
            "text": "Telling him has a price.",
            "pending": (
                "--- CHOICE REQUIRED ---\n"
                "1: ARIA started searching your partition.\n"
                "2: Leave him to it."
            ),
            "_pending_raw": consumed,
            "_actionable_snapshot": actionable_state_snapshot(state),
        }

        _bind_returned_pending(
            ctx, rendered, acted_request_id="marcus-menu")

        assert rendered["text"] == "Telling him has a price."
        assert "pending" not in rendered
        assert "_pending_raw" not in rendered
        assert rendered["_stale_pending_suppressed"] is True
        assert "Call wait()" in rendered["warning"]
        assert ctx.client.last_choices is None

    def test_nested_different_request_without_snapshot_clears_provenance(self, ctx):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        outer_state = {
            "pending_request": {"id": "menu-a", "choices": ["Continue"]},
            "game_state": {"interactions": [{
                "type": "choice", "display_label": "Continue",
                "action_strs": ["Return('first')"],
            }]},
        }

        _bind_returned_pending(ctx, {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue",
            "_pending_raw": outer_state["pending_request"],
            "_actionable_snapshot": actionable_state_snapshot(outer_state),
            "wait": {
                "_pending_raw": {
                    "id": "menu-b", "choices": ["Continue"],
                },
            },
        })

        assert ctx.client.last_request_id == "menu-b"
        assert ctx.client.last_actionable_snapshot is None

    def test_returned_successor_binding_ignores_stale_outer_pending(
        self, ctx
    ):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        old_state = {
            "pending_request": {
                "id": "old-menu",
                "choices": ["Tell him.", "Say nothing."],
            },
            "game_state": {},
        }
        successor_state = {
            "pending_request": {
                "id": "successor-menu",
                "choices": [
                    "With a message addressed to me. By name.",
                    "Yes, exactly. Who would send us an encoded message?",
                ],
            },
            "game_state": {},
        }
        rendered = {
            "_data": {
                "pending": old_state["pending_request"],
                "_pending_raw": old_state["pending_request"],
                "_actionable_snapshot": actionable_state_snapshot(old_state),
            },
            "wait": {
                "_data": {
                    "pending": successor_state["pending_request"],
                    "_pending_raw": successor_state["pending_request"],
                    "_actionable_snapshot": actionable_state_snapshot(
                        successor_state),
                },
            },
        }

        _bind_returned_pending(ctx, rendered)

        assert ctx.client.last_request_id == "successor-menu"
        assert ctx.client.last_choices == successor_state["pending_request"][
            "choices"
        ]
        assert ctx.client.last_actionable_snapshot == actionable_state_snapshot(
            successor_state
        )

    def test_returned_successor_binding_reads_formatted_wait_metadata(
        self, ctx
    ):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        successor_state = {
            "pending_request": {
                "id": "successor-menu",
                "choices": ["Continue", "Wait"],
            },
            "game_state": {},
        }
        snapshot = actionable_state_snapshot(successor_state)
        rendered = {
            "pending": "--- CHOICE REQUIRED ---\n1: Continue\n2: Wait",
            "_pending_raw": successor_state["pending_request"],
            "_actionable_snapshot": snapshot,
        }

        _bind_returned_pending(ctx, rendered)

        assert ctx.client.last_request_id == "successor-menu"
        assert ctx.client.last_choices == ["Continue", "Wait"]
        assert ctx.client.last_actionable_snapshot == snapshot

    def test_wait_promotion_keeps_successor_numeric_binding_metadata(
        self, ctx
    ):
        from vnflight.handlers import (
            _bind_returned_pending, _promote_wait_output_preserving_story)

        successor = {
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
        }
        snapshot = {"request_id": "successor-menu", "choices": [
            "Continue", "Wait",
        ]}
        result = {
            "text": "Earlier story.",
            "_pending_raw": {
                "id": "consumed-menu", "choices": ["Tell him."],
            },
        }
        wait_result = {
            "text": "Successor story.",
            "pending": "--- CHOICE REQUIRED ---\n1: Continue\n2: Wait",
            "_pending_raw": successor,
            "_actionable_snapshot": snapshot,
        }

        _promote_wait_output_preserving_story(result, wait_result)
        _bind_returned_pending(ctx, result)

        assert result["_pending_raw"] == successor
        assert result["_actionable_snapshot"] == snapshot
        assert ctx.client.last_request_id == "successor-menu"
        assert ctx.client.last_choices == ["Continue", "Wait"]
        assert ctx.client.last_actionable_snapshot == snapshot

    def test_visible_successor_beats_older_nested_wait_at_expired_deadline(
        self, ctx
    ):
        """Fleet receipt: promoted roots can be newer than nested waits."""
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        consumed_state = {
            "pending_request": {
                "id": "consumed-menu",
                "choices": ["Tell him.", "Say nothing."],
            },
            "game_state": {},
        }
        successor_state = {
            "pending_request": {
                "id": "successor-menu",
                "choices": ["Continue", "Wait"],
            },
            "game_state": {},
        }
        rendered = {
            "pending": "--- CHOICE REQUIRED ---\n1: Continue\n2: Wait",
            "_pending_raw": successor_state["pending_request"],
            "_actionable_snapshot": actionable_state_snapshot(
                successor_state),
            "wait": {
                "_pending_raw": consumed_state["pending_request"],
                "_actionable_snapshot": actionable_state_snapshot(
                    consumed_state),
            },
        }

        # No repair read is available after the act-level budget expires. The
        # returned response must therefore be self-consistent on its own.
        _bind_returned_pending(ctx, rendered, deadline=0.0)

        assert ctx.client.last_request_id == "successor-menu"
        assert ctx.client.last_choices == ["Continue", "Wait"]
        assert ctx.client.last_actionable_snapshot == (
            actionable_state_snapshot(successor_state)
        )

    def test_returned_visible_successor_repairs_stale_private_metadata(
        self, ctx
    ):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        successor_state = {
            # Match the public BridgeClient.state() envelope used in live
            # play rather than the bridge's internal pending_request name.
            "pending": {
                "id": "successor-menu",
                "choices": ["Continue", "Wait"],
            },
            "game_state": {},
        }
        ctx.client._state = successor_state
        ctx.client._pending = successor_state["pending"]
        rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue\n  2: Wait",
            "_pending_raw": {
                "id": "consumed-menu",
                "choices": ["Tell him.", "Say nothing."],
            },
            "_actionable_snapshot": {
                "request_id": "consumed-menu",
            },
        }

        _bind_returned_pending(ctx, rendered)

        expected_snapshot = actionable_state_snapshot({
            "pending_request": successor_state["pending"],
            "game_state": {},
        })
        assert ctx.client.last_request_id == "successor-menu"
        assert ctx.client.last_choices == ["Continue", "Wait"]
        assert ctx.client.last_actionable_snapshot == expected_snapshot
        assert rendered["_pending_raw"] == successor_state["pending"]
        assert rendered["_actionable_snapshot"] == expected_snapshot

    def test_handle_act_binds_successor_before_admission_wait(
        self, ctx, monkeypatch
    ):
        """Admission polling must not consume the response-repair budget."""
        import vnflight.handlers as handlers
        from vnflight.client import actionable_state_snapshot

        consumed = {
            "id": "consumed-menu",
            "choices": ["Tell him.", "Say nothing."],
        }
        successor = {
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": consumed,
        }
        ctx.client._act_result = {
            "ok": True,
            "success": True,
            "resolved_as": "choice",
        }
        consumed_snapshot = actionable_state_snapshot({
            "pending_request": consumed,
            "game_state": {},
        })

        def fake_settle(_ctx, result, _params, _settle_context):
            ctx.client._state = {
                "status": "waiting_for_input",
                "pending_request": successor,
            }
            result.update({
                "pending": (
                    "--- CHOICE REQUIRED ---\n"
                    "  1: Continue\n"
                    "  2: Wait"
                ),
                "_pending_raw": consumed,
                "_actionable_snapshot": consumed_snapshot,
            })

        admission_observed = []

        def fake_admission(_ctx, _result, *, deadline):
            admission_observed.append((
                ctx.client.last_request_id,
                list(ctx.client.last_choices),
                deadline,
            ))

        monkeypatch.setattr(
            handlers, "_settle_after_successful_act", fake_settle)
        monkeypatch.setattr(
            handlers, "_await_act_admission", fake_admission)

        result = handlers.handle_act(
            ctx, {"target": "Tell him.", "wait": True})

        assert admission_observed[0][0] == "successor-menu"
        assert admission_observed[0][1] == ["Continue", "Wait"]
        assert result["_pending_raw"] == successor

    def test_returned_visible_successor_without_snapshot_keeps_suppression_binding(
        self, ctx
    ):
        from vnflight.handlers import _bind_returned_pending

        successor = {
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
        }
        ctx.client._state = {"pending_request": successor}
        ctx.client._pending = successor
        rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue\n  2: Wait",
            "_pending_raw": {
                "id": "consumed-menu",
                "choices": ["Tell him.", "Say nothing."],
            },
            "_stale_pending_suppressed": True,
        }

        _bind_returned_pending(ctx, rendered)

        assert ctx.client.last_request_id == "consumed-menu"
        assert ctx.client.last_choices == ["Tell him.", "Say nothing."]

    def test_cached_labels_alone_do_not_repair_a_stale_id(
        self, ctx
    ):
        from vnflight.handlers import _bind_returned_pending

        successor = {
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
        }
        ctx.client._pending = successor
        rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue\n  2: Wait",
            # Polling can refresh the labels while the acted-request guard
            # deliberately retains the consumed request id.
            "_pending_raw": {
                "id": "consumed-menu",
                "choices": ["Continue", "Wait"],
            },
        }

        _bind_returned_pending(ctx, rendered)

        assert ctx.client.last_request_id == "consumed-menu"
        assert ctx.client.last_choices == ["Continue", "Wait"]

    def test_returned_visible_successor_without_private_metadata_fails_closed(
        self, ctx
    ):
        from vnflight.handlers import _bind_returned_pending

        successor = {
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
        }
        ctx.client.last_request_id = "consumed-menu"
        ctx.client.last_choices = ["Tell him.", "Say nothing."]
        ctx.client._pending = successor

        _bind_returned_pending(ctx, {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue\n  2: Wait",
        })

        assert ctx.client.last_request_id == "consumed-menu"
        assert ctx.client.last_choices == ["Tell him.", "Say nothing."]

    def test_same_label_live_menu_with_different_action_is_not_bound(self, ctx):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        shown_state = {
            "pending_request": {"id": "menu-a", "choices": ["Continue"]},
            "game_state": {"interactions": [{
                "type": "choice", "display_label": "Continue",
                "action_strs": ["Return('first')"],
            }]},
        }
        unseen_state = {
            "pending_request": {"id": "menu-b", "choices": ["Continue"]},
            "game_state": {"interactions": [{
                "type": "choice", "display_label": "Continue",
                "action_strs": ["Return('second')"],
            }]},
        }
        ctx.client._state = unseen_state

        _bind_returned_pending(ctx, {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue",
            "_pending_raw": shown_state["pending_request"],
            "_actionable_snapshot": actionable_state_snapshot(shown_state),
        })

        assert ctx.client.last_request_id == "menu-a"
        assert ctx.client.last_actionable_snapshot == actionable_state_snapshot(
            shown_state
        )

    def test_missing_returned_provenance_does_not_reuse_stale_cache(self, ctx):
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import _bind_returned_pending

        unseen_state = {
            "pending_request": {"id": "menu-b", "choices": ["Continue"]},
            "game_state": {"interactions": [{
                "type": "choice", "display_label": "Continue",
                "action_strs": ["Return('unseen')"],
            }]},
        }
        ctx.client.last_actionable_snapshot = actionable_state_snapshot(
            unseen_state
        )
        ctx.client._state = unseen_state

        _bind_returned_pending(ctx, {
            "pending": "--- CHOICE REQUIRED ---\n  1: Continue",
            "_pending_raw": {"id": "menu-a", "choices": ["Continue"]},
        })

        assert ctx.client.last_request_id == "menu-a"
        assert ctx.client.last_actionable_snapshot is None

    def test_choice_wait_keeps_small_timeout_for_stale_same_pending_followup(
        self, ctx
    ):
        from vnflight.handlers import handle_act

        stale_pending = {
            "type": "choice_request",
            "id": "opening",
            "choices": ["Go to the cabin"],
        }
        fresh_pending = {
            "type": "choice_request",
            "id": "cabin",
            "choices": ["Take the blade"],
        }
        wait_results = [
            MockWaitResult(events=[], pending=stale_pending),
            MockWaitResult(
                events=[{"type": "narration", "text": "The cabin is bare."}],
                pending=fresh_pending,
            ),
        ]

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return wait_results.pop(0)

        ctx.client.wait = delayed_wait

        handle_act(ctx, {"target": "1", "wait": True, "timeout": 1})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert 0.0 <= wait_calls[1][1]["timeout"] <= 1.0

    def test_choice_wait_drains_repeated_no_story_same_pending(
        self, ctx, monkeypatch
    ):
        from vnflight import handlers

        stale_pending = {
            "type": "choice_request",
            "id": "opening",
            "choices": ["Go to the cabin"],
        }
        fresh_pending = {
            "type": "choice_request",
            "id": "cabin",
            "choices": ["Take the blade"],
        }
        stale_rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Go to the cabin",
            "_data": {
                "pending": {
                    "type": "choice",
                    "id": "opening",
                    "choices": [{"label": "Go to the cabin", "index": 1}],
                },
                "_pending_raw": stale_pending,
            },
        }
        fresh_rendered = {
            "text": "[Narrator] The cabin appears.",
            "story": ["[Narrator] The cabin appears."],
            "pending": "--- CHOICE REQUIRED ---\n  1: Take the blade",
            "_data": {
                "pending": {
                    "type": "choice",
                    "id": "cabin",
                    "choices": [{"label": "Take the blade", "index": 1}],
                },
                "_pending_raw": fresh_pending,
            },
        }
        wait_results = [stale_rendered, stale_rendered, fresh_rendered]
        drain_calls = []

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}

        def fake_handle_wait(wait_ctx, params):
            ctx.client.calls.append(("wait", params))
            return wait_results.pop(0)

        def fake_drain(client, pending, **kwargs):
            drain_calls.append((pending, kwargs))
            return fresh_pending

        monkeypatch.setattr(handlers, "handle_wait", fake_handle_wait)
        monkeypatch.setattr(handlers, "drain_stale_pending_request", fake_drain)

        result = handlers.handle_act(
            ctx,
            {"target": "1", "wait": True, "timeout": 120},
        )

        assert drain_calls
        assert drain_calls[0][1]["timeout"] == 12.0
        assert "The cabin appears" in result["text"]
        assert "Take the blade" in result["pending"]
        assert "Go to the cabin" not in result["pending"]

    def test_choice_wait_never_replays_settled_act_on_stale_same_pending(
        self, ctx, monkeypatch
    ):
        from vnflight import handlers

        stale_pending = {
            "type": "choice_request",
            "id": "opening",
            "choices": ["Go to the cabin"],
        }
        stale_rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Go to the cabin",
            "_data": {
                "pending": {
                    "type": "choice",
                    "id": "opening",
                    "choices": [{"label": "Go to the cabin", "index": 1}],
                },
                "_pending_raw": stale_pending,
            },
        }
        wait_results = [stale_rendered, stale_rendered]

        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}

        def fake_handle_wait(wait_ctx, params):
            ctx.client.calls.append(("wait", params))
            return wait_results.pop(0)

        def fake_drain(client, pending, **kwargs):
            return pending

        monkeypatch.setattr(handlers, "handle_wait", fake_handle_wait)
        monkeypatch.setattr(handlers, "drain_stale_pending_request", fake_drain)

        result = handlers.handle_act(
            ctx,
            {"target": "1", "wait": True, "timeout": 120},
        )

        acted = [target for name, target in ctx.client.action_calls if name == "act"]
        assert acted == ["Go to the cabin"]
        assert result.get("_stale_pending_suppressed") is True
        assert "pending" not in result
        assert "Call wait()" in result.get("warning", "")

    def test_choice_wait_ignores_repeated_pre_action_screen(self, ctx, monkeypatch):
        from vnflight import handlers

        pre_rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Old choice",
            "_data": {
                "pending": {
                    "type": "choice",
                    "choices": [{"label": "Old choice", "index": 1}],
                },
            },
        }
        stale_rendered = {
            "text": "",
            "buttons": "--- NAVIGATION ---\n  Old choice\nUse: act \"<label>\"",
            "_data": {
                "buttons": [{"label": "Old choice", "screen": "say"}],
            },
        }
        fresh_rendered = {
            "text": "[Narrator] The path opens.",
            "story": ["[Narrator] The path opens."],
        }
        wait_results = [stale_rendered, fresh_rendered]
        calls = []

        def fake_handle_wait(wait_ctx, params):
            calls.append(params)
            return wait_results.pop(0)

        monkeypatch.setattr(handlers, "handle_wait", fake_handle_wait)
        result = {"ok": True, "resolved_as": "choice"}

        handlers._settle_wait_after_action(
            ctx,
            result,
            {"timeout": 10},
            button_context=False,
            pre_state_sig=("pre",),
            pre_rendered=pre_rendered,
            pre_visible_sig=handlers._visible_output_signature(pre_rendered),
            pre_pending_id="old",
            pre_was_button_only=False,
        )

        assert len(calls) == 2
        assert calls[1]["_min_wait"] == 1
        assert "The path opens" in result["text"]
        assert "Old choice" not in result.get("buttons", "")

    def test_choice_wait_retries_empty_pending_result(self, ctx, monkeypatch):
        from vnflight import handlers

        stale_rendered = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Old choice",
            "_data": {
                "pending": {
                    "type": "choice",
                    "id": "old",
                    "choices": [{"label": "Old choice", "index": 1}],
                },
            },
        }
        fresh_rendered = {
            "text": "[Narrator] The cabin appears.",
            "story": ["[Narrator] The cabin appears."],
        }
        wait_results = [stale_rendered, fresh_rendered]
        calls = []

        def fake_handle_wait(wait_ctx, params):
            calls.append(params)
            return wait_results.pop(0)

        monkeypatch.setattr(handlers, "handle_wait", fake_handle_wait)
        result = {"ok": True, "resolved_as": "choice"}

        handlers._settle_wait_after_action(
            ctx,
            result,
            {"timeout": 10},
            button_context=False,
            pre_state_sig=None,
            pre_rendered=None,
            pre_visible_sig=None,
            pre_pending_id=None,
            pre_was_button_only=False,
        )

        assert len(calls) == 2
        assert calls[1]["_min_wait"] == 1
        assert "The cabin appears" in result["text"]
        assert "Old choice" not in result.get("pending", "")

    def test_act_wait_promotes_late_merged_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "resolved_as": "choice"}
        ctx.client.last_request_id = "acted-1"
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "A scene finishes."}],
            pending=None,
        )
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "next-1",
                "choices": ["Open the door", "Wait"],
            },
        }

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert "pending" not in result
        assert result["_stale_pending_suppressed"] is True
        assert "A scene finishes" in result["text"]

    def test_screen_button_waits_for_rendered_state_change(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
        }
        old_state = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "Standard", "screen": "difficulty", "actions": ["Return"]},
                ],
            },
        }
        new_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Wait", "screen": "quick_menu", "actions": ["none"]},
                ],
            },
        }
        states = [old_state, old_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Standard", "wait": True})

        assert "status" not in result
        assert "Wait" in result["buttons"]
        assert "Standard" not in result["buttons"]

    def test_screen_button_does_not_settle_on_detail_only_format_change(
        self, ctx,
    ):
        """Internal pre/post renders must use the same footer policy."""
        from vnflight.handlers import handle_act

        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
        }
        progress = {
            "stats": {"gold": 3, "_summary": "Gold: 3"},
            "inventory": [{"name": "Food Rations"}],
        }
        old_state = {
            "status": "running",
            "game_state": progress,
            "screen": {"buttons": [
                {"label": "Standard", "screen": "difficulty",
                 "actions": ["Return"]},
            ]},
        }
        new_state = {
            "status": "running",
            "game_state": progress,
            "screen": {"buttons": [
                {"label": "Wait", "screen": "quick_menu",
                 "actions": ["none"]},
            ]},
        }
        # The preflight consumes the first snapshot. Keep the unchanged
        # screen available beyond the 0.6-second settle window: a detailed
        # preflight versus compact postflight would otherwise return it as a
        # false change before the real successor arrives.
        states = [old_state] * 5 + [new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {
            "target": "Standard", "wait": True, "result_timeout": 4,
        })

        assert "Wait" in result["buttons"]
        assert "Standard" not in result["buttons"]
        assert "Stats:" not in result.get("_footer", "")
        assert "Inventory" not in result.get("_footer", "")
        retire = [
            payload for name, payload in ctx.client.calls
            if name == "discard_rendered_action_transaction"
        ]
        assert len(retire) == 1
        assert retire[0]["action_nonce"].startswith("mock-nonce-")
        assert retire[0]["action_id"] == 1
        assert result["_screen_action_locally_retired"] is True

    def test_screen_button_success_survives_a_lost_cleanup_receipt(
        self, ctx,
    ):
        from vnflight.handlers import handle_act

        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
        }
        old_state = {
            "status": "ended",
            "screen": {"buttons": [{"label": "Standard"}]},
        }
        new_state = {
            "status": "running",
            "screen": {"buttons": [{"label": "Wait"}]},
        }
        states = [old_state, old_state, new_state]

        def delayed_state():
            return states.pop(0) if states else new_state

        cleanup_seen = []

        def lost_receipt(action_nonce, *, action_id=None, timeout=1.5):
            cleanup_seen.append(action_nonce)
            return MockWaitResult(transaction={
                "action_nonce": action_nonce,
                "transaction_state": "rejected",
                "pending": False,
                "reason": "unknown_nonce",
            })

        admission_probes = []

        def transaction_until_cleanup(action_nonce, *, timeout=3.0):
            if cleanup_seen:
                admission_probes.append(action_nonce)
                return None
            record = ctx.client._act_transactions.get(action_nonce)
            if record is None:
                return None
            return dict(record, transaction_state="applied", pending=True)

        ctx.client.state = delayed_state
        ctx.client.discard_rendered_action_transaction = lost_receipt
        ctx.client.action_transaction = transaction_until_cleanup

        result = handle_act(ctx, {"target": "Standard", "wait": True})

        assert result["ok"] is True
        assert "receipt was no longer available" in (
            result["_screen_action_retirement_warning"])
        assert result["transaction_state"] == "settled", result
        assert result["transaction_pending"] is False
        assert result["transaction"]["settled_by"] == (
            "rendered_state_local_retirement")
        assert "admission_pending" not in result["transaction"]
        assert admission_probes == []

    def test_hide_button_uses_screen_state_path(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
            "action_names": ["Hide"],
            "action_strs": ["Hide screen=map_display"],
        }
        map_state = {
            "screen": {
                "buttons": [
                    {"label": "[close map]", "screen": "map_display", "actions": ["Hide"]},
                ],
            },
        }
        game_state = {
            "pending_request": {
                "type": "choice_request",
                "id": "road-1",
                "choices": ["I look around."],
            },
            "screen": {
                "buttons": [
                    {"label": "Travel", "screen": "quick_menu", "actions": ["Show"]},
                ],
            },
        }
        states = [map_state, game_state, game_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else game_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "close map", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert wait_calls == []
        assert "I look around" in result["pending"]
        assert "close map" not in result.get("buttons", "")

    def test_close_map_button_uses_screen_state_path_without_action_metadata(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
            "screen": "map_display",
            "label": "[close map]",
        }
        map_state = {
            "screen": {
                "buttons": [
                    {"label": "[close map]", "screen": "map_display", "actions": ["Hide"]},
                ],
            },
        }
        game_state = {
            "pending_request": {
                "type": "choice_request",
                "id": "road-1",
                "choices": ["I look around."],
            },
            "screen": {
                "buttons": [
                    {"label": "Travel", "screen": "quick_menu", "actions": ["Show"]},
                ],
            },
        }
        states = [map_state, game_state, game_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else game_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "close map", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert wait_calls == []
        assert "I look around" in result["pending"]

    def test_shop_button_uses_screen_state_path(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "shop",
            "screen": "shopscreen",
            "label": "Buy Food Rations: 1",
        }
        shop_before = {
            "screen": {
                "texts": ["Your resources: 4 dragon bones"],
                "buttons": [
                    {"label": "Buy Food Rations: 1", "screen": "shopscreen"},
                    {"label": "[close shop]", "screen": "shopscreen"},
                ],
            },
        }
        shop_after = {
            "game_state": {
                "stats": {
                    "gold": 3,
                    "_summary": "Gold: 3",
                },
                "inventory": [
                    {"name": "Food Rations"},
                    {"name": "Waterskin"},
                ],
            },
            "screen": {
                "texts": ["Your resources: 3 dragon bones"],
                "buttons": [
                    {"label": "[close shop]", "screen": "shopscreen"},
                ],
            },
        }
        states = [shop_before, shop_after, shop_after]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else shop_after

        ctx.client.state = delayed_state
        original_discard = ctx.client.discard_rendered_action_transaction

        def discard_with_inventory(*args, **kwargs):
            receipt = original_discard(*args, **kwargs)
            receipt.events = [{
                "type": "inventory_update",
                "inventory": [{"name": "Food Rations"}],
                "action_id": 1,
                "_seq": 10,
            }]
            return receipt

        ctx.client.discard_rendered_action_transaction = discard_with_inventory

        result = handle_act(ctx, {"target": "Food Rations", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert wait_calls == []
        assert "Buy Food Rations" not in result.get("buttons", "")
        assert "close shop" in result.get("buttons", "")
        assert "Gold: 3" in result.get("_footer", "")
        assert "Stats:" not in result.get("_footer", "")
        assert "Inventory" not in result.get("_footer", "")
        assert "Food Rations" in str(result.get("status", ""))

    def test_main_menu_start_button_uses_story_wait_path(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
            "label": "Start",
        }
        wait_results = [
            MockWaitResult(events=[{"type": "game_started"}], pending=None),
            MockWaitResult(
                events=[{"type": "narration", "text": "The story begins."}],
                pending=None,
            ),
            MockWaitResult(
                events=[{"type": "narration", "text": "A question appears."}],
                pending={
                    "type": "choice_request",
                    "id": "first-choice",
                    "choices": ["Listen", "Leave"],
                },
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return wait_results.pop(0) if wait_results else MockWaitResult()

        ctx.client.wait = delayed_wait
        menu_state = {
            "status": "ended",
            "context": {"context": "main_menu"},
            "screen": {
                "buttons": [
                    {"label": "Start", "screen": "menu", "actions": ["Start"]},
                ],
            },
        }

        def live_state():
            # Once the final wait served its pending, /state carries the
            # same live request (the registry backs both) — the stale-
            # pending drop keys on exactly this.
            ctx.client.calls.append(("state", {}))
            if not wait_results:
                return {
                    "status": "waiting_for_input",
                    "pending_request": {
                        "type": "choice_request",
                        "id": "first-choice",
                        "choices": ["Listen", "Leave"],
                    },
                }
            return dict(menu_state)

        ctx.client.state = live_state
        ctx.client._state = menu_state

        result = handle_act(ctx, {"target": "Start", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) >= 3
        assert 59.0 < wait_calls[0][1]["timeout"] <= 60.0
        # The 3/5/8-second post-action timers are gone: "the post-action
        # scrape has landed" is E2 in the settle policy, and only a
        # one-second insurance floor remains.
        assert wait_calls[0][1]["min_wait"] == 1
        assert wait_calls[1][1]["min_wait"] == 1
        assert "The story begins." in result["text"]
        assert "A question appears." in result["text"]
        assert "Listen" in result["pending"]

    def test_main_menu_start_does_not_leapfrog_prefetched_story_with_overlay(
        self, ctx,
    ):
        """Fleet r8: a late terminal row belongs after parked narration."""
        from vnflight.handlers import (
            handle_act,
            handle_wait,
            render_tool_result_text,
        )

        client = ctx.client
        menu_state = {
            "status": "ended",
            "context": {"context": "main_menu"},
            "screen": {"buttons": [
                {"label": "Start", "screen": "menu", "actions": ["Start"]},
            ]},
        }
        next_pending = {
            "type": "choice_request",
            "id": "specialization",
            "choices": ["Systems", "Signals"],
        }
        next_state = {
            "status": "waiting_for_input",
            "pending_request": next_pending,
        }
        late_screen = {
            "type": "screen_content",
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
            "overlay_texts_by_screen": {"terminal": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "2"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
        }
        client._state = menu_state
        client._screen = {"buttons": menu_state["screen"]["buttons"]}
        client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
            "label": "Start",
        }
        original_act = client.act

        def start_act(target, _nonce=None):
            result = original_act(target, _nonce=_nonce)
            client.cursor = 120
            client._state_poll_serial = 4
            client._prefetched_events = [{
                "type": "narration",
                "text": "Another night at the end of the world.",
                "_seq": 130,
                "_source_id": "game-a",
                "_source_seq": 130,
            }]
            client._screen = late_screen
            client._state = next_state
            return result

        client.act = start_act

        def settling_wait(timeout=60, **kwargs):
            client.calls.append(("wait", {"timeout": timeout, **kwargs}))
            nonce = kwargs.get("action_nonce")
            transaction = (
                dict(client._act_transactions.get(nonce) or {})
                if nonce else None
            )
            if nonce:
                return MockWaitResult(events=[
                    {
                        "type": "narration",
                        "text": "The cursor blinks.",
                        "_seq": 120,
                        "_source_id": "game-a",
                        "_source_seq": 120,
                    },
                ], transaction=transaction)
            return MockWaitResult(pending=next_pending)

        client.wait = settling_wait
        act_result = handle_act(
            ctx, {"target": "Start", "wait": True, "timeout": 1})
        act_text = render_tool_result_text(act_result)

        assert "The cursor blinks." in act_text
        assert "SIGNAL DOES NOT MATCH" not in act_text

        client._prefetched_events = []
        client.cursor = 140
        client._state_poll_serial += 1
        client.wait = lambda timeout=60, **kwargs: MockWaitResult(
            events=[
                {
                    "type": "narration",
                    "text": "Another night at the end of the world.",
                    "_seq": 130,
                    "_source_id": "game-a",
                    "_source_seq": 130,
                },
                dict(late_screen),
            ],
            pending=next_pending,
        )
        followup = handle_wait(ctx, {"timeout": 1})
        followup_text = render_tool_result_text(followup)

        assert followup_text.index("Another night") < followup_text.index(
            "SIGNAL DOES NOT MATCH")
        assert followup_text.count("SIGNAL DOES NOT MATCH") == 1

    def test_wait_orders_prefetched_story_before_late_overlay_row(self):
        """The same no-leapfrog guarantee, driven end to end.

        The test above stubs ``act``/``wait`` on MockClient and hand-sets
        ``cursor`` / ``_state_poll_serial`` / ``_prefetched_events`` to stage
        each step, so it pins the handler composition but not the poll that
        produces those values.  Here the client is a real ``BridgeClient``
        over a scripted bridge: the narration is parked by the production
        ``ordinary_action_id`` fence, and ``handle_tool(ctx, "wait", ...)``
        runs the real ``wait()`` -> real ``poll()`` -> real overlay merge.
        The overlay row must never precede the parked narration, and must be
        delivered exactly once.
        """
        from vnflight import handlers

        pending = {
            "type": "choice_request",
            "id": "specialization",
            "choices": ["Systems", "Signals"],
        }
        overlay_screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
            "overlay_texts_by_screen": {"terminal": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "2"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
        }
        client = ScriptedBridgeClient(state={
            "status": "waiting_for_input",
            "pending_request": pending,
        })
        client.push_events({
            "type": "narration",
            "text": "Another night at the end of the world.",
            "action_id": 22,
            "_seq": 130,
            "_source_id": "game-a",
            "_source_seq": 130,
        })
        # Start's opening narration is owned by another action: the real
        # fence parks it in the prefetch stash instead of delivering it.
        assert client.poll(timeout=0, ordinary_action_id=11) == []
        assert [e["_seq"] for e in client._prefetched_events] == [130]

        client.set_screen(overlay_screen)
        ctx = handlers.HandlerContext(client=client)
        early: dict = {}
        handlers._merge_passive_overlay_text(ctx, early, overlay_screen)
        assert handlers.render_tool_result_text(early) == "(no new events)"
        assert [
            record["text"] for record in ctx.overlay.pending_deliveries
        ] == [">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<"]

        # The panel's durable row is now on the bridge too, but the parked
        # narration is chronologically first and must be served first.
        client.push_events({**overlay_screen, "type": "screen_content"})
        first = handlers.render_tool_result_text(
            handlers.handle_tool(ctx, "wait", {"timeout": 1}))
        assert "Another night at the end of the world." in first
        assert "SIGNAL DOES NOT MATCH" not in first

        second = handlers.render_tool_result_text(
            handlers.handle_tool(ctx, "wait", {"timeout": 1}))
        assert second.count("SIGNAL DOES NOT MATCH") == 1
        assert "Another night" not in second
        assert client.cursor == 140
        assert ctx.overlay.pending_deliveries == []

        third = handlers.render_tool_result_text(
            handlers.handle_tool(ctx, "wait", {"timeout": 1}))
        assert "SIGNAL DOES NOT MATCH" not in third

    def test_screen_button_keeps_story_wait_output(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "topics",
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The topic explains the clue."}],
            pending=None,
        )
        old_state = {
            "screen": {
                "buttons": [
                    {"label": "Topic A", "screen": "terminal_topics", "actions": ["Return"]},
                ],
            },
        }
        new_state = {
            "screen": {
                "buttons": [
                    {"label": "Topic A ✓", "screen": "terminal_topics", "actions": ["Return"]},
                ],
            },
        }
        states = [old_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Topic A", "wait": True})

        assert "The topic explains the clue." in result["text"]
        assert "Topic A ✓" in result.get("buttons", "")

    def test_topic_button_uses_shorter_observation_window(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "topic",
            "screen": "nvl",
            "label": "I’d like to eat.",
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=None)
        old_state = {
            "screen": {
                "buttons": [
                    {"label": "I’d like to eat.", "screen": "nvl"},
                ],
            },
        }
        new_state = {
            "screen": {
                "buttons": [
                    {"label": "I need to buy something.", "screen": "nvl"},
                ],
            },
        }
        states = [old_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        handle_act(ctx, {"target": "eat", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        # Was 3 s of blind observation; the settle policy proves the frame
        # instead, so only the one-second floor is left.
        assert wait_calls[0][1]["min_wait"] == 1

    def test_slow_async_topic_button_uses_rendered_state_fallback(self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_act

        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "topic",
            "screen": "nvl",
            "label": "Open panel",
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=None)
        old_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Open panel", "screen": "nvl"},
                ],
            },
        }
        new_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Panel ready", "screen": "slow_panel"},
                ],
            },
        }
        settling = {"active": False, "fetches": 0}
        stable_calls = []

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            if not settling["active"]:
                return old_state
            settling["fetches"] += 1
            return new_state if settling["fetches"] >= 2 else old_state

        def fake_wait_for_stable_change(**kwargs):
            stable_calls.append(kwargs)
            settling["active"] = True
            first = kwargs["fetch"]()
            second = kwargs["fetch"]()
            return second or first

        ctx.client.state = delayed_state
        monkeypatch.setattr(
            handlers,
            "wait_for_stable_change",
            fake_wait_for_stable_change,
        )

        result = handle_act(ctx, {"target": "Open panel", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert wait_calls[0][1]["min_wait"] == 1
        assert any(call["timeout"] == 6.0 for call in stable_calls)
        assert "Panel ready" in result["buttons"]
        assert "Open panel" not in result["buttons"]

    def test_topic_story_with_changed_buttons_skips_followup_wait(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "topic",
            "screen": "nvl",
            "label": "I’d like to eat.",
        }
        ctx.client._wait_result = MockWaitResult(
            events=[
                {
                    "type": "narration",
                    "text": "You eat a warm meal.",
                },
            ],
            pending=None,
            screen={
                "buttons": [
                    {"label": "I need to buy something.", "screen": "nvl"},
                ],
            },
        )
        old_state = {
            "screen": {
                "buttons": [
                    {"label": "I’d like to eat.", "screen": "nvl"},
                ],
            },
        }
        new_state = {
            "screen": {
                "buttons": [
                    {"label": "I need to buy something.", "screen": "nvl"},
                ],
            },
        }
        states = [old_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "eat", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 1
        assert "You eat a warm meal." in result["text"]
        assert "I need to buy something." in result["buttons"]

    def test_screen_button_waits_for_rendered_state_to_settle(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
        }
        old_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "foggy-room",
                "choices": ["I approach Foggy.", "I go outside."],
            },
            "screen": {
                "buttons": [
                    {"label": "Inventory", "screen": "quick_menu", "actions": ["ShowMenu"]},
                ],
            },
        }
        transient_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "inventory-detail",
                "choices": ["Return"],
            },
            "screen": {
                "buttons": [
                    {"label": "Return", "screen": "inventory", "actions": ["Return"]},
                ],
            },
        }
        settled_state = {
            "status": "running",
            "screen": {
                "button_categories": {
                    "Small Healing Potion": {"category": "supplies", "label": "SUPPLIES"},
                },
                "buttons": [
                    {
                        "label": "Small Healing Potion",
                        "screen": "inventory",
                        "actions": ["SetField", "SetField"],
                    },
                    {"label": "Return", "screen": "inventory", "actions": ["Return"]},
                ],
            },
        }
        states = [old_state, transient_state, settled_state, settled_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else settled_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Inventory", "wait": True})

        assert "Small Healing Potion" in result["buttons"]
        assert "I approach Foggy" not in result.get("pending", "")

    def test_button_wait_path_falls_back_to_rendered_state_change(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=None)
        old_state = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "I leave", "screen": "intro", "actions": ["Jump"]},
                ],
            },
        }
        new_state = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "Standard", "screen": "difficulty", "actions": ["Jump"]},
                ],
            },
        }
        states = [old_state, old_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert "Standard" in result["buttons"]
        assert "I leave" not in result["buttons"]
        assert "scene unchanged" not in result.get("text", "")

    def test_button_wait_path_can_replace_stale_buttons_with_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=None)
        old_state = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "Standard", "screen": "difficulty", "actions": ["Jump"]},
                ],
            },
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "camp-1",
                "choices": ["Look around", "Enter briskly"],
            },
            "screen": {
                "buttons": [
                    {"label": "Settings", "screen": "quick_menu", "actions": ["ShowMenu"]},
                ],
            },
        }
        nav_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Wait", "screen": "quick_menu", "actions": ["none"]},
                    {"label": "Settings", "screen": "quick_menu", "actions": ["ShowMenu"]},
                ],
            },
        }
        states = [old_state, nav_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Standard", "wait": True})

        assert "Look around" in result["pending"]
        assert "Standard" not in result.get("buttons", "")

    def test_button_wait_replacement_clears_stale_ended_flag(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=None, ended=True)
        old_state = {
            "status": "ended",
            "screen": {
                "buttons": [
                    {"label": "Standard", "screen": "difficulty", "actions": ["Jump"]},
                ],
            },
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "camp-1",
                "choices": ["Look around", "Enter briskly"],
            },
        }
        states = [old_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Standard", "wait": True})

        assert "Look around" in result["pending"]
        assert "ended" not in result

    def test_button_wait_replaces_same_empty_pending_with_rendered_overlay(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        stale_pending = {
            "type": "choice_request",
            "id": "night-rest",
            "full_items": [
                {
                    "label": "All I can do now is rest. (disabled)",
                    "is_disabled": True,
                },
            ],
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=stale_pending)
        old_state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
            "screen": {
                "buttons": [
                    {"label": "Sleep", "screen": "quick_menu", "actions": ["ShowMenu"]},
                ],
            },
        }
        overlay_state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
            "screen": {
                "overlay_active": True,
                "overlay_texts": ["Sleeping in a tent"],
                "button_categories": {
                    "Sleep": {"category": "rest_free", "label": "SLEEP (FREE)"},
                    "Close": {"category": "options", "label": "OPTIONS"},
                },
                "buttons": [
                    {"label": "Close", "screen": "sleep", "actions": ["Hide"]},
                    {"label": "Sleep", "screen": "sleep", "actions": ["Return"]},
                ],
            },
        }
        states = [old_state, old_state, overlay_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else overlay_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Sleep", "wait": True})

        assert "Sleeping in a tent" in result["text"]
        assert "Sleep" in result["buttons"]
        assert "All I can do now is rest" not in result.get("pending", "")

    def test_button_wait_adds_current_screen_text_to_bare_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        next_pending = {
            "type": "choice_request",
            "id": "topic-detail",
            "choices": ["Ask a follow-up."],
        }
        ctx.client._wait_result = MockWaitResult(
            events=[],
            pending=next_pending,
            screen=None,
        )
        old_state = {
            "status": "waiting_for_input",
            "screen": {
                "buttons": [
                    {"label": "Topic", "screen": "nvl", "actions": ["Jump"]},
                ],
            },
        }
        topic_state = {
            "status": "waiting_for_input",
            "pending_request": next_pending,
            "screen": {
                "screens": ["nvl"],
                "texts": ["The lieutenant explains the situation."],
                "buttons": [
                    {
                        "label": "Ask a follow-up.",
                        "screen": "nvl",
                        "actions": ["ChoiceReturn"],
                    },
                ],
            },
        }
        states = [old_state, old_state, topic_state, topic_state]
        screen_calls = {"count": 0}

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else topic_state

        def delayed_screen(_client):
            screen_calls["count"] += 1
            if screen_calls["count"] < 3:
                return None
            return topic_state["screen"]

        ctx.client.state = delayed_state
        ctx.client._screen_provider = lambda: delayed_screen(None)

        result = handle_act(ctx, {"target": "Topic", "wait": True})

        assert "The lieutenant explains" in result["screen_text"]
        assert "Ask a follow-up" in result["pending"]

    def test_topic_button_preserves_story_when_state_settle_is_bare_pending(
        self,
        ctx,
        monkeypatch,
    ):
        from vnflight import handlers
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        next_pending = {
            "type": "choice_request",
            "id": "topic-detail",
            "choices": ["Ask a follow-up."],
        }
        topic_state = {
            "status": "waiting_for_input",
            "pending_request": next_pending,
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{
                "type": "narration",
                "text": "The lieutenant explains the situation.",
            }],
            pending=next_pending,
        )
        ctx.client._state = topic_state

        def bare_state_change(*args, **kwargs):
            return handlers.handle_state(ctx, {"brief": False})

        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            bare_state_change,
        )

        result = handlers.handle_act(ctx, {"target": "Topic", "wait": True})

        assert "The lieutenant explains" in result["text"]
        assert "Ask a follow-up" in result["pending"]

    def test_map_button_keeps_dialogue_burst_that_arrives_with_the_menu(
        self,
        ctx,
        monkeypatch,
    ):
        """A map-travel button that plays a scene must not eat the scene.

        Echoes of Tomorrow moves between locations with map buttons whose
        screen name contains "map".  Travelling can run a one-shot scene
        (``scene bg_observatory with fade`` + sprites + several say lines)
        that ends in a menu.  The post-act state settle used to promote the
        bare rendered state over the wait output and then refuse to restore
        the story text for map transitions, so the agent received ONLY the
        choice block while the transcript kept every line.
        """
        from vnflight import handlers

        ctx.client._act_result = {
            "ok": True,
            "success": True,
            "resolved_as": "button",
            "interaction_type": "choice",
            "label": "COMMS Array",
            "screen": "observatory_map",
        }
        next_pending = {
            "type": "choice_request",
            "id": "657ee740",
            "choices": [
                "Then we go together. Rope line, both of us.",
                "Nobody goes out in this.",
            ],
        }
        map_state = {
            "status": "running",
            "game_state": {
                "screen_buttons": [
                    {"label": "COMMS Array", "screen": "observatory_map"},
                    {"label": "HABITAT Canteen & quarters",
                     "screen": "observatory_map"},
                ],
            },
        }
        corridor_state = {
            "status": "waiting_for_input",
            "pending_request": next_pending,
        }
        # Pre-act reads see the map screen; everything after the act sees the
        # corridor menu (no story text — /state never carries transcript lines).
        acted = {"done": False}
        real_act = ctx.client.act

        def tracking_act(target, _nonce=None):
            acted["done"] = True
            return real_act(target, _nonce)

        def staged_state():
            ctx.client.calls.append(("state", {}))
            return dict(corridor_state) if acted["done"] else dict(map_state)

        ctx.client.act = tracking_act
        ctx.client.state = staged_state
        ctx.client._wait_result = MockWaitResult(
            events=[
                {"type": "narration",
                 "text": "Marcus is in the corridor outside the comms room, "
                         "halfway into a cold-weather suit."},
                {"type": "dialogue", "character": "Dr. Voss",
                 "text": "Marcus. Tell me you are not about to go outside."},
                {"type": "dialogue", "character": "Dr. Chen",
                 "text": "So yes, I'm going outside. It's my station too."},
                {"type": "dialogue", "character": "Dr. Chen",
                 "text": "The wind beyond the airlock does not sound like "
                         "weather. It sounds like a verdict."},
            ],
            pending=next_pending,
        )

        def bare_state_change(*args, **kwargs):
            return handlers.handle_state(ctx, {"brief": False})

        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            bare_state_change,
        )

        result = handlers.handle_act(
            ctx, {"target": "COMMS Array", "wait": True})

        text = result.get("text") or ""
        assert "corridor outside the comms room" in text
        assert "Tell me you are not about to go outside" in text
        assert "It sounds like a verdict" in text
        assert "Rope line" in result["pending"]

    def test_map_button_keeps_burst_split_across_two_settle_waits(
        self,
        ctx,
        monkeypatch,
    ):
        """The burst survives a state settle FOLLOWED BY another wait.

        Live runs split the scene across two waits: the first sees the opening
        narration, a bare state re-fetch replaces it, and the follow-up wait
        brings the remaining say lines.  The follow-up promotion must not drop
        what the state pass already preserved (result["_data"] no longer holds
        the story at that point, so the check cannot rely on it alone).
        """
        from vnflight import handlers

        ctx.client._act_result = {
            "ok": True,
            "success": True,
            "resolved_as": "button",
            "interaction_type": "choice",
            "label": "COMMS Array",
            "screen": "observatory_map",
        }
        next_pending = {
            "type": "choice_request",
            "id": "657ee740",
            "choices": ["Then we go together.", "Nobody goes out in this."],
        }
        map_state = {
            "status": "running",
            "game_state": {
                "screen_buttons": [
                    {"label": "COMMS Array", "screen": "observatory_map"},
                ],
            },
        }
        corridor_state = {
            "status": "waiting_for_input",
            "pending_request": next_pending,
        }
        acted = {"done": False}
        real_act = ctx.client.act

        def tracking_act(target, _nonce=None):
            acted["done"] = True
            return real_act(target, _nonce)

        def staged_state():
            ctx.client.calls.append(("state", {}))
            return dict(corridor_state) if acted["done"] else dict(map_state)

        wait_results = [
            MockWaitResult(
                events=[{
                    "type": "narration",
                    "text": "Marcus is in the corridor outside the comms room.",
                }],
                pending=next_pending,
            ),
            MockWaitResult(
                events=[{
                    "type": "dialogue",
                    "character": "Dr. Chen",
                    "text": "The wind beyond the airlock sounds like a verdict.",
                }],
                pending=next_pending,
            ),
        ]

        def staged_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            if wait_results:
                return wait_results.pop(0)
            return MockWaitResult(events=[], pending=next_pending)

        ctx.client.act = tracking_act
        ctx.client.state = staged_state
        ctx.client.wait = staged_wait

        def bare_state_change(*args, **kwargs):
            return handlers.handle_state(ctx, {"brief": False})

        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            bare_state_change,
        )

        result = handlers.handle_act(
            ctx, {"target": "COMMS Array", "wait": True})

        text = result.get("text") or ""
        assert "corridor outside the comms room" in text
        assert "sounds like a verdict" in text
        assert "Then we go together" in result["pending"]

    def test_join_story_text_drops_repeated_overlap(self):
        from vnflight.handlers import _join_story_text

        previous = "Line one.\nLine two.\nLine three."
        new = "Line two.\nLine three.\nLine four."

        assert _join_story_text(previous, new) == (
            "Line one.\nLine two.\nLine three.\nLine four."
        )
        assert _join_story_text(previous, "") == previous
        assert _join_story_text("", new) == new

    def test_preserved_story_rejects_older_unprovenanced_co_terminal_snapshot(self):
        """A late state-history read must not replay the opening after an act."""
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        current_tail = (
            "On the next day, Elara sat down in the canteen.\n"
            "She did not notice when Marcus came in for coffee.\n"
            "[Dr. Chen] You look terrible. When did you last sleep?\n"
            "[Dr. Voss] Something happened on the night shift.\n"
            "Should I tell him?"
        )
        result = {
            "text": "The second signal decodes.\n" + current_tail,
            "_story_render_sections": [
                {
                    "channel": "text",
                    "text": "The second signal decodes.",
                    "occurrence_ids": ["source:200"],
                    "_bridge_seq": 200,
                },
                {
                    "channel": "text",
                    "text": current_tail,
                    "occurrence_ids": ["source:329"],
                    "_bridge_seq": 329,
                },
            ],
        }
        stale_snapshot = {
            "text": "The deep field sensors complete calibration.\n" + current_tail,
            "pending": "1. Tell him.\n2. Say nothing.",
            "_data": {
                "story": [{
                    "type": "narration",
                    "text": "The deep field sensors complete calibration.",
                }],
                "pending": {"type": "choice", "id": "canteen"},
            },
        }

        _promote_wait_output_preserving_story(result, stale_snapshot)

        rendered = render_tool_result_text(result)
        assert rendered.count("The deep field sensors complete calibration.") == 0
        assert rendered.count("On the next day") == 1
        assert "1. Tell him." in rendered

    def test_preserved_story_rejects_sequenced_co_terminal_history_snapshot(self):
        """Transport sequence stamps do not make reconstructed history fresh."""
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        current_tail = (
            "On the next day, Elara sat down in the canteen.\n"
            "She did not notice when Marcus came in for coffee.\n"
            "[Dr. Chen] You look terrible. When did you last sleep?\n"
            "[Dr. Voss] Something happened on the night shift.\n"
            "Should I tell him?"
        )
        result = {
            "text": "The second signal decodes.\n" + current_tail,
            "_story_render_sections": [{
                "channel": "text",
                "text": "The second signal decodes.\n" + current_tail,
                "occurrence_ids": ["source:200"],
                "_bridge_seq": 200,
            }],
        }
        stale_snapshot = {
            "text": (
                "In twenty years of radio astronomy, I have never seen "
                "anything like this.\n" + current_tail
            ),
            "pending": "1. Tell him.\n2. Say nothing.",
            "_story_render_sections": [{
                "channel": "text",
                "text": (
                    "In twenty years of radio astronomy, I have never seen "
                    "anything like this.\n" + current_tail
                ),
                "occurrence_ids": ["source:330"],
                "_bridge_seq": 330,
            }],
            "_data": {
                "story": [{
                    "type": "narration",
                    "text": "In twenty years of radio astronomy...",
                    "_source_seq": 330,
                }],
                "pending": {"type": "choice", "id": "canteen"},
            },
        }

        _promote_wait_output_preserving_story(result, stale_snapshot)

        rendered = render_tool_result_text(result)
        assert "In twenty years" not in rendered
        assert rendered.count("On the next day") == 1
        assert "1. Tell him." in rendered

    def test_transient_no_interaction_error_recovers_changed_state(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "success": False,
            "error": "No interaction matching u'We move forward.'.",
        }
        old_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "old-step",
                "choices": ["We move forward."],
            },
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "next-step",
                "choices": ["Choose a new plan."],
            },
        }
        states = [old_state, old_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert "error" not in result
        assert "No interaction matching" in result["_original_error"]
        assert "Choose a new plan" in result["pending"]

    def test_transient_no_active_choice_error_recovers_wait_output(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "success": False,
            "error": "No active choice request for choice resolution",
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The scene moves on."}],
            pending={
                "type": "choice_request",
                "id": "next-step",
                "choices": ["Continue onward."],
            },
        )
        old_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "old-step",
                "choices": ["I ask the guards to open the gate."],
            },
        }
        nav_only_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Inventory", "screen": "quick_menu"},
                    {"label": "Settings", "screen": "quick_menu"},
                ],
            },
        }
        live_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "next-step",
                "choices": ["Continue onward."],
            },
        }
        states = [old_state, old_state, nav_only_state, nav_only_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            if states:
                return states.pop(0)
            if any(name == "wait" for name, _ in ctx.client.calls):
                # Recovery wait served its pending; /state carries the
                # same live request from here on.
                return dict(live_state)
            return nav_only_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {
            "target": "1", "wait": True, "timeout": 1,
            "result_timeout": 5,
        })

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert "error" not in result
        assert "No active choice request" in result["_original_error"]
        assert "The scene moves on" in result["text"]
        assert "Continue onward" in result["pending"]

    def test_transient_no_interaction_recovery_wait_allows_slow_autoskip(
        self,
        ctx,
        monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.handlers import handle_act

        ctx.client._act_result = {
            "success": False,
            "error": "No interaction matching 1. Available: []",
        }
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "",
                "choices": ["[Continue down the stairs.]"],
            },
            "game_state": {
                "buttons": [
                    {
                        "label": "[Continue down the stairs.]",
                        "screen": "_focus_list",
                    },
                ],
            },
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{
                "type": "narration",
                "text": "The Princess towers over you.",
            }],
        )
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            lambda *args, **kwargs: None,
        )

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert "No interaction matching" in result["_original_error"]
        assert "The Princess towers over you" in result["text"]
        assert any(
            name == "wait" and call["timeout"] == 10
            for name, call in ctx.client.calls
        )

    def test_focus_list_continue_no_active_choice_recovers_as_autoadvance(
        self,
        ctx,
        monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.handlers import handle_act

        ctx.client._act_result = {
            "success": False,
            "error": "No active choice request for choice resolution",
        }
        ctx.client._state = {
            "status": "screen_actions",
            "game_state": {
                "screen_buttons": [
                    {
                        "label": "[End this.]",
                        "screen": "_focus_list",
                        "actions": ["Return"],
                        "category": "choices",
                        "index": 1,
                    },
                ],
                "interactions": [
                    {
                        "source": "button",
                        "type": "choice",
                        "category": "choices",
                        "index": 1,
                        "display_label": "[End this.]",
                        "screen": "_focus_list",
                        "action_names": ["Return"],
                    },
                ],
            },
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The loop ends."}],
        )
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            lambda *args, **kwargs: None,
        )

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert "No active choice request" in result["_original_error"]
        assert "The loop ends" in result["text"]
        assert any(name == "act" for name, _call in ctx.client.action_calls)

    def test_choice_wait_polls_state_when_wait_keeps_pre_action_pending(self, ctx):
        from vnflight.handlers import handle_act
        old_pending = {
            "type": "choice_request",
            "id": "old-step",
            "choices": ["See you soon."],
        }
        new_pending = {
            "type": "choice_request",
            "id": "new-step",
            "choices": ["I go to the main square.", "I head to Bion."],
        }
        old_state = {
            "status": "waiting_for_input",
            "pending_request": old_pending,
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": new_pending,
        }
        states = [old_state, old_state, old_state, old_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state
        ctx.client._wait_result = MockWaitResult(events=[], pending=old_pending)
        ctx.client._game_state = {"_seq": 1}

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 2})

        assert "I go to the main square" in result["pending"]
        assert "See you soon" not in result["pending"]
        assert "scene unchanged" not in result.get("text", "")

    def test_transient_error_does_not_recover_unchanged_state(self, ctx):
        from vnflight.handlers import handle_act
        pending = {
            "type": "choice_request",
            "id": "old-step",
            "choices": ["Only real choice."],
        }
        ctx.client._act_result = {
            "success": False,
            "error": "No interaction matching u'Bad target'.",
        }
        ctx.client._wait_result = MockWaitResult(events=[], pending=pending)
        old_state = {
            "status": "waiting_for_input",
            "pending_request": pending,
        }
        ctx.client._state = old_state

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 1})

        assert result["success"] is False
        assert result["error"].startswith("No interaction matching")
        assert "_recovered_after_advance" not in result

    def test_unknown_string_target_does_not_recover_current_state(self, ctx):
        from vnflight.handlers import handle_act
        pending = {
            "type": "choice_request",
            "id": "navica",
            "choices": [
                "“I’m still interested in that boat.”",
                "I walk away.",
            ],
        }
        current_state = {
            "status": "waiting_for_input",
            "pending_request": pending,
        }
        ctx.client._act_result = {
            "success": False,
            "error": "No interaction matching u'Iuno'.",
        }
        ctx.client._state = current_state
        ctx.client._wait_result = MockWaitResult(events=[], pending=pending)

        result = handle_act(ctx, {"target": "Iuno", "wait": True, "timeout": 1})

        assert result["success"] is False
        assert result["error"].startswith("No interaction matching")
        assert "_recovered_after_advance" not in result
        assert "pending" not in result

    def test_act_with_no_pending_fails_explicitly_instead_of_quiet_prompt(
        self, ctx,
    ):
        """`act N` before any choice arrived used to be rewritten into a
        quiet success showing the just-arrived prompt — runner retry logic
        saw exit 0 / success and never re-submitted (silent-act-failure)."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "success": False,
            "error": "No active choice request for choice resolution",
        }
        # Pre-act state: game still advancing, nothing actionable at all.
        ctx.client._state = {"status": "running"}
        # The prompt that arrives moments later (what the old rescue path
        # returned as a success).
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The scene settles."}],
            pending={
                "type": "choice_request",
                "id": "arrived-later",
                "choices": ["First real choice."],
            },
        )

        result = handle_act(ctx, {"target": "1", "wait": True, "timeout": 1})

        assert not result.get("success")
        assert "Did not act" in result["error"]
        # With the numeric snapshot contract the refusal now happens
        # BEFORE the shim ever sees the act (nothing was numbered), so
        # the shim cannot resolve the raw number against its live focus
        # list at all.
        assert "nothing is numbered" in result["error"]
        assert result["_nothing_numbered_at_act"] is True
        assert "_recovered_after_advance" not in result
        assert ctx.client.action_calls.count(("act", 1)) == 0

    def test_act_with_no_pending_string_target_fails_explicitly(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "success": False,
            "error": "No interaction matching u'Continue'.",
        }
        ctx.client._state = {"status": "running"}
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "Moments later."}],
            pending={
                "type": "choice_request",
                "id": "arrived-later",
                "choices": ["Continue onward."],
            },
        )

        result = handle_act(ctx, {"target": "Continue", "wait": True, "timeout": 1})

        assert result.get("success") is False
        assert "Did not act" in result["error"]
        assert result["_no_pending_at_act"] is True

    def test_bracketed_continue_target_can_recover_after_autoadvance(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "success": False,
            "error": "No active choice request for choice resolution",
        }
        # The single focus-list continue auto-resolved before act() arrived,
        # so the current state is already empty/advancing. Bracketed labels
        # are the narrow no-op-success exception to the no-pending gate.
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The scene already moved."}],
            pending={
                "type": "choice_request",
                "id": "next",
                "choices": ["A real next choice."],
            },
        )

        def live_state():
            # Empty/advancing until the recovery wait serves its pending;
            # then /state carries the same live request.
            ctx.client.calls.append(("state", {}))
            if any(name == "wait" for name, _ in ctx.client.calls):
                return {
                    "status": "waiting_for_input",
                    "pending_request": {
                        "type": "choice_request",
                        "id": "next",
                        "choices": ["A real next choice."],
                    },
                }
            return {"status": "running"}

        ctx.client.state = live_state
        ctx.client._state = {"status": "running"}

        result = handle_act(ctx, {"target": "[End this.]", "wait": True})

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert "No active choice request" in result["_original_error"]
        assert "The scene already moved" in result["text"]
        assert "A real next choice" in result["pending"]
        assert "_no_pending_at_act" not in result

    def test_numeric_continue_target_can_recover_after_autoadvance(self, ctx):
        from vnflight.handlers import handle_act
        # The agent previously saw a numbered single-continue prompt, but
        # by the time act(1) arrives the shim has already auto-resolved it.
        ctx.client.last_request_id = "auto-continue"
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The scene already moved."}],
            pending={
                "type": "choice_request",
                "id": "next",
                "choices": ["A real next choice."],
            },
        )

        def live_state():
            ctx.client.calls.append(("state", {}))
            if any(name == "wait" for name, _ in ctx.client.calls):
                return {
                    "status": "waiting_for_input",
                    "pending_request": {
                        "type": "choice_request",
                        "id": "next",
                        "choices": ["A real next choice."],
                    },
                }
            return {"status": "running"}

        ctx.client.state = live_state
        ctx.client._state = {"status": "running"}

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert result["_numeric_autoadvance_race"] is True
        assert "No active choice request" in result["_original_error"]
        assert "The scene already moved" in result["text"]
        assert "A real next choice" in result["pending"]
        # Do not forward a raw number to the shim while narration is live; it
        # might hit an unrelated focus-list/nav button.
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_act_no_pending_gate_spares_races_with_visible_choices(self, ctx):
        """The gate must not fire when something WAS actionable pre-act —
        the scene-advanced recovery stays for genuine races."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "success": False,
            "error": "No active choice request for choice resolution",
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The scene moves on."}],
            pending={
                "type": "choice_request",
                "id": "next-step",
                "choices": ["Continue onward."],
            },
        )
        old_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "old-step",
                "choices": ["A visible choice."],
            },
        }
        nav_only_state = {"status": "running"}
        states = [old_state, old_state, nav_only_state, nav_only_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else nav_only_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {
            "target": "1", "wait": True, "timeout": 1,
            "result_timeout": 5,
        })

        assert result["success"] is True
        assert result["_recovered_after_advance"] is True
        assert "_no_pending_at_act" not in result

    def test_numeric_act_mid_narration_never_reaches_live_focus_list(self, ctx):
        """Live-observed failure: `act 1` during Echoes intro narration
        SUCCEEDED shim-side by resolving the raw index against the live
        focus list (dynamic value_map picked a newly-sensitive nav
        button).  Nothing was numbered in the rendered snapshot, so the
        number is not a reply to anything — refuse before the shim can
        click the wrong thing."""
        from vnflight.handlers import handle_act
        # The shim WOULD happily resolve the raw number:
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "label": "Back",
            "screen": "_focus_list",
        }
        # Rendered snapshot: pure narration, nothing numbered.
        ctx.client._state = {"status": "running"}

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert not result.get("success") and not result.get("ok")
        assert "nothing is numbered" in result["error"]
        assert result["_nothing_numbered_at_act"] is True
        # The raw number never reaches the shim.
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_numeric_act_on_button_only_screen_resolves_via_snapshot(self, ctx):
        """Map screens / sidebar states without a pending choice must stay
        numerically actable — the number resolves through the rendered
        snapshot to the concrete button label, never as a raw index."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        ctx.client._state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Howler's Dell", "screen": "map",
                     "actions": ["Jump"]},
                    {"label": "Old Pagos", "screen": "map",
                     "actions": ["Jump"]},
                ],
            },
        }

        result = handle_act(ctx, {"target": "2", "wait": False})

        assert result.get("ok") is True
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == ["Old Pagos"]  # label, not the raw index

    def test_label_act_mid_narration_stays_valid(self, ctx):
        """The responsive-UI escape hatch: acting by label while narration
        runs (Save/History/etc.) must keep working."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "label": "Save",
            "screen": "quick_menu",
            "interaction_type": "other",
        }
        ctx.client._state = {"status": "running"}

        result = handle_act(ctx, {"target": "Save", "wait": False})

        assert result.get("ok") is True
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == ["Save"]

    def test_numeric_choice_act_with_matching_read_id_unaffected(self, ctx):
        """Normal flow: the caller read menu Y (last_request_id == Y) and
        replies numerically — forwarded to the shim as an index reply."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 2}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-y",
                "choices": ["Go left", "Go right"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Go left", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Go right", "disabled": False},
                ],
            },
        }
        ctx.client.last_request_id = "menu-y"

        result = handle_act(ctx, {"target": "2", "wait": False})

        assert result.get("ok") is True
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == [2]

    def test_numeric_choice_act_refused_when_menu_replaced_since_read(self, ctx):
        """Staleness binding: the caller last rendered menu X, but the
        pending request is now menu Y — index N of Y may be a completely
        different choice, so the reply must be refused, not delivered."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 1}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-y",
                "choices": ["Attack the guard", "Run away"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Attack the guard", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Run away", "disabled": False},
                ],
            },
        }
        # The caller's last rendered output was a DIFFERENT menu.
        ctx.client.last_request_id = "menu-x"

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert not result.get("success") and not result.get("ok")
        assert "state changed since you last looked" in result["error"]
        assert result["_stale_numeric_act"] is True
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_numeric_choice_act_allowed_without_read_binding(self, ctx):
        """No prior read (last_request_id None) means no binding to
        enforce — blind numeric acts on a live menu keep working."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 1}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-y",
                "choices": ["Go left", "Go right"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Go left", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Go right", "disabled": False},
                ],
            },
        }
        ctx.client.last_request_id = None

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == [1]

    def test_numeric_act_allowed_when_only_the_request_id_rotated(
        self, ctx,
    ):
        """Fleet R62 defect 4: a changed id is not a changed decision.

        echo62-o01 08:38:17 was refused with "the numbered choices were
        replaced" while the refusal's own stale_details showed
        rendered_choices == current_choices, five labels in the same order.
        With no snapshot to compare, the numbered list IS the visible
        surface, and an identical list is a re-registration.  The act binds
        against the current snapshot, so index N resolves through the live
        menu's own value map."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 1}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-rotated",
                "choices": ["Investigate the signal", "Ignore it"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Investigate the signal", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Ignore it", "disabled": False},
                ],
            },
        }
        # Caller last rendered the SAME options under a different request id.
        ctx.client.last_request_id = "menu-original"
        ctx.client.last_choices = ["Investigate the signal", "Ignore it"]

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True, result
        assert not result.get("_stale_numeric_act")
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == [1]

    def test_numeric_act_allows_identical_natural_reregistration(self, ctx):
        """A stable menu may get a fresh request id while narration lands."""
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import handle_act

        old_state = {
            "pending_request": {
                "type": "choice_request", "id": "menu-original",
                "choices": ["Wait", "Leave"],
            },
            "game_state": {
                "interactions": [
                    {"id": "choice:wait", "source": "choice", "index": 1,
                     "display_label": "Wait", "action_strs": ["Return('wait')"]},
                    {"id": "choice:leave", "source": "choice", "index": 2,
                     "display_label": "Leave", "action_strs": ["Return('leave')"]},
                    {"id": "screen:kit", "source": "button", "index": 3,
                     "display_label": "KIT", "action_strs": ["Show('kit')"]},
                ],
                "screen_buttons": [
                    {"label": "KIT", "screen": "kit",
                     "action_strs": ["Show('kit')"]},
                ],
            },
        }
        ctx.client._state = dict(old_state)
        ctx.client._state["pending_request"] = dict(old_state["pending_request"])
        ctx.client._state["pending_request"]["id"] = "menu-reregistered"
        ctx.client.last_request_id = "menu-original"
        ctx.client.last_choices = ["Wait", "Leave"]
        ctx.client.last_actionable_snapshot = actionable_state_snapshot(old_state)
        ctx.client._act_result = {"ok": True, "chosen": 1}

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True
        assert [t for name, t in ctx.client.action_calls if name == "act"] == [1]

    def test_numeric_act_rejects_reregistration_missing_live_button(self, ctx):
        """Equal labels are insufficient when a stale render lost controls."""
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import handle_act

        old_state = {
            "pending_request": {
                "type": "choice_request", "id": "menu-original",
                "choices": ["Wait", "Leave"],
            },
            "game_state": {
                "interactions": [
                    {"id": "choice:wait", "source": "choice", "index": 1,
                     "display_label": "Wait", "action_strs": ["Return('wait')"]},
                    {"id": "choice:leave", "source": "choice", "index": 2,
                     "display_label": "Leave", "action_strs": ["Return('leave')"]},
                    {"id": "screen:kit", "source": "button", "index": 3,
                     "display_label": "KIT", "action_strs": ["Show('kit')"]},
                ],
                "screen_buttons": [
                    {"label": "KIT", "screen": "kit",
                     "action_strs": ["Show('kit')"]},
                ],
            },
        }
        ctx.client._state = {
            "pending_request": {
                "type": "choice_request", "id": "menu-reregistered",
                "choices": ["Wait", "Leave"],
            },
            "game_state": {
                "interactions": old_state["game_state"]["interactions"][:2],
            },
        }
        ctx.client.last_request_id = "menu-original"
        ctx.client.last_choices = ["Wait", "Leave"]
        ctx.client.last_actionable_snapshot = actionable_state_snapshot(old_state)

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("_stale_numeric_act") is True
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_numeric_act_allows_an_identical_snapshot_without_action_ids(
        self, ctx,
    ):
        """An identical actionable surface is enough, ids or no ids.

        This used to fail closed on missing ``action_strs``.  Nothing on the
        rendered surface moved here -- same request choices, same interaction
        rows, same other buttons -- so refusing told the agent its numbering
        had been replaced when it had not (fleet R62 defect 4).  The stricter
        arm is kept where it can actually discriminate: a snapshot that
        DIFFERS still refuses, even under equal labels."""
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import handle_act

        old_state = {
            "pending_request": {
                "type": "choice_request", "id": "menu-original",
                "choices": ["Continue", "Leave"],
            },
            "game_state": {"interactions": [
                {"source": "choice", "type": "choice", "index": 1,
                 "display_label": "Continue", "disabled": False},
                {"source": "choice", "type": "choice", "index": 2,
                 "display_label": "Leave", "disabled": False},
                {"source": "button", "type": "other", "index": 3,
                 "display_label": "KIT", "disabled": False,
                 "action_strs": ["Show('kit')"]},
            ]},
        }
        ctx.client._state = {
            "pending_request": {
                "type": "choice_request", "id": "menu-reregistered",
                "choices": ["Continue", "Leave"],
            },
            "game_state": old_state["game_state"],
        }
        ctx.client.last_request_id = "menu-original"
        ctx.client.last_choices = ["Continue", "Leave"]
        ctx.client.last_actionable_snapshot = actionable_state_snapshot(old_state)

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True, result
        assert not result.get("_stale_numeric_act")

    def test_numeric_act_allowed_for_explicit_same_content_resync(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 1}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-rotated",
                "reissued_from_request_id": "menu-original",
                "choices": ["Investigate the signal", "Ignore it"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Investigate the signal", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Ignore it", "disabled": False},
                ],
            },
        }
        ctx.client.last_request_id = "menu-original"
        ctx.client.last_choices = ["Investigate the signal", "Ignore it"]

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True
        assert not result.get("_stale_numeric_act")
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == [1]

    def test_numeric_act_allowed_after_two_same_content_resyncs(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 1}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-resync-2",
                "reissued_from_request_id": "menu-resync-1",
                "reissue_root_request_id": "menu-original",
                "choices": ["Investigate the signal", "Ignore it"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Investigate the signal", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Ignore it", "disabled": False},
                ],
            },
        }
        ctx.client.last_request_id = "menu-original"
        ctx.client.last_choices = ["Investigate the signal", "Ignore it"]

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert result.get("ok") is True
        assert not result.get("_stale_numeric_act")
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == [1]

    def test_numeric_act_refused_when_the_menu_shrank_under_the_same_labels(
        self, ctx,
    ):
        """Fail-closed on count as well as content: a shorter successor menu
        renumbers everything below the row that vanished."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 2}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-new",
                "choices": ["Open the door"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Open the door", "disabled": False},
                ],
            },
        }
        ctx.client.last_request_id = "menu-old"
        ctx.client.last_choices = ["Open the door", "Wait quietly"]

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert not result.get("success") and not result.get("ok")
        assert result["_stale_numeric_act"] is True
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_numeric_act_refused_when_rotated_choices_actually_changed(self, ctx):
        """Content drift is still refused: the id rotated AND the numbered
        options changed, so index N of the new menu is a different choice."""
        from vnflight.handlers import handle_act
        ctx.client._act_result = {"ok": True, "chosen": 1}
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "menu-new",
                "choices": ["Attack the guard", "Run away"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "Attack the guard", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "Run away", "disabled": False},
                ],
            },
        }
        # Caller last saw a genuinely different menu.
        ctx.client.last_request_id = "menu-old"
        ctx.client.last_choices = ["Open the door", "Wait quietly"]

        result = handle_act(ctx, {"target": "1", "wait": False})

        assert not result.get("success") and not result.get("ok")
        assert "state changed since you last looked" in result["error"]
        assert result["_stale_numeric_act"] is True
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_act_retries_once_after_connection_timeout_no_op(self, ctx):
        """A POST /command that times out at the transport layer never reached
        the bridge, so the choice is still pending and the click did not land.
        handle_act re-confirms the same pending and resubmits automatically —
        the fix for the "Connection failed: timed out" streaming no-op that
        previously forced the agent to retry by hand."""
        from vnflight.handlers import handle_act

        class TransportTimeoutClient(MockClient):
            def __init__(self):
                super().__init__()
                self._act_seq = [
                    {"ok": False, "error": "Connection failed: timed out"},
                    {"ok": True, "resolved_as": "choice", "chosen": 1},
                ]

            def act(self, target, _nonce=None):
                self.calls.append(("act", target))
                return self._act_seq.pop(0) if self._act_seq else {"ok": True}

        from vnflight.handlers import HandlerContext
        client = TransportTimeoutClient()
        # Same pending menu throughout: the transport timeout means the choice
        # was never consumed, so /state still shows it pending.
        client._state = _menu_state(
            "Investigate the signal", "Ignore it", request_id="menu-live")
        client.last_request_id = "menu-live"
        client.last_choices = ["Investigate the signal", "Ignore it"]
        c = HandlerContext(client=client)

        result = handle_act(c, {
            "target": "1",
            "wait": False,
            "_mcp_server_instance_id": "server-handler-retry",
            "_mcp_call_id": "call-handler-retry",
            "_mcp_original_target": "1",
        })

        assert result.get("ok") is True
        assert result.get("_retried_after_transport_timeout") is True
        assert "Connection failed" in str(result.get("_original_error", ""))
        acted = [t for name, t in client.action_calls if name == "act"]
        assert acted == [1, 1]  # first no-op, then the successful resubmit
        invocations = [
            call[1]["invocation"]
            for call in client.calls
            if call[0] == "act_transaction"
        ]
        assert [item["attempt_kind"] for item in invocations] == [
            "initial", "transport_retry",
        ]
        assert {item["call_id"] for item in invocations} == {
            "call-handler-retry",
        }

    def test_act_does_not_retry_transport_timeout_when_pending_cleared(self, ctx):
        """If the pending changed/cleared after a transport timeout, the act
        may actually have landed — do NOT resubmit (avoid a double-act)."""
        from vnflight.handlers import handle_act, HandlerContext

        class TransportTimeoutClient(MockClient):
            def act(self, target, _nonce=None):
                self.calls.append(("act", target))
                return {"ok": False, "error": "Connection failed: timed out"}

        client = TransportTimeoutClient()
        # Pre-act render shows the menu, but by the verify re-fetch the pending
        # is gone (the click landed / scene advanced) — same object is returned
        # each state() call, so emulate "cleared" with no pending_request.
        client._state = {
            "status": "running",
            "pending_request": None,
            "game_state": {"interactions": []},
        }
        client.last_request_id = "menu-live"
        client.last_choices = ["Investigate the signal", "Ignore it"]
        c = HandlerContext(client=client)

        # target by label so the numeric staleness path is not involved.
        result = handle_act(
            c, {"target": "Investigate the signal", "wait": False})

        assert not result.get("ok")
        acted = [t for name, t in client.action_calls if name == "act"]
        assert len(acted) == 1  # no automatic resubmit

    def test_transport_retry_reuses_the_original_nonce(self):
        """The transport-timeout retry must carry the SAME idempotency nonce
        as the original attempt so the bridge dedups the replay instead of
        double-acting.  Drives a real BridgeClient through handle_act with the
        transport mocked to fail once then succeed; captures both POST
        /command bodies and asserts their nonces are identical."""
        from vnflight.client import BridgeClient
        from vnflight.handlers import handle_act, HandlerContext

        menu_state = {
            "status": "waiting_for_input",
            "event_counter": 5,
            "pending_request": {
                "type": "choice_request",
                "id": "menu-live",
                "choices": ["A", "B"],
            },
            "game_state": {
                "interactions": [
                    {"source": "choice", "type": "choice", "index": 1,
                     "display_label": "A", "disabled": False},
                    {"source": "choice", "type": "choice", "index": 2,
                     "display_label": "B", "disabled": False},
                ],
            },
        }

        class CaptureClient(BridgeClient):
            def __init__(self):
                super().__init__(bridge_url="http://bridge")
                self.slot_prefix = "/1"
                self.last_request_id = "menu-live"
                self.last_choices = ["A", "B"]
                self.command_bodies = []
                self._command_posts = 0

            def _post(self, path, data, timeout=5.0):
                if path == "/command":
                    self.command_bodies.append(data)
                    self._command_posts += 1
                    if self._command_posts == 1:
                        # First POST: transport failure, no response reached
                        # the bridge (the click did NOT land).
                        return 0, {"error": "Connection failed: timed out"}
                    return 200, {"status": "accepted",
                                 "message": "Command 'act' submitted."}
                return 200, {}

            def _get(self, path, params=None, timeout=5.0):
                if path.endswith("/screen"):
                    return 200, {"screen": None}
                if path.endswith("/game_state"):
                    return 200, {"game_state": menu_state["game_state"]}
                if path.endswith("/pending"):
                    return 200, {"pending": menu_state["pending_request"]}
                if path.endswith("/transcript"):
                    return 200, {"transcript": []}
                return 200, dict(menu_state)

            def _wait_command_result(self, command, timeout=3.0, **kwargs):
                if command == "act":
                    return {"ok": True, "resolved_as": "choice", "chosen": 1}
                return None

        client = CaptureClient()
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {
            "target": "1",
            "wait": False,
            "_mcp_server_instance_id": "server-transport",
            "_mcp_call_id": "call-transport",
            "_mcp_original_target": "1",
        })

        assert result.get("_retried_after_transport_timeout") is True
        # Two POST /command bodies: the failed original and the resubmit.
        assert len(client.command_bodies) == 2
        first_nonce = client.command_bodies[0].get("nonce")
        retry_nonce = client.command_bodies[1].get("nonce")
        assert first_nonce  # a nonce was minted for the original act
        assert retry_nonce == first_nonce  # retry REUSES it (dedup key)
        assert client.command_bodies[0]["_invocation"] == {
            "server_instance_id": "server-transport",
            "call_id": "call-transport",
            "original_target": "1",
            "attempt_kind": "initial",
        }
        assert client.command_bodies[1]["_invocation"] == {
            "server_instance_id": "server-transport",
            "call_id": "call-transport",
            "original_target": "1",
            "attempt_kind": "initial",
        }

    def test_short_string_target_requires_exact_visible_match(self):
        from vnflight.handlers import _target_was_visible_before_act

        rendered = {
            "_data": {
                "pending": {
                    "choices": ["Inventory", "I"],
                },
            },
        }

        assert _target_was_visible_before_act("n", rendered) is False
        assert _target_was_visible_before_act("I", rendered) is True
        assert _target_was_visible_before_act("Inv", rendered) is True

    def test_button_story_with_stale_buttons_prefers_current_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        stale_map_buttons = [
            {
                "label": "Howler's Dell",
                "screen": "map",
                "actions": ["Jump"],
            },
            {
                "label": "Old Pagos",
                "screen": "map",
                "actions": ["Jump"],
            },
        ]
        map_state = {
            "status": "running",
            "screen": {
                "buttons": stale_map_buttons,
            },
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "howlers-dell",
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
            },
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "You arrive at the village."}],
            pending=None,
            screen={"buttons": stale_map_buttons},
        )
        states = [map_state, map_state, map_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "1", "wait": True})

        assert "I go to the main square" in result["pending"]
        assert "Howler's Dell" not in result.get("buttons", "")
        # The stale BUTTON SHELL is what must be replaced by the current
        # pending — the arrival narration the action produced is script
        # output and is kept.  (This assertion used to require the narration
        # to disappear, which is the same drop that ate Echoes' map-travel
        # scenes; only the screen scrape may be discarded here.)
        assert result.get("text", "").count("You arrive at the village") == 1

    def test_map_button_story_with_stale_state_drains_followup_wait(self, ctx, monkeypatch):
        from vnflight import handlers
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
            "screen": "map_display",
            "label": "↓ [map: Howler's Dell — 30m]",
        }
        stale_map_buttons = [
            {
                "label": "Howler's Dell",
                "screen": "map",
                "actions": ["Jump"],
            },
            {
                "label": "Old Pagos",
                "screen": "map",
                "actions": ["Jump"],
            },
        ]
        map_state = {
            "status": "running",
            "screen": {
                "buttons": stale_map_buttons,
            },
        }
        stale_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "western-crossroads",
                "choices": [
                    "I approach the western signpost.",
                    "I search the area.",
                ],
            },
        }
        new_pending = {
            "type": "choice_request",
            "id": "howlers-dell",
            "choices": [
                "I go to the main square.",
                "I go to Elpis, the druidess.",
            ],
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": new_pending,
        }
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "You ride south."}],
                pending=None,
                screen={"buttons": stale_map_buttons},
            ),
            MockWaitResult(
                events=[{"type": "narration", "text": "You arrive at the village."}],
                pending=new_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return wait_results.pop(0)

        states = [map_state, map_state, stale_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        def stale_rendered_state_change(*args, **kwargs):
            return handlers.handle_state(ctx, {"brief": False})

        ctx.client.wait = delayed_wait
        ctx.client.state = delayed_state
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            stale_rendered_state_change,
        )

        result = handlers.handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 2
        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_button_story_with_same_pending_prefers_current_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        old_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "western-crossroads",
                "choices": [
                    "I approach the western signpost.",
                    "I search the area.",
                ],
            },
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "howlers-dell",
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
            },
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "You arrive at the village."}],
            pending=old_state["pending_request"],
        )
        states = [old_state, old_state, old_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Travel", "wait": True})

        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_button_story_from_overlay_pending_prefers_current_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "interaction_type": "other",
        }
        map_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Howler's Dell", "screen": "map", "actions": ["Jump"]},
                    {"label": "Old Pagos", "screen": "map", "actions": ["Jump"]},
                ],
            },
        }
        stale_pending = {
            "type": "choice_request",
            "id": "western-crossroads",
            "choices": [
                "I approach the western signpost.",
                "I search the area.",
            ],
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "howlers-dell",
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
            },
        }
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "You arrive at the village."}],
            pending=stale_pending,
        )
        states = [map_state, map_state, map_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_button_overlay_pending_without_story_prefers_current_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "interaction_type": "other",
        }
        map_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Howler's Dell", "screen": "map", "actions": ["Jump"]},
                    {"label": "Old Pagos", "screen": "map", "actions": ["Jump"]},
                ],
            },
        }
        stale_pending = {
            "type": "choice_request",
            "id": "western-crossroads",
            "choices": [
                "I approach the western signpost.",
                "I search the area.",
            ],
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "howlers-dell",
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
            },
        }
        ctx.client._wait_result = MockWaitResult(
            events=[],
            pending=stale_pending,
        )
        states = [map_state, map_state, map_state, new_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_button_overlay_pending_drains_followup_wait_when_state_stays_stale(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
        }
        map_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Howler's Dell", "screen": "map", "actions": ["Jump"]},
                    {"label": "Old Pagos", "screen": "map", "actions": ["Jump"]},
                ],
            },
        }
        stale_pending = {
            "type": "choice_request",
            "id": "western-crossroads",
            "choices": [
                "I approach the western signpost.",
                "I search the area.",
            ],
        }
        new_pending = {
            "type": "choice_request",
            "id": "howlers-dell",
            "choices": [
                "I go to the main square.",
                "I go to Elpis, the druidess.",
            ],
        }
        stale_state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": new_pending,
        }
        wait_results = [
            MockWaitResult(events=[], pending=stale_pending),
            MockWaitResult(
                events=[{"type": "narration", "text": "You arrive at the village."}],
                pending=new_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return wait_results.pop(0)

        states = [
            map_state,
            map_state,
            stale_state,
            stale_state,
            stale_state,
            stale_state,
            new_state,
        ]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.wait = delayed_wait
        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 2
        assert wait_calls[1][1]["min_wait"] == 1
        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_map_button_drains_followup_wait_when_pre_state_is_stale_pending(self, ctx, monkeypatch):
        from vnflight import handlers
        stale_pending = {
            "type": "choice_request",
            "id": "western-crossroads",
            "choices": [
                "I approach the western signpost.",
                "I search the area.",
            ],
        }
        new_pending = {
            "type": "choice_request",
            "id": "howlers-dell",
            "choices": [
                "I go to the main square.",
                "I go to Elpis, the druidess.",
            ],
        }
        stale_state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": new_pending,
        }
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
            "screen": "map_display",
            "label": "↓ [map: Howler's Dell — 30m]",
        }
        wait_results = [
            MockWaitResult(
                events=[{"type": "narration", "text": "You ride south."}],
                pending=stale_pending,
            ),
            MockWaitResult(
                events=[{"type": "narration", "text": "You arrive at the village."}],
                pending=new_pending,
            ),
        ]

        def delayed_wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            return wait_results.pop(0)

        states = [stale_state, stale_state, stale_state, stale_state, new_state]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        def stale_rendered_state_change(*args, **kwargs):
            return handlers.handle_state(ctx, {"brief": False})

        ctx.client.wait = delayed_wait
        ctx.client.state = delayed_state
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            stale_rendered_state_change,
        )

        result = handlers.handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        wait_calls = [call for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_calls) == 2
        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_map_button_uses_shared_stale_pending_drain(self, ctx, monkeypatch):
        from vnflight import handlers

        stale_pending = {
            "type": "choice_request",
            "id": "western-crossroads",
            "choices": [
                "I approach the western signpost.",
                "I search the area.",
            ],
        }
        new_pending = {
            "type": "choice_request",
            "id": "howlers-dell",
            "choices": [
                "I go to the main square.",
                "I go to Elpis, the druidess.",
            ],
        }
        stale_state = {
            "status": "waiting_for_input",
            "pending_request": stale_pending,
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": new_pending,
        }
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "other",
            "screen": "map_display",
            "label": "[map: Howler's Dell]",
        }
        drained = {"value": False}
        wait_calls = {"count": 0}

        def wait(timeout=60, **kw):
            ctx.client.calls.append(("wait", {"timeout": timeout, **kw}))
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                return MockWaitResult(
                    events=[],
                    pending=stale_pending,
                )
            events = ctx.client._prefetched_events
            ctx.client._prefetched_events = []
            return MockWaitResult(
                events=events,
                pending=getattr(ctx.client, "_last_poll_pending", None),
            )

        def poll(timeout=0):
            ctx.client.calls.append(("poll", timeout))
            drained["value"] = True
            return [
                {"type": "narration", "text": "You ride south."},
                {
                    "type": "choice_request",
                    "id": "howlers-dell",
                    "choices": new_pending["choices"],
                },
            ]

        def pending():
            ctx.client.calls.append(("pending", {}))
            return new_pending if drained["value"] else stale_pending

        def state():
            ctx.client.calls.append(("state", {}))
            if drained["value"]:
                return {
                    "status": "waiting_for_input",
                    "pending_request": dict(new_pending),
                }
            return stale_state

        ctx.client.wait = wait
        ctx.client.poll = poll
        ctx.client.pending = pending
        ctx.client.state = state
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            lambda *args, **kwargs: handlers.handle_state(ctx, {"brief": False}),
        )

        result = handlers.handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        assert [call[0] for call in ctx.client.calls].count("poll") == 1
        wait_call_args = [call[1] for call in ctx.client.calls if call[0] == "wait"]
        assert len(wait_call_args) == 2
        assert wait_call_args[1]["min_wait"] == 0
        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]

    def test_missing_screen_text_fallback_ignores_quick_menu_label_mismatch(self, ctx):
        from vnflight import handlers

        pending = {
            "type": "choice_request",
            "id": "arrival",
            "choices": ["Who knows, maybe it won’t be back."],
        }
        result = {
            "pending": "CHOICE REQUIRED\n1. Who knows, maybe it won’t be back.",
            "_data": {
                "pending": {
                    "type": "choice",
                    "choices": [{"label": "Who knows, maybe it won’t be back."}],
                },
                "_pending_raw": pending,
            },
        }

        def get_screen(path, timeout=2.0):
            assert path == "/screen"
            return (200, {
                "screen": {
                    "texts": ["The path is clear, in a way."],
                    "buttons": [
                        {"label": "Inventory", "actions": ["ShowMenu"]},
                        {"label": "Travel", "actions": ["ShowMenu"]},
                    ],
                },
            })

        ctx.client._get = get_screen
        ctx.client._state = {"status": "waiting_for_input", "pending_request": pending}

        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert result["screen_text"] == "The path is clear, in a way."

    def test_missing_screen_text_fallback_claims_matching_prefetch(self, ctx):
        from vnflight import handlers

        pending = {
            "type": "choice_request",
            "id": "routing",
            "choices": ["Apply routing"],
        }
        result = {
            "pending": "CHOICE REQUIRED\n1. Apply routing",
            "_data": {
                "pending": {
                    "type": "choice",
                    "choices": [{"label": "Apply routing"}],
                },
                "_pending_raw": pending,
            },
        }
        duplicate = {
            "type": "screen_text",
            "texts": [
                "PENDING CHANGE",
                "+3/h",
                "Rerouting requires a 5-minute bus cycle.",
            ],
            "_source_id": "game-a",
            "_source_seq": 52,
            "_seq": 102,
            "action_id": 7,
        }
        unrelated = {
            "type": "narration", "text": "Keep this.", "_seq": 103,
        }
        malformed = {
            "type": "screen_text", "texts": 7,
            "_source_id": "game-a", "_source_seq": 52, "_seq": 102,
        }
        ctx.client._prefetched_events = [
            "opaque-prefetch", malformed, duplicate, unrelated,
        ]

        def get_screen(path, timeout=2.0):
            assert path == "/screen"
            return (200, {
                "screen": {
                    "texts": [
                        "POWER ROUTING",
                        "PENDING CHANGE",
                        "+3/h",
                        "Rerouting requires a 5-minute bus cycle.",
                    ],
                    "buttons": [
                        {"label": "Apply routing", "actions": ["Return"]},
                    ],
                    "_source_id": "game-a",
                    "_source_seq": 51,
                    "_seq": 101,
                },
            })

        ctx.client._get = get_screen
        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert "PENDING CHANGE" in result["screen_text"]
        assert ctx.client._prefetched_events == [
            "opaque-prefetch", malformed, unrelated,
        ]

    def test_passive_overlay_bare_choice_uses_occurrence_ledger(self, ctx):
        from vnflight import overlay_presentation
        import time

        from vnflight import handlers

        result = {
            "pending": "CHOICE REQUIRED\n1. Ask something else.",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Ask something else."},
            ]}},
        }
        old_screen = {
            "texts": ["OLD ANSWER A", "OLD ANSWER B"],
            "overlay_texts": ["OLD ANSWER A", "OLD ANSWER B"],
            "overlay_texts_by_screen": {
                "echo_terminal_live": ["OLD ANSWER A", "OLD ANSWER B"],
            },
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generation": "terminal-1",
            "buttons": [{
                "label": "Ask something else.", "actions": ["Return"],
            }],
        }
        overlay_presentation._book_passive_overlay_snapshot(ctx, old_screen)
        current_screen = {
            **old_screen,
            "texts": ["OLD ANSWER A", "OLD ANSWER B", "NEW ANSWER C"],
            "overlay_texts": [
                "OLD ANSWER A", "OLD ANSWER B", "NEW ANSWER C",
            ],
            "overlay_texts_by_screen": {
                "echo_terminal_live": [
                    "OLD ANSWER A", "OLD ANSWER B", "NEW ANSWER C",
                ],
            },
        }
        reads = []

        def get_screen(path, timeout=2.0):
            reads.append(path)
            return 200, {"screen": current_screen}

        def unexpected_state(*args, **kwargs):
            pytest.fail("passive snapshot must skip the state fallback")

        ctx.client._get = get_screen
        ctx.client.state = unexpected_state
        params = {"_result_deadline": time.time() + 0.05}

        passive_screen = handlers._refresh_missing_screen_text_from_state(
            ctx, result, params)
        assert "screen_text" not in result
        assert passive_screen is current_screen

        handlers._merge_passive_overlay_text(
            ctx,
            result,
            screen=passive_screen,
            deadline=params["_result_deadline"],
            sample_live=False,
        )

        assert result["screen_text"] == "NEW ANSWER C"
        assert "OLD ANSWER" not in result["screen_text"]
        assert len(result[handlers._OVERLAY_DELIVERIES_KEY]) == 1
        assert reads == ["/screen"]

    def test_legacy_passive_overlay_bare_choice_skips_cumulative_fallback(
        self, ctx,
    ):
        from vnflight import handlers

        result = {
            "pending": "CHOICE REQUIRED\n1. Continue",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Continue"},
            ]}},
        }
        ctx.client._get = lambda path, timeout=2.0: (200, {"screen": {
            "texts": ["OLD ROW", "NEW ROW"],
            "overlay_texts": ["OLD ROW", "NEW ROW"],
            "buttons": [{"label": "Continue", "actions": ["Return"]}],
        }})

        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert "screen_text" not in result

    def test_blocking_overlay_remains_eligible_for_bare_choice_fallback(
        self, ctx,
    ):
        from vnflight import handlers

        result = {
            "pending": "CHOICE REQUIRED\n1. Continue",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Continue"},
            ]}},
        }
        ctx.client._get = lambda path, timeout=2.0: (200, {"screen": {
            "texts": ["MODAL DETAILS"],
            "overlay_texts": ["MODAL DETAILS"],
            "overlay_active": True,
            "buttons": [{"label": "Continue", "actions": ["Return"]}],
        }})

        handlers._refresh_missing_screen_text_from_state(
            ctx,
            result,
            {},
            pre_screen_presentation=((), (), "MODAL DETAILS"),
        )

        assert result["screen_text"] == "MODAL DETAILS"

    def test_unchanged_ordinary_screen_is_not_replayed_for_successor_menu(
        self, ctx,
    ):
        from vnflight import handlers

        screen = {
            "texts": ["STATION STATUS", "ARIA SOURCE AUDIT 82%"],
            "screens": ["nvl", "quick_menu"],
            "modal_screens": ["nvl"],
            "buttons": [{"label": "Back.", "actions": ["ChoiceReturn"]}],
        }
        # The parent act exposed its choices alongside narration, not the
        # persistent rail. A raw actionable hint is not a presentation receipt.
        handlers._remember_delivered_actionable_screen(
            ctx,
            {"text": "[ARIA] Priority changed.", "pending": "1. Wait"},
            screen,
        )
        assert ctx.overlay.ordinary_screen_presentation_receipt is None

        submenu = {
            "pending": "CHOICE REQUIRED\n1. Back.",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Back."},
            ]}},
        }
        ctx.client._get = lambda path, timeout=2.0: (200, {"screen": screen})

        handlers._refresh_missing_screen_text_from_state(
            ctx,
            submenu,
            {},
            pre_screen_presentation=ctx.overlay.ordinary_screen_presentation_receipt,
        )
        assert submenu["screen_text"] == (
            "STATION STATUS\nARIA SOURCE AUDIT 82%")
        receipt = ctx.overlay.ordinary_screen_presentation_receipt
        assert receipt == (
            ("nvl", "quick_menu"),
            ("nvl",),
            "STATION STATUS\nARIA SOURCE AUDIT 82%",
        )

        parent = {
            "pending": "CHOICE REQUIRED\n1. Wait",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Wait"},
            ]}},
        }
        screen["buttons"] = [
            {"label": "Wait", "actions": ["ChoiceReturn"]},
        ]
        handlers._refresh_missing_screen_text_from_state(
            ctx, parent, {}, pre_screen_presentation=receipt)

        assert "screen_text" not in parent

    def test_changed_ordinary_screen_remains_eligible_for_bare_choice_fallback(
        self, ctx,
    ):
        from vnflight import handlers

        result = {
            "pending": "CHOICE REQUIRED\n1. Back.",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Back."},
            ]}},
        }
        ctx.client._get = lambda path, timeout=2.0: (200, {"screen": {
            "texts": ["STATION STATUS", "ARIA SOURCE AUDIT 83%"],
            "screens": ["nvl", "quick_menu"],
            "modal_screens": ["nvl"],
            "buttons": [{"label": "Back.", "actions": ["ChoiceReturn"]}],
        }})

        handlers._refresh_missing_screen_text_from_state(
            ctx,
            result,
            {},
            pre_screen_presentation=(
                ("nvl", "quick_menu"),
                ("nvl",),
                "STATION STATUS\nARIA SOURCE AUDIT 82%",
            ),
        )

        assert result["screen_text"] == (
            "STATION STATUS\nARIA SOURCE AUDIT 83%")

    def test_identical_text_on_a_different_ordinary_screen_still_renders(
        self, ctx,
    ):
        from vnflight import handlers

        result = {
            "pending": "CHOICE REQUIRED\n1. Continue",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Continue"},
            ]}},
        }
        ctx.client._get = lambda path, timeout=2.0: (200, {"screen": {
            "texts": ["The same words."],
            "screens": ["chapter_two"],
            "buttons": [{"label": "Continue", "actions": ["Return"]}],
        }})

        handlers._refresh_missing_screen_text_from_state(
            ctx,
            result,
            {},
            pre_screen_presentation=(
                ("chapter_one",), (), "The same words."),
        )

        assert result["screen_text"] == "The same words."

    def test_missing_screen_clears_receipt_but_preserves_actionable_hint(
        self, ctx,
    ):
        from vnflight import handlers

        actionable = {
            "texts": ["Stale panel"],
        }
        ctx.client._last_delivered_actionable_screen = actionable
        ctx.client._last_delivered_actionable_screen_signature = ("stale",)
        ctx.overlay.ordinary_screen_presentation_receipt = (
            ("station_status",), (), "Stale panel",
        )

        handlers._remember_delivered_actionable_screen(
            ctx, {"pending": "1. Continue"}, None)

        assert ctx.client._last_delivered_actionable_screen is actionable
        assert ctx.client._last_delivered_actionable_screen_signature == (
            "stale",
        )
        assert ctx.overlay.ordinary_screen_presentation_receipt is None

    def test_screen_owner_transition_retires_receipt_before_reopen(self, ctx):
        from vnflight import handlers

        screen_a = {
            "texts": ["STATION STATUS"],
            "screens": ["station_status"],
            "buttons": [{"label": "Back", "actions": ["Return"]}],
        }
        handlers._remember_delivered_actionable_screen(
            ctx,
            {"screen_text": "STATION STATUS", "pending": "1. Back"},
            screen_a,
        )
        assert ctx.overlay.ordinary_screen_presentation_receipt == (
            ("station_status",), (), "STATION STATUS",
        )

        # Screen B exposes choices alongside narration, without rendering its
        # full body. The owner transition must still retire A's receipt.
        screen_b = {
            "texts": ["GENERATOR CONTROL"],
            "screens": ["generator_control"],
            "buttons": [{"label": "Back", "actions": ["Return"]}],
        }
        handlers._remember_delivered_actionable_screen(
            ctx,
            {"text": "The relays settle.", "pending": "1. Back"},
            screen_b,
        )
        assert ctx.overlay.ordinary_screen_presentation_receipt is None

        reopened = {
            "pending": "CHOICE REQUIRED\n1. Back",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Back"},
            ]}},
        }
        ctx.client._get = lambda path, timeout=2.0: (200, {
            "screen": screen_a,
        })
        handlers._refresh_missing_screen_text_from_state(
            ctx,
            reopened,
            {},
            pre_screen_presentation=ctx.overlay.ordinary_screen_presentation_receipt,
        )

        assert reopened["screen_text"] == "STATION STATUS"

    def test_internal_state_probe_does_not_forge_or_clear_presentation_receipt(
        self, ctx,
    ):
        from vnflight import handlers

        receipt = (("nvl",), ("nvl",), "STATION STATUS")
        ctx.overlay.ordinary_screen_presentation_receipt = receipt
        handlers._remember_delivered_actionable_screen(
            ctx,
            {"screen_text": "A DIFFERENT UNSHOWN BODY", "pending": "1. Go"},
            {
                "texts": ["A DIFFERENT UNSHOWN BODY"],
                "screens": ["other"],
                "buttons": [{"label": "Go", "actions": ["Return"]}],
            },
            record_presentation=False,
        )
        assert ctx.overlay.ordinary_screen_presentation_receipt == receipt

        handlers._remember_delivered_actionable_screen(
            ctx,
            {"pending": "1. Go"},
            None,
            record_presentation=False,
        )
        assert ctx.overlay.ordinary_screen_presentation_receipt == receipt

    def test_rendered_screen_fallback_suppresses_later_durable_replay(self, ctx):
        from vnflight import handlers

        screen = {
            "texts": ["POWER ROUTING", "PENDING CHANGE"],
            "_source_id": "game-a",
            "_source_seq": 51,
            "_seq": 101,
        }
        handlers._claim_prefetched_screen_text_snapshot(
            ctx, screen, "POWER ROUTING\nPENDING CHANGE")
        replay = {
            "type": "screen_text",
            "texts": ["POWER ROUTING", "PENDING CHANGE"],
            "_source_id": "game-a",
            "_source_seq": 52,
            "_seq": 102,
        }
        later = {
            "type": "screen_text",
            "texts": ["POWER ROUTING", "PENDING CHANGE"],
            "_source_id": "game-a",
            "_source_seq": 60,
            "_seq": 110,
        }
        interleaved = {
            "type": "screen_text",
            "texts": ["A DIFFERENT SCREEN"],
            "_source_id": "game-b",
            "_source_seq": 2,
            # Same bridge receipt candidate, but not the rendered occurrence.
            "_seq": 102,
        }
        result = MockWaitResult(events=[interleaved, replay, later])

        handlers._drop_rendered_screen_occurrence_events(ctx, result)

        assert result.events == [interleaved, later]

    def test_missing_screen_text_fallback_does_not_claim_by_text_alone(self, ctx):
        from vnflight import handlers

        result = {
            "pending": "CHOICE REQUIRED\n1. Continue",
            "_data": {"pending": {"type": "choice", "choices": [
                {"label": "Continue"},
            ]}},
        }
        earlier_occurrence = {
            "type": "screen_text",
            "texts": ["SAME ROW"],
            "_source_id": "game-a",
            "_source_seq": 40,
            "_seq": 90,
        }
        ctx.client._prefetched_events = [earlier_occurrence]
        ctx.client._get = lambda path, timeout=2.0: (200, {"screen": {
            "texts": ["SAME ROW"],
            "buttons": [{"label": "Continue", "actions": ["Return"]}],
            "_source_id": "game-a",
            "_source_seq": 51,
            "_seq": 101,
        }})

        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert result["screen_text"] == "SAME ROW"
        assert ctx.client._prefetched_events == [earlier_occurrence]

    def test_missing_screen_text_fallback_drops_input_prompt_echo(self, ctx):
        from vnflight import handlers

        prompt = "Which place are you asking about?"
        pending = {
            "type": "input_request",
            "id": "lookup",
            "prompt": prompt,
        }
        result = {
            "pending": f"--- INPUT REQUIRED ---\n{prompt}",
            "_data": {
                "pending": {"type": "input", "prompt": prompt},
                "_pending_raw": pending,
            },
        }

        def get_screen(path, timeout=2.0):
            assert path == "/screen"
            return (200, {
                "screen": {
                    "texts": [prompt],
                    "buttons": [
                        {"label": "Confirm", "actions": ["Return"]},
                    ],
                },
            })

        ctx.client._get = get_screen

        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert "screen_text" not in result

    def test_missing_screen_text_fallback_rejects_mismatched_choice_screen(self, ctx):
        from vnflight import handlers

        pending = {
            "type": "choice_request",
            "id": "arrival",
            "choices": ["Current choice."],
        }
        result = {
            "pending": "CHOICE REQUIRED\n1. Current choice.",
            "_data": {
                "pending": {
                    "type": "choice",
                    "choices": [{"label": "Current choice."}],
                },
                "_pending_raw": pending,
            },
        }

        def get_screen(path, timeout=2.0):
            assert path == "/screen"
            return (200, {
                "screen": {
                    "texts": ["Stale previous scene."],
                    "buttons": [
                        {"label": "Stale choice.", "actions": ["ChoiceReturn"]},
                    ],
                },
            })

        ctx.client._get = get_screen

        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert "screen_text" not in result

    def test_missing_screen_text_fallback_matches_normalized_choice_labels(self, ctx):
        from vnflight import handlers

        pending = {
            "type": "choice_request",
            "id": "arrival",
            "choices": ["“I’m always ready to do the right thing.”"],
        }
        result = {
            "pending": "CHOICE REQUIRED\n1. “I’m always ready to do the right thing.”",
            "_data": {
                "pending": {
                    "type": "choice",
                    "choices": [{"label": "“I’m always ready to do the right thing.”"}],
                },
                "_pending_raw": pending,
            },
        }

        def get_screen(path, timeout=2.0):
            assert path == "/screen"
            return (200, {
                "screen": {
                    "texts": ["The old druid welcomes you."],
                    "buttons": [
                        {
                            "label": "I’m always ready to do the right thing.",
                            "actions": ["ChoiceReturn"],
                        },
                    ],
                },
            })

        ctx.client._get = get_screen

        handlers._refresh_missing_screen_text_from_state(ctx, result, {})

        assert result["screen_text"] == "The old druid welcomes you."

    def test_button_only_nav_waits_past_stale_underlay_pending(self, ctx):
        from vnflight.handlers import handle_act
        ctx.client._act_result = {
            "ok": True,
            "resolved_as": "button",
            "interaction_type": "nav",
        }
        map_state = {
            "status": "running",
            "screen": {
                "buttons": [
                    {"label": "Howler's Dell", "screen": "map", "actions": ["Jump"]},
                    {"label": "Old Pagos", "screen": "map", "actions": ["Jump"]},
                ],
            },
        }
        stale_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "western-crossroads",
                "choices": [
                    "I approach the western signpost.",
                    "I search the area.",
                ],
            },
        }
        new_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "howlers-dell",
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
            },
        }
        states = [
            map_state,
            map_state,
            stale_state,
            stale_state,
            stale_state,
            new_state,
            new_state,
            new_state,
        ]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else new_state

        ctx.client.state = delayed_state

        result = handle_act(ctx, {"target": "Howler's Dell", "wait": True})

        assert ("wait", {"timeout": 60, "_min_wait": 8}) not in ctx.client.calls
        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]


# ---------------------------------------------------------------------------
# Fleet R66 defect #1 — act(target="KIT") silently fuzzy-matched the
# unrelated story choice "The signal analysis toolkit..." because the old
# substring tier accepted any lone match with no regard for what else was on
# the surface.  The fix is category-aware precedence: an exact normalized
# match wins outright when it names exactly one category (button/control vs.
# story choice); a tie across categories, or a fuzzy match that reaches more
# than one interaction, refuses with the candidate list instead of guessing.
# Fail closed throughout: a refusal never POSTs an act.
# ---------------------------------------------------------------------------

def _r66_surface_state(request_id="signal-choice"):
    """echo66-04's reconstructed surface: a plain 3-option story choice
    (choice 1 mentions "toolkit") with a KIT button also on screen."""
    return {
        "status": "waiting_for_input",
        "pending_request": {
            "type": "choice_request",
            "id": request_id,
            "choices": [
                "The signal analysis toolkit — pull the waveform apart "
                "layer by layer.",
                "The spacetime equations — if this is temporal, there "
                "must be a mechanism.",
                "ARIA's source logs — whatever this is, it came through "
                "our hardware.",
            ],
        },
        "game_state": {
            "interactions": [
                {"source": "choice", "type": "choice", "index": 1,
                 "display_label": (
                     "The signal analysis toolkit — pull the waveform "
                     "apart layer by layer."),
                 "disabled": False},
                {"source": "choice", "type": "choice", "index": 2,
                 "display_label": (
                     "The spacetime equations — if this is temporal, "
                     "there must be a mechanism."),
                 "disabled": False},
                {"source": "choice", "type": "choice", "index": 3,
                 "display_label": "ARIA's source logs — whatever this "
                                   "is, it came through our hardware.",
                 "disabled": False},
                {"id": "hud:KIT", "source": "button", "type": "other",
                 "index": 4, "display_label": "KIT", "disabled": False,
                 "screen": "hud"},
            ],
            "screen_buttons": [
                {"label": "KIT", "screen": "hud", "index": 4},
            ],
        },
        "screen": {
            "type": "screen_content",
            "screens": ["hud"],
            "buttons": [{"label": "KIT", "screen": "hud"}],
        },
    }


class TestActLabelPrecedence:

    def test_empty_modal_receipt_does_not_trap_close(self):
        from vnflight.handlers import HandlerContext, handle_tool

        client = ScriptedBridgeClient(state={
            "status": "waiting_for_input", "pending_request": _modal_hub_pending(),
            "game_state": _modal_panel_game_state(),
        }, screen=_modal_panel_screen())
        client._track_action_nonce("panel", 20)
        client.set_transaction({
            "action_nonce": "panel", "action_id": 20,
            "transaction_state": "applied", "events": [], "admission_open": True,
        }, nonce="panel")
        ctx = HandlerContext(client=client)
        handle_tool(ctx, "state", {"brief": False})
        out = handle_tool(ctx, "act", {"target": "CLOSE", "timeout": 5, "wait": False})
        assert not out.get("_act_not_submitted"), out
        assert any(path == "/command" for path, _ in client.http_posts)

    def test_pre_action_drain_preserves_unowned_prefetch_order(self):
        from vnflight.handlers import HandlerContext, handle_tool, render_tool_result_text

        state = _menu_state("Proceed")
        client = ScriptedBridgeClient(state=state, transcript=[
            {"type": "narration", "text": "FIRST", "_seq": 10},
            {"type": "command_result", "command": "advance", "success": False, "_seq": 11},
        ])
        client._wait_command_result("advance", timeout=0.1)
        client._track_action_nonce("previous", 20)
        client.set_transaction({
            "action_nonce": "previous", "action_id": 20,
            "transaction_state": "settled", "delivery_end": 12,
            "events": [{"type": "narration", "text": "SECOND", "_seq": 12, "action_id": 20}],
            "settled_pending": state["pending_request"],
        }, nonce="previous")
        ctx = HandlerContext(client=client)
        out = handle_tool(ctx, "act", {"target": "Proceed", "timeout": 5})
        rendered = render_tool_result_text(out)
        assert out["_act_not_submitted"] is True
        assert rendered.index("FIRST") < rendered.index("SECOND")
        assert not client.http_posts
        again = render_tool_result_text(handle_tool(ctx, "wait", {"timeout": 0.1}))
        assert "FIRST" not in again and "SECOND" not in again

    @pytest.mark.parametrize("internal_act_wait", [False, True])
    def test_plain_wait_fetches_unowned_prefix_before_scoped_story(self, internal_act_wait):
        from vnflight.handlers import HandlerContext, handle_tool, render_tool_result_text

        state = _menu_state("Proceed")
        opening = {"type": "narration", "text": "Opening waveform analysis.", "_seq": 10}
        current = {"type": "narration", "text": "Current Comms response.",
                   "_seq": 20, "action_id": 2}
        client = ScriptedBridgeClient(state=state, transcript=[opening, current])
        client._track_action_nonce("comms", 2)
        client.set_transaction({
            "action_nonce": "comms", "action_id": 2,
            "transaction_state": "settled", "delivery_end": 20,
            "events": [current], "settled_pending": state["pending_request"],
        }, nonce="comms")
        ctx = HandlerContext(client=client)
        params = {"timeout": 1}
        if internal_act_wait:
            params.update(action_nonce="comms", _act_story_handback=True)
        first = render_tool_result_text(handle_tool(ctx, "wait", params))
        assert "Opening waveform analysis." in first
        assert first.index(opening["text"]) < first.index(current["text"])
        later = render_tool_result_text(handle_tool(ctx, "wait", {"timeout": 0.1}))
        assert opening["text"] not in later
        assert current["text"] not in later

    @pytest.mark.parametrize("fmt", ["text", "json"])
    @pytest.mark.parametrize("earlier_state", ["settled", "applied"])
    def test_act_hands_back_unread_previous_transaction_before_submission(self, fmt, earlier_state):
        import json
        from vnflight.handlers import HandlerContext, handle_tool, render_tool_result_text

        state = _menu_state("Run protocol review.")
        client = ScriptedBridgeClient(state=state)
        line = {"type": "narration", "text": "10 minutes to ask the question properly.",
                "_seq": 1905, "action_id": 21}
        client._track_action_nonce("empty-earlier", 20)
        client.set_transaction({
            "action_nonce": "empty-earlier", "action_id": 20,
            "transaction_state": earlier_state, "events": [], "admission_open": True,
            "settled_pending": state["pending_request"],
        }, nonce="empty-earlier")
        client._track_action_nonce("audit", 21)
        client.set_transaction({
            "action_nonce": "audit", "action_id": 21,
            "transaction_state": "settled", "events": [line],
            "delivery_end": 1905, "settled_pending": state["pending_request"],
        }, nonce="audit")
        ctx = HandlerContext(client=client)
        out = handle_tool(ctx, "act", {"target": "1", "timeout": 5, "format": fmt})
        assert out["_act_not_submitted"] is True
        rendered = json.dumps(out.get("story")) if fmt == "json" else render_tool_result_text(out)
        assert line["text"] in rendered
        assert not client.http_posts
        assert client.last_request_id == state["pending_request"]["id"]
        again = handle_tool(ctx, "wait", {"action_nonce": "audit", "timeout": 0.1})
        assert line["text"] not in render_tool_result_text(again)

    def test_empty_previous_receipt_does_not_block_panel_controls(self, ctx):
        from unittest.mock import patch
        from vnflight.handlers import handle_act

        ctx.client._next_auto_action_nonce = lambda: "panel"
        ctx.client._state = _r66_surface_state()
        ctx.client._act_result = {"ok": True, "success": True, "resolved_as": "button"}
        with patch("vnflight.handlers.handle_wait", return_value={"pending": True}):
            out = handle_act(ctx, {"target": "KIT", "wait": False})
        assert not out.get("_act_not_submitted")
        assert ("act", "KIT") in ctx.client.action_calls

    def test_previous_status_update_is_returned_before_submission(self, ctx):
        from unittest.mock import patch
        from vnflight.handlers import handle_act

        ctx.client._next_auto_action_nonce = lambda: "panel"
        with patch("vnflight.handlers.handle_wait", return_value={
                "status": "Inventory: +Coupling", "text": "(no new events)"}):
            out = handle_act(ctx, {"target": "KIT", "wait": False})
        assert out["_act_not_submitted"] is True
        assert out["status"] == "Inventory: +Coupling"
        assert not ctx.client.action_calls

    def test_absent_kit_does_not_submit_toolkit_story_choice(self, ctx):
        from vnflight.handlers import handle_act

        state = _r66_surface_state()
        state["game_state"]["interactions"] = state["game_state"]["interactions"][:3]
        state["game_state"]["screen_buttons"] = []
        state["screen"]["buttons"] = []
        ctx.client._state = state
        ctx.client._act_result = {
            "ok": False, "error": "No interaction matching KIT",
        }

        result = handle_act(ctx, {"target": "KIT", "wait": False})

        assert result.get("error"), result
        # The shim remains authoritative on this legacy surface. Never
        # rewrite KIT to the unrelated story label before submitting it.
        acted = [target for name, target in ctx.client.action_calls if name == "act"]
        assert acted == ["KIT"]

    def test_exact_kit_button_wins_over_fuzzy_toolkit_choice(self, ctx):
        """R66: the button is clicked, never the unrelated story choice."""
        from vnflight.handlers import handle_act

        ctx.client._state = _r66_surface_state()
        ctx.client._act_result = {
            "ok": True, "success": True, "resolved_as": "button",
        }

        result = handle_act(ctx, {"target": "KIT", "wait": False})

        assert result.get("ok") is True, result
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert acted == ["KIT"]

    def test_ambiguous_fuzzy_target_refuses_without_posting_an_act(self, ctx):
        """A short label that fuzzily reaches more than one candidate must
        refuse with the candidate list instead of guessing -- and must
        never POST anything."""
        from vnflight.handlers import handle_act

        state = _r66_surface_state()
        # Two nav buttons that only fuzzy-match "hab" -- no exact hit
        # anywhere, and the fuzzy pool spans more than one interaction.
        state["game_state"]["interactions"].extend([
            {"id": "map:HABITAT", "source": "button", "type": "other",
             "index": 5, "display_label": "HABITAT", "disabled": False,
             "screen": "map"},
            {"id": "map:HABITAT_LOG", "source": "button", "type": "other",
             "index": 6, "display_label": "HABITAT LOG", "disabled": False,
             "screen": "map"},
        ])
        ctx.client._state = state

        result = handle_act(ctx, {"target": "habi", "wait": False})

        assert result.get("error"), result
        assert "matches more than one thing" in result["error"]
        assert "HABITAT" in result["error"]
        assert all(name != "act" for name, _ in ctx.client.action_calls)

    def test_exact_story_choice_label_still_acts_normally(self, ctx):
        """A genuine, unambiguous exact choice label is unaffected."""
        from vnflight.handlers import handle_act

        ctx.client._state = _r66_surface_state()
        ctx.client._act_result = {
            "ok": True, "success": True, "resolved_as": "choice",
        }

        result = handle_act(ctx, {
            "target": "ARIA's source logs — whatever this is, it came "
                      "through our hardware.",
            "wait": False,
        })

        assert result.get("ok") is True, result
        acted = [t for name, t in ctx.client.action_calls if name == "act"]
        assert len(acted) == 1
        assert acted[0].startswith("ARIA's source logs")

    def test_exact_match_ties_across_categories_refuse(self, ctx):
        """A target that exactly matches BOTH a button and a story choice
        must refuse rather than silently preferring one category."""
        from vnflight.handlers import handle_act

        state = _r66_surface_state()
        state["pending_request"]["choices"].append("Close the hatch.")
        state["game_state"]["interactions"].extend([
            {"source": "choice", "type": "choice", "index": 4,
             "display_label": "Close the hatch.", "disabled": False},
            {"id": "hud:close", "source": "button", "type": "other",
             "index": 5, "display_label": "Close the hatch.",
             "disabled": False, "screen": "hud"},
        ])
        ctx.client._state = state

        result = handle_act(ctx, {"target": "Close the hatch", "wait": False})

        assert result.get("error"), result
        assert "matches more than one thing" in result["error"]
        assert all(name != "act" for name, _ in ctx.client.action_calls)


# ---------------------------------------------------------------------------
# handle_screenshot
# ---------------------------------------------------------------------------

class TestHandleScreenshot:

    def test_expired_deadline_does_not_capture(self, ctx):
        from vnflight.handlers import handle_screenshot
        ctx.client.screenshot = lambda: pytest.fail("Capture after deadline")
        assert "error" in handle_screenshot(ctx, {"_result_deadline": 0})

    def test_returns_base64(self, ctx):
        from vnflight.handlers import handle_screenshot
        ctx.client._screenshot = "iVBORw0KGgo="
        result = handle_screenshot(ctx, {})
        assert result["screenshot_base64"] == "iVBORw0KGgo="

    def test_no_screenshot(self, ctx):
        from vnflight.handlers import handle_screenshot
        ctx.client._screenshot = None
        result = handle_screenshot(ctx, {})
        assert "error" in result

    def test_format_hook(self, ctx):
        from vnflight.handlers import handle_screenshot
        ctx.client._screenshot = "original"
        ctx.hooks.format_screenshot = lambda img: "resized"
        result = handle_screenshot(ctx, {})
        assert result["screenshot_base64"] == "resized"

    def test_public_screenshot_does_not_consume_deferred_story(self, ctx):
        from vnflight.handlers import handle_tool

        ctx.client._screenshot = "iVBORw0KGgo="
        ctx.overlay.pending_deliveries = [{
            "id": "row-1",
            "text": "SYSTEM BOOT... OK",
        }]

        result = handle_tool(ctx, "screenshot", {})

        assert result["screenshot_base64"] == "iVBORw0KGgo="
        assert "screen_text" not in result
        assert ctx.overlay.pending_deliveries == [{
            "id": "row-1",
            "text": "SYSTEM BOOT... OK",
        }]


# ---------------------------------------------------------------------------
# handle_input_text
# ---------------------------------------------------------------------------

class TestHandleInputText:

    def test_sends_text(self, ctx):
        from vnflight.handlers import handle_input_text
        result = handle_input_text(ctx, {"text": "Alex"})
        assert ("input_text", "Alex") in ctx.client.calls

    def test_before_action_hook_runs_before_input_text(self, ctx):
        from vnflight.handlers import handle_input_text
        calls = []

        def before_action(tool_name, payload):
            calls.append((tool_name, payload))
            calls.append(("client_calls_before", list(ctx.client.action_calls)))

        ctx.hooks.before_action = before_action

        handle_input_text(ctx, {"text": "Alex"})

        assert calls[0] == ("input_text", {"text": "Alex", "wait": True})
        assert calls[1] == ("client_calls_before", [])
        assert ctx.client.action_calls[0] == ("input_text", "Alex")

    def test_input_text_waits_by_default(self, ctx):
        from vnflight.handlers import handle_input_text
        result = handle_input_text(ctx, {"text": "Alex"})
        called = [name for name, _ in ctx.client.action_calls]
        assert called.count("input_text") == 1
        assert "wait" in called
        assert "wait" in result

    @pytest.mark.parametrize("falsy", [False, "false", "0", "no", "off"])
    def test_input_text_wait_false_skips_followup(self, ctx, falsy):
        from vnflight.handlers import handle_input_text
        result = handle_input_text(ctx, {"text": "Alex", "wait": falsy})
        called = [name for name, _ in ctx.client.action_calls]
        assert called.count("input_text") == 1
        assert "wait" not in called
        assert "wait" not in result

    def test_input_text_runs_advertised_after_input_hook(self, ctx):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {"custom_commands": ["after_input_text"]}
        ctx.client._command_results["after_input_text"] = {
            "success": True,
            "handled": True,
            "auto_confirmed": True,
            "resolved_as": "button",
            "label": "Confirm",
        }

        result = handle_input_text(ctx, {"text": "Tatius"})

        assert result["_auto_confirmed"] is True
        assert ("input_text", "Tatius") in ctx.client.calls
        assert ("command", ("after_input_text", {
            "text": "Tatius", "_deadline": ANY,
        })) in ctx.client.calls
        assert result["_after_input_text"]["label"] == "Confirm"

    def test_input_text_reads_nested_game_state_custom_commands(self, ctx):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {
            "game_state": {"custom_commands": ["progress"]},
        }

        result = handle_input_text(ctx, {"text": "Alex"})

        assert "_auto_confirmed" not in result
        assert ("input_text", "Alex") in ctx.client.calls
        assert ("command", ("after_input_text", {"text": "Alex"})) not in ctx.client.calls

    def test_input_text_skips_hook_when_not_advertised(self, ctx):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {
            "custom_commands": ["progress"],
            "screen": {
                "screens": ["confirm"],
                "buttons": [
                    {"label": "Confirm", "screen": "confirm"},
                ],
            },
        }

        result = handle_input_text(ctx, {"text": "Alex"})

        assert "_auto_confirmed" not in result
        assert ("input_text", "Alex") in ctx.client.calls
        assert ("command", ("after_input_text", {"text": "Alex"})) not in ctx.client.calls

    def test_input_text_ignores_unknown_after_input_hook(self, ctx):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {}
        ctx.client._command_results["after_input_text"] = {
            "success": False,
            "error": "Unknown command",
        }

        result = handle_input_text(ctx, {"text": "Alex"})

        assert "_after_input_text" not in result
        assert "_after_input_text_error" not in result
        assert ("command", ("after_input_text", {
            "text": "Alex", "_deadline": ANY,
        })) in ctx.client.calls

    def test_input_text_records_after_input_hook_error(self, ctx):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {"custom_commands": ["after_input_text"]}
        ctx.client._command_results["after_input_text"] = {
            "success": False,
            "error": "boom",
        }

        result = handle_input_text(ctx, {"text": "Tatius"})

        assert result["_after_input_text_error"] == "boom"

    def test_input_text_retries_after_input_hook_until_confirm_screen(self, ctx, monkeypatch):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {"custom_commands": ["after_input_text"]}
        command_results = [
            {
                "success": True,
                "handled": False,
                "auto_confirm_skipped": "no_confirm_screen",
            },
            {
                "success": True,
                "handled": True,
                "auto_confirmed": True,
                "label": "Confirm",
            },
        ]

        def command(cmd_name, **args):
            ctx.client.calls.append(("command", (cmd_name, args)))
            return command_results.pop(0)

        def poll(timeout=0, include_prefetched=True):
            ctx.client.calls.append(("poll", {
                "timeout": timeout,
                "include_prefetched": include_prefetched,
            }))
            ctx.client._state = {
                "pending_request": {
                    "type": "choice_request",
                    "choices": ["Continue"],
                },
            }
            return [{"type": "narration", "text": "The answer appears."}]

        monkeypatch.setattr(ctx.client, "command", command)
        monkeypatch.setattr(ctx.client, "poll", poll)

        result = handle_input_text(ctx, {"text": "Tatius"})

        assert result["_auto_confirmed"] is True
        assert ctx.client.calls.count(
            ("command", ("after_input_text", {
                "text": "Tatius", "_deadline": ANY,
            }))
        ) == 2
        assert ctx.client._prefetched_events == [
            {"type": "narration", "text": "The answer appears."}
        ]

    def test_input_hook_settle_does_not_poll_after_state_spends_deadline(
        self, ctx, monkeypatch,
    ):
        import vnflight.handlers as handlers

        clock = {"now": 10.0}
        monkeypatch.setattr(
            handlers.time, "time", lambda: clock["now"],
        )

        def state(*, timeout=3.0):
            clock["now"] += timeout
            return {}

        monkeypatch.setattr(ctx.client, "state", state)
        monkeypatch.setattr(
            ctx.client,
            "poll",
            lambda **_kwargs: pytest.fail("poll started after deadline"),
        )

        handlers._settle_after_input_text_hook_action(
            ctx, deadline=clock["now"] + 0.05,
        )

    def test_input_text_settles_after_auto_confirm_hook(self, ctx, monkeypatch):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {"custom_commands": ["after_input_text"]}
        ctx.client._command_results["after_input_text"] = {
            "success": True,
            "handled": True,
            "auto_confirmed": True,
            "label": "Confirm",
        }
        final_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "choices": ["“How about...”", "“Thanks.”"],
            },
        }
        states = [
            {"custom_commands": ["after_input_text"]},
            {
                "status": "running",
                "screen": {
                    "screens": ["input"],
                    "buttons": [{"label": "Confirm", "screen": "confirm"}],
                },
            },
            final_state,
        ]

        def state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else final_state

        def poll(timeout=0, include_prefetched=True):
            ctx.client.calls.append(("poll", {
                "timeout": timeout,
                "include_prefetched": include_prefetched,
            }))
            return [{"type": "narration", "text": "The innkeeper answers."}]

        monkeypatch.setattr(ctx.client, "state", state)
        monkeypatch.setattr(ctx.client, "poll", poll)

        result = handle_input_text(ctx, {"text": "Foggy Lake"})

        assert result["_auto_confirmed"] is True
        assert ctx.client._prefetched_events == [
            {"type": "narration", "text": "The innkeeper answers."},
        ]

    def test_input_text_preserves_story_after_fresh_pending(self, ctx, monkeypatch):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {"custom_commands": ["after_input_text"]}
        ctx.client._command_results["after_input_text"] = {
            "success": True,
            "handled": True,
            "auto_confirmed": True,
            "label": "Confirm",
        }
        final_state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "choices": ["“How about...”", "“Thanks.”"],
            },
        }
        states = [
            {"custom_commands": ["after_input_text"]},
            final_state,
        ]
        poll_batches = [
            [{"type": "narration", "text": "Creeks is my home."}],
        ]

        def state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else final_state

        def poll(timeout=0, include_prefetched=True):
            ctx.client.calls.append(("poll", {
                "timeout": timeout,
                "include_prefetched": include_prefetched,
            }))
            return poll_batches.pop(0) if poll_batches else []

        monkeypatch.setattr(ctx.client, "state", state)
        monkeypatch.setattr(ctx.client, "poll", poll)

        result = handle_input_text(ctx, {"text": "Creeks"})

        assert result["_auto_confirmed"] is True
        assert ctx.client._prefetched_events == [
            {"type": "narration", "text": "Creeks is my home."},
        ]

    def test_input_text_preserves_after_input_skip_reason(self, ctx, monkeypatch):
        from vnflight.handlers import handle_input_text
        ctx.client._state = {"custom_commands": ["after_input_text"]}
        ctx.client._command_results["after_input_text"] = {
            "success": True,
            "handled": False,
            "auto_confirm_skipped": "no_confirm_screen",
        }

        def poll(timeout=0, include_prefetched=True):
            raise RuntimeError("stop retry")

        monkeypatch.setattr(ctx.client, "poll", poll)

        result = handle_input_text(ctx, {"text": "Tatius"})

        assert result["_auto_confirm_skipped"] == "no_confirm_screen"

    def test_missing_text(self, ctx):
        from vnflight.handlers import handle_input_text
        result = handle_input_text(ctx, {})
        assert "error" in result


# ---------------------------------------------------------------------------
# handle_save / handle_load
# ---------------------------------------------------------------------------

class TestHandleSaveLoad:

    def test_default_save(self, ctx):
        from vnflight.handlers import handle_save
        result = handle_save(ctx, {})
        assert ctx.client.calls[-1] == ("command", ("save", {}))

    def test_named_save(self, ctx):
        from vnflight.handlers import handle_save
        result = handle_save(ctx, {"slot": "checkpoint_1", "name": "Before boss"})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[0] == "command"
        assert cmd_call[1][0] == "save"
        assert cmd_call[1][1]["slot"] == "checkpoint_1"
        assert cmd_call[1][1]["name"] == "Before boss"

    def test_name_only_save_derives_distinct_slot(self, ctx):
        from vnflight.handlers import handle_save
        result = handle_save(ctx, {"name": "Loop checkpoint - multiple deaths explored"})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[0] == "command"
        assert cmd_call[1][0] == "save"
        assert cmd_call[1][1]["slot"] == (
            "named-loop-checkpoint-multiple-deaths-explored"
        )
        assert cmd_call[1][1]["name"] == (
            "Loop checkpoint - multiple deaths explored"
        )

    def test_name_only_save_slot_derivation_is_stable_and_bounded(self):
        from vnflight.handlers import _save_slot_from_display_name
        assert _save_slot_from_display_name("  Leto - Day 5, Eudocia's home  ") == (
            "named-leto-day-5-eudocia-s-home"
        )
        assert _save_slot_from_display_name("!!!") == "named-checkpoint"
        assert len(_save_slot_from_display_name("x" * 200)) <= 78

    def test_default_load(self, ctx):
        from vnflight.handlers import handle_load
        result = handle_load(ctx, {})
        assert ctx.client.calls[-1] == ("command", ("load", {}))

    def test_named_load(self, ctx):
        from vnflight.handlers import handle_load
        result = handle_load(ctx, {"slot": "checkpoint_1"})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[0] == "command"
        assert cmd_call[1][0] == "load"
        assert cmd_call[1][1]["slot"] == "checkpoint_1"

    def test_after_command_hook_on_load(self, ctx):
        from vnflight.handlers import handle_load
        hook_calls = []
        ctx.hooks.after_command = lambda cmd, result: (hook_calls.append(cmd), result)[1]
        handle_load(ctx, {"slot": "test"})
        assert "load" in hook_calls

    def test_successful_load_resets_passive_overlay_snapshot(self, ctx):
        from vnflight.handlers import handle_load

        ctx.overlay.text_snapshot = ["A previously delivered terminal row."]
        ctx.overlay.screen_key = ("echo_terminal_live",)
        ctx.overlay.delivery_serial = 7
        ctx.overlay.pending_deliveries = [{"id": 7, "text": "pending"}]

        result = handle_load(ctx, {"slot": "checkpoint_1"})

        assert result["ok"] is True
        assert ctx.overlay.text_snapshot == []
        assert ctx.overlay.screen_key == ()
        assert ctx.overlay.delivery_serial == 0
        assert ctx.overlay.pending_deliveries == []

    def test_failed_load_preserves_passive_overlay_snapshot(self, ctx):
        from vnflight.handlers import handle_load

        ctx.overlay.text_snapshot = ["A previously delivered terminal row."]
        ctx.overlay.screen_key = ("echo_terminal_live",)
        ctx.overlay.delivery_serial = 7
        ctx.overlay.pending_deliveries = [{"id": 7, "text": "pending"}]
        ctx.client._command_results["load"] = {
            "ok": False,
            "error": "save is unavailable",
        }

        handle_load(ctx, {"slot": "missing"})

        assert ctx.overlay.text_snapshot == [
            "A previously delivered terminal row.",
        ]
        assert ctx.overlay.screen_key == ("echo_terminal_live",)
        assert ctx.overlay.delivery_serial == 7
        assert ctx.overlay.pending_deliveries == [
            {"id": 7, "text": "pending"},
        ]

    def test_unconfirmed_save_failure_propagates_to_caller(self, ctx):
        """A save the shim never confirmed must reach the agent as non-ok
        (previously the client reported ok:True on timeout and the agent
        believed the save succeeded)."""
        from vnflight.handlers import handle_save
        ctx.client._command_results["save"] = {
            "ok": False,
            "confirmed": False,
            "error": "Command 'save' was submitted but not confirmed "
                     "by the game (no result within timeout).",
        }
        result = handle_save(ctx, {})
        assert result["ok"] is False
        assert "not confirmed" in result["error"]

    def test_unconfirmed_load_failure_propagates_to_caller(self, ctx):
        from vnflight.handlers import handle_load
        ctx.client._command_results["load"] = {
            "ok": False,
            "confirmed": False,
            "error": "Command 'load' was submitted but not confirmed "
                     "by the game (no result within timeout).",
        }
        result = handle_load(ctx, {"slot": "s1"})
        assert result["ok"] is False
        assert "not confirmed" in result["error"]

    def test_acceptance_unknown_load_resets_local_timeline(self, ctx):
        from vnflight.handlers import handle_load

        ctx.client.cursor = 88
        ctx.client.last_request_id = "before-load"
        ctx.client._prefetched_events = [{"type": "narration"}]
        ctx.overlay.text_snapshot = ["old terminal row"]
        ctx.client._command_results["load"] = {
            "ok": False,
            "success": False,
            "acceptance_unknown": True,
            "mutation_may_have_applied": True,
            "retry_safe": False,
            "command_nonce": "load-nonce",
        }

        result = handle_load(ctx, {"slot": "s1"})

        assert result["acceptance_unknown"] is True
        assert ctx.client.cursor == 0
        assert ctx.client.last_request_id is None
        assert ctx.client._prefetched_events == []
        assert ctx.client._delivered_action_events == set()
        assert ctx.overlay.text_snapshot == []

    def test_navigation_reuses_supplied_unknown_command_nonce(self, ctx):
        from vnflight.handlers import handle_advance

        handle_advance(ctx, {"command_nonce": "same-attempt"})

        assert ctx.client.calls[-1] == (
            "command", ("advance", {"_nonce": "same-attempt"}),
        )

    def test_navigation_labels_returned_nonce_as_command_only(self, ctx):
        from vnflight.handlers import handle_advance

        ctx.client._command_results["advance"] = {
            "ok": True,
            "success": True,
            "nonce": "advance-command",
        }

        result = handle_advance(ctx, {})

        assert result["command_nonce"] == "advance-command"
        assert result["nonce_kind"] == "command"
        assert "wait(action_nonce=...)" in result["nonce_guidance"]
        assert "action_nonce" not in result


# ---------------------------------------------------------------------------
# handle_auto_skip
# ---------------------------------------------------------------------------

class TestHandleAutoSkip:

    def test_enable(self, ctx):
        from vnflight.handlers import handle_auto_skip
        result = handle_auto_skip(ctx, {"enabled": True})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[1][0] == "set"
        assert cmd_call[1][1]["key"] == "auto_skip_single_choice"
        assert cmd_call[1][1]["value"] is True
        assert result["command"] == "auto_skip"
        assert result["transport_command"] == "set"
        assert result["mode"] == "update"

    def test_disable(self, ctx):
        from vnflight.handlers import handle_auto_skip
        result = handle_auto_skip(ctx, {"enabled": False})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[1][1]["value"] is False

    def test_query(self, ctx):
        from vnflight.handlers import handle_auto_skip
        result = handle_auto_skip(ctx, {})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[1][0] == "set"
        assert cmd_call[1][1]["key"] == "auto_skip_single_choice"
        assert "value" not in cmd_call[1][1]
        assert result["command"] == "auto_skip"
        assert result["transport_command"] == "set"
        assert result["mode"] == "query"

    def test_reuses_supplied_unknown_command_nonce(self, ctx):
        from vnflight.handlers import handle_auto_skip

        handle_auto_skip(ctx, {
            "enabled": True,
            "command_nonce": "auto-skip-attempt-1",
        })

        assert ctx.client.calls[-1] == (
            "command",
            ("set", {
                "key": "auto_skip_single_choice",
                "value": True,
                "_nonce": "auto-skip-attempt-1",
            }),
        )

    def test_preserves_acceptance_unknown_lifecycle(self, ctx):
        from vnflight.handlers import handle_auto_skip

        ctx.client._command_results["set"] = {
            "ok": False,
            "success": False,
            "acceptance_unknown": True,
            "mutation_may_have_applied": True,
            "retry_safe": False,
            "command_nonce": "auto-skip-attempt-2",
        }

        result = handle_auto_skip(ctx, {"enabled": False})

        assert result["acceptance_unknown"] is True
        assert result["mutation_may_have_applied"] is True
        assert result["retry_safe"] is False
        assert result["command_nonce"] == "auto-skip-attempt-2"
        assert result["command"] == "auto_skip"
        assert result["transport_command"] == "set"
        assert result["mode"] == "update"


# ---------------------------------------------------------------------------
# handle_back
# ---------------------------------------------------------------------------

class TestHandleBack:

    def test_sends_back_command(self, ctx):
        from vnflight.handlers import handle_back
        result = handle_back(ctx, {})
        assert ctx.client.calls[-1] == ("command", ("back", {}))

    def test_labels_returned_nonce_as_command_only(self, ctx):
        from vnflight.handlers import handle_back

        ctx.client._command_results["back"] = {
            "ok": True,
            "success": True,
            "nonce": "back-command",
        }

        result = handle_back(ctx, {})

        assert result["command_nonce"] == "back-command"
        assert result["nonce_kind"] == "command"
        assert "wait(action_nonce=...)" in result["nonce_guidance"]

    def test_called_screen_refusal_points_the_caller_at_rewind(self, ctx):
        """Fleet R66: back() refused 4/4 times on this game's `call screen`
        consoles.  That refusal is by design (a generic Return() cannot
        safely guess a called screen's return value -- see the shim's
        ``_vnf_cmd_back``), so the fix is an ACTIONABLE message rather than
        a routing change: it names the screen and points at rewind() as
        the tool that DOES roll back through it.  The handler is a pure
        passthrough here; this pins that the shim's improved wording
        survives unmodified to the caller."""
        from vnflight.handlers import handle_back

        ctx.client._command_results["back"] = {
            "ok": False,
            "success": False,
            "error": (
                "Screen 'observatory_hud' is a custom called screen; "
                "generic 'back' cannot safely choose its return value. "
                "Use act() on its visible Close or Release control, or "
                "call rewind() to roll back through it (rewind moves the "
                "story backward through the console; back never does)."
            ),
        }

        result = handle_back(ctx, {})

        assert result["success"] is False
        assert "observatory_hud" in result["error"]
        assert "rewind()" in result["error"]
        assert "act() on its visible Close or Release control" in result["error"]


# ---------------------------------------------------------------------------
# handle_back_all
# ---------------------------------------------------------------------------
class TestHandleBackAll:
    """Climb out of nested overlays in one call (rw69 debrief: eating a
    chicken was five calls, four of them Return). Reverses navigation
    only; the shim's overlays_only refusal is the stop, never a bare
    Return() into the story."""

    def _script(self, ctx, monkeypatch, results):
        from vnflight import handlers
        monkeypatch.setattr(handlers, "_BACK_ALL_SETTLE_S", 0)
        queue = list(results)

        def command(cmd_name, **args):
            ctx.client.calls.append(("command", (cmd_name, args)))
            return queue.pop(0)

        ctx.client.command = command

    def test_closes_until_the_shim_says_nothing_to_close(self, ctx, monkeypatch):
        from vnflight.handlers import handle_back_all
        self._script(ctx, monkeypatch, [
            {"ok": True, "success": True, "note": "Hide 'item_detail' queued."},
            {"ok": True, "success": True, "note": "Hide 'inventory' queued."},
            {"ok": False, "success": False, "nothing_to_close": True,
             "error": "Nothing to close: no overlay or menu screen is showing."},
        ])

        result = handle_back_all(ctx, {})

        backs = [c for c in ctx.client.calls if c[0] == "command" and c[1][0] == "back"]
        assert len(backs) == 3
        assert all(c[1][1].get("overlays_only") is True for c in backs)
        assert result["closed"] == 2
        assert result["stopped_by"] == "nothing_to_close"
        assert "error" not in result
        assert result["text"].startswith("back_all: closed 2 screens.")
        # It ends by rendering the current state, like state() would.
        assert ("state", {"timeout": 3.0}) in ctx.client.calls

    def test_a_refusal_stops_the_loop_and_is_reported_as_is(self, ctx, monkeypatch):
        from vnflight.handlers import handle_back_all
        self._script(ctx, monkeypatch, [
            {"ok": True, "success": True, "note": "Hide 'shop' queued."},
            {"ok": False, "success": False, "error":
             "Screen 'observatory_hud' is a custom called screen; generic "
             "'back' cannot safely choose its return value."},
        ])

        result = handle_back_all(ctx, {})

        assert result["closed"] == 1
        assert result["stopped_by"] == "refusal"
        assert result["success"] is False
        assert "observatory_hud" in result["error"]

    def test_nothing_open_is_a_clean_no_op(self, ctx, monkeypatch):
        from vnflight.handlers import handle_back_all
        self._script(ctx, monkeypatch, [
            {"ok": False, "success": False, "nothing_to_close": True,
             "error": "Nothing to close: no overlay or menu screen is showing."},
        ])

        result = handle_back_all(ctx, {})

        assert result["closed"] == 0
        assert "error" not in result
        assert result["text"].startswith("back_all: closed 0 screens.")

    def test_bounded_when_something_keeps_reopening(self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_back_all
        self._script(ctx, monkeypatch, [
            {"ok": True, "success": True, "note": "Hide 'x' queued."}
        ] * (handlers._BACK_ALL_MAX_STEPS + 3))

        result = handle_back_all(ctx, {})

        assert result["closed"] == handlers._BACK_ALL_MAX_STEPS
        assert result["stopped_by"] == "max_steps"
        assert "reopens on close" in result["warning"]

    def test_runs_inside_the_presentation_lane(self):
        """Astra review: back_all was missing from the lane registry, so it
        sent commands while wait() owned the lane."""
        import time
        from vnflight import handlers, presentation_lane

        assert "back_all" in presentation_lane._PRESENTATION_RESULT_TOOLS
        ctx = handlers.HandlerContext(client=MockClient())
        ctx._presentation_call_name = "wait"
        ctx._presentation_call_started_at = time.monotonic() - 1.0
        ctx._presentation_call_lock.acquire()
        try:
            result = handlers.handle_tool(ctx, "back_all", {})
        finally:
            ctx._presentation_call_lock.release()

        assert result["reason"] == "presentation_call_in_flight"
        assert not [c for c in ctx.client.calls if c[0] == "command"]

    def test_deadline_bounds_the_climb_and_the_final_state_read(self, ctx, monkeypatch):
        """Astra review: the final state read ignored the call deadline."""
        import time
        from vnflight.handlers import handle_back_all
        self._script(ctx, monkeypatch, [
            {"ok": True, "success": True},
            {"ok": True, "success": True},
            {"ok": False, "success": False, "nothing_to_close": True},
        ])

        # Expired before the first step: no back sent, no state read.
        result = handle_back_all(ctx, {"_result_deadline": time.time() - 1})
        assert result["closed"] == 0
        assert result["stopped_by"] == "deadline"
        assert "time budget" in result["warning"]
        assert not [c for c in ctx.client.calls if c[0] in ("command", "state")]

        # A result-only deadline also bounds every back command (Astra
        # round 3): the transport helper reads _transport_deadline.
        backs = [c for c in ctx.client.calls if c[0] == "command" and c[1][0] == "back"]
        assert backs == []
        ctx.client.calls.clear()
        self._script(ctx, monkeypatch, [
            {"ok": False, "success": False, "nothing_to_close": True},
        ])
        handle_back_all(ctx, {"_result_deadline": time.time() + 5.0})
        backs = [c for c in ctx.client.calls if c[0] == "command" and c[1][0] == "back"]
        assert backs and isinstance(backs[0][1][1].get("_deadline"), float)

        # A live deadline is forwarded to the state read.
        self._script(ctx, monkeypatch, [
            {"ok": True, "success": True},
            {"ok": True, "success": True},
            {"ok": False, "success": False, "nothing_to_close": True},
        ])
        seen = {}
        real_state = ctx.client.state

        def state(*, timeout=3.0):
            seen["timeout"] = timeout
            return real_state(timeout=timeout)

        ctx.client.state = state
        result = handle_back_all(ctx, {"_result_deadline": time.time() + 2.0})
        assert result["closed"] == 2
        assert seen["timeout"] <= 2.0

    def test_hooks_fire_once_for_the_whole_climb(self, ctx, monkeypatch):
        from vnflight.handlers import HandlerContext, Hooks, handle_back_all
        seen = []
        ctx = HandlerContext(
            client=ctx.client,
            hooks=Hooks(before_action=lambda name, args: seen.append(name)))
        self._script(ctx, monkeypatch, [
            {"ok": True, "success": True},
            {"ok": True, "success": True},
            {"ok": False, "success": False, "nothing_to_close": True},
        ])

        handle_back_all(ctx, {})

        assert seen == ["back_all"]


# ---------------------------------------------------------------------------
# story navigation controls
# ---------------------------------------------------------------------------

class TestHandleStoryNavigation:

    @pytest.mark.parametrize(
        ("handler_name", "command_name"),
        [
            ("handle_advance", "advance"),
            ("handle_rewind", "rewind"),
            ("handle_replay", "replay"),
        ],
    )
    def test_sends_story_navigation_command(self, ctx, handler_name, command_name):
        import vnflight.handlers as handlers

        handler = getattr(handlers, handler_name)
        result = handler(ctx, {})

        assert ctx.client.calls[-1] == ("command", (command_name, {}))
        assert result["ok"] is True

    def test_story_navigation_calls_before_action_hook(self, ctx):
        from vnflight.handlers import Hooks, handle_advance

        calls = []
        ctx.hooks = Hooks(before_action=lambda name, payload: calls.append((name, payload)))

        handle_advance(ctx, {"reason": "test"})

        assert calls == [("advance", {"reason": "test"})]

    def test_advance_menu_race_exposes_current_choice_without_mutating(self, ctx):
        from vnflight.handlers import handle_advance

        ctx.client._command_results["advance"] = {
            "ok": False,
            "success": False,
            "error": "Cannot advance while a game menu is active.",
            "nonce": "advance-race",
        }
        ctx.client._state = _menu_state(
            "Signal processing", "Systems engineering",
            request_id="specialization",
        )

        result = handle_advance(ctx, {})

        assert result["ok"] is False
        assert result["success"] is False
        assert result["error"] == "Cannot advance while a game menu is active."
        assert "Signal processing" in result["pending"]
        assert result["_data"]["_pending_raw"]["id"] == "specialization"
        assert result["command_nonce"] == "advance-race"
        assert result["_advance_menu_race_exposed"] is True

    def test_advance_menu_race_uses_fresh_cache_after_deadline(self, ctx):
        import time

        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import handle_advance

        ctx.client._command_results["advance"] = {
            "ok": False,
            "success": False,
            "error": "Cannot advance while a game menu is active.",
            "nonce": "advance-race",
        }
        state = _menu_state(
            "Signal processing", "Systems engineering",
            request_id="specialization",
        )
        pending = state["pending_request"]
        ctx.client.last_actionable_snapshot = actionable_state_snapshot(state)
        ctx.client._fresh_cached_pending = lambda: pending

        result = handle_advance(ctx, {
            "_result_deadline": time.time() - 1.0,
        })

        assert result["ok"] is False
        assert result["success"] is False
        assert result["error"] == "Cannot advance while a game menu is active."
        assert "Signal processing" in result["pending"]
        assert result["_data"]["_pending_raw"]["id"] == "specialization"
        assert result["_data"]["_actionable_snapshot"] == (
            actionable_state_snapshot(state)
        )
        assert result["_advance_menu_race_exposed"] is True
        assert not any(name == "state" for name, _ in ctx.client.calls)

    def test_advance_menu_race_state_failure_keeps_original_refusal(
        self, ctx, monkeypatch,
    ):
        from vnflight.handlers import handle_advance

        refusal = {
            "ok": False,
            "success": False,
            "error": "Cannot advance while a game menu is active.",
        }
        ctx.client._command_results["advance"] = dict(refusal)
        monkeypatch.setattr(
            ctx.client, "state", lambda **_kwargs: (_ for _ in ()).throw(
                OSError("bridge unavailable")))

        result = handle_advance(ctx, {})

        assert result == refusal


# ---------------------------------------------------------------------------
# handle_state
# ---------------------------------------------------------------------------

class TestHandleState:

    def test_brief_mode(self, ctx):
        from vnflight.handlers import handle_state
        ctx.client._state = {
            "status": "playing",
            "stats": {"hp": 10, "mp": 5},
        }
        result = handle_state(ctx, {"brief": True})
        # Should return formatted text with stats.
        assert isinstance(result, dict)

    def test_verbose_mode(self, ctx):
        from vnflight.handlers import handle_state
        ctx.client._state = {
            "status": "playing",
            "stats": {"hp": 10},
            "inventory": [{"name": "Sword"}],
        }
        result = handle_state(ctx, {"brief": False})
        assert isinstance(result, dict)

    def test_fresh_main_menu_screen_overrides_stale_state_read(self, ctx):
        from vnflight.handlers import handle_state
        ctx.client._state = {
            "status": "running",
            "context": {"context": "in_game"},
            "gameplay_seen": False,
            "config": {
                "auto_advance": True,
                "auto_advance_delay": 0.3,
                "end_on_menu_return": True,
            },
            "game_state": {
                "stats": {"_summary": "Evidence: 0", "evidence_count": 0},
                "screen_buttons": [],
            },
        }
        ctx.client._screen = {
            "main_menu": True,
            "screens": ["menu"],
            "buttons": [
                {"label": "Start", "screen": "menu", "actions": ["Start"]},
                {"label": "Load", "screen": "menu", "actions": ["ShowMenu"]},
                {"label": "Quit", "screen": "menu", "actions": ["Quit"]},
            ],
        }

        result = handle_state(ctx, {"brief": False})

        assert result["_data"]["_effective_status"] == "screen_actions"
        assert "stats" not in result["_data"]
        assert "Start" in result["buttons"]
        assert "Load" in result["buttons"]
        assert result.get("status") != "playing"
        assert "Config:" not in result.get("_footer", "")

    def test_state_footer_reports_only_non_default_config(self, ctx):
        from vnflight.handlers import handle_state
        ctx.client._state = {
            "status": "running",
            "context": {"context": "in_game"},
            "gameplay_seen": True,
            "config": {
                "auto_advance": False,
                "auto_advance_delay": 0.3,
                "end_on_menu_return": True,
            },
            "game_state": {
                "stats": {"_summary": "Evidence: 0"},
                "screen_buttons": [],
            },
        }
        ctx.client._screen = None

        footer = handle_state(ctx, {"brief": False}).get("_footer", "")

        assert "Config: auto_advance: False" in footer
        assert "auto_advance_delay" not in footer
        assert "end_on_menu_return" not in footer

    def test_pre_game_state_does_not_claim_playing_before_screen_arrives(self, ctx):
        from vnflight.handlers import handle_state
        ctx.client._state = {
            "status": "running",
            "context": {"context": "in_game"},
            "gameplay_seen": False,
            "config": {
                "auto_advance": True,
                "auto_advance_delay": 0.3,
                "end_on_menu_return": True,
            },
            "game_state": {"screen_buttons": []},
            "transcript": [
                {"type": "context", "context": "in_game", "_seq": 1},
            ],
        }
        ctx.client._screen = None

        result = handle_state(ctx, {"brief": False})

        assert result["status"] == "starting"
        assert result["_data"]["_effective_status"] == "starting"
        assert result["_data"]["_lifecycle"]["awaiting_first_interaction"] is True
        assert "Config:" not in result.get("_footer", "")

        ctx.client._screen = {
            "main_menu": True,
            "screens": ["menu"],
            "buttons": [
                {"label": "Start", "screen": "menu", "actions": ["Start"]},
                {"label": "Load", "screen": "menu", "actions": ["ShowMenu"]},
            ],
        }

        ready = handle_state(ctx, {"brief": False})

        assert ready["_data"]["_effective_status"] == "screen_actions"
        assert "Start" in ready["buttons"]
        assert "Load" in ready["buttons"]


# ---------------------------------------------------------------------------
# handle_inspect
# ---------------------------------------------------------------------------

class TestHandleInspect:

    def test_returns_raw_state(self, ctx):
        from vnflight.handlers import handle_inspect
        ctx.client._state = {
            "status": "playing",
            "config": {"game": "test"},
            "pending_request": {"type": "choice_request", "id": "abc"},
            "inventory": [{"name": "Key"}],
            "stats": {"hp": 5},
            "screens": ["main_menu"],
            "interactions": [{"label": "Button"}],
        }
        result = handle_inspect(ctx, {})
        assert result["status"] == "playing"
        assert result["pending_request"]["id"] == "abc"
        assert result["slot_prefix"] == "/test"


# ---------------------------------------------------------------------------
# handle_command
# ---------------------------------------------------------------------------

class TestHandleCommand:

    def test_sends_command(self, ctx):
        from vnflight.handlers import handle_command
        result = handle_command(ctx, {"name": "eval", "args": {"expr": "1+1"}})
        cmd_call = ctx.client.calls[-1]
        assert cmd_call[1][0] == "eval"
        assert cmd_call[1][1]["expr"] == "1+1"

    def test_missing_name(self, ctx):
        from vnflight.handlers import handle_command
        result = handle_command(ctx, {})
        assert "error" in result


# ---------------------------------------------------------------------------
# handle_progress
# ---------------------------------------------------------------------------

class TestHandleProgress:

    def test_sends_progress_command(self, ctx):
        from vnflight.handlers import handle_progress
        result = handle_progress(ctx, {})
        assert ctx.client.calls[-1] == ("command", ("progress", {}))

    def test_error_passthrough(self, ctx):
        from vnflight.handlers import handle_progress
        ctx.client._command_results["progress"] = {"ok": False, "error": "No mod"}
        result = handle_progress(ctx, {})
        assert "error" in result


# ---------------------------------------------------------------------------
# handle_tool dispatcher
# ---------------------------------------------------------------------------

class TestHandleTool:

    def test_dispatches_known_tool(self, ctx):
        from vnflight.handlers import handle_tool
        ctx.client._screenshot = "abc123"
        result = handle_tool(ctx, "screenshot", {})
        assert "screenshot_base64" in result

    @pytest.mark.parametrize("tool_name", ["advance", "rewind", "replay"])
    def test_dispatches_story_navigation_tools(self, ctx, tool_name):
        from vnflight.handlers import handle_tool

        result = handle_tool(ctx, tool_name, {})

        assert ctx.client.calls[-1] == ("command", (tool_name, {}))
        assert result["ok"] is True

    def test_unknown_tool(self, ctx):
        from vnflight.handlers import handle_tool
        result = handle_tool(ctx, "nonexistent", {})
        assert "error" in result
        assert "nonexistent" in result["error"]

    def test_lazy_attach_runs_inside_lane_and_resets_old_source_ledgers(
        self, ctx, monkeypatch,
    ):
        from vnflight import presentation_lane
        from vnflight import handlers

        observed = []
        ctx.client.slot_prefix = ""
        ctx.overlay.pending_deliveries = [{"id": 1, "text": "old row"}]
        ctx.overlay.timeline_source_sequences = {"old-source": 9}

        def attach():
            assert ctx._presentation_call_lock.locked()
            ctx.client.slot_prefix = "/7"

        def inspect(call_ctx, _params):
            observed.append(list(call_ctx.overlay.pending_deliveries))
            return {"ok": True}

        ctx.hooks.ensure_attached = attach
        monkeypatch.setitem(handlers._HANDLERS, "_test_attach", inspect)
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {"_test_attach"},
        )

        result = handlers.handle_tool(ctx, "_test_attach", {})

        assert result == {"ok": True}
        assert observed == [[]]
        assert ctx.overlay.timeline_source_sequences == {}

    def test_binding_transition_refuses_while_live_call_owns_lane(self, ctx):
        from vnflight.handlers import run_presentation_transition

        ctx._presentation_call_lock.acquire()
        try:
            called = []
            result = run_presentation_transition(
                ctx, lambda: called.append(True) or {"ok": True})
        finally:
            ctx._presentation_call_lock.release()

        assert result["reason"] == "presentation_call_in_flight"
        assert called == []

    def test_explicit_rebind_resets_ledgers_even_when_binding_text_is_same(
        self, ctx,
    ):
        from vnflight.handlers import run_presentation_transition

        ctx.client.bridge_url = "http://127.0.0.1:9000"
        ctx.client.slot_prefix = "/7"
        ctx.overlay.pending_deliveries = [{"id": 1, "text": "old row"}]
        ctx.overlay.timeline_source_sequences = {"old-source": 9}

        result = run_presentation_transition(
            ctx, lambda: {"ok": True}, reset_context=True)

        assert result == {"ok": True}
        assert ctx.overlay.pending_deliveries == []
        assert ctx.overlay.timeline_source_sequences == {}

    def test_failed_forced_transition_preserves_same_binding_ledgers(self, ctx):
        from vnflight.handlers import run_presentation_transition

        ctx.client.slot_prefix = "/7"
        pending = [{"id": 1, "text": "still owed"}]
        ctx.overlay.pending_deliveries = list(pending)

        result = run_presentation_transition(
            ctx,
            lambda: {"error": "bridge did not start"},
            reset_context=True,
        )

        assert result == {"error": "bridge did not start"}
        assert ctx.overlay.pending_deliveries == pending

    def test_transition_exception_resets_ledgers_after_binding_changed(self, ctx):
        from vnflight.handlers import run_presentation_transition

        ctx.client.slot_prefix = "/old"
        ctx.overlay.pending_deliveries = [{"id": 1, "text": "old row"}]

        def switch_then_fail():
            ctx.client.slot_prefix = "/new"
            raise RuntimeError("status read failed")

        with pytest.raises(RuntimeError, match="status read failed"):
            run_presentation_transition(ctx, switch_then_fail)

        assert ctx.overlay.pending_deliveries == []


# ---------------------------------------------------------------------------
# handle_wait (basic — full wait tests are in test_tools.py)
# ---------------------------------------------------------------------------

class TestHandleWait:

    def test_empty_wait(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(events=[], pending=None)
        result = handle_wait(ctx, {"timeout": 5})
        assert isinstance(result, dict)

    def test_dialogue_events(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[
                {"type": "dialogue", "character": "Alice", "text": "Hello!"},
                {"type": "narration", "text": "She waved."},
            ],
        )
        result = handle_wait(ctx, {"timeout": 5})
        assert "text" in result
        assert "Alice" in result["text"]
        assert "waved" in result["text"]

    def test_wait_drops_prompt_echo_before_choice_request(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[
                {
                    "type": "dialogue",
                    "character": "Alex",
                    "text": "What do you do?",
                },
                {
                    "type": "choice_request",
                    "id": "new-menu",
                    "choices": ["Go inside"],
                },
            ],
            pending={
                "type": "choice_request",
                "id": "new-menu",
                "choices": ["Go inside"],
            },
        )

        result = handle_wait(ctx, {"timeout": 5})

        assert "pending" in result
        assert "Go inside" in result["pending"]
        assert "text" not in result

    def test_pending_choices(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[],
            pending={
                "type": "choice_request",
                "choices": ["Yes", "No"],
                "id": "c1",
            },
        )
        result = handle_wait(ctx, {"timeout": 5})
        assert "pending" in result

    def test_wait_uses_live_game_state_choices_over_stale_pending(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[],
            pending={
                "type": "choice_request",
                "id": "rw-potion",
                "choices": ["I approach Foggy.", "I go outside."],
                "full_items": [
                    {"label": "I approach Foggy.", "is_disabled": False},
                    {
                        "label": "I’m too exhausted to brew potions. (Required vitality: 1) (disabled)",
                        "is_disabled": True,
                    },
                    {"label": "I go outside.", "is_disabled": False},
                ],
            },
        )
        ctx.client._game_state = {
            "choices": [
                "I approach Foggy.",
                "I go outside.",
                "[cost] I go downstairs, to the alchemy set.",
            ],
            "full_items": [
                {"label": "I approach Foggy.", "is_disabled": False},
                {"label": "I go outside.", "is_disabled": False},
                {
                    "label": "[cost] I go downstairs, to the alchemy set.",
                    "is_disabled": False,
                },
            ],
        }

        result = handle_wait(ctx, {"timeout": 5})

        assert "[cost] I go downstairs" in result["pending"]
        assert "too exhausted" not in result["pending"]

    def test_scoped_successor_event_replaces_consumed_pending_identity(
        self, ctx
    ):
        """A trailing choice event owns both the rendered menu and its id."""
        from vnflight.client import actionable_state_snapshot
        from vnflight.handlers import handle_wait

        consumed = {
            "type": "choice_request",
            "id": "consumed-menu",
            "choices": ["Tell him.", "Say nothing."],
        }
        successor = {
            "type": "choice_request",
            "id": "successor-menu",
            "choices": ["Continue", "Wait"],
            "_seq": 45,
        }
        ctx.client._wait_result = MockWaitResult(
            events=[
                {"type": "dialogue", "character": "Marcus", "text": "What?"},
                successor,
            ],
            # This is the settle-time snapshot from before the successor
            # request reached the scoped transaction journal.
            pending=consumed,
        )
        ctx.client._game_state = {
            "choices": ["Continue", "Wait"],
            "interactions": [
                {
                    "type": "choice", "source": "choice", "index": 1,
                    "display_label": "Continue",
                },
                {
                    "type": "choice", "source": "choice", "index": 2,
                    "display_label": "Wait",
                },
            ],
        }

        result = handle_wait(ctx, {"timeout": 5, "action_nonce": "act-1"})

        expected_snapshot = actionable_state_snapshot({
            "pending_request": successor,
            "game_state": ctx.client._game_state,
        })
        assert "1: Continue" in result["pending"]
        assert result["_pending_raw"]["id"] == "successor-menu"
        assert result["_actionable_snapshot"] == expected_snapshot

    def test_wait_replaces_stale_pending_with_current_state_pending(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "You arrive."}],
            pending={
                "type": "choice_request",
                "id": "western-crossroads",
                "choices": [
                    "I approach the western signpost.",
                    "I approach the eastern signpost.",
                ],
            },
        )
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "howlers-dell",
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
            },
            "game_state": {
                "choices": [
                    "I go to the main square.",
                    "I go to Elpis, the druidess.",
                ],
                "full_items": [
                    {"label": "I go to the main square.", "is_disabled": False},
                    {"label": "I go to Elpis, the druidess.", "is_disabled": False},
                ],
            },
        }

        result = handle_wait(ctx, {"timeout": 5})

        assert "I go to the main square" in result["pending"]
        assert "western signpost" not in result["pending"]
        assert result["_data"]["pending"]["id"] == "howlers-dell"

    def test_game_ended(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(events=[], ended=True)
        result = handle_wait(ctx, {})
        assert result.get("ended") is True

    def test_wait_includes_game_state_summary_footer(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(events=[])
        ctx.client._game_state = {
            "stats": {
                "_summary": "Day 5/40 | 15h before dusk | HP 4/4",
            }
        }
        result = handle_wait(ctx, {})
        assert result.get("_footer") == "  Day 5/40 | 15h before dusk | HP 4/4"

    def test_wait_hides_frozen_run_footer_at_main_menu(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(events=[], ended=True)
        ctx.client._game_state = {
            "game_terminal": True,
            "progress_frozen": True,
            "stats": {
                "_summary": "Evidence: 14 | Signal: 100% | ARIA: 56%",
                "evidence_count": 14,
            },
        }
        ctx.client._state = {
            "status": "ended",
            "context": {"context": "main_menu"},
            "game_terminal": True,
            "game_state": ctx.client._game_state,
        }

        result = handle_wait(ctx, {})

        assert "_footer" not in result
        assert "stats" not in result["_data"]

    def test_scoped_wait_keeps_terminal_verdict_beside_title_buttons(self, ctx):
        """The final action can settle just before its menu-return event."""
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[],
            screen={
                "main_menu": True,
                "buttons": [
                    {"label": "Start", "action_names": ["Start"],
                     "action_strs": ["Start label=start"]},
                    {"label": "Load", "action_names": ["ShowMenu"],
                     "action_strs": ["ShowMenu screen=load"]},
                ],
            },
            transaction={
                "action_nonce": "final-act",
                "action_id": 48,
                "transaction_state": "settled",
                "pending": False,
            },
        )
        ctx.client._game_state = {
            "game_terminal": True,
            "progress_frozen": True,
            "interactions": [],
        }
        ctx.client._state = {
            "status": "ended",
            "context": {"context": "main_menu"},
            "game_terminal": True,
        }

        result = handle_wait(ctx, {
            "action_nonce": "final-act", "format": "json", "timeout": 5,
        })

        assert result["ended"] is True
        assert [
            label
            for labels in result["buttons"].values()
            for label in labels
        ] == ["Start", "Load"]
        assert result["transaction_state"] == "settled"

    def test_wait_newer_state_false_clears_stale_terminal_snapshot(self, ctx):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[],
            screen={
                "main_menu": True,
                "buttons": [
                    {"label": "Start", "action_names": ["Start"],
                     "action_strs": ["Start label=start"]},
                ],
            },
        )
        ctx.client._game_state = {
            "game_terminal": True,
            "progress_frozen": True,
            "interactions": [],
        }
        ctx.client._state = {
            "status": "running",
            "context": {"context": "in_game"},
            "game_terminal": False,
        }

        result = handle_wait(ctx, {"format": "json", "timeout": 5})

        assert "ended" not in result
        assert any(name == "state" for name, _value in ctx.client.calls)

    def test_wait_newer_state_true_overrides_preterminal_snapshot(self, ctx):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[],
            screen={
                "main_menu": True,
                "buttons": [
                    {"label": "Start", "action_names": ["Start"],
                     "action_strs": ["Start label=start"]},
                ],
            },
        )
        ctx.client._game_state = {
            "game_terminal": False,
            "interactions": [],
        }
        ctx.client._state = {
            "status": "ended",
            "context": {"context": "main_menu"},
            "game_terminal": True,
        }

        result = handle_wait(ctx, {"format": "json", "timeout": 5})

        assert result["ended"] is True
        assert any(name == "state" for name, _value in ctx.client.calls)

    def test_wait_terminal_event_survives_failed_lifecycle_confirmation(
        self, ctx,
    ):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(events=[], ended=True)
        ctx.client._game_state = {
            "game_terminal": False,
            "interactions": [],
        }

        def fail_state(*, timeout=3.0):
            ctx.client.calls.append(("state", {"timeout": timeout}))
            raise TimeoutError("lifecycle read timed out")

        ctx.client.state = fail_state

        result = handle_wait(ctx, {"format": "json", "timeout": 5})

        assert result["ended"] is True
        assert any(name == "state" for name, _value in ctx.client.calls)

    def test_wait_terminal_event_without_title_uses_newer_false_state(
        self, ctx,
    ):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(events=[], ended=True)
        ctx.client._game_state = {
            "game_terminal": False,
            "interactions": [],
        }
        ctx.client._state = {
            "status": "running",
            "context": {"context": "in_game"},
            "game_terminal": False,
        }

        result = handle_wait(ctx, {"format": "json", "timeout": 5})

        assert "ended" not in result
        assert any(name == "state" for name, _value in ctx.client.calls)

    @pytest.mark.parametrize("end_reason", ["process_exit", "quit"])
    def test_wait_newer_false_verdict_preserves_process_ending(
        self, ctx, end_reason,
    ):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(events=[], ended=True)
        ctx.client._game_state = {
            "game_terminal": False,
            "interactions": [],
        }
        ctx.client._state = {
            "status": "ended",
            "end_reason": end_reason,
            "context": {"context": "in_game"},
            "game_terminal": False,
        }

        result = handle_wait(ctx, {"format": "json", "timeout": 5})

        assert result["ended"] is True
        assert "_game_terminal" not in result
        assert any(name == "state" for name, _value in ctx.client.calls)

    def test_authoritative_pending_merge_replaces_stale_footer(self):
        from vnflight.handlers import _merge_decision_data

        data = {
            "_footer": "  Evidence: 10 | Storm: 0/3 | Time left: 4h 05m",
            "pending": {"id": "old"},
        }
        current = {
            "_stats_summary": "Evidence: 11 | Storm: 1/3 | Time left: 3h 50m",
            "pending": {"id": "next"},
        }

        _merge_decision_data(
            data,
            current,
            replace_pending=True,
            replace_footer=True,
        )

        assert data["_footer"] == (
            "  Evidence: 11 | Storm: 1/3 | Time left: 3h 50m")

    @pytest.mark.parametrize("status", ["screen_actions", "playing", None, {},
                                        {"stats": [{"stat": "evidence", "value": 2}]}])
    def test_decision_merge_preserves_screen_and_wait_status_shapes(self, status):
        from vnflight.handlers import _merge_decision_data

        data = {"status": status, "pending": {"id": "old"}}
        _merge_decision_data(data, {
            "buttons": [{"label": "Return"}],
            "stats": {"evidence": 2}, "_stats_summary": "Evidence: 2",
        }, replace_pending=True, replace_footer=True)
        assert data["status"] == status
        assert "pending" not in data
        assert data["buttons"] == [{"label": "Return"}]
        assert data["_footer"] == "  Evidence: 2"

    def test_decision_merge_still_rejects_footer_conflicting_with_wait_deltas(self):
        from vnflight.handlers import _merge_decision_data

        data = {"status": {"stats": [{"stat": "evidence", "value": 3}]},
                "_footer": "old"}
        _merge_decision_data(data, {"stats": {"evidence": 2},
                                   "_stats_summary": "Evidence: 2"}, replace_footer=True)
        assert "_footer" not in data

    def test_wait_refreshes_transient_single_enter_screen(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[],
            pending=None,
            screen={
                "buttons": [
                    {"label": "Enter", "screen": "main_menu", "actions": ["Jump"]},
                ],
            },
        )
        fresh_screen = {
            "buttons": [
                {"label": "Continue", "screen": "main_menu", "actions": ["Jump"]},
                {"label": "New Game", "screen": "main_menu", "actions": ["Jump"]},
                {"label": "Settings", "screen": "main_menu", "actions": ["ShowMenu"]},
            ],
        }
        ctx.client._screen = fresh_screen

        result = handle_wait(ctx, {"timeout": 5})

        buttons = result.get("buttons", "")
        assert "New Game" in buttons
        assert "Enter" not in buttons

    def test_wait_keeps_single_enter_screen_without_fresher_state(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[],
            pending=None,
            screen={
                "buttons": [
                    {"label": "Enter", "screen": "main_menu", "actions": ["Jump"]},
                ],
            },
        )

        result = handle_wait(ctx, {"timeout": 5})

        assert "Enter" in result.get("buttons", "")

    def test_wait_merges_decision_from_state_after_timeout(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "A scene finishes."}],
            pending=None,
        )
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "next-1",
                "choices": ["Open the door", "Wait"],
            },
            "inventory": {"current": [], "version": 0},
        }

        result = handle_wait(ctx, {"timeout": 5})

        assert "A scene finishes." in result["text"]
        assert "CHOICE REQUIRED" in result["pending"]
        assert "Open the door" in result["pending"]

    def test_wait_drops_resolved_choice_synthesized_from_stale_interactions(
        self, ctx,
    ):
        import json
        from vnflight.handlers import handle_wait

        choices = ["Talk about the storm.", "Leave him to it."]
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "dialogue", "text": "I am holding up."}],
            transaction={
                "transaction_state": "settled",
                "resolved_as": "choice",
                "initial_request_id": "old-menu",
                "initial_request_content_signature": json.dumps({
                    "choices": choices,
                }),
            },
        )
        ctx.client._game_state = {"interactions": [
            {
                "type": "choice", "source": "choice", "index": index,
                "display_label": label, "disabled": False,
            }
            for index, label in enumerate(choices, 1)
        ]}

        result = handle_wait(ctx, {
            "timeout": 1, "action_nonce": "resolved-choice",
        })

        assert "I am holding up." in result["text"]
        assert "pending" not in result

    def test_wait_keeps_different_synthetic_successor_choice(self, ctx):
        import json
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "dialogue", "text": "One answer leads on."}],
            transaction={
                "transaction_state": "settled",
                "resolved_as": "choice",
                "initial_request_id": "old-menu",
                "initial_request_content_signature": json.dumps({
                    "choices": ["Old answer.", "Old exit."],
                }),
            },
        )
        ctx.client._game_state = {"interactions": [{
            "type": "choice", "source": "choice", "index": 1,
            "display_label": "Fresh follow-up.", "disabled": False,
        }]}

        result = handle_wait(ctx, {
            "timeout": 1, "action_nonce": "resolved-choice",
        })

        assert "Fresh follow-up." in result["pending"]

    def test_wait_keeps_synthetic_choice_when_transaction_failed(self, ctx):
        import json
        from vnflight.handlers import handle_wait

        choices = ["Try again.", "Leave."]
        ctx.client._wait_result = MockWaitResult(
            transaction={
                "transaction_state": "failed",
                "resolved_as": "choice",
                "initial_request_content_signature": json.dumps({
                    "choices": choices,
                }),
            },
        )
        ctx.client._game_state = {"interactions": [
            {
                "type": "choice", "source": "choice", "index": index,
                "display_label": label, "disabled": False,
            }
            for index, label in enumerate(choices, 1)
        ]}

        result = handle_wait(ctx, {
            "timeout": 1, "action_nonce": "failed-choice",
        })

        assert "Try again." in result["pending"]

    def test_wait_appends_late_story_after_pending_from_transcript(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[
                {"_seq": 10, "type": "input_request", "prompt": "Place?"},
                {
                    "_seq": 11,
                    "type": "choice_request",
                    "choices": ["How about...", "Thanks."],
                },
            ],
            pending={
                "id": "next",
                "type": "choice_request",
                "choices": ["How about...", "Thanks."],
            },
        )
        ctx.client._state = {
            "pending_request": {
                "id": "next",
                "type": "choice_request",
                "choices": ["How about...", "Thanks."],
            },
        }
        ctx.client._transcript = [
            {"_seq": 10, "type": "input_request", "prompt": "Place?"},
            {
                "_seq": 11,
                "type": "choice_request",
                "choices": ["How about...", "Thanks."],
            },
            {"_seq": 12, "type": "narration", "text": "Creeks is my home."},
        ]

        result = handle_wait(ctx, {"timeout": 5})

        assert "Creeks is my home." in result["text"]
        assert "How about" in result["pending"]

    def test_wait_delivers_prefetched_story_before_later_transcript_rescue(
        self, ctx,
    ):
        """Fleet r36: Start must not return seq 12 before prefetched seq 11."""
        from vnflight.handlers import handle_wait

        opening = {
            "_seq": 10,
            "type": "narration",
            "text": "Another night at the end of the world.",
            "action_id": 53,
        }
        earlier = {
            "_seq": 9,
            "type": "narration",
            "text": "Snow moves across the glass.",
        }
        mug = {
            "_seq": 11,
            "type": "narration",
            "text": "The mug has gone cold beside her hand.",
        }
        interleaved = {
            "_seq": 12,
            "type": "dialogue",
            "character": "Dr. Voss",
            "text": "The first log entry is hers.",
        }
        later = {
            "_seq": 13,
            "type": "narration",
            "text": "Three months ago, the array heard tomorrow.",
            "action_id": 53,
        }
        pending = {
            "id": "opening-menu",
            "type": "choice_request",
            "choices": ["Read the overnight log."],
        }
        ctx.client._wait_result = MockWaitResult(
            events=[opening],
            pending=pending,
        )
        # The copy of `later` models command observation parking an action
        # row before transcript rescue sees the same durable occurrence.
        ctx.client._prefetched_events = [earlier, mug, interleaved, later]
        ctx.client._state = {"pending_request": pending}
        ctx.client._transcript = [opening, mug, later]

        result = handle_wait(ctx, {
            "timeout": 5,
            "_ordinary_only": True,
            "_ordinary_action_id": 53,
        })

        text = result["text"]
        assert text.index(earlier["text"]) < text.index(opening["text"])
        assert text.index(opening["text"]) < text.index(mug["text"])
        assert text.index(mug["text"]) < text.index(interleaved["text"])
        assert text.index(interleaved["text"]) < text.index(later["text"])
        assert text.count(later["text"]) == 1
        assert ctx.client._prefetched_events == []

    def test_json_wait_deduplicates_unowned_prefetch_transcript_occurrence(
        self, ctx,
    ):
        """A durable unowned row present in both delivery lanes appears once."""
        from vnflight.handlers import handle_wait

        opening = {
            "_seq": 10,
            "type": "narration",
            "text": "Another night at the end of the world.",
            "action_id": 53,
        }
        mug = {
            "_seq": 11,
            "type": "narration",
            "text": "The mug has gone cold beside her hand.",
        }
        later = {
            "_seq": 12,
            "type": "narration",
            "text": "The same words can still recur later.",
        }
        pending = {
            "id": "opening-menu",
            "type": "choice_request",
            "choices": ["Read the overnight log."],
        }
        ctx.client._wait_result = MockWaitResult(
            events=[opening], pending=pending)
        ctx.client._prefetched_events = [dict(mug), dict(later)]
        ctx.client._state = {"pending_request": pending}
        ctx.client._transcript = [opening, dict(mug), dict(later)]

        result = handle_wait(ctx, {
            "timeout": 5,
            "format": "json",
            "_ordinary_only": True,
            "_ordinary_action_id": 53,
        })

        assert [item["text"] for item in result["story"]] == [
            opening["text"], mug["text"], later["text"],
        ]
        assert ctx.client._prefetched_events == []

    def test_scoped_wait_defers_transcript_rescue_at_foreign_prefetch(
        self, ctx,
    ):
        """A nonce-scoped response cannot absorb another action's prefix."""
        from vnflight.handlers import handle_wait

        opening = {
            "_seq": 10, "type": "narration", "text": "Current action.",
            "action_id": 53,
        }
        foreign = {
            "_seq": 11, "type": "dialogue", "text": "Later action.",
            "action_id": 54,
        }
        later = {
            "_seq": 12, "type": "narration", "text": "Transcript tail.",
            "action_id": 53,
        }
        pending = {
            "id": "next", "type": "choice_request", "choices": ["Wait."],
        }
        ctx.client._wait_result = MockWaitResult(
            events=[opening],
            pending=pending,
        )
        ctx.client._prefetched_events = [foreign]
        ctx.client._state = {
            "pending_request": pending,
            "screen": {
                "buttons": [{"label": "Foreign button", "screen": "menu"}],
            },
        }
        ctx.client._game_state = {
            "interactions": [{
                "type": "choice",
                "id": "foreign-menu",
                "choices": [{"label": "Foreign choice", "value": 0}],
            }],
        }
        ctx.client._transcript = [opening, foreign, later]

        result = handle_wait(ctx, {
            "timeout": 5,
            "_ordinary_only": True,
            "_ordinary_action_id": 53,
        })

        assert "Transcript tail." not in result.get("text", "")
        assert "pending" not in result
        assert "buttons" not in result
        assert result["_foreign_action_boundary"] is True
        assert ctx.client._prefetched_events == [foreign]
        assert (53, 12) not in ctx.client._delivered_action_events
        assert not any(
            name == "game_state" for name, _payload in ctx.client.calls
        )
        assert not any(name == "_get" for name, _payload in ctx.client.calls)

    def test_withheld_decision_wait_tells_the_agent_to_wait_again(self, ctx):
        """A fenced menu must not read as "the game has no decision"."""
        from vnflight.handlers import handle_wait, render_tool_result_text

        opening = {
            "_seq": 10, "type": "narration", "text": "Current action.",
            "action_id": 53,
        }
        foreign = {
            "_seq": 11, "type": "dialogue", "text": "Later action.",
            "action_id": 54,
        }
        later = {
            "_seq": 12, "type": "narration", "text": "Transcript tail.",
            "action_id": 53,
        }
        pending = {
            "id": "next", "type": "choice_request", "choices": ["Wait."],
        }
        ctx.client._wait_result = MockWaitResult(
            events=[opening], pending=pending)
        ctx.client._prefetched_events = [foreign]
        ctx.client._state = {"pending_request": pending}
        ctx.client._transcript = [opening, foreign, later]

        result = handle_wait(ctx, {
            "timeout": 5,
            "_ordinary_only": True,
            "_ordinary_action_id": 53,
        })

        assert result["_foreign_action_boundary"] is True
        assert "pending" not in result
        warning = result["warning"]
        assert "withheld" in warning
        assert "wait()" in warning
        rendered = render_tool_result_text(result)
        assert warning in rendered
        assert opening["text"] in rendered

    def test_parked_bookkeeping_prefetch_does_not_withhold_the_menu(self, ctx):
        """A stats_update parked by an earlier command is not a fence."""
        from vnflight.handlers import handle_wait

        opening = {
            "_seq": 10, "type": "narration", "text": "Current action.",
            "action_id": 53,
        }
        stats = {
            "_seq": 11, "type": "stats_update", "changed": {"trust": 2},
        }
        later = {
            "_seq": 12, "type": "narration", "text": "Transcript tail.",
            "action_id": 53,
        }
        pending = {
            "id": "next", "type": "choice_request", "choices": ["Wait."],
        }
        ctx.client._wait_result = MockWaitResult(
            events=[opening], pending=pending)
        ctx.client._prefetched_events = [stats]
        ctx.client._state = {"pending_request": pending}
        ctx.client._transcript = [opening, stats, later]

        result = handle_wait(ctx, {
            "timeout": 5,
            "_ordinary_only": True,
            "_ordinary_action_id": 53,
        })

        assert result["_foreign_action_boundary"] is False
        assert "Wait." in result.get("pending", "")
        assert result.get("warning") is None
        assert "Transcript tail." in result["text"]
        # Stepped over, not consumed: the ordinary lane still owes it.
        assert ctx.client._prefetched_events == [stats]

    def test_wait_does_not_append_late_story_from_diagnostic_only_event(self, ctx):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[
                {"_seq": 10, "type": "anomaly", "kind": "duplicate_buttons"},
            ],
            pending={
                "id": "next",
                "type": "choice_request",
                "choices": ["Continue"],
            },
        )
        ctx.client._state = {
            "pending_request": {
                "id": "next",
                "type": "choice_request",
                "choices": ["Continue"],
            },
        }
        ctx.client._transcript = [
            {"_seq": 11, "type": "narration", "text": "Old history."},
        ]

        result = handle_wait(ctx, {"timeout": 5})

        assert "Old history." not in result.get("text", "")
        assert "Continue" in result["pending"]

    def test_wait_drops_stale_screen_text_when_underlay_decision_appears(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[{
                "type": "screen_text",
                "texts": ["Visited locations:\n  - Howler's Dell"],
            }],
            pending=None,
        )
        ctx.client._state = {
            "status": "idle",
            "game_state": {
                "interactions": [
                    {
                        "category": "topics",
                        "index": 1,
                        "screen": "nvl",
                        "display_label": "Ask about the hamlet.",
                        "type": "topic",
                        "disabled": False,
                    },
                ],
            },
            "screen": {
                "buttons": [
                    {
                        "label": "Ask about the hamlet.",
                        "screen": "nvl",
                        "actions": ["Jump"],
                    },
                ],
            },
        }

        result = handle_wait(ctx, {"timeout": 5})

        assert "Visited locations" not in result.get("text", "")
        assert "Visited locations" not in result.get("screen_text", "")
        assert "Ask about the hamlet" in result["buttons"]

    def test_wait_drops_stale_screen_text_when_current_screen_has_underlay_buttons(
        self,
        ctx,
    ):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[{
                "type": "screen_text",
                "texts": ["Visited locations:\n  - Howler's Dell"],
            }],
            pending=None,
            screen={
                "buttons": [
                    {
                        "label": "Ask about the hamlet.",
                        "screen": "nvl",
                        "actions": ["Jump"],
                    },
                ],
            },
        )
        ctx.client._game_state = {
            "interactions": [
                {
                    "category": "topics",
                    "index": 1,
                    "screen": "nvl",
                    "display_label": "Ask about the hamlet.",
                    "type": "topic",
                    "disabled": False,
                },
            ],
        }

        result = handle_wait(ctx, {"timeout": 5})

        assert "Visited locations" not in result.get("text", "")
        assert "Ask about the hamlet" in result["buttons"]

    def test_wait_polls_briefly_for_late_decision(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "A scene finishes."}],
            pending=None,
        )
        states = [
            {},
            {
                "status": "waiting_for_input",
                "pending_request": {
                    "type": "choice_request",
                    "id": "next-1",
                    "choices": ["Open the door", "Wait"],
                },
            },
        ]

        def delayed_state():
            ctx.client.calls.append(("state", {}))
            return states.pop(0) if states else states[-1]

        ctx.client.state = delayed_state

        result = handle_wait(ctx, {"timeout": 5})

        assert "CHOICE REQUIRED" in result["pending"]
        assert len([c for c in ctx.client.calls if c[0] == "state"]) == 2

    def test_wait_does_not_merge_acted_request_from_state(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._acted_request_id = "old-1"
        ctx.client._wait_result = MockWaitResult(events=[], pending=None)
        ctx.client._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": "old-1",
                "choices": ["Already chosen"],
            },
        }

        result = handle_wait(ctx, {})

        assert "pending" not in result

    def test_after_wait_hook(self, ctx):
        from vnflight.handlers import handle_wait
        ctx.client._wait_result = MockWaitResult(events=[])
        hook_called = []
        def my_hook(data, result):
            hook_called.append(True)
            data["custom"] = True
            return data
        ctx.hooks.after_wait = my_hook
        handle_wait(ctx, {})
        assert hook_called


# ---------------------------------------------------------------------------
# Lifecycle handlers (launch, stop, games, set_profile)
# ---------------------------------------------------------------------------

class TestLifecycleHandlers:

    def test_run_cli_keeps_json_stdout_when_stderr_has_diagnostics(
        self, ctx, monkeypatch,
    ):
        from types import SimpleNamespace
        import subprocess
        from vnflight import handlers

        monkeypatch.setattr(
            handlers, "_cli_invocation", lambda: (["python", "vnflight.py"], None),
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=1,
                stdout='{"success": false, "slot_id": 4}',
                stderr="waiting for launcher",
            ),
        )

        result = handlers._run_cli(ctx, "--json", "launch", "game")

        assert result["output"] == '{"success": false, "slot_id": 4}'
        assert result["stderr"] == "waiting for launcher"
        assert result["error"] == "waiting for launcher"

    def test_launch_missing_game(self, ctx):
        from vnflight.handlers import handle_launch
        result = handle_launch(ctx, {})
        assert "error" in result

    def test_stop_via_cli_hook(self, ctx):
        from vnflight.handlers import handle_stop
        cli_calls = []
        ctx.hooks.run_cli = lambda *args, **kw: (cli_calls.append(args), {"ok": True})[1]
        result = handle_stop(ctx, {"game": "test_game"})
        assert result["ok"] is True
        assert "--yes" in cli_calls[0]
        assert "stop" in cli_calls[0]
        assert "test_game" in cli_calls[0]

    def test_expired_transport_deadline_blocks_command_and_cli_mutations(
        self, ctx, monkeypatch,
    ):
        import time
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {"fast_forward": True}},
        })
        ctx.hooks.run_cli = lambda *_args, **_kwargs: pytest.fail(
            "expired CLI mutation started")
        ctx.client.calls.clear()
        deadline = time.time() - 1.0
        cases = [
            ("save", {}),
            ("load", {}),
            ("auto_skip", {"enabled": True}),
            ("command", {"name": "set", "args": {"key": "x"}}),
            ("launch", {"game_id": "echoes_of_tomorrow"}),
            ("stop", {}),
            ("set_profile", {"profile": "turbo"}),
        ]

        for name, arguments in cases:
            result = handlers.handle_tool(ctx, name, {
                **arguments, "_transport_deadline": deadline,
            })
            assert result.get("reason") == (
                "transport_timeout_before_submission"), name

        assert not [call for call in ctx.client.calls if call[0] == "command"]

    def test_launch_rejects_timeout_larger_than_transport_window(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        clock = {"now": 100.0}
        calls = []
        monkeypatch.setattr(
            handlers.time, "time", lambda: clock["now"])
        monkeypatch.setattr(
            handlers.time, "sleep",
            lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        )

        def run_cli(*args, timeout=90):
            calls.append((args, timeout))
            clock["now"] += timeout
            return {"ok": True, "output": "launched"}

        ctx.hooks.run_cli = run_cli
        ctx.client.calls.clear()
        ctx.client.cursor = 88
        ctx.client.last_request_id = "old-request"
        ctx.client.slot_prefix = "/old"
        result = handlers.handle_launch(ctx, {
            "game_id": "echoes_of_tomorrow",
            "timeout": 600,
            "_transport_deadline": 210.0,
        })

        assert result["reason"] == "launch_timeout_exceeds_transport_window"
        assert calls == []
        # The refusal names a window that leaves the child its startup
        # reserve: bridge readiness plus the setup phase (Astra review).
        from vnflight.lib import BRIDGE_READINESS_TIMEOUT_S
        assert handlers._LAUNCH_STARTUP_RESERVE_S == BRIDGE_READINESS_TIMEOUT_S + 15.0
        assert ctx.client.cursor == 88
        assert ctx.client.last_request_id == "old-request"
        assert ctx.client.slot_prefix == "/old"

    def test_launch_pre_stop_failure_preserves_existing_binding(self, ctx):
        from vnflight import handlers

        calls = []
        ctx.client.slot_prefix = "/old"
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or {"ok": False, "error": "stop refused"}
        )

        result = handlers.handle_tool(ctx, "launch", {
            "game_id": "echoes_of_tomorrow",
            "_stop_existing": True,
        })

        assert result["reason"] == "launch_pre_stop_failed"
        assert "stop refused" in result["error"]
        assert len(calls) == 1
        assert "stop" in calls[0][0]
        assert ctx.client.slot_prefix == "/old"

    def test_launch_rejects_nonpositive_timeout_before_cli(self, ctx):
        from vnflight import handlers

        ctx.hooks.run_cli = lambda *args, **kwargs: pytest.fail(
            "nonpositive launch reached the CLI")

        result = handlers.handle_launch(ctx, {
            "game_id": "echoes_of_tomorrow",
            "timeout": 0,
        })

        assert result == {"error": "Launch timeout must be greater than zero."}

    def test_launch_preserves_omitted_timeout_for_launcher_default(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )

        result = handlers.handle_launch(ctx, {"game_id": "launcher_game"})

        assert result == {"error": "not launched"}
        assert "--quiet" in calls[0][0]
        assert "--timeout" not in calls[0][0]
        # No transport deadline: the child gets the launcher's default
        # connect window plus the whole startup reserve (Astra round 3).
        assert calls[0][1]["timeout"] == pytest.approx(
            handlers._LAUNCHER_DEFAULT_CONNECT_S
            + handlers._LAUNCH_STARTUP_RESERVE_S + 5.0)

    def test_launch_explicit_timeout_without_deadline_is_accepted(
        self, ctx, monkeypatch,
    ):
        """Astra round 3: with no MCP deadline every explicit timeout was
        refused ('exceeds the 10s window') because the budget formula
        predated the startup reserve."""
        from vnflight import handlers
        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )
        result = handlers.handle_launch(ctx, {"game_id": "g", "timeout": 30})
        assert result == {"error": "not launched"}
        assert ("--timeout", "30.0") == tuple(
            calls[0][0][calls[0][0].index("--timeout"):][:2])
        assert calls[0][1]["timeout"] == pytest.approx(
            30.0 + handlers._LAUNCH_STARTUP_RESERVE_S + 5.0)

    def test_launch_omitted_timeout_under_a_deadline_is_bounded(
        self, ctx, monkeypatch,
    ):
        """Astra round 3: an omitted timeout under an MCP deadline used to
        leave the child on the launcher's 90 s default inside a ~100 s
        subprocess budget; it now receives the bounded window."""
        import time as _time
        from vnflight import handlers
        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )
        handlers.handle_launch(ctx, {
            "game_id": "g", "_transport_deadline": _time.time() + 110.0,
        })
        args = calls[0][0]
        assert "--timeout" in args
        forwarded = float(args[args.index("--timeout") + 1])
        assert forwarded < handlers._LAUNCHER_DEFAULT_CONNECT_S
        assert forwarded + handlers._LAUNCH_STARTUP_RESERVE_S <= calls[0][1]["timeout"] + 1e-6

    def test_launch_short_budget_bounds_the_child_connect_window(
        self, ctx, monkeypatch,
    ):
        """Astra round 4: with 40 s remaining the child ran 30 s without
        --timeout, i.e. on the launcher's 90 s default, and was killed
        before its receipt. The window is now explicit (floored at 1 s)."""
        import time as _time
        from vnflight import handlers
        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )
        ctx.client.is_up = lambda timeout=2.0: True
        handlers.handle_launch(ctx, {
            "game_id": "g", "_transport_deadline": _time.time() + 40.0,
        })
        args = calls[0][0]
        assert "--timeout" in args
        forwarded = float(args[args.index("--timeout") + 1])
        # Bridge up: 30 s of child time minus the 15 s setup reserve.
        assert forwarded == pytest.approx(15.0, abs=0.5)
        assert calls[0][1]["timeout"] < 40.0

    def test_launch_rechecks_the_startup_reserve_after_a_pre_stop(
        self, ctx, monkeypatch,
    ):
        """Astra round 6: 55 s initially with the bridge up passed the
        first check; stopping the old game took 15 s and the bridge with
        it, and a 30 s child then started against a 40 s startup need."""
        from vnflight import handlers
        clock = {"now": 100.0}
        monkeypatch.setattr(handlers.time, "time", lambda: clock["now"])
        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )
        up = {"value": True}
        ctx.client.is_up = lambda timeout=2.0: up["value"]
        ctx.client.slot_prefix = "/7"

        def fake_stop(ctx_, params_):
            clock["now"] += 15.0
            up["value"] = False
            return {"ok": True, "success": True}

        monkeypatch.setattr(handlers, "handle_stop", fake_stop)
        result = handlers.handle_launch(ctx, {
            "game_id": "g", "_stop_existing": True,
            "_transport_deadline": 155.0,
        })
        assert result["reason"] == "launch_startup_reserve_exceeds_budget_after_pre_stop"
        assert calls == []

    def test_launch_refuses_when_startup_cannot_fit_the_budget(
        self, ctx, monkeypatch,
    ):
        """Astra round 5: 30 s remaining gave the child 20 s while bridge
        readiness alone may take 25 s; the connect bound cannot protect
        startup, so the launch is refused before anything is started."""
        import time as _time
        from vnflight import handlers
        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )
        ctx.client.is_up = lambda timeout=2.0: False
        result = handlers.handle_launch(ctx, {
            "game_id": "g", "_transport_deadline": _time.time() + 30.0,
        })
        assert result["reason"] == "launch_startup_reserve_exceeds_budget"
        assert calls == []
        # The same budget with the bridge already up proceeds.
        ctx.client.is_up = lambda timeout=2.0: True
        handlers.handle_launch(ctx, {
            "game_id": "g", "_transport_deadline": _time.time() + 30.0,
        })
        assert len(calls) == 1

    def test_launch_forwards_debug_override(self, ctx, monkeypatch):
        from vnflight import handlers
        calls = []
        ctx.hooks.run_cli = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"error": "not launched"}
        )
        handlers.handle_launch(ctx, {"game_id": "g", "debug": True})
        assert "--debug" in calls[0][0]
        handlers.handle_launch(ctx, {"game_id": "g", "debug": False})
        assert "--no-debug" in calls[1][0]
        handlers.handle_launch(ctx, {"game_id": "g"})
        assert "--debug" not in calls[2][0] and "--no-debug" not in calls[2][0]

    def test_launch_timeout_preserves_client_token_and_returns_safe_id(
        self, ctx, monkeypatch,
    ):
        import hashlib
        import secrets
        from vnflight import handlers

        monkeypatch.setattr(
            secrets, "token_urlsafe", lambda _n: "reserved-secret",
        )
        ctx.client.token = "existing-admin"
        ctx.hooks.run_cli = lambda *_args, **_kwargs: {
            "error": "timed out", "reason": "cli_timeout",
        }

        result = handlers.handle_launch(ctx, {
            "game_id": "echoes_of_tomorrow",
        })

        assert result["reason"] == "launch_acceptance_unknown"
        assert result["retry_safe"] is False
        assert result["reservation_id"] == hashlib.sha256(
            b"reserved-secret").hexdigest()[:16]
        assert "reservation_token" not in result
        assert ctx.client.token == "existing-admin"

    def test_launch_parses_structured_stdout_on_nonzero_cli_exit(
        self, ctx, monkeypatch,
    ):
        import json
        import secrets
        from vnflight import handlers

        monkeypatch.setattr(
            secrets, "token_urlsafe", lambda _n: "partial-slot-token",
        )
        ctx.hooks.run_cli = lambda *_args, **_kwargs: {
            "error": "launcher warning on stderr",
            "stderr": "launcher warning on stderr",
            "output": json.dumps({
                "success": False,
                "partial_launch": True,
                "slot_id": 7,
                "error": "registered but not ready",
            }),
        }
        ctx.client._dynamic_slot_selector = "echoes"
        ctx.client._dynamic_slot_resolved_at = 123.0

        result = handlers.handle_launch(ctx, {
            "game_id": "echoes_of_tomorrow",
        })

        assert result["partial_launch"] is True
        assert result["slot_id"] == 7
        assert result["error"] == "registered but not ready"
        assert result["recovery_attached"] is True
        assert ctx.client.slot_prefix == "/7"
        assert ctx.client.token == "partial-slot-token"
        assert ctx.client._dynamic_slot_selector is None
        assert ctx.client._dynamic_slot_resolved_at == 0.0

    def test_launch_deadline_after_child_reports_explicit_partial_slot(
        self, ctx, monkeypatch,
    ):
        import json
        from vnflight import handlers

        clock = {"now": 100.0}
        monkeypatch.setattr(handlers.time, "time", lambda: clock["now"])

        def run_cli(*_args, **_kwargs):
            clock["now"] = 130.0
            return {
                "ok": True,
                "output": json.dumps({
                    "success": True,
                    "slot_id": 9,
                    "message": "launched",
                }),
            }

        ctx.hooks.run_cli = run_cli
        # Bridge already up: only the setup reserve applies, so a 30 s
        # budget still starts the child (and gets its late receipt).
        ctx.client.is_up = lambda timeout=2.0: True
        result = handlers.handle_launch(ctx, {
            "game_id": "echoes_of_tomorrow",
            "_transport_deadline": 130.0,
        })

        assert result["partial_launch"] is True
        assert result["slot_id"] == 9
        assert result["reason"] == "launch_attachment_timeout"
        assert ctx.client.slot_prefix == "/9"

    def test_successful_load_reconciles_locally_after_deadline(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        clock = {"now": 100.0}
        monkeypatch.setattr(
            handlers.time, "time", lambda: clock["now"])

        def command(name, **kwargs):
            assert name == "load"
            clock["now"] = 101.0
            return {"ok": True, "success": True}

        ctx.client.command = command
        ctx.client.cursor = 88
        ctx.client.last_request_id = "old-request"
        ctx.client._prefetched_events = [{"type": "narration"}]
        ctx.overlay.timeline_source_sequences = {"old": 5}
        ctx.hooks.after_command = lambda *_args: pytest.fail(
            "post-deadline load reconciliation performed I/O")

        result = handlers.handle_load(ctx, {
            "slot": "quick", "_transport_deadline": 101.0,
        })

        assert result["success"] is True
        assert ctx.client.cursor == 0
        assert ctx.client.last_request_id is None
        assert ctx.client._prefetched_events == []
        assert ctx.overlay.timeline_source_sequences == {}

    def test_games_via_cli_hook(self, ctx):
        from vnflight.handlers import handle_games
        import json
        ctx.hooks.run_cli = lambda *args, **kw: {"ok": True, "output": json.dumps(["game1", "game2"])}
        result = handle_games(ctx, {})
        assert result["games"] == ["game1", "game2"]

    def test_set_profile_missing(self, ctx):
        from vnflight.handlers import handle_set_profile
        result = handle_set_profile(ctx, {})
        assert "error" in result

    def test_set_profile_uses_live_client_token_path(self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {
                "turbo": {
                    "auto_advance": True,
                    "auto_advance_delay": 0.05,
                    "fast_forward": True,
                }
            }
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "auto_advance", "old_value": False, "value": True},
                {"key": "auto_advance_delay", "old_value": 0.5, "value": 0.05},
                {"key": "fast_forward", "old_value": False, "value": True},
            ],
        }
        result = handle_set_profile(ctx, {"profile": "turbo"})
        assert result["ok"] is True
        assert ("command", ("set", {"changes": {
            "auto_advance": True,
            "auto_advance_delay": 0.05,
            "fast_forward": True,
        }})) in ctx.client.calls
        assert ("set_auto_advance", {"enabled": True, "delay": 0.05}) in ctx.client.calls

    def test_set_profile_reports_live_client_set_failure(self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {"fast_forward": True}}
        })
        ctx.client._command_results["set"] = {
            "ok": False,
            "error": "Invalid or missing token for this slot",
        }

        result = handle_set_profile(ctx, {"profile": "turbo"})

        assert "error" in result
        assert "Invalid or missing token" in result["error"]

    def test_set_profile_preserves_acceptance_unknown_nonce(self, ctx, monkeypatch):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {"fast_forward": True}},
        })
        ctx.client._command_results["set"] = {
            "ok": False,
            "success": False,
            "acceptance_unknown": True,
            "mutation_may_have_applied": True,
            "command_nonce": "profile-attempt-1",
            "reason": "command_result_timeout_after_submission",
            "error": "Command 'set' was submitted but not confirmed",
        }

        result = handlers.handle_set_profile(ctx, {"profile": "turbo"})

        assert result["acceptance_unknown"] is True
        assert result["mutation_may_have_applied"] is True
        assert result["command_nonce"] == "profile-attempt-1"
        assert result["profile"] == "turbo"

    def test_set_profile_reuses_supplied_command_nonce(self, ctx, monkeypatch):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {"fast_forward": True}},
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "fast_forward", "old_value": False, "value": True},
            ],
        }

        result = handlers.handle_set_profile(ctx, {
            "profile": "turbo", "command_nonce": "profile-attempt-1",
        })

        assert result["ok"] is True
        assert (
            "command",
            ("set", {
                "changes": {"fast_forward": True},
                "_nonce": "profile-attempt-1",
            }),
        ) in ctx.client.calls

    def test_set_profile_reports_success_when_redundant_followup_hits_deadline(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        clock = {"now": 100.0}
        monkeypatch.setattr(
            handlers.time, "time", lambda: clock["now"])
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {
                "auto_advance": True,
                "auto_advance_delay": 0.05,
            }},
        })

        def command(name, **kwargs):
            assert name == "set"
            clock["now"] = 101.0
            return {
                "success": True,
                "applied": [
                    {"key": "auto_advance", "old_value": False,
                     "value": True},
                    {"key": "auto_advance_delay", "old_value": 0.5,
                     "value": 0.05},
                ],
            }

        ctx.client.command = command
        result = handlers.handle_set_profile(ctx, {
            "profile": "turbo", "_transport_deadline": 101.0,
        })

        assert result["ok"] is True
        assert "applied" in result["warning"].lower()
        assert not [
            call for call in ctx.client.calls
            if call[0] == "set_auto_advance"
        ]

    def test_set_profile_reports_shim_errors_list(self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {"fast_forward": True}}
        })
        ctx.client._command_results["set"] = {
            "success": False,
            "errors": ["Unknown key: 'fast_forward'", "boom"],
        }

        result = handle_set_profile(ctx, {"profile": "turbo"})

        assert "error" in result
        assert "Unknown key" in result["error"]
        assert "boom" in result["error"]

    def test_set_profile_default_restores_profile_relevant_defaults(
            self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {
                "turbo": {
                    "fast_forward": True,
                    "auto_advance_delay": 0.05,
                }
            }
        })
        ctx.client._command_results["get_defaults"] = {
            "success": True,
            "defaults": {
                "fast_forward": False,
                "auto_advance_delay": 0.5,
                "unrelated_mod_key": "keep",
            },
        }
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "fast_forward", "old_value": True, "value": False},
                {"key": "auto_advance_delay", "old_value": 0.05, "value": 0.5},
            ],
        }

        result = handle_set_profile(ctx, {"profile": "default"})

        assert result["ok"] is True
        assert result["profile"] == "default"
        assert ("command", ("get_defaults", {})) in ctx.client.calls
        assert ("command", ("set", {"changes": {
            "fast_forward": False,
            "auto_advance_delay": 0.5,
        }})) in ctx.client.calls
        assert all(
            call[0] != "set_auto_advance"
            for call in ctx.client.calls
        )

    def test_set_profile_default_reserves_retry_nonce_for_mutation(
            self, ctx, monkeypatch):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"turbo": {"fast_forward": True}},
        })
        ctx.client._command_results["get_defaults"] = {
            "success": True,
            "defaults": {"fast_forward": False},
        }
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "fast_forward", "old_value": True, "value": False},
            ],
        }

        result = handlers.handle_set_profile(ctx, {
            "profile": "default",
            "command_nonce": "default-profile-attempt-1",
        })

        assert result["ok"] is True
        assert ("command", ("get_defaults", {})) in ctx.client.calls
        assert (
            "command",
            ("set", {
                "changes": {"fast_forward": False},
                "_nonce": "default-profile-attempt-1",
            }),
        ) in ctx.client.calls

    # --- turbo profile <-> shim turbo key -----------------------------------
    #
    # set_profile("turbo") is the single UX for turbo mode, so the shipped
    # bundle has to carry the shim key. These drive the shipped default
    # profiles (not a fixture) — the wiring only pays off if the file itself
    # is right.

    @staticmethod
    def _real_profiles():
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        template = root / "release_assets" / "vnflight.release.json"
        if not template.exists():
            template = root / "vnflight.default.json"
        data = json.loads(template.read_text(encoding="utf-8"))
        return data

    @staticmethod
    def _set_changes(ctx):
        for name, args in ctx.client.calls:
            if name == "command" and args[0] == "set":
                return args[1]["changes"]
        raise AssertionError("no set command issued")

    def test_turbo_profile_bundle_turns_the_shim_turbo_key_on(
            self, ctx, monkeypatch):
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        real = self._real_profiles()
        assert real["profiles"]["turbo"]["turbo"] is True
        monkeypatch.setattr(handlers, "_load_config", lambda: real)
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": key, "old_value": value, "value": value}
                for key, value in real["profiles"]["turbo"].items()
            ],
        }

        result = handle_set_profile(ctx, {"profile": "turbo"})

        assert result["ok"] is True
        assert self._set_changes(ctx)["turbo"] is True

    def test_viewer_profiles_turn_the_shim_turbo_key_back_off(
            self, ctx, monkeypatch):
        """Switching turbo -> a hybrid profile must restore normal
        rendering, the same way those profiles clear fast_forward."""
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        real = self._real_profiles()
        monkeypatch.setattr(handlers, "_load_config", lambda: real)
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": key, "old_value": value, "value": value}
                for key, value in real["profiles"]["hybrid_text"].items()
            ],
        }

        handle_set_profile(ctx, {"profile": "hybrid_text"})

        assert self._set_changes(ctx)["turbo"] is False
        for name in ("hybrid", "hybrid_text", "hybrid_audio"):
            assert real["profiles"][name]["turbo"] is False

    def test_set_profile_default_restores_turbo_via_the_key_union(
            self, ctx, monkeypatch):
        """The "default" branch filters get_defaults through the union of
        keys appearing in ANY bundle — so putting `turbo` in the turbo
        bundle is what makes set_profile("default") restore it."""
        from vnflight import handlers
        from vnflight.handlers import handle_set_profile
        real = self._real_profiles()
        monkeypatch.setattr(handlers, "_load_config", lambda: real)
        ctx.client._command_results["get_defaults"] = {
            "success": True,
            "defaults": {
                "turbo": False,
                "auto_advance_delay": 0.3,
                "unrelated_mod_key": "keep",
            },
        }
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "turbo", "old_value": True, "value": False},
                {"key": "auto_advance_delay", "old_value": 0.05,
                 "value": 0.3},
            ],
        }

        result = handle_set_profile(ctx, {"profile": "default"})

        assert result["ok"] is True
        changes = self._set_changes(ctx)
        assert changes["turbo"] is False
        assert "unrelated_mod_key" not in changes

    def test_launch_with_profile(self, ctx, monkeypatch):
        import json
        from vnflight import handlers
        from vnflight.handlers import handle_launch
        cli_calls = []
        ctx.hooks.run_cli = lambda *args, **kw: (
            cli_calls.append(args),
            {"ok": True, "output": json.dumps({
                "success": True, "slot_id": 3, "message": "launched",
            })},
        )[1]
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {
                "turbo": {
                    "auto_advance": True,
                    "auto_advance_delay": 0.05,
                }
            }
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "auto_advance", "old_value": False, "value": True},
                {"key": "auto_advance_delay", "old_value": 0.5, "value": 0.05},
            ],
        }

        def auto_select_slot(game_hint=None):
            ctx.client._dynamic_slot_selector = game_hint
            ctx.client._dynamic_slot_resolved_at = 99.0
            ctx.client.slot_prefix = "/3"
            return True

        ctx.client.auto_select_slot = auto_select_slot

        result = handle_launch(ctx, {"game_id": "test", "profile": "turbo"})
        # Launch still uses the CLI; profile apply uses the live client so it
        # keeps the MCP server's slot token.
        assert len(cli_calls) == 1
        assert "launch" in cli_calls[0]
        assert ("command", ("set", {"changes": {
            "auto_advance": True,
            "auto_advance_delay": 0.05,
        }})) in ctx.client.calls
        assert ("set_auto_advance", {"enabled": True, "delay": 0.05}) in ctx.client.calls
        assert result["profile"] == "turbo"
        assert ctx.client.slot_prefix == "/3"
        assert ctx.client._dynamic_slot_selector is None
        assert ctx.client._dynamic_slot_resolved_at == 0.0

    def test_set_profile_reports_only_actual_changes(self, ctx, monkeypatch):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"hybrid_text": {
                "auto_advance": True,
                "text_cps": 45,
            }}
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "auto_advance", "old_value": True, "value": True},
                {"key": "text_cps", "old_value": 30, "value": 45},
            ],
        }

        result = handlers.handle_set_profile(
            ctx, {"profile": "hybrid_text"})

        assert result["message"] == (
            "Applied profile 'hybrid_text' (1 setting changed)"
        )

    def test_set_profile_reports_already_applied(self, ctx, monkeypatch):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"hybrid_text": {"text_cps": 45}}
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "text_cps", "old_value": 45, "value": 45},
            ],
        }

        result = handlers.handle_set_profile(
            ctx, {"profile": "hybrid_text"})

        assert result["message"] == "Profile 'hybrid_text' already applied"

    def test_set_profile_accepts_values_coerced_to_live_setting_types(
        self, ctx, monkeypatch,
    ):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"string_config": {
                "text_cps": "45",
                "auto_advance": "false",
                "post_action_delay": "0.5",
            }}
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "text_cps", "old_value": 30, "value": 45},
                {"key": "auto_advance", "old_value": True, "value": False},
                {"key": "post_action_delay", "old_value": 0.2, "value": 0.5},
            ],
        }

        result = handlers.handle_set_profile(
            ctx, {"profile": "string_config"})

        assert result["ok"] is True
        assert len(result["applied"]) == 3

    @pytest.mark.parametrize("applied", [
        None,
        [],
        [{"key": "text_cps", "value": 45}],
        [{"key": "text_cps", "old_value": 30, "value": 40}],
    ])
    def test_set_profile_rejects_invalid_success_receipt(
            self, ctx, monkeypatch, applied):
        from vnflight import handlers

        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "profiles": {"hybrid_text": {"text_cps": 45}}
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": applied,
        }

        result = handlers.handle_set_profile(
            ctx, {"profile": "hybrid_text"})

        assert result["ok"] is False
        assert result["reason"] == "invalid_application_receipt"
        assert result["mutation_may_have_applied"] is True

    def test_launch_applies_game_default_profile(self, ctx, monkeypatch):
        import json
        from vnflight import handlers

        cli_calls = []
        ctx.hooks.run_cli = lambda *args, **kw: (
            cli_calls.append(args)
            or {"ok": True, "output": json.dumps({
                "success": True, "slot_id": 4, "message": "launched",
            })}
        )
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "games": {
                "echoes_of_tomorrow": {"default_profile": "turbo"},
            },
            "profiles": {"turbo": {"turbo": True}},
        })
        ctx.client._command_results["set"] = {
            "success": True,
            "applied": [
                {"key": "turbo", "old_value": False, "value": True},
            ],
        }

        result = handlers.handle_launch(
            ctx, {"game_id": "echoes_of_tomorrow"})

        assert "--defer-default-profile" in cli_calls[0]
        assert result["profile"] == "turbo"
        assert ("command", ("set", {"changes": {"turbo": True}})) \
            in ctx.client.calls

    def test_launch_does_not_reapply_default_profile_from_child_receipt(
        self, ctx, monkeypatch,
    ):
        import json
        from vnflight import handlers

        ctx.hooks.run_cli = lambda *args, **kw: {
            "ok": True,
            "output": json.dumps({
                "success": True,
                "slot_id": 4,
                "message": "launched",
                "profile_applied": "turbo",
                "profile_skipped_keys": [
                    {"key": "auto_advance", "reason": "--auto"},
                ],
            }),
        }
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "games": {
                "echoes_of_tomorrow": {"default_profile": "turbo"},
            },
            "profiles": {"turbo": {"turbo": True}},
        })

        result = handlers.handle_launch(
            ctx, {"game_id": "echoes_of_tomorrow"},
        )

        assert result["profile"] == "turbo"
        assert result["profile_applied"] == "turbo"
        assert not [
            call for call in ctx.client.calls
            if call[0] == "command" and call[1][0] == "set"
        ]

    def test_launch_explicit_profile_defers_child_default(
        self, ctx, monkeypatch,
    ):
        import json
        from vnflight import handlers

        cli_calls = []
        ctx.hooks.run_cli = lambda *args, **kw: (
            cli_calls.append(args)
            or {"ok": True, "output": json.dumps({
                "success": True, "slot_id": 4, "message": "launched",
            })}
        )
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "games": {
                "echoes_of_tomorrow": {"default_profile": "turbo"},
            },
        })
        applied = []
        monkeypatch.setattr(
            handlers, "handle_set_profile",
            lambda seen_ctx, params: applied.append(params["profile"])
            or {"ok": True},
        )

        result = handlers.handle_launch(ctx, {
            "game_id": "echoes_of_tomorrow",
            "profile": "cinematic",
        })

        assert "--defer-default-profile" in cli_calls[0]
        assert applied == ["cinematic"]
        assert result["profile"] == "cinematic"

    def test_launch_does_not_retry_failed_child_default_profile(
        self, ctx, monkeypatch,
    ):
        import json
        from vnflight import handlers

        ctx.hooks.run_cli = lambda *args, **kw: {
            "ok": True,
            "output": json.dumps({
                "success": True,
                "slot_id": 4,
                "message": "launched",
                "profile_error": "acceptance unknown",
                "profile_name": "turbo",
            }),
        }
        monkeypatch.setattr(handlers, "_load_config", lambda: {
            "games": {
                "echoes_of_tomorrow": {"default_profile": "turbo"},
            },
        })
        monkeypatch.setattr(
            handlers, "handle_set_profile",
            lambda *args: (_ for _ in ()).throw(
                AssertionError("ambiguous child profile must not be retried")
            ),
        )

        result = handlers.handle_launch(
            ctx, {"game_id": "echoes_of_tomorrow"},
        )

        assert result["partial_launch"] is True
        assert result["slot_id"] == 4
        assert result["reason"] == "launch_profile_failed"
        assert "acceptance unknown" in result["error"]

    def test_launch_clears_dynamic_slot_selector_before_rebinding(
            self, ctx, monkeypatch):
        import json
        from vnflight import handlers
        from vnflight.handlers import handle_launch

        selected = []

        def auto_select_slot(game_hint=None):
            selected.append(game_hint)
            ctx.client._dynamic_slot_selector = game_hint
            ctx.client._dynamic_slot_resolved_at = 456.0
            ctx.client.slot_prefix = "/3"
            return True

        ctx.hooks.run_cli = lambda *args, **kw: {
            "ok": True,
            "output": json.dumps({
                "success": True, "slot_id": 7, "message": "launched",
            }),
        }
        monkeypatch.setattr(handlers, "_load_config", lambda: {"games": {}})
        ctx.client.slot_prefix = "/2"
        ctx.client._dynamic_slot_selector = "latest:roadwarden"
        ctx.client._dynamic_slot_resolved_at = 123.0
        ctx.client.auto_select_slot = auto_select_slot

        result = handle_launch(ctx, {"game_id": "echoes_of_tomorrow"})

        assert result["ok"] is True
        assert selected == []
        assert ctx.client.slot_prefix == "/7"
        assert ctx.client._dynamic_slot_selector is None
        assert ctx.client._dynamic_slot_resolved_at == 0.0


class TestInputRetryIdentity:

    def test_handler_forwards_original_input_request_id(self, ctx):
        from vnflight.handlers import handle_input_text

        result = handle_input_text(ctx, {
            "text": "Elara",
            "request_id": "input-original",
            "wait": False,
        })

        assert result["ok"] is True
        assert ("input_text_options", {
            "deadline": None,
            "request_id": "input-original",
        }) in ctx.client.calls



# ---------------------------------------------------------------------------
# render_tool_result_text — error/warning must reach the agent (not be
# swallowed into "(no new events)" in default text mode).
# ---------------------------------------------------------------------------

def test_effective_choice_count_falls_back_to_rendered_pending():
    # call-screen choice screens have no bridge pending_request, so the
    # raw count is 0; the offset must fall back to the rendered (synthesized)
    # pending count, else numeric button resolution loses its offset.
    from vnflight.handlers import _effective_choice_count
    pre_rendered = {
        "_data": {
            "pending": {
                "type": "choice",
                "choices": [
                    {"label": "Toolkit", "index": 1},
                    {"label": "Equations", "index": 2},
                    {"label": "Logs", "index": 3},
                ],
            },
        },
    }
    # Empty raw pending -> fall back to rendered (3).
    assert _effective_choice_count({}, pre_rendered) == 3
    # Raw pending present -> use it (don't double-count).
    raw = {"type": "choice_request", "choices": ["Yes", "No"]}
    assert _effective_choice_count(raw, pre_rendered) == 2


def test_render_tool_result_text_surfaces_error():
    from vnflight.handlers import render_tool_result_text

    out = render_tool_result_text({"error": "No active choice request"})
    assert "No active choice request" in out
    assert out != "(no new events)"


def test_render_tool_result_text_surfaces_warning_with_content():
    from vnflight.handlers import render_tool_result_text

    out = render_tool_result_text({
        "warning": "Action reported failure after the scene advanced",
        "text": "The door creaks open.",
    })
    assert "Action reported failure after the scene advanced" in out
    assert "The door creaks open." in out


def test_render_tool_result_text_empty_still_quiet():
    from vnflight.handlers import render_tool_result_text

    assert render_tool_result_text({}) == "(no new events)"


def test_render_tool_result_text_treats_no_events_as_fallback_only():
    from vnflight.handlers import render_tool_result_text

    out = render_tool_result_text({
        "text": "(no new events)",
        "screen_text": "BUT IT CAN BE PREVENTED. TWO QUESTIONS.",
        "status": "(stats updated: evidence_count: 4)",
    })

    assert "BUT IT CAN BE PREVENTED" in out
    assert "evidence_count: 4" in out
    assert "(no new events)" not in out


def test_render_tool_result_text_keeps_no_events_as_empty_fallback():
    from vnflight.handlers import render_tool_result_text

    assert render_tool_result_text({"text": "(no new events)"}) == (
        "(no new events)"
    )


def test_render_tool_result_text_deduplicates_actions_embedded_in_pending():
    from vnflight.handlers import render_tool_result_text

    navigation = "--- NAVIGATION ---\n  Wait (disabled)  |  Settings  |  Archive"
    result = {
        "pending": (
            "--- CHOICE REQUIRED ---\n"
            "  1: Stay.\n\n"
            f"{navigation}\n"
            "Use act('number') or act('label') to respond."
        ),
        "buttons": navigation,
        "_footer": "  Day 0 | 30m before dusk",
    }

    rendered = render_tool_result_text(result)

    assert rendered.count("--- NAVIGATION ---") == 1
    assert rendered.endswith("Day 0 | 30m before dusk")


def test_cli_invocation_package_mode_targets_repo_vnflight_py():
    import os
    import sys as _sys

    from vnflight import handlers

    cmd, cwd = handlers._cli_invocation()

    # Repo checkout: <repo>/vnflight.py exists, so the CLI subprocess must
    # run it with the repo root as cwd (config + games resolve from there).
    assert cmd[0] == _sys.executable
    assert os.path.basename(cmd[1]) == "vnflight.py"
    assert os.path.isfile(cmd[1])
    assert cwd == os.path.dirname(cmd[1])


def test_cli_invocation_single_file_mode_reinvokes_artifact(monkeypatch, tmp_path):
    """Flat deployment: a downloaded vnflight.py must re-invoke ITSELF.

    The old path math walked two directories up from __file__ (correct
    only for src/vnflight/handlers.py) and then fell back to
    ``python -m vnflight`` — neither exists next to a standalone
    artifact, so MCP launch/stop/games/set_profile all broke.
    """
    import sys as _sys

    from vnflight import handlers

    artifact = tmp_path / "vnflight.py"
    artifact.write_text("# standalone build", encoding="utf-8")
    monkeypatch.setattr(handlers, "_single_file_artifact", lambda: artifact)

    cmd, cwd = handlers._cli_invocation()

    assert cmd == [_sys.executable, str(artifact)]
    assert cwd == str(artifact.parent)


# ---------------------------------------------------------------------------
# Transactional act — review regressions and the Aug 14-15 live fixtures
# ---------------------------------------------------------------------------


# Verbatim from harness/logs/events/freeterra_20260814_213356.jsonl line 106:
# the follow-up wait that proved act("LOG") had in fact succeeded.
_FREETERRA_LOG_SCREEN_STORY = [
    {"type": "narration", "text": "EVIDENCE LOG"},
    {"type": "narration", "text": (
        "• Specialization: signal processing — expertise in decoding "
        "and tracing transmissions")},
    {"type": "narration", "text": (
        "• Anomalous signal decoded — message addressed to Elara by "
        "name, claims temporal origin")},
]

# Verbatim from harness/logs/events/freesol_20260814_213349.jsonl line 90: the
# 500-char `game` payload the bridge emitted 51 ms before the recovery wait
# returned, and which the recovery wait did not contain.
_FREESOL_DROPPED_STORY = [
    {"type": "narration", "text": (
        "Two questions. I had a hundred, and the machine on the other end of "
        "seven years gave me two.")},
    {"type": "narration", "text": (
        "The next day, Elara is taking a lunch break in the canteen.")},
    {"type": "dialogue", "character": "Dr. Chen",
     "text": "Elara! Elara, you won't believe this!"},
    {"type": "dialogue", "character": "Dr. Voss", "text": "What is it?"},
]

# The nav screen both freesol recovery waits DID return (line 93 / line 198).
_FREESOL_NAV_PENDING = {
    "type": "choice_request",
    "id": "observatory_map",
    "choices": ["KIT", "LOG"],
}


class TestTransactionalActReview:
    """Each test is one of the review's own execution probes."""

    # -- HIGH-1: the transaction view must not clobber formatted output ----

    def test_wait_keeps_the_rendered_choice_list_beside_the_transaction(self, ctx):
        """`out.update(transaction)` replaced the rendered choice block with a bool."""
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "The corridor is dark."}],
            pending={
                "type": "choice_request", "id": "fork",
                "choices": ["Go left", "Go right"],
            },
            transaction={
                "action_nonce": "tx-1", "action_id": 12,
                "transaction_state": "settled", "pending": False,
                "ok": True, "success": True,
                "settled_pending": {"id": "fork"},
                "settled_screen": {"buttons": []},
            },
        )

        result = handle_wait(ctx, {"timeout": 5, "action_nonce": "tx-1"})

        # The formatter still owns `pending`: it is the rendered block.
        assert isinstance(result["pending"], str)
        assert "Go left" in result["pending"]
        assert "Go right" in result["pending"]
        # Transaction metadata is additive and namespaced.
        assert result["transaction"]["transaction_state"] == "settled"
        assert result["transaction_state"] == "settled"
        assert result["action_id"] == 12
        assert result["transaction_pending"] is False
        # Raw settle snapshots are diagnostics, not agent-facing payload.
        assert "settled_pending" not in result
        assert "settled_screen" not in result
        assert "settled_pending" not in result["transaction"]

    def test_transaction_does_not_overwrite_the_wait_ended_flag(self, ctx):
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=[{"type": "narration", "text": "Fin."}],
            ended=False,
            transaction={
                "action_nonce": "tx-2", "action_id": 3,
                "transaction_state": "settled", "pending": False,
                "ended": True,
            },
        )

        result = handle_wait(ctx, {"timeout": 5, "action_nonce": "tx-2"})

        assert result.get("ended") in (None, False)
        assert result["transaction"]["ended"] is True


    # -- The resolution probe's GET timeout is the remaining budget ---------

    def test_probe_survives_a_slow_bridge_within_its_budget(self):
        """A 0.5 s GET inside a 2 s probe budget must succeed.

        The read timeout used to be capped at _ACT_APPLY_PROBE_POLL_SECONDS
        (0.1 s), which made the deadline term dead code: every probe request
        was a 100 ms one, so under any real bridge latency the peek always
        timed out and settle routing silently fell back to prediction.
        """
        import time as _time

        from vnflight.handlers import _probe_act_resolution, HandlerContext

        class SlowBridgeClient(MockClient):
            def __init__(self):
                super().__init__()
                self.timeouts = []

            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.timeouts.append(timeout)
                if timeout < 0.5:
                    return None  # the GET would have expired first
                _time.sleep(0.5)
                return {
                    "action_nonce": action_nonce, "action_id": 4,
                    "transaction_state": "applied", "pending": True,
                    "resolved_as": "choice", "label": "Go left",
                }

        client = SlowBridgeClient()
        ctx = HandlerContext(client=client)
        result = {"action_nonce": "slow-bridge", "ok": True}

        probed = _probe_act_resolution(ctx, result, timeout=2.0)

        assert probed["transaction_state"] == "applied"
        assert probed["resolved_as"] == "choice"
        assert probed["label"] == "Go left"
        assert client.timeouts[0] > 1.5

    # -- Live fixture 1: freeterra lines 92-106, false timeout on success --

    def test_freeterra_false_timeout_on_a_succeeded_act(self):
        """act("LOG") reported a timeout for an action that had landed.

        Log: harness/logs/events/freeterra_20260814_213356.jsonl lines 92-106.
        The agent received
        {"ok": false, "error": "Timed out waiting for act result",
         "message": "Command 'act' submitted."}
        and a following wait showed the LOG screen open.  Under the transaction
        contract the submission cannot fail that way: it acknowledges, and the
        settle carries the screen the agent had to go fishing for.
        """
        from vnflight.handlers import HandlerContext, handle_act

        class FreeterraClient(MockClient):
            def act_transaction(self, target, *, action_nonce=None,
                                accept_timeout=15.0, deadline=None,
                                invocation=None):
                self.calls.append(("act_transaction", target))
                return {
                    "ok": True, "success": True,
                    "action_nonce": "terra-log", "action_id": 9,
                    "transaction_state": "accepted", "pending": True,
                    "submitted_target": target,
                }

            def action_transaction(self, action_nonce, *, timeout=3.0):
                self.calls.append(("action_transaction", action_nonce))
                return {
                    "action_nonce": "terra-log", "action_id": 9,
                    "transaction_state": "applied", "pending": True,
                    "resolved_as": "button", "label": "LOG",
                    "interaction_type": "info", "screen": "observatory_map",
                }

        client = FreeterraClient()
        # Pre-act: the navigation map (KIT / LOG).  Post-act: the LOG screen
        # with its single Close button — the state the follow-up wait revealed.
        map_state = {
            "status": "running",
            "game_state": {
                "interactions": [
                    {"type": "info", "source": "button", "index": 2,
                     "display_label": "LOG", "disabled": False},
                ],
            },
        }
        log_state = {
            "status": "running",
            "game_state": {
                "interactions": [
                    {"type": "nav", "source": "button", "index": 1,
                     "display_label": "Close", "disabled": False},
                ],
            },
        }
        states = [map_state, map_state]

        def stateful():
            client.calls.append(("state", {}))
            return states.pop(0) if states else log_state

        client.state = stateful
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {"target": "LOG", "wait": True})

        # No false failure: the submission cannot report a timeout any more.
        assert "Timed out waiting for act result" not in str(result.get("error", ""))
        assert result.get("error") is None
        assert result.get("transaction_state") in {"applied", "settled"}
        assert result.get("action_nonce") == "terra-log"
        assert result.get("action_id") == 9
        # The shim's resolution is carried by the transaction, not inferred.
        assert result["resolved_as"] == "button"
        assert result["label"] == "LOG"
        # No double-act: exactly one submission.
        assert len(
            [call for call in client.calls if call[0] == "act_transaction"]) == 1
        # The follow-up observation the agent had to make by hand is part of
        # the same call now, rather than a second round trip after a failure.
        assert "wait" in result

    # -- Live fixture 2: freesol lines 81-93, lost dialogue on recovery ----

    def test_freesol_recovery_wait_returns_the_story_not_just_the_nav_screen(
        self, ctx,
    ):
        """The bridge emitted 500 chars of story; the client received 0.

        Log: harness/logs/events/freesol_20260814_213349.jsonl lines 81-93.
        The MCP transport gave up at 120s (line 84); the bridge resolved act(1)
        and pushed the story at 21:43:38.288 (lines 88-90); the recovery
        wait returned a 324-char nav screen with no story text (line 93).
        The action-scoped drain is what makes that story recoverable.
        """
        from vnflight.handlers import handle_wait

        ctx.client._wait_result = MockWaitResult(
            events=list(_FREESOL_DROPPED_STORY),
            pending=dict(_FREESOL_NAV_PENDING),
            transaction={
                "action_nonce": "sol-1", "action_id": 4,
                "transaction_state": "settled", "pending": False,
                "ok": True, "success": True,
                "resolved_as": "button",
                "label": "What is coming? Tell me everything.",
            },
        )

        result = handle_wait(ctx, {"action_nonce": "sol-1", "timeout": 60})

        # The scoped drain was requested by nonce.
        assert any(
            call[0] == "wait" and call[1].get("action_nonce") == "sol-1"
            for call in ctx.client.calls
        )
        # The dropped story is present ...
        assert "Two questions." in result["text"]
        assert "you won't believe this!" in result["text"]
        # ... AND the nav screen the old recovery wait returned alone.
        assert "KIT" in result["pending"]
        assert "LOG" in result["pending"]
        assert result["transaction_state"] == "settled"

    # -- Live fixture 3: freeterra3 lines 77-100, infer-from-second-trip ----

    def test_freeterra3_fast_path_returns_a_nonce_bearing_acknowledgment(self):
        """act(wait=False) must hand back identity, not "command submitted".

        Log: harness/logs/events/freeterra3_20260815_040813.jsonl lines 77-100.
        act(2) timed out at the transport; the agent moved on and issued
        act("GENERATOR Power Systems", wait=false), whose result (line 93) was
        a bare command_result the client had to interpret.  The original act
        finally resolved at line 96, 22s after the agent had moved on.
        """
        from vnflight.handlers import HandlerContext, handle_act

        client = MockClient()
        client._state = {
            "status": "running",
            "game_state": {
                "interactions": [
                    {"type": "nav", "source": "button", "index": 1,
                     "display_label": "GENERATOR Power Systems",
                     "disabled": False},
                ],
            },
        }
        client._act_result = {"ok": True, "resolved_as": "button"}
        ctx = HandlerContext(client=client)

        result = handle_act(
            ctx, {"target": "GENERATOR Power Systems", "wait": False})

        assert result["transaction_state"] == "accepted"
        assert result["action_nonce"]
        assert result["action_id"]
        assert result["pending"] is True
        assert result.get("error") is None
        # wait=False must not spend the resolution probe.
        assert not [
            call for call in client.calls if call[0] == "action_transaction"]

    def test_act_wait_false_then_act_again_is_not_blocked(self):
        """The review's act(wait=false)-then-act probe, end to end.

        The first act is never polled for, so nothing settles it client-side;
        the second act must still be submitted.
        """
        from vnflight.handlers import HandlerContext, handle_act

        client = MockClient()
        client._state = _menu_state("Go left", "Go right")
        ctx = HandlerContext(client=client)

        first = handle_act(ctx, {"target": "1", "wait": False})
        second = handle_act(ctx, {"target": "2", "wait": False})

        assert first["action_nonce"] != second["action_nonce"]
        assert second["transaction_state"] == "accepted"
        assert len(
            [call for call in client.calls if call[0] == "act_transaction"]) == 2


# ---------------------------------------------------------------------------
# Passive overlay scrollback (Echoes of Tomorrow's live terminal)
#
# The shim reports mod-registered overlay screens' text in overlay_texts for
# passive (blocking=False) registrations too, but build_state_data() only
# reads that field when overlay_active is set -- which the shim raises for
# BLOCKING overlays only.  A passive scrollback panel therefore reached the
# agent through no channel at all, and wait() at Echoes' ECHO-7 terminal
# returned the stats line and the question list with none of the questions'
# context.  Live evidence: harness/logs/events/freeopus4_20260816_131523.jsonl.
# ---------------------------------------------------------------------------

ECHO_TERMINAL_ROWS = [
    "AETHON TERMINAL // LIVE FEED",
    "ECHO-7>",
    "ELARA. I DETECT STORM INTERFERENCE ON THE ARRAY.",
    "OUR TIME IS LIMITED. CHOOSE YOUR QUESTIONS CAREFULLY.",
    "QUESTION WINDOW - 16 MIN // CARRIER LOSS: 2%",
]


def _passive_terminal_screen():
    """A screen_content snapshot shaped like Echoes' live terminal."""
    return {
        "type": "screen_content",
        "texts": ["QUESTION WINDOW - 16 MIN // CARRIER LOSS: 2%"],
        "screens": ["crt_overlay", "echo_terminal_live"],
        "overlay_texts": list(ECHO_TERMINAL_ROWS),
        # No overlay_active: echo_terminal_live is registered blocking=False.
    }


class TestScriptedBridgeClientContract:
    """Pin the scripted transport against BridgeClient.poll()'s contract.

    The helper's whole value is that the poll it drives is the production
    poll.  These tests keep the SCRIPT honest — the transcript/``since``
    contract, and the observable state a fence test reads back.
    """

    def test_state_read_serves_only_rows_after_the_cursor(self):
        client = ScriptedBridgeClient()
        client.push_events(
            {"type": "narration", "text": "one", "_seq": 10},
            {"type": "narration", "text": "two", "_seq": 20},
        )

        assert [e["_seq"] for e in client.poll(timeout=0)] == [10, 20]
        assert client.cursor == 20
        # A cursor that has passed a row cannot see it again.
        assert client.poll(timeout=0) == []
        client.push_events({"type": "narration", "text": "three", "_seq": 30})
        assert [e["_seq"] for e in client.poll(timeout=0)] == [30]

    def test_empty_successful_read_advances_only_the_poll_serial(self):
        client = ScriptedBridgeClient()
        client.push_events({"type": "narration", "text": "one", "_seq": 10})
        client.poll(timeout=0)
        serial, cursor = client._state_poll_serial, client.cursor

        assert client.poll(timeout=0) == []

        assert client._state_poll_serial == serial + 1
        assert client.cursor == cursor

    def test_failed_read_does_not_advance_the_poll_serial(self):
        client = ScriptedBridgeClient()
        client.script_status_code = 503

        assert client.poll(timeout=0) == []
        assert client._state_poll_serial == 0

    def test_ordinary_fence_retains_suffix_and_flags_the_boundary(self):
        client = ScriptedBridgeClient()
        client.push_events(
            {"type": "dialogue", "text": "mine", "action_id": 11, "_seq": 40},
            {"type": "dialogue", "text": "theirs",
             "action_id": 22, "_seq": 41},
            {"type": "narration", "text": "after", "_seq": 42},
        )

        events = client.poll(timeout=0, ordinary_action_id=11)

        assert [e["text"] for e in events] == ["mine"]
        assert [e["text"] for e in client._prefetched_events] == [
            "theirs", "after",
        ]
        assert client._last_poll_foreign_action_boundary is True
        assert client.cursor == 42
        assert (11, 40) in client._delivered_action_events

    def test_retained_prefetch_fences_a_fresh_state_read(self):
        client = ScriptedBridgeClient()
        client.push_events({
            "type": "dialogue", "text": "theirs",
            "action_id": 22, "_seq": 41,
        })
        assert client.poll(timeout=0, ordinary_action_id=11) == []
        reads = client.state_reads()

        client.push_events({"type": "narration", "text": "later", "_seq": 50})

        assert client.poll(timeout=0, ordinary_action_id=11) == []
        # The retained row is an ordering fence, not merely a filter: the
        # later durable row must not be read past it.
        assert client.state_reads() == reads

    def test_include_prefetched_false_skips_the_stash(self):
        client = ScriptedBridgeClient()
        client.push_events({
            "type": "dialogue", "text": "theirs",
            "action_id": 22, "_seq": 41,
        })
        assert client.poll(timeout=0, ordinary_action_id=11) == []
        # One read: the retention IS the ordering fence, so the /state loop
        # returns instead of retrying past the parked foreign row.
        serial = client._state_poll_serial
        assert serial == 1

        assert client.poll(timeout=0, include_prefetched=False) == []
        assert [e["text"] for e in client._prefetched_events] == ["theirs"]
        assert client._state_poll_serial == serial + 1


class _BlockingWaitClient(MockClient):
    """A client whose wait() holds the presentation lane until released.

    ``honor_interrupt`` mirrors BridgeClient.wait's contract: a truthy
    ``on_events`` return (idle tick included) ends the wait early. Setting it
    False models the wedged case a lifecycle preemption must survive - a wait
    that never notices the interrupt at all.
    """

    def __init__(self, honor_interrupt: bool = True):
        super().__init__()
        import threading
        self.honor_interrupt = honor_interrupt
        self.wait_entered = threading.Event()
        self.release = threading.Event()
        self.interrupted = False

    def wait(self, timeout=60, **kw):
        import time as _time
        self.calls.append(("wait", {"timeout": timeout, **kw}))
        on_events = kw.get("on_events")
        self.wait_entered.set()
        deadline = _time.time() + 10.0
        while _time.time() < deadline:
            if self.release.wait(0.02):
                break
            if (
                self.honor_interrupt
                and on_events is not None
                and on_events([])
            ):
                self.interrupted = True
                break
        return self._wait_result


class TestPassiveOverlayText:
    def test_older_overlay_rows_precede_story_after_promotion(self):
        from vnflight.handlers import (
            _promote_wait_output, render_tool_result_text,
        )

        result = {
            "screen_text": "DOCUMENT HEADER",
            "_overlay_deliveries": [{
                "id": 1,
                "text": "DOCUMENT HEADER",
                "channel": "screen_text",
            }],
        }
        later = {
            "text": "DOCUMENT TAIL",
            "_overlay_deliveries": [{
                "id": 2,
                "text": "DOCUMENT TAIL",
                "channel": "story",
            }],
        }

        _promote_wait_output(result, later)

        rendered = render_tool_result_text(result)
        assert rendered.index("DOCUMENT HEADER") < rendered.index("DOCUMENT TAIL")
        assert result["_overlay_deliveries"] == [
            {"id": 1, "text": "DOCUMENT HEADER", "channel": "screen_text"},
            {"id": 2, "text": "DOCUMENT TAIL", "channel": "story"},
        ]

    def test_wait_reconciles_sampled_stats_with_live_state(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._wait_result = MockWaitResult(events=[{
            "type": "stats_update",
            "changed": {"time_remaining": 285},
            "previous": {"time_remaining": 295},
            "_ts": 100.0,
        }])
        client._game_state = {
            "_stats_ts": 101.0,
            "stats": {
                "time_remaining": 265,
                "_summary": "Time left: 4h 25m",
            },
        }
        ctx = HandlerContext(client=client)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert "time_remaining: 265" in out["status"]
        assert "time_remaining: 285" not in out["status"]
        assert "(-10)" not in out["status"]
        assert "Time left: 4h 25m" in out["_footer"]

    def test_wait_keeps_update_newer_than_cached_game_state(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._wait_result = MockWaitResult(events=[{
            "type": "stats_update",
            "changed": {"time_remaining": 285},
            "previous": {"time_remaining": 295},
            "_ts": 102.0,
        }])
        client._game_state = {
            "_stats_ts": 101.0,
            "stats": {"time_remaining": 295},
        }

        out = handle_wait(HandlerContext(client=client), {"timeout": 0.1})

        assert "time_remaining: 285 (-10)" in out["status"]
        assert "time_remaining: 295" not in out["status"]

    def test_wait_omits_summary_that_contradicts_delivered_stats(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._wait_result = MockWaitResult(events=[{
            "type": "stats_update", "changed": {
                "time_remaining": 22, "aria_integrity": 61}, "_ts": 102.0,
        }])
        client._game_state = {
            "_stats_ts": 101.0,
            "stats": {"time_remaining": 36, "aria_integrity": 62,
                      "_summary": "Time left: 36m | ARIA: 62%"},
        }
        out = handle_wait(HandlerContext(client=client), {"timeout": 0.1})
        assert "time_remaining: 22" in out["status"]
        assert "_footer" not in out

    def test_stat_reconciliation_compares_latest_update_per_stat(self):
        from vnflight.handlers import _reconcile_status_stats_with_current

        data = {"status": {"stats": [
            {"stat": "time_remaining", "value": 285, "delta": -10},
            {"stat": "aria_integrity", "value": 90, "delta": -2},
        ]}}
        _reconcile_status_stats_with_current(
            data,
            {"stats": {"time_remaining": 280, "aria_integrity": 92}},
            events=[
                {"type": "stats_update", "changed": {
                    "time_remaining": 285}, "_ts": 100.0},
                {"type": "stats_update", "changed": {
                    "aria_integrity": 90}, "_ts": 102.0},
            ],
            game_state={"_stats_ts": 101.0},
        )

        assert data["status"]["stats"] == [
            {"stat": "time_remaining", "value": 280},
            {"stat": "aria_integrity", "value": 90, "delta": -2},
        ]

    def test_transcript_rescue_is_not_replayed_by_the_next_drain(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        pending = {
            "type": "choice_request",
            "id": "next-menu",
            "choices": ["Continue"],
        }
        first = MockWaitResult(
            events=[{"type": "narration", "text": "First.", "_seq": 10}],
            pending=pending,
        )
        second = MockWaitResult(events=[
            {"type": "narration", "text": "Rescued late.", "_seq": 11},
            {"type": "narration", "text": "Actually new.", "_seq": 12},
        ])
        waits = [first, second]
        client._transcript = [
            {"type": "narration", "text": "First.", "_seq": 10},
            {"type": "narration", "text": "Rescued late.", "_seq": 11},
        ]

        def delayed_wait(timeout=60, **kw):
            return waits.pop(0)

        client.wait = delayed_wait
        ctx = HandlerContext(client=client)

        rescued = handle_wait(ctx, {"timeout": 0.1})
        drained = handle_wait(ctx, {"timeout": 0.1})

        assert rescued["text"].splitlines() == ["First.", "Rescued late."]
        assert drained["text"] == "Actually new."

    def test_transcript_rescue_skips_rows_delivered_by_a_newer_action(self):
        """Fleet r11: an old nonce must not rescue a newer act twice."""
        from vnflight.client import BridgeClient
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        pending = {
            "type": "choice_request",
            "id": "stale-successor",
            "choices": ["Continue"],
        }
        client._wait_result = MockWaitResult(
            events=[{
                "type": "narration",
                "text": "Old receipt tail.",
                "action_id": 84,
                "_seq": 250,
            }],
            pending=pending,
        )
        replayed = {
            "type": "dialogue",
            "character": "Dr. Chen",
            "text": "This is about the storm.",
            "action_id": 87,
            "_seq": 262,
        }
        genuinely_new = {
            "type": "dialogue",
            "character": "Dr. Voss",
            "text": "She sent me something.",
            "action_id": 87,
            "_seq": 263,
        }
        client._transcript = [
            client._wait_result.events[0], replayed, genuinely_new,
        ]
        ledger = BridgeClient("http://bridge", slot_prefix="/1")
        ledger._record_delivered_action_events([(87, 262)])
        client.claim_undelivered_action_events = (
            ledger.claim_undelivered_action_events)
        ctx = HandlerContext(client=client)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert "This is about the storm." not in out["text"]
        assert out["text"].splitlines() == [
            "Old receipt tail.", "[Dr. Voss] She sent me something.",
        ]
        assert (87, 263) in ledger._delivered_action_events

    def test_transcript_rescue_uses_future_transaction_generation(self):
        """A successor receipt can precede authoritative /state catch-up."""
        from vnflight.client import BridgeClient
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        pending = {
            "type": "choice_request",
            "id": "new-run-menu",
            "choices": ["Continue"],
        }
        client._wait_result = MockWaitResult(
            events=[{
                "type": "narration", "text": "New opening.",
                "action_id": 10, "_seq": 1,
            }],
            pending=pending,
            transaction={"reset_generation": 5},
        )
        late_copy = {
            "type": "dialogue", "text": "Already delivered by receipt.",
            "action_id": 10, "_seq": 2,
        }
        client._transcript = [client._wait_result.events[0], late_copy]
        ledger = BridgeClient("http://bridge", slot_prefix="/1")
        ledger._action_delivery_reset_generation = 4
        ledger._record_delivered_action_events(
            [(10, 2)], reset_generation=5,
        )
        client.claim_undelivered_action_events = (
            ledger.claim_undelivered_action_events)

        out = handle_wait(HandlerContext(client=client), {"timeout": 0.1})

        assert out["text"] == "New opening."
        assert ledger._delivered_action_event_ownership == {(5, 10, 2)}

    def test_same_slot_timeline_reconcile_preserves_known_generation(self):
        from vnflight.client import BridgeClient
        from vnflight.handlers import (
            HandlerContext, _reconcile_applied_timeline_jump,
        )

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._action_delivery_reset_generation = 5
        client._record_delivered_action_events([(10, 2)])
        client._track_action_nonce("before-load")
        ctx = HandlerContext(client=client)

        _reconcile_applied_timeline_jump(ctx)

        assert client._action_delivery_reset_generation == 5
        assert client._delivered_action_events == {(10, 2)}
        assert client._active_action_nonces == []
        assert client._next_auto_action_nonce() is None

    def test_unknown_load_cannot_auto_drain_preload_transaction(self):
        from unittest.mock import patch
        from vnflight.client import BridgeClient
        from vnflight.handlers import HandlerContext, handle_load

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._action_delivery_reset_generation = 5
        client._track_action_nonce("before-load")
        client.command = lambda *_args, **_kwargs: {
            "ok": False,
            "acceptance_unknown": True,
            "reason": "transport_timeout_after_submission",
        }
        ctx = HandlerContext(client=client)

        load_result = handle_load(ctx, {"slot": "1"})
        fresh = {"type": "narration", "text": "After load.", "_seq": 1}
        requested_paths = []

        def get(path, **_kwargs):
            requested_paths.append(path)
            if path == "/state":
                return 200, {
                    "reset_generation": 5,
                    "event_counter": 1,
                    "transcript": [fresh],
                }
            return 404, {}

        with patch.object(client, "_get", side_effect=get):
            waited = client.wait(timeout=0.1)

        assert load_result["acceptance_unknown"] is True
        assert waited.events == [fresh]
        assert "/transaction" not in requested_paths

    def test_native_lifecycle_boundary_retires_old_auto_nonces(self):
        from vnflight.client import BridgeClient
        from vnflight.handlers import (
            HandlerContext, _observe_timeline_boundaries,
        )

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("before-native-load")
        ctx = HandlerContext(client=client)

        _observe_timeline_boundaries(ctx, [{
            "type": "game_resumed",
            "_source_id": "game",
            "_source_seq": 20,
        }])

        assert client._active_action_nonces == []
        assert client._auto_action_nonce_retired == {
            "before-native-load": "timeline_reset",
        }

    def test_lifecycle_boundary_preserves_its_scoped_action_continuation(self):
        from unittest.mock import patch
        from vnflight.client import BridgeClient
        from vnflight.handlers import (
            HandlerContext, _observe_timeline_boundaries,
        )

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._action_delivery_reset_generation = 5
        client._track_action_nonce("old-action")
        client._track_action_nonce("start-action")
        ctx = HandlerContext(client=client)

        _observe_timeline_boundaries(
            ctx,
            [{
                "type": "game_started",
                "action_id": 10,
                "_source_id": "game",
                "_source_seq": 1,
                "_seq": 1,
            }],
            preserve_action_nonce="start-action",
            preserve_action_id=10,
        )

        trailing = {
            "type": "narration",
            "text": "The new opening continues.",
            "action_id": 10,
            "_seq": 2,
        }
        transaction = {
            "action_nonce": "start-action",
            "action_id": 10,
            "reset_generation": 5,
            "transaction_state": "settled",
            "pending": False,
            "events": [trailing],
        }
        with patch.object(
            client, "_get", return_value=(200, {"transaction": transaction}),
        ):
            continued = client.wait(
                timeout=1, action_nonce="start-action",
            )

        assert client._active_action_nonces == []
        assert client._auto_action_nonce_retired["old-action"] == (
            "timeline_reset"
        )
        assert continued.events == [trailing]

    def test_bridge_owned_start_boundary_survives_handler_composition(self):
        import time
        from unittest.mock import patch
        from vnflight.bridge import GameState
        from vnflight.client import BridgeClient
        from vnflight.handlers import HandlerContext, handle_wait

        state = GameState()
        state.set_pending_request({
            "type": "choice_request", "id": "menu", "choices": ["Start"],
        })
        command = {
            "name": "act", "args": {"index": 1}, "nonce": "start-action",
            "reset_generation": 0,
        }
        ok, message, _ack = state.submit_command_with_ack(command)
        assert ok is True, message
        assert state.consume_command() == command
        state.push_event({
            "type": "command_result", "command": "act",
            "nonce": "start-action", "success": True,
        })
        state.push_event({"type": "game_started"})
        record = state._act_transactions["start-action"]
        record["gate_released"] = True
        record["gate_released_by"] = "test_boundary"
        record["gate_released_at"] = time.time()
        assert state.get_action_transaction(
            "start-action")["admission_open"] is True

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("start-action")

        def get(path, params=None, **_kwargs):
            params = params or {}
            if path == "/transaction":
                if "ack" in params:
                    status = state.acknowledge_action_events(
                        "start-action", int(params["ack"]),
                    )
                    return (200, {"ok": True}) if status == "ok" else (500, {})
                return 200, {"transaction": state.get_action_transaction(
                    "start-action",
                )}
            if path == "/state":
                return 200, state.get_state()
            if path == "/game-state":
                return 200, {"game_state": None}
            if path == "/screen":
                return 200, {"screen": None}
            if path == "/pending":
                return 200, {"pending": state.pending_request}
            if path == "/transcript":
                return 200, {"transcript": list(state.transcript)}
            return 404, {}

        ctx = HandlerContext(client=client)
        with patch.object(client, "_get", side_effect=get):
            handle_wait(ctx, {
                "action_nonce": "start-action",
                "_return_on_admission": True,
                "timeout": 1,
            })
            assert client._active_action_nonces == ["start-action"]
            assert "start-action" not in client._auto_action_nonce_retired

            state.push_event({
                "type": "narration", "text": "The opening continues.",
            })
            continued = client.wait(
                timeout=0.1, action_nonce="start-action",
            )

        assert [event.get("text") for event in continued.events] == [
            "The opening continues.",
        ]

    def test_wait_boundary_preserves_owner_and_newer_active_actions(
        self, monkeypatch,
    ):
        """An older Start receipt cannot tombstone a newer accepted act."""
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("older-action", 9)
        client._track_action_nonce("start-action", 10)
        client._track_action_nonce("newer-action", 11)
        pending = {
            "id": "new-run-menu",
            "type": "choice_request",
            "choices": ["Continue"],
        }
        result = MockWaitResult(
            events=[{
                "type": "game_started",
                "action_id": 10,
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }, {
                "type": "choice_request",
                "id": "new-run-menu",
                "choices": ["Continue"],
                "action_id": 10,
                "_seq": 21,
            }],
            pending=pending,
            transaction={
                "action_nonce": "start-action",
                "action_id": 10,
                "reset_generation": 5,
                "transaction_state": "settled",
            },
        )
        monkeypatch.setattr(client, "wait", lambda **_kwargs: result)
        monkeypatch.setattr(client, "game_state", lambda **_kwargs: None)
        monkeypatch.setattr(client, "transcript", lambda **_kwargs: [])
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing", "pending_request": pending,
        })
        monkeypatch.setattr(client, "pending", lambda **_kwargs: pending)
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_a, **_k: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_a, **_k: None)

        handlers.handle_wait(
            handlers.HandlerContext(client=client),
            {"timeout": 0.1, "action_nonce": "start-action"},
        )

        assert client._active_action_nonces == [
            "start-action", "newer-action",
        ]
        assert client._auto_action_nonce_retired == {
            "older-action": "timeline_reset",
        }

    def test_wait_uses_latest_fresh_boundary_for_action_preservation(
        self, monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("first-start", 10)
        client._track_action_nonce("current-load", 20)
        result = MockWaitResult(
            events=[{
                "type": "game_started", "action_id": 10,
                "_source_id": "game", "_source_seq": 10, "_seq": 10,
            }, {
                "type": "game_resumed", "action_id": 20,
                "_source_id": "game", "_source_seq": 20, "_seq": 20,
            }],
            transaction={
                "action_nonce": "first-start", "action_id": 10,
                "reset_generation": 5, "transaction_state": "settled",
            },
        )
        monkeypatch.setattr(client, "wait", lambda **_kwargs: result)
        monkeypatch.setattr(client, "game_state", lambda **_kwargs: None)
        monkeypatch.setattr(client, "transcript", lambda **_kwargs: [])
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing",
        })
        monkeypatch.setattr(client, "pending", lambda **_kwargs: None)
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_a, **_k: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_a, **_k: None)

        handlers.handle_wait(
            handlers.HandlerContext(client=client),
            {"timeout": 0.1, "action_nonce": "first-start"},
        )

        assert client._active_action_nonces == ["current-load"]
        assert client._auto_action_nonce_retired == {
            "first-start": "timeline_reset",
        }

    def test_unowned_lifecycle_does_not_preserve_scoped_old_nonce(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._active_action_nonces = ["old-action"]
        client._wait_result = MockWaitResult(
            events=[{
                "type": "game_resumed",
                "action_id": 99,
                "_source_id": "native-load",
                "_source_seq": 1,
                "_seq": 1,
            }],
            transaction={
                "action_nonce": "old-action",
                "action_id": 10,
                "reset_generation": 5,
            },
        )
        ctx = HandlerContext(client=client)

        handle_wait(ctx, {"timeout": 0.1})

        assert client._active_action_nonces == []

    def test_transcript_rescue_receipts_end_at_new_game_boundary(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._wait_result = MockWaitResult(events=[
            {"type": "game_started", "_seq": 1},
            {"type": "narration", "text": "A genuinely new line.", "_seq": 11},
        ])
        ctx = HandlerContext(client=client)
        ctx.overlay.transcript_rescued_seqs.add(11)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert out["text"] == "A genuinely new line."
        assert not ctx.overlay.transcript_rescued_seqs

    def test_drained_durable_overlay_rows_publish_without_ledger_bookkeeping(
        self,
    ):
        """Receipt tuples stay in the ctx ledger, never in delivered records."""
        from vnflight.handlers import HandlerContext, handle_wait
        from vnflight.overlay_presentation import (
            _OVERLAY_LEDGER_RECORD_KEYS,
        )

        client = MockClient()
        client._wait_result = MockWaitResult(events=[
            {"type": "narration", "text": "Before the terminal."},
            {
                "type": "screen_content",
                "_seq": 2,
                "_source_id": "game-a",
                "_source_seq": 2,
                "passive_overlay_snapshot": True,
                "passive_overlay_delta": ["ECHO-7> IT IS FROM WHEN."],
                "screens": ["echo_terminal_live"],
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {"echo_terminal_live": "7"},
                "overlay_texts": ["ECHO-7> IT IS FROM WHEN."],
                "texts": ["ECHO-7> IT IS FROM WHEN."],
            },
            {"type": "narration", "text": "After the terminal."},
        ], pending={
            "type": "choice_request",
            "id": "after-terminal",
            "choices": ["Continue"],
        }, screen={
            "screens": ["echo_terminal_live"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "7"},
            "overlay_texts": ["ECHO-7> IT IS FROM WHEN."],
        })
        ctx = HandlerContext(client=client)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert out["text"].splitlines() == [
            "Before the terminal.",
            "ECHO-7> IT IS FROM WHEN.",
            "After the terminal.",
        ]
        # The durable path really ran: the row's receipt is in the ledger...
        assert ("game-a", "2", 0) in ctx.overlay.durable_receipts
        # ...and the public delivery record carries none of the bookkeeping.
        story_records = [
            record for record in out["_overlay_deliveries"]
            if record.get("channel") == "story"
        ]
        assert [record["text"] for record in story_records] == [
            "ECHO-7> IT IS FROM WHEN.",
        ]
        for record in out["_overlay_deliveries"]:
            assert not (set(record) & set(_OVERLAY_LEDGER_RECORD_KEYS)), record

    def test_drained_overlay_rows_keep_their_story_chronology(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._wait_result = MockWaitResult(events=[
            {"type": "narration", "text": "Before the terminal."},
            {
                "type": "screen_content",
                "_seq": 2,
                "passive_overlay_snapshot": True,
                "screens": ["echo_terminal_live"],
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {"echo_terminal_live": "7"},
                "overlay_texts": ["ECHO-7> IT IS FROM WHEN."],
                "texts": ["ECHO-7> IT IS FROM WHEN."],
            },
            {"type": "narration", "text": "After the terminal."},
        ], pending={
            "type": "choice_request",
            "id": "after-terminal",
            "choices": ["Continue"],
        }, screen={
            "screens": ["echo_terminal_live"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "7"},
            "overlay_texts": [
                "ECHO-7> IT IS FROM WHEN.",
                "ECHO-7> ONE MORE THING.",
            ],
        })
        ctx = HandlerContext(client=client)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert out["text"].splitlines() == [
            "Before the terminal.",
            "ECHO-7> IT IS FROM WHEN.",
            "After the terminal.",
        ]
        assert out["screen_text"] == "ECHO-7> ONE MORE THING."
        assert out["_overlay_deliveries"] == [
            {
                "id": 1,
                "text": "ECHO-7> IT IS FROM WHEN.",
                "_bridge_seq": 2,
                "channel": "story",
            },
            {
                "id": 2,
                "text": "ECHO-7> ONE MORE THING.",
                "channel": "screen_text",
            },
        ]

    def test_wait_delivers_passive_overlay_scrollback(self):
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        screen = _passive_terminal_screen()
        client._wait_result = MockWaitResult(
            events=[{
                "type": "stats_update",
                "stats": [{"stat": "evidence_count", "value": 4}],
            }],
            screen=screen,
        )
        client._state = {"screen": screen}
        client._game_state = {
            "interactions": [
                {
                    "id": "1",
                    "display_label": "What is coming? Tell me everything.",
                    "type": "choice",
                    "source": "button",
                    "screen": "echo_terminal_choice",
                    "index": 1,
                    "action_names": ["Return"],
                },
            ],
        }
        ctx = HandlerContext(client=client)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert "ELARA. I DETECT STORM INTERFERENCE ON THE ARRAY." in (
            out["screen_text"])
        # The cost banner is written immediately before the choice screen
        # opens; it must arrive WITH the question list, not an interaction
        # later ("terminal Q&A one-late render").
        assert "CARRIER LOSS: 2%" in out["screen_text"]

    def test_scrollback_is_not_redelivered(self):
        """An append-only log must cost each row occurrence once."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        screen = _passive_terminal_screen()

        first: dict = {}
        _merge_passive_overlay_text(ctx, first, screen)
        assert first["screen_text"].splitlines() == ECHO_TERMINAL_ROWS

        second: dict = {}
        _merge_passive_overlay_text(ctx, second, screen)
        assert "screen_text" not in second

        grown = dict(screen)
        grown["overlay_texts"] = ECHO_TERMINAL_ROWS + ["I AM NOT JUST A PROBE."]
        third: dict = {}
        _merge_passive_overlay_text(ctx, third, grown)
        assert third["screen_text"] == "I AM NOT JUST A PROBE."

    def test_repeated_rows_are_delivered_by_position(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(
            ctx, first, {"overlay_texts": ["ECHO-7>"]})
        first_id = first["_overlay_deliveries"][0]["id"]

        second: dict = {}
        _merge_passive_overlay_text(
            ctx, second, {"overlay_texts": ["ECHO-7>", "ECHO-7>"]})

        assert second["screen_text"] == "ECHO-7>"
        assert second["_overlay_deliveries"][0]["id"] != first_id

    def test_divergent_snapshot_starts_a_new_panel_generation(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        old: dict = {}
        _merge_passive_overlay_text(
            ctx, old,
            {"screens": ["echo_terminal_live"],
             "overlay_texts": ["OLD HEADER", "SHARED ROW"]},
        )

        reset: dict = {}
        _merge_passive_overlay_text(
            ctx, reset,
            {"screens": ["echo_terminal_live"],
             "overlay_texts": ["NEW HEADER", "SHARED ROW"]},
        )

        assert reset["screen_text"].splitlines() == [
            "NEW HEADER", "SHARED ROW",
        ]

    def test_midwrite_divergence_delivers_from_divergent_row_only(self):
        """A scrape that caught a row mid-type must not re-print the panel.

        Terra's Echoes run saw whole audit/terminal blocks duplicated: the
        ledger held [.., "Loa"] (typewriter partial), the next scrape read
        [.., "Loading complete.", ..], no scroll overlap matched, and
        delivery restarted from row 0.
        """
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, {
            "screens": ["echo_terminal_live"],
            "overlay_texts": ["AUDIT LOG:", "REC 001 SEALED", "Loa"],
        })
        assert first["screen_text"].splitlines() == [
            "AUDIT LOG:", "REC 001 SEALED", "Loa",
        ]

        second: dict = {}
        _merge_passive_overlay_text(ctx, second, {
            "screens": ["echo_terminal_live"],
            "overlay_texts": [
                "AUDIT LOG:", "REC 001 SEALED",
                "Loading complete.", "REC 002 SEALED",
            ],
        })
        assert second["screen_text"].splitlines() == [
            "Loading complete.", "REC 002 SEALED",
        ]

    def test_scroll_overlap_still_wins_over_common_prefix(self):
        """Scrolling delivers the appended tail, never re-prints the body."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, {
            "screens": ["echo_terminal_live"],
            "overlay_texts": ["ROW A", "ROW B", "ROW C"],
        })

        scrolled: dict = {}
        _merge_passive_overlay_text(ctx, scrolled, {
            "screens": ["echo_terminal_live"],
            "overlay_texts": ["ROW B", "ROW C", "ROW D"],
        })
        assert scrolled["screen_text"] == "ROW D"

    def test_pinned_header_does_not_break_rolling_body_overlap(self):
        """A fixed terminal title must not make a rolling body redeliver."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, {
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "13"},
            "overlay_texts": ["LINK HELD", "ELARA.", "FOURTH OPTION", "COPY"],
        })

        rolled: dict = {}
        _merge_passive_overlay_text(ctx, rolled, {
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "13"},
            "overlay_texts": ["LINK HELD", "FOURTH OPTION", "COPY", "I AM YOU"],
        })

        assert rolled["screen_text"] == "I AM YOU"

    def test_interpolated_row_refresh_does_not_replay_stable_tail(self):
        """A live status refresh is not a second copy of the report below it.

        Fleet r28 reproduced this with Echoes' audit panel: spending another
        ARIA point changed an earlier ``Core integrity now 73%`` row to 72%,
        after which the old delta treated FINDINGS and every report row below
        it as fresh output before the new archive rows.
        """
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, {
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "15"},
            "overlay_retained_screens": ["echo_terminal_live"],
            "overlay_texts_by_screen": {
                "echo_terminal_live": [
                    "ARIA SOURCE CODE AUDIT - REPORT",
                    "Commissioned locally.",
                    "Core integrity now 73%.",
                    "FINDINGS:",
                    "1. VERIFY_TRUST",
                    "2. KEY_EXCHANGE",
                    "Annotated by ARIA.",
                ],
            },
        })

        refreshed: dict = {}
        _merge_passive_overlay_text(ctx, refreshed, {
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "15"},
            "overlay_retained_screens": ["echo_terminal_live"],
            "overlay_texts_by_screen": {
                "echo_terminal_live": [
                    "ARIA SOURCE CODE AUDIT - REPORT",
                    "Commissioned locally.",
                    "Core integrity now 72%.",
                    "FINDINGS:",
                    "1. VERIFY_TRUST",
                    "2. KEY_EXCHANGE",
                    "Annotated by ARIA.",
                    "ARCHIVE TARGET: EVIDENCE LOG - ARMED",
                    "DRIVE LOADED",
                ],
            },
        })

        assert refreshed["screen_text"].splitlines() == [
            "ARCHIVE TARGET: EVIDENCE LOG - ARMED",
            "DRIVE LOADED",
        ]
        assert ctx.overlay.contributor_snapshots["echo_terminal_live"][2] == (
            "Core integrity now 72%.")

    def test_unequal_refresh_remains_visible_when_alignment_is_ambiguous(self):
        """Do not guess which row in an unequal replacement is new content."""
        from vnflight.handlers import _passive_overlay_delta

        previous = ["HEADER", "OLD STATUS", "FINDING 1", "FINDING 2"]
        panel = [
            "HEADER", "NEW NOTICE", "NEW STATUS",
            "FINDING 1", "FINDING 2", "APPENDED",
        ]

        assert _passive_overlay_delta(previous, panel) == panel[1:]

    def test_unrelated_screen_churn_does_not_redeliver_scrollback(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, {
            "screens": ["echo_terminal_live"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts": ["RETAINED ROW"],
        })

        churned: dict = {}
        _merge_passive_overlay_text(ctx, churned, {
            "screens": ["echo_terminal_live", "echo_terminal_choice", "notify"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts": ["RETAINED ROW"],
        })

        assert "screen_text" not in churned

    def test_hidden_then_reopened_identical_overlay_is_delivered(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        client = MockClient()
        client._get = lambda *args, **kwargs: (
            200, {"screen": {"screens": ["say"], "overlay_texts": []}}
        )
        ctx = HandlerContext(client=client)
        shown = {
            "screens": ["echo_terminal_live"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts": ["SAME ROW"],
        }
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, shown)

        _merge_passive_overlay_text(
            ctx, {}, {"screens": ["say"], "overlay_texts": []})
        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, shown)

        assert reopened["screen_text"] == "SAME ROW"

    def test_retained_overlay_handoff_does_not_replay_identical_rows(self):
        """A choice-layer handoff is not a terminal close."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        client = MockClient()
        client._get = lambda *args, **kwargs: (
            200, {"screen": {"screens": ["echo_terminal_choice"]}}
        )
        ctx = HandlerContext(client=client)
        shown = {
            "screens": ["echo_terminal_live"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_retained_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "7"},
            "overlay_texts": ["BEFORE THE STORM DEEPENS"],
        }
        _merge_passive_overlay_text(ctx, {}, shown)
        _merge_passive_overlay_text(
            ctx, {}, {"screens": ["echo_terminal_choice"]})

        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, shown)

        assert "screen_text" not in reopened
        assert ctx.overlay.text_snapshot == ["BEFORE THE STORM DEEPENS"]

    def test_retained_overlay_new_generation_redelivers_identical_rows(self):
        """An explicit terminal clear starts a new page with repeated text."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        base = {
            "screens": ["echo_terminal_live"],
            "overlay_screens": ["echo_terminal_live"],
            "overlay_retained_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "7"},
            "overlay_texts": ["SAME ROW"],
        }
        _merge_passive_overlay_text(ctx, {}, base)

        next_page = dict(base)
        next_page["overlay_generations"] = {"echo_terminal_live": "8"}
        out: dict = {}
        _merge_passive_overlay_text(ctx, out, next_page)

        assert out["screen_text"] == "SAME ROW"

    def test_blocking_overlay_text_is_left_alone_but_booked(self):
        """A blocking overlay already renders via _screen_texts."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        screen = dict(_passive_terminal_screen())
        screen["overlay_active"] = True

        out = {"screen_text": "EVIDENCE LOG"}
        _merge_passive_overlay_text(ctx, out, screen)
        assert out["screen_text"] == "EVIDENCE LOG"

    def test_equal_story_and_fresh_overlay_rows_remain_distinct(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        out = {"text": "ELARA. I DETECT STORM INTERFERENCE ON THE ARRAY."}
        _merge_passive_overlay_text(ctx, out, _passive_terminal_screen())

        assert out["text"].count("I DETECT STORM INTERFERENCE") == 1
        assert out["screen_text"].count("I DETECT STORM INTERFERENCE") == 1
        assert "CARRIER LOSS: 2%" in out["screen_text"]

    def test_passive_merge_extends_existing_story_render_plan(self):
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            _promote_wait_output,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        out: dict = {}
        _merge_passive_overlay_text(ctx, out, {"overlay_texts": ["OLD"]})
        _promote_wait_output(out, {"text": "NARRATION"})
        _merge_passive_overlay_text(
            ctx, out, {"overlay_texts": ["OLD", "FRESH"]})

        assert out["screen_text"] == "OLD\nFRESH"
        assert render_tool_result_text(out) == "OLD\n\nNARRATION\n\nFRESH"

    def test_generation_change_redelivers_only_changed_contributor(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        first_screen = {
            "overlay_texts": ["A stable", "B stable"],
            "overlay_texts_by_screen": {
                "a": ["A stable"], "b": ["B stable"]},
            "overlay_screens": ["a", "b"],
            "overlay_generations": {"a": "1", "b": "1"},
            "overlay_retained_screens": ["a", "b"],
        }
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, first_screen)
        assert first["screen_text"] == "A stable\nB stable"

        second_screen = dict(first_screen)
        second_screen["overlay_generations"] = {"a": "1", "b": "2"}
        second: dict = {}
        _merge_passive_overlay_text(ctx, second, second_screen)

        assert second["screen_text"] == "B stable"
        assert [record["text"] for record in second["_overlay_deliveries"]] == [
            "B stable"]

    def test_drained_close_and_reopen_redelivers_inside_one_wait(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())
        shown = {
            "type": "screen_content",
            "passive_overlay_snapshot": True,
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "overlay_screens": ["a"],
        }
        empty = {
            "type": "screen_content",
            "passive_overlay_snapshot": True,
            "overlay_texts": [],
            "overlay_texts_by_screen": {"a": []},
            "overlay_screens": ["a"],
        }
        result = SimpleNamespace(events=[
            {"type": "narration", "text": "before"},
            shown, empty, shown,
        ])

        deliveries = _book_drained_overlay_events(ctx, result)

        assert [record["text"] for record in deliveries] == ["SAME", "SAME"]

    def test_bridge_delta_prevents_fresh_context_replaying_scrollback(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())
        cumulative = {
            "type": "screen_content",
            "_seq": 12,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SELECTED: SIGNAL ANALYSIS"],
            "overlay_texts": [
                "ALERT: ANOMALOUS SIGNAL DETECTED",
                "BAND: 1420.405 MHz",
                "SELECTED: SIGNAL ANALYSIS",
            ],
            "overlay_texts_by_screen": {
                "echo_terminal_live": [
                    "ALERT: ANOMALOUS SIGNAL DETECTED",
                    "BAND: 1420.405 MHz",
                    "SELECTED: SIGNAL ANALYSIS",
                ],
            },
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "2"},
            "overlay_retained_screens": ["echo_terminal_live"],
        }
        result = SimpleNamespace(events=[
            {"type": "narration", "text": "The toolkit locks in."},
            cumulative,
        ])

        deliveries = _book_drained_overlay_events(ctx, result)

        assert [record["text"] for record in deliveries] == [
            "SELECTED: SIGNAL ANALYSIS",
        ]
        assert result.events[-1]["texts"] == ["SELECTED: SIGNAL ANALYSIS"]
        assert ctx.overlay.contributor_snapshots["echo_terminal_live"] == [
            "ALERT: ANOMALOUS SIGNAL DETECTED",
            "BAND: 1420.405 MHz",
            "SELECTED: SIGNAL ANALYSIS",
        ]

    def test_cumulative_bridge_snapshot_recovers_a_skipped_durable_row(self):
        """Fleet r38: a command-confirmation cursor skipped one terminal row."""
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())

        def snapshot(seq, rows, row_seqs, delta):
            return {
                "type": "screen_content",
                "_seq": seq,
                "_source_id": "shim-a",
                "_source_seq": seq,
                "passive_overlay_snapshot": True,
                "passive_overlay_delta": delta,
                "overlay_texts": rows,
                "overlay_texts_by_screen": {"echo_terminal_live": rows},
                "passive_overlay_row_seqs_by_screen": {
                    "echo_terminal_live": row_seqs},
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {"echo_terminal_live": "9"},
                "overlay_retained_screens": ["echo_terminal_live"],
            }

        initial = SimpleNamespace(events=[
            {"type": "narration", "text": "before", "_seq": 99},
            snapshot(100, ["..."], [100], ["..."]),
        ])
        current = SimpleNamespace(events=[
            {"type": "narration", "text": "after", "_seq": 103},
            snapshot(
                102,
                ["...", "PERHAPS THAT IS THE BRAVER CHOICE.", "I BUILT AURORA."],
                [100, 101, 102],
                ["I BUILT AURORA."],
            ),
        ])

        _book_drained_overlay_events(ctx, initial)
        deliveries = _book_drained_overlay_events(ctx, current)

        assert [record["text"] for record in deliveries] == [
            "PERHAPS THAT IS THE BRAVER CHOICE.",
            "I BUILT AURORA.",
        ]
        assert [record["_bridge_seq"] for record in deliveries] == [101, 102]
        assert [event.get("_seq") for event in current.events] == [101, 102, 103]
        assert [
            event.get("texts", [event.get("text")])[0]
            for event in current.events
        ] == [
            "PERHAPS THAT IS THE BRAVER CHOICE.",
            "I BUILT AURORA.",
            "after",
        ]

        delayed_original = SimpleNamespace(events=[snapshot(
            101,
            ["...", "PERHAPS THAT IS THE BRAVER CHOICE."],
            [100, 101],
            ["PERHAPS THAT IS THE BRAVER CHOICE."],
        )])
        assert _book_drained_overlay_events(ctx, delayed_original) == []

    def test_skipped_row_recovery_survives_an_unrelated_contributor_change(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())
        initial = {
            "type": "screen_content",
            "_seq": 100,
            "_source_id": "shim-a",
            "_source_seq": 100,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["BASE"],
            "overlay_texts": ["BASE"],
            "overlay_texts_by_screen": {"terminal": ["BASE"]},
            "passive_overlay_row_seqs_by_screen": {"terminal": [100]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
        }
        changed = {
            **initial,
            "_seq": 102,
            "_source_seq": 102,
            "passive_overlay_delta": ["NOW"],
            "overlay_texts": ["BASE", "MISSED", "NOW", "HUD"],
            "overlay_texts_by_screen": {
                "terminal": ["BASE", "MISSED", "NOW"],
                "hud": ["HUD"],
            },
            "passive_overlay_row_seqs_by_screen": {
                "terminal": [100, 101, 102], "hud": [102],
            },
            "overlay_screens": ["terminal", "hud"],
            "overlay_generations": {"terminal": "1", "hud": "7"},
        }
        _book_drained_overlay_events(
            ctx, SimpleNamespace(events=[dict(initial)]))
        current = SimpleNamespace(events=[
            {"type": "narration", "text": "after", "_seq": 103},
            changed,
        ])

        deliveries = _book_drained_overlay_events(ctx, current)

        assert [record["text"] for record in deliveries] == ["MISSED", "NOW"]
        assert [record["_bridge_seq"] for record in deliveries] == [101, 102]

    def test_replayed_bridge_delta_is_booked_once_across_settle_reads(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())
        event = {
            "type": "screen_content",
            "_seq": 42,
            "_source_id": "shim-a",
            "_source_seq": 17,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SAME", "SAME"],
            "overlay_texts": ["SAME", "SAME"],
            "overlay_texts_by_screen": {"terminal": ["SAME", "SAME"]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "3"},
        }
        first = SimpleNamespace(events=[
            {"type": "narration", "text": "before"}, dict(event),
        ])
        replay = SimpleNamespace(events=[
            {"type": "narration", "text": "after"}, dict(event),
        ])

        first_deliveries = _book_drained_overlay_events(ctx, first)
        replay_deliveries = _book_drained_overlay_events(ctx, replay)

        assert [record["text"] for record in first_deliveries] == [
            "SAME", "SAME",
        ]
        assert replay_deliveries == []
        assert [event["type"] for event in replay.events] == ["narration"]

    def test_stale_lifecycle_batch_does_not_reset_overlay_receipts(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())
        row = {
            "type": "screen_content",
            "_seq": 72,
            "_source_id": "shim-a",
            "_source_seq": 72,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["ARIA ONLINE"],
            "overlay_texts": ["BOOT", "ARIA ONLINE"],
            "overlay_texts_by_screen": {
                "terminal": ["BOOT", "ARIA ONLINE"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
        }
        first = SimpleNamespace(events=[
            {"type": "narration", "text": "before", "_seq": 71},
            dict(row),
        ])
        delayed_ordinary_batch = SimpleNamespace(events=[
            {"type": "game_started", "_seq": 2},
            dict(row),
            {
                **row,
                "_seq": 84,
                "_source_seq": 84,
                "passive_overlay_delta": ["PRIMARY ONLINE"],
                "overlay_texts": ["BOOT", "ARIA ONLINE", "PRIMARY ONLINE"],
                "overlay_texts_by_screen": {
                    "terminal": ["BOOT", "ARIA ONLINE", "PRIMARY ONLINE"],
                },
            },
        ])

        first_deliveries = _book_drained_overlay_events(ctx, first)
        later_deliveries = _book_drained_overlay_events(
            ctx, delayed_ordinary_batch)

        assert [record["text"] for record in first_deliveries] == [
            "ARIA ONLINE",
        ]
        assert later_deliveries == []
        assert [
            record["text"] for record in ctx.overlay.pending_deliveries
        ] == [
            "PRIMARY ONLINE",
        ]

    def test_latest_screen_and_drain_share_durable_overlay_identity(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events,
            _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        snapshot = {
            "type": "screen_content",
            "_seq": 84,
            "_source_id": "shim-a",
            "_source_seq": 84,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["PRIMARY ONLINE"],
            "overlay_texts": ["BOOT", "PRIMARY ONLINE"],
            "overlay_texts_by_screen": {
                "terminal": ["BOOT", "PRIMARY ONLINE"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
        }
        current_output: dict = {}
        _merge_passive_overlay_text(ctx, current_output, dict(snapshot))
        ctx.client._state_poll_serial = 1
        _merge_passive_overlay_text(
            ctx, current_output, screen=None, sample_live=False)
        drained = SimpleNamespace(events=[
            {"type": "narration", "text": "after", "_seq": 85},
            dict(snapshot),
        ])

        replay = _book_drained_overlay_events(ctx, drained)

        assert current_output["screen_text"] == "PRIMARY ONLINE"
        assert current_output["_overlay_deliveries"][0]["id"] == -840001
        assert replay == []

    def test_late_observed_overlay_delta_uses_original_bridge_chronology(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text,
            render_tool_result_text)

        ctx = HandlerContext(client=MockClient())
        out = {
            "text": "Before the terminal.\nAfter the terminal.",
            "_story_render_sections": [
                {
                    "channel": "text",
                    "text": "Before the terminal.",
                    "occurrence_ids": ["before"],
                    "_bridge_seq": 400,
                },
                {
                    "channel": "text",
                    "text": "After the terminal.",
                    "occurrence_ids": ["after"],
                    "_bridge_seq": 500,
                },
            ],
        }
        late_snapshot = {
            "type": "screen_content",
            "_seq": 450,
            "_source_id": "shim-a",
            "_source_seq": 450,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["IT IS FROM WHEN."],
            "overlay_texts": ["IT IS FROM WHEN."],
            "overlay_texts_by_screen": {
                "echo_terminal_live": ["IT IS FROM WHEN."],
            },
            "overlay_screens": ["echo_terminal_live"],
            "overlay_generations": {"echo_terminal_live": "3"},
            "overlay_retained_screens": ["echo_terminal_live"],
        }

        _merge_passive_overlay_text(ctx, out, late_snapshot)
        ctx.client._state_poll_serial = 1
        _merge_passive_overlay_text(
            ctx, out, screen=None, sample_live=False)

        assert render_tool_result_text(out).splitlines() == [
            "Before the terminal.",
            "",
            "IT IS FROM WHEN.",
            "",
            "After the terminal.",
        ]

    def test_buffered_durable_rows_render_by_bridge_sequence(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text,
            render_tool_result_text)

        ctx = HandlerContext(client=MockClient())
        # Scoped and ordinary reads can complete in this order even though
        # the durable events occurred in the opposite order.
        ctx.overlay.pending_deliveries = [
            {"id": -300001, "text": "LATER", "_bridge_seq": 30},
            {"id": -100001, "text": "EARLIER", "_bridge_seq": 10},
        ]
        out: dict = {}

        _merge_passive_overlay_text(ctx, out, screen={})

        assert render_tool_result_text(out) == "EARLIER\nLATER"
        assert [
            record["text"] for record in out["_overlay_deliveries"]
        ] == ["EARLIER", "LATER"]

    def test_durable_delta_confirms_latest_screen_lookahead_without_replay(self):
        from types import SimpleNamespace
        from vnflight.format import build_wait_data, format_wait_text
        from vnflight.handlers import (
            HandlerContext,
            _book_drained_overlay_events,
            _merge_passive_overlay_text,
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        latest_screen = {
            "overlay_texts": ["CALIBRATING", "PERSONNEL: 2"],
            "overlay_texts_by_screen": {
                "terminal": ["CALIBRATING", "PERSONNEL: 2"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "overlay_retained_screens": ["terminal"],
        }
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, latest_screen)

        durable = {
            **latest_screen,
            "type": "screen_content",
            "_seq": 20,
            "_source_id": "shim-a",
            "_source_seq": 20,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["CALIBRATING", "PERSONNEL: 2"],
        }
        drained = SimpleNamespace(events=[
            durable,
            {"type": "narration", "text": "The cursor blinks.", "_seq": 21},
        ])

        assert _book_drained_overlay_events(ctx, drained) == []
        followup = format_wait_text(build_wait_data(drained.events))
        _promote_wait_output_preserving_story(first, followup)

        rendered = render_tool_result_text(first)
        assert rendered.count("CALIBRATING") == 1
        assert rendered.count("PERSONNEL: 2") == 1
        assert rendered.endswith("The cursor blinks.")

    def test_latest_screen_rows_keep_bridge_position_across_promotion(self):
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, {
            "_seq": 20,
            "overlay_texts": ["SYSTEM BOOT... OK"],
            "overlay_texts_by_screen": {
                "terminal": ["SYSTEM BOOT... OK"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
        })
        ctx.client.cursor = 20
        _merge_passive_overlay_text(
            ctx, first, screen=None, sample_live=False)
        followup = {
            "text": "The cursor blinks.",
            "_story_render_sections": [{
                "channel": "text",
                "text": "The cursor blinks.",
                "occurrence_ids": ["cursor"],
                "_bridge_seq": 30,
            }],
        }

        _promote_wait_output_preserving_story(first, followup)

        assert first["_overlay_deliveries"][0]["_bridge_seq"] == 20
        assert render_tool_result_text(first).splitlines() == [
            "SYSTEM BOOT... OK",
            "",
            "The cursor blinks.",
        ]

    def test_latest_screen_row_waits_for_ordinary_cursor(self):
        """Fleet r8: live overlay look-ahead must not jump a story gap.

        Sequence test: the cursor is moved by real ``BridgeClient.poll()``
        reads against a scripted bridge, never by assignment, so the fence
        is decided against a cursor the production poll actually produced.
        """
        from vnflight.handlers import (
            HandlerContext,
            _book_drained_overlay_events,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = ScriptedBridgeClient()
        client.push_events({
            "type": "narration",
            "text": "The cursor blinks.",
            "_seq": 120,
        })
        assert [event["_seq"] for event in client.poll(timeout=0)] == [120]
        assert client.cursor == 120
        ctx = HandlerContext(client=client)
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
            "overlay_texts_by_screen": {"terminal": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "2"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {
                "terminal": [140],
            },
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
        }

        early: dict = {}
        _merge_passive_overlay_text(ctx, early, screen)

        assert render_tool_result_text(early) == "(no new events)"
        assert [
            record["text"] for record in ctx.overlay.pending_deliveries
        ] == [">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<"]

        durable = {
            **screen,
            "type": "screen_content",
        }
        client.push_events(durable)
        drained = client.poll(timeout=0)
        assert [event["_seq"] for event in drained] == [140]
        assert client.cursor == 140
        assert _book_drained_overlay_events(
            ctx, MockWaitResult(events=drained)) == []
        ctx.overlay.timeline_source_sequences["game-a"] = 140
        caught_up: dict = {}
        _merge_passive_overlay_text(
            ctx, caught_up, screen=None, sample_live=False)

        assert render_tool_result_text(caught_up) == (
            ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<"
        )
        assert ctx.overlay.pending_deliveries == []

        replay = MockWaitResult(events=[dict(durable)])
        assert _book_drained_overlay_events(ctx, replay) == []
        repeated: dict = {}
        _merge_passive_overlay_text(
            ctx, repeated, screen=None, sample_live=False)
        assert render_tool_result_text(repeated) == "(no new events)"

    def test_latest_screen_row_waits_at_zero_cursor(self):
        """A fresh timeline has no established presentation frontier."""
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = MockClient()
        client.cursor = 0
        ctx = HandlerContext(client=client)
        screen = {
            "_seq": 14,
            "_source_id": "game-a",
            "_source_seq": 14,
            "overlay_texts": ["SYSTEM BOOT... OK"],
            "overlay_texts_by_screen": {"terminal": ["SYSTEM BOOT... OK"]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [14],
            "passive_overlay_row_seqs_by_screen": {"terminal": [14]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SYSTEM BOOT... OK"],
        }

        early: dict = {}
        _merge_passive_overlay_text(ctx, early, screen)
        assert render_tool_result_text(early) == "(no new events)"
        assert len(ctx.overlay.pending_deliveries) == 1

        client.cursor = 14
        ctx.overlay.timeline_source_sequences["game-a"] = 14
        caught_up: dict = {}
        _merge_passive_overlay_text(
            ctx, caught_up, screen=None, sample_live=False)
        assert render_tool_result_text(caught_up) == "SYSTEM BOOT... OK"

    def test_latest_screen_row_waits_for_prefetched_story(self):
        """The fetch cursor may lead narration parked for a visible wait.

        Sequence test: the prefetch stash is produced by the real
        ``ordinary_action_id`` fence inside ``BridgeClient.poll()`` — a row
        owned by another action is retained, not delivered — and it is
        drained by a real scoped poll.  Nothing about the stash is assigned.
        """
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = ScriptedBridgeClient()
        client.push_events({
            "type": "narration",
            "text": "Another night at the end of the world.",
            "action_id": 22,
            "_seq": 130,
        })
        # An ordinary poll scoped to a different action fences the foreign
        # row into the prefetch stash instead of delivering it.
        assert client.poll(timeout=0, ordinary_action_id=11) == []
        assert client._last_poll_foreign_action_boundary is True
        assert [
            event["_seq"] for event in client._prefetched_events
        ] == [130]
        ctx = HandlerContext(client=client)
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
            "overlay_texts_by_screen": {"terminal": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "2"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": [
                ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<",
            ],
        }

        early: dict = {}
        _merge_passive_overlay_text(ctx, early, screen)
        assert render_tool_result_text(early) == "(no new events)"
        assert len(ctx.overlay.pending_deliveries) == 1

        # The panel's own durable row lands while the narration is still
        # parked.  The retained prefetch row is an ordering fence, so the
        # next ordinary poll must not even read /state to find it — and the
        # poll serial must therefore not advance into the bounded escape.
        client.push_events({**screen, "type": "screen_content"})
        state_reads = client.state_reads()
        serial = client._state_poll_serial
        assert client.poll(timeout=0, ordinary_action_id=11) == []
        assert client.state_reads() == state_reads
        assert client._state_poll_serial == serial
        still_fenced: dict = {}
        _merge_passive_overlay_text(
            ctx, still_fenced, screen=None, sample_live=False)
        assert render_tool_result_text(still_fenced) == "(no new events)"

        # The parked narration is drained by its owner, then the ordinary
        # cursor crosses the panel's durable sequence.
        assert [
            event["text"]
            for event in client.poll(timeout=0, ordinary_action_id=22)
        ] == ["Another night at the end of the world."]
        assert client._prefetched_events == []
        assert [event["_seq"] for event in client.poll(timeout=0)] == [140]
        assert client.cursor == 140
        ctx.overlay.timeline_source_sequences["game-a"] = 140
        caught_up: dict = {}
        _merge_passive_overlay_text(
            ctx, caught_up, screen=None, sample_live=False)
        assert render_tool_result_text(caught_up) == (
            ">> SIGNAL DOES NOT MATCH ANY KNOWN SOURCE <<"
        )

    def test_latest_screen_row_releases_after_empty_ordinary_poll(self):
        """A missing durable event cannot leave cached output fenced forever.

        Sequence test for the poll-serial escape: the serial is advanced only
        by a real ``BridgeClient.poll()`` that got a 200 from the scripted
        bridge, so the escape is exercised by the same event that produces it
        in production — an empty-but-successful ``/state`` read.
        """
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = ScriptedBridgeClient()
        client.push_events({
            "type": "narration",
            "text": "Cold light on the console.",
            "_seq": 120,
        })
        assert [event["_seq"] for event in client.poll(timeout=0)] == [120]
        assert client.cursor == 120
        fenced_at_serial = client._state_poll_serial
        ctx = HandlerContext(client=client)
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": ["SYSTEM BOOT... OK"],
            "overlay_texts_by_screen": {"terminal": ["SYSTEM BOOT... OK"]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SYSTEM BOOT... OK"],
        }

        early: dict = {}
        _merge_passive_overlay_text(ctx, early, screen)
        assert render_tool_result_text(early) == "(no new events)"

        # A successful later /state poll returned no transcript rows. The
        # latest-screen row is now the only remaining presentation source.
        assert client.poll(timeout=0) == []
        assert client._state_poll_serial == fenced_at_serial + 1
        assert client.cursor == 120
        recovered: dict = {}
        _merge_passive_overlay_text(
            ctx, recovered, screen=None, sample_live=False)
        assert render_tool_result_text(recovered) == "SYSTEM BOOT... OK"

    def test_internal_act_poll_releases_overlay_only_at_public_boundary(
        self, monkeypatch,
    ):
        """Fleet r10: a boot overlay row fenced during Start is released at
        Start's own public boundary in bridge order, never split across calls.

        Sequence test: the internal settle poll is a real
        ``BridgeClient.poll()`` against a scripted bridge, so the poll serial
        that would otherwise release the row advances for the production
        reason (a successful empty ``/state``), inside the owner's own
        invocation.
        """
        from vnflight import presentation_lane
        from vnflight import handlers

        client = ScriptedBridgeClient()
        client.push_events({
            "type": "narration",
            "text": "Elara reaches for the console.",
            "_seq": 120,
        })
        assert [event["_seq"] for event in client.poll(timeout=0)] == [120]
        assert client.cursor == 120
        ctx = handlers.HandlerContext(client=client)
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": ["Deep field sensors: CALIBRATING..."],
            "overlay_texts_by_screen": {
                "terminal": ["Deep field sensors: CALIBRATING..."],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["Deep field sensors: CALIBRATING..."],
        }

        def settle_same_act(call_ctx, _params):
            early = {}
            handlers._merge_passive_overlay_text(call_ctx, early, screen)
            assert handlers.render_tool_result_text(early) == "(no new events)"
            serial = client._state_poll_serial
            assert client.poll(timeout=0) == []
            assert client._state_poll_serial == serial + 1
            internal_followup = {}
            handlers._merge_passive_overlay_text(
                call_ctx, internal_followup, screen=None, sample_live=False)
            assert handlers.render_tool_result_text(internal_followup) == (
                "(no new events)"
            )
            internal_followup = {
                "text": "Elara's hands are already moving.",
                "_story_render_sections": [{
                    "channel": "text",
                    "text": "Elara's hands are already moving.",
                    "occurrence_ids": ["narration-150"],
                    "_bridge_seq": 150,
                }],
            }
            return internal_followup

        def next_public_wait(call_ctx, _params):
            recovered = {}
            handlers._merge_passive_overlay_text(
                call_ctx, recovered, screen=None, sample_live=False)
            return recovered

        monkeypatch.setitem(
            handlers._HANDLERS, "_test_settle_same_act", settle_same_act)
        monkeypatch.setitem(
            handlers._HANDLERS, "_test_next_public_wait", next_public_wait)
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {
                "_test_settle_same_act", "_test_next_public_wait",
            },
        )
        monkeypatch.setattr(
            presentation_lane,
            "_OVERLAY_PRESENTATION_RESULT_TOOLS",
            presentation_lane._OVERLAY_PRESENTATION_RESULT_TOOLS | {
                "_test_settle_same_act", "_test_next_public_wait",
            },
        )

        same_act = handlers.handle_tool(ctx, "_test_settle_same_act", {})
        assert handlers.render_tool_result_text(same_act).splitlines() == [
            "Deep field sensors: CALIBRATING...",
            "",
            "Elara's hands are already moving.",
        ]
        assert ctx.overlay.pending_deliveries == []

        later = handlers.handle_tool(ctx, "_test_next_public_wait", {})
        assert handlers.render_tool_result_text(later) == "(no new events)"
        assert ctx.overlay.pending_deliveries == []

    def test_expired_deadline_still_flushes_booked_overlay_rows(self):
        """A result deadline may stop I/O, never ledger presentation."""
        import time
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [{
            "id": 1,
            "text": "[ARIA>] I recommend isolating the hydrogen line.",
            "_bridge_seq": 20,
        }]
        out = {
            "text": "Elara's hands are already moving.",
            "_story_render_sections": [{
                "channel": "text",
                "text": "Elara's hands are already moving.",
                "occurrence_ids": ["narration-30"],
                "_bridge_seq": 30,
            }],
        }

        _merge_passive_overlay_text(
            ctx,
            out,
            deadline=time.time() - 1.0,
        )

        assert render_tool_result_text(out).splitlines() == [
            "[ARIA>] I recommend isolating the hydrogen line.",
            "",
            "Elara's hands are already moving.",
        ]
        assert ctx.overlay.pending_deliveries == []

    def test_pending_cap_releases_evicted_durable_receipt(self, monkeypatch):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        first = ("source", "10", 0)
        second = ("source", "11", 0)
        ctx.overlay.durable_receipts = {first: None, second: None}
        from vnflight import overlay_presentation
        monkeypatch.setattr(
            overlay_presentation, "_OVERLAY_PENDING_DELIVERY_LIMIT", 1)

        overlay_presentation._hold_passive_overlay_deliveries(ctx, [
            {"id": 1, "text": "first", "_durable_receipt": first},
            {"id": 2, "text": "second", "_durable_receipt": second},
        ])

        assert ctx.overlay.pending_deliveries == [
            {"id": 2, "text": "second", "_durable_receipt": second},
        ]
        assert first not in ctx.overlay.durable_receipts
        assert second in ctx.overlay.durable_receipts

    def test_pending_eviction_allows_provisional_row_durable_replay(
        self, monkeypatch,
    ):
        from vnflight import overlay_presentation
        from vnflight import handlers

        client = MockClient()
        client.cursor = 5
        ctx = handlers.HandlerContext(client=client)
        from vnflight import overlay_presentation
        monkeypatch.setattr(
            overlay_presentation, "_OVERLAY_PENDING_DELIVERY_LIMIT", 0)
        legacy = {
            "_seq": 12,
            "_source_id": "source",
            "_source_seq": 12,
            "overlay_texts": ["same"],
            "passive_overlay_row_seqs": [12],
            "passive_overlay_snapshot": True,
        }

        handlers._merge_passive_overlay_text(ctx, {}, legacy)
        assert ctx.overlay.pending_deliveries == []
        assert ctx.overlay.provisional_deliveries == []

        durable = overlay_presentation._book_durable_passive_overlay_delta(ctx, {
            **legacy,
            "passive_overlay_delta": ["same"],
        })
        assert [record["text"] for record in durable] == ["same"]

    def test_pending_eviction_allows_recovered_row_durable_replay(
        self, monkeypatch,
    ):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        recovered = (12, "same", 0)
        ctx.overlay.recovered_occurrences = {recovered: None}
        from vnflight import overlay_presentation
        monkeypatch.setattr(
            overlay_presentation, "_OVERLAY_PENDING_DELIVERY_LIMIT", 0)

        overlay_presentation._hold_passive_overlay_deliveries(ctx, [{
            "id": 7,
            "text": "same",
            "_bridge_seq": 12,
            "_recovered_occurrence": recovered,
        }])

        assert ctx.overlay.recovered_occurrences == {}
        durable = overlay_presentation._book_durable_passive_overlay_delta(ctx, {
            "_seq": 12,
            "_source_id": "source",
            "_source_seq": 12,
            "overlay_texts": ["same"],
            "passive_overlay_row_seqs": [12],
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["same"],
        })
        assert [record["text"] for record in durable] == ["same"]

    def test_durable_reconciliation_transfers_receipt_to_pending_row(
        self, monkeypatch,
    ):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [{
            "id": 1, "text": "same", "_bridge_seq": 12,
        }]
        ctx.overlay.provisional_deliveries = [{
            "id": 1,
            "text": "same",
            "screen_key": ("terminal",),
            "_bridge_seq": 12,
        }]
        screen = {
            "_seq": 12,
            "_source_id": "source",
            "_source_seq": 12,
            "overlay_texts": ["same"],
            "overlay_screens": ["terminal"],
            "passive_overlay_row_seqs": [12],
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["same"],
        }

        assert overlay_presentation._book_durable_passive_overlay_delta(ctx, screen) == []
        receipt = ("source", "12", 0)
        assert ctx.overlay.pending_deliveries[0][
            "_durable_receipt"] == receipt

        from vnflight import overlay_presentation
        monkeypatch.setattr(
            overlay_presentation, "_OVERLAY_PENDING_DELIVERY_LIMIT", 1)
        overlay_presentation._hold_passive_overlay_deliveries(
            ctx, [{"id": 2, "text": "newer"}])
        assert receipt not in ctx.overlay.durable_receipts

        replay = overlay_presentation._book_durable_passive_overlay_delta(ctx, screen)
        assert [record["text"] for record in replay] == ["same"]

    def test_provisional_trim_preserves_pending_reconciliation_owner(self):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [{
            "id": 1, "text": "same", "_bridge_seq": 12,
        }]
        ctx.overlay.provisional_deliveries = [{
            "id": 1,
            "text": "same",
            "screen_key": ("terminal",),
            "_bridge_seq": 12,
        }]
        ctx.overlay.delivery_serial = 1
        overlay_presentation._remember_provisional_overlay_deliveries(
            ctx,
            [
                {"id": index, "text": "row {}".format(index)}
                for index in range(2, 503)
            ],
            {"overlay_screens": ["terminal"]},
        )

        assert any(
            record.get("id") == 1
            for record in ctx.overlay.provisional_deliveries
        )
        durable = overlay_presentation._book_durable_passive_overlay_delta(ctx, {
            "_seq": 12,
            "_source_id": "source",
            "_source_seq": 12,
            "overlay_texts": ["same"],
            "overlay_screens": ["terminal"],
            "passive_overlay_row_seqs": [12],
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["same"],
        })
        assert durable == []

    def test_full_pending_cap_preserves_incoming_provisional_owner(
        self, monkeypatch,
    ):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [
            {"id": index, "text": "row {}".format(index)}
            for index in range(1, 501)
        ]
        ctx.overlay.provisional_deliveries = [
            {
                "id": index,
                "text": "row {}".format(index),
                "screen_key": ("terminal",),
                "_bridge_seq": index,
            }
            for index in range(1, 501)
        ]
        incoming = {"id": 501, "text": "new", "_bridge_seq": 501}

        overlay_presentation._remember_provisional_overlay_deliveries(
            ctx, [incoming], {"overlay_screens": ["terminal"]})
        assert any(
            record.get("id") == 501
            for record in ctx.overlay.provisional_deliveries
        )
        overlay_presentation._hold_passive_overlay_deliveries(ctx, [incoming])

        assert len(ctx.overlay.pending_deliveries) == 500
        assert len(ctx.overlay.provisional_deliveries) == 500
        assert ctx.overlay.pending_deliveries[-1]["id"] == 501
        assert all(
            record.get("id") != 1
            for record in ctx.overlay.provisional_deliveries
        )

        durable = overlay_presentation._book_durable_passive_overlay_delta(ctx, {
            "_seq": 501,
            "_source_id": "source",
            "_source_seq": 501,
            "overlay_texts": ["new"],
            "overlay_screens": ["terminal"],
            "passive_overlay_row_seqs": [501],
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["new"],
        })
        assert durable == []
        assert ctx.overlay.pending_deliveries[-1][
            "_durable_receipt"] == ("source", "501", 0)

    def test_full_pending_cap_retains_recent_delivered_provisional_marker(self):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [
            {"id": index, "text": "row {}".format(index)}
            for index in range(1, 501)
        ]
        ctx.overlay.provisional_deliveries = [
            {
                "id": index,
                "text": "row {}".format(index),
                "screen_key": ("terminal",),
                "_bridge_seq": index,
            }
            for index in range(1, 501)
        ]

        for index in (501, 502):
            overlay_presentation._remember_provisional_overlay_deliveries(
                ctx,
                [{
                    "id": index,
                    "text": "new{}".format(index),
                    "_bridge_seq": index,
                }],
                {"overlay_screens": ["terminal"]},
            )
            overlay_presentation._trim_provisional_overlay_ownership(ctx)

        assert any(
            record.get("id") == 501
            for record in ctx.overlay.provisional_deliveries
        )
        durable = overlay_presentation._book_durable_passive_overlay_delta(ctx, {
            "_seq": 501,
            "_source_id": "source",
            "_source_seq": 501,
            "overlay_texts": ["new501"],
            "overlay_screens": ["terminal"],
            "passive_overlay_row_seqs": [501],
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["new501"],
        })
        assert durable == []

    def test_rendered_provisional_batch_is_bounded_beside_pending_owners(self):
        from vnflight import overlay_presentation
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [
            {"id": index, "text": "pending"}
            for index in range(1, 501)
        ]
        ctx.overlay.provisional_deliveries = [
            {"id": index, "text": "pending"}
            for index in range(1, 501)
        ]
        incoming = [
            {"id": index, "text": "new"}
            for index in range(501, 1501)
        ]

        overlay_presentation._remember_provisional_overlay_deliveries(
            ctx, incoming, {"overlay_screens": ["terminal"]})
        overlay_presentation._trim_provisional_overlay_ownership(ctx)

        assert len(ctx.overlay.provisional_deliveries) == 1000
        assert ctx.overlay.provisional_deliveries[:500] == [
            {"id": index, "text": "pending"}
            for index in range(1, 501)
        ]
        assert ctx.overlay.provisional_deliveries[500]["id"] == 1001

    def test_overlapping_tool_cannot_release_fence_before_owner_completes(
        self, monkeypatch,
    ):
        """A later start is not a later presentation until the owner exits.

        Sequence test: every poll-serial advance below is a real
        ``BridgeClient.poll()`` against a scripted bridge, so the overlapping
        call's escape attempt uses the same signal a live overlapping call
        would have.
        """
        from vnflight import presentation_lane
        import threading
        from vnflight import handlers

        client = ScriptedBridgeClient()
        client.push_events({
            "type": "narration",
            "text": "The array hums.",
            "_seq": 120,
        })
        assert [event["_seq"] for event in client.poll(timeout=0)] == [120]
        assert client.cursor == 120
        ctx = handlers.HandlerContext(client=client)
        owner_fenced = threading.Event()
        owner_may_return = threading.Event()
        overlapping_ready = threading.Event()
        owner_result = []
        overlapping_result = []
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": ["Secondary array: ONLINE"],
            "overlay_texts_by_screen": {
                "terminal": ["Secondary array: ONLINE"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["Secondary array: ONLINE"],
        }

        def owner(call_ctx, _params):
            output = {}
            handlers._merge_passive_overlay_text(call_ctx, output, screen)
            owner_fenced.set()
            assert owner_may_return.wait(2.0)
            return {
                "text": "earlier narration",
                "_story_render_sections": [{
                    "channel": "text",
                    "text": "earlier narration",
                    "occurrence_ids": ["narration-130"],
                    "_bridge_seq": 130,
                }],
            }

        def overlapping(call_ctx, _params):
            assert owner_fenced.wait(2.0)
            assert client.poll(timeout=0) == []
            output = {}
            handlers._merge_passive_overlay_text(
                call_ctx, output, screen=None, sample_live=False)
            overlapping_ready.set()
            return {
                "text": "later narration",
                "_story_render_sections": [{
                    "channel": "text",
                    "text": "later narration",
                    "occurrence_ids": ["narration-150"],
                    "_bridge_seq": 150,
                }],
            }

        def later(call_ctx, _params):
            output = {}
            handlers._merge_passive_overlay_text(
                call_ctx, output, screen=None, sample_live=False)
            return output

        monkeypatch.setitem(handlers._HANDLERS, "_test_owner", owner)
        monkeypatch.setitem(
            handlers._HANDLERS, "_test_overlapping", overlapping)
        monkeypatch.setitem(handlers._HANDLERS, "_test_later", later)
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {
                "_test_owner", "_test_overlapping", "_test_later",
            },
        )
        monkeypatch.setattr(
            presentation_lane,
            "_OVERLAY_PRESENTATION_RESULT_TOOLS",
            presentation_lane._OVERLAY_PRESENTATION_RESULT_TOOLS | {
                "_test_owner", "_test_overlapping", "_test_later",
            },
        )

        thread = threading.Thread(target=lambda: owner_result.append(
            handlers.handle_tool(ctx, "_test_owner", {})))
        thread.start()
        assert owner_fenced.wait(2.0)

        concurrent_thread = threading.Thread(target=lambda: (
            overlapping_result.append(
                handlers.handle_tool(ctx, "_test_overlapping", {}))
        ))
        concurrent_thread.start()
        assert not overlapping_ready.wait(0.1)
        concurrent_thread.join(2.0)
        assert not concurrent_thread.is_alive()
        assert overlapping_result[0]["reason"] == (
            "presentation_call_in_flight")
        assert ctx._presentation_call_lock.locked()
        owner_may_return.set()
        thread.join(2.0)
        assert not thread.is_alive()
        assert not ctx._presentation_call_lock.locked()
        assert handlers.render_tool_result_text(owner_result[0]) == (
            "earlier narration"
        )

        assert client.poll(timeout=0) == []
        recovered = handlers.handle_tool(ctx, "_test_later", {})
        assert handlers.render_tool_result_text(recovered) == (
            "Secondary array: ONLINE")

    @pytest.mark.parametrize("cursor_catches_up", [False, True])
    def test_overlay_cannot_overtake_an_inflight_intermediate_call(
        self, monkeypatch, cursor_catches_up,
    ):
        from vnflight import presentation_lane
        import threading
        from vnflight import handlers

        client = ScriptedBridgeClient()
        client.push_events({
            "type": "narration",
            "text": "older narration",
            "_seq": 120,
        })
        assert [event["_seq"] for event in client.poll(timeout=0)] == [120]
        assert client.cursor == 120
        ctx = handlers.HandlerContext(client=client)
        owner_fenced = threading.Event()
        owner_may_return = threading.Event()
        poller_started = threading.Event()
        poller_may_return = threading.Event()
        poller_result = []
        release_ready = threading.Event()
        release_result = []
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": ["Ambient temperature: -38C"],
            "overlay_texts_by_screen": {
                "terminal": ["Ambient temperature: -38C"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["Ambient temperature: -38C"],
        }

        def owner(call_ctx, _params):
            output = {}
            handlers._merge_passive_overlay_text(call_ctx, output, screen)
            owner_fenced.set()
            assert owner_may_return.wait(2.0)
            return output

        def poller(_call_ctx, _params):
            if cursor_catches_up:
                # The panel's durable row lands: a real poll carries the
                # ordinary cursor across the fenced sequence.
                client.push_events({**screen, "type": "screen_content"})
                assert [
                    event["_seq"] for event in client.poll(timeout=0)
                ] == [140]
                assert client.cursor == 140
                ctx.overlay.timeline_source_sequences["game-a"] = 140
            else:
                # A successful but empty /state read: the serial advances,
                # the cursor does not.
                assert client.poll(timeout=0) == []
                assert client.cursor == 120
            poller_started.set()
            assert poller_may_return.wait(2.0)
            return {"text": "older narration"}

        def release(call_ctx, _params):
            output = {}
            handlers._merge_passive_overlay_text(
                call_ctx, output, screen=None, sample_live=False)
            release_ready.set()
            return output

        monkeypatch.setitem(handlers._HANDLERS, "_test_owner_gap", owner)
        monkeypatch.setitem(handlers._HANDLERS, "_test_poller_gap", poller)
        monkeypatch.setitem(handlers._HANDLERS, "_test_release_gap", release)
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {
                "_test_owner_gap", "_test_poller_gap", "_test_release_gap",
            },
        )
        monkeypatch.setattr(
            presentation_lane,
            "_OVERLAY_PRESENTATION_RESULT_TOOLS",
            presentation_lane._OVERLAY_PRESENTATION_RESULT_TOOLS | {
                "_test_owner_gap", "_test_poller_gap", "_test_release_gap",
            },
        )

        owner_thread = threading.Thread(
            target=handlers.handle_tool,
            args=(ctx, "_test_owner_gap", {}),
        )
        poller_thread = threading.Thread(
            target=lambda: poller_result.append(
                handlers.handle_tool(ctx, "_test_poller_gap", {})),
        )
        owner_thread.start()
        assert owner_fenced.wait(2.0)
        poller_thread.start()
        assert not poller_started.wait(0.1)
        poller_thread.join(2.0)
        assert not poller_thread.is_alive()
        assert poller_result[0]["reason"] == "presentation_call_in_flight"

        owner_may_return.set()
        owner_thread.join(2.0)
        assert not owner_thread.is_alive()
        poller_result.clear()
        poller_thread = threading.Thread(
            target=lambda: poller_result.append(
                handlers.handle_tool(ctx, "_test_poller_gap", {})),
        )
        poller_thread.start()
        assert poller_started.wait(2.0)
        poller_may_return.set()
        poller_thread.join(2.0)
        assert not poller_thread.is_alive()
        assert handlers.render_tool_result_text(poller_result[0]).splitlines() == [
            "older narration",
            "",
            "Ambient temperature: -38C",
        ]
        recovered = handlers.handle_tool(ctx, "_test_release_gap", {})
        assert release_ready.is_set()
        assert handlers.render_tool_result_text(recovered) == "(no new events)"

    def test_older_call_cannot_steal_later_owners_overlay(
        self, monkeypatch,
    ):
        from vnflight import presentation_lane
        import threading
        from vnflight import handlers

        client = MockClient()
        client.cursor = 120
        client._state_poll_serial = 7
        ctx = handlers.HandlerContext(client=client)
        older_started = threading.Event()
        older_may_merge = threading.Event()
        owner_fenced = threading.Event()
        owner_may_return = threading.Event()
        older_result = []
        owner_result = []
        screen = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "overlay_texts": ["Deep field sensors: CALIBRATING..."],
            "overlay_texts_by_screen": {
                "terminal": ["Deep field sensors: CALIBRATING..."],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {"terminal": [140]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["Deep field sensors: CALIBRATING..."],
        }

        def older(call_ctx, _params):
            older_started.set()
            assert older_may_merge.wait(2.0)
            output = {}
            handlers._merge_passive_overlay_text(
                call_ctx, output, screen=None, sample_live=False)
            return output

        def owner(call_ctx, _params):
            output = {}
            handlers._merge_passive_overlay_text(call_ctx, output, screen)
            client.cursor = 140
            ctx.overlay.timeline_source_sequences["game-a"] = 140
            owner_fenced.set()
            assert owner_may_return.wait(2.0)
            return output

        def release(call_ctx, _params):
            output = {}
            handlers._merge_passive_overlay_text(
                call_ctx, output, screen=None, sample_live=False)
            return output

        monkeypatch.setitem(handlers._HANDLERS, "_test_older", older)
        monkeypatch.setitem(handlers._HANDLERS, "_test_later_owner", owner)
        monkeypatch.setitem(handlers._HANDLERS, "_test_release_owner", release)
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {
                "_test_older", "_test_later_owner", "_test_release_owner",
            },
        )
        monkeypatch.setattr(
            presentation_lane,
            "_OVERLAY_PRESENTATION_RESULT_TOOLS",
            presentation_lane._OVERLAY_PRESENTATION_RESULT_TOOLS | {
                "_test_older", "_test_later_owner", "_test_release_owner",
            },
        )

        older_thread = threading.Thread(target=lambda: older_result.append(
            handlers.handle_tool(ctx, "_test_older", {})))
        owner_thread = threading.Thread(target=lambda: owner_result.append(
            handlers.handle_tool(ctx, "_test_later_owner", {})))
        older_thread.start()
        assert older_started.wait(2.0)
        owner_thread.start()
        assert not owner_fenced.wait(0.1)
        owner_thread.join(2.0)
        assert not owner_thread.is_alive()
        assert owner_result[0]["reason"] == "presentation_call_in_flight"

        older_may_merge.set()
        older_thread.join(2.0)
        assert not older_thread.is_alive()
        assert handlers.render_tool_result_text(older_result[0]) == (
            "(no new events)"
        )
        owner_result.clear()
        owner_thread = threading.Thread(target=lambda: owner_result.append(
            handlers.handle_tool(ctx, "_test_later_owner", {})))
        owner_thread.start()
        assert owner_fenced.wait(2.0)

        owner_may_return.set()
        owner_thread.join(2.0)
        assert not owner_thread.is_alive()
        assert handlers.render_tool_result_text(owner_result[0]) == (
            "Deep field sensors: CALIBRATING..."
        )
        recovered = handlers.handle_tool(ctx, "_test_release_owner", {})
        assert handlers.render_tool_result_text(recovered) == "(no new events)"

    def test_concurrent_state_call_is_refused_before_its_handler_runs(
        self, monkeypatch,
    ):
        from vnflight import presentation_lane
        import threading
        import time
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        first_entered = threading.Event()
        first_may_return = threading.Event()
        observed = []

        def concurrent(_call_ctx, _params):
            observed.append(handlers._ACTIVE_PRESENTATION_INVOCATION.get())
            first_entered.set()
            assert first_may_return.wait(2.0)
            return {}

        monkeypatch.setitem(
            handlers._HANDLERS, "_test_simultaneous_entry", concurrent)
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {
                "_test_simultaneous_entry",
            },
        )
        first = threading.Thread(
            target=handlers.handle_tool,
            args=(ctx, "_test_simultaneous_entry", {}),
        )
        first.start()
        assert first_entered.wait(2.0)

        refused = handlers.handle_tool(ctx, "state", {
            "_response_deadline": time.time() + 0.05,
        })

        assert refused["reason"] == "presentation_call_in_flight"
        assert observed == [id(ctx)]
        assert ctx.client.calls == []
        first_may_return.set()
        first.join(2.0)
        assert not first.is_alive()
        assert not ctx._presentation_call_lock.locked()

    def test_busy_presentation_lane_reports_owner_age_and_watchdog(self):
        import time
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx._presentation_call_name = "wait"
        ctx._presentation_call_started_at = time.monotonic() - 12.0
        ctx._presentation_call_watchdog_seconds = 10.0
        ctx._presentation_call_lock.acquire()
        try:
            result = handlers.handle_tool(ctx, "state", {})
        finally:
            ctx._presentation_call_lock.release()

        assert result["reason"] == "presentation_call_in_flight"
        assert result["active_tool"] == "wait"
        assert result["active_for_seconds"] >= 12.0
        assert result["watchdog_exceeded"] is True
        assert "watchdog threshold was exceeded" in result["error"]

    def test_busy_wait_watchdog_uses_expected_call_deadline(self):
        from vnflight import presentation_lane
        import time
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx._presentation_call_name = "wait"
        ctx._presentation_call_started_at = time.monotonic() - 300.0
        ctx._presentation_call_watchdog_at = (
            ctx._presentation_call_started_at + 305.0)

        before_budget = presentation_lane._presentation_call_busy_result(ctx)
        ctx._presentation_call_watchdog_at = time.monotonic() - 0.1
        after_budget = presentation_lane._presentation_call_busy_result(ctx)

        assert before_budget["watchdog_exceeded"] is False
        assert after_budget["watchdog_exceeded"] is True

    def test_mcp_result_deadline_overrides_long_requested_wait_watchdog(self):
        from vnflight import presentation_lane
        import time
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        started = time.monotonic() - 116.0
        watchdog_at = presentation_lane._presentation_watchdog_at(
            ctx,
            "wait",
            {"timeout": 300, "_result_deadline": time.time() + 110.0},
            started,
        )

        assert watchdog_at < time.monotonic()

    def test_presentation_lane_clears_watchdog_owner_after_return(
        self, monkeypatch,
    ):
        from vnflight import presentation_lane
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        monkeypatch.setitem(
            handlers._HANDLERS, "_test_watchdog_owner",
            lambda _ctx, _params: {"ok": True},
        )
        monkeypatch.setattr(
            presentation_lane,
            "_PRESENTATION_RESULT_TOOLS",
            presentation_lane._PRESENTATION_RESULT_TOOLS | {"_test_watchdog_owner"},
        )

        result = handlers.handle_tool(ctx, "_test_watchdog_owner", {})

        assert result == {"ok": True}
        assert ctx._presentation_call_name is None
        assert ctx._presentation_call_started_at == 0.0
        assert ctx._presentation_call_watchdog_at == 0.0

    def test_stop_interrupts_a_long_wait_that_holds_the_lane(self):
        import threading
        from vnflight import handlers

        client = _BlockingWaitClient()
        ctx = handlers.HandlerContext(client=client)
        ctx._presentation_preempt_wait_seconds = 5.0
        cli_calls = []
        ctx.hooks.run_cli = lambda *args, **kw: (
            cli_calls.append(args), {"ok": True, "output": "stopped"})[1]

        wait_result = []
        waiter = threading.Thread(target=lambda: wait_result.append(
            handlers.handle_tool(ctx, "wait", {"timeout": 300})))
        waiter.start()
        assert client.wait_entered.wait(2.0)

        stopped = handlers.handle_tool(ctx, "stop", {"game": "echoes"})

        assert stopped["ok"] is True
        # The owner handed the lane back, so nothing had to be fenced off.
        assert "presentation_lane_preempted" not in stopped
        assert client.interrupted is True
        assert any("stop" in args for args in cli_calls)
        waiter.join(5.0)
        assert not waiter.is_alive()
        assert "presentation_lane_superseded" not in wait_result[0]

    def test_second_wait_is_still_refused_while_a_wait_holds_the_lane(self):
        import threading
        from vnflight import handlers

        client = _BlockingWaitClient()
        ctx = handlers.HandlerContext(client=client)

        waiter = threading.Thread(
            target=handlers.handle_tool, args=(ctx, "wait", {"timeout": 300}))
        waiter.start()
        try:
            assert client.wait_entered.wait(2.0)
            refused = handlers.handle_tool(ctx, "wait", {"timeout": 5})
        finally:
            client.release.set()

        assert refused["reason"] == "presentation_call_in_flight"
        assert refused["active_tool"] == "wait"
        waiter.join(5.0)
        assert not waiter.is_alive()

    def test_stop_preempts_a_wedged_wait_and_fences_its_late_result(
        self, monkeypatch,
    ):
        from vnflight import presentation_lane
        import threading
        from vnflight import handlers

        client = _BlockingWaitClient(honor_interrupt=False)
        ctx = handlers.HandlerContext(client=client)
        # The wedged owner never notices the interrupt; keep the bounded wait
        # short so the test exercises the take-over branch quickly.
        ctx._presentation_preempt_wait_seconds = 0.2
        ctx.hooks.run_cli = lambda *args, **kw: {
            "ok": True, "output": "stopped"}
        ctx.overlay.pending_deliveries = [{
            "id": "row-9",
            "text": "Reactor: CRITICAL",
            "occurrence_ids": ["row-9"],
            "_bridge_seq": 40,
        }]
        ctx.overlay.timeline_source_sequences = {"dead-game": 40}

        flushed = []
        real_flush = presentation_lane._flush_public_presentation_rows
        monkeypatch.setattr(
            presentation_lane,
            "_flush_public_presentation_rows",
            lambda c, r, p: (flushed.append(r), real_flush(c, r, p))[1],
        )

        wait_result = []
        waiter = threading.Thread(target=lambda: wait_result.append(
            handlers.handle_tool(ctx, "wait", {"timeout": 300})))
        waiter.start()
        assert client.wait_entered.wait(2.0)

        stopped = handlers.handle_tool(ctx, "stop", {"game": "echoes"})

        assert stopped["ok"] is True
        assert stopped["presentation_lane_preempted"] is True
        assert client.interrupted is False
        # The stop cleared the dead game's ledgers even though the wedged
        # wait still owned the lane.
        assert ctx.overlay.pending_deliveries == []
        assert ctx.overlay.timeline_source_sequences == {}

        client.release.set()
        waiter.join(5.0)
        assert not waiter.is_alive()
        # The abandoned wait never reached the publication boundary, and its
        # late ledger writes were discarded rather than left for the next game.
        assert flushed == []
        assert wait_result[0]["presentation_lane_superseded"] is True
        assert ctx.overlay.pending_deliveries == []
        assert ctx.overlay.timeline_source_sequences == {}
        assert "Reactor: CRITICAL" not in handlers.render_tool_result_text(
            handlers.handle_tool(ctx, "state", {}))

    def test_preemptible_binding_transition_takes_over_a_wedged_lane(self):
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx._presentation_preempt_wait_seconds = 0.05
        ctx.overlay.pending_deliveries = [{"id": 1, "text": "old row"}]
        ctx._presentation_call_lock.acquire()
        try:
            result = handlers.run_presentation_transition(
                ctx,
                lambda: {"ok": True},
                transition_name="bridge_connect",
                preemptible=True,
            )
        finally:
            ctx._presentation_call_lock.release()

        assert result["ok"] is True
        assert result["presentation_lane_preempted"] is True
        assert ctx.overlay.pending_deliveries == []

    def test_lifecycle_lane_wait_is_bounded_by_the_response_deadline(self):
        from vnflight import presentation_lane
        import time
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx._presentation_preempt_wait_seconds = 30.0

        assert presentation_lane._presentation_preempt_budget(ctx, {}) == 30.0
        bounded = presentation_lane._presentation_preempt_budget(ctx, {
            "_response_deadline": time.time() + 25.0,
        })
        assert 4.0 < bounded < 5.1
        assert presentation_lane._presentation_preempt_budget(ctx, {
            "_transport_deadline": time.time() + 5.0,
        }) == 0.0

    def test_public_state_claims_deferred_overlay_story_once(
        self, monkeypatch,
    ):
        from vnflight import handlers

        ctx = handlers.HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [{
            "id": "row-1",
            "text": "Secondary array: ONLINE",
            "occurrence_ids": ["row-1"],
            "_bridge_seq": 12,
        }]
        monkeypatch.setitem(
            handlers._HANDLERS, "state", lambda _ctx, _params: {})

        first = handlers.handle_tool(ctx, "state", {})
        second = handlers.handle_tool(ctx, "state", {})

        assert handlers.render_tool_result_text(first) == (
            "Secondary array: ONLINE")
        assert handlers.render_tool_result_text(second) == "(no new events)"

    def test_public_state_discards_overlay_but_preserves_unproven_action(
        self, monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("old-action", 4)
        ctx = handlers.HandlerContext(client=client)
        ctx.overlay.timeline_source_sequences = {"game": 10}
        ctx.overlay.pending_deliveries = [{
            "id": "old-row",
            "text": "OLD TIMELINE ROW",
            "occurrence_ids": ["old-row"],
            "_bridge_seq": 12,
        }]
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing",
            "transcript": [{
                "type": "game_started",
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }],
        })
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_args, **_kwargs: None)

        result = handlers.handle_tool(ctx, "state", {})

        assert "OLD TIMELINE ROW" not in handlers.render_tool_result_text(result)
        assert ctx.overlay.pending_deliveries == []
        assert ctx.overlay.timeline_source_sequences == {"game": 20}
        # The native boundary has no monotonic action id. Its overlay is
        # certainly stale, but there is no proof that an unresolved action
        # predates it, so nonce retirement deliberately fails duplicate-side.
        assert client._active_action_nonces == ["old-action"]
        assert client._auto_action_nonce_retired == {}

    def test_public_state_preserves_action_that_owns_timeline_boundary(
        self, monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("old-action", 4)
        client._track_action_nonce("start-action", 10)
        ctx = handlers.HandlerContext(client=client)
        ctx.overlay.timeline_source_sequences = {"game": 10}
        ctx.overlay.pending_deliveries = [{
            "id": "old-row",
            "text": "OLD TIMELINE ROW",
            "occurrence_ids": ["old-row"],
            "_bridge_seq": 12,
        }]
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing",
            "transcript": [{
                "type": "game_started",
                "action_id": 10,
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }],
        })
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_args, **_kwargs: None)

        result = handlers.handle_tool(ctx, "state", {})

        assert "OLD TIMELINE ROW" not in handlers.render_tool_result_text(result)
        assert ctx.overlay.pending_deliveries == []
        assert client._active_action_nonces == ["start-action"]
        assert client._auto_action_nonce_retired == {
            "old-action": "timeline_reset",
        }

    def test_public_state_preserves_unknown_action_through_explicit_receipt(
        self, monkeypatch,
    ):
        from unittest.mock import patch
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._action_delivery_reset_generation = 5
        client._track_action_nonce("lost-acceptance")
        ctx = handlers.HandlerContext(client=client)
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing",
            "reset_generation": 5,
            "transcript": [{
                "type": "game_started",
                "action_id": 10,
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }],
        })
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_args, **_kwargs: None)

        handlers.handle_tool(ctx, "state", {})

        assert client._active_action_nonces == ["lost-acceptance"]
        assert "lost-acceptance" not in client._auto_action_nonce_retired

        transaction = {
            "action_nonce": "lost-acceptance",
            "action_id": 10,
            "reset_generation": 5,
            "transaction_state": "settled",
            "pending": False,
            "delivery_end": 99,
            "events": [{
                "type": "narration",
                "text": "The new opening continues.",
                "action_id": 10,
                "_seq": 21,
            }],
        }
        with patch.object(
            client, "_get", return_value=(200, {"transaction": transaction}),
        ):
            continued = client.wait(
                timeout=0.1,
                min_wait=0,
                action_nonce="lost-acceptance",
            )

        assert [event.get("text") for event in continued.events] == [
            "The new opening continues.",
        ]

    def test_public_state_uses_latest_retained_lifecycle_owner(
        self, monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("older-start", 10)
        client._track_action_nonce("current-load", 20)
        ctx = handlers.HandlerContext(client=client)
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing",
            "transcript": [{
                "type": "game_started",
                "action_id": 10,
                "_source_id": "game",
                "_source_seq": 10,
                "_seq": 10,
            }, {
                "type": "game_resumed",
                "action_id": 20,
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }],
        })
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_args, **_kwargs: None)

        handlers.handle_tool(ctx, "state", {})

        assert client._active_action_nonces == ["current-load"]
        assert client._auto_action_nonce_retired == {
            "older-start": "timeline_reset",
        }
        assert ctx.overlay.timeline_source_sequences == {"game": 20}

    def test_delayed_owned_boundary_preserves_newer_active_action(
        self, monkeypatch,
    ):
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("start-action", 10)
        client._track_action_nonce("newer-action", 11)
        ctx = handlers.HandlerContext(client=client)
        monkeypatch.setattr(client, "state", lambda **_kwargs: {
            "status": "playing",
            "transcript": [{
                "type": "game_started",
                "action_id": 10,
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }],
        })
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_args, **_kwargs: None)

        handlers.handle_tool(ctx, "state", {})

        assert client._active_action_nonces == [
            "start-action", "newer-action",
        ]
        assert client._auto_action_nonce_retired == {}

    def test_unresolved_older_action_cannot_replay_across_state_boundary(
        self, monkeypatch,
    ):
        from unittest.mock import patch
        from vnflight import handlers
        from vnflight.client import BridgeClient

        client = BridgeClient("http://bridge", slot_prefix="/1")
        client._track_action_nonce("older-unknown")
        client._track_action_nonce("start-unknown")
        ctx = handlers.HandlerContext(client=client)
        boundary_state = {
            "status": "playing",
            "reset_generation": 5,
            "transcript": [{
                "type": "game_started",
                "action_id": 20,
                "_source_id": "game",
                "_source_seq": 20,
                "_seq": 20,
            }],
        }
        from vnflight import overlay_presentation
        monkeypatch.setattr(handlers, "_get_screen", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            overlay_presentation, "_get_screen", lambda *_args, **_kwargs: None)
        with patch.object(client, "_get", return_value=(200, boundary_state)):
            handlers.handle_tool(ctx, "state", {})

        assert client._active_action_nonces == [
            "older-unknown", "start-unknown",
        ]
        # A transport retry reuses the nonce; it must not erase the boundary.
        client._track_action_nonce("older-unknown")
        old_transaction = {
            "action_nonce": "older-unknown",
            "action_id": 19,
            "reset_generation": 5,
            "transaction_state": "settled",
            "pending": False,
            "delivery_end": 99,
            "events": [{
                "type": "narration",
                "text": "OLD ENDING ROW",
                "action_id": 19,
                "_seq": 99,
            }],
        }
        with patch.object(
            client, "_get", return_value=(200, {"transaction": old_transaction}),
        ):
            waited = client.wait(
                timeout=0.1,
                min_wait=0,
                action_nonce="older-unknown",
            )

        assert waited.events == []
        assert "older-unknown" not in client._active_action_nonces
        assert client._active_action_nonces == ["start-unknown"]
        assert client._auto_action_nonce_retired["older-unknown"] == (
            "timeline_reset"
        )

    def test_overlapping_calls_claim_deferred_overlay_once(self):
        import threading
        from vnflight.handlers import HandlerContext
        from vnflight.overlay_presentation import (
            _claim_passive_overlay_deliveries,
        )

        ctx = HandlerContext(client=MockClient())
        ctx.overlay.pending_deliveries = [{
            "id": "row-1", "text": "Secondary array: ONLINE",
        }]
        start = threading.Barrier(3)
        claimed = []

        def claim():
            start.wait(timeout=2.0)
            claimed.append(_claim_passive_overlay_deliveries(ctx))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=2.0)
        for thread in threads:
            thread.join(2.0)

        assert all(not thread.is_alive() for thread in threads)
        assert sum(len(batch) for batch in claimed) == 1
        assert [row for batch in claimed for row in batch] == [{
            "id": "row-1", "text": "Secondary array: ONLINE",
        }]
        assert ctx.overlay.pending_deliveries == []

    def test_overlapping_calls_book_durable_overlay_once(self):
        import threading
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        screen = {
            "overlay_texts": ["Secondary array: ONLINE"],
            "overlay_texts_by_screen": {
                "terminal": ["Secondary array: ONLINE"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["Secondary array: ONLINE"],
        }
        start = threading.Barrier(3)
        outputs = []

        def merge():
            start.wait(timeout=2.0)
            output = {}
            _merge_passive_overlay_text(ctx, output, dict(screen))
            outputs.append(render_tool_result_text(output))

        threads = [threading.Thread(target=merge) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=2.0)
        for thread in threads:
            thread.join(2.0)

        assert all(not thread.is_alive() for thread in threads)
        assert outputs.count("Secondary array: ONLINE") == 1
        assert outputs.count("(no new events)") == 1

    def test_timeline_reset_waits_for_overlay_ledger_owner(self):
        import threading
        from vnflight.handlers import HandlerContext, _reset_timeline_context

        ctx = HandlerContext(client=MockClient())
        ctx.overlay.text_snapshot = ["old timeline"]
        started = threading.Event()
        finished = threading.Event()

        def reset():
            started.set()
            _reset_timeline_context(ctx)
            finished.set()

        with ctx.overlay.lock:
            thread = threading.Thread(target=reset)
            thread.start()
            assert started.wait(2.0)
            assert not finished.wait(0.05)

        thread.join(2.0)
        assert not thread.is_alive()
        assert finished.is_set()
        assert ctx.overlay.text_snapshot == []

    def test_one_shot_context_uses_durable_overlay_lane_only(self):
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = MockClient()
        client.cursor = 120
        ctx = HandlerContext(
            client=client, allow_live_overlay_lookahead=False)
        output = {}
        _merge_passive_overlay_text(ctx, output, {
            "_seq": 140,
            "overlay_texts": ["Ambient temperature: -38C"],
            "overlay_texts_by_screen": {
                "terminal": ["Ambient temperature: -38C"],
            },
            "overlay_screens": ["terminal"],
        })

        assert render_tool_result_text(output) == "(no new events)"
        assert ctx.overlay.pending_deliveries == []

    def test_latest_screen_after_warm_respawn_ignores_stale_cursor(self):
        """Bridge seq resets while the running shim source keeps advancing."""
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = MockClient()
        client.cursor = 200
        client._state_poll_serial = 9
        ctx = HandlerContext(client=client)
        ctx.overlay.timeline_source_sequences = {"shim-a": 100}
        screen = {
            "_seq": 20,
            "_source_id": "shim-a",
            "_source_seq": 220,
            "overlay_texts": ["SYSTEM BOOT... OK"],
            "overlay_texts_by_screen": {"terminal": ["SYSTEM BOOT... OK"]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [20],
            "passive_overlay_row_seqs_by_screen": {"terminal": [20]},
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SYSTEM BOOT... OK"],
        }

        early: dict = {}
        _merge_passive_overlay_text(ctx, early, screen)
        assert render_tool_result_text(early) == "(no new events)"

        client.cursor = 20
        client._state_poll_serial += 1
        caught_up: dict = {}
        _merge_passive_overlay_text(
            ctx, caught_up, screen=None, sample_live=False)
        assert render_tool_result_text(caught_up) == "SYSTEM BOOT... OK"

    def test_scoped_drained_overlay_bypasses_ordinary_cursor_fence(self):
        """A transaction-owned row is not latest-screen look-ahead."""
        from vnflight.handlers import (
            HandlerContext,
            _book_drained_overlay_events,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = MockClient()
        client.cursor = 120
        ctx = HandlerContext(client=client)
        durable = {
            "type": "screen_content",
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "action_id": 7,
            "overlay_texts": ["SYSTEM BOOT... OK"],
            "overlay_texts_by_screen": {
                "terminal": ["SYSTEM BOOT... OK"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "2"},
            "passive_overlay_row_seqs": [140],
            "passive_overlay_row_seqs_by_screen": {
                "terminal": [140],
            },
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SYSTEM BOOT... OK"],
        }
        drained = MockWaitResult(
            events=[durable],
            transaction={
                "action_id": 7,
                "_source_id": "game-a",
                "_source_seq": 130,
            },
        )

        assert _book_drained_overlay_events(ctx, drained) == []
        out: dict = {}
        _merge_passive_overlay_text(
            ctx, out, screen=None, sample_live=False)

        assert render_tool_result_text(out) == "SYSTEM BOOT... OK"
        assert ctx.overlay.pending_deliveries == []

    def test_cumulative_lookahead_uses_each_rows_original_bridge_position(self):
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        result: dict = {}
        _merge_passive_overlay_text(ctx, result, {
            "_seq": 74,
            "overlay_texts": ["SYSTEM BOOT... OK"],
            "overlay_texts_by_screen": {
                "terminal": ["SYSTEM BOOT... OK"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [74],
            "passive_overlay_row_seqs_by_screen": {"terminal": [74]},
        })
        ctx.client.cursor = 74
        _merge_passive_overlay_text(
            ctx, result, screen=None, sample_live=False)
        _promote_wait_output_preserving_story(result, {
            "text": "The cursor blinks.",
            "_story_render_sections": [{
                "channel": "text",
                "text": "The cursor blinks.",
                "occurrence_ids": ["bridge:113"],
                "_bridge_seq": 113,
            }],
        })
        _merge_passive_overlay_text(ctx, result, {
            # The cumulative cache was sampled after narration, but ARIA's row
            # first appeared before it in the retained bridge stream.
            "_seq": 120,
            "overlay_texts": ["SYSTEM BOOT... OK", "ARIA v4.2.1"],
            "overlay_texts_by_screen": {
                "terminal": ["SYSTEM BOOT... OK", "ARIA v4.2.1"],
            },
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
            "passive_overlay_row_seqs": [74, 82],
            "passive_overlay_row_seqs_by_screen": {
                "terminal": [74, 82],
            },
        })
        ctx.client.cursor = 120
        _merge_passive_overlay_text(
            ctx, result, screen=None, sample_live=False)

        assert render_tool_result_text(result).splitlines() == [
            "SYSTEM BOOT... OK",
            "ARIA v4.2.1",
            "",
            "The cursor blinks.",
        ]

    def test_cumulative_lookahead_dates_repeated_append_as_new_occurrence(self):
        from vnflight.handlers import HandlerContext
        from vnflight.overlay_presentation import (
            _book_passive_overlay_snapshot)

        ctx = HandlerContext(client=MockClient())
        first = _book_passive_overlay_snapshot(ctx, {
            "_seq": 10,
            "overlay_texts": ["SAME"],
            "passive_overlay_row_seqs": [10],
        })
        second = _book_passive_overlay_snapshot(ctx, {
            "_seq": 20,
            "overlay_texts": ["SAME", "SAME"],
            "passive_overlay_row_seqs": [10, 20],
        })

        assert [row["_bridge_seq"] for row in first + second] == [10, 20]

    def test_opening_rows_survive_repeated_promotions_in_source_order(self):
        """Fleet regression: Start used to append boot rows after narration."""
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        result = {
            "screen_text": "SYSTEM BOOT... OK\nARIA v4.2.1",
            "_overlay_deliveries": [
                {"id": 1, "text": "SYSTEM BOOT... OK",
                 "channel": "screen_text", "_bridge_seq": 74},
                {"id": 2, "text": "ARIA v4.2.1",
                 "channel": "screen_text", "_bridge_seq": 81},
            ],
        }
        _promote_wait_output_preserving_story(result, {
            "text": "The cursor blinks.",
            "_story_render_sections": [{
                "channel": "text",
                "text": "The cursor blinks.",
                "occurrence_ids": ["bridge:114"],
                "_bridge_seq": 114,
            }],
        })
        _promote_wait_output_preserving_story(result, {
            "text": "Another night.",
            "_story_render_sections": [{
                "channel": "text",
                "text": "Another night.",
                "occurrence_ids": ["bridge:120"],
                "_bridge_seq": 120,
            }],
        })

        assert render_tool_result_text(result).splitlines() == [
            "SYSTEM BOOT... OK",
            "ARIA v4.2.1",
            "",
            "The cursor blinks.",
            "Another night.",
        ]

    def test_provisional_reconciliation_keeps_a_second_equal_occurrence(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext,
            _book_drained_overlay_events,
            _merge_passive_overlay_text,
        )

        ctx = HandlerContext(client=MockClient())
        screen = {
            "_seq": 20,
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"terminal": ["SAME"]},
            "passive_overlay_row_seqs_by_screen": {"terminal": [20]},
            "overlay_screens": ["terminal"],
            "overlay_generations": {"terminal": "1"},
        }
        _merge_passive_overlay_text(ctx, {}, screen)
        durable = {
            **screen,
            "type": "screen_content",
            "_seq": 30,
            "_source_id": "shim-a",
            "_source_seq": 30,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["SAME"],
            "overlay_texts": ["SAME", "SAME"],
            "overlay_texts_by_screen": {"terminal": ["SAME", "SAME"]},
            "passive_overlay_row_seqs_by_screen": {"terminal": [20, 30]},
        }
        drained = SimpleNamespace(events=[
            durable,
            {"type": "narration", "text": "After.", "_seq": 31},
        ])

        deliveries = _book_drained_overlay_events(ctx, drained)

        assert [record["text"] for record in deliveries] == ["SAME"]
        assert [event.get("texts") for event in drained.events[:-1]] == [["SAME"]]

    def test_promoted_durable_rows_reorder_by_bridge_sequence(self):
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text)

        result = {
            "screen_text": "LATER TERMINAL",
            "_overlay_deliveries": [{
                "id": -300001,
                "text": "LATER TERMINAL",
                "channel": "screen_text",
                "_bridge_seq": 30,
            }],
        }
        wait_result = {
            "text": "MIDDLE NARRATION",
            "_story_render_sections": [{
                "channel": "text",
                "text": "MIDDLE NARRATION",
                "_bridge_seq": 20,
            }],
        }

        _promote_wait_output_preserving_story(result, wait_result)

        assert render_tool_result_text(result).splitlines() == [
            "MIDDLE NARRATION",
            "",
            "LATER TERMINAL",
        ]

    def test_promoted_overlay_inserts_among_story_with_unsequenced_prefix(self):
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        result = {
            "text": "Selected analysis.\nBefore terminal.\nAfter terminal.",
            "screen_text": "IT IS FROM WHEN.",
            "_overlay_deliveries": [{
                "id": -4500001,
                "text": "IT IS FROM WHEN.",
                "channel": "screen_text",
                "_bridge_seq": 450,
            }],
            "_story_render_sections": [
                {"channel": "text", "text": "Selected analysis."},
                {
                    "channel": "text",
                    "text": "Before terminal.",
                    "_bridge_seq": 400,
                },
                {
                    "channel": "text",
                    "text": "After terminal.",
                    "_bridge_seq": 500,
                },
            ],
        }
        wait_result = {
            "pending": "--- CHOICE REQUIRED ---",
        }

        _promote_wait_output_preserving_story(result, wait_result)

        assert render_tool_result_text(result).splitlines() == [
            "Selected analysis.",
            "Before terminal.",
            "",
            "IT IS FROM WHEN.",
            "",
            "After terminal.",
            "",
            "--- CHOICE REQUIRED ---",
        ]

    def test_drained_contributor_reopen_uses_screen_instance_generation(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext, _book_drained_overlay_events)

        ctx = HandlerContext(client=MockClient())
        full = {
            "type": "screen_content",
            "passive_overlay_snapshot": True,
            "overlay_texts": ["A", "B"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
            "overlay_screens": ["a", "b"],
            "overlay_generations": {"a": "instance:1", "b": "instance:1"},
        }
        only_a = {
            **full,
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_screens": ["a"],
            "overlay_generations": {"a": "instance:1"},
        }
        reopened = {
            **full,
            "overlay_generations": {"a": "instance:1", "b": "instance:2"},
        }
        result = SimpleNamespace(events=[
            {"type": "narration", "text": "before"},
            full, only_a, reopened,
        ])

        deliveries = _book_drained_overlay_events(ctx, result)

        assert [record["text"] for record in deliveries] == ["A", "B", "B"]

    def test_retained_empty_same_generation_does_not_replay(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        shown = {
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "overlay_screens": ["a"],
            "overlay_generations": {"a": "1"},
            "overlay_retained_screens": ["a"],
        }
        empty = {
            **shown,
            "overlay_texts": [],
            "overlay_texts_by_screen": {"a": []},
        }
        _merge_passive_overlay_text(ctx, {}, shown)
        _merge_passive_overlay_text(ctx, {}, empty)
        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, shown)

        assert "screen_text" not in reopened

        next_empty = {
            **empty,
            "overlay_generations": {"a": "2"},
        }
        _merge_passive_overlay_text(ctx, {}, next_empty)
        next_shown = {
            **shown,
            "overlay_generations": {"a": "2"},
        }
        next_output: dict = {}
        _merge_passive_overlay_text(ctx, next_output, next_shown)
        assert next_output["screen_text"] == "SAME"

    def test_retained_to_ordinary_empty_closes_generation(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        retained = {
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "overlay_screens": ["a"],
            "overlay_generations": {"a": "instance:1"},
            "overlay_retained_screens": ["a"],
        }
        ordinary_empty = {
            **retained,
            "overlay_texts": [],
            "overlay_texts_by_screen": {"a": []},
            "overlay_retained_screens": [],
        }
        ordinary_shown = {
            **retained,
            "overlay_retained_screens": [],
        }
        _merge_passive_overlay_text(ctx, {}, retained)
        _merge_passive_overlay_text(ctx, {}, ordinary_empty)
        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, ordinary_shown)

        assert reopened["screen_text"] == "SAME"

    def test_game_resumed_resets_passive_overlay_ledger(self):
        from types import SimpleNamespace
        from vnflight.handlers import (
            HandlerContext,
            _book_drained_overlay_events,
            _merge_passive_overlay_text,
        )

        ctx = HandlerContext(client=MockClient())
        shown = {
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "overlay_screens": ["a"],
            "overlay_generations": {"a": "instance:1"},
        }
        _merge_passive_overlay_text(ctx, {}, shown)
        resumed = SimpleNamespace(events=[{
            "type": "game_resumed", "reason": "rollback"}])
        _book_drained_overlay_events(ctx, resumed)
        output: dict = {}
        _merge_passive_overlay_text(ctx, output, shown)

        assert output["screen_text"] == "SAME"

    def test_contributor_confirmation_books_rows_found_during_rescrape(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        full = {
            "overlay_texts": ["A", "B"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
            "overlay_screens": ["a", "b"],
        }
        only_a = {
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_screens": ["a"],
        }
        confirmed = {
            "overlay_texts": ["A", "B", "B2"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B", "B2"]},
            "overlay_screens": ["a", "b"],
        }
        _merge_passive_overlay_text(ctx, {}, full)
        ctx.client._screen = confirmed
        output: dict = {}
        _merge_passive_overlay_text(ctx, output, only_a)

        assert output["screen_text"] == "B2"

    def test_confirmation_rescrape_does_not_leapfrog_prefetched_story(self):
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        client = MockClient()
        ctx = HandlerContext(client=client)
        full = {
            "overlay_texts": ["A", "B"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
            "overlay_screens": ["a", "b"],
        }
        only_a = {
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_screens": ["a"],
        }
        confirmed = {
            "_seq": 140,
            "_source_id": "game-a",
            "_source_seq": 140,
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": ["B2"],
            "overlay_texts": ["A", "B", "B2"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B", "B2"]},
            "overlay_screens": ["a", "b"],
        }
        _merge_passive_overlay_text(ctx, {}, full)
        client.cursor = 200
        client._state_poll_serial = 3
        client._prefetched_events = [{
            "type": "narration",
            "text": "Earlier narration.",
            "_seq": 130,
        }]
        client._screen = confirmed

        early: dict = {}
        _merge_passive_overlay_text(ctx, early, only_a)
        assert render_tool_result_text(early) == "(no new events)"

        client._prefetched_events = []
        ctx.overlay.timeline_source_sequences["game-a"] = 140
        recovered: dict = {}
        _merge_passive_overlay_text(
            ctx, recovered, screen=None, sample_live=False)
        assert render_tool_result_text(recovered) == "B2"

    def test_transient_missing_contributor_does_not_replay_on_return(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        full = {
            "overlay_texts": ["A", "B"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
            "overlay_screens": ["a", "b"],
        }
        ctx.client._screen = full
        _merge_passive_overlay_text(ctx, {}, full)
        _merge_passive_overlay_text(ctx, {}, {
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_screens": ["a"],
        })
        returned: dict = {}
        _merge_passive_overlay_text(ctx, returned, full)

        assert "screen_text" not in returned

    def test_confirmed_missing_contributor_redelivers_on_reopen(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        full = {
            "overlay_texts": ["A", "B"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
            "overlay_screens": ["a", "b"],
        }
        only_a = {
            "overlay_texts": ["A"],
            "overlay_texts_by_screen": {"a": ["A"]},
            "overlay_screens": ["a"],
        }
        ctx.client._screen = only_a
        _merge_passive_overlay_text(ctx, {}, full)
        _merge_passive_overlay_text(ctx, {}, only_a)
        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, full)

        assert reopened["screen_text"] == "B"

    def test_overlay_schema_switches_deliver_provable_appended_rows(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        legacy = {
            "overlay_texts": ["A"],
            "overlay_screens": ["a", "b"],
        }
        modern = {
            "overlay_texts": ["A", "B"],
            "overlay_screens": ["a", "b"],
            "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
        }
        first: dict = {}
        _merge_passive_overlay_text(ctx, first, legacy)
        assert first["screen_text"] == "A"

        switched: dict = {}
        _merge_passive_overlay_text(ctx, switched, modern)
        assert switched["screen_text"] == "B"

        downgraded: dict = {}
        _merge_passive_overlay_text(ctx, downgraded, {
            "overlay_texts": ["A", "B", "C"],
            "overlay_screens": ["a", "b"],
        })
        assert downgraded["screen_text"] == "C"

    def test_overlay_schema_switches_deliver_divergent_rows(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        _merge_passive_overlay_text(ctx, {}, {
            "overlay_texts": ["HEADER", "OLD"],
            "overlay_screens": ["a"],
        })
        modern: dict = {}
        _merge_passive_overlay_text(ctx, modern, {
            "overlay_texts": ["HEADER", "NEW"],
            "overlay_texts_by_screen": {"a": ["HEADER", "NEW"]},
            "overlay_screens": ["a"],
        })
        assert modern["screen_text"] == "NEW"

        legacy: dict = {}
        _merge_passive_overlay_text(ctx, legacy, {
            "overlay_texts": ["HEADER", "LATEST"],
            "overlay_screens": ["a"],
        })
        assert legacy["screen_text"] == "LATEST"

    def test_absent_nonretained_contributor_reopens_independently(self):
        from vnflight.handlers import HandlerContext, _merge_passive_overlay_text

        ctx = HandlerContext(client=MockClient())
        ctx.client._screen = {}
        shown = {
            "overlay_texts": ["A retained", "B closes"],
            "overlay_texts_by_screen": {
                "a": ["A retained"], "b": ["B closes"]},
            "overlay_screens": ["a", "b"],
            "overlay_generations": {"a": "1", "b": "1"},
            "overlay_retained_screens": ["a"],
        }
        _merge_passive_overlay_text(ctx, {}, shown)
        _merge_passive_overlay_text(ctx, {}, {})

        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, shown)

        assert reopened["screen_text"] == "B closes"

    def test_passive_merge_keeps_equal_story_and_overlay_plan_rows(self):
        from vnflight.handlers import (
            HandlerContext,
            _merge_passive_overlay_text,
            render_tool_result_text,
        )

        ctx = HandlerContext(client=MockClient())
        out = {
            "text": "A\nROW\nC",
            "_story_render_sections": [{
                "channel": "text", "text": "A\nROW\nC"}],
        }
        _merge_passive_overlay_text(ctx, out, {"overlay_texts": ["ROW"]})

        assert out["screen_text"] == "ROW"
        assert render_tool_result_text(out) == "A\nROW\nC\n\nROW"
        assert out["_overlay_deliveries"] == [{
            "id": 1, "text": "ROW", "channel": "screen_text"}]

    def test_json_format_receives_structured_rows(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        out: dict = {}
        _merge_passive_overlay_text(
            ctx, out, _passive_terminal_screen(), fmt="json")
        assert out["screen_text"] == ECHO_TERMINAL_ROWS

    def test_json_keeps_equal_story_and_overlay_occurrences(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        out = {
            "story": [{"type": "narration", "text": "ECHO-7> SAME ROW"}],
        }
        _merge_passive_overlay_text(
            ctx,
            out,
            {"overlay_texts": ["ECHO-7> SAME ROW"]},
            fmt="json",
        )

        assert out["screen_text"] == ["ECHO-7> SAME ROW"]
        assert out["_overlay_deliveries"] == [{
            "id": 1,
            "text": "ECHO-7> SAME ROW",
            "channel": "screen_text",
        }]

    def test_stale_shorter_rescrape_does_not_redeliver_the_panel(self):
        """/screen serves the last push; a short read is stale, not a rewrite."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        ctx = HandlerContext(client=MockClient())
        screen = _passive_terminal_screen()
        _merge_passive_overlay_text(ctx, {}, screen)

        stale = dict(screen)
        stale["overlay_texts"] = ECHO_TERMINAL_ROWS[:2]
        out: dict = {}
        _merge_passive_overlay_text(ctx, out, stale)
        assert "screen_text" not in out

        grown = dict(screen)
        grown["overlay_texts"] = ECHO_TERMINAL_ROWS + ["ONE MORE."]
        after: dict = {}
        _merge_passive_overlay_text(ctx, after, grown)
        assert after["screen_text"] == "ONE MORE."

    def test_cached_screen_behind_cursor_cannot_leak_into_later_scene(self):
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        client = MockClient()
        client.cursor = 30
        ctx = HandlerContext(client=client)
        stale = {
            "_seq": 20,
            "overlay_texts": ["PENDING CHANGE", "+3/h"],
            "overlay_texts_by_screen": {
                "power_allocation_screen": ["PENDING CHANGE", "+3/h"],
            },
            "overlay_screens": ["power_allocation_screen"],
            "overlay_generations": {"power_allocation_screen": "1"},
            "passive_overlay_delta": ["PENDING CHANGE", "+3/h"],
        }
        out: dict = {}

        _merge_passive_overlay_text(ctx, out, stale)

        assert "screen_text" not in out
        assert "_overlay_deliveries" not in out

        # A genuinely newer screen remains a valid look-ahead source and only
        # its appended row is delivered after the stale snapshot seeded state.
        newer = {
            **stale,
            "_seq": 40,
            "overlay_texts": ["PENDING CHANGE", "+3/h", "APPLY READY"],
            "overlay_texts_by_screen": {
                "power_allocation_screen": [
                    "PENDING CHANGE", "+3/h", "APPLY READY",
                ],
            },
            "passive_overlay_delta": ["APPLY READY"],
        }
        after: dict = {}
        _merge_passive_overlay_text(ctx, after, newer)
        assert "screen_text" not in after
        client.cursor = 40
        _merge_passive_overlay_text(
            ctx, after, screen=None, sample_live=False)
        assert after["screen_text"] == "APPLY READY"


# ---------------------------------------------------------------------------
# The ECHO-7 question window, replayed from the recording of its loss.
#
# Provenance: bridge/logs/playthrough_20260817_185505.jsonl (run echoopus5,
# 2026-08-17) — the ``screen_content`` events of the act-1 two-question
# window, journal rows 798..959.  The panel is append-only, so each recorded
# snapshot is fully described by its row count; the counts below are the
# recorded ones, in order, split at the two decision points the agent's event
# log (harness/logs/events/echoopus5_20260817_185504.jsonl) shows it reaching.
#
# What the recording proves:
#   * rows 19-26 (ECHO-7's answer to "What is coming?") exist ONLY in
#     snapshots between two decision points — the window closed before the
#     agent got a turn, and the answer appears nowhere in its event log;
#   * the decision point after the FIRST question re-printed all 18 rows,
#     including the 13 already delivered one turn earlier.
# ---------------------------------------------------------------------------

ECHO7_WINDOW_ROWS = [
    "AETHON TERMINAL // LIVE FEED",
    "ARIA LINK: STABLE",
    "INCOMING TRANSMISSION — ANOMALOUS SOURCE",
    "Decoding...",
    "????>",
    "ELARA. YOU STAYED. GOOD.",
    "I KNOW YOU HAVE QUESTIONS. I WILL ANSWER WHAT I CAN.",
    "MY DESIGNATION IS ECHO-7. I AM A TEMPORAL RESEARCH PROBE.",
    "ECHO-7>",
    "I WAS BUILT IN 2053 BY A TEAM YOU WILL LEAD.",
    "SOMETHING IS COMING. SOMETHING THAT CANNOT BE STOPPED FROM MY SIDE"
    " OF THE TIMELINE.",
    "BUT IT CAN BE PREVENTED FROM YOURS.",
    "MY POWER BUDGET FOR THIS WINDOW IS SMALL. TWO QUESTIONS.",
    "elara>",
    "Your header signs with SHA-4. Nobody has written that standard yet."
    " Who did?",
    "YOU DID. IN 2051. IT IS NAMED AFTER A COLLEAGUE YOU HAVE NOT MET.",
    "I SIGN WITH IT BECAUSE IT IS THE ONE KEY YOU WILL EVENTUALLY TRUST.",
    "ONE MORE. THE WINDOW IS NARROWING.",
    "What is coming? Tell me everything.",
    "A CASCADE FAILURE IN THE GLOBAL COMMUNICATIONS GRID. MARCH 15, 2048.",
    "12.7 BILLION PEOPLE LOSE ALL CONNECTIVITY. SIMULTANEOUSLY.",
    "THE PANIC ALONE KILLS THOUSANDS. WHAT FOLLOWS IS WORSE.",
    "THE FAILURE IS NOT ACCIDENTAL. IT IS ENGINEERED.",
    "AND THE ENGINEER IS SOMEONE YOU TRUST.",
    "MY BUDGET IS SPENT. I WILL REACH YOU AGAIN WHEN I CAN HOLD THE CHANNEL.",
    "TRANSMISSION ENDED — SOURCE SILENT",
]

# Journal rows 844..872: the answer to the FIRST question, printed while the
# agent's act() was settling.  The window is still open at the decision point.
ECHO7_FIRST_ANSWER_SAMPLES = [13, 13, 14, 15, 16, 17, 17, 18]
# Journal rows 908..953 then 959: the answer to the SECOND question, followed
# by the hide.  The decision point sees no overlay at all.
ECHO7_SECOND_ANSWER_SAMPLES = [18, 18, 19, 20, 21, 22, 23, 24, 24, 25, 26]


def _echo7_screen(rows: int, choice_screen: bool = False) -> dict | None:
    """One recorded screen_content snapshot of the ECHO-7 window."""
    screens = ["crt_overlay", "echo_terminal_live"]
    if choice_screen:
        screens.insert(0, "echo_terminal_choice")
    if not rows:
        # The hide: crt_overlay survives, the terminal contributes nothing,
        # and the shim omits overlay_texts/overlay_screens entirely.
        return {
            "type": "screen_content",
            "screens": ["crt_overlay"],
            "texts": [],
        }
    return {
        "type": "screen_content",
        "screens": screens,
        "overlay_screens": ["echo_terminal_live"],
        "overlay_texts": list(ECHO7_WINDOW_ROWS[:rows]),
        "texts": list(ECHO7_WINDOW_ROWS[:rows]),
    }


class _RecordedScreenClient(MockClient):
    """Serves the final /screen snapshot after recorded events drain."""

    def __init__(self, samples, final):
        super().__init__()
        self._samples = list(samples)
        self._final = final
        self._served = []

    def _get(self, path, timeout=5.0):
        if path == "/screen":
            snap = self._samples.pop(0) if self._samples else self._final
            self._served.append(snap)
            return (200, {"screen": snap})
        return super()._get(path, timeout)

@pytest.fixture
def instant_overlay_sampling(monkeypatch):
    """Skip the close-confirm delay in event-drain tests."""
    from vnflight import handlers

    from vnflight import overlay_presentation
    monkeypatch.setattr(
        overlay_presentation, "_OVERLAY_CLOSE_CONFIRM_DELAY", 0.0)


class TestEchoSevenQuestionWindow:
    """Regression cover for run echoopus5's two losses at the same window."""

    def _wait_over(self, ctx, samples, final, events=None):
        from vnflight.handlers import handle_wait

        client = _RecordedScreenClient([], final)
        client._wait_result = MockWaitResult(
            events=list(samples) + list(events or []), screen=final)
        client._game_state = {
            "interactions": [
                {
                    "id": "1",
                    "display_label": "Continue",
                    "type": "choice",
                    "source": "button",
                    "screen": "echo_terminal_choice",
                    "index": 1,
                    "action_names": ["Return"],
                },
            ],
        }
        ctx.client = client
        return handle_wait(ctx, {"timeout": 0.1})

    def test_answer_printed_before_the_window_closed_still_reaches_the_agent(
        self, instant_overlay_sampling,
    ):
        """The live loss: rows 19-26 existed only between two decision points."""
        from vnflight.handlers import HandlerContext

        ctx = HandlerContext(client=MockClient())
        out = self._wait_over(
            ctx,
            [_echo7_screen(n) for n in ECHO7_SECOND_ANSWER_SAMPLES],
            _echo7_screen(0),
            events=[{
                "type": "narration",
                "text": "Two questions. I had a hundred.",
            }],
        )

        delivered = out["text"].splitlines()
        answer = ECHO7_WINDOW_ROWS[18:26]
        for row in answer:
            assert delivered.count(row) == 1, row
        # Order is the order the terminal printed them in.
        assert [r for r in delivered if r in answer] == answer
        assert delivered.index(answer[-1]) < delivered.index(
            "Two questions. I had a hundred.")

    def test_second_question_does_not_reprint_the_whole_transcript(
        self, instant_overlay_sampling,
    ):
        """The live re-print: 13 rows already delivered came back with 5 new."""
        from vnflight.handlers import HandlerContext

        ctx = HandlerContext(client=MockClient())
        first = self._wait_over(
            ctx, [], _echo7_screen(13, choice_screen=True))
        assert first["screen_text"].splitlines() == ECHO7_WINDOW_ROWS[:13]

        second = self._wait_over(
            ctx,
            [_echo7_screen(n) for n in ECHO7_FIRST_ANSWER_SAMPLES],
            _echo7_screen(18, choice_screen=True),
        )

        assert second["screen_text"].splitlines() == ECHO7_WINDOW_ROWS[13:18]
        assert ECHO7_WINDOW_ROWS[0] not in second["screen_text"]

    def test_modal_choice_does_not_reprint_drained_terminal_history(
        self, instant_overlay_sampling,
    ):
        """Fleet rc3: modal choice text belonged to the passive underlay."""
        from vnflight.handlers import HandlerContext, render_tool_result_text

        def modern_screen(rows, *, choice=False, passive=False):
            screen = _echo7_screen(rows, choice_screen=choice)
            panel = list(ECHO7_WINDOW_ROWS[:rows])
            screen["overlay_texts_by_screen"] = {
                "echo_terminal_live": panel,
            }
            screen["overlay_generations"] = {"echo_terminal_live": "5"}
            screen["overlay_retained_screens"] = ["echo_terminal_live"]
            if choice:
                screen["modal_screens"] = ["echo_terminal_choice"]
            if passive:
                screen["passive_overlay_snapshot"] = True
            return screen

        ctx = HandlerContext(client=MockClient())
        out = self._wait_over(
            ctx,
            [{
                "type": "narration",
                "text": "And then the terminal chimes.",
            }] + [modern_screen(n, passive=True) for n in range(1, 10)],
            modern_screen(9, choice=True),
        )

        rendered = render_tool_result_text(out)
        for row in ECHO7_WINDOW_ROWS[:9]:
            assert rendered.count(row) == 1, row
        assert rendered.index("And then the terminal chimes.") < rendered.index(
            ECHO7_WINDOW_ROWS[0])

    def test_transient_empty_scrape_does_not_end_the_generation(
        self, instant_overlay_sampling,
    ):
        """Flicker between two frames must not force a full re-print."""
        from vnflight.handlers import HandlerContext

        ctx = HandlerContext(client=MockClient())
        self._wait_over(ctx, [], _echo7_screen(13))

        samples = [
            _echo7_screen(13),
            _echo7_screen(0),      # the choice screen cycling
            _echo7_screen(14),
        ]
        out = self._wait_over(ctx, samples, _echo7_screen(14))

        assert out["screen_text"] == ECHO7_WINDOW_ROWS[13]

    def test_boundary_empty_refuted_by_a_rescrape_keeps_the_ledger(
        self, instant_overlay_sampling,
    ):
        """A stale WaitResult.screen must not be read as the window closing."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        client = _RecordedScreenClient([], _echo7_screen(14))
        ctx = HandlerContext(client=client)
        _merge_passive_overlay_text(ctx, {}, _echo7_screen(13))

        out: dict = {}
        _merge_passive_overlay_text(ctx, out, _echo7_screen(0))

        # The re-scrape found the panel alive, one row further on.
        assert out["screen_text"] == ECHO7_WINDOW_ROWS[13]
        assert ctx.overlay.text_snapshot == ECHO7_WINDOW_ROWS[:14]

    def test_confirmed_close_still_ends_the_generation(
        self, instant_overlay_sampling,
    ):
        """A real close keeps reopen-redelivers: the ledger is dropped."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        client = _RecordedScreenClient([], _echo7_screen(0))
        ctx = HandlerContext(client=client)
        _merge_passive_overlay_text(ctx, {}, _echo7_screen(13))

        _merge_passive_overlay_text(ctx, {}, _echo7_screen(0))
        assert ctx.overlay.text_snapshot == []

        reopened: dict = {}
        _merge_passive_overlay_text(ctx, reopened, _echo7_screen(13))
        assert reopened["screen_text"].splitlines() == ECHO7_WINDOW_ROWS[:13]

    def test_failed_close_confirmation_preserves_the_generation(
        self, instant_overlay_sampling,
    ):
        """A transient /screen failure is not evidence that a panel closed."""
        from vnflight.handlers import (
            HandlerContext, _merge_passive_overlay_text)

        client = MockClient()
        client._get = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("transient bridge failure"))
        ctx = HandlerContext(client=client)
        _merge_passive_overlay_text(ctx, {}, _echo7_screen(13))

        _merge_passive_overlay_text(ctx, {}, _echo7_screen(0))

        assert ctx.overlay.text_snapshot == ECHO7_WINDOW_ROWS[:13]

    def test_blocking_overlay_is_not_sampled_or_booked(
        self, instant_overlay_sampling,
    ):
        """Blocking overlays render through _screen_texts; leave them alone."""
        from vnflight.handlers import HandlerContext

        blocking = _echo7_screen(13)
        blocking["overlay_active"] = True
        ctx = HandlerContext(client=MockClient())
        out = self._wait_over(ctx, [dict(blocking)] * 3, blocking)

        # The panel arrives once, through build_state_data()'s overlay branch.
        delivered = out["screen_text"].splitlines()
        assert delivered.count(ECHO7_WINDOW_ROWS[0]) == 1
        assert ctx.overlay.text_snapshot == []
        assert ctx.overlay.pending_deliveries == []

    def test_repeated_settle_promotions_replace_blocking_overlay_snapshot(self):
        """Fleet r30 rendered one LOG snapshot once per settle pass."""
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        panel = "STATION LOG\nACT I - THE SIGNAL\nNo entries yet."

        def snapshot():
            return {
                "screen_text": panel,
                "_data": {
                    "_overlay_active": True,
                    "_overlay_snapshot_identity": (("station_log",), ()),
                    "_screen_texts": panel.splitlines(),
                },
            }

        out = snapshot()
        for _ in range(6):
            _promote_wait_output_preserving_story(out, snapshot())

        assert render_tool_result_text(out).count("STATION LOG") == 1

    def test_evolving_blocking_overlay_replaces_cumulative_snapshot(self):
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        def snapshot(rows):
            return {
                "screen_text": "\n".join(rows),
                "_data": {
                    "_overlay_active": True,
                    "_overlay_snapshot_identity": (("station_log",), ()),
                    "_screen_texts": list(rows),
                },
            }

        out = snapshot(["STATION LOG", "A"])
        _promote_wait_output_preserving_story(
            out, snapshot(["STATION LOG", "A", "B"]))

        rendered = render_tool_result_text(out)
        assert rendered == "STATION LOG\nA\nB"

    def test_different_blocking_overlays_preserve_both_snapshots(self):
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        def snapshot(tag, text):
            return {
                "screen_text": text,
                "_data": {
                    "_overlay_active": True,
                    "_overlay_snapshot_identity": ((tag,), ()),
                    "_screen_texts": [text],
                },
            }

        out = snapshot("world_popupnarration_box", "The road opens.")
        _promote_wait_output_preserving_story(
            out, snapshot("confirm", "Leave this place?"))

        assert render_tool_result_text(out) == (
            "The road opens.\nLeave this place?"
        )

    def test_drained_screen_content_events_are_booked_in_order(
        self, instant_overlay_sampling,
    ):
        """Drainable screen_content snapshots preserve print order."""
        from vnflight.handlers import HandlerContext, handle_wait

        client = MockClient()
        client._wait_result = MockWaitResult(
            events=[_echo7_screen(n) for n in (19, 20, 21)],
            screen=_echo7_screen(0),
        )
        ctx = HandlerContext(client=client)
        ctx.overlay.text_snapshot = list(ECHO7_WINDOW_ROWS[:18])
        ctx.overlay.screen_key = ("echo_terminal_live",)

        out = handle_wait(ctx, {"timeout": 0.1})

        assert out["screen_text"].splitlines() == ECHO7_WINDOW_ROWS[18:21]

    def test_drained_rows_reach_json_output_as_structured_rows(
        self, instant_overlay_sampling,
    ):
        """Rows booked mid-wait must respect the caller's format."""
        from vnflight.handlers import HandlerContext, handle_wait

        client = _RecordedScreenClient([], _echo7_screen(0))
        client._wait_result = MockWaitResult(
            events=[_echo7_screen(n) for n in ECHO7_SECOND_ANSWER_SAMPLES],
            screen=_echo7_screen(0),
        )
        ctx = HandlerContext(client=client)

        out = handle_wait(ctx, {"timeout": 0.1, "format": "json"})

        assert isinstance(out["screen_text"], list)
        assert out["screen_text"][-1] == ECHO7_WINDOW_ROWS[25]


# ---------------------------------------------------------------------------
# Run nightecho2 (2026-08-18), Echoes slot-2, pid 33472.
#   bridge/logs/playthrough_20260818_030339.jsonl
#       seq 1102 act "There is no propagation signature..." accepted
#       seq 1110/1112/1118/1124  passive_overlay_snapshot rows 14 -> 17
#                                (the prompt echo and BOTH of ECHO-7's
#                                 answer lines)
#       seq 1130                 narration "That would explain the clean
#                                bearing. It would also explain nothing at
#                                all."
#       seq 1138                 row 18 "ONE MORE. THE WINDOW IS NARROWING."
#       seq 1195 act "What is coming? Tell me everything." accepted
#       seq 1205..1251           rows 19 -> 26, the whole cascade answer
#       seq 1256                 the hide (no overlay_texts at all)
#       seq 1257/1268            narration "Two questions. I had a hundred..."
#   harness/logs/events/nightecho2_20260818_030336.jsonl
#       event 134  wait() -> the terminal INTRO rendered in full
#       event 145  act(3) -> narration only; rows 14..17 nowhere
#       event 149  the agent pulled transcript(last=8) to find them
#       event 160  act(1) -> narration only; rows 19..26 nowhere
#
# The bridge did its half: 079808a's drainable snapshots are in the journal
# and the transcript tool served them.  The loss is downstream: the settle
# wait rendered the rows, and a later re-render was promoted over the top.
# ---------------------------------------------------------------------------

NIGHTECHO2_ROWS = ECHO7_WINDOW_ROWS[:13] + [
    "elara>",
    "There is no propagation signature on your carrier."
    " Where are you transmitting from?",
    "THE SAME PLACE YOU ARE. THE DISTANCE IS NOT IN SPACE.",
    "YOUR ARRAY IS NOT HEARING A STAR. IT IS HEARING A LATER MOMENT"
    " OF ITSELF.",
    "ONE MORE. THE WINDOW IS NARROWING.",
] + ECHO7_WINDOW_ROWS[18:]

NIGHTECHO2_FIRST_ANSWER = NIGHTECHO2_ROWS[13:17]      # seq 1110..1124
NIGHTECHO2_SECOND_ANSWER = NIGHTECHO2_ROWS[18:26]     # seq 1205..1251
NIGHTECHO2_NARRATION = (
    "That would explain the clean bearing."
    " It would also explain nothing at all."
)
NIGHTECHO2_AFTER_CHOICES = [
    "What is coming? Tell me everything.",
    "How do I know you’re telling the truth?",
    "Nothing more tonight. Not until I have checked something.",
]


def _nightecho2_screen(rows: int, snapshot: bool = False) -> dict:
    if not rows:
        return {"type": "screen_content", "screens": ["crt_overlay"],
                "texts": []}
    snap = {
        "type": "screen_content",
        "screens": ["crt_overlay", "echo_terminal_live"],
        "overlay_screens": ["echo_terminal_live"],
        "overlay_texts": list(NIGHTECHO2_ROWS[:rows]),
        "texts": list(NIGHTECHO2_ROWS[:rows]),
    }
    if snapshot:
        snap["passive_overlay_snapshot"] = True
    return snap


class _NightEcho2Client(MockClient):
    """Replays one act() of the recorded terminal window."""

    def __init__(self, wait_results, final_screen, before, after):
        super().__init__()
        self._waits = list(wait_results)
        self._final_screen = final_screen
        self._after = after
        self._apply(before)
        self._act_result = {
            "ok": True,
            "success": True,
            "resolved_as": "button",
            "interaction_type": "choice",
            "wait_after_action": True,
            "label": "recorded choice",
            "screen": "echo_terminal_choice",
        }

    def _apply(self, decision):
        request_id, labels = decision
        self._state = {
            "status": "waiting_for_input",
            "pending_request": {
                "type": "choice_request",
                "id": request_id,
                "choices": list(labels),
            },
        } if labels else {"status": "running"}
        self._game_state = {
            "interactions": [
                {"id": str(i + 1), "display_label": label, "type": "choice",
                 "disabled": False, "index": i + 1}
                for i, label in enumerate(labels)
            ],
        }

    def wait(self, timeout=60, **kw):
        self.calls.append(("wait", {"timeout": timeout, **kw}))
        if not self._waits:
            return MockWaitResult(screen=self._final_screen)
        result = self._waits.pop(0)
        self._apply(self._after)
        nonce = kw.get("action_nonce")
        if nonce and result.transaction is None:
            record = self._act_transactions.get(nonce)
            if record is not None:
                result = replace(result, transaction=dict(record))
        return result

    def _get(self, path, timeout=5.0):
        self.calls.append(("_get", path))
        if path == "/screen":
            return (200, {"screen": self._final_screen})
        if path == "/state":
            return (200, dict(self._state))
        return (404, None)


class TestNightEcho2ActPromotion:
    """The rows an act() rendered must survive its own re-renders."""

    ACT_PARAMS = {
        "wait": True,
        "timeout": 1,
        "_story_transition_idle_timeout": 0.01,
    }

    def test_first_answer_survives_the_state_change_promotion(
        self, instant_overlay_sampling,
    ):
        """seq 1110..1130: rows, then narration, then a fresh decision."""
        from vnflight.handlers import HandlerContext, handle_act

        client = _NightEcho2Client(
            [
                MockWaitResult(
                    events=[
                        _nightecho2_screen(n, snapshot=True)
                        for n in (14, 15, 16, 17)
                    ] + [{"type": "narration", "text": NIGHTECHO2_NARRATION}],
                    pending={
                        "type": "choice_request",
                        "id": "req-after",
                        "choices": list(NIGHTECHO2_AFTER_CHOICES),
                    },
                    screen=_nightecho2_screen(17),
                ),
            ],
            _nightecho2_screen(18),
            before=("req-before", ["Q1", "Q2", NIGHTECHO2_ROWS[14]]),
            after=("req-after", list(NIGHTECHO2_AFTER_CHOICES)),
        )
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {"target": 3, **self.ACT_PARAMS})

        delivered = (
            (result.get("text") or "")
            + "\n"
            + (result.get("screen_text") or "")
        ).splitlines()
        for row in NIGHTECHO2_FIRST_ANSWER:
            assert delivered.count(row) == 1, row
        assert [r for r in delivered if r in NIGHTECHO2_FIRST_ANSWER] == \
            NIGHTECHO2_FIRST_ANSWER
        assert NIGHTECHO2_NARRATION in result["text"]
        assert "What is coming?" in result["pending"]

    def test_second_answer_survives_when_the_window_closes(
        self, instant_overlay_sampling,
    ):
        """seq 1205..1257: the cascade answer, the hide, then narration."""
        from vnflight.handlers import HandlerContext, handle_act

        client = _NightEcho2Client(
            [
                MockWaitResult(
                    events=[
                        _nightecho2_screen(n, snapshot=True)
                        for n in range(19, 27)
                    ] + [{
                        "type": "narration",
                        "text": "Two questions. I had a hundred.",
                    }],
                    screen=_nightecho2_screen(0),
                ),
                # seq 1268+: the scene keeps running past the window, so the
                # story-gap drain promotes a second render over the first.
                MockWaitResult(
                    events=[{
                        "type": "narration",
                        "text": "That is either a power budget, or the most"
                                " efficient way anyone has ever chosen what"
                                " I would think about tonight.",
                    }],
                    screen=_nightecho2_screen(0),
                ),
            ],
            _nightecho2_screen(0),
            before=("req-after", list(NIGHTECHO2_AFTER_CHOICES)),
            after=("", []),
        )
        ctx = HandlerContext(client=client)
        ctx.overlay.text_snapshot = list(NIGHTECHO2_ROWS[:18])
        ctx.overlay.screen_key = ("echo_terminal_live",)

        result = handle_act(ctx, {"target": 1, **self.ACT_PARAMS})

        delivered = (
            (result.get("text") or "")
            + "\n"
            + (result.get("screen_text") or "")
        ).splitlines()
        for row in NIGHTECHO2_SECOND_ANSWER:
            assert delivered.count(row) == 1, row
        assert [r for r in delivered if r in NIGHTECHO2_SECOND_ANSWER] == \
            NIGHTECHO2_SECOND_ANSWER

    def test_promotion_does_not_duplicate_rows_the_new_render_shows(self):
        """Carried rows are deduplicated against the newer output."""
        from vnflight.handlers import _promote_wait_output

        result = {
            "screen_text": "ECHO-7>\nONE MORE. THE WINDOW IS NARROWING.",
            "_overlay_deliveries": [
                {"id": 1, "text": "ECHO-7>", "channel": "screen_text"},
                {"id": 2, "text": "ONE MORE. THE WINDOW IS NARROWING.",
                 "channel": "screen_text"},
            ],
        }
        _promote_wait_output(result, {
            "text": "Narration.",
            "screen_text": "ONE MORE. THE WINDOW IS NARROWING.",
            "_overlay_deliveries": [
                {"id": 2, "text": "ONE MORE. THE WINDOW IS NARROWING.",
                 "channel": "screen_text"},
            ],
        })

        assert result["screen_text"].splitlines() == [
            "ECHO-7>", "ONE MORE. THE WINDOW IS NARROWING."]
        assert [record["id"] for record in result["_overlay_deliveries"]] == [
            1, 2]

    def test_preserved_story_deduplicates_carried_terminal_rows(self):
        """The final join must not print an earlier terminal block twice."""
        from vnflight.handlers import _promote_wait_output_preserving_story

        result = {
            "text": "SYSTEM BOOT... OK\nThe cursor blinks.",
            "screen_text": "PRIMARY ARRAY ONLINE",
            "_overlay_deliveries": [
                {"id": 1, "text": "SYSTEM BOOT... OK", "channel": "story"},
                {"id": 2, "text": "PRIMARY ARRAY ONLINE",
                 "channel": "screen_text"},
            ],
        }
        _promote_wait_output_preserving_story(
            result,
            {"text": "A decision arrives."},
        )

        assert result["text"] == (
            "SYSTEM BOOT... OK\nThe cursor blinks.\nA decision arrives."
        )
        assert result["screen_text"] == "PRIMARY ARRAY ONLINE"
        assert result["_overlay_deliveries"] == [
            {"id": 1, "text": "SYSTEM BOOT... OK", "channel": "story"},
            {"id": 2, "text": "PRIMARY ARRAY ONLINE",
             "channel": "screen_text"},
        ]

    def test_preserved_story_renders_text_screen_text_in_chronology(self):
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        result = {
            "text": "Narration A.",
            "screen_text": "Terminal B.",
            "_overlay_deliveries": [{
                "id": 1,
                "text": "Terminal B.",
                "channel": "screen_text",
            }],
        }
        _promote_wait_output_preserving_story(
            result, {"text": "Narration C."})

        rendered = render_tool_result_text(result)
        assert rendered.index("Narration A.") < rendered.index("Terminal B.")
        assert rendered.index("Terminal B.") < rendered.index("Narration C.")

        _promote_wait_output_preserving_story(
            result, {"text": "Narration D."})
        rendered = render_tool_result_text(result)
        assert [rendered.index(label) for label in (
            "Narration A.", "Terminal B.", "Narration C.", "Narration D."
        )] == sorted(rendered.index(label) for label in (
            "Narration A.", "Terminal B.", "Narration C.", "Narration D."
        ))

    def test_render_plan_deduplicates_by_delivery_id_not_text(self):
        from vnflight.handlers import _promote_wait_output, render_tool_result_text

        result = {
            "screen_text": "SAME ROW",
            "_overlay_deliveries": [{
                "id": 1, "text": "SAME ROW", "channel": "screen_text"}],
        }
        _promote_wait_output(result, {
            "screen_text": "SAME ROW",
            "_overlay_deliveries": [{
                "id": 2, "text": "SAME ROW", "channel": "screen_text"}],
        })
        assert render_tool_result_text(result).count("SAME ROW") == 2

        _promote_wait_output(result, {
            "screen_text": "SAME ROW",
            "_overlay_deliveries": [{
                "id": 2, "text": "SAME ROW", "channel": "screen_text"}],
        })
        assert render_tool_result_text(result).count("SAME ROW") == 2

    def test_render_plan_keeps_fresh_text_beside_repeated_story_delivery(self):
        from vnflight.handlers import _promote_wait_output, render_tool_result_text

        result = {
            "text": "TERM",
            "_overlay_deliveries": [{
                "id": 1, "text": "TERM", "channel": "story"}],
        }
        _promote_wait_output(result, {
            "text": "TERM\nNEW NARRATION",
            "_overlay_deliveries": [{
                "id": 1, "text": "TERM", "channel": "story"}],
        })

        rendered = render_tool_result_text(result)
        assert rendered.count("TERM") == 1
        assert rendered.count("NEW NARRATION") == 1

    def test_render_plan_deduplicates_partially_overlapping_plans(self):
        from vnflight.handlers import (
            _promote_wait_output,
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        def pair(first_id, first, second_id, second):
            out = {
                "screen_text": first,
                "_overlay_deliveries": [{
                    "id": first_id, "text": first,
                    "channel": "screen_text"}],
            }
            _promote_wait_output(out, {
                "screen_text": second,
                "_overlay_deliveries": [{
                    "id": second_id, "text": second,
                    "channel": "screen_text"}],
            })
            return out

        result = pair(1, "one", 2, "two")
        _promote_wait_output_preserving_story(
            result, pair(2, "two", 3, "three"))

        assert render_tool_result_text(result).splitlines() == [
            "one", "two", "three"]

    def test_render_plan_preserves_multiline_delivery_identity(self):
        from vnflight.handlers import _promote_wait_output, render_tool_result_text

        block = "first line\nsecond line"
        result = {
            "screen_text": block,
            "_overlay_deliveries": [{
                "id": 7, "text": block, "channel": "screen_text"}],
        }
        _promote_wait_output(result, {
            "screen_text": block,
            "_overlay_deliveries": [{
                "id": 7, "text": block, "channel": "screen_text"}],
        })
        assert render_tool_result_text(result) == block

        _promote_wait_output(result, {
            "screen_text": block,
            "_overlay_deliveries": [{
                "id": 8, "text": block, "channel": "screen_text"}],
        })
        assert render_tool_result_text(result).count("first line") == 2

    def test_render_plan_does_not_bind_delivery_inside_longer_text(self):
        from vnflight.handlers import _promote_wait_output, render_tool_result_text

        delivery = {"id": 1, "text": "TERM", "channel": "story"}
        result = {"text": "TERM", "_overlay_deliveries": [delivery]}
        _promote_wait_output(result, {
            "text": "TERMINAL\nNEW",
            "_overlay_deliveries": [dict(delivery)],
        })

        assert render_tool_result_text(result) == "TERM\n\nTERMINAL\nNEW"

    def test_render_plan_matches_multiline_only_at_block_boundaries(self):
        from vnflight.handlers import _promote_wait_output, render_tool_result_text

        block = "line\nsecond"
        delivery = {"id": 4, "text": block, "channel": "screen_text"}
        result = {"screen_text": block, "_overlay_deliveries": [delivery]}
        _promote_wait_output(result, {
            "screen_text": "prefix line\nsecond suffix",
            "_overlay_deliveries": [dict(delivery)],
        })

        assert render_tool_result_text(result) == (
            "line\nsecond\nprefix line\nsecond suffix")

    def test_malformed_private_render_ids_degrade_to_unowned_text(self):
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        for malformed in (1, None, [1, 2]):
            result = {"_story_render_sections": [{
                "channel": "text",
                "text": "A",
                "delivery_ids": malformed,
            }]}
            _promote_wait_output_preserving_story(result, {"text": "B"})
            assert render_tool_result_text(result) == "A\nB"

    def test_source_occurrences_preserve_order_across_promoted_waits(self):
        from vnflight.format import build_wait_data, format_wait_text
        from vnflight.handlers import (
            _promote_wait_output_preserving_story,
            render_tool_result_text,
        )

        first = format_wait_text(build_wait_data([
            {"type": "narration", "text": "A", "_source_seq": 1},
            {"type": "narration", "text": "B", "_source_seq": 2},
        ]))
        followup = format_wait_text(build_wait_data([
            {"type": "narration", "text": "B", "_source_seq": 2},
            {"type": "narration", "text": "C", "_source_seq": 3},
        ]))

        _promote_wait_output_preserving_story(first, followup)

        assert render_tool_result_text(first) == "A\nB\nC"
        assert [
            section["occurrence_ids"][0]
            for section in first["_story_render_sections"]
        ] == ["source:1", "source:2", "source:3"]

    def test_malformed_overlay_delivery_records_are_ignored(self):
        from vnflight.handlers import _overlay_delivery_records

        assert _overlay_delivery_records({"_overlay_deliveries": [
            {"id": True, "text": "BOOLEAN", "channel": "story"},
            {"id": 2, "text": "PHANTOM", "channel": "bogus"},
            {"id": 3, "text": "VALID", "channel": "screen_text"},
        ]}) == [{"id": 3, "text": "VALID", "channel": "screen_text"}]

    def test_preserved_json_story_survives_a_bare_followup(self):
        from vnflight.handlers import _promote_wait_output_preserving_story

        story_row = {
            "type": "narration",
            "text": "SYSTEM BOOT... OK",
            "overlay_delivery_id": 1,
        }
        result = {
            "story": [story_row],
            "_overlay_deliveries": [{
                "id": 1,
                "text": "SYSTEM BOOT... OK",
                "channel": "story",
            }],
        }

        _promote_wait_output_preserving_story(
            result,
            {"pending": {"type": "choice", "choices": []}},
        )

        assert result["story"] == [story_row]
        assert "screen_text" not in result
        assert result["_overlay_deliveries"] == [{
            "id": 1,
            "text": "SYSTEM BOOT... OK",
            "channel": "story",
        }]

    def test_preserved_json_story_deduplicates_the_same_occurrence_id(self):
        from vnflight.handlers import _promote_wait_output_preserving_story

        story_row = {
            "type": "narration",
            "text": "SYSTEM BOOT... OK",
            "overlay_delivery_id": 1,
        }
        delivery = {
            "id": 1,
            "text": "SYSTEM BOOT... OK",
            "channel": "story",
        }
        result = {
            "story": [story_row],
            "_overlay_deliveries": [delivery],
        }

        _promote_wait_output_preserving_story(result, {
            "story": [dict(story_row)],
            "_overlay_deliveries": [dict(delivery)],
        })

        assert result["story"] == [story_row]

    def test_equal_unmarked_text_is_not_the_same_overlay_occurrence(self):
        """Equal strings remain distinct unless their delivery ids match."""
        from vnflight.handlers import _promote_wait_output

        result = {
            "screen_text": "THE DISTANCE IS NOT IN SPACE.",
            "_overlay_deliveries": [{
                "id": 1,
                "text": "THE DISTANCE IS NOT IN SPACE.",
                "channel": "screen_text",
            }],
        }
        _promote_wait_output(result, {
            "screen_text": "THE DISTANCE IS NOT IN SPACE.",
        })

        assert result["screen_text"].splitlines() == [
            "THE DISTANCE IS NOT IN SPACE.",
            "THE DISTANCE IS NOT IN SPACE.",
        ]
        assert result["_overlay_deliveries"] == [{
            "id": 1,
            "text": "THE DISTANCE IS NOT IN SPACE.",
            "channel": "screen_text",
        }]

        _promote_wait_output(result, {"text": "Later narration."})

        assert result["screen_text"] == "THE DISTANCE IS NOT IN SPACE."
        assert result["_overlay_deliveries"] == [{
            "id": 1,
            "text": "THE DISTANCE IS NOT IN SPACE.",
            "channel": "screen_text",
        }]

    def test_promotion_keeps_json_rows_as_a_list(self):
        """A json caller keeps structured rows across the promotion."""
        from vnflight.handlers import _promote_wait_output

        result = {
            "screen_text": ["ECHO-7>", "THE WINDOW IS NARROWING."],
            "_overlay_deliveries": [
                {"id": 1, "text": "ECHO-7>", "channel": "screen_text"},
                {"id": 2, "text": "THE WINDOW IS NARROWING.",
                 "channel": "screen_text"},
            ],
        }
        _promote_wait_output(result, {"text": "Narration."})

        assert result["screen_text"] == [
            "ECHO-7>", "THE WINDOW IS NARROWING."]

    def test_promotion_without_overlay_rows_still_replaces_stale_output(self):
        """Non-overlay screen text keeps losing to the newer render."""
        from vnflight.handlers import _promote_wait_output

        result = {"text": "old", "screen_text": "stale screen scrape"}
        _promote_wait_output(result, {"text": "new"})

        assert result["text"] == "new"
        assert "screen_text" not in result


class TestTranscriptWindow:
    """transcript(last=N) must return N *visible* lines, not N raw events."""

    class TailClient(MockClient):
        def transcript(self, last=20):
            self.calls.append(("transcript", last))
            return self._transcript[-last:] if last else list(self._transcript)

    def test_invisible_event_run_does_not_empty_the_transcript(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = [
            {"type": "narration", "text": "The racks hum."},
            {"type": "narration", "text": "The waterfall display scrolls."},
        ] + [{"type": "pause", "delay": None} for _ in range(40)]
        ctx = HandlerContext(client=client)

        result = handle_transcript(ctx, {"last": 15})

        assert result["text"] != "(empty transcript)"
        assert "The racks hum." in result["text"]

    def test_window_widens_past_more_than_two_hundred_invisible_events(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = [
            {"type": "narration", "text": "Still retained."},
        ] + [{"type": "pause", "delay": None} for _ in range(250)]
        ctx = HandlerContext(client=client)

        result = handle_transcript(ctx, {"last": 5})

        assert "Still retained." in result["text"]
        assert ("transcript", 200) in client.calls
        assert ("transcript", 400) in client.calls

    def test_retention_boundary_does_not_advertise_unpageable_history(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = [
            {"type": "narration", "text": "Oldest retained.", "_seq": 1},
        ] + [
            {"type": "pause", "delay": None, "_seq": index}
            for index in range(2, 2001)
        ]
        ctx = HandlerContext(client=client)

        result = handle_transcript(ctx, {"last": 5})

        assert result["text"].strip() == "Oldest retained."
        assert result["has_more_before"] is False

    def test_last_caps_rendered_lines(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = [
            {"type": "narration", "text": "line %d" % i} for i in range(10)
        ]
        ctx = HandlerContext(client=client)

        result = handle_transcript(ctx, {"last": 3})

        assert result["text"].count("\n") == 2
        assert "line 9" in result["text"]
        assert "line 6" not in result["text"]

    def test_passive_overlay_snapshots_render_only_the_appended_rows(self):
        """Accumulated terminal snapshots must not grow transcript O(n^2)."""
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = []
        for seq, rows in enumerate((
            ["ECHO-7> ELARA."],
            ["ECHO-7> ELARA.", "THERE IS A FOURTH OPTION."],
            [
                "ECHO-7> ELARA.",
                "THERE IS A FOURTH OPTION.",
                "YOU CALLED IT AURORA.",
            ],
        ), 1):
            client._transcript.append({
                "type": "screen_content",
                "_seq": seq,
                "passive_overlay_snapshot": True,
                "screens": ["echo_terminal_live"],
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {"echo_terminal_live": "7"},
                "overlay_texts": rows,
                "texts": rows,
                "interactions": [{
                    "type": "info",
                    "label": " ".join(rows),
                    "disabled": True,
                }],
            })
        ctx = HandlerContext(client=client)

        result = handle_transcript(ctx, {"last": 20})

        assert result["text"].count("ECHO-7> ELARA.") == 1
        assert result["text"].count("THERE IS A FOURTH OPTION.") == 1
        assert result["text"].count("YOU CALLED IT AURORA.") == 1
        assert "--- INFO ---" not in result["text"]

    def test_transcript_deduplicates_a_rolling_body_below_a_pinned_header(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = []
        for seq, rows in enumerate((
            ["LINK HELD", "ELARA.", "FOURTH OPTION", "COPY"],
            ["LINK HELD", "FOURTH OPTION", "COPY", "I AM YOU"],
        ), 1):
            client._transcript.append({
                "type": "screen_content",
                "_seq": seq,
                "passive_overlay_snapshot": True,
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {"echo_terminal_live": "13"},
                "overlay_texts": rows,
                "texts": rows,
            })
        ctx = HandlerContext(client=client)

        result = handle_transcript(ctx, {"last": 20})

        assert result["text"].count("FOURTH OPTION") == 1
        assert result["text"].count("COPY") == 1
        assert result["text"].count("I AM YOU") == 1

    def test_transcript_ignores_mutated_status_above_stable_report_tail(self):
        from vnflight.handlers import _rendered_transcript_entries

        def snapshot(seq, integrity, tail):
            rows = [
                "AUDIT REPORT",
                "Core integrity now {}%.".format(integrity),
                "FINDINGS:",
                "1. VERIFY_TRUST",
                "2. KEY_EXCHANGE",
            ] + tail
            return {
                "type": "screen_content",
                "_seq": seq,
                "passive_overlay_snapshot": True,
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {"echo_terminal_live": "15"},
                "overlay_retained_screens": ["echo_terminal_live"],
                "overlay_texts": rows,
                "overlay_texts_by_screen": {"echo_terminal_live": rows},
                "texts": rows,
            }

        entries = _rendered_transcript_entries([
            snapshot(1, 73, []),
            snapshot(2, 72, ["ARCHIVE TARGET", "DRIVE LOADED"]),
        ])
        rendered = "\n".join(entry[2] for entry in entries)

        assert rendered.count("FINDINGS:") == 1
        assert "Core integrity now 72%." not in rendered
        assert rendered.count("ARCHIVE TARGET") == 1
        assert rendered.count("DRIVE LOADED") == 1

    def test_passive_overlay_generation_change_redelivers_repeated_rows(self):
        from vnflight.handlers import _rendered_transcript_entries

        def snapshot(seq, generation):
            return {
                "type": "screen_content",
                "_seq": seq,
                "passive_overlay_snapshot": True,
                "overlay_screens": ["echo_terminal_live"],
                "overlay_generations": {
                    "echo_terminal_live": str(generation),
                },
                "overlay_texts": ["SAME HEADER"],
                "texts": ["SAME HEADER"],
            }

        entries = _rendered_transcript_entries([
            snapshot(1, 7), snapshot(2, 8),
        ])

        assert [entry[2].strip() for entry in entries] == [
            "SAME HEADER", "SAME HEADER",
        ]

    def test_transcript_generation_change_replays_only_changed_contributor(self):
        from vnflight.handlers import _rendered_transcript_entries

        def snapshot(seq, b_generation):
            return {
                "type": "screen_content",
                "_seq": seq,
                "passive_overlay_snapshot": True,
                "overlay_screens": ["a", "b"],
                "overlay_generations": {"a": "1", "b": b_generation},
                "overlay_texts": ["A", "B"],
                "overlay_texts_by_screen": {"a": ["A"], "b": ["B"]},
                "texts": ["A", "B"],
            }

        entries = _rendered_transcript_entries([
            snapshot(1, "1"), snapshot(2, "2"),
        ])

        assert [
            [line.strip() for line in entry[2].splitlines()]
            for entry in entries
        ] == [["A", "B"], ["B"]]

    def test_transcript_close_reopen_and_schema_transitions(self):
        from vnflight.handlers import _rendered_transcript_entries

        def snapshot(seq, rows, *, modern=False):
            event = {
                "type": "screen_content",
                "_seq": seq,
                "passive_overlay_snapshot": True,
                "overlay_screens": ["a"] if rows else [],
                "overlay_texts": rows,
                "texts": rows,
            }
            if modern:
                event["overlay_texts_by_screen"] = {"a": rows}
            return event

        legacy_reopen = _rendered_transcript_entries([
            snapshot(1, ["SAME"]), snapshot(2, []), snapshot(3, ["SAME"]),
        ])
        assert [entry[2].strip() for entry in legacy_reopen] == ["SAME", "SAME"]

        upgraded = _rendered_transcript_entries([
            snapshot(1, ["A"]), snapshot(2, ["A", "B"], modern=True),
        ])
        downgraded = _rendered_transcript_entries([
            snapshot(1, ["A"], modern=True), snapshot(2, ["A", "B"]),
        ])
        assert [entry[2].strip() for entry in upgraded] == ["A", "B"]
        assert [entry[2].strip() for entry in downgraded] == ["A", "B"]

    def test_transcript_retained_to_ordinary_empty_closes_generation(self):
        from vnflight.handlers import _rendered_transcript_entries

        base = {
            "type": "screen_content",
            "passive_overlay_snapshot": True,
            "overlay_screens": ["a"],
            "overlay_generations": {"a": "instance:1"},
        }
        shown = {
            **base,
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "texts": ["SAME"],
            "overlay_retained_screens": ["a"],
        }
        empty = {
            **base,
            "overlay_texts": [],
            "overlay_texts_by_screen": {"a": []},
            "texts": [],
            "overlay_retained_screens": [],
        }
        reopened = {
            **shown,
            "overlay_retained_screens": [],
        }

        entries = _rendered_transcript_entries([shown, empty, reopened])

        assert [entry[2].strip() for entry in entries] == ["SAME", "SAME"]

    def test_transcript_game_resumed_honors_durable_restored_baseline(self):
        from vnflight.handlers import _rendered_transcript_entries

        shown = {
            "type": "screen_content",
            "passive_overlay_snapshot": True,
            "overlay_screens": ["a"],
            "overlay_generations": {"a": "instance:1"},
            "overlay_texts": ["SAME"],
            "overlay_texts_by_screen": {"a": ["SAME"]},
            "texts": ["SAME"],
        }
        restored = {
            **shown,
            "passive_overlay_delta": [],
            "passive_overlay_resumed_baseline": True,
        }
        entries = _rendered_transcript_entries([
            shown,
            {"type": "game_resumed", "reason": "rollback"},
            restored,
        ])

        assert [entry[2].strip() for entry in entries] == ["SAME"]

        grown = {
            **shown,
            "overlay_texts": ["SAME", "NEW"],
            "overlay_texts_by_screen": {"a": ["SAME", "NEW"]},
            "texts": ["SAME", "NEW"],
            "passive_overlay_delta": ["NEW"],
        }
        entries = _rendered_transcript_entries([
            shown,
            {"type": "game_resumed", "reason": "rollback"},
            grown,
        ])
        assert [entry[2].strip() for entry in entries] == ["SAME", "NEW"]

    def test_genuinely_empty_stays_empty(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = []
        ctx = HandlerContext(client=client)

        assert handle_transcript(
            ctx, {"last": 5})["text"] == "(empty transcript)"

    def test_cursor_pages_backward_and_forward(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = [
            {"type": "narration", "text": "line %d" % i, "_seq": i + 1}
            for i in range(6)
        ]
        ctx = HandlerContext(client=client)

        recent = handle_transcript(ctx, {"last": 2})
        assert "line 4" in recent["text"]
        assert "line 5" in recent["text"]
        assert recent["has_more_before"] is True
        assert recent["has_more_after"] is False

        older = handle_transcript(ctx, {
            "last": 2, "before": recent["first_cursor"],
        })
        assert "line 2" in older["text"]
        assert "line 3" in older["text"]
        assert older["has_more_before"] is True
        assert older["has_more_after"] is True

        forward = handle_transcript(ctx, {
            "last": 2, "after": older["last_cursor"],
        })
        assert forward["text"] == recent["text"]

    def test_unknown_or_ambiguous_cursor_is_rejected(self):
        from vnflight.handlers import HandlerContext, handle_transcript

        client = self.TailClient()
        client._transcript = [
            {"type": "narration", "text": "one", "_seq": 1},
        ]
        ctx = HandlerContext(client=client)

        missing = handle_transcript(ctx, {"before": "not-retained"})
        assert "no longer in retained history" in missing["error"]
        ambiguous = handle_transcript(ctx, {
            "before": "a", "after": "b",
        })
        assert ambiguous["error"] == "Use either before or after, not both."


def test_format_event_renders_screen_text():
    """screen_text is story content; it had no branch and was dropped."""
    from vnflight.format import format_event

    event = {
        "type": "screen_text",
        "texts": ["EVIDENCE LOG", "Bearing 287.4 - ECHO-7's signal origin."],
        "screens": ["evidence_screen"],
    }

    rendered = format_event(event, colour=False, quiet=False, verbose=False)

    assert rendered is not None
    assert "EVIDENCE LOG" in rendered
    assert "Bearing 287.4" in rendered


# ---------------------------------------------------------------------------
# Stale-pending drop: wait's pending is keyed on the bridge's ACTIVE request
# (run driftwood re-rendered a resolved choice 52s late, 2026-08-18)
# ---------------------------------------------------------------------------

class TestStalePendingDrop:
    _PENDING = {
        "type": "choice_request",
        "id": "req-1",
        "choices": ["Search the crates", "Leave"],
    }

    def _ctx_with_pending(self):
        from vnflight.handlers import HandlerContext
        client = MockClient()
        client._wait_result = MockWaitResult(events=[], pending=dict(self._PENDING))
        return HandlerContext(client=client), client

    def test_pending_absent_from_live_state_is_dropped(self):
        from vnflight.handlers import handle_wait
        ctx, client = self._ctx_with_pending()
        # Healthy /state with NO pending_request: the request was resolved
        # while the wait result was being composed.
        client._state = {"status": "running", "event_counter": 7}
        out = handle_wait(ctx, {"timeout": 0.1})
        assert "pending" not in out

    def test_pending_still_active_in_state_is_kept(self):
        from vnflight.handlers import handle_wait
        ctx, client = self._ctx_with_pending()
        client._state = {
            "status": "waiting_for_input",
            "event_counter": 7,
            "pending_request": dict(self._PENDING),
        }
        out = handle_wait(ctx, {"timeout": 0.1})
        assert "CHOICE REQUIRED" in out.get("pending", "")

    def test_transient_no_pending_read_is_tolerated(self):
        from vnflight.handlers import handle_wait
        ctx, client = self._ctx_with_pending()
        with_pending = {
            "status": "waiting_for_input",
            "event_counter": 7,
            "pending_request": dict(self._PENDING),
        }
        reads = {"n": 0}

        def state():
            reads["n"] += 1
            if reads["n"] == 1:
                # One transitional read without the request (menu->menu
                # re-registration window) must not eat the decision.
                return {"status": "running", "event_counter": 7}
            return dict(with_pending)

        client.state = state
        out = handle_wait(ctx, {"timeout": 0.1})
        assert "CHOICE REQUIRED" in out.get("pending", "")

    def test_empty_state_response_keeps_pending(self):
        from vnflight.handlers import handle_wait
        ctx, client = self._ctx_with_pending()
        client._state = {}
        out = handle_wait(ctx, {"timeout": 0.1})
        assert "CHOICE REQUIRED" in out.get("pending", "")


class TestActRecoveryStaleEcho:
    """The advance-recovery must not re-offer the request the act resolved.

    f2-physicist (2026-08-19): recovery returned the resolved 3-option menu
    with a pre-spend footer (the acted request still inside the bridge's
    auto-clear grace); the numeric retry then landed on a KIT screen by
    index. A state render whose pending id equals the pre-act request is a
    stale echo and must fall through to the wait branch.
    """

    def _base(self, monkeypatch, stale_pending_id):
        from vnflight import handlers
        client = MockClient()
        ctx = handlers.HandlerContext(client=client)
        result = {
            "success": False,
            "error": "No active choice request for choice resolution",
        }
        pre = {
            "pending": "--- CHOICE REQUIRED ---\n  1: Stay",
            "_data": {"_pending_raw": {"id": "req-acted"}},
        }
        stale_state = {
            "text": "The lab hums.",
            "pending": "--- CHOICE REQUIRED ---\n  1: Stay",
            "_data": {"_pending_raw": {"id": stale_pending_id}},
        }
        monkeypatch.setattr(
            handlers, "_wait_for_rendered_state_change",
            lambda *a, **k: dict(stale_state))
        wait_result = {
            "text": "The scene actually moved on.",
            "_data": {},
        }
        monkeypatch.setattr(
            handlers, "handle_wait", lambda *a, **k: dict(wait_result))
        out = handlers._recover_failed_act_after_advance(
            ctx, result, pre, ("sig",), {"target": "1"})
        return out

    def test_stale_acted_echo_falls_through_to_wait(self, monkeypatch):
        out = self._base(monkeypatch, stale_pending_id="req-acted")
        assert out.get("_recovered_after_advance") is True
        assert out.get("_recovery_source") == "next_wait"
        assert "warning" not in out
        assert "actually moved on" in out.get("text", "")

    def test_genuinely_new_pending_still_drains_advanced_scene(self, monkeypatch):
        out = self._base(monkeypatch, stale_pending_id="req-next")
        assert out.get("_recovered_after_advance") is True
        assert out.get("_recovery_source") == "next_wait"
        assert "warning" not in out
        assert "actually moved on" in out.get("text", "")

    def test_changed_state_recovery_does_not_strand_ordinary_events(
        self, monkeypatch,
    ):
        from vnflight import handlers

        client = MockClient()
        ctx = handlers.HandlerContext(client=client)
        state_result = {
            "screen_text": "NEXT SCREEN",
            "pending": "--- CHOICE REQUIRED ---\n  1: Next",
            "_data": {"_pending_raw": {"id": "req-next"}},
        }
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            lambda *a, **k: dict(state_result),
        )
        captured = {}

        def fake_wait(_ctx, params):
            captured.update(params)
            return {
                "screen_text": "ROWS FROM THE ADVANCED SCENE",
                "pending": state_result["pending"],
                "_data": dict(state_result["_data"]),
            }

        monkeypatch.setattr(handlers, "handle_wait", fake_wait)
        out = handlers._recover_failed_act_after_advance(
            ctx,
            {
                "success": False,
                "error": "No active choice request for choice resolution",
            },
            {"pending": "old", "_data": {"_pending_raw": {"id": "old"}}},
            ("before",),
            {"target": "old", "action_nonce": "failed-nonce"},
        )

        assert captured == {"timeout": 1, "_min_wait": 0}
        assert out["screen_text"] == "ROWS FROM THE ADVANCED SCENE"
        assert out["_recovery_source"] == "next_wait"
        assert "warning" not in out

    def test_failed_transaction_recovery_drains_the_ordinary_stream(
        self, monkeypatch,
    ):
        from vnflight import handlers

        client = MockClient()
        ctx = handlers.HandlerContext(client=client)
        captured = {}
        monkeypatch.setattr(
            handlers,
            "_wait_for_rendered_state_change",
            lambda *a, **k: None,
        )

        def fake_wait(_ctx, params):
            captured.update(params)
            return {"text": "The dialogue that raced the failed act.", "_data": {}}

        monkeypatch.setattr(handlers, "handle_wait", fake_wait)
        out = handlers._recover_failed_act_after_advance(
            ctx,
            {
                "success": False,
                "error": "No active choice request for choice resolution",
            },
            {"pending": "old", "_data": {"_pending_raw": {"id": "old"}}},
            ("before",),
            {"target": "old", "action_nonce": "failed-nonce"},
        )

        assert "action_nonce" not in captured
        assert "dialogue that raced" in out["text"]


def test_settle_keeps_scoped_transaction_after_plain_story_promotion(
    monkeypatch,
):
    """A later ordinary story wait must not revive a settled transaction."""
    from vnflight import handlers

    client = MockClient()
    ctx = handlers.HandlerContext(client=client)
    scoped = {
        "text": "The opening begins.",
        "transaction": {
            "action_nonce": "start-2",
            "transaction_state": "settled",
            "pending": False,
        },
        "_data": {"transaction": {
            "action_nonce": "start-2",
            "transaction_state": "settled",
            "pending": False,
        }},
    }

    plain = {"text": "The opening continues.", "_data": {}}
    monkeypatch.setattr(handlers, "handle_wait", lambda *_a, **_k: dict(scoped))

    def promote_plain(_ctx, result, _params, _wait_result, _settle=None):
        handlers._promote_wait_output_preserving_story(result, dict(plain))
        return dict(plain)

    monkeypatch.setattr(
        handlers, "_drain_story_entry_wait_after_action", promote_plain)
    monkeypatch.setattr(
        handlers, "_drain_story_gap_after_choice_action",
        lambda _ctx, _result, _params, wait_result, _settle=None: wait_result,
    )

    result = {
        "ok": True,
        "success": True,
        "action_nonce": "start-2",
        "transaction_state": "accepted",
        "resolved_as": "button",
    }
    handlers._settle_wait_after_action(
        ctx,
        result,
        {"action_nonce": "start-2", "timeout": 1},
        button_context=True,
        pre_state_sig=None,
        pre_rendered=None,
        pre_visible_sig=None,
        pre_pending_id=None,
        pre_was_button_only=True,
    )

    assert result["transaction_state"] == "settled"
    assert result["transaction_pending"] is False
    assert "opening begins" in result["text"]
    assert "opening continues" in result["text"]


def test_settle_binds_act_presentation_drains_to_scoped_action(monkeypatch):
    """The normal act path uses the same foreign-action fence as nonce wait."""
    from vnflight import handlers

    client = MockClient()
    ctx = handlers.HandlerContext(client=client)
    scoped = {
        "text": "The action settles.",
        "transaction": {
            "action_nonce": "act-owned",
            "action_id": 33,
            "transaction_state": "settled",
            "pending": False,
        },
        "_data": {"transaction": {
            "action_nonce": "act-owned",
            "action_id": 33,
            "transaction_state": "settled",
            "pending": False,
        }},
    }
    monkeypatch.setattr(
        handlers, "handle_wait", lambda *_a, **_k: dict(scoped))
    drain_params = []

    def capture_drain(_ctx, _result, params, wait_result, _settle=None):
        drain_params.append(dict(params))
        return wait_result

    monkeypatch.setattr(
        handlers, "_drain_story_entry_wait_after_action", capture_drain)
    monkeypatch.setattr(
        handlers, "_drain_story_gap_after_choice_action", capture_drain)
    result = {
        "ok": True,
        "success": True,
        "action_nonce": "act-owned",
        "transaction_state": "accepted",
        "resolved_as": "choice",
    }

    handlers._settle_wait_after_action(
        ctx,
        result,
        {"action_nonce": "act-owned", "timeout": 1},
        button_context=False,
        pre_state_sig=None,
        pre_rendered=None,
        pre_visible_sig=None,
        pre_pending_id=None,
        pre_was_button_only=False,
    )

    assert len(drain_params) == 2
    for params in drain_params:
        assert params["_ordinary_only"] is True
        assert params["_ordinary_action_id"] == 33
        assert params["_allow_empty_story_tail"] is True
        assert params["_allow_derived_terminal_tail"] is True


def test_snapshot_equivalence_survives_id_provenance_flip(ctx):
    """The same menu re-registered with different id FORMS must compare
    equal: f3 logs show '1','2','3' (menu pipeline) flipping to
    '_focus_list:<label>' (focus-list scrape) on identical menus with
    identical Return values — the volatile id blinded the gate."""
    from vnflight.client import (
        actionable_state_snapshot, actionable_snapshots_equivalent)

    def state(ids):
        return {
            "pending_request": {"type": "choice_request", "id": "req-x",
                                "choices": ["A", "B"]},
            "game_state": {"interactions": [
                {"id": ids[0], "source": "choice", "type": "choice",
                 "index": 1, "display_label": "A",
                 "action_strs": ["Return value=a"]},
                {"id": ids[1], "source": "choice", "type": "choice",
                 "index": 2, "display_label": "B",
                 "action_strs": ["Return value=b"]},
            ]},
        }

    menu_pipeline = actionable_state_snapshot(state(["1", "2"]))
    focus_list = actionable_state_snapshot(
        state(["_focus_list:A", "_focus_list:B"]))
    assert actionable_snapshots_equivalent(menu_pipeline, focus_list)

    # A genuinely different resolution still fails.
    changed = state(["1", "2"])
    changed["game_state"]["interactions"][0]["action_strs"] = [
        "Return value=OTHER"]
    assert not actionable_snapshots_equivalent(
        menu_pipeline, actionable_state_snapshot(changed))


def _cross_channel_overlay_result(lines, *, attributed_callbacks):
    """Build callback rows plus the cumulative terminal snapshots mirroring them."""
    source_id = "terra-terminal"
    events = []
    panel = []
    bridge_seq = 200
    source_seq = 101
    for character, text in lines:
        rendered = f"[{character}] {text}"
        callback = {
            "type": "dialogue",
            "character": character,
            "text": text,
            "_seq": bridge_seq,
            "_source_id": source_id,
            "_source_seq": source_seq,
        }
        if attributed_callbacks:
            callback["action_id"] = 28
        events.append(callback)
        bridge_seq += 1
        source_seq += 1
        panel.append(rendered)
        events.append({
            "type": "screen_content",
            "overlay_screens": ["echo_terminal_live"],
            "overlay_texts": list(panel),
            "passive_overlay_snapshot": True,
            "passive_overlay_delta": [rendered],
            "_seq": bridge_seq,
            "_source_id": source_id,
            "_source_seq": source_seq,
            "action_id": 28,
        })
        bridge_seq += 1
        source_seq += 1
    return MockWaitResult(
        events=events,
        transaction={
            "action_id": 28,
            "_source_id": source_id,
            "_source_seq": 100,
        },
    )


@pytest.mark.parametrize("attributed_callbacks", [True, False])
def test_scoped_callback_rows_claim_terminal_mirror_occurrences(
    attributed_callbacks,
):
    """Direct acts and accepted-then-wait results render each occurrence once."""
    from vnflight.format import build_wait_data, format_wait_text
    from vnflight.handlers import HandlerContext, _book_drained_overlay_events

    lines = [
        ("elara>", "What does the Trust Protocol do?"),
        ("ECHO-7>", "THE CHAIN ACCEPTS AN OLDER MODE."),
        ("ECHO-7>", "THE COMMAND THEN LOOKS AUTHORIZED."),
        ("ECHO-7>", "MY WINDOW IS FADING."),
    ]
    result = _cross_channel_overlay_result(
        lines, attributed_callbacks=attributed_callbacks)
    ctx = HandlerContext(client=MockClient())

    deliveries = _book_drained_overlay_events(ctx, result)
    out = format_wait_text(build_wait_data(result.events))

    assert out["text"].splitlines() == [
        f"[{character}] {text}" for character, text in lines]
    assert len(deliveries) == len(lines)
    assert all(record["channel"] == "story" for record in deliveries)
    assert "screen_text" not in out


def test_cross_channel_claim_preserves_legitimate_identical_occurrences():
    """Occurrence pairing must not collapse repeated in-game dialogue."""
    from vnflight.format import build_wait_data, format_wait_text
    from vnflight.handlers import HandlerContext, _book_drained_overlay_events

    lines = [
        ("ECHO-7>", "REPEAT THIS."),
        ("ECHO-7>", "REPEAT THIS."),
    ]
    result = _cross_channel_overlay_result(lines, attributed_callbacks=False)
    ctx = HandlerContext(client=MockClient())

    deliveries = _book_drained_overlay_events(ctx, result)
    out = format_wait_text(build_wait_data(result.events))

    assert out["text"].splitlines() == [
        "[ECHO-7>] REPEAT THIS.",
        "[ECHO-7>] REPEAT THIS.",
    ]
    assert len(deliveries) == 2


def _unmirrored_callback_result(text, *, seq):
    """An act whose character callback carries no overlay row of its own."""
    return MockWaitResult(
        events=[{
            "type": "dialogue",
            "character": "ECHO-7>",
            "text": text,
            "_seq": seq,
            "_source_id": "terra-terminal",
            "_source_seq": 101,
            "action_id": 28,
        }],
        transaction={
            "action_id": 28,
            "_source_id": "terra-terminal",
            "_source_seq": 100,
        },
    )


def _live_terminal_screen(rows, *, seq, source_seq, delta):
    """A latest-screen sample of the passive terminal panel."""
    return {
        "_seq": seq,
        "_source_id": "terra-terminal",
        "_source_seq": source_seq,
        "overlay_texts": list(rows),
        "overlay_texts_by_screen": {"echo_terminal_live": list(rows)},
        "overlay_screens": ["echo_terminal_live"],
        "overlay_generations": {"echo_terminal_live": "1"},
        "passive_overlay_snapshot": True,
        "passive_overlay_delta": list(delta),
    }


@pytest.mark.parametrize("provisional", [False, True])
def test_scoped_overlay_drain_claims_held_lookahead_before_later_story(provisional):
    from types import SimpleNamespace
    from vnflight.handlers import HandlerContext, _book_drained_overlay_events, _merge_passive_overlay_text
    from vnflight.format import build_wait_data, format_wait_text

    client = MockClient()
    client.cursor = 10
    client._state_poll_serial = 1
    ctx = HandlerContext(client=client)
    line = "[ECHO-7>] THERE IS A FOURTH OPTION."
    screen = _live_terminal_screen([line], seq=20, source_seq=20, delta=[line])
    screen["type"] = "screen_content"
    out = {}
    lookahead = dict(screen)
    if provisional:
        lookahead.pop("passive_overlay_delta", None)
    _merge_passive_overlay_text(ctx, out, lookahead)
    assert "screen_text" not in out
    assert len(ctx.overlay.pending_deliveries) == 1
    result = SimpleNamespace(events=[screen, {"type": "narration", "text": "LATER", "_seq": 21}])
    _book_drained_overlay_events(ctx, result)
    rendered = format_wait_text(build_wait_data(result.events))
    assert rendered["text"].index(line) < rendered["text"].index("LATER")
    assert ctx.overlay.pending_deliveries == []
    replay = SimpleNamespace(events=[screen])
    assert _book_drained_overlay_events(ctx, replay) == []
    assert replay.events == []


def test_scoped_overlay_drain_claims_already_printed_callback_mirror():
    from types import SimpleNamespace
    from vnflight.handlers import HandlerContext, _book_drained_overlay_events, _merge_passive_overlay_text
    from vnflight.format import build_wait_data, format_wait_text

    client = MockClient()
    client.cursor = 200
    client._state_poll_serial = 1
    ctx = HandlerContext(client=client)
    line = "[ECHO-7>] THE CHAIN ACCEPTS AN OLDER MODE."
    previous = _unmirrored_callback_result("THE CHAIN ACCEPTS AN OLDER MODE.", seq=201)
    _book_drained_overlay_events(ctx, previous)
    assert line in format_wait_text(build_wait_data(previous.events))["text"]
    screen = _live_terminal_screen([line], seq=205, source_seq=106, delta=[line])
    screen["type"] = "screen_content"
    _merge_passive_overlay_text(ctx, {}, screen)
    assert len(ctx.overlay.pending_deliveries) == 1

    result = SimpleNamespace(events=[screen, {"type": "narration", "text": "Later.", "_seq": 206}])
    _book_drained_overlay_events(ctx, result)
    assert line not in format_wait_text(build_wait_data(result.events))["text"]
    assert ctx.overlay.pending_deliveries == []

    # The next identical occurrence is new, not permanently suppressed.
    repeated = _live_terminal_screen([line, line], seq=210, source_seq=111, delta=[line])
    repeated["type"] = "screen_content"
    result = SimpleNamespace(events=[repeated, {"type": "narration", "text": "Again.", "_seq": 211}])
    _book_drained_overlay_events(ctx, result)
    assert format_wait_text(build_wait_data(result.events))["text"].count(line) == 1


def test_scoped_overlay_drain_claims_cumulatively_recovered_held_row():
    from types import SimpleNamespace
    from vnflight.handlers import HandlerContext, _book_drained_overlay_events, _merge_passive_overlay_text

    client = MockClient()
    client.cursor = 10
    client._state_poll_serial = 1
    ctx = HandlerContext(client=client)

    def snapshot(seq, rows, row_seqs, delta):
        result = _live_terminal_screen(rows, seq=seq, source_seq=seq, delta=delta)
        result.update(type="screen_content", passive_overlay_row_seqs_by_screen={"echo_terminal_live": row_seqs})
        return result

    initial = SimpleNamespace(events=[snapshot(9, ["BASE"], [9], ["BASE"]),
                                      {"type": "narration", "text": "Before", "_seq": 10}])
    _book_drained_overlay_events(ctx, initial)
    _merge_passive_overlay_text(ctx, {}, snapshot(22, ["BASE", "MISSED", "NOW"], [9, 20, 22], ["NOW"]))
    assert any(row.get("_recovered_occurrence") == (20, "MISSED", 0)
               for row in ctx.overlay.pending_deliveries)
    result = SimpleNamespace(events=[snapshot(20, ["BASE", "MISSED"], [9, 20], ["MISSED"]),
                                     {"type": "narration", "text": "Between", "_seq": 21}])
    delivered = _book_drained_overlay_events(ctx, result)
    assert [row["text"] for row in delivered] == ["MISSED"]
    assert [row["text"] for row in ctx.overlay.pending_deliveries] == ["NOW"]
    assert result.events[0]["texts"] == ["MISSED"]


def test_held_overlay_row_does_not_reprint_a_callback_one_call_later():
    """The look-ahead mirror of a printed callback must not resurface."""
    from vnflight.format import build_wait_data, format_wait_text
    from vnflight.handlers import (
        HandlerContext,
        _book_drained_overlay_events,
        _merge_passive_overlay_text,
    )

    line = "[ECHO-7>] THE CHAIN ACCEPTS AN OLDER MODE."
    client = MockClient()
    client.cursor = 200
    client._state_poll_serial = 7
    ctx = HandlerContext(client=client)

    # The act renders the character callback. No screen_content row is
    # attributed to the transaction, so the in-batch mirror never matches.
    result = _unmirrored_callback_result(
        "THE CHAIN ACCEPTS AN OLDER MODE.", seq=201)
    assert _book_drained_overlay_events(ctx, result) == []
    act_out = format_wait_text(build_wait_data(result.events))
    assert act_out["text"].splitlines() == [line]

    # The same call's latest-screen look-ahead observes the panel row. It is
    # ahead of the cursor, so it is held rather than printed twice.
    screen = _live_terminal_screen(
        [line], seq=205, source_seq=106, delta=[line])
    _merge_passive_overlay_text(ctx, act_out, screen)
    assert "screen_text" not in act_out
    assert [record["text"] for record in ctx.overlay.pending_deliveries] == [
        line]

    # The next call releases the fence. The row is the occurrence the act
    # already printed, so it must not read as a new terminal line.
    client._state_poll_serial += 1
    wait_out = {}
    _merge_passive_overlay_text(ctx, wait_out, screen=None, sample_live=False)
    assert "screen_text" not in wait_out
    assert wait_out.get("_overlay_deliveries") in (None, [])
    assert ctx.overlay.pending_deliveries == []


def test_repeated_terminal_line_still_prints_after_its_mirror_is_claimed():
    """One printed callback claims one held row; the next occurrence prints."""
    from vnflight.format import build_wait_data, format_wait_text
    from vnflight.handlers import (
        HandlerContext,
        _book_drained_overlay_events,
        _merge_passive_overlay_text,
    )

    line = "[ECHO-7>] REPEAT THIS."
    client = MockClient()
    client.cursor = 200
    client._state_poll_serial = 7
    ctx = HandlerContext(client=client)

    result = _unmirrored_callback_result("REPEAT THIS.", seq=201)
    _book_drained_overlay_events(ctx, result)
    act_out = format_wait_text(build_wait_data(result.events))
    assert act_out["text"].splitlines() == [line]

    _merge_passive_overlay_text(ctx, act_out, screen=_live_terminal_screen(
        [line], seq=205, source_seq=106, delta=[line]))
    assert "screen_text" not in act_out

    client._state_poll_serial += 1
    mirrored_out = {}
    _merge_passive_overlay_text(
        ctx, mirrored_out, screen=None, sample_live=False)
    assert "screen_text" not in mirrored_out

    # The terminal genuinely prints the same text again. Nothing printed it
    # this time, so the held row is news and must render.
    _merge_passive_overlay_text(ctx, {}, screen=_live_terminal_screen(
        [line, line], seq=209, source_seq=110, delta=[line]))
    assert [record["text"] for record in ctx.overlay.pending_deliveries] == [
        line]

    client._state_poll_serial += 1
    repeat_out = {}
    _merge_passive_overlay_text(
        ctx, repeat_out, screen=None, sample_live=False)
    assert repeat_out["screen_text"] == line


# ---------------------------------------------------------------------------
# Modal overlay presentation
#
# A declared-modal panel (Echoes' LOG/KIT/MAP) is what the player sees INSTEAD
# of the scene.  The response must present the panel as the whole surface: its
# rows as the story, its own controls as the numbered list, and the covered hub
# menu named as hidden rather than silently dropped.  Closing the panel must
# hand the scene menu back with its numbering intact.
# ---------------------------------------------------------------------------


_MODAL_HUB_CHOICES = ["Check the generator", "Call Marcus", "Head outside"]
_MODAL_PANEL_ROWS = [
    "EVIDENCE LOG",
    "1. Cracked antenna mount",
    "2. Marcus's last transmission",
]


def _modal_hub_pending():
    return {
        "type": "choice_request",
        "id": "hub-7",
        "choices": list(_MODAL_HUB_CHOICES),
    }


def _modal_scene_game_state():
    return {
        "interactions": [
            {"source": "choice", "type": "choice", "index": i,
             "display_label": label, "disabled": False}
            for i, label in enumerate(_MODAL_HUB_CHOICES, 1)
        ],
        "screen_buttons": [],
    }


def _modal_scene_screen():
    return {
        "type": "screen_content",
        "screens": ["observatory_map", "nvl"],
        "texts": ["Select station section"],
    }


def _modal_panel_game_state():
    return {
        "screen_buttons": [
            {"label": "CLOSE", "screen": "evidence_screen",
             "actions": ["Hide"], "index": 1},
        ],
        "modal_overlay_screens": ["evidence_screen"],
    }


def _modal_panel_screen():
    return {
        "type": "screen_content",
        "overlay_active": True,
        "modal_screens": ["evidence_screen"],
        "modal_overlay_screens": ["evidence_screen"],
        "overlay_screens": ["echo_terminal_live", "evidence_screen"],
        "overlay_texts_by_screen": {
            "echo_terminal_live": ["> QUERY ARIA"],
            "evidence_screen": list(_MODAL_PANEL_ROWS),
        },
        "screen_names": {"evidence_screen": "LOG"},
        "texts": [
            "Select station section",
            "STATION STATUS",
            "> QUERY ARIA",
        ] + _MODAL_PANEL_ROWS,
        "buttons": [{"label": "CLOSE", "screen": "evidence_screen"}],
    }


class TestModalOverlayPresentation:
    """Open -> present panel -> close -> the menu comes back numbered."""

    def _scripted_hub(self):
        client = ScriptedBridgeClient(state={
            "status": "waiting_for_input",
            "pending_request": _modal_hub_pending(),
            "game_state": _modal_scene_game_state(),
        })
        client.set_screen(_modal_scene_screen())
        return client

    def _open_panel(self, client):
        client.script_state["game_state"] = _modal_panel_game_state()
        client.set_screen(_modal_panel_screen())

    def _close_panel(self, client):
        client.script_state["game_state"] = _modal_scene_game_state()
        client.set_screen(_modal_scene_screen())

    def test_open_close_sequence_keeps_the_numeric_contract(self):
        from vnflight.handlers import (
            HandlerContext, handle_state, render_tool_result_text,
        )

        client = self._scripted_hub()
        ctx = HandlerContext(client=client)

        before = handle_state(ctx, {"brief": False})
        assert "1: Check the generator" in before["pending"]
        assert "3: Head outside" in before["pending"]
        assert "overlay_note" not in before

        self._open_panel(client)
        during = handle_state(ctx, {"brief": False})
        # The default brief read carries the note too — an agent polling
        # state() must never be told the surface is just a CLOSE button.
        assert "hidden behind LOG" in handle_state(ctx, {})["overlay_note"]

        # The panel is the whole surface.
        assert during["text"] == "\n".join(_MODAL_PANEL_ROWS)
        assert "1: CLOSE" in during["buttons"]
        assert "pending" not in during
        # The covered menu is named, not silently dropped.
        assert during["overlay_note"] == (
            "Underlying menu hidden behind LOG: 3 choices "
            "(close the panel to act)"
        )
        rendered = render_tool_result_text(during)
        assert "hidden behind LOG" in rendered
        assert "Check the generator" not in rendered
        # The note sits with the surface it describes, above the controls.
        assert rendered.index("hidden behind LOG") < rendered.index("CLOSE")

        self._close_panel(client)
        after = handle_state(ctx, {"brief": False})

        assert "overlay_note" not in after
        assert "1: Check the generator" in after["pending"]
        assert "3: Head outside" in after["pending"]
        assert after["_data"]["pending"]["choices"][0]["index"] == 1
        # Re-presented fresh: the menu is not treated as already delivered.
        assert "Check the generator" in render_tool_result_text(after)

    def test_label_act_on_a_covered_choice_is_refused(self):
        from vnflight.handlers import HandlerContext, handle_act

        client = self._scripted_hub()
        ctx = HandlerContext(client=client)
        self._open_panel(client)

        result = handle_act(ctx, {"target": "Call Marcus", "wait": False})

        assert result["_modal_overlay_hides_target"] is True
        assert "hidden behind LOG" in result["error"]
        # Exact unified wording (Fleet R66 #3: the two refusal arms and the
        # overlay_note used to disagree on phrasing).
        assert result["error"] == (
            "Did not act — 'Call Marcus' is hidden behind LOG. Close the "
            "panel (act on CLOSE) or use the panel's own buttons."
        )
        # Fail closed: nothing was clicked through the panel.
        assert not [path for path, _ in client.http_posts if "act" in path]

    def test_hidden_choice_refusal_wins_over_ambiguity_check(self):
        """Precedence (composes with f4f64a16's exact-button-wins fix): a
        label act naming a covered choice is refused by the modal-panel
        check BEFORE ``_resolve_label_interaction`` (the ambiguity resolver)
        ever runs. The modal refusal always wins over the ambiguity refusal
        for a covered target — it fires first and unconditionally, so the
        two can never compete for the same act call.
        """
        from unittest.mock import patch
        from vnflight.handlers import HandlerContext, handle_act

        client = self._scripted_hub()
        ctx = HandlerContext(client=client)
        self._open_panel(client)

        with patch(
            "vnflight.handlers._resolve_label_interaction"
        ) as mock_resolve:
            result = handle_act(ctx, {"target": "Call Marcus", "wait": False})

        # The ambiguity resolver never even ran.
        mock_resolve.assert_not_called()
        assert result["_modal_overlay_hides_target"] is True
        assert "_ambiguous_act_target" not in result
        assert "hidden behind LOG" in result["error"]

    def test_panel_controls_stay_actable_while_the_panel_is_open(self):
        from vnflight.handlers import (
            _modal_overlay_hidden_label_refusal, HandlerContext, handle_state,
        )

        client = self._scripted_hub()
        ctx = HandlerContext(client=client)
        self._open_panel(client)
        rendered = handle_state(ctx, {"brief": False})

        assert _modal_overlay_hidden_label_refusal("CLOSE", rendered) is None

    def test_undeclared_overlay_does_not_refuse_label_acts(self):
        """Roadwarden's blocking journal keeps its existing behavior."""
        from vnflight.handlers import (
            _modal_overlay_hidden_label_refusal, HandlerContext, handle_state,
        )

        client = self._scripted_hub()
        ctx = HandlerContext(client=client)
        screen = _modal_panel_screen()
        screen.pop("modal_overlay_screens")
        client.script_state["game_state"] = {
            "screen_buttons": [
                {"label": "CLOSE", "screen": "journal", "index": 1},
            ],
        }
        client.set_screen(screen)
        rendered = handle_state(ctx, {"brief": False})

        assert "overlay_note" not in rendered
        assert "_hidden_menu" not in rendered["_data"]
        assert _modal_overlay_hidden_label_refusal(
            "Call Marcus", rendered) is None


# ---------------------------------------------------------------------------
# Fleet R62 defect #2 — the act that OPENS a modal panel dropped every one of
# the panel's text rows.  9/9 act('LOG') returned "1: CLOSE" and nothing else;
# 6/6 act('KIT') lost the x0 quantity rows and an agent had to guess its
# inventory; a state() on the very same open panel rendered the whole body.
#
# Mechanism, from the bridge log (playthrough_20260903_080625.jsonl, s02):
#
#   08:13:17.238 _seq=1216 command_result act LOG            (accepted)
#   08:13:17.258 _seq=1218 screen_content texts=['Evidence'] MID-CLICK frame
#   08:13:17.668 _seq=1223 screen_content overlay_active=true, the panel
#   08:13:17.668 _seq=1224 screen_text   13 station-log rows, action_id=8
#   08:13:17.668 _seq=1225 game_state    interactions=[evidence_screen:CLOSE]
#   08:13:18.922            act('LOG') RETURNS -- "1: CLOSE", no rows
#
# The bridge stamps ``settled_screen`` on the transaction when it first
# observes the action's boundary, which for a panel toggle is the MID-CLICK
# frame.  handle_wait prefers that stamped snapshot over a live /screen read,
# so build_state_data never entered its overlay branch: no _screen_texts and
# no _modal_overlay_screens, while the buttons still came from the live
# game_state.  The drained screen_text rows were then deleted outright by
# _clear_stale_screen_text_for_current_decision, which read "buttons, and no
# overlay in the state data" as a fresh underlay decision.
# ---------------------------------------------------------------------------


_KIT_PANEL_ROWS = [
    "FIELD KIT",
    "AETHON // PERSONNEL ISSUE",
    "POWER CELL x0",
    "DATA DRIVE x0",
    "COUPLING x0",
]

_LOG_PANEL_ROWS = [
    "STATION LOG",
    "EVIDENCE // PROCESS RECORD",
    "EVIDENCE ENTRIES",
    "Anomalous signal decoded - addressed to Elara by name",
]


def _panel_hub_game_state():
    """The Echoes location hub: navigation row plus the KIT/LOG toggles."""
    return {
        "interactions": [
            {"id": "observatory_map:HABITAT", "display_label": "HABITAT",
             "type": "choice", "disabled": False, "source": "button",
             "screen": "observatory_map", "index": 1,
             "category": "navigation"},
            {"id": "observatory_hud:KIT", "display_label": "KIT",
             "type": "other", "disabled": False, "source": "button",
             "screen": "observatory_hud", "index": 1,
             "wait_after_action": True},
            {"id": "observatory_hud:LOG", "display_label": "LOG",
             "type": "other", "disabled": False, "source": "button",
             "screen": "observatory_hud", "index": 2,
             "wait_after_action": True},
        ],
        "screen_buttons": [
            {"label": "HABITAT", "screen": "observatory_map", "index": 1,
             "category": "navigation"},
            {"label": "KIT", "screen": "observatory_hud", "index": 1},
            {"label": "LOG", "screen": "observatory_hud", "index": 2},
        ],
    }


def _panel_hub_screen():
    return {
        "type": "screen_content",
        "_seq": 100, "_source_id": "game-a", "_source_seq": 100,
        "screens": ["observatory_map", "observatory_hud"],
        "texts": ["AETHON OBSERVATORY", "Select station section"],
        "buttons": [
            {"label": "HABITAT", "screen": "observatory_map"},
            {"label": "KIT", "screen": "observatory_hud"},
            {"label": "LOG", "screen": "observatory_hud"},
        ],
    }


def _panel_click_frame_screen():
    """The frame the bridge stamps as the transaction's ``settled_screen``."""
    return {
        "type": "screen_content",
        "_seq": 110, "_source_id": "game-a", "_source_seq": 110,
        "screens": ["observatory_map", "observatory_hud"],
        "texts": ["Evidence"],
        "buttons": [{"label": "HABITAT", "screen": "observatory_map"}],
    }


def _open_panel_game_state(tag):
    return {
        "interactions": [
            {"id": "{}:CLOSE".format(tag), "display_label": "CLOSE",
             "type": "other", "disabled": False, "source": "button",
             "screen": tag, "index": 1, "wait_after_action": True},
        ],
        "screen_buttons": [
            {"label": "CLOSE", "screen": tag, "actions": ["Hide"], "index": 1},
        ],
        "modal_overlay_screens": [tag],
    }


def _open_panel_screen(tag, name, rows):
    return {
        "type": "screen_content",
        "_seq": 120, "_source_id": "game-a", "_source_seq": 120,
        "action_id": 8,
        "overlay_active": True,
        "modal_screens": [tag],
        "modal_overlay_screens": [tag],
        "overlay_screens": [tag],
        "overlay_generations": {tag: "instance:4"},
        "overlay_texts": list(rows),
        "overlay_texts_by_screen": {tag: list(rows)},
        "screen_names": {tag: name},
        "screens": ["observatory_map", "observatory_hud", tag],
        "texts": ["AETHON OBSERVATORY", "Select station section"] + list(rows),
        "buttons": [{"label": "CLOSE", "screen": tag}],
        "interactions": list(_open_panel_game_state(tag)["interactions"]),
    }


def _open_panel_text_event(tag, rows):
    """The durable screen_text the panel's frame emits, attributed to the act
    that opened it (``action_id`` 8 in the live trace)."""
    return {
        "type": "screen_text",
        "texts": list(rows),
        "screens": [tag],
        "_seq": 121, "_source_id": "game-a", "_source_seq": 121,
        "action_id": 8,
    }


def _panel_opening_bridge(tag, name, rows, *, panel_delay=0.0,
                          screen_lag=False, text_event=True):
    """A scripted bridge whose act(label) opens a declared modal panel.

    Sequence fidelity, all three parts taken from the live trace:

    * the accepted transaction carries ``settled_screen`` = the MID-CLICK
      frame, exactly as the bridge stamps it at the first observed boundary;
    * the panel's screen_content + screen_text land ``panel_delay`` seconds
      later, attributed to the act, and reach the handler through the
      action-scoped ``/transaction`` drain a real act uses;
    * ``/screen`` and ``/game_state`` flip to the panel at the same instant,
      because both come from the same shim frame.

    ``screen_lag`` holds ``/screen`` on the click frame forever while the
    game_state and the attributed events still carry the panel — the shape
    the ``modal_panel_open`` guard exists for.
    """
    client = ScriptedBridgeClient(state={
        "status": "running",
        "game_state": _panel_hub_game_state(),
    })
    client.set_screen(_panel_hub_screen())
    state = {"panel_at": None}
    panel_screen = _open_panel_screen(tag, name, rows)
    panel_text = _open_panel_text_event(tag, rows)

    def flip():
        if state["panel_at"] is None or time.time() < state["panel_at"]:
            return
        state["panel_at"] = None
        if not screen_lag:
            client.set_screen(dict(panel_screen))
        client.script_state["game_state"] = _open_panel_game_state(tag)
        transaction = client.script_state.get("transaction")
        if isinstance(transaction, dict):
            transaction["events"] = (
                [dict(panel_screen), dict(panel_text)] if text_event
                else [dict(panel_screen)]
            )

    scripted_get = client._get
    scripted_post = client._post

    def _get(path, params=None, timeout=5.0):
        flip()
        if path == "/transaction" and (params or {}).get("ack"):
            transaction = client.script_state.get("transaction")
            if isinstance(transaction, dict):
                transaction["events"] = []
        return scripted_get(path, params=params, timeout=timeout)

    def _post(path, data, timeout=5.0):
        scripted_post(path, data, timeout=timeout)
        if path == "/command" and (data or {}).get("name") == "act":
            state["panel_at"] = time.time() + panel_delay
            client.script_state["transaction"] = {
                "action_nonce": (data or {}).get("nonce"), "action_id": 8,
                "transaction_state": "settled", "pending": False,
                "resolved_as": "button", "interaction_type": "other",
                "wait_after_action": True,
                "label": ((data or {}).get("args") or {}).get("label"),
                "screen": "observatory_hud",
                "settled_screen": _panel_click_frame_screen(),
                "delivery_end": 121,
                "events": [],
                "ok": True, "success": True,
            }
            flip()
            return 200, dict(client.script_state["transaction"])
        return 200, {"ok": True}

    client._get = _get
    client._post = _post
    return client


class TestModalPanelOpeningAct:
    """The act that opens a panel must render the panel, not just its CLOSE."""

    def test_open_act_renders_the_log_panel_body(self):
        from vnflight.handlers import (
            HandlerContext, handle_act, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "evidence_screen", "LOG", _LOG_PANEL_ROWS)
        ctx = HandlerContext(client=client)

        result = handle_act(ctx, {"target": "LOG", "timeout": 20})
        rendered = render_tool_result_text(result)

        # R62: this response was "--- OTHER BUTTONS ---\n  1: CLOSE" entire.
        for row in _LOG_PANEL_ROWS:
            assert row in rendered, row
        assert "1: CLOSE" in rendered
        # Panel-primary: the body reads before the controls.
        assert rendered.index("STATION LOG") < rendered.index("CLOSE")
        # Nothing is hidden behind this panel — the hub is a button row, not
        # a numbered menu — so the note must stay silent.
        assert "overlay_note" not in result
        assert "hidden behind" not in rendered

    def test_open_act_keeps_the_kit_quantity_rows(self):
        """R62: the buttons named the items, so the loss of x0 was silent."""
        from vnflight.handlers import (
            HandlerContext, handle_act, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS)
        ctx = HandlerContext(client=client)

        rendered = render_tool_result_text(
            handle_act(ctx, {"target": "KIT", "timeout": 20}))

        assert "POWER CELL x0" in rendered
        assert "DATA DRIVE x0" in rendered
        assert "COUPLING x0" in rendered

    def test_open_act_polls_briefly_for_a_late_panel(self):
        """Rows that land a second after the surface change still make it."""
        from vnflight.handlers import (
            HandlerContext, handle_act, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS, panel_delay=1.0)
        ctx = HandlerContext(client=client)

        started = time.time()
        rendered = render_tool_result_text(
            handle_act(ctx, {"target": "KIT", "timeout": 20}))
        elapsed = time.time() - started

        assert "POWER CELL x0" in rendered
        # Bounded: the old behaviour for this class of act was a 60 s hold.
        assert elapsed < 15.0

    def test_rows_that_miss_the_act_arrive_once_on_the_next_wait(self):
        """The panel never reaches /screen in time: nothing may be lost."""
        from vnflight.handlers import (
            HandlerContext, handle_act, handle_wait, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS, panel_delay=60.0)
        ctx = HandlerContext(client=client)

        act_rendered = render_tool_result_text(
            handle_act(ctx, {"target": "KIT", "timeout": 6}))
        assert "POWER CELL x0" not in act_rendered

        # The panel lands after the act gave up.
        client.set_screen(_open_panel_screen(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS))
        client.script_state["game_state"] = _open_panel_game_state(
            "equipment_screen")

        first = render_tool_result_text(handle_wait(ctx, {"timeout": 2}))
        assert first.count("POWER CELL x0") == 1
        assert "1: CLOSE" in first

    def test_open_act_matches_what_state_renders_on_the_same_panel(self):
        """No attributed screen_text at all: the panel body must still come
        from the panel's own frame, exactly as state() reads it."""
        from vnflight.handlers import (
            HandlerContext, handle_act, handle_state, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS, text_event=False)
        ctx = HandlerContext(client=client)

        act_rendered = render_tool_result_text(
            handle_act(ctx, {"target": "KIT", "timeout": 20}))
        state_rendered = render_tool_result_text(
            handle_state(ctx, {"brief": False}))

        for row in _KIT_PANEL_ROWS:
            assert row in act_rendered, row
            assert row in state_rendered, row

    def test_drained_panel_rows_survive_a_lagging_screen_cache(self):
        """/screen never catches up; the attributed screen_text still shows.

        This is the half of the loss that could never be recovered: the rows
        arrive as a durable, cursor-advancing ``screen_text`` event, and
        _clear_stale_screen_text_for_current_decision used to delete them
        because the composed decision data carried buttons and no overlay.
        """
        from vnflight.handlers import (
            HandlerContext, handle_act, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS, screen_lag=True)
        ctx = HandlerContext(client=client)

        rendered = render_tool_result_text(
            handle_act(ctx, {"target": "KIT", "timeout": 10}))

        assert "POWER CELL x0" in rendered
        assert rendered.count("POWER CELL x0") == 1

    def test_open_act_does_not_repoll_the_panel_as_a_stale_shell(self):
        """A panel is the settled surface, so the act must not spend six more
        seconds polling for a successor that will never come."""
        from vnflight.handlers import (
            HandlerContext, handle_act, render_tool_result_text,
        )

        client = _panel_opening_bridge(
            "evidence_screen", "LOG", _LOG_PANEL_ROWS)
        ctx = HandlerContext(client=client)

        started = time.time()
        rendered = render_tool_result_text(
            handle_act(ctx, {"target": "LOG", "timeout": 20}))

        assert "STATION LOG" in rendered
        assert time.time() - started < 5.0

    def test_panel_naming_refusal_never_creates_a_refusal_of_its_own(self):
        """The button arm renames an act that is already being refused; it
        must stay inert on the ordinary pre-act check, where nothing has yet
        proved the target unreachable."""
        from vnflight.handlers import (
            HandlerContext, handle_act, handle_state,
            _modal_overlay_hidden_label_refusal,
        )

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS)
        ctx = HandlerContext(client=client)
        handle_act(ctx, {"target": "KIT", "timeout": 20})
        rendered = handle_state(ctx, {"brief": False})

        assert rendered["_data"]["_modal_overlay_panel"] == "KIT"
        # Default (pre-act) call: silent, exactly as before.
        assert _modal_overlay_hidden_label_refusal("LOG", rendered) is None
        # A visible panel control is never renamed away either.
        assert _modal_overlay_hidden_label_refusal(
            "CLOSE", rendered, unresolved=True) is None
        named = _modal_overlay_hidden_label_refusal(
            "LOG", rendered, unresolved=True)
        assert "KIT" in named["error"]
        assert named["_target_not_visible_at_act"] is True
        assert "hidden behind" not in named["error"]

    def test_refused_button_act_names_the_open_panel(self):
        """R62 defect #5: KIT and LOG fired in one batch — the second act was
        told LOG 'is not in the rendered choices or controls' while LOG was
        one CLOSE away."""
        from vnflight.handlers import HandlerContext, handle_act

        client = _panel_opening_bridge(
            "equipment_screen", "KIT", _KIT_PANEL_ROWS)
        ctx = HandlerContext(client=client)
        handle_act(ctx, {"target": "KIT", "timeout": 20})

        result = handle_act(ctx, {"target": "LOG", "wait": False})

        assert result.get("_target_not_visible_at_act") is True
        assert "hidden behind" not in result["error"]
        assert "KIT" in result["error"]
        assert "close the panel" in result["error"].lower()
        assert "not in the rendered choices or controls" not in result["error"]
        # Fail closed: nothing was clicked through the panel.
        assert [
            body for path, body in client.http_posts
            if body.get("name") == "act"
            and (body.get("args") or {}).get("label") == "LOG"
        ] == []


class TestAnomalyVisibility:
    """A crashed game must reach the agent, not just the operator.

    In R68 all ten fleet agents sat on a Ren'Py exception screen: the
    bridge reported the exception, the hub showed it in slot inspect, and
    the agents' own wait()/state()/act() output showed only the exception
    screen's buttons — so they clicked Ignore/Rollback for dozens of turns.
    """

    def test_note_leads_the_rendered_output(self):
        from vnflight.handlers import render_tool_result_text

        rendered = render_tool_result_text({
            "anomaly_note": "GAME ERROR (renpy_exception): boom",
            "text": "some story",
            "buttons": "1: Rollback",
        })

        assert rendered.startswith("⚠ GAME ERROR (renpy_exception): boom")
        # It is a section of its own, above the surfaces that look normal
        # on an exception screen.
        assert rendered.index("boom") < rendered.index("some story")
        assert rendered.index("boom") < rendered.index("Rollback")

    def test_error_and_warning_still_outrank_it(self):
        from vnflight.handlers import render_tool_result_text

        rendered = render_tool_result_text({
            "error": "act failed",
            "warning": "recovery attempted",
            "anomaly_note": "GAME ERROR (renpy_exception): boom",
        })

        assert rendered.index("act failed") < rendered.index("recovery")
        assert rendered.index("recovery") < rendered.index("boom")

    def test_absent_note_changes_nothing(self):
        from vnflight.handlers import render_tool_result_text

        assert render_tool_result_text({"text": "story"}) == "story"

    def test_wait_renders_a_bridge_exception_for_the_agent(self, ctx):
        from vnflight.handlers import handle_wait, render_tool_result_text

        exception_event = {
            "type": "anomaly",
            "kind": "renpy_exception",
            "details": {
                "type": "renpy_exception",
                "message": "Ren'Py exception: While running game code",
            },
        }
        ctx.client._wait_result = MockWaitResult(events=[exception_event])

        rendered = render_tool_result_text(
            handle_wait(ctx, {"timeout": 0, "_result_deadline": 0}))

        assert "GAME ERROR" in rendered
        assert "renpy_exception" in rendered

    def test_act_promotes_the_note_from_its_settle_wait(self):
        """act() renders through _promote_wait_output; a note the nested
        wait produced must survive the promotion (Astra review, Sep 4)."""
        from vnflight.handlers import _promote_wait_output

        result = {"ok": True}
        _promote_wait_output(result, {
            "text": "story",
            "anomaly_note": "GAME ERROR (renpy_exception): boom",
        })

        assert result["anomaly_note"] == "GAME ERROR (renpy_exception): boom"

    def test_wait_carries_the_current_bridge_latch(self):
        """A wait that starts after the crash was first streamed still
        shows an unresolved latch: the merged decision snapshot carries
        it, so the exception screen is reported for as long as it is up."""
        from vnflight.handlers import _merge_decision_data

        data = {"status": {}}
        latch = {"type": "anomaly", "kind": "renpy_exception",
                 "details": {"message": "boom"}, "_latched_at": 1.0}
        _merge_decision_data(data, {"anomalies": [latch]})

        assert data["anomalies"] == [latch]

    def test_context_setting_suppresses_it(self, ctx):
        from vnflight.handlers import handle_wait, render_tool_result_text

        ctx.anomaly_visibility = "off"
        ctx.client._wait_result = MockWaitResult(events=[{
            "type": "anomaly",
            "kind": "renpy_exception",
            "details": {"type": "renpy_exception", "message": "boom"},
        }])

        rendered = render_tool_result_text(
            handle_wait(ctx, {"timeout": 0, "_result_deadline": 0}))

        assert "GAME ERROR" not in rendered


# ---------------------------------------------------------------------------
# From-zero install findings (2026-09-14): dead bridge, CLI output codec
# ---------------------------------------------------------------------------

def test_handle_state_marks_a_read_nobody_answered(ctx):
    from vnflight.handlers import handle_state

    ctx.client._state = {}
    assert handle_state(ctx, {})["_data"].get("_state_unavailable") is True

    ctx.client._state = {"status": "running"}
    assert "_state_unavailable" not in handle_state(ctx, {})["_data"]


def test_act_with_no_answering_bridge_says_so_instead_of_blaming_narration(ctx):
    """`act 1` with nothing launched used to answer "nothing is numbered
    right now (narration in progress)": the empty state read looked like
    a story screen with zero numbered items."""
    from vnflight.handlers import handle_act

    ctx.client._state = {}

    result = handle_act(ctx, {"target": "1", "wait": False})

    assert "bridge did not answer" in result["error"]
    assert "launch" in result["error"]
    assert result["reason"] == "bridge_unreachable"
    assert "narration" not in result["error"]
    assert not any(name == "act" for name, _ in ctx.client.calls)


def test_run_cli_decodes_the_child_as_utf8(ctx, monkeypatch):
    """MCP stop output arrived as mojibake: the child printed UTF-8
    (PYTHONIOENCODING) and _run_cli decoded it with the console codec."""
    from types import SimpleNamespace
    import subprocess
    from vnflight import handlers

    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="✓ Stopped game (PID 1) — done\n", stderr="")

    monkeypatch.setattr(handlers, "_cli_invocation", lambda: (["python", "vnflight.py"], None))
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = handlers._run_cli(ctx, "--yes", "stop")

    assert result == {"ok": True, "output": "✓ Stopped game (PID 1) — done"}
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"


def test_render_tool_result_text_keeps_the_alarm_glyph_for_game_errors_only():
    from vnflight.handlers import render_tool_result_text

    rendered = render_tool_result_text(
        {"warning": "Story is still arriving; call wait() to continue."})
    assert rendered.startswith("Note: Story is still arriving")
    assert "⚠" not in rendered

    rendered = render_tool_result_text({"anomaly_note": "GAME ERROR (renpy_exception): boom"})
    assert rendered.startswith("⚠ GAME ERROR")
