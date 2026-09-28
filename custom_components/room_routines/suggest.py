"""Suggestions for a new room, from what is in its Home Assistant area.

Only suggestions: the room form shows them pre-filled and every field accepts
any entity. Nothing here decides what a sensor can do; the person does.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

PRESENCE_CLASSES = {"motion", "occupancy", "presence"}


def _in_area(hass: HomeAssistant, area_id: str) -> list[er.RegistryEntry]:
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    found = {e.entity_id: e for e in er.async_entries_for_area(entities, area_id)}
    for device in dr.async_entries_for_area(devices, area_id):
        for e in er.async_entries_for_device(entities, device.id):
            if e.area_id is None:  # an entity's own area wins over its device's
                found.setdefault(e.entity_id, e)
    return [e for e in found.values() if e.disabled_by is None and e.hidden_by is None]


def _device_class(hass: HomeAssistant, entry: er.RegistryEntry) -> str | None:
    state = hass.states.get(entry.entity_id)
    return entry.device_class or entry.original_device_class or (
        state.attributes.get("device_class") if state else None
    )


def room_suggestions(hass: HomeAssistant, area_id: str | None) -> dict[str, list[str] | str]:
    if not area_id:
        return {}
    lights, sensors, lux = [], [], []
    for entry in sorted(_in_area(hass, area_id), key=lambda e: e.entity_id):
        cls = _device_class(hass, entry)
        if entry.domain == "light":
            lights.append(entry.entity_id)
        elif entry.domain == "binary_sensor" and cls in PRESENCE_CLASSES:
            sensors.append(entry.entity_id)
        elif entry.domain == "sensor" and cls == "illuminance":
            lux.append(entry.entity_id)
    out: dict[str, list[str] | str] = {}
    if lights:
        out["lights"] = lights
    if sensors:
        out["triggers"] = sensors
    if lux:
        out["lux_sensor"] = lux[0]
    return out
