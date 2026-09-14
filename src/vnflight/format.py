"""Event and state formatting for vnflight.

Transforms raw bridge events and state data into structured
intermediate formats, then renders them as text for different
consumers (CLI, MCP agents, dashboards).
"""

from __future__ import annotations

import json
import platform
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .lifecycle import (
    actions_are_disabled,
    classify_lifecycle,
    item_is_default_focus_chrome,
    item_is_disabled,
)

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------


def _colour(text: str, code: str) -> str:
    """Wrap *text* in ANSI colour codes (no-op if stdout is not a TTY)."""
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return text
    if _IS_WINDOWS:
        # Enable ANSI on Windows 10+ by setting the console mode.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str) -> str:
    return _colour(t, "32")


def _yellow(t: str) -> str:
    return _colour(t, "33")


def _red(t: str) -> str:
    return _colour(t, "31")


def _cyan(t: str) -> str:
    return _colour(t, "36")


def _dim(t: str) -> str:
    return _colour(t, "2")


def _bold(t: str) -> str:
    return _colour(t, "1")


def _display_text(value: Any) -> str:
    """Normalize Ren'Py escaped text for agent/CLI display."""
    return str(value).replace("%%", "%")


_RENPY_TEXT_TAG_RE = re.compile(r"\{/?[a-zA-Z_]+[^{}]*\}")


def _strip_renpy_text_tags(value: Any) -> str:
    """Drop Ren'Py text tags ({color=...}, {b}, {/i}, ...) from display text.

    Menu captions reach the bridge raw (the shim cleans selectable labels,
    not prompt lines), so an epilogue caption rendered as
    ``{color=#f6d6bd}Old Págos{/color}``. ``{{`` is Ren'Py's escaped brace.
    """
    text = str(value if value is not None else "")
    text = text.replace("{{", "\x00")
    text = _RENPY_TEXT_TAG_RE.sub("", text)
    return text.replace("\x00", "{")


def _story_echo_key(value: Any) -> str:
    """Comparison key for 'this prompt repeats that story line'."""
    return " ".join(_strip_renpy_text_tags(_display_text(value)).split()).lower()


def _normalize_input_prompt(prompt: Any) -> str:
    """Normalize input prompt text for display."""
    return _display_text(prompt if prompt is not None else "Enter text")


def _is_menu_prompt_echo(text: str) -> bool:
    """Return True for generic menu prompt lines repeated as dialogue."""
    lowered = text.strip().lower()
    if not lowered:
        return False
    exact_prompts = {
        "what do you do?",
        "what do i do?",
        "how do you respond?",
    }
    if lowered in exact_prompts:
        return True
    prompt_fragments = (
        "what do you do?",
        "what do i do?",
        "how do you respond?",
    )
    if any(fragment in lowered for fragment in prompt_fragments):
        return True
    if lowered.startswith("which ") and lowered.endswith(" do you choose?"):
        return True
    if lowered.endswith(" waits for your order."):
        return True
    return False


def _drop_menu_prompt_echoes(
    events: list[dict],
    pending: dict | None = None,
) -> list[dict]:
    """Drop prompt-like dialogue lines adjacent to choice prompts."""
    if not events:
        return events
    pending_is_choice = bool(
        pending and pending.get("type") in ("choice_request", "choices", "choice")
    )
    filtered: list[dict] = []
    for idx, ev in enumerate(events):
        etype = ev.get("type")
        if etype in ("dialogue", "narration"):
            text = ev.get("text") or ev.get("what") or ""
            if _is_menu_prompt_echo(_display_text(text)):
                next_ev = events[idx + 1] if idx + 1 < len(events) else None
                next_is_choice = bool(
                    next_ev
                    and next_ev.get("type") in ("choice_request", "choices")
                )
                if next_is_choice or (idx == len(events) - 1 and pending_is_choice):
                    continue
        filtered.append(ev)
    return filtered


# ---------------------------------------------------------------------------
# Wait result: raw events -> structured data
# ---------------------------------------------------------------------------

def build_wait_data(events: list[dict], pending: dict | None = None,
                    ended: bool = False) -> dict:
    """Build structured wait data from raw bridge events.

    This is the canonical intermediate format -- formatters consume
    this to produce output for different targets.
    """
    story: list[dict] = []
    status: dict[str, list] = {"stats": [], "auto_skipped": [], "resolved": []}
    buttons: list[dict] = []
    events = _drop_menu_prompt_echoes(events, pending)
    incremental_inventory_additions: list[str] = []

    for ev in events:
        etype = ev.get("type", "")
        source_id = ev.get("_source_id")
        source_seq = ev.get("_source_seq")
        bridge_seq = ev.get("_seq")
        occurrence_prefix = None
        if type(source_seq) is int:
            occurrence_prefix = (
                f"source:{source_id}:{source_seq}"
                if isinstance(source_id, str) and source_id
                else f"source:{source_seq}"
            )
        elif type(bridge_seq) is int:
            occurrence_prefix = f"bridge:{bridge_seq}"

        if etype == "dialogue":
            who = ev.get("character") or ev.get("who") or "Narrator"
            text = _display_text(ev.get("text") or ev.get("what", ""))
            user = ev.get("user_initiated", False)
            item = {"type": "dialogue", "character": who, "text": text,
                    **({"user": True} if user else {})}
            delivery_id = ev.get("overlay_delivery_id")
            if type(delivery_id) is int:
                item["overlay_delivery_id"] = delivery_id
                item["passive_overlay_snapshot"] = True
                item["_occurrence_id"] = f"overlay:{delivery_id}"
            if occurrence_prefix:
                item.setdefault("_occurrence_id", occurrence_prefix)
            if type(bridge_seq) is int:
                item["_bridge_seq"] = bridge_seq
            story.append(item)

        elif etype == "narration":
            text = _display_text(ev.get("text", ""))
            if text.strip():
                item = {"type": "narration", "text": text}
                delivery_id = ev.get("overlay_delivery_id")
                if type(delivery_id) is int:
                    item["overlay_delivery_id"] = delivery_id
                    item["passive_overlay_snapshot"] = True
                    item["_occurrence_id"] = f"overlay:{delivery_id}"
                if occurrence_prefix:
                    item.setdefault("_occurrence_id", occurrence_prefix)
                if type(bridge_seq) is int:
                    item["_bridge_seq"] = bridge_seq
                story.append(item)

        elif etype == "stats_update":
            if ev.get("post_terminal"):
                # Ren'Py reset its store on the return to the menu: every
                # stat "changed" back to its default.  The bridge tagged the
                # event; rendering the phantom deltas reads as an unearned
                # end-of-run reset (the same false report the game_state
                # freeze exists to prevent).
                continue
            changed = ev.get("changed", {})
            previous = ev.get("previous", {})
            removed = set(ev.get("removed") or [])
            full_stats = ev.get("stats")
            if (
                isinstance(full_stats, dict)
                and full_stats.get("_suppress_brief")
            ):
                # A game can retire its compact progress surface at a story
                # boundary. Suppress only keys removed by that teardown;
                # semantic Act 3 evidence/resource changes remain visible.
                teardown_names = removed | {
                    key for key in changed if key not in full_stats
                }
                changed = {
                    key: value for key, value in changed.items()
                    if key not in teardown_names
                }
                previous = {
                    key: value for key, value in previous.items()
                    if key in changed
                }
                removed = set()
                if not changed:
                    continue
            for k, v in changed.items():
                if k.startswith("_"):
                    continue
                entry: dict[str, Any] = {"stat": k, "value": v}
                if (
                    k in removed
                    or (isinstance(full_stats, dict) and k not in full_stats)
                    or (
                        not isinstance(full_stats, dict)
                        and k in previous
                        and previous[k] is not None
                        and v is None
                    )
                ):
                    # Null is a valid stat value. Explicit removal provenance
                    # (or absence from the accompanying full map) is what
                    # distinguishes a deleted key.
                    entry["delta"] = "removed"
                elif k in previous:
                    old = previous[k]
                    try:
                        entry["delta"] = v - old
                    except (TypeError, ValueError):
                        pass
                status["stats"].append(entry)

        elif etype == "auto_skipped":
            label = ev.get("label", ev.get("text", ""))
            if label:
                status["auto_skipped"].append(label)

        elif etype == "request_resolved":
            chosen = ev.get("value") or ev.get("chosen_label") or ev.get("label", ev.get("choice", ""))
            by = ev.get("by", "agent")
            if chosen:
                status["resolved"].append({"label": chosen, "by": by})

        elif etype == "user_choice":
            label = ev.get("label", "")
            if label:
                status["resolved"].append({"label": label, "by": "user"})

        elif etype == "anomaly":
            kind = ev.get("kind", "unknown")
            msg = ev.get("message", ev.get("details", {}).get("message", ""))
            # Anomalies go to status, not story — they're diagnostic noise.
            status.setdefault("anomalies", []).append("%s: %s" % (kind, msg))

        elif etype == "screen_text":
            # Modal screen text (dialog/confirm popups) — surface as narration.
            delivery_ids = ev.get("overlay_delivery_ids") or []
            for index, t in enumerate(ev.get("texts", [])):
                t = _display_text(t).strip()
                if t:
                    item = {"type": "narration", "text": t,
                            "source": "screen_text"}
                    if ev.get("passive_overlay_snapshot"):
                        item["passive_overlay_snapshot"] = True
                    if index < len(delivery_ids):
                        item["overlay_delivery_id"] = delivery_ids[index]
                        item["_occurrence_id"] = (
                            f"overlay:{delivery_ids[index]}")
                    elif occurrence_prefix:
                        item["_occurrence_id"] = (
                            f"{occurrence_prefix}:row:{index}")
                    if type(bridge_seq) is int:
                        item["_bridge_seq"] = bridge_seq
                    story.append(item)

        elif etype == "inventory_update":
            if ev.get("post_terminal"):
                # Post-menu-return store reset — see stats_update above.
                continue
            items = ev.get("changed")
            incremental = items is not None
            if items is None:
                items = ev.get("items")
            if items is None:
                # The live shim sends its full snapshot as `inventory`;
                # bridge/tests may use the normalized `items` spelling.
                items = ev.get("inventory", [])
            names = _inventory_display_names(items)
            if incremental:
                removed_names = _inventory_display_names(
                    ev.get("removed", []))
                unmatched_changed = list(names)
                unmatched_removed = []
                for name in removed_names:
                    if name in unmatched_changed:
                        # Same-name replacement is usually a quantity or
                        # metadata update, not an acquisition plus removal.
                        unmatched_changed.remove(name)
                    else:
                        unmatched_removed.append(name)
            if incremental:
                inventory_status = status.setdefault("inventory", [])
                # Inventory is state, not an occurrence transcript. A single
                # wait can observe an item being introduced and then replaced.
                # Net those transitions while retaining unmatched removals for
                # items that may predate this event batch.
                eligible_changed = list(unmatched_changed)
                for name in names:
                    removed_marker = name + " (removed)"
                    try:
                        prior_index = len(inventory_status) - 1 - list(
                            reversed(inventory_status)).index(removed_marker)
                    except ValueError:
                        pass
                    else:
                        del inventory_status[prior_index]
                    inventory_status.append(name)
                    if name in eligible_changed:
                        eligible_changed.remove(name)
                        incremental_inventory_additions.append(name)
                for name in unmatched_removed:
                    cancellable = name in incremental_inventory_additions
                    if cancellable:
                        incremental_inventory_additions.remove(name)
                    try:
                        prior_index = len(inventory_status) - 1 - list(
                            reversed(inventory_status)).index(name)
                    except ValueError:
                        prior_index = None
                    if prior_index is not None:
                        del inventory_status[prior_index]
                    if not cancellable:
                        inventory_status.append(name + " (removed)")
            else:
                # Legacy events carry complete snapshots; the newest one,
                # including an empty snapshot, supersedes any earlier one.
                status["inventory"] = names
                incremental_inventory_additions = []

    # Dedup buttons by label+screen.
    if buttons:
        seen: set[tuple[str, str]] = set()
        unique_buttons: list[dict] = []
        for b in buttons:
            key = (b["label"], b.get("screen", ""))
            if key not in seen:
                seen.add(key)
                unique_buttons.append(b)
        buttons = unique_buttons

    # A single wait can contain several updates for the same stat.  Report the
    # final value and net numeric delta once; rendering every intermediate
    # sample makes ordinary progress look like duplicated state.
    if status["stats"]:
        coalesced: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for entry in status["stats"]:
            name = entry["stat"]
            if name not in coalesced:
                coalesced[name] = dict(entry)
                order.append(name)
                continue
            merged = coalesced[name]
            merged["value"] = entry.get("value")
            old_delta = merged.get("delta")
            new_delta = entry.get("delta")
            if (
                isinstance(old_delta, (int, float))
                and isinstance(new_delta, (int, float))
            ):
                merged["delta"] = old_delta + new_delta
            elif "delta" in entry:
                merged["delta"] = new_delta
            else:
                # The final update re-added/retyped the stat without a
                # composable delta. Do not retain an earlier removal or
                # numeric transition that no longer describes its value.
                merged.pop("delta", None)
        status["stats"] = [coalesced[name] for name in order]

    # Strip empty status sections.
    status = {k: v for k, v in status.items() if v}

    out: dict[str, Any] = {"story": story}
    if status:
        out["status"] = status
    if pending:
        out["pending"] = _build_pending(pending)
        out["_pending_raw"] = pending
    if buttons:
        out["buttons"] = buttons
    if ended:
        out["ended"] = True
    if not out.get("story") and not out.get("pending") and not out.get("buttons"):
        pass  # Empty result -- caller decides what to show.
    return out


def _build_pending(raw: dict) -> dict:
    """Convert raw pending request to structured format."""
    ptype = raw.get("type", "")
    if ptype in ("choice_request", "choices"):
        choices = raw.get("choices", [])
        full_items = raw.get("full_items", [])
        pending_choices = []
        # Build choice ID lookup from choices list (may have {id, label}
        # dicts set by the augmenter pipeline). Key by the ENABLED-choice
        # ordinal — the same basis as choice_idx below — so a disabled or
        # caption row in `choices` (synthesized pendings where
        # choices == full_items) can't shift ids onto the wrong option.
        choice_ids: dict[int, str] = {}
        _cid_ordinal = 0
        for ch in choices:
            if isinstance(ch, dict):
                if _is_suppressed_pending_choice(ch):
                    continue
                if item_is_disabled(ch) or ch.get("is_caption"):
                    continue
                _cid_ordinal += 1
                if ch.get("id"):
                    choice_ids[_cid_ordinal] = str(ch["id"])
            else:
                _cid_ordinal += 1  # plain string = enabled choice
        # Use full_items when available — it includes disabled/caption
        # entries that choices list may omit.
        source = full_items if full_items else choices
        choice_idx = 0
        for i, item in enumerate(source):
            if _is_suppressed_pending_choice(item):
                continue
            if isinstance(item, dict):
                label = item.get("label", str(item))
                is_disabled = item_is_disabled(item)
                is_caption = item.get("is_caption", False)
                annotation = item.get("annotation")
            else:
                # Simple string from choices list.
                label = str(item)
                is_disabled = False
                is_caption = False
                annotation = None
            # Skip shim placeholder disabled entries (empty label or
            # literal "(disabled)" sentinel) — they carry no info.
            _stripped = (label or "").strip()
            if is_disabled and (not _stripped or _stripped == "(disabled)"):
                continue
            entry: dict[str, Any] = {"label": label}
            # Captions are menu prompt text, not selectable options — the
            # shim excludes them from its enabled-only numeric map, so they
            # must not consume an agent-visible number here either (doing so
            # shifted every following choice/button off by one).
            if is_disabled or is_caption:
                entry["disabled"] = bool(is_disabled)
                entry["index"] = None
            else:
                choice_idx += 1
                entry["index"] = choice_idx
                cid = choice_ids.get(choice_idx)
                if cid:
                    entry["id"] = cid
            if is_caption:
                entry["caption"] = True
            if annotation:
                entry["annotation"] = annotation
            pending_choices.append(entry)
        result = {"type": "choice", "id": raw.get("id", ""),
                  "choices": pending_choices}
        interactions = raw.get("interactions")
        if isinstance(interactions, list):
            promoted_actions = _promoted_actions_from_interactions(
                interactions
            )
            interaction_actions = _interaction_actions_from_interactions(
                interactions,
                display_index_offset=choice_idx,
            )
        else:
            promoted_actions = _promoted_actions_from_buttons(
                raw.get("promoted_buttons") or []
            )
            interaction_actions = []
        promoted_ids = {
            action.get("id")
            for action in promoted_actions
            if action.get("id") is not None
        }
        promoted_labels = {
            str(action.get("label") or "").strip()
            for action in promoted_actions
            if str(action.get("label") or "").strip()
        }
        for action in interaction_actions:
            action_id = action.get("id")
            label = str(action.get("label") or "").strip()
            if (
                (action_id is not None and action_id in promoted_ids)
                or (action_id is None and label in promoted_labels)
            ):
                continue
            promoted_actions.append(action)
        if promoted_actions:
            result["actions"] = promoted_actions
        # Carry game-registered category metadata (headers / compact flags)
        # so the pending renderer can group trailing non-choice actions under
        # their category header the same way state()'s screen rendering does.
        button_categories = raw.get("button_categories")
        if isinstance(button_categories, dict) and button_categories:
            result["_button_categories"] = button_categories
        # Tag auto-advancing choices so agents know not to act.
        # The shim is authoritative here; a single visible choice can still
        # require manual action when auto-skip is paused or blocked.
        if raw.get("_auto_advancing"):
            result["auto_advancing"] = True
        return result
    elif ptype == "input_request":
        default = (
            raw.get("default")
            or raw.get("value")
            or raw.get("current")
            or raw.get("text")
            or ""
        )
        return {"type": "input", "id": raw.get("id", ""),
                "prompt": _normalize_input_prompt(raw.get("prompt", "Enter text")),
                "default": default}
    return {"type": ptype}


def _promoted_actions_from_interactions(interactions: list) -> list[dict]:
    actions = []
    for interaction in interactions or []:
        if not isinstance(interaction, dict) or not interaction.get("promoted"):
            continue
        if _is_suppressed_pending_action(interaction):
            continue
        if item_is_default_focus_chrome(interaction):
            continue
        if item_is_disabled(interaction):
            continue
        label = _button_display_label(interaction)
        if not str(label).strip():
            continue
        action: dict[str, Any] = {"label": label}
        if interaction.get("annotation"):
            action["annotation"] = interaction["annotation"]
        if interaction.get("id") is not None:
            action["id"] = interaction["id"]
        actions.append(action)
    return actions


def _promoted_actions_from_buttons(buttons: list) -> list[dict]:
    actions = []
    for button in buttons or []:
        if not isinstance(button, dict):
            continue
        if _is_suppressed_pending_action(button):
            continue
        if item_is_default_focus_chrome(button):
            continue
        if item_is_disabled(button):
            continue
        label = _button_display_label(button)
        if not str(label).strip():
            continue
        action: dict[str, Any] = {"label": label}
        if button.get("annotation"):
            action["annotation"] = button["annotation"]
        if button.get("id") is not None:
            action["id"] = button["id"]
        actions.append(action)
    return actions


def _is_suppressed_pending_action(item: dict) -> bool:
    """Return True when a mod marked an auxiliary pending action as stale."""
    return bool(
        item.get("_suppress_pending_action")
        or item.get("suppress_pending_action")
    )


def _is_suppressed_pending_choice(item: Any) -> bool:
    """Return True when a mod marked a choice as a non-actionable container."""
    return isinstance(item, dict) and bool(
        item.get("_suppress_pending_choice")
        or item.get("suppress_pending_choice")
    )


def _interaction_actions_from_interactions(
    interactions: list,
    *,
    display_index_offset: int = 0,
) -> list[dict]:
    """Return non-choice visible interactions as agent-facing actions.

    Disabled non-choice interactions are kept (tagged disabled) so the
    pending renderer can show them grouped under their category header the
    same way state() does (e.g. a quick-menu "Wait (disabled)").  They are
    never counted as actionable — see _has_actionable_wait_pending, which
    guards on item_is_disabled.
    """
    non_choice = [
        interaction for interaction in interactions or []
        if (
            isinstance(interaction, dict)
            and interaction.get("type") != "choice"
            and not interaction.get("promoted")
            and not interaction.get("hidden")
            and not item_is_default_focus_chrome(interaction)
        )
    ]
    if not non_choice:
        return []
    buckets = _with_interaction_display_indices(non_choice)
    buckets = _apply_display_index_offset(buckets, display_index_offset)
    actions: list[dict] = []
    for category in _ordered_categories(buckets.keys()):
        if category == "info":
            continue
        for interaction in buckets.get(category) or []:
            label = (
                interaction.get("display_label")
                or interaction.get("label")
                or ""
            )
            if not str(label).strip():
                continue
            action: dict[str, Any] = {
                "label": label,
                "type": interaction.get("type", "other"),
                "category": category,
            }
            if interaction.get("display_index") is not None:
                action["index"] = interaction["display_index"]
            if interaction.get("id") is not None:
                action["id"] = interaction["id"]
            if interaction.get("annotation"):
                action["annotation"] = interaction["annotation"]
            if item_is_disabled(interaction):
                action["disabled"] = True
            actions.append(action)
    return actions


def _interaction_is_story_choice_candidate(itr: dict) -> bool:
    """Return True for story choices, not modal/menu buttons shaped as choices."""
    if not isinstance(itr, dict):
        return False
    if itr.get("source") == "choice":
        return True
    if itr.get("type") != "choice":
        return False
    category = str(itr.get("category") or "")
    if category == "choices":
        return True
    action_names = set(
        itr.get("action_names")
        or itr.get("actions")
        or itr.get("action_strs")
        or []
    )
    if "ChoiceReturn" in action_names:
        screen = str(itr.get("screen") or "")
        iid = str(itr.get("id") or "")
        return screen not in {"menu", "quick_menu"} and not iid.startswith((
            "menu:",
            "quick_menu:",
        ))
    # Legacy game_state snapshots may only expose type=choice for live
    # story choices.  Explicit button-sourced choices are screen actions
    # unless they met one of the story-choice checks above.
    return itr.get("source") != "button"


def _pending_from_choice_interactions(interactions: list[dict]) -> dict | None:
    """Build a choice pending block from live story-choice interactions."""
    items = []
    for itr in _normalize_interaction_disabled(interactions or []):
        if not _interaction_is_story_choice_candidate(itr):
            continue
        label = _button_display_label(itr)
        if not label:
            continue
        item: dict[str, Any] = {
            "label": label,
            "is_caption": bool(itr.get("caption")),
            "is_disabled": bool(
                itr.get("disabled") and not itr.get("caption")
            ),
        }
        if itr.get("annotation"):
            item["annotation"] = itr["annotation"]
        interaction_id = itr.get("id")
        # Button-backed focus-list choices synthesize internal IDs such as
        # "_focus_list:[Enter the basement.]". Those IDs are useful for
        # interaction resolution, but if copied into pending choices they
        # replace the numeric display key and leak as:
        # "_focus_list:[...]: [...]".
        if (
            interaction_id is not None
            and not (
                itr.get("source") == "button"
                and str(interaction_id).startswith("_focus_list:")
            )
        ):
            item["id"] = interaction_id
        items.append(item)
    if not items:
        return None
    return {
        "type": "choice_request",
        "id": "",
        "choices": items,
        "full_items": items,
    }


_ROADWARDEN_INVENTORY_DETAIL_FIELD = "item_detailedmenu"


def _is_roadwarden_inventory_detail_selector(
    btn: dict,
    interaction: dict | None,
) -> bool:
    """Return True for Roadwarden inventory buttons selecting an item."""
    action_strs = []
    if interaction:
        action_strs.extend(interaction.get("action_strs") or [])
    action_strs.extend(btn.get("action_strs") or [])
    return any(
        _ROADWARDEN_INVENTORY_DETAIL_FIELD in str(action)
        for action in action_strs
    )


def _looks_like_internal_label(label: str) -> bool:
    """Heuristic for raw ids such as ``goblinspear`` leaking as labels."""
    if not label:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    return all(ch in allowed for ch in label) and not any(ch.isspace() for ch in label)


def _is_uncategorized_roadwarden_inventory_selector(
    btn: dict,
    interaction: dict | None,
) -> bool:
    """Return True for raw inventory ids without game-provided display metadata.

    Roadwarden's inventory screen can expose helper buttons labelled with item
    ids (for example ``goblinspear``) when no category/display label is
    registered for them. They should remain actionable, but should not be
    mixed into ordinary navigation.
    """
    if _button_category_value(btn, interaction):
        return False
    label = str(btn.get("label") or (interaction or {}).get("display_label") or "").strip()
    return (
        _is_roadwarden_inventory_detail_selector(btn, interaction)
        and _looks_like_internal_label(label)
    )


def _button_category_value(
    btn: dict,
    interaction: dict | None = None,
) -> Any:
    """Return private or public button category metadata."""
    explicit = (
        btn.get("_category")
        or btn.get("category")
        or (interaction.get("category") if interaction else None)
    )
    if explicit:
        return explicit
    # The shim typed the matching interaction (its categorizer sees the
    # actions; the state button entry does not).  Navigation chrome such
    # as a quick menu's FileLoad ("Q.Load") must not fall through to a
    # numbered OTHER BUTTONS entry on every read.
    if interaction and interaction.get("type") == "nav":
        return "navigation"
    return None


def _button_display_label(btn: dict) -> str:
    """Return a cleaned display label for scraped screen buttons."""
    label = str(btn.get("label") or btn.get("display_label") or "").strip()
    if str(btn.get("screen") or "") == "_focus_list":
        while label.startswith("\u2022"):
            label = label[1:].strip()
    return label


def _mark_disabled(label: str) -> str:
    """Append the ``(disabled)`` marker unless the label already ends
    with one \u2014 Roadwarden's own unavailable options carry a game-side
    "(disabled)" suffix, and the doubled marker ("... (disabled)
    (disabled)") read as a render glitch in agent output."""
    if label.rstrip().endswith("(disabled)"):
        return label
    return f"{label} (disabled)"


def _is_pending_input_prompt_echo(text: Any, pending: dict | None) -> bool:
    """Return True when screen text duplicates the active input prompt."""
    if not pending or pending.get("type") not in ("input", "input_request"):
        return False
    prompt = _normalize_input_prompt(pending.get("prompt"))
    return _display_text(text).strip() == prompt.strip()


# ---------------------------------------------------------------------------
# Text formatting (for agents and CLI)
# ---------------------------------------------------------------------------

def format_wait_text(
    data: dict, fmt: str = "text", *, anomalies: str = "errors",
) -> dict:
    """Render wait data, attaching any agent-visible anomaly note.

    See ``anomaly_display_lines`` for what *anomalies* selects.
    """
    return _attach_anomaly_note(
        _format_wait_text_core(data, fmt), data, anomalies)


def _format_wait_text_core(data: dict, fmt: str = "text") -> dict:
    """Format structured wait data.

    fmt:
      "text"  — separate keys: text, status, pending, buttons (default)
      "json"  — structured dicts (topics, choices as lists)
      "quiet" — same as text but suppresses navigation/info/stats
    """
    if fmt == "json":
        return _format_wait_json(data)

    quiet = fmt == "quiet"
    out: dict[str, Any] = {}

    # Pending metadata.
    pending = data.get("pending")

    # Story text.  When an overlay is active and current overlay text is
    # available, drop historical screen_text events for that overlay; the
    # current screen snapshot is more authoritative and avoids modal repeats.
    story = data.get("story", [])
    story_seen_lines: set[str] = set()
    story_seen_occurrences: set[str] = set()
    story_seen_delivery_ids: set[int] = set()
    if story:
        lines = []
        render_sections = []
        skip_screen_text_story = bool(
            data.get("_overlay_active") and data.get("_screen_texts"))
        for item in story:
            if (
                skip_screen_text_story
                and item.get("source") == "screen_text"
                and not item.get("passive_overlay_snapshot")
            ):
                continue
            if (
                item.get("source") == "screen_text"
                and _is_pending_input_prompt_echo(item.get("text"), pending)
            ):
                continue
            itype = item.get("type", "")
            line = None
            if itype == "dialogue":
                prefix = "[user] " if item.get("user") else ""
                line = f"{prefix}[{item['character']}] {item['text']}"
            elif itype == "narration":
                line = item["text"]
            elif itype == "anomaly":
                line = item["text"]
            delivery_id = item.get("overlay_delivery_id")
            identified_overlay = (
                item.get("passive_overlay_snapshot")
                and type(delivery_id) is int
            )
            occurrence_id = item.get("_occurrence_id")
            identified_occurrence = isinstance(occurrence_id, str)
            append_line = False
            if line and identified_overlay:
                if delivery_id not in story_seen_delivery_ids:
                    story_seen_delivery_ids.add(delivery_id)
                    append_line = True
            elif line and identified_occurrence:
                if occurrence_id not in story_seen_occurrences:
                    story_seen_occurrences.add(occurrence_id)
                    append_line = True
            elif line and line not in story_seen_lines:
                story_seen_lines.add(line)
                append_line = True
            if append_line:
                lines.append(line)
                section = {"channel": "text", "text": line}
                if identified_overlay:
                    section["delivery_ids"] = [delivery_id]
                elif identified_occurrence:
                    section["occurrence_ids"] = [occurrence_id]
                if type(item.get("_bridge_seq")) is int:
                    section["_bridge_seq"] = item["_bridge_seq"]
                render_sections.append(section)
        if lines:
            out["text"] = "\n".join(lines)
            # Handlers merge promoted waits using this exact occurrence plan.
            # Older shims still fall back to the aggregate text reconstruction.
            out["_story_render_sections"] = render_sections

    # Status text (suppressed in quiet mode).
    if not quiet:
        status = data.get("status", {})
        if status:
            status_lines = []
            # Stats updates are diagnostics. The current state footer already
            # carries player-facing stats, so keep them in the status channel
            # rather than as raw story/event lines.
            if status.get("stats"):
                parts = []
                for stat in status["stats"]:
                    if not isinstance(stat, dict):
                        continue
                    name = stat.get("stat")
                    if not name:
                        continue
                    value = stat.get("value")
                    delta = stat.get("delta")
                    if isinstance(delta, (int, float)) and delta:
                        parts.append(f"{name}: {value} ({delta:+g})")
                    elif delta == "removed":
                        parts.append(f"{name}: removed")
                    else:
                        parts.append(f"{name}: {value}")
                if parts:
                    status_lines.append("(stats updated: " + ", ".join(parts) + ")")
            for label in status.get("auto_skipped", []):
                status_lines.append(f"(auto-advance: {label})")
            for r in status.get("resolved", []):
                label = r["label"]
                if label == "__aborted__":
                    continue  # Engine flow change — not a real choice.
                prefix = "[user chose]" if r.get("by") == "user" else "[chose]"
                status_lines.append(f"{prefix} {label}")
            if status.get("inventory"):
                status_lines.append("[inventory updated] " + ", ".join(status["inventory"]))
            if status_lines:
                out["status"] = "\n".join(status_lines)

    # Pending and buttons — handle overlay detection.
    btns = data.get("buttons", [])
    overlay_active = data.get("_overlay_active", False)

    has_actionable = False
    if pending and pending.get("type") == "choice":
        # Captions are prompt text, not selectable — must not count as
        # an actionable choice (matches format_pending_text's has_enabled).
        has_actionable = any(
            not c.get("disabled") and not c.get("caption")
            for c in pending.get("choices", [])
        )
    elif pending:
        has_actionable = True

    if pending and not overlay_active:
        seen_texts = {
            _story_echo_key(item.get("text"))
            for item in data.get("story") or []
            if isinstance(item, dict)
            and item.get("type") in ("narration", "dialogue")
            and str(item.get("text") or "").strip()
        }
        out["pending"] = format_pending_text(pending, seen_texts=seen_texts)
        out["_pending_raw"] = data.get("_pending_raw")
        if isinstance(data.get("_actionable_snapshot"), dict):
            out["_actionable_snapshot"] = data["_actionable_snapshot"]

    # Overlay screen texts (e.g. journal content, character stats).
    screen_texts = data.get("_screen_texts")
    if screen_texts:
        unique_screen_texts = []
        seen_screen_texts: set[str] = set()
        for text in screen_texts:
            if _is_pending_input_prompt_echo(text, pending):
                continue
            text = _display_text("\n".join(
                line for line in str(text).splitlines()
                if line not in story_seen_lines
            )).strip()
            if not text:
                continue
            if text not in seen_screen_texts:
                seen_screen_texts.add(text)
                unique_screen_texts.append(text)
        if unique_screen_texts:
            out["screen_text"] = "\n".join(unique_screen_texts)
            if out.get("_story_render_sections"):
                # A render plan owns both story channels. Keep the current
                # modal body in that plan when narration also arrived.
                out["_story_render_sections"].append({
                    "channel": "screen_text", "text": out["screen_text"],
                })

    note = hidden_menu_note(data)
    if note:
        out["overlay_note"] = note

    if btns:
        out["_buttons_raw"] = btns
        if not has_actionable or overlay_active:
            cats = categorize_buttons_indexed(btns)
            if quiet:
                cats = {k: v for k, v in cats.items()
                        if k not in ("navigation", "info")}
            formatted = format_categorized_buttons(
                cats, data.get("_button_categories"))
            if formatted:
                out["buttons"] = formatted
            out["_buttons_categorized"] = cats

    if _should_emit_wait_ended(data, pending, btns):
        out["ended"] = True
    footer = data.get("_footer")
    if footer and isinstance(footer, str):
        out["_footer"] = footer
    if not out:
        out["text"] = "(no new events)"
    return out


def _has_actionable_wait_buttons(buttons: Any) -> bool:
    if not isinstance(buttons, list):
        return False
    return any(
        isinstance(button, dict)
        and bool(str(button.get("label") or "").strip())
        and not item_is_disabled(button)
        for button in buttons
    )


def _has_actionable_wait_pending(pending: Any) -> bool:
    if not isinstance(pending, dict):
        return False
    actions = pending.get("actions") or []
    if any(
        isinstance(action, dict)
        and bool(str(action.get("label") or "").strip())
        and not item_is_disabled(action)
        for action in actions
    ):
        return True
    if pending.get("type") != "choice":
        return True
    choices = pending.get("choices") or []
    return any(
        isinstance(choice, dict)
        and not _is_suppressed_pending_choice(choice)
        and not choice.get("disabled")
        and not choice.get("caption")
        for choice in choices
    )


def _should_emit_wait_ended(data: dict, pending: Any, buttons: Any) -> bool:
    return bool(
        data.get("_game_terminal")
        or (
            data.get("ended")
            and not _has_actionable_wait_pending(pending)
            and not _has_actionable_wait_buttons(buttons)
        )
    )


def _format_wait_json(data: dict) -> dict:
    """Format wait data as structured JSON (no text rendering)."""
    out: dict[str, Any] = {}

    story = data.get("story", [])
    if story:
        out["story"] = [
            {
                key: value for key, value in item.items()
                if not str(key).startswith("_")
            }
            if isinstance(item, dict) else item
            for item in story
        ]

    status = data.get("status", {})
    if status:
        out["status"] = status

    pending = data.get("pending")
    if pending:
        out["pending"] = pending
        out["_pending_raw"] = data.get("_pending_raw")

    note = hidden_menu_note(data)
    if note:
        out["overlay_note"] = note

    btns = data.get("buttons", [])
    if btns:
        cats = categorize_buttons(btns)
        if cats:
            out["buttons"] = cats
        out["_buttons_raw"] = btns

    if _should_emit_wait_ended(data, pending, btns):
        out["ended"] = True
    return out


def _format_pending_action_groups(
    actions: list[dict],
    extra_categories: dict[str, dict] | None = None,
) -> list[str]:
    """Group categorized pending actions under `--- CATEGORY ---` headers.

    Mirrors format_interactions' per-category rendering (get_category_meta +
    compact/pipe vs. numbered) so the pending / wait() block shows trailing
    non-choice buttons (e.g. Roadwarden's quick-menu navigation) the same way
    state()'s screen rendering does, rather than as bare `act "label"` lines.
    """
    meta, _order = get_category_meta(extra_categories)
    buckets: Dict[str, List[dict]] = {}
    for action in actions:
        buckets.setdefault(action["category"], []).append(action)

    lines: list[str] = []
    for cat in _ordered_categories(buckets.keys(), extra_categories):
        entries = buckets.get(cat)
        if not entries:
            continue
        header_text, _hint, compact = meta.get(
            cat, (cat.upper(), 'act "<label>"', False))
        lines.append("")
        lines.append(f"--- {header_text} ---")
        if compact:
            parts = []
            for action in entries:
                label = action.get("label", "?")
                if item_is_disabled(action):
                    parts.append(_mark_disabled(label))
                else:
                    parts.append(label)
            lines.append("  " + "  |  ".join(parts))
        else:
            for action in entries:
                label = action.get("label", "?")
                annotation = action.get("annotation", "")
                ann_suffix = f" — {annotation}" if annotation else ""
                if item_is_disabled(action):
                    lines.append(f"  {_mark_disabled(label)}{ann_suffix}")
                elif action.get("index") is not None:
                    lines.append(f"  {action['index']}: {label}{ann_suffix}")
                else:
                    lines.append(f'  act "{label}"{ann_suffix}')
    return lines


def format_pending_text(
    pending: dict, *, seen_texts: set[str] | None = None,
) -> str:
    """Format structured pending data as text.

    *seen_texts* (story-echo keys of narration/dialogue already rendered
    in the same output) suppresses a caption that merely repeats one of
    them: Roadwarden's epilogue menu carries the paragraph as its own
    caption, so the agent read every paragraph twice.
    """
    if pending["type"] == "choice":
        choices = [
            c for c in pending.get("choices", [])
            if not _is_suppressed_pending_choice(c)
        ]
        auto_advancing = pending.get("auto_advancing", False)
        # Captions are prompt text, not selectable (index=None, caption=True).
        has_enabled = any(
            not c.get("disabled") and not c.get("caption") for c in choices)
        if auto_advancing:
            lines = ["--- CHOICE (auto-advancing) ---"]
        elif not has_enabled:
            lines = ["--- NO AVAILABLE CHOICES ---"]
        else:
            lines = ["--- CHOICE REQUIRED ---"]
        # Caption(s): the menu's prompt line(s), shown unnumbered up top.
        for c in choices:
            if c.get("caption"):
                raw = c.get("label") or ""
                cap = _strip_renpy_text_tags(raw).strip()
                if not cap:
                    continue
                # Key from the RAW label: keying the stripped caption would
                # strip a second time, turning an escaped brace ("{{b}" ->
                # "{b}") into a tag and erasing a distinct caption.
                if seen_texts and _story_echo_key(raw) in seen_texts:
                    continue
                lines.append(f"  | {cap}")
        # Enabled choices (numbered, or keyed by ID when available).
        for c in choices:
            if c.get("disabled") or c.get("caption"):
                continue
            key = c.get("id", c["index"])
            ann = c.get("annotation")
            suffix = f"  ({ann})" if ann and ann != str(key) else ""
            lines.append(f"  {key}: {c['label']}{suffix}")
        # Disabled choices (shown as unavailable hints).
        # Skip empty/whitespace labels and bare "(disabled)" sentinels —
        # they are placeholder entries that provide no information.
        def _is_real_disabled(c):
            lbl = (c.get("label") or "").strip()
            return bool(lbl) and lbl != "(disabled)"
        disabled = [c for c in choices
                    if c.get("disabled") and _is_real_disabled(c)]
        if disabled:
            lines.append("")
            lines.append("  Unavailable:")
            for c in disabled:
                label = c["label"]
                lines.append(f"  - {label}")
        # Trailing non-choice actions (promoted CTAs + screen navigation).
        # Uncategorized actions (mod-promoted story buttons) keep their bare
        # `act "label"` lines.  Categorized actions (e.g. quick-menu
        # navigation) are grouped under their `--- CATEGORY ---` header — the
        # same rendering state() uses — instead of reading as loose choices.
        actions = [
            action for action in pending.get("actions") or []
            if (
                isinstance(action, dict)
                and str(action.get("label") or "").strip()
                and not _is_suppressed_pending_action(action)
                and (action.get("category") or not item_is_disabled(action))
            )
        ]
        bare_actions = [a for a in actions if not a.get("category")]
        categorized_actions = [a for a in actions if a.get("category")]
        if bare_actions:
            lines.append("")
            for action in bare_actions:
                label = action.get("label", "?")
                annotation = action.get("annotation", "")
                if annotation:
                    lines.append(f'  act "{label}" — {annotation}')
                else:
                    lines.append(f'  act "{label}"')
        if categorized_actions:
            lines.extend(_format_pending_action_groups(
                categorized_actions, pending.get("_button_categories")))
        if auto_advancing:
            lines.append("")
            lines.append("This choice will auto-advance. Use wait() to continue.")
        elif not has_enabled:
            lines.append("")
            lines.append(
                "No enabled choices are currently available. "
                "Use a visible screen/navigation action if shown, "
                "or load/rollback if this is a dead end."
            )
        else:
            lines.append('Use act <N> or act "<label>" to respond.')
        return "\n".join(lines)
    elif pending["type"] == "input":
        prompt = pending.get("prompt", "Enter text")
        default = pending.get("default")
        if default not in (None, ""):
            prompt = f"{prompt} (default: {default})"
        return (
            f"--- INPUT REQUIRED ---\n{prompt}\n\n"
            "Use input_text('your text') in MCP, or "
            'python vnflight.py input "your text" in the CLI.'
        )
    return f"--- PENDING: {pending['type']} ---"


# ---------------------------------------------------------------------------
# Anomaly formatting
# ---------------------------------------------------------------------------

# Detail keys lifted from `details` into the rendered summary, ordered by
# importance. Skipped when missing / empty so summaries stay short.
ANOMALY_VISIBILITY_MODES = ("off", "errors", "all")

# Anomaly kinds that mean the GAME IS BROKEN, not that a scrape looked odd.
# Everything else (duplicate_buttons, choice_count_mismatch,
# empty_screen_with_choices, orphaned_choice_screen, menu_index_out_of_range)
# is diagnostic: it was interleaved with dialogue until 578c4247 moved all
# anomalies into the status channel, where nothing rendered them. Errors ride
# the agent-facing channel again; the noisy kinds stay opt-in.
ANOMALY_ERROR_KINDS = frozenset({"renpy_exception"})

_ANOMALY_NOTE_MAX_CHARS = 240
_ANOMALY_NOTE_MAX_LINES = 3


def anomaly_is_error(kind: Any, message: Any = "") -> bool:
    """True when this anomaly means the game itself failed."""
    if str(kind or "").strip().lower() in ANOMALY_ERROR_KINDS:
        return True
    lowered = str(message or "").lower()
    return "ren'py exception" in lowered or "renpy exception" in lowered


def _anomaly_kind_and_message(entry: Any) -> tuple[str, str]:
    """Split a status string ("kind: msg") or a raw anomaly event."""
    if isinstance(entry, dict):
        details = entry.get("details")
        details = details if isinstance(details, dict) else {}
        kind = (
            entry.get("kind")
            or entry.get("type")
            or details.get("type")
            or "unknown"
        )
        if str(kind) == "anomaly":
            kind = details.get("type") or entry.get("kind") or "unknown"
        kind = str(kind)
        # Same text as the wait path builds from the event stream
        # (build_wait_data: "kind: message"), so a latched copy of an
        # anomaly and its streamed original render as one line.
        message = entry.get("message") or details.get("message")
        if message:
            return kind, str(message)
        text = format_anomaly_text(entry)
        if text.startswith(kind):
            text = text[len(kind):].strip()
        return kind, text
    text = str(entry or "")
    kind, sep, message = text.partition(": ")
    if not sep:
        return "unknown", text
    return kind, message


def anomaly_display_lines(entries: Any, mode: str = "errors") -> list[str]:
    """Agent-facing lines for anomalies, per visibility mode.

    mode: "off" (render nothing), "errors" (game failures only, default),
    "all" (every anomaly, including scrape diagnostics).
    """
    if not entries or mode == "off":
        return []
    if mode not in ANOMALY_VISIBILITY_MODES:
        mode = "errors"
    if isinstance(entries, (str, dict)):
        entries = [entries]
    lines: list[str] = []
    for entry in entries:
        kind, message = _anomaly_kind_and_message(entry)
        is_error = anomaly_is_error(kind, message)
        if mode == "errors" and not is_error:
            continue
        flat = " ".join(str(message or "").split())
        if len(flat) > _ANOMALY_NOTE_MAX_CHARS:
            flat = flat[:_ANOMALY_NOTE_MAX_CHARS - 1].rstrip() + "…"
        label = "GAME ERROR" if is_error else "anomaly"
        line = f"{label} ({kind})"
        if flat:
            line += f": {flat}"
        if is_error:
            line = line.rstrip(". ")
            line += (
                ". The game may be showing its exception screen"
                " (Ignore continues, Rollback rewinds, Reload restarts);"
                " story choices will not respond until it is dismissed."
            )
        if line not in lines:
            lines.append(line)
    return lines[:_ANOMALY_NOTE_MAX_LINES]


def _anomaly_latch_is_current(anomaly: Any) -> bool:
    """True while a latched anomaly still describes the game's situation.

    The bridge marks the latch resolved when a later story event arrives
    (dialogue, menu, lifecycle boundary), so "current" means no story
    progress since the anomaly: an exception screen that is still up stays
    reported however long it has been up, and a recovered one goes quiet
    as soon as play continues. Age plays no part.
    """
    if not isinstance(anomaly, dict):
        return False
    if not isinstance(anomaly.get("_latched_at"), (int, float)):
        # Every bridge we ship stamps the latch (same artifact, same build).
        # An unstamped latch is malformed, not old.
        return False
    return "_resolved_at" not in anomaly


def _anomaly_entries_from_data(data: Any) -> list:
    """Collect anomaly entries from wait data or state data."""
    if not isinstance(data, dict):
        return []
    entries: list = []
    status = data.get("status")
    if isinstance(status, dict):
        found = status.get("anomalies")
        if isinstance(found, list):
            entries.extend(found)
    found = data.get("anomalies")
    if isinstance(found, list):
        entries.extend(found)
    elif isinstance(found, (str, dict)):
        entries.append(found)
    return entries


def _attach_anomaly_note(out: Any, data: Any, mode: str) -> Any:
    """Set out["anomaly_note"] when visible anomalies are present."""
    if not isinstance(out, dict):
        return out
    lines = anomaly_display_lines(_anomaly_entries_from_data(data), mode)
    if lines:
        out["anomaly_note"] = "\n".join(lines)
    return out


ANOMALY_DETAIL_KEYS = (
    "screen", "label", "kind", "request_id", "index", "expected", "actual",
    "exception", "message", "missing", "duplicates",
)


def format_anomaly_text(ev: dict) -> str:
    """Build a human-readable summary from a vnflight anomaly event.

    Anomaly events ship as ``{"type": "anomaly", "kind": "...", "details": {...}}``
    with no ``text`` field — the watchdog/shim layer assumes consumers
    render ``kind`` + ``details``. This helper is the single canonical
    formatter shared between the vnflight CLI / tool handlers and the
    harness VN plugin so all anomaly surfaces read the same way.
    """
    if not isinstance(ev, dict):
        return ""
    if ev.get("text"):
        return str(ev["text"])
    kind = ev.get("kind") or "unknown"
    details = ev.get("details") or {}
    parts: list = [str(kind)]
    if isinstance(details, dict):
        for key in ANOMALY_DETAIL_KEYS:
            val = details.get(key)
            if val in (None, "", [], {}):
                continue
            if isinstance(val, (list, tuple)):
                rendered = ", ".join(str(x) for x in val[:3])
                if len(val) > 3:
                    rendered += f", … (+{len(val) - 3})"
                parts.append(f"{key}=[{rendered}]")
            else:
                rendered = str(val)
                if len(rendered) > 80:
                    rendered = rendered[:77] + "..."
                parts.append(f"{key}={rendered}")
    return " ".join(parts)


def anomaly_summary(ev: dict) -> dict:
    """Compact dict shape used everywhere we surface an anomaly:
    {kind, summary, details}. Both consumers (cli.py + harness) read
    `summary` for user display and keep `details` for inspection /
    debugging."""
    if not isinstance(ev, dict):
        return {}
    return {
        "kind": ev.get("kind") or "unknown",
        "summary": format_anomaly_text(ev),
        "details": ev.get("details") or {},
    }


# ---------------------------------------------------------------------------
# State formatting
# ---------------------------------------------------------------------------

def build_state_data(raw: dict) -> dict:
    """Build structured state data from raw bridge /state response.

    Data priority: game_state (live, post-transform) > pending_request
    (snapshot from menu creation) > screen (cached screen_content) >
    transcript (historical).
    """
    status = raw.get("status", "unknown")
    lifecycle = classify_lifecycle(raw)
    data: dict[str, Any] = {
        "status": status,
        "_effective_status": lifecycle["effective_status"],
        "_lifecycle": lifecycle,
    }
    # The bridge latches the last anomaly on /state and /status. Keep it on
    # the state data so renderers (and JSON consumers) can see a crashed game
    # without diffing the event stream — but only while it is still current
    # (unresolved: no story progress since). A resolved latch is kept under
    # _stale_anomaly for diagnostics and never presented as happening now.
    sticky_anomaly = raw.get("anomaly")
    if isinstance(sticky_anomaly, dict) and sticky_anomaly:
        if _anomaly_latch_is_current(sticky_anomaly):
            data["anomalies"] = [sticky_anomaly]
        else:
            data["_stale_anomaly"] = sticky_anomaly

    game_state = raw.get("game_state") or {}
    screen = raw.get("screen")
    button_screen = None if lifecycle.get("stale_menu_overlay") else screen
    pending = raw.get("pending_request")
    if lifecycle.get("stale_main_menu_pending"):
        pending = None
    live_interactions_provided = False
    canonical_live_interactions_provided = False
    live_interactions = None
    if "interactions" in game_state:
        live_interactions_provided = True
        canonical_live_interactions_provided = True
        live_interactions = game_state["interactions"]
    elif isinstance(button_screen, dict) and "interactions" in button_screen:
        live_interactions_provided = True
        live_interactions = button_screen.get("interactions") or []

    # Stats: game_state (live) > pending (snapshot) > transcript. The bridge
    # deliberately freezes the terminal run snapshot while Ren'Py resets its
    # store, but that snapshot is historical once the main menu is visible and
    # must not be presented as live state.
    show_run_state = (
        lifecycle.get("context") not in ("main_menu", "menu")
        and raw.get("gameplay_seen") is not False
    )
    stats = {}
    if show_run_state:
        stats = game_state.get("stats") or {}
        if not stats:
            stats = raw.get("stats") or raw.get("config", {}).get("stats", {})
        if not stats and pending:
            stats = pending.get("stats", {})
        if not stats:
            for ev in reversed(raw.get("transcript", [])):
                ev_stats = ev.get("stats")
                if ev_stats and isinstance(ev_stats, dict):
                    stats = ev_stats
                    break
        if stats:
            summary = stats.get("_summary")
            if summary:
                data["_stats_summary"] = summary
            if stats.get("_suppress_brief"):
                data["_suppress_brief_stats"] = True
            inv_label = stats.get("_inventory_label")
            data["stats"] = {
                k: v for k, v in stats.items()
                if not str(k).startswith("_") and v is not None
            }
            if inv_label:
                data["_inventory_label"] = inv_label

    # Inventory: game_state (live) > raw.inventory.
    inv = game_state.get("inventory") if show_run_state else None
    if show_run_state and not inv:
        inv = raw.get("inventory")
    if inv:
        if isinstance(inv, list):
            data["inventory"] = [
                i if isinstance(i, str) else i.get("name", str(i))
                for i in inv
            ]
        elif isinstance(inv, dict):
            data["inventory"] = inv

    config = raw.get("config")
    if config:
        data["config"] = config

    # Pending: game_state is the canonical source for choice labels
    # and order (the shim's scraper runs the augmenter pipeline every
    # tick, so synthesized items and live sensitivity both flow
    # through).  Fall back to pending when game_state is empty.
    if pending:
        ptype = pending.get("type", "")
        if ptype in ("choice_request", "choices"):
            enriched = dict(pending)
            use_live_choice_rows = not _live_choices_duplicate_pending(
                pending,
                game_state,
            )
            if use_live_choice_rows and "choices" in game_state:
                enriched["choices"] = game_state["choices"]
            if use_live_choice_rows and "full_items" in game_state:
                enriched["full_items"] = game_state["full_items"]
            if live_interactions_provided:
                enriched["interactions"] = live_interactions
                has_live_choice_interactions = any(
                    isinstance(interaction, dict)
                    and (
                        interaction.get("source") == "choice"
                        or interaction.get("type") == "choice"
                    )
                    for interaction in (live_interactions or [])
                )
                has_pending_choice_interactions = any(
                    isinstance(interaction, dict)
                    and (
                        interaction.get("source") == "choice"
                        or interaction.get("type") == "choice"
                    )
                    for interaction in (pending.get("interactions") or [])
                )
                if (
                    canonical_live_interactions_provided
                    and not live_interactions
                    and not has_pending_choice_interactions
                    and "choices" not in game_state
                    and "full_items" not in game_state
                ):
                    enriched["choices"] = []
                    enriched["full_items"] = []
                elif (
                    canonical_live_interactions_provided
                    and live_interactions
                    and not has_live_choice_interactions
                    and not has_pending_choice_interactions
                    and "choices" not in game_state
                    and "full_items" not in game_state
                ):
                    enriched["choices"] = []
                    enriched["full_items"] = []
            elif game_state.get("promoted_buttons"):
                enriched["promoted_buttons"] = game_state["promoted_buttons"]
            if game_state.get("_auto_advancing"):
                enriched["_auto_advancing"] = True
            data["pending"] = _build_pending(enriched)
        elif ptype == "input_request":
            data["pending"] = _build_pending(pending)
        data["_pending_raw"] = pending

    # Screen buttons: game_state (post-transform, no ChoiceReturn) >
    # pending.screen_buttons (initial push) > screen.buttons (raw). At an
    # explicit main-menu boundary the separate screen read is newer than the
    # lagging game_state/context pair, so it becomes authoritative.
    interaction_lookup = _interaction_lookup(live_interactions or [])
    if lifecycle.get("screen_main_menu_boundary") and button_screen:
        btn_source = [
            b for b in button_screen.get("buttons", [])
            if "ChoiceReturn" not in b.get("actions", [])
        ]
    elif "screen_buttons" in game_state:
        btn_source = game_state["screen_buttons"]
    elif pending and pending.get("screen_buttons"):
        btn_source = pending["screen_buttons"]
    else:
        raw_btns = button_screen.get("buttons", []) if button_screen else []
        # Filter ChoiceReturn from raw screen (those are shown as choices).
        btn_source = [b for b in raw_btns
                      if "ChoiceReturn" not in b.get("actions", [])]
    if btn_source:
        buttons = []
        for btn in btn_source:
            if item_is_default_focus_chrome(btn):
                continue
            label = _button_display_label(btn)
            if label:
                interaction = interaction_lookup.get(
                    (btn.get("screen", ""), label))
                entry: dict[str, Any] = {"label": label}
                scr = btn.get("screen", "")
                if scr:
                    entry["screen"] = scr
                if _button_is_disabled(btn):
                    entry["disabled"] = True
                if btn.get("annotation"):
                    entry["annotation"] = btn["annotation"]
                idx = btn.get("index")
                if idx is None and interaction:
                    idx = interaction.get("index")
                if idx is not None:
                    entry["index"] = idx
                cat = _button_category_value(btn, interaction)
                if not cat and _is_uncategorized_roadwarden_inventory_selector(
                    btn,
                    interaction,
                ):
                    cat = "raw_item_actions"
                if cat:
                    entry["_category"] = cat
                buttons.append(entry)
        if buttons:
            data["buttons"] = buttons

    # Store interactions for downstream consumers (CLI, MCP).
    if live_interactions_provided:
        data["_interactions"] = _normalize_interaction_disabled(
            live_interactions or [])

    # Capture game-registered button categories from canonical state first,
    # falling back to screen_content for older shim snapshots.
    if game_state.get("button_categories"):
        data["_button_categories"] = game_state["button_categories"]
    elif button_screen and button_screen.get("button_categories"):
        data["_button_categories"] = button_screen["button_categories"]

    # Main-menu pages (About, Help, etc.) have no story-event counterpart.
    # Render their current text without treating gameplay HUD text as narration.
    if lifecycle.get("screen_main_menu_boundary") and button_screen:
        if button_screen.get("texts"):
            data["_screen_texts"] = button_screen["texts"]

    # Overlay detection: the shim sets overlay_active on screen_content
    # events and pending requests when a registered overlay screen is
    # visible.  If active, suppress pending choices and show live
    # screen buttons instead.
    # input_request is never suppressed (it IS the active screen).
    overlay = bool(
        (screen and (
            screen.get("overlay_active") or screen.get("modal_screens")
        ))
        or (pending and (
            pending.get("overlay_active") or pending.get("modal_screens")
        )))
    if overlay:
        ptype = pending.get("type", "") if pending else ""
        # A declared-modal overlay is a full-screen panel: the player sees it
        # INSTEAD of the scene. Record what it covers before the covered menu
        # is dropped, so the response can say so instead of silently
        # presenting a surface with one decision quietly missing.
        modal_tags = _declared_modal_overlay_tags(screen, game_state, pending)
        if modal_tags:
            data["_modal_overlay_screens"] = modal_tags
            # The agent-facing name of the panel, recorded whether or not it
            # covers a numbered menu.  A refusal aimed at a control on the
            # surface UNDERNEATH has no _hidden_menu to read the name from.
            data["_modal_overlay_panel"] = modal_overlay_panel_name(
                modal_tags, screen)
        # Suppress stale pending choices when an overlay is on top.
        if ptype in ("choice_request", "choices"):
            if modal_tags:
                hidden = _hidden_menu_record(
                    modal_tags, data.get("pending"), pending, screen,
                )
                if hidden:
                    data["_hidden_menu"] = hidden
            data.pop("pending", None)
            data.pop("_pending_raw", None)
        data["_overlay_active"] = True
        identity_source = (
            screen if screen and (
                screen.get("overlay_active") or screen.get("modal_screens")
            ) else pending or screen or {}
        )
        identity_tags = sorted({
            str(tag) for tag in (
                list(identity_source.get("modal_screens") or [])
                + list(identity_source.get("overlay_screens") or [])
            ) if str(tag)
        })
        if identity_tags:
            generations = identity_source.get("overlay_generations") or {}
            data["_overlay_snapshot_identity"] = (
                tuple(identity_tags),
                tuple(sorted(
                    (tag, str(generations[tag]))
                    for tag in identity_tags if tag in generations
                )),
            )
        # Show live screen buttons (post-transform from game_state).
        ov_btn_source = (game_state.get("screen_buttons")
                         or (screen.get("buttons", []) if screen else []))
        if ov_btn_source:
            overlay_buttons = []
            for btn in ov_btn_source:
                if item_is_default_focus_chrome(btn):
                    continue
                label = _button_display_label(btn)
                if label:
                    interaction = interaction_lookup.get(
                        (btn.get("screen", ""), label))
                    entry: dict[str, Any] = {"label": label}
                    scr = btn.get("screen", "")
                    if scr:
                        entry["screen"] = scr
                    if _button_is_disabled(btn):
                        entry["disabled"] = True
                    idx = btn.get("index")
                    if idx is None and interaction:
                        idx = interaction.get("index")
                    if idx is not None:
                        entry["index"] = idx
                    cat = _button_category_value(btn, interaction)
                    if not cat and _is_uncategorized_roadwarden_inventory_selector(
                        btn,
                        interaction,
                    ):
                        cat = "raw_item_actions"
                    if cat:
                        entry["_category"] = cat
                    overlay_buttons.append(entry)
            if overlay_buttons:
                data["buttons"] = overlay_buttons
        # Include text owned by the active modal as context (e.g. a confirm
        # prompt or shop resources).  Registered passive overlays may remain
        # visible behind a modal choice screen.  Their cumulative scrollback
        # is delivered chronologically from drainable screen_content events;
        # copying it here as modal context renders the same block a second
        # time and moves it after later narration.
        if screen:
            ov_texts = _modal_overlay_context_texts(screen)
            if ov_texts:
                data["_screen_texts"] = ov_texts

    if not data.get("pending") and not data.get("_overlay_active"):
        live_pending = _pending_from_choice_interactions(
            live_interactions or [])
        if live_pending:
            data["pending"] = _build_pending(live_pending)
            data["_pending_raw"] = live_pending

    # Drop screen buttons that duplicate a pending choice. A `call screen`
    # choice screen (plain Return() buttons) records each option as both a
    # choice-category screen button AND — once a pending choice block is
    # built or synthesized above — a pending choice, so without this the
    # same option renders twice (choices 1-N, then phantom buttons N+1..)
    # and act(N) ranges inflate. Runs after both pending paths so it covers
    # the synthesized case too. Skipped under overlay (pending is popped).
    _pending_block = data.get("pending")
    if (isinstance(_pending_block, dict)
            and _pending_block.get("type") == "choice"
            and data.get("buttons")):
        _choice_label_set = set()
        for _pc in _pending_block.get("choices") or []:
            # Only numbered (enabled, non-caption) choices shadow a button.
            # A button matching a *disabled* choice's label is still a real
            # action and must not be dropped (it would look unavailable).
            if (isinstance(_pc, dict)
                    and _pc.get("index") is not None
                    and not _pc.get("disabled")
                    and not _pc.get("caption")):
                _pcl = (_pc.get("label") or "").strip()
                if _pcl:
                    _choice_label_set.add(_pcl)
        if _choice_label_set:
            # Drop only buttons that are choice DUPLICATES — i.e. in the
            # "choices" category (call-screen Return() buttons the shim
            # tags that way; raw-screen ChoiceReturn buttons are already
            # filtered upstream). A coincidental same-label button in
            # another category (topics/nav/info) is a real action and is
            # kept, so numbered_button_count / act(N) range stay correct.
            def _is_choice_dup(b):
                return (
                    b.get("_category") == "choices"
                    and (b.get("label") or "").strip() in _choice_label_set
                )
            _deduped = [b for b in data["buttons"] if not _is_choice_dup(b)]
            if len(_deduped) != len(data["buttons"]):
                if _deduped:
                    data["buttons"] = _deduped
                else:
                    data.pop("buttons", None)

    return data


def _declared_modal_overlay_tags(*sources: dict | None) -> list:
    """Modal-presentation overlay tags shown, in panel order, first source wins.

    ``modal_overlay_screens`` is additive: a shim or mod that never declares
    one emits nothing, and every caller here degrades to the pre-existing
    layered presentation.
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        tags = []
        for raw in source.get("modal_overlay_screens") or []:
            tag = str(raw)
            if tag and tag not in tags:
                tags.append(tag)
        if tags:
            return tags
    return []


def modal_overlay_panel_name(tags: list, screen: dict | None) -> str:
    """The name to call the panel in agent-facing prose."""
    names = (screen or {}).get("screen_names")
    labelled = []
    for tag in tags:
        display = names.get(tag) if isinstance(names, dict) else None
        labelled.append(str(display) if display else str(tag))
    return " + ".join(labelled)


def _hidden_menu_record(
    tags: list,
    structured_pending: dict | None,
    raw_pending: dict | None,
    screen: dict | None,
) -> dict | None:
    """Describe the scene menu a modal panel is covering.

    The count is the number the menu WOULD have consumed, so the note and the
    restored numbering after the panel closes agree.
    """
    source = structured_pending or raw_pending
    count = pending_numbered_choice_count(source)
    labels = []
    for item in (
        (source or {}).get("choices") or (source or {}).get("full_items") or []
    ):
        if isinstance(item, dict):
            if item.get("disabled") or item.get("is_disabled"):
                continue
            if item.get("caption") or item.get("is_caption"):
                continue
            label = str(item.get("label") or "").strip()
        else:
            label = str(item).strip()
        if label and label not in labels:
            labels.append(label)
    if not count and not labels:
        return None
    return {
        "screens": list(tags),
        "panel": modal_overlay_panel_name(tags, screen),
        "count": count,
        "labels": labels,
    }


def hidden_menu_note(data: dict | None) -> str:
    """One line telling the agent a decision exists behind the open panel."""
    hidden = (data or {}).get("_hidden_menu")
    if not isinstance(hidden, dict):
        return ""
    panel = str(hidden.get("panel") or "the panel")
    count = hidden.get("count")
    if not isinstance(count, int) or count <= 0:
        return ""
    return (
        "Underlying menu hidden behind {}: {} choice{} "
        "(close the panel to act)".format(
            panel, count, "" if count == 1 else "s")
    )


def _modal_overlay_panel_texts(screen: dict) -> list:
    """Rows owned by the declared-modal panels, in panel order.

    A modal panel replaces the scene, so its body is the story text — the map
    and HUD rows still in the raw scrape are underneath it and are not what
    the player is reading.
    """
    tags = _declared_modal_overlay_tags(screen)
    by_screen = screen.get("overlay_texts_by_screen")
    if not tags or not isinstance(by_screen, dict):
        return []
    rows: list = []
    for tag in tags:
        raw_rows = by_screen.get(tag)
        if not isinstance(raw_rows, list):
            continue
        rows.extend(raw_rows)
    return rows


def _modal_overlay_context_texts(screen: dict) -> list:
    """Return visible text owned by the active blocking/modal layer.

    ``overlay_texts_by_screen`` identifies rows contributed by registered
    overlays.  A passive terminal can stay visible behind an unrelated modal
    choice screen, so those rows must be subtracted from the flattened
    ``texts`` scrape unless that contributor is itself the active modal.
    Older shims without contributor provenance retain the legacy fallback.
    """
    panel_rows = _modal_overlay_panel_texts(screen)
    if panel_rows:
        return panel_rows
    texts = list(screen.get("texts") or [])
    overlay_active = bool(screen.get("overlay_active"))
    modal_screens = {
        str(tag) for tag in (screen.get("modal_screens") or []) if str(tag)
    }
    raw_by_screen = screen.get("overlay_texts_by_screen")

    if not modal_screens or not isinstance(raw_by_screen, dict):
        return (
            list(screen.get("overlay_texts") or texts)
            if overlay_active else texts
        )

    passive_rows: dict[str, int] = {}
    modal_rows: list = []
    for raw_tag, raw_rows in raw_by_screen.items():
        tag = str(raw_tag)
        rows = list(raw_rows or []) if isinstance(raw_rows, list) else []
        if tag in modal_screens:
            modal_rows.extend(rows)
            continue
        for row in rows:
            key = str(row)
            passive_rows[key] = passive_rows.get(key, 0) + 1

    owned = []
    for row in texts:
        key = str(row)
        remaining = passive_rows.get(key, 0)
        if remaining:
            passive_rows[key] = remaining - 1
            continue
        owned.append(row)
    for row in modal_rows:
        if row not in owned:
            owned.append(row)
    return owned


def categorize_buttons(buttons: list[dict]) -> dict[str, list[str]]:
    """Group buttons by category (topics, navigation, info, etc.).

    Returns a dict of category -> list of labels. If a button has a
    ``_category`` field (set by game-specific screen transforms), that
    is used directly. Otherwise falls back to action-based heuristics
    via _categorize_button().
    """
    by_cat: dict[str, list[str]] = {}
    for b in buttons:
        if item_is_default_focus_chrome(b):
            continue
        cat = _button_category_value(b) or _categorize_button(b)
        if cat is None:
            continue
        label = _button_display_label(b)
        if not label.strip():
            continue
        if b.get("disabled"):
            label = _mark_disabled(label)
        by_cat.setdefault(cat, []).append(label)
    return by_cat


def categorize_buttons_indexed(buttons: list[dict]) -> dict[str, list[dict]]:
    """Like categorize_buttons but preserves each button's shim index.

    Returns {category: [{"label", "index", "disabled"}, ...]}. The raw
    index is preserved for diagnostics/fallbacks; rendering assigns separate
    display indices for visible clickable rows.
    """
    by_cat: dict[str, list[dict]] = {}
    for flat_idx, b in enumerate(buttons, 1):
        if item_is_default_focus_chrome(b):
            continue
        cat = _button_category_value(b) or _categorize_button(b)
        if cat is None:
            continue
        label = _button_display_label(b)
        if not label.strip():
            continue
        # Use the button's own index if present, else its position in
        # the input list (post-transform).
        idx = b.get("index", flat_idx)
        entry = {
            "label": label,
            "index": idx,
            "disabled": bool(b.get("disabled")),
        }
        if b.get("annotation"):
            entry["annotation"] = b["annotation"]
        by_cat.setdefault(cat, []).append(entry)
    return by_cat


def _ordered_categories(categories: Iterable[str], extra_categories: dict[str, dict] | None = None) -> list[str]:
    """Return formatter category order with unknown categories inserted sensibly."""
    _meta, order = get_category_meta(extra_categories)
    order = list(order)
    insert_idx = order.index("navigation") if "navigation" in order else len(order)
    for cat in categories:
        if cat not in order:
            order.insert(insert_idx, cat)
            insert_idx += 1
    if "raw_item_actions" in order:
        order.remove("raw_item_actions")
        raw_idx = order.index("navigation") if "navigation" in order else len(order)
        order.insert(raw_idx, "raw_item_actions")
    seen = set()
    return [cat for cat in order if not (cat in seen or seen.add(cat))]


def _numbered_category(cat: str, meta: dict[str, tuple]) -> bool:
    """Return whether this category renders numeric action targets."""
    if cat == "info":
        return False
    _header, _hint, compact = meta.get(cat, (cat.upper(), None, False))
    return not compact


def _with_display_indices(
    by_cat: dict[str, list],
    extra_categories: dict[str, dict] | None = None,
) -> dict[str, list]:
    """Clone categorized entries and assign contiguous visible indices.

    Raw shim indices can have gaps because hidden/info/disabled widgets still
    occupy positions.  Display indices are only for visible clickable rows; the
    raw ``index`` remains available for diagnostics and fallback matching.
    """
    meta, _base_order = get_category_meta(extra_categories)
    order = _ordered_categories(by_cat.keys(), extra_categories)
    out: dict[str, list] = {}
    display_idx = 1
    for cat in order:
        items = by_cat.get(cat)
        if not items:
            continue
        numbered = _numbered_category(cat, meta)
        cloned = []
        for item in items:
            if isinstance(item, dict):
                entry = dict(item)
                if numbered and not item_is_disabled(entry):
                    if entry.get("display_index") is None:
                        entry["display_index"] = display_idx
                    display_idx += 1
                cloned.append(entry)
            else:
                cloned.append(item)
                if numbered:
                    display_idx += 1
        out[cat] = cloned
    return out


def button_label_for_display_index(
    buttons: list[dict],
    idx: int | None,
    extra_categories: dict[str, dict] | None = None,
) -> str | None:
    """Return the label shown for a visible button display index."""
    if idx is None:
        return None
    cats = _with_display_indices(categorize_buttons_indexed(buttons), extra_categories)
    for items in cats.values():
        for item in items:
            if isinstance(item, dict) and item.get("display_index") == idx:
                label = str(item.get("label", "")).strip()
                return label or None
    return None


def numbered_button_count(
    buttons: list[dict] | None,
    extra_categories: dict[str, dict] | None = None,
) -> int:
    """How many buttons render with an agent-visible number.

    Disabled/info/compact-category rows don't get a number, so this is the
    button half of the visible-index range used to detect out-of-range acts.
    """
    if not buttons:
        return 0
    cats = _with_display_indices(categorize_buttons_indexed(buttons), extra_categories)
    return sum(
        1
        for items in cats.values()
        for item in items
        if isinstance(item, dict) and item.get("display_index") is not None
    )


def format_categorized_buttons(
    by_cat: dict[str, list],
    extra_categories: dict[str, dict] | None = None,
) -> str:
    """Render categorized buttons as text with headers and numbering.

    Accepts either {cat: [label_string]} (legacy — uses a continuous
    local counter) or {cat: [{"label", "index", "disabled"}]}. Dict entries
    render with contiguous visible display indices; raw shim indices remain
    available to resolvers as a fallback.
    """
    meta, _base_order = get_category_meta(extra_categories)
    by_cat = _with_display_indices(by_cat, extra_categories)
    order = _ordered_categories(by_cat.keys(), extra_categories)

    # Detect whether entries are dicts (indexed) or bare strings.
    _first_item = None
    for cat in order:
        items = by_cat.get(cat) or []
        if items:
            _first_item = items[0]
            break
    use_real_indices = isinstance(_first_item, dict)

    blocks = []
    num = 1  # Only used in legacy mode.
    for cat in order:
        items = by_cat.get(cat)
        if not items:
            continue
        cat_meta = meta.get(cat, (cat.upper(), None, False))
        header, _hint, compact = cat_meta
        lines = [f"--- {header} ---"]
        if compact:
            _labels = []
            for item in items:
                if isinstance(item, dict):
                    label = item["label"]
                    if item.get("annotation"):
                        label = f"{label} \u2014 {item['annotation']}"
                    if item.get("disabled"):
                        label = _mark_disabled(label)
                    _labels.append(label)
                else:
                    _labels.append(item)
            lines.append("  " + "  |  ".join(_labels))
        elif cat == "info":
            for item in items:
                label = item["label"] if isinstance(item, dict) else item
                lines.append(f"  {label}")
        else:
            for item in items:
                if isinstance(item, dict):
                    label = item["label"]
                    ann = f" \u2014 {item['annotation']}" if item.get("annotation") else ""
                    idx = item.get("display_index")
                    if item.get("disabled"):
                        lines.append(f"  -: {_mark_disabled(label)}{ann}")
                    elif idx is None:
                        lines.append(f"  -: {label}{ann}")  # disabled/unclickable
                    else:
                        lines.append(f"  {idx}: {label}{ann}")
                else:
                    lines.append(f"  {num}: {item}")
                    num += 1
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else ""


def _format_categorized_buttons(buttons: list[dict]) -> str:
    """Categorize and format buttons in one step (convenience wrapper)."""
    return format_categorized_buttons(categorize_buttons(buttons))


def pending_numbered_choice_count(pending: dict | None) -> int:
    """Number of choices that consumed an agent-visible number.

    The single source of truth for the choice/button display boundary,
    used by the renderer (button offset) and the act resolver. Naming-
    agnostic across pending shapes so a caller can't slip disabled/caption
    rows into the count:
      * structured (post _build_pending): ``index`` (None = unnumbered),
        ``disabled``, ``caption``.
      * raw bridge / synthesized pending: ``is_disabled`` / ``is_caption``;
        plain-string entries are enabled.
    Counts ``choices`` when present, else falls back to ``full_items``.
    """
    pending = pending or {}
    source = pending.get("choices") or pending.get("full_items") or []
    n = 0
    for c in source:
        if _is_suppressed_pending_choice(c):
            continue
        if not isinstance(c, dict):
            n += 1  # plain string choice = enabled
            continue
        if c.get("disabled") or c.get("is_disabled"):
            continue
        if c.get("caption") or c.get("is_caption"):
            continue
        # Structured entries carry an explicit index; None means it
        # didn't consume an agent-visible number. Absent key (raw) is fine.
        if c.get("index", "_absent") is None:
            continue
        if not (c.get("label") or "").strip():
            continue
        n += 1
    return n


def _buttons_not_in_pending_actions(buttons: list, pending: dict) -> list:
    """Drop the buttons the pending block lists as actions (by id, else label)."""
    listed_ids: set[str] = set()
    listed_labels: set[str] = set()
    for action in (pending or {}).get("actions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("id") is not None:
            listed_ids.add(str(action["id"]))
        label = str(action.get("label") or "").strip().lower()
        if label:
            listed_labels.add(label)
    if not listed_ids and not listed_labels:
        return buttons
    kept = []
    for btn in buttons:
        if not isinstance(btn, dict):
            kept.append(btn)
            continue
        bid = btn.get("id")
        if bid is not None and str(bid) in listed_ids:
            continue
        if str(_button_display_label(btn) or "").strip().lower() in listed_labels:
            continue
        kept.append(btn)
    return kept


def format_state_text(
    data: dict,
    verbose: bool = False,
    fmt: str = "text",
    *,
    include_details: bool = True,
    anomalies: str = "errors",
) -> dict:
    """Format structured state data, attaching any anomaly note."""
    return _attach_anomaly_note(
        _format_state_text_core(
            data, verbose, fmt, include_details=include_details),
        data,
        anomalies,
    )


def _format_state_text_core(
    data: dict,
    verbose: bool = False,
    fmt: str = "text",
    *,
    include_details: bool = True,
) -> dict:
    """Format structured state data for agents."""
    if fmt == "json":
        # Return structured data directly.
        out = dict(data)
        effective_status = out.pop("_effective_status", None)
        out.pop("_suppress_brief_stats", None)
        # Public JSON must retain screen context when internal keys are stripped.
        if out.get("_screen_texts"):
            out["screen_text"] = list(out["_screen_texts"])
        if effective_status is not None:
            raw_status = out.get("status")
            if raw_status != effective_status:
                out["_raw_status"] = raw_status
            out["status"] = effective_status
        btns = out.pop("buttons", None)
        if btns:
            out["buttons"] = categorize_buttons(btns)
        return out
    if not verbose:
        # Prefer the game mod's one-liner summary (e.g. "Day 3 | 12h
        # before dusk | Scholar, HP 3/4 | Food: hungry").
        summary = data.get("_stats_summary")
        if summary:
            brief_str = summary
        elif not data.get("_suppress_brief_stats"):
            parts = []
            stats = data.get("stats", {})
            if isinstance(stats, dict):
                for k, v in stats.items():
                    parts.append(f"{k}: {v}")
            inv = data.get("inventory")
            if isinstance(inv, list) and inv:
                parts.append(f"items: {', '.join(str(i) for i in inv[:10])}")
            elif isinstance(inv, dict):
                for k, v in inv.items():
                    if k == "version":
                        continue
                    if v in (None, "", [], {}):
                        continue
                    if k == "current" and isinstance(v, list):
                        parts.append(
                            f"items: {', '.join(str(i) for i in v[:10])}"
                        )
                    else:
                        parts.append(f"{k}: {v}")
            if parts:
                brief_str = " | ".join(parts)
            elif data.get("pending") or any(
                not b.get("disabled") for b in data.get("buttons") or []
            ):
                brief_str = ""
            else:
                brief_str = f"status: {data.get('_effective_status', data.get('status', 'unknown'))}"
        else:
            brief_str = ""
        out: dict[str, Any] = {}
        if brief_str:
            out["brief"] = brief_str
        pending = data.get("pending")
        if pending:
            out["pending"] = format_pending_text(pending)
            if isinstance(data.get("_pending_raw"), dict):
                out["_pending_raw"] = data["_pending_raw"]
            if isinstance(data.get("_actionable_snapshot"), dict):
                out["_actionable_snapshot"] = data["_actionable_snapshot"]
        note = hidden_menu_note(data)
        if note:
            out["overlay_note"] = note
        buttons = data.get("buttons")
        if buttons and not pending:
            enabled = [b for b in buttons if not b.get("disabled")]
            if enabled:
                cats = categorize_buttons_indexed(enabled)
                if cats:
                    out["buttons"] = format_categorized_buttons(
                        cats, data.get("_button_categories"))
            else:
                # Preserve the distinction between a known disabled surface
                # and missing button telemetry. Tests and structured callers
                # use it to retain the blocked status above.
                out["buttons"] = []
        return out

    pending = data.get("pending")
    status = data.get("_effective_status", data.get("status", "unknown"))
    # Only show status when there's no pending choice (redundant with
    # CHOICE REQUIRED header) and no overlay active (redundant with
    # overlay buttons).  Still useful for advancing, game_ended,
    # or input_request.
    out: dict[str, Any] = {}
    has_overlay = data.get("_overlay_active", False)
    if not pending and not has_overlay:
        buttons = data.get("buttons")
        quiet_statuses = {
            "waiting_for_input",
            "running",
            "idle",
            "playing",
            "menu",
            "setup",
            "screen_actions",
            "blocked_on_choice",
            "blocked_on_input",
        }
        if status not in quiet_statuses or not buttons:
            out["status"] = status
    # Overlay screen texts (post-transform context like shop resources).
    screen_texts = data.get("_screen_texts")
    if screen_texts:
        texts = [
            _display_text(text) for text in screen_texts
            if not _is_pending_input_prompt_echo(text, pending)
        ]
        if texts:
            out["text"] = "\n".join(texts)
    note = hidden_menu_note(data)
    if note:
        out["overlay_note"] = note
    if pending:
        out["pending"] = format_pending_text(pending)
        if isinstance(data.get("_pending_raw"), dict):
            out["_pending_raw"] = data["_pending_raw"]
        if isinstance(data.get("_actionable_snapshot"), dict):
            out["_actionable_snapshot"] = data["_actionable_snapshot"]
    buttons = data.get("buttons")
    extra_cats = data.get("_button_categories")
    if buttons and pending:
        # The pending block already renders the screen buttons that ride
        # along with the menu (quick-menu items, promoted CTAs) under their
        # own headers with the choice offset; rendering them again here
        # printed every such button twice ("4: Q.Load" under the choices
        # and again below).
        buttons = _buttons_not_in_pending_actions(buttons, pending)
    if buttons:
        cats = categorize_buttons_indexed(buttons)
        if cats:
            if pending:
                cats = _with_display_indices(cats, extra_cats)
                cats = _apply_display_index_offset(
                    cats,
                    pending_numbered_choice_count(pending),
                )
            out["buttons"] = format_categorized_buttons(cats, extra_cats)
            out["_buttons_categorized"] = cats
    # Stats summary footer (compact one-liner from game mod), then the
    # detail this mode exists for.  brief=False is documented as "full
    # inventory and config", but it used to render the same one-line
    # summary brief mode shows: the detailed stats dict and the inventory
    # entries sat in `data` and reached agents only through format="json",
    # so the flag read as a no-op.  The footer is the right channel — it
    # is the state block's own trailer, and render_tool_result_text()
    # passes it through while ignoring unknown top-level keys.
    summary = data.get("_stats_summary")
    inv = data.get("inventory")
    inv_label = data.get("_inventory_label", "Inventory")
    inv_names = _inventory_display_names(inv)
    footer_lines = []
    if summary:
        footer_lines.append(f"  {summary}")
    stats = data.get("stats")
    if include_details and isinstance(stats, dict):
        detail = [
            f"{k}: {v}" for k, v in stats.items()
            if not str(k).startswith("_") and v is not None
        ]
        if detail:
            footer_lines.append("  Stats: " + ", ".join(detail))
    config = data.get("config")
    if include_details and isinstance(config, dict):
        # Only the settings that were actually changed.  The bridge sends
        # its whole config map on every /state, so rendering all of it put
        # an identical "auto_advance: True, ..." line under every verbose
        # state for agents that never touch those knobs.  A default value
        # tells the agent nothing it could not assume, so the line earns
        # its place only where it reports a deviation.
        detail = [
            f"{k}: {v}" for k, v in config.items()
            if not str(k).startswith("_") and v is not None
            and _config_value_is_non_default(k, v)
        ]
        if detail:
            footer_lines.append("  Config: " + ", ".join(detail))
    if include_details and inv_names:
        footer_lines.append(f"  {inv_label} ({len(inv_names)}):")
        footer_lines.extend(f"    • {name}" for name in inv_names)
    elif include_details and summary:
        # Keep the old count line for inventory shapes with no names.
        inv_count = inv.get("current") if isinstance(inv, dict) else None
        if isinstance(inv_count, list):
            inv_count = len(inv_count) if inv_count else None
        if inv_count:
            footer_lines.append(f"  {inv_label}: {inv_count} items")
    if footer_lines:
        out["_footer"] = "\n".join(footer_lines)
    return out


# Bridge config defaults (BridgeHandler.__init__ in bridge.py); a value
# equal to its default is not worth a line in the state footer.
_CONFIG_DEFAULTS: dict[str, Any] = {
    "auto_advance": True,
    "auto_advance_delay": 0.3,
    "end_on_menu_return": True,
}


def _config_value_is_non_default(key: Any, value: Any) -> bool:
    """True when a bridge config entry deviates from its default.

    Unknown keys always report True: we cannot know their default, and a
    key we do not recognise is more likely deliberate than boilerplate.
    """
    if key not in _CONFIG_DEFAULTS:
        return True
    default = _CONFIG_DEFAULTS[key]
    if isinstance(default, bool):
        return bool(value) is not default
    if isinstance(default, float) and isinstance(value, (int, float)):
        return abs(float(value) - default) > 1e-9
    return value != default


def _inventory_display_names(inv: Any) -> list[str]:
    """Item names from either inventory shape (list, or dict['current'])."""
    items = inv
    if isinstance(inv, dict):
        items = inv.get("current")
    if not isinstance(items, list):
        return []
    names = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("label")
        else:
            name = item
        name = str(name).strip() if name is not None else ""
        if name:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# Event formatting (CLI display)
# ---------------------------------------------------------------------------


def format_event(
    event: dict, colour: bool = True, quiet: bool = False, show_stats: bool = True,
    verbose: bool = True, visible_labels: Optional[Set[str]] = None,
) -> Optional[str]:
    """Convert a bridge event dict into a human-readable line or block."""
    etype = event.get("type", "")

    # In quiet mode, hide certain events (but keep user actions visible)
    if quiet:
        if etype == "request_resolved" and event.get("by") != "user":
            return None
        if etype == "command_result":
            return None

    if etype == "dialogue":
        char = event.get("character", "???")
        text = _display_text(event.get("text", ""))
        user = event.get("user_initiated")
        prefix = _yellow("[user] ") if user and colour else "[user] " if user else ""
        if colour:
            return f"{prefix}{_cyan('[' + char + ']')} {text}"
        return f"{prefix}[{char}] {text}"

    if etype == "narration":
        text = _display_text(event.get("text", ""))
        user = event.get("user_initiated")
        prefix = _yellow("[user] ") if user and colour else "[user] " if user else ""
        if colour:
            return f"{prefix}  {text}"
        return f"{prefix}  {text}"

    if etype == "screen_text":
        # Overlay / modal panel text (Roadwarden's journal, Echoes' KIT-LOG-MAP
        # panels). This is real story content. Most screen_content remains
        # stateful (passive overlay snapshots are the narrow exception), but
        # this explicit event had no branch here and fell through to the unknown-type
        # tail and was dropped whenever verbose was off.  handle_transcript()
        # is the only caller that passes verbose=False, which is exactly how a
        # transcript full of panel text reported "(empty transcript)".
        texts = [
            _display_text(str(t)).strip()
            for t in (event.get("texts") or [])
        ]
        texts = [t for t in texts if t]
        if not texts:
            return None
        return "\n".join("  " + t for t in texts)

    if etype == "choice_request":
        choices = event.get("choices", [])
        full_items = event.get("full_items", [])
        visible_choices = [
            c for c in choices
            if not _is_suppressed_pending_choice(c)
        ]
        visible_full_items = [
            item for item in full_items
            if not _is_suppressed_pending_choice(item)
        ]
        has_enabled_choices = bool(visible_choices)
        if not has_enabled_choices and visible_full_items:
            has_enabled_choices = any(
                item
                and not item.get("is_caption")
                and not item.get("is_disabled")
                for item in visible_full_items
            )

        if quiet:
            # Quiet mode: just show the choices without the header
            lines = []
        else:
            header = (
                "--- CHOICE REQUIRED ---"
                if has_enabled_choices
                else "--- NO AVAILABLE CHOICES ---"
            )
            lines = [_bold(header) if colour else header]

        # Ren'Py menu captions give context to the choices.
        captions = [i["label"] for i in visible_full_items if i and i.get("is_caption")]
        if captions:
            for cap in captions:
                rendered_caption = f"| {cap}"
                lines.append(
                    f"  {_dim(rendered_caption) if colour else rendered_caption}"
                )

        # Build annotation map: choice label -> annotation from full_items.
        _ann_map: dict[str, str] = {}
        for item in visible_full_items:
            if item and item.get("annotation") and not item.get("is_caption") and not item.get("is_disabled"):
                _ann_map[item["label"]] = item["annotation"]

        # Display choices with IDs (string or integer) for identification.
        # When enriched dicts are present, use the id field.
        # Otherwise fall back to 1-based integer indexing.
        if visible_choices:
            for i, c in enumerate(visible_choices):
                if isinstance(c, dict):
                    display_id = c.get("id", i + 1)
                    c_label = c.get("label", "")
                else:
                    display_id = i + 1
                    c_label = c
                ann = _ann_map.get(c_label, "")
                suffix = f"  ({ann})" if ann else ""
                if colour:
                    lines.append(f"  {_yellow(str(display_id))}: {c_label}{_dim(suffix) if suffix else ''}")
                else:
                    lines.append(f"  {display_id}: {c_label}{suffix}")
        elif visible_full_items:
            idx = 1
            for item in visible_full_items:
                if not item or item.get("is_caption") or item.get("is_disabled"):
                    continue
                label = item["label"]
                ann = item.get("annotation", "")
                suffix = f"  ({ann})" if ann else ""
                if colour:
                    lines.append(f"  {_yellow(str(idx))}: {label}{_dim(suffix) if suffix else ''}")
                else:
                    lines.append(f"  {idx}: {label}{suffix}")
                idx += 1

        # Disabled choices: show as hints for options that may become available.
        # When visible_labels is provided, only show disabled choices whose
        # label is actually rendered on screen (some games hide insensitive
        # items entirely rather than dimming them).
        disabled_items = []
        for i in visible_full_items:
            if not i or not i.get("is_disabled") or i.get("is_caption"):
                continue
            lbl = i["label"]
            if lbl.endswith(" (disabled)"):
                lbl = lbl[:-len(" (disabled)")]
            if visible_labels is not None and lbl not in visible_labels:
                continue
            disabled_items.append(i)
        if disabled_items and not quiet:
            # Filter out empty/whitespace-only disabled labels.
            _vis_disabled = []
            for item in disabled_items:
                label = item["label"]
                if label.endswith(" (disabled)"):
                    label = label[:-len(" (disabled)")]
                if label.strip():
                    _vis_disabled.append(label)
            if _vis_disabled:
                lines.append("")
                dis_hdr = "Unavailable:"
                lines.append(f"  {_dim(dis_hdr) if colour else dis_hdr}")
                for label in _vis_disabled:
                    lines.append(f"  {_dim('- ' + label) if colour else '- ' + label}")

        # Non-choice interactions (topics, nav, promoted, etc.).
        # Use canonical interactions when available, fall back to legacy.
        _cr_interactions = event.get("interactions")
        if isinstance(_cr_interactions, list):
            if not quiet:
                # Promoted buttons from interactions.
                _promoted = [
                    i for i in _cr_interactions
                    if i.get("promoted") and not _is_suppressed_pending_action(i)
                ]
                if _promoted:
                    lines.append("")
                    for pi in _promoted:
                        pi_label = pi.get("display_label", "?")
                        pi_ann = pi.get("annotation", "")
                        if pi_ann:
                            entry = f'  act "{pi_label}" \u2014 {pi_ann}'
                        else:
                            entry = f'  act "{pi_label}"'
                        lines.append(_dim(entry) if colour else entry)

                # Non-choice interactions (topics, nav, shop, etc.).
                _non_choice = [
                    i for i in _cr_interactions
                    if i.get("type") != "choice" and not i.get("promoted")
                ]
                if _non_choice:
                    lines.append("")
                    _choice_index_offset = len(visible_choices) if visible_choices else sum(
                        1
                        for item in visible_full_items
                        if item
                        and not item.get("is_caption")
                        and not item.get("is_disabled")
                    )
                    itr_text = format_interactions(
                        _non_choice, colour=colour, quiet=False,
                        verbose=verbose, show_choices=False,
                        display_index_offset=_choice_index_offset,
                        extra_categories=event.get("button_categories"),
                    )
                    if itr_text:
                        lines.append(itr_text)
        else:
            # Legacy path: promoted_buttons + screen_buttons.
            promoted_buttons = event.get("promoted_buttons", [])
            if promoted_buttons and not quiet:
                visible_promoted = [
                    pb for pb in promoted_buttons
                    if not _is_suppressed_pending_action(pb)
                ]
                if visible_promoted:
                    lines.append("")
                for pb in visible_promoted:
                    pb_label = pb.get("label", "?")
                    pb_ann = pb.get("annotation", "")
                    if pb_ann:
                        entry = f'  act "{pb_label}" \u2014 {pb_ann}'
                    else:
                        entry = f'  act "{pb_label}"'
                    if colour:
                        lines.append(_dim(entry))
                    else:
                        lines.append(entry)

            screen_buttons = event.get("screen_buttons", [])
            if screen_buttons and not quiet:
                visible_sb = [
                    sb for sb in screen_buttons
                    if _categorize_button(sb) is not None
                ]
                if visible_sb:
                    lines.append("")
                    btn_text = format_screen_buttons(
                        visible_sb, colour=colour, quiet=False,
                        screen_categories=event.get("button_categories"),
                        verbose=verbose,
                    )
                    if btn_text:
                        lines.append(btn_text)

        if not quiet:
            if not has_enabled_choices:
                lines.append("")
                message = (
                    "No enabled choices are currently available. "
                    "Use a visible screen/navigation action if shown, "
                    "or load/rollback if this is a dead end."
                )
                lines.append(_dim(message) if colour else message)
            lines.append("---")

        # Include inventory/stats context if present (suppressed when
        # show_stats is False to reduce noise in --wait output).
        if show_stats:
            inv = event.get("inventory", [])
            stats = event.get("stats", {})
            inv_label = stats.pop("_inventory_label", "Evidence") if stats else "Evidence"
            # Strip metadata keys before display.
            if stats:
                stats = {k: v for k, v in stats.items() if not k.startswith("_")}
            if inv:
                lines.append("")
                hdr = f"{inv_label}:"
                lines.append(_dim(hdr) if colour else hdr)
                for item in inv:
                    name = item.get("name", str(item))
                    lines.append(f"  \u2022 {name}")
            if stats:
                lines.append("")
                lines.append(_dim("Stats:") if colour else "Stats:")
                for k, v in stats.items():
                    if k.endswith("_desc"):
                        continue
                    desc = stats.get(f"{k}_desc")
                    if desc:
                        lines.append(f"  {k}: {v} ({desc})")
                    else:
                        lines.append(f"  {k}: {v}")
        if quiet:
            # In quiet mode: show choices with numbers but without header/separator
            result_lines = []
            if visible_choices:
                for i, c in enumerate(visible_choices):
                    result_lines.append(f"  {i + 1}: {c}")
            elif visible_full_items:
                idx = 1
                for item in visible_full_items:
                    if not item or item.get("is_caption") or item.get("is_disabled"):
                        continue
                    label = item["label"]
                    result_lines.append(f"  {idx}: {label}")
                    idx += 1
            return "\n".join(result_lines) if result_lines else None
        return "\n".join(lines)

    if etype == "input_request":
        prompt = _normalize_input_prompt(event.get("prompt", "Enter text:"))
        if quiet:
            # Quiet mode: just show the prompt without the header
            return prompt
        if colour:
            return _bold(f"--- INPUT REQUIRED: {prompt} ---")
        return f"--- INPUT REQUIRED: {prompt} ---"

    if etype == "scene":
        if not verbose:
            return None
        name = event.get("name", "")
        if colour:
            return _dim(f"(scene: {name})")
        return f"(scene: {name})"

    if etype == "show":
        if not verbose:
            return None
        name = event.get("name", "")
        if colour:
            return _dim(f"(show: {name})")
        return f"(show: {name})"

    if etype == "hide":
        if not verbose:
            return None
        name = event.get("name", "")
        if colour:
            return _dim(f"(hide: {name})")
        return f"(hide: {name})"

    if etype == "text_overlay":
        text = event.get("text", "")
        if colour:
            return f"  {_bold(text)}"
        return f"  {text}"

    if etype == "pause":
        if quiet or not verbose:
            return None
        delay = event.get("delay")
        msg = f"(pause: {delay}s)" if delay is not None else "(pause)"
        return _dim(msg) if colour else msg

    if etype in ("nvl_show", "nvl_hide", "mod_loaded", "context"):
        # Hide these in quiet mode or non-verbose mode
        if quiet or not verbose:
            return None
        if colour:
            return _dim(f"[{etype}]")
        return f"[{etype}]"

    if etype == "nvl_clear":
        if not verbose:
            return None
        return _dim("---") if colour else "---"

    if etype == "context":
        if quiet or not verbose:
            return None
        ctx = event.get("context", "?")
        if colour:
            return _dim(f"[context \u2192 {ctx}]")
        return f"[context \u2192 {ctx}]"

    if etype == "game_started":
        # Hide in quiet mode or non-verbose
        if quiet or not verbose:
            return None
        name = event.get("name", "")
        if colour:
            return _green(f"\u25b6 Game started: {name}")
        return f"> Game started: {name}"

    if etype == "game_resumed":
        if quiet or not verbose:
            return None
        reason = event.get("reason", "load or rollback")
        line = f"Game resumed ({reason})"
        return _dim(line) if colour else line

    if etype == "game_ended":
        reason = event.get("reason", "")
        if event.get("terminal") is False:
            # A menu return the bridge ruled non-terminal for this game
            # (end_on_menu_return opt-out \u2014 e.g. Slay the Princess, whose
            # live gameplay screens are classified as main_menu).  Do NOT
            # render "Game ended": that phrase trips downstream terminal
            # phrase-heuristics into a false auto-end.  Tell the agent what
            # actually happened so it can keep playing.
            if event.get("suppressed") == "no_gameplay_seen":
                # This is the ordinary splash/title transition at launch, not
                # a return from a playthrough. Full transcripts can include it
                # beside the real ending much later, so name the chronology.
                line = "Reached the main menu before gameplay began."
            else:
                line = ("Returned to the main menu (not treated as an ending "
                        "for this game \u2014 continue or load to keep playing).")
            return _dim(line) if colour else line
        if colour:
            return _red(f"\u25a0 Game ended ({reason})")
        return f"X Game ended ({reason})"

    if etype == "request_resolved":
        by = event.get("by", "")
        if by == "user":
            value = event.get("value", "?")
            line = f"[user chose] {value}"
            return _yellow(line) if colour else line
        return None  # other resolutions are internal

    if etype in (
        "command_result", "observation_started", "observation_progress",
    ):
        return None  # internal, not shown to the player

    if etype == "user_choice":
        label = event.get("label", "?")
        line = f"[user chose] {label}"
        return _yellow(line) if colour else line

    if etype == "auto_skipped":
        label = event.get("label", "?")
        text = event.get("text")
        line = f"[auto-advance, no input needed] {label}"
        if text:
            line += f"\n  {text}"
        return _dim(line) if colour else line

    if etype == "inventory_update":
        inv = event.get("inventory", [])
        if not inv:
            return None
        # Non-verbose: only show if there's a "changed" field (incremental).
        changed = event.get("changed")
        if not verbose and changed is None:
            return None
        items = ", ".join(i.get("name", "?") for i in inv)
        line = f"[inventory] {items}"
        return _dim(line) if colour else line

    if etype == "stats_update":
        if not verbose:
            return None
        # Show only changed keys when available.
        changed = event.get("changed")
        stats = event.get("stats", {})
        if changed is not None and len(changed) < len(stats):
            # Incremental update -- show only what changed.
            # In non-verbose mode, skip metadata keys (prefixed with _).
            if verbose:
                parts = [f"{k}: {v}" for k, v in changed.items()]
            else:
                parts = [f"{k}: {v}" for k, v in changed.items()
                         if not k.startswith("_")]
            if not parts:
                return None
            line = "[stats] " + ", ".join(parts)
        elif stats:
            if not verbose:
                return None  # suppress full stats dump in non-verbose
            # Initial push or full reset -- just note the count.
            line = f"[stats] {len(stats)} stats tracked"
        else:
            return None
        return _dim(line) if colour else line

    if etype == "screen_content":
        texts = [_display_text(t) for t in event.get("texts", [])]
        buttons = event.get("buttons", [])
        screens = event.get("screens", [])
        user = event.get("user_initiated")
        if quiet and not texts and not buttons and not user:
            return None
        lines = []
        if user:
            parts = ["[user screen change]"]
            added = event.get("screens_added")
            removed = event.get("screens_removed")
            if added:
                parts.append("+" + ",".join(added))
            if removed:
                parts.append("-" + ",".join(removed))
            hint = " ".join(parts)
            lines.append(_yellow(hint) if colour else hint)
        screen_label = ", ".join(screens) if screens else "unknown"
        if not quiet and verbose:
            header = f"[screen: {screen_label}]"
            lines.append(_dim(header) if colour else header)
        _modal_set = set(event.get("modal_screens") or [])
        _text_sources = event.get("text_sources", [])
        if _modal_set and _text_sources and len(_text_sources) == len(texts):
            _base = [t for t, s in zip(texts, _text_sources) if s not in _modal_set]
            _modal: Dict[str, List[str]] = {}
            for t, s in zip(texts, _text_sources):
                if s in _modal_set:
                    _modal.setdefault(s, []).append(t)
            _screen_names = event.get("screen_names", {})
            for t in _base:
                lines.append(f"  {t}")
            for _ms, _mt in _modal.items():
                _ms_label = _screen_names.get(_ms, _ms)
                hdr = f"[{_ms_label}]"
                lines.append(_bold(hdr) if colour else hdr)
                for t in _mt:
                    lines.append(f"  {t}")
        else:
            for t in texts:
                lines.append(f"  {t}")
        interactions = event.get("interactions")
        if interactions:
            itr_text = format_interactions(
                interactions, colour=colour, quiet=quiet,
                verbose=verbose,
                extra_categories=event.get("button_categories"),
            )
            if itr_text:
                lines.append(itr_text)
        elif buttons:
            btn_text = format_screen_buttons(
                buttons, screens, colour=colour, quiet=quiet,
                screen_categories=event.get("button_categories"),
                verbose=verbose,
            )
            if btn_text:
                lines.append(btn_text)
        return "\n".join(lines) if lines else None

    if etype == "screenshot":
        return None

    if etype == "mod_loaded":
        if not verbose:
            return None
        config = event.get("config", {})
        if colour:
            return _dim(f"[mod_loaded] {config}")
        return f"[mod_loaded] {config}"

    # Unknown event type -- show raw for debugging in verbose mode only.
    if not verbose:
        return None
    return (
        _dim(f"[{etype}] {json.dumps(event, ensure_ascii=False)[:120]}")
        if colour
        else f"[{etype}] {json.dumps(event, ensure_ascii=False)[:120]}"
    )


def format_events(
    events: List[dict], colour: bool = True, quiet: bool = False,
    show_stats: bool = True, verbose: bool = True,
) -> str:
    """Format a list of events into a multi-line string."""
    lines: list[str] = []
    prev: Optional[str] = None
    for ev in events:
        formatted = format_event(ev, colour=colour, quiet=quiet,
                                 show_stats=show_stats, verbose=verbose)
        if formatted is not None and formatted != prev:
            lines.append(formatted)
            prev = formatted
    return "\n".join(lines)


def format_pending_request(
    pending: dict, colour: bool = True, quiet: bool = False, show_stats: bool = True,
    verbose: bool = True, visible_labels: Optional[Set[str]] = None,
) -> str:
    """Format a pending request for display (choice or input)."""
    return format_event(pending, colour=colour, quiet=quiet, show_stats=show_stats,
                        verbose=verbose, visible_labels=visible_labels) or ""


# ---------------------------------------------------------------------------
# Button classification
# ---------------------------------------------------------------------------

def _is_disabled_button(actions: List[str]) -> bool:
    """Return True if the button's actions indicate it is disabled / no-op."""
    return actions_are_disabled(actions)


def _button_is_disabled(btn: dict) -> bool:
    return item_is_disabled(btn)


# Screen name -> category mapping.  None = hide entirely.
_DEFAULT_SCREEN_CATEGORIES: Dict[str, Optional[str]] = {
    "doubleimage": None,
    "doubleimage2": None,
    "mundanejob": "rest_util",
    "tutorialtooltips": "info",
    "characterstatus": "info",
    "achievements": "info",
    "notifyimage": "info",
}

# Default category metadata: (header, hint, compact).
# compact=True renders labels pipe-separated on one line.
# Game-specific categories are registered via the shim's
# _vnf_register_button_category() and arrive in screen_content
# events as button_categories.  Use get_category_meta() to merge.
_CATEGORY_META: Dict[str, tuple] = {
    "choices":    ("CHOICES",        'act <N> or act "<label>"', False),
    "items":      ("ITEMS",          'act <N> or act "<label>"', False),
    "raw_item_actions": ("UNLABELED ITEM ACTIONS", 'act <N> or act "<raw id>"', False),
    "rest_util":  ("OPTIONS",        'act "<label>"',            False),
    "navigation": ("NAVIGATION",     'act "<label>"',            True),
    "info":       ("INFO",           None,                          False),
    "other":      ("OTHER BUTTONS",  'act "<label>"',            False),
}

# Default display order.  Game-specific categories are inserted
# before "navigation" when discovered.
_CATEGORY_ORDER = [
    "choices", "items", "raw_item_actions",
    "rest_util", "navigation", "other", "info",
]


def get_category_meta(
    extra: dict[str, dict] | None = None,
) -> tuple[dict[str, tuple], list[str]]:
    """Merge default and game-registered category metadata.

    Args:
        extra: button_categories dict from screen_content event.
               Each value: {"header": str, "compact": bool}.

    Returns:
        (meta_dict, order_list) — merged metadata and display order.
    """
    if not extra:
        return _CATEGORY_META, _CATEGORY_ORDER

    meta = dict(_CATEGORY_META)
    order = list(_CATEGORY_ORDER)
    # Insert game categories before "navigation".
    insert_idx = order.index("navigation") if "navigation" in order else len(order)
    for name, info in extra.items():
        if name not in meta:
            header = info.get("header", name.upper())
            compact = info.get("compact", False)
            meta[name] = (header, None, compact)
            order.insert(insert_idx, name)
            insert_idx += 1
    return meta, order

# Map interaction types (from shim) to display categories.
_INTERACTION_TYPE_TO_CATEGORY: Dict[str, str] = {
    "choice": "choices",
    "items":  "items",
    "topic":  "topics",
    "nav":    "navigation",
    "shop":   "shop",
    "info":   "info",
    "other":  "other",
}


def _categorize_button(btn: dict, screen_categories: Optional[dict] = None) -> Optional[str]:
    """Assign a display category to a button.

    Returns None to hide the button from display entirely.
    """
    if item_is_default_focus_chrome(btn):
        return None
    screen = btn.get("screen", "")
    actions = btn.get("actions", [])

    # Game-specific overrides first.
    cats = screen_categories or {}
    if screen in cats:
        return cats[screen]

    # Built-in screen mapping.
    if screen in _DEFAULT_SCREEN_CATEGORIES:
        return _DEFAULT_SCREEN_CATEGORIES[screen]

    # Action-based heuristics.
    action_set = set(actions)

    if "ChoiceReturn" in action_set:
        return "choices"
    if "Return" in action_set:
        return "navigation"
    if "shopscreen" in screen:
        return "shop"
    if action_set & {
        "ShowMenu", "QuickSave", "QuickLoad", "Save", "Load",
        "LoadMostRecent", "Quit", "OpenURL",
    }:
        return "navigation"
    # Quick-menu file actions ("Q.Load" = FileLoad) are navigation chrome;
    # the same actions on the save/load screens are the slot buttons.
    if action_set & _QUICK_FILE_ACTIONS and screen not in _FILE_SLOT_SCREENS:
        return "navigation"
    if screen == "menu":
        return "navigation"
    if screen == "quick_menu":
        return "navigation"
    if "Jump" in action_set and screen == "nvl":
        return "topics"

    # NullAction = informational.
    if _is_disabled_button(actions):
        return "info"

    return "other"


# ---------------------------------------------------------------------------
# Button and interaction formatting
# ---------------------------------------------------------------------------

def format_screen_buttons(
    buttons: List[dict],
    screens: Optional[List[str]] = None,
    colour: bool = True,
    quiet: bool = False,
    screen_categories: Optional[dict] = None,
    verbose: bool = True,
) -> str:
    """Format screen buttons grouped by category.

    Button indices match the original flat list so ``act <N>`` resolves
    correctly on the shim side.  In quiet mode only actionable
    categories (choices, topics, shop, other) are shown.
    """
    if not buttons:
        return ""

    # --- Bucket buttons by category, preserving original 1-based index ---
    buckets: Dict[str, List[Tuple[int, dict]]] = {}
    for flat_idx, b in enumerate(buttons, 1):
        cat = _button_category_value(b) or _categorize_button(b, screen_categories)
        if cat is None:
            continue
        buckets.setdefault(cat, []).append((flat_idx, b))

    if not buckets:
        return ""

    lines: list = []

    meta, order = get_category_meta(screen_categories)
    order = list(order)
    for cat in buckets:
        if cat not in order:
            insert_idx = order.index("navigation") if "navigation" in order else len(order)
            order.insert(insert_idx, cat)

    for cat in order:
        cat_entries = buckets.get(cat)
        if not cat_entries:
            continue
        header_text, hint, compact = meta.get(
            cat, (cat.upper(), 'act "<label>"', False))

        if quiet:
            # Quiet mode: only show actionable categories.
            if cat in ("info",):
                continue
            for flat_idx, b in cat_entries:
                label = _button_display_label(b) or "?"
                disabled = _button_is_disabled(b)
                suffix = (
                    ""
                    if not disabled or label.rstrip().endswith("(disabled)")
                    else f"  {_dim('(disabled)') if colour else '(disabled)'}"
                )
                if colour:
                    lines.append(f"  {_yellow(str(flat_idx))}: {label}{suffix}")
                else:
                    lines.append(f"  {flat_idx}: {label}{suffix}")
            continue

        # Verbose mode -- show header and buttons.
        hdr = f"--- {header_text} ---"
        lines.append(_bold(hdr) if colour else hdr)

        if compact:
            # Render as pipe-separated labels on one line.
            # Append "(disabled)" to grayed-out buttons so agents reading
            # plain text can distinguish clickable from not.  Colour
            # mode also dims the label for extra contrast.
            parts = []
            for _, b in cat_entries:
                lbl = _button_display_label(b) or "?"
                disabled = _button_is_disabled(b)
                if disabled:
                    marked = _mark_disabled(lbl)
                    parts.append(_dim(marked) if colour else marked)
                else:
                    parts.append(lbl)
            line = "  " + "  |  ".join(parts)
            lines.append(line)
        else:
            for flat_idx, b in cat_entries:
                label = _button_display_label(b) or "?"
                actions = b.get("actions", [])
                screen = b.get("screen", "")

                if cat == "info":
                    # Info items: just show text, no numbering.
                    lines.append(f"  {_dim(label) if colour else label}")
                elif verbose:
                    action_str = ", ".join(actions)
                    screen_hint = f"  [{screen}]" if screen else ""
                    if colour:
                        lines.append(
                            f"  {_yellow(str(flat_idx))}: {label}  "
                            f"{_dim('(' + action_str + ')' + screen_hint)}"
                        )
                    else:
                        lines.append(
                            f"  {flat_idx}: {label}  ({action_str}){screen_hint}"
                        )
                else:
                    if colour:
                        lines.append(f"  {_yellow(str(flat_idx))}: {label}")
                    else:
                        lines.append(f"  {flat_idx}: {label}")

        if hint:
            lines.append(_dim(f"Use: {hint}") if colour else f"Use: {hint}")

    return "\n".join(lines)


def _interaction_category(itr: dict) -> str:
    itype = itr.get("type", "other")
    cat = itr.get("category")
    if not cat:
        action_cat = _categorize_button({
            "screen": itr.get("screen", ""),
            "actions": itr.get("action_names", []),
        })
        if itr.get("source") == "button" and action_cat:
            cat = action_cat
    if not cat and itype != "other":
        cat = _INTERACTION_TYPE_TO_CATEGORY.get(itype)
    if not cat:
        cat = _categorize_button({
            "screen": itr.get("screen", ""),
            "actions": itr.get("action_names", []),
        }) or _INTERACTION_TYPE_TO_CATEGORY.get(itype, "other")
    return cat


def _categorize_interactions(interactions: list[dict]) -> dict[str, list[dict]]:
    buckets: Dict[str, List[dict]] = {}
    for itr in interactions:
        if item_is_default_focus_chrome(itr):
            continue
        buckets.setdefault(_interaction_category(itr), []).append(itr)
    return buckets


def _with_interaction_display_indices(
    interactions: list[dict],
    extra_categories: dict[str, dict] | None = None,
) -> dict[str, list[dict]]:
    meta, _base_order = get_category_meta(extra_categories)
    buckets = _categorize_interactions(interactions)
    order = _ordered_categories(buckets.keys(), extra_categories)
    out: dict[str, list[dict]] = {}
    display_idx = 1
    for cat in order:
        entries = buckets.get(cat)
        if not entries:
            continue
        numbered = _numbered_category(cat, meta)
        cloned = []
        for itr in entries:
            entry = dict(itr)
            if numbered and not item_is_disabled(entry):
                entry["display_index"] = display_idx
                display_idx += 1
            cloned.append(entry)
        out[cat] = cloned
    return out


def _apply_display_index_offset(
    buckets: dict[str, list[dict]],
    offset: int,
) -> dict[str, list[dict]]:
    if offset <= 0:
        return buckets
    adjusted: dict[str, list[dict]] = {}
    for cat, entries in buckets.items():
        adjusted_entries = []
        for itr in entries:
            entry = dict(itr)
            if entry.get("display_index") is not None:
                entry["display_index"] = entry["display_index"] + offset
            adjusted_entries.append(entry)
        adjusted[cat] = adjusted_entries
    return adjusted


def format_interactions(
    interactions: List[dict],
    colour: bool = True,
    quiet: bool = False,
    verbose: bool = False,
    show_choices: bool = True,
    display_index_offset: int = 0,
    extra_categories: dict[str, dict] | None = None,
) -> str:
    """Format a canonical interaction list grouped by type.

    When show_choices is False, choice-type interactions are skipped
    (they're rendered separately by format_event for choice_request).
    """
    if not interactions:
        return ""

    # Bucket by display category and assign contiguous visible indices.
    buckets = _with_interaction_display_indices(
        interactions, extra_categories=extra_categories)
    if not show_choices:
        buckets.pop("choices", None)
        buckets = _apply_display_index_offset(buckets, display_index_offset)

    if not buckets:
        return ""

    lines: list = []

    order = _ordered_categories(buckets.keys(), extra_categories)

    for cat in order:
        cat_entries = buckets.get(cat)
        if not cat_entries:
            continue
        meta, _base_order = get_category_meta(extra_categories)
        header_text, hint, compact = meta.get(
            cat, (cat.upper(), 'act "<label>"', False))

        if quiet:
            if cat in ("info",):
                continue
            for itr in cat_entries:
                label = itr.get("display_label", "?")
                disabled = item_is_disabled(itr)
                suffix = (
                    ""
                    if not disabled or label.rstrip().endswith("(disabled)")
                    else f"  {_dim('(disabled)') if colour else '(disabled)'}"
                )
                idx = itr.get("display_index", "?")
                if colour:
                    lines.append(f"  {_yellow(str(idx))}: {label}{suffix}")
                else:
                    lines.append(f"  {idx}: {label}{suffix}")
            continue

        hdr = f"--- {header_text} ---"
        lines.append(_bold(hdr) if colour else hdr)

        if compact:
            parts = []
            for itr in cat_entries:
                lbl = itr.get("display_label", "?")
                disabled = item_is_disabled(itr)
                if disabled:
                    marked = _mark_disabled(lbl)
                    parts.append(_dim(marked) if colour else marked)
                else:
                    parts.append(lbl)
            line = "  " + "  |  ".join(parts)
            lines.append(_dim(line) if colour else line)
        else:
            # Group items by subcategory when available.
            _has_subcats = (cat == "items"
                           and any(e.get("category") for e in cat_entries))
            if _has_subcats:
                # Bucket by subcategory, preserving order of first appearance.
                _sub_order: list = []
                _sub_buckets: Dict[str, list] = {}
                _no_cat: list = []
                for itr in cat_entries:
                    sc = itr.get("category", "")
                    if sc:
                        if sc not in _sub_buckets:
                            _sub_order.append(sc)
                            _sub_buckets[sc] = []
                        _sub_buckets[sc].append(itr)
                    else:
                        _no_cat.append(itr)
                _all_groups = [(sc, _sub_buckets[sc]) for sc in _sub_order]
                if _no_cat:
                    _all_groups.append(("Other", _no_cat))
                for _sc_name, _sc_items in _all_groups:
                    sc_hdr = f"  {_sc_name}:"
                    lines.append(_bold(sc_hdr) if colour else sc_hdr)
                    for itr in _sc_items:
                        label = itr.get("display_label", "?")
                        idx = itr.get("display_index", "?")
                        ann = itr.get("annotation", "")
                        ann_suffix = f" \u2014 {ann}" if ann else ""
                        if colour:
                            lines.append(f"    {_yellow(str(idx))}: {label}{_dim(ann_suffix) if ann_suffix else ''}")
                        else:
                            lines.append(f"    {idx}: {label}{ann_suffix}")
            else:
                for itr in cat_entries:
                    label = itr.get("display_label", "?")
                    disabled = item_is_disabled(itr)
                    idx = itr.get("display_index", "?")
                    ann = itr.get("annotation", "")

                    if cat == "info":
                        lines.append(f"  {_dim(label) if colour else label}")
                    elif verbose:
                        action_strs = itr.get("action_strs", [])
                        action_str = ", ".join(action_strs)
                        screen = itr.get("screen", "")
                        screen_hint = f"  [{screen}]" if screen else ""
                        if colour:
                            lines.append(
                                f"  {_yellow(str(idx))}: {label}  "
                                f"{_dim('(' + action_str + ')' + screen_hint)}"
                            )
                        else:
                            lines.append(
                                f"  {idx}: {label}  ({action_str}){screen_hint}"
                            )
                    else:
                        ann_suffix = f" \u2014 {ann}" if ann else ""
                        if disabled:
                            entry = f"  {_dim('- ' + label + ann_suffix) if colour else '- ' + label + ann_suffix}"
                            lines.append(entry)
                        elif colour:
                            lines.append(f"  {_yellow(str(idx))}: {label}{_dim(ann_suffix) if ann_suffix else ''}")
                        else:
                            lines.append(f"  {idx}: {label}{ann_suffix}")

        if hint:
            lines.append(_dim(f"Use: {hint}") if colour else f"Use: {hint}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session-dependent screen/interaction helpers
# ---------------------------------------------------------------------------

def _get_live_screen(session: Any) -> Optional[dict]:
    """Return the bridge's current /screen snapshot when available.

    Transcript screen_content can lag behind modal/screen transforms. The
    state command already prefers /screen; action rendering needs the same source
    when resolving overlay buttons.
    """
    getter = getattr(session, "_get", None)
    if not callable(getter):
        return None
    try:
        code, data = getter("/screen", timeout=2.0)
    except Exception:
        return None
    if code == 200 and data:
        screen = data.get("screen")
        if screen and not screen.get("_lightweight") and (
            "buttons" in screen
            or "interactions" in screen
            or isinstance(screen.get("screens"), list)
        ):
            return screen
    return None


def _get_latest_screen_buttons(session: Any) -> Optional[dict]:
    """Return the latest full screen snapshot, including a buttonless screen.

    A closed modal must not reappear just because it was the last screen with
    buttons. Lightweight say-progress events do not replace a full snapshot.
    ``session`` must support ``get_transcript(last_n=N)`` (duck-typed).
    """
    live_screen = _get_live_screen(session)
    if live_screen:
        return live_screen

    transcript = session.get_transcript(last_n=30)
    for ev in reversed(transcript):
        if (ev.get("type") == "screen_content" and not ev.get("_lightweight")
                and ("buttons" in ev or "interactions" in ev
                     or isinstance(ev.get("screens"), list))):
            return ev
    return None


def _has_choice_overlay(sc_ev: Optional[dict], pending: Optional[dict]) -> bool:
    """Return True when a pending choice exists but the choice buttons
    aren't currently visible on screen (an overlay covers them)."""
    if not pending or pending.get("type") != "choice_request":
        return False
    if not sc_ev:
        return False
    if not sc_ev.get("overlay_active") and not sc_ev.get("modal_screens"):
        sc_screens = set(sc_ev.get("screens") or [])
        sc_screens.discard("vnf_command_poller")
        sc_screens.discard("vnf_player_debug")
        sc_screens.discard("llm_command_poller")
        sc_screens.discard("llm_player_debug")
        if not (sc_screens and sc_screens <= {"menu"}):
            return False
    sc_buttons = sc_ev.get("buttons", [])
    # Choice buttons have a ChoiceReturn action.
    if any("ChoiceReturn" in str(b.get("action_strs", ""))
           for b in sc_buttons):
        return False
    return True


def _get_latest_interactions(
    session: Any,
    pending: Optional[dict] = None,
    overlay_active: bool = False,
    ignore_screen: bool = False,
) -> list:
    """Get the current canonical interaction list.

    When overlay_active is True and pending has choices, the choices are
    marked disabled and the current screen_content buttons are used
    instead of the stale button set from menu creation time.

    ``session`` must support ``get_transcript(last_n=N)`` (duck-typed).
    """
    # Find the most recent screen_content interactions.
    live_screen = None
    sc_interactions = None
    if not ignore_screen:
        live_screen = _get_latest_screen_buttons(session)
        if live_screen and "interactions" in live_screen:
            sc_interactions = _normalize_interaction_disabled(
                live_screen.get("interactions") or []
            )

    pending_interactions = None
    if pending and "interactions" in pending:
        pending_interactions = _normalize_interaction_disabled(
            pending.get("interactions") or [])

    if pending_interactions is None:
        return sc_interactions if sc_interactions is not None else []

    if sc_interactions == []:
        return []

    if sc_interactions is not None and live_screen and live_screen.get("overlay_active"):
        return sc_interactions

    if sc_interactions is not None and _live_choices_differ(
        sc_interactions,
        pending_interactions,
    ):
        return sc_interactions

    if not overlay_active or sc_interactions is None:
        if sc_interactions is not None:
            return _merge_pending_choices_with_live_buttons(
                pending_interactions,
                sc_interactions,
            )
        return pending_interactions

    # Overlay covers the choices -- drop them entirely and use the
    # overlay's buttons.
    merged = _reclassify_nav_items(sc_interactions, skip_choices=True)
    return merged


def _merge_pending_choices_with_live_buttons(
    pending_interactions: list,
    live_interactions: list,
) -> list:
    """Keep pending choices, but refresh non-choice buttons from /screen.

    Choice requests can outlive quick-menu/nav updates in Roadwarden. The
    pending request is still the source of truth for hidden or subset choices,
    but live /screen interactions are more reliable for buttons such as
    Sleep/Travel.
    """
    live_non_choices = [
        i for i in live_interactions
        if i.get("type") != "choice"
    ]
    if not live_non_choices:
        return pending_interactions

    pending_choices = [
        i for i in pending_interactions
        if i.get("type") == "choice"
    ]
    if not pending_choices:
        return live_interactions

    return pending_choices + live_non_choices


def _choice_source_items(raw: dict) -> list:
    """Return the selectable source rows used to render a choice request."""
    full_items = raw.get("full_items")
    if isinstance(full_items, list) and full_items:
        return full_items
    choices = raw.get("choices")
    if isinstance(choices, list):
        return choices
    return []


def _choice_item_label(item: Any) -> str:
    if isinstance(item, dict):
        return _display_text(item.get("label", ""))
    return _display_text(item)


def _choice_equivalence_key(item: Any) -> tuple[str, bool, bool]:
    """Return a display-equivalence key for comparing choice rows.

    The bridge pending request is the source consumed by wait().  State may
    also include live/screen-derived choices, and those can echo the same rows
    with lightweight list-marker chrome stripped.  Compare on the stable user
    text while keeping disabled/caption rows distinct.
    """
    if isinstance(item, dict):
        disabled = item_is_disabled(item)
        caption = bool(item.get("is_caption"))
    else:
        disabled = False
        caption = False
    label = _choice_item_label(item).strip()
    label = label.lstrip("\u2022\u2023\u25e6\u2043").strip()
    return (label, disabled, caption)


def _live_choices_duplicate_pending(
    pending: dict,
    game_state: dict,
) -> bool:
    """Return True when live choices are only a duplicate-expanded pending.

    In that case state should format the same pending request as wait(), not
    renumber the duplicate live rows as additional choices.
    """
    pending_items = _choice_source_items(pending)
    live_items = _choice_source_items(game_state)
    if not pending_items or len(live_items) <= len(pending_items):
        return False

    pending_keys = [_choice_equivalence_key(item) for item in pending_items]
    live_keys = [_choice_equivalence_key(item) for item in live_items]
    if live_keys[:len(pending_keys)] != pending_keys:
        return False
    pending_key_set = set(pending_keys)
    return all(key in pending_key_set for key in live_keys[len(pending_keys):])


def _normalize_interaction_disabled(interactions: list) -> list:
    normalized = []
    for itr in interactions:
        if not isinstance(itr, dict):
            normalized.append(itr)
            continue
        updated = dict(itr)
        if _button_is_disabled(updated):
            updated["disabled"] = True
        normalized.append(updated)
    return normalized


def _interaction_lookup(interactions: list) -> dict[tuple[str, str], dict]:
    lookup = {}
    for itr in _normalize_interaction_disabled(interactions):
        if not isinstance(itr, dict):
            continue
        label = _button_display_label(itr)
        if not label:
            continue
        lookup[(itr.get("screen", ""), label)] = itr
    return lookup


def _live_choices_differ(
    live_interactions: list,
    pending_interactions: list,
) -> bool:
    """Return True when the current screen has a newer choice set.

    Ren'Py screens can recompute sensitivity/labels after a modal action
    without issuing a fresh choice_request. In that case the pending request is
    stale, but /screen already contains the actual actionable choices.
    """
    live_choices = [
        i for i in live_interactions
        if i.get("type") == "choice"
    ]
    pending_choices = [
        i for i in pending_interactions
        if i.get("type") == "choice"
    ]
    if not live_choices or not pending_choices:
        return False
    if len(live_choices) < len(pending_choices):
        return False

    def _signature(items: list) -> list[tuple[str, bool]]:
        return [
            (
                str(i.get("display_label") or "").strip(),
                bool(i.get("disabled")),
            )
            for i in items
        ]

    return _signature(live_choices) != _signature(pending_choices)


_QUICK_FILE_ACTIONS = {"FileLoad", "FileSave", "FileTakeScreenshot"}
_FILE_SLOT_SCREENS = {"save", "load", "file_slots"}

# System nav actions that should stay as "nav" (compact display).
_SYS_NAV_ACTIONS = {
    "ShowMenu", "QuickSave", "QuickLoad", "Save", "Load",
    "LoadMostRecent", "Quit", "MainMenu", "OpenURL",
}


def _reclassify_nav_items(
    interactions: list,
    skip_choices: bool = False,
) -> list:
    """Reclassify non-system nav buttons as 'items' and re-index.

    Content buttons (inventory items, shop items, etc.) are categorized
    as 'nav' by the shim because they're on menu/quick_menu screens.
    This function splits them into 'items' type so they render as
    indexed entries instead of compact pipe-separated navigation.
    """
    result = []
    idx = 1
    for itr in interactions:
        if skip_choices and itr.get("source") == "choice":
            continue
        itr_copy = dict(itr)
        if itr_copy.get("type") == "nav":
            action_set = set(itr_copy.get("action_names", []))
            if not (action_set & _SYS_NAV_ACTIONS):
                itr_copy["type"] = "items"
        if not itr_copy.get("disabled"):
            itr_copy["index"] = idx
            idx += 1
        result.append(itr_copy)
    return result


# ---------------------------------------------------------------------------
# Quote normalization and interaction matching
# ---------------------------------------------------------------------------

def _normalize_quotes_client(s: str) -> str:
    """Fold Unicode quotes to ASCII for matching."""
    return (s
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'"))


def _interaction_aliases(itr: dict) -> list[str]:
    label = str(itr.get("display_label", "") or "")
    aliases = [label]
    iid = itr.get("id")
    if iid is not None and iid != "":
        aliases.extend([str(iid), f"{iid}: {label}"])
    ann = itr.get("annotation")
    if ann:
        aliases.extend([str(ann), f"{ann}: {label}"])
    aliases.extend(str(a) for a in itr.get("aliases", []) if a)
    return aliases


def _act_match_category(itr: dict) -> str:
    """'choice' for a story choice, 'control' for everything else.

    'control' covers screen buttons, nav rows, topics, items — anything a
    mod or the base game presents as a clickable UI control rather than a
    menu-arm decision.  The category is the unit precedence and ambiguity
    are judged against below.  Distinct from ``_interaction_category``
    (display-header categorization, e.g. "topics"/"items"/"other") — this
    one only ever distinguishes choice from everything else.
    """
    return "choice" if itr.get("source") == "choice" else "control"


def _exact_match_key(s: str) -> str:
    """Normalize a label for the EXACT-match tier only.

    Case, whitespace, and TRAILING punctuation are folded away so a target
    typed without the game's own trailing period/ellipsis ("Grab the
    toolkit" vs. the rendered "Grab the toolkit.") still counts as an
    exact hit rather than falling through to the stricter fuzzy tier.
    Containment (prefix/substring) matching is unaffected — it already
    tolerates a missing period by definition.
    """
    import re
    normalized = _normalize_quotes_client(str(s)).lower().strip()
    return re.sub(r"[\s.,!?:;…]+$", "", normalized)


def _label_exact_hit(target_key: str, itr: dict) -> bool:
    for alias in _interaction_aliases(itr):
        if _exact_match_key(alias) == target_key:
            return True
    return False


def _label_prefix_hit(target_lower: str, itr: dict) -> bool:
    for alias in _interaction_aliases(itr):
        if _normalize_quotes_client(alias).lower().strip().startswith(target_lower):
            return True
    return False


def _label_substring_hit(target_lower: str, itr: dict) -> bool:
    dl = _normalize_quotes_client(itr.get("display_label", "")).lower().strip()
    return bool(target_lower) and target_lower in dl


def _dedup_interactions(items: Iterable[dict]) -> list[dict]:
    seen: set[int] = set()
    out: list[dict] = []
    for itr in items:
        key = id(itr)
        if key in seen:
            continue
        seen.add(key)
        out.append(itr)
    return out


class InteractionMatch:
    """Outcome of resolving a label target against the interaction list.

    ``matched`` names the single control to act on.  ``ambiguous`` lists
    every candidate a tied exact match or a fuzzy hit found when the target
    did not resolve to exactly one — the caller fails closed and shows the
    list rather than guessing.  Neither is set when the target reached
    nothing at all.
    """

    __slots__ = ("matched", "ambiguous")

    def __init__(
        self,
        matched: Optional[dict] = None,
        ambiguous: Optional[list] = None,
    ) -> None:
        self.matched = matched
        self.ambiguous = ambiguous

    def __bool__(self) -> bool:
        return self.matched is not None


def _resolve_label_interaction(
    target: str,
    interactions: list,
    hidden_choice_labels: Optional[Set[str]] = None,
) -> "InteractionMatch":
    """Resolve *target* to exactly one interaction, or flag it ambiguous.

    Precedence: an id match is exact and unambiguous by construction (ids
    are internal identifiers, never player-typed labels, so they cannot
    collide across categories).  Failing that, an exact normalized
    label/alias match wins outright when it names interactions in exactly
    one CATEGORY — a story choice vs. any other screen control (see
    ``_act_match_category``) — even if a short administrative label
    (KIT, MAP, LOG) happens to be a SUBSTRING of an unrelated, much longer
    story choice: substring containment is fuzzy, not exact, so it never
    competes with a real exact hit.  A tie of exact hits ACROSS categories
    refuses instead of guessing which the caller meant.

    Targets of three characters or fewer require an exact hit. Only once
    nothing matches exactly does a longer target's fuzzy pass run (prefix
    first, substring only when no prefix candidate exists at all), and it
    is held to a stricter rule than the exact tier: it must land on
    exactly one interaction, in any category — two fuzzy hits in the same
    category are just as ambiguous as one in each.  Fleet R66:
    ``act(target="KIT")`` fuzzy-matched the unrelated story choice "The
    signal analysis toolkit..." because the old substring tier accepted
    any lone match regardless of what else was on the surface; a KIT
    button on the same surface must now win via the exact tier before
    fuzzy ever runs.

    *hidden_choice_labels* excludes story choices a modal panel currently
    covers from competing at all: the shim's raw interaction list keeps
    them for "hidden menu" bookkeeping even though the player cannot see
    or act on them, so a real, visible panel control sharing that label is
    not a genuine tie.  A target that reaches ONLY a hidden choice still
    resolves to nothing here — the caller's existing hidden-menu refusal
    names the panel instead of this function inventing a new one.
    """
    if not interactions:
        return InteractionMatch()
    hidden = hidden_choice_labels or set()

    def _visible(itr: dict) -> bool:
        if itr.get("source") != "choice":
            return True
        return _normalize_label(str(itr.get("display_label") or "")) not in hidden

    pool = [itr for itr in interactions if _visible(itr)]
    if not pool:
        return InteractionMatch()

    for itr in pool:
        if itr.get("id") == target:
            return InteractionMatch(matched=itr)

    target_lower = _normalize_quotes_client(str(target)).lower().strip()
    if not target_lower:
        return InteractionMatch()

    target_key = _exact_match_key(target)
    exact_by_category: dict[str, list[dict]] = {}
    for itr in pool:
        if _label_exact_hit(target_key, itr):
            exact_by_category.setdefault(_act_match_category(itr), []).append(itr)
    if exact_by_category:
        if len(exact_by_category) == 1:
            return InteractionMatch(matched=next(iter(exact_by_category.values()))[0])
        candidates = [itr for items in exact_by_category.values() for itr in items]
        return InteractionMatch(ambiguous=candidates)

    # Short control names must not become unrelated story fragments when
    # the control is absent (KIT -> toolkit, LOG -> logs).
    if len(target_lower) <= 3:
        return InteractionMatch()

    prefix_candidates = _dedup_interactions(
        itr for itr in pool if _label_prefix_hit(target_lower, itr))
    fuzzy_candidates = prefix_candidates or _dedup_interactions(
        itr for itr in pool if _label_substring_hit(target_lower, itr))
    if not fuzzy_candidates:
        return InteractionMatch()
    if len(fuzzy_candidates) == 1:
        return InteractionMatch(matched=fuzzy_candidates[0])
    return InteractionMatch(ambiguous=fuzzy_candidates)


def _match_interaction(
    target: str,
    interactions: list,
    display_index_offset: int = 0,
) -> Optional[dict]:
    """Match a target string or index against the interaction list.

    Returns the matched interaction dict, or None.  Label resolution
    delegates to ``_resolve_label_interaction`` for the category-aware
    exact/fuzzy precedence; an ambiguous outcome collapses to ``None``
    here, matching this function's legacy no-guess contract for callers
    that do not consume the candidate list.
    """
    if not interactions:
        return None

    # Integer index.
    try:
        idx = int(target)
        buckets = _with_interaction_display_indices(interactions)
        buckets = _apply_display_index_offset(buckets, display_index_offset)
        for entries in buckets.values():
            for itr in entries:
                if itr.get("display_index") == idx:
                    return itr
        # Fallback: accept raw shim indices for callers that already know them.
        for itr in interactions:
            if itr.get("index") == idx:
                return itr
        return None
    except (ValueError, TypeError):
        pass

    return _resolve_label_interaction(target, interactions).matched


# ---------------------------------------------------------------------------
# Main menu formatting
# ---------------------------------------------------------------------------

def format_main_menu(ctx: dict, colour: bool = True, quiet: bool = False) -> str:
    """Format main-menu available commands as a choice-like display."""
    commands = ctx.get("available_commands", [])
    if not commands:
        return ""
    if quiet:
        # Quiet mode: just show commands without headers
        return "\n".join(f"  \u2022 {cmd}" for cmd in commands)
    header = "--- MAIN MENU ---"
    lines = [_bold(header) if colour else header]
    for cmd in commands:
        if colour:
            lines.append(f"  \u2022 {_yellow(cmd)}")
        else:
            lines.append(f"  \u2022 {cmd}")
    lines.append("---")
    lines.append(
        _dim('Use: act "<command>"  (e.g. act "start")')
        if colour
        else 'Use: act "<command>"  (e.g. act "start")'
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Choice matching helpers
# ---------------------------------------------------------------------------

def _choice_submit_key(choice: Any, positional_idx: int) -> Any:
    """Return the best key for submitting a choice: its ``id`` if present,
    its ``index`` field, or the 1-based positional index as fallback."""
    if isinstance(choice, dict):
        cid = choice.get("id")
        if cid is not None and cid != "":
            return cid
        cidx = choice.get("index")
        if cidx is not None:
            return cidx
    return positional_idx


def _choice_label_and_annotation(choice: Any) -> tuple[str, str]:
    """Return the actionable label and optional display annotation."""
    if isinstance(choice, dict):
        label = choice.get("label", choice.get("caption", ""))
        annotation = choice.get("annotation", "")
        return str(label or ""), str(annotation or "")
    return str(choice), ""


def _choice_id(choice: Any) -> str:
    if isinstance(choice, dict):
        cid = choice.get("id")
        if cid is not None and cid != "":
            return str(cid)
    return ""


def _normalise_choice_target(s: str) -> str:
    return _normalize_quotes_client(str(s)).lower().strip()


def _choice_rendered_aliases(label: str, annotation: str) -> list[str]:
    aliases = [label]
    if not annotation:
        return aliases
    aliases.extend([
        f"{label}  ({annotation})",
        f"{label} ({annotation})",
        f"{annotation}: {label}",
    ])
    return aliases


def _choice_candidates_for_target(target: str, choices: list) -> list[tuple[int, Any, str]]:
    target_norm = _normalise_choice_target(target)
    if not target_norm:
        return []
    matches = []
    for i, choice in enumerate(choices or [], 1):
        label, annotation = _choice_label_and_annotation(choice)
        aliases = _choice_rendered_aliases(label, annotation)
        cid = _choice_id(choice)
        if cid:
            aliases.extend([cid, f"{cid}: {label}"])
        if annotation:
            aliases.append(annotation)
        if any(target_norm == _normalise_choice_target(alias) for alias in aliases):
            matches.append((i, choice, label))
    return matches


def _unique_choice_prefix_match(target: str, choices: list) -> Optional[tuple[int, Any, str]]:
    target_norm = _normalise_choice_target(target)
    if len(target_norm) <= 3:
        return None
    matches = []
    for i, choice in enumerate(choices or [], 1):
        label, annotation = _choice_label_and_annotation(choice)
        aliases = _choice_rendered_aliases(label, annotation)
        cid = _choice_id(choice)
        if cid:
            aliases.extend([cid, f"{cid}: {label}"])
        if any(_normalise_choice_target(alias).startswith(target_norm) for alias in aliases):
            matches.append((i, choice, label))
    return matches[0] if len(matches) == 1 else None


def choice_target_to_action_label(target: str, choices: list) -> str:
    """Map a rendered annotated choice label back to its actionable label.

    Text renderers display annotations such as ``(friendly)`` to help agents
    reason about attitude choices, but Ren'Py's interaction label usually
    remains the raw dialogue.  This keeps copy/pasted rendered labels
    round-trippable without adding broad fuzzy matching.
    """
    candidates = _choice_candidates_for_target(target, choices)
    if len(candidates) == 1:
        return candidates[0][2]
    if candidates:
        # A choice can arrive through both pending.full_items and the live
        # interaction list. The latter commonly adds the stable choice ID,
        # making the two dicts unequal even though they describe one visible
        # row. Coalesce only when every match has the same actionable label
        # and the nonempty IDs do not conflict; genuinely duplicated labels
        # with different IDs remain ambiguous.
        labels = {
            _normalise_choice_target(label): label
            for _index, _choice, label in candidates
        }
        choice_ids = {
            _choice_id(choice)
            for _index, choice, _label in candidates
            if _choice_id(choice)
        }
        if len(labels) == 1 and len(choice_ids) <= 1:
            return next(iter(labels.values()))
    prefix = _unique_choice_prefix_match(target, choices)
    if prefix is not None:
        return prefix[2]
    return target


def _match_choice_target(target: str, choices: list) -> Optional[tuple]:
    """Try to match *target* against a list of enriched choices.

    Returns ``(submit_key, label)`` on match, else ``None``.
    *submit_key* is the choice's ``id``, ``index``, or 1-based positional
    index -- whichever the shim will accept.
    Matching order: integer index -> string ID -> exact label -> unique prefix
    -> unique substring. Targets of at most three characters are exact-only.
    """
    if not choices:
        return None

    # 1. Integer index (1-based positional).
    try:
        idx = int(target)
        if 1 <= idx <= len(choices):
            c = choices[idx - 1]
            lbl = c.get("label", c) if isinstance(c, dict) else str(c)
            return _choice_submit_key(c, idx), lbl
        return None
    except (ValueError, TypeError):
        pass

    target_lower = target.lower().strip()

    # 2. String ID (exact, case-insensitive).
    for i, c in enumerate(choices):
        if isinstance(c, dict):
            cid = str(c.get("id", "")).lower()
            if cid and cid == target_lower:
                return _choice_submit_key(c, i + 1), c.get("label", "")

    # 3. Exact label/rendered annotation match.
    exact_matches = _choice_candidates_for_target(target, choices)
    if len(exact_matches) == 1:
        i, c, lbl = exact_matches[0]
        return _choice_submit_key(c, i), lbl

    # 4. Unique prefix match against labels and rendered annotated labels.
    if len(target_lower) <= 3:
        return None
    prefix_match = _unique_choice_prefix_match(target, choices)
    if prefix_match is not None:
        i, c, lbl = prefix_match
        return _choice_submit_key(c, i), lbl

    # 5. Unique substring label match.  Ambiguous fragments should not pick
    # the first visible choice by accident.
    substring_matches = []
    for i, c in enumerate(choices):
        clabel = (c.get("label", "") if isinstance(c, dict) else str(c)).lower().strip()
        if clabel and target_lower in clabel:
            lbl = c.get("label", c) if isinstance(c, dict) else str(c)
            substring_matches.append((i + 1, c, lbl))
    if len(substring_matches) == 1:
        i, c, lbl = substring_matches[0]
        return _choice_submit_key(c, i), lbl

    return None


# ---------------------------------------------------------------------------
# UI Inspector formatting
# ---------------------------------------------------------------------------

def _format_inspect_result(data: dict, colour: bool = True, verify: bool = False,
                           focus_only: bool = False) -> str:
    """Format the inspect command_result into human-readable text."""
    lines: list[str] = []
    header = "=== UI INSPECT ==="
    lines.append(_cyan(header) if colour else header)

    sw = data.get("screen_width", 0)
    sh = data.get("screen_height", 0)
    lines.append(f"  Resolution: {sw}x{sh}")

    # Screens.
    if not focus_only:
        screens = data.get("screens", [])
        lines.append("")
        lines.append(f"SCREENS ({len(screens)} active):")
        if not screens:
            lines.append("  (none)")
        for s in screens:
            tag = s.get("tag", "?")
            zo = s.get("zorder", 0)
            flags = []
            if s.get("modal"):
                flags.append("modal")
            if s.get("transient"):
                flags.append("transient")
            flag_str = "  " + " ".join(flags) if flags else ""
            lines.append(f"  {tag:<24s} zorder={zo}{flag_str}")

    # Focus list (clickable regions).
    focus_list = data.get("focus_list", [])
    lines.append("")
    lines.append(f"CLICKABLE REGIONS ({len(focus_list)}):")
    if not focus_list:
        lines.append("  (none \u2014 focus list empty)")
    else:
        # Group by screen.
        by_screen: dict[str, list] = {}
        for i, item in enumerate(focus_list):
            scr = item.get("screen", "")
            by_screen.setdefault(scr, []).append((i, item))

        idx = 0
        for scr, items in by_screen.items():
            scr_header = f"  [{scr or '?'}]"
            lines.append(_dim(scr_header) if colour else scr_header)
            for _, item in items:
                idx += 1
                label = item.get("label", "")
                action = item.get("action_type", "")
                x, y = item.get("x", 0), item.get("y", 0)
                w, h = item.get("w", 0), item.get("h", 0)
                focused = item.get("is_focused", False)

                # Truncate long labels.
                disp_label = label if len(label) <= 50 else label[:47] + "..."
                focus_mark = "  *focused*" if focused else ""

                pos_str = f"@ ({x},{y}) {w}x{h}"
                line = f"    {idx}: \"{disp_label}\" ({action}) {pos_str}{focus_mark}"

                # Flag anomalies.
                anomalies = []
                if sw and sh:
                    if x + w < 0 or y + h < 0 or x > sw or y > sh:
                        anomalies.append("OFF-SCREEN")
                    if w >= sw - 10 and h >= sh - 10:
                        anomalies.append("FULL-SCREEN")
                if w < 5 or h < 5:
                    anomalies.append("TINY")
                if anomalies:
                    warn = " [" + ", ".join(anomalies) + "]"
                    line += (_red(warn) if colour else warn)
                lines.append(line)

    # Viewports.
    if not focus_only:
        viewports = data.get("viewports", [])
        if viewports:
            lines.append("")
            lines.append("VIEWPORTS:")
            for vp in viewports:
                scr = vp.get("screen", "?")
                pct = vp.get("scroll_percent", 0)
                val = vp.get("scroll_value", 0)
                rng = vp.get("scroll_range", 0)
                page = vp.get("scroll_page", 0)
                lines.append(f"  {scr}: scroll {pct}% (value={val}, range={rng}, page={page})")

    # Mouse.
    mouse = data.get("mouse", {})
    if mouse:
        mx = mouse.get("x", "?")
        my = mouse.get("y", "?")
        fl = mouse.get("focused_label", "")
        fs = mouse.get("focused_screen", "")
        if fl:
            lines.append("")
            lines.append(f"MOUSE: ({mx}, {my}) -> focused: \"{fl}\" [{fs}]")
        else:
            lines.append("")
            lines.append(f"MOUSE: ({mx}, {my}) -> no focus")

    # Cross-reference: scraped vs focus.
    if verify and not focus_only:
        scraped = data.get("scraped", {})
        scraped_buttons = scraped.get("buttons", [])
        scraped_screens = scraped.get("screens", [])

        lines.append("")
        verify_header = "SCRAPE vs FOCUS:"
        lines.append(_cyan(verify_header) if colour else verify_header)
        lines.append(f"  Scraped: {len(scraped_buttons)} buttons from {{{', '.join(scraped_screens)}}}")

        focus_screens = set(item.get("screen", "") for item in focus_list)
        lines.append(f"  Focus:   {len(focus_list)} clickable regions from {{{', '.join(sorted(focus_screens))}}}")

        # Match scraped buttons to focus entries by label substring.
        matched = 0
        unmatched_scraped = []
        for btn in scraped_buttons:
            btn_label = btn.get("label", "")
            found = False
            for fi in focus_list:
                fl_label = fi.get("label", "")
                if btn_label and fl_label and (btn_label in fl_label or fl_label in btn_label):
                    found = True
                    break
            if found:
                matched += 1
            else:
                unmatched_scraped.append(btn_label)

        # Focus entries not in scrape.
        focus_labels = set(fi.get("label", "") for fi in focus_list if fi.get("label"))
        scraped_labels = set(b.get("label", "") for b in scraped_buttons if b.get("label"))
        extra_focus = []
        for fl_label in focus_labels:
            if not any(fl_label in sl or sl in fl_label for sl in scraped_labels):
                extra_focus.append(fl_label)

        lines.append(f"  Match:   {matched}/{len(scraped_buttons)} scraped buttons found in focus list")

        if unmatched_scraped:
            warn = f"  Missing: {len(unmatched_scraped)} scraped buttons NOT in focus list:"
            lines.append(_red(warn) if colour else warn)
            for lbl in unmatched_scraped[:10]:
                disp = lbl if len(lbl) <= 60 else lbl[:57] + "..."
                lines.append(f"    - \"{disp}\"")
        if extra_focus:
            lines.append(f"  Extra:   {len(extra_focus)} focus regions not in scrape")
            for lbl in extra_focus[:10]:
                disp = lbl if len(lbl) <= 60 else lbl[:57] + "..."
                lines.append(f"    + \"{disp}\"")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Label and interaction normalization shared with the handler layer
# ---------------------------------------------------------------------------

def _normalize_label(s: str) -> str:
    """Normalize a label for comparison — fold quotes, strip bullets, collapse whitespace.

    Also strips all quote characters entirely so the agent can match
    'Thank you' against the game's "Thank you" without caring about
    punctuation.
    """
    import re
    for ch in "\u201c\u201d\u201e\u201f\u2033":
        s = s.replace(ch, '"')
    for ch in "\u2018\u2019\u201a\u201b\u2032":
        s = s.replace(ch, "'")
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    s = s.replace("\u2026", "...")
    s = re.sub(r"^[\s\u2022\u25cf\u25e6\u2023\-\*]+", "", s)
    s = re.sub(r"^\d+[\.\)]\s*", "", s)
    # Drop all straight quotes so 'Thank you' == "Thank you".
    s = s.replace('"', "").replace("'", "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _choice_interactions(state: dict) -> list[dict]:
    return [
        interaction for interaction in (state.get("interactions") or [])
        if isinstance(interaction, dict)
        and (
            interaction.get("source") == "choice"
            or interaction.get("type") == "choice"
        )
    ]


def _raw_pending_labels(pending: dict | None) -> list[str]:
    """Normalize the actionable labels carried by private request metadata."""
    if not isinstance(pending, dict):
        return []
    labels = []
    for choice in pending.get("choices") or []:
        label = (
            choice.get("label") or choice.get("caption")
            if isinstance(choice, dict) else choice
        )
        if str(label or "").strip():
            labels.append(_normalize_label(str(label).strip()))
    return labels
