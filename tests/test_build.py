"""Tests for build_vnflight.py — single-file build script."""

import sys
import os
import ast
import json
import re
import subprocess
import tempfile
import textwrap
import types

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
if _root not in sys.path:
    sys.path.insert(1, _root)


def _read_normalized(path: str) -> str:
    """Read a file and strip CR so platform-newline drift doesn't fail comparison."""
    with open(path, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


def _read_repo_text(*parts: str) -> str:
    return open(os.path.join(_root, *parts), encoding="utf-8").read()


class TestBuildScript:
    """Tests for the vnflight single-file build process."""


    def test_build_script_exists(self):
        assert os.path.exists(os.path.join(_root, "build_vnflight.py"))

    def test_source_modules_exist(self):
        src_dir = os.path.join(_root, "src", "vnflight")
        assert os.path.exists(os.path.join(src_dir, "lib.py"))
        assert os.path.exists(os.path.join(src_dir, "format.py"))
        assert os.path.exists(os.path.join(src_dir, "cli.py"))
        assert os.path.exists(os.path.join(src_dir, "mcp.py"))
        assert os.path.exists(os.path.join(src_dir, "__init__.py"))

    def test_source_modules_parse(self):
        """All source modules should be valid Python."""
        src_dir = os.path.join(_root, "src", "vnflight")
        for fname in os.listdir(src_dir):
            if fname.endswith(".py"):
                path = os.path.join(src_dir, fname)
                source = open(path, encoding="utf-8").read()
                try:
                    ast.parse(source)
                except SyntaxError as e:
                    assert False, f"Syntax error in {fname}: {e}"

    def test_built_file_parses(self):
        """The built vnflight.py should be valid Python (if it exists)."""
        built = os.path.join(_root, "vnflight_built.py")
        if os.path.exists(built):
            source = open(built, encoding="utf-8").read()
            ast.parse(source)  # Would raise SyntaxError on failure.

    def test_vnflight_py_matches_build_output(self):
        """Committed vnflight.py must equal what build_vnflight.py produces.

        Without this, hand-edits to src/vnflight/ that aren't followed by a
        build run go unnoticed and vnflight.py drifts. CI shouldn't accept
        a state where running the build script would change a tracked file.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "vnflight_check.py")
            result = subprocess.run(
                [sys.executable, os.path.join(_root, "build_vnflight.py"),
                 "--output", out],
                capture_output=True, text=True, cwd=_root, timeout=60,
            )
            assert result.returncode == 0, (
                f"build_vnflight.py failed: stderr={result.stderr}"
            )
            committed = _read_normalized(os.path.join(_root, "dist", "vnflight.py"))
            built = _read_normalized(out)
            if committed != built:
                # Show a tight summary; full diff would be 10k+ lines.
                msg = (
                    "vnflight.py is out of sync with build_vnflight.py output. "
                    "Run `python build_vnflight.py --output vnflight.py` to "
                    "regenerate.\n"
                    f"  committed: {len(committed):,} chars / "
                    f"{committed.count(chr(10)):,} lines\n"
                    f"  built:     {len(built):,} chars / "
                    f"{built.count(chr(10)):,} lines"
                )
                # Find the first differing line for a useful pointer.
                c_lines = committed.splitlines()
                b_lines = built.splitlines()
                for i, (a, b) in enumerate(zip(c_lines, b_lines)):
                    if a != b:
                        msg += (
                            f"\n  first diff at line {i + 1}:\n"
                            f"    committed: {a!r}\n"
                            f"    built:     {b!r}"
                        )
                        break
                assert False, msg

    def test_vnflight_py_exists(self):
        """The committed artifact lives at dist/vnflight.py."""
        assert os.path.exists(os.path.join(_root, "dist", "vnflight.py"))

    def test_vnflight_py_executes_delivery_ownership(self):
        code = (
            "import runpy; "
            "ns=runpy.run_path('dist/vnflight.py', run_name='artifact_test'); "
            "c=ns['BridgeClient'].__new__(ns['BridgeClient']); "
            "c._delivered_action_events=set(); "
            "c._delivered_action_event_ownership=set(); "
            "c._record_delivered_action_events([(1, 2)]); "
            "assert c._action_event_was_delivered((1, 2)); "
            "c.reset_action_event_delivery(); "
            "assert not c._action_event_was_delivered((1, 2))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=_root, timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_vnflight_py_parses(self):
        source = open(os.path.join(_root, "dist", "vnflight.py"), encoding="utf-8").read()
        ast.parse(source)

    def test_vnflight_py_executes_presentation_merge(self):
        """Stripped package imports must not leave unresolved aliases."""
        code = (
            "import runpy; "
            "ns=runpy.run_path('dist/vnflight.py', run_name='artifact_test'); "
            "assert ns['_merge_story_render_sections_by_bridge_sequence']"
            "([], []) == []"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=_root, timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_vnflight_py_executes_action_surface_projection(self):
        """The built artifact must retain the extracted signature policy."""
        code = (
            "import json, runpy; "
            "ns=runpy.run_path('dist/vnflight.py', run_name='artifact_test'); "
            "value=ns['actionable_request_signature']({"
            "'id':'next','reissue_root_request_id':'root',"
            "'type':'choice_request','choices':[{'label':'Continue'}]}); "
            "data=json.loads(value); "
            "assert data['request_id']=='root'; "
            "assert data['choices']==[{'label':'Continue'}]"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=_root, timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_shim_exists(self):
        assert os.path.exists(os.path.join(_root, "vnflight.rpy"))

    def test_shim_rechecks_stale_disabled_choice_conditions(self):
        source = open(os.path.join(_root, "vnflight.rpy"), encoding="utf-8").read()
        assert "_drop_when_condition_false" in source
        assert "_raw_condition_by_label" in source

    def test_shim_load_uses_public_renpy_load_api(self):
        source = open(os.path.join(_root, "vnflight.rpy"), encoding="utf-8").read()
        assert 'getattr(renpy.exports, "load", None)' in source
        assert "renpy.loadsave.list_slots()" in source
        assert "Save slot not found" in source
        assert '_load_fn(slot)' in source
        assert 'success=False' in source
        assert "renpy.loadsave.load(slot)" not in source

    def test_bridge_exists(self):
        assert os.path.exists(os.path.join(_root, "src", "vnflight", "bridge.py"))

    def test_bridge_parses(self):
        source = open(os.path.join(_root, "src", "vnflight", "bridge.py"), encoding="utf-8").read()
        ast.parse(source)


class TestDuplicateNameGuard:
    """Duplicate top-level names across concatenated modules shadow each
    other in the built file (last module wins), so the artifact behaves
    differently from the tested package — e.g. lib.py's
    _normalized_game_id_collision used to silently replace client.py's
    version in vnflight.py.  The build must fail on any new duplicate."""

    def test_guard_detects_cross_module_duplicates(self):
        import build_vnflight

        dups = build_vnflight._find_duplicate_definitions({
            "a.py": "def foo():\n    return 1\n\nBAR = 1\n",
            "b.py": "def foo():\n    return 2\n\ndef unique():\n    pass\n",
            "c.py": "BAR = 2\n",
        })
        assert dups == {"foo": ["a.py", "b.py"], "BAR": ["a.py", "c.py"]}

    def test_guard_detects_duplicate_classes(self):
        import build_vnflight

        dups = build_vnflight._find_duplicate_definitions({
            "a.py": "class Thing:\n    pass\n",
            "b.py": "class Thing:\n    pass\n",
        })
        assert dups == {"Thing": ["a.py", "b.py"]}

    def test_guard_ignores_imports_and_same_module_rebinds(self):
        import build_vnflight

        dups = build_vnflight._find_duplicate_definitions({
            "a.py": "def foo():\n    return 1\n",
            # Importing a shared name is the sanctioned way to reuse it;
            # rebinding within one module shadows identically in package
            # and artifact, so neither is a build-breaking duplicate.
            "b.py": "from .a import foo\n\nlocal = 1\nlocal = 2\n",
        })
        assert dups == {}

    def test_guard_honours_allowlist(self):
        import build_vnflight

        dups = build_vnflight._find_duplicate_definitions(
            {
                "bridge.py": "def main():\n    pass\n",
                "cli.py": "def main():\n    pass\n",
            },
        )
        assert dups == {}  # "main" is the documented deliberate override.
        dups = build_vnflight._find_duplicate_definitions(
            {
                "bridge.py": "def main():\n    pass\n",
                "cli.py": "def main():\n    pass\n",
            },
            allowlist=frozenset(),
        )
        assert dups == {"main": ["bridge.py", "cli.py"]}

    def test_build_aborts_on_synthetic_duplicate(self, monkeypatch, tmp_path):
        import build_vnflight

        (tmp_path / "one.py").write_text(
            "def collide():\n    return 1\n", encoding="utf-8"
        )
        (tmp_path / "two.py").write_text(
            "def collide():\n    return 2\n", encoding="utf-8"
        )
        monkeypatch.setattr(build_vnflight, "SRC_DIR", tmp_path)
        monkeypatch.setattr(build_vnflight, "MODULE_ORDER", ["one.py", "two.py"])
        out = tmp_path / "out.py"
        with pytest.raises(SystemExit):
            build_vnflight.build(out)
        assert not out.exists()

    def test_real_modules_have_no_unallowlisted_duplicates(self):
        import build_vnflight

        module_sources = {}
        for mod_name in build_vnflight.MODULE_ORDER:
            path = os.path.join(_root, "src", "vnflight", mod_name)
            if os.path.exists(path):
                module_sources[mod_name] = open(path, encoding="utf-8").read()
        dups = build_vnflight._find_duplicate_definitions(module_sources)
        assert dups == {}, (
            "Duplicate top-level names across src/vnflight modules — the "
            f"built vnflight.py would shadow them: {dups}"
        )

    def test_real_modules_do_not_alias_stripped_intra_package_imports(self):
        """The concatenated build strips imports, not their alias contract."""
        import build_vnflight

        offenders = []
        allowed = [
            ("cli.py", ("main",), "bridge", 1,
             "run_bridge_server", "_bridge_run"),
        ]
        seen_allowed = []
        for mod_name in build_vnflight.MODULE_ORDER:
            path = os.path.join(_root, "src", "vnflight", mod_name)
            if not os.path.exists(path):
                continue
            tree = ast.parse(open(path, encoding="utf-8").read())
            scope = []

            class ImportAliasVisitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node):
                    scope.append(node.name)
                    self.generic_visit(node)
                    scope.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_ImportFrom(self, node):
                    intra_package = node.level > 0 or (
                        node.module or "").startswith("vnflight.")
                    if not intra_package:
                        return
                    for alias in node.names:
                        if not alias.asname or alias.asname == alias.name:
                            continue
                        signature = (
                            mod_name, tuple(scope), node.module, node.level,
                            alias.name, alias.asname,
                        )
                        if signature in allowed:
                            seen_allowed.append(signature)
                        else:
                            offenders.append(
                                f"{mod_name}:{node.lineno} "
                                f"{alias.name} as {alias.asname}"
                            )

            ImportAliasVisitor().visit(tree)

        assert offenders == [], (
            "Intra-package aliases are stripped by the single-file "
            f"build and become undefined: {offenders}"
        )
        assert seen_allowed == allowed, (
            "The CLI bridge-mode import has an explicit built-artifact "
            "fallback. Its scope and occurrence count are part of the narrow "
            "exception; update it when that code changes: "
            f"{seen_allowed}"
        )


class TestConfigFiles:
    """Tests for configuration files."""

    def test_vnflight_json_exists(self):
        path = os.path.join(_root, "vnflight.json")
        if not os.path.exists(path):
            # Try default.
            path = os.path.join(_root, "vnflight.default.json")
        assert os.path.exists(path), "Neither vnflight.json nor vnflight.default.json found"

    def test_vnflight_json_valid(self):
        for name in ("vnflight.json", "vnflight.default.json"):
            path = os.path.join(_root, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                assert isinstance(data, dict)
                break

    def test_vnflight_game_ids_do_not_have_normalized_collisions(self):
        for name in ("vnflight.json", "vnflight.default.json"):
            path = os.path.join(_root, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            seen: dict[str, str] = {}
            for game_id in (data.get("games") or {}):
                normalized = re.sub(r"[^a-z0-9]+", "", game_id.lower())
                previous = seen.get(normalized)
                assert previous is None, (
                    f"{name} has normalized game-id collision: "
                    f"{previous!r} and {game_id!r}"
                )
                seen[normalized] = game_id

    def test_default_configs_have_required_fields(self):
        for name in ("vnflight.default.json",):
            path = os.path.join(_root, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                assert "games" in data or "profiles" in data, \
                    f"{name} should have 'games' or 'profiles'"


    def test_default_config_mod_entries_use_the_form_install_shim_accepts(self):
        """install-shim indexes mod["source"] / mod["target"]; the default
        config's examples were plain strings and crashed the quick install
        for anyone who copied them."""
        # Dev repo: the default template and the release template.
        # Assembled release tree: the staged vnflight.json template.
        candidates = [
            os.path.join(_root, "vnflight.default.json"),
            os.path.join(_root, "release_assets", "vnflight.release.json"),
            os.path.join(_root, "vnflight.json"),
        ]
        checked = 0
        for path in candidates:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            checked += 1
            for game_id, game in (data.get("games") or {}).items():
                if not isinstance(game, dict):  # "_comment" keys
                    continue
                for mod in game.get("mods") or []:
                    assert isinstance(mod, dict), (path, game_id, mod)
                    source = mod.get("source")
                    target = mod.get("target")
                    assert isinstance(source, str) and source.endswith(".rpy"), (path, game_id, mod)
                    assert isinstance(target, str) and target.startswith("vnf_"), (path, game_id, mod)
        assert checked, "no config template found"


class TestArtifactLayouts:
    """The committed artifact must resolve its project root from both
    layouts users get: a clone (dist/vnflight.py, shim and config one level
    up) and the flat release download (all three files in one folder)."""

    def _layout(self, tmp_path, flat):
        import shutil
        root = tmp_path / ("flat" if flat else "clone")
        root.mkdir()
        target = root / "vnflight.py" if flat else root / "dist" / "vnflight.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(_root, "dist", "vnflight.py"), target)
        shutil.copy(os.path.join(_root, "vnflight.rpy"), root / "vnflight.rpy")
        game = root / "mygame"
        (game / "game").mkdir(parents=True)
        (root / "vnflight.json").write_text(json.dumps({
            "games": {"mygame": {"name": "My Game", "launch": "renpy.exe mygame",
                                 "game_dir": "mygame", "mods": []}}}), encoding="utf-8")
        return root, target, game

    def _run(self, root, target, *args):
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   VNFLIGHT_DATA_DIR=str(root / ".state"))
        return subprocess.run(
            [sys.executable, rel, *args], cwd=root, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=120)

    @pytest.mark.parametrize("flat", [False, True], ids=["clone-dist", "flat-release"])
    def test_games_and_install_shim_resolve_the_root(self, tmp_path, flat):
        root, target, game = self._layout(tmp_path, flat)

        games = self._run(root, target, "games")
        assert games.returncode == 0, games.stdout + games.stderr
        assert "mygame" in games.stdout

        install = self._run(root, target, "--yes", "--quiet", "install-shim", "mygame")
        assert install.returncode == 0, install.stdout + install.stderr
        assert (game / "game" / "vnflight.rpy").read_bytes() == \
            (root / "vnflight.rpy").read_bytes()


class TestVersionConstant:
    """One version string: src/vnflight/__init__.py.  The build copies it
    into the artifact; nothing else may spell it out."""

    def test_artifact_version_equals_the_package_version(self):
        sys.path.insert(0, os.path.join(_root, "src"))
        try:
            from vnflight import __version__
        finally:
            sys.path.pop(0)
        text = open(os.path.join(_root, "dist", "vnflight.py"), encoding="utf-8").read()
        found = re.findall(r'^__version__ = "([^"]+)"$', text, re.M)
        assert found == [__version__]
        assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)

    def test_no_module_hardcodes_a_version(self):
        src = os.path.join(_root, "src", "vnflight")
        for name in os.listdir(src):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            text = open(os.path.join(src, name), encoding="utf-8").read()
            assert not re.search(r'^VERSION\s*=\s*"', text, re.M), name
            assert not re.search(r'"version":\s*"\d+\.\d+', text), name

