"""Modul PUR de program: expandarea ocurențelor + regula săptămânii de tranziție.

Fără importuri Home Assistant — testabil direct cu pytest.

Regula „săptămânii de tranziție de lună" (specifică programului clădirii):
o zi `d` este în săptămâna de tranziție dacă duminica ce încheie săptămâna ei
(prima duminică ≥ d; d însăși dacă e duminică) pică într-o ALTĂ lună calendaristică
decât d. Exemplu: joi 29 oct 2026 → duminica ei e 1 nov → tranziție (joia sare,
vinerea se ține). O duminică nu e niciodată în tranziție față de ea însăși.
"""

from __future__ import annotations

import datetime as dt

WF_ALWAYS = "always"
WF_ONLY_TRANSITION = "only_transition"
WF_EXCEPT_TRANSITION = "except_transition"

DEFAULT_HORIZON_DAYS = 14


def sunday_of_week(d: dt.date) -> dt.date:
    """Prima duminică ≥ d (luni=0 … duminică=6)."""
    return d + dt.timedelta(days=(6 - d.weekday()) % 7)


def is_month_transition(d: dt.date) -> bool:
    """True dacă duminica săptămânii lui `d` pică în altă lună (conștient de an)."""
    s = sunday_of_week(d)
    return (s.year, s.month) != (d.year, d.month)


def _passes_week_filter(week_filter: str, d: dt.date) -> bool:
    if week_filter == WF_ONLY_TRANSITION:
        return is_month_transition(d)
    if week_filter == WF_EXCEPT_TRANSITION:
        return not is_month_transition(d)
    return True  # always / necunoscut → permisiv


def _parse_hm(value: str) -> dt.time | None:
    try:
        hour, minute = value.split(":")
        return dt.time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def occurrence_key(event_id: str, d: dt.date) -> str:
    """Cheia unei ocurențe — folosită de markerii de skip (`skip_next`)."""
    return f"{event_id}:{d.isoformat()}"


def _occurrence(
    event: dict, d: dt.date, tz: dt.tzinfo, *, one_off: bool = False
) -> dict | None:
    start_t = _parse_hm(event.get("start", ""))
    end_t = _parse_hm(event.get("end", ""))
    if start_t is None or end_t is None or end_t <= start_t:
        return None
    return {
        "event_id": event.get("id", ""),
        "name": event.get("name", ""),
        "date": d,
        "start": dt.datetime.combine(d, start_t, tzinfo=tz),
        "end": dt.datetime.combine(d, end_t, tzinfo=tz),
        "target": event.get("target"),
        "one_off": one_off,
        "key": occurrence_key(event.get("id", ""), d),
    }


def expand_occurrences(
    events: list[dict],
    one_offs: list[dict],
    skips: set[str],
    now: dt.datetime,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> list[dict]:
    """Ocurențele următoare (cu `end` în viitor), sortate după start.

    Include ocurența aflată în desfășurare (start ≤ now < end) — un restart HA
    în mijlocul unui eveniment trebuie să o regăsească. Ocurențele cu marker de
    skip sunt excluse.
    """
    tz = now.tzinfo
    today = now.date()
    out: list[dict] = []

    for offset in range(horizon_days + 1):
        d = today + dt.timedelta(days=offset)
        for event in events:
            if event.get("enabled", True) is False:
                continue
            if d.weekday() not in event.get("days", []):
                continue
            if not _passes_week_filter(event.get("week_filter", WF_ALWAYS), d):
                continue
            occ = _occurrence(event, d, tz)
            if occ is not None:
                out.append(occ)

    for item in one_offs:
        try:
            d = dt.date.fromisoformat(item.get("date", ""))
        except ValueError:
            continue
        if d < today or d > today + dt.timedelta(days=horizon_days):
            continue
        occ = _occurrence(item, d, tz, one_off=True)
        if occ is not None:
            out.append(occ)

    out = [o for o in out if o["end"] > now and o["key"] not in skips]
    out.sort(key=lambda o: o["start"])
    return out


def current_occurrence(occurrences: list[dict], now: dt.datetime) -> dict | None:
    """Ocurența în desfășurare acum, dacă există."""
    for occ in occurrences:
        if occ["start"] <= now < occ["end"]:
            return occ
    return None


def next_occurrence(occurrences: list[dict], now: dt.datetime) -> dict | None:
    """Prima ocurență care începe în viitor."""
    for occ in occurrences:
        if occ["start"] > now:
            return occ
    return None


def consume_past_skips(skips: set[str], now: dt.datetime) -> set[str]:
    """Elimină markerii de skip ale căror date au trecut (curățare la tick)."""
    today = now.date()
    kept: set[str] = set()
    for key in skips:
        _, _, date_part = key.rpartition(":")
        try:
            d = dt.date.fromisoformat(date_part)
        except ValueError:
            continue
        if d >= today:
            kept.add(key)
    return kept
