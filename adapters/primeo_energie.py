"""
adapters/primeo_energie.py
==========================
Adapter per Primeo Energie — tariffario NetzDynamisch.

Usato da tre EVU che condividono la stessa piattaforma API:
  • Primeo Energie         (tariff_id: primeo_netzdynamisch)
  • Aare Versorgungs AG    (tariff_id: avag_primeo_netzdynamisch)
  • Elektra Gretzenbach AG (tariff_id: elag_primeo_netzdynamisch)

Endpoint: https://tarife.primeo-energie.ch/api/v1/tariffs

Formato risposta verificato (2026-03-30):
  {
    "publication_timestamp": "2026-03-29T17:30:07+02:00",
    "tariff_name": "NetzDynamisch",
    "prices": [
      {
        "start_timestamp": "2026-03-30T00:00:00+02:00",
        "end_timestamp":   "2026-03-30T00:15:00+02:00",
        "grid":       [{"unit": "CHF_kWh", "value": 0.0303}],
        "grid_usage": [{"unit": "CHF_kWh", "value": 0.0}],
        "electricity":[{"unit": "CHF_kWh", "value": 0.13}],
        "integrated": [{"unit": "CHF_kWh", "value": 0.1603}],
        "feed_in": null
      },
      ...
    ]
  }

Caratteristiche:
  - Timestamp in ora locale CH (+01:00/+02:00)
  - Risoluzione output: 15 min
  - Prezzi statici per ora intera (4 slot identici per ora)
  - 96 slot/giorno in condizioni normali
  - Pubblicazione entro le 18:00 del giorno precedente
  - Differenziazione geografica per Netzgebiet (opzionale via api_params)
  - API day-ahead only: senza storico → backfill_supported: false in evu_list.json

Mapping componenti → PriceSlot (conforme PDF T-Swiss):
  electricity → energy_price    (energia pura, fisso)
  grid        → grid_price      (tariffa di rete dinamica totale)
  grid_usage  → residual_price  (componente dinamica pura della rete)
  integrated  → fallback se i precedenti mancano

Note:
  grid = componente base fissa + grid_usage (dinamica)
  Nelle ore di bassa domanda grid_usage = 0 → grid = solo base rate
  Nelle ore di picco grid_usage > 0 → la tariffa sale
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from adapters.base import AdapterEmptyError, AdapterParseError, BaseAdapter
from schemas import PriceSlot


class PrimeoEnergieAdapter(BaseAdapter):
    """
    Adapter per Primeo Energie / AVAG / ELAG — NetzDynamisch.

    Campi letti da evu_list.json:
      api_base_url           → URL endpoint
      auth_type              → "none"
      api_params:
        date_format          → formato data da passare all'API (default: "%Y-%m-%d")
        date_param           → nome del parametro data (default: "date")
        netzgebiet           → codice Netzgebiet opzionale (es. "AVAG", "ELAG")
        skip_date_param      → true per chiamare l'API senza parametro data
        backfill_supported   → false (API day-ahead only — impostare sempre!)
    """

    # ── Costruzione URL ───────────────────────────────────────────────────────

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        Costruisce l'URL con il parametro ?date=YYYY-MM-DD.

        Primeo pubblica per giornata locale CH. La data da richiedere
        è il giorno corrispondente a start_utc in ora locale svizzera.

        Esempio (CEST +02:00):
          start_utc = 2026-03-30T22:00:00Z
          → in CH CEST (+02:00) è 2026-03-31T00:00
          → richiediamo ?date=2026-03-31
        """
        tz_ch = self._get_tz_ch()
        target_date_local = start_utc.astimezone(tz_ch).date()

        api_params = self.config.get("api_params", {})

        params: dict = {}

        # Parametro data
        if not api_params.get("skip_date_param", False):
            date_format = api_params.get("date_format", "%Y-%m-%d")
            date_param  = api_params.get("date_param", "date")
            params[date_param] = target_date_local.strftime(date_format)

        # Netzgebiet opzionale — differenzia AVAG / Primeo / ELAG
        netzgebiet = api_params.get("netzgebiet")
        if netzgebiet:
            params["netzgebiet"] = netzgebiet

        if params:
            return f"{self.base_url}?{urlencode(params)}"
        return self.base_url

    # ── Parsing risposta ──────────────────────────────────────────────────────

    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta Primeo e ritorna i PriceSlot per target_date.

        Struttura risposta:
          dict con chiave "prices": lista di slot da 15 min
          Ogni slot ha: start_timestamp, grid, grid_usage, electricity, integrated, feed_in

        Filtra per target_date in ora locale CH per escludere
        eventuali slot di bordo da altri giorni.
        """
        raw_prices = self._extract_prices_list(response_data, target_date)

        if not raw_prices:
            raise AdapterEmptyError(
                f"Primeo ({self.tariff_id}): lista 'prices' vuota per {target_date}. "
                f"L'API è day-ahead only: i dati sono disponibili solo dopo la "
                f"pubblicazione (entro le 18:00 ora locale del giorno precedente)."
            )

        tz_ch = self._get_tz_ch()
        slots: list[PriceSlot] = []
        skipped = 0

        for item in raw_prices:
            try:
                slot = self._parse_item(item)
                if slot is None:
                    continue

                # Filtra: tieni solo gli slot del giorno target in ora locale CH
                local_date = slot.slot_start_utc.astimezone(tz_ch).date()
                if local_date == target_date:
                    slots.append(slot)
                else:
                    skipped += 1
                    self._log.debug(
                        f"Primeo: slot {slot.slot_start_utc.isoformat()} scartato "
                        f"(data locale {local_date} ≠ target {target_date})"
                    )

            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(
                    f"Primeo ({self.tariff_id}): item non parsabile: {e} — {item}"
                )

        if skipped > 0:
            self._log.debug(
                f"Primeo ({self.tariff_id}): {skipped} slot di bordo scartati"
            )

        if not slots:
            raise AdapterEmptyError(
                f"Primeo ({self.tariff_id}): 0 slot validi da {len(raw_prices)} item "
                f"per {target_date}. "
                f"Verifica che la data richiesta corrisponda ai dati pubblicati dall'API."
            )

        return sorted(slots, key=lambda s: s.slot_start_utc)

    def _extract_prices_list(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list:
        """
        Estrae la lista 'prices' dalla risposta.
        Gestisce sia dict (formato normale) che list (formato alternativo).
        """
        if isinstance(response_data, dict):
            # Formato normale: {"publication_timestamp": ..., "prices": [...]}
            if "prices" in response_data:
                return response_data["prices"] or []

            # Risposta di errore con status/message
            if "status" in response_data or "message" in response_data or "error" in response_data:
                status  = response_data.get("status", "")
                message = response_data.get("message", response_data.get("error", ""))
                raise AdapterEmptyError(
                    f"Primeo ({self.tariff_id}): risposta di errore dall'API — "
                    f"status={status!r}, message={message!r}. "
                    f"Possibile causa: data fuori range (API day-ahead only)."
                )

            # Dict senza 'prices' né errori noti
            raise AdapterParseError(
                f"Primeo ({self.tariff_id}): chiave 'prices' mancante nella risposta. "
                f"Chiavi presenti: {list(response_data.keys())}"
            )

        elif isinstance(response_data, list):
            # L'API ha risposto direttamente con una lista di slot
            return response_data

        else:
            raise AdapterParseError(
                f"Primeo ({self.tariff_id}): tipo risposta inatteso {type(response_data).__name__}. "
                f"Atteso dict o list."
            )

    def _parse_item(self, item: dict) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot dalla risposta Primeo.

        Mapping:
          electricity → energy_price    (CHF/kWh, prezzo energia — spesso fisso)
          grid        → grid_price      (CHF/kWh, tariffa rete dinamica totale)
          grid_usage  → residual_price  (CHF/kWh, solo componente dinamica — può essere 0)
          integrated  → fallback combinato se gli altri mancano

        Nota su grid_usage = 0:
          Nelle ore di bassa domanda la componente dinamica è 0.
          Manteniamo il valore 0.0 esplicito — NON lo convertiamo in None.
          Lo scheduler/API lo gestisce correttamente.
        """
        start_raw = item.get("start_timestamp")
        if not start_raw:
            return None

        start_utc = self.parse_utc_datetime(str(start_raw))

        # Estrai tutti i componenti
        energy_price   = self._extract_chf(item, "electricity")
        grid_price     = self._extract_chf(item, "grid")
        residual_price = self._extract_chf(item, "grid_usage")
        integrated     = self._extract_chf(item, "integrated")

        # Fallback: se mancano grid e electricity ma abbiamo integrated
        if grid_price is None and energy_price is None and integrated is not None:
            grid_price = integrated
            self._log.debug(
                f"Primeo ({self.tariff_id}): slot {start_utc.isoformat()} — "
                "usato 'integrated' come fallback per grid_price"
            )

        # Slot inutile se non abbiamo nessun prezzo
        if all(v is None for v in (energy_price, grid_price, residual_price)):
            self._log.debug(
                f"Primeo ({self.tariff_id}): slot {start_utc.isoformat()} scartato — "
                "nessun valore di prezzo disponibile"
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
    def _extract_chf(item: dict, key: str) -> Optional[float]:
        """
        Estrae il valore CHF/kWh da un campo della risposta Primeo.

        Formato atteso: [{"unit": "CHF_kWh", "value": 0.0303}]
        Gestisce anche:
          - Valori diretti (int/float)
          - Liste vuote → None
          - feed_in null → None
          - Unità in Rappen (Rp) → converte in CHF
        """
        raw = item.get(key)

        # Campo assente o null/None
        if raw is None:
            return None

        # Formato lista: [{"unit": "CHF_kWh", "value": 0.xx}]
        if isinstance(raw, list):
            if not raw:
                return None
            raw = raw[0]

        # Oggetto con unit + value
        if isinstance(raw, dict):
            value = raw.get("value")
            if value is None:
                return None
            unit  = str(raw.get("unit", "")).upper()
            price = float(value)
            # Conversione: se l'unità è Rappen → CHF
            if "RP" in unit or "RAPPEN" in unit:
                return round(price / 100.0, 8)
            return price  # CHF_kWh è già CHF

        # Valore numerico diretto
        if isinstance(raw, (int, float)):
            return float(raw)

        return None

    @staticmethod
    def _get_tz_ch():
        """Ritorna il timezone per Europe/Zurich (gestisce sia zoneinfo che pytz)."""
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            return pytz.timezone("Europe/Zurich")

    def _preprocess_response(self, raw: bytes) -> dict | list:
        """
        Decodifica la risposta JSON.
        Usa utf-8-sig per gestire eventuali BOM (come in CKW).
        Gestisce anche risposte non-JSON con messaggi di errore chiari.
        """
        import json

        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1", errors="replace")

        try:
            return json.loads(decoded)
        except json.JSONDecodeError as e:
            # Risposta non-JSON (es. HTML di errore, plaintext)
            preview = decoded[:200].strip()
            raise AdapterParseError(
                f"Primeo ({self.tariff_id}): risposta non è JSON valido — "
                f"JSONDecodeError: {e}. "
                f"Inizio risposta: {preview!r}"
            )