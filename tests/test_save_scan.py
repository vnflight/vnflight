import os
import sys
import json
import zipfile
from argparse import Namespace

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from vnflight import cli
from vnflight.handlers import HandlerContext, handle_save_scan
from vnflight import save_scan


def test_scan_save_file_finds_vnflight_tokens_in_zip_member(tmp_path):
    save = tmp_path / "slot-LT1.save"
    with zipfile.ZipFile(save, "w") as zf:
        zf.writestr("json", "{}")
        zf.writestr("log", b"store.vnf_player references vnflight runtime")

    result = save_scan.scan_save_file(save)

    assert result["risk"] == "possible_shim_reference"
    assert {m["pattern"] for m in result["matches"]} >= {"vnflight", "vnf_"}
    assert {m["category"] for m in result["matches"]} == {"current"}
    assert result["categories"] == ["current"]
    assert {m["entry"] for m in result["matches"]} == {"log"}


def test_scan_save_file_reports_clean_zip_save(tmp_path):
    save = tmp_path / "clean-LT1.save"
    with zipfile.ZipFile(save, "w") as zf:
        zf.writestr("json", '{"_save_name": "Clean"}')
        zf.writestr("log", b"store.player_health")

    result = save_scan.scan_save_file(save)

    assert result["risk"] == "none"
    assert result["matches"] == []


def test_scan_save_file_finds_raw_save_tokens(tmp_path):
    save = tmp_path / "raw.save"
    save.write_bytes(b"pickle-ish data with llm_player state")

    result = save_scan.scan_save_file(save)

    assert result["risk"] == "possible_shim_reference"
    assert any(m["pattern"] == "llm_" for m in result["matches"])
    assert "legacy" in result["categories"]


def test_save_scan_summary_counts_categories_per_save(tmp_path):
    current = tmp_path / "current.save"
    legacy = tmp_path / "legacy.save"
    mixed = tmp_path / "mixed.save"
    current.write_bytes(b"vnflight and vnf_player")
    legacy.write_bytes(b"llm_player old state")
    mixed.write_bytes(b"vnf_player plus vnharness")

    results = [save_scan.scan_save_file(p) for p in [current, legacy, mixed]]
    summary = save_scan.summarize_save_scan_results(results)

    assert summary["count"] == 3
    assert summary["possible_shim_coupled"] == 3
    assert summary["current_vnflight"] == 2
    assert summary["legacy_llm"] == 1
    assert summary["harness"] == 1
    assert summary["other"] == 0


def test_scan_save_file_skips_oversized_raw_saves(tmp_path, monkeypatch):
    save = tmp_path / "large.save"
    save.write_bytes(b"llm_player state hidden behind a raw size limit")
    monkeypatch.setattr(save_scan, "SAVE_SCAN_RAW_LIMIT", 8)

    result = save_scan.scan_save_file(save)

    assert result["risk"] == "none"
    assert result["matches"] == []
    assert "skipped <raw>" in result["warnings"][0]


def test_cmd_save_scan_prints_warning_summary(tmp_path, capsys):
    save = tmp_path / "slot-LT1.save"
    with zipfile.ZipFile(save, "w") as zf:
        zf.writestr("log", b"store.vnf_player")

    args = Namespace(path=str(tmp_path), recursive=False, json=False)

    assert cli.cmd_save_scan(args, object()) == 0
    out = capsys.readouterr().out
    assert "Scanned 1 save file(s)" in out
    assert "Possible shim-coupled saves: 1" in out
    assert "current vnflight/vnf=1" in out
    assert "slot-LT1.save" in out
    assert "vnf_ (current)" in out


def test_cmd_save_scan_json_includes_category_summary(tmp_path, capsys):
    current = tmp_path / "current.save"
    legacy = tmp_path / "legacy.save"
    current.write_bytes(b"vnflight and vnf_player")
    legacy.write_bytes(b"llm_player old state")

    args = Namespace(path=str(tmp_path), recursive=False, json=True)

    assert cli.cmd_save_scan(args, object()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 2
    assert data["possible_shim_coupled"] == 2
    assert data["errors"] == 0
    assert data["current_vnflight"] == 1
    assert data["legacy_llm"] == 1
    assert data["harness"] == 0
    assert data["other"] == 0
    assert len(data["results"]) == 2


def test_handle_save_scan_reuses_scanner_for_mcp(tmp_path):
    save = tmp_path / "slot-LT1.save"
    with zipfile.ZipFile(save, "w") as zf:
        zf.writestr("log", b"store.vnf_player")

    result = handle_save_scan(
        HandlerContext(client=object()),
        {"path": str(tmp_path)},
    )

    assert result["count"] == 1
    assert result["possible_shim_coupled"] == 1
    assert result["current_vnflight"] == 1
    assert result["legacy_llm"] == 0
    assert result["errors"] == 0


def test_handle_save_scan_ignores_non_save_files_for_mcp(tmp_path):
    secret = tmp_path / "notes.txt"
    secret.write_text("contains llm_player but is not a Ren'Py save")

    result = handle_save_scan(
        HandlerContext(client=object()),
        {"path": str(secret)},
    )

    assert result["count"] == 0
    assert result["possible_shim_coupled"] == 0
    assert result["results"] == []


def test_iter_save_files_recursive_flag(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    root_save = tmp_path / "root.save"
    nested_save = nested / "nested.save"
    root_save.write_bytes(b"")
    nested_save.write_bytes(b"")

    assert save_scan.iter_save_files(tmp_path, recursive=False) == [root_save]
    assert save_scan.iter_save_files(tmp_path, recursive=True) == [nested_save, root_save]


def test_iter_save_files_ignores_non_save_file_paths(tmp_path):
    note = tmp_path / "note.txt"
    upper = tmp_path / "slot.SAVE"
    note.write_bytes(b"llm_player")
    upper.write_bytes(b"")

    assert save_scan.iter_save_files(note, recursive=False) == []
    assert save_scan.iter_save_files(tmp_path, recursive=False) == [upper]
