"""Room Routines: motion lights that follow the household's daily routine.

The decisions live in ``core/`` (no Home Assistant imports); ``house.py`` runs
them inside Home Assistant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import ANY_SIGNAL, DOMAIN, NAME, PANEL_COMPONENT, PANEL_URL, SERVICE_SET_LOOK, STATIC_URL, clean_options
from .core.serial import look_to
from .house import House, capture, room_of
from .websocket import async_register_websocket

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

async def _async_register_panel(hass: HomeAssistant) -> None:
    """Sidebar page. Skipped quietly where the frontend isn't loaded (tests)."""
    if hass.data.get(f"{DOMAIN}_panel") or "frontend" not in hass.config.components:
        return
    from homeassistant.components import panel_custom
    from homeassistant.components.http import StaticPathConfig

    www = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(www), False)])
    version = (await hass.async_add_executor_job((www / "room-routines-panel.js").stat)).st_mtime_ns
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=NAME,
        sidebar_icon="mdi:lightbulb-auto",
        module_url=f"{STATIC_URL}/room-routines-panel.js?v={version}",
        # Admins only for now. The page itself hides the deeper settings from
        # non-admins and the websocket refuses them, so opening it up later is
        # just this flag.
        require_admin=True,
    )
    hass.data[f"{DOMAIN}_panel"] = True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_websocket(hass)
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
        options: dict[str, Any] = clean_options(house.entry.options)
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
    if (options := clean_options(entry.options)) != dict(entry.options):
        # Keys outside periods / alt_days / rooms were written by something other than
        # this integration's own settings; drop them before anything reads the options.
        hass.config_entries.async_update_entry(entry, options=options)
    house = House(hass, entry)
    entry.runtime_data = house
    _remove_old_rooms(hass, entry, house)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    house.start()
    entry.async_on_unload(house.stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await _async_register_panel(hass)
    async_dispatcher_send(hass, ANY_SIGNAL)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> None:
    if entry.runtime_data.take_looks(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    async_dispatcher_send(hass, ANY_SIGNAL)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> None:
    if hass.data.pop(f"{DOMAIN}_panel", None):
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, PANEL_URL)
