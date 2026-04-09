"""
adapters/groupe_e.py
====================
Adapter per Groupe E — tariffario VARIO.

Endpoint: https://api.tariffs.groupe-e.ch/v2/tariffs
Accesso: pubblico, nessuna autenticazione

Formato risposta verificato (2026-04-09):
  {
    "publication_timestamp": "2026-04-08T14:48:23+02:00",
    "prices": [
      {
        "start_timestamp": "2026-04-09T00:00:00+02:00",
        "end_timestamp":   "2026-04-09T00:15:00+02:00",
        "grid":       [{"unit": "CHF_kWh", "value": 0.102}],
        "integrated": [{"unit": "CHF_kWh", "value": 0.2215}]
      },
      ...  (96 slot, prezzi variano quasi ogni slot)
    ]
  }

Caratteristiche:
  - Solo componente rete è dinamica ("Nur der Netznutzungstarif ist dynamisch")
  - electricity e regional_fees NON presenti → energy_price e residual_price = None
  - Prezzi possono essere NEGATIVI (surplus rinnovabile)
  - 87+ valori distinti su 96 slot — vera granularità 15min
  - Timestamp standard +02:00 (nessun fix necessario)
  - publication_timestamp giornaliero → day-ahead vero
  - ?date= ignorato silenziosamente → backfill_supported: false

Mapping → PriceSlot:
  grid[0].value       → grid_price      (dinamico, può essere negativo)
  energy_price        → None            (non presente in questa API)
  residual_price      → None            (non presente in questa API)
  integrated[0].value → fallback se grid manca

Note sul prezzo negativo:
  Un grid_price negativo è fisicamente corretto — in ore di alta produzione
  rinnovabile e bassa domanda, la rete "paga" i consumatori per assorbire
  l'eccesso. Il sistema lo gestisce senza problemi (salvato come float negativo).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from urllib.parse import urlencode

from adapters.base import AdapterEmptyError, AdapterParseError, BaseAdapter
from schemas import NormalizedPrices, PriceSlot, make_day_range_utc, utc_now


class GroupeEAdapter(BaseAdapter):
    """
    Adapter per Groupe E — VARIO.

    Config in evu_list.json:
      api_base_url  → "https://api.tariffs.groupe-e.ch/v2/tariffs"
      auth_type     → "none"
      api_params:
        backfill_supported → false (API day-ahead only, ignora ?date=)
    """

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        L'API Groupe E ignora ?date= — restituisce sempre il giorno ahead.
        Chiamiamo l'endpoint direttamente senza parametri.
        """
        return self.base_url

    async def fetch(self, target_date: date) -> NormalizedPrices:
        """
        Override fetch: usa actual_date degli slot come source_date.
        Groupe E restituisce sempre domani (day-ahead vero).
        """
        start_utc, end_utc = make_day_range_utc(target_date)
        url        = self._build_url(start_utc, end_utc)
        headers    = self._build_headers()
        fetched_at = utc_now()

        self._log.info(f"Fetch {target_date} → {url}...")

        raw_response  = await self._http_get_with_retry(url, headers)
        response_data = self._preprocess_response(raw_response)
        slots         = self._parse(response_data, target_date)

        if not slots:
            raise AdapterEmptyError(f"{self.tariff_id}: nessun slot ricevuto")

        try:
            import zoneinfo
            tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            tz_ch = pytz.timezone("Europe/Zurich")

        actual_date = slots[0].slot_start_utc.astimezone(tz_ch).date()
        if actual_date != target_date:
            self._log.info(
                f"Groupe E: API ha restituito {actual_date} "
                f"(richiesto {target_date}) — normale per API day-ahead."
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
            result.warnings.append(
                f"Slot count inatteso: {result.slot_count} (attesi ~96)"
            )
            self._log.warning(result.warnings[-1])

        self._log.info(result.summary())
        return result

    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta Groupe E.

        Struttura identica a CKW/EKZ ma con solo 'grid' e 'integrated'.
        energy_price e residual_price sono None — solo la rete è dinamica.
        """
        if isinstance(response_data, dict):
            raw_prices = response_data.get("prices")
            if raw_prices is None:
                raise AdapterParseError(
                    f"Groupe E ({self.tariff_id}): chiave 'prices' mancante. "
                    f"Chiavi: {list(response_data.keys())}"
                )
        elif isinstance(response_data, list):
            raw_prices = response_data
        else:
            raise AdapterParseError(
                f"Groupe E ({self.tariff_id}): tipo risposta inatteso "
                f"{type(response_data).__name__}"
            )

        if not raw_prices:
            raise AdapterEmptyError(
                f"Groupe E ({self.tariff_id}): 'prices' vuota per {target_date}."
            )

        slots: list[PriceSlot] = []
        for item in raw_prices:
            try:
                slot = self._parse_item(item)
                if slot is not None:
                    slots.append(slot)
            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(
                    f"Groupe E ({self.tariff_id}): item non parsabile: {e}"
                )

        if not slots:
            raise AdapterEmptyError(
                f"Groupe E ({self.tariff_id}): 0 slot parsabili da "
                f"{len(raw_prices)} item."
            )

        return sorted(slots, key=lambda s: s.slot_start_utc)

    def _parse_item(self, item: dict) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot Groupe E.

        Solo grid è presente — energy e residual sono None.
        I prezzi possono essere negativi (surplus rinnovabile): corretto.
        """
        start_raw = item.get("start_timestamp")
        if not start_raw:
            return None

        start_utc  = self.parse_utc_datetime(str(start_raw))
        grid_price = self._extract_chf(item.get("grid", []))
        integrated = self._extract_chf(item.get("integrated", []))

        # Fallback: usa integrated se grid manca
        if grid_price is None and integrated is not None:
            grid_price = integrated
            self._log.debug(
                f"Groupe E: slot {start_utc.isoformat()} — "
                "usato 'integrated' come fallback per grid_price"
            )

        if grid_price is None:
            return None

        return PriceSlot(
            slot_start_utc = start_utc,
            energy_price   = None,   # non disponibile in questa API
            grid_price     = grid_price,
            residual_price = None,   # non disponibile in questa API
        )

    @staticmethod
    def _extract_chf(entries: list) -> Optional[float]:
        """
        Estrae CHF/kWh dal primo elemento della lista.
        Accetta valori negativi (prezzi spot negativi sono validi).

        Formato: [{"unit": "CHF_kWh", "value": 0.102}]
        """
        if not entries or not isinstance(entries, list):
            return None
        entry = entries[0]
        if not isinstance(entry, dict):
            return None
        value = entry.get("value")
        if value is None:
            return None
        unit  = str(entry.get("unit", "")).upper()
        price = float(value)
        if "RP" in unit or "RAPPEN" in unit:
            return round(price / 100.0, 8)
        return price  # accetta negativi senza filtro

    def _preprocess_response(self, raw: bytes) -> dict | list:
        """Decodifica JSON con supporto BOM."""
        import json
        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1", errors="replace")
        try:
            return json.loads(decoded)
        except json.JSONDecodeError as e:
            raise AdapterParseError(
                f"Groupe E ({self.tariff_id}): risposta non è JSON — {e}. "
                f"Preview: {decoded[:200]!r}"
            )