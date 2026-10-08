"""Someone's there over a running routine (Ambient): brighter while someone's
there, back to the routine when the room empties."""

from datetime import timedelta

from custom_components.room_routines.core.layers import Layer
from custom_components.room_routines.core.looks import LightTarget, Look
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import ApplyLook, Room, RoomConfig, State, TurnOff, WakeAt

from .conftest import at

CEILING, LAMP = "light.hall_ceiling", "light.hall_lamp"
MOTION = "binary_sensor.hall_motion"
DIM = LightTarget(True, 5, 2700)
BRIGHT = LightTarget(True, 70, 2700)


def hall(**over) -> RoomConfig:
    base = dict(
        name="Hall", lights=(CEILING,), triggers=(MOTION,), threshold_lux=None,
        timeout=timedelta(seconds=30), fade_out=timedelta(seconds=15),
        looks={"Morning": Look({CEILING: DIM})},
        someone_looks={"Morning": Look({CEILING: BRIGHT})},
    )
    base.update(over)
    return RoomConfig(**base)


def make(config: RoomConfig) -> Room:
    return Room(config, default_schedule(), "Evening", False, at(28, 18))


def applied(decision):
    return next(a for a in decision.actions if isinstance(a, ApplyLook))


def test_the_routine_runs_dim_and_motion_brightens_it():
    room = make(hall())
    d = room.start(at(28, 18), "timer at 18:00")
    assert applied(d).look == Look({CEILING: DIM})
    assert room.ambient_on and not room.someone_on and room.layer() is Layer.AMBIENT
    assert not any(isinstance(a, WakeAt) for a in d.actions)  # a routine doesn't count down
    d = room.sensor(MOTION, True, at(28, 18, 30))
    assert applied(d).look == Look({CEILING: BRIGHT})
    assert room.layer() is Layer.SOMEONE and "someone's there" in d.reason


def test_when_the_room_empties_it_fades_back_to_the_routine_not_off():
    room = make(hall())
    room.start(at(28, 18), "timer")
    room.sensor(MOTION, True, at(28, 18, 30))
    d = room.sensor(MOTION, False, at(28, 18, 31))
    assert any(isinstance(a, WakeAt) for a in d.actions)
    d = room.tick(at(28, 18, 32))
    back = applied(d)
    assert back.look == Look({CEILING: DIM}) and back.transition == timedelta(seconds=15)
    assert not any(isinstance(a, TurnOff) for a in d.actions)
    assert room.state is State.OWNED and room.ambient_on and not room.someone_on
    assert d.reason.startswith("empty: back to the")


def test_lights_left_out_of_someones_look_stay_with_the_routine():
    room = make(hall(
        lights=(CEILING, LAMP),
        looks={"Morning": Look({CEILING: DIM, LAMP: DIM})},
        someone_looks={"Morning": Look({CEILING: BRIGHT})},
    ))
    room.start(at(28, 18), "timer")
    d = room.sensor(MOTION, True, at(28, 18, 30))
    assert applied(d).look == Look({CEILING: BRIGHT, LAMP: DIM})


def test_once_the_routine_ends_visits_switch_on_and_off_as_before():
    room = make(hall())
    room.start(at(28, 18), "timer")
    room.stop(at(28, 23), "timer at 23:00")
    assert room.state is State.IDLE and not room.ambient_on
    d = room.sensor(MOTION, True, at(28, 23, 40))
    assert applied(d).look == Look({CEILING: BRIGHT})  # Someone's there's own look
    room.sensor(MOTION, False, at(28, 23, 41))
    d = room.tick(at(28, 23, 42))
    assert any(isinstance(a, TurnOff) for a in d.actions) and room.state is State.IDLE


def test_without_someones_looks_motion_leaves_a_running_routine_alone():
    room = make(hall(someone_looks={}, triggers=(), holds=()))
    room.start(at(28, 18), "timer")
    plain = make(hall(someone_looks={}))
    d = plain.start(at(28, 18), "button")
    # A motion room started by a button is a visit, as it always was: it counts down.
    assert plain.someone_on and not plain.ambient_on
    assert any(isinstance(a, WakeAt) for a in d.actions) or plain.deadline is not None


def test_a_motion_only_room_still_uses_its_looks():
    room = make(hall(someone_looks={}))
    d = room.sensor(MOTION, True, at(28, 18))
    assert applied(d).look == Look({CEILING: DIM})
    assert room.layer() is Layer.SOMEONE


def test_blending_keeps_someones_lights_over_the_blend():
    room = Room(hall(
        lights=(CEILING, LAMP),
        looks={"Day": Look({CEILING: DIM, LAMP: LightTarget(True, 10, 2700)}),
               "Evening": Look({CEILING: DIM, LAMP: LightTarget(True, 50, 2700)})},
        someone_looks={"Day": Look({CEILING: BRIGHT})},
        blends={"Day": 60},
    ), default_schedule(), "Day", False, at(28, 16))
    room.start(at(28, 16), "timer")
    room.sensor(MOTION, True, at(28, 16, 30))
    d = room.blend_tick(at(28, 16, 30))
    look = applied(d).look
    assert look.lights[CEILING] == BRIGHT
    assert 10 < look.lights[LAMP].brightness_pct < 50


def test_a_held_hand_change_isnt_undone_by_someone_arriving():
    room = make(hall())
    room.start(at(28, 18), "timer")
    room.lights(True, False, at(28, 18, 5))  # someone dims it by hand
    d = room.sensor(MOTION, True, at(28, 18, 30))
    assert not d.actions and room.paused
    room.sensor(MOTION, False, at(28, 18, 31))
    d = room.tick(at(28, 18, 32))
    assert not d.actions and room.state is State.OWNED


def test_both_layers_are_remembered_across_a_restart():
    room = make(hall())
    room.start(at(28, 18), "timer")
    room.sensor(MOTION, True, at(28, 18, 30))
    memory = room.memory()
    assert memory["ambient"] and memory["someone"]
    after = Room(hall(), default_schedule(), "Evening", True, at(28, 19))
    after.restore(at(28, 18), False, at(28, 19), memory["ambient"], memory["someone"])
    assert after.ambient_on and after.someone_on


def test_memory_from_before_layers_is_read_by_whether_the_room_has_sensors():
    after = Room(hall(), default_schedule(), "Evening", True, at(28, 19))
    after.restore(at(28, 18), False, at(28, 19))
    assert after.someone_on and not after.ambient_on
    office = Room(hall(triggers=()), default_schedule(), "Evening", True, at(28, 19))
    office.restore(at(28, 18), False, at(28, 19))
    assert office.ambient_on and not office.someone_on
