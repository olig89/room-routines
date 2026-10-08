"""Changing the stored settings. Shared by the settings form and the panel.

Every function takes the entry's options and returns a new, validated copy;
nothing here touches Home Assistant, so the rules live in one place.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from .const import CONF_ALT_DAYS, CONF_HIDDEN_AREAS, CONF_HOUSE_RULES, CONF_PERIODS, CONF_ROOMS, CONF_TRACKS, clean_options
from .core.rules import condition_from, condition_to, rule_from, rule_to
from .core.signals import signal_from, signal_to
from .core.serial import first_look, look_from, look_to, room_from, schedule_from, tracks_from, tracks_to
from .core.tracks import DIM, NORMAL, TRACKS

LOOK_KEYS = {NORMAL: "looks", DIM: "dim_looks"}
# Someone's there looks: brighter while someone's there, over a running routine.
SOMEONE_KEYS = {NORMAL: "someone_looks", DIM: "someone_dim_looks"}
ALL_LOOK_KEYS = (*LOOK_KEYS.values(), *SOMEONE_KEYS.values())
LAYERS = ("base", "someone")


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
    # Time-based routines (all optional: a room with none of them is a motion room).
    room["on_by_hand"] = user_input.get("on_by_hand", room.get("on_by_hand")) or "leave"
    room["hand_hold"] = user_input.get("hand_hold", room.get("hand_hold")) or "until_off"
    room["hand_minutes"] = int(user_input.get("hand_minutes", room.get("hand_minutes")) or 30)
    sensors = set(room["triggers"]) | set(room["holds"])
    room["ends"] = [s for s in user_input.get("ends", room.get("ends")) or [] if s in sensors]
    room["blends"] = {
        str(p): int(m) for p, m in (user_input.get("blends", room.get("blends")) or {}).items() if m
    }
    room["period_starts"] = {
        str(p): str(t)[:5] for p, t in (user_input.get("period_starts", room.get("period_starts")) or {}).items() if t
    }
    room["timers"] = [dict(t) for t in user_input.get("timers", room.get("timers")) or []]
    try:
        for key in ("starters", "only_when"):
            room[key] = [condition_to(condition_from(c)) for c in user_input.get(key, room.get(key)) or []]
        room["rules"] = [
            rule_to(rule_from({k: v for k, v in r.items() if k != "rooms"}))
            for r in user_input.get("rules", room.get("rules")) or []
        ]
    except (ValueError, KeyError, TypeError) as err:
        raise SettingsError("invalid_rules") from err
    # Signals: only the room's own lights; one left with no lights goes.
    signals = []
    try:
        for data in user_input.get("signals", room.get("signals")) or []:
            data = dict(data)
            data["lights"] = {l: t for l, t in (data.get("lights") or {}).items() if l in room["lights"]}
            if data["lights"]:
                signals.append(signal_to(signal_from(data)))
    except (ValueError, KeyError, TypeError) as err:
        raise SettingsError("invalid_signals") from err
    room["signals"] = signals
    if not room["name"]:
        raise SettingsError("no_name")
    # A room needs lights; sensors are optional (a room can run on timers, buttons
    # or lights switched on by hand).
    if not room["lights"]:
        raise SettingsError("invalid_room")
    # Looks may only mention the room's lights.
    kept = set(room["lights"])
    for key in ALL_LOOK_KEYS:
        for look in (room.get(key) or {}).values():
            if "lights" in look:
                look["lights"] = {light: t for light, t in look["lights"].items() if light in kept}
    try:
        room_from(room)
    except (ValueError, KeyError, TypeError) as err:
        raise SettingsError("invalid_room") from err
    return room


def _check_times(options: Mapping[str, Any], room: Mapping[str, Any]) -> None:
    """A room's own period times must still give every period its own start."""
    try:
        schedule_from(options).with_starts(room_from(room).period_starts)
    except ValueError as err:
        raise SettingsError("invalid_room_times") from err


def add_room(options: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """A new room, starting with every light on at its last brightness."""
    out = _options(options)
    room = clean_room(data, None)
    _check_times(out, room)
    room["looks"] = first_look(room["lights"], schedule_from(out))
    room_id = uuid.uuid4().hex[:8]
    out[CONF_ROOMS][room_id] = room
    return out, room_id


def update_room(options: Mapping[str, Any], room_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    out = _options(options)
    previous = _room(out, room_id)
    room = clean_room({**previous, **data}, previous)
    _check_times(out, room)
    out[CONF_ROOMS][room_id] = room
    return out


def remove_room(options: Mapping[str, Any], room_id: str) -> dict[str, Any]:
    out = _options(options)
    area_id = _room(out, room_id).get("area_id")
    del out[CONF_ROOMS][room_id]
    for rule in out.get(CONF_HOUSE_RULES) or []:
        if rule.get("rooms") and room_id in rule["rooms"]:
            rule["rooms"] = [r for r in rule["rooms"] if r != room_id]
    # a rule left with no rooms would cover every room: drop it instead
    out[CONF_HOUSE_RULES] = [
        r for r in out.get(CONF_HOUSE_RULES) or [] if "rooms" not in r or r["rooms"]
    ]
    # A room made from an area would come straight back: hide the area instead.
    if area_id and not any(r.get("area_id") == area_id for r in out[CONF_ROOMS].values()):
        hidden = set(out.get(CONF_HIDDEN_AREAS) or [])
        hidden.add(area_id)
        out[CONF_HIDDEN_AREAS] = sorted(hidden)
    return out


def unhide_area(options: Mapping[str, Any], area_id: str) -> dict[str, Any]:
    out = _options(options)
    out[CONF_HIDDEN_AREAS] = [a for a in out.get(CONF_HIDDEN_AREAS) or [] if a != area_id]
    return out


def add_area_rooms(options: Mapping[str, Any], areas: Mapping[str, tuple[str, list[str]]]) -> dict[str, Any] | None:
    """A room for every area with lights that isn't a room yet and isn't hidden.

    ``areas`` maps an area id to (its name, its lights). New rooms start off,
    with no sensors, so nothing changes until someone gives them something to
    do. Returns None when there is nothing to add.
    """
    out = _options(options)
    taken = {r.get("area_id") for r in out[CONF_ROOMS].values()}
    hidden = set(out.get(CONF_HIDDEN_AREAS) or [])
    added = False
    for area_id, (name, lights) in sorted(areas.items(), key=lambda a: a[1][0]):
        if area_id in taken or area_id in hidden or not lights:
            continue
        room = clean_room(
            {
                "name": name, "area_id": area_id, "lights": sorted(lights),
                "timeout_s": 300, "fade_out_s": 15, "cooldown_s": 30, "drift_s": 90,
                "threshold_lux": None,
            },
            None,
        )
        room["looks"] = first_look(room["lights"], schedule_from(out))
        room["start_mode"] = "off"
        out[CONF_ROOMS][f"area_{area_id}"] = room
        added = True
    return out if added else None


def set_house_rules(options: Mapping[str, Any], rules: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The house's "while X" rules, each for chosen rooms (none = every room)."""
    out = _options(options)
    known = set(out[CONF_ROOMS])
    try:
        cleaned = []
        for data in rules:
            rule = rule_from(data)
            if any(r not in known for r in rule.rooms):
                raise SettingsError("unknown_room")
            cleaned.append(rule_to(rule))
    except (ValueError, KeyError, TypeError) as err:
        raise SettingsError("invalid_rules") from err
    out[CONF_HOUSE_RULES] = cleaned
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
    removed), so every room's looks follow their period. A removed period's look
    passes to the period after it when that one had none of its own (it was
    using the removed period's look, so it keeps it); otherwise it's dropped.
    """
    old_order = schedule_from(options).order()
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
        before = {k: dict(room.get(k) or {}) for k in list(room) if k.endswith("looks")}
        for key in ALL_LOOK_KEYS:
            if key not in room:
                continue
            looks: dict[str, Any] = {}
            old_looks = before.get(key) or {}
            # A Dark Day look stops at a period's own Normal look too.
            normal = before.get(key.replace("dim_looks", "looks")) if "dim_looks" in key else None
            # Built fresh rather than edited in place, so two periods can swap names.
            for period, look in old_looks.items():
                target = renames.get(period, period)
                if target and target in names:
                    looks[target] = look
            _carry_removed(old_order, old_looks, looks, renames, names, normal)
            room[key] = looks
        # A room's own times and blends follow the period too.
        for key in ("blends", "period_starts"):
            if room.get(key):
                moved = {}
                for period, value in room[key].items():
                    target = renames.get(period, period)
                    if target and target in names:
                        moved[target] = value
                room[key] = moved
    # A rule using another period's look follows a rename (a removed period: the rule goes).
    def _moved_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = []
        for rule in rules:
            if rule.get("period"):
                target = renames.get(rule["period"], rule["period"])
                if not target or target not in names:
                    continue
                rule = {**rule, "period": target}
            kept.append(rule)
        return kept

    for room in out[CONF_ROOMS].values():
        if room.get("rules"):
            room["rules"] = _moved_rules(room["rules"])
    if out.get(CONF_HOUSE_RULES):
        out[CONF_HOUSE_RULES] = _moved_rules(out[CONF_HOUSE_RULES])
    tracks = out.get(CONF_TRACKS)
    if tracks and tracks.get("periods") is not None:
        # The Dark Day periods follow renames too (a removed one drops out).
        moved = [renames.get(p, p) for p in tracks["periods"]]
        out[CONF_TRACKS] = {**tracks, "periods": [p for p in moved if p and p in names]}
    out[CONF_PERIODS] = clean_rows
    out[CONF_ALT_DAYS] = days
    return out


def _carry_removed(
    old_order: tuple[str, ...],
    old_looks: Mapping[str, Any],
    looks: dict[str, Any],
    renames: Mapping[str, str | None],
    names: set[str],
    normal: Mapping[str, Any] | None = None,
) -> None:
    """Give each removed period's look to the next surviving period that used it.

    A period without a look of its own uses the one before it, so the periods
    after a removed one were showing its look; the first of them keeps it. For
    Dark Day looks, ``normal`` is the Normal looks: a period with its own Normal
    look never used an earlier Dark Day look.
    """
    n = len(old_order)

    def own(p: str) -> bool:
        return p in old_looks or (normal is not None and p in normal)

    def survivor(p: str) -> str | None:
        target = renames.get(p, p)
        return target if target and target in names else None

    for i, period in enumerate(old_order):
        if survivor(period) is not None or period not in old_looks:
            continue
        for step in range(1, n):
            nxt = old_order[(i + step) % n]
            if (target := survivor(nxt)) is not None:
                if not own(nxt) and target not in looks:
                    looks[target] = old_looks[period]
                break
            if own(nxt):
                break  # another removed period with its own look takes over from here


# ---- looks ---------------------------------------------------------------------


def set_look(
    options: Mapping[str, Any],
    room_id: str,
    period: str,
    look: Mapping[str, Any] | None,
    track: str = NORMAL,
    layer: str = "base",
) -> dict[str, Any]:
    """A room's look for one period on one track. ``None`` removes it: on Normal
    the period then borrows the previous period's; on a Dark Day it uses its Normal look.
    ``layer`` "someone" is the Someone's there table (brighter over a running routine)."""
    out = _options(options)
    room = _room(out, room_id)
    if period not in {p.name for p in schedule_from(out).periods}:
        raise SettingsError("unknown_period")
    if track not in TRACKS:
        raise SettingsError("unknown_track")
    if layer not in LAYERS:
        raise SettingsError("unknown_layer")
    key = (SOMEONE_KEYS if layer == "someone" else LOOK_KEYS)[track]
    looks = room.setdefault(key, {})
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


# ---- dark days -----------------------------------------------------------------


def set_tracks(options: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    """Dark Days: on or off, where the light reading comes from, the periods
    they apply to, the thresholds and the Dark Day brightness."""
    out = _options(options)
    periods = data.get("periods")
    if periods is not None:
        known = set(schedule_from(out).order())
        periods = [p for p in periods if p in known]
    row = {
        "on": bool(data.get("on", False)),
        "weather": bool(data.get("weather", True)),
        "sensor": data.get("sensor") or None,
        "fallback": data.get("fallback") or None,
        "first": data.get("first", "weather"),
        "periods": periods,
        "dark_below_pct": data.get("dark_below_pct", 40),
        "normal_above_pct": data.get("normal_above_pct", 55),
        "brightness_pct": data.get("brightness_pct", 100),
        "dark_below_wm2": data.get("dark_below_wm2", 150),
    }
    try:
        settings = tracks_from({CONF_TRACKS: row})
    except (ValueError, TypeError) as err:
        raise SettingsError("invalid_tracks") from err
    out[CONF_TRACKS] = tracks_to(settings)
    return out
