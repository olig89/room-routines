"""Lights out: off now where nobody is, and once empty where someone is (until
the next period starts); "all off now" whoever is there."""

from datetime import date, time, timedelta

import pytest

from custom_components.room_routines.core.lights_out import (
    AT_ENTITY,
    AT_PERIOD,
    AT_TIME,
    NOW,
    LightsOut,
    describe,
    lights_out_from,
    lights_out_to,
)
from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig, State, TurnOff, WakeAt
from custom_components.room_routines.core.signals import Signal
from custom_components.room_routines.core.rules import Condition
from custom_components.room_routines.core.timers import Today

from .conftest import at

LIGHT, LAMP = "light.ceiling", "light.lamp"
MOTION, PRESENCE = "binary_sensor.motion", "binary_sensor.presence"
ON = Look({LIGHT: LightTarget(True)})
UNTIL = at(6, 5, 30)  # the next period start
NIGHT = at(6, 1)


def bathroom(**over) -> RoomConfig:
    base = dict(
        name="Bathroom", lights=(LIGHT,), triggers=(MOTION,), holds=(PRESENCE,),
        looks={"Overnight": ON}, timeout=timedelta(seconds=30), fade_out=timedelta(seconds=15),
    )
    base.update(over)
    return RoomConfig(**base)


def lit(config: RoomConfig, now=None) -> Room:
    """Someone walked in: the room is lit by motion."""
    room = Room(config, default_schedule(), "Overnight", False, now or at(6, 0, 50))
    room.sensor(MOTION, True, now or at(6, 0, 50))
    assert room.state is State.OWNED
    return room


def offs(decision):
    return [a for a in decision.actions if isinstance(a, TurnOff)]


def test_an_empty_room_goes_off_now_with_its_fade():
    room = lit(bathroom())
    room.sensor(MOTION, False, at(6, 0, 55))
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    [off] = offs(d)
    assert off.lights == (LIGHT,) and off.transition == timedelta(seconds=15)
    assert room.state is State.IDLE and room.lights_out_until is None


def test_someone_there_marks_the_room_and_it_goes_off_once_empty():
    room = lit(bathroom())
    room.sensor(PRESENCE, True, at(6, 0, 52))  # sitting still
    room.sensor(MOTION, False, at(6, 0, 55))
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    assert not offs(d) and room.lights_out_until == UNTIL
    assert "once the room is empty" in d.reason
    d = room.sensor(PRESENCE, False, NIGHT + timedelta(minutes=10))
    assert "lights off in 30 s" in d.reason
    [wake] = [a for a in d.actions if isinstance(a, WakeAt)]
    assert wake.at == NIGHT + timedelta(minutes=10, seconds=30)
    d = room.tick(wake.at)
    [off] = offs(d)
    assert off.transition == timedelta(seconds=15)
    assert room.state is State.IDLE and room.lights_out_until is None


def test_a_presence_sensor_counts_even_when_it_only_keeps_lights_on():
    # someone_seen counts holds whoever lit the room; the room's own countdown wouldn't.
    room = Room(bathroom(), default_schedule(), "Overnight", True, at(6, 0, 50))  # lit by hand
    room.sensor(PRESENCE, True, at(6, 0, 51))
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    assert not offs(d) and room.lights_out_until == UNTIL


def test_coming_back_cancels_the_countdown():
    room = lit(bathroom())
    room.lights_out(NIGHT, UNTIL, "Lights out")  # motion still on
    room.sensor(MOTION, False, NIGHT + timedelta(minutes=1))
    d = room.sensor(MOTION, True, NIGHT + timedelta(minutes=1, seconds=10))
    assert "someone's here again" in d.reason and room.lights_out_at is None
    assert not offs(room.tick(NIGHT + timedelta(minutes=1, seconds=40)))
    assert room.state is State.OWNED


def test_it_goes_fully_off_not_back_to_the_routine():
    # A routine runs (Ambient) and motion brightened it: empty means off, not the routine's look.
    config = bathroom(
        someone_looks={"Overnight": Look({LIGHT: LightTarget(True, 80)})},
        looks={"Overnight": Look({LIGHT: LightTarget(True, 10)})},
        timers=(),
    )
    room = Room(config, default_schedule(), "Overnight", False, at(6, 0, 40))
    room.start(at(6, 0, 40), "button")
    room.ambient_on = True
    room.sensor(MOTION, True, at(6, 0, 50))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    room.sensor(MOTION, False, NIGHT + timedelta(minutes=2))
    d = room.tick(NIGHT + timedelta(minutes=2, seconds=30))
    assert offs(d) and not [a for a in d.actions if isinstance(a, ApplyLook)]
    assert room.state is State.IDLE


def test_motion_works_as_usual_after_the_room_went_off():
    room = lit(bathroom())
    room.sensor(MOTION, False, at(6, 0, 55))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    d = room.sensor(MOTION, True, NIGHT + timedelta(minutes=5))
    assert room.state is State.OWNED and [a for a in d.actions if isinstance(a, ApplyLook)]


def test_the_mark_ends_at_the_next_period_start():
    room = lit(bathroom())
    room.lights_out(NIGHT, UNTIL, "Lights out")
    d = room.period_changed("Early morning", UNTIL)
    assert room.lights_out_until is None and "the room carries on" in d.reason
    assert room.state is State.OWNED
    # From now on it's an ordinary room: empty counts down as usual.
    d = room.sensor(MOTION, False, UNTIL + timedelta(minutes=1))
    assert "lights off in 30 s" in d.reason and room.deadline is not None


def test_the_mark_also_ends_on_its_own_wake_up():
    room = lit(bathroom())
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    assert WakeAt(UNTIL) in d.actions and room.next_wake() == UNTIL
    d = room.tick(UNTIL)
    assert room.lights_out_until is None and room.state is State.OWNED


def test_all_off_now_ignores_who_is_there():
    room = lit(bathroom())
    d = room.lights_out(NIGHT, UNTIL, "Lights out", all_now=True)
    assert offs(d) and room.state is State.IDLE


def test_a_room_without_sensors_goes_off_at_once():
    room = Room(bathroom(triggers=(), holds=()), default_schedule(), "Overnight", False, at(6, 0))
    room.start(at(6, 0), "button")
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    assert offs(d) and room.state is State.IDLE


def test_an_idle_room_is_left_alone():
    room = Room(bathroom(), default_schedule(), "Overnight", False, at(6, 0))
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    assert not d.actions and "already off" in d.reason


def test_stealth_counts_as_nobody_there():
    room = lit(bathroom())
    room.lights_out(NIGHT, UNTIL, "Lights out")
    d = room.set_stealth(True, NIGHT + timedelta(minutes=1))
    assert "lights off in 30 s" in d.reason


def test_the_normal_countdown_stands_aside_while_waiting():
    room = lit(bathroom())
    room.sensor(MOTION, False, at(6, 0, 55))  # normal countdown running
    room.sensor(MOTION, True, at(6, 0, 56))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    assert room.deadline is None
    room.sensor(MOTION, False, NIGHT + timedelta(seconds=5))
    assert room.deadline is None and room.lights_out_at is not None


def test_no_boost_while_waiting():
    config = bathroom(someone_looks={"Overnight": Look({LIGHT: LightTarget(True, 80)})})
    room = Room(config, default_schedule(), "Overnight", True, at(6, 0, 40))
    room.state = State.OWNED
    room.ambient_on = True
    room.sensor(PRESENCE, True, at(6, 0, 45))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    d = room.sensor(MOTION, True, NIGHT + timedelta(seconds=10))
    assert not [a for a in d.actions if isinstance(a, ApplyLook)]


def test_starting_the_routine_lets_go():
    room = Room(bathroom(), default_schedule(), "Overnight", False, at(6, 0, 40))
    room.sensor(MOTION, True, at(6, 0, 50))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    room.start(NIGHT + timedelta(minutes=1), "button")
    assert room.lights_out_until is None


def test_switched_off_by_hand_clears_the_mark():
    room = lit(bathroom())
    room.lights_out(NIGHT, UNTIL, "Lights out")
    room.lights(False, own=False, now=NIGHT + timedelta(minutes=1), light=LIGHT)
    assert room.lights_out_until is None and room.state is State.IDLE


def test_lights_held_by_inform_are_not_switched_off():
    signal = Signal("Call", Condition("binary_sensor.call"), {LAMP: LightTarget(True, 100, rgb=(128, 0, 255))})
    room = Room(bathroom(lights=(LIGHT, LAMP), signals=(signal,)), default_schedule(), "Overnight", False, at(6, 0, 40))
    room.sensor(MOTION, True, at(6, 0, 50))
    room.set_signals({"binary_sensor.call": "on"}, {}, at(6, 0, 51))
    room.sensor(MOTION, False, at(6, 0, 55))
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    [off] = offs(d)
    assert off.lights == (LIGHT,)
    assert room.before[LAMP] == LightTarget(False)  # when the call ends, the lamp goes off


def test_the_ends_sensor_still_switches_off_at_once():
    room = lit(bathroom(ends=(MOTION,)))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    d = room.sensor(MOTION, False, NIGHT + timedelta(seconds=20))
    [off] = offs(d)
    assert off.transition is None and room.state is State.IDLE


# ---- the settings ----


def test_round_trip_and_describe():
    lo = LightsOut("a", when=AT_TIME, at=time(1, 0), days="workdays", rooms=("x",), style=NOW, mode="live")
    assert lights_out_from(lights_out_to(lo)) == lo
    assert describe(lo) == "at 01:00 on workdays"
    lo = LightsOut("b", when=AT_PERIOD, period="Overnight", days="days", weekdays=frozenset({4, 5}))
    assert lights_out_from(lights_out_to(lo)) == lo
    assert describe(lo) == "when Overnight starts on Fri, Sat"
    lo = LightsOut("c", when=AT_ENTITY, entity="input_boolean.bed", state="on")
    assert lights_out_from(lights_out_to(lo)) == lo
    assert describe(lo, "Bed") == "when Bed is on"
    assert lo.whole_house and lo.covers("anything") and lo.mode == "log_only"


@pytest.mark.parametrize(
    "bad",
    [
        dict(when=AT_TIME),
        dict(when=AT_PERIOD),
        dict(when=AT_ENTITY),
        dict(when="sometime", at=time(1)),
        dict(at=time(1), days="days"),
        dict(at=time(1), style="eventually"),
        dict(at=time(1), mode="maybe"),
    ],
)
def test_bad_settings_are_refused(bad):
    with pytest.raises(ValueError):
        LightsOut("x", **bad)


def test_runs_today_follows_the_days():
    lo = LightsOut("a", at=time(1), days="workdays")
    friday, saturday = date(2026, 10, 9), date(2026, 10, 10)
    assert lo.runs_today(Today(friday, None, False))[0]
    assert lo.runs_today(Today(saturday, None, False)) == (False, "not a workday")
    assert not lo.runs_today(Today(friday, False, False))[0]  # a public holiday


# ---- from the review ----


def test_a_sensor_not_heard_since_a_restart_counts_as_someone_there():
    room = Room(bathroom(), default_schedule(), "Overnight", True, at(6, 0, 50))  # lit, MANUAL
    room.unheard = {MOTION, PRESENCE}
    d = room.lights_out(NIGHT, UNTIL, "Lights out")
    assert not offs(d) and room.lights_out_until == UNTIL
    # Once the wait for them is over, an empty room counts down and goes off.
    room.tick(room.unheard_until)
    d = room.tick(room.lights_out_at)
    assert offs(d) and room.state is State.IDLE


def test_off_at_once_while_waiting_in_a_room_lit_by_hand():
    room = Room(bathroom(ends=(MOTION,)), default_schedule(), "Overnight", True, at(6, 0, 50))
    room.sensor(MOTION, True, at(6, 0, 51))
    room.lights_out(NIGHT, UNTIL, "Lights out")
    d = room.sensor(MOTION, False, NIGHT + timedelta(seconds=5))
    [off] = offs(d)
    assert off.transition is None and room.state is State.IDLE


def test_the_rooms_own_switch_off_of_a_manual_room_goes_idle():
    room = Room(bathroom(), default_schedule(), "Overnight", True, at(6, 0, 50))
    room.lights(False, own=True, now=at(6, 1))
    assert room.state is State.IDLE
    d = room.sensor(MOTION, True, at(6, 2))
    assert [a for a in d.actions if isinstance(a, ApplyLook)]  # motion works again


def test_a_wait_carried_over_marks_without_switching_off():
    room = Room(bathroom(), default_schedule(), "Overnight", True, at(6, 0, 50))  # lit after a restart
    d = room.wait_lights_out(NIGHT, UNTIL, "Lights out")
    assert not offs(d) and room.lights_out_until == UNTIL
    assert "lights off in 30 s" in d.reason  # nobody seen: the countdown starts, it doesn't switch off now
    assert room.waiting_for() == {"name": "Lights out", "until": UNTIL}


def test_restore_keeps_a_lights_out_mark():
    room = Room(bathroom(), default_schedule(), "Overnight", True, at(6, 0, 50))
    room.sensor(PRESENCE, True, at(6, 0, 51))
    room.wait_lights_out(NIGHT, UNTIL, "Lights out")
    room.restore(at(6, 0, 40), False, NIGHT + timedelta(seconds=1), ambient=False, someone=True)
    assert room.lights_out_until == UNTIL
