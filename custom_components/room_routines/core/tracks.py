"""Normal days and Dark Days.

Every room can have a second set of looks for Dark Days: heavy overcast, a
winter morning before sunrise. Whether today is a Dark Day is judged against a
*clear* day at the same height of the sun, not against a fixed light level, so
an evening isn't "dark" just because the sun is getting low, and a bright but
cloudy day isn't dark just because it's cloudy.

Two sources measure "how much of a clear day's light is there right now":

* **the weather**: the sunlight reaching the ground at Home Assistant's
  location (Open-Meteo), divided by what a cloudless sky would give at this
  sun height (see ``sun.clear_sky``);
* **a light sensor** (optional, with a backup sensor): its 15-minute average,
  divided by what it reads on a clear day at this sun height, learned from its
  own history (see ``daylight.DaylightReference``).

One is checked first (the weather, unless the setting says otherwise), the
other when the first has nothing to say.

Rules, in order:

1. Dark Days only happen in the periods chosen for them (by default the ones
   that start between 06:00 and 15:00). Any other period is a Normal day.
2. With the sun down (below ``MIN_ELEVATION``) it's a Dark Day.
3. With the weather on and less than ``dark_below_wm2`` W/m² of sunlight
   reaching the ground, it's a Dark Day whatever a clear day would give: a
   clear winter noon is still darker indoors than a grey summer day. It's
   Normal again only above ``NORMAL_AGAIN_RATIO`` times that (and only if rule 4
   agrees). 0 switches this rule off.
4. Below ``dark_below`` percent of a clear day it becomes a Dark Day; above
   ``normal_above`` it becomes Normal again; in between it stays as it is.
   The day holds for at least ``min_hold`` before changing again, so passing
   clouds don't flip it back and forth. At start-up and at each period start it
   is picked straight away.

No reading at all changes nothing: the current day stays.

On a Dark Day, a period without its own Dark Day look uses its Normal look with
every set brightness at ``brightness_pct`` percent (below 100 dims, above 100
brightens, but no light goes past its own maximum).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .daylight import MIN_ELEVATION, DaylightReference
from .sun import SunPosition

NORMAL = "normal"
DIM = "dim"  # stored id of the Dark Day track
TRACKS = (NORMAL, DIM)
TRACK_LABELS = {NORMAL: "Normal day", DIM: "Dark Day"}

WEATHER = "weather"
SENSOR = "sensor"
SOURCES = (WEATHER, SENSOR)

DEFAULT_DARK_BELOW = 40.0
DEFAULT_NORMAL_ABOVE = 55.0
DEFAULT_BRIGHTNESS_PCT = 100.0
DEFAULT_DARK_BELOW_WM2 = 150.0  # sunlight on the ground, W/m²; 0 = off
MAX_DARK_BELOW_WM2 = 600.0
NORMAL_AGAIN_RATIO = 1.2  # Normal again above 1.2 × the sunlight floor
MAX_BRIGHTNESS_PCT = 300.0
DEFAULT_FIRST_START = time(6, 0)
DEFAULT_LAST_START = time(15, 0)
WEATHER_STALE = timedelta(minutes=45)


def default_periods(starts: dict[str, time]) -> tuple[str, ...]:
    """The periods Dark Days apply to when none are chosen: the daytime ones."""
    return tuple(n for n, t in starts.items() if DEFAULT_FIRST_START <= t <= DEFAULT_LAST_START)


@dataclass(frozen=True)
class TrackSettings:
    on: bool = False
    weather: bool = True
    sensor: str | None = None
    fallback: str | None = None
    first: str = WEATHER
    periods: tuple[str, ...] | None = None  # None: the default daytime periods
    dark_below: float = DEFAULT_DARK_BELOW  # percent of a clear day
    normal_above: float = DEFAULT_NORMAL_ABOVE
    brightness_pct: float = DEFAULT_BRIGHTNESS_PCT
    dark_below_wm2: float = DEFAULT_DARK_BELOW_WM2  # 0 = off
    window: timedelta = timedelta(minutes=15)
    min_hold: timedelta = timedelta(minutes=20)

    def __post_init__(self) -> None:
        if self.first not in SOURCES:
            raise ValueError(f"unknown source {self.first!r}")
        if not 0 <= self.dark_below < self.normal_above <= 200:
            raise ValueError("dark_below must be lower than normal_above")
        if not 0 <= self.dark_below_wm2 <= MAX_DARK_BELOW_WM2:
            raise ValueError(f"dark_below_wm2 must be between 0 and {MAX_DARK_BELOW_WM2:g}")
        if not 1 <= self.brightness_pct <= MAX_BRIGHTNESS_PCT:
            raise ValueError(f"brightness_pct must be between 1 and {MAX_BRIGHTNESS_PCT:g}")

    @property
    def factor(self) -> float:
        """Dark Day brightness for Normal looks, as a factor (1.0 = unchanged)."""
        return self.brightness_pct / 100

    @property
    def has_sensor(self) -> bool:
        return bool(self.sensor or self.fallback)

    @property
    def enabled(self) -> bool:
        return self.on and (self.weather or self.has_sensor)

    def order(self) -> tuple[str, ...]:
        """The sources in use, the one checked first first."""
        use = [s for s in SOURCES if (s == WEATHER and self.weather) or (s == SENSOR and self.has_sensor)]
        return tuple(sorted(use, key=lambda s: s != self.first))


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


@dataclass(frozen=True)
class Reading:
    """How much of a clear day's light there is, and where that came from."""

    pct: float | None  # percent of a clear day; None = no reading
    source: str | None = None  # WEATHER or SENSOR
    lux: float | None = None  # the sensor's average, for a sensor reading
    sensor: str | None = None
    sunlight: float | None = None  # W/m² on the ground, for a weather reading


@dataclass
class TrackChooser:
    settings: TrackSettings
    track: str = NORMAL
    since: datetime | None = None
    by_hand: bool = False
    reason: str = ""
    main: LightLevel = field(default_factory=LightLevel)
    backup: LightLevel = field(default_factory=LightLevel)
    main_ok: bool = False
    backup_ok: bool = False
    weather_pct: float | None = None
    weather_at: datetime | None = None
    weather_sunlight: float | None = None
    references: dict[str, DaylightReference] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.main.window = self.settings.window
        self.backup.window = self.settings.window

    # -- inputs --

    def reading(self, sensor: str, value: float | None, at: datetime) -> None:
        """A light sensor's new value. ``None`` = unavailable."""
        if sensor == self.settings.sensor:
            self.main_ok = value is not None
            if value is not None:
                self.main.add(value, at)
        if sensor == self.settings.fallback:
            self.backup_ok = value is not None
            if value is not None:
                self.backup.add(value, at)

    def weather(self, pct: float | None, at: datetime, sunlight: float | None = None) -> None:
        """The weather's percent of a clear day (``None``: no answer, e.g. the sun
        too low to tell) and the sunlight it measured, in W/m²."""
        self.weather_pct = pct
        self.weather_at = at
        self.weather_sunlight = sunlight

    # -- readings --

    def weather_reading(self, now: datetime) -> Reading:
        if not self.settings.weather or self.weather_at is None or now - self.weather_at > WEATHER_STALE:
            return Reading(None)
        if self.weather_pct is None:
            return Reading(None, sunlight=self.weather_sunlight)
        return Reading(self.weather_pct, WEATHER, sunlight=self.weather_sunlight)

    def sensor_level(self, now: datetime) -> tuple[float | None, str | None]:
        """The averaged light level and the sensor it came from."""
        if self.main_ok and (avg := self.main.average(now)) is not None:
            return avg, self.settings.sensor
        if self.backup_ok and (avg := self.backup.average(now)) is not None:
            return avg, self.settings.fallback
        return None, None

    def sensor_reading(self, now: datetime, sun: SunPosition) -> Reading:
        lux, sensor = self.sensor_level(now)
        if lux is None or sensor is None:
            return Reading(None)
        reference = self.references.get(sensor)
        clear = reference.lookup(sun.elevation, sun.morning) if reference else None
        if not clear:
            return Reading(None, None, lux, sensor)  # still learning this sun height
        return Reading(round(100 * lux / clear, 1), SENSOR, lux, sensor)

    def current(self, now: datetime, sun: SunPosition) -> Reading:
        """The reading from the first source that has one."""
        fallback = Reading(None)
        for source in self.settings.order():
            got = self.weather_reading(now) if source == WEATHER else self.sensor_reading(now, sun)
            if got.pct is not None:
                return got
            if got.lux is not None and fallback.lux is None:
                fallback = got
        return fallback

    # -- deciding --

    def _wanted(self, now: datetime, sun: SunPosition, active: bool, first: bool) -> str | None:
        if not active:
            self.reason = "not a Dark Day period"
            return NORMAL
        if sun.elevation < MIN_ELEVATION:
            self.reason = "the sun is down"
            return DIM
        floor = self.settings.dark_below_wm2
        sunlight = self.weather_reading(now).sunlight if floor > 0 else None
        if sunlight is not None and sunlight < floor:
            self.reason = f"{round(sunlight)} W/m² of sunlight, below {floor:g}"
            return DIM
        got = self.current(now, sun)
        if got.pct is None:
            self.reason = "no light reading"
            return None
        where = "the weather" if got.source == WEATHER else "the light sensor"
        self.reason = f"{round(got.pct)} % of a clear day, from {where}"
        if got.pct < self.settings.dark_below:
            return DIM
        settled = first or self.since is None
        if sunlight is not None and sunlight < floor * NORMAL_AGAIN_RATIO:
            # Just above the sunlight floor: not bright enough to call it Normal yet.
            self.reason = f"{round(sunlight)} W/m² of sunlight, just above {floor:g}"
            if settled:
                return DIM if sunlight < floor * (1 + NORMAL_AGAIN_RATIO) / 2 else NORMAL
            return None
        if got.pct > self.settings.normal_above:
            return NORMAL
        if settled:
            return DIM if got.pct < (self.settings.dark_below + self.settings.normal_above) / 2 else NORMAL
        return None  # in between: stays

    def update(self, now: datetime, sun: SunPosition, active: bool, first: bool = False) -> bool:
        """Re-check the day. Returns whether it changed.

        ``first`` (at start-up and each period start) picks straight away,
        without waiting for the minimum hold. Leaving the chosen periods is
        always immediate.
        """
        if not self.settings.enabled:
            self.reason = "Dark Days are off"
            changed = self.track != NORMAL
            self.track, self.since, self.by_hand = NORMAL, None, False
            return changed
        if self.by_hand:
            return False
        new = self._wanted(now, sun, active, first)
        if new is None or new == self.track:
            if new is not None and self.since is None:
                self.since = now
            return False
        immediate = first or self.since is None or not active
        if not immediate and now - self.since < self.settings.min_hold:
            return False
        self.track = new
        self.since = now
        return True

    def choose(self, track: str, now: datetime) -> bool:
        """Chosen by hand: held until ``release`` (the next period start)."""
        if track not in TRACKS:
            raise ValueError(f"unknown track {track!r}")
        self.by_hand = True
        self.reason = "chosen by hand"
        changed = track != self.track
        self.track = track
        self.since = now
        return changed

    def release(self) -> None:
        """Back to following the light (the caller then re-checks with ``first``)."""
        self.by_hand = False
