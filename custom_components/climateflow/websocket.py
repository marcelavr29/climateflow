"""API websocket ClimateFlow (v0.1: minimul pentru editarea evenimentelor + comenzi).

Panoul din sidebar vine în v0.3; până atunci comenzile pot fi apelate din
Developer Tools → tab-ul Websocket sau din panouri custom.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_AC_UNITS,
    CONF_BOILER,
    CONF_EVENTS,
    CONF_INDOOR_SENSOR,
    CONF_NOTIFY_SERVICE,
    CONF_ONE_OFFS,
    CONF_OUTDOOR_SENSOR,
    DOMAIN,
    VAL_ENABLED,
    VAL_EXTREME_COOL,
    VAL_EXTREME_HEAT,
    VAL_FAN_CAP,
    VAL_GUARD,
    VAL_MAX_LEAD,
    VAL_NOTIFY,
    VAL_OVERSHOOT,
    VAL_RATE_COOL,
    VAL_RATE_HEAT,
    VAL_ROOM_CONTROL,
    VAL_SEASON,
    VAL_TARGET,
    VAL_TOLERANCE,
)

_LOGGER = logging.getLogger(__name__)


def async_register(hass: HomeAssistant) -> None:
    """Înregistrează comenzile websocket o singură dată (apelat la setup-ul intrării)."""
    store = hass.data.setdefault(DOMAIN, {})
    if store.get("ws_registered"):
        return
    store["ws_registered"] = True
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_save_events)
    websocket_api.async_register_command(hass, ws_save_general)
    websocket_api.async_register_command(hass, ws_skip_next)
    websocket_api.async_register_command(hass, ws_toggle_skip)
    websocket_api.async_register_command(hass, ws_boost_now)
    websocket_api.async_register_command(hass, ws_stop)
    websocket_api.async_register_command(hass, ws_history)


def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is not None:
            return entry
    return None


@callback
def _entities(hass: HomeAssistant, domain: str, device_class: str | None = None) -> list[dict]:
    out = []
    for st in hass.states.async_all(domain):
        if device_class and st.attributes.get("device_class") != device_class:
            continue
        out.append(
            {"entity_id": st.entity_id, "name": st.attributes.get("friendly_name", st.entity_id)}
        )
    out.sort(key=lambda s: s["name"].lower())
    return out


@callback
def _controls(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Entity_id-urile reglajelor (panoul le comută prin hass.callService)."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)

    def eid(platform: str, suffix: str) -> str | None:
        return reg.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{suffix}")

    return {
        "enabled": eid("switch", VAL_ENABLED),
        "guard": eid("switch", VAL_GUARD),
        "notify": eid("switch", VAL_NOTIFY),
        "room_control": eid("switch", VAL_ROOM_CONTROL),
        "season": eid("select", VAL_SEASON),
        "fan_cap": eid("select", VAL_FAN_CAP),
        "target": eid("number", VAL_TARGET),
        "max_lead": eid("number", VAL_MAX_LEAD),
        "tolerance": eid("number", VAL_TOLERANCE),
        "rate_heat": eid("number", VAL_RATE_HEAT),
        "rate_cool": eid("number", VAL_RATE_COOL),
        "overshoot": eid("number", VAL_OVERSHOOT),
        "extreme_cool": eid("number", VAL_EXTREME_COOL),
        "extreme_heat": eid("number", VAL_EXTREME_HEAT),
    }


@websocket_api.websocket_command({vol.Required("type"): "climateflow/get"})
@callback
def ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Întoarce configurarea + starea live."""
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_found", "ClimateFlow nu e configurat")
        return
    coordinator = entry.runtime_data
    connection.send_result(
        msg["id"],
        {
            "general": {
                k: entry.data.get(k)
                for k in (
                    CONF_INDOOR_SENSOR,
                    CONF_OUTDOOR_SENSOR,
                    CONF_AC_UNITS,
                    CONF_BOILER,
                    CONF_NOTIFY_SERVICE,
                )
            },
            "events": entry.options.get(CONF_EVENTS, []),
            "one_offs": entry.options.get(CONF_ONE_OFFS, []),
            "live": coordinator._build_data(),
            "agenda": coordinator.agenda(),
            "controls": _controls(hass, entry),
            "temp_sensors": _entities(hass, "sensor", device_class="temperature"),
            "climates": _entities(hass, "climate"),
            "notify_services": sorted(hass.services.async_services().get("notify", {})),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "climateflow/save_events",
        vol.Required("events"): list,
        vol.Optional("one_offs"): list,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_found", "ClimateFlow nu e configurat")
        return
    options = {**entry.options, CONF_EVENTS: msg["events"]}
    if "one_offs" in msg:
        options[CONF_ONE_OFFS] = msg["one_offs"]
    hass.config_entries.async_update_entry(entry, options=options)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "climateflow/save_general",
        vol.Required(CONF_INDOOR_SENSOR): str,
        vol.Required(CONF_OUTDOOR_SENSOR): str,
        vol.Required(CONF_AC_UNITS): list,
        vol.Optional(CONF_BOILER): vol.Any(str, None),
        vol.Optional(CONF_NOTIFY_SERVICE): vol.Any(str, None),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_general(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_found", "ClimateFlow nu e configurat")
        return
    new_data = {
        **entry.data,
        CONF_INDOOR_SENSOR: msg[CONF_INDOOR_SENSOR],
        CONF_OUTDOOR_SENSOR: msg[CONF_OUTDOOR_SENSOR],
        CONF_AC_UNITS: msg[CONF_AC_UNITS],
        CONF_BOILER: msg.get(CONF_BOILER) or None,
        CONF_NOTIFY_SERVICE: msg.get(CONF_NOTIFY_SERVICE) or None,
    }
    hass.config_entries.async_update_entry(entry, data=new_data)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): "climateflow/skip_next"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_skip_next(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    entry = _entry(hass)
    if entry is not None:
        entry.runtime_data.skip_next()
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {vol.Required("type"): "climateflow/toggle_skip", vol.Required("key"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_toggle_skip(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    entry = _entry(hass)
    if entry is not None:
        entry.runtime_data.toggle_skip(msg["key"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): "climateflow/boost_now"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_boost_now(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    entry = _entry(hass)
    if entry is not None:
        await entry.runtime_data.async_boost_now()
    connection.send_result(msg["id"], {"ok": True})


# FĂRĂ require_admin: oprirea e direcția SIGURĂ (stinge tot) și trebuie să
# funcționeze de pe orice profil (lecția ZoneFlow, incident 2026-08-09).
@websocket_api.websocket_command({vol.Required("type"): "climateflow/stop"})
@websocket_api.async_response
async def ws_stop(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Oprirea NU cere admin și NU are voie să eșueze vizibil."""
    entry = _entry(hass)
    if entry is not None:
        try:
            await entry.runtime_data.async_stop()
        except Exception:  # noqa: BLE001 — starea e deja resetată sincron
            _LOGGER.exception("Eroare la oprire (starea a fost totuși resetată)")
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({vol.Required("type"): "climateflow/history"})
@callback
def ws_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_found", "ClimateFlow nu e configurat")
        return
    connection.send_result(msg["id"], entry.runtime_data.history())
