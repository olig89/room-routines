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


def test_after_the_settle_window_another_level_is_a_person():
    m = matcher_with(on=True, brightness_pct=60)
    assert not m.is_own(LIGHT, at(28, 20, 0, 11), True, 30, context_id="wall")


def test_a_slow_report_of_the_asked_level_is_own():
    # Rule 4: a Bluetooth-mesh lamp reporting 40 s late, exactly as asked.
    m = matcher_with(on=True, brightness_pct=60)
    assert m.is_own(LIGHT, at(28, 20, 0, 40), True, 60.4, context_id="mesh")


def test_a_slow_report_stops_counting_after_the_late_window():
    m = matcher_with(on=True, brightness_pct=60)
    assert not m.is_own(LIGHT, at(28, 20, 2, 30), True, 60, context_id="wall")


def test_on_at_last_brightness_isnt_matched_late():
    # "On at its last brightness" has no value to match: a dim a minute later is a person.
    m = matcher_with(on=True)
    assert not m.is_own(LIGHT, at(28, 20, 1, 0), True, 20, context_id="wall")


def test_old_context_ids_dont_count_after_the_settle_window():
    m = matcher_with(on=True, brightness_pct=60)
    assert not m.is_own(LIGHT, at(28, 20, 1, 0), True, 20, context_id="ctx-1")


def test_levels_on_the_way_during_a_transition_are_own():
    # A 90 s drift from 20 % to 80 %: the light reports its progress.
    m = OwnChangeMatcher()
    m.record(Command(LIGHT, "drift", at(28, 20), True, 80,
                     transition=timedelta(seconds=90), from_pct=20))
    assert m.is_own(LIGHT, at(28, 20, 0, 30), True, 41, context_id="hue")
    assert m.is_own(LIGHT, at(28, 20, 1, 20), True, 74, context_id="hue")
    # Outside the range it was travelling is still a person.
    assert not m.is_own(LIGHT, at(28, 20, 0, 30), True, 5, context_id="wall")


def test_a_transition_down_to_off_reports_levels_on_the_way():
    m = OwnChangeMatcher()
    m.record(Command(LIGHT, "fade", at(28, 20), False,
                     transition=timedelta(seconds=15), from_pct=60))
    assert m.is_own(LIGHT, at(28, 20, 0, 12), True, 25, context_id="hue")
    assert m.is_own(LIGHT, at(28, 20, 0, 16), False, context_id="hue")


def test_a_transition_window_ends():
    m = OwnChangeMatcher()
    m.record(Command(LIGHT, "drift", at(28, 20), True, 80,
                     transition=timedelta(seconds=90), from_pct=20))
    # 100 s after: the transition and settle are over; 41 % is someone's.
    assert not m.is_own(LIGHT, at(28, 20, 1, 41), True, 41, context_id="wall")


def test_same_level_reports_are_not_changes():
    from custom_components.room_routines.core.matching import same_level

    assert same_level(True, 36.1, True, 36.5)  # a colour settling
    assert same_level(False, None, False, None)
    assert not same_level(True, 36.0, True, 50.0)
    assert not same_level(False, None, True, 36.0)
    assert not same_level(True, 36.0, False, None)


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
