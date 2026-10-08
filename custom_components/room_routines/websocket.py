"""The panel's API (websocket commands).

Everyday commands (seeing the house, saving a room's look, dismissing a
suggestion) are open to anyone who can open the panel. Changing rooms, periods
and the dark-day sensor is for admins only: the panel hides those parts from
other users and these commands refuse them.

Scenes are saved by the page itself through Home Assistant's own scene API (the
one the scene editor uses, admins only), so they stay ordinary Home Assistant
scenes. ``scene_draft`` gives the page what to save; ``save_look`` with
``how: scene`` then points the look at it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any

import asyncio

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util, slugify

from .const import ANY_SIGNAL, DOMAIN, MODE_LOG_ONLY, VERSION
from .core.habits import Suggestion
from .core.lux import dark_enough
from .core.parity import any_on, compare, edges, windows, within
from .core.serial import look_to, schedule_to, target_to, tracks_to
from .core.tracks import DIM, NORMAL, TRACK_LABELS, TRACKS
from .house import House, RoomRunner, capture, scene_entities
from .settings import (
    SettingsError,
    add_room,
    remove_room,
    set_look,
    set_house_rules,
    set_periods,
    set_tracks,
    unhide_area,
    update_room,
)

HISTORY_MAX_HOURS = 24 * 14


def async_register_websocket(hass: HomeAssistant) -> None:
    for command in (
        ws_subscribe,
        ws_save_look,
        ws_save_room,
        ws_unhide_area,
        ws_remove_room,
        ws_save_periods,
        ws_save_tracks,
        ws_save_house_rules,
        ws_scene_draft,
        ws_dismiss,
        ws_history,
    ):
        websocket_api.async_register_command(hass, command)


def _house(hass: HomeAssistant) -> House | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        house = getattr(entry, "runtime_data", None)
        if isinstance(house, House):
            return house
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _suggestion(s: Suggestion) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": s.key, "kind": s.kind, "period": s.period, "track": s.track,
        "count": s.count, "days": s.days, "text": s.text,
    }
    if s.look is not None:
        out["lights"] = {light: target_to(t) for light, t in s.look.lights.items()}
        out["scene_entities"] = _entities_from_targets(s.look.lights)
    if s.move_period:
        out["move_period"] = s.move_period
        out["new_start"] = s.new_start.strftime("%H:%M") if s.new_start else None
    return out


def _entities_from_targets(lights) -> dict[str, dict[str, Any]]:
    """Look targets in the form a Home Assistant scene stores."""
    out: dict[str, dict[str, Any]] = {}
    for light, t in lights.items():
        if not t.on:
            out[light] = {"state": "off"}
            continue
        entry: dict[str, Any] = {"state": "on"}
        if t.brightness_pct is not None:
            entry["brightness"] = round(t.brightness_pct / 100 * 255)
        if t.color_temp_kelvin is not None:
            entry["color_mode"] = "color_temp"
            entry["color_temp_kelvin"] = t.color_temp_kelvin
        elif t.rgb is not None:
            entry["color_mode"] = "rgb"
            entry["rgb_color"] = list(t.rgb)
        out[light] = entry
    return out


def room_snapshot(hass: HomeAssistant, runner: RoomRunner) -> dict[str, Any]:
    house = runner.house
    stored = (house.options.get("rooms") or {}).get(runner.room_id, {})
    room = runner.room
    now = dt_util.now()
    ambient = room.ambient.ambient(now) if room else None
    area = ar.async_get(hass).async_get_area(runner.area_id) if runner.area_id else None
    source = room.source() if room else None
    blending = room.blending(now) if room and room.state.value == "owned" and not room.paused else None
    return {
        "id": runner.room_id,
        "name": runner.config.name,
        "area_id": runner.area_id,
        "area_name": area.name if area else None,
        "mode": runner.mode,
        "mode_entity": runner.mode_entity_id,
        "status_entity": runner.status_entity_id,
        "state": room.state.value if room else None,
        "reason": room.last.reason if room else None,
        "lights_off_at": _iso(room.deadline) if room else None,
        "cooldown_until": _iso(room.cooldown_until) if room else None,
        "sensors": dict(room.sensors) if room else {},
        "ambient": ambient,
        "dark_enough": dark_enough(ambient, runner.config.threshold_lux),
        "stealth": room.stealth if room else house.stealth,
        "look_period": source.period if source else None,
        "look_track": source.track if source else NORMAL,
        "look_factor": room.dim_factor() if room else 1.0,
        "current_look": look_to(source.look) if source else None,
        "period": room.period if room else house.period,
        "next_change": _iso(runner.next_change()),
        "own_times": bool(runner.config.period_starts),
        "has_sensors": runner.config.has_sensors,
        "paused": room.paused if room else False,
        # Nothing to switch on in any period: the room can never light up.
        "no_look": (not room.has_any_look()) if room else False,
        **runner.context_status(),
        "blending": (
            {"fraction": round(blending[0], 3), "into": blending[1]} if blending else None
        ),
        # What is stored, so the settings page edits exactly that.
        "settings": {
            "lights": list(stored.get("lights") or []),
            "triggers": list(stored.get("triggers") or []),
            "holds": list(stored.get("holds") or []),
            "lux_sensor": stored.get("lux_sensor"),
            "threshold_lux": stored.get("threshold_lux"),
            "timeout_s": stored.get("timeout_s"),
            "fade_out_s": stored.get("fade_out_s"),
            "cooldown_s": stored.get("cooldown_s"),
            "drift_s": stored.get("drift_s"),
            "self_fading": list(stored.get("self_fading") or []),
            "on_by_hand": stored.get("on_by_hand") or "leave",
            "blends": dict(stored.get("blends") or {}),
            "period_starts": dict(stored.get("period_starts") or {}),
            "timers": list(stored.get("timers") or []),
            "starters": list(stored.get("starters") or []),
            "only_when": list(stored.get("only_when") or []),
            "rules": list(stored.get("rules") or []),
        },
        "looks": dict(stored.get("looks") or {}),
        "dim_looks": dict(stored.get("dim_looks") or {}),
        "suggestions": [_suggestion(s) for s in house.suggestions(runner)],
        "hand_changes_28d": sum(
            1 for c in house.changes if c.room_id == runner.room_id and now - c.at <= timedelta(days=28)
        ),
    }


def snapshot(hass: HomeAssistant) -> dict[str, Any]:
    house = _house(hass)
    if house is None:
        return {"version": VERSION, "set_up": False}
    schedule = schedule_to(house.schedule)
    return {
        "version": VERSION,
        "set_up": True,
        "house": {
            "period": house.period,
            "next_start": _iso(house.next_start),
            "next_period": house.schedule.current(dt_util.now()).next_name,
            "overridden": house.overridden,
            "stealth": house.stealth,
            "period_entity": house.period_entity_id,
            "stealth_entity": house.stealth_entity_id,
            "periods": schedule["periods"],
            "alt_days": schedule["alt_days"],
            "order": list(house.schedule.order()),
            "track": house.track,
            "track_entity": house.track_entity_id,
            "track_by_hand": house.chooser.by_hand,
            "tracks": {**tracks_to(house.tracks), **house.dark_day_status()},
            "hidden_areas": [
                {"id": area_id, "name": (a.name if (a := ar.async_get(hass).async_get_area(area_id)) else area_id)}
                for area_id in house.options.get("hidden_areas") or []
            ],
            "people": sorted(state.entity_id for state in hass.states.async_all("person")),
            "workday_sensor": house.workday_sensor(),
            "house_rules": list(house.options.get("house_rules") or []),
        },
        "rooms": [room_snapshot(hass, runner) for runner in house.rooms.values()],
    }


# ---- live view -----------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The house as the panel shows it, sent now and again after every change (at most every 0.5 s)."""
    pending: dict[str, Any] = {"cancel": None}

    @callback
    def _send(_now=None) -> None:
        pending["cancel"] = None
        connection.send_message(websocket_api.event_message(msg["id"], snapshot(hass)))

    @callback
    def _changed() -> None:
        if pending["cancel"] is None:
            pending["cancel"] = async_call_later(hass, 0.5, _send)

    unsub = async_dispatcher_connect(hass, ANY_SIGNAL, _changed)

    @callback
    def _unsubscribe() -> None:
        unsub()
        if pending["cancel"]:
            pending["cancel"]()

    connection.subscriptions[msg["id"]] = _unsubscribe
    connection.send_result(msg["id"])
    _send()


# ---- changes -------------------------------------------------------------------


def _save(hass: HomeAssistant, house: House, options: dict[str, Any]) -> None:
    hass.config_entries.async_update_entry(house.entry, options=options)


def _error(connection: websocket_api.ActiveConnection, msg: dict[str, Any], err: SettingsError) -> None:
    connection.send_error(msg["id"], err.key, str(err))


SCENE_WAIT_S = 10.0


async def _scene_entity(hass: HomeAssistant, config_id: str) -> str | None:
    """The entity of a scene saved through the scene editor's API, once Home
    Assistant has reloaded scenes (it does that just after saving)."""
    registry = er.async_get(hass)
    for _ in range(int(SCENE_WAIT_S / 0.25)):
        entity_id = registry.async_get_entity_id("scene", "homeassistant", config_id)
        if entity_id and hass.states.get(entity_id) is not None:
            return entity_id
        await asyncio.sleep(0.25)
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_look",
        vol.Required("room_id"): str,
        vol.Required("period"): str,
        vol.Optional("track", default=NORMAL): vol.In(list(TRACKS)),
        # "custom" stores ``look``; "current" takes the lights as they are now;
        # "scene" turns on ``scene`` (an entity) or ``scene_id`` (a scene just
        # saved through Home Assistant's scene API); "nothing" keeps the room
        # dark; "borrow" removes the period's own look on this track.
        vol.Required("how"): vol.In(["custom", "current", "scene", "nothing", "borrow"]),
        vol.Optional("look"): dict,
        vol.Optional("scene"): str,
        vol.Optional("scene_id"): str,
    }
)
@websocket_api.async_response
async def ws_save_look(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    house = _house(hass)
    if house is None or msg["room_id"] not in house.rooms:
        connection.send_error(msg["id"], "unknown_room", "No such room")
        return
    how = msg["how"]
    if how == "custom":
        look: dict[str, Any] | None = msg.get("look") or {"lights": {}}
    elif how == "current":
        look = look_to(capture(hass, house.rooms[msg["room_id"]].config))
    elif how == "scene":
        scene = msg.get("scene")
        if not scene and msg.get("scene_id"):
            scene = await _scene_entity(hass, msg["scene_id"])
        if not scene or not scene.startswith("scene.") or hass.states.get(scene) is None:
            connection.send_error(msg["id"], "unknown_scene", "That scene doesn't exist (yet)")
            return
        look = {"scene": scene}
    elif how == "nothing":
        look = {"nothing": True}
    else:
        look = None
    house = _house(hass)  # the options may have moved on while waiting
    try:
        options = set_look(house.entry.options, msg["room_id"], msg["period"], look, msg["track"])
    except SettingsError as err:
        _error(connection, msg, err)
        return
    _save(hass, house, options)
    key = "dim_looks" if msg["track"] == DIM else "looks"
    connection.send_result(msg["id"], {"look": options["rooms"][msg["room_id"]].get(key, {}).get(msg["period"])})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/scene_draft",
        vol.Required("room_id"): str,
        vol.Required("period"): str,
        vol.Optional("track", default=NORMAL): vol.In(list(TRACKS)),
    }
)
@callback
def ws_scene_draft(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What to save as a Home Assistant scene for this room, period and track: the
    room's lights as they are now, under the scene the look already uses (so saving
    again updates it) or a new one."""
    house = _house(hass)
    runner = house.rooms.get(msg["room_id"]) if house else None
    if runner is None:
        connection.send_error(msg["id"], "unknown_room", "No such room")
        return
    period, track = msg["period"], msg["track"]
    stored = (house.options.get("rooms") or {}).get(runner.room_id, {})
    own = (stored.get("dim_looks" if track == DIM else "looks") or {}).get(period) or {}
    config_id = None
    if scene := own.get("scene"):
        state = hass.states.get(scene)
        config_id = state.attributes.get("id") if state else None
    entities = scene_entities(hass, runner.config.switchable())
    connection.send_result(
        msg["id"],
        {
            "config_id": config_id or f"{DOMAIN}_{runner.room_id}_{slugify(period)}_{track}",
            "existing": bool(config_id),
            "name": f"{runner.config.name} · {period}{' · ' + TRACK_LABELS[DIM] if track == DIM else ''}",
            "entities": entities,
            "any_on": any(e.get("state") == "on" for e in entities.values()),
        },
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/dismiss", vol.Required("key"): str})
@callback
def ws_dismiss(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Hide a suggestion for four weeks."""
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    house.dismiss(msg["key"])
    connection.send_result(msg["id"], {})


ROOM_FIELDS = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Optional("area_id"): vol.Any(None, str),
        vol.Required("lights"): [str],
        vol.Optional("triggers", default=list): [str],
        vol.Optional("holds", default=list): [str],
        vol.Optional("lux_sensor"): vol.Any(None, str),
        vol.Optional("threshold_lux"): vol.Any(None, vol.Coerce(float)),
        vol.Required("timeout_s"): vol.All(vol.Coerce(int), vol.Range(min=0, max=7200)),
        vol.Required("fade_out_s"): vol.All(vol.Coerce(int), vol.Range(min=0, max=300)),
        vol.Required("cooldown_s"): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
        vol.Required("drift_s"): vol.All(vol.Coerce(int), vol.Range(min=0, max=600)),
        vol.Optional("self_fading", default=list): [str],
        vol.Optional("on_by_hand"): vol.In(["leave", "routine"]),
        vol.Optional("blends"): {str: vol.All(vol.Coerce(int), vol.Range(min=0, max=1440))},
        vol.Optional("period_starts"): {str: vol.Any(None, str)},
        vol.Optional("timers"): [dict],
        vol.Optional("starters"): [dict],
        vol.Optional("only_when"): [dict],
        vol.Optional("rules"): [dict],
    }
)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_room",
        vol.Optional("room_id"): str,
        vol.Required("room"): ROOM_FIELDS,
    }
)
@websocket_api.require_admin
@callback
def ws_save_room(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    try:
        if room_id := msg.get("room_id"):
            options = update_room(house.entry.options, room_id, msg["room"])
        else:
            options, room_id = add_room(house.entry.options, msg["room"])
    except SettingsError as err:
        _error(connection, msg, err)
        return
    _save(hass, house, options)
    connection.send_result(msg["id"], {"room_id": room_id})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/unhide_area", vol.Required("area_id"): str})
@websocket_api.require_admin
@callback
def ws_unhide_area(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Bring a hidden area back as a room (it is added again on the reload)."""
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    _save(hass, house, unhide_area(house.entry.options, msg["area_id"]))
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/remove_room", vol.Required("room_id"): str})
@websocket_api.require_admin
@callback
def ws_remove_room(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    try:
        options = remove_room(house.entry.options, msg["room_id"])
    except SettingsError as err:
        _error(connection, msg, err)
        return
    _save(hass, house, options)
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_periods",
        vol.Required("periods"): [
            vol.Schema({vol.Required("name"): str, vol.Required("start"): str, vol.Optional("alt_start"): vol.Any(None, str)})
        ],
        vol.Required("alt_days"): [vol.All(vol.Coerce(int), vol.Range(min=0, max=6))],
        vol.Optional("renames", default=dict): {str: vol.Any(None, str)},
    }
)
@websocket_api.require_admin
@callback
def ws_save_periods(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    try:
        options = set_periods(house.entry.options, msg["periods"], msg["alt_days"], msg["renames"])
    except SettingsError as err:
        _error(connection, msg, err)
        return
    _save(hass, house, options)
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_tracks",
        vol.Optional("on", default=False): bool,
        vol.Optional("weather", default=True): bool,
        vol.Optional("sensor"): vol.Any(None, str),
        vol.Optional("fallback"): vol.Any(None, str),
        vol.Optional("first", default="weather"): vol.In(["weather", "sensor"]),
        vol.Optional("periods"): vol.Any(None, [str]),
        vol.Optional("dark_below_pct", default=40): vol.All(vol.Coerce(float), vol.Range(min=0, max=200)),
        vol.Optional("normal_above_pct", default=55): vol.All(vol.Coerce(float), vol.Range(min=0, max=200)),
        vol.Optional("brightness_pct", default=100): vol.All(vol.Coerce(float), vol.Range(min=1, max=300)),
        vol.Optional("dark_below_wm2", default=150): vol.All(vol.Coerce(float), vol.Range(min=0, max=600)),
    }
)
@websocket_api.require_admin
@callback
def ws_save_tracks(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    try:
        options = set_tracks(house.entry.options, msg)
    except SettingsError as err:
        _error(connection, msg, err)
        return
    _save(hass, house, options)
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/save_house_rules", vol.Required("rules"): [dict]}
)
@websocket_api.require_admin
@callback
def ws_save_house_rules(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    house = _house(hass)
    if house is None:
        connection.send_error(msg["id"], "not_set_up", "Room Routines isn't set up")
        return
    try:
        options = set_house_rules(house.entry.options, msg["rules"])
    except SettingsError as err:
        _error(connection, msg, err)
        return
    _save(hass, house, options)
    connection.send_result(msg["id"], {})


# ---- history and the dry-run check -------------------------------------------------


def _rows(states: list[Any]) -> list[tuple[datetime, str, dict[str, Any]]]:
    """Recorder rows (full states, or minimal dicts after the first) as (time, state, attributes)."""
    out = []
    for row in states:
        if isinstance(row, dict):
            at = dt_util.parse_datetime(row.get("last_changed") or row.get("lu") or "")
            state = row.get("state", row.get("s"))
            attrs = row.get("attributes") or {}
        else:
            at, state, attrs = row.last_changed, row.state, dict(row.attributes)
        if at is not None:
            out.append((at, state, attrs))
    return out


def _on(state: str) -> bool | None:
    if state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
        return None
    return state == STATE_ON


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Required("room_id"): str,
        vol.Optional("hours", default=72): vol.All(vol.Coerce(float), vol.Range(min=1, max=HISTORY_MAX_HOURS)),
        vol.Optional("tolerance_s", default=20): vol.All(vol.Coerce(float), vol.Range(min=1, max=600)),
    }
)
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The room's decisions and its lights' real changes, from Home Assistant's history,
    plus the dry-run check for the time it spent in log-only."""
    house = _house(hass)
    runner = house.rooms.get(msg["room_id"]) if house else None
    if runner is None:
        connection.send_error(msg["id"], "unknown_room", "No such room")
        return
    if "recorder" not in hass.config.components:
        connection.send_error(msg["id"], "no_recorder", "History needs the recorder")
        return
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    end = dt_util.utcnow()
    start = end - timedelta(hours=msg["hours"])
    lights = list(runner.config.switchable())
    tracked = [e for e in (runner.status_entity_id, runner.mode_entity_id) if e]
    recorder = get_instance(hass)
    with_attrs = await recorder.async_add_executor_job(
        partial(get_significant_states, hass, start, end, tracked, None, True, False, False, False)
    )
    light_rows = await recorder.async_add_executor_job(
        partial(get_significant_states, hass, start, end, lights, None, True, False, True, True)
    )

    status = _rows(with_attrs.get(runner.status_entity_id or "", []))
    modes = _rows(with_attrs.get(runner.mode_entity_id or "", []))
    per_light = {light: [(at, _on(state)) for at, state, _ in _rows(light_rows.get(light, []))] for light in lights}

    # The room "would have" its lights on while it owns them.
    routine_timeline = [(at, None if state == "manual" else state == "owned") for at, state, _ in status]
    log_only = windows([(at, state) for at, state, _ in modes], MODE_LOG_ONLY, until=end)
    parity = compare(
        within(edges(routine_timeline), log_only),
        within(edges(any_on(per_light)), log_only),
        timedelta(seconds=msg["tolerance_s"]),
    )
    connection.send_result(
        msg["id"],
        {
            "since": start.isoformat(),
            "until": end.isoformat(),
            "status": [{"at": at.isoformat(), "state": state, "reason": attrs.get("reason")} for at, state, attrs in status],
            "modes": [{"at": at.isoformat(), "mode": state} for at, state, _ in modes],
            "lights": {
                light: [{"at": at.isoformat(), "on": on} for at, on in rows] for light, rows in per_light.items()
            },
            "log_only": [[a.isoformat(), b.isoformat()] for a, b in log_only],
            "parity": {
                "matched": parity.matched,
                "total": parity.total,
                "pairs": [
                    {
                        "on": p.on,
                        "routine_at": _iso(p.routine_at),
                        "light_at": _iso(p.light_at),
                        "delta_s": p.delta_s,
                    }
                    for p in parity.pairs
                ],
            },
        },
    )
