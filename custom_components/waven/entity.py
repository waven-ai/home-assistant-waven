"""Shared device info + a coordinator-entity base for the sensors."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

try:  # HA ≥ 2025.2 split DeviceInfo into its own module …
    from homeassistant.helpers.device_info import DeviceInfo
except ImportError:  # … 2025.1 (our hacs.json floor) still exposes it here.
    from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import WavenCoordinator


def waven_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One device per config entry; STT, TTS and the sensors hang off it."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Waven",
        manufacturer="Waven",
        model="Hosted STT + TTS",
        configuration_url="https://waven.ai/dashboard",
    )


class WavenCoordinatorEntity(CoordinatorEntity[WavenCoordinator]):
    """Base for the diagnostic sensors so they share device + attribution."""

    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_device_info = waven_device_info(entry)


class WavenLocalEntity(WavenCoordinatorEntity):
    """Base for entities whose value comes from the LOCAL tracker, not the poll.

    CoordinatorEntity ties `available` to `coordinator.last_update_success`, so
    a backend outage marked today's usage, today's remaining minutes and the
    audit card unavailable — precisely when a user opens the dashboard to find
    out what happened, and precisely the numbers that are still perfectly
    accurate because they are computed in Home Assistant from the local
    daily-cap tracker. (The binary sensors already opt out the same way; the
    genuinely cloud-derived sensors — monthly pool, last contact — correctly
    keep the coordinator's availability.)
    """

    @property
    def available(self) -> bool:
        return True
