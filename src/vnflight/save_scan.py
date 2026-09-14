"""Read-only Ren'Py save scanner for likely vnflight shim references."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

SAVE_SCAN_PATTERNS = (
    "vnflight",
    "VNFLIGHT",
    "vnf_",
    "llm_",
    "llm_player",
    "vnharness",
)
SAVE_SCAN_ENTRY_LIMIT = 16 * 1024 * 1024
SAVE_SCAN_RAW_LIMIT = SAVE_SCAN_ENTRY_LIMIT
SAVE_SCAN_PATTERN_CATEGORIES = {
    "vnflight": "current",
    "VNFLIGHT": "current",
    "vnf_": "current",
    "llm_": "legacy",
    "llm_player": "legacy",
    "vnharness": "harness",
}


def classify_save_scan_pattern(pattern: str) -> str:
    return SAVE_SCAN_PATTERN_CATEGORIES.get(pattern, "other")


def printable_snippet(blob: bytes, pos: int, needle_len: int) -> str:
    start = max(0, pos - 48)
    end = min(len(blob), pos + needle_len + 48)
    snippet = blob[start:end].decode("utf-8", errors="replace")
    return "".join(ch if ch.isprintable() else "." for ch in snippet)


def scan_save_bytes(
    blob: bytes,
    *,
    entry: str,
    patterns: tuple[str, ...] = SAVE_SCAN_PATTERNS,
    max_snippets: int = 3,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for pattern in patterns:
        needle = pattern.encode("utf-8")
        positions: list[int] = []
        pos = blob.find(needle)
        while pos >= 0:
            positions.append(pos)
            pos = blob.find(needle, pos + len(needle))
        if positions:
            matches.append({
                "entry": entry,
                "pattern": pattern,
                "category": classify_save_scan_pattern(pattern),
                "count": len(positions),
                "snippets": [
                    printable_snippet(blob, p, len(needle))
                    for p in positions[:max_snippets]
                ],
            })
    return matches


def scan_save_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "matches": [],
        "warnings": [],
        "error": None,
    }
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.file_size > SAVE_SCAN_ENTRY_LIMIT:
                        result["warnings"].append(
                            f"skipped {info.filename}: {info.file_size} bytes"
                        )
                        continue
                    data = zf.read(info)
                    result["matches"].extend(
                        scan_save_bytes(data, entry=info.filename)
                    )
        else:
            if result["size"] > SAVE_SCAN_RAW_LIMIT:
                result["warnings"].append(
                    f"skipped <raw>: {result['size']} bytes"
                )
                result["risk"] = "none"
                return result
            data = path.read_bytes()
            result["matches"].extend(scan_save_bytes(data, entry="<raw>"))
    except Exception as exc:
        result["error"] = str(exc)
    result["risk"] = (
        "error"
        if result["error"]
        else "possible_shim_reference"
        if result["matches"]
        else "none"
    )
    result["categories"] = sorted({
        match.get("category") or classify_save_scan_pattern(match.get("pattern", ""))
        for match in result["matches"]
    })
    return result


def iter_save_files(path: Path, recursive: bool = False) -> list[Path]:
    def is_save_file(candidate: Path) -> bool:
        return candidate.is_file() and candidate.suffix.lower() == ".save"

    if path.is_file():
        return [path] if is_save_file(path) else []
    globber = path.rglob if recursive else path.glob
    return sorted(
        p for p in globber("*")
        if is_save_file(p)
    )


def summarize_save_scan_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "count": len(results),
        "possible_shim_coupled": sum(1 for r in results if r.get("matches")),
        "errors": sum(1 for r in results if r.get("error")),
        "current_vnflight": 0,
        "legacy_llm": 0,
        "harness": 0,
        "other": 0,
    }
    category_to_summary_key = {
        "current": "current_vnflight",
        "legacy": "legacy_llm",
        "harness": "harness",
        "other": "other",
    }
    for result in results:
        categories = result.get("categories")
        if not categories:
            categories = {
                match.get("category")
                or classify_save_scan_pattern(match.get("pattern", ""))
                for match in result.get("matches") or []
            }
        for category in categories:
            key = category_to_summary_key.get(category, "other")
            summary[key] += 1
    return summary


def format_save_scan_results(root: Path, results: list[dict[str, Any]]) -> str:
    risky = [r for r in results if r.get("matches")]
    errors = [r for r in results if r.get("error")]
    summary = summarize_save_scan_results(results)
    lines = [
        f"Scanned {len(results)} save file(s) under {root}",
        f"Possible shim-coupled saves: {len(risky)}",
    ]
    if risky:
        lines.append(
            "Categories: "
            f"current vnflight/vnf={summary['current_vnflight']}, "
            f"legacy llm={summary['legacy_llm']}, "
            f"harness={summary['harness']}, "
            f"other={summary['other']}"
        )
    if errors:
        lines.append(f"Unreadable saves: {len(errors)}")
    lines.append("")
    if not results:
        lines.append("No .save files found.")
        return "\n".join(lines)
    if not risky and not errors:
        lines.append("No vnflight/vnf/llm shim references found.")
        lines.append("This is a best-effort byte scan, not proof the saves are clean.")
        return "\n".join(lines)
    for item in results:
        if not item.get("matches") and not item.get("error"):
            continue
        marker = "!" if item.get("matches") else "?"
        lines.append(f"{marker} {item['name']}")
        if item.get("error"):
            lines.append(f"  error: {item['error']}")
        for warning in item.get("warnings") or []:
            lines.append(f"  warning: {warning}")
        for match in item.get("matches") or []:
            lines.append(
                f"  {match['entry']}: {match['pattern']} "
                f"({match.get('category', 'other')}) x{match['count']}"
            )
            for snippet in match.get("snippets") or []:
                lines.append(f"    ...{snippet}...")
    lines.append("")
    lines.append("Treat matches as warnings. Back up saves before cleanup or uninstall.")
    return "\n".join(lines)
