"""Binary sensors: precondiționare / eveniment în curs, deficit de confort."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ClimateFlowConfigEntry
from .const import STATE_EVENT, STATE_PRECONDITIONING
from .coordinator import ClimateFlowCoordinator
from .entity import ClimateFlowEntity


@dataclass(frozen=True, kw_only=True)
class ClimateFlowBinaryDef:
    key: str
    name: str
    value_fn: Callable[[dict], bool]
    attrs_fn: Callable[[dict], dict] = field(default=lambda d: {})
    device_class: BinarySensorDeviceClass | None = None
    icon: str | None = None


_DEFS = [
    ClimateFlowBinaryDef(
        key="preconditioning",
        name="Precondiționare în curs",
        value_fn=lambda d: d.get("state") == STATE_PRECONDITIONING,
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:heat-wave",
    ),
    ClimateFlowBinaryDef(
        key="event_active",
        name="Eveniment în curs",
        value_fn=lambda d: d.get("state") == STATE_EVENT,
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:account-group",
    ),
    ClimateFlowBinaryDef(
        key="comfort_deficit",
        name="Confort sub țintă",
        value_fn=lambda d: bool(d.get("comfort_deficit")),
        attrs_fn=lambda d: {"guard_fighting": d.get("guard_fighting")},
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:thermometer-alert",
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ClimateFlowBinarySensor(coordinator, d) for d in _DEFS)


class ClimateFlowBinarySensor(
    ClimateFlowEntity, CoordinatorEntity[ClimateFlowCoordinator], BinarySensorEntity
):
    """Binary sensor derivat din datele coordinatorului."""

    def __init__(
        self, coordinator: ClimateFlowCoordinator, definition: ClimateFlowBinaryDef
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ClimateFlowEntity.__init__(self, coordinator)
        self._def = definition
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{definition.key}"
        self._attr_name = definition.name
        self._attr_device_class = definition.device_class
        self._attr_icon = definition.icon

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self._def.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return self._def.attrs_fn(self.coordinator.data)
