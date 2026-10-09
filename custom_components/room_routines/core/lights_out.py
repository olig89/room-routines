"""Lights out: switching the house's lights off at the end of the day.

A Lights out runs at a time, when a period starts, or when an entity reaches a
state (a "going to bed" switch), on every day, on workdays or on chosen days.
It covers the whole house or chosen rooms, in one of two ways:

- **off once empty** (the default): rooms where nobody is detected go dark now.
  A room where a sensor sees someone is marked instead, and goes fully off the
  next time it is empty, after its own timeout and with its own fade (it doesn't
  fall back to its routine's look). The mark lasts until the next period
  starts; after it, the room carries on as usual.
- **all off now**: everything goes off now, whoever is there.

Rooms without sensors, and lights that are in no room, go off at once either
way. Lights an Inform holds and power circuits are never touched. Once a room
has gone off, motion works in it as usual.

Each Lights out has its own mode (off / log only / live), log only by default,
so it can run beside an automation it replaces until it's trusted. A house-wide
"skip the next Lights out" switch skips one run and switches itself off.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import Any

from .timers import CHOSEN_DAYS, DAY_KINDS, EVERY_DAY, OFF as TIMER_OFF, WEEKDAY_NAMES, WORKDAYS, Timer, Today, due

ONCE_EMPTY = "once_empty"
NOW = "now"
STYLES = (ONCE_EMPTY, NOW)

AT_TIME = "time"
AT_PERIOD = "period"
AT_ENTITY = "entity"
KINDS = (AT_TIME, AT_PERIOD, AT_ENTITY)

MODE_OFF = "off"
MODE_LOG_ONLY = "log_only"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_LOG_ONLY, MODE_LIVE)


@dataclass(frozen=True)
class LightsOut:
    id: str
    name: str = "Lights out"
    when: str = AT_TIME
    at: time | None = None  # for AT_TIME
    period: str | None = None  # for AT_PERIOD: when this period starts
    entity: str | None = None  # for AT_ENTITY: when it reaches ``state``
    state: str = "on"
    days: str = EVERY_DAY
    weekdays: frozenset[int] = frozenset()  # 0 = Monday, for days == CHOSEN_DAYS
    rooms: tuple[str, ...] = ()  # none: the whole house, lights in no room included
    style: str = ONCE_EMPTY
    mode: str = MODE_LOG_ONLY

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("a Lights out needs an id")
        if self.when not in KINDS:
            raise ValueError(f"unknown when {self.when!r}")
        if self.when == AT_TIME and self.at is None:
            raise ValueError("a Lights out at a time needs the time")
        if self.when == AT_PERIOD and not self.period:
            raise ValueError("a Lights out at a period's start needs the period")
        if self.when == AT_ENTITY and (not self.entity or not self.state):
            raise ValueError("a Lights out on an entity needs the entity and its state")
        if self.days not in DAY_KINDS:
            raise ValueError(f"unknown days {self.days!r}")
        if self.days == CHOSEN_DAYS and not self.weekdays:
            raise ValueError("chosen days needs at least one weekday")
        if any(not 0 <= d <= 6 for d in self.weekdays):
            raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        if self.style not in STYLES:
            raise ValueError(f"unknown style {self.style!r}")
        if self.mode not in MODES:
            raise ValueError(f"unknown mode {self.mode!r}")

    @property
    def whole_house(self) -> bool:
        return not self.rooms

    @property
    def now_style(self) -> bool:
        return self.style == NOW

    def covers(self, room_id: str) -> bool:
        return self.whole_house or room_id in self.rooms

    def runs_today(self, today: Today) -> tuple[bool, str]:
        """Whether it runs today (the days), and if not, why."""
        return due(Timer(time(0), TIMER_OFF, self.days, self.weekdays), today)


def describe(lo: LightsOut, entity_name: str | None = None) -> str:
    if lo.when == AT_TIME and lo.at is not None:
        what = f"at {lo.at.strftime('%H:%M')}"
    elif lo.when == AT_PERIOD:
        what = f"when {lo.period} starts"
    else:
        what = f"when {entity_name or lo.entity} is {lo.state}"
    if lo.days == WORKDAYS:
        what += " on workdays"
    elif lo.days == CHOSEN_DAYS:
        what += " on " + ", ".join(WEEKDAY_NAMES[d][:3] for d in sorted(lo.weekdays))
    return what


def lights_out_from(data: Mapping[str, Any]) -> LightsOut:
    at = data.get("at")
    return LightsOut(
        id=str(data["id"]),
        name=str(data.get("name") or "Lights out").strip() or "Lights out",
        when=str(data.get("when", AT_TIME)),
        at=time.fromisoformat(str(at)[:5]) if at else None,
        period=str(data["period"]) if data.get("period") else None,
        entity=str(data["entity"]).strip() if data.get("entity") else None,
        state=str(data.get("state") or "on").strip(),
        days=str(data.get("days", EVERY_DAY)),
        weekdays=frozenset(int(d) for d in data.get("weekdays") or ()),
        rooms=tuple(str(r) for r in data.get("rooms") or ()),
        style=str(data.get("style", ONCE_EMPTY)),
        mode=str(data.get("mode", MODE_LOG_ONLY)),
    )


def lights_out_to(lo: LightsOut) -> dict[str, Any]:
    out: dict[str, Any] = {"id": lo.id, "name": lo.name, "when": lo.when}
    if lo.when == AT_TIME and lo.at is not None:
        out["at"] = lo.at.strftime("%H:%M")
    elif lo.when == AT_PERIOD:
        out["period"] = lo.period
    else:
        out["entity"] = lo.entity
        out["state"] = lo.state
    out["days"] = lo.days
    if lo.days == CHOSEN_DAYS:
        out["weekdays"] = sorted(lo.weekdays)
    if lo.rooms:
        out["rooms"] = list(lo.rooms)
    out["style"] = lo.style
    out["mode"] = lo.mode
    return out
