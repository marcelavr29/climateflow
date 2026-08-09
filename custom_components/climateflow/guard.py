"""Modul PUR al gărzii anti-modificare: detectarea devierii + politica anti-luptă.

Fără importuri Home Assistant — testabil direct cu pytest. Coordinatorul îi dă
timpul (`ts`, secunde monotone sau epoch) și starea entităților; politica decide
dacă și ce re-asertăm.
"""

from __future__ import annotations

TEMP_TOLERANCE_C = 0.5
GRACE_S = 45.0  # după propria comandă ignorăm „devierile" (echo optimist al integrării)
DEBOUNCE_S = 20.0  # absoarbe rafala de schimbări de la o telecomandă IR
MAX_REASSERTS = 3  # per entitate, per fereastră
WINDOW_S = 300.0
BACKOFF_S = 300.0  # pauză după depășirea limitei (cineva insistă de la telecomandă)


def deviations(commanded: dict, actual: dict, tolerance: float = TEMP_TOLERANCE_C) -> dict:
    """Sub-setul de atribute comandate care diferă în starea reală.

    `commanded`/`actual`: {"hvac_mode": str, "temperature": float, "fan_mode": str} —
    cheile lipsă din `commanded` nu se compară (ex. centrala fără fan). O stare
    reală `unavailable`/`unknown`/None NU e deviere (integrarea poate fi doar căzută).
    """
    out: dict = {}
    hvac_actual = actual.get("hvac_mode")
    if (
        "hvac_mode" in commanded
        and hvac_actual not in (None, "unavailable", "unknown")
        and hvac_actual != commanded["hvac_mode"]
    ):
        out["hvac_mode"] = commanded["hvac_mode"]
    temp_actual = actual.get("temperature")
    if (
        "temperature" in commanded
        and temp_actual is not None
        and abs(float(temp_actual) - float(commanded["temperature"])) > tolerance
    ):
        out["temperature"] = commanded["temperature"]
    fan_actual = actual.get("fan_mode")
    if (
        "fan_mode" in commanded
        and fan_actual not in (None, "unavailable", "unknown")
        and fan_actual != commanded["fan_mode"]
    ):
        out["fan_mode"] = commanded["fan_mode"]
    return out


class GuardPolicy:
    """Politica anti-luptă per entitate: grace, rate-limit, backoff."""

    def __init__(
        self,
        grace_s: float = GRACE_S,
        max_reasserts: int = MAX_REASSERTS,
        window_s: float = WINDOW_S,
        backoff_s: float = BACKOFF_S,
    ) -> None:
        self.grace_s = grace_s
        self.max_reasserts = max_reasserts
        self.window_s = window_s
        self.backoff_s = backoff_s
        self._last_command: dict[str, float] = {}
        self._reasserts: dict[str, list[float]] = {}
        self._backoff_until: dict[str, float] = {}

    def note_command(self, entity_id: str, ts: float) -> None:
        """Coordinatorul a trimis chiar el o comandă — pornește fereastra de grace."""
        self._last_command[entity_id] = ts

    def in_grace(self, entity_id: str, ts: float) -> bool:
        last = self._last_command.get(entity_id)
        return last is not None and ts - last < self.grace_s

    def in_backoff(self, entity_id: str, ts: float) -> bool:
        until = self._backoff_until.get(entity_id)
        return until is not None and ts < until

    def is_fighting(self, entity_id: str, ts: float) -> bool:
        """True cât timp entitatea e în backoff (cineva insistă de la telecomandă)."""
        return self.in_backoff(entity_id, ts)

    def should_check(self, entity_id: str, ts: float) -> bool:
        """Merită să comparăm starea acum? (nu în grace, nu în backoff)."""
        return not self.in_grace(entity_id, ts) and not self.in_backoff(entity_id, ts)

    def note_reassert(self, entity_id: str, ts: float) -> bool:
        """Înregistrează o re-asertare. False ⇒ limita atinsă → intră în backoff
        (apelantul NU trebuie să trimită comanda)."""
        window = [
            t for t in self._reasserts.get(entity_id, []) if ts - t < self.window_s
        ]
        if len(window) >= self.max_reasserts:
            self._backoff_until[entity_id] = ts + self.backoff_s
            self._reasserts[entity_id] = window
            return False
        window.append(ts)
        self._reasserts[entity_id] = window
        return True

    def reset(self, entity_id: str | None = None) -> None:
        """Uită istoricul (la ieșirea din perioada gestionată)."""
        if entity_id is None:
            self._reasserts.clear()
            self._backoff_until.clear()
        else:
            self._reasserts.pop(entity_id, None)
            self._backoff_until.pop(entity_id, None)
