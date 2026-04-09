"""
adapters/aem.py
===============
Adapter per Azienda Elettrica di Massagno (AEM) SA — Tariffa Dinamica.

Endpoint: https://servizi.aemsa.ch/tariffe
Accesso: pubblico, nessuna autenticazione

Formato risposta:
  {
    "publication_timestamp": "2026-04-03T18:00:00+02:00",
    "fixed_prices": {
      "grid": [
        {"name": "Potenza",            "unit": "CHF_kW_t", "value": 2.0},
        {"name": "Tassa base",         "unit": "CHF_m",    "value": 2.6},
        {"name": "Tassa di misurazione","unit": "CHF_m",   "value": 4.4}
      ],
      "electricity": [
        {"name": "Energia",  "unit": "CHF_kWh", "value": 0.118},
        {"name": "tiacqua",  "unit": "CHF_kWh", "value": 0.006}
      ],
      "taxes": [
        {"name": "SDL",                          "unit": "CHF_kWh", "value": 0.0027},
        {"name": "RIC",                          "unit": "CHF_kWh", "value": 0.022},
        {"name": "Risanamento forza idrica",     "unit": "CHF_kWh", "value": 0.001},
        {"name": "Utilizzo demanio pubblico",    "unit": "CHF_kWh", "value": 0.0102},
        {"name": "FER",                          "unit": "CHF_kWh", "value": 0.012},
        {"name": "Riserva energetica",           "unit": "CHF_kWh", "value": 0.0041},
        {"name": "Supplemento per costi solidali","unit": "CHF_kWh","value": 0.0005}
      ]
    },
    "dynamic_prices": [
      {
        "start_timestamp": "2026-04-04T00:00:00+02:00",
        "end_timestamp":   "2026-04-04T00:15:00+02:00",
        "grid":  [{"name": "Trasporto", "unit": "CHF_kWh", "value": 0.0845}],
        "total": [{"name": "Totale IVA esclusa", "unit": "CHF_kWh", "value": 0.261}]
      },
      ...
    ]
  }

Mapping → PriceSlot (stesso standard di CKW e Primeo):

  grid_price     ← dynamic_prices[].grid[0].value  (Trasporto — DINAMICO per slot)
  energy_price   ← fixed_prices.electricity somma  (Energia + tiacqua — FISSO)
  residual_price ← fixed_prices.taxes somma         (SDL+RIC+FER+... — FISSO)

Perché energy_price e residual_price sono fissi ma li salviamo per slot:
  Il nostro schema richiede i tre componenti per ogni slot.
  Avere valori fissi per energy e residual è normale (CKW e Primeo fanno lo stesso).
  Il DB li salva, la dashboard li mostra. Nessun problema.

Perché NON usiamo "total":
  total = grid + electricity + taxes (già calcolato dall'API).
  Lo salveremmo in doppio. Il nostro sistema calcola il totale da solo
  sommando i tre componenti. Lo usiamo solo come fallback se manca grid.

Caratteristiche API:
  - Day-ahead: restituisce sempre i dati del giorno successivo
    (a differenza di Primeo che restituisce oggi)
  - Nessun parametro data accettato
  - Nessuna autenticazione
  - Timestamp in ora locale CH (+01:00/+02:00)
  - 96 slot da 15 min per giorno normale
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from adapters.base import AdapterEmptyError, AdapterParseError, BaseAdapter
from schemas import NormalizedPrices, PriceSlot, make_day_range_utc, utc_now


class AemAdapter(BaseAdapter):
    """
    Adapter per AEM — Azienda Elettrica di Massagno SA.

    Config in evu_list.json:
      api_base_url  → "https://servizi.aemsa.ch/tariffe"
      auth_type     → "none"
      api_params:
        backfill_supported → false (API day-ahead only)
    """

    # ── Costruzione URL ───────────────────────────────────────────────────────

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        L'API AEM non accetta parametri — restituisce sempre il giorno corrente.
        """
        return self.base_url

    # ── fetch() override — corregge source_date ───────────────────────────────

    async def fetch(self, target_date: date) -> NormalizedPrices:
        """
        Override del fetch base.

        AEM restituisce i dati del giorno SUCCESSIVO rispetto a oggi
        (day-ahead vero). Quindi se oggi è il 3 aprile, restituisce il 4 aprile.
        Usiamo la data reale degli slot (actual_date) come source_date,
        non target_date, per garantire coerenza nel DB.
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
            raise AdapterEmptyError(
                f"{self.tariff_id}: nessun slot ricevuto"
            )

        # Usa la data reale degli slot, non target_date
        try:
            import zoneinfo
            tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            tz_ch = pytz.timezone("Europe/Zurich")

        actual_date = slots[0].slot_start_utc.astimezone(tz_ch).date()
        if actual_date != target_date:
            self._log.info(
                f"AEM ({self.tariff_id}): API ha restituito {actual_date} "
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

    # ── Parsing risposta ──────────────────────────────────────────────────────

    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta AEM.

        Strategia:
          1. Legge fixed_prices una volta → energy_price e residual_price fissi
          2. Itera dynamic_prices → grid_price dinamico per slot
          3. Combina: ogni slot ha grid dinamico + energy fisso + residual fisso
        """
        if not isinstance(response_data, dict):
            raise AdapterParseError(
                f"AEM ({self.tariff_id}): tipo risposta inatteso "
                f"{type(response_data).__name__} — atteso dict"
            )

        # ── 1. Leggi prezzi fissi ─────────────────────────────────────────────
        fixed = response_data.get("fixed_prices", {})
        energy_price   = self._sum_chf_kwh(fixed.get("electricity", []))
        residual_price = self._sum_chf_kwh(fixed.get("taxes", []))

        self._log.debug(
            f"AEM: fixed prices — "
            f"energy={energy_price} CHF/kWh, residual={residual_price} CHF/kWh"
        )

        # ── 2. Leggi slot dinamici ────────────────────────────────────────────
        dynamic_prices = response_data.get("dynamic_prices")
        if dynamic_prices is None:
            raise AdapterParseError(
                f"AEM ({self.tariff_id}): chiave 'dynamic_prices' mancante. "
                f"Chiavi presenti: {list(response_data.keys())}"
            )

        if not dynamic_prices:
            raise AdapterEmptyError(
                f"AEM ({self.tariff_id}): lista 'dynamic_prices' vuota. "
                f"L'API è day-ahead: pubblica dopo le 18:00 ora locale CH."
            )

        # ── 3. Combina slot ───────────────────────────────────────────────────
        slots: list[PriceSlot] = []

        for item in dynamic_prices:
            try:
                slot = self._parse_item(item, energy_price, residual_price)
                if slot is not None:
                    slots.append(slot)
            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(
                    f"AEM ({self.tariff_id}): item non parsabile: {e} — {item}"
                )

        if not slots:
            raise AdapterEmptyError(
                f"AEM ({self.tariff_id}): 0 slot parsabili da "
                f"{len(dynamic_prices)} item."
            )

        return sorted(slots, key=lambda s: s.slot_start_utc)

    def _parse_item(
        self,
        item: dict,
        energy_price: Optional[float],
        residual_price: Optional[float],
    ) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot dinamico AEM.

        grid_price     → dynamic_prices[].grid[0].value  (Trasporto — dinamico)
        energy_price   → passato da fixed_prices (fisso per tutti gli slot)
        residual_price → passato da fixed_prices (fisso per tutti gli slot)

        Fallback: se grid manca, usa total - energy - residual (se tutti presenti).
        """
        start_raw = item.get("start_timestamp")
        if not start_raw:
            return None

        start_utc = self.parse_utc_datetime(str(start_raw))

        # Componente dinamica: grid = Trasporto
        grid_price = self._extract_first_chf(item.get("grid", []))

        # Fallback: se grid manca ma abbiamo total, lo usiamo come grid_price
        # (non è perfetto ma meglio di niente)
        if grid_price is None:
            total = self._extract_first_chf(item.get("total", []))
            if total is not None:
                grid_price = total
                self._log.debug(
                    f"AEM: slot {start_utc.isoformat()} — "
                    "usato 'total' come fallback per grid_price (grid mancante)"
                )

        if all(v is None for v in (grid_price, energy_price, residual_price)):
            return None

        return PriceSlot(
            slot_start_utc=start_utc,
            energy_price=energy_price,
            grid_price=grid_price,
            residual_price=residual_price,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _sum_chf_kwh(items: list) -> Optional[float]:
        """
        Somma tutti i valori CHF/kWh da una lista di componenti fixed_prices.

        Ignora le voci con unità diverse da CHF_kWh (es. CHF_kW_t, CHF_m)
        perché non sono comparabili con i prezzi per kWh.

        Esempio:
          electricity: [
            {"name": "Energia",  "unit": "CHF_kWh", "value": 0.118},  ← incluso
            {"name": "tiacqua",  "unit": "CHF_kWh", "value": 0.006},  ← incluso
          ]
          → somma = 0.124 CHF/kWh

          grid: [
            {"name": "Potenza",  "unit": "CHF_kW_t", "value": 2.0},   ← ESCLUSO
            {"name": "Tassa base","unit": "CHF_m",   "value": 2.6},   ← ESCLUSO
          ]
          → somma = None (nessuna voce CHF/kWh)
        """
        total = 0.0
        found = False
        for item in items:
            if not isinstance(item, dict):
                continue
            unit = str(item.get("unit", "")).upper()
            if "KWH" not in unit:
                continue  # salta CHF/kW, CHF/mese, ecc.
            value = item.get("value")
            if value is None:
                continue
            unit_str = unit
            price = float(value)
            if "RP" in unit_str or "RAPPEN" in unit_str:
                price = price / 100.0
            total += price
            found = True

        return round(total, 8) if found else None

    @staticmethod
    def _extract_first_chf(items: list) -> Optional[float]:
        """
        Estrae il valore CHF/kWh dal primo elemento di una lista dinamica.

        Formato: [{"name": "Trasporto", "unit": "CHF_kWh", "value": 0.0845}]
        """
        if not items or not isinstance(items, list):
            return None
        item = items[0]
        if not isinstance(item, dict):
            return None
        value = item.get("value")
        if value is None:
            return None
        unit = str(item.get("unit", "")).upper()
        price = float(value)
        if "RP" in unit or "RAPPEN" in unit:
            return round(price / 100.0, 8)
        return price

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
                f"AEM ({self.tariff_id}): risposta non è JSON — {e}. "
                f"Preview: {decoded[:200]!r}"
            )