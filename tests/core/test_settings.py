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
