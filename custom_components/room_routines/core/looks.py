"""What a room's lights should do in a period.

A look lists, per light, whether it is on and how. ``brightness_pct=None`` on
a light that is on means "at its last brightness": a plain switch-on, which is
what a KNX motion sensor does today and what the first looks copy.

A look can also be "do nothing" (``NOTHING``): the room stays dark in that
period. A period with no look of its own borrows the previous period's.

Blinds can be listed in a look, but they are never moved by motion; the room
only moves them when a new period starts, and only if the room asks for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from .periods import Schedule

MIN_BRIGHTNESS_PCT = 1.0


@dataclass(frozen=True)
class LightTarget:
    on: bool
    brightness_pct: float | None = None  # None while on = last brightness
    color_temp_kelvin: int | None = None
    rgb: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.brightness_pct is not None and not 0 < self.brightness_pct <= 100:
            raise ValueError(f"brightness_pct must be in (0, 100], got {self.brightness_pct}")
        if self.color_temp_kelvin is not None and self.rgb is not None:
            raise ValueError("give a colour temperature or a colour, not both")


ON = LightTarget(True)
OFF = LightTarget(False)


@dataclass(frozen=True)
class Look:
    lights: Mapping[str, LightTarget] = field(default_factory=dict)
    blinds: Mapping[str, int] = field(default_factory=dict)  # cover position 0-100
    nothing: bool = False

    def lit(self) -> list[str]:
        return [light for light, target in self.lights.items() if target.on]


NOTHING = Look(nothing=True)


def look_for(period: str, looks: Mapping[str, Look], schedule: Schedule) -> Look:
    """The room's look for ``period``, falling back through earlier periods."""
    name = period
    for _ in range(len(schedule.periods)):
        if name in looks:
            return looks[name]
        name = schedule.previous(name)
    return NOTHING


def scaled(look: Look, factor: float) -> Look:
    """The look with every set brightness multiplied by ``factor``.

    Lights at their last brightness are left alone: there is no number to scale.
    """
    if factor == 1.0 or look.nothing:
        return look
    lights = {}
    for light, target in look.lights.items():
        if target.on and target.brightness_pct is not None:
            pct = min(100.0, max(MIN_BRIGHTNESS_PCT, round(target.brightness_pct * factor, 1)))
            target = replace(target, brightness_pct=pct)
        lights[light] = target
    return replace(look, lights=lights)


def uniform(lights: list[str] | tuple[str, ...], target: LightTarget = ON) -> Look:
    """The same target on every light: how the first, copy-today looks are made."""
    return Look({light: target for light in lights})
