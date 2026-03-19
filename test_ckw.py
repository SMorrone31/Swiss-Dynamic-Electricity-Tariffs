"""
adapters/ckw.py
===============
Adapter per CKW — legge TUTTA la config da evu_list.json.

Formato risposta reale verificato (2026-03-16):
  Senza tariff_type → tutti i componenti:
    grid, electricity, integrated, grid_usage

  Con tariff_type=grid → solo campo "grid"
  Con tariff_type=integrated → solo campo "integrated"

Strategia: facciamo UNA sola chiamata senza tariff_type
per ottenere tutti i componenti in una volta.

Mapping componenti → PriceSlot:
  electricity → energy_price    (energia pura)
  grid        → grid_price      (rete pura)
  grid_usage  → residual_price  (componente residua SDL+Stromreserve+...)
  integrated  → usato solo come fallback se gli altri mancano
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from adapters.base import AdapterEmptyError, AdapterParseError, BaseAdapter
from schemas import PriceSlot


class CkwAdapter(BaseAdapter):
    """
    Adapter CKW. Legge tutto da evu_list.json — nessuna logica hardcodata.

    Campi usati da evu_list.json:
      api_base_url  → URL endpoint
      api_params    → {tariff_name, default_tariff_type}
      auth_type     → "none"
    """

    def _build_url(self, start_utc: datetime, end_utc: datetime) -> str:
        """
        Costruisce l'URL con il range UTC corrispondente alla giornata locale
        svizzera (CET/CEST), non la giornata UTC pura.

        CKW pubblica per giornata locale: 1 gen 00:00 CET = 31 dic 23:00 UTC.
        Se chiediamo 00:00Z→00:00Z otteniamo slot di due giornate locali → 97 slot.
        Dobbiamo chiedere 23:00Z(ieri)→23:00Z(oggi) in inverno, 22:00Z→22:00Z in estate.
        """
        tariff_name = self._get_tariff_name()

        try:
            import zoneinfo
            tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            tz_ch = pytz.timezone("Europe/Zurich")

        # start_utc è mezzanotte UTC del giorno target
        # Lo convertiamo in ora locale CH e prendiamo mezzanotte locale
        start_local = start_utc.astimezone(tz_ch)
        midnight_local = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_next  = midnight_local + timedelta(days=1)

        # Riconvertiamo in UTC — questo ci dà il range corretto
        start_ch_utc = midnight_local.astimezone(timezone.utc)
        end_ch_utc   = midnight_next.astimezone(timezone.utc)

        params = {
            "tariff_name":     tariff_name,
            "start_timestamp": start_ch_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_timestamp":   end_ch_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        api_params = self.config.get("api_params", {})
        forced_type = api_params.get("force_tariff_type")
        if forced_type:
            params["tariff_type"] = forced_type

        return f"{self.base_url}?{urlencode(params)}"

    def _get_tariff_name(self) -> str:
        """Legge tariff_name da api_params o tariff_name in evu_list.json."""
        # Prima cerca in api_params (campo specifico per i parametri API)
        api_params = self.config.get("api_params", {})
        if "tariff_name" in api_params:
            return api_params["tariff_name"]

        # Poi in tariff_name diretto
        tn = self.config.get("tariff_name", "")
        if tn in ("home_dynamic", "business_dynamic",
                  "CKW Netz Home dynamic", "CKW Netz Business dynamic"):
            if "Business" in tn or "business" in tn:
                return "business_dynamic"
            return "home_dynamic"

        # Fallback dal tariff_id
        return "business_dynamic" if "business" in self.tariff_id else "home_dynamic"

    def _parse(
        self,
        response_data: dict | list,
        target_date: date,
    ) -> list[PriceSlot]:
        """
        Parsa la risposta CKW.

        Struttura risposta:
          {
            "publication_timestamp": "2026-03-15T11:10:05+01:00",
            "prices": [
              {
                "start_timestamp": "2026-03-16T00:00Z",   ← UTC (con Z) o locale (+01:00)
                "end_timestamp":   "2026-03-16T00:15Z",
                "grid":        [{"unit": "CHF_kWh", "value": 0.0383}],
                "electricity": [{"unit": "CHF_kWh", "value": 0.12}],    ← presente se no tariff_type
                "integrated":  [{"unit": "CHF_kWh", "value": 0.1583}],  ← presente se no tariff_type
                "grid_usage":  [{"unit": "CHF_kWh", "value": 0.0093}],  ← presente se no tariff_type
              },
              ...
            ]
          }
        """
        # Estrae la lista prices
        if isinstance(response_data, dict):
            raw_prices = response_data.get("prices")
            if raw_prices is None:
                # CKW risponde con {"status": ..., "body": ...} quando i dati
                # non sono ancora disponibili (prima delle 12:00 ora locale)
                status = response_data.get("status", "")
                body   = response_data.get("body", "")
                if "status" in response_data and "body" in response_data:
                    raise AdapterEmptyError(
                        f"CKW ({self.tariff_id}): prezzi non ancora pubblicati. "
                        f"status={status!r} — riprovare dopo le 11:00 UTC"
                    )
                raise AdapterParseError(
                    f"CKW ({self.tariff_id}): chiave 'prices' mancante. "
                    f"Chiavi: {list(response_data.keys())}"
                )
        elif isinstance(response_data, list):
            raw_prices = response_data
        else:
            raise AdapterParseError(f"CKW: tipo inatteso {type(response_data)}")

        if not raw_prices:
            raise AdapterEmptyError(
                f"CKW ({self.tariff_id}): 'prices' vuota per {target_date}"
            )

        slots: list[PriceSlot] = []

        # Prepara il filtro per ora locale svizzera
        try:
            import zoneinfo
            tz_ch = zoneinfo.ZoneInfo("Europe/Zurich")
        except ImportError:
            import pytz
            tz_ch = pytz.timezone("Europe/Zurich")

        for item in raw_prices:
            try:
                slot = self._parse_item(item)
                if slot is None:
                    continue
                # Filtra: mantieni solo gli slot che appartengono a target_date
                # in ora locale svizzera (CET/CEST).
                # CKW include spesso 1 slot extra al confine del range (es. il
                # primo slot del range UTC che in ora locale è ancora il giorno prima).
                local_date = slot.slot_start_utc.astimezone(tz_ch).date()
                if local_date == target_date:
                    slots.append(slot)
                else:
                    self._log.debug(
                        f"CKW: slot {slot.slot_start_utc} scartato "
                        f"(data locale {local_date} ≠ target {target_date})"
                    )
            except (KeyError, ValueError, TypeError) as e:
                self._log.warning(f"CKW: item non parsabile: {e} — {item}")

        if not slots:
            raise AdapterEmptyError(
                f"CKW ({self.tariff_id}): 0 slot validi da {len(raw_prices)} item"
            )

        return sorted(slots, key=lambda s: s.slot_start_utc)

    def _parse_item(self, item: dict) -> Optional[PriceSlot]:
        """
        Parsa un singolo slot dalla risposta CKW.

        Gestisce sia timestamp UTC (con Z) che ora locale (+01:00 / +02:00).
        Estrae tutti i componenti disponibili.
        """
        start_raw = item.get("start_timestamp")
        if not start_raw:
            return None

        start_utc = self.parse_utc_datetime(str(start_raw))

        # Estrae i quattro componenti possibili
        energy_price   = self._extract_chf(item, "electricity")
        grid_price     = self._extract_chf(item, "grid")
        residual_price = self._extract_chf(item, "grid_usage")
        integrated     = self._extract_chf(item, "integrated")

        # Se non abbiamo né grid né electricity ma abbiamo integrated,
        # lo mettiamo in grid_price come valore complessivo
        if grid_price is None and energy_price is None and integrated is not None:
            grid_price = integrated

        # Se non c'è nulla, lo slot non è utile
        if all(v is None for v in (energy_price, grid_price, residual_price)):
            return None

        return PriceSlot(
            slot_start_utc = start_utc,
            energy_price   = energy_price,
            grid_price     = grid_price,
            residual_price = residual_price,
        )

    @staticmethod
    def _extract_chf(item: dict, key: str) -> Optional[float]:
        """
        Estrae il valore CHF/kWh da un componente CKW.

        Formato: [{"unit": "CHF_kWh", "value": 0.0396}]
        L'unità è sempre CHF_kWh — nessuna conversione necessaria.
        """
        raw = item.get(key)
        if raw is None:
            return None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw is None:
            return None
        if isinstance(raw, dict):
            value = raw.get("value")
            if value is None:
                return None
            unit = str(raw.get("unit", "")).upper()
            price = float(value)
            # Sicurezza: se fosse in Rp lo convertiamo
            if "RP" in unit or "RAPPEN" in unit:
                return price / 100.0
            return price  # CHF_kWh → già CHF
        if isinstance(raw, (int, float)):
            return float(raw)
        return None

    def _preprocess_response(self, raw: bytes) -> dict | list:
        import json
        return json.loads(raw.decode("utf-8-sig"))