"""Fleet regression: independent bridge startups must not rewrite shared history."""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

import pytest

from vnflight import bridge


def test_same_second_playthrough_logs_are_exclusive_and_namespaced(tmp_path, monkeypatch):
    class FrozenDatetime:
        @staticmethod
        def now():
            return datetime(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr(bridge, "datetime", FrozenDatetime)
    # Several slots share each bridge namespace, but never a transcript file.
    def write(slot):
        directory = tmp_path / str(9600 + slot % 2)
        gs = bridge.GameState(storage_dir=str(directory))
        for seq in range(5):
            gs.push_event({"type": "dialogue", "text": "caf\u00e9", "owner": slot, "row": seq})
        path = Path(gs._log_path)
        gs._close_log()
        return slot, directory, path
    with ThreadPoolExecutor(max_workers=15) as pool:
        results = list(pool.map(write, range(15)))
    assert len({path for _, _, path in results}) == 15
    for slot, directory, path in results:
        assert path.parent == directory
        assert path.name.startswith("playthrough_20260905_120000_")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [row["row"] for row in rows] == list(range(5))
        assert all(row["owner"] == slot and row["text"] == "caf\u00e9" for row in rows)


def test_playthrough_rotation_never_reopens_previous_file(tmp_path, monkeypatch):
    class FrozenDatetime:
        @staticmethod
        def now():
            return datetime(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr(bridge, "datetime", FrozenDatetime)
    gs = bridge.GameState(storage_dir=str(tmp_path))
    gs._MAX_LOG_BYTES = 1
    for seq in range(3):
        gs.push_event({"type": "dialogue", "text": str(seq)})
    gs._close_log()
    logs = list(tmp_path.glob("playthrough_*.jsonl"))
    assert len(logs) == 3
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in logs]
    assert sorted(row["text"] for row in rows) == ["0", "1", "2"]


def archived(manager, game="example", nonce="pending"):
    sid = manager.assign(game, game_pid=12345)
    gs = manager.get(sid)
    gs.submit_command_with_ack({
        "name": "act", "args": {"index": 1}, "nonce": nonce,
        "reset_generation": 0,
    })
    assert manager.free(sid)
    return sid, Path(gs._transaction_log_path)


def test_independent_storage_and_restart_recovery(tmp_path):
    first = bridge.SlotManager(storage_dir=str(tmp_path / "9601"))
    sid, journal = archived(first)
    before = journal.read_bytes()
    second = bridge.SlotManager(storage_dir=str(tmp_path / "9602"))
    assert second.get_transaction_archive(sid) is None
    other_id, other_journal = archived(second, nonce="other")
    assert journal != other_journal
    restored = bridge.SlotManager(storage_dir=str(tmp_path / "9601"))
    assert restored.get_transaction_archive(sid).get_action_transaction("pending")
    assert restored.get_transaction_archive(sid).get_action_transaction("other") is None
    assert journal.read_bytes() == before


def test_archive_loading_does_not_rewrite_large_journal(tmp_path, monkeypatch):
    manager = bridge.SlotManager(storage_dir=str(tmp_path))
    _, journal = archived(manager)
    before = journal.read_bytes()
    index = Path(manager._transaction_archive_path)
    index_before = index.read_bytes()
    monkeypatch.setattr(bridge.GameState, "_MAX_TRANSACTION_JOURNAL_BYTES", 1)
    def forbidden(*args):
        pytest.fail("recovery must not rewrite historical storage")
    monkeypatch.setattr(bridge.GameState, "_maybe_rewrite_transaction_journal", forbidden)
    bridge.SlotManager(storage_dir=str(tmp_path))
    assert journal.read_bytes() == before
    assert index.read_bytes() == index_before


def test_corrupt_archive_warns_preserves_and_does_not_block_healthy_archive(tmp_path, capsys):
    manager = bridge.SlotManager(storage_dir=str(tmp_path))
    bad_id, bad = archived(manager, "bad")
    good_id, good = archived(manager, "good")
    bad.write_bytes(bad.read_bytes() + b"\x80\x9d\n")
    damaged = bad.read_bytes()
    restored = bridge.SlotManager(storage_dir=str(tmp_path))
    assert restored.get_transaction_archive(bad_id) is None
    assert restored.get_transaction_archive(good_id) is not None
    assert "Archive unavailable" in capsys.readouterr().err
    assert bad.read_bytes() == damaged
    # Retention cleanup must not destroy the evidence either.
    restored._delete_archived_journals([str(bad)])
    assert bad.read_bytes() == damaged


def test_corrupt_active_identity_refuses_partial_nonce_recovery(tmp_path):
    gs = bridge.GameState(storage_dir=str(tmp_path))
    gs.configure_identity(1, "bad", 42)
    Path(gs._transaction_log_path).write_bytes(b"\x80\n")
    with pytest.raises(bridge.TransactionJournalError, match="Invalid transaction journal"):
        bridge.GameState(storage_dir=str(tmp_path)).configure_identity(1, "bad", 42)


@pytest.mark.parametrize("payload", [b"[]\n", b"{broken\n", b'{"journal_type":"act_transaction_meta","reset_generation":"oops"}\n'])
def test_invalid_journal_schema_is_unavailable_not_partial(tmp_path, payload, capsys):
    manager = bridge.SlotManager(storage_dir=str(tmp_path))
    sid, journal = archived(manager)
    journal.write_bytes(journal.read_bytes() + payload)
    before = journal.read_bytes()
    recovered = bridge.SlotManager(storage_dir=str(tmp_path))
    assert recovered.get_transaction_archive(sid) is None
    assert journal.read_bytes() == before
    assert "Archive unavailable" in capsys.readouterr().err


def test_index_cannot_import_foreign_journal(tmp_path, capsys):
    foreign = bridge.SlotManager(storage_dir=str(tmp_path / "foreign"))
    sid, journal = archived(foreign)
    own = tmp_path / "own"
    own.mkdir()
    (own / "transaction_archives.jsonl").write_bytes(Path(foreign._transaction_archive_path).read_bytes())
    before = journal.read_bytes()
    manager = bridge.SlotManager(storage_dir=str(own))
    assert manager.get_transaction_archive(sid) is None
    assert "outside owned storage" in capsys.readouterr().err
    assert journal.read_bytes() == before


@pytest.mark.parametrize("bad_row", [b"\x80\n", b'{"slot_id":1e999,"timestamp":0}\n'])
def test_corrupt_index_row_does_not_block_valid_rows(tmp_path, capsys, bad_row):
    manager = bridge.SlotManager(storage_dir=str(tmp_path))
    sid, _ = archived(manager)
    index = Path(manager._transaction_archive_path)
    index.write_bytes(bad_row + index.read_bytes())
    before = index.read_bytes()
    recovered = bridge.SlotManager(storage_dir=str(tmp_path))
    assert recovered.get_transaction_archive(sid) is not None
    assert "Invalid archive index row" in capsys.readouterr().err
    assert index.read_bytes() == before


def test_corrupt_index_does_not_crash_free_or_get_overwritten(tmp_path, monkeypatch, capsys):
    index = tmp_path / "transaction_archives.jsonl"
    index.write_bytes(b"\x80\n")
    manager = bridge.SlotManager(storage_dir=str(tmp_path))
    monkeypatch.setattr(manager, "_MAX_TRANSACTION_ARCHIVES", 1)
    archived(manager, "first")
    sid, journal = archived(manager, "second")
    assert manager.get(sid) is None
    assert journal.exists()
    assert index.read_bytes().startswith(b"\x80\n")
    assert "compaction refused" in capsys.readouterr().err


def test_server_claims_endpoint_before_storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    order = []
    class Server:
        server_address = ("127.0.0.1", 12345)
        def __init__(self, *args):
            order.append("bound")
        def serve_forever(self):
            order.append("served")
    real_manager = bridge.SlotManager
    def manager(**kwargs):
        assert order == ["bound"]
        assert kwargs["storage_dir"] == str(tmp_path / "bridge/logs/endpoints/127.0.0.1-12345")
        order.append("storage")
        return real_manager(**kwargs)
    monkeypatch.setattr(bridge, "slots", bridge.slots)
    monkeypatch.setattr(bridge, "ThreadedHTTPServer", Server)
    monkeypatch.setattr(bridge, "SlotManager", manager)
    monkeypatch.setattr(real_manager, "start_pid_watchdog", lambda *args, **kwargs: None)
    bridge.run_bridge_server(port=12345, verbose=False)
    assert order == ["bound", "storage", "served"]


def test_failed_bind_never_loads_storage(monkeypatch):
    def occupied(*args):
        raise OSError("address in use")
    def forbidden(**kwargs):
        pytest.fail("failed bind must not touch storage")
    monkeypatch.setattr(bridge, "ThreadedHTTPServer", occupied)
    monkeypatch.setattr(bridge, "SlotManager", forbidden)
    with pytest.raises(OSError, match="address in use"):
        bridge.run_bridge_server()


def test_actual_endpoint_refuses_second_owner():
    server = bridge.ThreadedHTTPServer(("127.0.0.1", 0), bridge.BridgeHandler)
    try:
        with pytest.raises(OSError):
            duplicate = bridge.ThreadedHTTPServer(server.server_address, bridge.BridgeHandler)
            duplicate.server_close()
    finally:
        server.server_close()


def test_fifteen_bundled_startups_ignore_legacy_corruption(tmp_path):
    """Exercise import AND server startup in separate processes sharing a cwd."""
    artifact = Path(__file__).resolve().parents[1] / "vnflight.py"
    legacy = tmp_path / "bridge/logs"
    legacy.mkdir(parents=True)
    journal = legacy / "transactions_old_42.jsonl"
    journal.write_bytes(b"\x80\x9d\n")
    index = legacy / "transaction_archives.jsonl"
    index.write_text(json.dumps({
        "slot_id": 1, "game_id": "old", "journal_path": str(journal),
    }) + "\n", encoding="utf-8")
    before = (journal.read_bytes(), index.read_bytes())
    script = """
import json, runpy, sys
ns = runpy.run_path(sys.argv[1])
ns['SlotManager'].start_pid_watchdog = lambda *args, **kwargs: None
def served(server):
    owner = ns['run_bridge_server'].__globals__['slots']
    owner.assign('same_game', game_pid=42)
    print('STORAGE=' + owner._storage_dir, flush=True)
ns['ThreadedHTTPServer'].serve_forever = served
ns['run_bridge_server'](port=0, verbose=False)
"""
    def launch(_):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(artifact)], cwd=tmp_path,
            capture_output=True, text=True, timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        return next(line.removeprefix("STORAGE=") for line in completed.stdout.splitlines()
                    if line.startswith("STORAGE="))
    with ThreadPoolExecutor(max_workers=15) as pool:
        directories = list(pool.map(launch, range(15)))
    assert len(set(directories)) == 15
    assert all(str(legacy / "endpoints") in path for path in directories)
    assert (journal.read_bytes(), index.read_bytes()) == before
