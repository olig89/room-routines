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

Layers (see ``layers``): the room's routine is *Ambient* (started by a timer,
a starter, the switch or lights switched on by hand; it follows the day until
it's stopped). Motion is *Someone's there*: in a room with nothing else running
it switches the lights on and off, as it always has. A room that also has
*Someone's there looks* (``someone_looks``) brightens over a running routine:
the lights those looks name follow them while someone's there, the others stay
with the routine, and when the room empties they fade back to the routine
instead of off. Without such looks, motion in a running routine changes nothing.
A period without its own Someone's there look uses the room's look for that
period (Someone's there looks don't borrow from earlier periods).

Hand: a light changed by hand while the room runs its lights is left as it was
set; the room's other lights carry on. How long it's left alone is the room's
``hand_hold``: until the lights are switched off (the default), until the
routine moves on (a new period, a Dark Day change, a rule starting or ending,
someone arriving or the room emptying), or for ``hand_minutes``. Then it goes
back to what the room is doing. ``layer()`` says which layer is on top now.

A sensor listed in ``ends`` ends a visit the moment it goes off (a pantry door
closing): the lights go off at once, whatever the hold sensors say, without the
timeout or the fade.

Signals (see ``signals``) sit above all of that: while one holds a light, the
room leaves that light alone (a scene look is read and sent without it), and
when the signal ends the light goes back to what the room is doing, or to how
it was before the signal took it.

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
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from enum import Enum

from .blend import blend, blendable, fraction
from .layers import Layer
from .looks import LightTarget, Look, LookSource, resolve, scaled
from .lux import AmbientTracker, dark_enough
from .periods import Schedule
from .rules import CAP, LOOK, NOTHING, Condition, Rule
from .signals import Signal, held_by, targets_to
from .timers import Timer
from .tracks import DIM, NORMAL, TRACK_LABELS

LEAVE = "leave"
ROUTINE = "routine"
# How long a light changed by hand is left as it was set.
UNTIL_OFF = "until_off"
MOVES_ON = "moves_on"
FOR_MINUTES = "minutes"
HAND_HOLDS = (UNTIL_OFF, MOVES_ON, FOR_MINUTES)
ADOPT_WAIT = timedelta(seconds=3)  # let a hand switch-on settle before taking over
BLEND_STEP = timedelta(minutes=2)  # how often a blending room is moved on
# After a restart, how long a sensor that hasn't reported counts as "someone's there".
UNHEARD_FOR = timedelta(minutes=15)
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
    # Looks for while someone's there, over a running routine (and for motion
    # whenever the room has any). Empty: motion uses ``looks``.
    someone_looks: Mapping[str, Look] = field(default_factory=dict)
    someone_dim_looks: Mapping[str, Look] = field(default_factory=dict)
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
    # How long a light changed by hand is left as it was set: until the lights
    # go off, until the routine moves on, or for ``hand_minutes``.
    hand_hold: str = UNTIL_OFF
    hand_minutes: float = 30.0
    # Sensors that end a visit the moment they go off (a door closing).
    ends: tuple[str, ...] = ()
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
    # Signals: some lights show something while it's true (first in the list wins).
    signals: tuple[Signal, ...] = ()

    def __post_init__(self) -> None:
        overlap = set(self.triggers) & set(self.holds)
        if overlap:
            raise ValueError(f"a sensor can't be both trigger and hold: {sorted(overlap)}")
        if not self.lights:
            raise ValueError("a room needs at least one light")
        if self.on_by_hand not in (LEAVE, ROUTINE):
            raise ValueError(f"unknown on_by_hand {self.on_by_hand!r}")
        if self.hand_hold not in HAND_HOLDS:
            raise ValueError(f"unknown hand_hold {self.hand_hold!r}")
        if self.hand_minutes <= 0:
            raise ValueError("hand_minutes must be more than 0")
        stray_ends = set(self.ends) - set(self.triggers) - set(self.holds)
        if stray_ends:
            raise ValueError(f"only the room's own sensors can end a visit, not {sorted(stray_ends)}")
        if any(m < 0 for m in self.blends.values()):
            raise ValueError("blend minutes can't be negative")
        for signal in self.signals:
            stray = set(signal.lights) - set(self.lights)
            if stray:
                raise ValueError(f"a signal can only use the room's lights, not {sorted(stray)}")

    @property
    def has_sensors(self) -> bool:
        return bool(self.triggers or self.holds)

    @property
    def has_someone_looks(self) -> bool:
        return bool(self.someone_looks or self.someone_dim_looks)

    @property
    def runs_ambient(self) -> bool:
        """Whether starting the routine runs Ambient (until stopped) rather than acting
        like a visit: a room without sensors, or one with Someone's there looks and a
        timer or starter to run its routine."""
        return not self.has_sensors or (self.has_someone_looks and bool(self.timers or self.starters))

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
    # A signal's own command (never held back for a signal): flash once, a light effect.
    signal: bool = False
    flash: bool = False
    effect: str | None = None


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
        # Which layers are lit while OWNED: the routine (Ambient), motion (Someone's there).
        self.ambient_on = False
        self.someone_on = False
        # Lights Someone's there has lit over the routine: handed back (or off)
        # when it lets go, whatever period it is by then.
        self.someone_lit: set[str] = set()
        self.sensors: dict[str, bool] = {}
        # Sensors with no real state yet (just after a restart): a picked-up room
        # doesn't count down on them until they report.
        self.unheard: set[str] = set()
        # Lights a signal holds now, and how each was before the signal took it.
        self.held: dict[str, Signal] = {}
        self.before: dict[str, LightTarget] = {}
        self.unheard_until: datetime | None = None
        self.deadline: datetime | None = None
        self.cooldown_until: datetime | None = None
        self.stealth = stealth
        # Lights changed by hand while the room runs them: left as they were set
        # until each one's time (None: until the lights go off, or until the
        # routine moves on, by ``hand_hold``). The room's other lights carry on.
        self.hand: dict[str, datetime | None] = {}
        # Lights switched on by hand in a ROUTINE room: taken over at this time.
        self.adopt_at: datetime | None = None
        # A Lights out waiting for the room to empty, until this time (the next
        # period start). While it waits the room counts down on its own clock
        # (``lights_out_at``) and goes fully off, not back to its routine.
        self.lights_out_until: datetime | None = None
        self.lights_out_at: datetime | None = None
        self.lights_out_name = ""
        self._last_blend: Look | None = None
        # From the integration (``set_context``): the rule in force, and why
        # starting isn't allowed right now (an "only when" not met).
        self.rule: Rule | None = None
        self.rule_text = ""
        self.unmet_text = ""
        self.last = Decision(reason="manual: lights were on at start" if lights_on else "idle")

    # -- helpers --

    @property
    def paused(self) -> bool:
        """Every light the room switches is held by hand: nothing it does shows."""
        lights = self.config.switchable()
        return bool(self.hand) and all(light in self.hand for light in lights)

    def next_wake(self) -> datetime | None:
        """The earliest time ``tick`` has something to do."""
        times = [
            t for t in (self.deadline, self.adopt_at, self.unheard_until, self.lights_out_at, self.lights_out_until)
            if t is not None
        ]
        times += [until for until in self.hand.values() if until is not None]
        return min(times) if times else None

    def _hand_until(self, now: datetime) -> datetime | None:
        if self.config.hand_hold == FOR_MINUTES:
            return now + timedelta(minutes=self.config.hand_minutes)
        return None

    def _moves_on(self) -> list[str]:
        """The routine moves on: lights held "until it moves on" go back to it."""
        if self.config.hand_hold != MOVES_ON or not self.hand:
            return []
        released = list(self.hand)
        self.hand = {}
        return released

    def _hand_tail(self) -> str:
        if not self.hand or self.paused:
            return ""
        n = len(self.hand)
        return f" ({n} light{'s' if n != 1 else ''} left as changed by hand)"

    def _any(self, sensors: tuple[str, ...]) -> bool:
        return any(self.sensors.get(s, False) for s in sensors)

    def occupied(self) -> bool:
        if self.stealth:
            return False
        if self._any(self.config.triggers):
            return True
        if self.someone_on and self.unheard_until is not None and self.unheard & (
            set(self.config.triggers) | set(self.config.holds)
        ):
            return True  # just after a restart: a sensor that hasn't reported yet
        return self.someone_on and self._any(self.config.holds)

    def someone_seen(self) -> bool:
        """Whether any sensor sees someone now. For Lights out a presence sensor counts
        even when it didn't switch the lights on (someone sitting in a lit bathroom)."""
        if self.stealth:
            return False
        sensors = set(self.config.triggers) | set(self.config.holds)
        if self.unheard_until is not None and self.unheard & sensors:
            return True  # a sensor that hasn't reported since the restart may see someone
        return self._any(self.config.triggers) or self._any(self.config.holds)

    def _tables(self, layer: Layer) -> tuple[Mapping[str, Look], Mapping[str, Look]]:
        """The looks a layer uses: Someone's there its own, when the room has any."""
        if layer is Layer.SOMEONE and self.config.has_someone_looks:
            return self.config.someone_looks, self.config.someone_dim_looks
        return self.config.looks, self.config.dim_looks

    def _top(self) -> Layer:
        """The layer whose look shows now (or would, for an idle room)."""
        if self.someone_on:
            return Layer.SOMEONE
        if self.ambient_on:
            return Layer.AMBIENT
        return self.config.base_layer

    def _someone_source(self, period: str) -> LookSource | None:
        """The period's own Someone's there look, if it has one (no borrowing)."""
        if self.track != NORMAL and period in self.config.someone_dim_looks:
            return LookSource(self.config.someone_dim_looks[period], period, self.track)
        if period in self.config.someone_looks:
            return LookSource(self.config.someone_looks[period], period, NORMAL)
        return None

    def _resolve(self, layer: Layer, period: str) -> LookSource:
        if layer is Layer.SOMEONE and (own := self._someone_source(period)) is not None:
            return own
        return resolve(period, self.track, self.config.looks, self.config.dim_looks, self.schedule)

    def _boosting(self) -> bool:
        """Someone's there over a running routine: two looks at once."""
        return self.ambient_on and self.someone_on and self._someone_source(self.period) is not None

    def source(self, layer: Layer | None = None) -> LookSource:
        layer = layer or self._top()
        rule = self.rule
        if rule is not None and rule.action == LOOK:
            if rule.scene:
                return LookSource(Look(scene=rule.scene), self.period, self.track)
            return self._resolve(layer, rule.period or self.period)
        return self._resolve(layer, self.period)

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

    def _look(self, layer: Layer | None = None) -> Look:
        return self.source(layer).look

    def _factor_for(self, source: LookSource) -> float:
        if self.track != NORMAL and source.track == NORMAL:
            return self.auto_dim
        return 1.0

    def dim_factor(self, layer: Layer | None = None) -> float:
        """Dark Day brightness for the look in use: on a Dark Day, a Normal look is
        turned down (or up)."""
        return self._factor_for(self.source(layer))

    def _to_apply(self, look: Look, layer: Layer | None = None) -> tuple[Look, float]:
        """The look as sent (motion never moves blinds), with auto-dim applied
        or, for a scene, passed on."""
        factor = self.dim_factor(layer)
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

    def blending(self, now: datetime, layer: Layer | None = None) -> tuple[float, str, Look] | None:
        """(fraction, next period, blended look) while the room is blending, else None.
        Blending belongs to the routine when it's running, else to the layer on top."""
        if layer is None:
            layer = Layer.AMBIENT if self.ambient_on else self._top()
        minutes = self.config.blends.get(self.period, 0)
        if not minutes or (self.rule is not None and self.rule.action == LOOK):
            return None
        at = self.schedule.current(now)
        if at.name != self.period:
            return None  # the period was chosen by hand: no blending
        f = fraction(now, at.next_start, minutes)
        if f is None:
            return None
        here = self._as_lights(self.source(layer))
        there = self._as_lights(self._resolve(layer, at.next_name))
        if here is None or there is None:
            return None
        return f, at.next_name, blend(here, there, f)

    def _layer_target(self, layer: Layer, now: datetime, blending: bool = True) -> tuple[Look, float, str]:
        """One layer's look as it should be sent now, blending included."""
        b = self.blending(now, layer) if blending else None
        if b is not None:
            f, nxt, look = b
            return look, 1.0, f"{self.period} look, blending into {nxt} ({round(f * 100)} %)"
        look, factor = self._to_apply(self._look(layer), layer)
        return look, factor, self._look_name(layer, tail=False)

    def _note_someone_lit(self) -> None:
        """Remember the lights this period's Someone's there look lights over the routine."""
        if self._boosting():
            lit = self._someone_lit()
            self.someone_lit |= lit if lit is not None else set(self.config.switchable())

    def _handover(self, look: Look | None, factor: float) -> tuple[str, ...]:
        """Someone's there lets go of the lights it lit: those the routine's look
        (None: "do nothing") doesn't light have nothing below them, so they go off."""
        if look is None:
            kept: set[str] = set()
        else:
            plain = self._plain(look, factor)
            kept = set(plain.lit()) if plain is not None else set(self.config.switchable())
        out = tuple(l for l in self.config.switchable() if l in self.someone_lit and l not in kept)
        # A light held by hand keeps that note: when its hold ends it goes off then.
        self.someone_lit = {l for l in self.someone_lit if l in self.hand}
        return out

    def _someone_lit(self) -> set[str] | None:
        """The lights this period's Someone's there look lights (None: can't tell)."""
        own = self._someone_source(self.period)
        if own is None or own.look.nothing:
            return set()
        plain = self._plain(own.look, 1.0)
        return set(plain.lit()) if plain is not None else None

    def _plain(self, look: Look, factor: float) -> Look | None:
        """A look as lights with its factor applied (a scene read), or None if unreadable."""
        if look.scene:
            lights = self.scene_reader(look.scene) if self.scene_reader else None
            if lights is None:
                return None
            look = Look(dict(lights))
        return scaled(Look(look.lights), factor)

    def _target(self, now: datetime) -> tuple[Look, float, str]:
        """What to send now: (look, factor still to apply, its name for the log).
        Over a running routine, the lights Someone's there names follow it and the
        others stay with the routine."""
        if self._boosting():
            base, bf, bname = self._layer_target(Layer.AMBIENT, now)
            top, tf, tname = self._layer_target(Layer.SOMEONE, now, blending=False)  # the routine blends, not this
            if top.nothing:
                look, factor = self._cap(base, bf)
                return look, factor, f"{bname}{self._rule_tail()}"
            if not base.nothing:
                under, over = self._plain(base, bf), self._plain(top, tf)
                if under is not None and over is not None:
                    look, _ = self._cap(Look({**under.lights, **over.lights}), 1.0)
                    return look, 1.0, f"someone's there: {tname} over the {bname}{self._rule_tail()}"
            # The routine is dark, or a scene can't be read: Someone's there's look whole.
            look, factor = self._cap(top, tf)
            return look, factor, f"someone's there: {tname}{self._rule_tail()}"
        look, factor, name = self._layer_target(self._top(), now)
        look, factor = self._cap(look, factor)
        return look, factor, f"{name}{self._rule_tail()}"

    def _rule_tail(self) -> str:
        return f" ({self.rule_text})" if self.rule is not None and self.rule.action != NOTHING else ""

    def _look_name(self, layer: Layer | None = None, tail: bool = True) -> str:
        rule = self.rule
        end = self._rule_tail() if tail else ""
        if rule is not None and rule.action == LOOK and rule.scene:
            return f"scene {rule.scene}{end}"
        source = self.source(layer)
        dim = source.track != NORMAL
        factor = self.dim_factor(layer)
        auto = f" at {round(factor * 100)} %" if factor != 1.0 else ""
        period = source.period if rule is not None and rule.action == LOOK and source.period else self.period
        return f"{period} look{' for Dark Days' if dim else ''}{auto}{end}"

    def _power_for(self, look: Look) -> tuple[str, ...]:
        return tuple(sorted({self.config.powered_by[b] for b in look.lit() if b in self.config.powered_by}))

    def _unchanged(self) -> Decision:
        """Nothing to do; the status keeps its last reason."""
        return Decision((), self.last.reason)

    def _decide(self, reason: str, *actions: Action) -> Decision:
        shielded: list[Action] = []
        for action in actions:
            shielded.extend(self._shield(action))
        self.last = Decision(tuple(shielded), reason)
        return self.last

    def _shield(self, action: Action) -> list[Action]:
        """Keep the room's own commands off the lights a signal holds."""
        blocked = set(self.held) | set(self.hand)
        if not blocked or not isinstance(action, (ApplyLook, TurnOff)):
            return [action]
        if isinstance(action, TurnOff):
            lights = tuple(light for light in action.lights if light not in blocked)
            return [TurnOff(lights, action.transition)] if lights else []
        if action.signal:
            return [action]  # a signal is above everything, a hand change included
        look = action.look
        if look.scene:
            lights = self.scene_reader(look.scene) if self.scene_reader else None
            if lights is None:
                # A scene that can't be read (another app's): turn it on, then put
                # the signals back on the lights it may have changed.
                if self.hand:
                    return []  # it would change lights left as set by hand: leave the room as it is
                return [action, *self._signal_actions(list(self.held), flash=False)] if self.held else [action]
            look = scaled(Look(dict(lights)), action.factor)
            action = replace(action, factor=1.0)
        kept = {light: t for light, t in look.lights.items() if light not in blocked}
        if not kept:
            return []
        return [replace(action, look=Look(kept, look.blinds))]

    def _signal_actions(self, lights: list[str], flash: bool = True) -> list[Action]:
        """Each signal's look on the given lights it holds, one command per signal."""
        groups: dict[int, tuple[Signal, list[str]]] = {}
        for light in lights:
            signal = self.held[light]
            groups.setdefault(id(signal), (signal, []))[1].append(light)
        return [
            ApplyLook(Look({l: signal.lights[l] for l in ls}), signal=True,
                      flash=flash and signal.flash, effect=signal.effect)
            for signal, ls in groups.values()
        ]

    def set_signals(
        self,
        states: Mapping[str, str | None],
        current: Mapping[str, LightTarget],
        now: datetime,
        flash: bool = True,
    ) -> Decision:
        """The entities signals read changed: take, change or give back lights.
        ``current`` is how the lights are now (what to go back to later). An entity
        that's unavailable or unknown changes nothing (a blip isn't the end of a
        call); a missing entity counts as false. ``flash`` False when the signal
        isn't new (start-up, an entity reporting in late)."""
        unsure = {s.when.entity for s in self.config.signals if states.get(s.when.entity) in ("unavailable", "unknown")}
        new = held_by(self.config.signals, states, self.config.lights)
        for light, signal in self.held.items():
            if light not in new and signal.when.entity in unsure:
                new[light] = signal
        old = self.held
        # Lights remembered from before a restart that no signal holds any more.
        stale = [
            light for light in self.before
            if light not in old and light not in new
            and not any(light in s.lights and s.when.entity in unsure for s in self.config.signals)
        ]
        if new == self.held and not stale:
            return self._unchanged()
        changed = [light for light in new if old.get(light) != new[light]]
        released = [light for light in old if light not in new] + stale
        for light in changed:
            if light not in old and light not in self.before:
                self.before[light] = current.get(light, LightTarget(False))
        back = self._fallback(released, now)
        self.held = new
        for light in released:
            self.before.pop(light, None)
        actions = self._signal_actions(changed, flash=flash)
        if back:
            effects = any(old[light].effect for light in released if light in old)
            actions.append(ApplyLook(Look(back), signal=True, effect="off" if effects else None))
        parts = []
        started = {new[light].name for light in changed}
        if started:
            parts.append("inform: " + ", ".join(sorted(started)))
        ended = {old[light].name for light in released if light in old}
        if ended:
            parts.append("inform over: " + ", ".join(sorted(ended)))
        return self._decide("; ".join(parts), *actions)

    def release_signals(self, now: datetime) -> Decision:
        """Give every held light back (the room is being switched to log-only or off)."""
        if not self.held:
            return self._unchanged()
        released = list(self.held)
        back = self._fallback(released, now)
        effects = any(self.held[light].effect for light in released)
        self.held = {}
        for light in released:
            self.before.pop(light, None)
        return self._decide("inform lights handed back", ApplyLook(Look(back), signal=True, effect="off" if effects else None))

    def held_switched_off(self, light: str) -> None:
        """Someone switched a signal's light off: it stays off when the signal ends."""
        if light in self.held:
            self.before[light] = LightTarget(False)

    def _fallback(self, lights: list[str], now: datetime) -> dict[str, LightTarget]:
        """Where released lights go: what the room is doing with them, else how
        they were before the signal took them (off if that isn't known)."""
        if not lights:
            return {}
        room: Mapping[str, LightTarget] = {}
        if self.state is State.OWNED and not self.paused:
            look, factor, _ = self._target(now)
            plain = None if look.nothing else self._plain(look, factor)
            room = plain.lights if plain is not None else {}
        return {
            light: (None if light in self.hand else room.get(light)) or self.before.get(light, LightTarget(False))
            for light in lights
        }

    def signal_status(self) -> list[dict]:
        """For the page: each signal holding lights now."""
        out: dict[str, list[str]] = {}
        for light, signal in self.held.items():
            out.setdefault(signal.name, []).append(light)
        return [{"name": name, "lights": lights} for name, lights in out.items()]

    def _check_timer(self, now: datetime) -> Decision | None:
        """Start or cancel the switch-off countdown for an owned room."""
        if self.lights_out_until is not None:
            return None  # Lights out is waiting: it keeps its own countdown
        if self.state is not State.OWNED or not self.someone_on:
            return None  # only motion counts down; a routine keeps its lights until stopped
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
        self.hand = {}
        self.adopt_at = None
        self._last_blend = None
        self._clear_lights_out()  # started on purpose (a timer, a button): Lights out lets go
        self.ambient.lights_changed(True, now)

    def _go_idle(self) -> None:
        self.someone_lit.clear()
        for light in self.held:
            self.before[light] = LightTarget(False)  # the room went off under the signal
        self.state = State.IDLE
        self.ambient_on = False
        self.someone_on = False
        self.deadline = None
        self.owned_at = None
        self.hand = {}
        self.adopt_at = None
        self._last_blend = None
        self._clear_lights_out()  # off: motion works as usual again

    def has_any_look(self) -> bool:
        """Whether any period gives the lights something to do, on either kind of day."""
        tables = [(self.config.looks, self.config.dim_looks)]
        if self.config.has_someone_looks:
            tables.append((self.config.someone_looks, self.config.someone_dim_looks))
        return any(
            not resolve(p, track, looks, dim, self.schedule).look.nothing
            for looks, dim in tables
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
        if self.someone_on:
            return Layer.SOMEONE
        if self.ambient_on:
            return Layer.AMBIENT
        return self.config.base_layer

    def memory(self) -> dict | None:
        """What to remember across a restart: a running routine, and how the lights
        a signal holds were before it."""
        out: dict = {}
        if self.state is State.OWNED:
            out = {
                "owned_at": (self.owned_at.isoformat() if self.owned_at else None),
                "paused": self.paused,
                "hand": {light: (until.isoformat() if until else None) for light, until in self.hand.items()},
                "ambient": self.ambient_on,
                "someone": self.someone_on,
            }
        if self.before:
            # How signal-held lights were before, so a restart can still give them back.
            out["signal_before"] = targets_to(self.before)
        return out or None

    def restore(
        self,
        owned_at: datetime | None,
        paused: bool,
        now: datetime,
        ambient: bool | None = None,
        someone: bool | None = None,
        hand: Mapping[str, datetime | None] | None = None,
    ) -> Decision:
        """After a restart: the routine was running these lights, so carry on.
        Only for a room whose lights were found on (``MANUAL``). Memory from before
        layers says neither: a room with sensors was lit by motion, else by its routine."""
        if self.state is not State.MANUAL:
            return self._unchanged()
        mark = (self.lights_out_until, self.lights_out_at, self.lights_out_name)
        self._own(now)
        self.lights_out_until, self.lights_out_at, self.lights_out_name = mark
        if ambient is None and someone is None:
            someone = self.config.has_sensors
            ambient = not someone
        self.ambient_on, self.someone_on = bool(ambient), bool(someone)
        self.owned_at = owned_at or now
        switchable = self.config.switchable()
        if hand is not None:
            # Lights still held by hand (one whose time ran out goes back to the routine).
            self.hand = {l: u for l, u in hand.items() if l in switchable and (u is None or u > now)}
        elif paused:
            self.hand = {light: None for light in switchable}  # memory from before per-light holds
        if self.config.hand_hold == FOR_MINUTES:
            # A hold by minutes with no time (old memory, an unreadable one): from now.
            self.hand = {l: u if u is not None else self._hand_until(now) for l, u in self.hand.items()}
        self._note_someone_lit()
        decision = self._restyle("picked up again after a restart", blinds=False, now=now, release=False)
        if self.unheard & (set(self.config.triggers) | set(self.config.holds)):
            # A presence sensor that hasn't reported yet reads as "nobody": until each
            # one reports (or UNHEARD_FOR passes), it counts as someone being there.
            self.unheard_until = now + UNHEARD_FOR
            return self._decide(
                f"{decision.reason}; waiting for the sensors to report",
                *decision.actions, WakeAt(self.unheard_until),
            )
        countdown = self._check_timer(now)
        if countdown is not None:
            decision = self._decide(f"{decision.reason}; {countdown.reason}", *decision.actions, *countdown.actions)
        if (wake := self.next_wake()) is not None:
            decision = self._decide(decision.reason, *decision.actions, WakeAt(wake))
        return decision

    # -- events --

    def sensor(self, entity: str, on: bool, now: datetime) -> Decision:
        was = self.sensors.get(entity, False)
        self.sensors[entity] = on
        self.unheard.discard(entity)
        is_trigger = entity in self.config.triggers
        waiting = self.lights_out_until is not None and self.state is not State.IDLE
        if entity in self.config.ends and was and not on and (
            waiting or (self.state is State.OWNED and self.someone_on)
        ):
            # This sensor ends a visit (a door closing): lights off at once.
            return self._empty(now, None, f"{entity} went off", f"{entity} went off: lights off at once")
        if (waiting := self._lights_out_check(now)) is not None:
            return waiting
        if self.stealth:
            return self._check_timer(now) or self._decide("stealth mode: motion ignored")
        if self.state is State.MANUAL:
            return self._decide("manual: a light was switched on by hand")
        if self.state is State.IDLE:
            if not (on and not was and is_trigger):
                return self._decide("idle")
            return self._switch_on(entity, now)
        if on and not was and is_trigger and not self.someone_on and self.ambient_on and self.lights_out_until is None:
            return self._boost(entity, now)
        return self._check_timer(now) or self._decide(
            "owned: occupied" if self.occupied() else "owned: empty, counting down"
        )

    def _boost(self, entity: str, now: datetime) -> Decision:
        """Someone arrives while the routine runs: brighten with Someone's there's looks."""
        if self._someone_source(self.period) is None:
            return self._unchanged()  # nothing to brighten with in this period: the routine carries on
        refused = self.refusal()
        if refused:
            return self._decide(f"motion at {entity}, but {refused}")
        if self.cooldown_until is not None and now < self.cooldown_until:
            return self._decide("motion ignored: lights were just switched off by hand")
        self.someone_on = True
        self.deadline = None
        self._moves_on()
        if self.paused:
            return self._decide(f"motion at {entity}: lights left as changed by hand")
        self._note_someone_lit()
        look, factor, name = self._target(now)
        return self._decide(f"motion at {entity}: {name}", ApplyLook(look, self._power_for(look), factor=factor))

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
        if self._look(Layer.SOMEONE).nothing:
            if not self.has_any_look():
                return self._decide(f"motion at {entity}, but {NO_LOOK}")
            return self._decide(f"motion at {entity}: {self._look_name(Layer.SOMEONE)} is 'do nothing'")
        self._own(now)
        self.someone_on = True
        look, factor, name = self._target(now)
        lux = "unknown" if ambient is None else f"{ambient:g} lux"
        return self._decide(
            f"motion at {entity}: {name} (ambient {lux})",
            ApplyLook(look, self._power_for(look), factor=factor),
        )

    def start(self, now: datetime, why: str, visit: bool = False) -> Decision:
        """Start the routine (a timer, a button, the switch_on action): the current
        look, then following the day, whatever the lights were doing. In a room with
        sensors and no Someone's there looks it's a visit, as it always was: it
        switches off once the room is empty."""
        refused = self.refusal()
        if refused:
            return self._decide(f"{why}, but {refused}")
        layer = Layer.AMBIENT if self.config.runs_ambient and not visit else Layer.SOMEONE
        if self._look(layer).nothing:
            if not self.has_any_look():
                return self._decide(f"{why}, but {NO_LOOK}")
            return self._decide(f"{why}: {self._look_name(layer)} is 'do nothing'")
        someone = self.someone_on and self.state is State.OWNED
        self._own(now)
        if layer is Layer.AMBIENT:
            self.ambient_on, self.someone_on = True, someone
        else:
            self.someone_on = True
        self._note_someone_lit()  # a visit the routine starts under: its lights are Someone's
        look, factor, name = self._target(now)
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

    # -- Lights out --

    def lights_out(self, now: datetime, until: datetime, name: str, all_now: bool = False) -> Decision:
        """A Lights out reaches the room: lights off now, or, if a sensor sees someone
        and it isn't "all off now", once the room is empty (until ``until``)."""
        if self.state is State.IDLE:
            self._clear_lights_out()
            return self._decide(f"{name}: already off")
        if self.unheard & (set(self.config.triggers) | set(self.config.holds)) and self.unheard_until is None:
            self.unheard_until = now + UNHEARD_FOR  # sensors not heard since a restart: wait for them
        if all_now or not self.someone_seen():
            return self._lights_out_off(now, f"{name}: lights off", self._fade())
        self.lights_out_until = until
        self.lights_out_name = name
        self.lights_out_at = None
        self.deadline = None  # the room now counts down on Lights out's clock
        return self._decide(f"{name}: someone's here, lights off once the room is empty", WakeAt(until))

    def wait_lights_out(self, now: datetime, until: datetime, name: str) -> Decision:
        """Carry on a Lights out that was waiting before a restart or reload: the room
        goes off once it is empty, whatever it looks like now."""
        if self.state is State.IDLE or now >= until:
            return self._unchanged()
        self.lights_out_until, self.lights_out_name, self.lights_out_at = until, name, None
        self.deadline = None
        if self.unheard & (set(self.config.triggers) | set(self.config.holds)) and self.unheard_until is None:
            self.unheard_until = now + UNHEARD_FOR
        decision = self._lights_out_check(now)
        wakes = [WakeAt(t) for t in (self.next_wake(),) if t is not None]
        if decision is None:
            return self._decide(f"{name}: still waiting for the room to empty", *wakes)
        return self._decide(decision.reason, *decision.actions, *wakes)

    def waiting_for(self) -> dict | None:
        """A Lights out waiting here, to remember across a restart."""
        if self.lights_out_until is None or self.state is State.IDLE:
            return None
        return {"name": self.lights_out_name, "until": self.lights_out_until}

    def _fade(self) -> timedelta | None:
        return self.config.fade_out if self.config.fade_out > timedelta(0) else None

    def _clear_lights_out(self) -> None:
        self.lights_out_until = None
        self.lights_out_at = None
        self.lights_out_name = ""

    def _lights_out_off(self, now: datetime, reason: str, fade: timedelta | None) -> Decision:
        self._go_idle()
        self.ambient.lights_changed(False, now)
        return self._decide(reason, TurnOff(self.config.switchable(), fade))

    def _lights_out_check(self, now: datetime) -> Decision | None:
        """While a Lights out waits for the room: count down once it's empty, and
        switch off at the end; let go when the next period starts."""
        if self.lights_out_until is None:
            return None
        name = self.lights_out_name or "Lights out"
        if self.state is State.IDLE:
            self._clear_lights_out()
            return None
        if now >= self.lights_out_until:
            self._clear_lights_out()
            countdown = self._check_timer(now)
            return self._decide(
                f"{name} over: a new period, the room carries on",
                *(countdown.actions if countdown is not None else ()),
            )
        if self.someone_seen():
            if self.lights_out_at is not None:
                self.lights_out_at = None
                return self._decide(f"{name}: someone's here again, waiting for the room to empty")
            return None
        if self.lights_out_at is None:
            self.lights_out_at = now + self.config.timeout
            return self._decide(
                f"{name}: empty, lights off in {int(self.config.timeout.total_seconds())} s",
                WakeAt(self.lights_out_at),
            )
        if now >= self.lights_out_at:
            return self._lights_out_off(now, f"{name}: the room is empty, lights off", self._fade())
        return None

    def _hand_change(self, kind: str, now: datetime) -> HandChange:
        since = self.owned_at or now
        return HandChange(kind, self.period, self.track, now - since)

    def lights(self, any_on: bool, own: bool, now: datetime, light: str | None = None) -> Decision:
        """A light in the room changed (``light``, if known). ``any_on`` is the room
        after the change."""
        self.ambient.lights_changed(any_on, now)
        if own:
            if not any_on and self.state is State.MANUAL:
                self._go_idle()  # switched off by something of the room's own (a Lights out)
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
            # Changed by hand: that light is left as it was set (the others carry on);
            # a room with sensors still switches off once it is empty.
            touched = [light] if light in self.config.switchable() else list(self.config.switchable())
            until = self._hand_until(now)
            for l in touched:
                self.hand[l] = until
            what = f"{touched[0]} changed by hand" if len(touched) == 1 else "changed by hand"
            if self.config.hand_hold == FOR_MINUTES:
                how = f"left as set for {self.config.hand_minutes:g} min"
            elif self.config.hand_hold == MOVES_ON:
                how = "left as set until the routine moves on"
            else:
                how = "left as set until the lights are switched off"
            tail = ", still switches off when empty" if self.someone_on and not self.ambient_on else ""
            others = "" if self.paused else "; the other lights carry on"
            self.last = Decision(
                (WakeAt(until),) if until is not None else (),
                f"{what}: {how}{others}{tail}",
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
        decision = self._restyle(f"period is now {period}", blinds=True, now=now)
        if self.lights_out_until is not None and now >= self.lights_out_until:
            over = self._lights_out_check(now)
            if over is not None:
                decision = self._decide(f"{decision.reason}; {over.reason}", *decision.actions, *over.actions)
        return decision

    def track_changed(self, track: str, now: datetime) -> Decision:
        if track == self.track:
            return self._unchanged()
        self.track = track
        return self._restyle(TRACK_LABELS.get(track, track), blinds=False, now=now)

    def _restyle(self, reason: str, blinds: bool, now: datetime, release: bool = True) -> Decision:
        """The look changed under the room: move a lit room to it. ``release`` False
        when nothing really moved on (picking up after a restart)."""
        if release and self._moves_on():
            reason += " (changes by hand let go)"
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
                reason += f": drifting to the {name}{self._hand_tail()}"
                if self._boosting():
                    self._note_someone_lit()
                elif self.someone_lit:
                    # This period has no Someone's there look: hand its lights back.
                    nothing = self._look(Layer.AMBIENT).nothing
                    off = self._handover(None if nothing else lit, factor)
                    if off:
                        actions.append(TurnOff(off, self.config.drift))
        elif self.state is State.OWNED and self.someone_lit and not self.paused:
            # Nothing to show now ("do nothing"): what Someone's there lit goes off.
            off = self._handover(None, 1.0)
            if off:
                actions.append(TurnOff(off, self.config.drift))
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
        if self.blending(now) is None:
            return self._unchanged()
        self._note_someone_lit()
        look, factor, name = self._target(now)  # Someone's there's lights stay over the blend
        if look == self._last_blend:
            return self._unchanged()
        self._last_blend = look
        return self._decide(name, ApplyLook(look, self._power_for(look), BLEND_STEP, factor))

    def set_stealth(self, on: bool, now: datetime) -> Decision:
        self.stealth = on
        if (waiting := self._lights_out_check(now)) is not None:
            return waiting
        if on:
            return self._check_timer(now) or self._decide("stealth mode: motion ignored")
        seen = [s for s in self.config.triggers if self.sensors.get(s, False)]
        if seen and self.state is State.IDLE:
            return self._switch_on(seen[0], now)
        if seen and self.state is State.OWNED and self.ambient_on and not self.someone_on:
            return self._boost(seen[0], now)
        return self._check_timer(now) or self._decide(f"stealth mode off: {self.state.value}")

    def tick(self, now: datetime) -> Decision:
        if self.unheard_until is not None and now >= self.unheard_until:
            # Sensors still silent: stop waiting for them and count down as usual.
            self.unheard.clear()
            self.unheard_until = None
            countdown = self._check_timer(now)
            if countdown is not None:
                return countdown
        if (waiting := self._lights_out_check(now)) is not None:
            return waiting
        if self.adopt_at is not None and now >= self.adopt_at:
            self.adopt_at = None
            if self.state is State.MANUAL:
                return self.start(now, "switched on by hand", visit=self.config.has_sensors)
        expired = [light for light, until in self.hand.items() if until is not None and now >= until]
        if expired:
            for light in expired:
                del self.hand[light]
            going_dark = self.deadline is not None and now >= self.deadline and not self.occupied()
            if self.state is State.OWNED and not going_dark:
                return self._rejoin(expired, now)
        if self.state is not State.OWNED or self.deadline is None or now < self.deadline:
            return self._unchanged()
        if self.occupied():
            self.deadline = None
            return self._decide("owned: occupied")
        fade = self.config.fade_out if self.config.fade_out > timedelta(0) else None
        return self._empty(now, fade, "empty", "empty for the timeout: lights off")

    def _rejoin(self, lights: list[str], now: datetime) -> Decision:
        """Lights held by hand go back to what the room is doing."""
        look, factor, name = self._target(now)
        # Lights Someone's there lit that the routine doesn't want go off now.
        theirs = [light for light in lights if light in self.someone_lit]
        if look.nothing:
            self.someone_lit -= set(theirs)
            if theirs:
                return self._decide(f"change by hand over: {name} is 'do nothing', so what was lit goes off",
                                    TurnOff(tuple(theirs), self.config.drift))
            return self._decide(f"change by hand over: {name} is 'do nothing', lights left as they are")
        plain = self._plain(look, factor)
        if plain is None:
            if self.hand:
                return self._decide(f"change by hand over, but the {name} is a scene that can't be read "
                                    "and other lights are still left as set: nothing changed")
            # A scene that can't be read: send it whole (the other lights already show it).
            return self._decide(f"change by hand over: back to the {name}",
                                ApplyLook(look, self._power_for(look), self.config.drift, factor))
        lit = set(plain.lit())
        off = tuple(light for light in theirs if light not in lit)
        self.someone_lit -= set(off)
        back = {light: t for light, t in plain.lights.items() if light in lights}
        actions: list[Action] = []
        if back:
            rejoin = Look(back)
            actions.append(ApplyLook(rejoin, self._power_for(rejoin), self.config.drift))
        if off:
            actions.append(TurnOff(off, self.config.drift))
        if not actions:
            return self._decide(f"change by hand over: the {name} leaves those lights alone")
        return self._decide(f"change by hand over: back to the {name}{self._hand_tail()}", *actions)

    def _empty(self, now: datetime, fade: timedelta | None, why: str, off_reason: str) -> Decision:
        """Nobody's there any more: back to the routine, or lights off."""
        if self.lights_out_until is not None:
            # A Lights out is waiting for exactly this: fully off, not back to the routine.
            return self._lights_out_off(now, f"{self.lights_out_name or 'Lights out'}: {off_reason}", fade)
        if self.ambient_on:
            # Someone's there lets go: the lights fall back to the routine.
            self.someone_on = False
            self.deadline = None
            self._last_blend = None
            self._moves_on()
            if self.paused:
                self.someone_lit.clear()
                return self._decide(f"{why}: the routine carries on, lights left as changed by hand")
            nothing = self._look(Layer.AMBIENT).nothing
            look, factor, name = self._target(now)
            off = self._handover(None if nothing else look, factor)
            actions: list[Action] = [] if nothing else [ApplyLook(look, self._power_for(look), fade, factor)]
            if off:
                actions.append(TurnOff(off, fade))  # nothing below wants them: off
            if nothing:
                return self._decide(f"{why}: {name} is 'do nothing', so what was lit goes off", *actions)
            return self._decide(f"{why}: back to the {name}{self._hand_tail()}", *actions)
        self._go_idle()
        self.ambient.lights_changed(False, now)
        return self._decide(off_reason, TurnOff(self.config.switchable(), fade))
