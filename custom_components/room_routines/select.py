"""The house's current period, and each room's mode (off / log only / live)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RoomRoutinesConfigEntry
from .const import MODE_LOG_ONLY, MODES
from .entity import HouseEntity, RoomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomRoutinesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    house = entry.runtime_data
    async_add_entities(
        [PeriodSelect(house), *(RoomModeSelect(house, runner) for runner in house.rooms.values())]
    )


class PeriodSelect(HouseEntity, SelectEntity):
    """Shows the current period; choosing one holds it until the next scheduled start."""

    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, house) -> None:
        super().__init__(house, "period")
        self._attr_options = list(house.schedule.order())

    @property
    def current_option(self) -> str:
        return self.house.period

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "next_change": self.house.next_start.isoformat() if self.house.next_start else None,
            "chosen_by_hand": self.house.overridden,
        }

    async def async_select_option(self, option: str) -> None:
        self.house.override_period(option)


class RoomModeSelect(RoomEntity, SelectEntity, RestoreEntity):
    _attr_options = list(MODES)
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, house, runner) -> None:
        super().__init__(house, runner, "mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self.runner.set_mode(last.state if last and last.state in MODES else MODE_LOG_ONLY)

    @property
    def current_option(self) -> str:
        return self.runner.mode

    async def async_select_option(self, option: str) -> None:
        self.runner.set_mode(option)
