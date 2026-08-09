"""Coordinatorul ClimateFlow: program, precondiționare, eveniment, gardă.

Model: tick idempotent la 1 minut (starea se derivă mereu din ceas vs. program,
deci un restart HA în mijlocul unei ferestre gestionate se recuperează singur)
+ listeners de stare pentru gardă și senzori.

Stările: IDLE → PRECONDITIONING → EVENT → COOLDOWN → IDLE.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import guard, schedule, thermal
from .const import (
    AUTO_SUMMER_ABOVE_C,
    AUTO_WINTER_BELOW_C,
    CONF_AC_UNITS,
    CONF_BOILER,
    CONF_EVENTS,
    CONF_INDOOR_SENSOR,
    CONF_NOTIFY_SERVICE,
    CONF_ONE_OFFS,
    CONF_OUTDOOR_SENSOR,
    CONF_TARGET,
    DEFAULT_EXTREME_COOL_C,
    DEFAULT_EXTREME_HEAT_C,
    DEFAULT_FAN_CAP,
    DEFAULT_MAX_LEAD_MIN,
    DEFAULT_OVERSHOOT_C,
    DEFAULT_RATE_COOL,
    DEFAULT_RATE_HEAT,
    DEFAULT_TARGET_C,
    DEFAULT_TOLERANCE_C,
    DOMAIN,
    MODE_COOL,
    MODE_HEAT_BOILER,
    MODE_HEAT_BOOST,
    SEASON_AUTO,
    SEASON_SUMMER,
    SEASON_WINTER,
    STATE_COOLDOWN,
    STATE_EVENT,
    STATE_IDLE,
    STATE_PRECONDITIONING,
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

UPDATE_INTERVAL = dt.timedelta(minutes=1)

# Timeout pentru orice comandă climate (fără el, o integrare blocată agață tot —
# aceeași lecție ca la ZoneFlow, incident 2026-07-07).
CLIMATE_CALL_TIMEOUT = 30.0
# Pauză între comenzile către aceeași unitate (set_hvac_mode → set_temperature):
# multe integrări pierd atribute dacă primesc comenzi în rafală / unitatea e off.
CMD_GAP_S = 2.0
# Praguri deficit de confort în timpul evenimentului.
DEFICIT_NOTIFY_MIN = 5.0
DEFICIT_BOOST_MIN = 10.0
# Durata unei porniri manuale fără eveniment programat.
MANUAL_RUN_MINUTES = 60.0

# Scări de preferință: plafonul generic → fan_mode-urile reale ale unității
# (mărcile diferă: low/quiet/1/silent…). Primul candidat existent câștigă.
FAN_LADDERS: dict[str, list[str]] = {
    "quiet": ["quiet", "silent", "silence", "night", "low", "min", "1", "auto"],
    "low": ["low", "min", "quiet", "silent", "1", "2", "auto"],
    "medium": ["medium", "mid", "middle", "med", "2", "3", "auto"],
    # Treptele numerice în ordine DESCRESCĂTOARE — pe unități 1–5, „maxim" e 5, nu 4.
    "high": ["high", "max", "strong", "turbo", "powerful", "5", "4", "auto"],
    "auto": ["auto"],
}


def map_fan_mode(cap: str, available: list[str] | None) -> str | None:
    """Traduce plafonul generic într-un fan_mode real; None ⇒ nu comanda ventilatorul."""
    if not available:
        return None
    lower = {str(m).lower(): m for m in available}
    for candidate in FAN_LADDERS.get(cap, [cap]):
        if candidate in lower:
            return lower[candidate]
    if cap == "auto":
        return None
    non_auto = [m for m in available if str(m).lower() != "auto"] or list(available)
    return non_auto[-1] if cap == "high" else non_auto[0]


def _num(value: object, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class ClimateFlowCoordinator(DataUpdateCoordinator):
    """Ține starea sistemului de climatizare și comandă actuatoarele."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.entry = entry
        self.indoor_sensor: str = entry.data[CONF_INDOOR_SENSOR]
        self.outdoor_sensor: str = entry.data[CONF_OUTDOOR_SENSOR]
        self.ac_units: list[str] = [e for e in entry.data.get(CONF_AC_UNITS, []) if e]
        self.boiler: str | None = entry.data.get(CONF_BOILER) or None

        # Cache în memorie (supraviețuiește reload-ului intrării, NU restartului HA) —
        # păstrează starea/rularea curentă la salvarea unui eveniment din panou.
        self._cache: dict = hass.data.setdefault(DOMAIN, {}).setdefault(
            f"cache_{entry.entry_id}", {}
        )

        self.values: dict[str, object] = {}
        self.state_name: str = self._cache.get("state", STATE_IDLE)
        # Rularea gestionată curentă (precondiționare/eveniment): plan, snapshot-uri.
        self._active: dict | None = self._cache.get("active")
        # Starea comandată per entitate — sursa de adevăr a gărzii.
        self._commanded: dict[str, dict] = self._cache.get("commanded", {})
        self._resolved_season: str = self._cache.get("resolved_season", SEASON_WINTER)
        self._manual_occ: dict | None = None
        self._deficit_since: dt.datetime | None = None
        self._fight_notified = False

        self._skips: set[str] = set(self._cache.get("skips", []))
        self._store: Store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")

        # Învățarea termică (persistată): rate EMA pe benzi + eșantioane + istoric.
        self._learn_store: Store = Store(hass, 1, f"{DOMAIN}_learn_{entry.entry_id}")
        self._rates: dict = self._cache.get("rates", {})
        self._samples: list[dict] = self._cache.get("samples", [])
        self._records: list[dict] = self._cache.get("records", [])
        # Contor cumulativ (listele de mai sus sunt trunchiate la 200) —
        # senzorul „Eșantioane învățate” e TOTAL_INCREASING și nu are voie să scadă.
        self._total_samples: int = self._cache.get("total_samples", 0)

        # După un reload/update în mijlocul unei ferestre gestionate, actuatoarele au
        # rămas pe comenzile VECHI — primul tick retrimite comenzile cu setările noi.
        self._recommand: bool = self.state_name in (STATE_PRECONDITIONING, STATE_EVENT)

        self.policy = guard.GuardPolicy()
        self._pending_reasserts: dict[str, CALLBACK_TYPE] = {}
        self._unsub_climate: CALLBACK_TYPE | None = None
        self._unsub_sensors: CALLBACK_TYPE | None = None
        self._cooldown_task: asyncio.Task | None = None

        # Injectabile în teste (valorile de producție vin din constante).
        self._svc_timeout: float = CLIMATE_CALL_TIMEOUT
        self._retry_sleep: float = 2.0
        self._cmd_gap: float = CMD_GAP_S

    # ------------------------------------------------------------------ stores
    async def async_load_store(self) -> None:
        data = await self._store.async_load() or {}
        self._skips = set(data.get("skips", []))
        self._cache["skips"] = list(self._skips)

    async def _save_store(self) -> None:
        self._cache["skips"] = list(self._skips)
        await self._store.async_save({"skips": sorted(self._skips)})

    async def async_load_learning(self) -> None:
        data = await self._learn_store.async_load() or {}
        self._rates = data.get("rates", self._rates)
        self._samples = data.get("samples", self._samples)
        self._records = data.get("records", self._records)
        self._total_samples = data.get("total_samples", len(self._samples))
        self._cache_learning()

    def _cache_learning(self) -> None:
        self._cache["rates"] = self._rates
        self._cache["samples"] = self._samples
        self._cache["records"] = self._records
        self._cache["total_samples"] = self._total_samples

    async def _save_learning(self) -> None:
        self._cache_learning()
        await self._learn_store.async_save(
            {
                "rates": self._rates,
                "samples": self._samples[-200:],
                "records": self._records[-200:],
                "total_samples": self._total_samples,
            }
        )

    def _sync_cache(self) -> None:
        self._cache["state"] = self.state_name
        self._cache["active"] = self._active
        self._cache["commanded"] = self._commanded
        self._cache["resolved_season"] = self._resolved_season

    # --------------------------------------------------------------- topologie
    @property
    def events(self) -> list[dict]:
        return self.entry.options.get(CONF_EVENTS, [])

    @property
    def one_offs(self) -> list[dict]:
        return self.entry.options.get(CONF_ONE_OFFS, [])

    def all_actuators(self) -> list[str]:
        out = list(self.ac_units)
        if self.boiler:
            out.append(self.boiler)
        return out

    def _actuators_for(self, mode: str) -> list[str]:
        if mode == MODE_COOL:
            return list(self.ac_units)
        if mode == MODE_HEAT_BOILER:
            return [self.boiler] if self.boiler else list(self.ac_units)
        # boost: centrala + AC-urile (sau doar AC-urile dacă nu există centrală)
        return ([self.boiler] if self.boiler else []) + list(self.ac_units)

    # ------------------------------------------------------------------ utils
    def get_float(self, key: str, default: float = 0.0) -> float:
        return _num(self.values.get(key), default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.values.get(key)
        return bool(val) if val is not None else default

    def get_str(self, key: str, default: str) -> str:
        val = self.values.get(key)
        return str(val) if val else default

    @callback
    def set_value(self, key: str, value: object) -> None:
        """Apelat de entitățile de reglaj când valoarea se schimbă."""
        self.values[key] = value
        if key == VAL_ENABLED and not value and self.state_name in (
            STATE_PRECONDITIONING,
            STATE_EVENT,
        ):
            self._enter_cooldown("sistem dezactivat")
        if key == VAL_GUARD and self._active is not None:
            # Gardă comutată în mijlocul rulării → eșantionul de învățare e contaminat.
            self._active["tainted"] = True
        self.recompute()

    def _read_temp(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @property
    def indoor_temp(self) -> float | None:
        return self._read_temp(self.indoor_sensor)

    @property
    def outdoor_temp(self) -> float | None:
        return self._read_temp(self.outdoor_sensor)

    # ------------------------------------------------------------------ program
    def _occurrences(self, now: dt.datetime) -> list[dict]:
        occs = schedule.expand_occurrences(self.events, self.one_offs, self._skips, now)
        if self._manual_occ is not None and self._manual_occ["end"] > now:
            occs = sorted([self._manual_occ, *occs], key=lambda o: o["start"])
        return occs

    def _occ_target(self, occ: dict) -> float:
        return _num(occ.get(CONF_TARGET), self.get_float(VAL_TARGET, DEFAULT_TARGET_C))

    def _resolve_season(self) -> str:
        selected = self.get_str(VAL_SEASON, SEASON_AUTO)
        if selected != SEASON_AUTO:
            self._resolved_season = selected
            return selected
        outdoor = self.outdoor_temp
        if outdoor is not None:
            if outdoor < AUTO_WINTER_BELOW_C:
                self._resolved_season = SEASON_WINTER
            elif outdoor > AUTO_SUMMER_ABOVE_C:
                self._resolved_season = SEASON_SUMMER
            # între praguri: histerezis — păstrează sezonul rezolvat anterior
        return self._resolved_season

    def _plan(self, occ: dict, now: dt.datetime) -> dict:
        """Alege modul, rata și avansul de pornire pentru o ocurență, la condițiile de acum."""
        indoor = self.indoor_temp
        outdoor = self.outdoor_temp
        target = self._occ_target(occ)
        season = self._resolve_season()
        max_lead = self.get_float(VAL_MAX_LEAD, DEFAULT_MAX_LEAD_MIN)

        if season == SEASON_SUMMER:
            mode = MODE_COOL
            needed = (indoor - target) if indoor is not None else None
            default_rate = self.get_float(VAL_RATE_COOL, DEFAULT_RATE_COOL)
        else:
            mode = MODE_HEAT_BOILER if self.boiler else MODE_HEAT_BOOST
            needed = (target - indoor) if indoor is not None else None
            default_rate = self.get_float(VAL_RATE_HEAT, DEFAULT_RATE_HEAT)

        band = thermal.band_key(outdoor, mode) if outdoor is not None else None
        rate = (
            thermal.effective_rate(self._rates, mode, band, default_rate)
            if band is not None
            else default_rate
        )

        if needed is None:
            # Fără senzor interior nu putem estima — pornim cu avansul maxim (sigur).
            lead = max_lead
        else:
            lead = thermal.lead_minutes(needed, rate, max_lead)
            # Iarna: dacă nici avansul maxim nu ajunge cu centrala singură → boost cu AC-urile.
            if (
                season == SEASON_WINTER
                and mode == MODE_HEAT_BOILER
                and self.ac_units
                and lead >= max_lead
                and rate < thermal.required_rate(needed, max_lead)
            ):
                mode = MODE_HEAT_BOOST
                boost_rate = (
                    thermal.effective_rate(self._rates, mode, band, default_rate)
                    if band is not None
                    else default_rate
                )
                lead = thermal.lead_minutes(needed, boost_rate, max_lead)
                rate = boost_rate

        return {
            "mode": mode,
            "season": season,
            "target": target,
            "lead": lead,
            "rate": rate,
            "band": band,
        }

    # ----------------------------------------------------------- state machine
    async def _async_update_data(self) -> dict:
        now = dt_util.now()
        kept = schedule.consume_past_skips(self._skips, now)
        if kept != self._skips:
            self._skips = kept
            self.hass.async_create_task(self._save_store())
        if self._manual_occ is not None and self._manual_occ["end"] <= now:
            self._manual_occ = None

        occs = self._occurrences(now)
        current = schedule.current_occurrence(occs, now)
        upcoming = schedule.next_occurrence(occs, now)
        await self._advance(now, current, upcoming)
        self._sync_cache()
        return self._build_data()

    async def _advance(
        self, now: dt.datetime, current: dict | None, upcoming: dict | None
    ) -> None:
        if not self.get_bool(VAL_ENABLED, True):
            if self.state_name in (STATE_PRECONDITIONING, STATE_EVENT):
                self._enter_cooldown("sistem dezactivat")
            return
        if self.state_name == STATE_COOLDOWN:
            return  # task-ul de cooldown termină singur și trece în IDLE

        if current is not None:
            if (
                self.state_name != STATE_EVENT
                or self._active is None
                or self._active.get("key") != current["key"]
            ):
                await self._enter_event(now, current)
            else:
                await self._during_event(now, current)
            return

        if upcoming is not None:
            if (
                self.state_name == STATE_PRECONDITIONING
                and self._active is not None
                and self._active.get("key") == upcoming["key"]
            ):
                # Avansul e ÎNGHEȚAT odată intrați (nu re-evaluăm fereastra — altfel
                # o rată re-învățată ar face starea să oscileze).
                await self._during_precondition(now, upcoming)
                return
            plan = self._plan(upcoming, now)
            if now >= upcoming["start"] - dt.timedelta(minutes=plan["lead"]):
                await self._enter_precondition(now, upcoming, plan)
                return

        if self.state_name in (STATE_PRECONDITIONING, STATE_EVENT):
            self._enter_cooldown("fereastra s-a încheiat")
        elif self.state_name != STATE_IDLE:
            self.state_name = STATE_IDLE

    async def _enter_precondition(self, now: dt.datetime, occ: dict, plan: dict) -> None:
        _LOGGER.info(
            "Precondiționare pentru „%s” (start %s, mod %s, avans %.0f min)",
            occ["name"], occ["start"], plan["mode"], plan["lead"],
        )
        self.state_name = STATE_PRECONDITIONING
        indoor = self.indoor_temp
        outdoor = self.outdoor_temp
        self._active = {
            "key": occ["key"],
            "name": occ["name"],
            "event_start": occ["start"],
            "event_end": occ["end"],
            "plan": plan,
            "precond_start": now,
            "start_temp": indoor,
            "outdoor_start": outdoor,
            "boost_since": now if plan["mode"] == MODE_HEAT_BOOST else None,
            "escalated": False,
            "tainted": indoor is None or outdoor is None,
            "manual": bool(occ.get("manual")),
        }
        self._deficit_since = None
        self._fight_notified = False
        self.policy.reset()
        await self._command_mode(plan["mode"], self._precond_target(plan), fan_cap="high")
        self._notify(
            "ClimateFlow — precondiționare",
            f"Am pornit climatizarea pentru „{occ['name']}” "
            f"(start {occ['start'].strftime('%H:%M')}).",
            kind="precondition",
        )

    def _precond_target(self, plan: dict) -> float:
        overshoot = self.get_float(VAL_OVERSHOOT, DEFAULT_OVERSHOOT_C)
        if plan["mode"] == MODE_COOL:
            return plan["target"] - overshoot
        return plan["target"] + overshoot

    async def _during_precondition(self, now: dt.datetime, occ: dict) -> None:
        active = self._active
        plan = active["plan"]
        if self._recommand:
            self._recommand = False
            await self._command_mode(plan["mode"], self._precond_target(plan), fan_cap="high")
        # Escaladare unică la boost dacă ETA proiectat depășește startul evenimentului.
        if (
            plan["season"] == SEASON_WINTER
            and plan["mode"] == MODE_HEAT_BOILER
            and self.ac_units
            and not active["escalated"]
        ):
            indoor = self.indoor_temp
            if indoor is not None:
                needed = plan["target"] - indoor
                remaining_min = (occ["start"] - now).total_seconds() / 60.0
                if needed > 0 and plan["rate"] > 0:
                    eta_min = needed / plan["rate"] * 60.0
                    if eta_min > remaining_min + 5.0:
                        _LOGGER.info("Escaladez la boost: ETA %.0f min > rămas %.0f min", eta_min, remaining_min)
                        active["escalated"] = True
                        active["boost_since"] = now
                        plan["mode"] = MODE_HEAT_BOOST
                        self._record_boost(now, active, "preconditioning")
                        await self._command_mode(
                            MODE_HEAT_BOOST, self._precond_target(plan), fan_cap="high"
                        )
                        self._notify(
                            "ClimateFlow — boost",
                            "Centrala singură nu prinde ținta la timp — am pornit și aerele pe încălzire.",
                            kind="boost",
                        )
        await self._room_thermostat(self._precond_target(plan), "high")
        await self._reconcile(now)

    async def _enter_event(self, now: dt.datetime, occ: dict) -> None:
        prev = self._active
        came_from_precond = (
            self.state_name == STATE_PRECONDITIONING
            and prev is not None
            and prev.get("key") == occ["key"]
        )
        if came_from_precond:
            self._capture_sample(now, prev)
            plan = prev["plan"]
        else:
            # Restart HA / pornire în mijlocul evenimentului — plan proaspăt, fără eșantion.
            plan = self._plan(occ, now)

        _LOGGER.info(
            "Eveniment „%s” — mențin %.1f°C, ventilator plafonat", occ["name"], plan["target"]
        )
        self.state_name = STATE_EVENT
        manual = bool(occ.get("manual"))
        self._active = {
            "key": occ["key"],
            "name": occ["name"],
            "event_start": occ["start"],
            "event_end": occ["end"],
            "plan": plan,
            "event_boost": False,
            "deficit_notified": False,
            "manual": manual,
        }
        self._deficit_since = None
        self._fight_notified = False
        self.policy.reset()
        # Plafonul de ventilator e constrângerea de confort a evenimentului;
        # pornirile manuale (fără public) rămân pe viteză mare.
        fan_cap = "high" if manual else self.get_str(VAL_FAN_CAP, DEFAULT_FAN_CAP)
        await self._command_mode(plan["mode"], plan["target"], fan_cap=fan_cap)

    async def _during_event(self, now: dt.datetime, occ: dict) -> None:
        active = self._active
        plan = active["plan"]
        if self._recommand:
            self._recommand = False
            cap = "high" if active.get("manual") else self.get_str(VAL_FAN_CAP, DEFAULT_FAN_CAP)
            await self._command_mode(plan["mode"], plan["target"], fan_cap=cap)
        indoor = self.indoor_temp
        tolerance = self.get_float(VAL_TOLERANCE, DEFAULT_TOLERANCE_C)

        deficit = False
        if indoor is not None:
            if plan["mode"] == MODE_COOL:
                deficit = indoor > plan["target"] + tolerance
            else:
                deficit = indoor < plan["target"] - tolerance

        if deficit:
            if self._deficit_since is None:
                self._deficit_since = now
            elapsed_min = (now - self._deficit_since).total_seconds() / 60.0
            if elapsed_min >= DEFICIT_NOTIFY_MIN and not active["deficit_notified"]:
                active["deficit_notified"] = True
                self._notify(
                    "ClimateFlow — confort sub țintă",
                    f"Temperatura nu se menține la {plan['target']:.1f}°C cu ventilatorul "
                    "plafonat. Plafonul rămâne (confortul publicului are prioritate).",
                    kind="deficit",
                )
            # Iarna: după 10 min de deficit, aerele intră în ajutor — dar TOT plafonate.
            if (
                elapsed_min >= DEFICIT_BOOST_MIN
                and plan["season"] == SEASON_WINTER
                and plan["mode"] == MODE_HEAT_BOILER
                and self.ac_units
                and not active["event_boost"]
            ):
                active["event_boost"] = True
                plan["mode"] = MODE_HEAT_BOOST
                self._record_boost(now, active, "event")
                fan_cap = self.get_str(VAL_FAN_CAP, DEFAULT_FAN_CAP)
                await self._command_mode(MODE_HEAT_BOOST, plan["target"], fan_cap=fan_cap)
        else:
            self._deficit_since = None

        fan_cap = "high" if active.get("manual") else self.get_str(VAL_FAN_CAP, DEFAULT_FAN_CAP)
        await self._room_thermostat(plan["target"], fan_cap)
        await self._reconcile(now)

    @callback
    def _enter_cooldown(self, reason: str) -> None:
        """Închide fereastra gestionată: totul OFF, apoi mâinile jos (IDLE)."""
        if self.state_name == STATE_COOLDOWN:
            return
        _LOGGER.info("Cooldown (%s): opresc toate actuatoarele", reason)
        self.state_name = STATE_COOLDOWN
        self._active = None
        self._manual_occ = None
        self._deficit_since = None
        self._commanded = {}
        self.policy.reset()
        self._cancel_pending_reasserts()
        self._sync_cache()
        self._cooldown_task = self.hass.async_create_task(self._run_cooldown())

    async def _run_cooldown(self) -> None:
        try:
            await self.async_all_off()
        finally:
            self.state_name = STATE_IDLE
            self._cooldown_task = None
            self._sync_cache()
            self.recompute()

    # ---------------------------------------------------------------- învățare
    def _capture_sample(self, now: dt.datetime, active: dict) -> None:
        """La trecerea precondiționare → eveniment: măsoară rata realizată și învață."""
        plan = active["plan"]
        indoor = self.indoor_temp
        target = plan["target"]
        tolerance = self.get_float(VAL_TOLERANCE, DEFAULT_TOLERANCE_C)
        minutes = (now - active["precond_start"]).total_seconds() / 60.0

        if indoor is None:
            on_time = None
        elif plan["mode"] == MODE_COOL:
            on_time = indoor <= target + tolerance
        else:
            on_time = indoor >= target - tolerance

        record: dict = {
            "type": "precondition",
            "ts": now.isoformat(),
            "name": active.get("name", ""),
            "minutes": round(minutes, 1),
            "mode": plan["mode"],
            "on_time": on_time,
        }

        rate = None
        if not active.get("tainted") and active.get("outdoor_start") is not None:
            rate = thermal.observed_rate(active.get("start_temp"), indoor, minutes)

        # Atribuirea modului: o escaladare mid-run contaminează măsurătoarea modului
        # de bază; o acceptăm ca „boost” doar dacă boost-ul a acoperit ≥70% din rulare.
        mode = plan["mode"]
        if rate is not None and active.get("escalated"):
            boost_min = (now - active["boost_since"]).total_seconds() / 60.0
            if minutes > 0 and boost_min / minutes >= 0.7:
                mode = MODE_HEAT_BOOST
            else:
                rate = None

        if rate is not None:
            band = thermal.band_key(active["outdoor_start"], mode)
            self._rates = thermal.update_rate(self._rates, mode, band, rate)
            self._total_samples += 1
            self._samples.append(
                {
                    "ts": now.isoformat(),
                    "mode": mode,
                    "band": band,
                    "outdoor": active["outdoor_start"],
                    "start_temp": active["start_temp"],
                    "end_temp": indoor,
                    "minutes": round(minutes, 1),
                    "rate": round(rate, 2),
                }
            )
            self._samples = self._samples[-200:]
            record["rate"] = round(rate, 2)

        self._records.append(record)
        self._records = self._records[-200:]
        self.hass.async_create_task(self._save_learning())

    def _record_boost(self, now: dt.datetime, active: dict, phase: str) -> None:
        """Consemnează o escaladare la boost (vizibilă în istoricul panoului)."""
        self._records.append(
            {
                "type": "boost",
                "ts": now.isoformat(),
                "name": active.get("name", ""),
                "phase": phase,
            }
        )
        self._records = self._records[-200:]
        self.hass.async_create_task(self._save_learning())

    def _on_time_pct(self, now: dt.datetime) -> float | None:
        cutoff = now - dt.timedelta(days=30)
        results = []
        for r in self._records:
            if r.get("type") != "precondition" or r.get("on_time") is None:
                continue
            ts = dt_util.parse_datetime(r.get("ts", ""))
            if ts is not None and ts >= cutoff:
                results.append(bool(r["on_time"]))
        if not results:
            return None
        return round(100.0 * sum(results) / len(results), 0)

    # ------------------------------------------------------------------- gardă
    def _guard_active(self) -> bool:
        return (
            self.get_bool(VAL_ENABLED, True)
            and self.get_bool(VAL_GUARD, True)
            and self.state_name in (STATE_PRECONDITIONING, STATE_EVENT)
        )

    @callback
    def async_setup_listeners(self) -> None:
        actuators = self.all_actuators()
        if actuators:
            self._unsub_climate = async_track_state_change_event(
                self.hass, actuators, self._on_climate_event
            )
        sensors = [s for s in (self.indoor_sensor, self.outdoor_sensor) if s]
        if sensors:
            self._unsub_sensors = async_track_state_change_event(
                self.hass, sensors, self._on_sensor_event
            )

    @callback
    def async_shutdown_listeners(self) -> None:
        if self._unsub_climate is not None:
            self._unsub_climate()
            self._unsub_climate = None
        if self._unsub_sensors is not None:
            self._unsub_sensors()
            self._unsub_sensors = None
        self._cancel_pending_reasserts()

    @callback
    def _cancel_pending_reasserts(self) -> None:
        for unsub in self._pending_reasserts.values():
            unsub()
        self._pending_reasserts.clear()

    @callback
    def _on_sensor_event(self, _event: Event) -> None:
        self.recompute()

    @callback
    def _on_climate_event(self, event: Event) -> None:
        """Cineva a umblat la un AC (telecomandă): programăm o verificare cu debounce."""
        entity_id = event.data["entity_id"]
        if not self._guard_active() or entity_id not in self._commanded:
            return
        ts = dt_util.utcnow().timestamp()
        if not self.policy.should_check(entity_id, ts):
            return
        if entity_id in self._pending_reasserts:
            return  # verificare deja programată — absoarbe rafala de schimbări

        @callback
        def _fire(_now: dt.datetime) -> None:
            self._pending_reasserts.pop(entity_id, None)
            self.hass.async_create_task(self._check_and_reassert(entity_id))

        self._pending_reasserts[entity_id] = async_call_later(
            self.hass, guard.DEBOUNCE_S, _fire
        )

    async def _check_and_reassert(self, entity_id: str) -> None:
        """După debounce: re-verifică devierea (poate s-a corectat singură) și re-asertează."""
        if not self._guard_active():
            return
        commanded = self._commanded.get(entity_id)
        if commanded is None:
            return
        devs = self._deviations_for(entity_id, commanded)
        if not devs:
            return
        ts = dt_util.utcnow().timestamp()
        if not self.policy.note_reassert(entity_id, ts):
            self._notify_fighting(entity_id)
            return
        await self._reassert(entity_id, devs)

    def _deviations_for(self, entity_id: str, commanded: dict) -> dict:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return {}
        actual = {
            "hvac_mode": state.state,
            "temperature": state.attributes.get("temperature"),
            "fan_mode": state.attributes.get("fan_mode"),
        }
        return guard.deviations(commanded, actual)

    async def _reconcile(self, now: dt.datetime) -> None:
        """Reconciliere periodică (tick): prinde evenimentele pierdute de listener."""
        if not self._guard_active():
            return
        ts = now.timestamp()
        for entity_id, commanded in list(self._commanded.items()):
            if not self.policy.should_check(entity_id, ts):
                continue
            devs = self._deviations_for(entity_id, commanded)
            if not devs:
                continue
            if not self.policy.note_reassert(entity_id, ts):
                self._notify_fighting(entity_id)
                continue
            await self._reassert(entity_id, devs)

    async def _reassert(self, entity_id: str, devs: dict) -> None:
        """Retrimite DOAR atributele deviate (minimizează traficul IR/cloud)."""
        _LOGGER.info("Garda re-asertează %s pe %s", list(devs), entity_id)
        ts = dt_util.utcnow().timestamp()
        self.policy.note_command(entity_id, ts)
        remaining = dict(devs)
        if "hvac_mode" in remaining:
            await self._climate_call(
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": remaining.pop("hvac_mode")},
            )
            if remaining:
                await asyncio.sleep(self._cmd_gap)
        if "temperature" in remaining:
            await self._climate_call(
                "set_temperature",
                {"entity_id": entity_id, "temperature": remaining.pop("temperature")},
            )
        if "fan_mode" in remaining:
            await self._climate_call(
                "set_fan_mode",
                {"entity_id": entity_id, "fan_mode": remaining.pop("fan_mode")},
            )
        self.policy.note_command(entity_id, dt_util.utcnow().timestamp())

    def _notify_fighting(self, entity_id: str) -> None:
        if self._fight_notified:
            return
        self._fight_notified = True
        self._notify(
            "ClimateFlow — modificări repetate",
            f"Cineva modifică repetat {entity_id} de la telecomandă. "
            "Garda ia o pauză de 5 minute, apoi reia.",
            kind="fighting",
        )

    # ------------------------------------------------ comenzi sigure către climate
    async def _climate_call(self, service: str, data: dict) -> bool:
        """Comandă climate cu timeout. NU aruncă niciodată; False la eșec."""
        try:
            async with asyncio.timeout(self._svc_timeout):
                await self.hass.services.async_call("climate", service, data, blocking=True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — inclusiv TimeoutError
            _LOGGER.error("climate.%s a eșuat pentru %s: %s", service, data, err)
            return False

    def _room_control_on(self) -> bool:
        return self.get_bool(VAL_ROOM_CONTROL, True)

    def _extreme_setpoint(self, entity_id: str, hvac_mode: str) -> float:
        """Setpoint-ul extrem comandat AC-urilor (configurabil; implicit 12°/30°).

        Termostatul intern al AC-ului măsoară doar aerul din jurul unității — la
        setpoint normal se „satură" de la propriul aer rece și taie compresorul deși
        sala e încă departe de țintă. Cu setpoint extrem nu se oprește singur
        niciodată; sala e ținută de `_room_thermostat` pe senzorul real.

        Valoarea vine din reglajele utilizatorului (el își cunoaște aparatele);
        atributele min/max_temp ale unității sunt folosite DOAR ca limită de siguranță
        (multe integrări nu le publică sau le publică greșit — nu ne bazăm pe ele).
        """
        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state is not None else {}
        if hvac_mode == "cool":
            configured = self.get_float(VAL_EXTREME_COOL, DEFAULT_EXTREME_COOL_C)
            return max(configured, _num(attrs.get("min_temp"), configured))
        configured = self.get_float(VAL_EXTREME_HEAT, DEFAULT_EXTREME_HEAT_C)
        return min(configured, _num(attrs.get("max_temp"), configured))

    async def _command_mode(self, mode: str, temperature: float, fan_cap: str) -> None:
        """Comandă toate actuatoarele modului: hvac_mode → temperatură → ventilator.

        Cu „termostat pe senzorul de sală" activ, AC-urile primesc setpoint extrem
        (centrala rămâne pe țintă — ea are termostat de cameră adevărat)."""
        self._recommand = False  # orice comandă completă anulează re-comandarea de după reload
        hvac_mode = "cool" if mode == MODE_COOL else "heat"
        for entity_id in self._actuators_for(mode):
            temp = temperature
            if self._room_control_on() and entity_id in self.ac_units:
                temp = self._extreme_setpoint(entity_id, hvac_mode)
            await self._command_entity(entity_id, hvac_mode, temp, fan_cap)
        if self._active is not None:
            self._active["acs_on"] = True

    async def _turn_off_acs(self) -> None:
        """Oprește doar AC-urile (histerezis) — starea OFF devine cea apărată de gardă."""
        for entity_id in self.ac_units:
            self.policy.note_command(entity_id, dt_util.utcnow().timestamp())
            if await self._climate_call("turn_off", {"entity_id": entity_id}):
                self._commanded[entity_id] = {"hvac_mode": "off"}
            self.policy.note_command(entity_id, dt_util.utcnow().timestamp())
        if self._active is not None:
            self._active["acs_on"] = False
        self._sync_cache()

    async def _room_thermostat(self, phase_target: float, fan_cap: str) -> None:
        """Histerezis pe senzorul de sală: pornește/oprește AC-urile după nevoia reală."""
        active = self._active
        mode = active["plan"]["mode"]
        if not self._room_control_on() or not self.ac_units:
            return
        if not any(e in self.ac_units for e in self._actuators_for(mode)):
            return  # în modul curent lucrează doar centrala
        indoor = self.indoor_temp
        if indoor is None:
            return
        tol = self.get_float(VAL_TOLERANCE, DEFAULT_TOLERANCE_C)
        cooling = mode == MODE_COOL
        need_more = indoor > phase_target + tol if cooling else indoor < phase_target - tol
        satisfied = indoor < phase_target - tol if cooling else indoor > phase_target + tol
        acs_on = active.get("acs_on", True)
        if satisfied and acs_on:
            _LOGGER.info("Sala a atins ținta (%.1f°) — opresc AC-urile (histerezis)", indoor)
            await self._turn_off_acs()
        elif need_more and not acs_on:
            _LOGGER.info("Sala a derivat (%.1f°) — repornesc AC-urile", indoor)
            hvac_mode = "cool" if cooling else "heat"
            for entity_id in self.ac_units:
                await self._command_entity(
                    entity_id, hvac_mode, self._extreme_setpoint(entity_id, hvac_mode), fan_cap
                )
            active["acs_on"] = True
            self._sync_cache()

    async def _command_entity(
        self, entity_id: str, hvac_mode: str, temperature: float, fan_cap: str
    ) -> None:
        self.policy.note_command(entity_id, dt_util.utcnow().timestamp())
        commanded: dict = {"hvac_mode": hvac_mode}
        await self._climate_call(
            "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": hvac_mode}
        )
        # Pauză: unele integrări pierd set_temperature dacă vine imediat după pornire.
        await asyncio.sleep(self._cmd_gap)
        if await self._climate_call(
            "set_temperature", {"entity_id": entity_id, "temperature": temperature}
        ):
            commanded["temperature"] = temperature

        state = self.hass.states.get(entity_id)
        fan_modes = state.attributes.get("fan_modes") if state is not None else None
        fan_mode = map_fan_mode(fan_cap, fan_modes)
        if fan_mode is not None:
            if await self._climate_call(
                "set_fan_mode", {"entity_id": entity_id, "fan_mode": fan_mode}
            ):
                commanded["fan_mode"] = fan_mode

        self._commanded[entity_id] = commanded
        self.policy.note_command(entity_id, dt_util.utcnow().timestamp())
        self._sync_cache()

    async def async_all_off(self) -> None:
        """Oprește toate actuatoarele — cu timeout și 3 încercări; notifică dacă eșuează."""
        actuators = self.all_actuators()
        if not actuators:
            return
        for _attempt in range(3):
            if await self._climate_call("turn_off", {"entity_id": actuators}):
                return
            await asyncio.sleep(self._retry_sleep)
        _LOGGER.critical("Nu am putut opri actuatoarele %s după 3 încercări!", actuators)
        self._notify(
            "ClimateFlow — ATENȚIE",
            "Nu am putut opri aerele/centrala — verifică MANUAL dacă au rămas pornite!",
            kind="error",
        )

    # ------------------------------------------------------------------ acțiuni
    @callback
    def toggle_skip(self, key: str) -> None:
        """Comută markerul de skip pe o ocurență anume (din agenda panoului)."""
        if key in self._skips:
            self._skips.discard(key)
        else:
            self._skips.add(key)
        self.hass.async_create_task(self._save_store())
        self.recompute()

    @callback
    def skip_next(self) -> None:
        """Comută markerul de skip pe următoarea ocurență programată."""
        now = dt_util.now()
        upcoming = schedule.next_occurrence(self._occurrences(now), now)
        if upcoming is None or upcoming.get("manual"):
            return
        armed = upcoming["key"] not in self._skips
        self.toggle_skip(upcoming["key"])
        if armed:
            self._notify(
                "ClimateFlow — eveniment sărit",
                f"„{upcoming['name']}” din {upcoming['start'].strftime('%d.%m %H:%M')} "
                "nu va porni climatizarea.",
                kind="skip",
            )

    async def async_boost_now(self) -> None:
        """Pornește precondiționarea acum (spre următorul eveniment sau rulare manuală)."""
        if self.state_name in (STATE_PRECONDITIONING, STATE_EVENT):
            return
        now = dt_util.now()
        upcoming = schedule.next_occurrence(self._occurrences(now), now)
        if upcoming is not None:
            plan = self._plan(upcoming, now)
            await self._enter_precondition(now, upcoming, plan)
        else:
            self._manual_occ = {
                "event_id": "manual",
                "name": "Pornire manuală",
                "date": now.date(),
                "start": now,
                "end": now + dt.timedelta(minutes=MANUAL_RUN_MINUTES),
                "target": None,
                "one_off": True,
                "manual": True,
                "key": f"manual:{now.isoformat()}",
            }
            await self.async_refresh()
            return
        self._sync_cache()
        self.recompute()

    async def async_stop(self) -> None:
        """Oprește tot: forțează cooldown (totul OFF), apoi mâinile jos.

        NU are voie să arunce NICIODATĂ — e calea de urgență (lecția ZoneFlow)."""
        try:
            self._manual_occ = None
            if self.state_name != STATE_COOLDOWN:
                self._enter_cooldown("oprire manuală")
            self.recompute()
        except Exception:  # noqa: BLE001 — oprirea nu eșuează niciodată vizibil
            _LOGGER.exception("Eroare la oprire (starea a fost totuși resetată)")

    def add_one_off(self, name: str, date: str, start: str, end: str, target: float | None) -> None:
        """Adaugă un eveniment unic (nuntă etc.) în opțiuni — declanșează reload."""
        import uuid

        item = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "date": date,
            "start": start,
            "end": end,
            "target": target,
        }
        one_offs = [*self.one_offs, item]
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_ONE_OFFS: one_offs}
        )

    # ----------------------------------------------------------------- date UI
    def agenda(self, days: int = 7) -> list[dict]:
        """Ocurențele următoarelor zile, INCLUSIV cele sărite (pentru panou).

        Spre deosebire de `_occurrences` (care exclude skip-urile), agenda le arată
        anotat cu `skipped`, ca panoul să le afișeze tăiate + buton de anulare.
        """
        now = dt_util.now()
        occs = schedule.expand_occurrences(
            self.events, self.one_offs, set(), now, horizon_days=days
        )
        filters = {e.get("id"): e.get("week_filter", "always") for e in self.events}
        return [
            {
                "event_id": occ["event_id"],
                "key": occ["key"],
                "name": occ["name"],
                "date": occ["date"].isoformat(),
                "start": occ["start"].isoformat(),
                "end": occ["end"].isoformat(),
                "target": self._occ_target(occ),
                "one_off": occ["one_off"],
                "week_filter": filters.get(occ["event_id"], "always"),
                "skipped": occ["key"] in self._skips,
            }
            for occ in occs
        ]

    def _build_data(self) -> dict:
        now = dt_util.now()
        occs = self._occurrences(now)
        current = schedule.current_occurrence(occs, now)
        upcoming = schedule.next_occurrence(occs, now)
        next_occ = current or upcoming

        active = self._active
        if active is not None and self.state_name in (STATE_PRECONDITIONING, STATE_EVENT):
            plan = active["plan"]
            # ETA înghețat: precondiționarea a început deja la momentul consemnat.
            precond_start = active.get("precond_start", active.get("event_start"))
        elif upcoming is not None:
            plan = self._plan(upcoming, now)
            precond_start = upcoming["start"] - dt.timedelta(minutes=plan["lead"])
        else:
            plan = None
            precond_start = None

        deficit = (
            self._deficit_since is not None
            and (now - self._deficit_since).total_seconds() / 60.0 >= DEFICIT_NOTIFY_MIN
        )
        ts = now.timestamp()
        fighting = any(
            self.policy.in_backoff(entity_id, ts) for entity_id in self._commanded
        )

        # Rularea activă — hero-ul panoului calculează din ea progresul și countdown-ul.
        run = None
        if active is not None and self.state_name in (STATE_PRECONDITIONING, STATE_EVENT):
            precond = active.get("precond_start")
            ac_cmd = next(
                (self._commanded[e] for e in self.ac_units if e in self._commanded), None
            )
            run = {
                "name": active.get("name"),
                "target": active["plan"]["target"],
                "start_temp": active.get("start_temp"),
                "outdoor_start": active.get("outdoor_start"),
                "precond_start": precond.isoformat() if precond else None,
                "event_start": active["event_start"].isoformat(),
                "event_end": active["event_end"].isoformat(),
                "escalated": active.get("escalated", False),
                "event_boost": active.get("event_boost", False),
                "manual": active.get("manual", False),
                "acs_on": active.get("acs_on", True),
                "ac_setpoint": ac_cmd.get("temperature") if ac_cmd else None,
            }

        return {
            "run": run,
            "state": self.state_name,
            "indoor_temp": self.indoor_temp,
            "outdoor_temp": self.outdoor_temp,
            "season": self._resolved_season,
            "next_event": next_occ["start"].isoformat() if next_occ else None,
            "next_event_name": next_occ["name"] if next_occ else None,
            "next_event_end": next_occ["end"].isoformat() if next_occ else None,
            "next_event_target": self._occ_target(next_occ) if next_occ else None,
            "next_event_key": next_occ["key"] if next_occ else None,
            "precondition_start": precond_start.isoformat() if precond_start else None,
            "lead_minutes": plan["lead"] if plan else None,
            "mode": plan["mode"] if plan else None,
            "learned_rate_current": round(plan["rate"], 2) if plan else None,
            "rates": self._rates,
            "sample_count": self._total_samples,
            "on_time_pct": self._on_time_pct(now),
            "transition_week": schedule.is_month_transition(now.date()),
            "comfort_deficit": deficit,
            "guard_fighting": fighting,
            "skip_next_armed": bool(
                upcoming is not None and upcoming["key"] in self._skips
            ),
            "skips": sorted(self._skips),
        }

    @callback
    def recompute(self) -> None:
        """Recalculează și împinge datele către senzori (fără să aștepte tick-ul)."""
        if self.hass is None:
            return
        self.async_set_updated_data(self._build_data())

    def history(self) -> dict:
        """Istoric + învățare + statistici agregate pentru panou/diagnostic."""
        now = dt_util.now()
        cutoff = now - dt.timedelta(days=30)

        def _recent(kind: str) -> list[dict]:
            out = []
            for r in self._records:
                if r.get("type") != kind:
                    continue
                ts = dt_util.parse_datetime(r.get("ts", ""))
                if ts is not None and ts >= cutoff:
                    out.append(r)
            return out

        preconds = _recent("precondition")
        leads = [r["minutes"] for r in preconds if r.get("minutes")]
        return {
            "records": list(reversed(self._records[-50:])),
            "samples": list(reversed(self._samples[-50:])),
            "rates": self._rates,
            "on_time_pct": self._on_time_pct(now),
            "stats": {
                "events_30d": len(preconds),
                "avg_lead": round(sum(leads) / len(leads), 0) if leads else None,
                "boost_count_30d": len(_recent("boost")),
                "total_samples": self._total_samples,
            },
        }

    # -------------------------------------------------------------- notificări
    def _notify(self, title: str, message: str, kind: str = "info") -> None:
        """Notificare best-effort: clopoțel UI + push. NU are voie să arunce
        (lecția ZoneFlow, incident 2026-07-07: un NameError aici a blocat sesiunea)."""
        if not self.get_bool(VAL_NOTIFY, True) or self.hass is None:
            return
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": title,
                        "message": message,
                        "notification_id": f"{DOMAIN}_{self.entry.entry_id}_{kind}",
                    },
                    blocking=False,
                )
            )
            service = self.entry.data.get(CONF_NOTIFY_SERVICE)
            if service:
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "notify", service, {"title": title, "message": message}, blocking=False
                    )
                )
        except Exception:  # noqa: BLE001 — notificarea e best-effort
            _LOGGER.exception("Notificarea a eșuat (ignor, nu opresc controlul)")
