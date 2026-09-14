"""Execute the shim's pause/auto-advance hooks against a controlled clock."""
from types import SimpleNamespace as NS

import pytest

from test_shim_static import load_shim_functions


@pytest.mark.parametrize("main_menu", [False, True])
def test_save_refuses_main_menu_before_serialization(main_menu):
    saved, events = [], []
    ns = load_shim_functions("_vnf_cmd_save", namespace={
        "renpy": NS(store=NS(main_menu=main_menu),
                    loadsave=NS(save=lambda slot, **kw: saved.append(slot))),
        "_vnf_client": NS(push_event=events.append),
        "_vnf_log": lambda message: None,
    })
    ns["_vnf_cmd_save"]("save", {"slot": "test-checkpoint"})
    assert saved == ([] if main_menu else ["test-checkpoint"])
    assert events[-1]["success"] is (not main_menu)
    if main_menu:
        assert "Start or load" in events[-1]["error"]


@pytest.mark.parametrize("mode", ["text", "afm", "external"])
def test_auto_advance_setting_reconciles_runtime_even_when_unchanged(mode):
    player = NS(enabled=True, auto_advance=True, auto_advance_delay=0.05)
    prefs = NS(afm_enable=False, afm_time=0)
    ns = load_shim_functions(
        "_vnf_set_config_key", "_vnf_coerce_config_value",
        "_vnf_enable_auto_advance", "_vnf_disable_auto_advance",
        namespace={
            "vnf_player": player,
            "renpy": NS(game=NS(preferences=prefs)),
            "_vnf_auto_advance_active": False,
            "_vnf_afm_intentionally_off": [True],
            "_vnf_effective_dialogue_advance_mode": lambda: mode,
            "_vnf_compute_afm_time": lambda: 1.0,
            "_vnf_log": lambda message: None,
        })
    assert ns["_vnf_set_config_key"]("auto_advance", True) == (True, True)
    assert ns["_vnf_auto_advance_active"] is True
    assert prefs.afm_enable is (mode == "afm")
    assert ns["_vnf_afm_intentionally_off"] == [False]
    ns["_vnf_set_config_key"]("auto_advance", False)
    assert ns["_vnf_auto_advance_active"] is False
    assert prefs.afm_enable is False


@pytest.mark.parametrize("mode", ["text", "external"])
@pytest.mark.parametrize("delay", [None, 2.0])
def test_pause_pacing_owns_its_timer_and_resets_stale_dialogue(mode, delay):
    dismissed = []
    clock = [100.0]
    state = NS(_last_say_what="Previous dialogue", main_menu=False)
    player = NS(enabled=True, auto_advance=True, fast_forward=False,
                allow_user_override=True, pause_timeout=None, reading_cps=32,
                text_cps=45, post_reveal_hold=1.0, auto_advance_delay=0.3)
    ns = load_shim_functions(
        "_vnf_pause_wrapper", "_vnf_periodic_auto_advance",
        namespace={
            "renpy": NS(store=state, get_screen=lambda tag: None,
                        game=NS(preferences=NS(afm_enable=False)),
                        exports=NS(end_interaction=dismissed.append)),
            "vnf_player": player,
            "_vnf_effective_dialogue_advance_mode": lambda: mode,
            "_vnf_auto_advance_active": True,
            "_vnf_auto_advance_last_what": "Previous dialogue",
            "_vnf_auto_advance_say_time": 0.0,
            "_vnf_has_modal_overlay": lambda: False,
            "_vnf_request": NS(request_id=None),
            "_vnf_autoskip": NS(pause_reasons=[]),
            "_vnf_current_menu_context": [None],
            "_vnf_refresh_transform_pause_reasons": lambda: False,
            "_vnf_auto_advanced_flag": [False],
            "_vnf_log": lambda message: None,
            "_vnf_client": NS(push_event=lambda event: None),
            "time": NS(time=lambda: clock[0]),
            "_time": NS(time=lambda: clock[0]),
        })

    def pause_body(delay=None, **kwargs):
        assert state._vnf_in_pause
        assert state._vnf_in_timed_pause == (delay is not None)
        assert ns["_vnf_auto_advance_last_what"] is None
        tick = ns["_vnf_periodic_auto_advance"]
        tick()
        assert not dismissed  # An expired previous line cannot dismiss this pause.
        clock[0] += 0.1
        tick()
        if mode == "external":
            assert dismissed == [True]
            return
        assert not dismissed
        clock[0] += 5.0
        tick()
        assert dismissed == ([] if delay is not None else [True])

    ns["_vnf_original_pause"] = pause_body
    ns["_vnf_pause_wrapper"](delay)
    assert not state._vnf_in_pause
    assert not state._vnf_in_timed_pause
    assert ns["_vnf_auto_advance_last_what"] is None
