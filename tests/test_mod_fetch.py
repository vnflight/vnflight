import hashlib
import io
import json
import sys

import pytest

from vnflight import mod_fetch
from vnflight.lib import resolve_game_mods


def snapshot(monkeypatch, mod=None, data=b"init python:\n    pass\n"):
    mod = mod or {"file": "sample.rpy", "target": "vnf_sample.rpy",
                  "sha256": hashlib.sha256(data).hexdigest()}
    raw = json.dumps({"manifest_version": 1,
                      "license": {"file": "LICENSE", "sha256": hashlib.sha256(data).hexdigest()},
                      "games": {"sample": {"mods": [mod]}}}).encode()
    calls = []
    def download(url, deadline, limit):
        calls.append(url)
        return raw if url.endswith("manifest.json") else data
    monkeypatch.setattr(mod_fetch, "_mods_download", download)
    return hashlib.sha256(raw).hexdigest(), calls


def test_fetch_snapshot_resolves_with_existing_installer(monkeypatch, tmp_path):
    digest, calls = snapshot(monkeypatch)
    dest = tmp_path / "snapshot"
    path = mod_fetch.fetch_mods_manifest("https://example.org/repo/manifest.json", digest, dest)
    result = resolve_game_mods({"mods_manifest": str(path)}, "sample", {}, tmp_path)
    assert not result.problems
    assert len(result.entries) == 1
    assert calls == ["https://example.org/repo/manifest.json", "https://example.org/repo/LICENSE",
                     "https://example.org/repo/sample.rpy"]
    assert (dest / "LICENSE").exists()


def test_manifest_hash_failure_downloads_no_code(monkeypatch, tmp_path):
    _, calls = snapshot(monkeypatch)
    with pytest.raises(ValueError, match="Manifest SHA"):
        mod_fetch.fetch_mods_manifest("https://example.org/manifest.json", "0" * 64, tmp_path / "out")
    assert len(calls) == 1
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("field,value", [
    ("file", "../escape.rpy"), ("file", "C:escape.rpy"),
    ("file", "https://evil.org/x.rpy"), ("file", "x.rpy?query"),
    ("target", "../vnf_escape.rpy"), ("target", "script.rpy"),
    ("sha256", None), ("sha256", "short"),
])
def test_invalid_entry_never_downloads_code(monkeypatch, tmp_path, field, value):
    mod = {"file": "sample.rpy", "target": "vnf_sample.rpy", "sha256": "0" * 64}
    mod[field] = value
    digest, calls = snapshot(monkeypatch, mod)
    with pytest.raises(ValueError):
        mod_fetch.fetch_mods_manifest("https://example.org/manifest.json", digest, tmp_path / "out")
    assert len(calls) == 1
    assert not (tmp_path / "out").exists()


def test_bad_adapter_hash_cleans_staging(monkeypatch, tmp_path):
    digest, _ = snapshot(monkeypatch, {"file": "sample.rpy", "target": "vnf_sample.rpy",
                                     "sha256": "0" * 64})
    with pytest.raises(ValueError, match="Adapter SHA"):
        mod_fetch.fetch_mods_manifest("https://example.org/manifest.json", digest, tmp_path / "out")
    assert list(tmp_path.iterdir()) == []


def test_existing_destination_untouched(monkeypatch, tmp_path):
    digest, calls = snapshot(monkeypatch)
    with pytest.raises(ValueError, match="already exists"):
        mod_fetch.fetch_mods_manifest("https://example.org/manifest.json", digest, tmp_path)
    assert not calls


@pytest.mark.parametrize("url", ["http://example.org/manifest.json", "file:///manifest.json",
                                "https://user:secret@example.org/manifest.json"])
def test_non_https_or_credentials_refused(monkeypatch, tmp_path, url):
    digest, calls = snapshot(monkeypatch)
    with pytest.raises(ValueError):
        mod_fetch.fetch_mods_manifest(url, digest, tmp_path / "out")
    assert not calls


def test_redirect_downgrade_refused():
    with pytest.raises(ValueError, match="HTTPS"):
        mod_fetch.ModsHTTPSOnly().redirect_request(None, None, 302, "", {}, "http://example.org")


def test_real_reader_enforces_limit(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(b"12345")
    monkeypatch.setattr(mod_fetch.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="size limit"):
        mod_fetch._mods_download("https://example.org", mod_fetch.time.monotonic() + 10, 4)


def test_expired_download_has_no_network(monkeypatch):
    def forbidden(*args):
        pytest.fail("Network called after deadline")
    monkeypatch.setattr(mod_fetch.urllib.request, "build_opener", forbidden)
    with pytest.raises(TimeoutError):
        mod_fetch._mods_download("https://example.org", 0, 4)


def test_built_artifact_fetches_verified_snapshot(monkeypatch, tmp_path):
    import runpy
    from pathlib import Path
    digest, _ = snapshot(monkeypatch)
    artifact = Path(__file__).resolve().parents[1] / "vnflight.py"
    namespace = runpy.run_path(str(artifact), run_name="vnflight_fetch_test")
    fetch = namespace["fetch_mods_manifest"]
    monkeypatch.setitem(fetch.__globals__, "_mods_download", mod_fetch._mods_download)
    path = fetch("https://example.org/manifest.json", digest, tmp_path / "built")
    assert path.is_file()
    assert (path.parent / "LICENSE").is_file()


def test_cli_fetch_output(monkeypatch, tmp_path, capsys):
    from vnflight import cli
    digest, _ = snapshot(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["vnflight", "fetch-mods", "https://example.org/manifest.json",
                                     "--sha256", digest, "--output", str(tmp_path / "out")])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["mods_manifest"].endswith("manifest.json")
