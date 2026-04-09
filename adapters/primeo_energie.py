"""
adapters/primeo_energie.py
==========================
Adapter per Primeo Energie — tariffario NetzDynamisch.

Usato da tre EVU che condividono la stessa piattaforma API:
  • Primeo Energie         (tariff_id: primeo_netzdynamisch)
  • Aare Versorgungs AG    (tariff_id: avag_primeo_netzdynamisch)
  • Elektra Gretzenbach AG (tariff_id: elag_primeo_netzdynamisch)

Endpoint:
  https://tarife.primeo-energie.ch/api/v1/tariffs

Filtro corretto:
  ?tariff_name=NetzDynamisch
  ?tariff_name=NetzDynamischAVAG
  ?tariff_name=NetzDynamischELAG

Formato risposta atteso:
  {
    "publication_timestamp": "2026-04-01T17:30:07+02:00",
    "tariff_name": "NetzDynamisch",
    "prices": [
      {
        "start_timestamp": "2026-04-02T00:00:00+02:00",
        "end_timestamp":   "2026-04-02T00:15:00+02:00",
        "grid":       [{"unit": "CHF_kWh", "value": 0.0603}],
        "grid_usage": [{"unit": "CHF_kWh", "value": 0.03}],
        "electricity":[{"unit": "CHF_kWh", "value": 0.13}],
        "integrated": [{"unit": "CHF_kWh", "value": 0.1903}],
        "feed_in": null
      },
      ...
    ]
  }

Caratteristiche:
  - Timestamp in ora locale CH (+01:00 / +02:00)
  - Risoluzione output: 15 min
  - Prezzi spesso identici per i 4 quarti d'ora della stessa ora
  - 96 slot/giorno in condizioni normali
  - Pubblicazione entro le 18:00 del giorno precedente
  - API day-ahead only: nessun backfill storico via query date
  - Distinzione Primeo / AVAG / ELAG via tariff_name

Mapping componenti → PriceSlot:
  electricity → energy_price
  grid        → grid_price
  grid_usage  → residual_price
  integrated  → fallback se mancano gli altri

Nota:
  grid = componente rete totale
  grid_usage = componente dinamica pura della rete
"""

from __future__ import annotations

from datetime import date, datetime
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
        tariff_name          → nome tariffa da passare all'API
                               ("NetzDynamisch", "NetzDynamischAVAG", "NetzDynamischELAG")
        backfill_supported   → false (API day-ahead only)
    """

    # ── Costruzione URL ───────────────────────────────────────────────────────

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        Costruisce l'URL usando il parametro corretto `tariff_name`.

        Esempi:
          ?tariff_name=NetzDynamisch
          ?tariff_name=NetzDynamischAVAG
          ?tariff_name=NetzDynamischELAG
        """
        api_params = self.config.get("api_params", {})
        params: dict = {}

        tariff_name = api_params.get("tariff_name")
        if tariff_name:
            params["tariff_name"] = tariff_name

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
        Parsa la risposta Primeo e ritorna i PriceSlot.

        NOTA IMPORTANTE sull'API Primeo:
        L'API day-ahead restituisce SEMPRE i dati del giorno corrente
        (non domani). Quindi non filtriamo per target_date: accettiamo
        tutti i 96 slot restituiti e aggiorniamo source_date di conseguenza.

        Esempio: fetch alle 09:40 del 2026-04-03 con target_date=2026-04-04
            → l'API restituisce slot 2026-04-03T00:00 … 2026-04-03T23:45
            → li accettiamo tutti, source_date = 2026-04-03

        Questo comportamento è corretto: i dati vengono salvati con la data
        reale degli slot (quella in UTC nel DB), non con target_date.
        Il sistema li troverà comunque perché la query sul DB usa slot_start_utc.
        """
        raw_prices = self._extract_prices_list(response_data)

        if not raw_prices:
            raise AdapterEmptyError(
                f"Primeo ({self.tariff_id}): lista 'prices' vuota. "
                f"L'API è day-ahead e pubblica dopo le 18:00 ora locale CH."
            )

        slots: list[PriceSlot] = []

        for item in raw_prices:
            try:
                slot = self._parse_item(item)
                if slot is not None:
                    slots.append(slot)
            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(
                    f"Primeo ({self.tariff_id}): item non parsabile: {e} — {item}"
                )

        if not slots:
            raise AdapterEmptyError(
                f"Primeo ({self.tariff_id}): 0 slot parsabili da {len(raw_prices)} item."
            )

        slots = sorted(slots, key=lambda s: s.slot_start_utc)

        # Log informativo: mostra quale giorno è stato effettivamente restituito
        tz_ch = self._get_tz_ch()
        actual_date = slots[0].slot_start_utc.astimezone(tz_ch).date()
        if actual_date != target_date:
            self._log.info(
                f"Primeo ({self.tariff_id}): API ha restituito {actual_date} "
                f"(richiesto {target_date}) — normale per API day-ahead."
            )

        return slots

    def _extract_prices_list(
        self,
        response_data: dict | list,
    ) -> list:
        """
        Estrae la lista 'prices' dalla risposta.
        Gestisce sia dict (formato normale) che list (formato alternativo).
        """
        if isinstance(response_data, dict):
            if "prices" in response_data:
                return response_data["prices"] or []

            # Risposta validazione / errore API
            if "detail" in response_data:
                raise AdapterEmptyError(
                    f"Primeo ({self.tariff_id}): risposta di errore dall'API — "
                    f"detail={response_data.get('detail')!r}"
                )

            if "status" in response_data or "message" in response_data or "error" in response_data:
                status = response_data.get("status", "")
                message = response_data.get("message", response_data.get("error", ""))
                raise AdapterEmptyError(
                    f"Primeo ({self.tariff_id}): risposta di errore dall'API — "
                    f"status={status!r}, message={message!r}"
                )

            raise AdapterParseError(
                f"Primeo ({self.tariff_id}): chiave 'prices' mancante nella risposta. "
                f"Chiavi presenti: {list(response_data.keys())}"
            )

        elif isinstance(response_data, list):
            return response_data

        else:
            raise AdapterParseError(
                f"Primeo ({self.tariff_id}): tipo risposta inatteso "
                f"{type(response_data).__name__}. Atteso dict o list."
            )

    def _parse_item(self, item: dict) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot dalla risposta Primeo.

        Mapping:
          electricity → energy_price
          grid        → grid_price
          grid_usage  → residual_price
          integrated  → fallback combinato se gli altri mancano
        """
        start_raw = item.get("start_timestamp")
        if not start_raw:
            return None

        start_utc = self.parse_utc_datetime(str(start_raw))

        energy_price   = self._extract_chf(item, "electricity")
        grid_price     = self._extract_chf(item, "grid")
        residual_price = self._extract_chf(item, "grid_usage")
        integrated     = self._extract_chf(item, "integrated")

        # fallback: se mancano energia e rete ma integrated esiste
        if grid_price is None and energy_price is None and integrated is not None:
            grid_price = integrated
            self._log.debug(
                f"Primeo ({self.tariff_id}): slot {start_utc.isoformat()} — "
                "usato 'integrated' come fallback per grid_price"
            )

        if all(v is None for v in (energy_price, grid_price, residual_price)):
            self._log.debug(
                f"Primeo ({self.tariff_id}): slot {start_utc.isoformat()} scartato — "
                "nessun valore di prezzo disponibile"
            )
            return None

        return PriceSlot(
            slot_start_utc=start_utc,
            energy_price=energy_price,
            grid_price=grid_price,
            residual_price=residual_price,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_chf(item: dict, key: str) -> Optional[float]:
        """
        Estrae il valore CHF/kWh da un campo della risposta Primeo.

        Formato atteso:
          [{"unit": "CHF_kWh", "value": 0.0303}]

        Gestisce anche:
          - valori diretti (int/float)
          - liste vuote → None
          - null → None
          - unità in Rappen → conversione a CHF
        """
        raw = item.get(key)

        if raw is None:
            return None

        if isinstance(raw, list):
            if not raw:
                return None
            raw = raw[0]

        if isinstance(raw, dict):
            value = raw.get("value")
            if value is None:
                return None
            unit = str(raw.get("unit", "")).upper()
            price = float(value)

            if "RP" in unit or "RAPPEN" in unit:
                return round(price / 100.0, 8)

            return price

        if isinstance(raw, (int, float)):
            return float(raw)

        return None

    @staticmethod
    def _get_tz_ch():
        """Ritorna il timezone Europe/Zurich."""
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            return pytz.timezone("Europe/Zurich")

    def _preprocess_response(self, raw: bytes) -> dict | list:
        """
        Decodifica la risposta JSON.
        Usa utf-8-sig per gestire eventuali BOM.
        """
        import json

        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1", errors="replace")

        try:
            return json.loads(decoded)
        except json.JSONDecodeError as e:
            preview = decoded[:200].strip()
            raise AdapterParseError(
                f"Primeo ({self.tariff_id}): risposta non è JSON valido — "
                f"JSONDecodeError: {e}. Inizio risposta: {preview!r}"
            )