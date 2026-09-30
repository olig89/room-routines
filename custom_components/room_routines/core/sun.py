"""Where the sun is, and how bright a clear sky would be.

``position`` uses NOAA's low-precision solar equations (good to a fraction of a
degree, plenty for telling a grey day from a bright one). ``clear_sky`` is the
Haurwitz clear-sky model: the sunlight reaching the ground (W/m²) under a
cloudless sky at a given sun height.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import acos, cos, degrees, exp, pi, radians, sin


@dataclass(frozen=True)
class SunPosition:
    elevation: float  # degrees above the horizon (negative below)
    morning: bool  # before solar noon


def position(latitude: float, longitude: float, when: datetime) -> SunPosition:
    if when.tzinfo is None:
        raise ValueError("when must be timezone-aware")
    t = when.astimezone(timezone.utc)
    day = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * pi / 365 * (day - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * cos(g) - 0.032077 * sin(g) - 0.014615 * cos(2 * g) - 0.040849 * sin(2 * g)
    )
    decl = (
        0.006918 - 0.399912 * cos(g) + 0.070257 * sin(g) - 0.006758 * cos(2 * g)
        + 0.000907 * sin(2 * g) - 0.002697 * cos(3 * g) + 0.00148 * sin(3 * g)
    )
    true_solar_minutes = hour * 60 + eqtime + 4 * longitude
    hour_angle = radians(true_solar_minutes / 4 - 180)
    lat = radians(latitude)
    cos_zenith = sin(lat) * sin(decl) + cos(lat) * cos(decl) * cos(hour_angle)
    zenith = degrees(acos(max(-1.0, min(1.0, cos_zenith))))
    # Normalise the hour angle to -180..180: negative before solar noon.
    ha = (degrees(hour_angle) + 180) % 360 - 180
    return SunPosition(90 - zenith, ha < 0)


def clear_sky(elevation: float) -> float:
    """Sunlight on the ground under a clear sky, in W/m² (Haurwitz)."""
    if elevation <= 0:
        return 0.0
    s = sin(radians(elevation))
    return 1098 * s * exp(-0.057 / s)
