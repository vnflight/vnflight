"""CLI inspection must use the current command's correlated receipt."""

from argparse import Namespace
import json

import pytest

from vnflight import cli


@pytest.mark.parametrize("success", [True, False])
def test_inspect_uses_bounded_correlated_command(monkeypatch, capsys, success):
    receipt = {
        "type": "command_result", "command": "inspect", "nonce": "current",
        "success": success, "screens": ["echo_terminal_choice"],
    }
    if not success:
        receipt["error"] = "Inspection unavailable"
    calls = []

    class Session:
        def command(self, name, **kwargs):
            calls.append((name, kwargs))
            return receipt

        def _send_command(self, *args, **kwargs):
            pytest.fail("Uncorrelated command submission")

        def poll(self, **kwargs):
            pytest.fail("Old receipts must not be selected by command name")

    session = Session()
    saved = []
    monkeypatch.setattr(cli, "_make_session", lambda *args: session)
    monkeypatch.setattr(cli, "_save_session", lambda *args: saved.append(args[0]))
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)
    assert cli.cmd_inspect(Namespace(json=True), object()) == (0 if success else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is success
    if success:
        assert result["nonce"] == "current"
        assert result["screens"] == ["echo_terminal_choice"]
    else:
        assert result["error"] == receipt["error"]
    assert calls == [("inspect", {"_deadline": 105.0})]
    assert saved == [session]
