"""Layers: who decides each light.

A light can be wanted by several things at once. Each one is a *layer*, and the
most important layer that wants a light decides it. When that layer lets go,
the light falls to the next one down, at whatever that layer is doing then
(part-way through a blend, say), and to off when nothing wants it.

1. **Inform**: chosen lights show something while it's true (in a call: the
   desk lamp purple). Only the lights it names.
2. **Hand**: the lights someone changed. How long a change holds is the room's
   choice.
3. **Someone's there**: while the room's sensors see someone.
4. **Ambient**: the room's routine, from its start (a timer, a starter, the
   switch) until it's switched off, following the periods and blending.

A layer that has nothing to say about a light (a look that leaves it out)
doesn't take it: the light stays with the layer below.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum

from .looks import LightTarget


class Layer(IntEnum):
    """Lower is more important."""

    SIGNAL = 1
    HAND = 2
    SOMEONE = 3
    AMBIENT = 4


LAYER_IDS = {
    Layer.SIGNAL: "inform",
    Layer.HAND: "hand",
    Layer.SOMEONE: "someone",
    Layer.AMBIENT: "ambient",
}
LAYER_LABELS = {
    Layer.SIGNAL: "Inform",
    Layer.HAND: "Hand",
    Layer.SOMEONE: "Someone's there",
    Layer.AMBIENT: "Ambient",
}


@dataclass(frozen=True)
class Claim:
    """A layer wanting some lights, each at a target."""

    layer: Layer
    targets: Mapping[str, LightTarget] = field(default_factory=dict)
    name: str = ""  # for the status and log: "In a call", "Evening look"


def owners(lights: Iterable[str], claims: Iterable[Claim]) -> dict[str, Claim | None]:
    """For each light, the most important claim that names it (None: nothing wants it)."""
    ranked = sorted(claims, key=lambda c: c.layer)  # stable: equal layers keep their order
    out: dict[str, Claim | None] = {}
    for light in lights:
        out[light] = next((c for c in ranked if light in c.targets), None)
    return out


def combine(lights: Iterable[str], claims: Iterable[Claim]) -> dict[str, LightTarget]:
    """What each light should be: its owner's target, or off when nothing wants it."""
    result: dict[str, LightTarget] = {}
    for light, claim in owners(lights, claims).items():
        result[light] = claim.targets[light] if claim is not None else LightTarget(False)
    return result
