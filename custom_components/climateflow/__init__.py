"""Integrarea ClimateFlow pentru Home Assistant."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_ONE_OFF,
    SERVICE_BOOST_NOW,
    SERVICE_SKIP_NEXT,
    SERVICE_STOP,
)
from .coordinator import ClimateFlowCoordinator
from .migrate import async_migrate_entry  # noqa: F401  (expus pentru HA)
from .panel import async_register_panel, async_remove_panel
from .websocket import async_register as async_register_ws

type ClimateFlowConfigEntry = ConfigEntry[ClimateFlowCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ClimateFlowConfigEntry) -> bool:
    """Configurează o intrare a integrării."""
    coordinator = ClimateFlowCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await coordinator.async_load_store()
    await coordinator.async_load_learning()

    # NU facem all-off orb la pornire: dacă HA a repornit în mijlocul unei ferestre
    # gestionate, primul tick re-derivă starea din ceas vs. program și continuă.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    coordinator.async_setup_listeners()
    _async_register_services(hass)
    async_register_ws(hass)
    await async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # După ce HA a pornit complet (senzorii/AC-urile sunt gata), reîmprospătăm —
    # altfel temperaturile apar „—” până la următorul tick.
    @callback
    def _refresh_when_started(_hass: HomeAssistant) -> None:
        hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(async_at_started(hass, _refresh_when_started))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClimateFlowConfigEntry) -> bool:
    """Descarcă o intrare (rulează și la fiecare reload, ex. după salvarea unui eveniment).

    NU scoatem panoul aici: reload-ul ar redirecționa utilizatorul către dashboard.
    Panoul se scoate doar la ștergerea integrării — vezi `async_remove_entry`.
    """
    coordinator = entry.runtime_data
    coordinator.async_shutdown_listeners()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ClimateFlowConfigEntry) -> None:
    """Apelat DOAR la ștergerea integrării (nu la reload): scoatem panoul."""
    remaining = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id
    ]
    if not remaining:
        async_remove_panel(hass)
        store = hass.data.get(DOMAIN, {})
        store.pop("panel_registered", None)
        store.pop("ws_registered", None)


async def _async_reload_entry(hass: HomeAssistant, entry: ClimateFlowConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Înregistrează serviciile globale (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_STOP):
        return

    def _coordinators() -> list[ClimateFlowCoordinator]:
        return [
            coordinator
            for entry in hass.config_entries.async_entries(DOMAIN)
            if (coordinator := getattr(entry, "runtime_data", None)) is not None
        ]

    async def _handle_skip_next(call: ServiceCall) -> None:
        for coordinator in _coordinators():
            coordinator.skip_next()

    async def _handle_boost_now(call: ServiceCall) -> None:
        for coordinator in _coordinators():
            await coordinator.async_boost_now()

    async def _handle_stop(call: ServiceCall) -> None:
        for coordinator in _coordinators():
            await coordinator.async_stop()

    async def _handle_add_one_off(call: ServiceCall) -> None:
        for coordinator in _coordinators():
            coordinator.add_one_off(
                call.data["name"],
                call.data["date"],
                call.data["start"],
                call.data["end"],
                call.data.get("target"),
            )

    hass.services.async_register(DOMAIN, SERVICE_SKIP_NEXT, _handle_skip_next)
    hass.services.async_register(DOMAIN, SERVICE_BOOST_NOW, _handle_boost_now)
    hass.services.async_register(DOMAIN, SERVICE_STOP, _handle_stop)
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_ONE_OFF,
        _handle_add_one_off,
        schema=vol.Schema(
            {
                vol.Required("name"): cv.string,
                vol.Required("date"): cv.string,
                vol.Required("start"): cv.string,
                vol.Required("end"): cv.string,
                vol.Optional("target"): vol.Coerce(float),
            }
        ),
    )
