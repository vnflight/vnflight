import json

from vnflight.action_surface import (
    action_request_signature,
    action_screen_signature,
    actionable_request_content_signature,
    actionable_request_signature,
    actionable_screen_signature,
    choice_screen_content_signature,
)
from vnflight.bridge import GameState
from vnflight.client import _project_actionable_items as client_project_items


def test_complete_signatures_preserve_identity_and_ignore_transport_metadata():
    screen = {
        "interactions": [{"id": "choice:1", "label": "Żurawie"}],
        "screen_buttons": [{"label": "LOG"}],
        "choices": [{"id": 1, "label": "Continue"}],
        "texts": ["presentation only"],
    }
    request = {
        "id": "request-1",
        "type": "choice_request",
        "choices": [{"id": 1, "label": "Continue"}],
        "stats": {"time": 4},
        "_seq": 12,
        "_source_ts": 44.0,
    }

    assert json.loads(action_screen_signature(screen)) == {
        "interactions": screen["interactions"],
        "screen_buttons": screen["screen_buttons"],
        "choices": screen["choices"],
    }
    assert json.loads(action_request_signature(request)) == {
        "id": "request-1",
        "type": "choice_request",
        "choices": request["choices"],
        "stats": {"time": 4},
    }


def test_choice_content_ignores_render_identity_and_normalizes_labels():
    first = {
        "interactions": [
            {
                "id": "menu:1",
                "type": "choice",
                "label": "  Ask   about  ARIA ",
                "aliases": ["  second ", "first"],
                "screen": "menu",
                "index": 0,
                "action_strs": ["Return('aria')"],
            },
            {"id": "quick:1", "type": "button", "label": "Save"},
        ],
        "choices": [],
    }
    rebuilt = {
        "interactions": [
            {
                "id": "focus:8",
                "type": "choice",
                "label": "Ask about ARIA",
                "aliases": ["first", "second"],
                "screen": "_focus_list",
                "index": 8,
                "action_strs": ["Return('aria')"],
            },
        ],
        "choices": [],
    }

    assert choice_screen_content_signature(first) == (
        choice_screen_content_signature(rebuilt)
    )


def test_actionable_request_identity_and_content_have_distinct_contracts():
    original = {
        "id": "request-1",
        "type": "choice_request",
        "interactions": [{
            "id": "choice:1",
            "label": "Proceed",
            "aliases": ["go", "continue"],
            "action_strs": ["Return(True)"],
        }],
    }
    reissued = {
        **original,
        "id": "request-2",
        "reissue_root_request_id": "request-1",
    }
    successor = {**original, "id": "request-3"}

    assert actionable_request_signature(original) == (
        actionable_request_signature(reissued)
    )
    assert actionable_request_signature(original) != (
        actionable_request_signature(successor)
    )
    assert actionable_request_content_signature(original) == (
        actionable_request_content_signature(successor)
    )


def test_bridge_delegators_preserve_class_level_schema_overrides():
    class LabelOnlyGameState(GameState):
        _ACTIONABLE_ITEM_FIELDS = frozenset({"label"})

    screen = {
        "interactions": [{
            "id": "unstable",
            "label": "Continue",
            "action_strs": ["Return(True)"],
        }],
    }

    assert LabelOnlyGameState._actionable_screen_signature(screen) == (
        actionable_screen_signature(
            screen, item_fields=frozenset({"label"}))
    )
    projected = json.loads(
        LabelOnlyGameState._actionable_screen_signature(screen))
    assert projected["interactions"] == [{"label": "Continue"}]


def test_bridge_delegators_preserve_method_level_policy_overrides():
    class CustomizedGameState(GameState):
        @staticmethod
        def _normalize_choice_label(value):
            return "<{}>".format(value)

        @classmethod
        def _project_actionable_items(cls, items):
            return [{"custom_items": len(items or [])}]

        @classmethod
        def _actionable_request_surface(cls, request):
            return {"custom_request": request.get("id")}

    choice_screen = {
        "interactions": [{"type": "choice", "label": "Continue"}],
    }
    choice_content = json.loads(
        CustomizedGameState._choice_screen_content_signature(choice_screen))
    assert choice_content["interactions"] == [{
        "label": "<Continue>",
        "type": "choice",
    }]

    actionable_screen = json.loads(
        CustomizedGameState._actionable_screen_signature(choice_screen))
    assert actionable_screen == {
        "interactions": [{"custom_items": 1}],
        "screen_buttons": [{"custom_items": 0}],
        "choices": [{"custom_items": 0}],
    }

    request = {"id": "request-1", "type": "choice_request"}
    assert json.loads(
        CustomizedGameState._actionable_request_content_signature(request)
    ) == {"custom_request": "request-1"}
    assert json.loads(
        CustomizedGameState._actionable_request_signature(request)
    ) == {
        "custom_request": "request-1",
        "request_id": "request-1",
    }


def test_client_projection_uses_shared_policy_with_provenance_id_removed():
    rows = [{
        "id": "focus-list:1",
        "label": "Continue",
        "aliases": ["next", "advance"],
        "action_strs": ["Return(True)"],
        "annotation": "presentation only",
    }]

    assert client_project_items(rows) == [{
        "label": "Continue",
        "aliases": ["advance", "next"],
        "action_strs": ["Return(True)"],
    }]
