"""Changing stored settings, as the form and the panel both do."""

import pytest

from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.serial import schedule_to
from custom_components.room_routines.settings import (
    SettingsError,
    add_area_rooms,
    add_room,
    remove_room,
    set_house_rules,
    set_look,
    set_periods,
    set_tracks,
    unhide_area,
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


def test_a_room_needs_a_name_and_lights_but_not_sensors():
    with pytest.raises(SettingsError) as err:
        add_room(base(), {**ROOM, "name": " "})
    assert err.value.key == "no_name"
    with pytest.raises(SettingsError) as err:
        add_room(base(), {**ROOM, "lights": []})
    assert err.value.key == "invalid_room"
    options, room_id = add_room(base(), {**ROOM, "triggers": []})
    assert options["rooms"][room_id]["triggers"] == []


def test_routine_settings_are_kept_and_checked():
    data = {
        **ROOM, "triggers": [], "on_by_hand": "routine", "blends": {"Day": 300, "Evening": 0},
        "period_starts": {"Evening": "18:00"},
        "timers": [{"at": "08:30", "action": "on", "days": "workdays", "only_dark": True}],
    }
    options, room_id = add_room(base(), data)
    room = options["rooms"][room_id]
    assert room["blends"] == {"Day": 300}
    assert room["period_starts"] == {"Evening": "18:00"}
    assert room["timers"][0]["days"] == "workdays"
    with pytest.raises(SettingsError) as err:
        add_room(base(), {**data, "period_starts": {"Evening": "09:00"}})  # same as Day
    assert err.value.key == "invalid_room_times"
    with pytest.raises(SettingsError):
        add_room(base(), {**data, "timers": [{"at": "08:30", "action": "dance"}]})
    renamed = set_periods(
        options,
        [{"name": n, "start": t} for n, t in (
            ("Overnight", "23:00"), ("Early morning", "05:30"), ("Morning", "07:00"),
            ("Work", "09:00"), ("Evening", "17:00"),
        )],
        [], {"Day": "Work"},
    )
    assert renamed["rooms"][room_id]["blends"] == {"Work": 300}


def test_areas_become_rooms_that_start_off_and_stay_hidden_once_removed():
    areas = {"office": ("Office", ["light.a", "light.b"]), "hall": ("Hall", []), "wc": ("WC", ["light.c"])}
    options = add_area_rooms(base(), areas)
    rooms = options["rooms"]
    assert set(rooms) == {"area_office", "area_wc"}  # the hall has no lights
    assert rooms["area_office"]["start_mode"] == "off" and rooms["area_office"]["triggers"] == []
    assert add_area_rooms(options, areas) is None  # nothing new
    options = remove_room(options, "area_wc")
    assert options["hidden_areas"] == ["wc"]
    assert add_area_rooms(options, areas) is None
    options = unhide_area(options, "wc")
    assert "area_wc" in add_area_rooms(options, areas)["rooms"]


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
    options = set_tracks(base(), {"on": True, "sensor": "sensor.window", "dark_below_pct": 30, "normal_above_pct": 50})
    assert options["tracks"] == {
        "on": True, "weather": True, "sensor": "sensor.window", "fallback": None, "first": "weather",
        "periods": None, "dark_below_pct": 30.0, "normal_above_pct": 50.0, "brightness_pct": 100.0, "dark_below_wm2": 150.0,
    }
    assert set_tracks(base(), {"brightness_pct": 150})["tracks"]["brightness_pct"] == 150.0
    assert "stray" not in options
    with pytest.raises(SettingsError):
        set_tracks(base(), {"dark_below_pct": 50, "normal_above_pct": 50})
    with pytest.raises(SettingsError):
        set_tracks(base(), {"first": "moon"})


def test_dark_day_periods_keep_only_known_ones_and_follow_renames():
    options = set_tracks(base(), {"on": True, "periods": ["Morning", "Day", "Nope"]})
    assert options["tracks"]["periods"] == ["Morning", "Day"]
    rows = [{"name": n, "start": t} for n, t in (
        ("Overnight", "23:00"), ("Early morning", "05:30"), ("Breakfast", "07:00"), ("Evening", "17:00"),
    )]
    options = set_periods(options, rows, [], {"Morning": "Breakfast", "Day": None})
    assert options["tracks"]["periods"] == ["Breakfast"]


def test_old_lux_settings_carry_over():
    from custom_components.room_routines.core.serial import tracks_from

    old = {"tracks": {"sensor": "sensor.window", "fallback": None, "dim_below": 800, "normal_above": 1500, "auto_dim_pct": 60}}
    t = tracks_from(old)
    assert t.on and t.weather and t.sensor == "sensor.window"
    assert (t.dark_below, t.normal_above, t.brightness_pct) == (40, 55, 60)
    assert not tracks_from({}).on


BEDTIME = {"entity": "binary_sensor.baby_bedtime", "state": "on"}


def test_a_room_keeps_its_starters_conditions_and_rules():
    data = {
        **ROOM,
        "starters": [{"entity": "binary_sensor.pc", "state": "on"}],
        "only_when": [{**BEDTIME, "negate": True}],
        "rules": [{"when": BEDTIME, "action": "look", "period": "Overnight"}],
    }
    options, room_id = add_room(base(), data)
    room = options["rooms"][room_id]
    assert room["starters"] == [{"entity": "binary_sensor.pc", "state": "on"}]
    assert room["only_when"] == [{**BEDTIME, "negate": True}]
    assert room["rules"] == [{"when": BEDTIME, "action": "look", "period": "Overnight"}]
    with pytest.raises(SettingsError) as err:
        add_room(base(), {**ROOM, "rules": [{"when": BEDTIME, "action": "cap"}]})
    assert err.value.key == "invalid_rules"


def test_house_rules_name_real_rooms_and_follow_removals_and_renames():
    options, a = add_room(base(), ROOM)
    options, b = add_room(options, {**ROOM, "name": "Landing", "area_id": "landing"})
    options = set_house_rules(options, [
        {"when": BEDTIME, "action": "look", "period": "Overnight", "rooms": [a]},
        {"when": {"entity": "zone.home", "state": "0"}, "action": "nothing"},
    ])
    assert options["house_rules"][0]["rooms"] == [a]
    with pytest.raises(SettingsError):
        set_house_rules(options, [{"when": BEDTIME, "action": "nothing", "rooms": ["nope"]}])
    renamed = set_periods(
        options, [{"name": "Night" if r["name"] == "Overnight" else r["name"], "start": r["start"]} for r in options["periods"]],
        [], {"Overnight": "Night"},
    )
    assert renamed["house_rules"][0]["period"] == "Night"
    removed = remove_room(options, a)
    # the rule was only for the removed room: gone, not widened to every room
    assert removed["house_rules"] == [{"when": {"entity": "zone.home", "state": "0"}, "action": "nothing"}]


ROWS_WITHOUT_EARLY = [
    {"name": "Morning", "start": "06:30"}, {"name": "Day", "start": "09:00"},
    {"name": "Late Afternoon", "start": "17:00"}, {"name": "Evening", "start": "20:00"},
    {"name": "Overnight", "start": "23:00"},
]


def test_removing_the_period_holding_a_rooms_only_look_keeps_the_look():
    # A new room's one look sits under the earliest period (Early morning) and
    # every other period uses it. Removing Early morning used to delete it.
    options, room_id = add_room(base(), ROOM)
    only = options["rooms"][room_id]["looks"]["Early morning"]
    options = set_periods(options, ROWS_WITHOUT_EARLY, [], {"Early morning": None})
    assert options["rooms"][room_id]["looks"] == {"Morning": only}


def test_a_removed_periods_look_goes_only_where_it_was_used():
    options, room_id = add_room(base(), ROOM)
    own = {"lights": {"light.ceiling": {"on": True, "brightness_pct": 40}}}
    options = set_look(options, room_id, "Morning", own)
    # Morning has its own look, so nothing was using Early morning's: it goes.
    options = set_periods(options, ROWS_WITHOUT_EARLY, [], {"Early morning": None})
    assert options["rooms"][room_id]["looks"] == {"Morning": own}


def test_two_removed_periods_in_a_row_hand_on_the_later_look():
    options, room_id = add_room(base(), ROOM)
    later = {"lights": {"light.ceiling": {"on": True, "brightness_pct": 70}}}
    options = set_look(options, room_id, "Morning", later)
    rows = [{"name": "Day", "start": "09:00"}, {"name": "Evening", "start": "17:00"}, {"name": "Overnight", "start": "23:00"}]
    options = set_periods(options, rows, [], {"Early morning": None, "Morning": None})
    assert options["rooms"][room_id]["looks"] == {"Day": later}


def test_removing_a_period_never_changes_what_the_other_periods_show():
    """Every mix of Normal and Dark Day looks over the five default periods, each
    period removed in turn: every period left shows exactly what it did before."""
    from itertools import product

    from custom_components.room_routines.core.looks import resolve
    from custom_components.room_routines.core.serial import room_from, schedule_from

    rows = schedule_to(default_schedule())["periods"]
    names = [r["name"] for r in rows]
    for choice in product(range(4), repeat=len(names)):
        looks = {n: {"lights": {"light.ceiling": {"on": True, "brightness_pct": 10 + i}}}
                 for i, (n, c) in enumerate(zip(names, choice)) if c & 1}
        dim = {n: {"lights": {"light.ceiling": {"on": True, "brightness_pct": 60 + i}}}
               for i, (n, c) in enumerate(zip(names, choice)) if c & 2}
        if not looks:
            continue
        room = {**ROOM, "looks": looks, "dim_looks": dim}
        options = {**base(), "rooms": {"r": room}}
        old = room_from(room)
        old_schedule = schedule_from(options)
        for gone in names:
            kept = [r for r in rows if r["name"] != gone]
            new_options = set_periods(options, kept, [], {gone: None})
            new = room_from(new_options["rooms"]["r"])
            new_schedule = schedule_from(new_options)
            for r in kept:
                for track in ("normal", "dim"):
                    before = resolve(r["name"], track, old.looks, old.dim_looks, old_schedule).look
                    after = resolve(r["name"], track, new.looks, new.dim_looks, new_schedule).look
                    assert before == after, (choice, gone, r["name"], track)


def test_a_dark_day_look_only_carries_where_it_was_used():
    # The auditor's case: Morning has its own Normal look, so on Dark Days it never
    # used Early morning's Dark Day look and mustn't get it.
    options, room_id = add_room(base(), ROOM)
    early = {"lights": {"light.ceiling": {"on": True, "brightness_pct": 10}}}
    morning = {"lights": {"light.ceiling": {"on": True, "brightness_pct": 70}}}
    options["rooms"][room_id]["looks"] = {"Early morning": early, "Morning": morning}
    options["rooms"][room_id]["dim_looks"] = {"Early morning": {"lights": {"light.ceiling": {"on": True, "brightness_pct": 5}}}}
    options = set_periods(options, ROWS_WITHOUT_EARLY, [], {"Early morning": None})
    assert options["rooms"][room_id]["dim_looks"] == {}


# ---- Lights out ----

from custom_components.room_routines.settings import set_lights_outs  # noqa: E402


def test_lights_outs_get_ids_and_are_checked():
    options, room_id = add_room(base(), ROOM)
    options = set_lights_outs(options, [
        {"name": "Night", "when": "time", "at": "01:00"},
        {"when": "period", "period": "Overnight", "rooms": [room_id], "style": "now", "mode": "live"},
    ])
    first, second = options["lights_outs"]
    assert first["id"] and first["id"] != second["id"] and first["mode"] == "log_only"
    assert second["rooms"] == [room_id] and second["style"] == "now"
    # Saving again keeps the ids.
    again = set_lights_outs(options, options["lights_outs"])
    assert [r["id"] for r in again["lights_outs"]] == [first["id"], second["id"]]
    with pytest.raises(SettingsError) as err:
        set_lights_outs(options, [{"when": "time", "at": "01:00", "rooms": ["nowhere"]}])
    assert err.value.key == "unknown_room"
    for bad in ({"when": "period", "period": "Brunch"}, {"when": "time"}, {"when": "entity"},
                {"when": "time", "at": "01:00", "days": "days"}):
        with pytest.raises(SettingsError) as err:
            set_lights_outs(options, [bad])
        assert err.value.key == "invalid_lights_out"
    with pytest.raises(SettingsError):
        set_lights_outs(options, [first, first])  # the same id twice


def test_lights_outs_follow_period_renames_and_room_removals():
    options, room_id = add_room(base(), ROOM)
    options, other = add_room(options, {**ROOM, "name": "Hall", "area_id": "hall"})
    options = set_lights_outs(options, [
        {"id": "a", "when": "period", "period": "Evening"},
        {"id": "b", "when": "time", "at": "01:00", "rooms": [room_id, other]},
        {"id": "c", "when": "time", "at": "02:00", "rooms": [room_id]},
    ])
    rows = [dict(p) for p in schedule_to(default_schedule())["periods"]]
    rows = [{**r, "name": "Dusk"} if r["name"] == "Evening" else r for r in rows]
    renamed = set_periods(options, rows, [], {"Evening": "Dusk"})
    assert renamed["lights_outs"][0]["period"] == "Dusk"
    removed = set_periods(options, [r for r in rows if r["name"] != "Dusk"], [], {"Evening": None})
    assert [r["id"] for r in removed["lights_outs"]] == ["b", "c"]
    # Removing a room takes it out; a Lights out left with no rooms goes (it would cover the house).
    gone = remove_room(options, room_id)
    assert [r["id"] for r in gone["lights_outs"]] == ["a", "b"]
    assert gone["lights_outs"][1]["rooms"] == [other]
