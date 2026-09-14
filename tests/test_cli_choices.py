"""Screen-only menus must remain visible to CLI players."""

from argparse import Namespace
import json

import pytest

from vnflight import cli


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("has_choices", [False, True])
def test_choices_without_standard_pending_request(monkeypatch, capsys, as_json, has_choices):
    interactions = ([{
        "type": "choice", "display_label": "Analyze the signal",
        "screen": "custom_terminal", "actions": ["Return"],
    }] if has_choices else [])
    screen = {
        "screens": ["custom_terminal"], "interactions": interactions,
        "buttons": [], "modal_screens": ["custom_terminal"],
    }

    class Session:
        def pending(self):
            return None

        def state(self):
            return {"status": "running", "context": {"context": "in_game"}}

        def _get(self, path, **kwargs):
            assert path == "/screen"
            return 200, {"screen": screen}

        def get_transcript(self, **kwargs):
            return []

        def is_up(self):
            return True

    session = Session()
    monkeypatch.setattr(cli, "_make_session", lambda *args: session)
    monkeypatch.setattr(cli, "_save_session", lambda *args: None)
    assert cli.cmd_choices(Namespace(json=as_json, quiet=False), object()) == 0
    output = capsys.readouterr().out
    if as_json:
        result = json.loads(output)
        if has_choices:
            assert result["interactions"][0]["display_label"] == "Analyze the signal"
        else:
            assert result == {"pending_action": None}
    elif has_choices:
        assert "Analyze the signal" in output
        assert "No choices" not in output
    else:
        assert "No choices are currently pending" in output
