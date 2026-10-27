"""Changes by hand: only the touched lights are left as set, for as long as the
room says; a sensor that ends a visit the moment it goes off."""

from datetime import time, timedelta

import pytest

from custom_components.room_routines.core.layers import Layer
from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import (
    BLENDS,
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


# ---- held lights that blend into the next period ------------------------------
# Day borrows Morning's FOCUS; blending over the last hour, 16:00-17:00.

DINNER = LightTarget(True, 80, 3000)


def kitchen(**over) -> RoomConfig:
    return office(**{"hand_hold": BLENDS, "blends": {"Day": 60}, **over})


def held_look(decision):
    """The command sent to the lights set by hand (the last one in a blend step)."""
    return applied(decision)[-1].look.lights


def test_a_held_light_waits_then_blends_from_how_it_was_set_into_the_next_look():
    room = started(kitchen())
    d = room.lights(True, own=False, now=at(5, 11), light=A, state=DINNER)
    assert "until the next period, blending into it" in d.reason
    assert room.blend_tick(at(5, 15, 30)).actions == ()  # the window isn't open yet
    d = room.blend_tick(at(5, 16, 30))
    routine, held = applied(d)
    assert set(routine.look.lights) == {B}  # the routine blends its own light
    assert held.look.lights[A].brightness_pct == pytest.approx(42.5)  # half way from 80 to 5
    assert held.look.lights[A].color_temp_kelvin == 2538  # half way in mireds from 3000 to 2200
    assert held.transition == timedelta(minutes=2)
    assert "1 light set by hand blending into Evening (50 %)" in d.reason
    assert room.blend_tick(at(5, 16, 30)).actions == ()  # nothing new to send
    d = room.period_changed("Evening", at(5, 17))
    assert room.hand == {} and applied(d)[0].look == DARK  # let go at the period start


def test_a_hand_set_scene_with_a_light_on_dims_and_goes_off_when_the_next_look_has_it_off():
    evening = Look({A: LightTarget(False), B: LightTarget(True, 2, 2200)})
    room = started(kitchen(looks={"Morning": FOCUS, "Evening": evening}))
    room.lights(True, own=False, now=at(5, 11), light=A, state=DINNER)
    room.lights(True, own=False, now=at(5, 11), light=B, state=LightTarget(True, 60, 2700))
    assert room.paused  # the whole scene is held, as when it's picked by hand
    d = room.blend_tick(at(5, 16, 30))
    [held] = applied(d)  # paused: only the held lights move
    assert held.look.lights[A] == LightTarget(True, 40.5, 3000)  # towards the minimum, colour kept
    assert held.look.lights[B] == LightTarget(True, 31.0, 2424)
    d = room.blend_tick(at(5, 16, 59))
    assert held_look(d)[A].brightness_pct == pytest.approx(2.3)
    d = room.period_changed("Evening", at(5, 17))
    [look] = applied(d)
    assert look.look.lights[A] == LightTarget(False) and look.transition == timedelta(seconds=90)


def test_a_light_set_during_the_window_starts_from_there_without_jumping():
    room = started(kitchen())
    room.lights(True, own=False, now=at(5, 16, 30), light=A, state=DINNER)
    assert held_look(room.blend_tick(at(5, 16, 30)))[A] == DINNER
    d = room.blend_tick(at(5, 16, 45))  # half of what's left of the window
    assert held_look(d)[A].brightness_pct == pytest.approx(42.5)


def test_the_latest_report_of_a_hand_change_is_the_one_blended_from():
    room = started(kitchen())
    room.lights(True, own=False, now=at(5, 11), light=A, state=LightTarget(True, 30, 3000))
    room.lights(True, own=False, now=at(5, 11, 0, 2), light=A, state=DINNER)  # the scene's transition settles
    assert held_look(room.blend_tick(at(5, 16, 30)))[A].brightness_pct == pytest.approx(42.5)


@pytest.mark.parametrize("state", [
    LightTarget(True, 80, rgb=(255, 120, 0)),  # a colour, and the next look has a white
    LightTarget(True),  # brightness unknown
    LightTarget(False),  # off, the next look has it on
    None,  # not reported
])
def test_a_held_light_that_cant_blend_waits_for_the_period_start(state):
    room = started(kitchen())
    room.lights(True, own=False, now=at(5, 11), light=A, state=state)
    d = room.blend_tick(at(5, 16, 30))
    assert all(A not in a.look.lights for a in applied(d))
    d = room.period_changed("Evening", at(5, 17))
    assert room.hand == {} and applied(d)[0].look == DARK


def test_without_a_blend_for_the_period_it_holds_until_the_routine_moves_on():
    room = started(kitchen(blends={}))
    room.lights(True, own=False, now=at(5, 11), light=A, state=DINNER)
    assert room.blend_tick(at(5, 16, 30)).actions == ()
    d = room.period_changed("Evening", at(5, 17))
    assert room.hand == {} and applied(d)[0].look == DARK


def test_a_rule_starting_lets_go_mid_blend():
    from custom_components.room_routines.core.rules import CAP, Condition, Rule

    room = started(kitchen())
    room.lights(True, own=False, now=at(5, 11), light=A, state=DINNER)
    room.blend_tick(at(5, 16, 30))
    room.set_context(Rule(Condition("binary_sensor.tv"), CAP, max_pct=30), "while the TV is on", "", at(5, 16, 40))
    assert room.hand == {}
    assert len(applied(room.blend_tick(at(5, 16, 44)))) == 1  # just the routine now


def test_a_signal_keeps_the_blend_off_its_light():
    from custom_components.room_routines.core.rules import Condition
    from custom_components.room_routines.core.signals import Signal

    call = Signal("Call", Condition("binary_sensor.call"), {A: LightTarget(True, 100, rgb=(102, 0, 255))})
    room = started(kitchen(signals=(call,)))
    room.lights(True, own=False, now=at(5, 11), light=A, state=DINNER)
    room.set_signals({"binary_sensor.call": "on"}, {A: DINNER}, at(5, 16, 20))
    d = room.blend_tick(at(5, 16, 30))
    assert all(A not in a.look.lights for a in applied(d))


def test_how_a_held_light_was_set_survives_a_restart():
    room = started(kitchen())
    room.lights(True, own=False, now=at(5, 11), light=A, state=DINNER)
    memory = room.memory()
    assert memory["hand_set"][A]["at"] == at(5, 11).isoformat()
    after = Room(kitchen(), default_schedule(), "Day", True, at(5, 16))
    after.restore(at(5, 10), False, at(5, 16), True, False, {A: None}, {A: (DINNER, at(5, 11))})
    assert held_look(after.blend_tick(at(5, 16, 30)))[A].brightness_pct == pytest.approx(42.5)


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


# ---- from the review --------------------------------------------------------


def test_a_restart_asks_to_be_woken_for_a_hold_by_minutes():
    after = Room(office(hand_hold=FOR_MINUTES), default_schedule(), "Day", True, at(5, 11, 5))
    d = after.restore(at(5, 10), False, at(5, 11, 5), True, False, {A: at(5, 11, 20)})
    assert WakeAt(at(5, 11, 20)) in d.actions


def test_a_restart_doesnt_let_go_of_holds_until_the_routine_moves_on():
    after = Room(office(hand_hold=MOVES_ON), default_schedule(), "Day", True, at(5, 11, 5))
    d = after.restore(at(5, 10), False, at(5, 11, 5), True, False, {A: None})
    assert after.hand == {A: None}
    assert all(A not in a.look.lights for a in applied(d))


def test_old_paused_memory_in_a_minutes_room_counts_from_the_restart():
    after = Room(office(hand_hold=FOR_MINUTES, hand_minutes=20), default_schedule(), "Day", True, at(5, 11))
    after.restore(at(5, 10), True, at(5, 11))
    assert after.hand == {A: at(5, 11, 20), B: at(5, 11, 20)}


def test_a_held_light_someone_lit_goes_off_when_its_hold_ends():
    C = "light.extra"
    hall = office(
        lights=(A, C), triggers=(MOTION,), threshold_lux=None, timers=(Timer(time(10, 0)),),
        hand_hold=FOR_MINUTES, hand_minutes=30,
        looks={"Morning": Look({A: LightTarget(True, 5)})},
        someone_looks={"Day": Look({A: LightTarget(True, 70), C: LightTarget(True, 70)})},
    )
    room = started(hall)
    room.sensor(MOTION, True, at(5, 10, 1))
    room.lights(True, own=False, now=at(5, 10, 2), light=C)
    room.sensor(MOTION, False, at(5, 10, 3))
    d = room.tick(at(5, 10, 4))
    assert not any(isinstance(a, TurnOff) and C in a.lights for a in d.actions)  # held: left on
    d = room.tick(at(5, 10, 32))
    assert any(isinstance(a, TurnOff) and C in a.lights for a in d.actions)


def test_an_unreadable_scene_doesnt_override_a_held_light():
    room = Room(office(looks={"Morning": FOCUS, "Evening": Look(scene="scene.hue_evening")}),
                default_schedule(), "Day", False, at(5, 10), scene_reader=lambda s: None)
    room.start(at(5, 10), "button")
    room.lights(True, own=False, now=at(5, 11), light=A)
    d = room.period_changed("Evening", at(5, 18))
    assert not applied(d)


def test_a_door_closing_switches_off_at_once_in_stealth_mode_too():
    room = Room(pantry(), default_schedule(), "Day", False, at(5, 10))
    room.sensor(DOOR, True, at(5, 10))
    room.set_stealth(True, at(5, 10, 0, 5))
    d = room.sensor(DOOR, False, at(5, 10, 0, 10))
    assert room.state is State.IDLE and any(isinstance(a, TurnOff) for a in d.actions)
