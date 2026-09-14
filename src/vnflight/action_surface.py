"""Pure actionable-screen and request signature policy.

The bridge uses full surface signatures for admission identity and narrower
content signatures for transaction settlement. Keeping the projections here
makes those distinctions explicit without coupling them to ``GameState``
locking, persistence, or request lifecycle state.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .shim_schema import (
    ACTIONABLE_ITEM_FIELDS,
    ACTIONABLE_REQUEST_TARGET_FIELDS,
)


CHOICE_CONTENT_VOLATILE_FIELDS = frozenset({
    "id", "screen", "index", "choice_index", "wait_after_action",
})
CHOICE_CONTENT_LABEL_FIELDS = frozenset({
    "label", "display_label", "original_label",
})


def _serialize_surface(surface: object) -> str:
    try:
        return json.dumps(surface, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(surface)


def modal_overlay_tags(screen: dict | None) -> list[str]:
    """Declared-modal overlay tags currently shown, in panel order.

    A modal overlay owns the whole actionable surface, so opening or closing
    one is a surface change in its own right even when the underlying
    controls happen to project identically.
    """
    if not isinstance(screen, dict):
        return []
    tags: list[str] = []
    for raw in screen.get("modal_overlay_screens") or []:
        tag = str(raw)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def action_screen_signature(screen: dict | None) -> str:
    """Return the complete action-bearing portion of a screen snapshot."""
    if not screen:
        return ""
    surface = {
        "interactions": screen.get("interactions") or [],
        "screen_buttons": screen.get("screen_buttons") or [],
        "choices": screen.get("choices") or [],
    }
    # Additive: absent for every surface without a declared modal panel, so
    # signatures for unmodified games are byte-identical to before.
    modal = modal_overlay_tags(screen)
    if modal:
        surface["modal_overlay_screens"] = modal
    return _serialize_surface(surface)


def normalize_choice_content_label(value: object) -> object:
    if not isinstance(value, str):
        return value
    return " ".join(value.split())


def project_choice_content_items(
    items: object,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
    volatile_fields: frozenset[str] = CHOICE_CONTENT_VOLATILE_FIELDS,
    label_fields: frozenset[str] = CHOICE_CONTENT_LABEL_FIELDS,
    normalize_label: Callable[[object], object] = normalize_choice_content_label,
) -> list:
    """Project rendered choices to stable decision semantics."""
    projected = []
    for item in items or []:
        if not isinstance(item, dict):
            projected.append(normalize_label(item))
            continue
        action = {
            key: item[key]
            for key in item_fields
            if key in item and key not in volatile_fields
        }
        for key in label_fields:
            if key in action:
                action[key] = normalize_label(action[key])
        aliases = action.get("aliases")
        if isinstance(aliases, (list, tuple)):
            action["aliases"] = sorted(
                (normalize_label(value) for value in aliases),
                key=lambda value: str(value),
            )
        projected.append(action)
    return projected


def choice_screen_content_signature(
    screen: dict | None,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
    volatile_fields: frozenset[str] = CHOICE_CONTENT_VOLATILE_FIELDS,
    label_fields: frozenset[str] = CHOICE_CONTENT_LABEL_FIELDS,
    project_items: Callable[[object], list] | None = None,
) -> str:
    """Return choice content without utility chrome or screen identity."""
    if not screen:
        return ""
    interactions = [
        item for item in (screen.get("interactions") or [])
        if isinstance(item, dict)
        and (
            item.get("source") == "choice"
            or item.get("type") == "choice"
            or item.get("category") == "choices"
            or item.get("_category") == "choices"
        )
    ]
    if project_items is None:
        def project_items(value: object) -> list:
            return project_choice_content_items(
                value,
                item_fields=item_fields,
                volatile_fields=volatile_fields,
                label_fields=label_fields,
            )
    surface = {
        "interactions": project_items(interactions),
        "choices": project_items(screen.get("choices") or []),
    }
    if not any(surface.values()):
        return ""
    return _serialize_surface(surface)


def action_request_signature(request: dict | None) -> str:
    """Return request identity excluding transport provenance fields."""
    if not request:
        return ""
    return _serialize_surface({
        key: value for key, value in request.items()
        if key not in {
            "_seq", "_set_at", "_source_id", "_source_seq", "_source_ts",
        }
    })


def project_bridge_actionable_items(
    items: object,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
) -> list:
    """Keep fields that can affect act target lookup or execution."""
    projected = []
    for item in items or []:
        if not isinstance(item, dict):
            projected.append(item)
            continue
        action = {
            key: item[key]
            for key in item_fields
            if key in item
        }
        aliases = action.get("aliases")
        if isinstance(aliases, (list, tuple)):
            action["aliases"] = sorted(aliases, key=lambda value: str(value))
        projected.append(action)
    return projected


def actionable_screen_signature(
    screen: dict | None,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
    project_items: Callable[[object], list] | None = None,
) -> str:
    """Return the part of a game-state screen that a queued act can target."""
    if not screen:
        return ""
    if project_items is None:
        def project_items(value: object) -> list:
            return project_bridge_actionable_items(
                value, item_fields=item_fields)
    surface = {
        key: project_items(screen.get(key))
        for key in ("interactions", "screen_buttons", "choices")
    }
    modal = modal_overlay_tags(screen)
    if modal:
        surface["modal_overlay_screens"] = modal
    return _serialize_surface(surface)


def actionable_request_surface(
    request: dict | None,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
    target_fields: frozenset[str] = ACTIONABLE_REQUEST_TARGET_FIELDS,
    project_items: Callable[[object], list] | None = None,
) -> dict:
    """Project a request to controls that can resolve an act."""
    if not request:
        return {}
    if project_items is None:
        def project_items(value: object) -> list:
            return project_bridge_actionable_items(
                value, item_fields=item_fields)
    surface = {}
    if "type" in request:
        surface["type"] = request["type"]
    for key in target_fields:
        surface[key] = project_items(request.get(key))
    return surface


def actionable_request_content_signature(
    request: dict | None,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
    target_fields: frozenset[str] = ACTIONABLE_REQUEST_TARGET_FIELDS,
    project_request: Callable[[dict | None], dict] | None = None,
) -> str:
    """Return actionable request content without transient identity."""
    surface = (
        project_request(request)
        if project_request is not None
        else actionable_request_surface(
            request,
            item_fields=item_fields,
            target_fields=target_fields,
        )
    )
    return _serialize_surface(surface) if surface else ""


def actionable_request_signature(
    request: dict | None,
    *,
    item_fields: frozenset[str] = ACTIONABLE_ITEM_FIELDS,
    target_fields: frozenset[str] = ACTIONABLE_REQUEST_TARGET_FIELDS,
    project_request: Callable[[dict | None], dict] | None = None,
) -> str:
    """Return actionable content plus its logical request identity."""
    if not request:
        return ""
    surface = (
        project_request(request)
        if project_request is not None
        else actionable_request_surface(
            request,
            item_fields=item_fields,
            target_fields=target_fields,
        )
    )
    surface["request_id"] = (
        request.get("reissue_root_request_id")
        or request.get("reissued_from_request_id")
        or request.get("id")
    )
    return _serialize_surface(surface)
