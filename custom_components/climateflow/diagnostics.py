"""Diagnostics pentru ClimateFlow (Setări → Dispozitive → ⋮ → Descarcă diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Dump complet pentru depanare: configurare + stare live + învățare."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "state": coordinator.state_name,
        "live": coordinator._build_data(),
        "history": coordinator.history(),
        "commanded": coordinator._commanded,
        "values": {k: str(v) for k, v in coordinator.values.items()},
    }
