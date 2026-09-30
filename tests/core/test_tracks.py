"""Normal days and Dark Days: percent of a clear day, from the weather or a light sensor."""

from datetime import timedelta

import pytest

from custom_components.room_routines.core.daylight import DaylightReference, bucket
from custom_components.room_routines.core.looks import NOTHING, ON, LightTarget, Look, resolve, scaled
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig
from custom_components.room_routines.core.sun import SunPosition
from custom_components.room_routines.core.tracks import (
    DIM,
    NORMAL,
    SENSOR,
    WEATHER,
    LightLevel,
    TrackChooser,
    TrackSettings,
    default_periods,
)

from .conftest import at

WINDOW = "sensor.window_lux"
STAIRS = "sensor.hallway_stairs_lux"
SETTINGS = TrackSettings(on=True, sensor=WINDOW, fallback=STAIRS)
HIGH = SunPosition(30.0, True)  # mid-morning, sun well up
DOWN = SunPosition(-5.0, True)


def chooser(settings=SETTINGS, clear_lux=10000.0):
    c = TrackChooser(settings)
    # Both sensors read 10000 lx on a clear morning with the sun at 30 degrees.
    for sensor in (WINDOW, STAIRS):
        c.references[sensor] = DaylightReference({bucket(30.0, True): clear_lux})
    return c


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


def test_weather_first_picks_the_day_straight_away():
    c = chooser()
    c.weather(25.0, at(30, 10))
    assert c.update(at(30, 10), HIGH, True, first=True)
    assert c.track == DIM
    assert c.reason == "25 % of a clear day, from the weather"


def test_a_bright_cloudy_day_is_normal_not_dark():
    c = chooser()
    c.weather(70.0, at(30, 10))
    c.update(at(30, 10), HIGH, True, first=True)
    assert c.track == NORMAL


def test_outside_the_chosen_periods_it_is_always_a_normal_day():
    c = chooser()
    c.weather(5.0, at(30, 10))
    c.update(at(30, 10), HIGH, True, first=True)
    assert c.track == DIM
    # Not a Dark Day period: Normal at once, no hold.
    assert c.update(at(30, 10, 1), DOWN, False)
    assert c.track == NORMAL and c.reason == "not a Dark Day period"


def test_sun_down_in_a_chosen_period_is_a_dark_day():
    c = chooser()
    assert c.update(at(30, 6), DOWN, True, first=True)
    assert c.track == DIM and c.reason == "the sun is down"


def test_hold_and_the_gap_between_thresholds():
    c = chooser()
    c.weather(80.0, at(30, 9))
    c.update(at(30, 9), HIGH, True, first=True)
    assert c.track == NORMAL
    c.weather(30.0, at(30, 9, 10))
    assert not c.update(at(30, 9, 10), HIGH, True)  # Normal has held only 10 min
    assert c.update(at(30, 9, 21), HIGH, True)
    assert c.track == DIM
    c.weather(50.0, at(30, 10))  # between 40 and 55: stays
    assert not c.update(at(30, 10), HIGH, True)
    c.weather(60.0, at(30, 10, 15))
    assert c.update(at(30, 10, 15), HIGH, True)
    assert c.track == NORMAL


def test_first_pick_between_the_thresholds_goes_by_the_middle():
    c = chooser()
    c.weather(45.0, at(30, 9))
    c.update(at(30, 9), HIGH, True, first=True)
    assert c.track == DIM
    c2 = chooser()
    c2.weather(50.0, at(30, 9))
    c2.update(at(30, 9), HIGH, True, first=True)
    assert c2.track == NORMAL


def test_stale_weather_falls_back_to_the_light_sensor():
    c = chooser()
    c.weather(80.0, at(30, 9))
    c.reading(WINDOW, 2000, at(30, 9))  # 20 % of its clear 10000 lx
    assert c.current(at(30, 9, 30), HIGH).source == WEATHER
    got = c.current(at(30, 10), HIGH)  # weather now an hour old
    assert got.source == SENSOR and got.pct == 20.0 and got.sensor == WINDOW


def test_sensor_first_when_chosen():
    c = chooser(TrackSettings(on=True, sensor=WINDOW, first=SENSOR))
    c.weather(80.0, at(30, 9))
    c.reading(WINDOW, 2000, at(30, 9))
    assert c.current(at(30, 9, 5), HIGH).source == SENSOR
    # A sun height the sensor hasn't learned yet: the weather answers.
    assert c.current(at(30, 9, 5), SunPosition(12.0, False)).source == WEATHER


def test_backup_sensor_when_the_main_one_is_unavailable():
    c = chooser(TrackSettings(on=True, weather=False, sensor=WINDOW, fallback=STAIRS))
    c.reading(WINDOW, None, at(30, 9))
    c.reading(STAIRS, 9000, at(30, 9))
    got = c.current(at(30, 9, 5), HIGH)
    assert got.sensor == STAIRS and got.pct == 90.0


def test_no_reading_keeps_the_day():
    c = chooser()
    c.weather(20.0, at(30, 9))
    c.update(at(30, 9), HIGH, True, first=True)
    assert not c.update(at(30, 11), HIGH, True)  # weather stale, no sensor reading
    assert c.track == DIM and c.reason == "no light reading"


def test_off_is_always_normal():
    c = chooser(TrackSettings(on=False, sensor=WINDOW))
    c.weather(5.0, at(30, 9))
    assert not c.update(at(30, 9), HIGH, True, first=True)
    assert c.track == NORMAL


def test_weather_only_needs_no_sensor():
    s = TrackSettings(on=True)
    assert s.enabled and s.order() == (WEATHER,)
    assert not TrackSettings(on=True, weather=False).enabled


def test_chosen_by_hand_holds_until_released():
    c = chooser()
    c.weather(80.0, at(30, 9))
    c.update(at(30, 9), HIGH, True, first=True)
    assert c.choose(DIM, at(30, 9, 1))
    assert not c.update(at(30, 12), HIGH, True)
    assert c.track == DIM
    c.release()
    c.weather(80.0, at(30, 17))
    assert c.update(at(30, 17), HIGH, True, first=True)
    assert c.track == NORMAL


def test_settings_are_checked():
    with pytest.raises(ValueError):
        TrackSettings(dark_below=50, normal_above=50)
    with pytest.raises(ValueError):
        TrackSettings(brightness_pct=0)
    with pytest.raises(ValueError):
        TrackSettings(brightness_pct=301)
    with pytest.raises(ValueError):
        TrackSettings(first="moon")


def test_default_periods_are_the_daytime_ones():
    s = default_schedule()
    assert default_periods({p.name: p.start for p in s.periods}) == ("Morning", "Day")


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
    assert "Dark Day" in d.reason and "Day look for Dark Days" in d.reason
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


# ---- auto-dim --------------------------------------------------------------------


def test_scaling_turns_set_brightness_down_only():
    look = Look({"light.a": LightTarget(True, 66, 2700), "light.b": ON, "light.c": LightTarget(False)})
    half = scaled(look, 0.5)
    assert half.lights["light.a"] == LightTarget(True, 33, 2700)
    assert half.lights["light.b"] == ON  # last brightness: no number to halve
    assert half.lights["light.c"] == LightTarget(False)
    assert scaled(Look({"light.a": LightTarget(True, 1.5)}), 0.1).lights["light.a"].brightness_pct == 1.0
    assert scaled(Look(scene="scene.x"), 0.5) == Look(scene="scene.x")


def test_dim_day_without_a_dim_look_uses_the_normal_look_turned_down():
    cfg = RoomConfig("Kitchen", ("light.a",), ("binary_sensor.m",), looks={"Day": DAY}, dim_looks={"Evening": EVENING})
    r = Room(cfg, default_schedule(), "Day", False, at(30, 12), track=DIM, auto_dim=0.5)
    d = r.sensor("binary_sensor.m", True, at(30, 12))
    [apply] = [a for a in d.actions if isinstance(a, ApplyLook)]
    assert apply.look.lights["light.a"].brightness_pct == 30  # Day's 60 % at 50 %
    assert "Day look at 50 %" in d.reason
    # An explicit Dim look is used as it is.
    r2 = Room(cfg, default_schedule(), "Evening", False, at(30, 18), track=DIM, auto_dim=0.5)
    [apply2] = [a for a in r2.sensor("binary_sensor.m", True, at(30, 18)).actions if isinstance(a, ApplyLook)]
    assert apply2.look == EVENING and apply2.factor == 1.0
    # Normal days are never dimmed.
    r3 = Room(cfg, default_schedule(), "Day", False, at(30, 12), auto_dim=0.5)
    [apply3] = [a for a in r3.sensor("binary_sensor.m", True, at(30, 12)).actions if isinstance(a, ApplyLook)]
    assert apply3.look == DAY


def test_auto_dim_for_a_scene_is_passed_on_to_be_read_and_dimmed():
    cfg = RoomConfig("Kitchen", ("light.a",), ("binary_sensor.m",), looks={"Day": Look(scene="scene.kitchen_day")})
    r = Room(cfg, default_schedule(), "Day", False, at(30, 12), track=DIM, auto_dim=0.5)
    [apply] = [a for a in r.sensor("binary_sensor.m", True, at(30, 12)).actions if isinstance(a, ApplyLook)]
    assert apply.look.scene == "scene.kitchen_day" and apply.factor == 0.5


def test_brightening_stops_at_each_lights_maximum():
    look = Look({"light.a": LightTarget(True, 40), "light.b": LightTarget(True, 80), "light.c": ON})
    up = scaled(look, 1.5)
    assert up.lights["light.a"].brightness_pct == 60
    assert up.lights["light.b"].brightness_pct == 100  # not 120
    assert up.lights["light.c"] == ON
