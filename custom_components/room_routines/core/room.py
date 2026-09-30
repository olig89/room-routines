"""One room's behaviour: events in, actions out.

The room never talks to Home Assistant. The integration feeds it events
(a sensor changed, a light changed, a lux reading, the period changed, time
passed) and carries out the actions it returns. Every decision carries a
plain-language reason, which becomes the room's status and log line.

States:

- **idle**: lights off (as far as the room knows). Motion may switch them on.
- **owned**: the room switched the lights on, so it will also switch them off
  once the room has been empty for the timeout.
- **manual**: someone else switched a light on. The room does nothing until
  every light is off again.

After someone switches the room's lights off by hand, motion is ignored for
the cooldown, so a person can leave a room in the dark.

Each sensor has one of two roles, chosen per room. A *trigger* can switch the
lights on. A *hold* sensor can only keep an owned room occupied, so its false
alarms can at worst keep a light on a little longer. Any kind of sensor can
take either role (a presence sensor can be a room's only trigger).

Lights fed by a switched power circuit (a KNX circuit powering smart bulbs) are
listed in ``powered_by``: the room switches the circuit on before the bulbs and
never switches a circuit off.

Stealth mode (a house-wide switch) makes every motion sensor read as "nobody
here", whatever it really sees. So nothing switches on, and a lit room counts
down and goes dark as normal. Nothing else changes: periods, looks and blinds
carry on. When stealth ends the sensors count again; one that sees someone at
that moment acts like fresh motion.

Dark Days: the house tells every room which *track* it is on (Normal or Dark Day);
a room uses its Dark Day look for the period where it has one. A track change in a
lit room drifts to the new look, as a period change does.

Hand changes: when someone changes a light the room switched on (dims it,
switches it off), the decision carries a ``HandChange`` so the integration can
remember it and, if it keeps happening, suggest a better look or schedule.

Log-only mode lives in the integration, not here: it carries out no actions
and reports the room's own commands back as light changes, so the room runs on
what it *would* have done while the real lights follow the old wall-sensor
wiring.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .looks import Look, LookSource, resolve, scaled
from .lux import AmbientTracker, dark_enough
from .periods import Schedule
from .tracks import NORMAL, TRACK_LABELS


class State(Enum):
    IDLE = "idle"
    OWNED = "owned"
    MANUAL = "manual"


@dataclass(frozen=True)
class RoomConfig:
    name: str
    lights: tuple[str, ...]
    triggers: tuple[str, ...]
    holds: tuple[str, ...] = ()
    looks: Mapping[str, Look] = field(default_factory=dict)
    dim_looks: Mapping[str, Look] = field(default_factory=dict)
    threshold_lux: float | None = 50.0
    timeout: timedelta = timedelta(seconds=30)
    cooldown: timedelta = timedelta(seconds=30)
    drift: timedelta = timedelta(seconds=90)
    fade_out: timedelta = timedelta(seconds=15)
    powered_by: Mapping[str, str] = field(default_factory=dict)  # bulb -> power circuit
    blinds_with_periods: bool = False

    def __post_init__(self) -> None:
        overlap = set(self.triggers) & set(self.holds)
        if overlap:
            raise ValueError(f"a sensor can't be both trigger and hold: {sorted(overlap)}")
        if not self.lights:
            raise ValueError("a room needs at least one light")

    def switchable(self) -> tuple[str, ...]:
        """The lights the room may switch off: never a power circuit."""
        circuits = set(self.powered_by.values())
        return tuple(light for light in self.lights if light not in circuits)


# ---- actions -----------------------------------------------------------------


@dataclass(frozen=True)
class ApplyLook:
    look: Look
    power_first: tuple[str, ...] = ()
    transition: timedelta | None = None
    # Auto-dim still to apply: only for a scene look, whose settings the
    # integration has to read first (a lights look arrives already scaled).
    factor: float = 1.0


@dataclass(frozen=True)
class TurnOff:
    lights: tuple[str, ...]
    transition: timedelta | None = None


@dataclass(frozen=True)
class MoveBlinds:
    positions: Mapping[str, int]


@dataclass(frozen=True)
class WakeAt:
    """Call ``Room.tick`` at (or after) this time."""

    at: datetime


Action = ApplyLook | TurnOff | MoveBlinds | WakeAt

ADJUSTED = "adjusted"
SWITCHED_OFF = "switched_off"


@dataclass(frozen=True)
class HandChange:
    """Someone changed lights the room had switched on."""

    kind: str  # ADJUSTED or SWITCHED_OFF
    period: str
    track: str
    after: timedelta  # since the room switched the lights on


@dataclass(frozen=True)
class Decision:
    actions: tuple[Action, ...] = ()
    reason: str = ""
    change: HandChange | None = None


# ---- the room ----------------------------------------------------------------


class Room:
    def __init__(
        self,
        config: RoomConfig,
        schedule: Schedule,
        period: str,
        lights_on: bool,
        now: datetime,
        ambient: AmbientTracker | None = None,
        stealth: bool = False,
        track: str = NORMAL,
        auto_dim: float = 1.0,
    ) -> None:
        self.config = config
        self.schedule = schedule
        self.period = period
        self.track = track
        self.auto_dim = auto_dim  # house-wide: Normal looks at this factor on Dark Days
        self.owned_at: datetime | None = None
        self.ambient = ambient or AmbientTracker()
        if lights_on:
            self.ambient.lights_changed(True, now)
        # After a restart with lights on, the room can't know who switched them
        # on, so it never switches off what it didn't switch on.
        self.state = State.MANUAL if lights_on else State.IDLE
        self.sensors: dict[str, bool] = {}
        self.deadline: datetime | None = None
        self.cooldown_until: datetime | None = None
        self.stealth = stealth
        self.last = Decision(reason="manual: lights were on at start" if lights_on else "idle")

    # -- helpers --

    def _any(self, sensors: tuple[str, ...]) -> bool:
        return any(self.sensors.get(s, False) for s in sensors)

    def occupied(self) -> bool:
        if self.stealth:
            return False
        if self._any(self.config.triggers):
            return True
        return self.state is State.OWNED and self._any(self.config.holds)

    def source(self) -> LookSource:
        return resolve(self.period, self.track, self.config.looks, self.config.dim_looks, self.schedule)

    def _look(self) -> Look:
        return self.source().look

    def dim_factor(self) -> float:
        """Dark Day brightness for the look in use: on a Dark Day, a Normal look is
        turned down (or up)."""
        if self.track != NORMAL and self.source().track == NORMAL:
            return self.auto_dim
        return 1.0

    def _to_apply(self, look: Look) -> tuple[Look, float]:
        """The look as sent (motion never moves blinds), with auto-dim applied
        or, for a scene, passed on."""
        factor = self.dim_factor()
        if look.scene:
            return Look(scene=look.scene), factor
        return scaled(Look(look.lights), factor), 1.0

    def _look_name(self) -> str:
        dim = self.source().track != NORMAL
        factor = self.dim_factor()
        auto = f" at {round(factor * 100)} %" if factor != 1.0 else ""
        return f"{self.period} look{' for Dark Days' if dim else ''}{auto}"

    def _power_for(self, look: Look) -> tuple[str, ...]:
        return tuple(sorted({self.config.powered_by[b] for b in look.lit() if b in self.config.powered_by}))

    def _unchanged(self) -> Decision:
        """Nothing to do; the status keeps its last reason."""
        return Decision((), self.last.reason)

    def _decide(self, reason: str, *actions: Action) -> Decision:
        self.last = Decision(tuple(actions), reason)
        return self.last

    def _check_timer(self, now: datetime) -> Decision | None:
        """Start or cancel the switch-off countdown for an owned room."""
        if self.state is not State.OWNED:
            return None
        if self.occupied():
            if self.deadline is not None:
                self.deadline = None
                return self._decide("occupied again: countdown cancelled")
            return None
        if self.deadline is None:
            self.deadline = now + self.config.timeout
            return self._decide(
                f"empty: lights off in {int(self.config.timeout.total_seconds())} s",
                WakeAt(self.deadline),
            )
        return None

    # -- events --

    def sensor(self, entity: str, on: bool, now: datetime) -> Decision:
        was = self.sensors.get(entity, False)
        self.sensors[entity] = on
        is_trigger = entity in self.config.triggers
        if self.stealth:
            return self._check_timer(now) or self._decide("stealth mode: motion ignored")
        if self.state is State.MANUAL:
            return self._decide("manual: a light was switched on by hand")
        if self.state is State.IDLE:
            if not (on and not was and is_trigger):
                return self._decide("idle")
            return self._switch_on(entity, now)
        return self._check_timer(now) or self._decide(
            "owned: occupied" if self.occupied() else "owned: empty, counting down"
        )

    def _switch_on(self, entity: str, now: datetime) -> Decision:
        if self.cooldown_until is not None and now < self.cooldown_until:
            return self._decide("motion ignored: lights were just switched off by hand")
        ambient = self.ambient.ambient(now)
        if not dark_enough(ambient, self.config.threshold_lux):
            return self._decide(
                f"motion at {entity}, but too bright ({ambient:g} lux, switches on below "
                f"{self.config.threshold_lux:g})"
            )
        look = self._look()
        if look.nothing:
            return self._decide(f"motion at {entity}: {self._look_name()} is 'do nothing'")
        motion_look, factor = self._to_apply(look)
        self.state = State.OWNED
        self.owned_at = now
        self.deadline = None
        self.ambient.lights_changed(True, now)
        lux = "unknown" if ambient is None else f"{ambient:g} lux"
        return self._decide(
            f"motion at {entity}: {self._look_name()} (ambient {lux})",
            ApplyLook(motion_look, self._power_for(motion_look), factor=factor),
        )

    def _hand_change(self, kind: str, now: datetime) -> HandChange:
        since = self.owned_at or now
        return HandChange(kind, self.period, self.track, now - since)

    def lights(self, any_on: bool, own: bool, now: datetime) -> Decision:
        """A light in the room changed. ``any_on`` is the room after the change."""
        self.ambient.lights_changed(any_on, now)
        if own:
            return self._unchanged()
        if self.state is State.IDLE:
            if any_on:
                self.state = State.MANUAL
                return self._decide("manual: a light was switched on by hand")
            return self._unchanged()
        if any_on:
            # Dimmed or partly switched by hand: an owned room keeps ownership
            # and still switches off after the timeout.
            if self.state is State.MANUAL:
                return self._unchanged()
            self.last = Decision(
                (), "owned: adjusted by hand, still switches off when empty", self._hand_change(ADJUSTED, now)
            )
            return self.last
        change = self._hand_change(SWITCHED_OFF, now) if self.state is State.OWNED else None
        self.state = State.IDLE
        self.deadline = None
        self.owned_at = None
        self.cooldown_until = now + self.config.cooldown
        self.last = Decision(
            (), f"switched off by hand: motion ignored for {int(self.config.cooldown.total_seconds())} s", change
        )
        return self.last

    def lux(self, value: float, now: datetime) -> bool:
        return self.ambient.reading(value, now)

    def period_changed(self, period: str, now: datetime) -> Decision:
        self.period = period
        return self._restyle(f"period is now {period}", blinds=True)

    def track_changed(self, track: str, now: datetime) -> Decision:
        if track == self.track:
            return self._unchanged()
        self.track = track
        return self._restyle(TRACK_LABELS.get(track, track), blinds=False)

    def _restyle(self, reason: str, blinds: bool) -> Decision:
        """The look changed under the room: move a lit room to it."""
        look = self._look()
        actions: list[Action] = []
        if blinds and self.config.blinds_with_periods and look.blinds:
            actions.append(MoveBlinds(look.blinds))
        if self.state is State.OWNED and not look.nothing:
            lit, factor = self._to_apply(look)
            actions.append(ApplyLook(lit, self._power_for(lit), self.config.drift, factor))
            reason += f": drifting to the {self._look_name()}"
        return self._decide(reason, *actions)

    def set_stealth(self, on: bool, now: datetime) -> Decision:
        self.stealth = on
        if on:
            return self._check_timer(now) or self._decide("stealth mode: motion ignored")
        if self.state is State.IDLE:
            seen = [s for s in self.config.triggers if self.sensors.get(s, False)]
            if seen:
                return self._switch_on(seen[0], now)
        return self._check_timer(now) or self._decide(f"stealth mode off: {self.state.value}")

    def tick(self, now: datetime) -> Decision:
        if self.state is not State.OWNED or self.deadline is None or now < self.deadline:
            return self._unchanged()
        if self.occupied():
            self.deadline = None
            return self._decide("owned: occupied")
        self.state = State.IDLE
        self.deadline = None
        self.owned_at = None
        self.ambient.lights_changed(False, now)
        fade = self.config.fade_out if self.config.fade_out > timedelta(0) else None
        return self._decide("empty for the timeout: lights off", TurnOff(self.config.switchable(), fade))
