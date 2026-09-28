"""Changing the stored settings. Shared by the settings form and the panel.

Every function takes the entry's options and returns a new, validated copy;
nothing here touches Home Assistant, so the rules live in one place.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from .const import CONF_ALT_DAYS, CONF_PERIODS, CONF_ROOMS, clean_options
from .core.serial import first_look, look_from, look_to, room_from, schedule_from


class SettingsError(ValueError):
    """Not a valid change. ``key`` names the problem (it matches the form's error keys)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _options(options: Mapping[str, Any]) -> dict[str, Any]:
    out = clean_options(options)
    out.setdefault(CONF_ROOMS, {})
    return out


def _room(options: dict[str, Any], room_id: str) -> dict[str, Any]:
    try:
        return options[CONF_ROOMS][room_id]
    except KeyError as err:
        raise SettingsError("unknown_room") from err


# ---- rooms ---------------------------------------------------------------------


def clean_room(user_input: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Stored room settings from a form. Raises SettingsError if they don't make a room."""
    room = copy.deepcopy(dict(previous or {}))
    room.update(
        name=str(user_input.get("name", room.get("name", ""))).strip(),
        area_id=user_input.get("area_id", room.get("area_id")) or None,
        lights=list(user_input.get("lights") or []),
        triggers=list(user_input.get("triggers") or []),
        holds=list(user_input.get("holds") or []),
        lux_sensor=user_input.get("lux_sensor") or None,
        threshold_lux=user_input.get("threshold_lux"),
        timeout_s=int(user_input["timeout_s"]),
        fade_out_s=int(user_input["fade_out_s"]),
        cooldown_s=int(user_input["cooldown_s"]),
        drift_s=int(user_input["drift_s"]),
    )
    # Only the room's own lights can be marked as fading by themselves.
    room["self_fading"] = [light for light in user_input.get("self_fading") or [] if light in room["lights"]]
    if not room["name"]:
        raise SettingsError("no_name")
    if not room["lights"] or not room["triggers"]:
        raise SettingsError("invalid_room")
    # Looks may only mention the room's lights.
    kept = set(room["lights"])
    for look in (room.get("looks") or {}).values():
        if "lights" in look:
            look["lights"] = {light: t for light, t in look["lights"].items() if light in kept}
    try:
        room_from(room)
    except (ValueError, KeyError, TypeError) as err:
        raise SettingsError("invalid_room") from err
    return room


def add_room(options: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """A new room, starting with every light on at its last brightness."""
    out = _options(options)
    room = clean_room(data, None)
    room["looks"] = first_look(room["lights"], schedule_from(out))
    room_id = uuid.uuid4().hex[:8]
    out[CONF_ROOMS][room_id] = room
    return out, room_id


def update_room(options: Mapping[str, Any], room_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    out = _options(options)
    previous = _room(out, room_id)
    out[CONF_ROOMS][room_id] = clean_room({**previous, **data}, previous)
    return out


def remove_room(options: Mapping[str, Any], room_id: str) -> dict[str, Any]:
    out = _options(options)
    _room(out, room_id)
    del out[CONF_ROOMS][room_id]
    return out


# ---- periods -------------------------------------------------------------------


def _hhmm(value: Any) -> str | None:
    return str(value).strip()[:5] if value else None


def set_periods(
    options: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    alt_days: Iterable[int],
    renames: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The whole period list at once.

    ``renames`` maps an old period name to its new name (or ``None`` if it was
    removed), so every room's looks follow their period. Looks for periods that
    no longer exist are dropped; a room then borrows the previous period's look.
    """
    out = _options(options)
    clean_rows = [
        {"name": str(r.get("name", "")).strip(), "start": _hhmm(r.get("start")), "alt_start": _hhmm(r.get("alt_start"))}
        for r in rows
    ]
    days = sorted({int(d) for d in alt_days})
    if not clean_rows:
        raise SettingsError("last_period")
    if any(not r["name"] or not r["start"] for r in clean_rows):
        raise SettingsError("invalid_periods")
    try:
        schedule_from({CONF_PERIODS: clean_rows, CONF_ALT_DAYS: days})
    except (ValueError, TypeError) as err:
        raise SettingsError("invalid_periods") from err
    names = {r["name"] for r in clean_rows}
    renames = dict(renames or {})
    for room in out[CONF_ROOMS].values():
        looks: dict[str, Any] = {}
        # Built fresh rather than edited in place, so two periods can swap names.
        for period, look in (room.get("looks") or {}).items():
            target = renames.get(period, period)
            if target and target in names:
                looks[target] = look
        room["looks"] = looks
    out[CONF_PERIODS] = clean_rows
    out[CONF_ALT_DAYS] = days
    return out


# ---- looks ---------------------------------------------------------------------


def set_look(
    options: Mapping[str, Any], room_id: str, period: str, look: Mapping[str, Any] | None
) -> dict[str, Any]:
    """A room's look for one period. ``None`` removes it (the period borrows the previous one)."""
    out = _options(options)
    room = _room(out, room_id)
    if period not in {p.name for p in schedule_from(out).periods}:
        raise SettingsError("unknown_period")
    looks = room.setdefault("looks", {})
    if look is None:
        looks.pop(period, None)
        return out
    data = dict(look)
    if "lights" in data:
        data["lights"] = {light: t for light, t in (data["lights"] or {}).items() if light in room["lights"]}
    try:
        looks[period] = look_to(look_from(data))
    except (ValueError, TypeError, KeyError) as err:
        raise SettingsError("invalid_look") from err
    return out
