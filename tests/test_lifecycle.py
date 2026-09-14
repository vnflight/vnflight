from vnflight.lifecycle import (
    actions_are_disabled,
    classify_lifecycle,
    item_is_default_focus_chrome,
    item_is_disabled,
)


def test_lifecycle_suppresses_raw_ended_with_pending_choice():
    state = {
        "status": "ended",
        "pending_request": {
            "type": "choice_request",
            "id": "camp",
            "choices": ["Look around"],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["raw_status"] == "ended"
    assert lifecycle["effective_status"] == "blocked_on_choice"
    assert lifecycle["terminal"] is False
    assert lifecycle["suppress_raw_ended"] is True


def test_lifecycle_marks_stale_menu_overlay_during_gameplay_choice():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "pending_request": {
            "type": "choice_request",
            "id": "next",
            "choices": ["Stay"],
        },
        "screen": {
            "screens": ["menu", "vnf_command_poller"],
            "buttons": [{"label": "Start", "screen": "menu"}],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["effective_status"] == "blocked_on_choice"
    assert lifecycle["stale_menu_overlay"] is True


def test_lifecycle_marks_stale_main_menu_pending():
    state = {
        "status": "running",
        "context": {"context": "main_menu"},
        "pending_request": {
            "type": "choice_request",
            "id": "old-ending-continue",
            "choices": ["(continue)"],
        },
        "game_state": {
            "screen_buttons": [
                {"label": "Continue", "screen": "menu", "actions": ["LoadMostRecent"]},
                {"label": "New Game", "screen": "menu", "actions": ["Start"]},
            ],
        },
        "screen": {
            "screens": ["menu"],
            "buttons": [
                {"label": "Continue", "screen": "menu", "actions": ["LoadMostRecent"]},
                {"label": "New Game", "screen": "menu", "actions": ["Start"]},
            ],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["effective_status"] == "screen_actions"
    assert lifecycle["stale_main_menu_pending"] is True


def test_explicit_main_menu_screen_overrides_lagging_game_context():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "config": {"end_on_menu_return": True},
        "game_state": {"screen_buttons": []},
        "screen": {
            "main_menu": True,
            "screens": ["menu"],
            "buttons": [
                {"label": "Start", "screen": "menu", "actions": ["Start"]},
            ],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["raw_context"] == "in_game"
    assert lifecycle["context"] == "main_menu"
    assert lifecycle["screen_main_menu_boundary"] is True
    assert lifecycle["effective_status"] == "screen_actions"
    assert lifecycle["has_screen_actions"] is True


def test_main_menu_screen_does_not_override_opt_out_game_context():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": True,
        "config": {"end_on_menu_return": False},
        "screen": {"main_menu": True, "screens": ["menu"], "buttons": []},
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["context"] == "in_game"
    assert lifecycle["screen_main_menu_boundary"] is False
    assert lifecycle["effective_status"] == "playing"


def test_bare_pre_game_context_is_starting_not_playing():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": False,
        "config": {"end_on_menu_return": True},
        "game_state": {"screen_buttons": []},
        "transcript": [
            {"type": "context", "context": "in_game", "_seq": 1},
        ],
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["raw_context"] == "in_game"
    assert lifecycle["awaiting_first_interaction"] is True
    assert lifecycle["effective_status"] == "starting"


def test_real_gameplay_evidence_keeps_bare_context_playing():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "gameplay_seen": True,
        "config": {"end_on_menu_return": True},
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["awaiting_first_interaction"] is False
    assert lifecycle["effective_status"] == "playing"


def test_missing_gameplay_verdict_keeps_legacy_bare_context_playing():
    lifecycle = classify_lifecycle({
        "status": "running",
        "context": {"context": "in_game"},
    })

    assert lifecycle["awaiting_first_interaction"] is False
    assert lifecycle["effective_status"] == "playing"


def test_lifecycle_keeps_terminal_ended_when_only_disabled_buttons_remain():
    state = {
        "status": "ended",
        "screen": {
            "buttons": [
                {"label": "Continue", "enabled": False},
            ],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["effective_status"] == "ended"
    assert lifecycle["terminal"] is True
    assert lifecycle["has_screen_actions"] is False


def test_lifecycle_ignores_quick_menu_as_screen_actions():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "game_state": {
            "screen_buttons": [
                {
                    "label": "Q. Save",
                    "screen": "quick_menu",
                    "actions": ["FileSave"],
                },
            ],
        },
        "screen": {
            "screens": ["quick_menu"],
            "buttons": [
                {
                    "label": "Q. Save",
                    "screen": "quick_menu",
                    "actions": ["FileSave"],
                },
            ],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert lifecycle["effective_status"] == "playing"
    assert lifecycle["has_screen_actions"] is False
    assert lifecycle["has_screen_buttons"] is True


def test_lifecycle_ignores_default_focus_list_chrome_as_screen_actions():
    state = {
        "status": "running",
        "context": {"context": "in_game"},
        "game_state": {
            "screen_buttons": [
                {
                    "label": "History",
                    "screen": "_focus_list",
                    "actions": ["ShowMenu"],
                },
                {
                    "label": "Q.Save",
                    "screen": "_focus_list",
                    "actions": ["FileTakeScreenshot", "FileSave"],
                },
            ],
        },
    }

    lifecycle = classify_lifecycle(state)

    assert item_is_default_focus_chrome(state["game_state"]["screen_buttons"][0])
    assert lifecycle["effective_status"] == "playing"
    assert lifecycle["has_screen_actions"] is False


def test_default_focus_list_chrome_predicate_keeps_non_chrome_actions():
    assert item_is_default_focus_chrome({
        "label": "Back",
        "screen": "_focus_list",
        "actions": ["Return"],
    }) is False
    assert item_is_default_focus_chrome({
        "label": "Talk",
        "screen": "_focus_list",
        "actions": ["ShowMenu"],
    }) is False
    assert item_is_default_focus_chrome({
        "label": "Save",
        "screen": "_focus_list",
        "actions": ["CustomSaveAction"],
    }) is False


def test_lifecycle_disabled_predicate_handles_common_button_flags():
    assert item_is_disabled({"label": "A", "disabled": True}) is True
    assert item_is_disabled({"label": "A", "is_disabled": True}) is True
    assert item_is_disabled({"label": "A", "sensitive": False}) is True
    assert item_is_disabled({"label": "A", "enabled": False}) is True
    assert item_is_disabled({"label": "A", "actions": ["NullAction"]}) is True
    assert item_is_disabled({"label": "A", "action_names": ["None"]}) is True
    assert item_is_disabled({"label": "A", "actions": ["Jump"]}) is False


def test_lifecycle_action_list_disabled_only_when_all_actions_are_noops():
    assert actions_are_disabled(["NullAction"]) is True
    assert actions_are_disabled(["NullAction", "None"]) is True
    assert actions_are_disabled(["NullAction", "Jump"]) is False
    assert actions_are_disabled([]) is False
