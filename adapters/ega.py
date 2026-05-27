"""
adapters/ega.py
===============
Adapter per Elektra Aettenschwil EGA — Einheitstarif mit dynamischem Netznutzungstarif.

Piattaforma: Swisspower ESIT (esit.code-fabrik.ch)
Documentazione: https://esit.code-fabrik.ch/doc_v1#tag/tariffs/GET/tariff_name

Autenticazione: customer-specific
  - X-Messpunkt: <messpunkt_nummer>   (numero punto di misura del cliente)
  - Authorization: Bearer <auth_token>

Endpoint pubblico tariffe:
  GET {api_base_url}/{tariff_name}
  Ritorna i prezzi del giorno corrente e/o successivo in slot da 15 min.

Struttura risposta ESIT (dedotta da doc + pattern Swisspower):
  [
    {
      "start":      "2026-05-18T22:00:00Z",   // UTC ISO 8601
      "end":        "2026-05-18T22:15:00Z",
      "gridPrice":  0.0842,                    // CHF/kWh — DINAMICO
      "energyPrice": 0.1180,                   // CHF/kWh — fisso (o null)
      "taxes":      0.0328                     // CHF/kWh — fisso (o null)
    },
    ...
  ]

Nota: solo "grid" è dinamico per EGA (dynamic_elements: ["grid"] in evu_list.json).
energyPrice e taxes possono essere presenti come valori fissi già inclusi nella risposta
(comportamento identico ad AEM) oppure assenti → in quel caso li impostiamo a None.

Mapping → PriceSlot:
  grid_price     ← gridPrice     (dinamico per slot)
  energy_price   ← energyPrice   (fisso, può essere None)
  residual_price ← taxes         (fisso, può essere None)

Config in evu_list.json:
  api_base_url          → es. "https://esit.code-fabrik.ch/api/v1"
  auth_type             → "messpunkt_token"
  auth_config:
    messpunkt_nummer    → numero punto di misura (stringa, es. "CH000123456789")
    auth_token          → Bearer token fornito da EGA/ESIT
  api_params:
    tariff_name         → nome tariffa da appendere all'URL (es. "ega_einheitstarif")
    backfill_supported  → false (ESIT pubblica day-ahead, no storico)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from adapters.base import AdapterEmptyError, AdapterParseError, BaseAdapter
from schemas import NormalizedPrices, PriceSlot, make_day_range_utc, utc_now


class EsitAdapter(BaseAdapter):
    """
    Adapter per la piattaforma Swisspower ESIT.
    Usato attualmente da: EGA (Elektra Aettenschwil).

    La classe si chiama EsitAdapter (non EgaAdapter) perché la stessa
    piattaforma potrebbe essere usata da altri EVU in futuro.
    """

    # ── URL ───────────────────────────────────────────────────────────────────

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        Costruisce l'URL: {api_base_url}/{tariff_name}

        Il nome tariffa è in api_params.tariff_name oppure si usa
        il tariff_id come fallback.
        """
        api_params   = self.config.get("api_params", {})
        tariff_name  = api_params.get("tariff_name") or self.tariff_id
        base         = self.base_url.rstrip("/")
        return f"{base}/{tariff_name}"

    # ── Headers ───────────────────────────────────────────────────────────────
    # BaseAdapter gestisce già messpunkt_token:
    #   X-Messpunkt: auth_config.messpunkt_nummer
    #   Authorization: Bearer auth_config.auth_token
    # Nessun override necessario.

    # ── fetch() override — corregge source_date ───────────────────────────────

    async def fetch(self, target_date: date) -> NormalizedPrices:
        """
        Override del fetch base.

        ESIT è day-ahead: pubblica i prezzi del giorno successivo.
        Usiamo la data reale degli slot come source_date (pattern AEM).
        """
        try:
            import zoneinfo
            tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            tz_ch = pytz.timezone("Europe/Zurich")

        start_utc, end_utc = make_day_range_utc(target_date)
        url        = self._build_url(start_utc, end_utc)
        headers    = self._build_headers()
        fetched_at = utc_now()

        self._log.info(f"[ESIT/EGA] Fetch {target_date} → {url}")

        raw_response  = await self._http_get_with_retry(url, headers)
        response_data = self._preprocess_response(raw_response)
        slots         = self._parse(response_data, target_date)

        if not slots:
            raise AdapterEmptyError(
                f"{self.tariff_id}: nessun slot ricevuto per {target_date}"
            )

        # Usa la data reale degli slot
        actual_date = slots[0].slot_start_utc.astimezone(tz_ch).date()
        if actual_date != target_date:
            self._log.info(
                f"[ESIT/EGA] API ha restituito {actual_date} "
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

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta ESIT.

        Formati supportati:
          1. Lista diretta di slot: [{start, end, gridPrice, ...}, ...]
          2. Oggetto con chiave "data" o "prices" contenente la lista
          3. Oggetto con chiave "tariff" → "prices"

        Se gridPrice manca ma c'è "price" o "value", proviamo quello.
        """
        # Normalizza a lista
        items = self._extract_items(response_data)

        if not items:
            raise AdapterEmptyError(
                f"{self.tariff_id}: lista slot vuota nella risposta ESIT. "
                f"L'API pubblica i prezzi dopo le 17:00 UTC."
            )

        slots: list[PriceSlot] = []
        for item in items:
            try:
                slot = self._parse_item(item)
                if slot is not None:
                    slots.append(slot)
            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(
                    f"[ESIT/EGA] item non parsabile: {e} — {item}"
                )

        if not slots:
            raise AdapterEmptyError(
                f"{self.tariff_id}: 0 slot parsabili da {len(items)} item."
            )

        self._log.info(f"[ESIT/EGA] Parsati {len(slots)} slot.")
        return sorted(slots, key=lambda s: s.slot_start_utc)

    def _extract_items(self, data: dict | list) -> list:
        """Normalizza la risposta a lista di item slot, gestisce vari wrapper."""
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            # Prova chiavi comuni
            for key in ("data", "prices", "slots", "tariffSlots",
                        "quarterhourlyPrices", "quarterHourlyPrices",
                        "intervals", "values"):
                if key in data and isinstance(data[key], list):
                    self._log.debug(f"[ESIT/EGA] Estratti item da chiave '{key}'")
                    return data[key]

            # Prova wrapper tariff → prices
            if "tariff" in data and isinstance(data["tariff"], dict):
                inner = data["tariff"]
                for key in ("prices", "data", "slots"):
                    if key in inner and isinstance(inner[key], list):
                        return inner[key]

        self._log.warning(
            f"[ESIT/EGA] Struttura risposta non riconosciuta. "
            f"Tipo: {type(data).__name__}. "
            f"Chiavi: {list(data.keys()) if isinstance(data, dict) else 'N/A'}"
        )
        return []

    def _parse_item(self, item: dict) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot ESIT.

        Campi attesi (case-insensitive mapping gestito):
          start / startTime / from / timestamp → datetime UTC
          gridPrice / grid / netznutzung / networkPrice → float CHF/kWh
          energyPrice / energy / energiepreis → float CHF/kWh (opzionale)
          taxes / tax / abgaben / residual → float CHF/kWh (opzionale)
        """
        if not isinstance(item, dict):
            return None

        # ── Timestamp ─────────────────────────────────────────────────────────
        start_raw = (
            item.get("start") or item.get("startTime") or
            item.get("from")  or item.get("timestamp") or
            item.get("Start") or item.get("StartTime")
        )
        if not start_raw:
            self._log.warning(f"[ESIT/EGA] Slot senza campo start: {item}")
            return None

        start_utc = self.parse_utc_datetime(str(start_raw))

        # ── Grid price (dinamico) ─────────────────────────────────────────────
        grid_price = self._pick_float(item, [
            "gridPrice", "grid", "GridPrice", "Grid",
            "netznutzung", "Netznutzung", "networkPrice",
            "netPrice", "net_price", "price", "value",
        ])

        # ── Energy price (fisso, opzionale) ───────────────────────────────────
        energy_price = self._pick_float(item, [
            "energyPrice", "energy", "EnergyPrice", "Energy",
            "energiepreis", "Energiepreis", "energy_price",
        ])

        # ── Taxes / residual (fisso, opzionale) ───────────────────────────────
        residual_price = self._pick_float(item, [
            "taxes", "tax", "Taxes", "Tax",
            "abgaben", "Abgaben", "residual",
            "fees", "surcharges",
        ])

        # Almeno grid_price deve essere presente
        if grid_price is None:
            self._log.debug(
                f"[ESIT/EGA] Slot {start_utc.isoformat()} senza gridPrice — skip. "
                f"Campi disponibili: {list(item.keys())}"
            )
            return None

        return PriceSlot(
            slot_start_utc = start_utc,
            energy_price   = energy_price,
            grid_price     = grid_price,
            residual_price = residual_price,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_float(item: dict, keys: list[str]) -> Optional[float]:
        """
        Tenta di estrarre un float da un dict provando chiavi in ordine.
        Gestisce valori in Rappen (> 5.0 è quasi certamente Rappen, non CHF/kWh).
        """
        for k in keys:
            v = item.get(k)
            if v is None:
                continue
            try:
                f = float(v)
                # Heuristica: se il valore > 5.0 probabilmente è in Rp/kWh
                if f > 5.0:
                    return round(f / 100.0, 8)
                return f
            except (TypeError, ValueError):
                continue
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
                f"{self.tariff_id}: risposta non è JSON valido — {e}. "
                f"Preview: {decoded[:300]!r}"
            )