from datetime import timedelta

import pytest

from custom_components.room_routines.core.lux import AmbientTracker, Scaling, dark_enough

from .conftest import at


def test_readings_while_the_lights_are_on_are_ignored():
    t = AmbientTracker()
    assert t.reading(8, at(28, 20))
    t.lights_changed(True, at(28, 20, 1))
    assert not t.reading(31, at(28, 20, 2))  # the lamp, not the room
    assert t.ambient(at(28, 20, 2)) == 8


def test_readings_just_after_switch_off_are_ignored():
    t = AmbientTracker()
    t.lights_changed(True, at(28, 20))
    t.lights_changed(False, at(28, 20, 5))
    assert not t.reading(25, at(28, 20, 5, 5))
    assert t.reading(9, at(28, 20, 5, 15))
    assert t.ambient(at(28, 20, 6)) == 9


def test_a_repeated_off_report_does_not_restart_the_settle_window():
    t = AmbientTracker()
    t.lights_changed(False, at(28, 20))
    t.lights_changed(False, at(28, 20, 0, 8))
    assert t.reading(9, at(28, 20, 0, 11))


def test_stale_ambient_counts_as_unknown():
    t = AmbientTracker(stale_after=timedelta(minutes=30))
    t.reading(8, at(28, 20))
    assert t.ambient(at(28, 20, 30)) == 8
    assert t.ambient(at(28, 20, 31)) is None


@pytest.mark.parametrize(
    "ambient, threshold, expected",
    [(8, 50, True), (50, 50, False), (120, 50, False), (None, 50, True), (500, None, True)],
)
def test_dark_enough(ambient, threshold, expected):
    assert dark_enough(ambient, threshold) is expected


def test_scaling_eases_from_full_to_minimum():
    s = Scaling(full_dark=5, threshold=50, min_factor=0.3)
    assert s.factor(None) == 1.0
    assert s.factor(2) == 1.0
    assert s.factor(50) == 0.3
    assert s.factor(200) == 0.3
    assert s.factor(27.5) == pytest.approx(0.65)


@pytest.mark.parametrize("kwargs", [{"full_dark": 50, "threshold": 50}, {"min_factor": 0}])
def test_bad_scaling_refused(kwargs):
    with pytest.raises(ValueError):
        Scaling(**kwargs)
