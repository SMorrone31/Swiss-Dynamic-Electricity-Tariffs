"""
adapters/ail.py
===============
Adapter per Aziende industriali di Lugano (AIL) SA — Tariffa Dinamica.

Fonte: https://www.ail.ch/privati/elettricita/prodotti/Tariffa-dinamica/tariffa-dinamica.html
Accesso: pubblico, nessuna autenticazione
Metodo: web scraping (HTML) — AIL non espone un'API JSON pubblica.

STRUTTURA TARIFFARIA AIL
─────────────────────────
AIL pubblica i prezzi suddivisi in 4 fasce orarie (ora locale CH):

  Fascia         Ore locali CH    UTC inverno (CET+1)   UTC estate (CEST+2)
  ──────────────────────────────────────────────────────────────────────────
  Mattutina      06:00 – 10:00    05:00 – 09:00         04:00 – 08:00
  Solare         10:00 – 17:00    09:00 – 16:00         08:00 – 15:00
  Serale         17:00 – 22:00    16:00 – 21:00         15:00 – 20:00
  Notturna       22:00 – 06:00    21:00 – 05:00         20:00 – 04:00

L'adapter espande ogni ora in 4 slot da 15 min -> 96 slot/giorno.

STRUTTURA TABELLA HTML
───────────────────────
  Col 0: Fascia (nome + orari)
  Col 1: Utilizzo rete [CHF/100kWh]          -> grid_price
  Col 2: Fornitura energia [CHF/100kWh]      \
  Col 3: Tiacqua [CHF/100kWh]                -> energy_price (somma)
  Col 4: Tasse [CHF/100kWh]                  -> residual_price
  Col 5: Totale [CHF/100kWh]                 (ignorato)

Tutti in CHF/100kWh (Rappen) -> dividiamo per 100 -> CHF/kWh.
"""

from __future__ import annotations

import logging
import re
import zoneinfo
from datetime import date, datetime, timezone
from typing import Optional

from adapters.base import (
    AdapterEmptyError,
    AdapterParseError,
    BaseAdapter,
)
from schemas import NormalizedPrices, PriceSlot, make_day_range_utc, utc_now

log = logging.getLogger(__name__)

TZ_CH = zoneinfo.ZoneInfo("Europe/Zurich")

_MONTH_IT: dict[str, int] = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

_BAND_KEYWORDS: dict[str, list[str]] = {
    "mattutina": ["mattutina", "mattino", "morning", "morgen"],
    "solare":    ["solare", "solar", "giorno", "mittag"],
    "serale":    ["serale", "sera", "evening", "abend"],
    "notturna":  ["notturna", "notte", "night", "nacht"],
}


class AilAdapter(BaseAdapter):
    """
    Adapter per AIL — Aziende industriali di Lugano SA.

    Config in evu_list.json:
      api_base_url -> URL della sottopagina prezzi AIL (non la pagina marketing)
      auth_type    -> "none"
      api_params:
        backfill_supported -> false
        scraping           -> true
    """

    TIMEOUT_SECONDS: int = 30

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        return self.base_url

    def _build_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-CH,it;q=0.9,de-CH;q=0.8",
            "Cache-Control":   "no-cache",
        }

    def _preprocess_response(self, raw: bytes) -> dict | list:
        """
        Override: parsa HTML invece di JSON.

        Ritorna:
          {
            "target_date_iso": "2026-04-19",   # data dalla pagina
            "bands": {
              "mattutina": {"grid": 0.0964, "energy": 0.1226, "residual": 0.0525},
              "solare":    {"grid": 0.0288, "energy": 0.0514, "residual": 0.0525},
              "serale":    {"grid": 0.1034, "energy": 0.0973, "residual": 0.0525},
              "notturna":  {"grid": 0.0645, "energy": 0.0800, "residual": 0.0525},
            }
          }
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise AdapterParseError(
                "beautifulsoup4 non installato — aggiungerlo a requirements.txt."
            )

        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        target_date_iso = self._extract_date(soup)
        self._log.info(f"AIL: data sulla pagina = {target_date_iso}")

        bands = self._extract_bands(soup)
        if not bands:
            raise AdapterEmptyError(
                "AIL: tabella prezzi non trovata. "
                "I prezzi potrebbero non essere ancora pubblicati "
                "(AIL pubblica entro le 18:00 ora locale CH). "
                "URL: " + self.base_url
            )

        if len(bands) < 4:
            self._log.warning(
                f"AIL: trovate {len(bands)}/4 fasce: {list(bands.keys())}"
            )

        return {"target_date_iso": target_date_iso, "bands": bands}

    def _parse(self, response_data: dict | list, target_date: date) -> list[PriceSlot]:
        """
        Converte le 4 fasce in 96 slot da 15 min UTC.
        """
        if not isinstance(response_data, dict):
            raise AdapterParseError(
                f"AIL: tipo risposta inatteso {type(response_data).__name__}"
            )

        bands: dict[str, dict] = response_data.get("bands", {})
        if not bands:
            raise AdapterEmptyError("AIL: nessuna fascia estratta.")

        # Data degli slot: dalla pagina se disponibile
        date_iso = response_data.get("target_date_iso")
        if date_iso:
            try:
                slot_date = date.fromisoformat(date_iso)
            except ValueError:
                self._log.warning(f"AIL: data non parsabile ({date_iso!r}), uso {target_date}")
                slot_date = target_date
        else:
            self._log.warning(f"AIL: data non trovata sulla pagina, uso {target_date}")
            slot_date = target_date

        self._log.info(f"AIL: genero slot per {slot_date}, fasce: {list(bands.keys())}")

        hourly: list[tuple[datetime, Optional[float], Optional[float], Optional[float]]] = []
        for hour in range(24):
            local_dt = datetime(
                slot_date.year, slot_date.month, slot_date.day,
                hour, 0, 0, tzinfo=TZ_CH,
            )
            utc_dt   = local_dt.astimezone(timezone.utc)
            bd       = bands.get(_hour_to_band(hour), {})
            hourly.append((utc_dt, bd.get("energy"), bd.get("grid"), bd.get("residual")))

        slots = self.make_slots_from_hourly(hourly)
        if not slots:
            raise AdapterEmptyError(f"AIL: 0 slot generati per {slot_date}.")

        self._log.info(f"AIL: {len(slots)} slot generati per {slot_date}")
        return slots

    async def fetch(self, target_date: date) -> NormalizedPrices:
        """
        Override: usa la data reale della pagina come source_date
        (pattern identico ad AemAdapter).
        """
        start_utc, end_utc = make_day_range_utc(target_date)
        url        = self._build_url(start_utc, end_utc)
        headers    = self._build_headers()
        fetched_at = utc_now()

        self._log.info(f"AIL scraping {target_date} -> {url}")

        raw_response  = await self._http_get_with_retry(url, headers)
        response_data = self._preprocess_response(raw_response)
        slots         = self._parse(response_data, target_date)

        if not slots:
            raise AdapterEmptyError(f"{self.tariff_id}: nessun slot generato")

        actual_date = slots[0].slot_start_utc.astimezone(TZ_CH).date()
        if actual_date != target_date:
            self._log.info(
                f"AIL: pagina contiene {actual_date} (richiesto {target_date}) — normale day-ahead."
            )

        result = NormalizedPrices(
            tariff_id    = self.tariff_id,
            source_date  = actual_date,
            fetched_at   = fetched_at,
            slots        = sorted(slots, key=lambda s: s.slot_start_utc),
            adapter_name = self.__class__.__name__,
            raw_url      = url,
        )

        if not result.is_complete:
            result.warnings.append(f"Slot count inatteso: {result.slot_count} (attesi 96)")
            self._log.warning(result.warnings[-1])

        self._log.info(result.summary())
        return result

    # ── HTML helpers ──────────────────────────────────────────────────────────

    def _extract_date(self, soup) -> Optional[str]:
        """Trova la data italiana nella pagina, es. '19 aprile 2026' -> '2026-04-19'."""
        text = soup.get_text(separator=" ")

        m = re.search(
            r"(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|"
            r"luglio|agosto|settembre|ottobre|novembre|dicembre)\s+(20\d{2})",
            text, re.IGNORECASE,
        )
        if m:
            try:
                return date(
                    int(m.group(3)),
                    _MONTH_IT[m.group(2).lower()],
                    int(m.group(1)),
                ).isoformat()
            except (ValueError, KeyError):
                pass

        # Fallback ISO
        m_iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
        if m_iso:
            return m_iso.group(0)

        return None

    def _extract_bands(self, soup) -> dict[str, dict]:
        """
        Estrae le 4 fasce dalla <table> della pagina.

        Colonne (CHF/100kWh):
          0=fascia, 1=rete, 2=fornitura, 3=tiacqua, 4=tasse, 5=totale

        Mapping:
          grid_price     = col1 / 100
          energy_price   = (col2 + col3) / 100
          residual_price = col4 / 100
        """
        bands: dict[str, dict] = {}
        table = soup.find("table")

        if not table:
            self._log.warning("AIL: nessuna <table> trovata nella pagina")
            return bands

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 5:
                continue

            label = cells[0].get_text(separator=" ", strip=True).lower()
            band  = _match_band(label)
            if not band:
                continue  # header o riga non riconoscibile

            col1 = _parse_float(cells[1].get_text(strip=True))  # rete
            col2 = _parse_float(cells[2].get_text(strip=True))  # fornitura
            col3 = _parse_float(cells[3].get_text(strip=True))  # tiacqua
            col4 = _parse_float(cells[4].get_text(strip=True))  # tasse

            if col1 is None or col2 is None:
                self._log.warning(f"AIL: valori principali mancanti per fascia {band!r}")
                continue

            grid     = round(col1 / 100.0, 8)
            energy   = round(((col2 or 0.0) + (col3 or 0.0)) / 100.0, 8)
            residual = round((col4 or 0.0) / 100.0, 8) if col4 is not None else None

            bands[band] = {"grid": grid, "energy": energy, "residual": residual}
            self._log.debug(
                f"AIL: {band:10s} grid={grid:.4f} energy={energy:.4f} residual={residual} CHF/kWh"
            )

        return bands


# ── Funzioni modulo ────────────────────────────────────────────────────────────

def _hour_to_band(hour: int) -> str:
    """Mappa ora locale CH (0-23) -> fascia AIL."""
    if hour < 6 or hour >= 22:
        return "notturna"
    if hour < 10:
        return "mattutina"
    if hour < 17:
        return "solare"
    return "serale"


def _match_band(label: str) -> Optional[str]:
    """Riconosce il nome della fascia dal testo della cella. None se header/sconosciuto."""
    for band, keywords in _BAND_KEYWORDS.items():
        for kw in keywords:
            if kw in label:
                return band
    return None


def _parse_float(text: str) -> Optional[float]:
    """Parsa numero da testo (gestisce virgola). None se non trovato."""
    text = text.strip().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None