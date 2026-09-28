from datetime import datetime, time

import pytest

from custom_components.room_routines.core.periods import Period, Schedule, default_schedule

from .conftest import TLL, at

# 2026-09-28 is a Monday.


def test_current_period_during_the_day():
    now = default_schedule().current(at(28, 12))
    assert now.name == "Day"
    assert now.started == at(28, 9)
    assert (now.next_name, now.next_start) == ("Evening", at(28, 17))


def test_overnight_started_yesterday_still_counts_after_midnight():
    now = default_schedule().current(at(29, 2))
    assert now.name == "Overnight"
    assert now.started == at(28, 23)
    assert (now.next_name, now.next_start) == ("Early morning", at(29, 5, 30))


def test_start_boundary_belongs_to_the_new_period():
    assert default_schedule().current(at(28, 17)).name == "Evening"
    assert default_schedule().current(at(28, 16, 59, 59)).name == "Day"


def test_evening_runs_until_overnight():
    now = default_schedule().current(at(28, 22, 30))
    assert now.name == "Evening"
    assert now.next_start == at(28, 23)


def test_alt_days_use_their_own_start_times():
    s = Schedule(
        (
            Period("Overnight", time(23, 0)),
            Period("Morning", time(7, 0), alt_start=time(8, 30)),
            Period("Evening", time(17, 0)),
        ),
        alt_days=frozenset({5, 6}),
    )
    assert s.current(at(26, 8)).name == "Overnight"  # Saturday: morning starts 08:30
    assert s.current(at(26, 8, 30)).name == "Morning"
    assert s.current(at(28, 8)).name == "Morning"  # Monday: normal 07:00
    # Sunday night into Monday: the next start is Monday's normal 07:00.
    assert s.current(at(27, 23, 30)).next_start == at(28, 7)


def test_clock_change_keeps_wall_clock_starts():
    # Tallinn falls back from EEST to EET on 2026-10-25.
    s = default_schedule()
    assert s.current(at(25, 17, 30, month=10)).started == at(25, 17, month=10)
    assert s.current(at(25, 12, month=10)).name == "Day"


def test_naive_time_is_refused():
    with pytest.raises(ValueError):
        default_schedule().current(datetime(2026, 9, 28, 12))


def test_order_and_previous_wrap_round_the_day():
    s = default_schedule()
    assert s.order() == ("Early morning", "Morning", "Day", "Evening", "Overnight")
    assert s.previous("Morning") == "Early morning"
    assert s.previous("Early morning") == "Overnight"


@pytest.mark.parametrize(
    "periods",
    [
        (),
        (Period("A", time(7)), Period("A", time(9))),
        (Period("A", time(7)), Period("B", time(7))),
        (Period("A", time(7), alt_start=time(9)), Period("B", time(9))),
    ],
)
def test_bad_schedules_are_refused(periods):
    alt = frozenset({6})
    with pytest.raises(ValueError):
        Schedule(periods, alt_days=alt)


def test_single_period_is_always_current():
    s = Schedule((Period("All day", time(0)),))
    now = s.current(datetime(2026, 9, 28, 15, tzinfo=TLL))
    assert now.name == now.next_name == "All day"
