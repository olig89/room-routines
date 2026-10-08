"""Layers: the most important layer that wants a light decides it."""

from datetime import timedelta

from custom_components.room_routines.core.layers import Claim, Layer, combine, owners
from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig, State

from .conftest import at

IRIS, PLAY = "light.iris", "light.play"
DARK = LightTarget(True, 5, 2200)
PURPLE = LightTarget(True, 100)
MOTION = "binary_sensor.motion"


def test_the_most_important_claim_wins_light_by_light():
    ambient = Claim(Layer.AMBIENT, {IRIS: DARK, PLAY: DARK}, "Evening look")
    signal = Claim(Layer.SIGNAL, {IRIS: PURPLE}, "In a call")
    got = owners([IRIS, PLAY], [ambient, signal])
    assert got[IRIS] is signal and got[PLAY] is ambient
    assert combine([IRIS, PLAY], [ambient, signal]) == {IRIS: PURPLE, PLAY: DARK}


def test_a_light_nothing_wants_is_off_and_ties_keep_their_order():
    first = Claim(Layer.SIGNAL, {IRIS: PURPLE}, "first")
    second = Claim(Layer.SIGNAL, {IRIS: DARK}, "second")
    assert owners([IRIS], [first, second])[IRIS] is first
    assert combine([IRIS, PLAY], [first]) == {IRIS: PURPLE, PLAY: LightTarget(False)}


def office(**over) -> RoomConfig:
    base = dict(name="Office", lights=(PLAY,), triggers=(), threshold_lux=None,
                looks={"Morning": Look({PLAY: DARK})})
    base.update(over)
    return RoomConfig(**base)


def test_a_room_says_which_layer_has_its_lights():
    room = Room(office(), default_schedule(), "Day", False, at(28, 10))
    assert room.layer() is None
    room.start(at(28, 10), "timer at 10:00")
    assert room.layer() is Layer.AMBIENT
    room.lights(True, False, at(28, 10, 5))  # someone turns it up
    assert room.layer() is Layer.HAND
    hall = Room(office(triggers=(MOTION,)), default_schedule(), "Day", False, at(28, 10))
    hall.sensor(MOTION, True, at(28, 10))
    assert hall.layer() is Layer.SOMEONE


def test_a_running_routine_is_remembered_and_picked_up_after_a_restart():
    room = Room(office(), default_schedule(), "Day", False, at(28, 10))
    room.start(at(28, 10), "timer at 10:00")
    memory = room.memory()
    assert memory == {"owned_at": at(28, 10).isoformat(), "paused": False}
    # The restart: the lights are on, so a new room starts in manual.
    after = Room(office(), default_schedule(), "Day", True, at(28, 11))
    assert after.state is State.MANUAL
    d = after.restore(at(28, 10), False, at(28, 11))
    assert after.state is State.OWNED and after.owned_at == at(28, 10)
    assert any(isinstance(a, ApplyLook) for a in d.actions)
    assert d.reason.startswith("picked up again after a restart")


def test_a_held_hand_change_stays_held_after_a_restart():
    after = Room(office(), default_schedule(), "Day", True, at(28, 11))
    d = after.restore(at(28, 10), True, at(28, 11))
    assert after.paused and after.layer() is Layer.HAND
    assert not d.actions


def test_only_a_room_found_lit_is_picked_up():
    idle = Room(office(), default_schedule(), "Day", False, at(28, 11))
    assert not idle.restore(at(28, 10), False, at(28, 11)).actions
    assert idle.state is State.IDLE and idle.memory() is None


def test_a_motion_room_picked_up_counts_down_when_empty():
    after = Room(office(triggers=(MOTION,), timeout=timedelta(seconds=30)), default_schedule(), "Day", True, at(28, 11))
    d = after.restore(at(28, 10), False, at(28, 11))
    assert after.deadline == at(28, 11) + timedelta(seconds=30)
    assert "lights off in 30 s" in d.reason


def test_a_room_with_no_look_anywhere_says_so():
    empty = Room(office(triggers=(MOTION,), looks={}), default_schedule(), "Day", False, at(28, 10))
    assert not empty.has_any_look()
    d = empty.sensor(MOTION, True, at(28, 10))
    assert not d.actions and "has no look to switch on in any period" in d.reason
    assert Room(office(), default_schedule(), "Day", False, at(28, 10)).has_any_look()


def test_a_picked_up_motion_room_waits_for_its_sensors_before_counting_down():
    after = Room(office(triggers=(MOTION,), timeout=timedelta(seconds=30)), default_schedule(), "Day", True, at(28, 11))
    after.unheard.add(MOTION)
    d = after.restore(at(28, 10), False, at(28, 11))
    assert after.deadline is None and "waiting for the sensors" in d.reason
    d = after.sensor(MOTION, False, at(28, 11, 1))  # it reports: nobody there
    assert after.deadline == at(28, 11, 1) + timedelta(seconds=30)


def test_a_room_with_only_dark_day_looks_has_a_look():
    room = Room(office(looks={}, dim_looks={"Day": Look({PLAY: DARK})}), default_schedule(), "Day", False, at(28, 10))
    assert room.has_any_look()
