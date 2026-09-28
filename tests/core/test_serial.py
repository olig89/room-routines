from datetime import time, timedelta

from custom_components.room_routines.core.looks import NOTHING, LightTarget, Look, look_for
from custom_components.room_routines.core.periods import Period, Schedule, default_schedule
from custom_components.room_routines.core.serial import (
    first_look,
    look_from,
    look_to,
    room_from,
    schedule_from,
    schedule_to,
)


def test_schedule_round_trip():
    s = Schedule(
        (Period("Night", time(23)), Period("Morning", time(7), time(8, 30))),
        frozenset({5, 6}),
    )
    assert schedule_from(schedule_to(s)) == s


def test_missing_periods_mean_the_defaults():
    assert schedule_from({}) == default_schedule()


def test_look_round_trip():
    look = Look(
        {
            "light.a": LightTarget(True, 60, 2700),
            "light.b": LightTarget(True),
            "light.c": LightTarget(False),
            "light.d": LightTarget(True, 30, rgb=(255, 120, 0)),
        },
        {"cover.living_room": 20},
    )
    assert look_from(look_to(look)) == look
    assert look_to(look)["lights"]["light.b"] == {"on": True}


def test_do_nothing_round_trip():
    assert look_from(look_to(NOTHING)) == NOTHING


def test_room_from_stored_settings():
    room = room_from(
        {
            "name": "Downstairs Toilet",
            "lights": ["light.downstairs_toilet_ceiling"],
            "triggers": ["binary_sensor.downstairs_toilet_motion"],
            "timeout_s": 45,
            "threshold_lux": None,
            "looks": {"Evening": {"lights": {"light.downstairs_toilet_ceiling": {"on": True}}}},
        }
    )
    assert room.timeout == timedelta(seconds=45)
    assert room.cooldown == timedelta(seconds=30)
    assert room.threshold_lux is None
    assert room.looks["Evening"].lights["light.downstairs_toilet_ceiling"] == LightTarget(True)


def test_first_look_covers_every_period_through_the_fallback():
    s = default_schedule()
    looks = {p: look_from(l) for p, l in first_look(["light.a", "light.b"], s).items()}
    for period in s.order():
        assert look_for(period, looks, s).lights == {"light.a": LightTarget(True), "light.b": LightTarget(True)}
