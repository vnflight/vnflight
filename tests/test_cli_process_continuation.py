"""Continuation survives real CLI process exits against a real bridge."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from vnflight import bridge


def test_retained_terminal_reopen_does_not_replay_across_cli_processes(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    manager = bridge.SlotManager(admin_token="test-only", require_token=False)
    monkeypatch.setattr(bridge, "slots", manager)
    slot = manager.assign("terminal-test")
    game = manager.get(slot)
    server = bridge.ThreadedHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    server.verbose = False
    server.allowed_hosts = set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, VNFLIGHT_DATA_DIR=str(tmp_path / "client"),
               PYTHONPATH=str(root / "src"), PYTHONIOENCODING="utf-8")
    base = [sys.executable, "-c", "from vnflight.cli import main; raise SystemExit(main())",
            "--bridge", "http://127.0.0.1:" + str(server.server_port), "--slot", str(slot)]

    def run(*args):
        result = subprocess.run(base + list(args), env=env, cwd=tmp_path,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def panel(rows, visible=True, generation="one", empty_texts=False):
        choices = (game.get_pending_request() or {}).get("choices", [])
        game.push_event({
            "type": "screen_content", "texts": rows if visible and not empty_texts else [],
            "screens": ["terminal"] if visible else [],
            "buttons": [{"label": label, "actions": ["ChoiceReturn"],
                         "action_strs": ["ChoiceReturn"], "screen": "choice"}
                        for label in choices],
            "interactions": [{"id": "choice:" + str(index), "type": "choice",
                              "display_label": label, "disabled": False,
                              "index": index, "screen": "choice"}
                             for index, label in enumerate(choices, 1)],
            "overlay_texts": rows if visible else [], "overlay_active": False,
            "overlay_screens": ["terminal"] if visible else [],
            "overlay_texts_by_screen": {"terminal": rows} if visible else {},
            "overlay_generations": {"terminal": generation},
            "overlay_retained_screens": ["terminal"],
        })

    try:
        # Establish a persisted zero cursor before story arrives.
        run("wait", "--timeout", "2")
        old = "ARCHIVED TRANSMISSION ROW"
        panel([old])
        assert run("wait", "--timeout", "2").count(old) == 1
        panel([], visible=False)
        for number in range(25):
            game.push_event({"type": "narration", "text": "Interlude " + str(number)})
        run("wait", "--timeout", "2")
        game.set_pending_request({"type": "choice_request", "id": "identity",
                                  "choices": ["Decide independently"]})
        panel([old])
        reopened = run("wait", "--timeout", "2")
        assert old not in reopened
        assert "Decide independently" in reopened
        # With the event cursor caught up, wait renders the live pending menu
        # through its latest-screen fallback instead of the event-batch path.
        pending_again = run("wait", "--timeout", "2")
        assert old not in pending_again
        assert "Decide independently" in pending_again
        panel([old, old])
        assert run("wait", "--timeout", "2").count(old) == 1
        panel([old], generation="two")
        assert run("wait", "--timeout", "2").count(old) == 1
        # A closing panel can retain a fresh passive row while its ordinary
        # screen scrape is already empty (the AURORA receiving-status case).
        fresh = "RECEIVING NEW SPECIFICATIONS"
        panel([old, fresh], generation="two", empty_texts=True)
        assert run("wait", "--timeout", "2").count(fresh) == 1
        assert fresh not in run("wait", "--timeout", "2")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("partial_act", [False, True])
def test_act_then_fresh_explicit_wait_preserves_intervening_story(
    monkeypatch, tmp_path, json_mode, partial_act,
):
    monkeypatch.chdir(tmp_path)
    manager = bridge.SlotManager(admin_token="test-only", require_token=False)
    monkeypatch.setattr(bridge, "slots", manager)
    slot = manager.assign("continuation-test")
    game = manager.get(slot)
    game.push_event({"type": "game_started", "game": "continuation-test"})
    game.set_pending_request({
        "type": "choice_request", "id": "first", "choices": ["Go"],
    })
    server = bridge.ThreadedHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    server.verbose = False
    server.allowed_hosts = set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, VNFLIGHT_DATA_DIR=str(tmp_path / "client"),
               PYTHONPATH=str(root / "src"), PYTHONIOENCODING="utf-8")
    base = [sys.executable, "-c", "from vnflight.cli import main; raise SystemExit(main())",
            "--bridge", "http://127.0.0.1:" + str(server.server_port),
            "--slot", str(slot)]
    if json_mode:
        base.append("--json")

    def run(*args):
        result = subprocess.run(base + list(args), env=env, cwd=tmp_path,
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        def resolve(command):
            assert command and command["name"] == "act"
            game.push_event({"type": "command_result", "command": "act",
                             "nonce": command["nonce"], "success": True,
                             "resolved_as": "choice", "label": "Go"})

        if partial_act:
            def respond():
                for _ in range(500):
                    command = game.consume_command()
                    if command:
                        # The story row lands BEFORE the command result.  The
                        # earlier order (result, then dialogue) raced the act's
                        # settle window: when the CLI read the result with no
                        # story behind it yet, it correctly answered "accepted,
                        # still settling" and the dialogue reached the next
                        # wait instead, which this test does not model.  A
                        # real shim also publishes the row before it confirms
                        # the action that produced it.
                        game.push_event({"type": "dialogue", "character": "Elara",
                                         "text": "What is it?"})
                        resolve(command)
                        return
                    time.sleep(0.01)

            responder = threading.Thread(target=respond, daemon=True)
            responder.start()
            first = run("act", "1", "--timeout", "5")
            responder.join(timeout=10)
            assert not responder.is_alive()
            # The product contract (act_settle.py): an act returns within its
            # budget whenever story is still arriving, rendering whatever
            # has landed, or hands back ("still settling; call wait") and the
            # next wait continues from the same cursor.  Both are correct;
            # what must never happen is the row showing twice or not at all.
            shown_by_act = "What is it?" in first
            if not shown_by_act:
                assert "still settling" in first, first
        else:
            run("act", "1", "--no-wait", "--timeout", "5")
            resolve(game.consume_command())
            shown_by_act = True   # nothing was pushed for act 1 to show
        # These arrive after process 1 exited, so they cannot be in its stash.
        game.push_event({"type": "dialogue", "character": "Marcus",
                         "text": "The grant was extended by eighteen months."})
        game.push_event({"type": "narration", "text": "He folds the wrapper."})
        game.set_pending_request({
            "type": "choice_request", "id": "second", "choices": ["Ask about Geneva"],
        })
        output = run("wait", "--timeout", "2")
        assert output.count("The grant was extended by eighteen months.") == 1
        assert output.count("He folds the wrapper.") == 1
        assert output.count("What is it?") == (0 if shown_by_act else 1)
        assert "Ask about Geneva" in output
        repeated = run("wait", "--timeout", "2")
        assert "The grant was extended" not in repeated
        assert "He folds the wrapper" not in repeated
        assert "Ask about Geneva" in repeated
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_wait_reads_through_a_quick_menu_and_a_later_wait_shows_late_rows(
    monkeypatch, tmp_path,
):
    """Two release-facing facts, seen live on Mystic Cafe's opening:

    1. A surface whose only buttons are the stock quick menu (Q.Load is a
       FileLoad from QuickLoad()) is not a decision: `wait` keeps reading
       until its timeout instead of returning at once.
    2. Rows that land after a wait has returned are rendered, once, by the
       next wait: nothing an earlier wait did not print is ever skipped.
    """
    monkeypatch.chdir(tmp_path)
    manager = bridge.SlotManager(admin_token="test-only", require_token=False)
    monkeypatch.setattr(bridge, "slots", manager)
    slot = manager.assign("quick-menu-test")
    game = manager.get(slot)
    game.push_event({"type": "game_started", "game": "quick-menu-test"})
    game.push_event({"type": "context", "context": "in_game",
                     "available_commands": ["save", "load", "quit"]})
    game.push_event({
        "type": "screen_content", "screens": ["say", "quick_menu"],
        "texts": [], "buttons": [
            {"label": "Q.Save", "screen": "_focus_list",
             "actions": ["FileTakeScreenshot", "FileSave"], "index": 6},
            {"label": "Q.Load", "screen": "_focus_list",
             "actions": ["FileLoad"], "index": 7},
        ],
    })
    server = bridge.ThreadedHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    server.verbose = False
    server.allowed_hosts = set()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, VNFLIGHT_DATA_DIR=str(tmp_path / "client"),
               PYTHONPATH=str(root / "src"), PYTHONIOENCODING="utf-8")
    base = [sys.executable, "-c", "from vnflight.cli import main; raise SystemExit(main())",
            "--bridge", "http://127.0.0.1:" + str(server.server_port),
            "--slot", str(slot)]

    def run(*args):
        result = subprocess.run(base + list(args), env=env, cwd=tmp_path,
                                capture_output=True, text=True, encoding="utf-8", timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        started = time.time()
        first = run("wait", "--timeout", "3")
        elapsed = time.time() - started
        # Not a decision: the wait ran to its timeout (process start-up
        # adds to the floor, never subtracts from it).
        assert elapsed >= 2.5, (elapsed, first)
        assert "Q.Load" not in first or "NAVIGATION" in first

        game.push_event({"type": "dialogue", "character": "Narrator",
                         "text": "The city felt different tonight."})
        game.push_event({"type": "dialogue", "character": "Mira",
                         "text": "I really should have left the office earlier..."})
        second = run("wait", "--timeout", "2")
        assert second.count("The city felt different tonight.") == 1
        assert second.count("I really should have left the office earlier...") == 1

        third = run("wait", "--timeout", "1")
        assert "The city felt different tonight." not in third
        assert "office earlier" not in third
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
