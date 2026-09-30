"""Normal and Dim days, from one light sensor."""

from datetime import timedelta

import pytest

from custom_components.room_routines.core.looks import NOTHING, LightTarget, Look, resolve
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig
from custom_components.room_routines.core.tracks import DIM, NORMAL, LightLevel, TrackChooser, TrackSettings

from .conftest import at

WINDOW = "sensor.window_lux"
STAIRS = "sensor.hallway_stairs_lux"
SETTINGS = TrackSettings(sensor=WINDOW, fallback=STAIRS, dim_below=800, normal_above=1500)


def chooser():
    return TrackChooser(SETTINGS)


def test_average_is_weighted_by_how_long_each_value_held():
    lvl = LightLevel(timedelta(minutes=15))
    lvl.add(2000, at(30, 11, 45))  # before the window: covers its start
    lvl.add(100, at(30, 11, 55))
    # 12:00 window 11:45-12:00: 10 min at 2000, 5 min at 100.
    assert lvl.average(at(30, 12)) == pytest.approx((2000 * 10 + 100 * 5) / 15)


def test_a_burst_of_readings_doesnt_outweigh_a_long_steady_stretch():
    lvl = LightLevel(timedelta(minutes=15))
    lvl.add(2000, at(30, 11, 30))
    for s in range(0, 60, 10):  # six quick dark readings in the last minute
        lvl.add(100, at(30, 11, 59, s))
    assert lvl.average(at(30, 12)) > 1800


def test_first_reading_picks_the_track_straight_away():
    c = chooser()
    c.reading(WINDOW, 300, at(30, 12))
    assert c.update(at(30, 12), first=True)
    assert c.track == DIM


def test_track_waits_for_the_minimum_hold_and_the_gap_between_thresholds():
    c = chooser()
    c.reading(WINDOW, 3000, at(30, 9))
    c.update(at(30, 9), first=True)
    assert c.track == NORMAL
    c.reading(WINDOW, 500, at(30, 9, 5))
    assert not c.update(at(30, 9, 15))  # average 9:00-9:15 is still above 800
    assert not c.update(at(30, 9, 19))  # dark enough now, but Normal has held only 19 min
    assert c.update(at(30, 9, 21))
    assert c.track == DIM


def test_dim_after_the_hold_then_normal_only_above_the_upper_threshold():
    c = chooser()
    c.reading(WINDOW, 3000, at(30, 9))
    c.update(at(30, 9), first=True)
    c.reading(WINDOW, 500, at(30, 9, 30))
    assert c.update(at(30, 9, 50))
    assert c.track == DIM
    c.reading(WINDOW, 1200, at(30, 10, 30))  # between the thresholds: stays Dim
    assert not c.update(at(30, 11))
    assert c.track == DIM
    c.reading(WINDOW, 1600, at(30, 11))
    assert c.update(at(30, 11, 20))
    assert c.track == NORMAL


def test_unavailable_sensor_keeps_the_track_or_uses_the_fallback():
    c = chooser()
    c.reading(WINDOW, 3000, at(30, 9))
    c.update(at(30, 9), first=True)
    c.reading(WINDOW, None, at(30, 9, 10))  # battery died
    assert not c.update(at(30, 10))
    assert c.track == NORMAL
    c.reading(STAIRS, 100, at(30, 10))
    assert c.update(at(30, 10, 20))
    assert c.track == DIM
    assert c.level(at(30, 10, 20))[1] == STAIRS


def test_chosen_by_hand_holds_until_released():
    c = chooser()
    c.reading(WINDOW, 3000, at(30, 9))
    c.update(at(30, 9), first=True)
    assert c.choose(DIM, at(30, 9, 1))
    assert not c.update(at(30, 12))
    assert c.track == DIM
    assert c.release(at(30, 17))
    assert c.track == NORMAL


def test_thresholds_must_leave_a_gap():
    with pytest.raises(ValueError):
        TrackSettings(WINDOW, dim_below=1000, normal_above=1000)


# ---- which look a room uses --------------------------------------------------

DAY = Look({"light.a": LightTarget(True, 60)})
DAY_DIM = Look({"light.a": LightTarget(True, 100)})
EVENING = Look({"light.a": LightTarget(True, 30)})


def test_dim_look_wins_on_dim_days_and_normal_is_used_where_there_is_none():
    s = default_schedule()
    looks = {"Day": DAY, "Evening": EVENING}
    dim = {"Day": DAY_DIM}
    assert resolve("Day", DIM, looks, dim, s).look is DAY_DIM
    assert resolve("Day", NORMAL, looks, dim, s).look is DAY
    src = resolve("Evening", DIM, looks, dim, s)
    assert src.look is EVENING and src.track == NORMAL


def test_a_periods_normal_look_stops_the_walk_back_to_an_earlier_dim_look():
    s = default_schedule()
    # Evening has its own Normal look, so Dim Evening doesn't borrow Day's Dim look.
    assert resolve("Evening", DIM, {"Day": DAY, "Evening": EVENING}, {"Day": DAY_DIM}, s).look is EVENING
    # Overnight has no look at all: it borrows Evening's.
    assert resolve("Overnight", DIM, {"Day": DAY, "Evening": EVENING}, {"Day": DAY_DIM}, s).look is EVENING


def test_scene_look():
    look = Look(scene="scene.kitchen_evening")
    assert look.lit() == []
    with pytest.raises(ValueError):
        Look({"light.a": LightTarget(True)}, scene="scene.x")
    assert resolve("Day", NORMAL, {"Day": NOTHING}, None, default_schedule()).look.nothing


def test_a_lit_room_drifts_to_its_dim_look_when_the_day_turns_dim():
    cfg = RoomConfig("Kitchen", ("light.a",), ("binary_sensor.m",), looks={"Day": DAY}, dim_looks={"Day": DAY_DIM})
    r = Room(cfg, default_schedule(), "Day", False, at(30, 12))
    first = r.sensor("binary_sensor.m", True, at(30, 12))
    assert [a.look for a in first.actions if isinstance(a, ApplyLook)] == [DAY]
    d = r.track_changed(DIM, at(30, 12, 5))
    [apply] = [a for a in d.actions if isinstance(a, ApplyLook)]
    assert apply.look == DAY_DIM and apply.transition == cfg.drift
    assert "Dim day" in d.reason and "Day Dim look" in d.reason
    assert r.track_changed(DIM, at(30, 12, 6)).actions == ()


def test_an_empty_room_just_remembers_the_track():
    cfg = RoomConfig("Kitchen", ("light.a",), ("binary_sensor.m",), looks={"Day": DAY}, dim_looks={"Day": DAY_DIM})
    r = Room(cfg, default_schedule(), "Day", False, at(30, 12))
    assert r.track_changed(DIM, at(30, 12)).actions == ()
    d = r.sensor("binary_sensor.m", True, at(30, 12, 1))
    assert [a.look for a in d.actions if isinstance(a, ApplyLook)] == [DAY_DIM]


def test_scene_looks_are_passed_on_to_carry_out():
    cfg = RoomConfig("Kitchen", ("light.a",), ("binary_sensor.m",), looks={"Day": Look(scene="scene.kitchen_day")})
    r = Room(cfg, default_schedule(), "Day", False, at(30, 12))
    d = r.sensor("binary_sensor.m", True, at(30, 12))
    [apply] = [a for a in d.actions if isinstance(a, ApplyLook)]
    assert apply.look.scene == "scene.kitchen_day"
