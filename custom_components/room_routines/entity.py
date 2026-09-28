"""Shared entity plumbing: one device for the house, one per room."""

from __future__ import annotations

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME, house_signal, room_signal
from .house import House, RoomRunner


def house_device(house: House) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, house.entry.entry_id)}, name=NAME)


class HouseEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, house: House, key: str) -> None:
        self.house = house
        self._attr_unique_id = f"{house.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = house_device(house)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, house_signal(self.house.entry.entry_id), self.async_write_ha_state)
        )


class RoomEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, house: House, runner: RoomRunner, key: str) -> None:
        self.house = house
        self.runner = runner
        self._attr_unique_id = f"{house.entry.entry_id}_{runner.room_id}_{key}"
        self._attr_translation_key = key
        area = None
        if runner.area_id:
            entry = ar.async_get(house.hass).async_get_area(runner.area_id)
            area = entry.name if entry else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{house.entry.entry_id}_{runner.room_id}")},
            name=f"{runner.config.name} routine",
            suggested_area=area,
        )

    async def async_added_to_hass(self) -> None:
        signal = room_signal(self.house.entry.entry_id, self.runner.room_id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self.async_write_ha_state))
        self.async_on_remove(
            async_dispatcher_connect(self.hass, house_signal(self.house.entry.entry_id), self.async_write_ha_state)
        )
