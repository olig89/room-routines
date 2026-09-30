"""What a clear day looks like to one light sensor.

A light sensor's reading depends on where it is (behind glass, facing which
way), so "dark" can't be a fixed number of lux. Instead the sensor is compared
with itself: for every height of the sun (in 2° steps, mornings and afternoons
kept apart, since a window sees them differently) the reference is how bright
the sensor reads on a clear day, taken as the upper end (90th percentile) of
what it has read at that sun height recently.

It is learned from Home Assistant's own 5-minute statistics once a day. Old
references fade slowly (``FADE`` a day), so a bright day from last season
doesn't set the bar for ever, and a sun height needs ``MIN_SAMPLES`` readings
before it counts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

STEP = 2.0  # degrees of sun height per bucket
MIN_SAMPLES = 6
PERCENTILE = 0.9
FADE = 0.97  # a day, for a bucket not refreshed by new readings
MIN_ELEVATION = 2.0  # below this the sun is as good as down


def bucket(elevation: float, morning: bool) -> str:
    return f"{'am' if morning else 'pm'}{int(elevation // STEP)}"


@dataclass
class DaylightReference:
    table: dict[str, float] = field(default_factory=dict)  # bucket -> lux on a clear day

    def lookup(self, elevation: float, morning: bool) -> float | None:
        if elevation < MIN_ELEVATION:
            return None
        return self.table.get(bucket(elevation, morning))

    def learn(self, samples: Iterable[tuple[float, bool, float]], days: float = 1.0) -> None:
        """Samples as (sun elevation, morning, lux). Buckets not seen fade by ``days``."""
        seen: dict[str, list[float]] = {}
        for elevation, morning, lux in samples:
            if elevation < MIN_ELEVATION or lux is None:
                continue
            seen.setdefault(bucket(elevation, morning), []).append(float(lux))
        fade = FADE ** max(0.0, days)
        new: dict[str, float] = {}
        for key, old in self.table.items():
            new[key] = old * fade
        for key, values in seen.items():
            if len(values) < MIN_SAMPLES:
                continue
            values.sort()
            high = values[min(len(values) - 1, int(PERCENTILE * len(values)))]
            new[key] = max(high, new.get(key, 0.0))
        self.table = {k: round(v, 1) for k, v in new.items() if v > 0}

    @property
    def learned(self) -> int:
        return len(self.table)

    def to_dict(self) -> dict[str, float]:
        return dict(self.table)

    @classmethod
    def from_dict(cls, data: Mapping[str, float] | None) -> DaylightReference:
        return cls({str(k): float(v) for k, v in (data or {}).items()})
