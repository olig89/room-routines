"""The page's Timeline: every room's lanes for today, and what happened so far.

The lanes come from ``core/timeline.py`` (the rooms' own settings). This module
adds what only Home Assistant knows: friendly names, what scenes set, the sun,
which day is a workday, and today's history (when the lights were really on,
when a person changed them, when a rule or an Inform held).

Home Assistant's history keeps only on and off for lights, never their
brightness or colour, so "today so far" says when lights were on and whether
the room had put them on; the page colours those times from the lane.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import MODE_LIVE, MODE_OFF
from .core.daylight import MIN_ELEVATION
from .core.habits import ADJUSTED
from .core.lights_out import AT_PERIOD, AT_TIME, MODE_LIVE as LIGHTS_OUT_LIVE, MODE_OFF as LIGHTS_OUT_OFF, LightsOut
from .core.looks import Look
from .core.room import BLENDS, FOR_MINUTES, MOVES_ON
from .core.timeline import Band, DayFacts, Lane, day_spans, room_lanes
from .core.timers import Today
from .house import House, RoomRunner, scene_lights


def _minute(at: datetime) -> int:
    local = dt_util.as_local(at)
    return local.hour * 60 + local.minute


def _name(hass: HomeAssistant, entity: str) -> str:
    state = hass.states.get(entity)
    if state is not None and state.attributes.get("friendly_name"):
        return str(state.attributes["friendly_name"])
    return entity


def look_json(hass: HomeAssistant, runner: RoomRunner, look: Look | None, factor: float = 1.0,
              cap: float | None = None) -> dict[str, Any] | None:
    """A look as the page draws it: each light's level and colour, where known."""
    if look is None:
        return None
    if look.nothing:
        return {"kind": "nothing", "name": "Stays dark", "lights": []}
    name = "Custom"
    lights = look.lights
    if look.scene:
        name = _name(hass, look.scene)
        read = scene_lights(hass, look.scene, runner.config.switchable())
        if read is None:
            return {"kind": "scene", "name": name, "scene": look.scene, "known": False, "lights": []}
        lights = read
    out = []
    for light, target in lights.items():
        pct = target.brightness_pct
        if target.on and pct is not None:
            pct = min(100.0, max(1.0, round(pct * factor, 1)))
        if target.on and cap is not None:
            pct = cap if pct is None else min(pct, cap)
        out.append({
            "entity": light, "name": _name(hass, light), "on": target.on, "pct": pct,
            "kelvin": target.color_temp_kelvin, "rgb": list(target.rgb) if target.rgb else None,
        })
    if not look.scene and all(l["on"] and l["pct"] is None for l in out):
        name = "Last brightness"
    return {"kind": "scene" if look.scene else "lights", "name": name, "scene": look.scene, "known": True, "lights": out}


def _band_json(hass: HomeAssistant, runner: RoomRunner, band: Band) -> dict[str, Any]:
    return {
        "start": band.start, "end": band.end, "period": band.period, "visit": band.visit, "track": band.track,
        "look": look_json(hass, runner, band.look, band.factor, band.cap),
        "blend_to": look_json(hass, runner, band.blend_to, band.blend_factor, band.cap),
        "blend_period": band.blend_period, "cap": band.cap,
    }


def _lane_json(hass: HomeAssistant, runner: RoomRunner, lane: Lane) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": lane.key, "label": lane.label, "note": lane.note, "dark": lane.dark, "today": lane.today,
        "index": lane.index, "bands": [_band_json(hass, runner, b) for b in lane.bands],
    }
    if lane.key == "inform" and lane.index is not None:
        signal = runner.config.signals[lane.index]
        out["look"] = look_json(hass, runner, Look(dict(signal.lights)))
    return out


def _lights_out_at(house: House, lo: LightsOut, day: date) -> int | None:
    """The minute a Lights out runs on a day, if it runs at a known time (a
    period's start: the house's period, as the house runs it)."""
    if lo.when == AT_TIME and lo.at is not None:
        return lo.at.hour * 60 + lo.at.minute
    if lo.when == AT_PERIOD:
        period = next((p for p in house.schedule.periods if p.name == lo.period), None)
        if period is not None:
            start = house.schedule.start_on(period, day)
            return start.hour * 60 + start.minute
    return None


def _lights_out(house: House, runner: RoomRunner) -> Callable[[date, bool], int | None]:
    """When a live Lights out covering the room runs on a day (log only ones
    only say what they would do, so the room carries on)."""
    covering = [lo for lo in house.lights_outs if lo.mode == LIGHTS_OUT_LIVE and lo.covers(runner.room_id)]

    def at(day: date, workday: bool) -> int | None:
        found: int | None = None
        for lo in covering:
            if not lo.runs_today(Today(day, workday, False, {}))[0]:
                continue
            minute = _lights_out_at(house, lo, day)
            if minute is not None and (found is None or minute < found):
                found = minute
        return found

    return at


def _intervals(rows: list[tuple[datetime, Any]], start: datetime, end: datetime, holds) -> list[list[int]]:
    """Times today something held, as [start, end] minutes, from (time, state) rows."""
    out: list[list[int]] = []
    since: datetime | None = None
    for at, state in rows:
        at = max(at, start)
        if holds(state):
            if since is None:
                since = at
        elif since is not None:
            if at > since:
                out.append([_minute(since), max(_minute(since) + 1, _minute(at))])
            since = None
    if since is not None:
        out.append([_minute(since), max(_minute(since) + 1, _minute(end))])
    return out


async def _history(hass: HomeAssistant, house: House, start: datetime, end: datetime) -> dict[str, list]:
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    from .websocket import _rows

    wanted: set[str] = set()
    for runner in house.rooms.values():
        if runner.mode == MODE_OFF:
            continue
        wanted.update(runner.config.switchable())
        if runner.status_entity_id:
            wanted.add(runner.status_entity_id)
        wanted.update(r.when.entity for r in runner.rules)
        wanted.update(s.when.entity for s in runner.config.signals)
    if not wanted:
        return {}
    rows = await get_instance(hass).async_add_executor_job(
        partial(get_significant_states, hass, start, end, sorted(wanted), None, True, True, True, True)
    )
    return {entity: [(at, state) for at, state, _ in _rows(rows.get(entity, []))] for entity in wanted}


def _room_history(hass: HomeAssistant, house: House, runner: RoomRunner, rows: dict[str, list],
                  lanes: list[Lane], start: datetime, now: datetime) -> dict[str, Any]:
    """Today so far: lights on (and whether the room put them on), changes by hand,
    and when each rule and Inform held."""
    status = rows.get(runner.status_entity_id or "", [])

    def status_at(minute: float) -> str | None:
        state = None
        for at, value in status:
            if _minute(max(at, start)) <= minute:
                state = value
        return state

    # When any of the room's lights was on.
    edges: list[tuple[datetime, bool]] = []
    current: dict[str, bool] = {}
    events = sorted(
        ((max(at, start), light, state == STATE_ON) for light in runner.config.switchable() for at, state in rows.get(light, [])
         if state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None)),
        key=lambda e: e[0],
    )
    for at, light, on in events:
        before = any(current.values())
        current[light] = on
        after = any(current.values())
        if after != before:
            edges.append((at, after))
    on_spans = _intervals(edges, start, now, lambda v: v)
    # Owned: the room put them on. A room in log only doesn't (the old wiring does).
    live = runner.mode == MODE_LIVE
    on = [[a, b, live and status_at((a + b) / 2) == "owned"] for a, b in on_spans]

    # Changes by hand: from the room's own log (live rooms only).
    hand = []
    for change in house.changes:
        if change.room_id != runner.room_id or change.kind != ADJUSTED or change.at < start:
            continue
        a = _minute(change.at)
        # A hand change holds until the lights go off, and for its minutes or until the routine moves on
        # (a held light that blends is still the hand's until the next period starts).
        b = next((_minute(at) for at, value in status if at > change.at and value == "idle"), _minute(now))
        if runner.config.hand_hold == FOR_MINUTES:
            b = min(b, a + int(runner.config.hand_minutes))
        elif runner.config.hand_hold in (MOVES_ON, BLENDS):
            b = min(b, next((s for s, _, _ in day_spans(runner.schedule, start.date()) if s > a), b))
        hand.append([a, max(a + 1, min(b, _minute(now)))])

    held: dict[str, list] = {}
    for lane in lanes:
        if lane.key in ("rule", "house_rule") and lane.index is not None:
            rule = (runner.config.rules if lane.key == "rule" else house.house_rules)[lane.index]
            cond = rule.when
        elif lane.key == "inform" and lane.index is not None:
            cond = runner.config.signals[lane.index].when
        else:
            continue
        held[f"{lane.key}:{lane.index}"] = _intervals(
            rows.get(cond.entity, []), start, now, lambda v, c=cond: c.holds({c.entity: v})
        )
    return {"on": on, "hand": hand, "held": held}


async def timeline_data(hass: HomeAssistant, house: House) -> dict[str, Any]:
    now = dt_util.now()
    start = dt_util.start_of_local_day(now)
    today = house.today(now)

    def sun_down(minute: int) -> bool:
        return house.sun(start + timedelta(minutes=minute)).elevation < MIN_ELEVATION

    rows = await _history(hass, house, start, now)
    areas = ar.async_get(hass)
    rooms = []
    for runner in house.rooms.values():
        area = areas.async_get_area(runner.area_id) if runner.area_id else None
        base = {"id": runner.room_id, "name": runner.config.name, "mode": runner.mode,
                "area_id": runner.area_id, "area_name": area.name if area else None}
        if runner.mode == MODE_OFF:
            rooms.append({**base, "group": "off"})
            continue
        lights_out = _lights_out(house, runner)
        facts = DayFacts(
            today=now.date(), workday_today=today.workday, dark_possible=house.tracks.enabled,
            dark_periods=tuple(house.dark_periods()), auto_dim=house.tracks.factor, sun_down=sun_down,
            lights_out=lights_out, house_schedule=house.schedule,
        )
        minute = lights_out(now.date(), facts.workday(now.date()))
        house_rules = [(i, r) for i, r in enumerate(house.house_rules) if r.applies_to(runner.room_id)]
        entities = {r.when.entity for r in runner.rules} | {s.when.entity for s in runner.config.signals}
        entities |= set(runner.config.lights)
        names = {e: _name(hass, e) for e in entities}
        lanes = room_lanes(runner.config, runner.schedule, facts, house_rules, names)
        rooms.append({
            **base,
            "group": "allday" if runner.config.runs_ambient else "someone",
            "own_periods": [{"start": s, "end": e, "name": n} for s, e, n in day_spans(runner.schedule, now.date())],
            "lights_out": minute,
            "lanes": [_lane_json(hass, runner, lane) for lane in lanes],
            "history": _room_history(hass, house, runner, rows, lanes, start, now) if rows else {},
        })

    def sun_event(event: str) -> int | None:
        at = get_astral_event_date(hass, event, now.date())
        return _minute(at) if at else None

    return {
        "now": _minute(now),
        "date": now.date().isoformat(),
        "workday": today.workday if today.workday is not None else now.weekday() < 5,
        "periods": [{"start": s, "end": e, "name": n} for s, e, n in day_spans(house.schedule, now.date())],
        "sunrise": sun_event("sunrise"),
        "sunset": sun_event("sunset"),
        "dark_periods": list(house.dark_periods()) if house.tracks.enabled else [],
        "lights_outs": [
            {"name": lo.name, "mode": lo.mode, "style": lo.style, "text": house.describe_lights_out(lo),
             "minute": _lights_out_at(house, lo, now.date()),
             "whole_house": lo.whole_house, "rooms": list(lo.rooms)}
            for lo in house.lights_outs if lo.mode != LIGHTS_OUT_OFF
        ],
        "skip_next": house.skip_next,
        "rooms": rooms,
    }
