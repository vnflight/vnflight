"""CLI robustness tests: dead-bridge detection and install-shim reporting.

Covers the June 2026 review cluster: `wait` must not hang forever when
the bridge process dies mid-wait, read-only commands must exit non-zero
when the bridge is unreachable, and install-shim must never half-succeed
silently.
"""

import json
import os
import sys
from argparse import Namespace

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from vnflight import cli


class FakeClientState:
    def get_cursor(self, key):
        return 0

    def get_last_request_id(self, key):
        return None

    def set_cursor(self, key, cursor):
        pass

    def set_last_request_id(self, key, request_id):
        pass

    def save(self):
        pass


class DeadBridgeSession:
    """A session whose bridge process has died: every request fails."""

    bridge_url = "http://127.0.0.1:8385"
    slot_prefix = ""
    cursor = 0
    last_request_id = None
    _bridge_up = False

    def transcript(self, last=20):
        return []

    def get_transcript(self, last_n=30):
        return []

    def poll(self, timeout=0):
        return []

    def pending(self):
        return None

    def state(self):
        return {}

    def status(self):
        return {}

    def screenshot(self):
        return None

    def is_up(self):
        return False

    def _get(self, path, params=None, timeout=2.0):
        return 0, {"error": "Connection failed: refused"}


def _wait_args(**overrides):
    base = dict(json=False, quiet=False, verbose=False)
    base.update(overrides)
    return Namespace(**base)


# ---------------------------------------------------------------------------
# (a) _perform_wait must fail out when the bridge dies mid-wait
# ---------------------------------------------------------------------------


def test_perform_wait_fails_out_when_bridge_unreachable(monkeypatch, capsys):
    """timeout=None used to hang forever on a dead bridge: poll() swallows
    connection errors and returns [], indistinguishable from a quiet game."""
    session = DeadBridgeSession()
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "_WAIT_BRIDGE_DOWN_GRACE", 0.0)

    rc = cli._perform_wait(session, _wait_args(), FakeClientState(), None)

    assert rc != 0
    out = capsys.readouterr().out
    assert "unreachable" in out.lower()
    assert session.bridge_url in out


def test_perform_wait_bridge_unreachable_json(monkeypatch, capsys):
    session = DeadBridgeSession()
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "_WAIT_BRIDGE_DOWN_GRACE", 0.0)

    rc = cli._perform_wait(session, _wait_args(json=True), FakeClientState(), None)

    assert rc != 0
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "bridge_unreachable"
    assert data["bridge"] == session.bridge_url


def test_perform_wait_bounded_timeout_reports_dead_bridge(monkeypatch, capsys):
    """A bounded wait that expires while the bridge is down must report the
    dead bridge instead of a quiet '(timeout)' with exit 0."""
    session = DeadBridgeSession()
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    # Grace longer than the wait timeout: the loop exits via the deadline.
    monkeypatch.setattr(cli, "_WAIT_BRIDGE_DOWN_GRACE", 60.0)
    # Collapse the deadline arithmetic so the loop exits immediately.
    real_time = cli.time.time
    start = real_time()
    ticks = {"n": 0}

    def fake_time():
        ticks["n"] += 1
        # Jump past any deadline after a few loop iterations.
        return start + (0 if ticks["n"] < 40 else 10_000)

    monkeypatch.setattr(cli.time, "time", fake_time)

    rc = cli._perform_wait(session, _wait_args(), FakeClientState(), 1.0)

    assert rc != 0
    out = capsys.readouterr().out
    assert "unreachable" in out.lower()


# ---------------------------------------------------------------------------
# (b) read-only commands must exit non-zero on a dead bridge
# ---------------------------------------------------------------------------


def test_cmd_state_dead_bridge_exits_nonzero(monkeypatch, capsys):
    """BridgeClient.state() returns {} on connection failure; the old
    `state is None` check printed nothing useful and exited 0."""
    session = DeadBridgeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_state(_wait_args(), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "unreachable" in out.lower()
    assert session.bridge_url in out


def test_cmd_state_bridge_up_but_no_state_exits_nonzero(monkeypatch, capsys):
    class NoStateSession(DeadBridgeSession):
        _bridge_up = True

        def is_up(self):
            return True

    session = NoStateSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_state(_wait_args(), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "no game" in out.lower()


def test_cmd_state_dead_bridge_json_error(monkeypatch, capsys):
    session = DeadBridgeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_state(_wait_args(json=True), FakeClientState())

    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "bridge_unreachable"
    assert data["bridge"] == session.bridge_url


def test_cmd_history_dead_bridge_exits_nonzero(monkeypatch, capsys):
    session = DeadBridgeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    args = _wait_args(all=False, first=None, last=None)
    rc = cli.cmd_history(args, FakeClientState())

    assert rc == 1
    assert "unreachable" in capsys.readouterr().out.lower()


def test_cmd_poll_dead_bridge_exits_nonzero(monkeypatch, capsys):
    session = DeadBridgeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    args = _wait_args(wait_timeout=0, all=False)
    rc = cli.cmd_poll(args, FakeClientState())

    assert rc == 1
    assert "unreachable" in capsys.readouterr().out.lower()


def test_cmd_poll_no_events_on_live_bridge_still_exits_zero(monkeypatch, capsys):
    class QuietSession(DeadBridgeSession):
        _bridge_up = True

        def is_up(self):
            return True

    session = QuietSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    args = _wait_args(wait_timeout=0, all=False)
    rc = cli.cmd_poll(args, FakeClientState())

    assert rc == 0
    assert "no new events" in capsys.readouterr().out


def test_cmd_poll_all_drops_the_restored_stash_before_the_full_read(monkeypatch, capsys):
    """Astra: `poll --all` zeroed the cursor but kept the stash restored
    from the session file; poll() returned only the stash, cursor 0 was
    saved, and the next poll repeated those rows with the history."""
    seen = {}

    class StashSession(DeadBridgeSession):
        _bridge_up = True
        cursor = 45
        _prefetched_events = [{"type": "narration", "text": "deferred", "_seq": 41}]

        def is_up(self):
            return True

        def poll(self, timeout=0, include_prefetched=True):
            seen["cursor"] = self.cursor
            seen["stash"] = list(self._prefetched_events)
            return [{"type": "narration", "text": "from history", "_seq": 1}]

    session = StashSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    state = _RecordingClientState()

    rc = cli.cmd_poll(_wait_args(wait_timeout=0, all=True), state)

    assert rc == 0
    assert seen == {"cursor": 0, "stash": []}
    assert state.deferred[session.bridge_url + session.slot_prefix] == []
    assert "from history" in capsys.readouterr().out


def test_cmd_poll_without_all_keeps_the_restored_stash(monkeypatch, capsys):
    seen = {}

    class StashSession(DeadBridgeSession):
        _bridge_up = True
        cursor = 45
        _prefetched_events = [{"type": "narration", "text": "deferred", "_seq": 41}]

        def is_up(self):
            return True

        def poll(self, timeout=0, include_prefetched=True):
            seen["cursor"] = self.cursor
            seen["stash"] = list(self._prefetched_events)
            return list(self._prefetched_events)

    session = StashSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_poll(_wait_args(wait_timeout=0, all=False), _RecordingClientState())

    assert rc == 0
    assert seen["cursor"] == 45
    assert seen["stash"] == [{"type": "narration", "text": "deferred", "_seq": 41}]


def test_cmd_choices_dead_bridge_exits_nonzero(monkeypatch, capsys):
    session = DeadBridgeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_choices(_wait_args(), FakeClientState())

    assert rc == 1
    assert "unreachable" in capsys.readouterr().out.lower()


def test_cmd_screenshot_dead_bridge_reports_unreachable(monkeypatch, capsys):
    session = DeadBridgeSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    args = _wait_args(output="out.png")
    rc = cli.cmd_screenshot(args, FakeClientState())

    assert rc == 1
    assert "unreachable" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# (e) install-shim must never half-succeed silently
# ---------------------------------------------------------------------------


def _install_args(game, root, **overrides):
    base = dict(
        game=game,
        games_dir=str(root),
        yes=True,
        always_on=False,
        no_mods=False,
        json=False,
        quiet=False,
    )
    base.update(overrides)
    return Namespace(**base)


def _make_install_root(tmp_path, config=None):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "vnflight.rpy").write_text("# shim\n", encoding="utf-8")
    mods = root / "mods"
    mods.mkdir()
    (mods / "thing.rpy").write_text("# mod\n", encoding="utf-8")
    game_dir = root / "mygame"
    (game_dir / "game").mkdir(parents=True)
    if config is not None:
        (root / "vnflight.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
    return root, game_dir


_MOD_CONFIG = {
    "games": {
        "mygame": {
            "game_dir": "mygame",
            "mods": [{"source": "mods/thing.rpy", "target": "vnf_thing.rpy"}],
        }
    }
}


def test_install_shim_installs_mods_and_prints_summary(tmp_path, capsys):
    root, game_dir = _make_install_root(tmp_path, config=_MOD_CONFIG)

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 0
    assert (game_dir / "game" / "vnflight.rpy").exists()
    assert (game_dir / "game" / "vnf_thing.rpy").exists()
    out = capsys.readouterr().out
    assert "Installed vnf_thing.rpy" in out
    assert "Installed shim + 1 mod for 'mygame'" in out


def test_install_shim_up_to_date_reports_mod_count(tmp_path, capsys):
    root, game_dir = _make_install_root(tmp_path, config=_MOD_CONFIG)

    assert cli.cmd_install_shim(
        _install_args("mygame", root), FakeClientState()
    ) == 0
    capsys.readouterr()
    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 0
    out = capsys.readouterr().out
    assert "already up to date for 'mygame' (shim + 1 mod)" in out


def test_install_shim_matches_config_entry_by_install_path(tmp_path, capsys):
    """Passing the install directory (GOG-style) instead of the config id
    used to skip the game's mods silently."""
    root, game_dir = _make_install_root(tmp_path, config=_MOD_CONFIG)

    rc = cli.cmd_install_shim(
        _install_args(str(game_dir), root), FakeClientState()
    )

    assert rc == 0
    assert (game_dir / "game" / "vnf_thing.rpy").exists()
    out = capsys.readouterr().out
    assert "Matched config entry 'mygame' by install path" in out
    assert "Installed shim + 1 mod for 'mygame'" in out


def test_install_shim_missing_mod_source_fails_loudly(tmp_path, capsys):
    config = {
        "games": {
            "mygame": {
                "game_dir": "mygame",
                "mods": [
                    {"source": "mods/gone.rpy", "target": "vnf_gone.rpy"}
                ],
            }
        }
    }
    root, game_dir = _make_install_root(tmp_path, config=config)

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "Mod source not found" in out
    assert "gone.rpy" in out


def test_install_shim_no_config_entry_warns_explicitly(tmp_path, capsys):
    config = {"games": {"othergame": {"game_dir": "elsewhere"}}}
    root, game_dir = _make_install_root(tmp_path, config=config)

    rc = cli.cmd_install_shim(
        _install_args(str(game_dir), root), FakeClientState()
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "no config entry matches" in out
    assert "WITHOUT mods" in out


def test_install_shim_unreadable_config_warns_explicitly(tmp_path, capsys):
    root, game_dir = _make_install_root(tmp_path)
    (root / "vnflight.json").write_text("{not json", encoding="utf-8")

    rc = cli.cmd_install_shim(
        _install_args(str(game_dir), root), FakeClientState()
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "could not be read" in out
    assert "WITHOUT mods" in out


def test_install_shim_no_mods_configured_is_reported(tmp_path, capsys):
    config = {"games": {"mygame": {"game_dir": "mygame"}}}
    root, game_dir = _make_install_root(tmp_path, config=config)

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 0
    out = capsys.readouterr().out
    assert "no mods configured for 'mygame'" in out


class TestMcpSubcommandBridgeToken:
    """`vnflight.py mcp --bridge URL --token T` — the documented form.

    The mcp subparser used to accept only --game/--slot/--capabilities,
    so the invocation shown in mcp.py's own docstring was an argparse
    error (exit 2), and binding a single-file MCP server to a specific
    slot bridge required running src/vnflight/mcp.py instead of the
    downloadable artifact.  Token passthrough matches the bridge
    security plumbing (ca4ba72): a token-gated bridge needs the token
    at BridgeClient construction.
    """

    def test_mcp_subcommand_accepts_bridge_and_token(self):
        parser = cli.build_parser()
        args = parser.parse_args([
            "mcp",
            "--bridge", "http://127.0.0.1:9501",
            "--token", "sekrit",
            "--game", "roadwarden",
        ])
        assert args.command == "mcp"
        assert args.mcp_bridge == "http://127.0.0.1:9501"
        assert args.mcp_token == "sekrit"
        assert args.game == "roadwarden"

    def test_mcp_subcommand_bridge_defaults_to_global_flag(self):
        parser = cli.build_parser()
        # Global --bridge before the subcommand must keep working, and the
        # subcommand-level option must not clobber it with its default.
        args = parser.parse_args(["--bridge", "http://127.0.0.1:7777", "mcp"])
        assert args.bridge == "http://127.0.0.1:7777"
        assert args.mcp_bridge is None
        assert args.mcp_token is None

    def test_main_wires_mcp_bridge_and_token_to_run_server(self, monkeypatch):
        import vnflight.mcp as vmcp

        captured = {}

        def fake_run_server(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(vmcp, "run_server", fake_run_server)
        monkeypatch.setattr(sys, "argv", [
            "vnflight.py", "mcp",
            "--bridge", "http://127.0.0.1:9501",
            "--token", "sekrit",
            "--slot", "latest:roadwarden",
        ])

        rc = cli.main()

        assert rc == 0
        assert captured["bridge_url"] == "http://127.0.0.1:9501"
        assert captured["token"] == "sekrit"
        assert captured["slot"] == "latest:roadwarden"

    def test_main_mcp_falls_back_to_global_bridge(self, monkeypatch):
        import vnflight.mcp as vmcp

        captured = {}
        monkeypatch.setattr(vmcp, "run_server", lambda **kw: captured.update(kw))
        monkeypatch.setattr(sys, "argv", [
            "vnflight.py", "--bridge", "http://127.0.0.1:7777", "mcp",
        ])

        rc = cli.main()

        assert rc == 0
        assert captured["bridge_url"] == "http://127.0.0.1:7777"
        assert captured["token"] is None

    def test_act_schema_explains_result_timeout_wall_clock(self):
        import vnflight.mcp as vmcp

        act = next(tool for tool in vmcp._TOOLS if tool["name"] == "act")
        description = (
            act["parameters"]["properties"]["result_timeout"]["description"])

        assert "Action-lifecycle seconds" in description
        assert "preflight, bridge acceptance, and settlement" in description
        assert "stream-presentation drain time is excluded" in description
        assert "Total tool wall time may be longer" in description
        assert "MCP transport/mutation budget" in description


# ---------------------------------------------------------------------------
# (e) _make_session must carry the stored bridge admin token
# ---------------------------------------------------------------------------


class TokenClientState(FakeClientState):
    def __init__(self, token=None):
        self.token = token

    def get_admin_token(self, bridge_url):
        return self.token


class TestMakeSessionToken:
    """launch reserves the game slot with a minted token; the bridge then
    token-gates every slot-scoped route.  A session built without the
    stored admin token 403s on state/wait/act right after a successful
    launch (regression from the ca4ba72 lockdown)."""

    def _args(self):
        return Namespace(bridge="http://127.0.0.1:8385", target_slot=None)

    def test_session_carries_stored_admin_token(self, monkeypatch):
        monkeypatch.delenv("VNFLIGHT_TOKEN", raising=False)
        monkeypatch.setattr(
            cli.BridgeClient, "auto_select_slot", lambda self, hint=None: True
        )

        session = cli._make_session(self._args(), TokenClientState("sekrit"))

        assert session.token == "sekrit"

    def test_session_env_token_fallback(self, monkeypatch):
        monkeypatch.setenv("VNFLIGHT_TOKEN", "env-tok")
        monkeypatch.setattr(
            cli.BridgeClient, "auto_select_slot", lambda self, hint=None: True
        )

        session = cli._make_session(self._args(), TokenClientState(None))

        assert session.token == "env-tok"

    def test_session_tokenless_without_stored_or_env(self, monkeypatch):
        monkeypatch.delenv("VNFLIGHT_TOKEN", raising=False)
        monkeypatch.setattr(
            cli.BridgeClient, "auto_select_slot", lambda self, hint=None: True
        )

        session = cli._make_session(self._args(), TokenClientState(None))

        assert session.token is None


# ---------------------------------------------------------------------------
# (g) explicit launch flags must survive the auto-applied default profile
# ---------------------------------------------------------------------------


class ProfileCaptureSession:
    """Records the 'set' command the profile apply sends to the shim."""

    def __init__(self):
        self.sent_changes = None
        self.sent_nonce = None
        self.wait_after_seq = None
        self.wait_match = None

    def _get(self, path, timeout=2.0):
        assert path == "/state"
        return 200, {"event_counter": 17, "transcript": []}

    def _send_command(self, name, args=None, nonce=None):
        self.sent_nonce = nonce
        if name == "set":
            self.sent_changes = (args or {}).get("changes")
        return True, "ok"

    def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
        self.wait_after_seq = after_seq
        self.wait_match = match
        applied = [
            {"key": key, "old_value": None, "value": value}
            for key, value in (self.sent_changes or {}).items()
        ]
        result = {"success": True, "applied": applied, "nonce": self.sent_nonce}
        if match is not None and not match(result):
            return None
        return result


def test_cli_command_helper_threads_submit_boundary_and_nonce():
    session = ProfileCaptureSession()

    submitted, msg, result = cli._send_and_wait_command_result(
        session,
        "set",
        {"changes": {"text_cps": 40}},
        timeout=4.0,
    )

    assert submitted is True
    assert msg == "ok"
    assert result and result["success"] is True
    assert session.sent_nonce
    assert session.wait_after_seq == 17
    assert session.wait_match is not None


def test_cli_command_helper_rejects_mismatched_nonce_result():
    class MismatchedSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            self.wait_after_seq = after_seq
            self.wait_match = match
            wrong = {"success": True, "nonce": "other-client"}
            return wrong if match is None or match(wrong) else None

    session = MismatchedSession()

    submitted, msg, result = cli._send_and_wait_command_result(
        session,
        "set",
        {"changes": {"text_cps": 40}},
    )

    assert submitted is True
    assert msg == "ok"
    assert result is None
    assert session.wait_after_seq == 17


def test_cli_command_helper_can_ignore_submit_boundary_for_reset_commands():
    session = ProfileCaptureSession()

    submitted, msg, result = cli._send_and_wait_command_result(
        session,
        "load",
        {"slot": "checkpoint"},
        reset_boundary=True,
    )

    assert submitted is True
    assert msg == "ok"
    assert result and result["success"] is True
    assert session.sent_nonce
    assert session.wait_after_seq is None
    assert session.wait_match is not None


def test_cmd_debug_set_uses_submit_boundary_and_nonce(monkeypatch, capsys):
    class DebugSetSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            self.wait_after_seq = after_seq
            self.wait_match = match
            result = {
                "type": "command_result",
                "command": command,
                "success": True,
                "nonce": self.sent_nonce,
                "value": 40,
            }
            if match is not None and not match(result):
                return None
            return result

    session = DebugSetSession()
    saved = []
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: saved.append(s))

    args = Namespace(
        command_name="set",
        command_args=["text_cps=40"],
        json=True,
        quiet=True,
        wait=False,
    )

    assert cli.cmd_cmd(args, FakeClientState()) == 0
    capsys.readouterr()
    assert session.sent_changes is None
    assert session.sent_nonce
    assert session.wait_after_seq == 17
    assert session.wait_match is not None
    assert saved


def test_apply_profile_reports_only_actual_changes(monkeypatch):
    monkeypatch.setattr(cli, "_load_config", lambda *_args, **_kwargs: {
        "profiles": {"hybrid_text": {"text_cps": 45, "turbo": False}},
    })
    monkeypatch.setattr(
        cli,
        "_send_and_wait_command_result",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            {
                "success": True,
                "applied": [
                    {"key": "text_cps", "old_value": 30, "value": 45},
                    {"key": "turbo", "old_value": False, "value": False},
                ],
            },
        ),
    )

    ok, message, data = cli._apply_profile(
        "hybrid_text", object(), Namespace(games_dir=None), FakeClientState())

    assert ok is True
    assert message.splitlines() == [
        "Applied profile 'hybrid_text' (1 setting changed)",
        "  text_cps: 30 → 45",
    ]
    assert len(data["applied"]) == 2


def test_apply_profile_reports_already_applied(monkeypatch):
    monkeypatch.setattr(cli, "_load_config", lambda *_args, **_kwargs: {
        "profiles": {"hybrid_text": {"text_cps": 45}},
    })
    monkeypatch.setattr(
        cli,
        "_send_and_wait_command_result",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            {
                "success": True,
                "applied": [
                    {"key": "text_cps", "old_value": 45, "value": 45},
                ],
            },
        ),
    )

    ok, message, _data = cli._apply_profile(
        "hybrid_text", object(), Namespace(games_dir=None), FakeClientState())

    assert ok is True
    assert message == "Profile 'hybrid_text' already applied"


def test_apply_profile_accepts_values_coerced_to_live_setting_types(monkeypatch):
    monkeypatch.setattr(cli, "_load_config", lambda *_args, **_kwargs: {
        "profiles": {"string_config": {
            "text_cps": "45", "auto_advance": "false",
            "post_action_delay": "0.5",
        }},
    })
    monkeypatch.setattr(
        cli, "_send_and_wait_command_result",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            {"success": True, "applied": [
                {"key": "text_cps", "old_value": 30, "value": 45},
                {"key": "auto_advance", "old_value": True, "value": False},
                {"key": "post_action_delay", "old_value": 0.2, "value": 0.5},
            ]},
        ),
    )

    ok, _message, data = cli._apply_profile(
        "string_config", object(), Namespace(games_dir=None), FakeClientState())

    assert ok is True
    assert len(data["applied"]) == 3


@pytest.mark.parametrize("applied", [
    None,
    [],
    [{"key": "text_cps", "value": 45}],
    [{"key": "text_cps", "old_value": 30, "value": 40}],
])
def test_apply_profile_rejects_invalid_success_receipt(monkeypatch, applied):
    monkeypatch.setattr(cli, "_load_config", lambda *_args, **_kwargs: {
        "profiles": {"hybrid_text": {"text_cps": 45}},
    })
    monkeypatch.setattr(
        cli,
        "_send_and_wait_command_result",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            {"success": True, "applied": applied},
        ),
    )

    ok, message, data = cli._apply_profile(
        "hybrid_text", object(), Namespace(games_dir=None), FakeClientState())

    assert ok is False
    assert "invalid application receipt" in message
    assert data["reason"] == "invalid_application_receipt"
    assert data["mutation_may_have_applied"] is True


def test_cmd_debug_set_fails_when_confirmed_result_missing(monkeypatch, capsys):
    class TimeoutSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            self.wait_after_seq = after_seq
            self.wait_match = match
            return None

    session = TimeoutSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: None)

    args = Namespace(
        command_name="set",
        command_args=["text_cps=40"],
        json=True,
        quiet=True,
        wait=False,
    )

    assert cli.cmd_cmd(args, FakeClientState()) == 1
    out = capsys.readouterr().out
    assert "not confirmed" in out
    assert session.sent_nonce
    assert session.wait_after_seq == 17
    assert session.wait_match is not None


def test_cmd_save_uses_nonce_confirmed_result(monkeypatch, capsys):
    session = ProfileCaptureSession()
    saved = []
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: saved.append(s))

    args = Namespace(
        slot="checkpoint",
        name="Checkpoint",
        json=True,
        quiet=True,
    )

    assert cli.cmd_save(args, FakeClientState()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is True
    assert data["slot"] == "checkpoint"
    assert session.sent_nonce
    assert session.wait_after_seq == 17
    assert session.wait_match is not None
    assert saved == [session]


def test_cmd_save_fails_when_confirmed_result_missing(monkeypatch, capsys):
    class TimeoutSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            self.wait_after_seq = after_seq
            self.wait_match = match
            return None

    session = TimeoutSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: None)

    args = Namespace(
        slot="checkpoint",
        name="Checkpoint",
        json=True,
        quiet=True,
    )

    assert cli.cmd_save(args, FakeClientState()) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is False
    assert data["confirmed"] is False
    assert session.sent_nonce
    assert session.wait_after_seq == 17
    assert session.wait_match is not None


def test_cmd_save_reports_confirmed_failure(monkeypatch, capsys):
    class FailedSaveSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            self.wait_after_seq = after_seq
            self.wait_match = match
            result = {
                "type": "command_result",
                "command": command,
                "success": False,
                "error": "disk full",
                "nonce": self.sent_nonce,
            }
            if match is not None and not match(result):
                return None
            return result

    session = FailedSaveSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: None)

    args = Namespace(
        slot="checkpoint",
        name="Checkpoint",
        json=False,
        quiet=False,
    )

    assert cli.cmd_save(args, FakeClientState()) == 1
    out = capsys.readouterr().out
    assert "Save failed for slot 'checkpoint': disk full" in out
    assert "confirmed" not in out
    assert session.sent_nonce
    assert session.wait_after_seq == 17
    assert session.wait_match is not None


def _profile_config_root(tmp_path, profile):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "vnflight.json").write_text(
        json.dumps(
            {
                "games": {"mygame": {"default_profile": "hybrid_test"}},
                "profiles": {"hybrid_test": profile},
            }
        ),
        encoding="utf-8",
    )
    return root


def _launch_args(root, **overrides):
    base = dict(
        game="mygame",
        games_dir=str(root),
        json=False,
        quiet=False,
        fast_forward=False,
        auto=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_launch_fast_forward_survives_default_profile(tmp_path, capsys):
    """`launch --fast-forward` sends fast_forward_on; the auto-applied
    default profile's `fast_forward: false` used to flip it right back
    (self-clobber, proven in playthrough JSONL seq 8 -> seq 86)."""
    root = _profile_config_root(
        tmp_path, {"fast_forward": False, "text_cps": 45}
    )
    session = ProfileCaptureSession()

    cli._auto_apply_default_profile(
        _launch_args(root, fast_forward=True), session, FakeClientState()
    )

    assert session.sent_changes is not None
    assert "fast_forward" not in session.sent_changes
    assert session.sent_changes["text_cps"] == 45
    out = capsys.readouterr().out
    assert "--fast-forward" in out  # the kept flag is reported


def test_launch_auto_flag_survives_default_profile(tmp_path, capsys):
    root = _profile_config_root(
        tmp_path,
        {"auto_advance": False, "auto_advance_on_start": False, "reading_cps": 35},
    )
    session = ProfileCaptureSession()

    cli._auto_apply_default_profile(
        _launch_args(root, auto=True), session, FakeClientState()
    )

    assert session.sent_changes is not None
    assert "auto_advance" not in session.sent_changes
    assert "auto_advance_on_start" not in session.sent_changes
    assert session.sent_changes["reading_cps"] == 35


def test_default_profile_applies_fully_without_explicit_flags(tmp_path, capsys):
    root = _profile_config_root(
        tmp_path, {"fast_forward": False, "text_cps": 45}
    )
    session = ProfileCaptureSession()

    result = cli._auto_apply_default_profile(
        _launch_args(root), session, FakeClientState()
    )

    assert session.sent_changes == {"fast_forward": False, "text_cps": 45}
    assert result["profile_applied"] == "hybrid_test"


def test_default_profile_fully_overridden_still_succeeds(tmp_path, capsys):
    """A profile whose every key conflicts with explicit flags must not
    error out (or send an empty set command) — it just has nothing to do."""
    root = _profile_config_root(tmp_path, {"fast_forward": False})
    session = ProfileCaptureSession()

    cli._auto_apply_default_profile(
        _launch_args(root, fast_forward=True), session, FakeClientState()
    )

    assert session.sent_changes is None  # no 'set' command sent at all
    out = capsys.readouterr().out
    assert "✓" in out  # still reported as applied/ok, not an error



# ---------------------------------------------------------------------------
# (h) act on nothing must exit non-zero (silent-act-failure family)
# ---------------------------------------------------------------------------


def test_cmd_act_did_not_act_exits_nonzero(monkeypatch, capsys):
    """A no-pending act must reach runners as a failure: exit 1 plus an
    explicit 'Did not act' message, never a quiet prompt with exit 0."""

    class IdleSession(DeadBridgeSession):
        _bridge_up = True

        def is_up(self):
            return True

    session = IdleSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(
        cli,
        "handle_tool",
        lambda ctx, name, params: {
            "success": False,
            "ok": False,
            "error": (
                "Did not act — no choice or button was pending when the "
                "command ran (target '1')."
            ),
            "_no_pending_at_act": True,
        },
    )

    args = _wait_args(target="1", wait=True, timeout=None)
    rc = cli.cmd_act(args, FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "Did not act" in out
    assert "✗" in out


# ---------------------------------------------------------------------------
# (i) 403 on a reserved slot must render as access denied, not "no game"
# ---------------------------------------------------------------------------


class ReservedSlotSession(DeadBridgeSession):
    """Bridge is up; the slot is reserved and our token is wrong/missing."""

    _bridge_up = True
    slot_prefix = "/1"
    last_http_status = 403
    last_http_error = "Invalid or missing token for this slot."

    def is_up(self):
        return True


def test_cmd_state_reserved_slot_renders_access_denied(monkeypatch, capsys):
    session = ReservedSlotSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_state(_wait_args(), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "Access denied" in out
    assert "slot '1'" in out
    assert "token" in out
    assert "no game is connected" not in out
    assert "unreachable" not in out.lower()


def test_cmd_state_reserved_slot_json_error_code(monkeypatch, capsys):
    session = ReservedSlotSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_state(_wait_args(json=True), FakeClientState())

    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "access_denied"
    assert data["slot"] == "1"


def test_cmd_poll_reserved_slot_renders_access_denied(monkeypatch, capsys):
    session = ReservedSlotSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_poll(_wait_args(wait_timeout=0, all=False), FakeClientState())

    assert rc == 1
    assert "Access denied" in capsys.readouterr().out


def test_cmd_choices_reserved_slot_renders_access_denied(monkeypatch, capsys):
    session = ReservedSlotSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_choices(_wait_args(), FakeClientState())

    assert rc == 1
    assert "Access denied" in capsys.readouterr().out


def test_cmd_history_reserved_slot_renders_access_denied(monkeypatch, capsys):
    session = ReservedSlotSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    args = _wait_args(all=False, first=None, last=None)
    rc = cli.cmd_history(args, FakeClientState())

    assert rc == 1
    assert "Access denied" in capsys.readouterr().out


def test_cmd_screenshot_reserved_slot_renders_access_denied(monkeypatch, capsys):
    session = ReservedSlotSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_screenshot(_wait_args(output="out.png"), FakeClientState())

    assert rc == 1
    assert "Access denied" in capsys.readouterr().out


def test_perform_wait_reserved_slot_fails_out_access_denied(monkeypatch, capsys):
    """Pre-fix, a 403'd wait spun quietly until timeout (or forever with
    timeout=None).  Bounded timeout here so a regression fails fast
    instead of hanging the suite."""
    session = ReservedSlotSession()
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    rc = cli._perform_wait(session, _wait_args(), FakeClientState(), 1.0)

    assert rc == 1
    out = capsys.readouterr().out
    assert "Access denied" in out
    assert "unreachable" not in out.lower()


def test_cmd_slots_reserved_bridge_renders_access_denied(monkeypatch, capsys):
    session = ReservedSlotSession()
    session.slot_prefix = ""

    def make_client(bridge, token=None):
        return session

    class SlotDenyingClient:
        bridge_url = "http://127.0.0.1:8385"
        slot_prefix = ""
        last_http_status = 403
        last_http_error = "Bridge requires a token."

        def __init__(self, bridge, token=None):
            pass

        def list_slots(self):
            return None

    monkeypatch.setattr(cli, "BridgeClient", SlotDenyingClient)

    args = _wait_args(bridge="http://127.0.0.1:8385", reap_stale=False, token=None)
    rc = cli.cmd_slots(args, FakeClientState())

    assert rc == 1
    assert "Access denied" in capsys.readouterr().out


def test_cli_token_flag_wins_over_stored_token():
    class StoredTokenState(FakeClientState):
        def get_admin_token(self, bridge_url):
            return "stored-token"

    args = Namespace(token="explicit-token")
    assert cli._cli_token(args, StoredTokenState(), "http://b") == "explicit-token"

    args = Namespace(token=None)
    assert cli._cli_token(args, StoredTokenState(), "http://b") == "stored-token"


# ---------------------------------------------------------------------------
# (j) common flags accepted after the subcommand (--slot/--bridge/--token/...)
# ---------------------------------------------------------------------------


class TestSubcommandCommonFlags:
    """`vnflight.py state --slot 1` was an argparse error: --slot (and
    --bridge/--token/--quiet/--json) were global-only.  The subcommand
    copies use distinct dests (the 3c17347 mcp --bridge pattern) so a
    subparser default can't clobber a value the main parser already
    parsed."""

    def _parse_and_merge(self, argv):
        parser = cli.build_parser()
        args = parser.parse_args(argv)
        cli._merge_subcommand_override_flags(args)
        return args

    def test_slot_after_subcommand(self):
        args = self._parse_and_merge(["state", "--slot", "1"])
        assert args.target_slot == "1"

    def test_slot_before_subcommand_still_works(self):
        args = self._parse_and_merge(["--slot", "2", "state"])
        assert args.target_slot == "2"

    def test_subcommand_slot_wins_over_global(self):
        args = self._parse_and_merge(["--slot", "2", "state", "--slot", "7"])
        assert args.target_slot == "7"

    def test_bridge_token_quiet_json_after_subcommand(self):
        args = self._parse_and_merge([
            "act", "1",
            "--bridge", "http://127.0.0.1:9999",
            "--token", "tok",
            "--quiet",
            "--json",
        ])
        assert args.bridge == "http://127.0.0.1:9999"
        assert args.token == "tok"
        assert args.quiet is True
        assert args.json is True

    def test_globals_survive_when_subcommand_flags_absent(self):
        args = self._parse_and_merge([
            "--bridge", "http://127.0.0.1:8000",
            "--token", "global-tok",
            "wait",
        ])
        assert args.bridge == "http://127.0.0.1:8000"
        assert args.token == "global-tok"

    def test_reset_keeps_its_own_bridge_flag(self):
        # `reset --bridge` historically means "also reset the bridge
        # server" (store_true, dest bridge_reset) — must not be repurposed.
        args = self._parse_and_merge(["reset", "--bridge"])
        assert args.bridge_reset is True
        assert args.bridge == cli.DEFAULT_BRIDGE_URL

    def test_mcp_keeps_distinct_dests(self):
        args = self._parse_and_merge([
            "mcp", "--bridge", "http://127.0.0.1:9000", "--token", "t",
            "--slot", "3",
        ])
        assert args.mcp_bridge == "http://127.0.0.1:9000"
        assert args.mcp_token == "t"
        assert args.slot == "3"

    def test_stop_accepts_slot_flag(self):
        args = self._parse_and_merge(["stop", "--slot", "4"])
        assert args.target_slot == "4"


@pytest.mark.parametrize("json_mode", [False, True])
def test_cmd_state_main_menu_credits_render_once(monkeypatch, capsys, json_mode):
    class AboutSession(DeadBridgeSession):
        def state(self):
            return {"status": "running", "context": {"context": "main_menu"},
                    "screen": {"main_menu": True, "screens": ["menu"],
                               "texts": ["Music", "Composer"],
                               "buttons": [{"label": "Return", "actions": ["Return"]}]}}
    monkeypatch.setattr(cli, "_make_session", lambda args, state: AboutSession())
    assert cli.cmd_state(_wait_args(json=json_mode), FakeClientState()) == 0
    output = capsys.readouterr().out
    assert output.count("Composer") == 1
    if json_mode:
        assert json.loads(output)["screen_text"] == ["Music", "Composer"]


def test_cmd_state_persists_rendered_request_id(monkeypatch, capsys):
    """`state` output is "the last rendered numbered list" — the request id
    it showed must be persisted so a follow-up `act N` binds to it (the
    numeric staleness refusal would otherwise fire on wait → state → act)."""
    saved = {}

    class CapturingState(FakeClientState):
        def set_last_request_id(self, key, request_id):
            saved["request_id"] = request_id

    class MenuSession(DeadBridgeSession):
        _bridge_up = True
        last_request_id = None

        def is_up(self):
            return True

        def state(self):
            self.last_request_id = "menu-42"
            return {
                "status": "waiting_for_input",
                "pending_request": {
                    "type": "choice_request",
                    "id": "menu-42",
                    "choices": ["Go"],
                },
            }

    session = MenuSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)

    rc = cli.cmd_state(_wait_args(json=True), CapturingState())

    assert rc == 0
    assert saved.get("request_id") == "menu-42"


def _read_json_objects(text):
    """Decode a stdout blob holding several back-to-back json.dumps() calls.

    `_output()` prints one indent=2 JSON document per call with no
    delimiter between them, so a plain json.loads() on the whole blob
    fails once more than one _output(..., data=...) call has fired
    (e.g. the launch receipt followed by the --wait outcome).
    """
    decoder = json.JSONDecoder()
    text = text.strip()
    objs = []
    idx = 0
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        objs.append(obj)
        idx = end
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
    return objs


def test_cmd_launch_wait_reports_connect_and_first_choice_timing(
    monkeypatch, capsys,
):
    """A slow first boot (freshly cleared .rpyc/.rpyb cache) that connects
    WITHIN the caller's --timeout must report success, and the JSON result
    must carry connected_after_s (from launch_game's connect wait) and a
    trailing first_choice_after_s covering the --wait phase that follows.

    Regression for: `launch echoes_of_tomorrow_r7 --wait --timeout 150`
    reporting "Game did not connect to the bridge in time." while the game
    had, in fact, connected fine (see lib.launch_game's game_dir-alias
    slot-matching fix) -- this pins the timing telemetry the diagnosis
    needed and didn't have.
    """

    def fake_launch_game(*args, **kwargs):
        diagnostics = kwargs.get("diagnostics")
        if diagnostics is not None:
            # Simulated slow recompile: connected late, but inside timeout.
            diagnostics["connected_after_s"] = 118.4
        return True, "Launched 'echoes_of_tomorrow_r7' (slot 9)", 9

    monkeypatch.setattr(cli, "launch_game", fake_launch_game)
    monkeypatch.setattr(cli, "_make_session", lambda *a: object())
    monkeypatch.setattr(cli, "_perform_wait", lambda *a: 0)

    args = Namespace(
        game="echoes_of_tomorrow_r7", bridge="http://bridge", games_dir=None,
        fast_forward=False, auto=False, timeout=150, save_slot=None,
        quiet=True, json=True, wait=True, target_slot=None, token=None,
        # Skip real default-profile config discovery -- irrelevant here and
        # would otherwise hit this repo's own vnflight.json.
        defer_default_profile=True,
    )

    rc = cli.cmd_launch(args, FakeClientState())
    objs = _read_json_objects(capsys.readouterr().out)

    assert rc == 0
    assert len(objs) == 2
    receipt, timing = objs
    assert receipt["success"] is True
    assert receipt["slot_id"] == 9
    assert receipt["connected_after_s"] == 118.4
    assert isinstance(timing["first_choice_after_s"], (int, float))


def test_cmd_launch_registration_rejection_failure_surfaces_reason(
    monkeypatch, capsys,
):
    """A recorded shim registration rejection is a real failure (unlike a
    404 from /registration-rejection, which means "no rejection on file"
    and must NOT fail the launch) -- and it must carry the reason text plus
    how long the connect wait ran before giving up."""

    def fake_launch_game(*args, **kwargs):
        diagnostics = kwargs.get("diagnostics")
        if diagnostics is not None:
            diagnostics["connect_failed_after_s"] = 3.2
        return (
            False,
            "Game shim registration failed: shim protocol mismatch",
            None,
        )

    monkeypatch.setattr(cli, "launch_game", fake_launch_game)

    args = Namespace(
        game="echoes_of_tomorrow_r7", bridge="http://bridge", games_dir=None,
        fast_forward=False, auto=False, timeout=150, save_slot=None,
        quiet=True, json=True, wait=False, target_slot=None, token=None,
    )

    rc = cli.cmd_launch(args, FakeClientState())
    (result,) = _read_json_objects(capsys.readouterr().out)

    assert rc == 1
    assert result["success"] is False
    assert "shim registration failed" in result["error"]
    assert "shim protocol mismatch" in result["error"]
    assert result["connect_failed_after_s"] == 3.2


def test_cmd_launch_names_which_phase_timed_out(monkeypatch, capsys):
    """A connect-phase timeout and a later wait/first-choice-phase timeout
    must be distinguishable in the JSON output -- they are different
    failures needing different fixes (a stale-cache slow boot vs. a stuck
    story)."""

    connect_args = Namespace(
        game="echoes_of_tomorrow_r7", bridge="http://bridge", games_dir=None,
        fast_forward=False, auto=False, timeout=150, save_slot=None,
        quiet=True, json=True, wait=True, target_slot=None, token=None,
    )

    def failing_connect(*args, **kwargs):
        diagnostics = kwargs.get("diagnostics")
        if diagnostics is not None:
            diagnostics["connect_failed_after_s"] = 150.0
        return False, "Game did not connect to the bridge in time.", None

    monkeypatch.setattr(cli, "launch_game", failing_connect)
    monkeypatch.setattr(
        cli, "_perform_wait",
        lambda *a: pytest.fail("must not wait for a choice: never connected"),
    )

    rc = cli.cmd_launch(connect_args, FakeClientState())
    (connect_result,) = _read_json_objects(capsys.readouterr().out)

    assert rc == 1
    assert connect_result["success"] is False
    assert "did not connect" in connect_result["error"]
    assert connect_result["connect_failed_after_s"] == 150.0
    assert "status" not in connect_result  # not the wait-phase shape

    # A launch that DID connect, but then never saw a first choice, is a
    # different failure: success stays True and a separate "timeout"
    # status object (from _perform_wait) follows the launch receipt.
    wait_args = Namespace(
        game="echoes_of_tomorrow_r7", bridge="http://bridge", games_dir=None,
        fast_forward=False, auto=False, timeout=150, save_slot=None,
        quiet=True, json=True, wait=True, target_slot=None, token=None,
        defer_default_profile=True,
    )

    def succeeding_connect(*args, **kwargs):
        diagnostics = kwargs.get("diagnostics")
        if diagnostics is not None:
            diagnostics["connected_after_s"] = 2.1
        return True, "Launched 'echoes_of_tomorrow_r7' (slot 9)", 9

    def timed_out_wait(session, args, client_state, timeout):
        cli._output(
            args, data={"status": "timeout", "message": "Wait timed out."},
        )
        return 0

    monkeypatch.setattr(cli, "launch_game", succeeding_connect)
    monkeypatch.setattr(cli, "_make_session", lambda *a: object())
    monkeypatch.setattr(cli, "_perform_wait", timed_out_wait)

    rc = cli.cmd_launch(wait_args, FakeClientState())
    receipt, wait_result, timing = _read_json_objects(capsys.readouterr().out)

    assert rc == 0
    assert receipt["success"] is True
    assert receipt["connected_after_s"] == 2.1
    assert wait_result["status"] == "timeout"
    assert isinstance(timing["first_choice_after_s"], (int, float))


# ---------------------------------------------------------------------------
# (j) navigation verbs report the shim's result, not the submission
# ---------------------------------------------------------------------------


class _LiveIdleSession(DeadBridgeSession):
    _bridge_up = True

    def is_up(self):
        return True


def _stub_handle_tool(monkeypatch, result):
    calls = []

    def fake(ctx, name, params):
        calls.append((name, dict(params)))
        return dict(result)

    monkeypatch.setattr(cli, "handle_tool", fake)
    monkeypatch.setattr(cli, "_make_session", lambda args, state: _LiveIdleSession())
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: None)
    return calls


_REWIND_REFUSAL = {
    "type": "command_result",
    "command": "rewind",
    "success": False,
    "ok": False,
    "reason": "rollback_disabled_by_game",
    "error": (
        "Rollback is disabled by this game (config.rollback_enabled is "
        "False) -- rewind is never available in this playthrough."
    ),
    "nonce": "abc",
    "command_nonce": "abc",
}


class _RecordingClientState(FakeClientState):
    def __init__(self, deferred=None):
        self.cursors = {}
        self.deferred = {}
        self._restore = list(deferred or [])

    def set_cursor(self, key, cursor):
        self.cursors[key] = cursor

    def get_deferred_events(self, key):
        return list(self._restore)

    def set_deferred_events(self, key, events):
        self.deferred[key] = list(events)


class _StashSession:
    bridge_url = "http://127.0.0.1:8385"
    slot_prefix = "/slot-1"
    last_request_id = None

    def __init__(self, cursor, stash):
        self.cursor = cursor
        self._prefetched_events = list(stash)


_DEFERRED_ROWS = [
    {"type": "narration", "text": "The road narrows.", "_seq": 41},
    {"type": "dialogue", "who": "A", "what": "Hm.", "_seq": 43},
]


def test_save_session_persists_the_unconsumed_stash_and_the_real_cursor():
    """Astra P2: rewind/back/resync (and save/set/cmd) wait for a
    nonce-matched command_result; the client stashes the narration it
    polled past and advances the cursor. Without --wait the CLI exits and
    the stash died with it. It now rides in the session file; the cursor
    is NOT rewound (the stash is the delivery, a rewind would double it)."""
    state = _RecordingClientState()
    session = _StashSession(cursor=45, stash=_DEFERRED_ROWS)

    cli._save_session(session, _wait_args(json=False, wait=False, timeout=None), state)

    key = "http://127.0.0.1:8385/slot-1"
    assert state.cursors[key] == 45
    assert state.deferred[key] == _DEFERRED_ROWS


def test_save_session_persists_an_empty_stash_after_it_was_drained():
    state = _RecordingClientState()
    session = _StashSession(cursor=45, stash=[])
    cli._save_session(session, _wait_args(json=False, wait=False, timeout=None), state)
    assert state.deferred["http://127.0.0.1:8385/slot-1"] == []


def test_make_session_restores_deferred_rows_into_the_stash(monkeypatch):
    from vnflight.client import BridgeClient

    monkeypatch.setattr(BridgeClient, "auto_select_slot", lambda self, hint=None: True)
    state = _RecordingClientState(deferred=_DEFERRED_ROWS)
    args = _wait_args(json=False, wait=False, timeout=None)
    args.bridge = "http://127.0.0.1:8385"

    session = cli._make_session(args, state)

    assert session._prefetched_events == _DEFERRED_ROWS


def _fake_state_payload(rows, *, action_id=7, pending=None, counter=None):
    seqs = [r["_seq"] for r in rows]
    return 200, {
        "transcript": rows,
        "event_counter": counter if counter is not None else (max(seqs) if seqs else 0),
        "reset_generation": 3,
        "pending_request": pending,
        "status": "waiting_for_input" if pending else "running",
    }


def test_act_delivered_rows_are_not_reprinted_by_the_next_invocation(monkeypatch, tmp_path):
    """Release run, CLI agent: `act 2 --wait` printed five lines and handed
    back "Story is still arriving"; the next `wait` printed the same five
    again.  The act's scoped receipt drain records the rows it delivered in
    the client's in-memory ledger without moving the cursor; the ordinary
    poll filters by that ledger.  The CLI must carry the ledger across
    processes the way it carries the stash."""
    from vnflight.client import BridgeClient
    from vnflight.lib import ClientState

    five = [
        {"type": "narration", "text": f"line {i}", "_seq": 40 + i, "action_id": 7}
        for i in range(1, 6)
    ]
    args = _wait_args(json=False, wait=False, timeout=None)
    args.bridge = "http://127.0.0.1:8385"
    monkeypatch.setattr(BridgeClient, "auto_select_slot", lambda self, hint=None: True)

    # Process 1: the act's scoped drain delivered the five rows (ledger),
    # cursor still at 40.
    state = ClientState(str(tmp_path))
    first = cli._make_session(args, state)
    first.cursor = 40
    first._observe_action_delivery_generation(3)
    first._record_delivered_action_events([(7, r["_seq"]) for r in five])
    cli._save_session(first, args, state)

    # Process 2: fresh state, fresh session; the bridge still serves the
    # five rows after cursor 40 plus one new row.
    state2 = ClientState(str(tmp_path))
    second = cli._make_session(args, state2)
    assert second.cursor == 40
    served = five + [{"type": "narration", "text": "new line", "_seq": 46, "action_id": 7}]
    monkeypatch.setattr(BridgeClient, "_get",
                        lambda self, path, params=None, timeout=3.0: _fake_state_payload(served))
    delivered = second.poll(timeout=0)

    assert [e["text"] for e in delivered] == ["new line"]


def test_save_session_without_a_ledger_persists_nothing_harmful(monkeypatch):
    state = _RecordingClientState()
    session = _StashSession(cursor=45, stash=[])
    cli._save_session(session, _wait_args(json=False, wait=False, timeout=None), state)
    # A minimal double has no ledger accessor and no data dict: no error.
    assert state.cursors["http://127.0.0.1:8385/slot-1"] == 45


def test_explicit_slot_attach_fast_forwards_the_cursor_but_keeps_the_stash(monkeypatch):
    """The reviewer's gap: `--slot game wait` attaches at the active
    decision (cursor 40 -> 45). A cursor rewind is lost there; the stash
    is not, and poll(include_prefetched=True) drains it first."""
    from vnflight.client import BridgeClient

    session = BridgeClient("http://127.0.0.1:8385")
    session.cursor = 40
    session._prefetched_events = list(_DEFERRED_ROWS)

    def fast_forward(self, *a, **k):
        self.cursor = 45
        return True

    monkeypatch.setattr(BridgeClient, "mark_current_events_seen", fast_forward)
    assert session.attach_to_running_slot(warn=None) is True
    assert session.cursor == 45
    assert session._prefetched_events == _DEFERRED_ROWS


def test_cmd_rewind_reports_shim_refusal_and_exits_nonzero(monkeypatch, capsys):
    """Roadwarden smoke (Sep 7): `rewind` printed '✓ Rewind queued.' with
    exit 0 while the bridge recorded success=false rollback_disabled_by_game.
    The CLI must wait for the result the way MCP does and fail loudly."""
    calls = _stub_handle_tool(monkeypatch, _REWIND_REFUSAL)

    rc = cli.cmd_rewind(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 1
    assert calls == [("rewind", {})]
    out = capsys.readouterr().out
    assert "✗ Rewind failed: Rollback is disabled by this game" in out
    assert "queued" not in out
    assert "✓" not in out


def test_cmd_rewind_refusal_json_carries_reason(monkeypatch, capsys):
    _stub_handle_tool(monkeypatch, _REWIND_REFUSAL)

    rc = cli.cmd_rewind(_wait_args(json=True, wait=False, timeout=None), FakeClientState())

    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is False
    assert data["command"] == "rewind"
    assert data["reason"] == "rollback_disabled_by_game"
    assert "Rollback is disabled" in data["error"]


def test_cmd_rewind_unconfirmed_result_exits_nonzero(monkeypatch, capsys):
    """Accepted by the bridge but no command_result: not a success."""
    _stub_handle_tool(monkeypatch, {
        "ok": False,
        "success": False,
        "confirmed": False,
        "acceptance_unknown": True,
        "reason": "command_result_timeout_after_submission",
        "error": "Command 'rewind' was submitted but not confirmed by the game",
    })

    rc = cli.cmd_rewind(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 1
    assert "not confirmed" in capsys.readouterr().out


def test_cmd_rewind_applied_exits_zero(monkeypatch, capsys):
    _stub_handle_tool(monkeypatch, {
        "type": "command_result",
        "command": "rewind",
        "success": True,
        "nonce": "abc",
        "_private": "stripped",
    })

    rc = cli.cmd_rewind(_wait_args(json=True, wait=False, timeout=None), FakeClientState())

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is True
    assert data["command"] == "rewind"
    assert "_private" not in data


@pytest.mark.parametrize("cmd, name, description", [
    (cli.cmd_back, "back", "Back (Return)"),
    (cli.cmd_advance, "advance", "Advance"),
    (cli.cmd_replay, "replay", "Replay"),
])
def test_navigation_verbs_route_through_shared_handler(
    monkeypatch, capsys, cmd, name, description,
):
    calls = _stub_handle_tool(monkeypatch, {
        "success": False, "ok": False,
        "reason": "nothing_to_close",
        "error": "Nothing to close.",
    })

    rc = cmd(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 1
    assert calls == [(name, {})]
    out = capsys.readouterr().out
    assert f"✗ {description} failed: Nothing to close." in out


def test_cmd_advance_refused_by_new_menu_shows_the_decision(monkeypatch, capsys):
    """_recover_advance_menu_race attaches the menu that refused the advance;
    the CLI must show it instead of a bare error."""
    _stub_handle_tool(monkeypatch, {
        "success": False, "ok": False,
        "error": "Cannot advance while a choice is active.",
        "pending": "--- CHOICE REQUIRED ---\n  1: Stay\n  2: Leave",
        "status": "waiting_for_input",
    })

    rc = cli.cmd_advance(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "Cannot advance while a choice is active." in out
    assert "1: Stay" in out


def test_cmd_resync_reports_confirmed_failure(monkeypatch, capsys):
    """resync printed 'succeeded' on submission; the shim answers
    success=False 'No active menu' when there is nothing to re-push."""

    class NoMenuSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            result = {
                "type": "command_result", "command": command,
                "success": False, "error": "No active menu",
                "nonce": self.sent_nonce,
            }
            if match is not None and not match(result):
                return None
            return result

    session = NoMenuSession()
    monkeypatch.setattr(cli, "_make_session", lambda args, state: session)
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: None)

    rc = cli.cmd_resync(_wait_args(), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "Resync failed: No active menu" in out
    assert "succeeded" not in out
    assert session.sent_nonce


def test_cmd_resync_reports_confirmed_success(monkeypatch, capsys):
    class SyncedSession(ProfileCaptureSession):
        def _wait_command_result(self, command, timeout=5.0, after_seq=None, match=None):
            result = {
                "type": "command_result", "command": command,
                "success": True, "message": "Already in sync",
                "nonce": self.sent_nonce,
            }
            if match is not None and not match(result):
                return None
            return result

    monkeypatch.setattr(cli, "_make_session", lambda args, state: SyncedSession())
    monkeypatch.setattr(cli, "_save_session", lambda s, args, state: None)

    rc = cli.cmd_resync(_wait_args(json=True), FakeClientState())

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is True
    assert data["message"] == "Already in sync"


def test_cmd_act_rejected_without_error_key_exits_nonzero(monkeypatch, capsys):
    """Live repro (Sep 7): with no game connected the act result was
    {ok: false, success: false, transaction_state: "rejected", reason:
    "state_unavailable"} and the CLI printed '✓ Acted (action): Start'."""
    rejected = {
        "action_nonce": "n1",
        "transaction_state": "rejected",
        "pending": False,
        "reason": "state_unavailable",
        "ok": False,
        "success": False,
        "transaction_pending": False,
    }
    _stub_handle_tool(monkeypatch, rejected)

    rc = cli.cmd_act(_wait_args(target="Start", wait=True, timeout=None), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "✗ Did not act: state_unavailable" in out
    assert "Acted" not in out

    _stub_handle_tool(monkeypatch, rejected)
    rc = cli.cmd_act(
        _wait_args(target="Start", wait=True, timeout=None, json=True),
        FakeClientState(),
    )
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is False
    assert data["transaction_state"] == "rejected"
    assert data["reason"] == "state_unavailable"


def test_cmd_act_success_still_exits_zero(monkeypatch, capsys):
    _stub_handle_tool(monkeypatch, {
        "ok": True, "success": True, "resolved_as": "button",
        "label": "Start", "transaction_state": "applied",
    })

    rc = cli.cmd_act(_wait_args(target="Start", wait=False, timeout=None), FakeClientState())

    assert rc == 0
    assert "✓ Acted (button): Start" in capsys.readouterr().out


def test_cmd_back_all_reports_closed_count(monkeypatch, capsys):
    calls = _stub_handle_tool(monkeypatch, {
        "ok": True, "success": True, "closed": 2, "stopped_by": "world",
        "text": "back_all: closed 2 screens.\n\nOn Screen:\n  The lab",
        "_private": "stripped",
    })

    rc = cli.cmd_back_all(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 0
    assert calls == [("back_all", {})]
    out = capsys.readouterr().out
    assert "✓ Closed 2 overlays." in out
    assert "The lab" in out
    assert "back_all: closed" not in out  # summary line not repeated


def test_cmd_back_all_json_carries_public_result(monkeypatch, capsys):
    _stub_handle_tool(monkeypatch, {
        "ok": True, "success": True, "closed": 1, "stopped_by": "world",
        "text": "back_all: closed 1 screen.", "_private": "stripped",
    })

    rc = cli.cmd_back_all(_wait_args(json=True, wait=False, timeout=None), FakeClientState())

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is True
    assert data["command"] == "back_all"
    assert data["closed"] == 1
    assert "_private" not in data


def test_cmd_back_all_refusal_exits_nonzero(monkeypatch, capsys):
    _stub_handle_tool(monkeypatch, {
        "ok": False, "success": False, "closed": 0, "stopped_by": "refusal",
        "error": "Nothing to close.", "reason": "nothing_to_close",
        "text": "back_all: closed 0 screens. Stopped by a refusal.",
    })

    rc = cli.cmd_back_all(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 1
    out = capsys.readouterr().out
    assert "✗ back_all failed: Nothing to close." in out
    assert "✓" not in out

    _stub_handle_tool(monkeypatch, {
        "ok": False, "success": False, "closed": 1, "stopped_by": "refusal",
        "error": "Cannot close a called screen.",
    })
    rc = cli.cmd_back_all(_wait_args(json=True, wait=False, timeout=None), FakeClientState())
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is False
    assert data["command"] == "back_all"
    assert data["closed"] == 1
    assert data["error"] == "Cannot close a called screen."


def test_cmd_back_all_shows_max_steps_warning(monkeypatch, capsys):
    _stub_handle_tool(monkeypatch, {
        "ok": True, "success": True, "closed": 8, "stopped_by": "max_steps",
        "warning": "back_all stopped after 8 steps with screens still showing.",
    })

    rc = cli.cmd_back_all(_wait_args(wait=False, timeout=None), FakeClientState())

    assert rc == 0
    out = capsys.readouterr().out
    assert "✓ Closed 8 overlays. back_all stopped after 8 steps" in out


def test_cli_parser_exposes_back_all():
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        pytest.skip("no build_parser helper")
    ns = parser.parse_args(["back_all", "--wait", "--timeout", "5"])
    assert ns.command == "back_all"
    assert ns.wait is True
    assert ns.timeout == 5.0


def _make_manifest_root(tmp_path, tamper=False):
    """A project root whose game takes its adapters from a mods manifest."""
    import hashlib

    root = tmp_path / "proj"
    root.mkdir()
    (root / "vnflight.rpy").write_text("# shim\n", encoding="utf-8")
    repo = tmp_path / "modsrepo"
    repo.mkdir()
    (repo / "thing.rpy").write_text("# mod\n", encoding="utf-8")
    digest = hashlib.sha256((repo / "thing.rpy").read_bytes()).hexdigest()
    (repo / "manifest.json").write_text(json.dumps({
        "manifest_version": 1,
        "games": {"mygame": {"name": "My Game", "mods": [
            {"file": "thing.rpy", "target": "vnf_thing.rpy", "sha256": digest}]}},
    }), encoding="utf-8")
    if tamper:
        (repo / "thing.rpy").write_text("# tampered\n", encoding="utf-8")
    game_dir = root / "mygame"
    (game_dir / "game").mkdir(parents=True)
    (root / "vnflight.json").write_text(json.dumps({
        "mods_manifest": str(repo / "manifest.json"),
        "games": {"mygame": {"game_dir": "mygame"}},
    }), encoding="utf-8")
    return root, game_dir


def test_install_shim_installs_adapters_from_the_mods_manifest(tmp_path, capsys):
    root, game_dir = _make_manifest_root(tmp_path)

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 0
    assert (game_dir / "game" / "vnf_thing.rpy").read_text(encoding="utf-8") == "# mod\n"
    out = capsys.readouterr().out
    assert "Adapters from manifest" in out
    assert "Installed shim + 1 mod for 'mygame'" in out


def test_install_shim_refuses_a_manifest_adapter_with_a_wrong_hash(tmp_path, capsys):
    root, game_dir = _make_manifest_root(tmp_path, tamper=True)

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc != 0
    assert not (game_dir / "game" / "vnf_thing.rpy").exists()
    out = capsys.readouterr().out
    assert "does not match the manifest" in out


def test_cmd_games_shows_where_adapters_come_from(tmp_path, capsys):
    root, _ = _make_manifest_root(tmp_path)

    rc = cli.cmd_games(Namespace(games_dir=str(root), json=True, quiet=False), FakeClientState())

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["games"][0]["mods"] == "1 adapter (manifest)"


def test_confirm_treats_closed_stdin_as_no(monkeypatch, capsys):
    """install-shim with stdin closed used to die with
    'EOF when reading a line' instead of answering No."""
    import builtins

    def eof(prompt):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    assert cli._confirm("Confirm? [y/N]: ") is False

    monkeypatch.setattr(builtins, "input", lambda prompt: "y")
    assert cli._confirm("Confirm? [y/N]: ") is True
    monkeypatch.setattr(builtins, "input", lambda prompt: "")
    assert cli._confirm("Confirm? [y/N]: ") is False


def test_install_shim_prompt_no_longer_uses_the_legacy_name():
    import inspect

    src = inspect.getsource(cli.cmd_install_shim)
    assert "LLM Player" not in src
    assert "Install vnflight" in src


# -- From-zero install findings (2026-09-14) --------------------------------

_NO_MODS_CONFIG = {"games": {"mygame": {"game_dir": "mygame", "mods": []}}}


def test_install_shim_refuses_a_target_without_a_game_dir(tmp_path, capsys):
    """--yes used to auto-answer the old "create game/?" prompt, so a wrong
    path silently grew a game/ directory (a from-zero run created
    mods/game/ that way).  Creating it now takes an explicit flag."""
    root, game_dir = _make_install_root(tmp_path, config=_NO_MODS_CONFIG)
    (game_dir / "game").rmdir()

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 1
    assert not (game_dir / "game").exists()
    out = capsys.readouterr().out
    assert "no 'game/' subdirectory" in out
    assert "--create-game-dir" in out


def test_install_shim_honours_always_on_true_in_the_config(tmp_path, capsys):
    """`"always_on": true` is the documented spelling; only
    install_shim_flags used to be read."""
    config = {"games": {"mygame": {"game_dir": "mygame", "mods": [], "always_on": True}}}
    root, game_dir = _make_install_root(tmp_path, config=config)
    (root / "vnflight.rpy").write_text(
        'class P:\n    def __init__(self):\n'
        '        self.enabled = os.environ.get("VNFLIGHT_ENABLED") == "1"\n',
        encoding="utf-8")

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 0
    installed = (game_dir / "game" / "vnflight.rpy").read_text(encoding="utf-8")
    assert "self.enabled = True  # patched by install-shim --always-on" in installed
    assert "always enabled" in capsys.readouterr().out


def test_install_shim_creates_the_game_dir_only_when_asked(tmp_path, capsys):
    root, game_dir = _make_install_root(tmp_path, config=_NO_MODS_CONFIG)
    (game_dir / "game").rmdir()

    rc = cli.cmd_install_shim(
        _install_args("mygame", root, create_game_dir=True), FakeClientState())

    assert rc == 0
    assert (game_dir / "game" / "vnflight.rpy").exists()
    assert "creating it (--create-game-dir)" in capsys.readouterr().out


def test_install_shim_installs_nothing_when_an_adapter_fails(tmp_path, capsys):
    """A bad adapter used to leave "shim + the adapters that verified"
    behind; the docs promise the whole install is refused."""
    root, game_dir = _make_manifest_root(tmp_path, tamper=True)

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 1
    assert not (game_dir / "game" / "vnflight.rpy").exists()
    assert not (game_dir / "game" / "vnf_thing.rpy").exists()
    out = capsys.readouterr().out
    assert "does not match the manifest" in out
    assert "Nothing installed for 'mygame'" in out
    assert "--no-mods" in out


def test_cmd_games_says_what_the_adapter_problem_is(tmp_path, capsys):
    root, _ = _make_manifest_root(tmp_path, tamper=True)

    rc = cli.cmd_games(Namespace(games_dir=str(root), json=True, quiet=False), FakeClientState())

    assert rc == 0
    mods = json.loads(capsys.readouterr().out)["games"][0]["mods"]
    assert "1 problem(s): " in mods
    assert "does not match the manifest" in mods


def test_save_and_load_take_an_optional_save_slot():
    """docs: `save [slot]` / `load [slot]`; the parser required it."""
    parser = cli.build_parser()
    assert parser.parse_args(["save"]).slot is None
    assert parser.parse_args(["save", "1-2"]).slot == "1-2"
    assert parser.parse_args(["load"]).slot is None
    assert parser.parse_args(["load", "auto-1"]).slot == "auto-1"
    assert "save slot" in parser._subparsers._group_actions[0].choices["save"].format_help().lower()


def test_install_shim_has_a_create_game_dir_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["install-shim", "g", "--create-game-dir"])
    assert args.create_game_dir is True
    assert parser.parse_args(["install-shim", "g"]).create_game_dir is False


# -- fetch-mods with the snapshot pinned in the config ---------------------

_PINNED = {
    "url": "https://raw.githubusercontent.com/vnflight/mods/abc123/manifest.json",
    "sha256": "A" * 64,
}


def _fetch_root(tmp_path, snapshot=_PINNED):
    root = tmp_path / "proj"
    root.mkdir()
    config = {"games": {}}
    if snapshot is not None:
        config["mods_snapshot"] = dict(snapshot, _comment="pinned")
    (root / "vnflight.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def _fetch_args(root, url=None, sha256=None, yes=True, output="out"):
    return Namespace(url=url, sha256=sha256, output=output, games_dir=str(root),
                     yes=yes, json=False, quiet=False)


def _record_fetch(monkeypatch, tmp_path):
    calls = []

    def fake_fetch(url, expected_sha256, destination):
        calls.append((url, expected_sha256, str(destination)))
        return tmp_path / "snap" / "manifest.json"

    monkeypatch.setattr(cli, "fetch_mods_manifest", fake_fetch)
    return calls


def test_fetch_mods_uses_the_pinned_snapshot_and_asks_first(tmp_path, monkeypatch, capsys):
    root = _fetch_root(tmp_path)
    calls = _record_fetch(monkeypatch, tmp_path)
    prompts = []
    monkeypatch.setattr(cli, "_confirm", lambda prompt: (prompts.append(prompt), True)[1])

    rc = cli.cmd_fetch_mods(_fetch_args(root, yes=False), FakeClientState())

    assert rc == 0
    assert calls == [(_PINNED["url"], "a" * 64, "out")]
    assert len(prompts) == 1
    captured = capsys.readouterr()
    assert _PINNED["url"] in captured.out and "a" * 64 in captured.out
    assert json.loads(captured.out.splitlines()[-1])["mods_manifest"].endswith("manifest.json")
    assert '"mods_manifest"' in captured.err  # the how-to-wire hint, off stdout


def test_fetch_mods_yes_skips_the_question(tmp_path, monkeypatch, capsys):
    root = _fetch_root(tmp_path)
    calls = _record_fetch(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: pytest.fail("asked despite --yes"))

    assert cli.cmd_fetch_mods(_fetch_args(root, yes=True), FakeClientState()) == 0
    assert len(calls) == 1


def test_fetch_mods_declined_prompt_downloads_nothing(tmp_path, monkeypatch, capsys):
    root = _fetch_root(tmp_path)
    calls = _record_fetch(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: False)

    assert cli.cmd_fetch_mods(_fetch_args(root, yes=False), FakeClientState()) == 1
    assert calls == []
    assert "Aborted" in capsys.readouterr().out


def test_fetch_mods_explicit_arguments_win_over_the_pin(tmp_path, monkeypatch):
    root = _fetch_root(tmp_path)
    calls = _record_fetch(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: pytest.fail("explicit args never prompt"))

    rc = cli.cmd_fetch_mods(
        _fetch_args(root, url="https://example.org/m.json", sha256="B" * 64, yes=False),
        FakeClientState())

    assert rc == 0
    assert calls == [("https://example.org/m.json", "B" * 64, "out")]


def test_fetch_mods_refuses_without_arguments_or_a_pin(tmp_path, monkeypatch, capsys):
    root = _fetch_root(tmp_path, snapshot=None)
    calls = _record_fetch(monkeypatch, tmp_path)

    assert cli.cmd_fetch_mods(_fetch_args(root), FakeClientState()) == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "mods_snapshot" in err and "--sha256" in err


def test_fetch_mods_refuses_a_lone_url_or_digest(tmp_path, monkeypatch, capsys):
    root = _fetch_root(tmp_path)
    calls = _record_fetch(monkeypatch, tmp_path)

    assert cli.cmd_fetch_mods(_fetch_args(root, url="https://example.org/m.json"), FakeClientState()) == 1
    assert cli.cmd_fetch_mods(_fetch_args(root, sha256="B" * 64), FakeClientState()) == 1
    assert calls == []


def test_fetch_mods_parser_accepts_no_url():
    parser = cli.build_parser()
    args = parser.parse_args(["fetch-mods", "--output", "mods"])
    assert args.url is None and args.sha256 is None and args.output == "mods"


def test_template_pins_a_snapshot_and_games_tolerates_it(tmp_path, capsys):
    """The template's top-level mods_snapshot object is not a game and must
    not upset the listing or the loader."""
    from pathlib import Path
    from vnflight import lib

    template = json.loads(
        (Path(cli.__file__).resolve().parents[2] / "vnflight.default.json")
        .read_text(encoding="utf-8"))
    url, digest = lib.pinned_mods_snapshot(template)
    assert url.startswith("https://raw.githubusercontent.com/vnflight/mods/")
    assert len(digest) == 64
    root = tmp_path / "proj"
    root.mkdir()
    (root / "vnflight.json").write_text(json.dumps({
        "mods_snapshot": template["mods_snapshot"],
        "games": {"g": {"name": "G", "launch": "renpy.exe g"}},
    }), encoding="utf-8")

    assert [g["id"] for g in lib.discover_games(str(root))] == ["g"]
    assert cli.cmd_games(Namespace(games_dir=str(root), json=True, quiet=False), FakeClientState()) == 0
    assert json.loads(capsys.readouterr().out)["games"][0]["id"] == "g"
    assert lib.pinned_mods_snapshot({"mods_snapshot": {"url": 1}}) is None
    assert lib.pinned_mods_snapshot({}) is None


def _registered_verbs():
    import argparse

    parser = cli.build_parser()
    sub = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)][0]
    return list(sub.choices)


def test_every_verb_is_in_exactly_one_help_group():
    """A verb registered but missing from COMMAND_GROUPS (or listed there
    but never registered) is a --help lie; fail loudly."""
    listed = [verb for _title, verbs in cli.COMMAND_GROUPS for verb in verbs]
    assert len(listed) == len(set(listed)), "a verb appears in two groups"
    assert sorted(listed) == sorted(_registered_verbs())
    assert [title for title, _ in cli.COMMAND_GROUPS] == [
        "Setup", "Playing", "Navigation and extras", "Diagnostics"]


def test_top_level_help_is_grouped(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: vnflight.py [global options] <command> ...")
    for section in ("Connection:", "Output:", "Behaviour:", "commands:",
                    "  Setup:", "  Playing:", "  Navigation and extras:",
                    "  Diagnostics:"):
        assert section in out, section
    assert out.index("  Setup:") < out.index("  Playing:") < \
        out.index("  Navigation and extras:") < out.index("  Diagnostics:")
    for verb in _registered_verbs():
        assert f"\n    {verb:<18}" in out, verb
    assert "{games,slots" not in out          # no 33-verb soup
    assert "positional arguments" not in out
    assert "Ungrouped" not in out


def test_cli_reference_lists_every_verb_once_under_the_help_groups():
    """docs/CLI.md mirrors --help: same group titles in the same order, and
    every registered verb appears exactly once as a backticked entry."""
    import re
    from pathlib import Path

    text = (Path(cli.__file__).resolve().parents[2] / "docs" / "CLI.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings == ["Global options"] + [title for title, _ in cli.COMMAND_GROUPS]
    entries = re.findall(r"^- `([A-Za-z_-]+)", text, re.M)
    verbs = [verb for _title, verbs in cli.COMMAND_GROUPS for verb in verbs]
    for verb in verbs:
        assert entries.count(verb) == 1, verb
    assert not re.search(r"^\|", text, re.M), "no tables in CLI.md"


def test_unknown_command_still_lists_the_valid_ones(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["frobnicate"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'frobnicate'" in err
    # argparse quotes the choices on some Python versions and not others.
    choose = err.split("choose from", 1)[1]
    assert "games" in choose and "bridge" in choose and "save-scan" in choose


def test_games_on_the_untouched_template_exits_zero_with_a_hint(tmp_path, capsys):
    """README's first step is `games` on a fresh copy of the template; it
    used to print "No games found." and exit 1."""
    from pathlib import Path
    import shutil

    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(Path(cli.__file__).resolve().parents[2] / "vnflight.default.json",
                root / "vnflight.json")

    rc = cli.cmd_games(Namespace(games_dir=str(root), json=False, quiet=False), FakeClientState())

    assert rc == 0
    captured = capsys.readouterr()
    assert 'add one under "games" in vnflight.json' in captured.out
    # The template's placeholder manifest path is called out by key name.
    assert "mods_manifest" in captured.err and "path/to/mods/manifest.json" in captured.err

    rc = cli.cmd_games(Namespace(games_dir=str(root), json=True, quiet=False), FakeClientState())
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["games"] == []


def test_games_warns_once_about_a_missing_manifest_and_not_about_a_real_one(tmp_path, capsys):
    root, _ = _make_manifest_root(tmp_path)
    assert cli.cmd_games(Namespace(games_dir=str(root), json=True, quiet=False), FakeClientState()) == 0
    assert "Warning" not in capsys.readouterr().err

    config = json.loads((root / "vnflight.json").read_text(encoding="utf-8"))
    config["mods_manifest"] = "nowhere/manifest.json"
    (root / "vnflight.json").write_text(json.dumps(config), encoding="utf-8")
    assert cli.cmd_games(Namespace(games_dir=str(root), json=True, quiet=False), FakeClientState()) == 0
    err = capsys.readouterr().err
    assert err.count("Warning") == 1 and "mods_manifest: 'nowhere/manifest.json' does not exist" in err


def test_games_without_any_config_still_fails(tmp_path, capsys):
    root = tmp_path / "empty"
    root.mkdir()
    assert cli.cmd_games(Namespace(games_dir=str(root), json=False, quiet=False), FakeClientState()) == 1
    assert "No games found" in capsys.readouterr().out


def test_version_flag_prints_the_package_version(capsys):
    from vnflight import __version__

    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"vnflight {__version__}"
    assert cli.VERSION == __version__


def test_install_shim_takes_the_shim_next_to_the_code_when_the_root_has_none(tmp_path, capsys):
    """A --games-dir that only holds vnflight.json (the released layout:
    config and games in one place, the code elsewhere) used to fail with
    "Source shim not found at <games-dir>/vnflight.rpy".  The shim ships
    next to the code, so that is where it is taken from."""
    from vnflight.lib import shim_source_path

    root, game_dir = _make_install_root(tmp_path, config=_NO_MODS_CONFIG)
    (root / "vnflight.rpy").unlink()

    rc = cli.cmd_install_shim(_install_args("mygame", root), FakeClientState())

    assert rc == 0, capsys.readouterr().out
    installed = (game_dir / "game" / "vnflight.rpy").read_bytes()
    assert installed == shim_source_path(None).read_bytes()
    assert b"vnflight" in installed[:4096]
