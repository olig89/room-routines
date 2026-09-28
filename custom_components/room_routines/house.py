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

The house keeps the current period (with a timer for the next one) and the
stealth switch, and passes both to every room.
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
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import ANY_SIGNAL, DOMAIN, MODE_LIVE, MODE_LOG_ONLY, MODE_OFF, house_signal, room_signal
from .core.fade import plan_fade
from .core.looks import OFF, LightTarget, Look
from .core.matching import Command, OwnChangeMatcher
from .core.periods import Schedule
from .core.room import ApplyLook, Decision, MoveBlinds, Room, RoomConfig, TurnOff, WakeAt
from .core.serial import room_from, schedule_from

_LOGGER = logging.getLogger(__name__)

BULB_WAIT = timedelta(seconds=10)  # how long to wait for smart bulbs after powering their circuit


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


class RoomRunner:
    def __init__(self, house: House, setup: RoomSetup) -> None:
        self.house = house
        self.hass = house.hass
        self.room_id = setup.room_id
        self.config = setup.config
        self.area_id = setup.area_id
        self.lux_sensor = setup.lux_sensor
        self.self_fading = setup.self_fading
        self.mode = MODE_LOG_ONLY  # the mode select restores the real value on start-up
        self.matcher = OwnChangeMatcher()
        self.room: Room | None = None
        self.status_entity_id: str | None = None
        self.mode_entity_id: str | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._wake: CALLBACK_TYPE | None = None
        self._fades: list[CALLBACK_TYPE] = []
        # Brightness each light had before a stepped fade-out. A light that
        # remembers its last level (a KNX DALI light) would otherwise come back
        # at the dimmed level the fade left it at.
        self._before_fade: dict[str, float] = {}

    # -- life cycle --

    @callback
    def start(self) -> None:
        self._build()
        sensors = list(self.config.triggers) + list(self.config.holds)
        self._unsubs.append(async_track_state_change_event(self.hass, sensors, self._on_sensor))
        self._unsubs.append(async_track_state_change_event(self.hass, list(self.config.lights), self._on_light))
        if self.lux_sensor:
            self._unsubs.append(async_track_state_change_event(self.hass, [self.lux_sensor], self._on_lux))

    @callback
    def stop(self) -> None:
        self._cancel_timers()
        while self._unsubs:
            self._unsubs.pop()()

    def _cancel_timers(self) -> None:
        if self._wake:
            self._wake()
            self._wake = None
        self._cancel_fades()

    def _cancel_fades(self) -> None:
        while self._fades:
            self._fades.pop()()

    def _build(self) -> None:
        """A fresh core room, from the lights and sensors as they are now."""
        self._cancel_timers()
        now = dt_util.now()
        lights_on = self.mode == MODE_LIVE and self._any_on()
        self.room = Room(
            self.config, self.house.schedule, self.house.period, lights_on, now,
            stealth=self.house.stealth,
        )
        for sensor in (*self.config.triggers, *self.config.holds):
            state = self.hass.states.get(sensor)
            self.room.sensors[sensor] = state is not None and state.state == STATE_ON
        self._offer_lux(self.hass.states.get(self.lux_sensor) if self.lux_sensor else None, now)
        self._notify()

    @callback
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        self._build()

    @callback
    def update_config(self, config: RoomConfig) -> None:
        """New looks (or other settings that don't change what is listened to)."""
        self.config = config
        if self.room is not None:
            self.room.config = config
        self._notify()

    # -- events from Home Assistant --

    def _any_on(self) -> bool:
        for light in self.config.switchable():
            state = self.hass.states.get(light)
            if state is not None and state.state == STATE_ON:
                return True
        return False

    @callback
    def _on_sensor(self, event: Event[EventStateChangedData]) -> None:
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
        self._run(self.room.lights(self._any_on(), own, now))

    @callback
    def _on_lux(self, event: Event[EventStateChangedData]) -> None:
        self._offer_lux(event.data["new_state"], dt_util.now())
        self._notify()

    def _offer_lux(self, state, now: datetime) -> None:
        if self.room is None or state is None:
            return
        try:
            value = float(state.state)
        except ValueError:
            return
        self.room.lux(value, now)

    # -- from the house --

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
    def stealth_changed(self, on: bool) -> None:
        if self.room is None:
            return
        if self.mode == MODE_OFF:
            self.room.stealth = on
            self._notify()
            return
        self._run(self.room.set_stealth(on, dt_util.now()))

    # -- carrying out decisions --

    @callback
    def _tick(self, now: datetime) -> None:
        self._wake = None
        if self.room is not None and self.mode != MODE_OFF:
            self._run(self.room.tick(now))

    @callback
    def _run(self, decision: Decision) -> None:
        acting = False
        for action in decision.actions:
            if isinstance(action, WakeAt):
                if self._wake:
                    self._wake()
                self._wake = async_track_point_in_time(self.hass, self._tick, action.at)
                continue
            acting = True
            if self.mode != MODE_LIVE:
                continue
            if isinstance(action, (ApplyLook, TurnOff)):
                # A new instruction replaces any fade still under way (someone
                # walking back in during a fade-out brings the lights back).
                self._cancel_fades()
            if isinstance(action, ApplyLook):
                self.hass.async_create_task(self._apply(action))
            elif isinstance(action, TurnOff):
                self.hass.async_create_task(self._turn_off(action.lights, action.transition))
            elif isinstance(action, MoveBlinds):
                self.hass.async_create_task(self._move_blinds(action.positions))
        if acting:
            prefix = "" if self.mode == MODE_LIVE else "Log only: would act. "
            self.hass.async_create_task(self._log(prefix + decision.reason))
        self._notify()

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

    async def _send(self, light: str, target: LightTarget, transition: timedelta | None = None) -> None:
        ctx = Context()
        pct = target.brightness_pct if target.on else None
        self.matcher.record(Command(light, ctx.id, dt_util.now(), target.on, pct))
        data: dict[str, Any] = {ATTR_ENTITY_ID: light}
        if transition:
            data["transition"] = transition.total_seconds()
        if not target.on:
            await self.hass.services.async_call("light", "turn_off", data, context=ctx)
            return
        if target.brightness_pct is not None:
            data["brightness_pct"] = target.brightness_pct
        if target.color_temp_kelvin is not None:
            data["color_temp_kelvin"] = target.color_temp_kelvin
        if target.rgb is not None:
            data["rgb_color"] = list(target.rgb)
        await self.hass.services.async_call("light", "turn_on", data, context=ctx)

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
            await self._send(light, target, action.transition if how == "transition" else None)
        if fading:
            self._fade(fading, action.transition)

    def _fade(self, targets: Mapping[str, LightTarget], duration: timedelta) -> None:
        current: dict[str, float | None] = {}
        for light in targets:
            state = self.hass.states.get(light)
            on = state is not None and state.state == STATE_ON
            current[light] = (brightness_pct(state) or 100.0) if on else None
        for offset, sends in plan_fade(current, Look(dict(targets)), duration):
            @callback
            def _step(_now, sends=sends) -> None:
                for light, target in sends.items():
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


def capture(hass: HomeAssistant, config: RoomConfig) -> Look:
    """The room's lights as they are now, as a look."""
    lights: dict[str, LightTarget] = {}
    for light in config.switchable():
        state = hass.states.get(light)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue
        if state.state != STATE_ON:
            lights[light] = OFF
            continue
        mode = state.attributes.get("color_mode")
        kelvin = state.attributes.get("color_temp_kelvin") if mode == "color_temp" else None
        rgb = state.attributes.get("rgb_color") if mode in COLOUR_MODES else None
        lights[light] = LightTarget(
            True,
            brightness_pct(state),
            int(kelvin) if kelvin else None,
            tuple(int(c) for c in rgb) if rgb else None,
        )
    return Look(lights)


def rooms_from(options: Mapping[str, Any]) -> dict[str, RoomSetup]:
    rooms = {}
    for room_id, data in (options.get("rooms") or {}).items():
        rooms[room_id] = RoomSetup(
            room_id, room_from(data), data.get("area_id"), data.get("lux_sensor"),
            frozenset(data.get("self_fading") or ()),
        )
    return rooms


def structure(options: Mapping[str, Any]) -> dict[str, Any]:
    """The options with every room's looks removed: if only looks changed,
    the house can take them without a reload (and without forgetting which
    lights it switched on)."""
    rooms = {
        room_id: {k: v for k, v in data.items() if k != "looks"}
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
        self.rooms = {rid: RoomRunner(self, setup) for rid, setup in rooms_from(self.options).items()}
        self._unsub_period: CALLBACK_TYPE | None = None

    @callback
    def start(self) -> None:
        self._schedule_next()
        for runner in self.rooms.values():
            runner.start()

    @callback
    def stop(self) -> None:
        if self._unsub_period:
            self._unsub_period()
            self._unsub_period = None
        for runner in self.rooms.values():
            runner.stop()

    def _schedule_next(self) -> None:
        now = self.schedule.current(dt_util.now())
        self.next_start = now.next_start
        self._unsub_period = async_track_point_in_time(self.hass, self._period_tick, now.next_start)

    @callback
    def _period_tick(self, now: datetime) -> None:
        self.overridden = False
        self._set_period(self.schedule.current(now).name)
        self._schedule_next()

    def _set_period(self, name: str) -> None:
        if name != self.period:
            self.period = name
            for runner in self.rooms.values():
                runner.period_changed(name)
        async_dispatcher_send(self.hass, house_signal(self.entry.entry_id))
        async_dispatcher_send(self.hass, ANY_SIGNAL)

    @callback
    def override_period(self, name: str) -> None:
        """Chosen by hand: holds until the next scheduled start."""
        self.overridden = True
        self._set_period(name)

    @callback
    def set_stealth(self, on: bool) -> None:
        if on == self.stealth:
            return
        self.stealth = on
        for runner in self.rooms.values():
            runner.stealth_changed(on)
        async_dispatcher_send(self.hass, house_signal(self.entry.entry_id))
        async_dispatcher_send(self.hass, ANY_SIGNAL)

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

