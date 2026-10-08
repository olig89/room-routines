"""Room Routines: motion lights that follow the household's daily routine.

The decisions live in ``core/`` (no Home Assistant imports); ``house.py`` runs
them inside Home Assistant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import (
    ANY_SIGNAL,
    DOMAIN,
    NAME,
    PANEL_COMPONENT,
    PANEL_URL,
    SERVICE_SET_LOOK,
    SERVICE_SWITCH_OFF,
    SERVICE_SWITCH_ON,
    STATIC_URL,
    clean_options,
)
from .core.serial import look_to
from .core.tracks import NORMAL, TRACKS
from .house import House, capture, room_of
from .settings import LOOK_KEYS, add_area_rooms
from .websocket import async_register_websocket

PLATFORMS = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type RoomRoutinesConfigEntry = ConfigEntry[House]

LOOK_CURRENT = "current"
LOOK_SCENE = "scene"
LOOK_NOTHING = "nothing"
LOOK_BORROW = "borrow"

SET_LOOK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional("period"): cv.string,
        vol.Optional("track", default=NORMAL): vol.In(list(TRACKS)),
        vol.Optional("look", default=LOOK_CURRENT): vol.In([LOOK_CURRENT, LOOK_SCENE, LOOK_NOTHING, LOOK_BORROW]),
        vol.Optional("scene"): cv.entity_domain("scene"),
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
        looks = options["rooms"][runner.room_id].setdefault(LOOK_KEYS[call.data["track"]], {})
        kind = call.data["look"]
        if kind == LOOK_BORROW:
            looks.pop(period, None)
        elif kind == LOOK_NOTHING:
            looks[period] = {"nothing": True}
        elif kind == LOOK_SCENE:
            if not call.data.get("scene"):
                raise ServiceValidationError("Choose the scene to turn on.")
            looks[period] = {"scene": call.data["scene"]}
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

    def _runners(call: ServiceCall):
        found = []
        for entity_id in call.data[ATTR_ENTITY_ID]:
            room = room_of(hass, entity_id)
            if room is None:
                raise ServiceValidationError(f"{entity_id} is not a Room Routines room status sensor")
            found.append(room[1])
        return found

    async def switch_on(call: ServiceCall) -> None:
        """Start the routine: the current look, then following the day (a wall button)."""
        for runner in _runners(call):
            runner.start_routine("switched on by an action")

    async def switch_off(call: ServiceCall) -> None:
        for runner in _runners(call):
            runner.stop_routine("switched off by an action")

    schema = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})
    hass.services.async_register(DOMAIN, SERVICE_SWITCH_ON, switch_on, schema=schema)
    hass.services.async_register(DOMAIN, SERVICE_SWITCH_OFF, switch_off, schema=schema)
    return True


def _area_lights(hass: HomeAssistant) -> dict[str, tuple[str, list[str]]]:
    """Every area's lights: not disabled, not hidden, and not a group of other lights."""
    areas = ar.async_get(hass)
    devices = dr.async_get(hass)
    found: dict[str, tuple[str, list[str]]] = {
        area.id: (area.name, []) for area in areas.async_list_areas()
    }
    for entity in er.async_get(hass).entities.values():
        if entity.domain != "light" or entity.disabled_by or entity.hidden_by:
            continue
        if entity.platform == "group":
            continue
        state = hass.states.get(entity.entity_id)
        if state is not None and state.attributes.get("entity_id"):
            continue  # a Hue room/zone or other group that lists its member lights
        area_id = entity.area_id
        if area_id is None and entity.device_id:
            device = devices.async_get(entity.device_id)
            area_id = device.area_id if device else None
        if area_id in found:
            found[area_id][1].append(entity.entity_id)
    return found


@callback
def _add_area_rooms(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> None:
    """Make every area with lights a room (they start off). Saving the options
    reloads the integration with them."""
    options = add_area_rooms(entry.options, _area_lights(hass))
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)


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
    await house.async_load()
    entry.runtime_data = house
    _remove_old_rooms(hass, entry, house)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    house.start()
    entry.async_on_unload(house.stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # Rooms for new areas: once everything has started, so the lights' groups are known.
    if hass.state is CoreState.running:
        _add_area_rooms(hass, entry)
    else:
        # A one-time listener removes itself when it fires; removing it again on
        # unload makes Home Assistant log an error, so forget it once it has run.
        unsub: CALLBACK_TYPE | None = None

        @callback
        def _started(_event: Event) -> None:
            nonlocal unsub
            unsub = None
            _add_area_rooms(hass, entry)

        @callback
        def _cancel() -> None:
            if unsub is not None:
                unsub()

        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)
        entry.async_on_unload(_cancel)
    await _async_register_panel(hass)
    async_dispatcher_send(hass, ANY_SIGNAL)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> None:
    if entry.runtime_data.take_looks(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Write what each room is doing now, so a reload carries on where it was.
    await entry.runtime_data.async_save_rooms()
    async_dispatcher_send(hass, ANY_SIGNAL)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: RoomRoutinesConfigEntry) -> None:
    if hass.data.pop(f"{DOMAIN}_panel", None):
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, PANEL_URL)
