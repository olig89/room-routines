"""The room's own light level, measured while its lights are off.

A ceiling sensor sees its own lamp: one small bathroom reads 8 lux in the
dark and 31 lux with its light on. So a room keeps an *ambient* value, taken
only from readings made while all its lights are off, and not in the first few
seconds after they switch off (lamps fade, sensors average).

An ambient value older than ``stale_after`` counts as unknown, and unknown
counts as dark: the light comes on. Failing towards light is the safe side.
(Accepted for now; flagged to revisit with real data.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class AmbientTracker:
    stale_after: timedelta = timedelta(minutes=30)
    settle_after_off: timedelta = timedelta(seconds=10)
    value: float | None = None
    at: datetime | None = None
    _lights_on: bool = False
    _off_since: datetime | None = None

    def lights_changed(self, any_on: bool, t: datetime) -> None:
        if any_on:
            self._lights_on = True
        elif self._lights_on or self._off_since is None:
            self._lights_on = False
            self._off_since = t

    def reading(self, lux: float, t: datetime) -> bool:
        """Offer a reading. Returns whether it was taken as the ambient value."""
        if self._lights_on:
            return False
        if self._off_since is not None and t - self._off_since < self.settle_after_off:
            return False
        self.value, self.at = float(lux), t
        return True

    def ambient(self, now: datetime) -> float | None:
        if self.value is None or self.at is None or now - self.at > self.stale_after:
            return None
        return self.value


def dark_enough(ambient: float | None, threshold: float | None) -> bool:
    """Whether a room at ``ambient`` lux may switch on. No threshold = always."""
    return threshold is None or ambient is None or ambient < threshold
