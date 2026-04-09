"""
adapters/ekz.py
===============
Adapter per EKZ — EKZ Energie Dynamisch + EKZ Netz 400D.

Endpoint: https://api.tariffs.ekz.ch/v1/tariffs
Accesso: pubblico (NoSecurityScheme) per /v1/tariffs

Formato risposta verificato (2026-04-09):
  {
    "publication_timestamp": "2025-12-18T14:21:02+01:00",
    "prices": [
      {
        "start_timestamp": "2026-04-09T00:00:00+02:00",
        "end_timestamp":   "2026-04-09T00:15:00+02:00",
        "electricity": [
          {"unit": "CHF_m",   "value": 3.0},    ← canone mensile fisso → IGNORATO
          {"unit": "CHF_kWh", "value": 0.09}     ← prezzo energia dinamico → energy_price
        ],
        "grid": [
          {"unit": "CHF_m",   "value": 0.0},     ← canone mensile → IGNORATO
          {"unit": "CHF_kWh", "value": 0.1098}   ← prezzo rete dinamico → grid_price
        ],
        "integrated": [
          {"unit": "CHF_m",   "value": 3.0},
          {"unit": "CHF_kWh", "value": 0.1998}   ← fallback
        ],
        "metering": [
          {"unit": "CHF_m",   "value": 5.0}      ← solo mensile → IGNORATO
        ],
        "regional_fees": [
          {"unit": "CHF_m",   "value": 0.0},
          {"unit": "CHF_kWh", "value": 0.0016}   ← tasse regionali → residual_price
        ]
      },
      ...  (96 slot)
    ]
  }

Mapping → PriceSlot:
  electricity[CHF_kWh] → energy_price    (EKZ Energie Dynamisch — spot market)
  grid[CHF_kWh]        → grid_price      (EKZ Netz 400D — network load)
  regional_fees[CHF_kWh] → residual_price (Zuschläge/Abgaben)
  integrated[CHF_kWh]  → fallback se mancano electricity e grid

Nota importante:
  Ogni campo ha due entry: CHF_m (canone mensile fisso) e CHF_kWh (prezzo per kWh).
  Prendiamo SOLO CHF_kWh — il CHF_m non è comparabile con i prezzi per kWh.

Caratteristiche API:
  - Stessa struttura di CKW (stesso fornitore tecnologico?)
  - publication_timestamp: "2025-12-18" → prezzi probabilmente fissi per il 2026
    (o l'API ignora il parametro data e restituisce sempre gli stessi dati)
  - Verifica: testare con ?date= per capire se supporta storico
  - Timestamp in ora locale CH (+01:00/+02:00)
  - 96 slot da 15 min per giorno normale
  - Day-ahead: backfill_supported da verificare

Config in evu_list.json (api_params):
  backfill_supported → true/false (da verificare con test date storiche)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from adapters.base import AdapterEmptyError, AdapterParseError, BaseAdapter
from schemas import NormalizedPrices, PriceSlot, make_day_range_utc, utc_now


class EkzAdapter(BaseAdapter):
    """
    Adapter per EKZ — EKZ Energie Dynamisch + EKZ Netz 400D.

    Config in evu_list.json:
      api_base_url  → "https://api.tariffs.ekz.ch"
      auth_type     → "none"
      api_params:
        backfill_supported → true se l'API supporta date storiche, false altrimenti
    """

    # ── Costruzione URL ───────────────────────────────────────────────────────

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        Costruisce l'URL per /v1/tariffs.

        Prova a passare la data target — se l'API la ignora (come AEM),
        il fetch() override correggerà source_date con la data reale degli slot.
        """
        try:
            import zoneinfo
            tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            tz_ch = pytz.timezone("Europe/Zurich")

        target_date_local = start_utc.astimezone(tz_ch).date()

        api_params = self.config.get("api_params", {})

        # Prova il parametro data — se l'API lo ignora, viene gestito in fetch()
        if not api_params.get("skip_date_param", False):
            params = {"date": target_date_local.isoformat()}
            return f"{self.base_url}/v1/tariffs?{urlencode(params)}"

        return f"{self.base_url}/v1/tariffs"

    # ── fetch() override — corregge source_date se API ignora ?date= ──────────

    async def fetch(self, target_date: date) -> NormalizedPrices:
        """
        Override del fetch base.
        Usa la data reale degli slot come source_date (non target_date),
        per gestire correttamente sia API con storico che API day-ahead only.
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
                f"EKZ ({self.tariff_id}): API ha restituito {actual_date} "
                f"(richiesto {target_date}) — API day-ahead only."
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

    # ── Parsing risposta ──────────────────────────────────────────────────────

    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta EKZ.

        Struttura identica a CKW: {"prices": [...96 slot...]}
        Differenza: ogni componente ha due entry (CHF_m e CHF_kWh).
        Prendiamo sempre e solo CHF_kWh.
        """
        if isinstance(response_data, dict):
            raw_prices = response_data.get("prices")
            if raw_prices is None:
                if "status" in response_data or "body" in response_data:
                    raise AdapterEmptyError(
                        f"EKZ ({self.tariff_id}): prezzi non ancora pubblicati."
                    )
                raise AdapterParseError(
                    f"EKZ ({self.tariff_id}): chiave 'prices' mancante. "
                    f"Chiavi: {list(response_data.keys())}"
                )
        elif isinstance(response_data, list):
            raw_prices = response_data
        else:
            raise AdapterParseError(
                f"EKZ ({self.tariff_id}): tipo risposta inatteso "
                f"{type(response_data).__name__}"
            )

        if not raw_prices:
            raise AdapterEmptyError(
                f"EKZ ({self.tariff_id}): 'prices' vuota per {target_date}."
            )

        slots: list[PriceSlot] = []
        for item in raw_prices:
            try:
                slot = self._parse_item(item)
                if slot is not None:
                    slots.append(slot)
            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(f"EKZ ({self.tariff_id}): item non parsabile: {e}")

        if not slots:
            raise AdapterEmptyError(
                f"EKZ ({self.tariff_id}): 0 slot parsabili da {len(raw_prices)} item."
            )

        return sorted(slots, key=lambda s: s.slot_start_utc)

    def _parse_item(self, item: dict) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot EKZ.

        Ogni componente è una lista con entry CHF_m e CHF_kWh.
        Usiamo _extract_chf_kwh() che prende SOLO CHF_kWh.

        Mapping:
          electricity[CHF_kWh] → energy_price
          grid[CHF_kWh]        → grid_price
          regional_fees[CHF_kWh] → residual_price
          integrated[CHF_kWh]  → fallback
        """
        start_raw = item.get("start_timestamp")
        if not start_raw:
            return None

        start_utc      = self.parse_utc_datetime(str(start_raw))
        energy_price   = self._extract_chf_kwh(item.get("electricity", []))
        grid_price     = self._extract_chf_kwh(item.get("grid", []))
        residual_price = self._extract_chf_kwh(item.get("regional_fees", []))
        integrated     = self._extract_chf_kwh(item.get("integrated", []))

        # Fallback: usa integrated se mancano electricity e grid
        if grid_price is None and energy_price is None and integrated is not None:
            grid_price = integrated
            self._log.debug(
                f"EKZ ({self.tariff_id}): slot {start_utc.isoformat()} — "
                "usato 'integrated' come fallback"
            )

        if all(v is None for v in (energy_price, grid_price, residual_price)):
            return None

        return PriceSlot(
            slot_start_utc=start_utc,
            energy_price=energy_price,
            grid_price=grid_price,
            residual_price=residual_price,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_chf_kwh(entries: list) -> Optional[float]:
        """
        Estrae il valore CHF/kWh da una lista di componenti EKZ.

        EKZ usa sempre due entry per campo:
          [{"unit": "CHF_m", "value": 3.0}, {"unit": "CHF_kWh", "value": 0.09}]

        Prendiamo SOLO CHF_kWh. CHF_m è un canone mensile non comparabile.
        """
        if not entries or not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            unit = str(entry.get("unit", "")).upper()
            if "KWH" not in unit:
                continue  # salta CHF_m, CHF_kW_t, ecc.
            value = entry.get("value")
            if value is None:
                continue
            price = float(value)
            if "RP" in unit or "RAPPEN" in unit:
                return round(price / 100.0, 8)
            return price
        return None

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
                f"EKZ ({self.tariff_id}): risposta non è JSON — {e}. "
                f"Preview: {decoded[:200]!r}"
            )