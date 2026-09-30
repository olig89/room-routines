"""Normal and Dim days.

Every room can have a second set of looks, the *Dim* track, for dark days:
winter afternoons, heavy overcast. One light sensor for the whole house (ideally
outdoors, or one indoors that sees the sky) picks the track.

The light level is a 15-minute average, weighted by time: a sensor that only
reports when the light changes (as Hue sensors do) keeps its last value until
it sends a new one, so a burst of readings while clouds pass doesn't outweigh
a long steady stretch. The track changes only when the average crosses one of
two thresholds (Dim below ``dim_below``, back to Normal above ``normal_above``)
and only once the current track has held for ``min_hold``, so passing clouds
don't flip it back and forth.

Auto-dim: on a Dim day, a period without a Dim look can use its Normal look
with every set brightness at ``auto_dim_pct`` percent (50: a light at 66 % comes
on at 33 %). 100 turns it off. Explicit Dim looks always win.

A sensor that is unavailable, or hasn't reported yet, changes nothing: the
current track stays. (A room that falls to Dim every time a battery dies would
be worse than one that stays Normal a little too long.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

NORMAL = "normal"
DIM = "dim"
TRACKS = (NORMAL, DIM)
TRACK_LABELS = {NORMAL: "Normal", DIM: "Dim"}

DEFAULT_DIM_BELOW = 800.0
DEFAULT_NORMAL_ABOVE = 1500.0
DEFAULT_AUTO_DIM_PCT = 100.0


@dataclass(frozen=True)
class TrackSettings:
    sensor: str | None = None
    fallback: str | None = None
    dim_below: float = DEFAULT_DIM_BELOW
    normal_above: float = DEFAULT_NORMAL_ABOVE
    auto_dim_pct: float = DEFAULT_AUTO_DIM_PCT
    window: timedelta = timedelta(minutes=15)
    min_hold: timedelta = timedelta(minutes=20)

    def __post_init__(self) -> None:
        if not self.dim_below < self.normal_above:
            raise ValueError("dim_below must be lower than normal_above")
        if not 1 <= self.auto_dim_pct <= 100:
            raise ValueError("auto_dim_pct must be between 1 and 100")

    @property
    def auto_dim(self) -> float:
        """Auto-dim as a factor (1.0 = off)."""
        return self.auto_dim_pct / 100

    @property
    def enabled(self) -> bool:
        return bool(self.sensor or self.fallback)


@dataclass
class LightLevel:
    """Readings from one sensor, averaged over a window by how long each value held."""

    window: timedelta = timedelta(minutes=15)
    readings: list[tuple[datetime, float]] = field(default_factory=list)

    def add(self, value: float, at: datetime) -> None:
        if self.readings and at < self.readings[-1][0]:
            return  # out of order: ignore
        self.readings.append((at, float(value)))
        self._prune(at)

    def _prune(self, now: datetime) -> None:
        start = now - self.window
        # Keep the last reading from before the window: it covers the window's start.
        while len(self.readings) > 1 and self.readings[1][0] <= start:
            self.readings.pop(0)

    def average(self, now: datetime) -> float | None:
        self._prune(now)
        if not self.readings:
            return None
        start = now - self.window
        total = 0.0
        span = 0.0
        for i, (at, value) in enumerate(self.readings):
            begin = max(at, start)
            end = self.readings[i + 1][0] if i + 1 < len(self.readings) else now
            seconds = (end - begin).total_seconds()
            if seconds > 0:
                total += value * seconds
                span += seconds
        if span == 0:
            return self.readings[-1][1]
        return total / span


@dataclass
class TrackChooser:
    settings: TrackSettings
    track: str = NORMAL
    since: datetime | None = None
    by_hand: bool = False
    main: LightLevel = field(default_factory=LightLevel)
    backup: LightLevel = field(default_factory=LightLevel)
    main_ok: bool = False
    backup_ok: bool = False

    def __post_init__(self) -> None:
        self.main.window = self.settings.window
        self.backup.window = self.settings.window

    def reading(self, sensor: str, value: float | None, at: datetime) -> None:
        """A sensor's new value. ``None`` = unavailable."""
        if sensor == self.settings.sensor:
            self.main_ok = value is not None
            if value is not None:
                self.main.add(value, at)
        if sensor == self.settings.fallback:
            self.backup_ok = value is not None
            if value is not None:
                self.backup.add(value, at)

    def level(self, now: datetime) -> tuple[float | None, str | None]:
        """The averaged light level and the sensor it came from."""
        if self.main_ok and (avg := self.main.average(now)) is not None:
            return avg, self.settings.sensor
        if self.backup_ok and (avg := self.backup.average(now)) is not None:
            return avg, self.settings.fallback
        return None, None

    def update(self, now: datetime, first: bool = False) -> bool:
        """Re-check the track. Returns whether it changed.

        ``first`` (at start-up) picks the track straight from the light level,
        without waiting for the minimum hold.
        """
        if self.by_hand:
            return False
        level, _ = self.level(now)
        if level is None:
            return False
        if first or self.since is None:
            # Never decided from a real level yet: pick straight away.
            self.since = now
            new = DIM if level < self.settings.dim_below else NORMAL
            changed = new != self.track
            self.track = new
            return changed
        if now - self.since < self.settings.min_hold:
            return False
        if self.track == NORMAL and level < self.settings.dim_below:
            new = DIM
        elif self.track == DIM and level > self.settings.normal_above:
            new = NORMAL
        else:
            return False
        self.track = new
        self.since = now
        return True

    def choose(self, track: str, now: datetime) -> bool:
        """Chosen by hand: held until ``release`` (the next period start)."""
        if track not in TRACKS:
            raise ValueError(f"unknown track {track!r}")
        self.by_hand = True
        changed = track != self.track
        self.track = track
        self.since = now
        return changed

    def release(self, now: datetime) -> bool:
        """Back to following the light level. Returns whether the track changed."""
        if not self.by_hand:
            return False
        self.by_hand = False
        return self.update(now, first=True)
