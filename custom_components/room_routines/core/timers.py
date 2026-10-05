"""Switching a room's lights on or off at set times.

A timer is a clock time, the days it runs on, what it does, and optional
conditions:

- days: every day, workdays (Home Assistant's Workday sensor, so public
  holidays count as days off; without one, Monday to Friday), or chosen
  weekdays;
- "on" starts the room's routine (the current look, then following it through
  the day); "off" switches the room's lights off;
- only when it's dark: a Dark Day, or the sun below the horizon;
- only if someone is home: any of the chosen people.

The integration fires each timer at its time and asks ``due`` whether it
should act today; the answer carries the reason it didn't, for the room's log.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

EVERY_DAY = "every_day"
WORKDAYS = "workdays"
CHOSEN_DAYS = "days"
DAY_KINDS = (EVERY_DAY, WORKDAYS, CHOSEN_DAYS)

ON = "on"
OFF = "off"
ACTIONS = (ON, OFF)

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class Timer:
    at: time
    action: str = ON
    days: str = EVERY_DAY
    weekdays: frozenset[int] = frozenset()  # 0 = Monday, for days == CHOSEN_DAYS
    only_dark: bool = False
    only_home: tuple[str, ...] = field(default_factory=tuple)  # person entities, any one home

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown timer action {self.action!r}")
        if self.days not in DAY_KINDS:
            raise ValueError(f"unknown days {self.days!r}")
        if self.days == CHOSEN_DAYS and not self.weekdays:
            raise ValueError("chosen days needs at least one weekday")
        if any(not 0 <= d <= 6 for d in self.weekdays):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")


@dataclass(frozen=True)
class Today:
    """What the integration knows when a timer fires."""

    day: date
    workday: bool | None  # None: no Workday sensor
    dark: bool
    home: Mapping[str, bool] = field(default_factory=dict)  # person -> at home


def due(timer: Timer, today: Today) -> tuple[bool, str]:
    """Whether the timer acts today, and if not, why."""
    if timer.days == WORKDAYS:
        workday = today.workday if today.workday is not None else today.day.weekday() < 5
        if not workday:
            return False, "not a workday"
    elif timer.days == CHOSEN_DAYS and today.day.weekday() not in timer.weekdays:
        return False, f"not on {WEEKDAY_NAMES[today.day.weekday()]}s"
    if timer.only_dark and not today.dark:
        return False, "it isn't dark"
    if timer.only_home and not any(today.home.get(p, False) for p in timer.only_home):
        return False, "nobody chosen is home"
    return True, ""


def describe(timer: Timer) -> str:
    when = timer.at.strftime("%H:%M")
    if timer.days == WORKDAYS:
        days = " on workdays"
    elif timer.days == CHOSEN_DAYS:
        days = " on " + ", ".join(WEEKDAY_NAMES[d][:3] for d in sorted(timer.weekdays))
    else:
        days = ""
    return f"{'on' if timer.action == ON else 'off'} at {when}{days}"


def timer_from(data: Mapping[str, Any]) -> Timer:
    return Timer(
        at=time.fromisoformat(str(data["at"])[:5]),
        action=str(data.get("action", ON)),
        days=str(data.get("days", EVERY_DAY)),
        weekdays=frozenset(int(d) for d in data.get("weekdays") or ()),
        only_dark=bool(data.get("only_dark", False)),
        only_home=tuple(str(p) for p in data.get("only_home") or ()),
    )


def timer_to(timer: Timer) -> dict[str, Any]:
    out: dict[str, Any] = {"at": timer.at.strftime("%H:%M"), "action": timer.action, "days": timer.days}
    if timer.days == CHOSEN_DAYS:
        out["weekdays"] = sorted(timer.weekdays)
    if timer.only_dark:
        out["only_dark"] = True
    if timer.only_home:
        out["only_home"] = list(timer.only_home)
    return out
