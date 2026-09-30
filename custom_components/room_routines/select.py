"""The house's current period and whether it is a Normal day or a Dark Day, and each room's mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RoomRoutinesConfigEntry
from .const import MODE_LOG_ONLY, MODES
from .core.tracks import TRACKS
from .entity import HouseEntity, RoomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomRoutinesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    house = entry.runtime_data
    async_add_entities(
        [PeriodSelect(house), TrackSelect(house), *(RoomModeSelect(house, runner) for runner in house.rooms.values())]
    )


class PeriodSelect(HouseEntity, SelectEntity):
    """Shows the current period; choosing one holds it until the next scheduled start."""

    _platform_domain = "select"

    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, house) -> None:
        super().__init__(house, "period")
        self._attr_options = list(house.schedule.order())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.house.period_entity_id = self.entity_id

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


class TrackSelect(HouseEntity, SelectEntity):
    """Normal day or Dark Day; choosing one holds it until the next period."""

    _platform_domain = "select"
    _attr_options = list(TRACKS)

    def __init__(self, house) -> None:
        super().__init__(house, "track")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.house.track_entity_id = self.entity_id

    @property
    def icon(self) -> str:
        return "mdi:weather-cloudy" if self.house.track == "dim" else "mdi:weather-sunny"

    @property
    def current_option(self) -> str:
        return self.house.track

    @property
    def extra_state_attributes(self) -> dict:
        status = self.house.dark_day_status()
        return {
            "clear_day_pct": status["pct"],
            "source": status["source"],
            "reason": status["reason"],
            "weather_pct": status["weather_pct"],
            "weather_state": status["weather_state"],
            "light_sensor_state": status["sensor_state"],
            "light_sensor_pct": status["sensor_pct"],
            "light_level": status["level"],
            "light_sensor": status["level_sensor"],
            "dark_below_pct": self.house.tracks.dark_below,
            "normal_above_pct": self.house.tracks.normal_above,
            "dark_day_periods": status["periods"],
            "sun_elevation": status["sun_elevation"],
            "chosen_by_hand": self.house.chooser.by_hand,
            "enabled": self.house.tracks.enabled,
        }

    async def async_select_option(self, option: str) -> None:
        self.house.override_track(option)


class RoomModeSelect(RoomEntity, SelectEntity, RestoreEntity):
    _platform_domain = "select"
    _attr_options = list(MODES)
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, house, runner) -> None:
        super().__init__(house, runner, "mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.runner.mode_entity_id = self.entity_id
        last = await self.async_get_last_state()
        self.runner.set_mode(last.state if last and last.state in MODES else MODE_LOG_ONLY)

    @property
    def current_option(self) -> str:
        return self.runner.mode

    async def async_select_option(self, option: str) -> None:
        self.runner.set_mode(option)
