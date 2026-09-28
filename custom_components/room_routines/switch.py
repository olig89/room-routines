"""Stealth mode: every motion sensor reads as "nobody here" while it is on."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RoomRoutinesConfigEntry
from .entity import HouseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomRoutinesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([StealthSwitch(entry.runtime_data)])


class StealthSwitch(HouseEntity, SwitchEntity, RestoreEntity):
    def __init__(self, house) -> None:
        super().__init__(house, "stealth_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            self.house.set_stealth(True)

    @property
    def is_on(self) -> bool:
        return self.house.stealth

    @property
    def icon(self) -> str:
        return "mdi:incognito" if self.house.stealth else "mdi:incognito-off"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.house.set_stealth(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.house.set_stealth(False)
