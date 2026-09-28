import pytest

from custom_components.room_routines.core.looks import (
    NOTHING,
    OFF,
    ON,
    LightTarget,
    Look,
    look_for,
    scaled,
    uniform,
)
from custom_components.room_routines.core.periods import default_schedule

EVENING = Look({"light.ceiling": LightTarget(True, 60, 2700), "light.mirror": OFF})
DAY = Look({"light.ceiling": LightTarget(True, 100, 4000)})


def test_period_with_its_own_look():
    looks = {"Evening": EVENING, "Day": DAY}
    assert look_for("Evening", looks, default_schedule()) is EVENING


def test_missing_look_borrows_the_previous_period():
    looks = {"Day": DAY, "Evening": EVENING}
    # Overnight has no look: it follows Evening.
    assert look_for("Overnight", looks, default_schedule()) is EVENING
    # Early morning wraps back past Overnight to Evening too.
    assert look_for("Early morning", looks, default_schedule()) is EVENING


def test_do_nothing_look_is_honoured_not_skipped():
    looks = {"Evening": EVENING, "Overnight": NOTHING}
    assert look_for("Overnight", looks, default_schedule()).nothing
    assert look_for("Early morning", looks, default_schedule()).nothing


def test_no_looks_at_all_means_do_nothing():
    assert look_for("Day", {}, default_schedule()).nothing


def test_scaling_multiplies_set_brightness_only():
    look = Look({"light.a": LightTarget(True, 80), "light.b": ON, "light.c": OFF})
    half = scaled(look, 0.5)
    assert half.lights["light.a"].brightness_pct == 40
    assert half.lights["light.b"] == ON  # last brightness: nothing to scale
    assert half.lights["light.c"] == OFF


def test_scaling_never_goes_below_one_percent_or_above_100():
    look = Look({"light.a": LightTarget(True, 2), "light.b": LightTarget(True, 90)})
    assert scaled(look, 0.1).lights["light.a"].brightness_pct == 1.0
    assert scaled(look, 2).lights["light.b"].brightness_pct == 100.0


def test_scaling_keeps_colour_and_blinds():
    look = Look({"light.a": LightTarget(True, 80, 2700)}, blinds={"cover.x": 20})
    s = scaled(look, 0.5)
    assert s.lights["light.a"].color_temp_kelvin == 2700
    assert s.blinds == {"cover.x": 20}


def test_uniform_copies_todays_plain_switch_on():
    look = uniform(("light.a", "light.b"))
    assert look.lights == {"light.a": ON, "light.b": ON}
    assert look.lit() == ["light.a", "light.b"]


@pytest.mark.parametrize("kwargs", [{"brightness_pct": 0}, {"brightness_pct": 101}])
def test_bad_brightness_refused(kwargs):
    with pytest.raises(ValueError):
        LightTarget(True, **kwargs)


def test_colour_temperature_and_colour_together_refused():
    with pytest.raises(ValueError):
        LightTarget(True, 50, 2700, (255, 0, 0))
