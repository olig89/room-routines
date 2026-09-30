"""Where the sun is, and what a clear day looks like to a light sensor."""

from datetime import datetime

import pytest

from custom_components.room_routines.core.daylight import DaylightReference, bucket
from custom_components.room_routines.core.sun import clear_sky, position

from .conftest import at

TALLINN = (59.44, 24.75)


def test_sun_height_in_tallinn_at_the_end_of_september():
    noon = position(*TALLINN, at(30, 13, 11))  # solar noon, about 13:11 local
    assert noon.elevation == pytest.approx(27.7, abs=0.6)
    assert position(*TALLINN, at(30, 10)).morning
    assert not position(*TALLINN, at(30, 15)).morning
    assert position(*TALLINN, at(30, 23)).elevation < -20


def test_needs_a_timezone():
    with pytest.raises(ValueError):
        position(*TALLINN, datetime(2026, 9, 30, 12))


def test_clear_sky():
    assert clear_sky(-3) == 0
    assert clear_sky(27.7) == pytest.approx(452, abs=3)
    assert clear_sky(60) > clear_sky(30) > clear_sky(10)


def test_reference_learns_a_high_percentile_and_fades():
    ref = DaylightReference()
    # One sun height, 20 readings: 18 grey at 1000 lx, 2 bright at 9000.
    samples = [(20.0, True, 1000.0)] * 18 + [(20.0, True, 9000.0)] * 2
    ref.learn(samples, days=0)
    assert ref.lookup(20.5, True) == 9000.0
    assert ref.lookup(20.5, False) is None  # afternoons are learned separately
    assert ref.lookup(1.0, True) is None  # sun too low
    ref.learn([], days=10)  # ten days, nothing new: fades
    assert ref.lookup(20.5, True) == pytest.approx(9000 * 0.97**10, rel=0.01)
    ref.learn([(20.0, True, 8000.0)] * 6, days=1)  # a new bright day lifts it back
    assert ref.lookup(20.5, True) == 8000.0


def test_reference_needs_enough_readings_and_round_trips():
    ref = DaylightReference()
    ref.learn([(20.0, True, 5000.0)] * 5, days=0)
    assert ref.learned == 0
    ref.learn([(20.0, True, 5000.0)] * 6, days=0)
    assert DaylightReference.from_dict(ref.to_dict()).lookup(21, True) == 5000.0
    assert bucket(21.9, True) == "am10"
