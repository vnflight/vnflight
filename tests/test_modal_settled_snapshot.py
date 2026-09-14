"""Modal body ownership when a transaction settles on game-state evidence."""

import pytest

from test_handlers import MockClient, MockWaitResult
from vnflight.handlers import HandlerContext, handle_wait, render_tool_result_text


@pytest.mark.parametrize("live_state_available", [False, True])
def test_scoped_wait_fetches_modal_body_instead_of_game_state_witness(
    live_state_available,
):
    client = MockClient()
    button = {
        "label": "CLOSE", "screen": "archive_panel", "actions": ["Return"],
        "category": "navigation", "index": 1,
    }
    game_state = {
        "type": "game_state", "_seq": 22,
        "_source_id": "shim", "_source_seq": 12,
        "modal_overlay_screens": ["archive_panel"],
        "screen_buttons": [button],
        "interactions": [{
            "id": "archive_panel:CLOSE", "display_label": "CLOSE",
            "screen": "archive_panel", "type": "choice", "index": 1,
            "action_names": ["Return"], "category": "navigation",
        }],
    }
    client._game_state = game_state if live_state_available else None
    client._screen = {
        "type": "screen_content", "_seq": 20,
        "_source_id": "shim", "_source_seq": 10,
        "overlay_active": True,
        "modal_overlay_screens": ["archive_panel"],
        "texts": ["ARCHIVE", "Selected record description."],
        "overlay_texts": ["ARCHIVE", "Selected record description."],
        "buttons": [button],
    }
    client._wait_result = MockWaitResult(
        screen=game_state,
        events=[
            {"type": "screen_text", "texts": ["Selected record description."],
             "screens": ["archive_panel"], "_seq": 21, "action_id": 26},
            {"type": "inventory_update", "items": [{"name": "Receipt"}]},
            {"type": "narration", "text": "A genuinely separate observation.",
             "_seq": 23},
            {"type": "narration", "text": "A genuinely separate observation.",
             "_seq": 24},
        ],
        transaction={
            "action_id": 26, "action_nonce": "preview",
            "transaction_state": "settled", "resolved_as": "button",
            "interaction_type": "info",
        },
    )

    result = handle_wait(
        HandlerContext(client=client), {"action_nonce": "preview", "timeout": 2},
    )
    rendered = render_tool_result_text(result)

    assert "ARCHIVE" in rendered
    assert rendered.count("Selected record description.") == 1
    assert rendered.count("A genuinely separate observation.") == 2
    assert "Receipt" in rendered
    assert result["_data"]["_overlay_active"] is True
    assert len([call for call in client.calls if call[0] == "wait"]) == 1
