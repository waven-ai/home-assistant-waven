"""The Waven integration — hosted STT + TTS for Home Assistant's Assist.

Wake word, VAD and intent matching stay local; this integration ships only the
post-wake utterance to Waven for transcription and the response text back for
synthesis (optionally in a cloned voice). See the spec in the monorepo:
``docs/home-assistant-integration-spec.md``.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WavenClient
from .const import DEFAULT_REGION, REGION_HOSTS
from .coordinator import WavenCoordinator

PLATFORMS: list[Platform] = [
    Platform.STT,
    Platform.TTS,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

type WavenConfigEntry = ConfigEntry[WavenCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WavenConfigEntry) -> bool:
    """Set up Waven from a config entry."""
    session = async_get_clientsession(hass)

    # Region host honours an options override; fall back to the entry data.
    region = entry.options.get("region", entry.data.get("region", DEFAULT_REGION))
    host = REGION_HOSTS.get(region, REGION_HOSTS[DEFAULT_REGION])

    api_key = entry.options.get("api_key", entry.data.get("api_key", ""))
    client = WavenClient(session, api_key, host)

    coordinator = WavenCoordinator(hass, entry, client)
    await coordinator.async_load_store()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WavenConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: WavenConfigEntry) -> None:
    """Reload the entry when its options change (new voice, cap, region…)."""
    await hass.config_entries.async_reload(entry.entry_id)
