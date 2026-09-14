"""Build a single-file vnflight.py from src/vnflight/ modules.

Concatenates modules in dependency order, deduplicates imports,
and produces a standalone script with CLI entry point.

Usage:
    python build_vnflight.py [--output vnflight_built.py]
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent / "src" / "vnflight"

# Modules in dependency order (later modules may import from earlier ones).
MODULE_ORDER = [
    "mod_fetch.py", # Explicit verified adapter snapshot downloads.
    "shim_schema.py", # Shared shim/bridge schema contracts.
    "action_surface.py", # Pure act target/signature projection policy.
    "act_settle.py", # Pure post-act settling verdict policy.
    "lifecycle.py", # Shared lifecycle/state classification.
    "delivery_ownership.py", # Cross-lane action occurrence ownership.
    "settle.py",    # Shared settling/signature helpers.
    "overlay.py",   # Shared passive-overlay differencing policy.
    "overlay_ledger.py", # Passive-overlay occurrence ownership policy.
    "presentation.py", # Shared story occurrence ordering and rendering.
    "save_scan.py", # Read-only Ren'Py save scanner.
    "client.py",    # BridgeClient — HTTP bridge communication.
    "lib.py",       # Reusable utilities — game discovery, config, process management.
    "format.py",
    "overlay_presentation.py", # Stateful overlay/screen-text presentation ledger.
    "presentation_lane.py", # Serialized presentation ownership for public calls.
    "handlers.py",  # Shared CLI/MCP gameplay tool semantics.
    "mcp.py",
    "bridge.py",    # Bridge server — must be before cli.py so definitions
    "cli.py",       #   exist before the __main__ entry point runs.
]

HEADER = '''\
#!/usr/bin/env python3
"""vnflight — visual novel automation toolkit.

Single-file build generated from src/vnflight/ modules.
Do not edit directly — modify the source modules instead.
"""

'''

# Top-level names deliberately defined in more than one module.  In the
# concatenated build the LAST definition wins; every entry here must be an
# intentional override, documented at the definition site.
#   main — cli.py's main() is the artifact entry point and supersedes
#          bridge.py's standalone main().
DUPLICATE_NAME_ALLOWLIST = frozenset({"main"})


def _top_level_names(source: str) -> set[str]:
    """Top-level def/class/assignment names bound by a module body.

    Imports are excluded: the build deduplicates import lines separately,
    and intra-package ``from .x import name`` re-bindings are stripped.
    """
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _find_duplicate_definitions(
    module_sources: dict[str, str],
    allowlist: frozenset[str] = DUPLICATE_NAME_ALLOWLIST,
) -> dict[str, list[str]]:
    """Map duplicate top-level names to the modules that define them.

    In the concatenated single-file build a name defined by two modules is
    silently shadowed by the later module, so the artifact can behave
    differently from the tested package.  Any duplicate not on the
    allowlist must fail the build.
    """
    definers: dict[str, list[str]] = {}
    for mod_name, source in module_sources.items():
        for name in sorted(_top_level_names(source)):
            definers.setdefault(name, []).append(mod_name)
    return {
        name: mods
        for name, mods in definers.items()
        if len(mods) > 1 and name not in allowlist
    }

# Imports that should appear once at the top.
_STDLIB_IMPORTS: set[str] = set()
_THIRDPARTY_IMPORTS: set[str] = set()

# Patterns for import lines.
_IMPORT_RE = re.compile(r"^(import |from \S+ import )")
_FUTURE_RE = re.compile(r"^from __future__ import ")
_DOCSTRING_RE = re.compile(r'^""".*?"""', re.DOTALL)


def _classify_import(line: str) -> str | None:
    """Classify an import line. Returns 'stdlib', 'thirdparty', or None."""
    stripped = line.strip()
    if not _IMPORT_RE.match(stripped):
        return None
    # Third-party heuristic: known packages.
    thirdparty = {"mcp", "anyio", "anthropic", "openai"}
    if stripped.startswith("from "):
        pkg = stripped.split()[1].split(".")[0]
    else:
        pkg = stripped.split()[1].split(".")[0]
    if pkg in thirdparty:
        return "thirdparty"
    return "stdlib"


def _extract_module(path: Path) -> tuple[list[str], list[str]]:
    """Read a module file, separate imports from body.

    Returns (import_lines, body_lines).
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    imports: list[str] = []
    body: list[str] = []
    in_docstring = False
    past_header = False

    skip_until_close = False  # For multi-line intra-package imports.

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip continuation lines of a multi-line intra-package import.
        if skip_until_close:
            if ")" in stripped:
                skip_until_close = False
            continue

        # Skip __future__ imports (we'll add one at the top).
        if _FUTURE_RE.match(stripped):
            continue

        # Skip module-level docstrings.
        if not past_header and stripped.startswith('"""'):
            if stripped.count('"""') >= 2:
                continue  # Single-line docstring.
            in_docstring = not in_docstring
            continue
        if in_docstring:
            if '"""' in stripped:
                in_docstring = False
            continue

        # Classify imports.
        if not past_header and _IMPORT_RE.match(stripped):
            # Skip intra-package imports (from vnflight.lib import ...).
            if stripped.startswith("from vnflight.") or stripped.startswith("from ."):
                if ")" not in stripped and "(" in stripped:
                    skip_until_close = True  # Multi-line import.
                continue
            imports.append(line)
            continue

        # First non-import, non-blank line marks the body.
        if stripped and not past_header:
            past_header = True

        # Strip relative/intra-package imports from function bodies too.
        if past_header and _IMPORT_RE.match(stripped):
            if stripped.startswith("from .") or stripped.startswith("from vnflight."):
                if ")" not in stripped and "(" in stripped:
                    skip_until_close = True
                # Replace with comment + pass (pass prevents empty block
                # when the import was the sole body of try/if/etc.).
                indent = line[:len(line) - len(line.lstrip())]
                body.append(indent + "# (intra-package import stripped by build)")
                body.append(indent + "pass")
                continue

        body.append(line)

    return imports, body


_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.M)


def _package_version() -> str:
    """The __version__ string from src/vnflight/__init__.py."""
    text = (SRC_DIR / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if not match:
        raise SystemExit("src/vnflight/__init__.py has no __version__ line")
    return match.group(1)


def build(output: Path) -> None:
    all_imports: list[str] = []
    seen_imports: set[str] = set()
    all_body: list[str] = []

    # Guard: duplicate top-level names across modules silently shadow each
    # other in the concatenated file (last module wins), forking the built
    # artifact's behavior from the tested package.  Fail loudly instead.
    module_sources = {
        mod_name: (SRC_DIR / mod_name).read_text(encoding="utf-8")
        for mod_name in MODULE_ORDER
        if (SRC_DIR / mod_name).exists()
    }
    duplicates = _find_duplicate_definitions(module_sources)
    if duplicates:
        for name, mods in sorted(duplicates.items()):
            print(
                f"  ERROR: top-level name {name!r} defined in multiple "
                f"modules: {', '.join(mods)} — the later definition would "
                "shadow the earlier one in the built file. Share one "
                "definition (import it) or add the name to "
                "DUPLICATE_NAME_ALLOWLIST if the override is deliberate.",
                file=sys.stderr,
            )
        raise SystemExit(1)

    for mod_name in MODULE_ORDER:
        mod_path = SRC_DIR / mod_name
        if not mod_path.exists():
            print(f"  skip {mod_name} (not found)", file=sys.stderr)
            continue

        print(f"  include {mod_name}", file=sys.stderr)
        imports, body = _extract_module(mod_path)

        # Strip `if __name__ == "__main__"` blocks from non-CLI modules.
        # Only cli.py (the last real module) keeps its entry point.
        if mod_name != "cli.py":
            filtered_body = []
            skip_main = False
            for line in body:
                if line.strip().startswith('if __name__') and '__main__' in line:
                    skip_main = True
                    continue
                if skip_main:
                    if line and not line[0].isspace():
                        skip_main = False  # Unindented line = end of if block.
                    else:
                        continue
                filtered_body.append(line)
            body = filtered_body

        # Dedup imports.
        for imp in imports:
            key = imp.strip()
            if key not in seen_imports:
                seen_imports.add(key)
                all_imports.append(imp)

        # Add module separator and body.
        all_body.append(f"\n# {'=' * 70}")
        all_body.append(f"# Module: {mod_name}")
        all_body.append(f"# {'=' * 70}\n")
        all_body.extend(body)

    # Merge `from X import a, b` lines for the same module.
    merged_from: dict[str, set[str]] = {}
    plain: list[str] = []
    for imp in all_imports:
        m = re.match(r"from\s+(\S+)\s+import\s+(.+)", imp.strip())
        if m:
            mod = m.group(1)
            names = {n.strip() for n in m.group(2).split(",")}
            merged_from.setdefault(mod, set()).update(names)
        else:
            plain.append(imp)
    all_imports = list(plain)
    for mod, names in sorted(merged_from.items()):
        all_imports.append(f"from {mod} import {', '.join(sorted(names))}\n")

    # Assemble.
    parts = [HEADER, "from __future__ import annotations\n\n"]
    parts.append("\n".join(all_imports))
    parts.append("\n")
    # The version constant lives in src/vnflight/__init__.py, which is not
    # a listed module; copy that one line so the artifact reports the same
    # version as the package without a second copy of the string.
    parts.append(
        "\n# Version (copied from src/vnflight/__init__.py by the build)\n"
        f'__version__ = "{_package_version()}"\n'
    )
    parts.append("\n".join(all_body))

    # Add CLI entry point if not already present.
    combined = "\n".join(parts)
    if 'if __name__ == "__main__"' not in combined:
        parts.append('\n\nif __name__ == "__main__":\n    main()\n')

    output.write_text("\n".join(parts), encoding="utf-8")
    print(f"  wrote {output} ({output.stat().st_size:,} bytes)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build single-file vnflight.py")
    parser.add_argument("--output", "-o", default="vnflight_built.py",
                        help="Output file (default: vnflight_built.py)")
    args = parser.parse_args()

    print("Building vnflight single-file...", file=sys.stderr)
    build(Path(args.output))
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
