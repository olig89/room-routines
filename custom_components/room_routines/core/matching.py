"""Telling the integration's own light changes from a person's.

Every command the integration sends is recorded with its context id and the
values it asked for. A later light change is the integration's own if:

1. its context (or parent context) is one the integration used, or
2. it reports what a recent command asked for, within a tolerance, inside a
   settle window.

Rule 2 exists for lights that report their state late and separately, as KNX
lights do: the status telegram can land after Home Assistant has stopped
associating the change with the command's context, and on context alone it
would look like a person. (Adaptive Lighting handles the same problem the same
way.) It also covers each step of a stepped fade.

A scene whose settings can't be read (one made in the Hue app, say) is
recorded as "anything" for each of the room's lights: any change to them inside
the settle window counts as the scene's doing.

Known limit: a person pressing a wall switch to the same value inside the
settle window is counted as the integration's own. Rare and harmless: the light
is simply switched off later by the room's timeout.
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


class OwnChangeMatcher:
    def __init__(
        self,
        settle: timedelta = timedelta(seconds=10),
        tolerance_pct: float = 3.0,
    ) -> None:
        self.settle = settle
        self.tolerance_pct = tolerance_pct
        self._commands: list[Command] = []

    def record(self, command: Command) -> None:
        self._commands.append(command)

    def _prune(self, now: datetime) -> None:
        self._commands = [c for c in self._commands if now - c.at <= self.settle]

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
        ids = {c.context_id for c in self._commands}
        if (context_id and context_id in ids) or (parent_id and parent_id in ids):
            return True
        for c in self._commands:
            if c.light != light:
                continue
            if c.anything:
                return True
            if c.on != on:
                continue
            if not on or c.brightness_pct is None or brightness_pct is None:
                return True
            if abs(c.brightness_pct - brightness_pct) <= self.tolerance_pct:
                return True
        return False
