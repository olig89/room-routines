from datetime import timedelta

from custom_components.room_routines.core.matching import Command, OwnChangeMatcher

from .conftest import at

LIGHT = "light.downstairs_toilet_ceiling"


def matcher_with(**cmd):
    m = OwnChangeMatcher(settle=timedelta(seconds=10), tolerance_pct=3)
    m.record(Command(LIGHT, "ctx-1", at(28, 20), **cmd))
    return m


def test_same_context_is_own():
    m = matcher_with(on=True, brightness_pct=60)
    assert m.is_own(LIGHT, at(28, 20, 0, 1), True, 10, context_id="ctx-1")


def test_parent_context_is_own():
    m = matcher_with(on=True, brightness_pct=60)
    assert m.is_own(LIGHT, at(28, 20, 0, 1), True, 10, context_id="x", parent_id="ctx-1")


def test_late_status_report_with_the_asked_value_is_own():
    # A KNX status telegram arriving with a fresh context.
    m = matcher_with(on=True, brightness_pct=60)
    assert m.is_own(LIGHT, at(28, 20, 0, 6), True, 61.2, context_id="knx-status")


def test_a_different_brightness_is_a_person():
    m = matcher_with(on=True, brightness_pct=60)
    assert not m.is_own(LIGHT, at(28, 20, 0, 6), True, 30, context_id="wall")


def test_after_the_settle_window_everything_is_a_person():
    m = matcher_with(on=True, brightness_pct=60)
    assert not m.is_own(LIGHT, at(28, 20, 0, 11), True, 60, context_id="knx-status")


def test_switch_on_at_last_brightness_accepts_any_level():
    m = matcher_with(on=True)
    assert m.is_own(LIGHT, at(28, 20, 0, 2), True, 37)


def test_off_matches_off_only():
    m = matcher_with(on=False)
    assert m.is_own(LIGHT, at(28, 20, 0, 2), False)
    assert not m.is_own(LIGHT, at(28, 20, 0, 2), True, 50)


def test_other_lights_are_not_matched_by_value():
    m = matcher_with(on=True, brightness_pct=60)
    assert not m.is_own("light.other", at(28, 20, 0, 2), True, 60)


def test_each_fade_step_is_recognised():
    m = OwnChangeMatcher()
    for i, pct in enumerate((80, 70, 60)):
        m.record(Command(LIGHT, f"step-{i}", at(28, 20, 0, 10 * i), True, pct))
    # Each step's status report lands about a second after the step.
    assert m.is_own(LIGHT, at(28, 20, 0, 11), True, 70)
    assert m.is_own(LIGHT, at(28, 20, 0, 21), True, 60)
