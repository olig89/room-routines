"""A stepped fade, for lights that can't transition by themselves.

When the period changes while a room is lit, the room drifts to the new look
over ``duration`` (90 s by default). Lights that support Home Assistant's
``transition`` get one command; the rest (KNX lights, for one) get the fade as a
series of brightness steps, planned here.

Brightness is interpolated linearly. A light switching off fades down to the
minimum and switches off on the last step; a light switching on starts from the
minimum. A light that should be "on at its last brightness" has no number to
fade to, so it just switches on in the first step. Colour temperature and
colour are set on the last step.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from math import ceil

from .looks import MIN_BRIGHTNESS_PCT, LightTarget, Look


def step_for(duration: timedelta) -> timedelta:
    """At least five steps, at most one every 10 s, never under a second apart."""
    return max(timedelta(seconds=1), min(timedelta(seconds=10), duration / 5))


def plan_fade(
    current: Mapping[str, float | None],
    target: Look,
    duration: timedelta = timedelta(seconds=90),
    step: timedelta | None = None,
) -> list[tuple[timedelta, dict[str, LightTarget]]]:
    """Steps as (offset from now, {light: what to send}).

    ``current`` maps each light to its brightness now, or None if it is off.
    Lights in ``current`` but not in the look are left alone.
    """
    step = step or step_for(duration)
    steps = max(1, ceil(duration / step))
    plan: list[tuple[timedelta, dict[str, LightTarget]]] = [
        (duration * (i + 1) / steps, {}) for i in range(steps)
    ]
    for light, goal in target.lights.items():
        start = current.get(light)
        if not goal.on:
            if start is None:
                continue
            for i, (_, sends) in enumerate(plan[:-1]):
                pct = start + (MIN_BRIGHTNESS_PCT - start) * (i + 1) / steps
                sends[light] = LightTarget(True, round(pct, 1))
            plan[-1][1][light] = goal
            continue
        if goal.brightness_pct is None:
            if start is None:
                plan[0][1][light] = goal
            continue
        begin = MIN_BRIGHTNESS_PCT if start is None else start
        for i, (_, sends) in enumerate(plan):
            pct = begin + (goal.brightness_pct - begin) * (i + 1) / steps
            last = i == steps - 1
            sends[light] = LightTarget(
                True,
                round(pct, 1),
                goal.color_temp_kelvin if last else None,
                goal.rgb if last else None,
            )
    return [(offset, sends) for offset, sends in plan if sends]
