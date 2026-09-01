"""Binary sensors: cloud reachability and daily-cap-reached state.

These drive the spec's graceful-degradation card — a user can self-diagnose
("cloud reachable: no") and see when voice has paused for the day without
filing a ticket.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import WavenCoordinator
from .entity import WavenCoordinatorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            CloudReachableSensor(coordinator, entry),
            DailyCapReachedSensor(coordinator, entry),
        ]
    )


class CloudReachableSensor(WavenCoordinatorEntity, BinarySensorEntity):
    _attr_name = "Cloud reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "cloud_reachable")

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_reachable

    @property
    def available(self) -> bool:
        # Always available — its whole job is to report up/down.
        return True


class DailyCapReachedSensor(WavenCoordinatorEntity, BinarySensorEntity):
    _attr_name = "Daily cap reached"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "cap_reached")

    @property
    def is_on(self) -> bool:
        return self.coordinator.tracker.is_over_cap(self.coordinator.daily_cap_minutes)

    @property
    def available(self) -> bool:
        return True
