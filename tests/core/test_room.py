"""The room's behaviour, told as real rooms it will run."""

from datetime import timedelta

import pytest

from custom_components.room_routines.core.looks import NOTHING, LightTarget, Look, uniform
from custom_components.room_routines.core.lux import Scaling
from custom_components.room_routines.core.periods import default_schedule
from custom_components.room_routines.core.room import (
    ApplyLook,
    MoveBlinds,
    Room,
    RoomConfig,
    State,
    TurnOff,
    WakeAt,
)

from .conftest import at

FADE = timedelta(seconds=15)

CEILING = "light.downstairs_toilet_ceiling"
MOTION = "binary_sensor.downstairs_toilet_motion"
MMWAVE = "binary_sensor.downstairs_toilet_presence"

# The copy-today look: plain switch-on in every period, below 50 lux.
TOILET = RoomConfig(
    "Downstairs Toilet",
    lights=(CEILING,),
    triggers=(MOTION,),
    holds=(MMWAVE,),
    looks={"Day": uniform((CEILING,))},
)


def room(config=TOILET, period="Evening", lights_on=False, now=None):
    return Room(config, default_schedule(), period, lights_on, now or at(28, 20))


def only(decision, kind):
    found = [a for a in decision.actions if isinstance(a, kind)]
    assert len(found) == 1, decision
    return found[0]


def test_motion_in_the_dark_switches_on_and_owns():
    r = room()
    d = r.sensor(MOTION, True, at(28, 20))
    assert only(d, ApplyLook).look == uniform((CEILING,))
    assert r.state is State.OWNED
    assert "Evening look" in d.reason


def test_leaving_counts_down_then_switches_off():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    d = r.sensor(MOTION, False, at(28, 20, 1))
    assert only(d, WakeAt).at == at(28, 20, 1, 30)
    assert r.tick(at(28, 20, 1, 29)).actions == ()  # too early: nothing to do
    off = r.tick(at(28, 20, 1, 30))
    assert only(off, TurnOff).lights == (CEILING,)
    assert r.state is State.IDLE


def test_motion_again_cancels_the_countdown():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    r.sensor(MOTION, False, at(28, 20, 1))
    d = r.sensor(MOTION, True, at(28, 20, 1, 10))
    assert "cancelled" in d.reason
    assert r.tick(at(28, 20, 1, 30)).actions == ()
    assert r.state is State.OWNED


def test_mmwave_holds_an_owned_room_but_never_switches_on():
    r = room()
    assert r.sensor(MMWAVE, True, at(28, 20)).actions == ()
    assert r.state is State.IDLE
    r.sensor(MOTION, True, at(28, 20, 1))
    d = r.sensor(MOTION, False, at(28, 20, 2))  # PIR clears, mmWave still sees someone
    assert d.actions == ()
    assert r.occupied()
    r.sensor(MMWAVE, False, at(28, 20, 10))
    assert r.tick(at(28, 20, 10, 30)).actions == (TurnOff((CEILING,), FADE),)


def test_too_bright_does_nothing():
    r = room(now=at(28, 12))
    r.lux(120, at(28, 13))
    d = r.sensor(MOTION, True, at(28, 13, 1))
    assert d.actions == ()
    assert "too bright (120 lux" in d.reason


def test_unknown_lux_counts_as_dark():
    r = room()
    assert r.sensor(MOTION, True, at(28, 13)).actions


def test_do_nothing_look():
    cfg = RoomConfig("Hall", (CEILING,), (MOTION,), looks={"Evening": uniform((CEILING,)), "Overnight": NOTHING})
    r = room(cfg, period="Overnight")
    d = r.sensor(MOTION, True, at(29, 2))
    assert d.actions == ()
    assert "do nothing" in d.reason


def test_light_switched_on_by_hand_is_left_alone():
    r = room()
    r.lights(True, own=False, now=at(28, 20))
    assert r.state is State.MANUAL
    assert r.sensor(MOTION, True, at(28, 20, 1)).actions == ()
    r.sensor(MOTION, False, at(28, 20, 2))
    assert r.tick(at(28, 21)).actions == ()  # never switched off


def test_switched_off_by_hand_ignores_motion_for_the_cooldown():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    d = r.lights(False, own=False, now=at(28, 20, 5))
    assert r.state is State.IDLE and "ignored for 30 s" in d.reason
    r.sensor(MOTION, False, at(28, 20, 5, 1))
    assert r.sensor(MOTION, True, at(28, 20, 5, 20)).actions == ()
    r.sensor(MOTION, False, at(28, 20, 5, 25))
    assert r.sensor(MOTION, True, at(28, 20, 5, 31)).actions


def test_dimmed_by_hand_still_switches_off():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    r.lights(True, own=False, now=at(28, 20, 1))
    assert r.state is State.OWNED
    r.sensor(MOTION, False, at(28, 20, 2))
    assert r.tick(at(28, 20, 2, 30)).actions == (TurnOff((CEILING,), FADE),)


def test_own_light_reports_change_nothing():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    before = r.last
    d = r.lights(True, own=True, now=at(28, 20, 1))
    assert d.actions == () and d.reason == before.reason
    assert r.state is State.OWNED


def test_restart_with_lights_on_is_manual():
    r = room(lights_on=True)
    assert r.state is State.MANUAL
    assert r.sensor(MOTION, True, at(28, 20)).actions == ()
    r.lights(False, own=False, now=at(28, 20, 1))
    assert r.state is State.IDLE


def test_period_change_while_lit_drifts_to_the_new_look():
    evening = Look({CEILING: LightTarget(True, 80, 2700)})
    overnight = Look({CEILING: LightTarget(True, 10, 2200)})
    cfg = RoomConfig("Toilet", (CEILING,), (MOTION,), looks={"Evening": evening, "Overnight": overnight})
    r = room(cfg)
    r.sensor(MOTION, True, at(28, 22, 59))
    d = r.period_changed("Overnight", at(28, 23))
    apply = only(d, ApplyLook)
    assert apply.look == overnight
    assert apply.transition == timedelta(seconds=90)


def test_period_change_while_idle_or_manual_changes_no_lights():
    assert room().period_changed("Overnight", at(28, 23)).actions == ()
    assert room(lights_on=True).period_changed("Overnight", at(28, 23)).actions == ()


def test_blinds_move_only_with_periods_and_only_if_asked():
    look = Look({CEILING: LightTarget(True, 60)}, blinds={"cover.living_room": 0})
    cfg = RoomConfig("Living", (CEILING,), (MOTION,), looks={"Evening": look})
    r = room(cfg, period="Day")
    motion = r.sensor(MOTION, True, at(28, 16, 50))
    assert not any(isinstance(a, MoveBlinds) for a in motion.actions)
    assert only(motion, ApplyLook).look.blinds == {}
    assert not any(isinstance(a, MoveBlinds) for a in r.period_changed("Evening", at(28, 17)).actions)
    cfg_on = RoomConfig("Living", (CEILING,), (MOTION,), looks={"Evening": look}, blinds_with_periods=True)
    idle = room(cfg_on, period="Day")
    assert only(idle.period_changed("Evening", at(28, 17)), MoveBlinds).positions == {"cover.living_room": 0}


def test_walking_back_in_during_the_fade_out_brings_the_lights_back():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    r.sensor(MOTION, False, at(28, 20, 1))
    assert only(r.tick(at(28, 20, 1, 30)), TurnOff).transition == FADE
    d = r.sensor(MOTION, True, at(28, 20, 1, 35))  # 5 s into the 15 s fade
    assert only(d, ApplyLook).look == uniform((CEILING,))


def test_fade_out_length_is_the_room_setting():
    cfg = RoomConfig("Toilet", (CEILING,), (MOTION,), looks={"Day": uniform((CEILING,))}, fade_out=timedelta(0))
    r = room(cfg)
    r.sensor(MOTION, True, at(28, 20))
    r.sensor(MOTION, False, at(28, 20, 1))
    assert only(r.tick(at(28, 20, 2)), TurnOff).transition is None  # 0 = straight off


def test_power_circuit_goes_on_first_and_never_off():
    bulbs = ("light.stair_left", "light.stair_right")
    power = "light.stairs_hanging_lamp_power"
    cfg = RoomConfig(
        "Stairs",
        lights=(*bulbs, power, "light.stairs_handrail"),
        triggers=(MOTION,),
        looks={"Evening": uniform((*bulbs, "light.stairs_handrail"))},
        powered_by={b: power for b in bulbs},
    )
    r = room(cfg)
    d = r.sensor(MOTION, True, at(28, 20))
    assert only(d, ApplyLook).power_first == (power,)
    r.sensor(MOTION, False, at(28, 20, 1))
    off = only(r.tick(at(28, 20, 2)), TurnOff)
    assert power not in off.lights
    assert set(off.lights) == {*bulbs, "light.stairs_handrail"}


def test_lux_scaling_dims_the_look():
    cfg = RoomConfig(
        "Toilet",
        (CEILING,),
        (MOTION,),
        looks={"Evening": Look({CEILING: LightTarget(True, 100)})},
        scaling=Scaling(full_dark=5, threshold=50, min_factor=0.3),
    )
    r = room(cfg, now=at(28, 18))
    r.lux(27.5, at(28, 19))
    d = r.sensor(MOTION, True, at(28, 19, 1))
    assert only(d, ApplyLook).look.lights[CEILING].brightness_pct == 65.0


def test_two_trigger_sensors_share_one_room():
    # Stairs: sensors at the bottom and the top both wake the handrail.
    top, bottom = "binary_sensor.landing_stairs_motion", "binary_sensor.hallway_stairs_motion"
    cfg = RoomConfig("Stairs", ("light.stairs_handrail",), (bottom, top), looks={"Day": uniform(("light.stairs_handrail",))})
    r = room(cfg)
    r.sensor(top, True, at(28, 20))
    r.sensor(bottom, True, at(28, 20, 0, 5))
    r.sensor(top, False, at(28, 20, 0, 10))
    assert r.occupied()
    d = r.sensor(bottom, False, at(28, 20, 0, 20))
    assert only(d, WakeAt).at == at(28, 20, 0, 50)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lights": (), "triggers": (MOTION,)},
        {"lights": (CEILING,), "triggers": (MOTION,), "holds": (MOTION,)},
    ],
)
def test_bad_room_config_refused(kwargs):
    with pytest.raises(ValueError):
        RoomConfig("Bad", **kwargs)


# ---- stealth mode ----------------------------------------------------------
# Stealth makes every motion sensor read "nobody here". Everything else is normal.


def test_stealth_motion_switches_nothing_on():
    r = room()
    r.set_stealth(True, at(28, 23))
    d = r.sensor(MOTION, True, at(28, 23, 1))
    assert d.actions == ()
    assert "stealth" in d.reason
    assert r.state is State.IDLE


def test_stealth_lets_a_lit_room_go_dark_as_normal():
    r = room()
    r.sensor(MOTION, True, at(28, 20))  # someone is in, lights on
    d = r.set_stealth(True, at(28, 20, 1))
    assert only(d, WakeAt).at == at(28, 20, 1, 30)  # reads as empty straight away
    r.sensor(MOTION, True, at(28, 20, 1, 10))  # still moving: ignored
    assert r.tick(at(28, 20, 1, 30)).actions == (TurnOff((CEILING,), FADE),)


def test_stealth_neuters_hold_sensors_too():
    r = room()
    r.sensor(MOTION, True, at(28, 20))
    r.sensor(MMWAVE, True, at(28, 20, 0, 5))
    r.set_stealth(True, at(28, 20, 1))
    assert not r.occupied()


def test_stealth_off_counts_sensors_again():
    r = room()
    r.set_stealth(True, at(28, 20))
    r.sensor(MOTION, True, at(28, 20, 1))
    d = r.set_stealth(False, at(28, 20, 2))  # someone is still there
    assert only(d, ApplyLook).look == uniform((CEILING,))
    assert r.state is State.OWNED


def test_stealth_off_with_nobody_there_does_nothing():
    r = room()
    r.set_stealth(True, at(28, 20))
    assert r.set_stealth(False, at(28, 20, 2)).actions == ()
    assert r.state is State.IDLE


def test_stealth_leaves_periods_and_blinds_alone():
    look = Look({CEILING: LightTarget(True, 60)}, blinds={"cover.living_room": 0})
    cfg = RoomConfig("Living", (CEILING,), (MOTION,), looks={"Evening": look}, blinds_with_periods=True)
    r = room(cfg, period="Day")
    r.sensor(MOTION, True, at(28, 16, 50))
    r.set_stealth(True, at(28, 16, 55))
    d = r.period_changed("Evening", at(28, 17))
    assert only(d, ApplyLook).transition == timedelta(seconds=90)
    assert only(d, MoveBlinds).positions == {"cover.living_room": 0}


def test_manual_changes_are_still_tracked_in_stealth():
    r = room()
    r.set_stealth(True, at(28, 20))
    r.lights(True, own=False, now=at(28, 20, 1))
    assert r.state is State.MANUAL
    r.lights(False, own=False, now=at(28, 20, 2))
    assert r.state is State.IDLE
