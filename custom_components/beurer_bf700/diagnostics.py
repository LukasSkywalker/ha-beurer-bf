"""Diagnostics: include the raw frames of the last sync to help with other firmwares."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import BF700ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BF700ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "address": coordinator.address,
        "firmware_version": coordinator.firmware_version,
        "last_error": coordinator.last_error,
        "status": asdict(data.status) if data.status else None,
        "users": [{"uid": f"{u.uid:x}", "initials": u.initials} for u in data.users.values()],
        "latest": asdict(data.latest) if data.latest else None,
        "latest_by_user": {f"{uid:x}": asdict(m) for uid, m in data.latest_by_user.items()},
        "unassigned_count": data.unassigned_count,
        "last_sync": data.last_sync,
        "last_frames": coordinator.last_frames,
    }
