"""Signals: chosen lights show something while it's true, above everything else."""

from datetime import timedelta

from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig, State, TurnOff
from custom_components.room_routines.core.rules import Condition
from custom_components.room_routines.core.serial import room_from
from custom_components.room_routines.core.signals import Signal, signal_from, signal_to

from .conftest import at

PLAY, IRIS = "light.play", "light.iris"
CALL, MUTED = "binary_sensor.in_call", "binary_sensor.muted"
WHITE = LightTarget(True, 36, 5000)
PURPLE = LightTarget(True, 100, None, (102, 0, 255))
GREEN = LightTarget(True, 100, None, (0, 255, 0))
SIGNALS = (
    Signal("Muted", Condition(MUTED), {IRIS: GREEN}),
    Signal("In a call", Condition(CALL), {IRIS: PURPLE}, flash=True),
)


def office(**over) -> RoomConfig:
    base = dict(name="Office", lights=(PLAY, IRIS), triggers=(), threshold_lux=None,
                looks={"Morning": Look({PLAY: WHITE, IRIS: WHITE})}, signals=SIGNALS)
    base.update(over)
    return RoomConfig(**base)


def make(config=None, lights_on=False) -> Room:
    return Room(config or office(), default_schedule(), "Day", lights_on, at(28, 10))


def looks_sent(decision):
    return [a for a in decision.actions if isinstance(a, ApplyLook)]


def test_a_call_takes_the_iris_and_the_routine_leaves_it_alone():
    room = make()
    room.start(at(28, 10), "timer")
    d = room.set_signals({CALL: "on"}, {IRIS: WHITE}, at(28, 10, 5))
    (sent,) = looks_sent(d)
    assert sent.signal and sent.flash and sent.look == Look({IRIS: PURPLE})
    assert d.reason == "signal: In a call"
    d = room.period_changed("Evening", at(28, 17))  # the routine moves on: not the Iris
    assert all(IRIS not in a.look.lights for a in looks_sent(d))
    assert any(PLAY in a.look.lights for a in looks_sent(d))


def test_muted_wins_over_in_a_call_and_takes_over_without_a_flash():
    room = make()
    room.set_signals({CALL: "on"}, {IRIS: LightTarget(False)}, at(28, 10))
    d = room.set_signals({CALL: "on", MUTED: "on"}, {IRIS: PURPLE}, at(28, 10, 1))
    (sent,) = looks_sent(d)
    assert sent.look == Look({IRIS: GREEN}) and not sent.flash
    assert room.before == {IRIS: LightTarget(False)}  # still how it was before the call


def test_after_the_call_the_iris_rejoins_the_routine_where_it_has_got_to():
    room = make()
    room.start(at(28, 10), "timer")
    room.set_signals({CALL: "on"}, {IRIS: WHITE}, at(28, 10, 5))
    d = room.set_signals({CALL: "off"}, {IRIS: PURPLE}, at(28, 11))
    (back,) = looks_sent(d)
    assert back.look == Look({IRIS: WHITE}) and d.reason == "signal over: In a call"
    assert not room.held and not room.before


def test_with_the_room_off_the_iris_goes_back_to_how_it_was():
    room = make()
    room.set_signals({CALL: "on"}, {IRIS: LightTarget(False)}, at(28, 10))
    assert room.state is State.IDLE
    d = room.set_signals({CALL: "off"}, {IRIS: PURPLE}, at(28, 11))
    assert looks_sent(d)[0].look == Look({IRIS: LightTarget(False)})


def test_a_scene_look_is_sent_without_the_held_light():
    reader = {"scene.working": {PLAY: WHITE, IRIS: WHITE}}
    room = Room(office(looks={"Morning": Look(scene="scene.working")}), default_schedule(), "Day", False,
                at(28, 10), scene_reader=reader.get)
    room.set_signals({CALL: "on"}, {}, at(28, 10))
    d = room.start(at(28, 10, 1), "timer")
    (sent,) = looks_sent(d)
    assert sent.look == Look({PLAY: WHITE}) and not sent.look.scene


def test_a_scene_that_cant_be_read_is_followed_by_the_signal_again():
    room = Room(office(looks={"Morning": Look(scene="scene.hue_app")}), default_schedule(), "Day", False,
                at(28, 10), scene_reader=lambda s: None)
    room.set_signals({CALL: "on"}, {}, at(28, 10))
    d = room.start(at(28, 10, 1), "timer")
    first, second = looks_sent(d)
    assert first.look.scene == "scene.hue_app"
    assert second.signal and second.look == Look({IRIS: PURPLE}) and not second.flash


def test_switching_off_when_empty_leaves_the_signal_light_on():
    motion = "binary_sensor.motion"
    room = make(office(triggers=(motion,), timeout=timedelta(seconds=30)))
    room.sensor(motion, True, at(28, 10))
    room.set_signals({CALL: "on"}, {IRIS: WHITE}, at(28, 10, 1))
    room.sensor(motion, False, at(28, 10, 2))
    d = room.tick(at(28, 10, 33))
    (off,) = [a for a in d.actions if isinstance(a, TurnOff)]
    assert off.lights == (PLAY,)


def test_the_light_before_a_signal_is_remembered_for_a_restart():
    room = make()
    room.set_signals({CALL: "on"}, {IRIS: LightTarget(True, 20, 2700)}, at(28, 10))
    assert room.memory() == {"signal_before": {IRIS: {"on": True, "brightness_pct": 20.0, "color_temp_kelvin": 2700}}}


def test_signals_are_stored_and_read_back():
    data = signal_to(SIGNALS[1])
    assert signal_from(data) == SIGNALS[1]
    config = room_from({"name": "Office", "lights": [PLAY, IRIS], "triggers": [], "signals": [data]})
    assert config.signals == (SIGNALS[1],)
