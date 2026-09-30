"""What a room's lights should do in a period.

A look lists, per light, whether it is on and how. ``brightness_pct=None`` on
a light that is on means "at its last brightness": a plain switch-on, which is
what a KNX motion sensor does today and what the first looks copy.

A look can instead turn on a Home Assistant scene (``scene``), so scenes can be
built and kept in Home Assistant and shared with wall buttons and dashboards.

A look can also be "do nothing" (``NOTHING``): the room stays dark in that
period. A period with no look of its own borrows the previous period's.

Each room has a Normal set of looks and, optionally, a Dim set for dark days
(see ``tracks``). A period without a Dim look uses its Normal one.

Blinds can be listed in a look, but they are never moved by motion; the room
only moves them when a new period starts, and only if the room asks for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from .periods import Schedule
from .tracks import NORMAL

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
    scene: str | None = None  # a scene entity to turn on instead of set lights

    def __post_init__(self) -> None:
        if self.scene and (self.lights or self.nothing):
            raise ValueError("a scene look can't also list lights or be 'nothing'")

    def lit(self) -> list[str]:
        return [light for light, target in self.lights.items() if target.on]


NOTHING = Look(nothing=True)


@dataclass(frozen=True)
class LookSource:
    """The look a room uses, and the period and track it was saved under."""

    look: Look
    period: str | None
    track: str


def resolve(
    period: str,
    track: str,
    looks: Mapping[str, Look],
    dim_looks: Mapping[str, Look] | None,
    schedule: Schedule,
) -> LookSource:
    """The room's look for ``period`` on ``track``.

    Walks back through the periods like ``look_for``. On the Dim track a
    period's Dim look wins; a period with only a Normal look uses that, so Dim
    looks are only needed where they differ.
    """
    name = period
    for _ in range(len(schedule.periods)):
        if track != NORMAL and dim_looks and name in dim_looks:
            return LookSource(dim_looks[name], name, track)
        if name in looks:
            return LookSource(looks[name], name, NORMAL)
        name = schedule.previous(name)
    return LookSource(NOTHING, None, NORMAL)


def scaled(look: Look, factor: float) -> Look:
    """The look with every set brightness multiplied by ``factor`` (auto-dim).

    Lights at their last brightness are left alone (there is no number to
    scale), as are colours. A scene look can't be scaled here: the integration
    reads the scene first where it can.
    """
    if factor == 1.0 or look.nothing or look.scene:
        return look
    lights = {}
    for light, target in look.lights.items():
        if target.on and target.brightness_pct is not None:
            pct = min(100.0, max(MIN_BRIGHTNESS_PCT, round(target.brightness_pct * factor, 1)))
            target = replace(target, brightness_pct=pct)
        lights[light] = target
    return replace(look, lights=lights)


def look_for(period: str, looks: Mapping[str, Look], schedule: Schedule) -> Look:
    """The room's Normal look for ``period``, falling back through earlier periods."""
    return resolve(period, NORMAL, looks, None, schedule).look


def uniform(lights: list[str] | tuple[str, ...], target: LightTarget = ON) -> Look:
    """The same target on every light: how the first, copy-today looks are made."""
    return Look({light: target for light in lights})
