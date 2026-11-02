"""The Beurer BF700 integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .coordinator import BF700ConfigEntry, BF700Coordinator
from .sensor import scale_device_info

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: BF700ConfigEntry) -> bool:
    """Set up a scale from a config entry.

    The scale sleeps most of the time, so setup never waits for it: entities
    start from the stored state and update when the scale shows activity.
    """
    assert entry.unique_id is not None
    coordinator = BF700Coordinator(hass, entry, entry.unique_id)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    # Registered up front so per-user devices can point at it (via_device_id).
    coordinator.device_id = (
        dr.async_get(hass)
        .async_get_or_create(config_entry_id=entry.entry_id, **scale_device_info(coordinator))
        .id
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BF700ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
