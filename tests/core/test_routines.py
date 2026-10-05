"""Time-based routines: rooms without sensors, following the day, blending,
a room's own period times, timers."""

from datetime import date, time, timedelta

import pytest

from custom_components.room_routines.core.blend import blend, fraction
from custom_components.room_routines.core.looks import NOTHING, ON, LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import (
    BLEND_STEP,
    ROUTINE,
    ApplyLook,
    Room,
    RoomConfig,
    State,
    TurnOff,
    WakeAt,
)
from custom_components.room_routines.core.timers import (
    CHOSEN_DAYS,
    OFF,
    WORKDAYS,
    Timer,
    Today,
    describe,
    due,
    timer_from,
    timer_to,
)

from .conftest import at

A, B = "light.play", "light.points"
FOCUS = Look({A: LightTarget(True, 36, 5000), B: LightTarget(True, 18, 4900)})
DARK = Look({A: LightTarget(True, 5, 2200), B: LightTarget(True, 2, 2200)})


def office(**over) -> RoomConfig:
    base = dict(
        name="Office", lights=(A, B), triggers=(),
        looks={"Morning": FOCUS, "Evening": DARK},
        blends={"Day": 300},
        period_starts={"Evening": time(18, 0)},
    )
    base.update(over)
    return RoomConfig(**base)


def room(config: RoomConfig, period: str, now, lights_on=False) -> Room:
    schedule = default_schedule().with_starts(dict(config.period_starts))
    return Room(config, schedule, period, lights_on, now)


# ---- blending ----------------------------------------------------------------


def test_blend_moves_brightness_in_a_line_and_white_in_mireds():
    half = blend(FOCUS, DARK, 0.5)
    assert half.lights[A].brightness_pct == pytest.approx(20.5)
    # Halfway in mireds between 5000 K (200) and 2200 K (454.5) is ~3058 K, not 3600 K.
    assert half.lights[A].color_temp_kelvin == pytest.approx(3058, abs=2)
    assert blend(FOCUS, DARK, 0) == FOCUS
    assert blend(FOCUS, DARK, 1).lights[A] == DARK.lights[A]


def test_what_cannot_blend_waits_for_the_period_change():
    last = Look({A: ON})
    assert blend(last, DARK, 0.5) == last  # last brightness: no number to move
    assert blend(FOCUS, NOTHING, 0.5) == FOCUS
    going_off = blend(FOCUS, Look({A: LightTarget(False), B: LightTarget(False)}), 0.5)
    assert going_off.lights[A].on and going_off.lights[A].brightness_pct == pytest.approx(18.5)


def test_fraction_is_only_inside_the_window():
    nxt = at(5, 18)
    assert fraction(at(5, 12, 59), nxt, 300) is None
    assert fraction(at(5, 15, 30), nxt, 300) == pytest.approx(0.5)
    assert fraction(at(5, 18), nxt, 300) is None
    assert fraction(at(5, 15), nxt, 0) is None


# ---- a room's own times ------------------------------------------------------


def test_a_rooms_own_evening_starts_later():
    house = default_schedule()
    mine = house.with_starts({"Evening": time(18, 0)})
    assert house.current(at(5, 17, 30)).name == "Evening"
    assert mine.current(at(5, 17, 30)).name == "Day"
    assert mine.current(at(5, 18, 0)).name == "Evening"


# ---- rooms without sensors ---------------------------------------------------


def test_start_runs_the_routine_and_a_room_without_sensors_never_counts_down():
    r = room(office(), "Day", at(5, 9))
    d = r.start(at(5, 9), "button")
    [apply] = [a for a in d.actions if isinstance(a, ApplyLook)]
    assert apply.look == FOCUS  # Day borrows Morning's look
    assert r.state is State.OWNED
    assert not any(isinstance(a, WakeAt) for a in d.actions)
    assert r.tick(at(5, 12)).actions == ()


def test_blending_through_the_afternoon_then_the_evening_look():
    r = room(office(), "Day", at(5, 9))
    r.start(at(5, 9), "button")
    assert r.blend_tick(at(5, 12, 0)).actions == ()  # before 13:00
    d = r.blend_tick(at(5, 15, 30))
    [apply] = d.actions
    assert apply.transition == BLEND_STEP
    assert apply.look.lights[A].brightness_pct == pytest.approx(20.5)
    assert "blending into Evening (50 %)" in d.reason
    assert r.blend_tick(at(5, 15, 30)).actions == ()  # same look: nothing re-sent
    d = r.period_changed("Evening", at(5, 18))
    [apply] = d.actions
    assert apply.look == DARK


def test_switching_on_mid_blend_starts_at_the_blended_look():
    r = room(office(), "Day", at(5, 14))
    d = r.start(at(5, 15, 30), "button")
    [apply] = d.actions
    assert apply.look.lights[A].brightness_pct == pytest.approx(20.5)


def test_a_hand_change_pauses_following_until_the_lights_go_off():
    r = room(office(), "Day", at(5, 9))
    r.start(at(5, 9), "button")
    d = r.lights(True, own=False, now=at(5, 10))
    assert r.paused and d.change is not None
    assert "following paused until the lights are switched off" in d.reason
    assert r.blend_tick(at(5, 15)).actions == ()
    assert r.period_changed("Evening", at(5, 18)).actions == ()
    r.lights(False, own=False, now=at(5, 19))
    assert r.state is State.IDLE and not r.paused
    d = r.start(at(5, 20), "button")
    assert d.actions[0].look == DARK


def test_lights_switched_on_by_hand_are_taken_over_in_a_routine_room():
    r = room(office(on_by_hand=ROUTINE), "Day", at(5, 9))
    d = r.lights(True, own=False, now=at(5, 9))
    [wake] = d.actions
    assert isinstance(wake, WakeAt) and r.state is State.MANUAL
    d = r.tick(wake.at)
    assert r.state is State.OWNED
    assert d.actions[0].look == FOCUS


def test_lights_switched_on_by_hand_are_left_alone_by_default():
    r = room(office(), "Day", at(5, 9))
    d = r.lights(True, own=False, now=at(5, 9))
    assert d.actions == () and r.state is State.MANUAL
    assert r.blend_tick(at(5, 15)).actions == ()


def test_stop_switches_off_whatever_switched_the_lights_on():
    r = room(office(), "Evening", at(5, 20), lights_on=True)
    d = r.stop(at(6, 1), "timer")
    [off] = d.actions
    assert isinstance(off, TurnOff) and r.state is State.IDLE
    assert r.stop(at(6, 1, 1), "timer").actions == ()


def test_a_motion_room_started_by_a_timer_counts_down_when_nobody_is_there():
    cfg = office(triggers=("binary_sensor.m",))
    r = room(cfg, "Day", at(5, 9))
    d = r.start(at(5, 9), "timer")
    assert any(isinstance(a, WakeAt) for a in d.actions)


def test_blending_a_scene_look_needs_the_scene_read():
    cfg = office(looks={"Morning": Look(scene="scene.focus"), "Evening": DARK})
    r = room(cfg, "Day", at(5, 9))
    r.start(at(5, 9), "button")
    assert r.blend_tick(at(5, 15, 30)).actions == ()  # can't read the scene
    r.scene_reader = lambda scene: dict(FOCUS.lights) if scene == "scene.focus" else None
    [apply] = r.blend_tick(at(5, 15, 30)).actions
    assert apply.look.lights[A].brightness_pct == pytest.approx(20.5)


# ---- timers -------------------------------------------------------------------


def test_timers_check_days_darkness_and_who_is_home():
    t = Timer(time(8, 30), days=WORKDAYS, only_dark=True, only_home=("person.alex",))
    monday = date(2026, 10, 5)
    home = {"person.alex": True}
    assert due(t, Today(monday, workday=True, dark=True, home=home)) == (True, "")
    assert due(t, Today(monday, workday=False, dark=True, home=home)) == (False, "not a workday")
    assert due(t, Today(monday, workday=True, dark=False, home=home)) == (False, "it isn't dark")
    assert due(t, Today(monday, workday=True, dark=True, home={})) == (False, "nobody chosen is home")
    # No Workday sensor: Monday to Friday.
    assert due(t, Today(date(2026, 10, 10), workday=None, dark=True, home=home))[0] is False


def test_chosen_days_and_round_trip():
    t = Timer(time(23, 0), action=OFF, days=CHOSEN_DAYS, weekdays=frozenset({5, 6}))
    assert due(t, Today(date(2026, 10, 10), None, False))[0] is True  # Saturday
    assert due(t, Today(date(2026, 10, 5), None, False)) == (False, "not on Mondays")
    assert timer_from(timer_to(t)) == t
    assert describe(t) == "off at 23:00 on Sat, Sun"
    with pytest.raises(ValueError):
        Timer(time(8), days=CHOSEN_DAYS)


def test_config_checks():
    with pytest.raises(ValueError):
        office(on_by_hand="sometimes")
    with pytest.raises(ValueError):
        office(blends={"Day": -5})
    assert not office().has_sensors
    assert timedelta(minutes=2) == BLEND_STEP
