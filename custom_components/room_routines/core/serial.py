"""Settings as stored in the config entry, to and from the core's types.

The config entry's options hold plain JSON:

    {
      "periods": [{"name": "Overnight", "start": "23:00", "alt_start": null}, ...],
      "alt_days": [5, 6],
      "tracks": {"on": true, "weather": true, "sensor": "sensor.window_lux", "fallback": null,
                 "first": "weather", "periods": ["Morning", "Day"],
                 "dark_below_pct": 40, "normal_above_pct": 55, "brightness_pct": 50},
      "rooms": {
        "<room id>": {
          "name": "Downstairs Toilet",
          "area_id": "wc",
          "lights": [...], "triggers": [...], "holds": [...],
          "lux_sensor": "sensor.downstairs_toilet_lux",
          "threshold_lux": 50, "timeout_s": 30, "cooldown_s": 30, "drift_s": 90, "fade_out_s": 15,
          "powered_by": {"<bulb>": "<power circuit>"},
          "blinds_with_periods": false,
          "start_mode": "off",            (mode a new room starts in; rooms made from areas start off)
          "on_by_hand": "leave" | "routine",
          "blends": {"Day": 300},          (minutes before the next period to start blending)
          "period_starts": {"Evening": "18:00"},
          "timers": [{"at": "08:30", "action": "on", "days": "workdays", "only_dark": true,
                      "only_home": ["person.x"]}, ...],
          "looks": {"<period>": {"nothing": true} | {"scene": "scene.x"} | {"lights": {...}, "blinds": {...}}},
          "dim_looks": {"<period>": ...}   (same form; the Dark Day track)
        }
      }
    }

"hidden_areas": [area ids] lists areas the user doesn't want as rooms.

No "tracks" (or "on" false) means every day is a Normal day.

A look's light is ``{"on": true, "brightness_pct": 60, "color_temp_kelvin": 2700}``;
``brightness_pct`` left out means "on at its last brightness".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time, timedelta
from typing import Any

from .looks import NOTHING, LightTarget, Look
from .periods import Period, Schedule, default_schedule
from .room import RoomConfig
from .timers import timer_from
from .tracks import DEFAULT_BRIGHTNESS_PCT, DEFAULT_DARK_BELOW, DEFAULT_NORMAL_ABOVE, WEATHER, TrackSettings

DEFAULT_THRESHOLD_LUX = 50.0
DEFAULT_TIMEOUT_S = 30
DEFAULT_COOLDOWN_S = 30
DEFAULT_DRIFT_S = 90
DEFAULT_FADE_OUT_S = 15


def _time(text: str | None) -> time | None:
    if not text:
        return None
    return time.fromisoformat(text)


def _hhmm(t: time | None) -> str | None:
    return None if t is None else t.strftime("%H:%M")


# ---- periods ----------------------------------------------------------------


def schedule_from(options: Mapping[str, Any]) -> Schedule:
    rows = options.get("periods")
    if not rows:
        return default_schedule()
    return Schedule(
        tuple(Period(r["name"], _time(r["start"]), _time(r.get("alt_start"))) for r in rows),
        frozenset(int(d) for d in options.get("alt_days") or ()),
    )


def schedule_to(schedule: Schedule) -> dict[str, Any]:
    return {
        "periods": [
            {"name": p.name, "start": _hhmm(p.start), "alt_start": _hhmm(p.alt_start)}
            for p in schedule.periods
        ],
        "alt_days": sorted(schedule.alt_days),
    }


# ---- looks -------------------------------------------------------------------


def target_from(data: Mapping[str, Any]) -> LightTarget:
    rgb = data.get("rgb")
    return LightTarget(
        bool(data.get("on", True)),
        data.get("brightness_pct"),
        data.get("color_temp_kelvin"),
        tuple(rgb) if rgb else None,
    )


def target_to(target: LightTarget) -> dict[str, Any]:
    out: dict[str, Any] = {"on": target.on}
    if target.on:
        if target.brightness_pct is not None:
            out["brightness_pct"] = target.brightness_pct
        if target.color_temp_kelvin is not None:
            out["color_temp_kelvin"] = target.color_temp_kelvin
        if target.rgb is not None:
            out["rgb"] = list(target.rgb)
    return out


def look_from(data: Mapping[str, Any]) -> Look:
    if data.get("nothing"):
        return NOTHING
    if data.get("scene"):
        return Look(scene=str(data["scene"]))
    return Look(
        {light: target_from(t) for light, t in (data.get("lights") or {}).items()},
        {cover: int(pos) for cover, pos in (data.get("blinds") or {}).items()},
    )


def look_to(look: Look) -> dict[str, Any]:
    if look.nothing:
        return {"nothing": True}
    if look.scene:
        return {"scene": look.scene}
    out: dict[str, Any] = {"lights": {light: target_to(t) for light, t in look.lights.items()}}
    if look.blinds:
        out["blinds"] = dict(look.blinds)
    return out


# ---- rooms -------------------------------------------------------------------


def room_from(data: Mapping[str, Any]) -> RoomConfig:
    threshold = data.get("threshold_lux", DEFAULT_THRESHOLD_LUX)
    return RoomConfig(
        name=data["name"],
        lights=tuple(data.get("lights") or ()),
        triggers=tuple(data.get("triggers") or ()),
        holds=tuple(data.get("holds") or ()),
        looks={period: look_from(look) for period, look in (data.get("looks") or {}).items()},
        dim_looks={period: look_from(look) for period, look in (data.get("dim_looks") or {}).items()},
        threshold_lux=None if threshold is None else float(threshold),
        timeout=timedelta(seconds=data.get("timeout_s", DEFAULT_TIMEOUT_S)),
        cooldown=timedelta(seconds=data.get("cooldown_s", DEFAULT_COOLDOWN_S)),
        drift=timedelta(seconds=data.get("drift_s", DEFAULT_DRIFT_S)),
        fade_out=timedelta(seconds=data.get("fade_out_s", DEFAULT_FADE_OUT_S)),
        powered_by=dict(data.get("powered_by") or {}),
        blinds_with_periods=bool(data.get("blinds_with_periods", False)),
        on_by_hand=str(data.get("on_by_hand") or "leave"),
        blends={str(p): float(m) for p, m in (data.get("blends") or {}).items() if m},
        period_starts={str(p): _time(t) for p, t in (data.get("period_starts") or {}).items() if t},
        timers=tuple(timer_from(t) for t in data.get("timers") or ()),
    )


def first_look(lights: list[str], schedule: Schedule) -> dict[str, Any]:
    """A new room's looks: every light on at its last brightness, in every period.

    Stored on the first period of the day only; the others borrow it.
    """
    return {schedule.order()[0]: {"lights": {light: {"on": True} for light in lights}}}


# ---- tracks ------------------------------------------------------------------


def tracks_from(options: Mapping[str, Any]) -> TrackSettings:
    data = options.get("tracks") or {}
    periods = data.get("periods")
    # 0.3.x stored lux thresholds (dim_below/normal_above) and auto_dim_pct:
    # the thresholds don't carry over (they were lux, these are percent of a
    # clear day), auto-dim becomes the Dark Day brightness, and a house that
    # had a sensor chosen keeps Dark Days on.
    return TrackSettings(
        on=bool(data.get("on", bool(data.get("sensor") or data.get("fallback")))),
        weather=bool(data.get("weather", True)),
        sensor=data.get("sensor") or None,
        fallback=data.get("fallback") or None,
        first=str(data.get("first", WEATHER)),
        periods=tuple(str(p) for p in periods) if periods is not None else None,
        dark_below=float(data.get("dark_below_pct", DEFAULT_DARK_BELOW)),
        normal_above=float(data.get("normal_above_pct", DEFAULT_NORMAL_ABOVE)),
        brightness_pct=float(data.get("brightness_pct", data.get("auto_dim_pct", DEFAULT_BRIGHTNESS_PCT))),
    )


def tracks_to(settings: TrackSettings) -> dict[str, Any]:
    return {
        "on": settings.on,
        "weather": settings.weather,
        "sensor": settings.sensor,
        "fallback": settings.fallback,
        "first": settings.first,
        "periods": list(settings.periods) if settings.periods is not None else None,
        "dark_below_pct": settings.dark_below,
        "normal_above_pct": settings.normal_above,
        "brightness_pct": settings.brightness_pct,
    }
