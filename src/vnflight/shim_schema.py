"""Shared contracts for actionable data emitted by the Ren'Py shim."""

from __future__ import annotations


# Increment only for an incompatible bridge/shim wire-contract change. Both
# sides refuse a mismatch; this project is pre-release, so there is no legacy
# protocol branch to maintain.
#
# The frozensets below pin ITEM shapes (interactions, buttons, choices) and the
# choice-request payload. Screen-level event keys — overlay_screens,
# overlay_texts_by_screen, overlay_retained_screens, overlay_generations,
# modal_overlay_screens — are deliberately NOT enumerated here: consumers read
# them with .get() and ignore what they do not know, so a new sibling key is
# additive and needs no version bump. Adding a field to an item dict does.
SHIM_PROTOCOL_VERSION = 4


SHIM_INTERACTION_FIELDS = frozenset({
    "id", "display_label", "type", "disabled", "caption", "aliases", "source",
    "screen", "index", "annotation", "choice_index", "action_strs",
    "action_names", "promoted", "_suppress_pending_action",
    "original_label", "category", "wait_after_action",
    # "the click runs script" -- distinct from wait_after_action, which only
    # means "the UI rebuilds after this frame".  See the act settle policy.
    "story_entry", "is_selected",
})

SHIM_GAME_STATE_BUTTON_FIELDS = frozenset({
    "label", "screen", "actions", "is_disabled", "index", "action_strs",
    "annotation", "_suppress_pending_action", "_category", "category",
    "is_selected",
})

SHIM_REQUEST_BUTTON_FIELDS = frozenset({
    "label", "actions", "action_strs", "screen", "annotation",
    "_suppress_pending_action",
})

SHIM_REQUEST_CHOICE_FIELDS = frozenset({"id", "label"})

SHIM_CHOICE_REQUEST_PAYLOAD_FIELDS = frozenset({
    "choices", "full_items", "is_nvl", "inventory", "stats",
    "screen_buttons", "promoted_buttons", "interactions",
})

# Union of fields that can change target lookup or execution across canonical
# interactions and the pre/post-render fallback lists. Presentation-only shim
# fields such as annotation and category are deliberately absent.
ACTIONABLE_ITEM_FIELDS = frozenset({
    "id", "label", "display_label", "original_label", "type", "disabled",
    "is_disabled", "caption", "is_caption", "aliases", "source", "screen",
    "index", "choice_index", "action_strs", "actions", "action_names",
    "promoted", "_suppress_pending_action", "wait_after_action",
    "story_entry",
})

# Request identity is added separately from id/reissue provenance. These are
# the only request payload fields that can change how an act resolves.
ACTIONABLE_REQUEST_TARGET_FIELDS = frozenset({
    "interactions", "screen_buttons", "promoted_buttons", "choices",
})

ACTIONABLE_REQUEST_FIELDS = frozenset({"type"}) | (
    ACTIONABLE_REQUEST_TARGET_FIELDS
)
