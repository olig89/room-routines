"""Signals: some of a room's lights showing something while it's true.

"While I'm in a call, the desk lamp is purple; while I'm muted, green." A signal
names an entity and a state (as rules and starters do), the lights it takes and
how each should look, and optionally a light effect and a flash when it starts.

Signals are the top layer: while one holds a light, nothing else in the room
(its routine, motion, blending, period changes) touches that light. A signal
shows even when the room is off. When it ends, the light goes back to what the
room is doing, or, if the room isn't doing anything with it, to how it was
before the signal took it. The first signal in the room's list wins a light
that two want at once.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .looks import LightTarget
from .rules import Condition, condition_from, condition_to


@dataclass(frozen=True)
class Signal:
    name: str
    when: Condition
    lights: Mapping[str, LightTarget] = field(default_factory=dict)
    flash: bool = False  # flash once when it starts
    effect: str | None = None  # one of the light's own effects

    def __post_init__(self) -> None:
        if not self.lights:
            raise ValueError("a signal needs at least one light")


def held_by(signals: Iterable[Signal], states: Mapping[str, str | None], lights: Iterable[str]) -> dict[str, Signal]:
    """Which signal holds each light now (lights no signal wants are left out)."""
    active = [s for s in signals if s.when.holds(states)]
    out: dict[str, Signal] = {}
    for light in lights:
        for signal in active:
            if light in signal.lights:
                out[light] = signal
                break
    return out


def _target_to(t: LightTarget) -> dict[str, Any]:
    out: dict[str, Any] = {"on": t.on}
    if t.brightness_pct is not None:
        out["brightness_pct"] = t.brightness_pct
    if t.color_temp_kelvin is not None:
        out["color_temp_kelvin"] = t.color_temp_kelvin
    if t.rgb is not None:
        out["rgb"] = list(t.rgb)
    return out


def _target_from(data: Mapping[str, Any]) -> LightTarget:
    rgb = data.get("rgb")
    return LightTarget(
        bool(data.get("on", True)),
        None if data.get("brightness_pct") is None else float(data["brightness_pct"]),
        None if data.get("color_temp_kelvin") is None else int(data["color_temp_kelvin"]),
        tuple(int(c) for c in rgb) if rgb else None,
    )


def signal_from(data: Mapping[str, Any]) -> Signal:
    return Signal(
        name=str(data.get("name") or "").strip() or "Signal",
        when=condition_from(data["when"]),
        lights={str(light): _target_from(t) for light, t in (data.get("lights") or {}).items()},
        flash=bool(data.get("flash", False)),
        effect=(str(data["effect"]).strip() or None) if data.get("effect") else None,
    )


def signal_to(signal: Signal) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": signal.name,
        "when": condition_to(signal.when),
        "lights": {light: _target_to(t) for light, t in signal.lights.items()},
    }
    if signal.flash:
        out["flash"] = True
    if signal.effect:
        out["effect"] = signal.effect
    return out


def targets_to(targets: Mapping[str, LightTarget]) -> dict[str, Any]:
    return {light: _target_to(t) for light, t in targets.items()}


def targets_from(data: Mapping[str, Any]) -> dict[str, LightTarget]:
    return {str(light): _target_from(t) for light, t in (data or {}).items()}
