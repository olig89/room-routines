"""Changes by hand: only the touched lights are left as set, for as long as the
room says; a sensor that ends a visit the moment it goes off."""

from datetime import time, timedelta

import pytest

from custom_components.room_routines.core.layers import Layer
from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import (
    FOR_MINUTES,
    MOVES_ON,
    ApplyLook,
    Room,
    RoomConfig,
    State,
    TurnOff,
    WakeAt,
)
from custom_components.room_routines.core.timers import Timer

from .conftest import at

A, B = "light.play", "light.points"
FOCUS = Look({A: LightTarget(True, 36, 5000), B: LightTarget(True, 18, 4900)})
DARK = Look({A: LightTarget(True, 5, 2200), B: LightTarget(True, 2, 2200)})
DOOR, MOTION = "binary_sensor.pantry_door", "binary_sensor.pantry_motion"


def office(**over) -> RoomConfig:
    base = dict(name="Office", lights=(A, B), triggers=(), looks={"Morning": FOCUS, "Evening": DARK})
    base.update(over)
    return RoomConfig(**base)


def started(config: RoomConfig, now=None) -> Room:
    room = Room(config, default_schedule(), "Day", False, now or at(5, 10))
    room.start(now or at(5, 10), "button")
    return room


def applied(decision):
    return [a for a in decision.actions if isinstance(a, ApplyLook)]


def test_only_the_touched_light_is_held_the_others_follow_the_day():
    room = started(office())
    d = room.lights(True, own=False, now=at(5, 11), light=A)
    assert room.hand == {A: None} and not room.paused
    assert "light.play changed by hand" in d.reason and "the other lights carry on" in d.reason
    assert room.layer() is Layer.AMBIENT  # the routine still has the room
    d = room.period_changed("Evening", at(5, 18))
    [look] = applied(d)
    assert set(look.look.lights) == {B}  # A is left as set
    assert "1 light left as changed by hand" in d.reason


def test_until_off_holds_until_the_lights_go_off():
    room = started(office())
    room.lights(True, own=False, now=at(5, 11), light=A)
    room.period_changed("Evening", at(5, 18))
    assert room.hand == {A: None}
    room.lights(False, own=False, now=at(5, 19), light=A)
    assert room.state is State.IDLE and room.hand == {}


def test_every_light_touched_pauses_the_room_as_before():
    room = started(office())
    room.lights(True, own=False, now=at(5, 11), light=A)
    room.lights(True, own=False, now=at(5, 11), light=B)
    assert room.paused and room.layer() is Layer.HAND
    d = room.period_changed("Evening", at(5, 18))
    assert not applied(d) and "left as changed by hand" in d.reason


def test_moves_on_lets_go_at_the_next_period():
    room = started(office(hand_hold=MOVES_ON))
    d = room.lights(True, own=False, now=at(5, 11), light=A)
    assert "until the routine moves on" in d.reason
    d = room.period_changed("Evening", at(5, 18))
    [look] = applied(d)
    assert look.look == DARK and room.hand == {}  # both lights to the new look
    assert "changes by hand let go" in d.reason


def test_moves_on_lets_go_on_a_dark_day_change_too():
    room = started(office(hand_hold=MOVES_ON, dim_looks={"Morning": DARK}))
    room.lights(True, own=False, now=at(5, 11), light=A)
    d = room.track_changed("dim", at(5, 12))
    assert room.hand == {} and set(applied(d)[0].look.lights) == {A, B}


def test_until_off_ignores_the_routine_moving_on():
    room = started(office())
    room.lights(True, own=False, now=at(5, 11), light=A)
    room.track_changed("dim", at(5, 12))
    assert room.hand == {A: None}


def test_for_minutes_goes_back_to_the_routine_on_time():
    room = started(office(hand_hold=FOR_MINUTES, hand_minutes=20))
    d = room.lights(True, own=False, now=at(5, 11), light=A)
    [wake] = d.actions
    assert isinstance(wake, WakeAt) and wake.at == at(5, 11, 20)
    assert room.next_wake() == at(5, 11, 20)
    assert "for 20 min" in d.reason
    assert room.tick(at(5, 11, 10)).actions == ()  # not yet
    d = room.tick(at(5, 11, 20))
    [back] = applied(d)
    assert back.look == Look({A: FOCUS.lights[A]}) and back.transition == timedelta(seconds=90)
    assert room.hand == {} and d.reason.startswith("change by hand over: back to the")


def test_for_minutes_ignores_the_routine_moving_on():
    room = started(office(hand_hold=FOR_MINUTES))
    room.lights(True, own=False, now=at(5, 11), light=A)
    room.period_changed("Evening", at(5, 18))
    assert A in room.hand


def test_a_touch_again_restarts_the_minutes():
    room = started(office(hand_hold=FOR_MINUTES, hand_minutes=20))
    room.lights(True, own=False, now=at(5, 11), light=A)
    room.lights(True, own=False, now=at(5, 11, 15), light=A)
    assert room.hand[A] == at(5, 11, 35)
    assert room.tick(at(5, 11, 20)).actions == ()


def test_an_unknown_light_still_holds_the_whole_room():
    room = started(office())
    room.lights(True, own=False, now=at(5, 11))
    assert room.paused


def test_a_hand_held_light_isnt_switched_off_when_someone_leaves():
    hall = office(
        triggers=(MOTION,), threshold_lux=None, timers=(Timer(time(10, 0)),),
        looks={"Morning": Look({A: LightTarget(True, 5)})},
        someone_looks={"Day": Look({A: LightTarget(True, 70), B: LightTarget(True, 70)})},
    )
    room = started(hall)
    room.sensor(MOTION, True, at(5, 10, 1))
    room.lights(True, own=False, now=at(5, 10, 2), light=B)
    room.sensor(MOTION, False, at(5, 10, 3))
    d = room.tick(at(5, 10, 4))
    assert not any(isinstance(a, TurnOff) and B in a.lights for a in d.actions)
    assert all(B not in a.look.lights for a in applied(d))


def test_moves_on_lets_go_when_the_room_empties():
    hall = office(
        triggers=(MOTION,), threshold_lux=None, timers=(Timer(time(10, 0)),), hand_hold=MOVES_ON,
        looks={"Morning": Look({A: LightTarget(True, 5), B: LightTarget(True, 5)})},
        someone_looks={"Day": Look({A: LightTarget(True, 70)})},
    )
    room = started(hall)
    room.sensor(MOTION, True, at(5, 10, 1))
    room.lights(True, own=False, now=at(5, 10, 2), light=B)
    room.sensor(MOTION, False, at(5, 10, 3))
    d = room.tick(at(5, 10, 4))
    assert room.hand == {}
    assert B in applied(d)[0].look.lights


def test_hand_holds_survive_a_restart():
    room = started(office(hand_hold=FOR_MINUTES, hand_minutes=20))
    room.lights(True, own=False, now=at(5, 11), light=A)
    memory = room.memory()
    assert memory["hand"] == {A: at(5, 11, 20).isoformat()}
    after = Room(office(hand_hold=FOR_MINUTES), default_schedule(), "Day", True, at(5, 11, 5))
    d = after.restore(at(5, 10), False, at(5, 11, 5), True, False, {A: at(5, 11, 20)})
    assert after.hand == {A: at(5, 11, 20)}
    assert set(applied(d)[0].look.lights) == {B}
    # One whose time ran out during the restart goes back to the routine.
    late = Room(office(hand_hold=FOR_MINUTES), default_schedule(), "Day", True, at(5, 11, 30))
    late.restore(at(5, 10), False, at(5, 11, 30), True, False, {A: at(5, 11, 20)})
    assert late.hand == {}


def test_a_signal_released_from_a_hand_held_light_goes_back_to_how_it_was():
    from custom_components.room_routines.core.rules import Condition
    from custom_components.room_routines.core.signals import Signal

    call = Signal("Call", Condition("binary_sensor.call"), {A: LightTarget(True, 100, rgb=(102, 0, 255))})
    room = started(office(signals=(call,)))
    room.lights(True, own=False, now=at(5, 11), light=A)
    set_by_hand = LightTarget(True, 80, 3000)
    room.set_signals({"binary_sensor.call": "on"}, {A: set_by_hand}, at(5, 11, 1))
    d = room.set_signals({"binary_sensor.call": "off"}, {}, at(5, 11, 2))
    [back] = applied(d)
    assert back.look.lights[A] == set_by_hand  # not the routine's look: it's held by hand


# ---- a sensor that ends a visit -----------------------------------------------


def pantry(**over) -> RoomConfig:
    base = dict(
        name="Pantry", lights=(A,), triggers=(DOOR,), holds=(MOTION,), ends=(DOOR,),
        threshold_lux=None, timeout=timedelta(seconds=5), fade_out=timedelta(seconds=5),
        looks={"Morning": Look({A: LightTarget(True)})},
    )
    base.update(over)
    return RoomConfig(**base)


def test_closing_the_door_switches_off_at_once_whatever_the_motion_says():
    room = Room(pantry(), default_schedule(), "Day", False, at(5, 10))
    room.sensor(DOOR, True, at(5, 10))
    room.sensor(MOTION, True, at(5, 10, 0, 2))
    d = room.sensor(DOOR, False, at(5, 10, 0, 20))
    [off] = d.actions
    assert isinstance(off, TurnOff) and off.transition is None
    assert room.state is State.IDLE and "lights off at once" in d.reason


def test_without_ends_the_door_closing_waits_for_the_motion():
    room = Room(pantry(ends=()), default_schedule(), "Day", False, at(5, 10))
    room.sensor(DOOR, True, at(5, 10))
    room.sensor(MOTION, True, at(5, 10, 0, 2))
    d = room.sensor(DOOR, False, at(5, 10, 0, 20))
    assert room.state is State.OWNED and not any(isinstance(a, TurnOff) for a in d.actions)


def test_only_the_rooms_own_sensors_can_end_a_visit():
    with pytest.raises(ValueError):
        pantry(ends=("binary_sensor.somewhere_else",))
