"""Telling the integration's own light changes from a person's.

Every command the integration sends is recorded with its context id and the
values it asked for. A later light change is the integration's own if:

1. its context (or parent context) is one the integration used, or
2. it reports what a recent command asked for, within a tolerance, inside a
   settle window, or
3. it is part of a transition the integration started: while a command's
   transition runs (plus the settle window), any brightness between where the
   light started and where it was asked to go counts, or
4. it reports, a little late, exactly what a command asked for: every command
   stays matchable by its value for ``late`` (only an explicit level or off;
   "on at its last brightness" isn't a value to match, and context ids only
   count inside the settle window).

Rule 2 exists for lights that report their state late and separately, as KNX
lights do: the status telegram can land after Home Assistant has stopped
associating the change with the command's context, and on context alone it
would look like a person. (Adaptive Lighting handles the same problem the same
way.) It also covers each step of a stepped fade.

Rule 3 covers lights that report their progress during a transition (a drift
to a new period's look is sent with a 90 s transition): each report is a level
on the way, not the one asked for.

Rule 4 covers slow reporters (a Bluetooth-mesh lamp behind a template light,
a Zigbee light that reports after a poll): the value they settle on is the one
asked for, just later than the settle window.

A scene whose settings can't be read (one made in the Hue app, say) is
recorded as "anything" for each of the room's lights: any change to them inside
the settle window (and its transition) counts as the scene's doing.

Known limit: a person setting a light to the very value the integration last
asked for, shortly afterwards, is counted as the integration's own. Rare and
harmless: the light is where the routine wanted it anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Command:
    light: str
    context_id: str
    at: datetime
    on: bool
    brightness_pct: float | None = None  # None while on = any brightness is fine
    anything: bool = False  # any state is fine (a scene we can't read)
    transition: timedelta = timedelta(0)
    from_pct: float | None = None  # the light's brightness when sent (None: off or unknown)


class OwnChangeMatcher:
    def __init__(
        self,
        settle: timedelta = timedelta(seconds=10),
        tolerance_pct: float = 3.0,
        late: timedelta = timedelta(minutes=2),
    ) -> None:
        self.settle = settle
        self.tolerance_pct = tolerance_pct
        self.late = late
        self._commands: list[Command] = []

    def record(self, command: Command) -> None:
        self._commands.append(command)

    def _prune(self, now: datetime) -> None:
        horizon = max(self.late, self.settle)
        self._commands = [c for c in self._commands if now - c.at <= horizon + c.transition]

    def _value_matches(self, c: Command, on: bool, brightness_pct: float | None, in_transition: bool) -> bool:
        if c.anything:
            return True
        if not in_transition and c.on != on:
            return False
        if not on:
            # Off reported: the command was off (or the light hasn't started yet).
            return not c.on or (in_transition and c.from_pct is None)
        if c.brightness_pct is None or brightness_pct is None:
            return True
        if abs(c.brightness_pct - brightness_pct) <= self.tolerance_pct:
            return True
        if in_transition:
            start = c.from_pct if c.from_pct is not None else 0.0
            target = c.brightness_pct if c.on else 0.0
            low, high = min(start, target), max(start, target)
            return low - self.tolerance_pct <= brightness_pct <= high + self.tolerance_pct
        return False

    def is_own(
        self,
        light: str,
        at: datetime,
        on: bool,
        brightness_pct: float | None = None,
        context_id: str | None = None,
        parent_id: str | None = None,
    ) -> bool:
        self._prune(at)
        recent = [c for c in self._commands if at - c.at <= self.settle + c.transition]
        ids = {c.context_id for c in recent}
        if (context_id and context_id in ids) or (parent_id and parent_id in ids):
            return True
        for c in self._commands:
            if c.light != light:
                continue
            if c in recent:
                if self._value_matches(c, on, brightness_pct, c.transition > timedelta(0)):
                    return True
                continue
            # Rule 4: late, but exactly what was asked for (an explicit level, or off).
            if c.anything or c.on != on:
                continue
            if not on:
                return True
            if c.brightness_pct is not None and brightness_pct is not None:
                if abs(c.brightness_pct - brightness_pct) <= self.tolerance_pct:
                    return True
        return False


def same_level(
    old_on: bool, old_pct: float | None, new_on: bool, new_pct: float | None, tolerance_pct: float = 3.0
) -> bool:
    """Whether a state report leaves the light where it was: same on/off and, while
    on, the same brightness (within ``tolerance_pct``). Such a report (a colour
    settling, an attribute refresh, a template light re-rendering) never counts
    as a change by hand."""
    if old_on != new_on:
        return False
    if not new_on:
        return True
    if old_pct is None or new_pct is None:
        return old_pct is None and new_pct is None
    return abs(old_pct - new_pct) <= tolerance_pct
