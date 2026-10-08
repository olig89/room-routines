"""One room's behaviour: events in, actions out.

The room never talks to Home Assistant. The integration feeds it events
(a sensor changed, a light changed, a lux reading, the period changed, time
passed) and carries out the actions it returns. Every decision carries a
plain-language reason, which becomes the room's status and log line.

States:

- **idle**: lights off (as far as the room knows). Motion may switch them on.
- **owned**: the routine is running: the room switched the lights on (motion,
  a timer, a starter, the switch_on action) and follows the day with them. With
  sensors it switches them off once the room has been empty for the timeout;
  a room without sensors keeps them on until someone (or a timer) switches
  them off.
- **manual**: someone else switched a light on. The room does nothing until
  every light is off again. A room set to ``on_by_hand = ROUTINE`` instead
  takes such lights over after a few seconds and runs its routine on them.

Following the day: a lit, owned room drifts to each new period's look and,
where the room asks for it, blends gradually into the next period's look over
the last minutes of a period (``blends``). As soon as someone changes the
lights by hand, the room stops following until the lights are switched off.

A room can have its own start times for some periods (``period_starts``: an
office whose evening starts after the working day), and timers that start or
stop the routine at set times (see ``timers``).

What else it listens to (see ``rules``): *starters* (start the routine when an
entity becomes, say, "on"), *only when* conditions that every way of starting
must meet, and *rules* that change how the room behaves while something holds
(do nothing, use another look, keep below a brightness). The integration works
out which hold and tells the room with ``set_context``.

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

Layers (see ``layers``): a room with sensors runs *Someone's there*, one
without runs *Ambient*; a light changed by hand is held by *Hand* until the
lights are switched off. ``layer()`` says which one has the lights now.

After a restart the room can't see who switched its lights on. The integration
keeps ``memory()`` (was the routine running, was a hand change holding it) and
hands it back with ``restore``, so a routine that was running carries on.

Log-only mode lives in the integration, not here: it carries out no actions
and reports the room's own commands back as light changes, so the room runs on
what it *would* have done while the real lights follow the old wall-sensor
wiring.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum

from .blend import blend, blendable, fraction
from .layers import Layer
from .looks import LightTarget, Look, LookSource, resolve, scaled
from .lux import AmbientTracker, dark_enough
from .periods import Schedule
from .rules import CAP, LOOK, NOTHING, Condition, Rule
from .timers import Timer
from .tracks import DIM, NORMAL, TRACK_LABELS

LEAVE = "leave"
ROUTINE = "routine"
ADOPT_WAIT = timedelta(seconds=3)  # let a hand switch-on settle before taking over
BLEND_STEP = timedelta(minutes=2)  # how often a blending room is moved on
NO_LOOK = "the room has no look to switch on in any period: set one in its looks table"

SceneReader = Callable[[str], Mapping[str, LightTarget] | None]


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
    # Lights switched on some other way (a wall button, the app): LEAVE them, or
    # bring them to the routine's look and follow it (ROUTINE).
    on_by_hand: str = "leave"
    # Blend into the next period over the last N minutes of a period: {period: minutes}.
    blends: Mapping[str, float] = field(default_factory=dict)
    # The room's own start times for some periods: {period: time}. The house's otherwise.
    period_starts: Mapping[str, time] = field(default_factory=dict)
    timers: tuple[Timer, ...] = ()
    # Start the routine when one of these becomes true.
    starters: tuple[Condition, ...] = ()
    # Every start needs all of these.
    only_when: tuple[Condition, ...] = ()
    # The room's own "while X" rules (the house's come with the context).
    rules: tuple[Rule, ...] = ()

    def __post_init__(self) -> None:
        overlap = set(self.triggers) & set(self.holds)
        if overlap:
            raise ValueError(f"a sensor can't be both trigger and hold: {sorted(overlap)}")
        if not self.lights:
            raise ValueError("a room needs at least one light")
        if self.on_by_hand not in (LEAVE, ROUTINE):
            raise ValueError(f"unknown on_by_hand {self.on_by_hand!r}")
        if any(m < 0 for m in self.blends.values()):
            raise ValueError("blend minutes can't be negative")

    @property
    def has_sensors(self) -> bool:
        return bool(self.triggers or self.holds)

    @property
    def base_layer(self) -> Layer:
        """The layer the room's looks belong to: Someone's there with sensors, else Ambient."""
        return Layer.SOMEONE if self.has_sensors else Layer.AMBIENT

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
        scene_reader: SceneReader | None = None,
    ) -> None:
        self.config = config
        self.schedule = schedule
        self.period = period
        self.track = track
        self.auto_dim = auto_dim  # house-wide: Normal looks at this factor on Dark Days
        self.scene_reader = scene_reader
        self.owned_at: datetime | None = None
        self.ambient = ambient or AmbientTracker()
        if lights_on:
            self.ambient.lights_changed(True, now)
        # After a restart with lights on, the room can't know who switched them
        # on, so it never switches off what it didn't switch on.
        self.state = State.MANUAL if lights_on else State.IDLE
        self.sensors: dict[str, bool] = {}
        # Sensors with no real state yet (just after a restart): a picked-up room
        # doesn't count down on them until they report.
        self.unheard: set[str] = set()
        self.deadline: datetime | None = None
        self.cooldown_until: datetime | None = None
        self.stealth = stealth
        # Someone changed the lights the room switched on: it stops following the
        # day (period changes, blending) until the lights are switched off.
        self.paused = False
        # Lights switched on by hand in a ROUTINE room: taken over at this time.
        self.adopt_at: datetime | None = None
        self._last_blend: Look | None = None
        # From the integration (``set_context``): the rule in force, and why
        # starting isn't allowed right now (an "only when" not met).
        self.rule: Rule | None = None
        self.rule_text = ""
        self.unmet_text = ""
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
        rule = self.rule
        if rule is not None and rule.action == LOOK:
            if rule.scene:
                return LookSource(Look(scene=rule.scene), self.period, self.track)
            return resolve(rule.period or self.period, self.track, self.config.looks, self.config.dim_looks, self.schedule)
        return resolve(self.period, self.track, self.config.looks, self.config.dim_looks, self.schedule)

    def _cap(self, look: Look, factor: float) -> tuple[Look, float]:
        """Keep every light at or below a "no brighter than" rule's level. A light
        at its last brightness gets the level itself (there is no number to compare)."""
        rule = self.rule
        if rule is None or rule.action != CAP or look.nothing:
            return look, factor
        if look.scene:
            lights = self.scene_reader(look.scene) if self.scene_reader else None
            if lights is None:
                return look, factor  # can't read it: turned on as it is
            look = scaled(Look(dict(lights)), factor)
            factor = 1.0
        cap = float(rule.max_pct or 100)
        lights = {}
        for light, target in look.lights.items():
            if target.on and (target.brightness_pct is None or target.brightness_pct > cap):
                target = LightTarget(True, cap, target.color_temp_kelvin, target.rgb)
            lights[light] = target
        return Look(lights, look.blinds), factor

    def refusal(self) -> str | None:
        """Why the room may not start now, or None."""
        if self.rule is not None and self.rule.action == NOTHING:
            return self.rule_text or "a rule says do nothing"
        if self.unmet_text:
            return f"only when {self.unmet_text}"
        return None

    def _look(self) -> Look:
        return self.source().look

    def _factor_for(self, source: LookSource) -> float:
        if self.track != NORMAL and source.track == NORMAL:
            return self.auto_dim
        return 1.0

    def dim_factor(self) -> float:
        """Dark Day brightness for the look in use: on a Dark Day, a Normal look is
        turned down (or up)."""
        return self._factor_for(self.source())

    def _to_apply(self, look: Look) -> tuple[Look, float]:
        """The look as sent (motion never moves blinds), with auto-dim applied
        or, for a scene, passed on."""
        factor = self.dim_factor()
        if look.scene:
            return Look(scene=look.scene), factor
        return scaled(Look(look.lights), factor), 1.0

    def _as_lights(self, source: LookSource) -> Look | None:
        """A look as plain lights, ready to send (a scene read where possible)."""
        look = source.look
        if look.scene:
            lights = self.scene_reader(look.scene) if self.scene_reader else None
            if lights is None:
                return None
            look = Look(dict(lights))
        if not blendable(look):
            return None
        return scaled(Look(look.lights), self._factor_for(source))

    def blending(self, now: datetime) -> tuple[float, str, Look] | None:
        """(fraction, next period, blended look) while the room is blending, else None."""
        minutes = self.config.blends.get(self.period, 0)
        if not minutes or (self.rule is not None and self.rule.action == LOOK):
            return None
        at = self.schedule.current(now)
        if at.name != self.period:
            return None  # the period was chosen by hand: no blending
        f = fraction(now, at.next_start, minutes)
        if f is None:
            return None
        here = self._as_lights(self.source())
        there = self._as_lights(
            resolve(at.next_name, self.track, self.config.looks, self.config.dim_looks, self.schedule)
        )
        if here is None or there is None:
            return None
        return f, at.next_name, blend(here, there, f)

    def _target(self, now: datetime) -> tuple[Look, float, str]:
        """What to send now: (look, factor still to apply, its name for the log)."""
        b = self.blending(now)
        if b is not None:
            f, nxt, look = b
            look, _ = self._cap(look, 1.0)
            return look, 1.0, f"{self.period} look, blending into {nxt} ({round(f * 100)} %){self._rule_tail()}"
        look, factor = self._to_apply(self._look())
        look, factor = self._cap(look, factor)
        return look, factor, self._look_name()

    def _rule_tail(self) -> str:
        return f" ({self.rule_text})" if self.rule is not None and self.rule.action != NOTHING else ""

    def _look_name(self) -> str:
        rule = self.rule
        if rule is not None and rule.action == LOOK and rule.scene:
            return f"scene {rule.scene}{self._rule_tail()}"
        source = self.source()
        dim = source.track != NORMAL
        factor = self.dim_factor()
        auto = f" at {round(factor * 100)} %" if factor != 1.0 else ""
        period = source.period if rule is not None and rule.action == LOOK and source.period else self.period
        return f"{period} look{' for Dark Days' if dim else ''}{auto}{self._rule_tail()}"

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
        if self.state is not State.OWNED or not self.config.has_sensors:
            return None  # a room without sensors keeps its lights until switched off
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

    def _own(self, now: datetime) -> None:
        self.state = State.OWNED
        self.owned_at = now
        self.deadline = None
        self.paused = False
        self.adopt_at = None
        self._last_blend = None
        self.ambient.lights_changed(True, now)

    def _go_idle(self) -> None:
        self.state = State.IDLE
        self.deadline = None
        self.owned_at = None
        self.paused = False
        self.adopt_at = None
        self._last_blend = None

    def has_any_look(self) -> bool:
        """Whether any period gives the lights something to do, on either kind of day."""
        return any(
            not resolve(p, track, self.config.looks, self.config.dim_looks, self.schedule).look.nothing
            for track in (NORMAL, DIM)
            for p in self.schedule.order()
        )

    # -- layers --

    def layer(self) -> Layer | None:
        """Which layer has the room's lights now (None: they're off)."""
        if self.state is State.IDLE:
            return None
        if self.state is State.MANUAL or self.paused:
            return Layer.HAND
        return self.config.base_layer

    def memory(self) -> dict | None:
        """What to remember across a restart: only a running routine."""
        if self.state is not State.OWNED:
            return None
        return {"owned_at": (self.owned_at.isoformat() if self.owned_at else None), "paused": self.paused}

    def restore(self, owned_at: datetime | None, paused: bool, now: datetime) -> Decision:
        """After a restart: the routine was running these lights, so carry on.
        Only for a room whose lights were found on (``MANUAL``)."""
        if self.state is not State.MANUAL:
            return self._unchanged()
        self._own(now)
        self.owned_at = owned_at or now
        self.paused = paused
        decision = self._restyle("picked up again after a restart", blinds=False, now=now)
        if self.unheard:
            # A presence sensor that hasn't reported yet reads as "nobody": don't
            # switch off on someone sitting still. Its first report decides.
            return self._decide(f"{decision.reason}; waiting for the sensors to report", *decision.actions)
        countdown = self._check_timer(now)
        if countdown is not None:
            return self._decide(f"{decision.reason}; {countdown.reason}", *decision.actions, *countdown.actions)
        return decision

    # -- events --

    def sensor(self, entity: str, on: bool, now: datetime) -> Decision:
        was = self.sensors.get(entity, False)
        self.sensors[entity] = on
        self.unheard.discard(entity)
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
        refused = self.refusal()
        if refused:
            return self._decide(f"motion at {entity}, but {refused}")
        if self.cooldown_until is not None and now < self.cooldown_until:
            return self._decide("motion ignored: lights were just switched off by hand")
        ambient = self.ambient.ambient(now)
        if not dark_enough(ambient, self.config.threshold_lux):
            return self._decide(
                f"motion at {entity}, but too bright ({ambient:g} lux, switches on below "
                f"{self.config.threshold_lux:g})"
            )
        if self._look().nothing:
            if not self.has_any_look():
                return self._decide(f"motion at {entity}, but {NO_LOOK}")
            return self._decide(f"motion at {entity}: {self._look_name()} is 'do nothing'")
        look, factor, name = self._target(now)
        self._own(now)
        lux = "unknown" if ambient is None else f"{ambient:g} lux"
        return self._decide(
            f"motion at {entity}: {name} (ambient {lux})",
            ApplyLook(look, self._power_for(look), factor=factor),
        )

    def start(self, now: datetime, why: str) -> Decision:
        """Start the routine (a timer, a button, the switch_on action): the current
        look, then following the day, whatever the lights were doing."""
        refused = self.refusal()
        if refused:
            return self._decide(f"{why}, but {refused}")
        if self._look().nothing:
            if not self.has_any_look():
                return self._decide(f"{why}, but {NO_LOOK}")
            return self._decide(f"{why}: {self._look_name()} is 'do nothing'")
        look, factor, name = self._target(now)
        self._own(now)
        decision = self._decide(f"{why}: {name}", ApplyLook(look, self._power_for(look), factor=factor))
        countdown = self._check_timer(now)
        if countdown is not None:
            return Decision(decision.actions + countdown.actions, f"{decision.reason}; {countdown.reason}")
        return decision

    def stop(self, now: datetime, why: str) -> Decision:
        """Switch the room's lights off (a timer, the switch_off action)."""
        if self.state is State.IDLE:
            return self._decide(f"{why}: already off")
        self._go_idle()
        self.ambient.lights_changed(False, now)
        fade = self.config.fade_out if self.config.fade_out > timedelta(0) else None
        return self._decide(f"{why}: lights off", TurnOff(self.config.switchable(), fade))

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
                if self.config.on_by_hand == ROUTINE:
                    self.adopt_at = now + ADOPT_WAIT
                    return self._decide("switched on by hand: bringing it to the routine", WakeAt(self.adopt_at))
                return self._decide("manual: a light was switched on by hand")
            return self._unchanged()
        if any_on:
            if self.state is State.MANUAL:
                return self._unchanged()
            # Changed by hand: the room stops following the day until the lights go
            # off; a room with sensors still switches off once it is empty.
            self.paused = True
            tail = ", still switches off when empty" if self.config.has_sensors else ""
            self.last = Decision(
                (), f"adjusted by hand: following paused until the lights are switched off{tail}",
                self._hand_change(ADJUSTED, now),
            )
            return self.last
        change = self._hand_change(SWITCHED_OFF, now) if self.state is State.OWNED else None
        self._go_idle()
        self.cooldown_until = now + self.config.cooldown
        self.last = Decision(
            (), f"switched off by hand: motion ignored for {int(self.config.cooldown.total_seconds())} s", change
        )
        return self.last

    def lux(self, value: float, now: datetime) -> bool:
        return self.ambient.reading(value, now)

    def period_changed(self, period: str, now: datetime) -> Decision:
        self.period = period
        self._last_blend = None
        return self._restyle(f"period is now {period}", blinds=True, now=now)

    def track_changed(self, track: str, now: datetime) -> Decision:
        if track == self.track:
            return self._unchanged()
        self.track = track
        return self._restyle(TRACK_LABELS.get(track, track), blinds=False, now=now)

    def _restyle(self, reason: str, blinds: bool, now: datetime) -> Decision:
        """The look changed under the room: move a lit room to it."""
        look = self._look()
        actions: list[Action] = []
        if blinds and self.config.blinds_with_periods and look.blinds:
            actions.append(MoveBlinds(look.blinds))
        if self.state is State.OWNED and not look.nothing:
            if self.paused:
                reason += ": lights left as changed by hand"
            else:
                lit, factor, name = self._target(now)
                actions.append(ApplyLook(lit, self._power_for(lit), self.config.drift, factor))
                reason += f": drifting to the {name}"
        return self._decide(reason, *actions)

    def set_context(
        self, rule: Rule | None, rule_text: str, unmet_text: str, now: datetime
    ) -> Decision:
        """The rule in force and any unmet "only when", worked out by the integration
        from the house's states. A lit room moves to a changed look; an idle room
        whose start was refused switches on if a trigger still sees someone."""
        was_rule, was_text, was_refused = self.rule, self.rule_text, self.refusal()
        self.rule, self.rule_text, self.unmet_text = rule, rule_text if rule else "", unmet_text
        refused = self.refusal()
        if rule != was_rule:
            reason = rule_text if rule is not None else f"over: {was_text}"
            if self.state is State.OWNED:
                return self._restyle(reason, blinds=False, now=now)
            if self.state is State.IDLE and was_refused and not refused:
                seen = [t for t in self.config.triggers if self.sensors.get(t, False)]
                if seen and not self.stealth:
                    return self._switch_on(seen[0], now)
            return self._decide(f"{reason}: {self.state.value}")
        if self.state is State.IDLE and was_refused and not refused and not self.stealth:
            seen = [t for t in self.config.triggers if self.sensors.get(t, False)]
            if seen:
                return self._switch_on(seen[0], now)
        return self._unchanged()

    def blend_tick(self, now: datetime) -> Decision:
        """Move a lit, blending room one step on (called every ``BLEND_STEP``)."""
        if self.state is not State.OWNED or self.paused:
            return self._unchanged()
        b = self.blending(now)
        if b is None:
            return self._unchanged()
        f, nxt, look = b
        look, _ = self._cap(look, 1.0)
        if look == self._last_blend:
            return self._unchanged()
        self._last_blend = look
        return self._decide(
            f"{self.period} look, blending into {nxt} ({round(f * 100)} %){self._rule_tail()}",
            ApplyLook(look, self._power_for(look), BLEND_STEP),
        )

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
        if self.adopt_at is not None and now >= self.adopt_at:
            self.adopt_at = None
            if self.state is State.MANUAL:
                return self.start(now, "switched on by hand")
        if self.state is not State.OWNED or self.deadline is None or now < self.deadline:
            return self._unchanged()
        if self.occupied():
            self.deadline = None
            return self._decide("owned: occupied")
        self._go_idle()
        self.ambient.lights_changed(False, now)
        fade = self.config.fade_out if self.config.fade_out > timedelta(0) else None
        return self._decide("empty for the timeout: lights off", TurnOff(self.config.switchable(), fade))
