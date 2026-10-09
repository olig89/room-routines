"""Runs the rooms inside Home Assistant.

The core decides; this module listens and acts. For each room it:

- feeds sensor, light and lux changes into the core ``Room``;
- carries out the actions the room returns, but only in **live** mode;
- keeps the room's wake-up timer (the switch-off countdown);
- records every command it sends, so the room can tell its own light changes
  from a person's.

In **log-only** mode nothing is sent. The room then runs on its own belief about
its lights (it assumes its commands worked), and real light changes are not fed
in, so what it logs is what it would have done. **Off** ignores everything.

The house keeps the current period (with a timer for the next one), the
stealth switch and whether today is a Normal day or a Dark Day (from the
weather and/or a light sensor, see ``core/tracks.py``), and passes them to
every room. It also keeps the log of hand changes (in Home
Assistant's storage) that the page's suggestions come from, and runs the
Lights outs (see ``core/lights_out.py``): a room whose routine is live is told
and decides for itself; in a room whose routine is off or in log only, and for
lights in no room, the Lights out reads the sensors and switches the lights
itself.

A look can be a Home Assistant scene. Turning it on is Home Assistant's job;
the room reads the scene's settings where it can (scenes made in Home
Assistant) so it can recognise the result as its own and step-fade lights that
can't fade themselves. A scene it can't read (a Hue app scene) is turned on
as it is, and any change to the room's lights in the next few seconds counts as
the scene's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, LightEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import ANY_SIGNAL, DOMAIN, MODE_LIVE, MODE_LOG_ONLY, MODE_OFF, house_signal, room_signal
from .core.fade import plan_fade
from .core.layers import LAYER_IDS
from .core.signals import held_by, targets_from, targets_to
from .core.habits import Change, Suggestion, change_from, change_to, suggest
from .core.looks import OFF, LightTarget, Look, resolve, scaled
from .core.matching import Command, OwnChangeMatcher
from .core.periods import Schedule
from .core.room import (
    ADJUSTED,
    ApplyLook,
    Decision,
    HandChange,
    MoveBlinds,
    Room,
    RoomConfig,
    State,
    TurnOff,
    WakeAt,
)
from .core.serial import room_from, schedule_from, tracks_from
from .core.daylight import MIN_ELEVATION, DaylightReference
from .core.sun import SunPosition, clear_sky, position
from .core.room import BLEND_STEP
from .core.timers import ON as TIMER_ON
from .core.rules import Condition, Rule, active_rule, describe_condition, describe_rule, first_unmet, rule_from
from .core.timers import Timer, Today, describe, due
from .core.tracks import DIM, WEATHER_STALE, TrackChooser, default_periods
from .core.lights_out import (
    AT_ENTITY,
    AT_PERIOD,
    AT_TIME,
    MODE_LIVE as LIGHTS_OUT_LIVE,
    MODE_OFF as LIGHTS_OUT_OFF,
    LightsOut,
    describe as describe_lights_out,
    lights_out_from,
)

_LOGGER = logging.getLogger(__name__)

BULB_WAIT = timedelta(seconds=10)  # how long to wait for smart bulbs after powering their circuit
SETTLE = timedelta(seconds=10)  # after a hand change, when to read how the lights ended up
TRACK_CHECK = timedelta(minutes=1)
WEATHER_EVERY = timedelta(minutes=15)
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
LEARN_DAYS = 10  # Home Assistant keeps 5-minute statistics for about this long
KEEP_CHANGES = timedelta(days=60)
MAX_CHANGES = 2000
DISMISS_FOR = timedelta(days=28)
STORE_VERSION = 1
# After a restart, how long a room waits for its lights to come back before it
# stops trying to pick its routine up again (lights reporting late, as KNX can).
RESTORE_GRACE = timedelta(minutes=5)
# A routine remembered from longer ago than this counts as started at the restart
# (so habits don't read a days-long downtime as time with the lights on).
MAX_REMEMBERED_AGE = timedelta(days=1)
SCENE_PLATFORM = "homeassistant_scene"  # Home Assistant's own (YAML / editor) scenes
LIGHTS_OUT_RUNS = 20  # Lights out runs kept for the page
# What a Lights out does in a room: switch it off now, wait for it to empty, or nothing (already off).
OUT_OFF = "off"
OUT_WAITING = "waiting"
OUT_NOTHING = "nothing"


def brightness_pct(state) -> float | None:
    raw = state.attributes.get(ATTR_BRIGHTNESS)
    return None if raw is None else round(raw / 255 * 100, 1)


@dataclass
class RoomSetup:
    """A room as stored: the core config plus what only the HA side needs."""

    room_id: str
    config: RoomConfig
    area_id: str | None
    lux_sensor: str | None
    # Lights whose own device fades them (e.g. a DALI gateway with a switch-off
    # fade time): they get plain commands, never steps.
    self_fading: frozenset[str] = frozenset()
    start_mode: str = MODE_LOG_ONLY  # a new room's mode until its select remembers one
    # Every light-level sensor (the room is dark when any of them is); lux_sensor is the first.
    lux_sensors: tuple[str, ...] = ()


class RoomRunner:
    def __init__(self, house: House, setup: RoomSetup) -> None:
        self.house = house
        self.hass = house.hass
        self.room_id = setup.room_id
        self.config = setup.config
        self.area_id = setup.area_id
        self.lux_sensors = setup.lux_sensors or ((setup.lux_sensor,) if setup.lux_sensor else ())
        self.lux_sensor = self.lux_sensors[0] if self.lux_sensors else None
        self.self_fading = setup.self_fading
        self.start_mode = setup.start_mode
        self.mode = setup.start_mode  # the mode select restores the real value on start-up
        # The room's own clock: the house's periods, with any start times of its own.
        self.schedule = house.schedule.with_starts(dict(self.config.period_starts))
        self.matcher = OwnChangeMatcher()
        self.room: Room | None = None
        self.status_entity_id: str | None = None
        self.mode_entity_id: str | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._wake: CALLBACK_TYPE | None = None
        self._wake_at: datetime | None = None
        self._fades: list[CALLBACK_TYPE] = []
        # Bumped for a light when a new command for it stops its fade.
        self._fade_gen: dict[str, int] = {}
        # Brightness each light had before a stepped fade-out. A light that
        # remembers its last level (a KNX DALI light) would otherwise come back
        # at the dimmed level the fade left it at.
        self._before_fade: dict[str, float] = {}
        self._pending_change: CALLBACK_TYPE | None = None
        self._own_period: CALLBACK_TYPE | None = None
        self._warned_scenes: set[str] = set()
        # What the room was doing before a restart (see ``Room.memory``), until used.
        self._restore: dict[str, Any] | None = None
        self._restore_until: datetime | None = None
        self._memory_read = False
        self._room_mode: str | None = None  # the mode the current core room was built for
        self._grace_end: CALLBACK_TYPE | None = None
        # Lights a signal left with an effect while switching them off: the next
        # switch-on clears it.
        self._effect_left: set[str] = set()
        # A Lights out waiting in a room whose routine isn't live (off, or log only):
        # it watches the sensors itself and switches the real lights. {name, until, at}
        self._plain_out: dict[str, Any] | None = None
        self._plain_out_unsubs: list[CALLBACK_TYPE] = []
        self._plain_out_timer: CALLBACK_TYPE | None = None
        # The room's own rules first, then the house's that cover it.
        self.rules: tuple[Rule, ...] = tuple(self.config.rules) + tuple(
            r for r in house.house_rules if r.applies_to(self.room_id)
        )

    # -- life cycle --

    @callback
    def start(self) -> None:
        if self.room is None or self._room_mode != self.mode:
            self._build()  # the mode select may already have built it
        sensors = list(self.config.triggers) + list(self.config.holds)
        if sensors:
            self._unsubs.append(async_track_state_change_event(self.hass, sensors, self._on_sensor))
        if self.config.period_starts:
            self._schedule_own_period()
        if self.config.blends:
            self._unsubs.append(async_track_time_interval(self.hass, self._blend_tick, BLEND_STEP))
        for timer in self.config.timers:
            self._unsubs.append(
                async_track_time_change(
                    self.hass, self._timer_callback(timer), hour=timer.at.hour, minute=timer.at.minute, second=0
                )
            )
        watched = self.context_entities()
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_context))
        self._unsubs.append(async_track_state_change_event(self.hass, list(self.config.lights), self._on_light))
        if self.lux_sensors:
            self._unsubs.append(async_track_state_change_event(self.hass, list(self.lux_sensors), self._on_lux))

    @callback
    def stop(self) -> None:
        self._cancel_timers()
        self._stop_plain_out()
        if self._grace_end:
            self._grace_end()
            self._grace_end = None
        while self._unsubs:
            self._unsubs.pop()()

    def _cancel_timers(self) -> None:
        if self._wake:
            self._wake()
            self._wake = None
        self._wake_at = None
        if self._own_period:
            self._own_period()
            self._own_period = None
        if self._pending_change:
            self._pending_change()
            self._pending_change = None
        self._cancel_fades()

    def _cancel_fades(self, lights: tuple[str, ...] | None = None) -> None:
        """Stop fades under way: every light's, or only these lights'."""
        if lights is None:
            while self._fades:
                self._fades.pop()()
            return
        for light in lights:
            self._fade_gen[light] = self._fade_gen.get(light, 0) + 1

    def _lights_of(self, action: ApplyLook | TurnOff) -> tuple[str, ...] | None:
        """The lights a command changes (None: can't tell, a scene from another app)."""
        if isinstance(action, TurnOff):
            return action.lights
        if action.look.scene:
            return None
        return tuple(action.look.lights)

    def _build(self) -> None:
        """A fresh core room, from the lights and sensors as they are now."""
        self._cancel_timers()
        now = dt_util.now()
        if not self._memory_read:
            # The first build (the mode select can get here before ``start``).
            self._memory_read = True
            self._restore = self.house.room_memory.get(self.room_id)
            self._restore_until = now + RESTORE_GRACE if self._restore else None
            if self._restore:
                self._grace_end = async_call_later(
                    self.hass, RESTORE_GRACE.total_seconds() + 1, self._grace_over
                )
        self._room_mode = self.mode
        # Lights a signal holds don't make the room "on" (a call light at start-up).
        signalled = set(held_by(self.config.signals, self._states(), self.config.lights))
        lights_on = self.mode == MODE_LIVE and self._any_on(signalled)
        self.room = Room(
            self.config, self.schedule, self.period_now(now), lights_on, now,
            stealth=self.house.stealth, track=self.house.track, auto_dim=self.house.tracks.factor,
            scene_reader=self._read_scene,
        )
        for sensor in (*self.config.triggers, *self.config.holds):
            state = self.hass.states.get(sensor)
            self.room.sensors[sensor] = state is not None and state.state == STATE_ON
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self.room.unheard.add(sensor)
        rule, rule_text, unmet_text = self._context(self._states())
        self.room.rule, self.room.rule_text, self.room.unmet_text = rule, rule_text, unmet_text
        self._offer_lux(now)
        if self._restore and self._restore.get("signal_before"):
            self.room.before.update(targets_from(self._restore["signal_before"]))
        elif (kept := self.house.room_memory.get(self.room_id)) and kept.get("signal_before"):
            self.room.before.update(targets_from(kept["signal_before"]))
        if self.mode != MODE_OFF and self.config.signals:
            self._run(self.room.set_signals(
                self._states(), capture(self.hass, self.config).lights, now, flash=False
            ))
        self._try_restore(now)
        self._persist()
        self._notify()

    @callback
    def _grace_over(self, _now: datetime) -> None:
        """Nothing picked the routine up in time: forget it and save what's true now."""
        self._grace_end = None
        if self._restore is not None:
            self._restore = None
            self._persist()

    def _try_restore(self, now: datetime) -> None:
        """Pick up a routine that was running before the restart, once the room is
        live and its lights are found on."""
        if self._restore is None or self.room is None or self.mode != MODE_LIVE:
            return
        if self._restore_until is not None and now > self._restore_until:
            self._restore = None
            return
        if "owned_at" not in self._restore:
            self._restore = None  # only signals' "before" was remembered: nothing to pick up
            return
        if self.room.state is not State.MANUAL:
            return  # lights not back yet (or off): keep waiting until the grace ends
        memory, self._restore = self._restore, None
        owned_at = dt_util.parse_datetime(memory.get("owned_at") or "")
        if owned_at is not None and now - owned_at > MAX_REMEMBERED_AGE:
            owned_at = now
        hand = memory.get("hand")
        if isinstance(hand, dict):
            hand = {light: (dt_util.parse_datetime(u) if u else None) for light, u in hand.items()}
        else:
            hand = None
        self._run(self.room.restore(
            owned_at, bool(memory.get("paused")), now, memory.get("ambient"), memory.get("someone"), hand
        ))

    def _persist(self) -> None:
        """Remember whether the routine is running, for the next restart."""
        if self._restore is not None:
            if self._restore_until is not None and dt_util.now() <= self._restore_until:
                # Still waiting to pick it up: keep what was remembered, with the
                # signals' "before" as it is now.
                kept = {k: v for k, v in self._restore.items() if k != "signal_before"}
                if self.room is not None and self.room.before:
                    kept["signal_before"] = targets_to(self.room.before)
                self.house.remember(self.room_id, kept or None)
                return
            self._restore = None
        memory = self.room.memory() if self.room is not None and self.mode == MODE_LIVE else None
        self.house.remember(self.room_id, memory)

    def layer(self) -> str | None:
        """Which layer has the room's lights now, as an id (None: off)."""
        layer = self.room.layer() if self.room is not None and self.mode != MODE_OFF else None
        return LAYER_IDS[layer] if layer is not None else None

    @callback
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        if self.mode == MODE_LIVE and mode != MODE_LIVE and self.room is not None and self.room.held:
            self._run(self.room.release_signals(dt_util.now()))  # don't leave a light in a signal's colour
        self.mode = mode
        self._build()
        if self.config.period_starts:
            self._schedule_own_period()

    def _read_scene(self, scene: str):
        return scene_lights(self.hass, scene, self.config.switchable())

    @callback
    def update_config(self, config: RoomConfig) -> None:
        """New looks (or other settings that don't change what is listened to)."""
        self.config = config
        if self.room is not None:
            self.room.config = config
        self._notify()

    # -- events from Home Assistant --

    def _any_on(self, skip: set[str] | None = None) -> bool:
        if skip is None:
            skip = set(self.room.held) if self.room is not None else set()
        for light in self.config.switchable():
            if light in skip:
                continue
            state = self.hass.states.get(light)
            if state is not None and state.state == STATE_ON:
                return True
        return False

    @callback
    def _on_sensor(self, event: Event[EventStateChangedData]) -> None:
        if self._plain_out is not None:
            self._plain_out_check()
        if self.mode == MODE_OFF or self.room is None:
            return
        new = event.data["new_state"]
        on = new is not None and new.state == STATE_ON
        self._run(self.room.sensor(event.data["entity_id"], on, dt_util.now()))

    @callback
    def _on_light(self, event: Event[EventStateChangedData]) -> None:
        # Log-only runs on the room's own belief about its lights.
        if self.mode != MODE_LIVE or self.room is None:
            return
        entity = event.data["entity_id"]
        new = event.data["new_state"]
        if entity not in self.config.switchable() or new is None:
            return
        if entity in self.room.held:
            # A signal holds it: a change to it is the signal's, or shows over it until
            # the signal changes. Switched off by hand, it stays off afterwards.
            if new.state == STATE_OFF and not self.matcher.is_own(
                entity, dt_util.now(), False, None, context_id=new.context.id, parent_id=new.context.parent_id
            ):
                self.room.held_switched_off(entity)
                self._persist()
            return
        if new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        now = dt_util.now()
        on = new.state == STATE_ON
        own = self.matcher.is_own(
            entity, now, on, brightness_pct(new) if on else None,
            context_id=new.context.id, parent_id=new.context.parent_id,
        )
        if not own:
            self._before_fade.pop(entity, None)  # a person's level wins from now on
        self._run(self.room.lights(self._any_on(), own, now, entity))
        if self._restore is not None and on:
            # A light reporting in after the restart. KNX lights come back "off"
            # until their bus read answers, then "on", so any switch-on inside the
            # grace counts, whatever the state before it.
            self._try_restore(now)

    @callback
    def _on_lux(self, event: Event[EventStateChangedData]) -> None:
        self._offer_lux(dt_util.now())
        self._notify()

    def _offer_lux(self, now: datetime) -> None:
        """The darkest reading of the room's light sensors: dark enough anywhere
        (one end of a staircase) is dark enough for the room."""
        if self.room is None:
            return
        values = []
        for sensor in self.lux_sensors:
            state = self.hass.states.get(sensor)
            try:
                values.append(float(state.state))
            except (AttributeError, TypeError, ValueError):
                continue  # missing or unavailable: the others decide
        if values:
            self.room.lux(min(values), now)

    # -- what else the room listens to --

    def context_entities(self) -> list[str]:
        """Starters, "only when" conditions and rules: the entities they read."""
        found = [c.entity for c in self.config.starters]
        found += [c.entity for c in self.config.only_when]
        found += [r.when.entity for r in self.rules]
        found += [s.when.entity for s in self.config.signals]
        return sorted(set(found))

    def _states(self, override: Mapping[str, str | None] | None = None) -> dict[str, str | None]:
        states: dict[str, str | None] = {}
        for entity in self.context_entities():
            state = self.hass.states.get(entity)
            states[entity] = state.state if state is not None else None
        if override:
            states.update(override)
        return states

    def _name_of(self, entity: str) -> str:
        state = self.hass.states.get(entity)
        if state is not None and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        return entity

    def _context(self, states: Mapping[str, str | None]) -> tuple[Rule | None, str, str]:
        rule = active_rule(self.rules, states)
        rule_text = describe_rule(rule, self._name_of(rule.when.entity)) if rule else ""
        if rule is not None and rule.scene:
            rule_text = rule_text.replace(rule.scene, self._name_of(rule.scene))
        unmet = first_unmet(self.config.only_when, states)
        unmet_text = describe_condition(unmet, self._name_of(unmet.entity)) if unmet else ""
        return rule, rule_text, unmet_text

    @callback
    def _on_context(self, event: Event[EventStateChangedData]) -> None:
        if self.room is None or self.mode == MODE_OFF:
            return
        entity = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        now = dt_util.now()
        rule, rule_text, unmet_text = self._context(self._states())
        self._run(self.room.set_context(rule, rule_text, unmet_text, now))
        if self.config.signals:
            real_change = old is not None and old.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            self._run(self.room.set_signals(
                self._states(), capture(self.hass, self.config).lights, now, flash=real_change
            ))
        # A starter fires when it becomes true, not while it stays true.
        before = {entity: old.state if old is not None else None}
        after = {entity: new.state if new is not None else None}
        for starter in self.config.starters:
            if starter.entity == entity and starter.holds(after) and not starter.holds(before):
                self._run(self.room.start(now, describe_condition(starter, self._name_of(entity))))
                break

    def context_status(self) -> dict[str, Any]:
        """For the page: the rule in force and any unmet "only when"."""
        room = self.room
        return {
            "rule": room.rule_text if room is not None and room.rule is not None else None,
            "unmet": room.unmet_text if room is not None and room.unmet_text else None,
        }

    # -- from the house --

    def period_now(self, now: datetime) -> str:
        """The room's period: its own times, unless the house's was chosen by hand."""
        if self.house.overridden or not self.config.period_starts:
            return self.house.period
        return self.schedule.current(now).name

    def _schedule_own_period(self) -> None:
        if self._own_period:
            self._own_period()
        at = self.schedule.current(dt_util.now())
        self._own_period = async_track_point_in_time(self.hass, self._own_period_tick, at.next_start)

    @callback
    def _own_period_tick(self, _now: datetime) -> None:
        self._own_period = None
        self._schedule_own_period()
        self.sync_period()

    @callback
    def sync_period(self) -> None:
        """Bring the room to the period it should be in now."""
        if self.room is None:
            return
        period = self.period_now(dt_util.now())
        if period != self.room.period:
            self.period_changed(period)

    def next_change(self) -> datetime | None:
        if self.house.overridden or not self.config.period_starts:
            return self.house.next_start
        return self.schedule.current(dt_util.now()).next_start

    @callback
    def _blend_tick(self, now: datetime) -> None:
        if self.room is None or self.mode == MODE_OFF:
            return
        self._run(self.room.blend_tick(dt_util.now()))

    def _timer_callback(self, timer: Timer):
        @callback
        def _fire(_now: datetime) -> None:
            if self.room is None or self.mode == MODE_OFF:
                return
            now = dt_util.now()
            ok, why = due(timer, self.house.today(now, timer.only_home))
            what = f"timer {describe(timer)}"
            if not ok:
                self.hass.async_create_task(self._log(f"{what}: skipped, {why}"))
                return
            if timer.action == TIMER_ON:
                self._run(self.room.start(now, what))
            else:
                self._run(self.room.stop(now, what))

        return _fire

    @callback
    def start_routine(self, why: str) -> None:
        """Start the routine now (the switch_on action)."""
        if self.room is not None and self.mode != MODE_OFF:
            self._run(self.room.start(dt_util.now(), why))

    @callback
    def stop_routine(self, why: str) -> None:
        if self.room is not None and self.mode != MODE_OFF:
            self._run(self.room.stop(dt_util.now(), why))

    @callback
    def period_changed(self, period: str) -> None:
        if self.room is None:
            return
        if self.mode == MODE_OFF:
            self.room.period = period
            self._notify()
            return
        self._run(self.room.period_changed(period, dt_util.now()))

    @callback
    def track_changed(self, track: str) -> None:
        if self.room is None:
            return
        if self.mode == MODE_OFF:
            self.room.track = track
            self._notify()
            return
        self._run(self.room.track_changed(track, dt_util.now()))

    @callback
    def stealth_changed(self, on: bool) -> None:
        if self._plain_out is not None:
            self._plain_out_check()
        if self.room is None:
            return
        if self.mode == MODE_OFF:
            self.room.stealth = on
            self._notify()
            return
        self._run(self.room.set_stealth(on, dt_util.now()))

    # -- Lights out --

    def lit_lights(self) -> tuple[str, ...]:
        """The room's lights that are on now: never a power circuit, nor a light an Inform holds."""
        held = set(self.room.held) if self.room is not None and self.mode != MODE_OFF else set()
        out = []
        for light in self.config.switchable():
            state = self.hass.states.get(light)
            if light not in held and state is not None and state.state == STATE_ON:
                out.append(light)
        return tuple(out)

    def someone_seen(self) -> bool:
        """Whether any of the room's sensors sees someone, read from Home Assistant
        (stealth mode: nobody)."""
        if self.house.stealth:
            return False
        for sensor in (*self.config.triggers, *self.config.holds):
            state = self.hass.states.get(sensor)
            if state is not None and state.state == STATE_ON:
                return True
        return False

    def lights_out_status(self, all_now: bool) -> str:
        """What a Lights out would do here now."""
        if not self.lit_lights():
            return OUT_NOTHING
        if all_now or not self.someone_seen():
            return OUT_OFF
        return OUT_WAITING

    @callback
    def lights_out(self, name: str, until: datetime, all_now: bool) -> str:
        """A Lights out reaches the room. A live routine decides for itself; otherwise
        the Lights out switches the lights (and keeps a log-only room's belief in step)."""
        status = self.lights_out_status(all_now)
        now = dt_util.now()
        if self.mode == MODE_LIVE and self.room is not None:
            if self.room.state is State.IDLE:
                return OUT_NOTHING
            self._run(self.room.lights_out(now, until, name, all_now))
            return OUT_WAITING if self.room.lights_out_until is not None else OUT_OFF
        if self.mode == MODE_LOG_ONLY and self.room is not None and self.room.state is not State.IDLE:
            self._run(self.room.lights_out(now, until, name, all_now))
        if status == OUT_OFF:
            self._lights_out_now(name)
        elif status == OUT_WAITING:
            self._start_plain_out(name, until)
        return status

    def lights_out_waiting(self) -> dict[str, Any] | None:
        """A Lights out waiting for the room to empty, for the page."""
        if self._plain_out is not None:
            out = self._plain_out
        elif self.room is not None and self.mode != MODE_OFF and self.room.lights_out_until is not None:
            out = {"name": self.room.lights_out_name, "until": self.room.lights_out_until, "at": self.room.lights_out_at}
        else:
            return None
        return {"name": out["name"], "until": out["until"].isoformat(), "at": out["at"].isoformat() if out["at"] else None}

    def _lights_out_now(self, name: str) -> None:
        lights = self.lit_lights()
        if not lights:
            return
        fade = self.config.fade_out if self.config.fade_out > timedelta(0) else None
        self.hass.async_create_task(self._turn_off(lights, fade))
        self.hass.async_create_task(self._log(f"{name}: lights off (the room's routine isn't live, so {name} switched them)"))

    def _start_plain_out(self, name: str, until: datetime) -> None:
        self._stop_plain_out()
        self._plain_out = {"name": name, "until": until, "at": None}
        self._plain_out_unsubs.append(async_track_point_in_time(self.hass, self._plain_out_over, until))
        self.hass.async_create_task(self._log(f"{name}: someone's here, lights off once the room is empty"))
        self._plain_out_check()
        self._notify()

    def _stop_plain_out(self) -> None:
        while self._plain_out_unsubs:
            self._plain_out_unsubs.pop()()
        if self._plain_out_timer is not None:
            self._plain_out_timer()
            self._plain_out_timer = None
        self._plain_out = None

    @callback
    def _plain_out_over(self, _now: datetime) -> None:
        """The next period started: the Lights out lets go."""
        self._plain_out_unsubs.clear()  # this one has fired
        if self._plain_out is None:
            return
        name = self._plain_out["name"]
        self._stop_plain_out()
        self.hass.async_create_task(self._log(f"{name} over: a new period, the lights are left as they are"))
        self._notify()

    @callback
    def _plain_out_check(self) -> None:
        out = self._plain_out
        if out is None:
            return
        if not self.lit_lights():
            self._stop_plain_out()  # switched off some other way
            self._notify()
            return
        if self.someone_seen():
            if self._plain_out_timer is not None:
                self._plain_out_timer()
                self._plain_out_timer = None
                out["at"] = None
                self._notify()
            return
        if self._plain_out_timer is None:
            out["at"] = dt_util.now() + self.config.timeout
            self._plain_out_timer = async_call_later(
                self.hass, self.config.timeout.total_seconds(), self._plain_out_due
            )
            self._notify()

    @callback
    def _plain_out_due(self, _now: datetime) -> None:
        self._plain_out_timer = None
        out = self._plain_out
        if out is None:
            return
        out["at"] = None
        if self.someone_seen():
            return
        name = out["name"]
        self._stop_plain_out()
        self._lights_out_now(name)
        self._notify()

    # -- carrying out decisions --

    @callback
    def _tick(self, now: datetime) -> None:
        self._wake = None
        self._wake_at = None
        if self.room is not None and self.mode != MODE_OFF:
            self._run(self.room.tick(now))
            self._arm(None)  # whatever else the room still waits for

    def _arm(self, at: datetime | None) -> None:
        """Wake the room at the earliest thing it waits for (it keeps several: a
        countdown, a change by hand running out, sensors after a restart)."""
        times = [t for t in (at, self._wake_at, self.room.next_wake() if self.room else None) if t is not None]
        if not times:
            return
        first = min(times)
        if self._wake is not None and self._wake_at == first:
            return
        if self._wake:
            self._wake()
        self._wake_at = first
        self._wake = async_track_point_in_time(self.hass, self._tick, first)

    @callback
    def _run(self, decision: Decision) -> None:
        if decision.change is not None and self.mode == MODE_LIVE:
            self._note_change(decision.change)
        acting = False
        for action in decision.actions:
            if isinstance(action, WakeAt):
                self._arm(action.at)
                continue
            acting = True
            if self.mode != MODE_LIVE:
                continue
            if isinstance(action, (ApplyLook, TurnOff)) and not (isinstance(action, ApplyLook) and action.signal):
                # A new instruction replaces any fade still under way (someone
                # walking back in during a fade-out brings the lights back). A
                # signal's own command doesn't: the other lights carry on fading.
                self._cancel_fades(self._lights_of(action))
            if isinstance(action, ApplyLook):
                self.hass.async_create_task(self._apply(action))
            elif isinstance(action, TurnOff):
                self.hass.async_create_task(self._turn_off(action.lights, action.transition))
            elif isinstance(action, MoveBlinds):
                self.hass.async_create_task(self._move_blinds(action.positions))
        if acting:
            prefix = "" if self.mode == MODE_LIVE else "Log only: would act. "
            self.hass.async_create_task(self._log(prefix + decision.reason))
        self._arm(None)  # whatever the room now waits for (a hold by minutes after a restart)
        self._persist()
        self._notify()

    # -- hand changes --

    def _note_change(self, change: HandChange) -> None:
        """Remember a hand change. An adjustment is read once the lights settle."""
        if self._pending_change:
            self._pending_change()
            self._pending_change = None
        if change.kind != ADJUSTED:
            self.house.record_change(self._change(change, None))
            return

        @callback
        def _settled(_now) -> None:
            self._pending_change = None
            if self.room is None or self.room.state is not State.OWNED:
                return  # switched off meanwhile: that is recorded on its own
            lights = capture(self.hass, self.config).lights
            if any(t.on for t in lights.values()):
                self.house.record_change(self._change(change, lights))

        self._pending_change = async_call_later(self.hass, SETTLE.total_seconds(), _settled)

    def _change(self, change: HandChange, lights) -> Change:
        return Change(
            dt_util.now(), self.room_id, change.kind, change.period, change.track,
            change.after.total_seconds(), dict(lights) if lights is not None else None,
        )

    def look_of(self, period: str, track: str) -> Look | None:
        """A period's look as lights, for comparing with hand changes (None if unreadable)."""
        source = resolve(period, track, self.config.looks, self.config.dim_looks, self.schedule)
        look = source.look
        if look.scene:
            lights = scene_lights(self.hass, look.scene, self.config.switchable())
            if lights is None:
                return None
            look = Look(lights)
        if track != source.track:
            look = scaled(look, self.house.tracks.factor)
        return look

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, room_signal(self.house.entry.entry_id, self.room_id))
        async_dispatcher_send(self.hass, ANY_SIGNAL)

    async def _log(self, message: str) -> None:
        if "logbook" not in self.hass.config.components:
            return
        data: dict[str, Any] = {"name": self.config.name, "message": message}
        if self.status_entity_id:
            data[ATTR_ENTITY_ID] = self.status_entity_id
        await self.hass.services.async_call("logbook", "log", data)

    async def _send(
        self,
        light: str,
        target: LightTarget,
        transition: timedelta | None = None,
        flash: bool = False,
        effect: str | None = None,
    ) -> None:
        ctx = Context()
        pct = target.brightness_pct if target.on else None
        self.matcher.record(Command(light, ctx.id, dt_util.now(), target.on, pct))
        data: dict[str, Any] = {ATTR_ENTITY_ID: light}
        if transition:
            data["transition"] = transition.total_seconds()
        if not target.on:
            if effect == "off":
                self._effect_left.add(light)  # can't clear an effect while switching off
            await self.hass.services.async_call("light", "turn_off", data, context=ctx)
            return
        if target.brightness_pct is not None:
            data["brightness_pct"] = target.brightness_pct
        if target.color_temp_kelvin is not None:
            data["color_temp_kelvin"] = target.color_temp_kelvin
        if target.rgb is not None:
            data["rgb_color"] = list(target.rgb)
        state = self.hass.states.get(light)
        effects = (state.attributes.get("effect_list") or []) if state is not None else []
        if not effect and light in self._effect_left:
            effect = "off"
        if effect and effect in effects:
            data["effect"] = effect
        self._effect_left.discard(light)
        await self.hass.services.async_call("light", "turn_on", data, context=ctx)
        if flash:
            # After the colour, as its own command: some lights drop a colour sent with a flash.
            await self.hass.services.async_call(
                "light", "turn_on", {ATTR_ENTITY_ID: light, "flash": "short"}, context=ctx
            )

    def _supports_transition(self, light: str) -> bool:
        state = self.hass.states.get(light)
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) if state else 0
        return bool(features & LightEntityFeature.TRANSITION)

    def _how_to_fade(self, light: str, transition: timedelta | None) -> str:
        """'transition' (HA does it), 'plain' (no fade, or the light fades itself) or 'steps'."""
        if not transition:
            return "plain"
        if self._supports_transition(light):
            return "transition"
        if light in self.self_fading:
            return "plain"
        return "steps"

    async def _apply(self, action: ApplyLook) -> None:
        if action.look.scene:
            await self._apply_scene(action)
            return
        for circuit in action.power_first:
            state = self.hass.states.get(circuit)
            if state is None or state.state != STATE_ON:
                await self._send(circuit, LightTarget(True))
        if action.power_first:
            await self._wait_for([b for b in action.look.lit() if b in self.config.powered_by])
        fading: dict[str, LightTarget] = {}
        for light, target in action.look.lights.items():
            if self.hass.states.get(light) is None:
                continue
            if target.on and target.brightness_pct is None and light in self._before_fade:
                target = LightTarget(True, self._before_fade[light], target.color_temp_kelvin, target.rgb)
            self._before_fade.pop(light, None)
            how = self._how_to_fade(light, action.transition)
            if how == "steps":
                fading[light] = target
                continue
            await self._send(
                light, target, action.transition if how == "transition" else None,
                flash=action.flash, effect=action.effect,
            )
        if fading:
            self._fade(fading, action.transition)

    async def _apply_scene(self, action: ApplyLook) -> None:
        scene = action.look.scene
        if self.hass.states.get(scene) is None:
            _LOGGER.warning("%s: scene %s doesn't exist, so nothing was switched", self.config.name, scene)
            return
        lights = scene_lights(self.hass, scene, self.config.switchable())
        transition = action.transition
        if action.factor != 1.0:
            if lights:
                # Auto-dim: send the scene's lights turned down, light by light.
                await self._apply(ApplyLook(scaled(Look(lights), action.factor), action.power_first, transition))
                return
            if scene not in self._warned_scenes:
                self._warned_scenes.add(scene)
                _LOGGER.warning(
                    "%s: %s can't be read (a scene from another app, such as the Hue app), "
                    "so it is turned on as it is, not auto-dimmed",
                    self.config.name, scene,
                )
        if lights and transition and any(self._how_to_fade(l, transition) == "steps" for l in lights):
            # Some lights can't fade themselves: do the drift from the scene's
            # settings, light by light, as for any other look.
            await self._apply(ApplyLook(Look(lights), action.power_first, transition))
            return
        ctx = Context()
        now = dt_util.now()
        if lights is None:
            for light in self.config.switchable():
                self.matcher.record(Command(light, ctx.id, now, True, anything=True))
        else:
            for light, target in lights.items():
                self.matcher.record(
                    Command(light, ctx.id, now, target.on, target.brightness_pct if target.on else None)
                )
        data: dict[str, Any] = {ATTR_ENTITY_ID: scene}
        if transition:
            data["transition"] = transition.total_seconds()
        await self.hass.services.async_call("scene", "turn_on", data, context=ctx)

    def _fade(self, targets: Mapping[str, LightTarget], duration: timedelta) -> None:
        current: dict[str, float | None] = {}
        for light in targets:
            state = self.hass.states.get(light)
            on = state is not None and state.state == STATE_ON
            current[light] = (brightness_pct(state) or 100.0) if on else None
        gens = {light: self._fade_gen.get(light, 0) for light in targets}
        for offset, sends in plan_fade(current, Look(dict(targets)), duration):
            @callback
            def _step(_now, sends=sends) -> None:
                for light, target in sends.items():
                    if self._fade_gen.get(light, 0) != gens[light]:
                        continue  # a newer command for this light stopped its fade
                    if self.room is not None and (light in self.room.held or light in self.room.hand):
                        continue  # a signal took it, or someone changed it by hand
                    self.hass.async_create_task(self._send(light, target))

            self._fades.append(async_call_later(self.hass, offset.total_seconds(), _step))

    async def _wait_for(self, bulbs: list[str]) -> None:
        deadline = dt_util.now() + BULB_WAIT
        while dt_util.now() < deadline:
            states = [self.hass.states.get(b) for b in bulbs]
            if all(s is not None and s.state != STATE_UNAVAILABLE for s in states):
                return
            await asyncio.sleep(0.5)
        _LOGGER.warning("%s: bulbs still unavailable after powering their circuit: %s", self.config.name, bulbs)

    async def _turn_off(self, lights: tuple[str, ...], transition: timedelta | None) -> None:
        fading: dict[str, LightTarget] = {}
        for light in lights:
            state = self.hass.states.get(light)
            if state is None or state.state != STATE_ON:
                continue
            how = self._how_to_fade(light, transition)
            if how == "steps":
                fading[light] = OFF
                self._before_fade[light] = brightness_pct(state) or 100.0
                continue
            await self._send(light, OFF, transition if how == "transition" else None)
        if fading:
            self._fade(fading, transition)

    async def _move_blinds(self, positions: Mapping[str, int]) -> None:
        for cover, position in positions.items():
            await self.hass.services.async_call(
                "cover", "set_cover_position", {ATTR_ENTITY_ID: cover, "position": position},
                context=Context(),
            )


COLOUR_MODES = {"rgb", "rgbw", "rgbww", "hs", "xy"}


def target_of(state) -> LightTarget:
    """A light's state (live, or as stored in a scene) as a look's target."""
    if state.state != STATE_ON:
        return OFF
    mode = state.attributes.get("color_mode")
    kelvin = state.attributes.get("color_temp_kelvin")
    rgb = state.attributes.get("rgb_color")
    if mode is not None:
        kelvin = kelvin if mode == "color_temp" else None
        rgb = rgb if mode in COLOUR_MODES else None
    elif kelvin and rgb:
        rgb = None
    pct = brightness_pct(state)
    return LightTarget(
        True,
        pct if pct is None or pct > 0 else 1.0,
        int(kelvin) if kelvin else None,
        tuple(int(c) for c in rgb) if rgb else None,
    )


def capture(hass: HomeAssistant, config: RoomConfig) -> Look:
    """The room's lights as they are now, as a look."""
    lights: dict[str, LightTarget] = {}
    for light in config.switchable():
        state = hass.states.get(light)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        lights[light] = target_of(state)
    return Look(lights)


def scene_lights(hass: HomeAssistant, scene: str, lights) -> dict[str, LightTarget] | None:
    """What a Home Assistant scene sets the given lights to, or None if it can't be read.

    Only scenes made in Home Assistant (the scene editor or scenes.yaml) can be
    read; a scene from another integration (the Hue app's) can't.
    """
    platform = hass.data.get(SCENE_PLATFORM)
    entity = getattr(platform, "entities", {}).get(scene) if platform is not None else None
    states = getattr(getattr(entity, "scene_config", None), "states", None)
    if not isinstance(states, Mapping):
        return None
    return {light: target_of(states[light]) for light in lights if light in states}


def scene_entities(hass: HomeAssistant, lights) -> dict[str, dict[str, Any]]:
    """The lights as they are now, in the form a Home Assistant scene stores."""
    out: dict[str, dict[str, Any]] = {}
    for light in lights:
        state = hass.states.get(light)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        if state.state != STATE_ON:
            out[light] = {"state": "off"}
            continue
        entry: dict[str, Any] = {"state": "on"}
        for key in ("brightness", "color_mode", "color_temp_kelvin", "hs_color", "rgb_color", "xy_color", "effect"):
            value = state.attributes.get(key)
            if value is None:
                continue
            mode = state.attributes.get("color_mode")
            if key == "color_temp_kelvin" and mode != "color_temp":
                continue
            if key in ("hs_color", "rgb_color", "xy_color") and mode not in COLOUR_MODES:
                continue
            entry[key] = list(value) if isinstance(value, tuple) else value
        out[light] = entry
    return out


def lights_outs_from(options: Mapping[str, Any]) -> tuple[LightsOut, ...]:
    found = []
    for row in options.get("lights_outs") or []:
        try:
            found.append(lights_out_from(row))
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning("Ignoring a Lights out that can't be read (%s): %s", err, row)
    return tuple(found)


def rooms_from(options: Mapping[str, Any]) -> dict[str, RoomSetup]:
    rooms = {}
    for room_id, data in (options.get("rooms") or {}).items():
        rooms[room_id] = RoomSetup(
            room_id, room_from(data), data.get("area_id"), data.get("lux_sensor"),
            frozenset(data.get("self_fading") or ()),
            data.get("start_mode") if data.get("start_mode") in (MODE_OFF, MODE_LOG_ONLY, MODE_LIVE) else MODE_LOG_ONLY,
            tuple(data.get("lux_sensors") or ()),
        )
    return rooms


def structure(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options with every room's looks removed: if only looks changed,
    the house can take them without a reload (and without forgetting which
    lights it switched on)."""
    rooms = {
        room_id: {
            k: v for k, v in data.items()
            if k not in ("looks", "dim_looks", "someone_looks", "someone_dim_looks")
        }
        for room_id, data in (options.get("rooms") or {}).items()
    }
    return {**options, "rooms": rooms}


class House:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options = dict(entry.options)
        self.schedule: Schedule = schedule_from(self.options)
        self.period = self.schedule.current(dt_util.now()).name
        self.next_start: datetime | None = None
        self.overridden = False
        self.stealth = False
        self.period_entity_id: str | None = None
        self.stealth_entity_id: str | None = None
        self.track_entity_id: str | None = None
        self.tracks = tracks_from(self.options)
        self.chooser = TrackChooser(self.tracks)
        self.changes: list[Change] = []
        self.dismissed: dict[str, str] = {}  # suggestion key -> hidden until (ISO)
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.habits")
        self._daylight_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.daylight")
        # Rooms whose routine was running, so a restart can carry on (see Room.memory).
        self._rooms_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.rooms")
        self.room_memory: dict[str, dict[str, Any]] = {}
        self._learned: dict[str, datetime] = {}  # sensor -> statistics read up to
        self.house_rules: tuple[Rule, ...] = tuple(
            rule_from(r) for r in self.options.get("house_rules") or ()
        )
        self.weather: dict[str, Any] = {}  # the last weather answer, for the page
        self.weather_failed_at: datetime | None = None  # the last fetch that failed, if after the last answer
        self.rooms = {rid: RoomRunner(self, setup) for rid, setup in rooms_from(self.options).items()}
        self.lights_outs: tuple[LightsOut, ...] = lights_outs_from(self.options)
        self.skip_next = False  # skip the next Lights out (the switch turns itself off after one)
        self.skip_entity_id: str | None = None
        self.lights_out_runs: list[dict[str, Any]] = []
        self._unsub_period: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []

    @property
    def track(self) -> str:
        return self.chooser.track

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        for row in data.get("changes") or []:
            try:
                self.changes.append(change_from(row))
            except (KeyError, ValueError, TypeError):
                continue
        self.dismissed = dict(data.get("dismissed") or {})
        rooms = await self._rooms_store.async_load() or {}
        self.room_memory = {
            k: v for k, v in (rooms.get("rooms") or {}).items() if isinstance(v, dict) and k in self.rooms
        }
        daylight = await self._daylight_store.async_load() or {}
        for sensor, row in (daylight.get("sensors") or {}).items():
            try:
                self.chooser.references[sensor] = DaylightReference.from_dict(row.get("table"))
                if (at := dt_util.parse_datetime(row.get("learned") or "")) is not None:
                    self._learned[sensor] = at
            except (ValueError, TypeError, AttributeError):
                continue

    @callback
    def start(self) -> None:
        self._schedule_next()
        self._start_tracks()
        for runner in self.rooms.values():
            runner.start()
        self._start_lights_outs()
        # The entities wrote their first state before the next start was known.
        self._announce()

    def _announce(self) -> None:
        async_dispatcher_send(self.hass, house_signal(self.entry.entry_id))
        async_dispatcher_send(self.hass, ANY_SIGNAL)

    @callback
    def stop(self) -> None:
        if self._unsub_period:
            self._unsub_period()
            self._unsub_period = None
        while self._unsubs:
            self._unsubs.pop()()
        for runner in self.rooms.values():
            runner.stop()

    # -- Normal days and Dark Days --

    def dark_periods(self) -> tuple[str, ...]:
        if self.tracks.periods is not None:
            return self.tracks.periods
        return default_periods({p.name: p.start for p in self.schedule.periods})

    def sun(self, now: datetime) -> SunPosition:
        return position(self.hass.config.latitude, self.hass.config.longitude, now)

    def workday_sensor(self) -> str | None:
        """Home Assistant's Workday sensor, if there is one (public holidays count as days off)."""
        for state in self.hass.states.async_all("binary_sensor"):
            if state.entity_id.startswith("binary_sensor.workday"):
                return state.entity_id
        return None

    def today(self, now: datetime, people: tuple[str, ...] = ()) -> Today:
        """What a timer needs to know about today."""
        sensor = self.workday_sensor()
        state = self.hass.states.get(sensor) if sensor else None
        workday = None if state is None or state.state not in ("on", "off") else state.state == "on"
        dark = self.track == DIM or self.sun(now).elevation < MIN_ELEVATION
        home = {}
        for person in people:
            person_state = self.hass.states.get(person)
            home[person] = person_state is not None and person_state.state == "home"
        return Today(now.date(), workday, dark, home)

    def _start_tracks(self) -> None:
        now = dt_util.now()
        if not self.tracks.enabled:
            self.chooser.update(now, self.sun(now), False, first=True)
            return
        sensors = [s for s in (self.tracks.sensor, self.tracks.fallback) if s]
        for sensor in sensors:
            self._track_reading(sensor, self.hass.states.get(sensor), now)
        if sensors:
            self._unsubs.append(async_track_state_change_event(self.hass, sensors, self._on_track_sensor))
            self._unsubs.append(async_track_time_change(self.hass, self._learn_daily, hour=3, minute=17, second=0))
            self._background(self._learn(), "learn what a clear day looks like")
        if self.tracks.weather:
            self._unsubs.append(async_track_time_interval(self.hass, self._weather_tick, WEATHER_EVERY))
            self._background(self._fetch_weather(), "fetch the weather")
        self._check_track(now, first=True)
        self._unsubs.append(async_track_time_interval(self.hass, self._track_tick, TRACK_CHECK))

    def _background(self, coro, name: str) -> None:
        self.entry.async_create_background_task(self.hass, coro, f"{DOMAIN}: {name}")

    def _track_reading(self, sensor: str, state, now: datetime) -> None:
        value: float | None
        try:
            value = float(state.state) if state is not None else None
        except ValueError:
            value = None
        self.chooser.reading(sensor, value, now)

    @callback
    def _on_track_sensor(self, event: Event[EventStateChangedData]) -> None:
        now = dt_util.now()
        self._track_reading(event.data["entity_id"], event.data["new_state"], now)
        self._check_track(now)

    @callback
    def _track_tick(self, _now: datetime) -> None:
        self._check_track(dt_util.now())

    def _check_track(self, now: datetime, first: bool = False) -> None:
        if self.chooser.update(now, self.sun(now), self.period in self.dark_periods(), first=first):
            self._set_track()
        else:
            self._announce()  # the reading shown moves on

    def _set_track(self) -> None:
        for runner in self.rooms.values():
            runner.track_changed(self.track)
        self._announce()

    @callback
    def override_track(self, track: str) -> None:
        """Chosen by hand: holds until the next period starts."""
        if self.chooser.choose(track, dt_util.now()):
            self._set_track()
        else:
            self._announce()

    def light_level(self) -> tuple[float | None, str | None]:
        return self.chooser.sensor_level(dt_util.now())

    def dark_day_status(self) -> dict[str, Any]:
        """What the page shows about today."""
        now = dt_util.now()
        sun = self.sun(now)
        reading = self.chooser.current(now, sun)
        weather = self.chooser.weather_reading(now)
        sensor = self.chooser.sensor_reading(now, sun)
        return {
            "enabled": self.tracks.enabled,
            "periods": list(self.dark_periods()),
            "active": self.period in self.dark_periods(),
            "reason": self.chooser.reason,
            "pct": reading.pct,
            "source": reading.source,
            "weather_pct": weather.pct,
            "sunlight": round(weather.sunlight) if weather.sunlight is not None else None,
            "cloud_cover": self.weather.get("cloud_cover") if weather.pct is not None else None,
            "sensor_pct": sensor.pct,
            "level": round(sensor.lux) if sensor.lux is not None else None,
            "level_sensor": sensor.sensor,
            "learned": {s: r.learned for s, r in self.chooser.references.items()},
            "sun_elevation": round(sun.elevation, 1),
            "sun_down": sun.elevation < MIN_ELEVATION,
            "weather_state": self._weather_state(now, sun, weather.pct),
            "weather_at": self.weather["at"].isoformat() if self.weather.get("at") else None,
            "sensor_state": self._sensor_state(sun, sensor),
        }

    def _weather_state(self, now: datetime, sun: SunPosition, pct: float | None) -> str:
        """Why the weather says what it says: off / unreachable / sun_down /
        waiting / ok / stale."""
        if not self.tracks.weather:
            return "off"
        if self.weather_failed_at is not None:
            return "unreachable"
        if sun.elevation < MIN_ELEVATION:
            return "sun_down"
        if not self.weather:
            return "waiting"
        if pct is not None:
            return "ok"
        # An answer with no percentage: its sunlight was measured with the sun still
        # too low (just after sunrise), unless it's simply old.
        if self.weather.get("pct") is None and now - self.weather["at"] <= WEATHER_STALE:
            return "sun_down"
        return "stale"

    def _sensor_state(self, sun: SunPosition, reading) -> str:
        """none / no_reading / sun_down / learning / ok."""
        if not self.tracks.has_sensor:
            return "none"
        if reading.lux is None:
            return "no_reading"
        if sun.elevation < MIN_ELEVATION:
            return "sun_down"
        return "ok" if reading.pct is not None else "learning"

    # -- the weather --

    @callback
    def _weather_tick(self, _now: datetime) -> None:
        self._background(self._fetch_weather(), "fetch the weather")

    async def _fetch_weather(self) -> None:
        """Open-Meteo's current sunlight at Home Assistant's location, as percent
        of a clear sky. No key needed; four calls an hour."""
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        params = {
            "latitude": f"{lat:.3f}",
            "longitude": f"{lon:.3f}",
            "current": "shortwave_radiation,cloud_cover",
            "timezone": "GMT",
        }
        try:
            async with asyncio.timeout(30):
                resp = await async_get_clientsession(self.hass).get(WEATHER_URL, params=params)
                resp.raise_for_status()
                data = await resp.json()
            current = data["current"]
            radiation = float(current["shortwave_radiation"])
            interval = int(current.get("interval") or 900)
            at = datetime.fromisoformat(current["time"]).replace(tzinfo=dt_util.UTC)
            cloud = current.get("cloud_cover")
        except Exception as err:  # noqa: BLE001 - any failure: keep the last answer until it's stale
            _LOGGER.debug("Couldn't get the weather from Open-Meteo: %s", err)
            self.weather_failed_at = dt_util.now()
            self._announce()
            return
        self.take_weather(radiation, at, interval, cloud)

    def take_weather(self, radiation: float, at: datetime, interval: int = 900, cloud: Any = None) -> None:
        """A weather answer: the sunlight (W/m²) averaged over ``interval``
        seconds before ``at``."""
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        sun = position(lat, lon, at - timedelta(seconds=interval / 2))
        clear = clear_sky(sun.elevation)
        pct = round(100 * radiation / clear, 1) if sun.elevation >= MIN_ELEVATION and clear > 0 else None
        self.weather = {
            "radiation": radiation, "clear": round(clear), "cloud_cover": cloud, "pct": pct, "at": dt_util.now(),
        }
        self.weather_failed_at = None
        self.chooser.weather(pct, dt_util.now(), radiation)
        self._check_track(dt_util.now())

    # -- what a clear day looks like to the light sensor --

    @callback
    def _learn_daily(self, _now: datetime) -> None:
        self._background(self._learn(), "learn what a clear day looks like")

    async def _learn(self) -> None:
        """Read the sensors' 5-minute statistics since the last time and fold them
        into their clear-day reference."""
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        end = dt_util.utcnow()
        for sensor in (s for s in (self.tracks.sensor, self.tracks.fallback) if s):
            last = self._learned.get(sensor)
            start = max(last, end - timedelta(days=LEARN_DAYS)) if last else end - timedelta(days=LEARN_DAYS)
            try:
                rows = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period, self.hass, start, end, {sensor}, "5minute", None, {"mean"}
                )
            except Exception as err:  # noqa: BLE001 - no statistics: the sensor just can't be used yet
                _LOGGER.debug("Couldn't read statistics for %s: %s", sensor, err)
                continue
            samples = []
            for row in rows.get(sensor, []):
                if (mean := row.get("mean")) is None:
                    continue
                begin = row["start"]
                if isinstance(begin, (int, float)):
                    begin = datetime.fromtimestamp(begin, dt_util.UTC)
                sun = position(lat, lon, begin + timedelta(minutes=2.5))
                samples.append((sun.elevation, sun.morning, float(mean)))
            reference = self.chooser.references.setdefault(sensor, DaylightReference())
            reference.learn(samples, (end - last).total_seconds() / 86400 if last else 0.0)
            self._learned[sensor] = end
        self._daylight_store.async_delay_save(
            lambda: {
                "sensors": {
                    s: {"table": r.to_dict(), "learned": self._learned[s].isoformat() if s in self._learned else None}
                    for s, r in self.chooser.references.items()
                }
            },
            5,
        )
        self._check_track(dt_util.now())

    # -- hand changes --

    def record_change(self, change: Change) -> None:
        cutoff = change.at - KEEP_CHANGES
        self.changes = [c for c in self.changes if c.at >= cutoff][-(MAX_CHANGES - 1):] + [change]
        self._save()
        self._announce()

    def remember(self, room_id: str, memory: dict[str, Any] | None) -> None:
        """A room's state for the next restart (None: nothing running)."""
        if self.room_memory.get(room_id) == memory:
            return
        if memory is None:
            self.room_memory.pop(room_id, None)
        else:
            self.room_memory[room_id] = memory
        self._rooms_store.async_delay_save(lambda: {"rooms": dict(self.room_memory)}, 2)

    async def async_save_rooms(self) -> None:
        """Write the rooms' memory now (on unload: a delayed save might not happen first)."""
        await self._rooms_store.async_save({"rooms": dict(self.room_memory)})

    def dismiss(self, key: str) -> None:
        self.dismissed[key] = (dt_util.now() + DISMISS_FOR).isoformat()
        self._save()
        self._announce()

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {"changes": [change_to(c) for c in self.changes], "dismissed": self.dismissed}, 5
        )

    def suggestions(self, runner: RoomRunner) -> list[Suggestion]:
        now = dt_util.now()
        hidden = {
            key for key, until in self.dismissed.items()
            if (t := dt_util.parse_datetime(until)) is not None and t > now
        }
        found = suggest(self.changes, runner.room_id, runner.config.name, self.schedule, runner.look_of, now)
        return [s for s in found if s.key not in hidden]

    def _schedule_next(self) -> None:
        now = self.schedule.current(dt_util.now())
        self.next_start = now.next_start
        self._unsub_period = async_track_point_in_time(self.hass, self._period_tick, now.next_start)

    @callback
    def _period_tick(self, now: datetime) -> None:
        self.overridden = False
        # Schedule first, so what _set_period announces carries the new next start.
        self._schedule_next()
        self._set_period(self.schedule.current(now).name)
        for lights_out in self.lights_outs:
            if lights_out.mode != LIGHTS_OUT_OFF and lights_out.when == AT_PERIOD and lights_out.period == self.period:
                self.run_lights_out(lights_out, f"{self.period} started")
        # A day chosen by hand also holds only until the next period, and each
        # period start picks the day straight away.
        self.chooser.release()
        self._check_track(dt_util.now(), first=True)

    def _set_period(self, name: str) -> None:
        if name != self.period:
            self.period = name
        # Every room re-checks: a room with its own times may still be in its old
        # period, or go back to its own once a period chosen by hand ends.
        for runner in self.rooms.values():
            runner.sync_period()
        self._announce()

    @callback
    def override_period(self, name: str) -> None:
        """Chosen by hand: holds until the next scheduled start."""
        self.overridden = True
        self._set_period(name)

    # -- Lights out --

    def _start_lights_outs(self) -> None:
        for lights_out in self.lights_outs:
            if lights_out.mode == LIGHTS_OUT_OFF:
                continue
            if lights_out.when == AT_TIME and lights_out.at is not None:
                self._unsubs.append(async_track_time_change(
                    self.hass, self._lights_out_at(lights_out),
                    hour=lights_out.at.hour, minute=lights_out.at.minute, second=0,
                ))
            elif lights_out.when == AT_ENTITY and lights_out.entity:
                self._unsubs.append(async_track_state_change_event(
                    self.hass, [lights_out.entity], self._lights_out_on(lights_out)
                ))

    def _lights_out_at(self, lights_out: LightsOut):
        @callback
        def _fire(_now: datetime) -> None:
            self.run_lights_out(lights_out, f"at {lights_out.at.strftime('%H:%M')}")

        return _fire

    def _lights_out_on(self, lights_out: LightsOut):
        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            new, old = event.data["new_state"], event.data["old_state"]
            if new is None or new.state != lights_out.state:
                return
            if old is None or old.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, lights_out.state):
                return  # coming back after a restart isn't the moment it became true
            self.run_lights_out(lights_out, f"{self._name(lights_out.entity)} is {lights_out.state}")

        return _changed

    def _name(self, entity: str | None) -> str:
        state = self.hass.states.get(entity) if entity else None
        if state is not None and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        return entity or ""

    def describe_lights_out(self, lights_out: LightsOut) -> str:
        return describe_lights_out(lights_out, self._name(lights_out.entity))

    def loose_lights(self) -> list[str]:
        """Lights that are on and in no room: never a group of other lights, a power
        circuit, or a light hidden in Home Assistant (one behind another, such as a
        template light's own)."""
        in_rooms: set[str] = set()
        for runner in self.rooms.values():
            in_rooms.update(runner.config.lights)
            in_rooms.update(runner.config.powered_by.values())
        registry = er.async_get(self.hass)
        found = []
        for state in self.hass.states.async_all("light"):
            if state.state != STATE_ON or state.entity_id in in_rooms:
                continue
            if state.attributes.get(ATTR_ENTITY_ID):
                continue
            entry = registry.async_get(state.entity_id)
            if entry is not None and entry.hidden:
                continue
            found.append(state.entity_id)
        return sorted(found)

    def lights_out_plan(self, lights_out: LightsOut) -> dict[str, list[str]]:
        """What it would do now: rooms off now, rooms it would wait for, lights in no room."""
        plan: dict[str, list[str]] = {OUT_OFF: [], OUT_WAITING: [], "loose": []}
        for room_id, runner in self.rooms.items():
            if lights_out.covers(room_id):
                status = runner.lights_out_status(lights_out.now_style)
                if status != OUT_NOTHING:
                    plan[status].append(room_id)
        if lights_out.whole_house:
            plan["loose"] = self.loose_lights()
        return plan

    @callback
    def run_lights_out(self, lights_out: LightsOut, why: str) -> dict[str, Any]:
        now = dt_util.now()
        ok, not_today = lights_out.runs_today(self.today(now))
        if not ok:
            return self._lights_out_done(lights_out, now, why, skipped=not_today)
        if self.skip_next:
            self.set_skip_next(False)
            return self._lights_out_done(lights_out, now, why, skipped="'Skip the next Lights out' was on")
        if lights_out.mode != LIGHTS_OUT_LIVE:
            return self._lights_out_done(lights_out, now, why, plan=self.lights_out_plan(lights_out))
        until = self.next_start or now + timedelta(days=1)
        plan: dict[str, list[str]] = {OUT_OFF: [], OUT_WAITING: [], "loose": []}
        for room_id, runner in self.rooms.items():
            if lights_out.covers(room_id):
                status = runner.lights_out(lights_out.name, until, lights_out.now_style)
                if status != OUT_NOTHING:
                    plan[status].append(room_id)
        if lights_out.whole_house:
            plan["loose"] = self.loose_lights()
            if plan["loose"]:
                self.hass.async_create_task(self.hass.services.async_call(
                    "light", "turn_off", {ATTR_ENTITY_ID: plan["loose"]}, context=Context()
                ))
        return self._lights_out_done(lights_out, now, why, plan=plan)

    def _lights_out_done(
        self,
        lights_out: LightsOut,
        now: datetime,
        why: str,
        plan: dict[str, list[str]] | None = None,
        skipped: str | None = None,
    ) -> dict[str, Any]:
        run: dict[str, Any] = {"at": now.isoformat(), "id": lights_out.id, "name": lights_out.name,
                               "mode": lights_out.mode, "why": why}
        if skipped is not None:
            run["skipped"] = skipped
            message = f"{why}: skipped, {skipped}"
        else:
            run.update(plan or {})
            message = f"{why}: {self.plan_text(plan or {}, lights_out.mode == LIGHTS_OUT_LIVE)}"
        self.lights_out_runs = (self.lights_out_runs + [run])[-LIGHTS_OUT_RUNS:]
        if "logbook" in self.hass.config.components:
            data: dict[str, Any] = {"name": lights_out.name, "message": message}
            if self.skip_entity_id:
                data[ATTR_ENTITY_ID] = self.skip_entity_id
            self.hass.async_create_task(self.hass.services.async_call("logbook", "log", data))
        self._announce()
        return run

    def plan_text(self, plan: Mapping[str, list[str]], live: bool, log_only: bool = True) -> str:
        """``live``: what it did; otherwise what it would do (in log only, saying so)."""
        def rooms(ids: list[str]) -> str:
            return ", ".join(self.rooms[r].config.name if r in self.rooms else r for r in ids)

        bits = []
        if plan.get(OUT_OFF):
            bits.append(f"{'switched off' if live else 'would switch off'} {rooms(plan[OUT_OFF])}")
        if plan.get(OUT_WAITING):
            bits.append(f"{'waiting for' if live else 'would wait for'} {rooms(plan[OUT_WAITING])} to empty")
        if plan.get("loose"):
            n = len(plan["loose"])
            bits.append(f"{'switched off' if live else 'would switch off'} {n} light{'s' if n != 1 else ''} in no room")
        text = "; ".join(bits) or "nothing was on"
        return text if live or not log_only else f"log only, {text}"

    @callback
    def set_skip_next(self, on: bool) -> None:
        if on != self.skip_next:
            self.skip_next = on
            self._announce()

    @callback
    def set_stealth(self, on: bool) -> None:
        if on == self.stealth:
            return
        self.stealth = on
        for runner in self.rooms.values():
            runner.stealth_changed(on)
        self._announce()

    @callback
    def take_looks(self, options: Mapping[str, Any]) -> bool:
        """Apply new options without a reload if only looks changed."""
        if structure(options) != structure(self.options):
            return False
        self.options = dict(options)
        for room_id, setup in rooms_from(options).items():
            self.rooms[room_id].update_config(setup.config)
        return True


def room_of(hass: HomeAssistant, entity_id: str) -> tuple[House, RoomRunner] | None:
    """Find the room behind one of its status sensors."""
    for house in _houses(hass):
        for runner in house.rooms.values():
            if runner.status_entity_id == entity_id:
                return house, runner
    return None


def _houses(hass: HomeAssistant) -> list[House]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None) is not None
    ]

