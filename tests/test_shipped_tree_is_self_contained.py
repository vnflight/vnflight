"""The core repository's tests must not depend on private workspace content.

Local integration tests belong in the enclosing workspace, not this repo.
This guard runs in standalone clones as well as the development workspace.
"""
import os
import re

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)

_FORBIDDEN = [
    (re.compile(r"^\s*(from|import)\s+(harness|diagnostics|vnharness|journal|recall)\b"),
     "imports a package the release does not ship"),
    (re.compile(r"renpy-\d[\w.]*-sdk"), "reads Ren'Py SDK source"),
    (re.compile(r'_renpy6_sdk_root|/\s*"renpy"\s*/|/\s*"renpy/'), "reads Ren'Py engine source"),
    (re.compile(r'ROOT\s*/\s*"mods"|_root,\s*"mods"'), "reads the mods directory"),
    (re.compile(r'"mods/[A-Za-z0-9_]+\.rpy"'), "reads a game-specific mod file"),
    (re.compile(r"echoes_of_tomorrow/game|mystic_cafe/game"), "reads sample-game source"),
    (re.compile(r'"(echoes_of_tomorrow|mystic_cafe)"\s*,\s*"game"'), "reads sample-game source through a path helper"),
    (re.compile(r"GOG Games|Steam\\\\steamapps|steamapps/common"), "needs a store install"),
    (re.compile(r'"diagnostics/'), "reads diagnostics fixtures"),
]

# Fake mod entries in config fixtures and fake launch commands are fine:
# they name files that are never opened. Only a real read is a dependency,
# so the mod-file and SDK patterns apply to lines that also open/read/glob
# or build a path from one.
_READ_HINT = re.compile(
    r"read_text|read_bytes|open\(|glob\(|\.exists\(|\.is_file\(|read\(\)"
    r"|ROOT\s*/|Path\(|parametrize"
)
_NEEDS_READ_HINT = {"reads a game-specific mod file", "reads Ren'Py SDK source"}
_SELF = os.path.basename(__file__)

def _shipped_modules() -> list[str]:
    return sorted(
        n for n in os.listdir(_TESTS)
        if n.startswith("test_") and n.endswith(".py") and n != _SELF
    )


def test_private_test_tree_is_not_shipped():
    assert not os.path.exists(os.path.join(_TESTS, "local"))


@pytest.mark.parametrize("module", _shipped_modules())
def test_shipped_module_references_only_shipped_content(module):
    path = os.path.join(_TESTS, module)
    hits = []
    with open(path, encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern, why in _FORBIDDEN:
                if not pattern.search(line):
                    continue
                if why in _NEEDS_READ_HINT and not _READ_HINT.search(line):
                    continue
                hits.append(f"{module}:{lineno}: {why}: {stripped[:100]}")
    assert not hits, "\n".join(hits)
