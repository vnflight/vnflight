"""Shared lifecycle classification for bridge state snapshots."""

from __future__ import annotations

from typing import Any

DEBUG_SCREEN_NAMES = {
    "vnf_command_poller",
    "vnf_player_debug",
    "llm_command_poller",
    "llm_player_debug",
}

DISABLED_ACTION_NAMES = {"NullAction", "None", "none"}
# The stock Ren'Py quick menu as the focus fallback reports it.  Q.Load is
# `QuickLoad()`, which Ren'Py builds as a FileLoad on the quick page, so the
# scraped action name is "FileLoad"; before it was listed here a quick menu
# with Q.Load made every wait return at once as "screen_actions", and the
# opening of a game looked as if it never arrived.
DEFAULT_FOCUS_CHROME_LABELS = {
    "auto",
    "back",
    "history",
    "load",
    "load game",
    "prefs",
    "preferences",
    "q. load",
    "q.load",
    "q. save",
    "q.save",
    "quick load",
    "quick save",
    "save",
    "save game",
    "skip",
}
DEFAULT_FOCUS_CHROME_ACTIONS = {
    "FileLoad",
    "FileSave",
    "FileTakeScreenshot",
    "QuickLoad",
    "QuickSave",
    "Rollback",
    "ShowMenu",
    "Skip",
    "ToggleField",
}


def actions_are_disabled(actions: list[Any] | tuple[Any, ...] | None) -> bool:
    """Return True if an action list can only represent a disabled/no-op UI."""
    if not actions:
        return False
    return all(str(action) in DISABLED_ACTION_NAMES for action in actions)


def item_is_disabled(item: dict[str, Any]) -> bool:
    """Shared disabled predicate for scraped buttons/interactions."""
    if item.get("disabled") is True or item.get("is_disabled") is True:
        return True
    if item.get("sensitive") is False or item.get("enabled") is False:
        return True
    label = str(item.get("label") or item.get("display_label") or "").strip()
    if label.endswith("(disabled)"):
        return True
    actions = item.get("actions") or item.get("action_names") or []
    if actions_are_disabled(actions):
        return True
    return False


def item_is_default_focus_chrome(item: dict[str, Any]) -> bool:
    """Return True for default Ren'Py quick-menu widgets from focus fallback."""
    if str(item.get("screen") or "") != "_focus_list":
        return False
    label = str(item.get("label") or item.get("display_label") or "").strip().lower()
    if label not in DEFAULT_FOCUS_CHROME_LABELS:
        return False
    actions = item.get("actions") or item.get("action_names") or []
    return any(
        str(action) in DEFAULT_FOCUS_CHROME_ACTIONS
        for action in actions
    )


def screen_names(screen: dict | None) -> set[str]:
    names = set((screen or {}).get("screens") or [])
    return names - DEBUG_SCREEN_NAMES


def screen_is_menu_only(screen: dict | None) -> bool:
    return screen_names(screen) == {"menu"}


def has_actionable_screen_buttons(screen: dict | None) -> bool:
    for button in (screen or {}).get("buttons") or []:
        if str(button.get("screen") or "") == "quick_menu":
            continue
        if item_is_default_focus_chrome(button):
            continue
        if str(button.get("label", "")).strip() and not item_is_disabled(button):
            return True
    return False


def has_screen_buttons(screen: dict | None) -> bool:
    return any(
        str(b.get("label", "")).strip()
        for b in (screen or {}).get("buttons") or []
    )


def has_pending_request(pending: dict | None) -> bool:
    return bool(
        pending
        and pending.get("type") in ("choice_request", "choices", "input_request")
    )


def pending_kind(pending: dict | None) -> str | None:
    if not pending:
        return None
    ptype = pending.get("type")
    if ptype in ("choice_request", "choices"):
        return "choice"
    if ptype == "input_request":
        return "input"
    return None


def classify_lifecycle(raw: dict | None) -> dict[str, Any]:
    """Classify a raw bridge state into an agent-facing lifecycle phase.

    Raw bridge status is transport-oriented and can briefly say "ended" while
    menus, overlays, or pending requests are still actionable. This helper keeps
    that raw status available while naming the effective state consumers should
    render or act on.
    """
    raw = raw or {}
    raw_status = raw.get("status", "unknown")
    raw_context = (raw.get("context") or {}).get("context")
    pending = raw.get("pending_request")
    game_state = raw.get("game_state") or {}
    screen = raw.get("screen") or {}
    config = raw.get("config") or {}
    # screen_content is pushed immediately before game_state, while context
    # updates are rate-limited. At the title boundary its explicit
    # store.main_menu sample is therefore newer than a lingering in_game
    # context. Games that opt out of menu-return endings can legitimately
    # expose main_menu during play, so use the bridge's policy gate here too.
    screen_main_menu_boundary = bool(
        screen.get("main_menu") is True
        and config.get("end_on_menu_return", True) is not False
    )
    context = "main_menu" if screen_main_menu_boundary else raw_context
    action_buttons = (
        screen.get("buttons") or []
        if screen_main_menu_boundary
        else game_state.get("screen_buttons") or screen.get("buttons") or []
    )
    screen_for_actions = {
        "buttons": action_buttons
    }
    # Ren'Py reports a bare in_game context while its splash/title interaction
    # is still coming up.  The bridge deliberately does not treat that context
    # alone as gameplay evidence; mirror that contract here instead of telling
    # an agent the game is "playing" before any screen or decision exists.
    awaiting_first_interaction = bool(
        raw.get("gameplay_seen") is False
        and raw_status == "running"
        and raw_context == "in_game"
        and not has_pending_request(pending)
        and not has_actionable_screen_buttons(screen_for_actions)
    )

    pkind = pending_kind(pending)
    has_pending = pkind is not None
    has_actions = has_actionable_screen_buttons(screen_for_actions)
    stale_menu_overlay = bool(
        pkind == "choice"
        and context == "in_game"
        and screen_is_menu_only(screen)
    )
    stale_main_menu_pending = bool(
        pkind == "choice"
        and context in ("main_menu", "menu")
        and has_actions
    )

    if pkind == "input":
        phase = "blocked_on_input"
    elif pkind == "choice" and not stale_main_menu_pending:
        phase = "blocked_on_choice"
    elif has_actions:
        phase = "screen_actions"
    elif raw_status == "ended":
        phase = "ended"
    elif context in ("main_menu", "menu"):
        phase = "menu"
    elif context in ("setup", "preferences"):
        phase = "setup"
    elif awaiting_first_interaction:
        phase = "starting"
    elif context == "in_game":
        phase = "playing"
    else:
        phase = raw_status

    terminal = raw_status == "ended" and phase == "ended"
    return {
        "raw_status": raw_status,
        "effective_status": phase,
        "context": context,
        "raw_context": raw_context,
        "screen_main_menu_boundary": screen_main_menu_boundary,
        "awaiting_first_interaction": awaiting_first_interaction,
        "terminal": terminal,
        "has_pending": has_pending,
        "pending_kind": pkind,
        "has_screen_actions": has_actions,
        "has_screen_buttons": has_screen_buttons(screen_for_actions),
        "stale_menu_overlay": stale_menu_overlay,
        "stale_main_menu_pending": stale_main_menu_pending,
        "suppress_raw_ended": raw_status == "ended" and not terminal,
    }
