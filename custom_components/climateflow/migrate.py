"""Migrarea intrărilor de config către schema curentă (v1 — nimic de migrat încă)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Schema v1 e cea curentă; scheletul rămâne pentru versiunile viitoare."""
    if entry.version >= 1:
        return True
    return True
