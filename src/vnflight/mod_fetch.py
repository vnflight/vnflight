"""Explicit, hash-pinned adapter downloads; never invoked by observation tools."""

import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request


class ModsHTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Adapter downloads cannot redirect away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _mods_download(url, deadline, limit):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Adapter downloads require HTTPS")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Adapter download budget expired")
    opener = urllib.request.build_opener(ModsHTTPSOnly())
    with opener.open(url, timeout=min(10, remaining)) as response:
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Adapter download budget expired")
            chunk = response.read1(min(65536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("Adapter download exceeds size limit")


def _mods_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("A full SHA-256 digest is required")
    return value.lower()


def fetch_mods_manifest(url, expected_sha256, destination):
    """Fetch a complete verified snapshot into a new directory, without installing."""
    expected_sha256 = _mods_digest(expected_sha256)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTPS manifest URL without embedded credentials")
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError("Destination already exists; choose a new snapshot directory")
    deadline = time.monotonic() + 60
    raw = _mods_download(url, deadline, 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Manifest SHA-256 mismatch; no adapters downloaded")
    manifest = json.loads(raw.decode("utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("manifest_version") != 1
            or not isinstance(manifest.get("games"), dict)):
        raise ValueError("Unsupported or malformed adapter manifest")
    license_entry = manifest.get("license")
    if not isinstance(license_entry, dict) or license_entry.get("file") != "LICENSE":
        raise ValueError("Remote manifest must include a hash-pinned LICENSE")
    files = {"LICENSE": _mods_digest(license_entry.get("sha256"))}
    for entry in manifest["games"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("mods"), list):
            raise ValueError("Malformed adapter list")
        for mod in entry["mods"]:
            if not isinstance(mod, dict):
                raise ValueError("Malformed adapter entry")
            name, target = mod.get("file"), mod.get("target")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.rpy", name):
                raise ValueError("Adapter file must be a plain .rpy filename")
            if not isinstance(target, str) or not re.fullmatch(r"vnf_[A-Za-z0-9_-]+\.rpy", target):
                raise ValueError("Adapter target must be a plain vnf_*.rpy filename")
            digest = _mods_digest(mod.get("sha256"))
            if name in files and files[name] != digest:
                raise ValueError("Conflicting hashes for adapter " + name)
            files[name] = digest
    if len(files) > 128:
        raise ValueError("Too many adapters in manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vnflight-mods-", dir=destination.parent) as tmp:
        stage = Path(tmp) / "snapshot"
        stage.mkdir()
        total = len(raw)
        for name, digest in files.items():
            data = _mods_download(urllib.parse.urljoin(url, name), deadline, 4 * 1024 * 1024)
            total += len(data)
            if total > 32 * 1024 * 1024:
                raise ValueError("Adapter snapshot exceeds size limit")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Adapter SHA-256 mismatch: " + name)
            (stage / name).write_bytes(data)
        (stage / "manifest.json").write_bytes(raw)
        # Renaming a complete directory prevents exposing a partial snapshot.
        if destination.exists():
            raise ValueError("Destination appeared during download")
        stage.rename(destination)
    return destination / "manifest.json"
