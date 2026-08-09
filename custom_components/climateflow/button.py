"""Butoane: sări următorul eveniment / pornește acum / oprește tot."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClimateFlowConfigEntry
from .coordinator import ClimateFlowCoordinator
from .entity import ClimateFlowEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ClimateFlowSkipNextButton(coordinator),
            ClimateFlowBoostNowButton(coordinator),
            ClimateFlowStopButton(coordinator),
        ]
    )


class ClimateFlowSkipNextButton(ClimateFlowEntity, ButtonEntity):
    """Comută „sări peste următorul eveniment programat”."""

    _attr_icon = "mdi:skip-next"

    def __init__(self, coordinator: ClimateFlowCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_skip_next"
        self._attr_name = "Sări următorul eveniment"

    async def async_press(self) -> None:
        self.coordinator.skip_next()


class ClimateFlowBoostNowButton(ClimateFlowEntity, ButtonEntity):
    """Pornește precondiționarea acum (sau o rulare manuală de test)."""

    _attr_icon = "mdi:play"

    def __init__(self, coordinator: ClimateFlowCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_boost_now"
        self._attr_name = "Pornește acum"

    async def async_press(self) -> None:
        await self.coordinator.async_boost_now()


class ClimateFlowStopButton(ClimateFlowEntity, ButtonEntity):
    """Oprește tot (direcția sigură — nu are voie să arunce)."""

    _attr_icon = "mdi:stop"

    def __init__(self, coordinator: ClimateFlowCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_stop"
        self._attr_name = "Oprește tot"

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_stop()
        except Exception:  # noqa: BLE001 — oprirea nu are voie să arunce în UI
            _LOGGER.exception("Eroare la oprirea din buton")
