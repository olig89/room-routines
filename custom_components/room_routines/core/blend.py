"""Moving gradually from one period's look to the next.

A room can blend into the next period over the last N minutes of a period:
at a fraction ``f`` of the way through that window the lights sit ``f`` of the
way from this period's look to the next one's. The integration sends a fresh
blended look every few minutes with a matching transition, so the change is
too slow to notice.

Per light:

- both looks set a brightness: it moves in a straight line;
- both set a colour temperature: it moves in mireds (even to the eye);
- both set a colour: it moves through RGB;
- on in this look, off in the next: it dims towards the minimum and goes off
  when the next period starts;
- anything else (a light at its last brightness, off then on, one look with a
  colour and the other a white) can't be blended: it keeps this period's
  setting and changes when the next period starts, as without blending.

A "do nothing" look, or a scene the integration can't read, can't be blended
at all: such a room just changes when the period does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from .looks import MIN_BRIGHTNESS_PCT, LightTarget, Look


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def _kelvin(a: int, b: int, f: float) -> int:
    mired = _lerp(1_000_000 / a, 1_000_000 / b, f)
    return round(1_000_000 / mired)


def _target(a: LightTarget, b: LightTarget | None, f: float) -> LightTarget:
    if b is None or not a.on:
        return a
    if not b.on:
        # Fading out towards the next period: down to the minimum, then off on time.
        if a.brightness_pct is None:
            return a
        return replace(a, brightness_pct=round(max(MIN_BRIGHTNESS_PCT, _lerp(a.brightness_pct, MIN_BRIGHTNESS_PCT, f)), 1))
    out = a
    if a.brightness_pct is not None and b.brightness_pct is not None:
        out = replace(out, brightness_pct=round(max(MIN_BRIGHTNESS_PCT, _lerp(a.brightness_pct, b.brightness_pct, f)), 1))
    if a.color_temp_kelvin and b.color_temp_kelvin:
        out = replace(out, color_temp_kelvin=_kelvin(a.color_temp_kelvin, b.color_temp_kelvin, f))
    elif a.rgb and b.rgb:
        out = replace(out, rgb=tuple(round(_lerp(x, y, f)) for x, y in zip(a.rgb, b.rgb, strict=True)))
    return out


def blendable(look: Look) -> bool:
    return not look.nothing and not look.scene


def blend(a: Look, b: Look, f: float) -> Look:
    """The look ``f`` (0..1) of the way from ``a`` to ``b``. Lights only in ``b`` wait."""
    f = min(1.0, max(0.0, f))
    if not (blendable(a) and blendable(b)) or f == 0.0:
        return a
    return replace(a, lights={light: _target(t, b.lights.get(light), f) for light, t in a.lights.items()})


def fraction(now: datetime, next_start: datetime, minutes: float) -> float | None:
    """How far through the blend window ``now`` is, or None if outside it."""
    if minutes <= 0:
        return None
    window = timedelta(minutes=minutes)
    start = next_start - window
    if now < start or now >= next_start:
        return None
    return (now - start) / window
