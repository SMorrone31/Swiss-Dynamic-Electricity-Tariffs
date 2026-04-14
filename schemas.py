"""
schemas.py
==========
Strutture dati centrali del sistema.

Tutto il sistema ruota attorno a due classi:
  - PriceSlot  → un singolo slot da 15 minuti con i prezzi in CHF/kWh
  - NormalizedPrices → collezione di slot per una tariffa e un giorno

Gli adapter prendono la risposta grezza di ogni EVU e la convertono
in NormalizedPrices. Il resto del sistema (scheduler, DB, API)
lavora SOLO con queste strutture — non sa nulla dei formati originali.

Tutto in UTC. Tutti i prezzi in CHF/kWh (non Rp/kWh).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional


# ── Costanti ──────────────────────────────────────────────────────────────────

SLOT_MINUTES = 15
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES  # 96


# ── PriceSlot ─────────────────────────────────────────────────────────────────

@dataclass
class PriceSlot:
    """
    Un singolo slot temporale da 15 minuti.

    Campi prezzi:
      - energy_price   → componente energia  (CHF/kWh), None se non disponibile
      - grid_price     → componente rete     (CHF/kWh), None se non disponibile
      - residual_price → componente residua  (CHF/kWh), None se non disponibile
                         (SDL, Stromreserve, Netzzuschlag, Abgaben, ecc.)

    Il prezzo totale è la somma dei tre componenti disponibili.
    Usare total_price() per ottenerlo.

    Note sul DST (Daylight Saving Time):
      In una giornata di passaggio ora legale (marzo) ci sono 23 ore = 92 slot.
      In una giornata di passaggio ora solare (ottobre) ci sono 25 ore = 100 slot.
      Salvando sempre in UTC non ci sono buchi né duplicati.
    """

    slot_start_utc:  datetime       # inizio slot in UTC (es. 2026-03-16T00:00:00+00:00)
    energy_price:    Optional[float] = None  # CHF/kWh
    grid_price:      Optional[float] = None  # CHF/kWh
    residual_price:  Optional[float] = None  # CHF/kWh

    def __post_init__(self) -> None:
        # Garantisce che il datetime sia sempre timezone-aware UTC
        if self.slot_start_utc.tzinfo is None:
            raise ValueError(
                f"slot_start_utc deve essere timezone-aware (UTC). "
                f"Ricevuto: {self.slot_start_utc}. "
                f"Usa datetime(..., tzinfo=timezone.utc) o .replace(tzinfo=timezone.utc)."
            )
        # Normalizza sempre a UTC
        self.slot_start_utc = self.slot_start_utc.astimezone(timezone.utc)

    @property
    def slot_end_utc(self) -> datetime:
        """Fine dello slot (= inizio slot successivo)."""
        return self.slot_start_utc + timedelta(minutes=SLOT_MINUTES)

    @property
    def total_price(self) -> Optional[float]:
        """
        Somma dei componenti disponibili in CHF/kWh.
        Ritorna None se nessun componente è disponibile.
        """
        parts = [p for p in (self.energy_price, self.grid_price, self.residual_price)
                 if p is not None]
        return sum(parts) if parts else None

    @property
    def total_price_rappen(self) -> Optional[float]:
        """Prezzo totale in Rp/kWh (centesimi di franco)."""
        t = self.total_price
        return round(t * 100, 4) if t is not None else None

    def to_dict(self) -> dict:
        """Serializzazione per il DB e l'API."""
        return {
            "slot_start_utc":  self.slot_start_utc.isoformat(),
            "slot_end_utc":    self.slot_end_utc.isoformat(),
            "energy_price":    self.energy_price,
            "grid_price":      self.grid_price,
            "residual_price":  self.residual_price,
            "total_price":     self.total_price,
        }

    def __repr__(self) -> str:
        t = self.total_price
        price_str = f"{t*100:.2f} Rp/kWh" if t is not None else "N/A"
        return (
            f"PriceSlot({self.slot_start_utc.strftime('%Y-%m-%d %H:%M UTC')} "
            f"| {price_str})"
        )


# ── NormalizedPrices ──────────────────────────────────────────────────────────

@dataclass
class NormalizedPrices:
    """
    Collezione di slot per una tariffa e un giorno.

    Questa è la struttura che ogni adapter deve produrre.
    Contiene tutti gli slot del giorno richiesto (normalmente 96,
    ma 92 o 100 nei giorni di cambio ora).

    Il campo 'source_date' è la data del giorno a cui si riferiscono
    i prezzi (giorno successivo rispetto al fetch, in data locale CH).
    """

    tariff_id:    str           # es. "ckw_home_dynamic"
    source_date:  date          # data a cui si riferiscono i prezzi (ora locale CH)
    fetched_at:   datetime      # quando è stato fatto il fetch (UTC)
    slots:        list[PriceSlot] = field(default_factory=list)

    # Metadati opzionali aggiunti dall'adapter
    adapter_name: str = ""      # es. "CkwAdapter"
    raw_url:      str = ""      # URL chiamato per debug
    warnings:     list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at deve essere timezone-aware (UTC).")
        self.fetched_at = self.fetched_at.astimezone(timezone.utc)

    # ── Proprietà di validazione ──────────────────────────────────────────────

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @property
    def is_complete(self) -> bool:
        """
        True se ci sono i 96 slot attesi (o 92/100 nei giorni DST).
        Considera completo se ci sono tra 90 e 102 slot.
        """
        return 90 <= self.slot_count <= 102

    @property
    def has_energy_prices(self) -> bool:
        return any(s.energy_price is not None for s in self.slots)

    @property
    def has_grid_prices(self) -> bool:
        return any(s.grid_price is not None for s in self.slots)

    @property
    def has_residual_prices(self) -> bool:
        return any(s.residual_price is not None for s in self.slots)

    # ── Statistiche rapide ────────────────────────────────────────────────────

    @property
    def avg_total_price(self) -> Optional[float]:
        totals = [s.total_price for s in self.slots if s.total_price is not None]
        return sum(totals) / len(totals) if totals else None

    @property
    def min_slot(self) -> Optional[PriceSlot]:
        slots_with_price = [s for s in self.slots if s.total_price is not None]
        return min(slots_with_price, key=lambda s: s.total_price) if slots_with_price else None

    @property
    def max_slot(self) -> Optional[PriceSlot]:
        slots_with_price = [s for s in self.slots if s.total_price is not None]
        return max(slots_with_price, key=lambda s: s.total_price) if slots_with_price else None

    # ── Serializzazione ───────────────────────────────────────────────────────

    def to_api_response(self) -> dict:
        """
        Formato di risposta dell'API, esattamente come definito nel PDF:
          energy_price_utc:   [{start_time: netto_price}, ...]
          grid_price_utc:     [{start_time: netto_price}, ...]
          residual_price_utc: [{start_time: netto_price}, ...]
        """
        def build_series(attr: str) -> list[dict]:
            return [
                {s.slot_start_utc.isoformat(): getattr(s, attr)}
                for s in self.slots
                if getattr(s, attr) is not None
            ]

        response: dict = {
            "tariff_id":   self.tariff_id,
            "source_date": self.source_date.isoformat(),
            "fetched_at":  self.fetched_at.isoformat(),
            "slot_count":  self.slot_count,
        }

        energy   = build_series("energy_price")
        grid     = build_series("grid_price")
        residual = build_series("residual_price")

        if energy:
            response["energy_price_utc"] = energy
        if grid:
            response["grid_price_utc"] = grid
        if residual:
            response["residual_price_utc"] = residual

        return response

    def to_db_rows(self) -> list[dict]:
        """Righe da inserire in price_slots nel DB."""
        return [
            {
                "tariff_id":      self.tariff_id,
                "slot_start_utc": s.slot_start_utc,
                "energy_price":   s.energy_price,
                "grid_price":     s.grid_price,
                "residual_price": s.residual_price,
                "fetched_at_utc": self.fetched_at,
            }
            for s in self.slots
        ]

    def summary(self) -> str:
        avg = self.avg_total_price
        mn  = self.min_slot
        mx  = self.max_slot
        avg_str = f"{avg*100:.2f} Rp/kWh" if avg else "N/A"
        mn_str  = f"{mn.slot_start_utc.strftime('%H:%M')} ({mn.total_price*100:.2f} Rp)" if mn else "N/A"
        mx_str  = f"{mx.slot_start_utc.strftime('%H:%M')} ({mx.total_price*100:.2f} Rp)" if mx else "N/A"
        return (
            f"NormalizedPrices({self.tariff_id} | {self.source_date} | "
            f"{self.slot_count} slot | avg={avg_str} | "
            f"min={mn_str} | max={mx_str})"
        )

    def __repr__(self) -> str:
        return self.summary()


# ── Helpers UTC ───────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    """Datetime corrente in UTC."""
    return datetime.now(timezone.utc)


def make_day_range_utc(target_date: date) -> tuple[datetime, datetime]:
    """
    Ritorna (start_utc, end_utc) corrispondenti alla mezzanotte locale svizzera
    (CET/CEST) del giorno richiesto, espressi in UTC.

    es. date(2026, 3, 17) in CET (UTC+1):
        → (2026-03-16T23:00:00Z, 2026-03-17T23:00:00Z)

    es. date(2026, 7, 1) in CEST (UTC+2):
        → (2026-06-30T22:00:00Z, 2026-07-01T22:00:00Z)

    Questo è il range corretto da passare alle API degli EVU svizzeri,
    che pubblicano i prezzi per giornata locale (CET/CEST), non UTC.
    Gestisce automaticamente i cambi ora (23h/24h/25h per DST).

    Usato dagli adapter per costruire i parametri start/end della chiamata API.
    """
    try:
        import zoneinfo
        tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        tz_ch = pytz.timezone("Europe/Zurich")

    midnight_local = datetime(target_date.year, target_date.month, target_date.day,
                              tzinfo=tz_ch)
    midnight_next  = midnight_local + timedelta(days=1)
    return midnight_local.astimezone(timezone.utc), midnight_next.astimezone(timezone.utc)


def tomorrow_ch() -> date:
    """
    Data di domani in ora locale svizzera (CET/CEST).
    Usato dallo scheduler per sapere per quale giorno richiedere i prezzi.
    """
    import zoneinfo
    tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    now_ch = datetime.now(tz_ch)
    return (now_ch + timedelta(days=1)).date()

def today_ch() -> date:
    """
    Data di OGGI in ora locale svizzera (CET/CEST).
    Da usare ovunque al posto di date.today() che ritorna la data UTC.
    Tra le 22:00-00:00 UTC (= mezzanotte svizzera CEST), date.today()
    ritornerebbe il giorno sbagliato.
    """
    try:
        import zoneinfo
        tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
    except ImportError:
        import pytz
        tz_ch = pytz.timezone("Europe/Zurich")
    return datetime.now(timezone.utc).astimezone(tz_ch).date()