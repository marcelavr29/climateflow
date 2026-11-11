"""Coordinatorul ClimateFlow: program, precondiționare, eveniment, gardă.

Model: tick idempotent la 1 minut (starea se derivă mereu din ceas vs. program,
deci un restart HA în mijlocul unei ferestre gestionate se recuperează singur)
+ listeners de stare pentru gardă și senzori.

Stările: IDLE → PRECONDITIONING → EVENT → COOLDOWN → IDLE.
"""

from __future__ import annotations

import asyncio
import copy
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
    DEFAULT_BOILER_SETBACK_C,
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
    VAL_BOILER_SETBACK,
    VAL_BOILER_SETBACK_ON,
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
# Senzorul de sală indisponibil atâtea minute ⇒ AC-urile ies de pe setpoint-ul extrem
# (fără senzor nu mai există termostat care să le oprească).
SENSOR_LOST_FALLBACK_MIN = 3.0
# Senzor de sală care n-a mai raportat NIMIC de atâtea minute = „înghețat" (baterie
# moartă, rămas pe ultima valoare) — tratat ca indisponibil pentru termostatul de sală.
SENSOR_STALE_MIN = 120.0
# Escaladarea folosește rata OBSERVATĂ abia după atâtea minute de la prima mișcare
# (mai devreme, rezoluția senzorului ar da rate zgomotoase).
OBSERVED_SPAN_MIN = 30.0
# Învățarea din rulări „ținta neatinsă, aproape fără progres" cere un necesar inițial
# de cel puțin atât (sub el, zgomotul senzorului domină).
SLOW_RUN_MIN_NEEDED_C = 1.0
# De câte porniri cu boost din start (30 de zile) e nevoie ca să sugerăm „Avans maxim".
SUGGEST_AFTER_START_BOOSTS = 3
# Reglajele a căror schimbare în timpul unei rulări cere retrimiterea comenzilor.
LIVE_KEYS = frozenset(
    {
        VAL_TARGET,
        VAL_FAN_CAP,
        VAL_ROOM_CONTROL,
        VAL_EXTREME_COOL,
        VAL_EXTREME_HEAT,
        VAL_OVERSHOOT,
    }
)
RUN_STATES = (STATE_PRECONDITIONING, STATE_EVENT)

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
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        # Instantaneul `entry.data` de la setup: reload DOAR când se schimbă el
        # (senzori/actuatoare); programul din `entry.options` se citește live.
        self.entry_data_at_setup: dict = dict(entry.data)
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
        if self.state_name == STATE_COOLDOWN:
            # Task-ul de cooldown aparține coordinatorului VECHI (își termină singur
            # all-off-ul); cel nou nu are cine să-l scoată din COOLDOWN.
            self.state_name = STATE_IDLE
        # Copii, nu referințe: coordinatorul vechi poate avea încă un tick în zbor.
        # Rularea gestionată curentă (precondiționare/eveniment): plan, snapshot-uri.
        self._active: dict | None = copy.deepcopy(self._cache.get("active"))
        # Starea comandată per entitate — sursa de adevăr a gărzii.
        self._commanded: dict[str, dict] = copy.deepcopy(self._cache.get("commanded", {}))
        self._resolved_season: str = self._cache.get("resolved_season", SEASON_WINTER)
        self._manual_occ: dict | None = copy.deepcopy(self._cache.get("manual_occ"))
        # „Pornește acum" spre o ocurență apropiată: intră în precondiționare indiferent de avans.
        self._force_key: str | None = self._cache.get("force_key")
        self._deficit_since: dt.datetime | None = None
        self._fight_notified = False

        self._skips: set[str] = set(self._cache.get("skips", []))
        # Ocurențe oprite manual („Oprește tot") — nu mai repornesc până la următoarea.
        self._stopped: set[str] = set(self._cache.get("stopped", []))
        # Entități la care o comandă PROPRIE a eșuat — se reîncearcă și cu garda oprită.
        self._failed: set[str] = set()
        # Generația rulării: crește la cooldown/unload; secvențele de comenzi în zbor
        # o verifică și se opresc (altfel ar reporni unități DUPĂ all-off).
        self._gen: int = 0
        self._dead = False
        self._cmd_lock = asyncio.Lock()
        self._tick_lock = asyncio.Lock()
        # După un restart HA: a rămas o rulare neînchisă (HA a fost jos la final)?
        self._orphan_check = False
        self._orphan_entities: list[str] = []
        self._persisted: dict | None = None
        # Entități proaspăt comandate, încă neverificate: dacă unitatea își limitează
        # singură setpoint-ul, adoptăm valoarea raportată în loc să ne luptăm cu ea.
        self._await_verify: set[str] = set()
        self._store: Store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}")

        # Învățarea termică (persistată): rate EMA pe benzi + eșantioane + istoric.
        self._learn_store: Store = Store(hass, 1, f"{DOMAIN}_learn_{entry.entry_id}")
        self._rates: dict = self._cache.get("rates", {})
        # Timpul mort (inerția de pornire) per mod, în minute — vezi thermal.update_lag.
        self._lags: dict = self._cache.get("lags", {})
        self._samples: list[dict] = self._cache.get("samples", [])
        self._records: list[dict] = self._cache.get("records", [])
        # Contor cumulativ (listele de mai sus sunt trunchiate la 200) —
        # senzorul „Eșantioane învățate” e TOTAL_INCREASING și nu are voie să scadă.
        self._total_samples: int = self._cache.get("total_samples", 0)

        # După un reload/update în mijlocul unei ferestre gestionate, actuatoarele au
        # rămas pe comenzile VECHI — primul tick retrimite comenzile cu setările noi.
        self._recommand: bool = self.state_name in RUN_STATES

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
        self._stopped = set(data.get("stopped", []))
        restarted = "state" not in self._cache  # cache gol ⇒ restart HA, nu reload
        if restarted and data.get("season") in (SEASON_SUMMER, SEASON_WINTER):
            self._resolved_season = data["season"]
        if restarted:
            entities = data.get("run_entities")
            if entities is None and data.get("run_active"):  # format 0.6.0
                entities = self.all_actuators()
            known = set(self.all_actuators())
            self._orphan_entities = [e for e in (entities or []) if e in known]
            self._orphan_check = bool(self._orphan_entities)
        self._cache["skips"] = list(self._skips)
        self._cache["stopped"] = list(self._stopped)

    def _snapshot(self) -> dict:
        return {
            "skips": sorted(self._skips),
            "stopped": sorted(self._stopped),
            "season": self._resolved_season,
            # Ce a comandat rularea curentă — după un restart HA care a sărit peste
            # final, exact aceste entități trebuie închise.
            "run_entities": sorted(self._commanded) if self.state_name in RUN_STATES else [],
        }

    async def _save_store(self) -> None:
        await self._store.async_save(self._snapshot())

    @callback
    def _persist(self) -> None:
        """Salvează starea persistentă doar dacă s-a schimbat."""
        if self._dead:
            return
        self._cache["skips"] = list(self._skips)
        self._cache["stopped"] = list(self._stopped)
        snap = self._snapshot()
        if snap == self._persisted:
            return
        self._persisted = snap
        self.hass.async_create_task(self._save_store())

    async def async_load_learning(self) -> None:
        data = await self._learn_store.async_load() or {}
        self._rates = data.get("rates", self._rates)
        self._lags = data.get("lags", self._lags)
        self._samples = data.get("samples", self._samples)
        self._records = data.get("records", self._records)
        self._total_samples = data.get("total_samples", len(self._samples))
        self._cache_learning()

    def _cache_learning(self) -> None:
        self._cache["rates"] = self._rates
        self._cache["lags"] = self._lags
        self._cache["samples"] = self._samples
        self._cache["records"] = self._records
        self._cache["total_samples"] = self._total_samples

    async def _save_learning(self) -> None:
        self._cache_learning()
        await self._learn_store.async_save(
            {
                "rates": self._rates,
                "lags": self._lags,
                "samples": self._samples[-200:],
                "records": self._records[-200:],
                "total_samples": self._total_samples,
            }
        )

    def _sync_cache(self) -> None:
        if self._dead:
            return  # coordinator descărcat — cache-ul aparține celui nou
        self._cache["state"] = self.state_name
        self._cache["active"] = self._active
        self._cache["commanded"] = self._commanded
        self._cache["resolved_season"] = self._resolved_season
        self._cache["manual_occ"] = self._manual_occ
        self._cache["force_key"] = self._force_key

    def _ts(self) -> float:
        """Ceasul politicii de gardă (epoch) — un singur punct, injectabil în teste."""
        return dt_util.utcnow().timestamp()

    def _ha_running(self) -> bool:
        return bool(getattr(self.hass, "is_running", True))

    @callback
    def _request_tick(self) -> None:
        """Cere un tick în afara intervalului (ex. după schimbarea unui reglaj)."""
        if self.hass is None or self._dead:
            return
        self.hass.async_create_task(self.async_request_refresh())

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
        old = self.values.get(key)
        self.values[key] = value
        # `old is None` = restaurarea de la pornire/reload, nu o schimbare a utilizatorului.
        changed = old is not None and old != value
        if key == VAL_ENABLED and not value and self.state_name in RUN_STATES:
            self._enter_cooldown("sistem dezactivat")
        active = self._active
        if changed and active is not None and self.state_name in RUN_STATES:
            if key == VAL_GUARD:
                # Gardă comutată în mijlocul rulării → eșantionul de învățare e contaminat.
                active["tainted"] = True
            if key in LIVE_KEYS:
                # Reglajele „live" se aplică și rulării curente: următorul tick retrimite
                # comenzile cu valorile noi.
                if key == VAL_TARGET:
                    if active.get("default_target"):
                        active["plan"]["target"] = _num(value, active["plan"]["target"])
                    active["tainted"] = True
                self._recommand = True
                self._request_tick()
        if changed and key == VAL_ENABLED and value:
            self._request_tick()
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
    def _occurrences(self, now: dt.datetime, *, include_stopped: bool = False) -> list[dict]:
        occs = schedule.expand_occurrences(self.events, self.one_offs, self._skips, now)
        if not include_stopped:
            occs = [o for o in occs if o["key"] not in self._stopped]
        if self._manual_occ is not None and self._manual_occ["end"] > now:
            occs = sorted([self._manual_occ, *occs], key=lambda o: o["start"])
        return occs

    @staticmethod
    def _pick_current(occs: list[dict], now: dt.datetime) -> dict | None:
        """Ocurența în desfășurare; un eveniment programat are prioritate față de
        rularea manuală (publicul primește plafonul de ventilator)."""
        live = [o for o in occs if o["start"] <= now < o["end"]]
        return next((o for o in live if not o.get("manual")), live[0] if live else None)

    def _next_any(self, now: dt.datetime) -> dict | None:
        """Următoarea ocurență programată, INCLUSIV cele sărite/oprite."""
        occs = schedule.expand_occurrences(self.events, self.one_offs, set(), now)
        return schedule.next_occurrence(occs, now)

    def _occ_target(self, occ: dict) -> float:
        return _num(occ.get(CONF_TARGET), self.get_float(VAL_TARGET, DEFAULT_TARGET_C))

    def _resolve_season(
        self, indoor: float | None = None, target: float | None = None
    ) -> str:
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
            elif indoor is not None and target is not None:
                # Între praguri exteriorul nu spune nimic: decide SENSUL nevoii.
                # (Altfel toamna rămâne săptămâni pe „vară" cu sala rece.)
                tol = self.get_float(VAL_TOLERANCE, DEFAULT_TOLERANCE_C)
                if indoor < target - tol:
                    self._resolved_season = SEASON_WINTER
                elif indoor > target + tol:
                    self._resolved_season = SEASON_SUMMER
            # altfel: histerezis — păstrează sezonul rezolvat anterior
        return self._resolved_season

    def _plan(self, occ: dict, now: dt.datetime) -> dict:
        """Alege modul, rata și avansul de pornire pentru o ocurență, la condițiile de acum."""
        indoor = self.indoor_temp
        outdoor = self.outdoor_temp
        target = self._occ_target(occ)
        season = self._resolve_season(indoor, target)
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

        lag = _num(self._lags.get(mode), 0.0)
        boiler_need_min = None
        if needed is None:
            # Fără senzor interior nu putem estima — pornim cu avansul maxim (sigur).
            lead = max_lead
        else:
            lead = thermal.lead_minutes(needed, rate, max_lead, lag_min=lag)
            # Iarna: dacă nici avansul maxim nu ajunge cu centrala singură → boost cu AC-urile.
            if (
                season == SEASON_WINTER
                and mode == MODE_HEAT_BOILER
                and self.ac_units
                and lead >= max_lead
                and rate < thermal.required_rate(needed, max_lead - lag)
            ):
                # Cât i-ar trebui centralei singure — baza sugestiei de „Avans maxim".
                boiler_need_min = round(thermal.needed_minutes(needed, rate, lag_min=lag))
                mode = MODE_HEAT_BOOST
                boost_rate = (
                    thermal.effective_rate(self._rates, mode, band, default_rate)
                    if band is not None
                    else default_rate
                )
                lag = _num(self._lags.get(mode), 0.0)
                lead = thermal.lead_minutes(needed, boost_rate, max_lead, lag_min=lag)
                rate = boost_rate

        return {
            "mode": mode,
            "season": season,
            "target": target,
            "lead": lead,
            "rate": rate,
            "band": band,
            "lag": lag,
            "boiler_need_min": boiler_need_min,
        }

    # ----------------------------------------------------------- state machine
    async def _async_update_data(self) -> dict:
        # Un singur tick odată (tick-ul periodic vs. refresh-urile cerute din acțiuni).
        async with self._tick_lock:
            now = dt_util.now()
            self._skips = schedule.consume_past_skips(self._skips, now)
            self._stopped = schedule.consume_past_skips(self._stopped, now)
            if self._manual_occ is not None and self._manual_occ["end"] <= now:
                self._manual_occ = None

            occs = self._occurrences(now)
            current = self._pick_current(occs, now)
            upcoming = schedule.next_occurrence(occs, now)
            try:
                await self._advance(now, current, upcoming)
            except Exception:  # noqa: BLE001 — un tick stricat nu are voie să oprească bucla
                _LOGGER.exception("Tick ClimateFlow eșuat (continui la următorul)")

            if self._orphan_check and self._ha_running():
                # HA a fost jos peste finalul unei rulări: nimeni n-a stins aparatele.
                self._orphan_check = False
                if self.state_name == STATE_IDLE:
                    self._enter_cooldown(
                        "rulare rămasă neînchisă înainte de restart",
                        touched=self._orphan_entities,
                    )
            self._sync_cache()
            self._persist()
            return self._build_data()

    async def _advance(
        self, now: dt.datetime, current: dict | None, upcoming: dict | None
    ) -> None:
        if not self.get_bool(VAL_ENABLED, True):
            self._force_key = None
            if self.state_name in RUN_STATES:
                self._enter_cooldown("sistem dezactivat")
            return
        if self.state_name == STATE_COOLDOWN:
            return  # task-ul de cooldown termină singur și trece în IDLE
        if self.state_name == STATE_IDLE and not self._ha_running():
            # HA încă pornește: senzorii sunt indisponibili, deci planul (mod, avans)
            # ar fi greșit și ar rămâne ÎNGHEȚAT. Refresh-ul de la „started" reia.
            return

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
            forced = self._force_key == upcoming["key"]
            if forced or now >= upcoming["start"] - dt.timedelta(minutes=plan["lead"]):
                self._force_key = None
                await self._enter_precondition(now, upcoming, plan)
                return

        if self.state_name in RUN_STATES:
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
        active = self._active = {
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
            "default_target": occ.get(CONF_TARGET) is None,
        }
        self._deficit_since = None
        self._fight_notified = False
        self.policy.reset()
        await self._command_mode(plan["mode"], self._precond_target(plan), fan_cap="high")
        if self._active is not active:
            return  # oprit în timpul comenzilor
        if plan.get("boiler_need_min"):
            self._note_start_boost(now, active, plan)
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

    def _hyst_off(self, active: dict) -> bool:
        """AC-urile sunt oprite de histerezis (și trebuie să rămână așa la re-comandare)."""
        return self._room_control_on() and active.get("acs_on") is False

    def _follow_schedule_edits(self, active: dict, occ: dict) -> None:
        """Programul s-a schimbat în timpul rulării (salvare din panou, fără reload):
        preia orele noi și, dacă s-a schimbat, ținta ocurenței."""
        active["event_start"] = occ["start"]
        active["event_end"] = occ["end"]
        active["default_target"] = occ.get(CONF_TARGET) is None
        target = self._occ_target(occ)
        if target != active["plan"]["target"]:
            active["plan"]["target"] = target
            active["tainted"] = True
            self._recommand = True

    async def _during_precondition(self, now: dt.datetime, occ: dict) -> None:
        active = self._active
        plan = active["plan"]
        self._follow_schedule_edits(active, occ)
        if self._recommand:
            await self._command_mode(
                plan["mode"], self._precond_target(plan), fan_cap="high",
                keep_acs_off=self._hyst_off(active),
            )
            if self._active is not active:
                return
        indoor = self.indoor_temp
        # Momentul atingerii țintei — învățarea măsoară rata PÂNĂ aici, nu pe toată
        # fereastra (timpul în care sala doar stă la țintă ar dilua rata).
        if indoor is not None and "reached_at" not in active:
            reached = (
                indoor <= plan["target"] if plan["mode"] == MODE_COOL else indoor >= plan["target"]
            )
            if reached:
                active["reached_at"] = now
                active["reached_temp"] = indoor
        # Timpul mort: prima mișcare clară în sensul bun. Dacă primul semn e deja un salt
        # mare (senzor rar), momentul nu e de încredere — nu învățăm inerție din el.
        start_temp = active.get("start_temp")
        if (
            indoor is not None
            and start_temp is not None
            and "lag_at" not in active
            and not active["escalated"]
        ):
            progress = (
                start_temp - indoor if plan["mode"] == MODE_COOL else indoor - start_temp
            )
            if progress >= thermal.LAG_DELTA - 1e-6:  # 19.2 − 19.0 nu e exact 0.2 în float
                clean = progress < 3 * thermal.LAG_DELTA
                active["lag_at"] = now if clean else None
                active["lag_temp"] = indoor
                active["lag_mode"] = plan["mode"]
        # Escaladare unică la boost dacă ETA proiectat depășește startul evenimentului.
        # Nu în primele minute: caloriferele au inerție, iar o escaladare imediată n-ar
        # lăsa nici măcar un eșantion din care să învățăm rata centralei.
        elapsed_min = (now - active["precond_start"]).total_seconds() / 60.0
        if (
            plan["season"] == SEASON_WINTER
            and plan["mode"] == MODE_HEAT_BOILER
            and self.ac_units
            and not active["escalated"]
            and elapsed_min >= thermal.MIN_SAMPLE_MINUTES
            and indoor is not None
        ):
            needed = plan["target"] - indoor
            remaining_min = (occ["start"] - now).total_seconds() / 60.0
            if needed > 0 and plan["rate"] > 0:
                # ETA după progresul REAL când avem destul istoric; altfel după rata
                # planificată (+ inerția învățată, dacă sala încă n-a început să urce).
                rate_eff = plan["rate"]
                pending_lag = 0.0
                lag_at = active.get("lag_at")
                if lag_at is not None:
                    span_min = (now - lag_at).total_seconds() / 60.0
                    if span_min >= OBSERVED_SPAN_MIN:
                        rate_eff = max(
                            (indoor - active["lag_temp"]) / (span_min / 60.0),
                            thermal.MIN_LEARN_RATE,
                        )
                elif "lag_at" not in active:
                    pending_lag = max(0.0, plan.get("lag", 0.0) - elapsed_min)
                eta_min = needed / rate_eff * 60.0 + pending_lag
                if eta_min > remaining_min + 5.0:
                    _LOGGER.info("Escaladez la boost: ETA %.0f min > rămas %.0f min", eta_min, remaining_min)
                    # Centrala singură a fost prea lentă: ÎNVĂȚĂM asta acum, altfel
                    # rata ei rămâne optimistă și data viitoare pornim la fel de târziu.
                    self._learn_slow_boiler(now, active, indoor, elapsed_min)
                    active["escalated"] = True
                    active["boost_since"] = now
                    active["escalation_temp"] = indoor
                    plan["mode"] = MODE_HEAT_BOOST
                    self._record_boost(now, active, "preconditioning")
                    await self._command_mode(
                        MODE_HEAT_BOOST, self._precond_target(plan), fan_cap="high"
                    )
                    if self._active is not active:
                        return
                    self._notify(
                        "ClimateFlow — boost",
                        "Centrala singură nu prinde ținta la timp — am pornit și aerele pe încălzire.",
                        kind="boost",
                    )
        await self._room_thermostat(active, self._precond_target(plan), "high", now)
        if self._active is not active:
            return
        await self._reconcile(now)

    async def _enter_event(self, now: dt.datetime, occ: dict) -> None:
        prev = self._active
        came_from_precond = (
            self.state_name == STATE_PRECONDITIONING
            and prev is not None
            and prev.get("key") == occ["key"]
        )
        carry: dict = {}
        hyst_off = False
        if came_from_precond:
            self._capture_sample(now, prev)
            plan = prev["plan"]
            carry = {
                k: prev[k] for k in ("sensor_fallback", "sensor_lost_since") if k in prev
            }
            hyst_off = self._hyst_off(prev)
        else:
            # Restart HA / pornire în mijlocul evenimentului — plan proaspăt, fără eșantion.
            plan = self._plan(occ, now)

        _LOGGER.info(
            "Eveniment „%s” — mențin %.1f°C, ventilator plafonat", occ["name"], plan["target"]
        )
        self.state_name = STATE_EVENT
        manual = bool(occ.get("manual"))
        active = self._active = {
            "key": occ["key"],
            "name": occ["name"],
            "event_start": occ["start"],
            "event_end": occ["end"],
            "plan": plan,
            "event_boost": False,
            "deficit_notified": False,
            "manual": manual,
            "default_target": occ.get(CONF_TARGET) is None,
            **carry,
        }
        if hyst_off:
            active["acs_on"] = False
        self._deficit_since = None
        self._fight_notified = False
        self.policy.reset()
        fan_cap = self._event_fan_cap(active)
        # AC-urile oprite de histerezis în precondiționare NU se repornesc orbește la
        # începutul evenimentului — decide termostatul de sală, imediat mai jos.
        await self._command_mode(
            plan["mode"], plan["target"], fan_cap=fan_cap, keep_acs_off=hyst_off
        )
        if self._active is not active:
            return
        await self._room_thermostat(active, plan["target"], fan_cap, now)

    def _event_fan_cap(self, active: dict) -> str:
        """Plafonul de ventilator e constrângerea de confort a evenimentului;
        pornirile manuale (fără public) rămân pe viteză mare."""
        if active.get("manual"):
            return "high"
        return self.get_str(VAL_FAN_CAP, DEFAULT_FAN_CAP)

    async def _during_event(self, now: dt.datetime, occ: dict) -> None:
        active = self._active
        plan = active["plan"]
        self._follow_schedule_edits(active, occ)
        fan_cap = self._event_fan_cap(active)
        if self._recommand:
            await self._command_mode(
                plan["mode"], plan["target"], fan_cap=fan_cap,
                keep_acs_off=self._hyst_off(active),
            )
            if self._active is not active:
                return
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
                await self._command_mode(MODE_HEAT_BOOST, plan["target"], fan_cap=fan_cap)
                if self._active is not active:
                    return
        else:
            self._deficit_since = None

        await self._room_thermostat(active, plan["target"], fan_cap, now)
        if self._active is not active:
            return
        await self._reconcile(now)

    @callback
    def _enter_cooldown(
        self,
        reason: str,
        *,
        everything: bool = False,
        touched: list[str] | None = None,
    ) -> None:
        """Închide fereastra gestionată, apoi mâinile jos (IDLE).

        Se închide DOAR ce a comandat rularea (`touched`, implicit `_commanded`):
        AC-urile → off; centrala → temperatura de așteptare (sau off, dacă opțiunea e
        oprită). `everything=True` („Oprește tot"): toate AC-urile, indiferent cine
        le-a pornit; centrala nefolosită e stinsă doar dacă așteptarea e dezactivată.
        """
        if self.state_name == STATE_COOLDOWN:
            return
        used = set(self._commanded) if touched is None else set(touched)
        acs = [e for e in self.ac_units if everything or e in used]
        boiler_action: str | None = None
        if self.boiler:
            setback_on = self.get_bool(VAL_BOILER_SETBACK_ON, True)
            if self.boiler in used:
                boiler_action = "setback" if setback_on else "off"
            elif everything and not setback_on:
                boiler_action = "off"
        _LOGGER.info(
            "Cooldown (%s): opresc %s; centrala: %s", reason, acs or "nimic", boiler_action or "neatinsă"
        )
        self.state_name = STATE_COOLDOWN
        self._gen += 1  # oprește secvențele de comenzi aflate în zbor
        self._active = None
        self._manual_occ = None
        self._force_key = None
        self._deficit_since = None
        self._commanded = {}
        self._failed = set()
        self._await_verify = set()
        self.policy.reset()
        self._cancel_pending_reasserts()
        self._sync_cache()
        self._cooldown_task = self.hass.async_create_task(
            self._run_cooldown(acs, boiler_action)
        )

    async def _run_cooldown(self, acs: list[str], boiler_action: str | None) -> None:
        try:
            # Sub lock: închiderea pleacă abia după ce secvența în zbor s-a oprit — e
            # mereu ULTIMA comandă (altfel o unitate putea fi repornită după all-off).
            async with self._cmd_lock:
                off = list(acs)
                if boiler_action == "off" and self.boiler:
                    off.append(self.boiler)
                await self.async_all_off(off)
                if boiler_action == "setback":
                    await self._boiler_to_setback()
        finally:
            self.state_name = STATE_IDLE
            self._cooldown_task = None
            self._sync_cache()
            self._persist()
            self.recompute()

    async def _boiler_to_setback(self) -> None:
        """Centrala NU se stinge la final: coboară pe temperatura de așteptare, ca
        clădirea să nu se răcească de tot până la următorul eveniment."""
        temp = self.get_float(VAL_BOILER_SETBACK, DEFAULT_BOILER_SETBACK_C)
        for _attempt in range(3):
            ok = await self._climate_call(
                "set_hvac_mode", {"entity_id": self.boiler, "hvac_mode": "heat"}
            )
            await asyncio.sleep(self._cmd_gap)
            ok &= await self._climate_call(
                "set_temperature", {"entity_id": self.boiler, "temperature": temp}
            )
            if ok:
                return
            await asyncio.sleep(self._retry_sleep)
        _LOGGER.critical("Nu am putut trece centrala pe temperatura de așteptare (%.1f°)!", temp)
        self._notify(
            "ClimateFlow — ATENȚIE",
            f"Nu am putut trece centrala pe temperatura de așteptare ({temp:.1f}°C) — "
            "verifică MANUAL termostatul!",
            kind="error",
        )

    # ---------------------------------------------------------------- învățare
    def _learn_pessimistic(self, mode: str, band: str, rate: float, plan_rate: float) -> None:
        """EMA cu un eșantion „prea lent". Dacă banda n-are încă istoric, nu o seedăm
        direct cu el (e pesimist), ci corectăm rata cu care s-a planificat."""
        rates = self._rates
        if band not in (rates.get(mode) or {}):
            rates = thermal.update_rate(rates, mode, band, plan_rate)
        self._rates = thermal.update_rate(rates, mode, band, rate)

    def _sensor_reported_since(self, since: dt.datetime) -> bool:
        """Senzorul de sală a raportat ceva de la `since`? (altfel „fără progres" poate
        însemna doar senzor mort — nu învățăm din asta)."""
        state = self.hass.states.get(self.indoor_sensor)
        last = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        return last is not None and last > since

    def _note_start_boost(self, now: dt.datetime, active: dict, plan: dict) -> None:
        """Boost din start = centrala n-ajunge nici cu avansul maxim. Consemnăm și,
        dacă se repetă, sugerăm creșterea „Avans maxim" (învățarea nu mai are ce face)."""
        max_lead = self.get_float(VAL_MAX_LEAD, DEFAULT_MAX_LEAD_MIN)
        self._record_boost(
            now, active, "start", needed_min=plan["boiler_need_min"], max_lead=max_lead
        )
        suggestion = self._max_lead_suggestion(now)
        if suggestion is not None:
            self._notify(
                "ClimateFlow — avansul maxim e prea mic",
                f"Aerele au intrat în ajutor din start de {suggestion['count']} ori în "
                f"ultimele 30 de zile: centrala singură ar avea nevoie de "
                f"~{suggestion['needed_min']:.0f} min, iar „Avans maxim” e "
                f"{suggestion['max_lead']:.0f} min. Crește „Avans maxim” dacă vrei să "
                "pornească mai devreme în loc de boost.",
                kind="max_lead",
            )

    def _max_lead_suggestion(self, now: dt.datetime) -> dict | None:
        cutoff = now - dt.timedelta(days=30)
        starts = []
        for r in self._records:
            if r.get("type") != "boost" or r.get("phase") != "start" or not r.get("needed_min"):
                continue
            ts = dt_util.parse_datetime(r.get("ts", ""))
            if ts is not None and ts >= cutoff:
                starts.append(r)
        if len(starts) < SUGGEST_AFTER_START_BOOSTS:
            return None
        max_lead = self.get_float(VAL_MAX_LEAD, DEFAULT_MAX_LEAD_MIN)
        needed = max(r["needed_min"] for r in starts[-SUGGEST_AFTER_START_BOOSTS:])
        if needed <= max_lead:
            return None  # între timp avansul maxim a fost crescut destul
        return {"count": len(starts), "needed_min": needed, "max_lead": max_lead}

    def _learn_slow_boiler(
        self, now: dt.datetime, active: dict, indoor: float, minutes: float
    ) -> None:
        """La escaladare: rata realizată de centrala SINGURĂ până acum intră în EMA.

        Fără pragul minim de delta — „aproape deloc" e exact informația care lipsea
        (rata coboară ⇒ data viitoare avansul e mai mare și centrala pornește mai devreme).
        """
        start = active.get("start_temp")
        outdoor = active.get("outdoor_start")
        if active.get("tainted") or start is None or outdoor is None:
            return
        if minutes < thermal.MIN_SAMPLE_MINUTES:
            return
        seg_start, seg_minutes = start, minutes
        lag_at = active.get("lag_at")
        if lag_at is not None:
            # Inerția se învață separat; rata se măsoară de la prima mișcare încolo
            # (dacă a rămas destul timp de măsurat).
            lag_min = (lag_at - active["precond_start"]).total_seconds() / 60.0
            self._lags = thermal.update_lag(self._lags, MODE_HEAT_BOILER, lag_min)
            after = (now - lag_at).total_seconds() / 60.0
            if after >= thermal.MIN_SAMPLE_MINUTES:
                seg_start, seg_minutes = active["lag_temp"], after
        rate = max((indoor - seg_start) / (seg_minutes / 60.0), thermal.MIN_LEARN_RATE)
        if rate > thermal.MAX_SANE_RATE:
            return
        band = thermal.band_key(outdoor, MODE_HEAT_BOILER)
        self._learn_pessimistic(MODE_HEAT_BOILER, band, rate, active["plan"]["rate"])
        self._add_sample(now, MODE_HEAT_BOILER, band, outdoor, seg_start, indoor, seg_minutes, rate)
        _LOGGER.info(
            "Învățat din escaladare: centrala singură ≈ %.2f °C/h (banda %s)", rate, band
        )

    def _add_sample(
        self, now: dt.datetime, mode: str, band: str, outdoor: float,
        start_temp: float, end_temp: float, minutes: float, rate: float,
    ) -> None:
        self._total_samples += 1
        self._samples.append(
            {
                "ts": now.isoformat(),
                "mode": mode,
                "band": band,
                "outdoor": outdoor,
                "start_temp": start_temp,
                "end_temp": end_temp,
                "minutes": round(minutes, 1),
                "rate": round(rate, 2),
            }
        )
        self._samples = self._samples[-200:]

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

        # Intervalul măsurat: de la start (sau de la escaladare — porțiunea de boost,
        # cea „doar centrala" a fost învățată la escaladare) până la ATINGEREA țintei
        # (sau până acum, dacă n-a fost atinsă).
        seg_start_time = active["precond_start"]
        seg_start_temp = active.get("start_temp")
        mode = plan["mode"]
        usable = not active.get("tainted") and active.get("outdoor_start") is not None
        if active.get("escalated"):
            seg_start_time = active.get("boost_since") or seg_start_time
            seg_start_temp = active.get("escalation_temp")
            mode = MODE_HEAT_BOOST
        elif active.get("lag_at") is not None:
            # Timpul mort se învață separat și NU intră în rată (avans = inerție + Δ/rată).
            lag_min = (active["lag_at"] - active["precond_start"]).total_seconds() / 60.0
            if usable:
                self._lags = thermal.update_lag(self._lags, active.get("lag_mode", mode), lag_min)
            record["lag_min"] = round(lag_min, 1)
            seg_start_time = active["lag_at"]
            seg_start_temp = active.get("lag_temp")
        seg_end_time = now
        seg_end_temp = indoor
        reached_at = active.get("reached_at")
        if reached_at is not None and reached_at > seg_start_time:
            seg_end_time = reached_at
            seg_end_temp = active.get("reached_temp")
            record["reached_min"] = round(
                (reached_at - active["precond_start"]).total_seconds() / 60.0, 1
            )
        seg_minutes = (seg_end_time - seg_start_time).total_seconds() / 60.0

        rate = None
        pessimistic = False
        if usable:
            rate = thermal.observed_rate(seg_start_temp, seg_end_temp, seg_minutes)
            if rate is None:
                rate = self._slow_run_rate(active, indoor, minutes)
                if rate is not None:
                    pessimistic = True
                    seg_start_temp, seg_end_temp, seg_minutes = (
                        active.get("start_temp"), indoor, minutes
                    )

        if rate is not None:
            band = thermal.band_key(active["outdoor_start"], mode)
            if pessimistic:
                self._learn_pessimistic(mode, band, rate, plan["rate"])
            else:
                self._rates = thermal.update_rate(self._rates, mode, band, rate)
            self._add_sample(
                now, mode, band, active["outdoor_start"],
                seg_start_temp, seg_end_temp, seg_minutes, rate,
            )
            record["rate"] = round(rate, 2)

        self._records.append(record)
        self._records = self._records[-200:]
        self.hass.async_create_task(self._save_learning())

    def _slow_run_rate(
        self, active: dict, indoor: float | None, minutes: float
    ) -> float | None:
        """Rulare în care ținta NU a fost atinsă și sala abia s-a mișcat (ex. centrală
        fără AC-uri de rezervă): în loc să aruncăm eșantionul, învățăm „foarte lent"."""
        start = active.get("start_temp")
        plan = active["plan"]
        if (
            indoor is None
            or start is None
            or active.get("escalated")
            or active.get("reached_at") is not None
            or minutes < thermal.MIN_SAMPLE_MINUTES
            or abs(plan["target"] - start) < SLOW_RUN_MIN_NEEDED_C
            or not self._sensor_reported_since(active["precond_start"])
        ):
            return None
        progress = start - indoor if plan["mode"] == MODE_COOL else indoor - start
        if progress >= thermal.MIN_SAMPLE_DELTA:
            return None  # progres real — eșantionul normal a fost respins din alt motiv
        return max(progress / (minutes / 60.0), thermal.MIN_LEARN_RATE)

    def _record_boost(self, now: dt.datetime, active: dict, phase: str, **extra: object) -> None:
        """Consemnează un boost (vizibil în istoricul panoului)."""
        self._records.append(
            {
                "type": "boost",
                "ts": now.isoformat(),
                "name": active.get("name", ""),
                "phase": phase,
                **extra,
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
            and self.state_name in RUN_STATES
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
        # Coordinator descărcat (reload): secvențele în zbor se opresc, iar cache-ul
        # comun nu mai e scris de aici — cel nou retrimite comenzile (`_recommand`).
        self._dead = True
        self._gen += 1
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
        ts = self._ts()
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
        if not self._guard_active() or entity_id in self._await_verify:
            return  # prima verificare după propria comandă o face tick-ul (`_reconcile`)
        commanded = self._commanded.get(entity_id)
        if commanded is None:
            return
        devs = self._deviations_for(entity_id, commanded)
        if not devs:
            return
        ts = self._ts()
        if not self.policy.note_reassert(entity_id, ts):
            self._notify_fighting(entity_id)
            return
        await self._reassert(entity_id, devs)

    def _deviations_for(self, entity_id: str, commanded: dict) -> dict | None:
        """Atributele deviate; None dacă starea reală nu e cunoscută (integrare căzută)."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        actual = {
            "hvac_mode": state.state,
            "temperature": state.attributes.get("temperature"),
            "fan_mode": state.attributes.get("fan_mode"),
        }
        return guard.deviations(commanded, actual)

    async def _reconcile(self, now: dt.datetime) -> None:
        """Reconciliere periodică (tick): prinde evenimentele pierdute de listener.

        Cu garda oprită reîncearcă DOAR propriile comenzi eșuate (`_failed`) — garda
        e despre telecomenzi, nu despre timeout-urile integrării."""
        if self.state_name not in RUN_STATES or not self.get_bool(VAL_ENABLED, True):
            return
        guard_on = self.get_bool(VAL_GUARD, True)
        gen = self._gen
        ts = now.timestamp()
        for entity_id, commanded in list(self._commanded.items()):
            if gen != self._gen:
                return
            own_failure = entity_id in self._failed
            if not guard_on and not own_failure:
                continue
            if not self.policy.should_check(entity_id, ts):
                continue
            devs = self._deviations_for(entity_id, commanded)
            if devs is None:
                continue
            first_check = entity_id in self._await_verify
            self._await_verify.discard(entity_id)
            if first_check and not own_failure and "temperature" in devs:
                # Comanda a reușit, dar unitatea raportează alt setpoint: îl limitează
                # singură (ex. minim 16°). Adoptăm valoarea ei — altfel am re-aserta la
                # nesfârșit și am acuza degeaba o telecomandă.
                state = self.hass.states.get(entity_id)
                reported = _num(state.attributes.get("temperature"), commanded["temperature"])
                _LOGGER.warning(
                    "%s a limitat setpoint-ul la %.1f° (comandat %.1f°) — îl adopt. "
                    "Ajustează „Setpoint extrem” în setări.",
                    entity_id, reported, commanded["temperature"],
                )
                commanded["temperature"] = reported
                devs.pop("temperature")
            if not devs:
                self._failed.discard(entity_id)
                continue
            if not self.policy.note_reassert(entity_id, ts):
                if not own_failure:
                    self._notify_fighting(entity_id)
                continue
            await self._reassert(entity_id, devs)

    async def _reassert(self, entity_id: str, devs: dict) -> None:
        """Retrimite DOAR atributele deviate (minimizează traficul IR/cloud)."""
        gen = self._gen
        async with self._cmd_lock:
            if gen != self._gen:
                return
            _LOGGER.info("Garda re-asertează %s pe %s", list(devs), entity_id)
            self.policy.note_command(entity_id, self._ts())
            ok = True
            if "hvac_mode" in devs:
                ok &= await self._climate_call(
                    "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": devs["hvac_mode"]}
                )
                if len(devs) > 1:
                    await asyncio.sleep(self._cmd_gap)
            for attr, service in (("temperature", "set_temperature"), ("fan_mode", "set_fan_mode")):
                if attr not in devs:
                    continue
                if gen != self._gen:
                    return
                ok &= await self._climate_call(
                    service, {"entity_id": entity_id, attr: devs[attr]}
                )
            if gen != self._gen:
                return
            if ok:
                self._failed.discard(entity_id)
            else:
                self._failed.add(entity_id)
            self.policy.note_command(entity_id, self._ts())

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

    def _ac_setpoint(
        self, entity_id: str, hvac_mode: str, phase_target: float, active: dict | None
    ) -> float:
        """Setpoint-ul unui AC: extrem cu termostatul de sală activ ȘI senzorul viu;
        altfel ținta fazei (termostatul intern al unității rămâne singura protecție)."""
        if self._room_control_on() and not (active or {}).get("sensor_fallback"):
            return self._extreme_setpoint(entity_id, hvac_mode)
        return phase_target

    async def _command_mode(
        self, mode: str, temperature: float, fan_cap: str, *, keep_acs_off: bool = False
    ) -> None:
        """Comandă toate actuatoarele modului: hvac_mode → temperatură → ventilator.

        Cu „termostat pe senzorul de sală" activ, AC-urile primesc setpoint extrem
        (centrala rămâne pe țintă — ea are termostat de cameră adevărat).
        `keep_acs_off`: AC-urile oprite de histerezis rămân oprite."""
        self._recommand = False  # orice comandă completă anulează re-comandarea de după reload
        gen = self._gen
        active = self._active
        hvac_mode = "cool" if mode == MODE_COOL else "heat"
        async with self._cmd_lock:
            for entity_id in self._actuators_for(mode):
                if gen != self._gen:
                    return  # oprit între timp — nu mai pornim restul unităților
                temp = temperature
                if entity_id in self.ac_units:
                    if keep_acs_off:
                        continue
                    temp = self._ac_setpoint(entity_id, hvac_mode, temperature, active)
                await self._command_entity(entity_id, hvac_mode, temp, fan_cap, gen)
        if gen == self._gen and active is not None and not keep_acs_off:
            active["acs_on"] = True

    async def _command_acs(
        self, active: dict, hvac_mode: str, phase_target: float, fan_cap: str
    ) -> None:
        """(Re)pornește doar AC-urile, pe setpoint-ul potrivit stării curente."""
        gen = self._gen
        async with self._cmd_lock:
            for entity_id in self.ac_units:
                if gen != self._gen:
                    return
                await self._command_entity(
                    entity_id, hvac_mode,
                    self._ac_setpoint(entity_id, hvac_mode, phase_target, active),
                    fan_cap, gen,
                )
        if gen == self._gen:
            active["acs_on"] = True
            self._sync_cache()

    async def _turn_off_acs(self, active: dict) -> None:
        """Oprește doar AC-urile (histerezis) — starea OFF devine cea apărată de gardă."""
        gen = self._gen
        async with self._cmd_lock:
            for entity_id in self.ac_units:
                if gen != self._gen:
                    return
                self.policy.note_command(entity_id, self._ts())
                # OFF devine starea dorită chiar dacă apelul eșuează: reconcilierea îl
                # reîncearcă (altfel unitatea ar rămâne pe setpoint extrem, nesupravegheată).
                self._commanded[entity_id] = {"hvac_mode": "off"}
                ok = await self._climate_call("turn_off", {"entity_id": entity_id})
                if gen != self._gen:
                    return
                if ok:
                    self._failed.discard(entity_id)
                else:
                    self._failed.add(entity_id)
                self.policy.note_command(entity_id, self._ts())
        active["acs_on"] = False
        self._sync_cache()

    def _indoor_for_control(self, now: dt.datetime) -> float | None:
        """Temperatura sălii pentru termostat; None și când senzorul e „înghețat"
        (n-a mai raportat nimic de mult — bateria moartă îl lasă pe ultima valoare)."""
        value = self.indoor_temp
        if value is None:
            return None
        state = self.hass.states.get(self.indoor_sensor)
        last = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        if last is not None and (now - last).total_seconds() > SENSOR_STALE_MIN * 60.0:
            return None
        return value

    async def _room_thermostat(
        self, active: dict, phase_target: float, fan_cap: str, now: dt.datetime
    ) -> None:
        """Histerezis pe senzorul de sală: pornește/oprește AC-urile după nevoia reală."""
        mode = active["plan"]["mode"]
        if not self._room_control_on() or not self.ac_units:
            return
        if not any(e in self.ac_units for e in self._actuators_for(mode)):
            return  # în modul curent lucrează doar centrala
        cooling = mode == MODE_COOL
        hvac_mode = "cool" if cooling else "heat"
        indoor = self._indoor_for_control(now)
        if indoor is None:
            # Fără senzor nu mai există termostat: AC-urile NU pot rămâne pe 12°/30°.
            since = active.setdefault("sensor_lost_since", now)
            lost_min = (now - since).total_seconds() / 60.0
            if not active.get("sensor_fallback") and lost_min >= SENSOR_LOST_FALLBACK_MIN:
                _LOGGER.warning(
                    "Senzorul de sală e indisponibil de %.0f min — AC-urile trec pe ținta %.1f°",
                    lost_min, phase_target,
                )
                active["sensor_fallback"] = True
                await self._command_acs(active, hvac_mode, phase_target, fan_cap)
                self._notify(
                    "ClimateFlow — senzor de sală indisponibil",
                    "Senzorul de temperatură al sălii nu răspunde. Aerele au trecut pe "
                    "ținta normală (termostatul lor intern) până revine senzorul.",
                    kind="sensor",
                )
            return
        active.pop("sensor_lost_since", None)
        if active.get("sensor_fallback"):
            _LOGGER.info("Senzorul de sală a revenit — AC-urile revin pe setpoint extrem")
            active["sensor_fallback"] = False
            await self._command_acs(active, hvac_mode, phase_target, fan_cap)
            if self._active is not active:
                return
        tol = self.get_float(VAL_TOLERANCE, DEFAULT_TOLERANCE_C)
        # Repornire la jumătate de toleranță: pragul întreg e cel de „confort sub țintă",
        # iar histerezisul normal nu trebuie să-l atingă la fiecare ciclu.
        need_more = (
            indoor > phase_target + tol / 2 if cooling else indoor < phase_target - tol / 2
        )
        satisfied = indoor < phase_target - tol if cooling else indoor > phase_target + tol
        acs_on = active.get("acs_on", True)
        if satisfied and acs_on:
            _LOGGER.info("Sala a atins ținta (%.1f°) — opresc AC-urile (histerezis)", indoor)
            await self._turn_off_acs(active)
        elif need_more and not acs_on:
            _LOGGER.info("Sala a derivat (%.1f°) — repornesc AC-urile", indoor)
            await self._command_acs(active, hvac_mode, phase_target, fan_cap)

    async def _command_entity(
        self,
        entity_id: str,
        hvac_mode: str,
        temperature: float,
        fan_cap: str,
        gen: int | None = None,
    ) -> None:
        if gen is None:
            gen = self._gen
        self.policy.note_command(entity_id, self._ts())
        # Starea DORITĂ se consemnează integral, chiar dacă un apel eșuează:
        # reconcilierea vede devierea și reîncearcă.
        commanded: dict = {"hvac_mode": hvac_mode, "temperature": temperature}
        self._commanded[entity_id] = commanded
        ok = await self._climate_call(
            "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": hvac_mode}
        )
        if gen != self._gen:
            return
        # Pauză: unele integrări pierd set_temperature dacă vine imediat după pornire.
        await asyncio.sleep(self._cmd_gap)
        if gen != self._gen:
            return
        ok &= await self._climate_call(
            "set_temperature", {"entity_id": entity_id, "temperature": temperature}
        )
        if gen != self._gen:
            return

        state = self.hass.states.get(entity_id)
        fan_modes = state.attributes.get("fan_modes") if state is not None else None
        fan_mode = map_fan_mode(fan_cap, fan_modes)
        if fan_mode is not None:
            commanded["fan_mode"] = fan_mode
            ok &= await self._climate_call(
                "set_fan_mode", {"entity_id": entity_id, "fan_mode": fan_mode}
            )
            if gen != self._gen:
                return

        if ok:
            self._failed.discard(entity_id)
        else:
            self._failed.add(entity_id)
        self._await_verify.add(entity_id)
        self.policy.note_command(entity_id, self._ts())
        self._sync_cache()

    async def async_all_off(self, entities: list[str] | None = None) -> None:
        """Oprește actuatoarele date (implicit toate) — cu timeout și 3 încercări;
        notifică dacă eșuează."""
        actuators = self.all_actuators() if entities is None else list(entities)
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
        """Comută markerul de skip pe o ocurență anume (din agenda panoului).

        O ocurență oprită manual („Oprește tot") apare tot ca sărită — același
        comutator o reactivează."""
        if key in self._stopped:
            self._stopped.discard(key)
        elif key in self._skips:
            self._skips.discard(key)
        else:
            self._skips.add(key)
        self._persist()
        self.recompute()

    @callback
    def skip_next(self) -> None:
        """Comută markerul de skip pe următoarea ocurență programată.

        Caută INCLUSIV printre cele deja sărite — altfel a doua apăsare ar sări și
        evenimentul de după, în loc să anuleze."""
        upcoming = self._next_any(dt_util.now())
        if upcoming is None:
            return
        key = upcoming["key"]
        armed = key not in self._skips and key not in self._stopped
        self.toggle_skip(key)
        if armed:
            self._notify(
                "ClimateFlow — eveniment sărit",
                f"„{upcoming['name']}” din {upcoming['start'].strftime('%d.%m %H:%M')} "
                "nu va porni climatizarea.",
                kind="skip",
            )

    async def async_boost_now(self) -> None:
        """Pornește climatizarea acum.

        - eveniment în desfășurare (oprit manual) → îl reia;
        - următorul eveniment începe în cel mult „Avans maxim" → precondiționare spre el;
        - altfel → rulare manuală de 60 de minute (NU precondiționare de zile întregi
          spre un eveniment îndepărtat).
        """
        if self.state_name in RUN_STATES:
            return
        task = self._cooldown_task
        if task is not None:
            await asyncio.wait({task})
        now = dt_util.now()
        occs = self._occurrences(now, include_stopped=True)
        current = self._pick_current(occs, now)
        upcoming = schedule.next_occurrence(occs, now)
        max_lead = dt.timedelta(minutes=self.get_float(VAL_MAX_LEAD, DEFAULT_MAX_LEAD_MIN))
        if current is not None:
            self._stopped.discard(current["key"])
        elif upcoming is not None and upcoming["start"] - now <= max_lead:
            self._stopped.discard(upcoming["key"])
            self._force_key = upcoming["key"]
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
        self._persist()
        await self.async_refresh()

    async def async_stop(self) -> None:
        """Oprește tot: forțează cooldown (totul OFF), apoi mâinile jos.

        Ocurența oprită NU mai repornește la următorul tick — sistemul stă în repaus
        până la următorul eveniment (sau până la „Pornește acum").

        NU are voie să arunce NICIODATĂ — e calea de urgență (lecția ZoneFlow)."""
        try:
            self._manual_occ = None
            active = self._active
            if (
                active is not None
                and not active.get("manual")
                and self.state_name in RUN_STATES
            ):
                self._stopped.add(active["key"])
            if self.state_name != STATE_COOLDOWN:
                self._enter_cooldown("oprire manuală", everything=True)
            self._persist()
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
                "skipped": occ["key"] in self._skips or occ["key"] in self._stopped,
            }
            for occ in occs
        ]

    def _build_data(self) -> dict:
        now = dt_util.now()
        occs = self._occurrences(now)
        current = self._pick_current(occs, now)
        upcoming = schedule.next_occurrence(occs, now)
        next_occ = current or upcoming
        next_any = self._next_any(now)

        active = self._active
        if active is not None and self.state_name in RUN_STATES:
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
        if active is not None and self.state_name in RUN_STATES:
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
                next_any is not None
                and (next_any["key"] in self._skips or next_any["key"] in self._stopped)
            ),
            "skips": sorted(self._skips),
        }

    @callback
    def recompute(self) -> None:
        """Recalculează și împinge datele către senzori (fără să aștepte tick-ul)."""
        if self.hass is None or self._dead:
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
            "lags": {m: round(v, 1) for m, v in self._lags.items()},
            "suggestion": self._max_lead_suggestion(now),
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
