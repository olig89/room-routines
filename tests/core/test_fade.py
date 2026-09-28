from datetime import timedelta

from custom_components.room_routines.core.fade import plan_fade
from custom_components.room_routines.core.looks import OFF, ON, LightTarget, Look

S = timedelta(seconds=1)


def by_light(plan, light):
    return [(off, sends[light]) for off, sends in plan if light in sends]


def test_linear_steps_to_the_new_brightness():
    plan = plan_fade({"light.a": 100}, Look({"light.a": LightTarget(True, 40)}), 90 * S, 30 * S)
    assert [(o, t.brightness_pct) for o, t in by_light(plan, "light.a")] == [
        (30 * S, 80.0),
        (60 * S, 60.0),
        (90 * S, 40.0),
    ]


def test_colour_is_set_on_the_last_step_only():
    plan = plan_fade({"light.a": 100}, Look({"light.a": LightTarget(True, 40, 2200)}), 90 * S, 30 * S)
    steps = by_light(plan, "light.a")
    assert [t.color_temp_kelvin for _, t in steps] == [None, None, 2200]


def test_switching_off_fades_down_then_off():
    plan = plan_fade({"light.a": 91}, Look({"light.a": OFF}), 90 * S, 30 * S)
    steps = by_light(plan, "light.a")
    assert [t.brightness_pct for _, t in steps[:-1]] == [61.0, 31.0]
    assert steps[-1] == (90 * S, OFF)


def test_switching_on_starts_from_the_minimum():
    plan = plan_fade({"light.a": None}, Look({"light.a": LightTarget(True, 31)}), 90 * S, 30 * S)
    assert [t.brightness_pct for _, t in by_light(plan, "light.a")] == [11.0, 21.0, 31.0]


def test_last_brightness_light_switches_on_at_once_or_is_left_alone():
    off_now = plan_fade({"light.a": None}, Look({"light.a": ON}), 90 * S, 30 * S)
    assert by_light(off_now, "light.a") == [(30 * S, ON)]
    on_now = plan_fade({"light.a": 70}, Look({"light.a": ON}), 90 * S, 30 * S)
    assert on_now == []


def test_off_light_that_stays_off_and_unlisted_lights_are_untouched():
    plan = plan_fade(
        {"light.a": None, "light.b": 50}, Look({"light.a": OFF}), 90 * S, 30 * S
    )
    assert plan == []


def test_default_step_gives_a_short_fade_five_steps():
    plan = plan_fade({"light.a": 100}, Look({"light.a": OFF}), 15 * S)
    assert [o.total_seconds() for o, _ in plan] == [3, 6, 9, 12, 15]
    assert plan[-1][1]["light.a"] == OFF


def test_duration_shorter_than_a_step_is_one_step():
    plan = plan_fade({"light.a": 100}, Look({"light.a": LightTarget(True, 50)}), 5 * S, 10 * S)
    assert by_light(plan, "light.a") == [(5 * S, LightTarget(True, 50.0))]
