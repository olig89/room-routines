"""Changing stored settings, as the form and the panel both do."""

import pytest

from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.serial import schedule_to
from custom_components.room_routines.settings import (
    SettingsError,
    add_room,
    remove_room,
    set_look,
    set_periods,
    set_tracks,
    update_room,
)

ROOM = {
    "name": "Small Bathroom",
    "area_id": "small_bathroom",
    "lights": ["light.ceiling", "light.mirror"],
    "triggers": ["binary_sensor.motion"],
    "timeout_s": 30,
    "fade_out_s": 15,
    "cooldown_s": 30,
    "drift_s": 90,
}


def base():
    return {**schedule_to(default_schedule()), "rooms": {}, "stray": 1}


def test_add_room_starts_with_last_brightness_everywhere_and_drops_stray_keys():
    options, room_id = add_room(base(), ROOM)
    assert set(options) == {"periods", "alt_days", "rooms"}
    room = options["rooms"][room_id]
    assert room["looks"] == {"Early morning": {"lights": {"light.ceiling": {"on": True}, "light.mirror": {"on": True}}}}


def test_a_room_needs_a_name_and_a_trigger():
    with pytest.raises(SettingsError) as err:
        add_room(base(), {**ROOM, "name": " "})
    assert err.value.key == "no_name"
    with pytest.raises(SettingsError) as err:
        add_room(base(), {**ROOM, "triggers": []})
    assert err.value.key == "invalid_room"


def test_update_keeps_looks_for_lights_still_in_the_room():
    options, room_id = add_room(base(), ROOM)
    options = update_room(options, room_id, {"lights": ["light.ceiling"], "timeout_s": 60})
    room = options["rooms"][room_id]
    assert room["timeout_s"] == 60
    assert room["looks"]["Early morning"]["lights"] == {"light.ceiling": {"on": True}}


def test_remove_room():
    options, room_id = add_room(base(), ROOM)
    assert remove_room(options, room_id)["rooms"] == {}
    with pytest.raises(SettingsError):
        remove_room(options, "nope")


def test_periods_rename_swap_and_remove_carry_the_looks():
    options, room_id = add_room(base(), ROOM)
    options = set_look(options, room_id, "Evening", {"lights": {"light.ceiling": {"on": True, "brightness_pct": 40}}})
    options = set_look(options, room_id, "Day", {"nothing": True})
    rows = [
        {"name": "Overnight", "start": "23:00"},
        {"name": "Early morning", "start": "05:30"},
        {"name": "Morning", "start": "07:00"},
        {"name": "Evening", "start": "09:00"},  # Day and Evening swap names
        {"name": "Day", "start": "17:00"},
    ]
    options = set_periods(options, rows, [5, 6], {"Day": "Evening", "Evening": "Day"})
    looks = options["rooms"][room_id]["looks"]
    assert looks["Evening"] == {"nothing": True}
    assert looks["Day"]["lights"]["light.ceiling"]["brightness_pct"] == 40
    assert options["alt_days"] == [5, 6]
    options = set_periods(options, rows[:4], [], {"Day": None})
    assert "Day" not in options["rooms"][room_id]["looks"]


def test_periods_must_be_valid():
    with pytest.raises(SettingsError) as err:
        set_periods(base(), [], [])
    assert err.value.key == "last_period"
    with pytest.raises(SettingsError) as err:
        set_periods(base(), [{"name": "A", "start": "07:00"}, {"name": "A", "start": "09:00"}], [])
    assert err.value.key == "invalid_periods"


def test_set_look_only_keeps_the_rooms_lights_and_can_borrow():
    options, room_id = add_room(base(), ROOM)
    options = set_look(options, room_id, "Evening", {"lights": {"light.ceiling": {"on": False}, "light.elsewhere": {"on": True}}})
    assert options["rooms"][room_id]["looks"]["Evening"] == {"lights": {"light.ceiling": {"on": False}}}
    options = set_look(options, room_id, "Evening", None)
    assert "Evening" not in options["rooms"][room_id]["looks"]
    with pytest.raises(SettingsError):
        set_look(options, room_id, "Brunch", None)


def test_dim_looks_are_saved_apart_and_follow_period_renames():
    options, rid = add_room(base(), ROOM)
    options = set_look(options, rid, "Evening", {"scene": "scene.bath_dim"}, track="dim")
    assert options["rooms"][rid]["dim_looks"] == {"Evening": {"scene": "scene.bath_dim"}}
    assert "Evening" not in options["rooms"][rid]["looks"]
    rows = [{**r, "name": "Dusk"} if r["name"] == "Evening" else r for r in options["periods"]]
    options = set_periods(options, rows, [], {"Evening": "Dusk"})
    assert options["rooms"][rid]["dim_looks"] == {"Dusk": {"scene": "scene.bath_dim"}}
    options = set_look(options, rid, "Dusk", None, track="dim")
    assert options["rooms"][rid]["dim_looks"] == {}
    with pytest.raises(SettingsError):
        set_look(options, rid, "Dusk", None, track="bright")


def test_removing_a_light_drops_it_from_dim_looks_too():
    options, rid = add_room(base(), ROOM)
    look = {"lights": {"light.ceiling": {"on": True}, "light.mirror": {"on": False}}}
    options = set_look(options, rid, "Day", look, track="dim")
    options = update_room(options, rid, {"lights": ["light.ceiling"]})
    assert options["rooms"][rid]["dim_looks"]["Day"] == {"lights": {"light.ceiling": {"on": True}}}


def test_dark_day_settings():
    options = set_tracks(base(), {"sensor": "sensor.window", "dim_below": 600, "normal_above": 1200})
    assert options["tracks"] == {
        "sensor": "sensor.window", "fallback": None, "dim_below": 600.0, "normal_above": 1200.0, "auto_dim_pct": 100.0,
    }
    assert set_tracks(base(), {"sensor": "sensor.window", "auto_dim_pct": 50})["tracks"]["auto_dim_pct"] == 50.0
    assert "stray" not in options
    with pytest.raises(SettingsError):
        set_tracks(base(), {"sensor": "sensor.window", "dim_below": 900, "normal_above": 900})
