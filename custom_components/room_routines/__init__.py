"""Room Routines: motion lights that follow the household's daily routine.

The decisions live in ``core/`` (no Home Assistant imports); ``house.py`` runs
them inside Home Assistant.
"""

from __future__ import annotations

import copy
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, SERVICE_SET_LOOK
from .core.looks import OFF, LightTarget, Look
from .core.room import RoomConfig
from .core.serial import look_to
from .house import House, brightness_pct, room_of

PLATFORMS = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type RoomRoutinesConfigEntry = ConfigEntry[House]

LOOK_CURRENT = "current"
LOOK_NOTHING = "nothing"
LOOK_BORROW = "borrow"

SET_LOOK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("period"): cv.string,
        vol.Optional("look", default=LOOK_CURRENT): vol.In([LOOK_CURRENT, LOOK_NOTHING, LOOK_BORROW]),
    }
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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def set_look(call: ServiceCall) -> None:
        found = room_of(hass, call.data[ATTR_ENTITY_ID])
        if found is None:
            raise ServiceValidationError(
                f"{call.data[ATTR_ENTITY_ID]} is not a Room Routines room status sensor"
            )
        house, runner = found
        period = call.data.get("period") or house.period
        if period not in house.schedule.order():
            raise ServiceValidationError(
                f"There is no period called {period!r}. Periods: {', '.join(house.schedule.order())}"
            )
        options: dict[str, Any] = copy.deepcopy(dict(house.entry.options))
        looks = options["rooms"][runner.room_id].setdefault("looks", {})
        kind = call.data["look"]
        if kind == LOOK_BORROW:
            looks.pop(period, None)
        elif kind == LOOK_NOTHING:
            looks[period] = {"nothing": True}
        else:
            look = capture(hass, runner.config)
            if not look.lit():
                raise ServiceValidationError(
                    "None of the room's lights are on. Set them up as you want them first, "
                    "or save 'nothing' to keep the room dark in this period."
                )
            looks[period] = look_to(look)
        hass.config_entries.async_update_entry(house.entry, options=options)

    hass.services.async_register(DOMAIN, SERVICE_SET_LOOK, set_look, schema=SET_LOOK_SCHEMA)
    return True


def _remove_old_rooms(hass: HomeAssistant, entry: RoomRoutinesConfigEntry, house: House) -> None:
    """Drop the device (and so the entities) of any room that has been removed."""
    registry = dr.async_get(hass)
    keep = {entry.entry_id} | {f"{entry.entry_id}_{room_id}" for room_id in house.rooms}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not any(ident in keep for domain, ident in device.identifiers if domain == DOMAIN):
            registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> bool:
    house = House(hass, entry)
    entry.runtime_data = house
    _remove_old_rooms(hass, entry, house)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    house.start()
    entry.async_on_unload(house.stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> None:
    if entry.runtime_data.take_looks(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
