"""Which period of the day it is.

A schedule is an ordered set of named periods (overnight, early morning,
morning, day, evening, ...), each starting at a local clock time. The current
period is the one whose start was most recent, looking back into yesterday so
a period that started before midnight (overnight at 23:00) still counts after
it.

Some days can use different start times (weekends, say): each period may carry
an ``alt_start`` that applies on the weekdays listed in ``alt_days``.

Times are wall-clock times in the timezone of the ``now`` passed in, so a
clock change moves nothing: evening still starts at 17:00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass(frozen=True)
class Period:
    name: str
    start: time
    alt_start: time | None = None  # used on the schedule's alt_days, if set


@dataclass(frozen=True)
class PeriodAt:
    """The current period and when it changes next."""

    name: str
    started: datetime
    next_name: str
    next_start: datetime


@dataclass(frozen=True)
class Schedule:
    periods: tuple[Period, ...]
    alt_days: frozenset[int] = frozenset()  # 0 = Monday ... 6 = Sunday

    def __post_init__(self) -> None:
        if not self.periods:
            raise ValueError("a schedule needs at least one period")
        names = [p.name for p in self.periods]
        if len(set(names)) != len(names):
            raise ValueError(f"period names must be unique: {names}")
        for alt in (False, True):
            starts = [self._start(p, alt) for p in self.periods]
            if len(set(starts)) != len(starts):
                kind = "alternative" if alt else "normal"
                raise ValueError(f"two periods share a start time on {kind} days")

    @staticmethod
    def _start(period: Period, alt: bool) -> time:
        return period.alt_start if alt and period.alt_start is not None else period.start

    def start_on(self, period: Period, day: date) -> time:
        return self._start(period, day.weekday() in self.alt_days)

    def _starts(self, day: date, tz) -> list[tuple[datetime, Period]]:
        return [
            (datetime.combine(day, self.start_on(p, day), tzinfo=tz), p)
            for p in self.periods
        ]

    def current(self, now: datetime) -> PeriodAt:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        today = now.date()
        seen = self._starts(today - timedelta(days=1), now.tzinfo) + self._starts(today, now.tzinfo)
        started, period = max((s for s in seen if s[0] <= now), key=lambda s: s[0])
        ahead = self._starts(today, now.tzinfo) + self._starts(today + timedelta(days=1), now.tzinfo)
        next_start, next_period = min((s for s in ahead if s[0] > now), key=lambda s: s[0])
        return PeriodAt(period.name, started, next_period.name, next_start)

    def with_starts(self, starts: dict[str, time] | None) -> Schedule:
        """This schedule with some periods starting at other times (a room's own
        times; the same on every day). Unknown names are ignored."""
        if not starts:
            return self
        periods = tuple(
            Period(p.name, starts[p.name], None) if p.name in starts else p for p in self.periods
        )
        return Schedule(periods, self.alt_days)

    def order(self) -> tuple[str, ...]:
        """Period names in the order they happen through a normal day."""
        return tuple(p.name for p in sorted(self.periods, key=lambda p: p.start))

    def previous(self, name: str) -> str:
        """The period before ``name`` in the daily cycle (wrapping round)."""
        order = self.order()
        return order[order.index(name) - 1]


def default_schedule() -> Schedule:
    """The five periods Room Routines starts with. All editable."""
    return Schedule(
        (
            Period("Overnight", time(23, 0)),
            Period("Early morning", time(5, 30)),
            Period("Morning", time(7, 0)),
            Period("Day", time(9, 0)),
            Period("Evening", time(17, 0)),
        )
    )
