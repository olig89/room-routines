"""Per room: what the room is doing and why, and its held light level."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import LIGHT_LUX
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RoomRoutinesConfigEntry
from .core.room import State
from .entity import RoomEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomRoutinesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    house = entry.runtime_data
    entities: list[SensorEntity] = []
    for runner in house.rooms.values():
        entities.append(RoomStatusSensor(house, runner))
        if runner.lux_sensor:
            entities.append(RoomAmbientSensor(house, runner))
    async_add_entities(entities)


class RoomStatusSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in State]

    def __init__(self, house, runner) -> None:
        super().__init__(house, runner, "status")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.runner.status_entity_id = self.entity_id

    @property
    def native_value(self) -> str | None:
        return self.runner.room.state.value if self.runner.room else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.runner.room
        attrs: dict[str, Any] = {
            "reason": room.last.reason if room else None,
            "period": self.house.period,
            "track": self.house.track,
            "mode": self.runner.mode,
            "stealth": self.house.stealth,
        }
        if room and room.deadline:
            attrs["lights_off_at"] = room.deadline.isoformat()
        return attrs


class RoomAmbientSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, house, runner) -> None:
        super().__init__(house, runner, "ambient_lux")

    @property
    def native_value(self) -> float | None:
        room = self.runner.room
        return room.ambient.ambient(dt_util.now()) if room else None
