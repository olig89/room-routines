"""Whether today is a Dark Day, as plain on/off for automations and dashboards.

Read-only: choosing the day by hand stays on the Day select. This follows
whatever is in force, a day chosen by hand included.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoomRoutinesConfigEntry
from .core.tracks import DIM
from .entity import HouseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomRoutinesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DarkDaySensor(entry.runtime_data)])


class DarkDaySensor(HouseEntity, BinarySensorEntity):
    _platform_domain = "binary_sensor"

    def __init__(self, house) -> None:
        super().__init__(house, "dark_day")

    @property
    def icon(self) -> str:
        return "mdi:weather-cloudy" if self.is_on else "mdi:weather-sunny"

    @property
    def is_on(self) -> bool:
        return self.house.track == DIM

    @property
    def extra_state_attributes(self) -> dict:
        status = self.house.dark_day_status()
        return {
            "reason": status["reason"],
            "source": status["source"],
            "clear_day_pct": status["pct"],
            "chosen_by_hand": self.house.chooser.by_hand,
            "enabled": self.house.tracks.enabled,
        }
