"""Modul PUR de învățare termică: rate °C/h pe benzi de temperatură exterioară.

Fără importuri Home Assistant — testabil direct cu pytest.

Modelul: pentru fiecare mod de funcționare (cool / heat_boiler / heat_boiler_boost)
ținem o medie mobilă exponențială (EMA) a ratei realizate (°C/h), grupată pe benzi
de temperatură exterioară. Explicabil („când afară e ~0°C, centrala urcă ~1.2°C/h"),
robust la puține eșantioane, trivial de serializat.
"""

from __future__ import annotations

import math

INF = float("inf")

# Benzi (lo, hi, cheie) — hi exclusiv. Ordinea contează la fallback-ul pe vecini.
BANDS_HEAT: list[tuple[float, float, str]] = [
    (-INF, -5.0, "b0"),
    (-5.0, 0.0, "b1"),
    (0.0, 5.0, "b2"),
    (5.0, 10.0, "b3"),
    (10.0, INF, "b4"),
]
BANDS_COOL: list[tuple[float, float, str]] = [
    (-INF, 25.0, "c0"),
    (25.0, 30.0, "c1"),
    (30.0, 35.0, "c2"),
    (35.0, INF, "c3"),
]

EMA_ALPHA = 0.3
MIN_SAMPLE_MINUTES = 15.0
MIN_SAMPLE_DELTA = 0.3
MAX_SANE_RATE = 10.0  # °C/h — peste asta e glitch de senzor, nu fizică
# Podeaua ratei învățate dintr-o rulare „prea lentă” (evită 0 → avans infinit).
MIN_LEARN_RATE = 0.1

# Timpul mort (inerția de pornire): minutele până când sala începe efectiv să urce /
# coboare. Se învață per mod (EMA) și se adună la avans.
LAG_DELTA = 0.2  # °C — prima mișcare clară în sensul bun
MAX_LAG_MIN = 60.0  # peste asta nu mai e inerție, e lipsă de putere (o prinde rata)

DEFAULT_MARGIN = 1.15
MIN_LEAD_MIN = 10.0

MODE_COOL = "cool"


def _bands_for(mode: str) -> list[tuple[float, float, str]]:
    return BANDS_COOL if mode == MODE_COOL else BANDS_HEAT


def band_key(outdoor: float, mode: str) -> str:
    """Cheia benzii în care pică temperatura exterioară pentru modul dat."""
    for lo, hi, key in _bands_for(mode):
        if lo <= outdoor < hi:
            return key
    return _bands_for(mode)[-1][2]


def observed_rate(
    start_temp: float | None,
    end_temp: float | None,
    minutes: float,
) -> float | None:
    """Rata realizată |ΔT|/h a unei precondiționări, sau None dacă eșantionul e invalid.

    Invalid: rulare prea scurtă, delta sub pragul de zgomot, senzori lipsă sau
    rată absurdă (glitch).
    """
    if start_temp is None or end_temp is None:
        return None
    if minutes < MIN_SAMPLE_MINUTES:
        return None
    delta = abs(end_temp - start_temp)
    if delta < MIN_SAMPLE_DELTA:
        return None
    rate = delta / (minutes / 60.0)
    if rate > MAX_SANE_RATE:
        return None
    return rate


def update_rate(
    rates: dict, mode: str, band: str, sample: float, alpha: float = EMA_ALPHA
) -> dict:
    """Întoarce o copie a `rates` cu EMA actualizată; primul eșantion seedează direct."""
    out = {m: dict(bands) for m, bands in rates.items()}
    mode_rates = out.setdefault(mode, {})
    prev = mode_rates.get(band)
    mode_rates[band] = sample if prev is None else prev + alpha * (sample - prev)
    return out


def effective_rate(rates: dict, mode: str, band: str, default: float) -> float:
    """Rata de folosit: banda exactă → cea mai apropiată bandă vecină → default."""
    mode_rates = rates.get(mode) or {}
    if band in mode_rates:
        return mode_rates[band]
    keys = [key for _, _, key in _bands_for(mode)]
    if band in keys:
        idx = keys.index(band)
        by_distance = sorted(range(len(keys)), key=lambda i: abs(i - idx))
        for i in by_distance:
            if keys[i] in mode_rates:
                return mode_rates[keys[i]]
    return default


def update_lag(lags: dict, mode: str, sample_min: float, alpha: float = EMA_ALPHA) -> dict:
    """Copie a `lags` cu timpul mort al modului actualizat (EMA; primul eșantion seedează)."""
    out = dict(lags)
    sample = min(max(float(sample_min), 0.0), MAX_LAG_MIN)
    prev = out.get(mode)
    out[mode] = sample if prev is None else prev + alpha * (sample - prev)
    return out


def needed_minutes(
    needed_delta: float,
    rate_c_per_h: float,
    margin: float = DEFAULT_MARGIN,
    lag_min: float = 0.0,
) -> float:
    """Minutele necesare, NEPLAFONATE: timp mort + Δ/rată × marjă (inf la rată ≤ 0)."""
    if needed_delta <= 0:
        return 0.0
    if rate_c_per_h <= 0:
        return INF
    return lag_min + needed_delta / rate_c_per_h * 60.0 * margin


def lead_minutes(
    needed_delta: float,
    rate_c_per_h: float,
    max_lead: float,
    margin: float = DEFAULT_MARGIN,
    min_lead: float = MIN_LEAD_MIN,
    lag_min: float = 0.0,
) -> float:
    """Minutele de avans pentru a acoperi `needed_delta` la rata dată, cu marjă,
    plus timpul mort învățat (`lag_min`).

    0 dacă nu e nimic de recuperat; altfel limitat la [min_lead, max_lead].
    """
    if needed_delta <= 0:
        return 0.0
    minutes = needed_minutes(needed_delta, rate_c_per_h, margin, lag_min)
    if minutes == INF:
        return max_lead
    return min(max(float(math.ceil(minutes)), min_lead), max_lead)


def required_rate(needed_delta: float, minutes_available: float) -> float:
    """Rata (°C/h) necesară ca să acoperi delta în timpul disponibil."""
    if needed_delta <= 0:
        return 0.0
    if minutes_available <= 0:
        return INF
    return needed_delta / (minutes_available / 60.0)
